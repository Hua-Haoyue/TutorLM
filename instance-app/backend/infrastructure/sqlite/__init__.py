"""SQLite 实现：WAL、schema/迁移、UnitOfWork 与 repository。

已落盘：``database``（连接/WAL/PRAGMA）、``migrations``、``schema``、
``repositories``（各表实现）、``unit_of_work``（显式事务边界）。
"""

from __future__ import annotations

from backend.infrastructure.sqlite import database, migrations, repositories, schema, unit_of_work
from backend.infrastructure.sqlite.database import Database, open_database
from backend.infrastructure.sqlite.migrations import migrate
from backend.infrastructure.sqlite.repositories import SqliteArtifactRepository
from backend.infrastructure.sqlite.schema import SCHEMA_SQL, SCHEMA_VERSION
from backend.infrastructure.sqlite.unit_of_work import SqliteUnitOfWork, SqliteUnitOfWorkFactory

__all__ = [
    "Database",
    "SCHEMA_SQL",
    "SCHEMA_VERSION",
    "SqliteArtifactRepository",
    "SqliteUnitOfWork",
    "SqliteUnitOfWorkFactory",
    "database",
    "migrate",
    "migrations",
    "open_database",
    "repositories",
    "schema",
    "unit_of_work",
]
