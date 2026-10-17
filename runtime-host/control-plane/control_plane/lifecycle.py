"""槽位生命周期编排：create -> start -> ready -> (drain) -> stop -> reset。

这是控制面里最需要小心的一块。它把"容器操作"与"槽位状态"绑在同一处，
并且**任何一步失败都不允许槽位静默回到 FREE**——否则下一位访客会拿到
上一位的数据或一个半死的容器。失败路径统一走向
``SlotState.QUARANTINED``，必须显式 ``release_quarantine`` 才能复用。

状态机（与 domain.models.ALLOWED_TRANSITIONS 对应）：

    ABSENT -> CREATED -> STARTING -> READY -> STOPPING -> STOPPED -> REMOVED

就绪判定不只看容器 running：必须 ``GET /health/ready`` 返回 200 且
``status=ok``（contracts/instance-runtime-v1.md 的冻结约定）。
"""

from __future__ import annotations

import logging
import time
import threading
from dataclasses import dataclass
from functools import wraps
from typing import Any

from control_plane.config import ControlPlaneConfig
from control_plane.domain.models import (
    Lease,
    LeaseState,
    LEGACY_DATA_DIRECTORY,
    STORAGE_LAYOUT_CONTENT_ONLY,
    Slot,
    SlotState,
    WorkloadState,
    can_transition,
    workspace_data_directory,
    workspace_data_path,
)
from control_plane.errors import (
    ControlPlaneError,
    InstanceNotReady,
    RuntimeUnavailable,
    SlotBusy,
)
from control_plane.ports import InstanceSpec, RuntimeAdapter
from control_plane.store import ControlPlaneStore
from control_plane.security import app_bootstrap_token

logger = logging.getLogger(__name__)


def _slot_operation(operation):
    @wraps(operation)
    def guarded(self, target, *args, **kwargs):
        slot_id = target.slot_id if isinstance(target, Slot) else target
        with self._locks_guard:
            lock = self._slot_locks.setdefault(slot_id, threading.RLock())
        with lock:
            return operation(self, target, *args, **kwargs)
    return guarded


@dataclass(slots=True)
class ProvisionResult:
    """一次"把槽位送到 READY"尝试的结果。"""

    ok: bool
    slot: Slot
    upstream: str | None = None
    detail: str | None = None
    ready_payload: dict[str, Any] | None = None


