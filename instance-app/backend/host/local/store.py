"""Fallback SQLite repositories and content file storage.

Schema mirrors docs/instance-app-design.md section 4.3. Only the columns the
documented API needs are implemented; the foundation workstream is expected to
supersede this module behind `application/ports`.

Conventions kept identical to the documented design:

* WAL journal mode; every write wrapped in an explicit transaction;
* model/provider calls happen outside a transaction (see `host/runs.py`);
* content files live under `TUTORLM_DATA_DIR/sources/<document_id>/<revision_id>/`
  and are immutable; derived output lives under `artifacts/`;
* filesystem paths inside the volume are always resolved and re-checked.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
from contextlib import contextmanager
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..errors import Conflict, NotFound

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS workspace_binding (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  current_workspace_id TEXT,
  origin_workspace_id TEXT,
  owner_id TEXT,
  copy_id TEXT,
  storage_layout TEXT NOT NULL DEFAULT 'legacy_root',
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  description TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  title TEXT NOT NULL,
  source_filename TEXT,
  format TEXT NOT NULL,
  status TEXT NOT NULL,
  current_revision_id TEXT,
  order_index INTEGER NOT NULL DEFAULT 0,
  size_bytes INTEGER NOT NULL DEFAULT 0,
  extra_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_project ON documents(project_id, order_index);

CREATE TABLE IF NOT EXISTS document_revisions (
  id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL,
  revision_no INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  status TEXT NOT NULL,
  main_rel_path TEXT NOT NULL,
  resources_json TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_revisions_document ON document_revisions(document_id, revision_no);

CREATE TABLE IF NOT EXISTS nodes (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  document_id TEXT,
  title TEXT NOT NULL,
  kind TEXT NOT NULL,
  summary TEXT,
  content TEXT,
  order_index INTEGER NOT NULL DEFAULT 0,
  parent_id TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  revision INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nodes_project ON nodes(project_id);

CREATE TABLE IF NOT EXISTS edges (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  source_node_id TEXT NOT NULL,
  target_node_id TEXT NOT NULL,
  relation TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_edges_project ON edges(project_id);

CREATE TABLE IF NOT EXISTS explanations (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  node_id TEXT NOT NULL,
  question TEXT,
  answer TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_explanations_project ON explanations(project_id, created_at);

CREATE TABLE IF NOT EXISTS narrations (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  title TEXT NOT NULL,
  segment_count INTEGER NOT NULL,
  total_duration_seconds REAL NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  segments_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_narrations_project ON narrations(project_id, created_at);

CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  project_id TEXT,
  kind TEXT NOT NULL,
  workload TEXT NOT NULL DEFAULT 'model',
  state TEXT NOT NULL,
  progress INTEGER,
  input_json TEXT NOT NULL DEFAULT '{}',
  result_json TEXT,
  error_code TEXT,
  error_message TEXT,
  idempotency_key TEXT,
  idempotency_scope TEXT,
  session_id TEXT,
  target_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_project ON runs(project_id, created_at);
CREATE INDEX IF NOT EXISTS idx_runs_scope ON runs(idempotency_scope, idempotency_key);

CREATE TABLE IF NOT EXISTS document_extractions (
  id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL,
  revision_id TEXT NOT NULL,
  extractor_version TEXT NOT NULL,
  status TEXT NOT NULL,
  text_hash TEXT,
  page_count INTEGER,
  error_code TEXT,
  artifact_id TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_extractions_revision ON document_extractions(revision_id, extractor_version);

CREATE TABLE IF NOT EXISTS document_artifacts (
  id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL,
  revision_id TEXT NOT NULL,
  build_id TEXT,
  kind TEXT NOT NULL,
  rel_path TEXT NOT NULL,
  content_hash TEXT,
  size_bytes INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_revision ON document_artifacts(revision_id);

CREATE TABLE IF NOT EXISTS document_builds (
  id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL,
  revision_id TEXT NOT NULL,
  status TEXT NOT NULL,
  artifact_id TEXT,
  engine_version TEXT,
  source_map_json TEXT,
  error_code TEXT,
  error_message TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_builds_revision ON document_builds(revision_id, created_at);

CREATE TABLE IF NOT EXISTS learner_profiles (
  project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
  profile_json TEXT NOT NULL,
  revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS node_learning (
  node_id TEXT PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
  state_json TEXT NOT NULL,
  revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS run_events (
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  PRIMARY KEY (run_id, seq)
);
"""

RUN_STATES = ("queued", "running", "completed", "failed", "cancelled", "interrupted")
TERMINAL_STATES = ("completed", "failed", "cancelled", "interrupted")

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class PathEscape(Exception):
    """Raised when a computed path leaves its storage root."""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def new_id() -> str:
    return str(uuid.uuid4())


