"""UnitOfWork：显式事务边界，repository 不裸开事务（设计 §4.3）。

写入路径统一用 ``BEGIN IMMEDIATE``：它在事务开始就取写锁，因此
"读-改-写"不会因为执行到一半才升级锁而立刻 ``SQLITE_BUSY``；配合
``busy_timeout``（见 ``database.py``）表现为按序等待而不是失败。
读路径用 ``BEGIN DEFERRED``，允许并发读。

契约：

- ``with factory() as uow`` 打开事务；
- 只有显式 ``uow.commit()`` 才落盘；异常或未提交退出即 ``ROLLBACK``；
- **模型调用在事务外**：不要在持有 ``uow`` 时调用 provider。
"""

from __future__ import annotations

import sqlite3
from types import TracebackType

from backend.application.ports.unit_of_work import Repositories
from backend.domain.errors import DomainError
from backend.infrastructure.sqlite.database import Database
from backend.infrastructure.sqlite.repositories import (
    SqliteArtifactRepository,
    SqliteDocumentRepository,
    SqliteEdgeRepository,
    SqliteExplanationRepository,
    SqliteExtractionRepository,
    SqliteNarrationRepository,
    SqliteNodeRepository,
    SqliteProjectRepository,
    SqliteResourceRepository,
    SqliteRevisionRepository,
    SqliteRunEventRepository,
    SqliteRunRepository,
    SqliteWorkspaceBindingRepository,
)


class SqliteUnitOfWork:
    """一个 ``BEGIN IMMEDIATE`` 事务作用域。"""

    __slots__ = ("_conn", "_repos", "_artifacts", "_finished", "_nested_depth", "_writer")

    def __init__(self, conn: sqlite3.Connection, *, writer: bool = True) -> None:
        self._conn = conn
        self._writer = writer
        self._finished = False
        self._nested_depth = 0
        conn.execute("BEGIN IMMEDIATE" if writer else "BEGIN DEFERRED")
        self._repos = Repositories(
            projects=SqliteProjectRepository(conn),
            documents=SqliteDocumentRepository(conn),
            revisions=SqliteRevisionRepository(conn),
            resources=SqliteResourceRepository(conn),
            extractions=SqliteExtractionRepository(conn),
            nodes=SqliteNodeRepository(conn),
            edges=SqliteEdgeRepository(conn),
            explanations=SqliteExplanationRepository(conn),
            narrations=SqliteNarrationRepository(conn),
            runs=SqliteRunRepository(conn),
            run_events=SqliteRunEventRepository(conn),
            binding=SqliteWorkspaceBindingRepository(conn),
        )
        self._artifacts = SqliteArtifactRepository(conn)

    # ------------------------------------------------------------ 访问
    @property
    def repos(self) -> Repositories:
        return self._repos

    @property
    def artifacts(self) -> SqliteArtifactRepository:
        """artifacts/builds 不在 ``Repositories`` 里（端口是 ``ArtifactStore``）。"""

        return self._artifacts

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    # ------------------------------------------------------------ 提交/回滚
    def commit(self) -> None:
        if self._finished:
            raise DomainError("该事务已结束，不能重复 commit", code="transaction_closed", http_status=500)
        self._conn.execute("COMMIT")
        self._finished = True

    def rollback(self) -> None:
        if self._finished:
            return
        try:
            self._conn.execute("ROLLBACK")
        finally:
            self._finished = True

    # ------------------------------------------------------------ 上下文
    def __enter__(self) -> "SqliteUnitOfWork":
        self._nested_depth += 1
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        self._nested_depth -= 1
        if self._nested_depth > 0:
            # 内层只做作用域嵌套，不提前提交/回滚：事务边界归最外层。
            return None
        if exc_type is not None or not self._finished:
            self.rollback()
        return None


class SqliteUnitOfWorkFactory:
    """按调用创建 UoW；每次取当前线程的连接。"""

    __slots__ = ("_database", "_writer")

    def __init__(self, database: Database, *, writer: bool = True) -> None:
        self._database = database
        self._writer = writer

    def __call__(self) -> SqliteUnitOfWork:
        return SqliteUnitOfWork(self._database.connect(), writer=self._writer)

    def reader(self) -> SqliteUnitOfWork:
        """只读事务（``BEGIN DEFERRED``）；写操作会报 ``SQLITE_READONLY``。"""

        return SqliteUnitOfWork(self._database.connect(), writer=False)


__all__ = ["SqliteUnitOfWork", "SqliteUnitOfWorkFactory"]
