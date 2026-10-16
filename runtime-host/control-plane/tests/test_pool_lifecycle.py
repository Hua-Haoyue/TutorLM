"""端到端：固定池在"多人轮转"下的隔离与代次不变量。

这是最贴近真实黑客松现场的一组测试：多人反复申请/释放，验证
**数据不串、凭据不复活、槽位不漏**。
"""

from __future__ import annotations

import threading

from fastapi.testclient import TestClient

from tests.conftest import ADMIN_HEADERS, auth


def test_full_cycle_two_visitors_two_slots(client: TestClient, visitor_factory):
    bob_client = visitor_factory()
    carol_client = visitor_factory()
    alice = client.post("/control/leases", json={"label": "alice"}).json()
    bob = bob_client.post("/control/leases", json={"label": "bob"}).json()
    assert alice["lease"]["slot_id"] != bob["lease"]["slot_id"]
    assert client.get("/control/capacity").json()["slots_free"] == 0

    # 两人各自的令牌只解析到自己的租约
    a = client.get("/control/leases/current", headers=auth(alice["token"])).json()
    b = bob_client.get("/control/leases/current", headers=auth(bob["token"])).json()
    assert a["lease"]["lease_id"] != b["lease"]["lease_id"]
    assert a["lease"]["slot_id"] != b["lease"]["slot_id"]

    # 第三人被拒
    assert carol_client.post("/control/leases", json={}).status_code == 503

    # alice 释放后槽位可复用，且代次 +1
    client.delete("/control/leases/current", headers=auth(alice["token"]))
    carol = carol_client.post("/control/leases", json={"label": "carol"}).json()
    assert carol["lease"]["slot_id"] == alice["lease"]["slot_id"]
    assert carol["lease"]["generation"] == alice["lease"]["generation"] + 1
    # 旧凭据彻底失效
    assert (
        client.get(
            "/control/leases/current", headers=auth(alice["token"])
        ).status_code
        == 401
    )


def test_volume_is_deleted_and_regenerated_between_visitors(client: TestClient):
    """同一槽位的两位访客之间，数据卷必须被真正删除。"""

    adapter = client.fake_adapter
    first = client.post("/control/leases", json={}).json()
    assert adapter.volume_exists("tutorlm-instance-data-s00")
    client.delete("/control/leases/current", headers=auth(first["token"]))
    assert not adapter.volume_exists("tutorlm-instance-data-s00")

    client.post("/control/leases", json={})
    assert adapter.volume_exists("tutorlm-instance-data-s00")
    # 容器名带新代次，不会是上一位访客的容器
    assert any(
        name.endswith("g0001") for name in adapter.snapshot()["containers"]
    )


def test_each_slot_gets_its_own_volume(client: TestClient, visitor_factory):
    client.post("/control/leases", json={})
    visitor_factory().post("/control/leases", json={})
    volumes = client.fake_adapter.snapshot()["volumes"]
    assert volumes == ["tutorlm-instance-data-s00", "tutorlm-instance-data-s01"]


def test_quarantined_slot_reduces_effective_capacity(client: TestClient, visitor_factory):
    """一个槽位隔离后，可用容量必须真实下降（不能超卖）。"""

    client.fake_adapter.fail_start.add("tutorlm-instance-s00-g0000")
    client.post("/control/leases", json={})  # 槽位 0 隔离
    client.fake_adapter.fail_start.clear()

    capacity = client.get("/control/capacity").json()
    assert capacity["slots_quarantined"] == 1
    assert capacity["slots_free"] == 1

    # 只剩一个槽位可用
    assert client.post("/control/leases", json={}).status_code == 201
    assert visitor_factory().post("/control/leases", json={}).status_code == 503


def test_repeated_cycles_do_not_leak_slots(client: TestClient):
    """反复申请/释放多轮后，槽位与卷都应回到干净状态。"""

    adapter = client.fake_adapter
    for _ in range(5):
        lease = client.post("/control/leases", json={}).json()
        client.delete("/control/leases/current", headers=auth(lease["token"]))

    capacity = client.get("/control/capacity").json()
    assert capacity["slots_free"] == 2
    assert capacity["leases_live"] == 0
    snapshot = adapter.snapshot()
    assert snapshot["containers"] == {}
    assert snapshot["volumes"] == []
    # 分配器总是挑最小的空闲槽位，所以 5 轮都落在槽位 0：
    # 槽位 0 代次 = 5，槽位 1 从未被使用 = 0。
    # 代次严格递增说明每次都是"新实例"而不是复用旧容器。
    slots = client.get("/control/admin/slots", headers=ADMIN_HEADERS).json()["items"]
    by_id = {slot["slot_id"]: slot["generation"] for slot in slots}
    assert by_id == {0: 5, 1: 0}


def test_restart_reconcile_does_not_hand_out_dirty_slot(config, adapter):
    """控制面重启后，占用中的槽位不能被当成空闲发给新访客。"""

    from control_plane.api.app import create_app

    first_app = create_app(config, adapter=adapter, run_startup_tasks=False)
    with TestClient(first_app) as c:
        lease = c.post("/control/leases", json={}).json()

    # 模拟重启：新进程、同一个 DB
    second_app = create_app(config, adapter=adapter, run_startup_tasks=True)
    with TestClient(second_app) as c:
        capacity = c.get("/control/capacity").json()
        # 槽位仍被上一轮的租约占用（重启不会把它当空闲）
        assert capacity["slots_free"] == 1
        # 旧令牌在重启后依然能解析（盐已持久化）
        assert (
            c.get(
                "/control/leases/current", headers=auth(lease["token"])
            ).status_code
            == 200
        )


def test_concurrent_http_allocation_is_safe(config, adapter):
    """并发 HTTP 申请：成功数不超过槽位数，且槽位不重复。"""

    from control_plane.api.app import create_app

    app = create_app(config, adapter=adapter, run_startup_tasks=False)
    with TestClient(app) as c:
        results: list[int] = []
        slots: list[int] = []
        lock = threading.Lock()
        barrier = threading.Barrier(6)

        def worker() -> None:
            barrier.wait()
            # Do not share a cookie jar between independent concurrent visitors.
            visitor = TestClient(app)
            try:
                response = visitor.post("/control/leases", json={})
            finally:
                visitor.close()
            with lock:
                results.append(response.status_code)
                if response.status_code == 201:
                    slots.append(response.json()["lease"]["slot_id"])

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert results.count(201) == 2
        assert sorted(slots) == [0, 1]
        assert results.count(503) == 4
