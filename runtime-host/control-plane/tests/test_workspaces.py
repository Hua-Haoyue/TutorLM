"""Durable projects, identity recovery, compute reuse and storage retention."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading

from fastapi.testclient import TestClient
import pytest

from control_plane.api.app import build_components, create_app
from control_plane.config import ControlPlaneConfig
from control_plane.errors import SlotBusy, WorkspaceExpired
from control_plane.ports import ReadinessResult
from control_plane.runtime.fake import FakeRuntimeAdapter
from control_plane.store import SCHEMA_VERSION, ControlPlaneStore


@pytest.fixture()
def system(tmp_path):
    now = [1_800_000_000.0]
    config = ControlPlaneConfig.for_tests(tmp_path, lease_ttl_seconds=60)
    adapter = FakeRuntimeAdapter()
    app = create_app(config, adapter=adapter, clock=lambda: now[0], run_startup_tasks=False)
    with TestClient(app) as client:
        yield client, app.state, now


def register(client):
    response = client.post('/control/users')
    assert response.status_code == 201, response.text
    assert response.headers['cache-control'] == 'no-store'
    cookies = response.headers.get_list('set-cookie')
    assert any('httponly' in cookie.lower() and 'path=/control' in cookie.lower() for cookie in cookies)
    return response.json()


def create(client, title='Project'):
    response = client.post('/control/workspaces', json={'title': title})
    assert response.status_code == 201, response.text
    return response.json()['workspace']


def open_workspace(client, workspace):
    return client.post('/control/leases', json={'workspace_id': workspace['workspace_id']})


def login(client, credentials):
    return client.post('/control/users/session', json={
        'user_id': credentials['user']['user_id'], 'recovery_token': credentials['recovery_token'],
    })


def test_identity_and_multiple_projects_can_be_recovered(system):
    client, state, now = system
    credentials = register(client)
    first, second = create(client, 'First'), create(client, 'Second')
    assert first['workspace_id'] != second['workspace_id']
    stored = state.store.get_user(credentials['user']['user_id'])
    assert credentials['recovery_token'] not in repr(stored)
    client.cookies.clear()
    assert client.get('/control/workspaces').status_code == 401
    assert login(client, credentials).status_code == 200
    projects = client.get('/control/workspaces').json()['items']
    assert {w['title'] for w in projects} == {'First', 'Second'}
    assert all(w['last_used_at'] == now[0] for w in projects)
    assert all('volume_name' not in w for w in projects)


def test_uuid_alone_does_not_authorize_projects_or_compute(system):
    client, state, _ = system
    credentials = register(client)
    workspace = create(client)
    client.cookies.clear()
    headers = {'X-User-ID': credentials['user']['user_id']}
    assert client.get('/control/workspaces', headers=headers).status_code == 401
    assert open_workspace(client, workspace).status_code == 401
    assert state.store.list_live_leases() == []
    assert login(client, {**credentials, 'recovery_token': 'wrong'}).status_code == 401


def test_user_headers_are_supported_without_reusing_lease_bearer(system):
    client, _, _ = system
    credentials = register(client)
    client.cookies.clear()
    headers = {'X-User-ID': credentials['user']['user_id'], 'X-User-Token': credentials['recovery_token']}
    assert client.get('/control/users/me', headers=headers).status_code == 200
    response = client.get('/control/users/me', headers={'Authorization': f"Bearer {credentials['recovery_token']}"})
    assert response.status_code == 401


def test_other_user_cannot_list_open_or_touch_a_workspace(system):
    client, state, _ = system
    register(client)
    workspace = create(client)
    client.cookies.clear()
    register(client)
    assert client.get('/control/workspaces').json()['items'] == []
    path = f"/control/workspaces/{workspace['workspace_id']}"
    assert client.get(path).status_code == 404
    assert client.post(path + '/touch').status_code == 404
    assert open_workspace(client, workspace).status_code == 404
    assert state.store.list_live_leases() == []


def test_compute_release_retains_data_and_can_move_to_another_slot(system):
    client, state, _ = system
    register(client)
    workspace = create(client)
    first = open_workspace(client, workspace).json()
    stored = state.store.require_workspace(workspace['workspace_id'])
    volume = stored.volume_name
    state.adapter.write_volume(volume, 'document.txt', b'Persisted project contents')
    resumed = open_workspace(client, workspace).json()
    assert resumed['resumed'] is True
    assert resumed['lease']['lease_id'] == first['lease']['lease_id']
    assert client.delete('/control/leases/current').status_code == 202
    assert state.adapter.snapshot()['containers'] == {}
    assert state.adapter.volume_exists(volume)
    # Occupy the original slot with an unrelated ephemeral visitor.
    other = TestClient(client.app)
    try:
        occupied = other.post('/control/leases', json={}).json()
        reopened = open_workspace(client, workspace).json()
        assert reopened['lease']['slot_id'] != first['lease']['slot_id']
        assert reopened['lease']['lease_id'] != first['lease']['lease_id']
        assert reopened['lease']['workspace_id'] == workspace['workspace_id']
        assert state.adapter.read_volume(volume, 'document.txt') == b'Persisted project contents'
        assert state.adapter.snapshot()['containers'][
            state.config.container_name(reopened['lease']['slot_id'], reopened['lease']['generation'])
        ]['volume'] == volume
        assert occupied['lease']['slot_id'] == first['lease']['slot_id']
    finally:
        other.close()


def test_one_browser_must_release_compute_before_switching_project(system):
    client, state, _ = system
    register(client)
    first, second = create(client), create(client)
    assert open_workspace(client, first).status_code == 201
    assert open_workspace(client, second).status_code == 409
    assert len(state.store.list_live_leases()) == 1


def test_compute_expiration_does_not_delete_project_storage(system):
    client, state, now = system
    register(client)
    workspace = create(client)
    issued = open_workspace(client, workspace).json()
    volume = state.store.require_workspace(workspace['workspace_id']).volume_name
    now[0] += 61
    report = state.allocator.gc_once()
    assert issued['lease']['lease_id'] in report['reclaimed']
    assert state.adapter.snapshot()['containers'] == {}
    assert state.adapter.volume_exists(volume)
    assert client.get('/control/admission').status_code == 401
    assert open_workspace(client, workspace).status_code == 201


def test_exact_four_hours_keeps_data_then_gc_deletes_after_threshold(system):
    client, state, now = system
    register(client)
    workspace = create(client)
    open_workspace(client, workspace)
    client.delete('/control/leases/current')
    volume = state.store.require_workspace(workspace['workspace_id']).volume_name
    now[0] += 14400
    assert state.allocator.gc_once()['workspaces_deleted'] == []
    assert state.adapter.volume_exists(volume)
    now[0] += 1
    assert state.allocator.gc_once()['workspaces_deleted'] == [workspace['workspace_id']]
    assert not state.adapter.volume_exists(volume)
    assert state.store.require_workspace(workspace['workspace_id']).title == ''
    assert client.get('/control/workspaces').json()['items'] == []
    assert open_workspace(client, workspace).status_code == 410


def test_activity_is_per_project_and_listing_does_not_refresh_retention(system):
    client, state, now = system
    register(client)
    first, second = create(client, 'Active'), create(client, 'Idle')
    now[0] += 3 * 3600
    listing = client.get('/control/workspaces').json()['items']
    assert all(w['last_used_at'] == first['last_used_at'] for w in listing)
    assert client.post(f"/control/workspaces/{first['workspace_id']}/touch").status_code == 200
    now[0] += 3601
    report = state.allocator.gc_once()
    assert report['workspaces_deleted'] == [second['workspace_id']]
    assert [w['title'] for w in client.get('/control/workspaces').json()['items']] == ['Active']


def test_admission_refreshes_only_its_workspace(system):
    client, state, now = system
    register(client)
    first, second = create(client), create(client)
    open_workspace(client, first)
    now[0] += 20
    assert client.get('/control/admission').status_code == 204
    assert state.store.require_workspace(first['workspace_id']).last_used_at == now[0]
    assert state.store.require_workspace(second['workspace_id']).last_used_at == now[0] - 20


def test_storage_gc_never_deletes_a_mounted_workspace(system):
    client, state, now = system
    register(client)
    workspace = create(client)
    open_workspace(client, workspace)
    volume = state.store.require_workspace(workspace['workspace_id']).volume_name
    now[0] += 14401
    assert state.workspaces.gc_once()['deleted'] == []
    assert state.adapter.volume_exists(volume)
    # The combined GC releases expired compute before deleting expired storage.
    report = state.allocator.gc_once()
    assert report['workspaces_deleted'] == [workspace['workspace_id']]
    assert not state.adapter.volume_exists(volume)


def test_storage_cleanup_failure_is_retried_and_cannot_be_reopened(system):
    client, state, now = system
    register(client)
    workspace = create(client)
    open_workspace(client, workspace)
    client.delete('/control/leases/current')
    volume = state.store.require_workspace(workspace['workspace_id']).volume_name
    state.adapter.fail_remove_volume.add(volume)
    now[0] += 14401
    assert state.allocator.gc_once()['workspaces_failed'] == [workspace['workspace_id']]
    assert state.adapter.volume_exists(volume)
    assert open_workspace(client, workspace).status_code == 410
    state.adapter.fail_remove_volume.clear()
    assert state.allocator.gc_once()['workspaces_deleted'] == [workspace['workspace_id']]


def test_failed_stop_pins_storage_even_after_lease_is_terminal(system):
    client, state, now = system
    register(client)
    workspace = create(client)
    issued = open_workspace(client, workspace).json()['lease']
    container = state.config.container_name(issued['slot_id'], issued['generation'])
    volume = state.store.require_workspace(workspace['workspace_id']).volume_name
    state.adapter.fail_stop.add(container)
    client.delete('/control/leases/current')
    now[0] += 14401
    assert state.workspaces.gc_once()['deleted'] == []
    assert state.adapter.volume_exists(volume)
    state.adapter.fail_stop.clear()
    state.allocator.release_quarantine(issued['slot_id'], reason='test')
    assert state.workspaces.gc_once()['deleted'] == [workspace['workspace_id']]


def test_expired_workspace_claim_cannot_race_with_reopen(system):
    client, state, now = system
    credentials = register(client)
    workspace = create(client)
    now[0] += 14401
    assert len(state.store.claim_expired_workspaces(14400)) == 1
    with pytest.raises(WorkspaceExpired):
        state.allocator.allocate(workspace_id=workspace['workspace_id'], user_id=credentials['user']['user_id'])
    assert state.workspaces.gc_once()['deleted'] == [workspace['workspace_id']]


def test_concurrent_project_open_is_exclusive_across_store_connections(system):
    client, state, now = system
    credentials = register(client)
    workspace = create(client)
    second = build_components(state.config, adapter=state.adapter, clock=lambda: now[0])
    barrier = threading.Barrier(2)

    def open_from(allocator):
        barrier.wait()
        try:
            allocator.allocate(workspace_id=workspace['workspace_id'], user_id=credentials['user']['user_id'])
            return 'created'
        except SlotBusy:
            return 'busy'

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(open_from, [state.allocator, second['allocator']]))
        assert sorted(results) == ['busy', 'created']
        assert len(state.store.list_live_leases()) == 1
    finally:
        second['store'].close()


def test_missing_persistent_volume_is_not_silently_replaced(system):
    client, state, _ = system
    register(client)
    workspace = create(client)
    open_workspace(client, workspace)
    client.delete('/control/leases/current')
    volume = state.store.require_workspace(workspace['workspace_id']).volume_name
    state.adapter.remove_volume(volume)
    assert open_workspace(client, workspace).status_code == 503
    assert not state.adapter.volume_exists(volume)


def test_application_must_confirm_persistent_workspace_readiness(system, monkeypatch):
    client, state, _ = system
    register(client)
    workspace = create(client)
    monkeypatch.setattr(state.adapter, 'probe', lambda upstream: ReadinessResult(ready=True, payload={'status': 'ok'}))
    assert open_workspace(client, workspace).status_code == 503
    assert state.store.list_slots()[0].state == 'quarantined'
    assert state.adapter.volume_exists(state.store.require_workspace(workspace['workspace_id']).volume_name)


def test_logout_releases_compute_without_deleting_project(system):
    client, state, _ = system
    credentials = register(client)
    workspace = create(client)
    open_workspace(client, workspace)
    assert client.delete('/control/users/session').status_code == 204
    assert client.get('/control/users/me').status_code == 401
    assert state.adapter.snapshot()['containers'] == {}
    assert state.adapter.volume_exists(state.store.require_workspace(workspace['workspace_id']).volume_name)
    assert login(client, credentials).status_code == 200
    assert client.get('/control/workspaces').json()['items'][0]['workspace_id'] == workspace['workspace_id']


def test_projects_credentials_and_volume_survive_control_plane_restart(tmp_path):
    config = ControlPlaneConfig.for_tests(tmp_path)
    adapter = FakeRuntimeAdapter()
    with TestClient(create_app(config, adapter=adapter, run_startup_tasks=False)) as first:
        credentials = register(first)
        workspace = create(first)
        open_workspace(first, workspace)
        volume = first.app.state.store.require_workspace(workspace['workspace_id']).volume_name
        adapter.write_volume(volume, 'notes.md', b'Notes')
        first.delete('/control/leases/current')
    with TestClient(create_app(config, adapter=adapter, run_startup_tasks=False)) as second:
        assert login(second, credentials).status_code == 200
        assert second.get('/control/workspaces').json()['items'][0]['workspace_id'] == workspace['workspace_id']
        assert open_workspace(second, workspace).status_code == 201
        assert adapter.read_volume(volume, 'notes.md') == b'Notes'


def test_schema_upgrade_preserves_existing_ephemeral_leases(tmp_path):
    config = ControlPlaneConfig.for_tests(tmp_path)
    components = build_components(config)
    issued = components['allocator'].allocate()
    components['store'].close()
    with sqlite3.connect(config.db_path) as legacy:
        legacy.execute('DROP INDEX idx_leases_workspace')
        legacy.execute('ALTER TABLE leases DROP COLUMN workspace_id')
        legacy.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
    upgraded = ControlPlaneStore(config.db_path)
    try:
        assert upgraded.get_meta('schema_version') == str(SCHEMA_VERSION)
        assert upgraded.require_lease(issued.lease.lease_id).workspace_id is None
        assert upgraded.require_slot(issued.lease.slot_id).lease_id == issued.lease.lease_id
    finally:
        upgraded.close()


def test_generated_identity_pepper_survives_restart(tmp_path):
    config = ControlPlaneConfig.for_tests(tmp_path, token_pepper='')
    with TestClient(create_app(config, run_startup_tasks=False)) as first:
        credentials = register(first)
    with TestClient(create_app(config, run_startup_tasks=False)) as second:
        assert login(second, credentials).status_code == 200


def test_gateway_does_not_forward_long_lived_user_credentials():
    from pathlib import Path
    root = Path(__file__).resolve().parents[3]
    source = (root / 'runtime-host' / 'gateway' / 'nginx.conf').read_text()
    application_block = source.split('auth_request /_lease_auth;', 1)[1]
    assert "proxy_set_header Cookie '';" in application_block
    assert "proxy_set_header X-User-ID '';" in application_block
    assert "proxy_set_header X-User-Token '';" in application_block


def test_workspace_volume_cannot_point_to_another_project(system):
    client, state, now = system
    register(client)
    first, second = create(client), create(client)
    assert open_workspace(client, first).status_code == 201
    client.delete('/control/leases/current')
    volume = state.store.require_workspace(first['workspace_id']).volume_name
    with state.store.transaction() as conn:
        conn.execute('UPDATE workspaces SET volume_name=? WHERE workspace_id=?', (volume + '-corrupt', second['workspace_id']))
    assert open_workspace(client, second).status_code == 503
    assert state.adapter.volume_exists(volume)
    now[0] += 10800
    state.workspaces.get(first['workspace_id'], first['user_id'], touch=True)
    now[0] += 3601
    # Release quarantine to remove only second's compute reference; it must not delete first's volume.
    slot = next(slot for slot in state.store.list_slots() if slot.state == 'quarantined')
    state.allocator.release_quarantine(slot.slot_id, reason='test')
    state.workspaces.gc_once()
    assert state.store.require_workspace(second['workspace_id']).state == 'deleting'
    assert state.adapter.volume_exists(volume)


def test_persistent_data_dir_matches_the_actual_volume_mount(system):
    client, state, _ = system
    register(client)
    workspace = create(client)
    issued = open_workspace(client, workspace).json()['lease']
    slot = state.store.require_slot(issued['slot_id'])
    spec = state.lifecycle._build_spec(slot, issued['generation'])
    assert spec.env['TUTORLM_DATA_DIR'] == spec.mount_path
    assert spec.env['TUTORLM_WORKSPACE_ID'] == workspace['workspace_id']
    assert spec.volume_must_exist is True


def test_stale_lifecycle_operations_cannot_touch_a_reallocated_slot(system):
    client, state, _ = system
    register(client)
    workspace = create(client)
    first = open_workspace(client, workspace).json()['lease']
    old_slot = state.store.require_slot(first['slot_id'])
    client.delete('/control/leases/current')
    replacement = open_workspace(client, workspace).json()['lease']
    assert replacement['slot_id'] == first['slot_id']
    assert state.lifecycle.provision(old_slot).ok is False
    with pytest.raises(SlotBusy):
        state.lifecycle.reclaim(first['slot_id'], expected_lease_id=first['lease_id'])
    container = state.config.container_name(replacement['slot_id'], replacement['generation'])
    assert state.adapter.status(container).running is True


def test_switching_user_identity_drops_the_previous_project_lease(system):
    client, state, _ = system
    original = register(client)
    workspace = create(client)
    issued = open_workspace(client, workspace).json()['lease']
    other = TestClient(client.app)
    try:
        next_user = register(other)
    finally:
        other.close()
    assert login(client, next_user).status_code == 200
    assert client.get('/control/admission').status_code == 401
    assert client.get('/control/workspaces').json()['items'] == []
    assert state.store.require_lease(issued['lease_id']).state == 'reclaimed'
    assert state.adapter.volume_exists(state.store.require_workspace(workspace['workspace_id']).volume_name)
    assert login(client, original).status_code == 200
    assert client.get('/control/workspaces').json()['items'][0]['workspace_id'] == workspace['workspace_id']