class SlotLifecycle:
    """把裸的容器操作封装成可恢复的槽位生命周期。"""

    def __init__(
        self,
        config: ControlPlaneConfig,
        store: ControlPlaneStore,
        adapter: RuntimeAdapter,
    ) -> None:
        self._config = config
        self._store = store
        self._adapter = adapter
        self._locks_guard = threading.Lock()
        self._slot_locks: dict[int, Any] = {}
        #: 全局调度协调锁。入场抢占、卷复制与回收都在它下面做
        #: "选择受害者 -> 置换 -> 重分配"的组合动作，避免两个请求同时
        #: 抢同一个槽位。它不是 GC 不删卷的保证（那一点靠 DB pin）。
        self.scheduling_lock = threading.RLock()

    # ================================================================ helpers
    def _transition_workload(
        self, slot_id: int, target: WorkloadState, *, force: bool = False
    ) -> None:
        """按状态机推进 ``workload_state``。

        默认严格：非法迁移抛 :class:`SlotBusy`，因为 provision 路径上
        "状态不对"往往意味着有并发操作在抢同一个槽位。

        ``force=True`` 只用于**拆除**（reclaim/quarantine-release）：那时我们
        正在销毁资源，必须对"上一次失败留下的任意状态"保持幂等，而不是
        因为状态机较真而把槽位永久卡住。
        """

        slot = self._store.get_slot(slot_id)
        if slot is None:
            raise SlotBusy(f"槽位 {slot_id} 不存在")
        if not force and not can_transition(slot.workload_state, target):
            raise SlotBusy(
                f"槽位 {slot_id} 非法状态迁移：{slot.workload_state} -> {target}"
            )
        if slot.workload_state == target and not force:
            return
        self._store.update_slot(slot_id, workload_state=target)
        if not can_transition(slot.workload_state, target):
            logger.info(
                "槽位 %s 非标准迁移 %s -> %s（拆除路径）",
                slot_id,
                slot.workload_state,
                target,
            )

    def _quarantine(self, slot_id: int, reason: str) -> None:
        """隔离槽位。失败路径的唯一合法出口。"""

        logger.error("槽位 %s 进入隔离：%s", slot_id, reason)
        self._store.update_slot(
            slot_id,
            state=SlotState.QUARANTINED,
            quarantine_reason=reason[:500],
        )
        self._store.record_event(
            "slot.quarantined", slot_id=slot_id, payload={"reason": reason[:500]}
        )

    def _build_spec(self, slot: Slot, generation: int) -> InstanceSpec:
        config = self._config
        env = dict(config.instance_env)
        labels = config.labels()
        volume_name = config.volume_name(slot.slot_id)
        volume_must_exist = False
        lease = self._store.require_lease(slot.lease_id) if slot.lease_id else None
        if lease:
            env['TUTORLM_APP_BOOTSTRAP_TOKEN'] = app_bootstrap_token(config, self._store, lease)
            env['TUTORLM_SESSION_DATA_DIR'] = self._session_data_dir(slot.slot_id)
            env['TUTORLM_SESSION_TTL_SECONDS'] = str(max(1, int(lease.expires_at - self._store._clock())))
        if lease and lease.workspace_id:
            workspace = self._store.require_workspace(lease.workspace_id)
            volume_must_exist = bool(workspace.storage_initialized)
            volume_name = config.workspace_volume_name(workspace.workspace_id)
            if workspace.state != 'active' or workspace.volume_name != volume_name:
                raise RuntimeUnavailable('Workspace is unavailable or its volume is invalid')
            # 路径不能相信客户端或旧数据：从已持久化的 UUID 元数据重新推导并校验。
            data_directory = workspace_data_directory(workspace)
            data_path = workspace_data_path(config.instance_mount_path, data_directory)
            labels.update({'tutorlm.workspace_id': workspace.workspace_id, 'tutorlm.user_id': workspace.user_id})
            env.update({
                'TUTORLM_WORKSPACE_ID': workspace.workspace_id,
                'TUTORLM_OWNER_ID': workspace.user_id,
                'TUTORLM_PERSISTENT_WORKSPACE': 'true',
                # 容器内真实数据根（含项目子目录），与 mount_path 一致。
                'TUTORLM_DATA_DIR': data_path,
                'TUTORLM_WORKSPACE_SUBDIRECTORY': data_directory,
                # 应用 session/鉴权数据必须放在卷外临时目录，
                # 避免被卷复制带走成为接收者身份。
                'TUTORLM_SESSION_DATA_DIR': self._session_data_dir(slot.slot_id),
            })
            copies = [copy for copy in self._store.list_workspace_copies(states=('copied',))
                      if copy.destination_workspace_id == workspace.workspace_id]
            if workspace.source_workspace_id or copies:
                if len(copies) != 1 or (workspace.source_workspace_id and copies[0].source_workspace_id != workspace.source_workspace_id):
                    raise RuntimeUnavailable('Workspace copy provenance is missing or ambiguous')
                env['TUTORLM_WORKSPACE_SOURCE_ID'] = copies[0].source_workspace_id
                env['TUTORLM_WORKSPACE_COPY_ID'] = copies[0].copy_id
        return InstanceSpec(
            slot_id=slot.slot_id,
            generation=generation,
            container_name=config.container_name(slot.slot_id, generation),
            image=config.instance_image,
            volume_name=volume_name,
            mount_path=config.instance_mount_path,
            internal_port=config.instance_port,
            env=env,
            labels=labels,
            network=config.network,
            mem_limit=config.mem_limit,
            cpus=config.cpus,
            pids_limit=config.pids_limit,
            tmpfs_size=config.tmpfs_size,
            stop_grace_seconds=config.stop_grace_seconds,
            volume_must_exist=volume_must_exist,
        )

    def _session_data_dir(self, slot_id: int) -> str:
        """卷外的 session/鉴权目录。每个槽位一个临时路径，不会被卷复制。"""

        return f"/tmp/tutorlm-session/s{int(slot_id):02d}"

    def _wait_ready(self, upstream: str) -> tuple[bool, str | None, dict[str, Any] | None]:
        """轮询就绪探针。真实适配器有 ``wait_ready``，fake 只有 ``probe``。"""

        wait_ready = getattr(self._adapter, "wait_ready", None)
        if callable(wait_ready):
            result = wait_ready(
                upstream,
                timeout=self._config.ready_timeout_seconds,
                interval=self._config.ready_poll_interval_seconds,
            )
            return result.ready, result.detail, result.payload

        deadline = time.monotonic() + self._config.ready_timeout_seconds
        detail: str | None = "未开始探测"
        while True:
            result = self._adapter.probe(upstream)
            if result.ready:
                return True, None, result.payload
            detail = result.detail
            if time.monotonic() >= deadline:
                return False, detail, None
            time.sleep(self._config.ready_poll_interval_seconds)

    # ============================================================ provision
    @_slot_operation
    def provision(self, slot: Slot) -> ProvisionResult:
        """把已占位的槽位推进到 READY。

        调用前：``allocate`` 已把槽位置为 ``ALLOCATING`` 并绑定租约。
        成功：``ACTIVE`` + ``READY``，返回可用的 ``upstream``。
        失败：``QUARANTINED``，返回 ``ok=False``（不抛异常，让调用方决定
        租约是置 FAILED 还是保留重试）。
        """

        generation = slot.generation
        current = self._store.require_slot(slot.slot_id)
        if current.lease_id != slot.lease_id or current.generation != generation:
            return ProvisionResult(ok=False, slot=current, detail='Compute slot ownership changed before startup')
        try:
            spec = self._build_spec(slot, generation)
            self._store.update_slot(slot.slot_id, container_name=spec.container_name)
            self._adapter.create(spec)
            if spec.env.get('TUTORLM_WORKSPACE_ID'):
                self._store.mark_workspace_initialized(spec.env['TUTORLM_WORKSPACE_ID'])
            self._transition_workload(slot.slot_id, WorkloadState.CREATED)
            self._adapter.start(spec.container_name)
            self._transition_workload(slot.slot_id, WorkloadState.STARTING)
        except RuntimeUnavailable as exc:
            self._quarantine(slot.slot_id, f"创建/启动失败：{exc}")
            return ProvisionResult(ok=False, slot=self._store.get_slot(slot.slot_id), detail=str(exc))
        except ControlPlaneError as exc:
            self._quarantine(slot.slot_id, f"状态机错误：{exc}")
            return ProvisionResult(ok=False, slot=self._store.get_slot(slot.slot_id), detail=str(exc))

        upstream = f"http://{spec.container_name}:{self._config.instance_port}"
        ready, detail, payload = self._wait_ready(upstream)
        workspace_id = spec.env.get('TUTORLM_WORKSPACE_ID')
        if ready and workspace_id and (
            (payload or {}).get('workspace_id') != workspace_id
            or (payload or {}).get('storage_mode') != 'persistent'
        ):
            ready, detail = False, 'Application does not confirm the persistent workspace contract'
        if ready and workspace_id:
            # 应用可以在就绪声明中升级存储布局。只有声明 content_only
            # 后，项目才允许作为分享复制的源。未声明保持 legacy，
            # 因此旧项目不会被静默当成可分享。
            declared = str((payload or {}).get('storage_layout') or '').strip()
            if declared == STORAGE_LAYOUT_CONTENT_ONLY:
                self._store.mark_workspace_layout(workspace_id, STORAGE_LAYOUT_CONTENT_ONLY)
        if not ready:
            # 关键：未就绪绝不交给访客。停掉容器并隔离，避免半死实例被复用。
            self._best_effort_stop(spec.container_name)
            self._quarantine(slot.slot_id, f"就绪超时：{detail}")
            return ProvisionResult(
                ok=False, slot=self._store.get_slot(slot.slot_id), detail=detail
            )

        self._transition_workload(slot.slot_id, WorkloadState.READY)
        updated = self._store.update_slot(
            slot.slot_id,
            state=SlotState.ACTIVE,
            container_name=spec.container_name,
        )
        self._store.record_event(
            "slot.ready",
            lease_id=updated.lease_id,
            slot_id=slot.slot_id,
            payload={"generation": generation, "upstream": upstream},
        )
        return ProvisionResult(
            ok=True, slot=updated, upstream=upstream, ready_payload=payload
        )

    # =============================================================== drain
    def begin_drain(self, slot_id: int) -> Slot:
        """停止接收新任务。这里只改状态；等待活动任务由调用方负责。"""

        slot = self._store.get_slot(slot_id)
        if slot is None:
            raise SlotBusy(f"槽位 {slot_id} 不存在")
        if slot.state == SlotState.DRAINING:
            return slot
        if slot.state == SlotState.QUARANTINED:
            return slot
        updated = self._store.update_slot(slot_id, state=SlotState.DRAINING)
        self._store.record_event("slot.draining", slot_id=slot_id)
        return updated

    # ============================================================ teardown
    def _best_effort_stop(self, container_name: str | None) -> bool:
        """尽力停止；返回是否成功。绝不因为停止失败而抛异常掩盖原错误。"""

        if not container_name:
            return True
        try:
            self._adapter.stop(container_name, timeout=self._config.stop_grace_seconds)
            return True
        except Exception as exc:  # noqa: BLE001 - 失败要显式记录，不能吞掉
            logger.warning("停止容器 %s 失败：%s", container_name, exc)
            return False

    @_slot_operation
    def reclaim(self, slot_id: int, *, expected_lease_id: str | None = None) -> Slot:
        """Release compute; durable workspace volumes are reclaimed by storage GC.

        任何一步失败都隔离槽位并抛出，**不会**把槽位放回 FREE。
        持久项目仅释放容器；卷的删除由独立存储 GC 管理。
        """

        slot = self._store.get_slot(slot_id)
        if slot is None:
            raise SlotBusy(f"槽位 {slot_id} 不存在")

        container_name = slot.container_name
        if expected_lease_id is not None and slot.lease_id != expected_lease_id:
            raise SlotBusy('Lease no longer owns this compute slot')
        lease = self._store.require_lease(slot.lease_id) if slot.lease_id else None
        durable = lease is not None and lease.workspace_id is not None
        volume_name = self._config.volume_name(slot_id)
        self._store.update_slot(slot_id, state=SlotState.RECLAIMING)

        # 1) 停止（容器可能已被上一轮失败删掉，因此先看实况再动手）
        try:
            if container_name:
                status = self._adapter.status(container_name)
                if status.exists:
                    self._transition_workload(slot_id, WorkloadState.STOPPING, force=True)
                    self._adapter.stop(
                        container_name, timeout=self._config.stop_grace_seconds
                    )
                self._transition_workload(slot_id, WorkloadState.STOPPED, force=True)
        except Exception as exc:  # noqa: BLE001
            self._quarantine(slot_id, f"回收时停止失败：{exc}")
            raise RuntimeUnavailable(f"回收槽位 {slot_id} 失败：停止容器时出错") from exc

        # 2) 删除容器
        try:
            if container_name:
                self._adapter.remove(container_name)
            self._transition_workload(slot_id, WorkloadState.REMOVED, force=True)
        except Exception as exc:  # noqa: BLE001
            self._quarantine(slot_id, f"回收时删除容器失败：{exc}")
            raise RuntimeUnavailable(f"回收槽位 {slot_id} 失败：删除容器时出错") from exc

        # Anonymous volumes are ephemeral; durable workspace volumes survive compute teardown.
        try:
            if not durable:
                self._adapter.remove_volume(volume_name)
        except Exception as exc:  # noqa: BLE001
            self._quarantine(slot_id, f"回收时删除数据卷失败：{exc}")
            raise RuntimeUnavailable(f"回收槽位 {slot_id} 失败：删除数据卷时出错") from exc

        # 4) 新代次 + 回到 FREE（clear_lease 让旧租约无法再关联本槽位）
        next_generation = slot.generation + 1
        updated = self._store.update_slot(
            slot_id,
            state=SlotState.FREE,
            generation=next_generation,
            workload_state=WorkloadState.ABSENT,
            clear_lease=True,
            clear_quarantine=True,
        )
        # workload_state 被重置为 ABSENT，但上面的状态机仍记录在 events 里。
        self._store.record_event(
            "slot.reclaimed",
            slot_id=slot_id,
            payload={"generation": next_generation, "previous": slot.generation},
        )
        return updated

    @_slot_operation
    def release_quarantine(self, slot_id: int, *, reason: str = "") -> Slot:
        """人工确认后解除隔离并回收为新代次。

        必须真的把容器和卷清干净才允许回到 FREE；否则隔离只是摆设。
        """

        slot = self._store.get_slot(slot_id)
        if slot is None:
            raise SlotBusy(f"槽位 {slot_id} 不存在")
        if slot.state != SlotState.QUARANTINED:
            raise SlotBusy(f"槽位 {slot_id} 不在隔离状态（当前 {slot.state}）")
        self._store.record_event(
            "slot.quarantine_released",
            slot_id=slot_id,
            payload={"reason": reason},
        )
        # 只清隔离标记；workload_state 交给 reclaim 的 force 路径处理，
        # 这样无论上次失败停在哪个节点都能收敛到 FREE。
        self._store.update_slot(slot_id, clear_quarantine=True)
        return self.reclaim(slot_id)

    # =========================================================== reconcile
    def reconcile(self) -> dict[str, Any]:
        """进程重启后的对账：确认槽位记录的容器/卷与 Docker 实况一致。

        不做任何"猜测性修复"：只在实况明确时把槽位改回 FREE（例如容器
        已被人删掉，且租约已终止），其余一律隔离等人工确认。
        """

        report: dict[str, Any] = {"checked": 0, "freed": [], "quarantined": []}
        for slot in self._store.list_slots():
            report["checked"] += 1
            if slot.state in {SlotState.FREE, SlotState.QUARANTINED}:
                continue
            lease = self._store.get_lease_by_slot(slot.slot_id)
            if lease is None or lease.is_terminal:
                # 槽位被标记占用但没有有效租约 -> 状态不一致，隔离。
                self._quarantine(slot.slot_id, "槽位占用但找不到有效租约")
                report["quarantined"].append(slot.slot_id)
                continue
            if not slot.container_name:
                continue
            status = self._adapter.status(slot.container_name)
            if not status.exists:
                # 容器没了但租约还活着：无法保证数据隔离，隔离槽位。
                self._quarantine(
                    slot.slot_id, f"容器 {slot.container_name} 不存在，无法确认实例状态"
                )
                report["quarantined"].append(slot.slot_id)
        return report
