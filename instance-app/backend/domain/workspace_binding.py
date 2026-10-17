"""workspace access context：与业务内容解耦的**单行**访问上下文。

两类身份必须分离（设计 §4.5）：

- **业务 stable ID**（``project_id``/``document_id``/``node_id``/...）：跨克隆**原样保留**。
- **workspace access context**（``workspace_id``/``owner_id``/布局）：存于独立
  ``workspace_binding`` 行，克隆后**只重绑这一行**。

启动时执行的**确定验证算法**（全部输入取自控制面注入的 env，绝不取客户端）：

1. ``current_workspace_id == ws_dst`` -> 校验 ``owner_id``；一致即幂等打开，不一致 fail closed。
2. 不同（疑似克隆）-> 仅在**同时**满足"存在控制面注入的迁移标识、
   ``origin_workspace_id == TUTORLM_WORKSPACE_SOURCE_ID``、目标一致 ``ws_dst``
   （若提供 ``COPY_ID`` 则须与库内 ``copy_id`` 匹配）"时才重绑；
   在一个事务内更新为新 UUID + 新 owner，并写 ``workspace_rebound`` 事件。
3. 否则 fail closed：**不**盲写 owner，**不**把源内容当作目标 workspace 提供。

> 当前控制面**尚未注入** ``TUTORLM_WORKSPACE_SOURCE_ID`` / ``TUTORLM_WORKSPACE_COPY_ID``，
> 因此分享克隆的容器内端到端**尚未 ready**：缺标识时按第 3 步 fail closed，这是预期行为。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from backend.domain.enums import StorageLayout
from backend.domain.errors import (
    ConfigInvalid,
    WorkspaceBindingConflict,
    WorkspaceRebindForbidden,
)


class BindingAction(StrEnum):
    """启动绑定的判定结果。"""

    #: 绑定已存在且完全一致（幂等打开）。
    UNCHANGED = "unchanged"
    #: 库里没有绑定，且是首次初始化当前 workspace。
    CREATED = "created"
    #: 疑似克隆且满足全部可信条件：重绑访问上下文。
    REBOUND = "rebound"


@dataclass(frozen=True, slots=True)
class WorkspaceBinding:
    """``workspace_binding`` 行的领域表示。"""

    current_workspace_id: str
    origin_workspace_id: str
    owner_id: str
    updated_at: float
    copy_id: str | None = None
    storage_layout: StorageLayout = StorageLayout.LEGACY_ROOT

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "current_workspace_id": self.current_workspace_id,
            "origin_workspace_id": self.origin_workspace_id,
            "owner_id": self.owner_id,
            "storage_layout": str(self.storage_layout),
        }
        if self.copy_id is not None:
            payload["copy_id"] = self.copy_id
        return payload


@dataclass(frozen=True, slots=True)
class BindingDecision:
    """绑定判定的输出：动作 + 目标行（``CREATED``/``REBOUND`` 时由调用方持久化）。"""

    action: BindingAction
    binding: WorkspaceBinding
    reasons: tuple[str, ...] = ()

    @property
    def rebound(self) -> bool:
        return self.action is BindingAction.REBOUND


def decide_workspace_binding(
    existing: WorkspaceBinding | None,
    *,
    workspace_id: str,
    owner_id: str,
    source_workspace_id: str | None,
    copy_id: str | None,
    now: float,
    storage_layout: StorageLayout = StorageLayout.LEGACY_ROOT,
) -> BindingDecision:
    """执行设计 §4.5 的确定验证算法（纯函数，无 IO）。

    发起方负责在 ``CREATED``/``REBOUND`` 时**在同一事务**内写入绑定行
    （``REBOUND`` 时还要写 ``workspace_rebound`` 事件）。
    """

    if not workspace_id or not owner_id:
        raise ConfigInvalid("TUTORLM_WORKSPACE_ID / TUTORLM_OWNER_ID 缺失")

    if existing is None:
        return BindingDecision(
            action=BindingAction.CREATED,
            binding=WorkspaceBinding(
                current_workspace_id=workspace_id,
                origin_workspace_id=workspace_id,
                owner_id=owner_id,
                updated_at=now,
                copy_id=copy_id,
                storage_layout=storage_layout,
            ),
            reasons=("首次初始化：写入当前 workspace 的访问上下文",),
        )

    if existing.current_workspace_id == workspace_id:
        # 幂等打开；owner 必须一致，否则 fail closed（不盲写归属）。
        if existing.owner_id != owner_id:
            raise WorkspaceBindingConflict(
                "卷内 workspace 绑定的 owner 与 TUTORLM_OWNER_ID 不一致",
                details={
                    "workspace_id": workspace_id,
                    "bound_owner": existing.owner_id,
                    "injected_owner": owner_id,
                },
            )
        return BindingDecision(
            action=BindingAction.UNCHANGED,
            binding=existing,
            reasons=("workspace 与 owner 一致，幂等打开",),
        )

    # 疑似克隆：必须有控制面注入的可信迁移标识，逐项匹配后才允许重绑。
    reasons: list[str] = []
    if not source_workspace_id:
        reasons.append("缺少 TUTORLM_WORKSPACE_SOURCE_ID：无法证明复制血缘")
    elif existing.current_workspace_id != source_workspace_id:
        reasons.append("TUTORLM_WORKSPACE_SOURCE_ID 与库内 current_workspace_id 不匹配")
    if not copy_id:
        reasons.append("缺少 TUTORLM_WORKSPACE_COPY_ID：无法证明此次复制操作")

    if reasons:
        raise WorkspaceRebindForbidden(
            "疑似克隆但缺少可信迁移标识：fail closed，不盲写 owner",
            details={
                "current_workspace_id": existing.current_workspace_id,
                "injected_workspace_id": workspace_id,
                "reasons": reasons,
            },
        )

    return BindingDecision(
        action=BindingAction.REBOUND,
        binding=WorkspaceBinding(
            current_workspace_id=workspace_id,
            origin_workspace_id=existing.origin_workspace_id,
            owner_id=owner_id,
            updated_at=now,
            copy_id=copy_id if copy_id is not None else existing.copy_id,
            storage_layout=storage_layout,
        ),
        reasons=("控制面注入的迁移标识全部匹配：重绑访问上下文",),
    )
