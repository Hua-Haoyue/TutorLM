"""HTTP 层：状态码、鉴权、错误信封、管理接口。"""

from __future__ import annotations

from fastapi.testclient import TestClient

import pytest

from tests.conftest import ADMIN_HEADERS, auth


# ------------------------------------------------------------------ health
def test_liveness_and_readiness(client: TestClient):
    assert client.get("/health").json()["status"] == "ok"
    ready = client.get("/health/ready")
    assert ready.status_code == 200
    assert ready.json()["status"] == "ok"


def test_readiness_degrades_without_admin_token(config, adapter):
    from control_plane.api.app import create_app
    from control_plane.config import ControlPlaneConfig

    bare = ControlPlaneConfig.for_tests(config.db_path.parent, admin_token="")
    app = create_app(bare, adapter=adapter, run_startup_tasks=False)
    with TestClient(app) as c:
        body = c.get("/health/ready").json()
        assert body["status"] == "degraded"
        assert any("管理令牌" in reason for reason in body["degraded_reasons"])


def test_capacity_reports_pool_state(client: TestClient):
    body = client.get("/control/capacity").json()
    assert body["slots_total"] == 2
    assert body["slots_free"] == 2
    assert body["slots_ready"] == 2
    assert body["slots_starting"] == 0
    assert body["leases_active"] == 0
    assert body["accepting"] is True


def test_control_health_alias_exists(client: TestClient):
    body = client.get("/control/health").json()
    assert body["status"] == "ok"


# ------------------------------------------------------------------ leases
def test_create_lease_returns_token_once_and_never_slot_control(client: TestClient):
    response = client.post("/control/leases", json={"label": "alice"})
    assert response.status_code == 201
    body = response.json()
    assert body["token"]
    assert body["token_type"] == "Bearer"
    assert body["lease"]["state"] == "active"
    assert body["lease"]["label"] == "alice"
    # 响应里不能出现 upstream/内网地址，避免泄漏内部拓扑
    assert "upstream" not in body["lease"]
    assert "token_hash" not in body["lease"]


