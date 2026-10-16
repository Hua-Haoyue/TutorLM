"""项目分享：导入分享码、卷复制、回滚与对账。

这里的每一步都在回答同一个问题：**如果进程在任意一刻被杀死，源项目
和接收者分别会变成什么样子**。因此所有不可逆操作之前都先落库，并且
``workspace_copy_operations`` 中的 ``source_pinned`` 是 GC 不删源卷的唯一依据（不依赖任何
Python 锁）。

安全不变量：

* 只接受**已验证**的源/目标卷（名字由 DB 与配置推导），不接受任意 path；
* 目标卷必须全新，源卷必须 ``must_exist`` 且标签准确；
* 复制失败：目标回滚/标记不可用，源不受影响；失败的副本**不会**
  进入 active 可见；
* 只复制宣明为 ``content_only`` 的项目内容子目录，不整卷盲拷（避免拷贝应用
  session/凭证）。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from control_plane.config import ControlPlaneConfig
from control_plane.domain.models import (
    CopyState,
    LEGACY_DATA_DIRECTORY,
    LeaseState,
    STORAGE_LAYOUT_CONTENT_ONLY,
    Workspace,
    WorkspaceCopy,
    canonical_data_directory,
    workspace_data_directory,
)
from control_plane.errors import (
    CopyInProgress,
    Forbidden,
    InvalidInvitation,
    RuntimeUnavailable,
    ShareSourceUnsupported,
    WorkspaceCopyFailed,
    WorkspaceExpired,
)
from control_plane.invitations import InvitationService
from control_plane.lifecycle import SlotLifecycle
from control_plane.ports import RuntimeAdapter, VolumeCopySpec, VolumeSpec
from control_plane.store import ControlPlaneStore

logger = logging.getLogger(__name__)

#: 目标项目在复制完成前的状态。它不是 active，因此不可见、不可打开。
COPYING_STATE = "copying"

#: 按 basename 在任意深度排除的会话/鉴权目录（传给 ``exclude_names``）。
#: 这不替代应用侧 rebind：容器仍必须忽略旧 session 身份。
SESSION_SUBTREES: tuple[str, ...] = (
    "sessions",
    "session",
    "auth",
    "credentials",
    "tokens",
)


@dataclass(slots=True)
class ImportResult:
    workspace: Workspace
    copy: WorkspaceCopy
    #: 本次调用是否命中已有副本（幂等或默认去重）。
    reused: bool = False


class ShareService:
    """分享导入（卷复制）的唯一写入方。"""

    def __init__(
        self,
        config: ControlPlaneConfig,
        store: ControlPlaneStore,
        lifecycle: SlotLifecycle,
        adapter: RuntimeAdapter,
        invitations: InvitationService,
    ) -> None:
        self._config = config
        self._store = store
        self._lifecycle = lifecycle
        self._adapter = adapter
        self._invitations = invitations

    # ------------------------------------------------------------- 内部
    @property
    def _scheduling_lock(self):
        """与分配/抢占共用的全局调度协调锁。

        它只是短程串行化手段；真正保证 GC 不删源卷的是 DB 里的 pin。
        """

        return self._lifecycle.scheduling_lock

    def _pepper(self) -> str:
        from control_plane.security import generate_token

        return self._config.token_pepper.strip() or self._store.get_or_create_meta(
            'token_pepper', generate_token(24)
        )

    def _source_volume_spec(self, source: Workspace) -> VolumeSpec:
        labels = dict(self._config.labels())
        labels.update(
            {
                'tutorlm.workspace_id': source.workspace_id,
                'tutorlm.user_id': source.user_id,
            }
        )
        return VolumeSpec(name=source.volume_name, labels=labels, must_exist=True)

    def _destination_volume_spec(self, destination: Workspace) -> VolumeSpec:
        labels = dict(self._config.labels())
        labels.update(
            {
                'tutorlm.workspace_id': destination.workspace_id,
                'tutorlm.user_id': destination.user_id,
            }
        )
        # must_exist=False + 日志校验：目标必须全新。
        return VolumeSpec(name=destination.volume_name, labels=labels, must_exist=False)

    # ------------------------------------------------------------ 权限
    @property
    def workspace_idle_seconds(self) -> int:
        return int(self._config.workspace_idle_ttl_seconds)

    def require_shareable_workspace(self, workspace_id: str, user_id: str) -> Workspace:
        """只有项目所有者能为自己的项目操作分享码。"""

        workspace = self._store.owned_workspace(
            workspace_id, user_id, self.workspace_idle_seconds
        )
        return workspace

    # ------------------------------------------------------------- 导入
    def import_share(
        self,
        *,
        user_id: str,
        code: str,
        title: str | None = None,
        idempotency_key: str | None = None,
    ) -> ImportResult:
        """导入一个分享码，得到一个独立副本。

        幂等优先级：先看 ``Idempotency-Key``，再看（同用户 + 同源项目）已有副本。
        这样，前端在丢失响应后重试不会重复拷贝。
        """

        if not user_id:
            raise Forbidden("需要用户身份才能导入分享项目")
        if self._store.get_user(user_id) is None:
            raise Forbidden("用户身份无效")
        key = (idempotency_key or "").strip() or None
        if key is not None and len(key) > 128:
            raise InvalidInvitation("幂等键过长")

        with self._scheduling_lock:
            if key is not None:
                existing = self._store.copy_for_idempotency_key(user_id, key)
                if existing is not None:
                    return self._resume_existing(existing, user_id)

            invitation = self._invitations.verify_share(code)
            source = self._store.require_workspace(invitation.workspace_id or "")
            if source.storage_layout != STORAGE_LAYOUT_CONTENT_ONLY:
                # 防御性复查：当初发码时的布局判断已经改变就拒绝。
                raise ShareSourceUnsupported(
                    "源项目当前未声明 v3 内容存储布局，拒绝复制",
                    details={"workspace_id": source.workspace_id},
                )
            source_data_directory = workspace_data_directory(source)

            if key is None:
                # 默认去重：同一用户对同一源项目的重复导入返回已有副本。
                existing = self._find_existing_copy(user_id, source.workspace_id)
                if existing is not None:
                    return self._resume_existing(existing, user_id)

            # 关键顺序：**先 pin 源项目**（落库 + source_pinned=1），再停源计算。
            # 否则在 _stop_source_compute 与 begin_share_copy 之间，GC 可以因为
            # 看不到引用而删掉源卷，造成真实数据丢失。
            copy = self._begin_copy(
                source=source,
                user_id=user_id,
                title=title,
                invitation_id=invitation.invitation_id,
                idempotency_key=key,
            )
            destination = self._store.require_workspace(copy.destination_workspace_id)
            self._store.record_event(
                'workspace.copy_started',
                payload={'source': source.workspace_id, 'destination': destination.workspace_id},
            )
            try:
                # 此刻源已被 pin：即使停源失败，GC 也不会删源卷。
                self._stop_source_compute(source)
                self._copy_volume(source, destination, source_data_directory)
            except Exception as exc:  # noqa: BLE001 - 失败路径必须不伤源
                self._rollback(destination, copy, exc)
                raise
            return self._finalize(destination, copy, invitation.invitation_id)

    # ------------------------------------------------------------ 幂等
    def _find_existing_copy(self, user_id: str, source_workspace_id: str) -> WorkspaceCopy | None:
        for copy in self._store.list_workspace_copies(states=(str(CopyState.COPIED),)):
            if copy.recipient_user_id == user_id and copy.source_workspace_id == source_workspace_id:
                return copy
        return None

    def _resume_existing(self, copy: WorkspaceCopy, user_id: str) -> ImportResult:
        """将一次既有复制作为幂等结果返回。

        **必须重新校验目标项目的当前状态**：复制完成后目标仍可能
        已经过期被 GC 删除。此时返回一个已删对象（或假装成 reused）
        都是错的；应当告知调用方副本已不可用。
        """

        if copy.recipient_user_id != user_id:
            raise Forbidden("该导入操作属于其他用户")
        if copy.state == str(CopyState.PREPARING):
            raise CopyInProgress(
                "复制仍在进行中",
                details={'copy_id': copy.copy_id, 'destination_workspace_id': copy.destination_workspace_id},
            )
        if copy.state != str(CopyState.COPIED):
            raise WorkspaceCopyFailed("此前的复制已失败，请重新导入")
        workspace = self._store.get_workspace(copy.destination_workspace_id)
        if workspace is None or workspace.state != 'active':
            # 目标已被删除/回收：不能 reused。告诉对方需重新导入。
            raise WorkspaceExpired("已导入的副本已不可用，请重新导入")
        if workspace.user_id != user_id:
            raise Forbidden("该副本不属于当前用户")
        # 同样校验项目的存储路径仍然合法。
        workspace_data_directory(workspace)
        return ImportResult(workspace=workspace, copy=copy, reused=True)

    # ------------------------------------------------------------ 占位
    def _begin_copy(
        self,
        *,
        source: Workspace,
        user_id: str,
        title: str | None,
        invitation_id: str | None,
        idempotency_key: str | None,
    ) -> WorkspaceCopy:
        """在做任何可观察副作用之前，先把源与目标在**同一事务**里落库并 pin。

        这是整条路径上唯一能阻止“GC 在我们准备的空隙里删源卷”的动作，
        因此它必须发生在 ``_stop_source_compute`` 之前。
        """

        destination_id = str(uuid.uuid4())
        destination_data_directory = canonical_data_directory(user_id, destination_id)
        return self._store.begin_share_copy(
            copy_id=uuid.uuid4().hex,
            source_workspace_id=source.workspace_id,
            destination_workspace_id=destination_id,
            recipient_user_id=user_id,
            destination_volume_name=self._config.workspace_volume_name(destination_id),
            title=self._destination_title(title),
            invitation_id=invitation_id,
            idempotency_key=idempotency_key,
            data_directory=destination_data_directory,
        )

    # -------------------------------------------------------- 源准备
    def _stop_source_compute(self, source: Workspace) -> None:
        """卷复制前先停掉源项目的计算，保证卷内 SQLite 不在写入。

        这里只释放计算；源项目与其卷完整保留。调用方必须**先**
        ``_begin_copy``（已 pin 源）再调用本方法，否则 GC 会在中间窗口删源卷。
        """

        lease = self._store.live_lease_for_workspace(source.workspace_id)
        if lease is not None and not lease.is_terminal:
            self._store.update_lease(
                lease.lease_id, state=LeaseState.REVOKED, failure_reason='share_copy_quiesce'
            )
            try:
                self._lifecycle.reclaim(lease.slot_id, expected_lease_id=lease.lease_id)
            except Exception as exc:  # noqa: BLE001 - 停不下就不能拷
                raise RuntimeUnavailable(
                    "无法安全停下源项目计算，已拒绝复制（源项目保留）"
                ) from exc
            self._store.update_lease(lease.lease_id, state=LeaseState.RECLAIMED)

    # ------------------------------------------------------------ 目标
    @staticmethod
    def _destination_title(title: str | None) -> str:
        cleaned = (title or "").strip()[:200]
        return cleaned or "导入的项目"

    def _mark_destination_state(self, destination: Workspace, state: str) -> None:
        with self._store.transaction() as conn:
            conn.execute(
                'UPDATE workspaces SET state=? WHERE workspace_id=?',
                (str(state), destination.workspace_id),
            )

    # ------------------------------------------------------------ 复制
    def _copy_volume(self, source: Workspace, destination: Workspace, source_data_directory: str) -> None:
        if self._adapter.volume_exists(destination.volume_name):
            raise RuntimeUnavailable("目标卷已存在，拒绝覆盖复制")
        spec = VolumeCopySpec(
            source=self._source_volume_spec(source),
            destination=self._destination_volume_spec(destination),
            source_directory=source_data_directory,
            destination_directory=self._destination_data_directory(destination),
            helper_image=self._invitations.policy.helper_image(self._config.instance_image),
            timeout_seconds=int(getattr(self._config, 'copy_timeout_seconds', 120) or 120),
            # 即使项目声明了 content_only，也显式按**basename 在任意深度**
            # 排除会话/鉴权子目录与文件，避免应用把 session 写到业务目录（任意
            # 层级）时被拷成接收者身份。``exclude_paths`` 保留为源根相对匹配。
            exclude_names=SESSION_SUBTREES,
        )
        copy_volume = getattr(self._adapter, 'copy_volume', None)
        if not callable(copy_volume):  # pragma: no cover - fake/旧适配器兜底
            raise RuntimeUnavailable("当前运行时适配器不支持卷复制")
        copy_volume(spec)

    @staticmethod
    def _destination_data_directory(destination: Workspace) -> str:
        return validate_destination_directory(destination)

    # -------------------------------------------------------- 收尾/回滚
    def _finalize(self, destination: Workspace, copy: WorkspaceCopy, invitation_id: str) -> ImportResult:
        with self._store.transaction() as conn:
            conn.execute(
                'UPDATE workspaces SET state=?,storage_initialized=1 WHERE workspace_id=?',
                ('active', destination.workspace_id),
            )
        self._store.grant_trial(
            destination.workspace_id, destination.user_id,
            source='share_import', invitation_id=invitation_id,
        )
        self._store.unpin_workspace_copy(copy.copy_id, str(CopyState.COPIED))
        self._store.record_event(
            'workspace.copy_finished',
            payload={'source': copy.source_workspace_id, 'destination': destination.workspace_id},
        )
        return ImportResult(
            workspace=self._store.require_workspace(destination.workspace_id),
            copy=self._store.require_workspace_copy(copy.copy_id),
        )

    def _rollback(self, destination: Workspace, copy: WorkspaceCopy, error: Exception) -> None:
        """失败路径：目标标记为不可用，源项目与源卷不动。

        两个独立判定：

        * ``helper_cleaned`` **明确为 False** 时，说明适配器有残留 helper。
          它可能仍挂载着源卷，此时**保持源项目 pin**，阻止 GC 删卷
          也阻止源项目重新打开产生并发 writer；对账会重试收尾。
        * 其余情况（无 details、或 helper 已清干净）按普通失败收尾：
          释放 pin。没有 helper 的适配器（如 fake）正常走这条路。

        无论哪种情况，目标项目都不会可见（不是 active）。
        """

        message = str(error)[:500]
        details = dict(getattr(error, 'details', {}) or {})
        helper_cleaned = details.get('helper_cleaned')
        # 只有明确告知 helper 没清干净时才保留 pin。
        keep_source_pinned = helper_cleaned is False
        rollback_error = details.get('rollback_error')

        try:
            self._adapter.remove_volume(destination.volume_name)
        except Exception as exc:  # noqa: BLE001 - 清理失败不阻止收尾，但不能当作已删
            logger.warning("回滚目标卷失败，留待对账重试：%s", exc)
            rollback_error = rollback_error or str(exc)[:200]

        try:
            self._mark_destination_state(destination, 'failed')
            if keep_source_pinned:
                self._store.record_event(
                    'workspace.copy_failed',
                    payload={
                        'source': copy.source_workspace_id,
                        'destination': destination.workspace_id,
                        'error': message,
                        'helper_cleaned': False,
                        'source_pinned': True,
                    },
                )
            else:
                self._store.unpin_workspace_copy(
                    copy.copy_id, str(CopyState.FAILED),
                    error=(
                        message if rollback_error is None
                        else f"{message} | rollback: {rollback_error}"[:500]
                    ),
                )
                self._store.record_event(
                    'workspace.copy_failed',
                    payload={
                        'source': copy.source_workspace_id,
                        'destination': destination.workspace_id,
                        'error': message,
                        'helper_cleaned': helper_cleaned,
                        'rollback_error': rollback_error,
                    },
                )
        except Exception:  # noqa: BLE001 - 记录失败不能盖住原错误
            logger.exception("记录复制失败时出错")
        if isinstance(error, (InvalidInvitation, ShareSourceUnsupported, Forbidden)):
            return
        raise WorkspaceCopyFailed(
            "项目复制失败，源项目未受影响",
            details={'copy_id': copy.copy_id, 'source_pinned': keep_source_pinned},
        ) from error

    # ------------------------------------------------------------ 对账
    def _leftover_copy_helper_containers(self) -> list[str]:
        """列出可能仍在运行的卷复制 helper。

        适配器没有统一的“hepler 是否存在”接口，所以只能通过受控容器
        列表 + 标签推断。**取不到结果时必须保守地认为可能存在残留**：
        失败的侧分偶然会让源卷被长期挂载或被并发写入。
        """

        lister = getattr(self._adapter, 'list_managed', None)
        if not callable(lister):
            return ['<unavailable>']
        try:
            containers = lister()
        except Exception as exc:  # noqa: BLE001 - 无法列举时保守处理
            logger.warning("列举受控容器失败，按存在残留 helper 处理：%s", exc)
            return ['<unavailable>']

        helper_labels = {'tutorlm.copy': 'volume'}
        found: list[str] = []
        inspector = getattr(self._adapter, 'inspect_labels', None)
        for container in containers:
            name = getattr(container, 'container_name', None)
            if not name:
                continue
            labels: dict[str, str] = {}
            if callable(inspector):
                try:
                    labels = inspector(name) or {}
                except Exception:  # noqa: BLE001
                    labels = {}
            if labels.get('tutorlm.copy') == 'volume' or '-copy-' in name:
                found.append(name)
        return found

    def reconcile(self) -> dict[str, Any]:
        """进程启动时处理上一次未完成的复制。

        保守原则（宁可留失败副本残留，也不能删错）：

        1. 若有任何可能仍在运行的 helper，则**不释放任何 pin**、
           **不删除任何项目卷**，全部延后到人工确认（它可能仍挂载着源卷）；
        2. 否则逐个处理：先把目标标为不可用，再删卷，成功才释放 pin。
           删卷失败则保持 PREPARING 并在下一轮重试，源项目始终不动。
        """

        report: dict[str, Any] = {'cleaned': [], 'deferred': [], 'manual_review': []}
        pending = self._store.list_workspace_copies(states=(str(CopyState.PREPARING),))
        if not pending:
            return report

        leftovers = self._leftover_copy_helper_containers()
        if leftovers:
            # 无法确认 helper 已消失 -> 保持原状，交人确认。
            logger.error("检测到可能残留的卷复制 helper，保持所有 pin 不变：%s", leftovers)
            report['manual_review'] = [copy.copy_id for copy in pending]
            return report

        for copy in pending:
            destination = self._store.get_workspace(copy.destination_workspace_id)
            if destination is None or destination.state == 'deleted':
                self._store.unpin_workspace_copy(
                    copy.copy_id, str(CopyState.FAILED), error='interrupted_by_restart'
                )
                report['cleaned'].append(copy.copy_id)
                continue
            # 目标不可见（不是 active）。
            self._mark_destination_state(destination, 'failed')
            try:
                self._adapter.remove_volume(destination.volume_name)
            except Exception as exc:  # noqa: BLE001 - 可能仍有挂载/抖动
                logger.warning("对账删除半成品卷失败，保持 pin 并重试：%s", exc)
                report['deferred'].append(copy.copy_id)
                continue
            self._store.unpin_workspace_copy(
                copy.copy_id, str(CopyState.FAILED), error='interrupted_by_restart'
            )
            report['cleaned'].append(copy.copy_id)
            self._store.record_event(
                'workspace.copy_reconciled',
                payload={'copy_id': copy.copy_id, 'destination': copy.destination_workspace_id},
            )
        return report


def validate_destination_directory(destination: Workspace) -> str:
    """目标目录必须由 UUID 严格推导，不接受任意 path。"""

    data_directory = workspace_data_directory(destination)
    if data_directory == LEGACY_DATA_DIRECTORY:
        raise ShareSourceUnsupported("克隆目标必须使用 UUID 专属目录")
    return data_directory
