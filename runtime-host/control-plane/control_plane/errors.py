"""控制面错误类型。

每个错误都带 ``code``（稳定的机器可读标识）与 ``http_status``，
由 API 层统一映射成 ``{"error": {"code":..., "message":..., "details":...}}``，
与 instance-app 的错误信封保持一致，方便网关和后端共用一套错误解析。
"""

from __future__ import annotations

from typing import Any


class ControlPlaneError(Exception):
    """控制面错误基类。"""

    code = "control_plane_error"
    http_status = 500

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        http_status: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if http_status is not None:
            self.http_status = http_status
        self.details: dict[str, Any] = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


class ConfigError(ControlPlaneError):
    """配置非法；进程应拒绝启动而不是带着默认值继续跑。"""

    code = "config_invalid"
    http_status = 500


class CapacityError(ControlPlaneError):
    """固定槽位全部被占用，本次分配无法满足。"""

    code = "capacity_exhausted"
    http_status = 503


class LeaseNotFound(ControlPlaneError):
    """租约不存在、已释放或已过期。"""

    code = "lease_not_found"
    http_status = 404


class InvalidToken(ControlPlaneError):
    """访客令牌或管理令牌无效。"""

    code = "invalid_token"
    http_status = 401


class Forbidden(ControlPlaneError):
    """身份有效但无权访问该资源。"""

    code = "forbidden"
    http_status = 403


class SlotBusy(ControlPlaneError):
    """槽位正在被其他租约或生命周期操作占用。"""

    code = "slot_busy"
    http_status = 409


class SlotQuarantined(ControlPlaneError):
    """槽位处于隔离状态，必须人工确认后才能再次分配。"""

    code = "slot_quarantined"
    http_status = 503


class RuntimeUnavailable(ControlPlaneError):
    """Docker daemon 或实例运行时不可用。"""

    code = "runtime_unavailable"
    http_status = 503


class InstanceNotReady(ControlPlaneError):
    """实例容器已启动但未通过就绪探针。"""

    code = "instance_not_ready"
    http_status = 503


class NotFound(ControlPlaneError):
    code = "not_found"
    http_status = 404


class WorkspaceExpired(ControlPlaneError):
    code = 'workspace_expired'
    http_status = 410


class UntrustedUpstream(ControlPlaneError):
    """租约里的 upstream 与受控实例地址不一致。

    这是"数据被污染"的信号，不是普通的客户端错误。网关会把 admission 返回的
    地址当作转发目标，所以宁可拒绝服务也不能把流量导向未经控制的地址。
    """

    code = "untrusted_upstream"
    http_status = 500
class InvitationRequired(ControlPlaneError):
    """配置为仅邀请码准入，但本次申请没有提供有效邀请码。"""

    code = "invitation_required"
    http_status = 403


class InvalidInvitation(ControlPlaneError):
    """邀请码无效、被撤销、越权或已过期。

    对外一律使用同一个错误码，避免把"码是否存在"变成枚举 Oracle。
    """

    code = "invalid_invitation"
    http_status = 403


class InvitationRevoked(ControlPlaneError):
    code = "invitation_revoked"
    http_status = 403


class InvitationExpired(ControlPlaneError):
    code = "invitation_expired"
    http_status = 410


class InvitationConflict(ControlPlaneError):
    """邀请码操作与当前状态冲突（并发撤销、重复导入等）。"""

    code = "invitation_conflict"
    http_status = 409


class WorkspaceInUse(ControlPlaneError):
    """The workspace is open (a lease or slot uses it) or being copied."""

    code = "workspace_in_use"
    http_status = 409


class CopyInProgress(ControlPlaneError):
    """同一源项目/分享码的复制操作正在进行，不能重复启动。"""

    code = "copy_in_progress"
    http_status = 409


class WorkspaceCopyFailed(ControlPlaneError):
    """卷复制失败。目标项目不可用，源项目与其卷保持原状。"""

    code = "workspace_copy_failed"
    http_status = 502
class InvalidDataDirectory(ControlPlaneError):
    """项目存储路径不符合规范（必须是 '.' 或 UUID 专属子目录）。"""

    code = "invalid_data_directory"
    http_status = 500


class ShareSourceUnsupported(ControlPlaneError):
    """源项目未声明 v3 内容布局，卷内可能含应用 session 凭证，拒绝分享复制。"""

    code = "share_source_unsupported"
    http_status = 409