def test_client_cannot_choose_slot_or_upstream(client: TestClient):
    """传入的 slot_id/upstream 必须被忽略，不能影响分配结果。"""

    response = client.post(
        "/control/leases",
        json={"label": "x", "slot_id": 99, "upstream": "http://evil:8000"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["lease"]["slot_id"] in (0, 1)
    assert "upstream" not in body["lease"]


def test_repeat_allocation_with_token_resumes_same_lease(client: TestClient):
    """T1.2：同一浏览器重复申请必须复用租约，不额外占槽位。"""

    first = client.post("/control/leases", json={"label": "a"}).json()
    assert first["resumed"] is False

    again = client.post(
        "/control/leases", json={}, headers=auth(first["token"])
    )
    assert again.status_code == 201
    body = again.json()
    assert body["resumed"] is True
    assert body["lease"]["lease_id"] == first["lease"]["lease_id"]
    # 只占了一个槽位
    assert client.get("/control/capacity").json()["slots_free"] == 1


def test_repeat_allocation_with_revoked_token_gets_new_lease(client: TestClient):
    """已释放的凭据不能复活旧租约，必须正常分配一个新的。"""

    first = client.post("/control/leases", json={}).json()
    client.delete("/control/leases/current", headers=auth(first["token"]))
    response = client.post(
        "/control/leases", json={}, headers=auth(first["token"])
    )
    assert response.status_code == 201
    body = response.json()
    assert body["resumed"] is False
    assert body["lease"]["lease_id"] != first["lease"]["lease_id"]


def test_current_lease_requires_valid_bearer(client: TestClient):
    assert client.get("/control/leases/current").status_code == 401
    assert (
        client.get(
            "/control/leases/current", headers={"Authorization": "Bearer junk"}
        ).status_code
        == 401
    )


def test_current_lease_round_trip(client: TestClient, lease: dict):
    response = client.get(
        "/control/leases/current", headers=auth(lease["token"])
    )
    assert response.status_code == 200
    body = response.json()
    assert body["lease"]["lease_id"] == lease["lease"]["lease_id"]
    assert body["instance_url"] == "/"


def test_release_then_token_is_dead(client: TestClient, lease: dict):
    response = client.delete(
        "/control/leases/current", headers=auth(lease["token"])
    )
    assert response.status_code == 202
    assert response.json()["state"] == "reclaimed"
    # 旧令牌必须立刻失效
    assert (
        client.get(
            "/control/leases/current", headers=auth(lease["token"])
        ).status_code
        == 401
    )
    # 槽位回到空闲，容量恢复
    assert client.get("/control/capacity").json()["slots_free"] == 2


def test_capacity_exhaustion_returns_503_with_retry_after(client: TestClient, visitor_factory):
    assert client.post("/control/leases", json={}).status_code == 201
    assert visitor_factory().post("/control/leases", json={}).status_code == 201
    response = visitor_factory().post("/control/leases", json={})
    assert response.status_code == 503
    assert response.headers.get("Retry-After") == "5"
    assert response.json()["error"]["code"] == "capacity_exhausted"


def test_start_failure_returns_503_and_quarantines(client: TestClient):
    client.fake_adapter.fail_start.add("tutorlm-instance-s00-g0000")
    response = client.post("/control/leases", json={})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "runtime_unavailable"
    slots = client.get("/control/admin/slots", headers=ADMIN_HEADERS).json()["items"]
    assert any(slot["state"] == "quarantined" for slot in slots)


def test_second_lease_uses_other_slot(client: TestClient, visitor_factory):
    first = client.post("/control/leases", json={}).json()
    second = visitor_factory().post("/control/leases", json={}).json()
    assert first["lease"]["slot_id"] != second["lease"]["slot_id"]
    assert first["lease"]["generation"] == second["lease"]["generation"] == 0


def test_lease_response_hides_internals_but_admin_sees_them(client: TestClient, lease: dict):
    slots = client.get("/control/admin/slots", headers=ADMIN_HEADERS).json()["items"]
    assert slots[0]["container_name"].startswith("tutorlm-instance-")


# ------------------------------------------------------------------- admin
def test_admin_endpoints_require_token(client: TestClient):
    assert client.get("/control/admin/slots").status_code == 401
    assert (
        client.get(
            "/control/admin/slots", headers={"X-Admin-Token": "wrong"}
        ).status_code
        == 403
    )


def test_admin_closed_when_token_not_configured(config, adapter):
    """未配置管理令牌 -> 管理接口一律拒绝（fail closed）。"""

    from control_plane.api.app import create_app
    from control_plane.config import ControlPlaneConfig

    bare = ControlPlaneConfig.for_tests(config.db_path.parent, admin_token="")
    app = create_app(bare, adapter=adapter, run_startup_tasks=False)
    with TestClient(app) as c:
        response = c.get("/control/admin/slots", headers=ADMIN_HEADERS)
        assert response.status_code == 401


def test_admin_config_summary_has_no_secrets(client: TestClient):
    body = client.get("/control/admin/config", headers=ADMIN_HEADERS).json()
    assert body["slots"] == 2
    assert body["admin_token_configured"] is True
    assert "test-admin-token" not in repr(body)
    assert "test-pepper" not in repr(body)


@pytest.mark.parametrize("path", ["reconcile", "gc"])
def test_admin_maintenance_endpoints(client: TestClient, path: str):
    response = client.post(f"/control/admin/{path}", headers=ADMIN_HEADERS)
    assert response.status_code == 200
    assert "report" in response.json()


def test_admin_can_force_reclaim_and_reuse_slot(client: TestClient, lease: dict):
    response = client.post("/control/admin/slots/0/reclaim", headers=ADMIN_HEADERS)
    assert response.status_code == 200
    slot = response.json()["slot"]
    assert slot["state"] == "free"
    assert slot["generation"] == 1
    # 释放后旧令牌失效，新访客可用
    assert (
        client.get(
            "/control/leases/current", headers=auth(lease["token"])
        ).status_code
        == 401
    )


def test_admin_drain_and_release_quarantine(client: TestClient):
    client.fake_adapter.fail_start.add("tutorlm-instance-s00-g0000")
    client.post("/control/leases", json={})
    drain = client.post("/control/admin/slots/0/drain", headers=ADMIN_HEADERS)
    assert drain.status_code == 200
    client.fake_adapter.fail_start.clear()
    released = client.post(
        "/control/admin/slots/0/release-quarantine",
        headers=ADMIN_HEADERS,
        params={"reason": "ops verified"},
    )
    assert released.status_code == 200
    assert released.json()["slot"]["state"] == "free"


def test_admin_events_are_listed(client: TestClient, lease: dict):
    events = client.get("/control/admin/events", headers=ADMIN_HEADERS).json()["items"]
    kinds = {event["kind"] for event in events}
    assert "lease.allocated" in kinds
    assert "slot.ready" in kinds


def test_admin_leases_list_shows_ownership(client: TestClient, lease: dict):
    items = client.get("/control/admin/leases", headers=ADMIN_HEADERS).json()["items"]
    assert len(items) == 1
    assert items[0]["lease_id"] == lease["lease"]["lease_id"]
    assert "token_hash" not in items[0]


def test_validation_error_envelope(client: TestClient):
    response = client.post("/control/leases", json={"label": "x" * 200})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_input"


def test_unknown_route_is_404(client: TestClient):
    assert client.get("/control/nope").status_code == 404
