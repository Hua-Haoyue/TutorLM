"""内容/原件存储端口。

**不可变原件**：写一次即不可改；替换产生新 revision（设计 §14.1）。
``sources/<document_id>/<revision_id>/`` 下的文件名由服务端生成，
不接受用户文件名或路径。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import BinaryIO, Iterator, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class StoredObject:
    """已落盘原件的元数据。"""

    #: 相对数据根的 posix 路径（如 ``sources/<doc>/<rev>/main.md``）。
    relative_path: str
    content_hash: str
    size_bytes: int


@runtime_checkable
class ContentStore(Protocol):
    """原件与 artifacts 的文件存储。"""

    def write_source(
        self,
        *,
        document_id: str,
        revision_id: str,
        filename: str,
        data: bytes,
    ) -> StoredObject:
        """写一份**新**不可变原件；目标已存在则报 ``ImmutableContentError``。"""

        ...

    def write_resources(
        self,
        *,
        document_id: str,
        revision_id: str,
        resources: list[tuple[str, bytes]],
    ) -> list[StoredObject]:
        """写 tex 工程的 resources（``logical_path`` -> bytes）。"""

        ...

    def write_artifact(
        self,
        *,
        document_id: str,
        revision_id: str,
        kind: str,
        filename: str,
        data: bytes,
    ) -> StoredObject:
        """写一份派生产物（抽取文本/编译 PDF/build 日志）。"""

        ...

    def read_source(self, *, relative_path: str) -> bytes:
        """按相对路径读原件；越界/符号链接一律拒绝。"""

        ...

    def open_source(self, *, relative_path: str) -> BinaryIO:
        """流式读（供 Range/HEAD 使用）。"""

        ...

    def iter_source(self, *, relative_path: str, start: int = 0, end: int | None = None) -> Iterator[bytes]:
        """按字节区间迭代读取。"""

        ...

    def source_size(self, *, relative_path: str) -> int:
        ...

    def exists(self, *, relative_path: str) -> bool:
        ...

    def artifact_relative_path(self, *, document_id: str, revision_id: str, kind: str, filename: str) -> str:
        """计算 artifacts 相对路径（不落盘），用于先登记元数据再写文件。"""

        ...
