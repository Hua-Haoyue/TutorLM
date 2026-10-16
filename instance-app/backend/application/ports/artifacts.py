"""派生产物（artifacts/builds）元数据端口。

正文不落库：库里只存 ``content_hash``/``size_bytes``/相对路径（设计 §4.3）。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from backend.domain.models import DocumentArtifact, DocumentBuild


@runtime_checkable
class ArtifactStore(Protocol):
    def add(self, artifact: DocumentArtifact) -> None:
        ...

    def get(self, artifact_id: str) -> DocumentArtifact | None:
        ...

    def list_for_revision(self, revision_id: str) -> list[DocumentArtifact]:
        ...

    def latest_for_revision(self, revision_id: str, *, kind: str) -> DocumentArtifact | None:
        """按 kind 取该 revision 最新产物（``partial``/``failed`` 不覆盖旧成功产物）。"""

        ...

    def add_build(self, build: DocumentBuild) -> None:
        ...

    def get_build(self, build_id: str) -> DocumentBuild | None:
        ...

    def update_build(self, build: DocumentBuild) -> None:
        ...

    def list_builds(self, revision_id: str) -> list[DocumentBuild]:
        ...
