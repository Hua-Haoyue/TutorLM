"""Page close, document reload and absolute compute expiry are independent of storage."""
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from control_plane.api.app import create_app
from control_plane.config import ControlPlaneConfig
from control_plane.runtime.fake import FakeRuntimeAdapter


@pytest.fixture()
def browser(tmp_path):
    now = [1800000000.0]
    config = ControlPlaneConfig.for_tests(tmp_path, lease_ttl_seconds=3600)
    app = create_app(config, adapter=FakeRuntimeAdapter(), clock=lambda: now[0], run_startup_tasks=False)
    with TestClient(app) as client:
        client.post('/control/users')
        workspace = client.post('/control/workspaces', json={'title': 'Saved project'}).json()['workspace']
        issued = client.post('/control/leases', json={'workspace_id': workspace['workspace_id']}).json()
        lease = issued['lease']
        yield client, app.state, now, workspace, lease


def attach(client, lease, page=None):
    page = page or str(uuid4())
    base = f"/control/leases/{lease['lease_id']}/pages"
    response = client.post(base, json={'page_id': page})
    assert response.status_code == 201, response.text
    return base + '/' + page


def test_last_page_close_reclaims_compute_but_keeps_project(browser):
    client, state, now, workspace, lease = browser
    path = attach(client, lease)
    volume = state.store.require_workspace(workspace['workspace_id']).volume_name
    assert client.post(path + '/close').status_code == 202
    now[0] += 4
    assert state.allocator.gc_once()['reclaimed'] == []
    now[0] += 1
    assert state.allocator.gc_once()['reclaimed'] == [lease['lease_id']]
    assert state.adapter.snapshot()['containers'] == {}
    assert state.adapter.volume_exists(volume)
    assert client.get('/control/admission').status_code == 401
    assert client.get('/control/workspaces').json()['items'][0]['workspace_id'] == workspace['workspace_id']


def test_live_second_tab_keeps_compute_until_the_last_tab_closes(browser):
    client, state, now, _, lease = browser
    first, second = attach(client, lease), attach(client, lease)
    client.post(first + '/close')
    now[0] += 6
    assert state.allocator.gc_once()['reclaimed'] == []
    client.post(second + '/close')
    now[0] += 5
    assert state.allocator.gc_once()['reclaimed'] == [lease['lease_id']]


def test_reload_registers_new_document_before_close_grace_expires(browser):
    client, state, now, _, lease = browser
    previous = attach(client, lease)
    client.post(previous + '/close')
    now[0] += 2
    replacement = attach(client, lease)
    now[0] += 4
    assert state.allocator.gc_once()['reclaimed'] == []
    assert client.post(replacement + '/heartbeat').status_code == 200
    assert client.post(previous + '/heartbeat').status_code == 404


def test_missing_close_notification_is_reclaimed_by_presence_timeout(browser):
    client, state, now, workspace, lease = browser
    attach(client, lease)
    now[0] += 119
    assert state.allocator.gc_once()['reclaimed'] == []
    now[0] += 1
    assert state.allocator.gc_once()['reclaimed'] == [lease['lease_id']]
    assert state.store.require_workspace(workspace['workspace_id']).state == 'active'


def test_heartbeat_maintains_presence_not_compute_or_storage_ttl(browser):
    client, state, now, workspace, lease = browser
    path = attach(client, lease)
    used = state.store.require_workspace(workspace['workspace_id']).last_used_at
    now[0] += 110
    assert client.post(path + '/heartbeat').status_code == 200
    now[0] += 11
    assert state.allocator.gc_once()['reclaimed'] == []
    assert state.store.require_lease(lease['lease_id']).expires_at == lease['expires_at']
    assert state.store.require_workspace(workspace['workspace_id']).last_used_at == used
    now[0] = lease['expires_at']
    assert client.post(path + '/heartbeat').status_code == 401
    assert state.allocator.gc_once()['reclaimed'] == [lease['lease_id']]


def test_close_tombstone_wins_against_late_attach_and_heartbeat(browser):
    client, state, now, _, lease = browser
    page = str(uuid4())
    base = f"/control/leases/{lease['lease_id']}/pages"
    path = base + '/' + page
    assert client.post(path + '/close').status_code == 202
    assert client.post(base, json={'page_id': page}).status_code == 409
    assert client.post(path + '/heartbeat').status_code == 404
    now[0] += 3
    assert client.post(path + '/close').status_code == 202
    now[0] += 2
    assert state.allocator.gc_once()['reclaimed'] == [lease['lease_id']]


def test_old_page_close_never_releases_a_new_lease_or_clears_its_cookie(browser):
    client, state, now, workspace, lease = browser
    old = attach(client, lease)
    client.delete('/control/leases/current')
    replacement = client.post('/control/leases', json={'workspace_id': workspace['workspace_id']}).json()['lease']
    response = client.post(old + '/close')
    assert response.status_code == 401
    assert 'set-cookie' not in response.headers
    assert client.get('/control/leases/current').json()['lease']['lease_id'] == replacement['lease_id']
    assert state.store.require_lease(replacement['lease_id']).state == 'active'


def test_heartbeat_cannot_attach_to_another_visitors_lease(browser):
    client, state, now, _, lease = browser
    path = attach(client, lease)
    other = TestClient(client.app)
    try:
        other.post('/control/leases', json={})
        assert other.post(path + '/heartbeat').status_code == 401
        assert other.post(path + '/close').status_code == 401
    finally:
        other.close()
    assert state.store.require_lease(lease['lease_id']).state == 'active'


def test_gc_rechecks_presence_in_the_same_transaction_as_expiration(browser, monkeypatch):
    client, state, now, _, lease = browser
    path = attach(client, lease)
    now[0] += 120
    original = state.store.expire_disconnected_pages

    def with_arriving_heartbeat(*args):
        assert client.post(path + '/heartbeat').status_code == 200
        return original(*args)

    monkeypatch.setattr(state.store, 'expire_disconnected_pages', with_arriving_heartbeat)
    assert state.allocator.gc_once()['reclaimed'] == []
    assert state.store.require_lease(lease['lease_id']).state == 'active'


def test_non_browser_clients_keep_absolute_ttl_compatibility(browser):
    client, state, now, _, lease = browser
    now[0] += 121
    assert state.allocator.gc_once()['reclaimed'] == []
    now[0] = lease['expires_at']
    assert state.allocator.gc_once()['reclaimed'] == [lease['lease_id']]
