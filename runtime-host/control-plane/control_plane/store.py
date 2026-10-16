"""控制面持久化：SQLite 存槽位与租约。

这是**控制面自己的**数据库，与 instance-app 每个实例的 SQLite 完全分离；
两侧不共享 schema、不共享文件（见 docs/architecture.md「数据和权限」）。

原子性是这里最重要的性质：``allocate`` 必须在同一个事务里"选中空闲槽位 +
写入租约 + 把槽位标为 allocating"。多个访客同时点击时，靠
``BEGIN IMMEDIATE`` 与 ``UPDATE ... WHERE state='free'`` 的条件更新保证
**同一个槽位绝不会被分配两次**，而不是靠先在 Python 里读一遍再写回。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from control_plane.domain.models import (
    CopyState,
    Invitation,
    InvitationKind,
    InvitationState,
    Lease,
    LeaseState,
    LEGACY_DATA_DIRECTORY,
    Slot,
    SlotState,
    STORAGE_LAYOUT_CONTENT_ONLY,
    STORAGE_LAYOUT_LEGACY_ROOT,
    WorkloadState,
    Workspace,
    WorkspaceCopy,
    validate_data_directory,
)
from control_plane.errors import (
    CapacityError,
    CopyInProgress,
    InvalidToken,
    InvalidInvitation,
    InvitationExpired,
    LeaseNotFound,
    NotFound,
    SlotBusy,
    WorkspaceExpired,
)

SCHEMA_VERSION = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS slots (
    slot_id          INTEGER PRIMARY KEY,
    state            TEXT NOT NULL,
    generation       INTEGER NOT NULL DEFAULT 0,
    container_name   TEXT,
    lease_id         TEXT,
    workload_state   TEXT NOT NULL DEFAULT 'absent',
    quarantine_reason TEXT,
    updated_at       REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS leases (
    lease_id        TEXT PRIMARY KEY,
    slot_id         INTEGER NOT NULL,
    generation      INTEGER NOT NULL,
    state           TEXT NOT NULL,
    token_hash      TEXT NOT NULL,
    upstream        TEXT,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    expires_at      REAL NOT NULL,
    last_seen_at    REAL NOT NULL,
    label           TEXT NOT NULL DEFAULT '',
    failure_reason  TEXT,
    priority        INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (slot_id) REFERENCES slots(slot_id)
);

CREATE INDEX IF NOT EXISTS idx_leases_state ON leases(state);
CREATE INDEX IF NOT EXISTS idx_leases_slot ON leases(slot_id);

CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    at       REAL NOT NULL,
    kind     TEXT NOT NULL,
    lease_id TEXT,
    slot_id  INTEGER,
    payload  TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_lease ON events(lease_id);

CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS workspaces (
    workspace_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(user_id),
    title TEXT NOT NULL,
    volume_name TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL DEFAULT 'active',
    created_at REAL NOT NULL,
    last_used_at REAL NOT NULL,
    deleted_at REAL,
    last_error TEXT,
    storage_initialized INTEGER NOT NULL DEFAULT 0,
    data_directory TEXT NOT NULL DEFAULT '.',
    storage_layout TEXT NOT NULL DEFAULT 'legacy_root',
    source_workspace_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_workspaces_user ON workspaces(user_id);
CREATE INDEX IF NOT EXISTS idx_workspaces_idle ON workspaces(state, last_used_at);

-- Invitation codes. Only a hashed code is stored; plaintext exists solely in
-- the issuing response and in the operator's configuration input.
CREATE TABLE IF NOT EXISTS invitations (
    invitation_id    TEXT PRIMARY KEY,
    kind             TEXT NOT NULL,
    code_prefix      TEXT NOT NULL,
    code_hash        TEXT NOT NULL,
    label            TEXT NOT NULL DEFAULT '',
    workspace_id     TEXT,
    owner_user_id    TEXT,
    state            TEXT NOT NULL DEFAULT 'active',
    origin           TEXT NOT NULL DEFAULT 'api',
    created_at       REAL NOT NULL,
    revoked_at       REAL,
    expires_at       REAL,
    max_uses         INTEGER,
    use_count        INTEGER NOT NULL DEFAULT 0,
    last_used_at     REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_invitations_hash ON invitations(code_hash);
CREATE INDEX IF NOT EXISTS idx_invitations_kind ON invitations(kind, state);
CREATE INDEX IF NOT EXISTS idx_invitations_workspace ON invitations(workspace_id, state);

-- A durable reference for one share copy. Written BEFORE any container or
-- volume work so GC can always see that the source volume is pinned.
CREATE TABLE IF NOT EXISTS workspace_copy_operations (
    copy_id                  TEXT PRIMARY KEY,
    source_workspace_id      TEXT NOT NULL,
    destination_workspace_id TEXT NOT NULL UNIQUE,
    recipient_user_id        TEXT NOT NULL,
    invitation_id            TEXT,
    idempotency_key          TEXT,
    state                    TEXT NOT NULL,
    source_pinned            INTEGER NOT NULL DEFAULT 1,
    error                    TEXT,
    created_at               REAL NOT NULL,
    updated_at               REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_copy_source ON workspace_copy_operations(source_workspace_id, state);
CREATE UNIQUE INDEX IF NOT EXISTS idx_copy_idempotency
    ON workspace_copy_operations(recipient_user_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL AND state <> 'failed';

-- Invitation-derived grants that let a recipient open a copied project even
-- when the control plane is in invitation-only mode. They never carry system
-- priority.
CREATE TABLE IF NOT EXISTS trial_grants (
    workspace_id TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    source       TEXT NOT NULL,
    invitation_id TEXT,
    created_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS lease_pages (
    lease_id TEXT NOT NULL REFERENCES leases(lease_id),
    page_id TEXT NOT NULL,
    last_seen_at REAL NOT NULL,
    closed_at REAL,
    PRIMARY KEY (lease_id, page_id)
);
"""


