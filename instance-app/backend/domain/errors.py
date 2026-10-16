"""领域错误：稳定 ``code`` + ``http_status`` + 可选 ``details``。

与 ``docs/instance-app-api.md`` §通用约定 的错误信封一致：
``{"error":{"code":...,"message":...,"details":...}}``。HTTP 层（host/adapters）
只做映射，不重新定义码位。
"""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """应用领域错误基类。"""

    code = "internal_error"
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


class ConfigInvalid(DomainError):
    """``TUTORLM_*`` 配置缺失/非法：拒绝启动，而不是带着默认值继续跑。"""

    code = "config_invalid"
    http_status = 500


class StorageNotInitialized(DomainError):
    """卷被标记为已初始化但内容缺失/不一致：报错，不静默生成空卷冒充原项目。"""

    code = "storage_not_initialized"
    http_status = 500


class StorageLayoutError(DomainError):
    """数据根不可写或布局不可证明。"""

    code = "storage_layout_error"
    http_status = 500


class PathOutsideRoot(DomainError):
    """路径穿越/绝对路径/``..``：一律拒绝。"""

    code = "path_outside_root"
    http_status = 400


class SymlinkRejected(DomainError):
    """拒绝符号链接与非常规文件。"""

    code = "symlink_rejected"
    http_status = 400


class UnsupportedMedia(DomainError):
    """签名/扩展名/声明 mime 冲突或不可识别：拒绝，不按扩展名猜测。"""

    code = "unsupported_media"
    http_status = 400


class EncodingUnsupported(DomainError):
    """文本类解码失败：拒绝，不做有损替换。"""

    code = "encoding_unsupported"
    http_status = 400


class WorkspaceBindingConflict(DomainError):
    """owner 冲突等：fail closed，不盲写归属。"""

    code = "workspace_binding_conflict"
    http_status = 409


class WorkspaceRebindForbidden(DomainError):
    """疑似克隆但缺少可信迁移标识：fail closed。"""

    code = "workspace_rebind_forbidden"
    http_status = 409


class RevisionConflict(DomainError):
    """``PATCH`` 携带的旧 ``revision`` 与库内不一致。"""

    code = "revision_conflict"
    http_status = 409


class NotFound(DomainError):
    code = "not_found"
    http_status = 404


class Conflict(DomainError):
    code = "conflict"
    http_status = 409


class ImmutableContentError(DomainError):
    """试图就地改写不可变原件。"""

    code = "immutable_content"
    http_status = 409


class RevisionGone(DomainError):
    """引用绑定的 revision 已删除。"""

    code = "rev_gone"
    http_status = 410


class ArtifactStale(DomainError):
    """artifact/build 已过期或与 revision 不匹配。"""

    code = "artifact_stale"
    http_status = 409


class LimitsExceeded(DomainError):
    code = "limits_exceeded"
    http_status = 413


class RunNotRunning(DomainError):
    """run 已进入终态或被取消，写入结果必须丢弃。"""

    code = "run_not_running"
    http_status = 409


class ProviderUnavailable(DomainError):
    code = "provider_unavailable"
    http_status = 503


class InvalidInputError(DomainError):
    code = 'invalid_input'
    http_status = 422


NotFoundError = NotFound
ConflictError = Conflict
ProviderError = ProviderUnavailable


class TexCompileUnavailable(DomainError):
    """本仓库无安全编译执行器：默认不可用，不降级到不安全路径。"""

    code = "tex_compile_unavailable"
    http_status = 503
