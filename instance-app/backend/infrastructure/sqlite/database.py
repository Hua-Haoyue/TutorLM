"""SQLite 连接与 PRAGMA 设置（WAL、外键、busy timeout）。

并发约定（设计 §4.3）：库开 **WAL**，写事务走 ``BEGIN IMMEDIATE``；这样
多线程写不会因为"升级读锁失败"而立刻 ``SQLITE_BUSY``，而是按 ``busy_timeout``
串行等待。读可以并发进行。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

from backend.domain.errors import StorageNotInitialized

#: 写锁等待上限：容器内是单实例、多线程，等待 5s 足够而不会把请求钉死。
BUSY_TIMEOUT_MS = 5000

#: 应用层一致性保护：外键必须真正生效（``nodes.parent_id`` 的 ``SET NULL`` 语义
#: 依赖它）。
_APPLICATION_PRAGMAS = (
    "PRAGMA foreign_keys = ON",
    f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}",
    "PRAGMA synchronous = NORMAL",
)


def configure_connection(conn: sqlite3.Connection) -> None:
    """设置每个连接都必须有的 PRAGMA。WAL 是库级属性，另行设置一次。"""

    conn.row_factory = sqlite3.Row
    for statement in _APPLICATION_PRAGMAS:
        conn.execute(statement)


class Database:
    """一个业务数据库路径的连接工厂（每线程一个连接）。

    ``sqlite3`` 的默认 ``check_same_thread`` 限制让连接不能跨线程复用；容器内
    是"单进程 + 线程池"，因此这里用线程局部连接：同一线程顺序复用，
    不同线程各自持有连接与自己的事务。
    """

    __slots__ = ("_path", "_uri", "_local", "_lock", "_closed", "_timeout")

    def __init__(self, path: str, *, timeout: float = BUSY_TIMEOUT_MS / 1000) -> None:
        self._path = str(path)
        self._uri = Path(self._path).as_uri() if self._path.startswith("/") else self._path
        self._local = threading.local()
        self._lock = threading.Lock()
        self._closed = False
        self._timeout = timeout

    # ------------------------------------------------------------ 属性
    @property
    def path(self) -> str:
        return self._path

    # ------------------------------------------------------------ 生命周期
    def connect(self) -> sqlite3.Connection:
        """取当前线程的连接（不存在则新建）。"""

        if self._closed:
            raise StorageNotInitialized("数据库已关闭")
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._path, timeout=self._timeout, isolation_level=None)
            configure_connection(conn)
            self._local.conn = conn
        return conn

    def close(self) -> None:
        with self._lock:
            self._closed = True
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            finally:
                self._local.conn = None

    def close_all_threads(self) -> None:  # pragma: no cover - 仅供 host 收尾
        self.close()

    # ------------------------------------------------------------ schema/迁移
    def enable_wal(self) -> str:
        """开启 WAL 并返回实际 ``journal_mode``（供就绪/测试断言）。"""

        conn = self.connect()
        mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        return str(mode).lower()

    def journal_mode(self) -> str:
        conn = self.connect()
        row = conn.execute("PRAGMA journal_mode").fetchone()
        return str(row[0]).lower() if row else ""

    def meta_get(self, key: str) -> str | None:
        conn = self.connect()
        try:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        except sqlite3.OperationalError:
            return None
        return None if row is None else str(row[0])

    def meta_set(self, key: str, value: str) -> None:
        conn = self.connect()
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def table_names(self) -> frozenset[str]:
        conn = self.connect()
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        return frozenset(str(row[0]) for row in rows)

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        return self.connect().execute(sql, params)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"Database({self._path!r})"


def open_database(path: str) -> Database:
    """打开（必要时创建）业务库并开启 WAL。

    目录必须**已存在**：应用不替调用方创建数据根（数据根存在性是就绪前置，
    不是本函数职责）。刻意不 ``makedirs``，避免"以为挂上了卷，
    其实是写进容器可写层"这种静默错误。
    """

    parent = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(parent):
        raise StorageNotInitialized(
            f"业务库所在目录不存在：{parent}（拒绝静默创建，避免写进容器可写层）",
            details={"path": path},
        )
    database = Database(path)
    database.enable_wal()
    return database
