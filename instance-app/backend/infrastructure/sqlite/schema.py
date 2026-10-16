"""业务库 DDL。

字段对齐 ``docs/instance-app-design.md`` §4.3。要点：

- ``projects.workspace_id`` 唯一（每 workspace 恰好一行）。
- ``nodes.parent_id`` 用 ``ON DELETE SET NULL``：删除父节点时子节点**提升为顶层**，
  **不**级联删子树。
- ``document_revisions``/``document_resources``/``document_artifacts`` 是**不可变**
  原件的元数据；新内容 = 新 revision。
- ``runs`` 不留任何 token；``run_events`` 有 ``(run_id, seq)`` 单调唯一约束。
- ``workspace_binding`` 是**单行**访问上下文（``id=1`` 约束）。
"""

from __future__ import annotations

SCHEMA_VERSION = 1

#: ``workspace_binding`` 单行的固定主键：用 ``CHECK`` 保证不可能出现第二行。
BINDING_ROW_ID = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id            TEXT PRIMARY KEY,
    workspace_id  TEXT NOT NULL UNIQUE,
    title         TEXT NOT NULL,
    description   TEXT,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title               TEXT NOT NULL,
    source_filename     TEXT,
    format              TEXT NOT NULL,
    status              TEXT NOT NULL,
    current_revision_id TEXT,
    size_bytes          INTEGER NOT NULL DEFAULT 0,
    order_index         INTEGER NOT NULL DEFAULT 0,
    created_at          REAL NOT NULL,
    updated_at          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_project ON documents(project_id, order_index);

CREATE TABLE IF NOT EXISTS document_revisions (
    id              TEXT PRIMARY KEY,
    document_id     TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    revision_no     INTEGER NOT NULL,
    content_hash    TEXT NOT NULL,
    size_bytes      INTEGER NOT NULL,
    status          TEXT NOT NULL,
    source_filename TEXT,
    media_type      TEXT,
    created_at      REAL NOT NULL,
    UNIQUE(document_id, revision_no)
);
CREATE INDEX IF NOT EXISTS idx_revisions_document ON document_revisions(document_id, revision_no);
CREATE INDEX IF NOT EXISTS idx_revisions_hash ON document_revisions(document_id, content_hash);

CREATE TABLE IF NOT EXISTS document_resources (
    id           TEXT PRIMARY KEY,
    revision_id  TEXT NOT NULL REFERENCES document_revisions(id) ON DELETE CASCADE,
    logical_path TEXT NOT NULL,
    kind         TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    size_bytes   INTEGER NOT NULL,
    UNIQUE(revision_id, logical_path)
);

CREATE TABLE IF NOT EXISTS document_extractions (
    id                TEXT PRIMARY KEY,
    revision_id       TEXT NOT NULL REFERENCES document_revisions(id) ON DELETE CASCADE,
    extractor_version TEXT NOT NULL,
    status            TEXT NOT NULL,
    text_hash         TEXT,
    page_count        INTEGER,
    error_code        TEXT,
    artifact_id       TEXT,
    created_at        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_extractions_revision ON document_extractions(revision_id, created_at);

CREATE TABLE IF NOT EXISTS document_artifacts (
    id           TEXT PRIMARY KEY,
    revision_id  TEXT NOT NULL REFERENCES document_revisions(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,
    path         TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    size_bytes   INTEGER NOT NULL,
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_revision ON document_artifacts(revision_id, kind, created_at);

CREATE TABLE IF NOT EXISTS document_builds (
    id             TEXT PRIMARY KEY,
    revision_id    TEXT NOT NULL REFERENCES document_revisions(id) ON DELETE CASCADE,
    status         TEXT NOT NULL,
    source_map     TEXT,
    artifact_id    TEXT,
    engine_version TEXT,
    error_code     TEXT,
    error_message  TEXT,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_builds_revision ON document_builds(revision_id, created_at);

CREATE TABLE IF NOT EXISTS nodes (
    id           TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    document_id  TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    title        TEXT NOT NULL,
    kind         TEXT NOT NULL,
    summary      TEXT,
    content      TEXT,
    order_index  INTEGER NOT NULL DEFAULT 0,
    parent_id    TEXT REFERENCES nodes(id) ON DELETE SET NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    revision     INTEGER NOT NULL DEFAULT 1,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nodes_project ON nodes(project_id, order_index);
CREATE INDEX IF NOT EXISTS idx_nodes_document ON nodes(document_id);
CREATE INDEX IF NOT EXISTS idx_nodes_parent ON nodes(parent_id);

CREATE TABLE IF NOT EXISTS edges (
    id             TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    source_node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    target_node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    relation       TEXT NOT NULL,
    metadata_json  TEXT NOT NULL DEFAULT '{}',
    created_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_edges_project ON edges(project_id);
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_node_id);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_node_id);

CREATE TABLE IF NOT EXISTS explanations (
    id         TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    node_id    TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    question   TEXT,
    answer     TEXT NOT NULL,
    provider   TEXT NOT NULL,
    model      TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_explanations_project ON explanations(project_id, created_at);
CREATE INDEX IF NOT EXISTS idx_explanations_node ON explanations(node_id, created_at);

CREATE TABLE IF NOT EXISTS narrations (
    id                     TEXT PRIMARY KEY,
    project_id             TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title                  TEXT NOT NULL,
    provider               TEXT NOT NULL,
    model                  TEXT NOT NULL,
    created_at             REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_narrations_project ON narrations(project_id, created_at);

CREATE TABLE IF NOT EXISTS narration_segments (
    id               TEXT PRIMARY KEY,
    narration_id     TEXT NOT NULL REFERENCES narrations(id) ON DELETE CASCADE,
    order_index      INTEGER NOT NULL,
    node_id          TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    title            TEXT NOT NULL,
    text             TEXT NOT NULL,
    duration_seconds REAL NOT NULL,
    UNIQUE(narration_id, order_index)
);

CREATE TABLE IF NOT EXISTS runs (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,
    workload        TEXT NOT NULL,
    state           TEXT NOT NULL,
    input_json      TEXT NOT NULL DEFAULT '{}',
    result_json     TEXT,
    error_code      TEXT,
    error_message   TEXT,
    idempotency_key TEXT,
    idempotency_scope TEXT,
    progress        REAL,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_project ON runs(project_id, created_at);
CREATE INDEX IF NOT EXISTS idx_runs_state ON runs(state);
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_idempotency
    ON runs(idempotency_scope, idempotency_key) WHERE idempotency_key IS NOT NULL AND idempotency_scope IS NOT NULL;

CREATE TABLE IF NOT EXISTS run_events (
    run_id       TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL,
    type         TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at   REAL NOT NULL,
    PRIMARY KEY (run_id, seq)
);

CREATE TABLE IF NOT EXISTS workspace_binding (
    id                   INTEGER PRIMARY KEY CHECK (id = 1),
    current_workspace_id TEXT NOT NULL,
    origin_workspace_id  TEXT NOT NULL,
    owner_id             TEXT NOT NULL,
    copy_id              TEXT,
    storage_layout       TEXT NOT NULL DEFAULT 'legacy_root',
    updated_at           REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS workspace_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    type       TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
"""