def _lease_from_row(row: sqlite3.Row) -> Lease:
    return Lease(
        lease_id=row["lease_id"],
        slot_id=int(row["slot_id"]),
        generation=int(row["generation"]),
        state=LeaseState(row["state"]),
        token_hash=row["token_hash"],
        upstream=row["upstream"],
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        expires_at=float(row["expires_at"]),
        last_seen_at=float(row["last_seen_at"]),
        label=row["label"] or "",
        failure_reason=row["failure_reason"],
        workspace_id=row['workspace_id'],
        priority=int(row["priority"]) if row["priority"] is not None else 0,
    )


def _slot_from_row(row: sqlite3.Row) -> Slot:
    return Slot(
        slot_id=int(row["slot_id"]),
        state=SlotState(row["state"]),
        generation=int(row["generation"]),
        container_name=row["container_name"],
        lease_id=row["lease_id"],
        workload_state=WorkloadState(row["workload_state"]),
        quarantine_reason=row["quarantine_reason"],
        updated_at=float(row["updated_at"]),
    )


def _workspace_from_row(row: sqlite3.Row) -> Workspace:
    payload = dict(row)
    payload['data_directory'] = payload.get('data_directory') or LEGACY_DATA_DIRECTORY
    payload['storage_layout'] = payload.get('storage_layout') or STORAGE_LAYOUT_LEGACY_ROOT
    return Workspace(**payload)


def _invitation_from_row(row: sqlite3.Row) -> Invitation:
    return Invitation(
        invitation_id=row['invitation_id'],
        kind=row['kind'],
        code_prefix=row['code_prefix'],
        code_hash=row['code_hash'],
        label=row['label'] or '',
        workspace_id=row['workspace_id'],
        owner_user_id=row['owner_user_id'],
        state=row['state'],
        created_at=float(row['created_at']),
        revoked_at=float(row['revoked_at']) if row['revoked_at'] is not None else None,
        expires_at=float(row['expires_at']) if row['expires_at'] is not None else None,
        max_uses=int(row['max_uses']) if row['max_uses'] is not None else None,
        use_count=int(row['use_count'] or 0),
        last_used_at=float(row['last_used_at']) if row['last_used_at'] is not None else None,
    )


def _copy_from_row(row: sqlite3.Row) -> WorkspaceCopy:
    return WorkspaceCopy(
        copy_id=row['copy_id'],
        source_workspace_id=row['source_workspace_id'],
        destination_workspace_id=row['destination_workspace_id'],
        recipient_user_id=row['recipient_user_id'],
        state=row['state'],
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
        invitation_id=row['invitation_id'],
        idempotency_key=row['idempotency_key'],
        error=row['error'],
    )


