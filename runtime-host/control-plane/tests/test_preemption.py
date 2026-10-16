"""最高优先级抢占：满槽时系统码入场选择可安全回收的受控租约。

关键不变量：只抢占已就绪的受控 active 租约；不动 provisioning/copy 中的槽位；
被抢占者的**持久项目数据保留**；停止失败则隔离并跳到下一个候选或 503。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from control_plane.api.app import create_app
from control_plane.config import ControlPlaneConfig
from control_plane.invitations import SYSTEM_PRIORITY
from control_plane.runtime.fake import FakeRuntimeAdapter

ADMIN = {"X-Admin-Token": "test-admin-token"}


def build(tmp_path, **overrides):
    config = ControlPlaneConfig.for_tests(tmp_path, **overrides)
    adapter = FakeRuntimeAdapter()
    app = create_app(config, adapter=adapter, run_startup_tasks=False)
    return app, adapter


def new_user(app):
    client = TestClient(app)
    client.post("/control/users")
    return client


def open_project(client, title):
    workspace = client.post("/control/workspaces", json={"title": title}).json()["workspace"]
    issued = client.post(
        "/control/leases", json={"workspace_id": workspace["workspace_id"]}
    ).json()
    return workspace, issued["lease"]


def issue_system_code(client):
    response = client.post("/control/admin/invitations", json={"label": "ops"}, headers=ADMIN)
    assert response.status_code == 201, response.text
    return response.json()["code"]


def test_system_code_preempts_lowest_priority_and_keeps_victim_data(tmp_path):
    app, adapter = build(tmp_path, slots=1)
    with TestClient(app) as owner:
        # 一个普通访客先占住唐一槽位并写入持久数据。
        victim = new_user(app)
        try:
            workspace, lease = open_project(victim, "Victim")
            volume = app.state.store.require_workspace(workspace["workspace_id"]).volume_name
            # 项目在开启时创建了卷（挂载时），直接写入业务数据。
            adapter.write_volume(volume, "precious.md", b"student work")

            code = issue_system_code(owner)
            # 全部槽位已满：系统码入场必须抢占。
            newcomer = new_user(app)
            try:
                opened = newcomer.post("/control/leases", json={"invitation_code": code})
                assert opened.status_code == 201, opened.text
                new_lease = opened.json()["lease"]
                assert new_lease["priority"] == SYSTEM_PRIORITY
                # 新来者拿到的就是被抢占者的槽位，但是新代次。
                assert new_lease["slot_id"] == lease["slot_id"]
                assert new_lease["generation"] > lease["generation"]

                # 被抢占者的租约已终结。
                assert app.state.store.require_lease(lease["lease_id"]).state == "reclaimed"
                # **持久数据保留**：卷与内容都还在。
                assert adapter.volume_exists(volume)
                assert adapter.read_volume(volume, "precious.md") == b"student work"
                assert app.state.store.require_workspace(workspace["workspace_id"]).state == "active"
            finally:
                newcomer.close()
        finally:
            victim.close()


def test_old_lease_cannot_use_a_preempted_slot(tmp_path):
    app, adapter = build(tmp_path, slots=1)
    with TestClient(app) as owner:
        victim = new_user(app)
        try:
            _, lease = open_project(victim, "Victim")
            code = issue_system_code(owner)
            newcomer = new_user(app)
            try:
                newcomer.post("/control/leases", json={"invitation_code": code})
                # 被抢占的租约已终结；旧 cookie 不能再通过门禁。
                assert app.state.store.require_lease(lease["lease_id"]).is_terminal
                assert victim.get("/control/leases/current").status_code == 401
                assert victim.get("/control/admission").status_code == 401
            finally:
                newcomer.close()
        finally:
            victim.close()


def test_preemption_prefers_lowest_priority_then_oldest(tmp_path):
    app, adapter = build(tmp_path, slots=2)
    with TestClient(app) as owner:
        first = new_user(app)
        second = new_user(app)
        try:
            _, old_lease = open_project(first, "Old")
            _, new_lease = open_project(second, "New")
            assert old_lease["slot_id"] != new_lease["slot_id"]
            code = issue_system_code(owner)
            newcomer = new_user(app)
            try:
                opened = newcomer.post("/control/leases", json={"invitation_code": code}).json()["lease"]
                # 两个候选都是同优先级，应该选最旧的那个。
                assert opened["slot_id"] == old_lease["slot_id"]
                assert app.state.store.require_lease(old_lease["lease_id"]).is_terminal
                assert app.state.store.require_lease(new_lease["lease_id"]).state == "active"
            finally:
                newcomer.close()
        finally:
            first.close()
            second.close()


def test_system_does_not_preempt_another_system_lease(tmp_path):
    """同为系统优先级时不会互相抢占（避免系统用户互相驱逐）。"""

    app, adapter = build(tmp_path, slots=1)
    with TestClient(app) as owner:
        code = issue_system_code(owner)
        first = new_user(app)
        try:
            opened = first.post("/control/leases", json={"invitation_code": code})
            assert opened.status_code == 201
            second = new_user(app)
            try:
                contested = second.post("/control/leases", json={"invitation_code": code})
                assert contested.status_code == 503
                assert app.state.store.require_lease(opened.json()["lease"]["lease_id"]).state == "active"
            finally:
                second.close()
        finally:
            first.close()


def test_failed_stop_quarantines_and_does_not_delete_persistent_data(tmp_path):
    app, adapter = build(tmp_path, slots=1)
    with TestClient(app) as owner:
        victim = new_user(app)
        try:
            workspace, lease = open_project(victim, "Victim")
            volume = app.state.store.require_workspace(workspace["workspace_id"]).volume_name
            adapter.write_volume(volume, "precious.md", b"student work")
            container = app.state.config.container_name(lease["slot_id"], lease["generation"])
            adapter.fail_stop.add(container)

            code = issue_system_code(owner)
            newcomer = new_user(app)
            try:
                response = newcomer.post("/control/leases", json={"invitation_code": code})
                # 停不下旧容器 -> 不能把槽位给别人；返回 503 而非冒险分配。
                assert response.status_code == 503
                slot = app.state.store.get_slot(lease["slot_id"])
                assert slot.state == "quarantined"
                # 原持久数据未被删除。
                assert adapter.volume_exists(volume)
                assert adapter.read_volume(volume, "precious.md") == b"student work"
            finally:
                newcomer.close()
        finally:
            victim.close()


def test_preemption_skips_slots_still_provisioning(tmp_path):
    """不抢占尚在 provisioning 的槽位。"""

    app, adapter = build(tmp_path, slots=1)
    with TestClient(app) as owner:
        # 直接把槽位标为 allocating（模拟某个请求卡在启动中）。
        store = app.state.store
        # 模拟一个请求正卡在启动中（尚未 READY）。
        store.update_slot(
            0, state="allocating", lease_id="pending-lease", workload_state="starting"
        )

        code = issue_system_code(owner)
        newcomer = new_user(app)
        try:
            response = newcomer.post("/control/leases", json={"invitation_code": code})
            assert response.status_code == 503
            assert store.require_slot(0).state == "allocating"
        finally:
            newcomer.close()


def test_client_priority_field_is_ignored(tmp_path):
    app, adapter = build(tmp_path, slots=1)
    with TestClient(app) as client:
        opened = client.post("/control/leases", json={"priority": 999}).json()["lease"]
        assert opened["priority"] == 0


def test_preemption_requires_the_scheduling_lock_across_requests(tmp_path):
    """并发抢占不能把同一个槽位分给两个请求。"""

    import threading
    from concurrent.futures import ThreadPoolExecutor

    app, adapter = build(tmp_path, slots=1)
    with TestClient(app) as owner:
        victim = new_user(app)
        try:
            open_project(victim, "Victim")
            code = issue_system_code(owner)
            results: list[str] = []
            barrier = threading.Barrier(2)

            def contend():
                viewer = TestClient(app)
                try:
                    barrier.wait()
                    response = viewer.post("/control/leases", json={"invitation_code": code})
                    return response.status_code
                finally:
                    viewer.close()

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: contend(), range(2)))
            # 一个成功抢占，另一个应该得到 503（无可抢占槽位）。
            assert results.count(201) == 1
            assert set(results) == {201, 503}
            live = [l for l in app.state.store.list_live_leases() if l.is_serving]
            assert len(live) == 1
        finally:
            victim.close()


def test_system_code_on_existing_lease_promotes_in_place_without_new_slot(tmp_path):
    """同一浏览器已有普通租约时，再出示有效系统码应**就地提升**。

    不得新占槽位（否则单个浏览器会同时持有两个租约），旧凭据保持不变。
    """

    app, adapter = build(tmp_path, slots=2)
    with TestClient(app) as owner:
        code = issue_system_code(owner)
        browser = new_user(app)
        try:
            workspace = browser.post(
                "/control/workspaces", json={"title": "Mine"}
            ).json()["workspace"]
            first = browser.post(
                "/control/leases", json={"workspace_id": workspace["workspace_id"]}
            ).json()
            assert first["lease"]["priority"] == 0
            free_before = app.state.store.list_slots()
            free_before = sum(1 for s in free_before if s.state == "free")
            assert free_before == 1

            promoted = browser.post("/control/leases", json={"invitation_code": code})
            assert promoted.status_code == 201, promoted.text
            body = promoted.json()
            # 就地提升：同一租约、同一槽位、同一旧凭据。
            assert body["resumed"] is True
            assert body["lease"]["lease_id"] == first["lease"]["lease_id"]
            assert body["lease"]["slot_id"] == first["lease"]["slot_id"]
            assert body["token"] == first["token"]
            assert body["lease"]["priority"] == SYSTEM_PRIORITY
            # 没有多占槽位。
            free_after = sum(
                1 for s in app.state.store.list_slots() if s.state == "free"
            )
            assert free_after == free_before
            assert len(app.state.store.list_live_leases()) == 1
        finally:
            browser.close()


def test_invalid_code_on_existing_lease_is_rejected_not_promoted(tmp_path):
    app, adapter = build(tmp_path, slots=2)
    with TestClient(app) as owner:
        browser = new_user(app)
        try:
            workspace = browser.post(
                "/control/workspaces", json={"title": "Mine"}
            ).json()["workspace"]
            first = browser.post(
                "/control/leases", json={"workspace_id": workspace["workspace_id"]}
            ).json()
            response = browser.post(
                "/control/leases",
                json={"invitation_code": "TLM-SYS-v1-" + "A" * 43},
            )
            assert response.status_code == 403
            assert response.json()["error"]["code"] == "invalid_invitation"
            # 原租约保持不变且未被提升。
            assert app.state.store.require_lease(first["lease"]["lease_id"]).priority == 0
        finally:
            browser.close()


def test_switching_workspace_with_code_still_conflicts(tmp_path):
    app, adapter = build(tmp_path, slots=2)
    with TestClient(app) as owner:
        code = issue_system_code(owner)
        browser = new_user(app)
        try:
            first = browser.post("/control/workspaces", json={"title": "A"}).json()["workspace"]
            second = browser.post("/control/workspaces", json={"title": "B"}).json()["workspace"]
            browser.post("/control/leases", json={"workspace_id": first["workspace_id"]})
            response = browser.post(
                "/control/leases",
                json={"workspace_id": second["workspace_id"], "invitation_code": code},
            )
            assert response.status_code == 409
        finally:
            browser.close()
