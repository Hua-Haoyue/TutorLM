"""repository 实现：只使用 UoW 注入的连接，**自己不开事务**（设计 §4.3）。

约定：

- 时间戳在库里是 REAL（Unix 秒），领域模型是 ``float``；
- ``metadata_json``/``input_json``/``result_json``/``source_map`` 是 JSON 文本，
  解析失败视为数据损坏（抛领域错误），不静默当空对象；
- ``nodes.parent_id`` 用 ``ON DELETE SET NULL``：``delete`` 返回被提升为顶层的子节点 id；
- ``runs.compare_and_set_state`` 用 ``WHERE state = expected`` 条件更新实现乐观并发，
  返回 ``False`` 表示状态已被他人改变（取消/超时），调用方**必须丢弃**结果。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping

from backend.domain.enums import (
    ArtifactKind,
    BuildStatus,
    DocumentFormat,
    DocumentStatus,
    EdgeRelation,
    ExtractionStatus,
    NodeKind,
    RevisionStatus,
    RunKind,
    RunState,
    RunWorkload,
    StorageLayout,
)
from backend.domain.errors import DomainError
from backend.domain.models import (
    Document,
    DocumentArtifact,
    DocumentBuild,
    DocumentExtraction,
    DocumentResource,
    DocumentRevision,
    Edge,
    Explanation,
    Narration,
    NarrationSegment,
    Node,
    Project,
    Run,
    RunEvent,
)
from backend.domain.workspace_binding import WorkspaceBinding


def _json_load(raw: Any, *, what: str) -> dict[str, Any]:
    if raw is None or raw == "":
        return {}
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    try:
        decoded = json.loads(str(raw))
    except json.JSONDecodeError:
        raise DomainError(
            f"{what} 不是合法 JSON：数据已损坏",
            details={"raw_prefix": str(raw)[:64]},
        ) from None
    if not isinstance(decoded, dict):
        raise DomainError(f"{what} 必须是 JSON 对象")
    return decoded


def _json_load_optional(raw: Any, *, what: str) -> dict[str, Any] | None:
    if raw is None or raw == "":
        return None
    return _json_load(raw, what=what)


def _json_dump(payload: Mapping[str, Any] | None) -> str:
    return json.dumps(dict(payload or {}), ensure_ascii=False, sort_keys=True)


def _rows(cursor: sqlite3.Cursor) -> list[sqlite3.Row]:
    return list(cursor.fetchall())


# ====================================================================== 映射
def _project(row: sqlite3.Row) -> Project:
    return Project(
        id=row["id"],
        workspace_id=row["workspace_id"],
        title=row["title"],
        description=row["description"],
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


def _document(row: sqlite3.Row) -> Document:
    return Document(
        id=row["id"],
        project_id=row["project_id"],
        title=row["title"],
        source_filename=row["source_filename"],
        format=DocumentFormat(row["format"]),
        status=DocumentStatus(row["status"]),
        current_revision_id=row["current_revision_id"],
        size_bytes=int(row["size_bytes"]),
        order_index=int(row["order_index"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


def _revision(row: sqlite3.Row) -> DocumentRevision:
    keys = set(row.keys())
    return DocumentRevision(
        id=row["id"],
        document_id=row["document_id"],
        revision_no=int(row["revision_no"]),
        content_hash=row["content_hash"],
        size_bytes=int(row["size_bytes"]),
        status=RevisionStatus(row["status"]),
        created_at=float(row["created_at"]),
        source_filename=row["source_filename"] if "source_filename" in keys else None,
        media_type=row["media_type"] if "media_type" in keys else None,
    )


def _resource(row: sqlite3.Row) -> DocumentResource:
    return DocumentResource(
        id=row["id"],
        revision_id=row["revision_id"],
        logical_path=row["logical_path"],
        kind=row["kind"],
        content_hash=row["content_hash"],
        size_bytes=int(row["size_bytes"]),
    )


def _extraction(row: sqlite3.Row) -> DocumentExtraction:
    return DocumentExtraction(
        id=row["id"],
        revision_id=row["revision_id"],
        extractor_version=row["extractor_version"],
        status=ExtractionStatus(row["status"]),
        text_hash=row["text_hash"],
        page_count=None if row["page_count"] is None else int(row["page_count"]),
        error_code=row["error_code"],
        artifact_id=row["artifact_id"],
        created_at=float(row["created_at"]),
    )


def _artifact(row: sqlite3.Row) -> DocumentArtifact:
    return DocumentArtifact(
        id=row["id"],
        revision_id=row["revision_id"],
        kind=ArtifactKind(row["kind"]),
        path=row["path"],
        content_hash=row["content_hash"],
        size_bytes=int(row["size_bytes"]),
        created_at=float(row["created_at"]),
    )


def _build(row: sqlite3.Row) -> DocumentBuild:
    return DocumentBuild(
        id=row["id"],
        revision_id=row["revision_id"],
        status=BuildStatus(row["status"]),
        artifact_id=row["artifact_id"],
        engine_version=row["engine_version"],
        source_map=_json_load_optional(row["source_map"], what="document_builds.source_map"),
        error_code=row["error_code"],
        error_message=row["error_message"],
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


def _node(row: sqlite3.Row) -> Node:
    return Node(
        id=row["id"],
        project_id=row["project_id"],
        document_id=row["document_id"],
        title=row["title"],
        kind=NodeKind(row["kind"]),
        summary=row["summary"],
        content=row["content"],
        order_index=int(row["order_index"]),
        parent_id=row["parent_id"],
        metadata=_json_load(row["metadata_json"], what="nodes.metadata_json"),
        revision=int(row["revision"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


def _edge(row: sqlite3.Row) -> Edge:
    return Edge(
        id=row["id"],
        project_id=row["project_id"],
        source_node_id=row["source_node_id"],
        target_node_id=row["target_node_id"],
        relation=EdgeRelation(row["relation"]),
        metadata=_json_load(row["metadata_json"], what="edges.metadata_json"),
        created_at=float(row["created_at"]),
    )


def _explanation(row: sqlite3.Row) -> Explanation:
    return Explanation(
        id=row["id"],
        project_id=row["project_id"],
        node_id=row["node_id"],
        question=row["question"],
        answer=row["answer"],
        provider=row["provider"],
        model=row["model"],
        created_at=float(row["created_at"]),
    )


def _narration_segment(row: sqlite3.Row) -> NarrationSegment:
    return NarrationSegment(
        id=row["id"],
        narration_id=row["narration_id"],
        order_index=int(row["order_index"]),
        node_id=row["node_id"],
        title=row["title"],
        text=row["text"],
        duration_seconds=float(row["duration_seconds"]),
    )


def _run(row: sqlite3.Row) -> Run:
    return Run(
        id=row["id"],
        project_id=row["project_id"],
        kind=RunKind(row["kind"]),
        state=RunState(row["state"]),
        workload=RunWorkload(row["workload"]),
        input=_json_load(row["input_json"], what="runs.input_json"),
        result=_json_load_optional(row["result_json"], what="runs.result_json"),
        error_code=row["error_code"],
        error_message=row["error_message"],
        idempotency_key=row["idempotency_key"],
        idempotency_scope=row["idempotency_scope"],
        progress=None if row["progress"] is None else float(row["progress"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


def _run_event(row: sqlite3.Row) -> RunEvent:
    return RunEvent(
        run_id=row["run_id"],
        seq=int(row["seq"]),
        type=row["type"],
        payload=_json_load(row["payload_json"], what="run_events.payload_json"),
        created_at=float(row["created_at"]),
    )


def _binding(row: sqlite3.Row) -> WorkspaceBinding:
    return WorkspaceBinding(
        current_workspace_id=row["current_workspace_id"],
        origin_workspace_id=row["origin_workspace_id"],
        owner_id=row["owner_id"],
        copy_id=row["copy_id"],
        storage_layout=StorageLayout(row["storage_layout"]),
        updated_at=float(row["updated_at"]),
    )


# ================================================================== 单表实现
class SqliteProjectRepository:
    __slots__ = ("_conn",)

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get_for_workspace(self, workspace_id: str) -> Project | None:
        row = self._conn.execute(
            "SELECT * FROM projects WHERE workspace_id = ?", (workspace_id,)
        ).fetchone()
        return None if row is None else _project(row)

    def get(self, project_id: str) -> Project | None:
        row = self._conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return None if row is None else _project(row)

    def save(self, project: Project) -> None:
        """upsert：``workspace_id`` 唯一，重复初始化幂等（保留原 id）。"""

        self._conn.execute(
            "INSERT INTO projects(id, workspace_id, title, description, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(workspace_id) DO UPDATE SET "
            "  title = excluded.title, "
            "  description = COALESCE(excluded.description, projects.description), "
            "  updated_at = excluded.updated_at",
            (
                project.id,
                project.workspace_id,
                project.title,
                project.description,
                project.created_at,
                project.updated_at,
            ),
        )

    def update(self, project: Project) -> None:
        self._conn.execute(
            "UPDATE projects SET title = ?, description = ?, updated_at = ? WHERE id = ?",
            (project.title, project.description, project.updated_at, project.id),
        )


class SqliteDocumentRepository:
    __slots__ = ("_conn",)

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def add(self, document: Document) -> None:
        self._conn.execute(
            "INSERT INTO documents(id, project_id, title, source_filename, format, status, "
            "current_revision_id, size_bytes, order_index, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                document.id,
                document.project_id,
                document.title,
                document.source_filename,
                str(document.format),
                str(document.status),
                document.current_revision_id,
                document.size_bytes,
                document.order_index,
                document.created_at,
                document.updated_at,
            ),
        )

    def get(self, document_id: str) -> Document | None:
        row = self._conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
        return None if row is None else _document(row)

    def list_for_project(self, project_id: str) -> list[Document]:
        rows = _rows(
            self._conn.execute(
                "SELECT * FROM documents WHERE project_id = ? ORDER BY order_index, created_at",
                (project_id,),
            )
        )
        return [_document(row) for row in rows]

    def update(self, document: Document) -> None:
        self._conn.execute(
            "UPDATE documents SET title = ?, source_filename = ?, status = ?, updated_at = ? WHERE id = ?",
            (
                document.title,
                document.source_filename,
                str(document.status),
                document.updated_at,
                document.id,
            ),
        )

    def set_current_revision(
        self, document_id: str, revision_id: str, *, size_bytes: int, updated_at: float
    ) -> None:
        self._conn.execute(
            "UPDATE documents SET current_revision_id = ?, size_bytes = ?, updated_at = ? WHERE id = ?",
            (revision_id, size_bytes, updated_at, document_id),
        )

    def next_order_index(self, project_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(order_index), -1) + 1 AS next FROM documents WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        return int(row["next"])

    def delete(self, document_id: str) -> None:
        self._conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))


class SqliteRevisionRepository:
    __slots__ = ("_conn",)

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def add(self, revision: DocumentRevision) -> None:
        self._conn.execute(
            "INSERT INTO document_revisions(id, document_id, revision_no, content_hash, size_bytes, "
            "status, source_filename, media_type, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                revision.id,
                revision.document_id,
                revision.revision_no,
                revision.content_hash,
                revision.size_bytes,
                str(revision.status),
                revision.source_filename,
                revision.media_type,
                revision.created_at,
            ),
        )

    def get(self, revision_id: str) -> DocumentRevision | None:
        row = self._conn.execute(
            "SELECT * FROM document_revisions WHERE id = ?", (revision_id,)
        ).fetchone()
        return None if row is None else _revision(row)

    def list_for_document(self, document_id: str) -> list[DocumentRevision]:
        rows = _rows(
            self._conn.execute(
                "SELECT * FROM document_revisions WHERE document_id = ? ORDER BY revision_no",
                (document_id,),
            )
        )
        return [_revision(row) for row in rows]

    def next_revision_no(self, document_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(revision_no), 0) + 1 AS next FROM document_revisions WHERE document_id = ?",
            (document_id,),
        ).fetchone()
        return int(row["next"])

    def by_content_hash(self, document_id: str, content_hash: str) -> DocumentRevision | None:
        row = self._conn.execute(
            "SELECT * FROM document_revisions WHERE document_id = ? AND content_hash = ? "
            "ORDER BY revision_no LIMIT 1",
            (document_id, content_hash),
        ).fetchone()
        return None if row is None else _revision(row)


class SqliteResourceRepository:
    __slots__ = ("_conn",)

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def add_many(self, resources: list[DocumentResource]) -> None:
        if not resources:
            return
        self._conn.executemany(
            "INSERT INTO document_resources(id, revision_id, logical_path, kind, content_hash, size_bytes) "
            "VALUES(?,?,?,?,?,?)",
            [
                (
                    item.id,
                    item.revision_id,
                    item.logical_path,
                    item.kind,
                    item.content_hash,
                    item.size_bytes,
                )
                for item in resources
            ],
        )

    def list_for_revision(self, revision_id: str) -> list[DocumentResource]:
        rows = _rows(
            self._conn.execute(
                "SELECT * FROM document_resources WHERE revision_id = ? ORDER BY logical_path",
                (revision_id,),
            )
        )
        return [_resource(row) for row in rows]

    def get_by_logical_path(self, revision_id: str, logical_path: str) -> DocumentResource | None:
        row = self._conn.execute(
            "SELECT * FROM document_resources WHERE revision_id = ? AND logical_path = ?",
            (revision_id, logical_path),
        ).fetchone()
        return None if row is None else _resource(row)


class SqliteExtractionRepository:
    __slots__ = ("_conn",)

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def add(self, extraction: DocumentExtraction) -> None:
        self._conn.execute(
            "INSERT INTO document_extractions(id, revision_id, extractor_version, status, text_hash, "
            "page_count, error_code, artifact_id, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                extraction.id,
                extraction.revision_id,
                extraction.extractor_version,
                str(extraction.status),
                extraction.text_hash,
                extraction.page_count,
                extraction.error_code,
                extraction.artifact_id,
                extraction.created_at,
            ),
        )

    def update(self, extraction: DocumentExtraction) -> None:
        self._conn.execute(
            "UPDATE document_extractions SET status = ?, text_hash = ?, page_count = ?, "
            "error_code = ?, artifact_id = ? WHERE id = ?",
            (
                str(extraction.status),
                extraction.text_hash,
                extraction.page_count,
                extraction.error_code,
                extraction.artifact_id,
                extraction.id,
            ),
        )

    def get(self, extraction_id: str) -> DocumentExtraction | None:
        row = self._conn.execute(
            "SELECT * FROM document_extractions WHERE id = ?", (extraction_id,)
        ).fetchone()
        return None if row is None else _extraction(row)

    def list_for_revision(self, revision_id: str) -> list[DocumentExtraction]:
        rows = _rows(
            self._conn.execute(
                "SELECT * FROM document_extractions WHERE revision_id = ? ORDER BY created_at",
                (revision_id,),
            )
        )
        return [_extraction(row) for row in rows]

    def latest(
        self, revision_id: str, *, extractor_version: str | None = None
    ) -> DocumentExtraction | None:
        if extractor_version is None:
            row = self._conn.execute(
                "SELECT * FROM document_extractions WHERE revision_id = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (revision_id,),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT * FROM document_extractions WHERE revision_id = ? AND extractor_version = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (revision_id, extractor_version),
            ).fetchone()
        return None if row is None else _extraction(row)


class SqliteNodeRepository:
    __slots__ = ("_conn",)

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def add(self, node: Node) -> None:
        self._conn.execute(
            "INSERT INTO nodes(id, project_id, document_id, title, kind, summary, content, order_index, "
            "parent_id, metadata_json, revision, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                node.id,
                node.project_id,
                node.document_id,
                node.title,
                str(node.kind),
                node.summary,
                node.content,
                node.order_index,
                node.parent_id,
                _json_dump(node.metadata),
                node.revision,
                node.created_at,
                node.updated_at,
            ),
        )

    def get(self, node_id: str) -> Node | None:
        row = self._conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        return None if row is None else _node(row)

    def list_for_project(self, project_id: str) -> list[Node]:
        rows = _rows(
            self._conn.execute(
                "SELECT * FROM nodes WHERE project_id = ? ORDER BY order_index, created_at",
                (project_id,),
            )
        )
        return [_node(row) for row in rows]

    def list_for_document(self, document_id: str) -> list[Node]:
        rows = _rows(
            self._conn.execute(
                "SELECT * FROM nodes WHERE document_id = ? ORDER BY order_index, created_at",
                (document_id,),
            )
        )
        return [_node(row) for row in rows]

    def update(self, node: Node) -> None:
        self._conn.execute(
            "UPDATE nodes SET title = ?, kind = ?, summary = ?, content = ?, order_index = ?, "
            "parent_id = ?, metadata_json = ?, revision = ?, updated_at = ? WHERE id = ?",
            (
                node.title,
                str(node.kind),
                node.summary,
                node.content,
                node.order_index,
                node.parent_id,
                _json_dump(node.metadata),
                node.revision,
                node.updated_at,
                node.id,
            ),
        )

    def delete(self, node_id: str) -> list[str]:
        """删除节点并把子节点提升为顶层（``ON DELETE SET NULL``），不级联删子树。"""

        child_rows = _rows(
            self._conn.execute("SELECT id FROM nodes WHERE parent_id = ?", (node_id,))
        )
        promoted = [str(row["id"]) for row in child_rows]
        self._conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
        return promoted

    def children_of(self, node_id: str) -> list[Node]:
        rows = _rows(
            self._conn.execute(
                "SELECT * FROM nodes WHERE parent_id = ? ORDER BY order_index, created_at",
                (node_id,),
            )
        )
        return [_node(row) for row in rows]

    def next_order_index(self, project_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(order_index), -1) + 1 AS next FROM nodes WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        return int(row["next"])


class SqliteEdgeRepository:
    __slots__ = ("_conn",)

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def add(self, edge: Edge) -> None:
        self._conn.execute(
            "INSERT INTO edges(id, project_id, source_node_id, target_node_id, relation, "
            "metadata_json, created_at) VALUES(?,?,?,?,?,?,?)",
            (
                edge.id,
                edge.project_id,
                edge.source_node_id,
                edge.target_node_id,
                str(edge.relation),
                _json_dump(edge.metadata),
                edge.created_at,
            ),
        )

    def get(self, edge_id: str) -> Edge | None:
        row = self._conn.execute("SELECT * FROM edges WHERE id = ?", (edge_id,)).fetchone()
        return None if row is None else _edge(row)

    def list_for_project(self, project_id: str) -> list[Edge]:
        rows = _rows(
            self._conn.execute(
                "SELECT * FROM edges WHERE project_id = ? ORDER BY created_at", (project_id,)
            )
        )
        return [_edge(row) for row in rows]

    def delete(self, edge_id: str) -> None:
        self._conn.execute("DELETE FROM edges WHERE id = ?", (edge_id,))

    def delete_for_node(self, node_id: str) -> None:
        self._conn.execute(
            "DELETE FROM edges WHERE source_node_id = ? OR target_node_id = ?",
            (node_id, node_id),
        )


class SqliteExplanationRepository:
    __slots__ = ("_conn",)

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def add(self, explanation: Explanation) -> None:
        self._conn.execute(
            "INSERT INTO explanations(id, project_id, node_id, question, answer, provider, model, "
            "created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                explanation.id,
                explanation.project_id,
                explanation.node_id,
                explanation.question,
                explanation.answer,
                explanation.provider,
                explanation.model,
                explanation.created_at,
            ),
        )

    def get(self, explanation_id: str) -> Explanation | None:
        row = self._conn.execute(
            "SELECT * FROM explanations WHERE id = ?", (explanation_id,)
        ).fetchone()
        return None if row is None else _explanation(row)

    def list_for_project(self, project_id: str) -> list[Explanation]:
        rows = _rows(
            self._conn.execute(
                "SELECT * FROM explanations WHERE project_id = ? ORDER BY created_at",
                (project_id,),
            )
        )
        return [_explanation(row) for row in rows]

    def list_for_node(self, node_id: str) -> list[Explanation]:
        rows = _rows(
            self._conn.execute(
                "SELECT * FROM explanations WHERE node_id = ? ORDER BY created_at", (node_id,)
            )
        )
        return [_explanation(row) for row in rows]


class SqliteNarrationRepository:
    __slots__ = ("_conn",)

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def add(self, narration: Narration, segments: list[NarrationSegment]) -> None:
        self._conn.execute(
            "INSERT INTO narrations(id, project_id, title, provider, model, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                narration.id,
                narration.project_id,
                narration.title,
                narration.provider,
                narration.model,
                narration.created_at,
            ),
        )
        ordered = sorted(segments, key=lambda item: item.order_index)
        if ordered:
            self._conn.executemany(
                "INSERT INTO narration_segments(id, narration_id, order_index, node_id, title, text, "
                "duration_seconds) VALUES(?,?,?,?,?,?,?)",
                [
                    (
                        item.id,
                        narration.id,
                        item.order_index,
                        item.node_id,
                        item.title,
                        item.text,
                        item.duration_seconds,
                    )
                    for item in ordered
                ],
            )

    def _narration_row(self, row: sqlite3.Row) -> Narration:
        narration = Narration(
            id=row["id"],
            project_id=row["project_id"],
            title=row["title"],
            provider=row["provider"],
            model=row["model"],
            created_at=float(row["created_at"]),
        )
        narration.segments = self.list_segments(narration.id)
        return narration

    def get(self, narration_id: str) -> Narration | None:
        row = self._conn.execute(
            "SELECT * FROM narrations WHERE id = ?", (narration_id,)
        ).fetchone()
        return None if row is None else self._narration_row(row)

    def list_for_project(self, project_id: str) -> list[Narration]:
        rows = _rows(
            self._conn.execute(
                "SELECT * FROM narrations WHERE project_id = ? ORDER BY created_at", (project_id,)
            )
        )
        return [self._narration_row(row) for row in rows]

    def list_segments(self, narration_id: str) -> list[NarrationSegment]:
        rows = _rows(
            self._conn.execute(
                "SELECT * FROM narration_segments WHERE narration_id = ? ORDER BY order_index",
                (narration_id,),
            )
        )
        return [_narration_segment(row) for row in rows]


class SqliteRunRepository:
    __slots__ = ("_conn",)

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def add(self, run: Run) -> None:
        self._conn.execute(
            "INSERT INTO runs(id, project_id, kind, workload, state, input_json, result_json, "
            "error_code, error_message, idempotency_key, idempotency_scope, progress, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run.id,
                run.project_id,
                str(run.kind),
                str(run.workload),
                str(run.state),
                _json_dump(run.input),
                None if run.result is None else _json_dump(run.result),
                run.error_code,
                run.error_message,
                run.idempotency_key,
                run.idempotency_scope,
                run.progress,
                run.created_at,
                run.updated_at,
            ),
        )

    def get(self, run_id: str) -> Run | None:
        row = self._conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return None if row is None else _run(row)

    def list_for_project(self, project_id: str, *, limit: int = 100) -> list[Run]:
        rows = _rows(
            self._conn.execute(
                "SELECT * FROM runs WHERE project_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (project_id, int(limit)),
            )
        )
        return [_run(row) for row in rows]

    def update(self, run: Run) -> None:
        self._conn.execute(
            "UPDATE runs SET state = ?, result_json = ?, error_code = ?, error_message = ?, "
            "progress = ?, updated_at = ? WHERE id = ?",
            (
                str(run.state),
                None if run.result is None else _json_dump(run.result),
                run.error_code,
                run.error_message,
                run.progress,
                run.updated_at,
                run.id,
            ),
        )

    def find_by_idempotency_key(self, scope: str, idempotency_key: str) -> Run | None:
        if not idempotency_key:
            return None
        row = self._conn.execute(
            "SELECT * FROM runs WHERE idempotency_scope = ? AND idempotency_key = ? LIMIT 1",
            (scope, idempotency_key),
        ).fetchone()
        return None if row is None else _run(row)

    def compare_and_set_state(
        self,
        run_id: str,
        *,
        expected: str,
        target: str,
        updated_at: float,
        result: dict | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        progress: float | None = None,
    ) -> bool:
        """条件更新：``WHERE state = expected``。

        返回 ``False`` 表示状态已被取消/超时改变——调用方**必须丢弃** provider 结果
        （设计 §5"取消后不 commit"）。
        """

        cursor = self._conn.execute(
            "UPDATE runs SET state = ?, result_json = ?, error_code = ?, error_message = ?, "
            "progress = COALESCE(?, progress), updated_at = ? WHERE id = ? AND state = ?",
            (
                str(target),
                None if result is None else _json_dump(result),
                error_code,
                error_message,
                progress,
                updated_at,
                run_id,
                str(expected),
            ),
        )
        return cursor.rowcount == 1

    def mark_in_flight_interrupted(self) -> list[str]:
        """启动收尾：把 ``queued``/``running`` 的 run 置 ``interrupted``（不自动重试）。"""

        rows = _rows(
            self._conn.execute("SELECT id FROM runs WHERE state IN ('queued', 'running')")
        )
        if not rows:
            return []
        ids = [str(row["id"]) for row in rows]
        self._conn.executemany(
            "UPDATE runs SET state = 'interrupted' WHERE id = ?", [(run_id,) for run_id in ids]
        )
        return ids


class SqliteRunEventRepository:
    __slots__ = ("_conn",)

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def append(self, event: RunEvent) -> None:
        self._conn.execute(
            "INSERT INTO run_events(run_id, seq, type, payload_json, created_at) VALUES(?,?,?,?,?)",
            (event.run_id, event.seq, event.type, _json_dump(event.payload), event.created_at),
        )

    def list_for_run(self, run_id: str, *, after_seq: int = 0, limit: int = 200) -> list[RunEvent]:
        rows = _rows(
            self._conn.execute(
                "SELECT * FROM run_events WHERE run_id = ? AND seq > ? ORDER BY seq LIMIT ?",
                (run_id, int(after_seq), int(limit)),
            )
        )
        return [_run_event(row) for row in rows]

    def next_seq(self, run_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM run_events WHERE run_id = ?", (run_id,)
        ).fetchone()
        return int(row["next"])


class SqliteWorkspaceBindingRepository:
    """单行访问上下文（``CHECK (id = 1)`` 保证不可能出现第二行）。"""

    __slots__ = ("_conn",)

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self) -> WorkspaceBinding | None:
        row = self._conn.execute("SELECT * FROM workspace_binding WHERE id = 1").fetchone()
        return None if row is None else _binding(row)

    def save(self, binding: WorkspaceBinding) -> None:
        self._conn.execute(
            "INSERT INTO workspace_binding(id, current_workspace_id, origin_workspace_id, owner_id, "
            "copy_id, storage_layout, updated_at) VALUES(1,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "  current_workspace_id = excluded.current_workspace_id, "
            "  origin_workspace_id = excluded.origin_workspace_id, "
            "  owner_id = excluded.owner_id, "
            "  copy_id = excluded.copy_id, "
            "  storage_layout = excluded.storage_layout, "
            "  updated_at = excluded.updated_at",
            (
                binding.current_workspace_id,
                binding.origin_workspace_id,
                binding.owner_id,
                binding.copy_id,
                str(binding.storage_layout),
                binding.updated_at,
            ),
        )

    def rebind(self, binding: WorkspaceBinding, *, event_payload: dict) -> None:
        """重绑 + **同一事务**内写 ``workspace_rebound`` 事件。"""

        self.save(binding)
        record_workspace_event(
            self._conn,
            "workspace_rebound",
            payload=dict(event_payload or {}),
            created_at=binding.updated_at,
        )


def record_workspace_event(
    conn: sqlite3.Connection,
    event_type: str,
    *,
    payload: Mapping[str, Any] | None,
    created_at: float,
) -> None:
    """追加一条 workspace 级事件（重绑审计等）。"""

    conn.execute(
        "INSERT INTO workspace_events(type, payload_json, created_at) VALUES(?,?,?)",
        (event_type, _json_dump(payload), created_at),
    )


class SqliteArtifactRepository:
    """artifacts / builds 的实现（端口 ``ArtifactStore``）。"""

    __slots__ = ("_conn",)

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def add(self, artifact: DocumentArtifact) -> None:
        self._conn.execute(
            "INSERT INTO document_artifacts(id, revision_id, kind, path, content_hash, size_bytes, "
            "created_at) VALUES(?,?,?,?,?,?,?)",
            (
                artifact.id,
                artifact.revision_id,
                str(artifact.kind),
                artifact.path,
                artifact.content_hash,
                artifact.size_bytes,
                artifact.created_at,
            ),
        )

    def get(self, artifact_id: str) -> DocumentArtifact | None:
        row = self._conn.execute(
            "SELECT * FROM document_artifacts WHERE id = ?", (artifact_id,)
        ).fetchone()
        return None if row is None else _artifact(row)

    def list_for_revision(self, revision_id: str) -> list[DocumentArtifact]:
        rows = _rows(
            self._conn.execute(
                "SELECT * FROM document_artifacts WHERE revision_id = ? ORDER BY created_at, rowid",
                (revision_id,),
            )
        )
        return [_artifact(row) for row in rows]

    def latest_for_revision(self, revision_id: str, *, kind: str) -> DocumentArtifact | None:
        row = self._conn.execute(
            "SELECT * FROM document_artifacts WHERE revision_id = ? AND kind = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (revision_id, str(kind)),
        ).fetchone()
        return None if row is None else _artifact(row)

    def add_build(self, build: DocumentBuild) -> None:
        self._conn.execute(
            "INSERT INTO document_builds(id, revision_id, status, source_map, artifact_id, "
            "engine_version, error_code, error_message, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                build.id,
                build.revision_id,
                str(build.status),
                None if build.source_map is None else _json_dump(build.source_map),
                build.artifact_id,
                build.engine_version,
                build.error_code,
                build.error_message,
                build.created_at,
                build.updated_at,
            ),
        )

    def get_build(self, build_id: str) -> DocumentBuild | None:
        row = self._conn.execute(
            "SELECT * FROM document_builds WHERE id = ?", (build_id,)
        ).fetchone()
        return None if row is None else _build(row)

    def update_build(self, build: DocumentBuild) -> None:
        self._conn.execute(
            "UPDATE document_builds SET status = ?, source_map = ?, artifact_id = ?, engine_version = ?, "
            "error_code = ?, error_message = ?, updated_at = ? WHERE id = ?",
            (
                str(build.status),
                None if build.source_map is None else _json_dump(build.source_map),
                build.artifact_id,
                build.engine_version,
                build.error_code,
                build.error_message,
                build.updated_at,
                build.id,
            ),
        )

    def list_builds(self, revision_id: str) -> list[DocumentBuild]:
        rows = _rows(
            self._conn.execute(
                "SELECT * FROM document_builds WHERE revision_id = ? ORDER BY created_at, rowid",
                (revision_id,),
            )
        )
        return [_build(row) for row in rows]


__all__ = [
    "SqliteArtifactRepository",
    "SqliteDocumentRepository",
    "SqliteEdgeRepository",
    "SqliteExplanationRepository",
    "SqliteExtractionRepository",
    "SqliteNarrationRepository",
    "SqliteNodeRepository",
    "SqliteProjectRepository",
    "SqliteResourceRepository",
    "SqliteRevisionRepository",
    "SqliteRunEventRepository",
    "SqliteRunRepository",
    "SqliteWorkspaceBindingRepository",
    "record_workspace_event",
]
