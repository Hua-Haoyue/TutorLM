"""领域实体与值对象（无 IO）。

字段名对齐 ``docs/instance-app-design.md`` §4.3 schema 与
``docs/instance-app-api.md`` 的 TS 形状。这里保留**全部持久化字段**；
展示层裁剪由 adapters/http 决定（本层不做序列化取舍）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from backend.domain.enums import (
    ArtifactKind,
    BuildStatus,
    DocumentFormat,
    DocumentStatus,
    EdgeRelation,
    ExtractionStatus,
    LearnStatus,
    NodeKind,
    RevisionStatus,
    RunKind,
    RunState,
    RunWorkload,
)
from backend.domain.errors import DomainError
from backend.domain.hashing import content_hash, is_content_hash


def _iso(ts: float) -> str:
    """Unix 秒 -> UTC ISO8601（``Z`` 后缀）。展示用；库里仍存数值。"""

    from datetime import datetime, timezone

    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _dump(metadata: Mapping[str, Any] | None) -> str:
    return json.dumps(dict(metadata or {}), ensure_ascii=False, sort_keys=True)


def _load(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:  # pragma: no cover - 数据损坏时才发生
        raise DomainError("metadata_json 已损坏", details={"error": str(exc)}) from None
    if not isinstance(decoded, dict):
        raise DomainError("metadata_json 必须是 JSON 对象")
    return decoded


@dataclass(slots=True)
class Project:
    """每 workspace 恰好一行；``workspace_id`` 唯一。"""

    id: str
    workspace_id: str
    title: str
    created_at: float
    updated_at: float
    description: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "workspace_id": self.workspace_id,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }
        if self.description is not None:
            payload["description"] = self.description
        return payload


@dataclass(slots=True)
class Document:
    """一份文档；``format`` 由服务端探测，``current_revision_id`` 指向最新 revision。"""

    id: str
    project_id: str
    title: str
    format: DocumentFormat
    status: DocumentStatus
    created_at: float
    updated_at: float
    source_filename: str | None = None
    current_revision_id: str | None = None
    size_bytes: int = 0
    order_index: int = 0

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "project_id": self.project_id,
            "title": self.title,
            "format": str(self.format),
            "status": str(self.status),
            "size_bytes": self.size_bytes,
            "order_index": self.order_index,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }
        if self.source_filename is not None:
            payload["source_filename"] = self.source_filename
        if self.current_revision_id is not None:
            payload["current_revision_id"] = self.current_revision_id
        return payload


@dataclass(slots=True)
class DocumentRevision:
    """不可变原件的一行；``content_hash`` 为原件字节摘要。"""

    id: str
    document_id: str
    revision_no: int
    content_hash: str
    size_bytes: int
    status: RevisionStatus
    created_at: float
    source_filename: str | None = None
    media_type: str | None = None

    def __post_init__(self) -> None:
        if self.revision_no < 1:
            raise DomainError("revision_no 必须从 1 开始")
        if self.size_bytes < 0:
            raise DomainError("size_bytes 不能为负")
        if not is_content_hash(self.content_hash):
            raise DomainError(f"content_hash 必须是 {content_hash(b'')} 形式的 SHA-256")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "document_id": self.document_id,
            "revision_no": self.revision_no,
            "content_hash": self.content_hash,
            "size_bytes": self.size_bytes,
            "status": str(self.status),
            "created_at": _iso(self.created_at),
        }


@dataclass(slots=True)
class DocumentResource:
    """tex 多文件工程的一个 resource：``logical_path`` 是工程内相对路径，非磁盘路径。"""

    id: str
    revision_id: str
    logical_path: str
    kind: str
    content_hash: str
    size_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "revision_id": self.revision_id,
            "logical_path": self.logical_path,
            "kind": self.kind,
            "content_hash": self.content_hash,
            "size_bytes": self.size_bytes,
        }


@dataclass(slots=True)
class DocumentArtifact:
    """派生产物元数据；正文/二进制落 ``artifacts/``，**不入库**。"""

    id: str
    revision_id: str
    kind: ArtifactKind
    path: str
    content_hash: str
    size_bytes: int
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "revision_id": self.revision_id,
            "kind": str(self.kind),
            "path": self.path,
            "content_hash": self.content_hash,
            "size_bytes": self.size_bytes,
            "created_at": _iso(self.created_at),
        }


@dataclass(slots=True)
class DocumentExtraction:
    """抽取结果元数据；``extractor_version`` 变化即失效。"""

    id: str
    revision_id: str
    extractor_version: str
    status: ExtractionStatus
    created_at: float
    text_hash: str | None = None
    page_count: int | None = None
    error_code: str | None = None
    artifact_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "revision_id": self.revision_id,
            "extractor_version": self.extractor_version,
            "status": str(self.status),
            "created_at": _iso(self.created_at),
        }
        if self.text_hash is not None:
            payload["text_hash"] = self.text_hash
        if self.page_count is not None:
            payload["page_count"] = self.page_count
        if self.error_code is not None:
            payload["error_code"] = self.error_code
        return payload


@dataclass(slots=True)
class DocumentBuild:
    """编译 build；``source_map`` 可选，**不虚构**。"""

    id: str
    revision_id: str
    status: BuildStatus
    created_at: float
    updated_at: float
    artifact_id: str | None = None
    engine_version: str | None = None
    source_map: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "revision_id": self.revision_id,
            "status": str(self.status),
            "source_map": self.source_map,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }
        if self.artifact_id is not None:
            payload["artifact_id"] = self.artifact_id
        if self.engine_version is not None:
            payload["engine_version"] = self.engine_version
        if self.error_code is not None:
            payload["error"] = {"code": self.error_code, "message": self.error_message or ""}
        return payload


@dataclass(slots=True)
class Node:
    """图谱节点；``revision`` 用于乐观并发（PATCH 不匹配 -> 409）。"""

    id: str
    project_id: str
    document_id: str
    title: str
    kind: NodeKind
    revision: int
    created_at: float
    updated_at: float
    summary: str | None = None
    content: str | None = None
    order_index: int = 0
    parent_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.revision < 1:
            raise DomainError("nodes.revision 必须从 1 开始")

    @property
    def learn_status(self) -> str:
        raw = str(self.metadata.get("status") or LearnStatus.LEARNING)
        return raw

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "project_id": self.project_id,
            "document_id": self.document_id,
            "title": self.title,
            "kind": str(self.kind),
            "order_index": self.order_index,
            "metadata": dict(self.metadata),
            "revision": self.revision,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }
        if self.summary is not None:
            payload["summary"] = self.summary
        if self.content is not None:
            payload["content"] = self.content
        if self.parent_id is not None:
            payload["parent_id"] = self.parent_id
        return payload


@dataclass(slots=True)
class Edge:
    id: str
    project_id: str
    source_node_id: str
    target_node_id: str
    relation: EdgeRelation
    created_at: float
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "project_id": self.project_id,
            "source_node_id": self.source_node_id,
            "target_node_id": self.target_node_id,
            "relation": str(self.relation),
            "created_at": _iso(self.created_at),
        }
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload


@dataclass(slots=True)
class Explanation:
    id: str
    project_id: str
    node_id: str
    answer: str
    provider: str
    model: str
    created_at: float
    question: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "node_id": self.node_id,
            "answer": self.answer,
            "provider": self.provider,
            "model": self.model,
            "created_at": _iso(self.created_at),
        }
        if self.question is not None:
            payload["question"] = self.question
        return payload


@dataclass(slots=True)
class NarrationSegment:
    """串讲一段；``order_index`` 即数组顺序。"""

    id: str
    narration_id: str
    order_index: int
    node_id: str
    title: str
    text: str
    duration_seconds: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "title": self.title,
            "text": self.text,
            "duration_seconds": self.duration_seconds,
        }


@dataclass(slots=True)
class Narration:
    """``segment_count`` 与 ``total_duration_seconds`` 为**服务端派生**："""

    id: str
    project_id: str
    title: str
    provider: str
    model: str
    created_at: float
    segments: list[NarrationSegment] = field(default_factory=list)

    @property
    def segment_count(self) -> int:
        """等于 ``len(segments)``——不是独立真值，不落库。"""

        return len(self.segments)

    @property
    def total_duration_seconds(self) -> float:
        """等于各段 ``duration_seconds`` 之和——服务端派生，不落库。"""

        return sum(float(segment.duration_seconds) for segment in self.segments)

    def to_dict(self) -> dict[str, Any]:
        ordered = sorted(self.segments, key=lambda item: item.order_index)
        return {
            "id": self.id,
            "title": self.title,
            "segments": [segment.to_dict() for segment in ordered],
            "segment_count": len(ordered),
            "total_duration_seconds": sum(float(s.duration_seconds) for s in ordered),
            "provider": self.provider,
            "model": self.model,
            "created_at": _iso(self.created_at),
        }


@dataclass(slots=True)
class Run:
    """任务行；``state`` 走领域状态机。``input``/``result`` 为 JSON 对象。"""

    id: str
    project_id: str
    kind: RunKind
    state: RunState
    created_at: float
    updated_at: float
    workload: RunWorkload = RunWorkload.MODEL
    input: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    idempotency_key: str | None = None
    #: 幂等作用域 = workspace 身份（同 workspace 同 key 幂等），与应用 session 区分。
    idempotency_scope: str | None = None
    progress: float | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "kind": str(self.kind),
            "state": str(self.state),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }
        if self.progress is not None:
            payload["progress"] = self.progress
        if self.input:
            payload["input"] = dict(self.input)
        if self.result is not None:
            payload["result"] = dict(self.result)
        if self.error_code is not None:
            payload["error"] = {"code": self.error_code, "message": self.error_message or ""}
        return payload


@dataclass(slots=True)
class RunEvent:
    """SSE 可选事件：``seq`` 在单个 run 内单调递增。"""

    run_id: str
    seq: int
    type: str
    created_at: float
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "seq": self.seq,
            "type": self.type,
            "payload": dict(self.payload),
            "created_at": _iso(self.created_at),
        }


__all__ = [
    "Project",
    "Document",
    "DocumentRevision",
    "DocumentResource",
    "DocumentArtifact",
    "DocumentExtraction",
    "DocumentBuild",
    "Node",
    "Edge",
    "Explanation",
    "Narration",
    "NarrationSegment",
    "Run",
    "RunEvent",
    "_load",
    "_dump",
    "_iso",
]
