"""就绪声明（``GET /health/ready`` 的 payload）：v1/v2/v3 字段形状。

非持久：``{"status":"ok","mode":...}``。
持久：**必须**含 ``storage_mode:"persistent"`` 与 ``workspace_id``；
只有 ``content_only`` 已证实时才附 ``storage_layout``，否则不返回该字段
（控制面按 ``legacy_root`` 记录，并拒绝作为分享源）。

控制面校验（``control_plane/lifecycle.py``）会拒绝：
``workspace_id`` 与注入值不一致、``storage_mode != persistent``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.domain.enums import StorageLayout

STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"

STORAGE_MODE_PERSISTENT = "persistent"
STORAGE_MODE_EPHEMERAL = "ephemeral"


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    """就绪判定结果；``ok`` 决定 HTTP 状态与 ``status`` 字段。"""

    ok: bool
    storage_mode: str
    workspace_id: str | None = None
    storage_layout: StorageLayout | None = None
    mode: str | None = None
    provider: dict[str, Any] | None = None
    reasons: tuple[str, ...] = ()
    details: dict[str, Any] | None = None

    @property
    def http_status(self) -> int:
        return 200 if self.ok else 503

    def to_payload(self) -> dict[str, Any]:
        """序列化为就绪 JSON。

        未 ``ok`` 时也**不**附 ``storage_layout``：绝不把未经证明的布局
        当作已声明的 content_only 发出去。
        """

        payload: dict[str, Any] = {"status": STATUS_OK if self.ok else STATUS_DEGRADED}
        if self.storage_mode:
            payload["storage_mode"] = self.storage_mode
        if self.workspace_id:
            payload["workspace_id"] = self.workspace_id
        if self.ok and self.storage_layout is not None:
            payload["storage_layout"] = str(self.storage_layout)
        if self.mode:
            payload["mode"] = self.mode
        if self.provider is not None:
            payload["provider"] = dict(self.provider)
        if self.reasons:
            payload["reasons"] = list(self.reasons)
        if self.details:
            payload["details"] = dict(self.details)
        return payload


def persistent_report(
    *,
    workspace_id: str,
    ok: bool,
    storage_layout: StorageLayout | None,
    provider: dict[str, Any] | None = None,
    reasons: tuple[str, ...] = (),
    details: dict[str, Any] | None = None,
) -> ReadinessReport:
    """持久模式的就绪报告。``storage_layout`` 只在 ``ok`` 时才可能被序列化。"""

    return ReadinessReport(
        ok=ok,
        storage_mode=STORAGE_MODE_PERSISTENT,
        workspace_id=workspace_id,
        storage_layout=storage_layout if ok else None,
        provider=provider,
        reasons=reasons,
        details=details,
    )


def ephemeral_report(
    *,
    mode: str,
    ok: bool = True,
    provider: dict[str, Any] | None = None,
    reasons: tuple[str, ...] = (),
) -> ReadinessReport:
    """非持久模式：不含任何 workspace 绑定字段。"""

    return ReadinessReport(
        ok=ok,
        storage_mode=STORAGE_MODE_EPHEMERAL,
        mode=mode,
        provider=provider,
        reasons=reasons,
    )