def safe_join(root: Path, *parts: str) -> Path:
    """Join `parts` under `root`, rejecting traversal and symlink escapes."""
    root_resolved = Path(root).resolve()
    candidate = root_resolved
    for part in parts:
        if part in ("", ".", "..") or "/" in part or "\\" in part or os.path.isabs(part):
            raise PathEscape(f"unsafe path segment: {part!r}")
        candidate = candidate / part
    resolved = candidate.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise PathEscape("resolved path escapes the storage root")
    return resolved


def safe_relative_path(value: str) -> str:
    """Validate a canonical project-relative path (tex resources, main entry)."""
    cleaned = value.replace("\\", "/").strip()
    if not cleaned or cleaned.startswith("/") or cleaned.startswith("~"):
        raise PathEscape(f"unsafe relative path: {value!r}")
    parts = [p for p in cleaned.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise PathEscape(f"unsafe relative path: {value!r}")
    if any(":" in p for p in parts):
        raise PathEscape(f"unsafe relative path: {value!r}")
    return "/".join(parts)


def sanitize_filename(value: str | None, fallback: str = "main") -> str:
    if not value:
        return fallback
    name = os.path.basename(value.replace("\\", "/"))
    name = _UNSAFE_NAME.sub("_", name).strip("._-") or fallback
    return name[:120]


def content_hash(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()

class _TransactionConnection:
    """Nested repository operations borrow the outer Unit of Work connection."""

    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, parameters=()):
        if sql.strip().split()[0].upper() in {'BEGIN', 'COMMIT', 'ROLLBACK'}:
            return self.connection.execute('SELECT 1')
        return self.connection.execute(sql, parameters)

    def close(self):
        pass


class LocalStore:
    """SQLite-backed repository bundle used until domain/infrastructure land.

    Each public method opens its own short connection: WAL plus explicit
    transactions keeps this safe under the bounded executor threads without
    owning a connection pool.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ddl_lock = threading.Lock()
        self.lock = threading.RLock()
        self._local = threading.local()
        self._initialize()

    # -- infrastructure --------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        if getattr(self._local, 'connection', None) is not None:
            return _TransactionConnection(self._local.connection)
        conn = sqlite3.connect(str(self.db_path), timeout=15.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _initialize(self) -> None:
        with self._ddl_lock:
            conn = self._connect()
            try:
                conn.executescript(SCHEMA)
                columns = {r['name'] for r in conn.execute('PRAGMA table_info(explanations)')}
                if 'learning_json' not in columns:
                    conn.execute("ALTER TABLE explanations ADD COLUMN learning_json TEXT NOT NULL DEFAULT '{}'")
            finally:
                conn.close()

    def close(self) -> None:
        return None

    @contextmanager
    def transaction(self):
        with self.lock:
            if getattr(self._local, 'connection', None) is not None:
                yield self._local.connection
                return
            conn = self._connect()
            conn.execute('BEGIN IMMEDIATE')
            self._local.connection = conn
            try:
                yield conn
                conn.execute('COMMIT')
            except BaseException:
                conn.execute('ROLLBACK')
                raise
            finally:
                self._local.connection = None
                conn.close()

    @staticmethod
    def _rows(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
        return [dict(row) for row in cur.fetchall()]

    # -- workspace binding ----------------------------------------------
    def read_binding(self) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM workspace_binding WHERE id = 1").fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def ensure_binding(self, workspace_id: str, owner_id: str | None) -> dict[str, Any]:
        existing = self.read_binding()
        if existing is not None:
            return existing
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO workspace_binding (id, current_workspace_id, origin_workspace_id, owner_id,"
                " copy_id, updated_at) VALUES (1, ?, ?, ?, NULL, ?) ON CONFLICT(id) DO NOTHING",
                (workspace_id, workspace_id, owner_id, utcnow()),
            )
            conn.execute("COMMIT")
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return self.read_binding() or {}

    def rebind_workspace(
        self,
        *,
        workspace_id: str,
        owner_id: str | None,
        source_id: str | None,
        copy_id: str | None,
    ) -> None:
        """Re-bind only the access context; business stable ids stay untouched."""
        now = utcnow()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO workspace_binding (id, current_workspace_id, origin_workspace_id, owner_id,"
                " copy_id, updated_at) VALUES (1, ?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET"
                "   current_workspace_id = excluded.current_workspace_id,"
                "   origin_workspace_id = COALESCE(workspace_binding.origin_workspace_id, excluded.origin_workspace_id,"
                "                                   excluded.current_workspace_id),"
                "   owner_id = excluded.owner_id,"
                "   copy_id = COALESCE(excluded.copy_id, workspace_binding.copy_id),"
                "   updated_at = excluded.updated_at",
                (workspace_id, source_id, owner_id, copy_id, now),
            )
            conn.execute("COMMIT")
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def save_binding(self, binding: Any) -> None:
        """Upsert the single access-context row (domain ``WorkspaceBinding``)."""
        now = utcnow()
        layout = getattr(binding, "storage_layout", None)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO workspace_binding (id, current_workspace_id, origin_workspace_id, owner_id,"
                " copy_id, storage_layout, updated_at) VALUES (1, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET"
                "   current_workspace_id = excluded.current_workspace_id,"
                "   origin_workspace_id = excluded.origin_workspace_id,"
                "   owner_id = excluded.owner_id,"
                "   copy_id = excluded.copy_id,"
                "   storage_layout = excluded.storage_layout,"
                "   updated_at = excluded.updated_at",
                (
                    str(binding.current_workspace_id),
                    str(binding.origin_workspace_id),
                    str(binding.owner_id),
                    (str(binding.copy_id) if getattr(binding, "copy_id", None) else None),
                    (str(layout) if layout is not None else "legacy_root"),
                    now,
                ),
            )
            conn.execute("COMMIT")
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # -- projects --------------------------------------------------------
    def get_project(self, project_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_current_project(self, workspace_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM projects WHERE workspace_id = ?", (workspace_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def create_project(self, workspace_id: str, title: str, description: str | None = None) -> dict[str, Any]:
        now = utcnow()
        project_id = new_id()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO projects (id, workspace_id, title, description, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (project_id, workspace_id, title, description, now, now),
            )
            conn.execute("COMMIT")
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        project = self.get_project(project_id)
        assert project is not None
        return project

    def update_project(self, project_id: str, **fields: Any) -> dict[str, Any]:
        allowed = {k: v for k, v in fields.items() if k in ("title", "description") and v is not None}
        if allowed:
            assignments = ", ".join(f"{key} = ?" for key in allowed)
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    f"UPDATE projects SET {assignments}, updated_at = ? WHERE id = ?",
                    (*allowed.values(), utcnow(), project_id),
                )
                conn.execute("COMMIT")
            except sqlite3.Error:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()
        project = self.get_project(project_id)
        if project is None:
            raise NotFound("Project not found", code="project_not_found")
        return project

    def count_documents(self, project_id: str) -> int:
        conn = self._connect()
        try:
            row = conn.execute("SELECT COUNT(*) AS n FROM documents WHERE project_id = ?", (project_id,)).fetchone()
            return int(row["n"])
        finally:
            conn.close()

    def purge_project_content(self, project_id: str) -> None:
        """Delete business rows and runs for one project (files removed separately)."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            doc_ids = [r["id"] for r in conn.execute("SELECT id FROM documents WHERE project_id = ?", (project_id,))]
            for doc_id in doc_ids:
                for table in ("document_revisions", "document_extractions", "document_artifacts", "document_builds"):
                    conn.execute(f"DELETE FROM {table} WHERE document_id = ?", (doc_id,))
            conn.execute("DELETE FROM edges WHERE project_id = ?", (project_id,))
            conn.execute("DELETE FROM nodes WHERE project_id = ?", (project_id,))
            conn.execute("DELETE FROM explanations WHERE project_id = ?", (project_id,))
            conn.execute("DELETE FROM narrations WHERE project_id = ?", (project_id,))
            conn.execute("DELETE FROM documents WHERE project_id = ?", (project_id,))
            conn.execute("DELETE FROM runs WHERE project_id = ?", (project_id,))
            conn.execute("DELETE FROM learner_profiles WHERE project_id = ?", (project_id,))
            conn.execute("COMMIT")
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
    # -- documents / revisions -------------------------------------------
    def list_documents(self, project_id: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT * FROM documents WHERE project_id = ? ORDER BY order_index ASC, created_at ASC",
                (project_id,),
            )
            return self._rows(cur)
        finally:
            conn.close()

    def get_document(self, document_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def next_order_index(self, project_id: str) -> int:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COALESCE(MAX(order_index), -1) AS n FROM documents WHERE project_id = ?", (project_id,)
            ).fetchone()
            return int(row["n"]) + 1
        finally:
            conn.close()

    def create_document(
        self,
        *,
        project_id: str,
        title: str,
        fmt: str,
        source_filename: str | None,
        status: str,
        size_bytes: int,
        revision: Mapping[str, Any],
        extra: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        now = utcnow()
        document_id = new_id()
        revision_id = str(revision["id"])
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT COALESCE(MAX(order_index), -1) AS n FROM documents WHERE project_id = ?", (project_id,)
            ).fetchone()
            order_index = int(row["n"]) + 1
            conn.execute(
                "INSERT INTO documents (id, project_id, title, source_filename, format, status,"
                " current_revision_id, order_index, size_bytes, extra_json, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    document_id,
                    project_id,
                    title,
                    source_filename,
                    fmt,
                    status,
                    revision_id,
                    order_index,
                    size_bytes,
                    json.dumps(extra, ensure_ascii=False) if extra else None,
                    now,
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO document_revisions (id, document_id, revision_no, content_hash, size_bytes,"
                " status, main_rel_path, resources_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    revision_id,
                    document_id,
                    int(revision["revision_no"]),
                    str(revision["content_hash"]),
                    int(revision["size_bytes"]),
                    str(revision["status"]),
                    str(revision["main_rel_path"]),
                    json.dumps(list(revision.get("resources") or []), ensure_ascii=False),
                    now,
                ),
            )
            conn.execute("COMMIT")
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        document = self.get_document(document_id)
        created = self.get_revision(revision_id)
        assert document is not None and created is not None
        return document, created

    def update_document(self, document_id: str, **fields: Any) -> dict[str, Any]:
        allowed = {
            k: v
            for k, v in fields.items()
            if k
            in ("title", "status", "current_revision_id", "size_bytes", "source_filename", "format", "extra_json")
        }
        if allowed:
            assignments = ", ".join(f"{key} = ?" for key in allowed)
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    f"UPDATE documents SET {assignments}, updated_at = ? WHERE id = ?",
                    (*allowed.values(), utcnow(), document_id),
                )
                conn.execute("COMMIT")
            except sqlite3.Error:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()
        document = self.get_document(document_id)
        if document is None:
            raise NotFound("Document not found", code="document_not_found")
        return document

    def delete_document(self, document_id: str) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for table in ("document_revisions", "document_extractions", "document_artifacts", "document_builds"):
                conn.execute(f"DELETE FROM {table} WHERE document_id = ?", (document_id,))
            conn.execute("DELETE FROM nodes WHERE document_id = ?", (document_id,))
            conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))
            conn.execute("COMMIT")
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def next_revision_no(self, document_id: str) -> int:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COALESCE(MAX(revision_no), 0) AS n FROM document_revisions WHERE document_id = ?",
                (document_id,),
            ).fetchone()
            return int(row["n"]) + 1
        finally:
            conn.close()

    def add_revision(
        self,
        *,
        document_id: str,
        revision: Mapping[str, Any],
        size_bytes: int,
        status: str,
        fmt: str | None = None,
        source_filename: str | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Append an immutable revision and point `current_revision_id` at it."""
        now = utcnow()
        revision_id = str(revision["id"])
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO document_revisions (id, document_id, revision_no, content_hash, size_bytes,"
                " status, main_rel_path, resources_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    revision_id,
                    document_id,
                    int(revision["revision_no"]),
                    str(revision["content_hash"]),
                    int(revision["size_bytes"]),
                    str(revision["status"]),
                    str(revision["main_rel_path"]),
                    json.dumps(list(revision.get("resources") or []), ensure_ascii=False),
                    now,
                ),
            )
            sets = ["current_revision_id = ?", "size_bytes = ?", "status = ?", "updated_at = ?"]
            params: list[Any] = [revision_id, size_bytes, status, now]
            if fmt:
                sets.insert(0, "format = ?")
                params.insert(0, fmt)
            if source_filename:
                sets.append("source_filename = ?")
                params.append(source_filename)
            if extra is not None:
                sets.append("extra_json = ?")
                params.append(json.dumps(extra, ensure_ascii=False))
            params.append(document_id)
            conn.execute(f"UPDATE documents SET {', '.join(sets)} WHERE id = ?", tuple(params))
            conn.execute("COMMIT")
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        document = self.get_document(document_id)
        created = self.get_revision(revision_id)
        assert document is not None and created is not None
        return document, created

    def list_revisions(self, document_id: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT * FROM document_revisions WHERE document_id = ? ORDER BY revision_no DESC",
                (document_id,),
            )
            return self._rows(cur)
        finally:
            conn.close()

    def get_revision(self, revision_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM document_revisions WHERE id = ?", (revision_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def current_revision(self, document: Mapping[str, Any]) -> dict[str, Any] | None:
        revision_id = document.get("current_revision_id")
        return self.get_revision(str(revision_id)) if revision_id else None
    # -- nodes / edges ---------------------------------------------------
    def list_nodes(self, project_id: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT * FROM nodes WHERE project_id = ? ORDER BY order_index ASC, created_at ASC", (project_id,)
            )
            return self._rows(cur)
        finally:
            conn.close()

    def get_node(self, node_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def create_node(
        self,
        *,
        project_id: str,
        document_id: str | None,
        title: str,
        kind: str,
        content: str | None,
        summary: str | None,
        parent_id: str | None,
        metadata: Mapping[str, Any],
    ) -> dict[str, Any]:
        now = utcnow()
        node_id = new_id()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT COALESCE(MAX(order_index), -1) AS n FROM nodes WHERE project_id = ?", (project_id,)
            ).fetchone()
            order_index = int(row["n"]) + 1
            if parent_id:
                parent = conn.execute("SELECT project_id FROM nodes WHERE id = ?", (parent_id,)).fetchone()
                if parent is None or parent["project_id"] != project_id:
                    raise Conflict("Parent node not found in this project", code="parent_not_found")
            if document_id:
                doc = conn.execute("SELECT project_id FROM documents WHERE id = ?", (document_id,)).fetchone()
                if doc is None or doc["project_id"] != project_id:
                    raise NotFound("Document not found in this project", code="document_not_found")
            conn.execute(
                "INSERT INTO nodes (id, project_id, document_id, title, kind, summary, content, order_index,"
                " parent_id, metadata_json, revision, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (
                    node_id,
                    project_id,
                    document_id,
                    title,
                    kind,
                    summary,
                    content,
                    order_index,
                    parent_id,
                    json.dumps(dict(metadata), ensure_ascii=False),
                    now,
                    now,
                ),
            )
            conn.execute("COMMIT")
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        node = self.get_node(node_id)
        assert node is not None
        return node

    def update_node(self, node_id: str, *, expected_revision: int | None, **fields: Any) -> dict[str, Any]:
        """Optimistic concurrency: a mismatching `revision` is a 409."""
        allowed = {
            k: v
            for k, v in fields.items()
            if k in ("title", "kind", "content", "summary", "parent_id", "metadata_json", "order_index")
        }
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
            if row is None:
                raise NotFound("Node not found", code="node_not_found")
            if expected_revision is not None and int(row["revision"]) != int(expected_revision):
                raise Conflict(
                    "Node was modified by someone else",
                    code="revision_conflict",
                    details={"revision": int(row["revision"])},
                )
            assignments = ", ".join(f"{key} = ?" for key in allowed)
            prefix = f"{assignments}, " if assignments else ""
            conn.execute(
                f"UPDATE nodes SET {prefix}revision = revision + 1, updated_at = ? WHERE id = ?",
                (*allowed.values(), utcnow(), node_id),
            )
            conn.execute("COMMIT")
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        node = self.get_node(node_id)
        assert node is not None
        return node

    def delete_node(self, node_id: str) -> None:
        """Delete one node; children are promoted to top level (no cascade)."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT id FROM nodes WHERE id = ?", (node_id,)).fetchone()
            if row is None:
                raise NotFound("Node not found", code="node_not_found")
            conn.execute("UPDATE nodes SET parent_id = NULL WHERE parent_id = ?", (node_id,))
            conn.execute("DELETE FROM edges WHERE source_node_id = ? OR target_node_id = ?", (node_id, node_id))
            conn.execute("DELETE FROM explanations WHERE node_id = ?", (node_id,))
            conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
            conn.execute("COMMIT")
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def list_edges(self, project_id: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT * FROM edges WHERE project_id = ? ORDER BY created_at ASC", (project_id,)
            )
            return self._rows(cur)
        finally:
            conn.close()

    def get_edge(self, edge_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM edges WHERE id = ?", (edge_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def create_edge(
        self,
        *,
        project_id: str,
        source_node_id: str,
        target_node_id: str,
        relation: str,
        metadata: Mapping[str, Any],
    ) -> dict[str, Any]:
        now = utcnow()
        edge_id = new_id()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for node_id in (source_node_id, target_node_id):
                node = conn.execute("SELECT project_id FROM nodes WHERE id = ?", (node_id,)).fetchone()
                if node is None or node["project_id"] != project_id:
                    raise NotFound("Node not found in this project", code="node_not_found")
            if source_node_id == target_node_id:
                raise Conflict("An edge cannot connect a node to itself", code="self_edge")
            conn.execute(
                "INSERT INTO edges (id, project_id, source_node_id, target_node_id, relation, metadata_json,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    edge_id,
                    project_id,
                    source_node_id,
                    target_node_id,
                    relation,
                    json.dumps(dict(metadata), ensure_ascii=False),
                    now,
                ),
            )
            conn.execute("COMMIT")
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        edge = self.get_edge(edge_id)
        assert edge is not None
        return edge

    def delete_edge(self, project_id: str, edge_id: str) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT project_id FROM edges WHERE id = ?", (edge_id,)).fetchone()
            if row is None or row["project_id"] != project_id:
                raise NotFound("Edge not found", code="edge_not_found")
            conn.execute("DELETE FROM edges WHERE id = ?", (edge_id,))
            conn.execute("COMMIT")
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
    # -- explanations / narrations ---------------------------------------
    def list_explanations(self, project_id: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT * FROM explanations WHERE project_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 500",
                (project_id,),
            )
            return self._rows(cur)
        finally:
            conn.close()

    def add_explanation(
        self,
        *,
        project_id: str,
        node_id: str,
        question: str | None,
        answer: str,
        provider: str,
        model: str,
        entity_id: str | None = None,
        learning: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        record = {
            "id": entity_id or new_id(),
            "project_id": project_id,
            "node_id": node_id,
            "question": question,
            "answer": answer,
            "provider": provider,
            "model": model,
            "created_at": utcnow(),
        }
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO explanations (id, project_id, node_id, question, answer, provider, model, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record["id"],
                    project_id,
                    node_id,
                    question,
                    answer,
                    provider,
                    model,
                    record["created_at"],
                ),
            )
            if learning:
                conn.execute('UPDATE explanations SET learning_json=? WHERE id=?',
                             (json.dumps(dict(learning), allow_nan=False), record['id']))
                record['learning_json'] = json.dumps(dict(learning))
            conn.execute("COMMIT")
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return record

    def list_narrations(self, project_id: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT * FROM narrations WHERE project_id = ? ORDER BY created_at DESC LIMIT 200", (project_id,)
            )
            return self._rows(cur)
        finally:
            conn.close()

    def add_narration(
        self,
        *,
        project_id: str,
        title: str,
        segments: Sequence[Mapping[str, Any]],
        provider: str,
        model: str,
        entity_id: str | None = None,
    ) -> dict[str, Any]:
        """`segment_count`/`total_duration_seconds` are derived server-side."""
        total = sum(float(seg.get("duration_seconds") or 0) for seg in segments)
        record = {
            "id": entity_id or new_id(),
            "project_id": project_id,
            "title": title,
            "segment_count": len(segments),
            "total_duration_seconds": round(total, 3),
            "provider": provider,
            "model": model,
            "segments": [dict(seg) for seg in segments],
            "created_at": utcnow(),
        }
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO narrations (id, project_id, title, segment_count, total_duration_seconds,"
                " provider, model, segments_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record["id"],
                    project_id,
                    title,
                    record["segment_count"],
                    record["total_duration_seconds"],
                    provider,
                    model,
                    json.dumps(record["segments"], ensure_ascii=False),
                    record["created_at"],
                ),
            )
            conn.execute("COMMIT")
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return record

    # -- runs ------------------------------------------------------------
    def create_run(
        self,
        *,
        kind: str,
        workload: str,
        project_id: str | None,
        input_payload: Mapping[str, Any],
        idempotency_key: str | None,
        idempotency_scope: str | None,
        session_id: str | None,
        target_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Insert a queued run. Returns `(run, created)`, idempotent per scope+key."""
        now = utcnow()
        run_id = new_id()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if idempotency_key and idempotency_scope:
                existing = conn.execute(
                    "SELECT * FROM runs WHERE idempotency_scope = ? AND idempotency_key = ?",
                    (idempotency_scope, idempotency_key),
                ).fetchone()
                if existing is not None:
                    conn.execute("COMMIT")
                    return dict(existing), False
            conn.execute(
                "INSERT INTO runs (id, project_id, kind, workload, state, progress, input_json, result_json,"
                " error_code, error_message, idempotency_key, idempotency_scope, session_id, target_id,"
                " created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 'queued', NULL, ?, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    project_id,
                    kind,
                    workload,
                    json.dumps(dict(input_payload), ensure_ascii=False),
                    idempotency_key,
                    idempotency_scope,
                    session_id,
                    target_id,
                    now,
                    now,
                ),
            )
            conn.execute("COMMIT")
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        run = self.get_run(run_id)
        assert run is not None
        return run, True

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_runs(self, *, session_id: str | None = None, project_id: str | None = None) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            clauses: list[str] = []
            params: list[Any] = []
            if session_id:
                clauses.append("session_id = ?")
                params.append(session_id)
            if project_id:
                clauses.append("project_id = ?")
                params.append(project_id)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            cur = conn.execute(f"SELECT * FROM runs {where} ORDER BY created_at DESC LIMIT 200", tuple(params))
            return self._rows(cur)
        finally:
            conn.close()

    def count_runs(self, session_id: str) -> int:
        conn = self._connect()
        try:
            row = conn.execute("SELECT COUNT(*) AS n FROM runs WHERE session_id = ?", (session_id,)).fetchone()
            return int(row["n"])
        finally:
            conn.close()

    def mark_run_state(self, run_id: str, state: str, **fields: Any) -> dict[str, Any] | None:
        if state not in RUN_STATES:
            raise ValueError(f"unknown run state: {state}")
        assignments = ["state = ?", "updated_at = ?"]
        params: list[Any] = [state, utcnow()]
        for key in ("progress", "result_json", "error_code", "error_message"):
            if key in fields:
                assignments.append(f"{key} = ?")
                params.append(fields[key])
        params.append(run_id)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                f"UPDATE runs SET {', '.join(assignments)} WHERE id = ? AND state IN ('queued','running')",
                tuple(params),
            )
            conn.execute("COMMIT")
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return self.get_run(run_id)

    def interrupt_active_runs(self) -> int:
        """Container restart/hard stop: active work becomes `interrupted`.

        Paid provider calls are never replayed, so this only closes bookkeeping.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE runs SET state = 'interrupted',"
                " error_code = COALESCE(error_code, 'interrupted'),"
                " error_message = COALESCE(error_message, 'Instance restarted before the run finished'),"
                " updated_at = ? WHERE state IN ('queued', 'running')",
                (utcnow(),),
            )
            changed = cur.rowcount or 0
            conn.execute("COMMIT")
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return changed
    # -- extractions / artifacts / builds --------------------------------
    def upsert_extraction(
        self,
        *,
        document_id: str,
        revision_id: str,
        extractor_version: str,
        status: str,
        text_hash: str | None,
        page_count: int | None,
        error_code: str | None,
        artifact_id: str | None,
    ) -> dict[str, Any]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT id FROM document_extractions WHERE revision_id = ? AND extractor_version = ?",
                (revision_id, extractor_version),
            ).fetchone()
            if row is None:
                extraction_id = new_id()
                conn.execute(
                    "INSERT INTO document_extractions (id, document_id, revision_id, extractor_version, status,"
                    " text_hash, page_count, error_code, artifact_id, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        extraction_id,
                        document_id,
                        revision_id,
                        extractor_version,
                        status,
                        text_hash,
                        page_count,
                        error_code,
                        artifact_id,
                        utcnow(),
                    ),
                )
            else:
                extraction_id = str(row["id"])
                conn.execute(
                    "UPDATE document_extractions SET status = ?, text_hash = ?, page_count = ?,"
                    " error_code = ?, artifact_id = ? WHERE id = ?",
                    (status, text_hash, page_count, error_code, artifact_id, extraction_id),
                )
            conn.execute("COMMIT")
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return self.get_extraction(extraction_id) or {}

    def get_extraction(self, extraction_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM document_extractions WHERE id = ?", (extraction_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_extractions(
        self, *, document_id: str, revision_id: str | None = None, extractor_version: str | None = None
    ) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            clauses = ["document_id = ?"]
            params: list[Any] = [document_id]
            if revision_id:
                clauses.append("revision_id = ?")
                params.append(revision_id)
            if extractor_version:
                clauses.append("extractor_version = ?")
                params.append(extractor_version)
            cur = conn.execute(
                f"SELECT * FROM document_extractions WHERE {' AND '.join(clauses)}"
                " ORDER BY created_at DESC LIMIT 50",
                tuple(params),
            )
            return self._rows(cur)
        finally:
            conn.close()

    def add_artifact(
        self,
        *,
        document_id: str,
        revision_id: str,
        kind: str,
        rel_path: str,
        payload: bytes,
        build_id: str | None = None,
    ) -> dict[str, Any]:
        record = {
            "id": new_id(),
            "document_id": document_id,
            "revision_id": revision_id,
            "build_id": build_id,
            "kind": kind,
            "rel_path": rel_path,
            "content_hash": content_hash(payload),
            "size_bytes": len(payload),
            "created_at": utcnow(),
        }
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO document_artifacts (id, document_id, revision_id, build_id, kind, rel_path,"
                " content_hash, size_bytes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record["id"],
                    document_id,
                    revision_id,
                    build_id,
                    kind,
                    rel_path,
                    record["content_hash"],
                    record["size_bytes"],
                    record["created_at"],
                ),
            )
            conn.execute("COMMIT")
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return record

    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM document_artifacts WHERE id = ?", (artifact_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_artifacts(self, *, document_id: str, revision_id: str | None = None) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            if revision_id:
                cur = conn.execute(
                    "SELECT * FROM document_artifacts WHERE document_id = ? AND revision_id = ?"
                    " ORDER BY created_at DESC LIMIT 100",
                    (document_id, revision_id),
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM document_artifacts WHERE document_id = ? ORDER BY created_at DESC LIMIT 100",
                    (document_id,),
                )
            return self._rows(cur)
        finally:
            conn.close()

    def add_build(
        self,
        *,
        document_id: str,
        revision_id: str,
        status: str,
        engine_version: str | None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        now = utcnow()
        build_id = new_id()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO document_builds (id, document_id, revision_id, status, artifact_id, engine_version,"
                " source_map_json, error_code, error_message, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, NULL, ?, NULL, ?, ?, ?, ?)",
                (build_id, document_id, revision_id, status, engine_version, error_code, error_message, now, now),
            )
            conn.execute("COMMIT")
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        build = self.get_build(build_id)
        assert build is not None
        return build

    def get_build(self, build_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM document_builds WHERE id = ?", (build_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_builds(self, *, document_id: str, revision_id: str | None = None) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            if revision_id:
                cur = conn.execute(
                    "SELECT * FROM document_builds WHERE document_id = ? AND revision_id = ?"
                    " ORDER BY created_at DESC LIMIT 100",
                    (document_id, revision_id),
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM document_builds WHERE document_id = ? ORDER BY created_at DESC LIMIT 100",
                    (document_id,),
                )
            return self._rows(cur)
        finally:
            conn.close()

class LocalStorage:
    """Immutable source files and derived artifacts inside the workspace volume."""

    def __init__(self, data_dir: Path) -> None:
        self.root = Path(data_dir)
        self.sources_root = self.root / "sources"
        self.artifacts_root = self.root / "artifacts"

    def ensure_roots(self) -> None:
        self.sources_root.mkdir(parents=True, exist_ok=True)
        self.artifacts_root.mkdir(parents=True, exist_ok=True)

    # -- sources ---------------------------------------------------------
    def revision_dir(self, document_id: str, revision_id: str, *, create: bool = False) -> Path:
        path = safe_join(self.sources_root, document_id, revision_id)
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def write_revision_file(
        self,
        document_id: str,
        revision_id: str,
        *,
        relative_path: str,
        payload: bytes,
    ) -> str:
        """Write one immutable source file; returns its canonical relative path."""
        rel = safe_relative_path(relative_path)
        revision_dir = self.revision_dir(document_id, revision_id, create=True)
        target = safe_join(revision_dir, *rel.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise Conflict("Source file already exists; revisions are immutable", code="revision_immutable")
        target.write_bytes(payload)
        return rel

    def source_path(self, document_id: str, revision: Mapping[str, Any]) -> Path:
        revision_dir = self.revision_dir(document_id, str(revision["id"]))
        rel = str(revision.get("main_rel_path") or "main.bin")
        target = safe_join(revision_dir, *rel.split("/"))
        if target.is_symlink():
            raise NotFound("Source path is a symlink; refusing to read", code="source_symlink")
        return target

    def read_source(self, document_id: str, revision: Mapping[str, Any]) -> bytes:
        target = self.source_path(document_id, revision)
        if not target.is_file():
            raise NotFound("Source file is missing", code="source_missing")
        return target.read_bytes()

    def source_stat(self, document_id: str, revision: Mapping[str, Any]) -> os.stat_result:
        target = self.source_path(document_id, revision)
        if not target.is_file():
            raise NotFound("Source file is missing", code="source_missing")
        return target.stat()

    def delete_document_files(self, document_id: str) -> None:
        """Remove only this document's immutable sources and derived artifacts."""
        for base in (self.sources_root, self.artifacts_root):
            target = safe_join(base, document_id)
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)

    # -- artifacts -------------------------------------------------------
    def write_artifact(self, document_id: str, revision_id: str, name: str, payload: bytes) -> tuple[str, Path]:
        safe_name = sanitize_filename(name, "artifact.bin")
        target_dir = safe_join(self.artifacts_root, document_id, revision_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = safe_join(target_dir, safe_name)
        target.write_bytes(payload)
        rel_path = "/".join(["artifacts", document_id, revision_id, safe_name])
        return rel_path, target

    def artifact_path(self, artifact: Mapping[str, Any]) -> Path:
        rel = safe_relative_path(str(artifact.get("rel_path") or ""))
        parts = rel.split("/")
        if parts[0] != "artifacts":
            raise PathEscape("artifact paths must live under artifacts/")
        target = safe_join(self.root, *parts)
        if target.is_symlink():
            raise NotFound("Artifact path is a symlink; refusing to read", code="artifact_symlink")
        return target

    def read_artifact(self, artifact: Mapping[str, Any]) -> bytes:
        target = self.artifact_path(artifact)
        if not target.is_file():
            raise NotFound("Artifact file is missing", code="artifact_missing")
        return target.read_bytes()


__all__ = [
    "LocalStorage",
    "LocalStore",
    "PathEscape",
    "RUN_STATES",
    "TERMINAL_STATES",
    "content_hash",
    "new_id",
    "safe_join",
    "safe_relative_path",
    "sanitize_filename",
    "utcnow",
]
