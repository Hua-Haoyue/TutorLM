"""管理接口：槽位观测、强制回收、解除隔离、对账、收割。

全部要求 ``X-Admin-Token``（配置为空则一律 401，fail closed）。这些接口
不该暴露到公网；Nginx 只把 ``/control/*`` 里的租约部分放开，管理路径
限制在回环或运维网段。
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header

from control_plane.allocator import Allocator
from control_plane.api.dependencies import (
    get_allocator,
    get_config,
    get_lifecycle,
    get_store,
)
from control_plane.api.schemas import (
    AdminSlotResponse,
    GcResponse,
    ReconcileResponse,
    SlotListResponse,
)
from control_plane.config import ControlPlaneConfig
from control_plane.errors import Forbidden, InvalidToken
from control_plane.lifecycle import SlotLifecycle
from control_plane.store import ControlPlaneStore

router = APIRouter(prefix="/control/admin", tags=["admin"])


def require_admin(
    x_admin_token: str | None = Header(default=None),
    config: ControlPlaneConfig = Depends(get_config),
) -> None:
    """常数时间比较管理令牌；未配置则拒绝一切。"""

    expected = config.admin_token.strip()
    if not expected:
        raise InvalidToken("未配置管理令牌，管理接口已关闭")
    # 缺凭据是 401（未认证）；给了错误凭据是 403（已认证但无权）。
    if not x_admin_token:
        raise InvalidToken("缺少管理令牌")
    if not hmac.compare_digest(x_admin_token, expected):
        raise Forbidden("管理令牌无效")


@router.get(
    "/slots",
    response_model=SlotListResponse,
    dependencies=[Depends(require_admin)],
    summary="列出所有槽位",
)
def list_slots(store: ControlPlaneStore = Depends(get_store)) -> dict:
    return {"items": [slot.to_dict() for slot in store.list_slots()]}


@router.get(
    "/leases",
    dependencies=[Depends(require_admin)],
    summary="列出仍占用槽位的租约（含 slot/generation 归属）",
)
def list_leases(store: ControlPlaneStore = Depends(get_store)) -> dict:
    return {"items": [lease.to_dict() for lease in store.list_live_leases()]}


@router.get(
    "/events",
    dependencies=[Depends(require_admin)],
    summary="最近的控制面事件（运维排查用）",
)
def list_events(limit: int = 50, store: ControlPlaneStore = Depends(get_store)) -> dict:
    return {"items": store.recent_events(limit=max(1, min(limit, 500)))}


@router.post(
    "/slots/{slot_id}/reclaim",
    response_model=AdminSlotResponse,
    dependencies=[Depends(require_admin)],
    summary="强制回收某个槽位（停止 -> 删容器 -> 删卷 -> 新代次）",
)
def reclaim_slot(
    slot_id: int, allocator: Allocator = Depends(get_allocator)
) -> dict:
    return {"slot": allocator.force_reclaim_slot(slot_id).to_dict()}


@router.post(
    "/slots/{slot_id}/drain",
    response_model=AdminSlotResponse,
    dependencies=[Depends(require_admin)],
    summary="让槽位停止接收新任务",
)
def drain_slot(
    slot_id: int, lifecycle: SlotLifecycle = Depends(get_lifecycle)
) -> dict:
    return {"slot": lifecycle.begin_drain(slot_id).to_dict()}


@router.post(
    "/slots/{slot_id}/release-quarantine",
    response_model=AdminSlotResponse,
    dependencies=[Depends(require_admin)],
    summary="人工确认后解除隔离并彻底回收",
)
def release_quarantine(
    slot_id: int,
    allocator: Allocator = Depends(get_allocator),
    reason: str = "",
) -> dict:
    return {
        "slot": allocator.release_quarantine(slot_id, reason=reason).to_dict()
    }


@router.post(
    "/reconcile",
    response_model=ReconcileResponse,
    dependencies=[Depends(require_admin)],
    summary="与 Docker 实况对账（启动后或异常后调用）",
)
def reconcile(lifecycle: SlotLifecycle = Depends(get_lifecycle)) -> dict:
    return {"report": lifecycle.reconcile()}


@router.post(
    "/gc",
    response_model=GcResponse,
    dependencies=[Depends(require_admin)],
    summary="立即执行一次过期/撤销租约收割",
)
def gc(allocator: Allocator = Depends(get_allocator)) -> dict:
    return {"report": allocator.gc_once()}


@router.get(
    "/config",
    dependencies=[Depends(require_admin)],
    summary="控制面当前生效配置（不含任何令牌）",
)
def describe_config(config: ControlPlaneConfig = Depends(get_config)) -> dict:
    return config.describe()
