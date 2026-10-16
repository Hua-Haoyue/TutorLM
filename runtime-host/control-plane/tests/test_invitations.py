"""邀请码：格式/哈希/撤销、invitation-only 门禁与最高优先级来源。

这里只验证安全边界，不验证真实 Docker：全部用 fake adapter 与真实的
FastAPI 应用实例（共享同一个 lifespan）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from control_plane.api.app import build_components, create_app
from control_plane.config import ControlPlaneConfig
from control_plane.domain.models import InvitationKind, InvitationState
from control_plane.errors import ConfigError, InvitationRequired
from control_plane.invitations import (
    AccessPolicy,
    InvitationService,
    SYSTEM_CODE_RE,
    generate_system_code,
    generate_share_code,
    is_system_code_format,
)
from control_plane.runtime.fake import FakeRuntimeAdapter

ADMIN = {"X-Admin-Token": "test-admin-token"}


def make_app(tmp_path, **overrides):
    config = ControlPlaneConfig.for_tests(tmp_path, **overrides)
    adapter = FakeRuntimeAdapter()
    app = create_app(config, adapter=adapter, run_startup_tasks=True)
    return app, adapter


# ------------------------------------------------------------------ 格式
def test_generated_system_code_is_csprng_and_never_weak():
    code = generate_system_code()
    assert SYSTEM_CODE_RE.match(code)
    assert is_system_code_format(code)
    # 至少 32 字节（256 bit）的 base64url，不是可猜的短串。
    assert len(code.split("-v1-")[1]) >= 43
    # 分享码与系统码前缀/密码空间独立。
    share = generate_share_code()
    assert share.startswith("TLM-SHARE-")
    assert not is_system_code_format(share)


def test_weak_human_strings_are_rejected_as_system_codes():
    for weak in ("TLM-SYS-ADMIN", "TLM-SYS-1234", "secret", "TLM-SYS-v1-short"):
        assert not is_system_code_format(weak)


def test_invalid_configured_system_code_fails_closed(tmp_path):
    policy = AccessPolicy(system_invitation_codes=("TLM-SYS-tooweak",))
    with pytest.raises(ConfigError):
        policy.validate_system_codes()


def test_configured_system_codes_fail_closed_at_startup(tmp_path):
    # 非法配置必须在构建/启动阶段就失败，而不是带着一个可能被暴力
    # 猜测的"系统码"继续跑。校验可能发生在 dataclass 构造或
    # create_app 内部，两者都是 fail closed。
    with pytest.raises(ConfigError):
        config = ControlPlaneConfig.for_tests(
            tmp_path, system_invitation_codes=("not-a-real-code",)
        )
        create_app(config, adapter=FakeRuntimeAdapter(), run_startup_tasks=False)


# ------------------------------------------------------------------ 哈希
def test_plaintext_code_is_never_stored_and_only_returned_once(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        issued = client.post("/control/admin/invitations", json={"label": "ops"}, headers=ADMIN)
        assert issued.status_code == 201
        code = issued.json()["code"]
        invitation_id = issued.json()["invitation"]["invitation_id"]

        stored = app.state.store.require_invitation(invitation_id)
        assert code not in repr(stored)
        assert code not in stored.code_hash

        listed = client.get("/control/admin/invitations", headers=ADMIN).json()
        assert code not in repr(listed)
        assert all("code" not in item for item in listed["items"])


# ------------------------------------------------------------------ 门禁
def test_invitation_only_requires_a_valid_code(tmp_path):
    app, _ = make_app(tmp_path, invitation_only=True)
    with TestClient(app) as client:
        response = client.post("/control/leases", json={"label": "x"})
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "invitation_required"

        # 显式但无效的 code 必须拒绝，不能静默降级为无码入场。
        response = client.post(
            "/control/leases", json={"invitation_code": generate_system_code()}
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "invalid_invitation"


def test_invitation_only_false_allows_code_less_trials(tmp_path):
    app, _ = make_app(tmp_path, invitation_only=False)
    with TestClient(app) as client:
        assert client.post("/control/leases", json={}).status_code == 201


def test_user_registration_does_not_consume_compute(tmp_path):
    app, _ = make_app(tmp_path, invitation_only=True)
    with TestClient(app) as client:
        assert client.post("/control/users").status_code == 201
        assert app.state.store.list_live_leases() == []


def test_valid_system_code_grants_only_server_side_priority(tmp_path):
    app, _ = make_app(tmp_path, invitation_only=True)
    with TestClient(app) as client:
        code = client.post(
            "/control/admin/invitations", json={"label": "ops"}, headers=ADMIN
        ).json()["code"]
        # 客户端伪造的 priority 字段必须被忽略。
        response = client.post(
            "/control/leases",
            json={"invitation_code": code, "priority": 9999},
        )
        assert response.status_code == 201
        lease = response.json()["lease"]
        assert lease["priority"] == 100


def test_resume_cannot_bypass_invitation_only(tmp_path):
    """旧凭据不能绕过开关：resume 也必须出示有效邀请码。"""

    app, _ = make_app(tmp_path, invitation_only=False)
    with TestClient(app) as client:
        first = client.post("/control/leases", json={}).json()
        assert first["resumed"] is False
        # 切到 invitation-only 后，同一个 cookie 不能再 resume。
        app.state.invitations.policy = AccessPolicy(invitation_only=True)
        again = client.post("/control/leases", json={})
        assert again.status_code == 403
        assert again.json()["error"]["code"] == "invitation_required"


# ------------------------------------------------------------------ 撤销
def test_revoked_system_code_is_rejected_and_marked_revoked(tmp_path):
    app, _ = make_app(tmp_path, invitation_only=True)
    with TestClient(app) as client:
        issued = client.post("/control/admin/invitations", json={}, headers=ADMIN).json()
        code = issued["code"]
        invitation_id = issued["invitation"]["invitation_id"]
        assert client.post("/control/leases", json={"invitation_code": code}).status_code == 201
        assert client.delete(f"/control/admin/invitations/{invitation_id}", headers=ADMIN).status_code == 200
        rejected = client.post("/control/leases", json={"invitation_code": code})
        assert rejected.status_code == 403
        assert app.state.store.require_invitation(invitation_id).state == InvitationState.REVOKED


def test_system_code_is_reusable_until_revoked(tmp_path):
    app, _ = make_app(tmp_path, slots=2, invitation_only=True)
    with TestClient(app) as client:
        code = client.post("/control/admin/invitations", json={}, headers=ADMIN).json()["code"]
        assert client.post("/control/leases", json={"invitation_code": code}).status_code == 201
        # 换一个浏览器上下文（无旧 cookie）仍可再次使用同一系统码。
        second = TestClient(app)
        try:
            response = second.post("/control/leases", json={"invitation_code": code})
            assert response.status_code == 201
        finally:
            second.close()


def test_deleting_a_system_code_does_not_delete_user_data(tmp_path):
    app, _ = make_app(tmp_path, invitation_only=True)
    with TestClient(app) as client:
        code = client.post("/control/admin/invitations", json={}, headers=ADMIN).json()["code"]
        user = client.post("/control/users").json()
        workspace = client.post("/control/workspaces", json={"title": "P"}).json()["workspace"]
        issued = client.post(
            "/control/leases", json={"workspace_id": workspace["workspace_id"], "invitation_code": code}
        ).json()
        volume = app.state.store.require_workspace(workspace["workspace_id"]).volume_name
        app.state.adapter.write_volume(volume, "keep.txt", b"data")
        invitation_id = client.get("/control/admin/invitations", headers=ADMIN).json()["items"][0]["invitation_id"]
        client.delete(f"/control/admin/invitations/{invitation_id}", headers=ADMIN)
        assert app.state.store.require_workspace(workspace["workspace_id"]).state == "active"
        assert app.state.adapter.read_volume(volume, "keep.txt") == b"data"


# --------------------------------------------------------- 配置种入
def test_configured_system_code_is_seeded_and_grants_priority(tmp_path):
    code = generate_system_code()
    app, _ = make_app(
        tmp_path, invitation_only=True, system_invitation_codes=(code,)
    )
    with TestClient(app) as client:
        assert client.post("/control/leases", json={"invitation_code": code}).status_code == 201
        assert app.state.store.list_live_leases()[0].priority == 100


def test_revoked_configured_code_is_not_resurrected_on_restart(tmp_path):
    code = generate_system_code()
    app, _ = make_app(tmp_path, invitation_only=True, system_invitation_codes=(code,))
    with TestClient(app) as client:
        invitation_id = client.get("/control/admin/invitations", headers=ADMIN).json()["items"][0]["invitation_id"]
        client.delete(f"/control/admin/invitations/{invitation_id}", headers=ADMIN)

    # 重启：配置里仍然有同一个码，但撤销 tombstone 必须继续生效。
    app2, _ = make_app(tmp_path, invitation_only=True, system_invitation_codes=(code,))
    with TestClient(app2) as client:
        rejected = client.post("/control/leases", json={"invitation_code": code})
        assert rejected.status_code == 403


# ------------------------------------------------------------------ 能力
def test_access_policy_is_public_and_does_not_leak_secrets(tmp_path):
    app, _ = make_app(tmp_path, invitation_only=True)
    with TestClient(app) as client:
        response = client.get("/control/access-policy")
        assert response.status_code == 200
        body = response.json()
        assert body == {
            "invitation_only": True,
            "system_invitation_supported": True,
            "project_sharing_supported": True,
        }
        assert "code" not in repr(body)


def test_validation_errors_never_echo_input_values(tmp_path):
    """校验失败不能把敏感输入（邀请码/恢复凭据）回显给客户端。"""

    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        secret_code = "S" * 400
        response = client.post("/control/leases", json={"invitation_code": secret_code})
        assert response.status_code == 422
        assert secret_code not in response.text
        for item in response.json()["error"]["details"]["errors"]:
            assert set(item) <= {"type", "loc", "msg"}

        secret_token = "R" * 400
        response = client.post(
            "/control/users/session",
            json={"user_id": "not-a-uuid", "recovery_token": secret_token},
        )
        assert response.status_code == 422
        assert secret_token not in response.text


def test_only_one_system_code_format_exists():
    """格式锁死：不存在第二种“兼容”系统码格式。"""

    import control_plane.invitations as invitations

    assert not hasattr(invitations, "CONFIG_COMPAT_SYSTEM_CODE_RE")
    # Crockford 分组形式不再被接受。
    assert not invitations.is_system_code_format("TLM-SYS-A1B2-C3D4-E5F6-G7H8")
    # 唯一合法格式仍然成立。
    assert invitations.is_system_code_format(generate_system_code())
