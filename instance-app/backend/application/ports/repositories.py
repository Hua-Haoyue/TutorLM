"""repository 端口：只声明读写语义，**不裸开事务**。

写操作必须由 ``UnitOfWork`` 提供的事务边界包裹（设计 §4.3）。repository
实现（``infrastructure/sqlite``）只使用 UoW 注入的连接，自己不做 ``BEGIN``。

命名约定：``add_*`` 仅插入；``update_*`` 只在行存在时更新；``save_*`` 表示 upsert
（仅用于单行的 ``workspace_binding`` 与幂等的 ``project``）。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from backend.domain.models import (
    Document,
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


@runtime_checkable
class ProjectRepository(Protocol):
    """每 workspace 恰好一行。"""

    def get_for_workspace(self, workspace_id: str) -> Project | None:
        ...

    def get(self, project_id: str) -> Project | None:
        ...

    def save(self, project: Project) -> None:
        """upsert：``workspace_id`` 唯一，重复初始化幂等。"""

        ...

    def update(self, project: Project) -> None:
        ...


@runtime_checkable
class DocumentRepository(Protocol):
    def add(self, document: Document) -> None:
        ...

    def get(self, document_id: str) -> Document | None:
        ...

    def list_for_project(self, project_id: str) -> list[Document]:
        ...

    def update(self, document: Document) -> None:
        ...

    def set_current_revision(self, document_id: str, revision_id: str, *, size_bytes: int, updated_at: float) -> None:
        ...

    def next_order_index(self, project_id: str) -> int:
        ...

    def delete(self, document_id: str) -> None:
        ...


@runtime_checkable
class RevisionRepository(Protocol):
    def add(self, revision: DocumentRevision) -> None:
        ...

    def get(self, revision_id: str) -> DocumentRevision | None:
        ...

    def list_for_document(self, document_id: str) -> list[DocumentRevision]:
        ...

    def next_revision_no(self, document_id: str) -> int:
        ...

    def by_content_hash(self, document_id: str, content_hash: str) -> DocumentRevision | None:
        """用于"同一内容不重复落原件"的幂等判定。"""

        ...


@runtime_checkable
class ResourceRepository(Protocol):
    def add_many(self, resources: list[DocumentResource]) -> None:
        ...

    def list_for_revision(self, revision_id: str) -> list[DocumentResource]:
        ...

    def get_by_logical_path(self, revision_id: str, logical_path: str) -> DocumentResource | None:
        ...


@runtime_checkable
class ExtractionRepository(Protocol):
    def add(self, extraction: DocumentExtraction) -> None:
        ...

    def update(self, extraction: DocumentExtraction) -> None:
        ...

    def get(self, extraction_id: str) -> DocumentExtraction | None:
        ...

    def list_for_revision(self, revision_id: str) -> list[DocumentExtraction]:
        ...

    def latest(self, revision_id: str, *, extractor_version: str | None = None) -> DocumentExtraction | None:
        ...


@runtime_checkable
class NodeRepository(Protocol):
    def add(self, node: Node) -> None:
        ...

    def get(self, node_id: str) -> Node | None:
        ...

    def list_for_project(self, project_id: str) -> list[Node]:
        ...

    def list_for_document(self, document_id: str) -> list[Node]:
        ...

    def update(self, node: Node) -> None:
        ...

    def delete(self, node_id: str) -> list[str]:
        """删除并**把子节点提升为顶层**（``ON DELETE SET NULL`` 语义）。

        返回被提升为顶层的子节点 id 列表（便于测试与事件断言）。
        """

        ...

    def children_of(self, node_id: str) -> list[Node]:
        ...

    def next_order_index(self, project_id: str) -> int:
        ...


@runtime_checkable
class EdgeRepository(Protocol):
    def add(self, edge: Edge) -> None:
        ...

    def get(self, edge_id: str) -> Edge | None:
        ...

    def list_for_project(self, project_id: str) -> list[Edge]:
        ...

    def delete(self, edge_id: str) -> None:
        ...

    def delete_for_node(self, node_id: str) -> None:
        ...


@runtime_checkable
class ExplanationRepository(Protocol):
    def add(self, explanation: Explanation) -> None:
        ...

    def get(self, explanation_id: str) -> Explanation | None:
        ...

    def list_for_project(self, project_id: str) -> list[Explanation]:
        ...

    def list_for_node(self, node_id: str) -> list[Explanation]:
        ...


@runtime_checkable
class NarrationRepository(Protocol):
    def add(self, narration: Narration, segments: list[NarrationSegment]) -> None:
        """一次写入串讲头与全部段（段序即 ``order_index``）。"""

        ...

    def get(self, narration_id: str) -> Narration | None:
        ...

    def list_for_project(self, project_id: str) -> list[Narration]:
        ...

    def list_segments(self, narration_id: str) -> list[NarrationSegment]:
        ...


@runtime_checkable
class RunRepository(Protocol):
    def add(self, run: Run) -> None:
        ...

    def get(self, run_id: str) -> Run | None:
        ...

    def list_for_project(self, project_id: str, *, limit: int = 100) -> list[Run]:
        ...

    def update(self, run: Run) -> None:
        ...

    def find_by_idempotency_key(self, scope: str, idempotency_key: str) -> Run | None:
        """幂等作用域是 **workspace 身份**（同 workspace 同 key 幂等）。"""

        ...

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
        """条件更新：只有当前状态仍等于 ``expected`` 才写入。

        返回 ``False`` 表示状态已被他人改变（取消/超时），调用方**必须丢弃**结果。
        """

        ...

    def mark_in_flight_interrupted(self) -> list[str]:
        """启动收尾：把 ``queued``/``running`` 的 run 置 ``interrupted``。

        返回被改动的 run id；**不**自动重试（避免重复计费）。
        """

        ...


@runtime_checkable
class RunEventRepository(Protocol):
    def append(self, event: RunEvent) -> None:
        ...

    def list_for_run(self, run_id: str, *, after_seq: int = 0, limit: int = 200) -> list[RunEvent]:
        ...

    def next_seq(self, run_id: str) -> int:
        ...


@runtime_checkable
class WorkspaceBindingRepository(Protocol):
    """单行访问上下文；克隆时只改这里。"""

    def get(self) -> WorkspaceBinding | None:
        ...

    def save(self, binding: WorkspaceBinding) -> None:
        """upsert（至多一行）。"""

        ...

    def rebind(self, binding: WorkspaceBinding, *, event_payload: dict) -> None:
        """重绑 + 同事务写 ``workspace_rebound`` 事件。"""

        ...
