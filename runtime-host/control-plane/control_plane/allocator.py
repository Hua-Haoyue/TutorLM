"""分配器：把匿名访客变成"某个槽位上的一个租约"。

对外三件事：

* ``allocate``  ——  原子占位 + 启动容器 + 等就绪，返回明文令牌（仅此一次）；
* ``resolve``   ——  用令牌换取"这个请求应该转发到哪个 upstream"；
* ``release``   ——  主动结束试用，进入回收。

另外有 ``gc_once``：把过期/空闲的租约推进回收。**空闲判定同时看租约时间与
活动任务数**——只按 HTTP 访问次数关机是错的（见架构文档），所以这里
把"是否有活动任务"做成可注入的探针，由调用方提供实例的实现。
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from control_plane.config import ControlPlaneConfig
from control_plane.domain.models import (
    Lease,
    LeaseState,
    SlotState,
    is_lease_expired,
)
from control_plane.errors import (
    CapacityError,
    ControlPlaneError,
    InvalidToken,
    LeaseNotFound,
    RuntimeUnavailable,
)
from control_plane.lifecycle import SlotLifecycle
from control_plane.security import generate_token, hash_token, verify_token
from control_plane.store import ControlPlaneStore

logger = logging.getLogger(__name__)


class ActivityProbe(Protocol):
    """询问"该实例此刻还有没有正在跑的生成任务"。

    instance-app 现有的 ``GET /api/runs`` 是**会话级**的，不是全局空闲探针；
    在应用补上 ``/internal/status`` 之前，真实部署应使用保守实现
    （返回 ``True``，即视为一直有活动，从而不自动回收），交给人/管理接口
    显式释放。这样不会误杀正在生成的长任务。
    """

    def has_active_runs(self, upstream: str) -> bool: ...


class AlwaysBusyProbe:
    """保守默认：永远认为有活动任务，因此空闲回收不会自动触发。"""

    def has_active_runs(self, upstream: str) -> bool:  # noqa: ARG002
        return True


@dataclass(slots=True)
class AllocationResult:
    """分配成功的返回值。``token`` 只在这一次出现。"""

    lease: Lease
    token: str
    upstream: str
    ready_payload: dict[str, Any] | None = None


class Allocator:
    def __init__(
        self,
        config: ControlPlaneConfig,
        store: ControlPlaneStore,
        lifecycle: SlotLifecycle,
        *,
        activity_probe: ActivityProbe | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._store = store
        self._lifecycle = lifecycle
        self._activity = activity_probe or AlwaysBusyProbe()
        self._clock = clock

    # ------------------------------------------------------------- 分配
    def allocate(self, *, label: str = "") -> AllocationResult:
        """分配一个槽位并启动到就绪。

        失败时租约置 FAILED、槽位进入隔离，并把错误向上抛；调用方（HTTP 层）
        负责转成 503/429，绝不能返回一个不可用的 upstream。
        """

        lease_id = uuid.uuid4().hex
        token = generate_token()
        token_hash = hash_token(token, self._pepper())

        # 原子占位。容量不足会在这里抛 CapacityError，不会有半成品租约。
        lease = self._store.allocate(
            lease_id=lease_id,
            token_hash=token_hash,
            ttl_seconds=self._config.lease_ttl_seconds,
            label=label,
        )
        slot = self._store.require_slot(lease.slot_id)

        result = self._lifecycle.provision(slot)
        if not result.ok or not result.upstream:
            self._store.update_lease(
                lease_id,
                state=LeaseState.FAILED,
                failure_reason=result.detail or "启动失败",
            )
            raise RuntimeUnavailable(
                result.detail or "实例启动失败", details={"lease_id": lease_id}
            )

        updated = self._store.update_lease(
            lease_id,
            state=LeaseState.ACTIVE,
            upstream=result.upstream,
            last_seen_at=self._clock(),
        )
        self._store.record_event(
            "lease.active", lease_id=lease_id, slot_id=lease.slot_id
        )
        return AllocationResult(
            lease=updated,
            token=token,
            upstream=result.upstream,
            ready_payload=result.ready_payload,
        )

    def resume(self, token: str | None) -> Lease | None:
        """尝试把 ``token`` 解析成一个仍可复用的活跃租约（验收项 T1.2）。

        同一浏览器重复点击"申请试用"时，应当拿回**同一个租约**，而不是
        白占一个新槽位。只有 ``PENDING``/``ACTIVE`` 才复用：过期或已撤销的
        租约必须走正常分配流程，绝不能因为客户端还留着旧令牌就把旧工作区
        再交给他。

        无效令牌一律返回 ``None``（交给正常分配），不抛异常。
        """

        if not token:
            return None
        try:
            lease = self.authenticate(token)
        except ControlPlaneError:
            return None
        if lease.state not in {LeaseState.PENDING, LeaseState.ACTIVE}:
            return None
        self._store.touch_lease(lease.lease_id, at=self._clock())
        return lease

    def _pepper(self) -> str:
        """令牌盐：配置优先，否则持久化一个随机值（重启后仍能验证旧令牌）。"""

        configured = self._config.token_pepper.strip()
        if configured:
            return configured
        stored = self._store.get_meta("token_pepper")
        if stored:
            return stored
        generated = generate_token(24)
        self._store.set_meta("token_pepper", generated)
        return generated

    # ------------------------------------------------------------- 解析
    def authenticate(self, token: str | None) -> Lease:
        """用访客令牌解析出租约；无效/过期一律拒绝。"""

        if not token:
            raise InvalidToken("缺少访客令牌")
        pepper = self._pepper()
        now = self._clock()
        for lease in self._store.list_live_leases():
            if verify_token(token, lease.token_hash, pepper):
                if lease.state == LeaseState.EXPIRED:
                    raise InvalidToken("试用已过期")
                if lease.state == LeaseState.REVOKED:
                    raise InvalidToken("试用已结束")
                # TTL 必须在**请求时**生效，而不是等后台 GC 轮询才生效：
                # GC 默认 30 秒一轮，否则过期租约在这段窗口里仍能通过准入。
                # 这里只看绝对过期时间；空闲回收仍由 GC 结合活动探针决定。
                if lease.expires_at <= now:
                    raise InvalidToken("试用已过期")
                return lease
        raise InvalidToken("访客令牌无效")

    def resolve_target(self, token: str | None) -> tuple[Lease, str]:
        """返回 ``(lease, upstream)``，并刷新 ``last_seen_at``。

        只有 ACTIVE 且容器就绪的租约才可转发；否则抛 :class:`LeaseNotFound`
        或 :class:`ControlPlaneError`，避免把请求打到启动中/已回收的容器。
        """

        lease = self.authenticate(token)
        if not lease.is_serving:
            raise LeaseNotFound(f"租约 {lease.lease_id} 当前不可用（{lease.state}）")
        if not lease.upstream:
            raise LeaseNotFound("租约没有可用的上游实例")
        self._store.touch_lease(lease.lease_id, at=self._clock())
        return lease, lease.upstream

    # ------------------------------------------------------------- 释放
    def release(self, lease_id: str, *, reason: str = "") -> Lease:
        """撤销租约并回收槽位。幂等：已终止的租约直接返回。"""

        lease = self._store.require_lease(lease_id)
        if lease.is_terminal:
            return lease
        updated = self._store.update_lease(lease.lease_id, state=LeaseState.REVOKED)
        self._store.record_event(
            "lease.revoked",
            lease_id=lease.lease_id,
            slot_id=lease.slot_id,
            payload={"reason": reason},
        )
        return self._reclaim_lease(updated)

    def _reclaim_lease(self, lease: Lease) -> Lease:
        """执行槽位回收，并把租约终结为 RECLAIMED/FAILED。"""

        try:
            self._lifecycle.reclaim(lease.slot_id)
        except ControlPlaneError as exc:
            logger.error("回收租约 %s 失败：%s", lease.lease_id, exc)
            return self._store.update_lease(
                lease.lease_id,
                state=LeaseState.FAILED,
                failure_reason=f"回收失败：{exc}",
            )
        return self._store.update_lease(lease.lease_id, state=LeaseState.RECLAIMED)

    # --------------------------------------------------------- 管理操作
    def force_reclaim_slot(self, slot_id: int):
        """管理员强制回收槽位。

        **必须先终结该槽位上的租约再回收槽位**。否则会出现一个真实的安全
        漏洞：槽位被清空并重新分配给了新访客，而旧访客的令牌仍然是 ACTIVE
        且能通过 ``authenticate``——旧令牌会被解析到同一个槽位。
        回收本身（停容器/删卷/新代次）由 lifecycle 负责。
        """

        lease = self._store.get_lease_by_slot(slot_id)
        if lease is not None and not lease.is_terminal:
            self._store.update_lease(
                lease.lease_id,
                state=LeaseState.REVOKED,
                failure_reason="admin_force_reclaim",
            )
            self._store.record_event(
                "lease.admin_revoked", lease_id=lease.lease_id, slot_id=slot_id
            )
        slot = self._lifecycle.reclaim(slot_id)
        if lease is not None and not lease.is_terminal:
            self._store.update_lease(lease.lease_id, state=LeaseState.RECLAIMED)
        return slot

    def release_quarantine(self, slot_id: int, *, reason: str = ""):
        """管理员解除隔离；同样先终结租约，避免旧令牌复活。"""

        lease = self._store.get_lease_by_slot(slot_id)
        if lease is not None and not lease.is_terminal:
            self._store.update_lease(
                lease.lease_id,
                state=LeaseState.REVOKED,
                failure_reason="admin_release_quarantine",
            )
        slot = self._lifecycle.release_quarantine(slot_id, reason=reason)
        if lease is not None and not lease.is_terminal:
            self._store.update_lease(lease.lease_id, state=LeaseState.RECLAIMED)
        return slot

    # ------------------------------------------------------------- 收割
    def gc_once(self) -> dict[str, Any]:
        """扫一遍：过期租约与已撤销租约推进回收。

        空闲回收需要 ``reclaim_on_idle`` 且活动探针返回"无任务"。
        默认探针是保守的 AlwaysBusyProbe，所以默认不会因空闲而关机。
        """

        now = self._clock()
        report: dict[str, Any] = {"expired": [], "reclaimed": [], "failed": [], "skipped_busy": []}
        for lease in self._store.list_live_leases():
            expired = is_lease_expired(
                lease, now=now, idle_ttl_seconds=self._config.lease_idle_ttl_seconds
            )
            absolute_only = lease.expires_at <= now
            if not expired:
                continue
            # 空闲回收必须确认没有活动任务，避免打断长生成。
            if not absolute_only and lease.upstream:
                try:
                    if self._activity.has_active_runs(lease.upstream):
                        report["skipped_busy"].append(lease.lease_id)
                        continue
                except Exception as exc:  # noqa: BLE001 - 探针失败视为忙
                    logger.warning("活动探针失败，按忙处理：%s", exc)
                    report["skipped_busy"].append(lease.lease_id)
                    continue

            if lease.state in {LeaseState.PENDING, LeaseState.ACTIVE}:
                self._store.update_lease(lease.lease_id, state=LeaseState.EXPIRED)
                report["expired"].append(lease.lease_id)
                self._store.record_event(
                    "lease.expired", lease_id=lease.lease_id, slot_id=lease.slot_id
                )

            current = self._store.require_lease(lease.lease_id)
            if not current.is_terminal:
                final = self._reclaim_lease(current)
                key = "reclaimed" if final.state == LeaseState.RECLAIMED else "failed"
                report[key].append(lease.lease_id)
        return report

    def shutdown(self) -> None:
        """进程退出前的清理：把仍活着的租约交给回收流程。

        不删除租约记录，因此重启后 ``reconcile`` 仍能看到历史并做出正确判断。
        """

        try:
            self.gc_once()
        except Exception as exc:  # noqa: BLE001
            logger.warning("关闭时的收割失败：%s", exc)
