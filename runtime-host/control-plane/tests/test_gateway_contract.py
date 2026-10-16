"""Regression coverage for lease cookies and gateway admission."""
from __future__ import annotations

import time

from fastapi.testclient import TestClient

from control_plane.api.app import create_app
from control_plane.domain.models import LeaseState


def test_cookie_is_httponly_and_repeated_requests_resume(client):
    issued = client.post('/control/leases', json={})
    assert issued.status_code == 201
    cookie = issued.headers['set-cookie'].lower()
    assert 'httponly' in cookie
    assert 'path=/' in cookie
    assert 'samesite=lax' in cookie
    resumed = client.post('/control/leases', json={}).json()
    assert resumed['resumed'] is True
    assert resumed['lease']['lease_id'] == issued.json()['lease']['lease_id']
    assert client.get('/control/capacity').json()['slots_free'] == 1


def test_cookie_takes_precedence_over_application_bearer(client):
    issued = client.post('/control/leases', json={}).json()
    response = client.get('/control/admission', headers={'Authorization': 'Bearer app-session-token'})
    assert response.status_code == 204
    assert response.headers['x-tutorlm-lease'] == issued['lease']['lease_id']


def test_custom_cookie_name_and_bearer_fallback(config, adapter):
    custom = config.with_overrides(lease_cookie_name='developer_pool_lease')
    app = create_app(custom, adapter=adapter, run_startup_tasks=False)
    with TestClient(app) as client:
        issued = client.post('/control/leases', json={})
        assert 'developer_pool_lease=' in issued.headers['set-cookie']
        token = issued.json()['token']
        assert client.get('/control/admission').status_code == 204
        client.cookies.clear()
        assert client.get('/control/admission').status_code == 401
        headers = {'Authorization': f'Bearer {token}'}
        assert client.get('/control/admission', headers=headers).status_code == 204


def test_admission_only_returns_server_controlled_target(client, config):
    issued = client.post('/control/leases', json={}).json()
    response = client.get('/control/admission', headers={
        'X-Tutorlm-Slot': '99', 'X-Tutorlm-Upstream': 'http://untrusted.example',
    })
    lease = issued['lease']
    expected = f"http://{config.container_name(lease['slot_id'], lease['generation'])}:{config.instance_port}"
    assert response.status_code == 204
    assert response.headers['x-tutorlm-upstream'] == expected
    assert response.headers['x-tutorlm-generation'] == str(lease['generation'])
    assert 'upstream' not in lease
    assert 'token_hash' not in lease


def test_corrupted_upstream_is_rejected(client):
    lease = client.post('/control/leases', json={}).json()['lease']
    client.app.state.store.update_lease(lease['lease_id'], upstream='http://untrusted.example')
    response = client.get('/control/admission')
    assert response.status_code == 500
    assert response.json()['error']['code'] == 'untrusted_upstream'
    assert 'x-tutorlm-upstream' not in response.headers


def test_expiration_is_enforced_without_waiting_for_gc(client):
    lease = client.post('/control/leases', json={}).json()['lease']
    client.app.state.store.update_lease(lease['lease_id'], expires_at=time.time() - 1)
    assert client.get('/control/leases/current').status_code == 401
    assert client.get('/control/admission').status_code == 401


def test_pending_lease_is_denied_at_admission(client):
    lease = client.post('/control/leases', json={}).json()['lease']
    client.app.state.store.update_lease(lease['lease_id'], state=LeaseState.PENDING)
    assert client.get('/control/admission').status_code == 401


def test_release_clears_cookie_and_old_bearer_is_denied(client):
    issued = client.post('/control/leases', json={}).json()
    response = client.delete('/control/leases/current')
    assert response.status_code == 202
    assert 'max-age=0' in response.headers['set-cookie'].lower()
    headers = {'Authorization': f"Bearer {issued['token']}"}
    assert client.get('/control/admission', headers=headers).status_code == 401
