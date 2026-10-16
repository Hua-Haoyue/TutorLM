"""迁移：``meta.schema_version`` 驱动的**显式**升级路径。

规则：

- 迁移在一个事务内完成；失败即整体回滚，不留下半套 schema。
  （因此**不**用 ``executescript``：它会在执行前隐式 COMMIT 掉我们的事务。）
- 版本比代码更新 -> 拒绝打开（旧代码读新库会静默丢字段）。
- ``SCHEMA_VERSION`` 与 ``schema.py`` 的 DDL 一起演进；新增一步就在这里加一个
  函数并注册到 ``MIGRATIONS``。
"""

from __future__ import annotations

import sqlite3
from typing import Callable, Iterator

from backend.domain.errors import StorageNotInitialized
from backend.infrastructure.sqlite.database import Database
from backend.infrastructure.sqlite.schema import SCHEMA_VERSION, SCHEMA_SQL

MIGRATION_VERSION_KEY = "schema_version"

#: ``version -> 应用该版本的函数``；当前版本 1 仅依赖 ``SCHEMA_SQL``。
MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {}


def _iter_statements(sql: str) -> Iterator[str]:
    """把 DDL 拆成单条语句。

    ``SCHEMA_SQL`` 里没有含分号的字符串字面量，因此按 ``;`` 切分是安全的；
    这里额外丢掉注释行与空语句，保持事务内逐条执行、可回滚。
    """

    for raw in sql.split(";"):
        lines = [line for line in raw.splitlines() if not line.strip().startswith("--")]
        statement = "\n".join(lines).strip()
        if statement:
            yield statement


def current_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (MIGRATION_VERSION_KEY,)
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    if row is None:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        raise StorageNotInitialized(f"meta.{MIGRATION_VERSION_KEY} 不是整数：{row[0]!r}") from None


def migrate(database: Database) -> int:
    """把库升级到 ``SCHEMA_VERSION``；返回最终版本。"""

    conn = database.connect()
    version = current_version(conn)
    if version > SCHEMA_VERSION:
        raise StorageNotInitialized(
            "业务库 schema 版本高于当前代码：拒绝打开（避免旧代码静默丢字段）",
            details={"database": version, "code": SCHEMA_VERSION},
        )
    if version == SCHEMA_VERSION and _is_initialized(conn):
        return version

    _reject_foreign_schema(conn, version, database=database)
    conn.execute("BEGIN IMMEDIATE")
    try:
        for statement in _iter_statements(SCHEMA_SQL):
            conn.execute(statement)
        for step in range(version + 1, SCHEMA_VERSION + 1):
            migration = MIGRATIONS.get(step)
            if migration is not None:
                migration(conn)
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (MIGRATION_VERSION_KEY, str(SCHEMA_VERSION)),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return SCHEMA_VERSION


def _is_initialized(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'projects'"
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None



#: 本 foundation schema 拥有的表（与 ``schema.py`` 的 DDL 一致）。
_OWNED_TABLES: frozenset[str] = frozenset(
    {
        "meta",
        "projects",
        "documents",
        "document_revisions",
        "document_resources",
        "document_extractions",
        "document_artifacts",
        "document_builds",
        "nodes",
        "edges",
        "explanations",
        "narrations",
        "narration_segments",
        "runs",
        "run_events",
        "workspace_binding",
        "workspace_events",
    }
)


def _reject_foreign_schema(conn: sqlite3.Connection, version: int, *, database: Database) -> None:
    """拒绝接管**不是本 foundation 建的**库。

    容器内当前还有 ``host/local/store.py`` 这个临时 adapter，它自建一套
    *不同* 的 schema（无 ``meta`` 表、``runs.idempotency_scope`` 语义不同等）。
    若把两者指向同一个 ``business.sqlite3``，本模块的 ``CREATE TABLE IF NOT EXISTS``
    会静默沿用对方的列定义，之后所有读写都会按错误的形状解析——这是静默数据损坏，
    不属于"暂时不做"。

    因此：库中存在表、且**没有** ``meta`` 表时判定为外来库，直接 ``StorageNotInitialized``
    并给出可操作原因（迁移或另选库），而不是"顺手"建一套同名表覆盖它。
    """

    if version != 0:
        return
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    except sqlite3.OperationalError:  # pragma: no cover - 空库不会走到这里
        return
    existing = {str(row[0]) for row in rows}
    if not existing:
        return
    if "meta" in existing:
        # 有 meta 但没有版本行：可能是被截断的库，按 0 处理继续迁移即可。
        return
    foreign = sorted(existing - _OWNED_TABLES) or sorted(existing)
    raise StorageNotInitialized(
        "业务库不是本 foundation 建的：缺少 meta 与 schema_version。"
        "拒绝以 CREATE TABLE IF NOT EXISTS 静默沿用外来列定义（那会按错误形状解析数据）。",
        details={"database": database.path, "foreign_tables": foreign[:16]},
    )
