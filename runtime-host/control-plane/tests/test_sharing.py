"""项目分享：副本隔离、会话排除、撤销/过期、幂等、失败与重启对账。

全部使用 fake adapter：它的卷内容是独立映射，因此可以证明"不共享 bytes"。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from control_plane.api.app import build_components, create_app
from control_plane.config import ControlPlaneConfig
from control_plane.domain.models import STORAGE_LAYOUT_CONTENT_ONLY
from control_plane.runtime.fake import FakeRuntimeAdapter

ADMIN = {"X-Admin-Token": "test-admin-token"}


@pytest.fixture()
def system(tmp_path):
    config = ControlPlaneConfig.for_tests(tmp_path, slots=2)
    adapter = FakeRuntimeAdapter()
    app = create_app(config, adapter=adapter, run_startup_tasks=False)
    with TestClient(app) as client:
        # 每个测试都以一个已登录的所有者上下文开始。
        client.post("/control/users")
        yield client, app.state, adapter


def new_user(app, client=None):
    """新的浏览器上下文 + 新用户身份。"""

    view = client or TestClient(app)
    credentials = view.post("/control/users").json()
    return view, credentials["user"]["user_id"]


def create_workspace(client, title="Project"):
    return client.post("/control/workspaces", json={"title": title}).json()["workspace"]


def make_shareable(state, workspace, contents=(("notes.md", b"hello"),)):
    """把项目标记为 v3 内容布局并写入业务文件。"""

    stored = state.store.require_workspace(workspace["workspace_id"])
    # 项目创建时不分配计算，因此此刻卷尚未存在；先确保它存在。
    labels = dict(state.config.labels())
    labels.update(
        {"tutorlm.workspace_id": stored.workspace_id, "tutorlm.user_id": stored.user_id}
    )
    state.adapter.ensure_volume(
        __import__("control_plane.ports", fromlist=["VolumeSpec"]).VolumeSpec(
            name=stored.volume_name, labels=labels
        )
    )
    for key, data in contents:
        state.adapter.write_volume(stored.volume_name, f'{stored.data_directory}/{key}', data)
    state.store.mark_workspace_layout(workspace["workspace_id"], STORAGE_LAYOUT_CONTENT_ONLY)
    return stored


def issue_share(client, workspace, label="share"):
    response = client.post(
        f"/control/workspaces/{workspace['workspace_id']}/invitations", json={"label": label}
    )
    assert response.status_code == 201, response.text
    return response.json()["code"]


# ------------------------------------------------------------------ 宜当
def test_owner_can_issue_share_code_only_after_content_only_layout(system):
    client, state, _ = system
    workspace = create_workspace(client)
    # 未声明布局 -> 拒绝签发，避免整卷盲拷可能携带的 session。
    refused = client.post(f"/control/workspaces/{workspace['workspace_id']}/invitations", json={})
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "share_source_unsupported"

    make_shareable(state, workspace)
    assert client.post(
        f"/control/workspaces/{workspace['workspace_id']}/invitations", json={}
    ).status_code == 201


def test_other_user_cannot_issue_or_revoke_a_share_code(system):
    client, state, adapter = system
    workspace = create_workspace(client)
    make_shareable(state, workspace)
    code = issue_share(client, workspace)

    other = TestClient(client.app)
    try:
        other.post("/control/users")
        assert other.post(
            f"/control/workspaces/{workspace['workspace_id']}/invitations", json={}
        ).status_code == 404
        invitation_id = client.get(
            f"/control/workspaces/{workspace['workspace_id']}/invitations"
        ).json()["items"][0]["invitation_id"]
        assert other.delete(
            f"/control/workspaces/{workspace['workspace_id']}/invitations/{invitation_id}"
        ).status_code == 404
    finally:
        other.close()


# ------------------------------------------------------------------ 复制
def test_import_creates_independent_copy_and_never_shares_bytes(system):
    client, state, adapter = system
    workspace = create_workspace(client, "Source")
    stored = make_shareable(state, workspace, (("notes.md", b"original"),))
    code = issue_share(client, workspace)

    viewer = TestClient(client.app)
    try:
        viewer.post("/control/users")
        response = viewer.post(
            "/control/workspace-imports", json={"invitation_code": code, "title": "Copy"}
        )
        assert response.status_code == 200, response.text
        imported = response.json()["workspace"]
        assert imported["workspace_id"] != workspace["workspace_id"]

        copy = state.store.require_workspace(imported["workspace_id"])
        assert copy.source_workspace_id == workspace["workspace_id"]
        # UUID 专属目录，不是旧卷根。
        assert copy.data_directory == f"users/{copy.user_id}/workspaces/{copy.workspace_id}"
        assert copy.volume_name != stored.volume_name

        # 内容确实复制到了新卷的子目录。
        assert adapter.read_volume(copy.volume_name, f"{copy.data_directory}/notes.md") == b"original"

        # 修改副本不影响源。
        adapter.write_volume(copy.volume_name, f"{copy.data_directory}/notes.md", b"changed")
        assert adapter.read_volume(stored.volume_name, f"{stored.data_directory}/notes.md") == b"original"
        # 底层 bytes 字典也是独立的。
        assert adapter._volume_files[copy.volume_name] is not adapter._volume_files[stored.volume_name]
    finally:
        viewer.close()


def test_import_excludes_session_and_auth_subtrees(system):
    client, state, adapter = system
    workspace = create_workspace(client)
    stored = make_shareable(
        state,
        workspace,
        (
            ("notes.md", b"business"),
            ("sessions/session.json", b"SECRET-SESSION"),
            ("auth/tokens.json", b"SECRET-TOKEN"),
            ("credentials/db.sqlite3", b"SECRET-DB"),
        ),
    )
    code = issue_share(client, workspace)

    viewer = TestClient(client.app)
    try:
        viewer.post("/control/users")
        imported = viewer.post(
            "/control/workspace-imports", json={"invitation_code": code}
        ).json()["workspace"]
    finally:
        viewer.close()

    copy = state.store.require_workspace(imported["workspace_id"])
    copied_keys = set(adapter._volume_files[copy.volume_name])
    assert f"{copy.data_directory}/notes.md" in copied_keys
    assert not any("SECRET" in repr(key) for key in copied_keys)
    assert not any("sessions/" in key for key in copied_keys)
    assert not any("auth/" in key for key in copied_keys)
    assert not any("credentials/" in key for key in copied_keys)


def test_import_stops_source_compute_before_copying(system):
    client, state, adapter = system
    workspace = create_workspace(client)
    make_shareable(state, workspace)
    opened = client.post(
        "/control/leases", json={"workspace_id": workspace["workspace_id"]}
    ).json()["lease"]
    code = issue_share(client, workspace)

    viewer = TestClient(client.app)
    try:
        viewer.post("/control/users")
        assert viewer.post(
            "/control/workspace-imports", json={"invitation_code": code}
        ).status_code == 200
    finally:
        viewer.close()

    # 源容器已停止且槽位已释放，但项目与卷完整保留。
    container = state.config.container_name(opened["slot_id"], opened["generation"])
    assert state.adapter.status(container).running is False
    assert state.store.require_lease(opened["lease_id"]).state == "reclaimed"
    assert state.store.require_workspace(workspace["workspace_id"]).state == "active"


def test_import_keeps_source_volume_on_copy_failure(system):
    client, state, adapter = system
    workspace = create_workspace(client)
    stored = make_shareable(state, workspace, (("notes.md", b"original"),))
    code = issue_share(client, workspace)
    adapter.fail_copy.add(stored.volume_name)

    viewer = TestClient(client.app)
    try:
        viewer.post("/control/users")
        response = viewer.post("/control/workspace-imports", json={"invitation_code": code})
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "workspace_copy_failed"
        # 源项目与其内容完整保留。
        assert adapter.read_volume(stored.volume_name, f"{stored.data_directory}/notes.md") == b"original"
        assert state.store.require_workspace(workspace["workspace_id"]).state == "active"
        # 失败的副本不会可见（不是 active）。
        failed = [
            w for w in state.store.list_workspace_copies(states=("failed",))
        ]
        assert failed and failed[0].destination_workspace_id
        destination = state.store.require_workspace(failed[0].destination_workspace_id)
        assert destination.state == "failed"
        # 用户列表里不出现失败副本。
        assert viewer.get("/control/workspaces").json()["items"] == []
    finally:
        viewer.close()


def test_reconcile_marks_interrupted_copy_unavailable_without_touching_source(system):
    client, state, adapter = system
    workspace = create_workspace(client)
    stored = make_shareable(state, workspace, (("notes.md", b"original"),))
    code = issue_share(client, workspace)

    viewer = TestClient(client.app)
    try:
        viewer.post("/control/users")
        # 模拟进程在复制中途被杀：手工建一条 PREPARING 记录。
        import uuid as _uuid

        destination_id = str(_uuid.uuid4())
        user_id = state.store.get_user  # noqa: F841 - 保持可读性
        recipient = viewer.get("/control/users/me").json()["user"]["user_id"]
        state.store.begin_share_copy(
            copy_id="copy-in-flight",
            source_workspace_id=workspace["workspace_id"],
            destination_workspace_id=destination_id,
            recipient_user_id=recipient,
            destination_volume_name=state.config.workspace_volume_name(destination_id),
            title="half",
            invitation_id=None,
            idempotency_key=None,
            data_directory=f"users/{recipient}/workspaces/{destination_id}",
        )
        # GC 不能因为"看不到引用"而删掉源项目卷。
        now = state.store._clock() + 10 * 24 * 3600
        state.store._clock = lambda: now
        assert state.store.claim_expired_workspaces(14400) == []

        report = state.sharing.reconcile()
        assert "copy-in-flight" in report["cleaned"]
        assert state.store.require_workspace(destination_id).state == "failed"
        assert adapter.read_volume(stored.volume_name, f"{stored.data_directory}/notes.md") == b"original"
        assert state.store.require_workspace(workspace["workspace_id"]).state == "active"
    finally:
        viewer.close()


# ------------------------------------------------------------------ 幂等
def test_idempotency_key_returns_the_same_copy(system):
    client, state, adapter = system
    workspace = create_workspace(client)
    make_shareable(state, workspace)
    code = issue_share(client, workspace)

    viewer = TestClient(client.app)
    try:
        viewer.post("/control/users")
        headers = {"Idempotency-Key": "import-1"}
        first = viewer.post(
            "/control/workspace-imports",
            json={"invitation_code": code},
            headers=headers,
        ).json()
        second = viewer.post(
            "/control/workspace-imports",
            json={"invitation_code": code},
            headers=headers,
        ).json()
        assert first["workspace"]["workspace_id"] == second["workspace"]["workspace_id"]
        assert second["reused"] is True
        assert len(state.store.list_workspace_copies(states=("copied",))) == 1
    finally:
        viewer.close()


def test_repeated_import_without_key_defaults_to_the_existing_copy(system):
    client, state, adapter = system
    workspace = create_workspace(client)
    make_shareable(state, workspace)
    code = issue_share(client, workspace)

    viewer = TestClient(client.app)
    try:
        viewer.post("/control/users")
        first = viewer.post("/control/workspace-imports", json={"invitation_code": code}).json()
        second = viewer.post("/control/workspace-imports", json={"invitation_code": code}).json()
        assert first["workspace"]["workspace_id"] == second["workspace"]["workspace_id"]
        assert second["reused"] is True
    finally:
        viewer.close()


# ------------------------------------------------------------------ 撤销
def test_revoked_share_code_cannot_be_imported(system):
    client, state, adapter = system
    workspace = create_workspace(client)
    make_shareable(state, workspace)
    code = issue_share(client, workspace)
    invitation_id = client.get(
        f"/control/workspaces/{workspace['workspace_id']}/invitations"
    ).json()["items"][0]["invitation_id"]
    assert client.delete(
        f"/control/workspaces/{workspace['workspace_id']}/invitations/{invitation_id}"
    ).status_code == 200

    viewer = TestClient(client.app)
    try:
        viewer.post("/control/users")
        response = viewer.post("/control/workspace-imports", json={"invitation_code": code})
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "invalid_invitation"
    finally:
        viewer.close()


def test_expired_share_code_cannot_be_imported(system):
    client, state, adapter = system
    workspace = create_workspace(client)
    make_shareable(state, workspace)
    code = issue_share(client, workspace)
    # 跳到 4 小时之后。
    now = state.store._clock() + 4 * 3600 + 1
    state.store._clock = lambda: now
    state.allocator._clock = lambda: now

    viewer = TestClient(client.app)
    try:
        viewer.post("/control/users")
        response = viewer.post("/control/workspace-imports", json={"invitation_code": code})
        assert response.status_code == 410
        assert response.json()["error"]["code"] == "invitation_expired"
    finally:
        viewer.close()


def test_share_code_cannot_be_used_directly_on_the_lease_endpoint(system):
    """分享码**不能**直接对 ``/control/leases``。

    否则它就变成了直接挂载源项目的访问令牌。它只能走导入，
    导入后的普通 trial grant 才允许无码租约。
    """

    client, state, adapter = system
    workspace = create_workspace(client)
    make_shareable(state, workspace)
    share_code = _issue_share_code(client, workspace)

    viewer = TestClient(client.app)
    try:
        viewer.post("/control/users")
        # 有效分享码也不能直接申请计算（拒绝，不是“普通优先级”）。
        direct = viewer.post("/control/leases", json={"invitation_code": share_code})
        assert direct.status_code == 403
        assert direct.json()["error"]["code"] == "invalid_invitation"
        assert state.store.list_live_leases() == []

        # 导入后，副本可以无码申请普通租约。
        imported = viewer.post(
            "/control/workspace-imports", json={"invitation_code": share_code}
        ).json()["workspace"]
        opened = viewer.post(
            "/control/leases", json={"workspace_id": imported["workspace_id"]}
        )
        assert opened.status_code == 201
        assert opened.json()["lease"]["priority"] == 0
    finally:
        viewer.close()


def test_share_code_cannot_be_used_as_a_source_access_token(system):
    """分享码不能直接打开源项目。"""

    client, state, adapter = system
    workspace = create_workspace(client)
    make_shareable(state, workspace)
    code = issue_share(client, workspace)

    viewer = TestClient(client.app)
    try:
        viewer.post("/control/users")
        # 导入后得到的是独立副本，而不是源项目。
        imported = viewer.post(
            "/control/workspace-imports", json={"invitation_code": code}
        ).json()["workspace"]
        assert imported["workspace_id"] != workspace["workspace_id"]
        assert viewer.get(
            f"/control/workspaces/{workspace['workspace_id']}"
        ).status_code == 404
    finally:
        viewer.close()


def test_imported_copy_still_requires_system_invitation_when_invitation_only(system):
    """Sharing copies content, never bypasses invitation-only slot admission."""

    client, state, adapter = system
    workspace = create_workspace(client)
    make_shareable(state, workspace)
    code = issue_share(client, workspace)
    state.invitations.policy = type(state.invitations.policy)(
        invitation_only=True,
        system_invitation_codes=(),
        copy_helper_image="",
        share_ttl_seconds=state.invitations.policy.share_ttl_seconds,
    )

    viewer = TestClient(client.app)
    try:
        viewer.post("/control/users")
        imported = viewer.post(
            "/control/workspace-imports", json={"invitation_code": code}
        ).json()["workspace"]
        opened = viewer.post(
            "/control/leases", json={"workspace_id": imported["workspace_id"]}
        )
        assert opened.status_code == 403
        assert opened.json()['error']['code'] == 'invitation_required'
        invitation, system_code = state.invitations.issue_system_code(label='test')
        opened = viewer.post('/control/leases', json={
            'workspace_id': imported['workspace_id'], 'invitation_code': system_code,
        })
        assert opened.status_code == 201
        assert opened.json()['lease']['priority'] == 100
    finally:
        viewer.close()


def test_tampered_data_directory_is_rejected(system):
    """存储路径是信任边界：DB 被改成非规范路径后必须拒绝读取。"""

    client, state, adapter = system
    workspace = create_workspace(client)
    with state.store.transaction() as conn:
        conn.execute(
            "UPDATE workspaces SET data_directory=? WHERE workspace_id=?",
            ("../../etc", workspace["workspace_id"]),
        )
    from control_plane.errors import InvalidDataDirectory

    with pytest.raises(InvalidDataDirectory):
        state.store.require_workspace(workspace["workspace_id"])


def test_share_copy_target_is_always_a_brand_new_volume(system):
    client, state, adapter = system
    workspace = create_workspace(client)
    make_shareable(state, workspace)
    code = issue_share(client, workspace)

    viewer = TestClient(client.app)
    try:
        viewer.post("/control/users")
        imported = viewer.post(
            "/control/workspace-imports", json={"invitation_code": code}
        ).json()["workspace"]
    finally:
        viewer.close()

    copy = state.store.require_workspace(imported["workspace_id"])
    assert copy.volume_name not in {None, ""}
    assert copy.volume_name != state.store.require_workspace(workspace["workspace_id"]).volume_name
    # 目标卷不属于源，且内容只写在 UUID 专属子目录下。
    assert all(
        key.startswith(copy.data_directory + "/") for key in adapter._volume_files[copy.volume_name]
    )


def test_leaked_helper_keeps_source_pinned_and_target_invisible(system):
    """适配器报告 helper 未清干净时，不能释放源的 pin，也不能删目标卷。

    否则残留 helper 仍挂载源卷时，GC 会删掉它，或源项目被重新打开产生并发 writer。
    """

    from control_plane.errors import RuntimeUnavailable

    client, state, adapter = system
    workspace = create_workspace(client)
    stored = make_shareable(state, workspace, (("notes.md", b"original"),))
    code = issue_share(client, workspace)

    def leaked_helper(spec):
        raise RuntimeUnavailable(
            "helper leaked",
            details={
                "operation": "copy_volume",
                "source_volume": stored.volume_name,
                "destination_volume": spec.destination.name,
                "copy_completed": False,
                "helper_cleaned": False,
                "destination_rolled_back": True,
                "rollback_error": None,
            },
        )

    adapter.copy_volume = leaked_helper  # type: ignore[assignment]
    viewer = TestClient(client.app)
    try:
        viewer.post("/control/users")
        response = viewer.post("/control/workspace-imports", json={"invitation_code": code})
        assert response.status_code == 502
        assert response.json()["error"]["details"]["source_pinned"] is True
    finally:
        viewer.close()

    copy = state.store.list_workspace_copies()[0]
    # 仍在 PREPARING -> GC 仍会因 pin 而不删源项目卷。
    assert copy.state == "preparing"
    assert state.store.claim_expired_workspaces(0) == []
    assert adapter.read_volume(stored.volume_name, f"{stored.data_directory}/notes.md") == b"original"
    destination = state.store.require_workspace(copy.destination_workspace_id)
    assert destination.state == "failed"


def test_failed_copy_destination_is_reclaimed_by_storage_gc(system):
    """失败副本的目标项目不可见，且会被存储 GC 回收残留卷。"""

    client, state, adapter = system
    workspace = create_workspace(client)
    stored = make_shareable(state, workspace, (("notes.md", b"original"),))
    code = issue_share(client, workspace)
    adapter.fail_copy.add(stored.volume_name)

    viewer = TestClient(client.app)
    try:
        viewer.post("/control/users")
        assert viewer.post(
            "/control/workspace-imports", json={"invitation_code": code}
        ).status_code == 502
        # 失败副本不可见。
        assert viewer.get("/control/workspaces").json()["items"] == []
    finally:
        viewer.close()

    failed = state.store.list_workspace_copies(states=("failed",))
    assert failed
    destination_id = failed[0].destination_workspace_id
    assert state.store.require_workspace(destination_id).state == "failed"

    # 模拟回滚时删除失败：目标卷仍在，GC 应该在后续回收它。
    destination_volume = state.config.workspace_volume_name(destination_id)
    adapter.ensure_volume(
        __import__("control_plane.ports", fromlist=["VolumeSpec"]).VolumeSpec(
            name=destination_volume, labels=dict(state.config.labels())
        )
    )
    report = state.workspaces.gc_once()
    assert destination_id in report["failed_copies_claimed"]
    assert state.store.require_workspace(destination_id).state == "deleted"
    assert not adapter.volume_exists(destination_volume)
    # 源一直完好。
    assert adapter.read_volume(stored.volume_name, f"{stored.data_directory}/notes.md") == b"original"


def test_source_is_pinned_before_source_compute_is_stopped(system):
    """顺序不变量：必须先 pin 源（写入 copy 操作），再停源计算。

    否则在停源与建立副本记录之间，GC 可以因为看不到引用而删掉源卷，
    造成真实数据丢失。这里直接观察在 ``_stop_source_compute`` 执行时，源是否
    已经处于 pin 状态（存在 PREPARING 且 source_pinned=1），并让一次 GC 尝试回收。
    """

    client, state, adapter = system
    owner_id = client.get("/control/users/me").json()["user"]["user_id"]
    workspace = create_workspace(client)
    stored = make_shareable(state, workspace, (("notes.md", b"original"),))
    share_code = _issue_share_code(client, workspace)

    service = state.sharing
    original_stop = service._stop_source_compute
    observed: dict[str, object] = {}

    def stop_and_probe(source):
        # 此刻应当已有一条 pin 住源项目的 PREPARING 操作。
        ops = state.store.list_workspace_copies(states=("preparing",))
        observed["pinned_before_stop"] = any(
            op.source_workspace_id == source.workspace_id for op in ops
        )
        observed["claimed"] = [
            w.workspace_id
            for w in state.store.claim_expired_workspaces(0)
            if w.workspace_id == source.workspace_id
        ]
        return original_stop(source)

    service._stop_source_compute = stop_and_probe  # type: ignore[assignment]
    try:
        result = service.import_share(user_id=owner_id, code=share_code)
        assert result.workspace.workspace_id
    finally:
        service._stop_source_compute = original_stop  # type: ignore[assignment]

    assert observed["pinned_before_stop"] is True
    assert observed["claimed"] == []
    assert adapter.read_volume(stored.volume_name, f"{stored.data_directory}/notes.md") == b"original"


def _issue_share_code(client, workspace):
    response = client.post(
        f"/control/workspaces/{workspace['workspace_id']}/invitations", json={}
    )
    assert response.status_code == 201, response.text
    return response.json()["code"]


def test_reconcile_defers_when_helper_may_still_be_running(system):
    """检测到可能残留的 helper 时，对账不能释放 pin 或删卷。"""

    client, state, adapter = system
    owner_id = client.get("/control/users/me").json()["user"]["user_id"]
    workspace = create_workspace(client)
    stored = make_shareable(state, workspace, (("notes.md", b"original"),))

    destination_id = str(__import__("uuid").uuid4())
    state.store.begin_share_copy(
        copy_id="inflight-1",
        source_workspace_id=workspace["workspace_id"],
        destination_workspace_id=destination_id,
        recipient_user_id=owner_id,
        destination_volume_name=state.config.workspace_volume_name(destination_id),
        title="half",
        invitation_id=None,
        idempotency_key=None,
        data_directory=f"users/{owner_id}/workspaces/{destination_id}",
    )
    # 仿造一个仍在运行的 helper 容器（命名含 -copy-）。
    leftover = state.config.container_name(0, 0).replace("tutorlm-instance", "tutorlm-instance") + "-copy-deadbeef"
    from control_plane.ports import InstanceSpec
    adapter.create(InstanceSpec(
        slot_id=99, generation=0, container_name=leftover, image="helper",
        volume_name=state.config.workspace_volume_name(destination_id), mount_path="/data",
        internal_port=8000, labels={**state.config.labels(), "tutorlm.copy": "volume"},
    ))

    report = state.sharing.reconcile()
    assert report["manual_review"] == ["inflight-1"]
    assert report["cleaned"] == []
    # 源仍被 pin，目标不可见。
    assert state.store.claim_expired_workspaces(0) == []
    assert adapter.read_volume(stored.volume_name, f"{stored.data_directory}/notes.md") == b"original"
    assert state.store.require_workspace(destination_id).state == "copying"


def test_reused_copy_is_rejected_after_destination_is_gone(system):
    """目标副本被删后，重复导入不能返回一个已删对象。

    这是幂等路径的安全不变量：``reused`` 只能指向一个仍然 active 的副本。
    """

    from control_plane.errors import WorkspaceExpired

    client, state, adapter = system
    workspace = create_workspace(client)
    make_shareable(state, workspace)
    share_code = _issue_share_code(client, workspace)

    viewer = TestClient(client.app)
    try:
        viewer.post("/control/users")
        first = viewer.post("/control/workspace-imports", json={"invitation_code": share_code}).json()
        destination_id = first["workspace"]["workspace_id"]
    finally:
        viewer.close()

    copy = state.store.copy_for_destination(destination_id)
    assert copy is not None

    # 目标项目被 GC 删除（模拟超过空闲期）。
    state.store.claim_expired_workspaces(0)
    state.workspaces.gc_once()
    assert state.store.require_workspace(destination_id).state == "deleted"

    with pytest.raises(WorkspaceExpired):
        state.sharing._resume_existing(copy, copy.recipient_user_id)
