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
    Lease,
    LeaseState,
    Slot,
    SlotState,
    WorkloadState,
)
from control_plane.errors import CapacityError, LeaseNotFound, SlotBusy

SCHEMA_VERSION = 1

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
            self._conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
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
    ) -> Lease:
        """原子分配：选空闲槽位 + 建租约 + 占位。

        在同一个写事务里完成，并用条件更新 ``WHERE state = 'free'`` 兜底，
        因此并发调用不会拿到同一个槽位。容量耗尽抛 :class:`CapacityError`。
        """

        now = self._clock()
        with self.transaction() as conn:
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
                "created_at, updated_at, expires_at, last_seen_at, label) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                ),
            )
            conn.execute(
                "INSERT INTO events(at, kind, lease_id, slot_id, payload) VALUES (?, ?, ?, ?, ?)",
                (now, "lease.allocated", lease_id, slot_id, json.dumps({"generation": generation})),
            )
        lease = self.get_lease(lease_id)
        assert lease is not None
        return lease

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
