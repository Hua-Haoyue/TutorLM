"""健康与容量探针。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from control_plane.api.dependencies import get_allocator, get_config, get_store
from control_plane.api.schemas import CapacityResponse, HealthResponse
from control_plane.config import ControlPlaneConfig
from control_plane.domain.models import LeaseState, SlotState
from control_plane.store import ControlPlaneStore

router = APIRouter(tags=["health"])


def _capacity(store: ControlPlaneStore) -> dict:
    slots = store.list_slots()
    counts = {state: 0 for state in SlotState}
    for slot in slots:
        counts[slot.state] += 1
    live = store.list_live_leases()
    active_leases = sum(1 for l in live if l.state == LeaseState.ACTIVE)
    accepting = counts[SlotState.FREE] > 0
    return {
        "slots_total": len(slots),
        "slots_free": counts[SlotState.FREE],
        # 与验收清单的命名对齐（slots_ready == 可分配 == free）
        "slots_ready": counts[SlotState.FREE],
        "slots_starting": counts[SlotState.ALLOCATING],
        "slots_active": counts[SlotState.ACTIVE],
        "slots_draining": counts[SlotState.DRAINING],
        "slots_reclaiming": counts[SlotState.RECLAIMING],
        "slots_quarantined": counts[SlotState.QUARANTINED],
        "leases_live": len(live),
        "leases_active": active_leases,
        "accepting": accepting,
    }


@router.get("/control/health", summary="控制面自身存活（约定路径别名）")
def control_health(
    config: ControlPlaneConfig = Depends(get_config),
    store: ControlPlaneStore = Depends(get_store),
) -> dict:
    """与 ``/health`` 等价。

    验收清单把控制面存活约定为 ``GET /control/health``，这里保留两个路径，
    避免网关/前端按不同文档接错。它**只**代表控制面进程活着，不代表实例池
    可用；池子状态看 ``/control/capacity``。
    """

    capacity = _capacity(store)
    return {
        "status": "ok",
        "adapter": config.adapter,
        "slots_total": capacity["slots_total"],
        "slots_free": capacity["slots_free"],
    }


@router.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    """Lightweight orchestration liveness alias."""
    return {"status": "ok"}


@router.get("/health", response_model=HealthResponse, summary="存活探针")
def liveness(
    config: ControlPlaneConfig = Depends(get_config),
    store: ControlPlaneStore = Depends(get_store),
) -> dict:
    """只要进程能响应就返回 200；不触碰 Docker。"""

    capacity = _capacity(store)
    return {
        "status": "ok",
        "adapter": config.adapter,
        "slots_total": capacity["slots_total"],
        "slots_free": capacity["slots_free"],
    }


@router.get("/health/ready", summary="就绪探针")
def readiness(
    config: ControlPlaneConfig = Depends(get_config),
    store: ControlPlaneStore = Depends(get_store),
) -> dict:
    """控制面自身是否可对外服务。

    有意**不**在每次探测时 ping Docker：那会把 Docker 抖动放大成入口不可用。
    实例层面的可用性由分配时的 provision 与 ``/control/capacity`` 反映。
    未配置管理令牌属于"能跑但没人能管理"，因此标记 degraded。
    """

    capacity = _capacity(store)
    degraded_reasons = []
    if not config.is_admin_token_configured():
        degraded_reasons.append("管理令牌未配置，管理接口将拒绝所有请求")
    if capacity["slots_total"] < 1:
        degraded_reasons.append("没有配置任何槽位")
    status = "ok" if not degraded_reasons else "degraded"
    return {
        "status": status,
        "adapter": config.adapter,
        "degraded_reasons": degraded_reasons,
        **capacity,
    }


@router.get("/control/capacity", response_model=CapacityResponse, summary="容量概览")
def capacity(
    store: ControlPlaneStore = Depends(get_store),
    _allocator=Depends(get_allocator),
) -> dict:
    return _capacity(store)
