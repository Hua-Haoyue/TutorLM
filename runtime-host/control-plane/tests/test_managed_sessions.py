import pytest

from control_plane.api.routers import app_session
from control_plane.security import app_bootstrap_token
from control_plane.config import ControlPlaneConfig
from control_plane.invitations import AccessPolicy
from control_plane.errors import InvitationRequired


def test_bootstrap_rejects_missing_lease(client, monkeypatch):
    monkeypatch.setattr(app_session, 'issue_at_instance', lambda *args: pytest.fail('Untrusted call'))
    assert client.post('/control/app-session').status_code == 401


def test_bootstrap_uses_current_lease_and_never_exposes_internal_secret(client, monkeypatch):
    value = client.post('/control/leases', json={}).json()
    state = client.app.state
    lease = state.store.require_lease(value['lease']['lease_id'])
    secret = app_bootstrap_token(state.config, state.store, lease)
    spec = state.adapter._containers[lease.upstream.split('//')[1].split(':')[0]].spec
    assert spec.env['TUTORLM_APP_BOOTSTRAP_TOKEN'] == secret
    assert spec.env['TUTORLM_SESSION_DATA_DIR'].startswith('/tmp/')
    calls = []
    def issue(upstream, token):
        calls.append((upstream, token))
        return {'access_token': 'app-token', 'session': {'workspace_id': 'local', 'id': 'session'}}
    monkeypatch.setattr(app_session, 'issue_at_instance', issue)
    response = client.post('/control/app-session', json={'upstream': 'https://untrusted.test'})
    assert response.status_code == 201
    assert response.headers['cache-control'] == 'no-store'
    assert calls == [(lease.upstream, secret)]
    assert secret not in response.text
    assert secret not in client.get('/control/admin/config', headers={'X-Admin-Token': state.config.admin_token}).text


def test_revoked_or_changed_lease_discards_issued_session(client, monkeypatch):
    lease = client.post('/control/leases', json={}).json()['lease']
    def issue(*args):
        client.app.state.allocator.release(lease['lease_id'])
        return {'access_token': 'discard', 'session': {'workspace_id': 'local'}}
    monkeypatch.setattr(app_session, 'issue_at_instance', issue)
    response = client.post('/control/app-session')
    assert response.status_code in (401, 410)
    assert 'discard' not in response.text


def test_wrong_workspace_session_is_rejected(client, monkeypatch):
    client.post('/control/leases', json={})
    monkeypatch.setattr(app_session, 'issue_at_instance', lambda *args: {
        'access_token': 'discard', 'session': {'workspace_id': 'foreign'},
    })
    response = client.post('/control/app-session')
    assert response.status_code == 503
    assert 'discard' not in response.text


def test_invalid_upstream_is_not_contacted(client, monkeypatch):
    lease = client.post('/control/leases', json={}).json()['lease']
    with client.app.state.store.transaction() as conn:
        conn.execute('UPDATE leases SET upstream=? WHERE lease_id=?', ('http://untrusted.test', lease['lease_id']))
    monkeypatch.setattr(app_session, 'issue_at_instance', lambda *args: pytest.fail('Untrusted call'))
    assert client.post('/control/app-session').status_code != 201


def test_historical_trial_grants_cannot_bypass_invitation_only(client):
    client.post('/control/users')
    workspace = client.post('/control/workspaces', json={'title': 'Persisted copy'}).json()['workspace']
    state = client.app.state
    state.store.grant_trial(workspace['workspace_id'], workspace['user_id'], source='share_import', invitation_id=None)
    state.invitations.policy = AccessPolicy(invitation_only=True)
    with pytest.raises(InvitationRequired):
        state.invitations.admit(None, workspace_id=workspace['workspace_id'], user_id=workspace['user_id'])
    assert client.post('/control/leases', json={'workspace_id': workspace['workspace_id']}).status_code == 403


@pytest.mark.parametrize('text', ['inviteonly=true', '[control_plane]\ninviteonly=true'])
def test_inviteonly_config_alias(tmp_path, text):
    path = tmp_path / 'config.toml'
    path.write_text(text)
    assert ControlPlaneConfig.from_env({}, config_path=path).invitation_only
