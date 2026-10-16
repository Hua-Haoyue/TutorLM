"""控制面领域模型。

关键区分（见 contracts/instance-runtime-v1.md）：访客凭据、``lease_id``、
``slot_id``、``generation`` 与应用内部 session 是**四个不同概念**。
这里只用前四者；应用 session 由 instance-app 自己管理，控制面不伪造它。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from control_plane.errors import InvalidDataDirectory


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
    #: 正在释放计算；持久卷保留，临时卷清理，完成后回到 FREE。
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
    """A temporary compute slot; durable storage belongs to a workspace."""

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
    """A temporary compute lease, optionally bound to a durable workspace."""

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
    workspace_id: str | None = None
    #: 服务端判定的服务级别。绝不由客户端传入：只有已验证的最高级邀请码
    #: 才会产生 SYSTEM_PRIORITY 值。抢占时先比 priority，再比时间。
    priority: int = 0

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
            # 服务端判定的服务级别（只读，不可由客户端设置）。
            "priority": self.priority,
        }
        if self.label:
            payload["label"] = self.label
        if self.workspace_id:
            payload["workspace_id"] = self.workspace_id
        if self.failure_reason:
            payload["failure_reason"] = self.failure_reason
        return payload


#: 项目内容根目录的相对路径。``'.'`` 表示卷根（兼容 v2 旧项目）。
LEGACY_DATA_DIRECTORY = "."

#: 未声明存储布局的历史项目卷：不保证卷内没有应用
#: session/凭证，因此不允许作为分享复制的源。
STORAGE_LAYOUT_LEGACY_ROOT = "legacy_root"
#: 应用按 v3 契约声明：``data_directory`` 内只有项目业务内容，
#: 应用 session 与鉴权数据放在 ``TUTORLM_SESSION_DATA_DIR``（卷外临时目录）。
#: 只有这种项目才允许签发分享码。
STORAGE_LAYOUT_CONTENT_ONLY = "content_only"


@dataclass(frozen=True, slots=True)
class Workspace:
    workspace_id: str
    user_id: str
    title: str
    volume_name: str
    state: str
    created_at: float
    last_used_at: float
    deleted_at: float | None = None
    last_error: str | None = None
    storage_initialized: bool = False
    #: 项目内容根目录（相对卷根）。旧项目为 ``'.'``；
    #: 克隆目标为 UUID 专属子目录。
    data_directory: str = LEGACY_DATA_DIRECTORY
    #: 见 ``STORAGE_LAYOUT_*``。默认 legacy_root，必须由应用就绪声明升级。
    storage_layout: str = STORAGE_LAYOUT_LEGACY_ROOT
    #: 复制来源项目 UUID（普通创建为 ``None``）。
    source_workspace_id: str | None = None

    @property
    def is_share_safe_source(self) -> bool:
        """是否允许作为分享复制的源。

        只有显式声明 ``content_only`` 的项目才算"卷内无 session
        秘密"，否则拒绝签发分享码，而不是盲目整卷拷贝。
        """

        return self.storage_layout == STORAGE_LAYOUT_CONTENT_ONLY

    def to_dict(self, idle_seconds: int) -> dict[str, Any]:
        payload = {
            'workspace_id': self.workspace_id, 'user_id': self.user_id,
            'title': self.title, 'state': self.state,
            'created_at': self.created_at, 'last_used_at': self.last_used_at,
            'expires_at': self.last_used_at + idle_seconds,
            'data_directory': self.data_directory,
            'storage_layout': self.storage_layout,
        }
        if self.source_workspace_id:
            payload['source_workspace_id'] = self.source_workspace_id
        return payload


class InvitationKind(StrEnum):
    """邀请码用途。两类码永不互相提升权限。"""

    #: 系统码：``TLM-SYS-`` 前缀，可重复使用直到撤销，入场获得最高优先。
    SYSTEM = "system"
    #: 分享码：``TLM-SHARE-`` 前缀，绑定源项目，只授予普通 trial 能力。
    SHARE = "share"


class InvitationState(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class CopyState(StrEnum):
    """一次卷复制操作的状态机。DB 是唯一事实来源，GC 依赖它 pin 源卷。"""

    #: 已 pin 源项目、目标项目已建但尚未得到可用数据。
    PREPARING = "preparing"
    #: 复制成功，目标项目对接收者可见。
    COPIED = "copied"
    #: 复制失败，目标已回滚/标记不可用；源保持原状。
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Invitation:
    invitation_id: str
    kind: str
    code_prefix: str
    code_hash: str
    label: str
    workspace_id: str | None
    owner_user_id: str | None
    state: str
    created_at: float
    revoked_at: float | None = None
    expires_at: float | None = None
    max_uses: int | None = None
    use_count: int = 0
    last_used_at: float | None = None

    def is_expired(self, *, now: float) -> bool:
        return self.expires_at is not None and self.expires_at <= now

    def is_usable(self, *, now: float) -> bool:
        if self.state != InvitationState.ACTIVE:
            return False
        if self.is_expired(now=now):
            return False
        if self.max_uses is not None and self.use_count >= self.max_uses:
            return False
        return True

    def to_dict(self, *, now: float) -> dict[str, Any]:
        """对外序列化：绝不包含 ``code_hash`` 或任何明文片段。"""

        payload: dict[str, Any] = {
            'invitation_id': self.invitation_id,
            'kind': self.kind,
            'code_prefix': self.code_prefix,
            'label': self.label,
            'state': self.state,
            'created_at': self.created_at,
            'use_count': self.use_count,
        }
        if self.workspace_id:
            payload['workspace_id'] = self.workspace_id
        if self.owner_user_id:
            payload['owner_user_id'] = self.owner_user_id
        if self.expires_at is not None:
            payload['expires_at'] = self.expires_at
            payload['expired'] = self.is_expired(now=now)
        if self.revoked_at is not None:
            payload['revoked_at'] = self.revoked_at
        if self.last_used_at is not None:
            payload['last_used_at'] = self.last_used_at
        return payload


@dataclass(frozen=True, slots=True)
class WorkspaceCopy:
    """一次分享复制的持久化引用。

    在触碰容器之前先落库（``PREPARING``）并 ``source_pinned=1``，这样即使
    进程在同一瞬间被杀死，GC 也不会因为"看不到引用"而删掉源项目卷。
    """

    copy_id: str
    source_workspace_id: str
    destination_workspace_id: str
    recipient_user_id: str
    state: str
    created_at: float
    updated_at: float
    invitation_id: str | None = None
    #: 幂等键：同一用户 + 同一 key 只能产生一个副本。
    idempotency_key: str | None = None
    error: str | None = None

    @property
    def destination_ready(self) -> bool:
        return self.state == CopyState.COPIED

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            'copy_id': self.copy_id,
            'source_workspace_id': self.source_workspace_id,
            'destination_workspace_id': self.destination_workspace_id,
            'state': self.state,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }
        if self.invitation_id:
            payload['invitation_id'] = self.invitation_id
        if self.error:
            payload['error'] = self.error
        return payload


#: data_directory 只允许两种形态：旧项目的卷根 ``.``，或 UUID 专属子目录。
_DATA_DIRECTORY_RE = re.compile(
    r"^users/(?P<user>[0-9a-fA-F-]{36})/workspaces/(?P<workspace>[0-9a-fA-F-]{36})$"
)


def canonical_data_directory(user_id: str, workspace_id: str) -> str:
    """生成 UUID 专属目录：``users/<user>/workspaces/<workspace>``。

    只能由受信的 UUID 生成，绝不接受客户端传入的任意路径。
    """

    return f"users/{uuid.UUID(str(user_id))}/workspaces/{uuid.UUID(str(workspace_id))}"


def validate_data_directory(
    value: str | None,
    *,
    user_id: str | None = None,
    workspace_id: str | None = None,
) -> str:
    """校验存储路径必须是当前规范的标准形式。

    拒绝绝对路径、``..``、非 UUID 段以及非规范（大写/缺少连字符）的 UUID。
    存储层与生命周期层都要调用它，避免2 DB 被改成越权路径。
    """

    raw = (value or "").strip()
    if raw == LEGACY_DATA_DIRECTORY:
        return LEGACY_DATA_DIRECTORY
    match = _DATA_DIRECTORY_RE.match(raw)
    if match is None:
        raise InvalidDataDirectory(f"非法的项目存储路径：{raw!r}")
    canonical = canonical_data_directory(match.group("user"), match.group("workspace"))
    if raw != canonical:
        raise InvalidDataDirectory(f"项目存储路径必须是规范形式：{raw!r}")
    if user_id is not None and match.group("user") != str(uuid.UUID(str(user_id))):
        raise InvalidDataDirectory("项目存储路径与所属用户不一致")
    if workspace_id is not None and match.group("workspace") != str(uuid.UUID(str(workspace_id))):
        raise InvalidDataDirectory("项目存储路径与项目 UUID 不一致")
    return raw


def workspace_data_directory(workspace: "Workspace") -> str:
    """从已持久化的项目元数据推导并校验数据目录。"""

    return validate_data_directory(
        workspace.data_directory,
        user_id=workspace.user_id,
        workspace_id=workspace.workspace_id,
    )


def workspace_data_path(mount_path: str, data_directory: str) -> str:
    """容器内真实数据根：``mount_path`` 加上项目子目录。

    旧项目（``.``）直接用挂载点，保持 v2 行为不变。
    """

    base = mount_path.rstrip("/")
    if data_directory in ("", LEGACY_DATA_DIRECTORY):
        return base
    return f"{base}/{data_directory}"


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
