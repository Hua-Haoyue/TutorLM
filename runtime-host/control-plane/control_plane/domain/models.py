"""控制面领域模型。

关键区分（见 contracts/instance-runtime-v1.md）：访客凭据、``lease_id``、
``slot_id``、``generation`` 与应用内部 session 是**四个不同概念**。
这里只用前四者；应用 session 由 instance-app 自己管理，控制面不伪造它。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class SlotState(StrEnum):
    """槽位（固定容量的调度单位）状态。"""

    #: 空闲，可分配。
    FREE = "free"
    #: 已选中并正在创建/启动容器，尚未对访客开放。
    ALLOCATING = "allocating"
    #: 已就绪，正被某个租约使用。
    ACTIVE = "active"
    #: 租约已撤销，正在等待活动任务结束（拒绝新任务）。
    DRAINING = "draining"
    #: 正在停止容器并清理数据卷，完成后回到 FREE。
    RECLAIMING = "reclaiming"
    #: 生命周期操作失败，必须人工确认后才能重新分配。
    QUARANTINED = "quarantined"


#: 允许分配新租约的槽位状态。
ALLOCATABLE_STATES: frozenset[SlotState] = frozenset({SlotState.FREE})


class LeaseState(StrEnum):
    """访客租约状态。"""

    #: 已签发、容器正在启动；此时尚不能转发业务请求。
    PENDING = "pending"
    #: 实例就绪，可正常使用。
    ACTIVE = "active"
    #: 已达绝对 TTL（或空闲阈值），等待回收。
    EXPIRED = "expired"
    #: 访客主动结束或管理员撤销，等待回收。
    REVOKED = "revoked"
    #: 槽位已完成清理，租约终止。
    RECLAIMED = "reclaimed"
    #: 分配或启动失败，租约终止。
    FAILED = "failed"


#: 仍在占用槽位的租约状态。
LIVE_LEASE_STATES: frozenset[LeaseState] = frozenset(
    {LeaseState.PENDING, LeaseState.ACTIVE, LeaseState.EXPIRED, LeaseState.REVOKED}
)

#: 已终止、不再占用槽位的租约状态。
TERMINAL_LEASE_STATES: frozenset[LeaseState] = frozenset(
    {LeaseState.RECLAIMED, LeaseState.FAILED}
)

#: 可以接受业务流量的租约状态。
SERVING_LEASE_STATES: frozenset[LeaseState] = frozenset({LeaseState.ACTIVE})


class WorkloadState(StrEnum):
    """容器生命周期状态机的显式节点。"""

    ABSENT = "absent"
    CREATED = "created"
    STARTING = "starting"
    READY = "ready"
    STOPPING = "stopping"
    STOPPED = "stopped"
    REMOVED = "removed"
    FAILED = "failed"


#: 合法迁移：``from -> {to, ...}``。任何未列出的迁移都必须被拒绝，
#: 避免"停止失败但当作没事发生"这类把脏槽位发给下一位访客的错误。
ALLOWED_TRANSITIONS: dict[WorkloadState, frozenset[WorkloadState]] = {
    WorkloadState.ABSENT: frozenset({WorkloadState.CREATED, WorkloadState.FAILED}),
    WorkloadState.CREATED: frozenset(
        {WorkloadState.STARTING, WorkloadState.STOPPED, WorkloadState.REMOVED, WorkloadState.FAILED}
    ),
    WorkloadState.STARTING: frozenset(
        {WorkloadState.READY, WorkloadState.STOPPING, WorkloadState.REMOVED, WorkloadState.FAILED}
    ),
    WorkloadState.READY: frozenset(
        {WorkloadState.STOPPING, WorkloadState.REMOVED, WorkloadState.FAILED}
    ),
    WorkloadState.STOPPING: frozenset(
        {WorkloadState.STOPPED, WorkloadState.READY, WorkloadState.REMOVED, WorkloadState.FAILED}
    ),
    WorkloadState.STOPPED: frozenset({WorkloadState.REMOVED, WorkloadState.STARTING, WorkloadState.FAILED}),
    WorkloadState.REMOVED: frozenset(),
    WorkloadState.FAILED: frozenset({WorkloadState.REMOVED}),
}


def can_transition(current: WorkloadState, target: WorkloadState) -> bool:
    """状态迁移是否合法。同状态视为幂等，返回 ``True``。"""

    if current == target:
        return True
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


@dataclass(slots=True)
class Slot:
    """固定池中的一个槽位，对应一个专属数据卷。"""

    slot_id: int
    state: SlotState = SlotState.FREE
    #: 单调递增的代次；每次回收 +1，用来让旧凭据/旧容器名彻底作废。
    generation: int = 0
    #: 当前容器名（``generation`` 决定）；无容器时为 ``None``。
    container_name: str | None = None
    #: 当前租约 id。
    lease_id: str | None = None
    #: 工作负载状态机节点。
    workload_state: WorkloadState = WorkloadState.ABSENT
    #: 隔离原因，仅当 ``state == QUARANTINED`` 时有值。
    quarantine_reason: str | None = None
    updated_at: float = 0.0

    @property
    def is_allocatable(self) -> bool:
        return self.state in ALLOCATABLE_STATES and self.quarantine_reason is None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "slot_id": self.slot_id,
            "state": str(self.state),
            "generation": self.generation,
            "workload_state": str(self.workload_state),
            "updated_at": self.updated_at,
        }
        if self.container_name:
            payload["container_name"] = self.container_name
        if self.lease_id:
            payload["lease_id"] = self.lease_id
        if self.quarantine_reason:
            payload["quarantine_reason"] = self.quarantine_reason
        return payload


@dataclass(slots=True)
class Lease:
    """一次匿名访客试用。"""

    lease_id: str
    slot_id: int
    generation: int
    state: LeaseState = LeaseState.PENDING
    #: 访客令牌的哈希（绝不存明文）。
    token_hash: str = ""
    #: 容器内网地址（``http://<container>:8000``），仅控制面使用。
    upstream: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    #: 绝对过期时间。
    expires_at: float = 0.0
    #: 最近一次业务访问时间，空闲回收依据之一。
    last_seen_at: float = 0.0
    #: 对方可见的标签，仅用于运维排查，不参与鉴权。
    label: str = ""
    #: 失败原因（``state == FAILED`` 时）。
    failure_reason: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_LEASE_STATES

    @property
    def is_live(self) -> bool:
        return self.state in LIVE_LEASE_STATES

    @property
    def is_serving(self) -> bool:
        return self.state in SERVING_LEASE_STATES

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.expires_at - _now())

    def to_dict(self, *, now: float | None = None) -> dict[str, Any]:
        """对外序列化：**不含** ``token_hash`` 与 ``upstream``。"""

        moment = _now() if now is None else now
        payload: dict[str, Any] = {
            "lease_id": self.lease_id,
            "slot_id": self.slot_id,
            "generation": self.generation,
            "state": str(self.state),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "remaining_seconds": max(0.0, self.expires_at - moment),
            "last_seen_at": self.last_seen_at,
        }
        if self.label:
            payload["label"] = self.label
        if self.failure_reason:
            payload["failure_reason"] = self.failure_reason
        return payload


def _now() -> float:
    import time

    return time.time()


def is_lease_expired(lease: Lease, *, now: float, idle_ttl_seconds: int = 0) -> bool:
    """租约是否应当进入回收流程。

    绝对 TTL 到期即回收；空闲回收只在 ``idle_ttl_seconds > 0`` 时启用。
    调用方必须先确认没有活动任务（见 lifecycle 的 drain 流程），
    这里的判定只看时间，不做任务查询——那是控制面的职责，不是纯函数的。
    """

    if lease.is_terminal:
        return False
    if lease.expires_at <= now:
        return True
    if idle_ttl_seconds > 0:
        reference = max(lease.last_seen_at, lease.created_at)
        if now - reference >= idle_ttl_seconds:
            return True
    return False