class ControlPlaneStore:
    """SQLite 支撑的槽位 + 租约存储。

    线程安全：内部 ``threading.RLock`` 串行化写事务；FastAPI 的同步端点与
    后台 GC 线程因此可以共用同一个实例（``check_same_thread=False``）。
    """

    def __init__(
        self,
        db_path: Path | str,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db_path = Path(db_path)
        # 可注入时钟：租约 TTL 与"分配时刻"必须用同一个时间基准，否则
        # 测试里注入的假时钟与真实时间混用会得出错误结论。
        self._clock = clock
        if str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            isolation_level=None,  # 自己管事务
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_SCHEMA)
            with self.transaction() as conn:
                version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
                if version and int(version['value']) > SCHEMA_VERSION:
                    raise RuntimeError('Database schema is newer than this control plane')
                columns = {r['name'] for r in conn.execute('PRAGMA table_info(leases)')}
                if 'workspace_id' not in columns:
                    conn.execute('ALTER TABLE leases ADD COLUMN workspace_id TEXT REFERENCES workspaces(workspace_id)')
                workspace_columns = {r['name'] for r in conn.execute('PRAGMA table_info(workspaces)')}
                if 'storage_initialized' not in workspace_columns:
                    conn.execute('ALTER TABLE workspaces ADD COLUMN storage_initialized INTEGER NOT NULL DEFAULT 0')
                if 'data_directory' not in workspace_columns:
                    conn.execute("ALTER TABLE workspaces ADD COLUMN data_directory TEXT NOT NULL DEFAULT '.'")
                if 'storage_layout' not in workspace_columns:
                    conn.execute(
                        "ALTER TABLE workspaces ADD COLUMN storage_layout TEXT NOT NULL DEFAULT 'legacy_root'"
                    )
                if 'source_workspace_id' not in workspace_columns:
                    conn.execute('ALTER TABLE workspaces ADD COLUMN source_workspace_id TEXT')
                if 'priority' not in columns:
                    conn.execute('ALTER TABLE leases ADD COLUMN priority INTEGER NOT NULL DEFAULT 0')
                conn.execute('CREATE INDEX IF NOT EXISTS idx_leases_workspace ON leases(workspace_id)')
                conn.execute(
                    "INSERT INTO meta(key,value) VALUES('schema_version',?) "
                    'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (str(SCHEMA_VERSION),),
                )

    # ---------------------------------------------------------------- 事务
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """写事务。``BEGIN IMMEDIATE`` 立刻拿写锁，避免升级锁时死锁。"""

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------------------------------------------------------------- meta
    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def get_or_create_meta(self, key: str, value: str) -> str:
        with self.transaction() as conn:
            conn.execute('INSERT OR IGNORE INTO meta(key,value) VALUES(?,?)', (key, value))
            return conn.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()['value']

    # Durable identities and project storage are independent of compute slots.
    def create_user(self, user_id: str, token_hash: str) -> dict[str, Any]:
        now = self._clock()
        with self.transaction() as conn:
            conn.execute('INSERT INTO users VALUES(?,?,?)', (user_id, token_hash, now))
        return {'user_id': user_id, 'created_at': now}

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute('SELECT * FROM users WHERE user_id=?', (user_id,)).fetchone()
        return dict(row) if row else None

    def create_workspace(
        self,
        workspace_id: str,
        user_id: str,
        title: str,
        volume_name: str,
        *,
        data_directory: str = LEGACY_DATA_DIRECTORY,
        storage_layout: str = STORAGE_LAYOUT_LEGACY_ROOT,
        source_workspace_id: str | None = None,
    ) -> Workspace:
        now = self._clock()
        # Never trust a caller-supplied path: re-derive and validate it against
        # the owning UUIDs before it can reach the database.
        if data_directory != LEGACY_DATA_DIRECTORY:
            data_directory = validate_data_directory(
                data_directory, user_id=user_id, workspace_id=workspace_id
            )
        with self.transaction() as conn:
            conn.execute(
                'INSERT INTO workspaces(workspace_id,user_id,title,volume_name,created_at,last_used_at,'
                'data_directory,storage_layout,source_workspace_id) VALUES(?,?,?,?,?,?,?,?,?)',
                (
                    workspace_id, user_id, title, volume_name, now, now,
                    data_directory, storage_layout, source_workspace_id,
                ),
            )
        return self.require_workspace(workspace_id)

    def require_workspace(self, workspace_id: str) -> Workspace:
        with self._lock:
            row = self._conn.execute('SELECT * FROM workspaces WHERE workspace_id=?', (workspace_id,)).fetchone()
        if row is None:
            raise NotFound('Workspace not found')
        workspace = _workspace_from_row(row)
        # The stored path is a trust boundary: validate that it still matches
        # the canonical form for this project's UUIDs on every read.
        validate_data_directory(
            workspace.data_directory,
            user_id=workspace.user_id,
            workspace_id=workspace.workspace_id,
        )
        return workspace

    def list_workspaces(self, user_id: str) -> list[Workspace]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM workspaces WHERE user_id=? AND state!='deleted' ORDER BY created_at,workspace_id",
                (user_id,),
            ).fetchall()
        return [_workspace_from_row(row) for row in rows]

    def mark_workspace_initialized(self, workspace_id: str) -> None:
        with self.transaction() as conn:
            conn.execute('UPDATE workspaces SET storage_initialized=1 WHERE workspace_id=?', (workspace_id,))

    def get_workspace(self, workspace_id: str) -> Workspace | None:
        try:
            return self.require_workspace(workspace_id)
        except NotFound:
            return None

    def live_lease_for_workspace(self, workspace_id: str) -> Lease | None:
        """这个项目当前是否占着计算（仍在占槽的租约）。"""

        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM leases WHERE workspace_id=? AND state IN (?,?,?,?) "
                "ORDER BY created_at DESC LIMIT 1",
                (
                    workspace_id,
                    str(LeaseState.PENDING), str(LeaseState.ACTIVE),
                    str(LeaseState.EXPIRED), str(LeaseState.REVOKED),
                ),
            ).fetchone()
        return _lease_from_row(row) if row else None

    def mark_workspace_layout(self, workspace_id: str, storage_layout: str) -> None:
        """记录应用就绪时声明的存储布局。"""

        with self.transaction() as conn:
            conn.execute(
                'UPDATE workspaces SET storage_layout=? WHERE workspace_id=?',
                (str(storage_layout), workspace_id),
            )

    @staticmethod
    def _workspace_has_compute(conn: sqlite3.Connection, workspace_id: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM leases l WHERE l.workspace_id=? AND (l.state IN "
            "('pending','active','expired','revoked') OR EXISTS "
            '(SELECT 1 FROM slots s WHERE s.lease_id=l.lease_id)) LIMIT 1', (workspace_id,),
        ).fetchone() is not None

    @staticmethod
    def _workspace_in_use(conn: sqlite3.Connection, workspace_id: str) -> bool:
        if ControlPlaneStore._workspace_has_compute(conn, workspace_id):
            return True
        # A destination still being prepared is not "in use" for compute, but
        # its source pin and the copy transaction must block deletion/reopen.
        if ControlPlaneStore._workspace_pinned(conn, workspace_id):
            return True
        return conn.execute(
            "SELECT 1 FROM workspace_copy_operations WHERE destination_workspace_id=? "
            "AND state=? LIMIT 1",
            (workspace_id, str(CopyState.PREPARING)),
        ).fetchone() is not None

    def _owned_workspace(self, conn, workspace_id: str, user_id: str, idle_seconds: int) -> Workspace:
        row = conn.execute(
            'SELECT * FROM workspaces WHERE workspace_id=? AND user_id=?', (workspace_id, user_id),
        ).fetchone()
        if row is None:
            raise NotFound('Workspace not found')
        workspace = _workspace_from_row(row)
        validate_data_directory(
            workspace.data_directory,
            user_id=workspace.user_id,
            workspace_id=workspace.workspace_id,
        )
        if workspace.state != 'active' or (
            workspace.last_used_at < self._clock() - idle_seconds
            and not self._workspace_in_use(conn, workspace_id)
        ):
            raise WorkspaceExpired('Workspace expired or is being deleted')
        return workspace

    def owned_workspace(self, workspace_id: str, user_id: str, idle_seconds: int, *, touch: bool = False) -> Workspace:
        with self.transaction() as conn:
            workspace = self._owned_workspace(conn, workspace_id, user_id, idle_seconds)
            if touch:
                conn.execute('UPDATE workspaces SET last_used_at=? WHERE workspace_id=?', (self._clock(), workspace_id))
        return self.require_workspace(workspace_id) if touch else workspace

    def claim_expired_workspaces(self, idle_seconds: int) -> list[Workspace]:
        claimed = []
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM workspaces WHERE state='deleting' OR (state='active' AND last_used_at<?)",
                (self._clock() - idle_seconds,),
            ).fetchall()
            for row in rows:
                if self._workspace_in_use(conn, row['workspace_id']):
                    continue
                # A copy operation pins its source. Claiming it would let GC
                # delete the very volume a copy is reading from.
                if self._workspace_pinned(conn, row['workspace_id']):
                    continue
                conn.execute("UPDATE workspaces SET state='deleting' WHERE workspace_id=?", (row['workspace_id'],))
                claimed.append(Workspace(**{**dict(row), 'state': 'deleting'}))
        return claimed

    @staticmethod
    def _workspace_pinned(conn: sqlite3.Connection, workspace_id: str) -> bool:
        """是否有未完成的复制操作正在使用该项目卷。"""

        row = conn.execute(
            "SELECT 1 FROM workspace_copy_operations WHERE source_workspace_id=? "
            "AND source_pinned=1 AND state=? LIMIT 1",
            (workspace_id, str(CopyState.PREPARING)),
        ).fetchone()
        return row is not None

    def finish_workspace_deletion(self, workspace_id: str, error: str | None = None) -> None:
        with self.transaction() as conn:
            if error is not None:
                conn.execute("UPDATE workspaces SET last_error=? WHERE workspace_id=? AND state='deleting'", (error, workspace_id))
            else:
                conn.execute(
                    "UPDATE workspaces SET state='deleted',title='',deleted_at=?,last_error=NULL "
                    "WHERE workspace_id=? AND state='deleting'", (self._clock(), workspace_id),
                )

    def _active_page_lease(self, conn: sqlite3.Connection, lease_id: str):
        row = conn.execute(
            "SELECT l.* FROM leases l JOIN slots s ON s.slot_id=l.slot_id "
            "AND s.lease_id=l.lease_id AND s.generation=l.generation "
            "WHERE l.lease_id=? AND l.state='active' AND s.state='active' AND l.expires_at>?",
            (lease_id, self._clock()),
        ).fetchone()
        if row is None:
            raise InvalidToken('Compute lease is no longer active')
        return row

    def register_page(self, lease_id: str, page_id: str) -> None:
        now = self._clock()
        with self.transaction() as conn:
            lease = self._active_page_lease(conn, lease_id)
            page = conn.execute(
                'SELECT closed_at FROM lease_pages WHERE lease_id=? AND page_id=?', (lease_id, page_id),
            ).fetchone()
            if page is not None and page['closed_at'] is not None:
                raise SlotBusy('A closed page cannot reattach; use a new page UUID')
            conn.execute(
                'INSERT INTO lease_pages(lease_id,page_id,last_seen_at) VALUES(?,?,?) '
                'ON CONFLICT(lease_id,page_id) DO UPDATE SET last_seen_at=excluded.last_seen_at',
                (lease_id, page_id, now),
            )
            if page is None:
                conn.execute('UPDATE leases SET last_seen_at=?,updated_at=? WHERE lease_id=?', (now, now, lease_id))
                conn.execute(
                    "UPDATE workspaces SET last_used_at=? WHERE workspace_id=? AND state='active'",
                    (now, lease['workspace_id']),
                )

    def heartbeat_page(self, lease_id: str, page_id: str) -> None:
        with self.transaction() as conn:
            self._active_page_lease(conn, lease_id)
            cursor = conn.execute(
                'UPDATE lease_pages SET last_seen_at=? WHERE lease_id=? AND page_id=? AND closed_at IS NULL',
                (self._clock(), lease_id, page_id),
            )
            if cursor.rowcount != 1:
                raise NotFound('Page is not attached or is already closed')

    def close_page(self, lease_id: str, page_id: str) -> None:
        now = self._clock()
        with self.transaction() as conn:
            self._active_page_lease(conn, lease_id)
            # Keep a tombstone even if pagehide arrives before its attach request.
            conn.execute(
                'INSERT INTO lease_pages(lease_id,page_id,last_seen_at,closed_at) VALUES(?,?,?,?) '
                'ON CONFLICT(lease_id,page_id) DO UPDATE SET closed_at=COALESCE(lease_pages.closed_at,excluded.closed_at)',
                (lease_id, page_id, now, now),
            )

    def page_deadline(self, lease_id: str, heartbeat_timeout: int, close_grace: int) -> float | None:
        with self._lock:
            return self._page_deadline(self._conn, lease_id, heartbeat_timeout, close_grace)

    @staticmethod
    def _page_deadline(conn, lease_id: str, heartbeat_timeout: int, close_grace: int) -> float | None:
        row = conn.execute(
            'SELECT MAX(CASE WHEN closed_at IS NULL THEN last_seen_at+? ELSE closed_at+? END) AS deadline '
            'FROM lease_pages WHERE lease_id=?', (heartbeat_timeout, close_grace, lease_id),
        ).fetchone()
        return row['deadline']

    def expire_disconnected_pages(self, lease_id: str, heartbeat_timeout: int, close_grace: int) -> bool:
        with self.transaction() as conn:
            deadline = self._page_deadline(conn, lease_id, heartbeat_timeout, close_grace)
            if deadline is None or deadline > self._clock():
                return False
            cursor = conn.execute(
                "UPDATE leases SET state='expired',updated_at=? "
                "WHERE lease_id=? AND state IN ('pending','active','expired')", (self._clock(), lease_id),
            )
            return cursor.rowcount == 1

    # ------------------------------------------------------------- 抢占
    def preempt_candidates(self, *, now: float, exclude_priority_at_least: int) -> list[tuple[Lease, int]]:
        """列出可安全抢占的 (租约, slot_id)，按可抢占性排序。

        只选择已就绪并正在服务的受控租约（``state='active'`` 且槽位
        ``state='active'``）。仍在 ``allocating``/``copy`` 的槽位不会被选中，
        因为它们尚未建立可回收的安全状态。
        """

        with self._lock:
            rows = self._conn.execute(
                "SELECT l.*, s.slot_id AS slot_id FROM leases l JOIN slots s ON s.slot_id=l.slot_id "
                "AND s.lease_id=l.lease_id AND s.generation=l.generation "
                "WHERE l.state=? AND s.state=? AND l.expires_at>? AND l.priority<? "
                "ORDER BY l.priority, l.created_at, l.lease_id",
                (
                    str(LeaseState.ACTIVE), str(SlotState.ACTIVE), float(now),
                    int(exclude_priority_at_least),
                ),
            ).fetchall()
        return [(_lease_from_row(row), int(row['slot_id'])) for row in rows]

    def mark_preempting(self, lease_id: str, *, reason: str) -> bool:
        """原子地将某个 active 租约标记为被抢占。

        返回 ``False`` 表示它已经不再处于可被抢占的状态（其他请求先
        一步动手了），调用方应重新选择而不是硬写。
        """

        now = self._clock()
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE leases SET state=?,failure_reason=?,updated_at=? "
                "WHERE lease_id=? AND state=?",
                (str(LeaseState.REVOKED), str(reason)[:200], now, lease_id, str(LeaseState.ACTIVE)),
            )
            if cursor.rowcount != 1:
                return False
            conn.execute(
                "UPDATE slots SET state=?,updated_at=? WHERE slot_id="
                '(SELECT slot_id FROM leases WHERE lease_id=?) AND state=?',
                (str(SlotState.DRAINING), now, lease_id, str(SlotState.ACTIVE)),
            )
            conn.execute(
                "INSERT INTO events(at,kind,lease_id,slot_id,payload) VALUES(?,?,?,?,?)",
                (
                    now, 'lease.preempted', lease_id,
                    conn.execute('SELECT slot_id FROM leases WHERE lease_id=?', (lease_id,)).fetchone()['slot_id'],
                    json.dumps({'reason': str(reason)[:200]}),
                ),
            )
        return True

    # ------------------------------------------------------- 复制操作
    def begin_share_copy(
        self,
        *,
        copy_id: str,
        source_workspace_id: str,
        destination_workspace_id: str,
        recipient_user_id: str,
        destination_volume_name: str,
        title: str,
        invitation_id: str | None,
        idempotency_key: str | None,
        data_directory: str,
    ) -> WorkspaceCopy:
        """原子地同时建立目标项目行与复制操作行。

        两者必须在同一事务里落库：否则中间态会产生一个"看得到
        但没有任何副本记录"的空项目。目标项目初始状态为
        ``copying``，因此在复制成功前不会对接收者可见。
        """

        now = self._clock()
        canonical = validate_data_directory(
            data_directory, user_id=recipient_user_id, workspace_id=destination_workspace_id
        )
        with self.transaction() as conn:
            if idempotency_key is not None:
                existing = conn.execute(
                    'SELECT * FROM workspace_copy_operations WHERE recipient_user_id=? '
                    "AND idempotency_key=? AND state<>'failed' LIMIT 1",
                    (recipient_user_id, idempotency_key),
                ).fetchone()
                if existing is not None:
                    raise CopyInProgress(
                        '该导入操作已存在',
                        details={
                            'copy_id': existing['copy_id'],
                            'destination_workspace_id': existing['destination_workspace_id'],
                            'state': existing['state'],
                        },
                    )
            conn.execute(
                'INSERT INTO workspaces(workspace_id,user_id,title,volume_name,state,created_at,'
                'last_used_at,data_directory,storage_layout) VALUES(?,?,?,?,?,?,?,?,?)',
                (
                    destination_workspace_id, recipient_user_id, title, destination_volume_name,
                    'copying', now, now, canonical, STORAGE_LAYOUT_CONTENT_ONLY,
                ),
            )
            conn.execute(
                'INSERT INTO workspace_copy_operations(copy_id,source_workspace_id,destination_workspace_id,'
                'recipient_user_id,invitation_id,idempotency_key,state,source_pinned,created_at,updated_at) '
                'VALUES(?,?,?,?,?,?,?,1,?,?)',
                (
                    copy_id, source_workspace_id, destination_workspace_id, recipient_user_id,
                    invitation_id, idempotency_key, str(CopyState.PREPARING), now, now,
                ),
            )
        return self.require_workspace_copy(copy_id)

    def require_workspace_copy(self, copy_id: str) -> WorkspaceCopy:
        with self._lock:
            row = self._conn.execute(
                'SELECT * FROM workspace_copy_operations WHERE copy_id=?', (copy_id,)
            ).fetchone()
        if row is None:
            raise NotFound('复制操作不存在')
        return _copy_from_row(row)

    def copy_for_idempotency_key(self, recipient_user_id: str, key: str) -> WorkspaceCopy | None:
        with self._lock:
            row = self._conn.execute(
                'SELECT * FROM workspace_copy_operations WHERE recipient_user_id=? AND idempotency_key=? '
                "AND state<>'failed' ORDER BY created_at LIMIT 1",
                (recipient_user_id, key),
            ).fetchone()
        return _copy_from_row(row) if row else None

    def copy_for_destination(self, workspace_id: str) -> WorkspaceCopy | None:
        with self._lock:
            row = self._conn.execute(
                'SELECT * FROM workspace_copy_operations WHERE destination_workspace_id=?',
                (workspace_id,),
            ).fetchone()
        return _copy_from_row(row) if row else None

    def list_workspace_copies(self, *, states: tuple[str, ...] | None = None) -> list[WorkspaceCopy]:
        sql = 'SELECT * FROM workspace_copy_operations'
        params: tuple[Any, ...] = ()
        if states:
            placeholders = ','.join('?' for _ in states)
            sql += f' WHERE state IN ({placeholders})'
            params = tuple(str(state) for state in states)
        sql += ' ORDER BY created_at, copy_id'
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_copy_from_row(row) for row in rows]

    def unpin_workspace_copy(self, copy_id: str, state: str, *, error: str | None = None) -> WorkspaceCopy:
        """结束一次复制操作并释放源项目的 pin。"""

        with self.transaction() as conn:
            conn.execute(
                'UPDATE workspace_copy_operations SET state=?,source_pinned=0,error=?,updated_at=? '
                'WHERE copy_id=?',
                (str(state), error[:500] if error else None, self._clock(), copy_id),
            )
        return self.require_workspace_copy(copy_id)

    # ------------------------------------------------- 失败副本収尾
    def claim_failed_copy_destinations(self) -> list[str]:
        """把失败复制的**目标项目**交给存储 GC 清理。

        失败副本的目标项目永远不会可见（不是 active）。如果回滚时卷删除失败
        （``destination_may_contain_partial_data``），它会以 ``state='failed'`` 残留。下一次
        对账/收割时把它转为 ``deleting``，交给已有的存储 GC 重试删除与清理（不会重新打开）。

        只处理不再被任何 lease 引用的目标项目；返回本次提交的项目 UUID。
        """

        claimed: list[str] = []
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT w.workspace_id FROM workspaces w JOIN workspace_copy_operations c "
                "ON c.destination_workspace_id = w.workspace_id "
                "WHERE w.state='failed' AND c.state=? AND c.source_pinned=0",
                (str(CopyState.FAILED),),
            ).fetchall()
            for row in rows:
                workspace_id = row['workspace_id']
                if self._workspace_has_compute(conn, workspace_id):
                    continue
                conn.execute(
                    "UPDATE workspaces SET state='deleting' WHERE workspace_id=? AND state='failed'",
                    (workspace_id,),
                )
                claimed.append(workspace_id)
        return claimed

    # ----------------------------------------------------------- trial grant
    def grant_trial(self, workspace_id: str, user_id: str, *, source: str, invitation_id: str | None = None) -> None:
        with self.transaction() as conn:
            conn.execute(
                'INSERT INTO trial_grants(workspace_id,user_id,source,invitation_id,created_at) '
                'VALUES(?,?,?,?,?) ON CONFLICT(workspace_id) DO UPDATE SET '
                'user_id=excluded.user_id,source=excluded.source,invitation_id=excluded.invitation_id',
                (workspace_id, user_id, source, invitation_id, self._clock()),
            )

    def has_trial_grant(self, workspace_id: str, user_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                'SELECT 1 FROM trial_grants WHERE workspace_id=? AND user_id=? LIMIT 1',
                (workspace_id, user_id),
            ).fetchone()
        return row is not None

    # ---------------------------------------------------------------- 槽位
    def ensure_slots(self, count: int) -> int:
        """按配置补齐槽位行。返回本次新建数量；已有槽位保持不变。"""

        now = self._clock()
        created = 0
        with self.transaction() as conn:
            existing = {
                int(row["slot_id"])
                for row in conn.execute("SELECT slot_id FROM slots").fetchall()
            }
            for slot_id in range(count):
                if slot_id in existing:
                    continue
                conn.execute(
                    "INSERT INTO slots(slot_id, state, generation, workload_state, updated_at) "
                    "VALUES (?, ?, 0, ?, ?)",
                    (slot_id, str(SlotState.FREE), str(WorkloadState.ABSENT), now),
                )
                created += 1
        return created

    def list_slots(self) -> list[Slot]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM slots ORDER BY slot_id").fetchall()
        return [_slot_from_row(row) for row in rows]

    def get_slot(self, slot_id: int) -> Slot | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM slots WHERE slot_id = ?", (slot_id,)).fetchone()
        return _slot_from_row(row) if row else None

    def require_slot(self, slot_id: int) -> Slot:
        slot = self.get_slot(slot_id)
        if slot is None:
            raise SlotBusy(f"槽位 {slot_id} 不存在")
        return slot

    def update_slot(
        self,
        slot_id: int,
        *,
        state: SlotState | None = None,
        generation: int | None = None,
        container_name: str | None = None,
        lease_id: str | None = None,
        workload_state: WorkloadState | None = None,
        quarantine_reason: str | None = None,
        clear_lease: bool = False,
        clear_quarantine: bool = False,
    ) -> Slot:
        """更新槽位。``clear_lease``/``clear_quarantine`` 显式置 NULL，
        因为 ``None`` 参数本身无法区分"不改"与"清空"。"""

        sets: list[str] = []
        params: list[Any] = []
        if state is not None:
            sets.append("state = ?")
            params.append(str(state))
        if generation is not None:
            sets.append("generation = ?")
            params.append(int(generation))
        if container_name is not None:
            sets.append("container_name = ?")
            params.append(container_name)
        if lease_id is not None:
            sets.append("lease_id = ?")
            params.append(lease_id)
        if clear_lease:
            sets.append("lease_id = NULL")
        if workload_state is not None:
            sets.append("workload_state = ?")
            params.append(str(workload_state))
        if quarantine_reason is not None:
            sets.append("quarantine_reason = ?")
            params.append(quarantine_reason)
        if clear_quarantine:
            sets.append("quarantine_reason = NULL")
        sets.append("updated_at = ?")
        params.append(self._clock())
        params.append(int(slot_id))

        with self.transaction() as conn:
            conn.execute(f"UPDATE slots SET {', '.join(sets)} WHERE slot_id = ?", params)
            row = conn.execute("SELECT * FROM slots WHERE slot_id = ?", (int(slot_id),)).fetchone()
        if row is None:
            raise SlotBusy(f"槽位 {slot_id} 不存在")
        return _slot_from_row(row)

    # ---------------------------------------------------------------- 分配
    def allocate(
        self,
        *,
        lease_id: str,
        token_hash: str,
        ttl_seconds: int,
        label: str = "",
        workspace_id: str | None = None,
        user_id: str | None = None,
        workspace_idle_seconds: int = 14400,
        priority: int = 0,
    ) -> Lease:
        """原子分配：选空闲槽位 + 建租约 + 占位。

        在同一个写事务里完成，并用条件更新 ``WHERE state = 'free'`` 兜底，
        因此并发调用不会拿到同一个槽位。容量耗尽抛 :class:`CapacityError`。
        """

        now = self._clock()
        with self.transaction() as conn:
            if workspace_id is not None:
                self._owned_workspace(conn, workspace_id, user_id, workspace_idle_seconds)
                if self._workspace_in_use(conn, workspace_id):
                    raise SlotBusy('Workspace already has a compute lease or requires cleanup')
            row = conn.execute(
                "SELECT slot_id FROM slots WHERE state = ? AND quarantine_reason IS NULL "
                "ORDER BY slot_id LIMIT 1",
                (str(SlotState.FREE),),
            ).fetchone()
            if row is None:
                raise CapacityError("没有空闲槽位，请稍后再试")
            slot_id = int(row["slot_id"])
            # 条件更新：只有仍是 free 才占位，杜绝重复分配。
            cursor = conn.execute(
                "UPDATE slots SET state = ?, lease_id = ?, workload_state = ?, updated_at = ? "
                "WHERE slot_id = ? AND state = ?",
                (
                    str(SlotState.ALLOCATING),
                    lease_id,
                    str(WorkloadState.ABSENT),
                    now,
                    slot_id,
                    str(SlotState.FREE),
                ),
            )
            if cursor.rowcount != 1:
                raise CapacityError("槽位竞争失败，请重试")
            generation = int(
                conn.execute(
                    "SELECT generation FROM slots WHERE slot_id = ?", (slot_id,)
                ).fetchone()["generation"]
            )
            conn.execute(
                "INSERT INTO leases(lease_id, slot_id, generation, state, token_hash, "
                "created_at, updated_at, expires_at, last_seen_at, label, workspace_id, priority) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    lease_id,
                    slot_id,
                    generation,
                    str(LeaseState.PENDING),
                    token_hash,
                    now,
                    now,
                    now + ttl_seconds,
                    now,
                    label,
                    workspace_id,
                    int(priority),
                ),
            )
            if workspace_id is not None:
                conn.execute('UPDATE workspaces SET last_used_at=? WHERE workspace_id=?', (now, workspace_id))
            conn.execute(
                "INSERT INTO events(at, kind, lease_id, slot_id, payload) VALUES (?, ?, ?, ?, ?)",
                (now, "lease.allocated", lease_id, slot_id, json.dumps({"generation": generation})),
            )
        lease = self.get_lease(lease_id)
        assert lease is not None
        return lease

    def promote_lease_priority(self, lease_id: str, priority: int) -> Lease:
        """原子地把一个现有租约的服务级别提升到 ``priority``。

        CAS 语义：只升不降（``WHERE priority < ?``）。这样并发的两个请求
        不会把已有的高优先级又降回去，也不会因为竞争产生回退。
        不占用新槽位，不改变身份凭据。
        """

        with self.transaction() as conn:
            conn.execute(
                'UPDATE leases SET priority=?, updated_at=? WHERE lease_id=? AND priority<?',
                (int(priority), self._clock(), lease_id, int(priority)),
            )
            row = conn.execute('SELECT * FROM leases WHERE lease_id=?', (lease_id,)).fetchone()
        if row is None:
            raise LeaseNotFound('租约不存在')
        return _lease_from_row(row)

    # ----------------------------------------------------------- 邀请码
    def create_invitation(
        self,
        *,
        invitation_id: str,
        kind: str,
        code_prefix: str,
        code_hash: str,
        label: str = "",
        workspace_id: str | None = None,
        owner_user_id: str | None = None,
        origin: str = "api",
        expires_at: float | None = None,
        max_uses: int | None = None,
    ) -> Invitation:
        now = self._clock()
        with self.transaction() as conn:
            conn.execute(
                'INSERT INTO invitations(invitation_id,kind,code_prefix,code_hash,label,workspace_id,'
                'owner_user_id,state,origin,created_at,expires_at,max_uses) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (
                    invitation_id, str(kind), code_prefix, code_hash, label, workspace_id,
                    owner_user_id, str(InvitationState.ACTIVE), origin, now, expires_at, max_uses,
                ),
            )
        return self.require_invitation(invitation_id)

    def require_invitation(self, invitation_id: str) -> Invitation:
        with self._lock:
            row = self._conn.execute(
                'SELECT * FROM invitations WHERE invitation_id=?', (invitation_id,)
            ).fetchone()
        if row is None:
            raise InvalidInvitation('邀请码不存在')
        return _invitation_from_row(row)

    def find_invitation_by_hash(self, code_hash: str) -> Invitation | None:
        with self._lock:
            row = self._conn.execute(
                'SELECT * FROM invitations WHERE code_hash=?', (code_hash,)
            ).fetchone()
        return _invitation_from_row(row) if row else None

    def list_invitations(self, *, kind: str | None = None) -> list[Invitation]:
        sql = 'SELECT * FROM invitations'
        params: tuple[Any, ...] = ()
        if kind is not None:
            sql += ' WHERE kind=?'
            params = (str(kind),)
        sql += ' ORDER BY created_at, invitation_id'
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_invitation_from_row(row) for row in rows]

    def list_invitations_for_workspace(self, workspace_id: str) -> list[Invitation]:
        with self._lock:
            rows = self._conn.execute(
                'SELECT * FROM invitations WHERE workspace_id=? ORDER BY created_at, invitation_id',
                (workspace_id,),
            ).fetchall()
        return [_invitation_from_row(row) for row in rows]

    def revoke_invitation(self, invitation_id: str) -> Invitation:
        """撤销邀请码。写入持久 tombstone，即使后续配置重新包含同一个
        系统码，启动时也不会把它复活。"""

        with self.transaction() as conn:
            row = conn.execute(
                'SELECT state FROM invitations WHERE invitation_id=?', (invitation_id,)
            ).fetchone()
            if row is None:
                raise InvalidInvitation('邀请码不存在')
            conn.execute(
                "UPDATE invitations SET state=?,revoked_at=COALESCE(revoked_at,?) WHERE invitation_id=?",
                (str(InvitationState.REVOKED), self._clock(), invitation_id),
            )
        return self.require_invitation(invitation_id)

    def revoke_invitations_for_workspace(self, workspace_id: str) -> int:
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE invitations SET state=?,revoked_at=COALESCE(revoked_at,?) "
                "WHERE workspace_id=? AND state=?",
                (str(InvitationState.REVOKED), self._clock(), workspace_id, str(InvitationState.ACTIVE)),
            )
        return int(cursor.rowcount)

    def register_invitation_use(self, invitation_id: str) -> Invitation:
        with self.transaction() as conn:
            cursor = conn.execute(
                'UPDATE invitations SET use_count=use_count+1,last_used_at=? WHERE invitation_id=?',
                (self._clock(), invitation_id),
            )
            if cursor.rowcount != 1:
                raise InvalidInvitation('邀请码不存在')
            row = conn.execute(
                'SELECT * FROM invitations WHERE invitation_id=?', (invitation_id,)
            ).fetchone()
        return _invitation_from_row(row)

    def invitation_tombstone_exists(self, code_hash: str) -> bool:
        """是否存在同一密码的历史记录（包括已撤销）。

        启动时用它避免把配置文件里仍然存在的已撤销系统码重新种入。
        """

        with self._lock:
            row = self._conn.execute(
                'SELECT 1 FROM invitations WHERE code_hash=? LIMIT 1', (code_hash,)
            ).fetchone()
        return row is not None

    # ---------------------------------------------------------------- 租约
    def get_lease(self, lease_id: str) -> Lease | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM leases WHERE lease_id = ?", (lease_id,)
            ).fetchone()
        return _lease_from_row(row) if row else None

    def require_lease(self, lease_id: str) -> Lease:
        lease = self.get_lease(lease_id)
        if lease is None:
            raise LeaseNotFound("租约不存在")
        return lease

    def get_lease_by_slot(self, slot_id: int) -> Lease | None:
        """返回该槽位上仍占用它的租约（最新的）。"""

        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM leases WHERE slot_id = ? AND state IN (?, ?, ?, ?) "
                "ORDER BY created_at DESC LIMIT 1",
                (
                    int(slot_id),
                    str(LeaseState.PENDING),
                    str(LeaseState.ACTIVE),
                    str(LeaseState.EXPIRED),
                    str(LeaseState.REVOKED),
                ),
            ).fetchone()
        return _lease_from_row(row) if row else None

    def list_live_leases(self) -> list[Lease]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM leases WHERE state IN (?, ?, ?, ?) ORDER BY created_at",
                (
                    str(LeaseState.PENDING),
                    str(LeaseState.ACTIVE),
                    str(LeaseState.EXPIRED),
                    str(LeaseState.REVOKED),
                ),
            ).fetchall()
        return [_lease_from_row(row) for row in rows]

    def update_lease(
        self,
        lease_id: str,
        *,
        state: LeaseState | None = None,
        upstream: str | None = None,
        expires_at: float | None = None,
        last_seen_at: float | None = None,
        failure_reason: str | None = None,
        clear_failure: bool = False,
    ) -> Lease:
        sets: list[str] = []
        params: list[Any] = []
        if state is not None:
            sets.append("state = ?")
            params.append(str(state))
        if upstream is not None:
            sets.append("upstream = ?")
            params.append(upstream)
        if expires_at is not None:
            sets.append("expires_at = ?")
            params.append(float(expires_at))
        if last_seen_at is not None:
            sets.append("last_seen_at = ?")
            params.append(float(last_seen_at))
        if failure_reason is not None:
            sets.append("failure_reason = ?")
            params.append(failure_reason)
        if clear_failure:
            sets.append("failure_reason = NULL")
        sets.append("updated_at = ?")
        params.append(self._clock())
        params.append(lease_id)

        with self.transaction() as conn:
            conn.execute(f"UPDATE leases SET {', '.join(sets)} WHERE lease_id = ?", params)
            if state in {LeaseState.RECLAIMED, LeaseState.FAILED}:
                conn.execute('DELETE FROM lease_pages WHERE lease_id=?', (lease_id,))
            row = conn.execute("SELECT * FROM leases WHERE lease_id = ?", (lease_id,)).fetchone()
        if row is None:
            raise LeaseNotFound(f"租约 {lease_id} 不存在")
        return _lease_from_row(row)

    def touch_lease(self, lease_id: str, *, at: float | None = None) -> None:
        """记录一次业务访问，供空闲回收判定。"""

        moment = self._clock() if at is None else at
        with self.transaction() as conn:
            conn.execute(
                "UPDATE leases SET last_seen_at = ?, updated_at = ? WHERE lease_id = ?",
                (moment, moment, lease_id),
            )
            conn.execute(
                "UPDATE workspaces SET last_used_at=? WHERE state='active' AND workspace_id="
                '(SELECT workspace_id FROM leases WHERE lease_id=?)', (moment, lease_id),
            )

    def record_event(
        self,
        kind: str,
        *,
        lease_id: str | None = None,
        slot_id: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO events(at, kind, lease_id, slot_id, payload) VALUES (?, ?, ?, ?, ?)",
                (
                    self._clock(),
                    kind,
                    lease_id,
                    int(slot_id) if slot_id is not None else None,
                    json.dumps(payload or {}, ensure_ascii=False),
                ),
            )

    def recent_events(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "at": float(row["at"]),
                "kind": row["kind"],
                "lease_id": row["lease_id"],
                "slot_id": row["slot_id"],
                "payload": json.loads(row["payload"]) if row["payload"] else {},
            }
            for row in rows
        ]
