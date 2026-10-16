"""不可变原件与 artifacts 的本地实现。

路径布局（设计 §4.1）::

    <data_dir>/sources/<document_id>/<revision_id>/main.<ext>
    <data_dir>/sources/<document_id>/<revision_id>/resources/...
    <data_dir>/artifacts/<document_id>/<revision_id>/<kind>/<name>

不变量：

- **原件不可变**：目录已存在（含 ``main`` 文件）即拒绝覆盖，抛
  ``ImmutableContentError``；替换内容必须走新 revision；
- 目录名/文件名由服务端生成（``<document_id>``/``<revision_id>``/规范文件名），
  不使用用户提供的文件名或路径；
- 所有读取都经过 ``assert_regular_file``：拒绝符号链接与非常规文件。
"""

from __future__ import annotations

import os
from typing import BinaryIO, Iterator

from backend.application.ports.content import StoredObject
from backend.domain.errors import (
    ImmutableContentError,
    NotFound,
    PathOutsideRoot,
)
from backend.domain.hashing import content_hash_of_chunks
from backend.domain.paths import assert_safe_segment, normalize_relative_path
from backend.infrastructure.storage.paths import (
    assert_directory,
    assert_regular_file,
    resolve_relative_under_root,
    resolve_under_root,
    safe_open_read,
)

SOURCES_DIR = "sources"
ARTIFACTS_DIR = "artifacts"
RESOURCES_DIR = "resources"
#: 原作者件统一用这个文件名（服务端生成，不用用户文件名）。
MAIN_STEM = "main"

#: 读取区间时单次产出的块大小。
CHUNK_SIZE = 1024 * 256


class LocalContentStore:
    """按 ``data_dir`` 组织原件与 artifacts 的内容存储。"""

    __slots__ = ("_root",)

    def __init__(self, data_dir: str) -> None:
        self._root = os.path.abspath(data_dir)

    # ------------------------------------------------------------ 属性
    @property
    def root(self) -> str:
        return self._root

    @property
    def sources_root(self) -> str:
        return os.path.join(self._root, SOURCES_DIR)

    @property
    def artifacts_root(self) -> str:
        return os.path.join(self._root, ARTIFACTS_DIR)

    # ------------------------------------------------------------ 原件
    def revision_dir(
        self, *, document_id: str, revision_id: str, create: bool = False
    ) -> str:
        path = resolve_under_root(
            self.sources_root,
            assert_safe_segment(document_id, what="document_id"),
            assert_safe_segment(revision_id, what="revision_id"),
        )
        if create:
            os.makedirs(path, exist_ok=True)
        return path

    def write_source(
        self,
        *,
        document_id: str,
        revision_id: str,
        filename: str,
        data: bytes,
    ) -> StoredObject:
        """写一份**新**不可变原件；目标已存在则拒绝覆盖。"""

        if not isinstance(data, (bytes, bytearray)):
            raise PathOutsideRoot("原件数据必须是 bytes")
        safe_name = assert_safe_segment(filename, what="原文件名")
        directory = self.revision_dir(document_id=document_id, revision_id=revision_id, create=True)
        target = os.path.join(directory, safe_name)
        if os.path.lexists(target):
            raise ImmutableContentError(
                "该 revision 的原件已存在：原件不可变，替换内容必须创建新 revision",
                details={"document_id": document_id, "revision_id": revision_id, "filename": safe_name},
            )
        # 先写临时文件再原子改名：hardstop 不会留下半个"已存在"的原件。
        tmp = f"{target}.tmp-{os.getpid()}"
        with open(tmp, "wb") as handle:
            handle.write(bytes(data))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        return StoredObject(
            relative_path=self._relative(target),
            content_hash=content_hash_of_chunks([bytes(data)]),
            size_bytes=len(data),
        )

    def write_resources(
        self,
        *,
        document_id: str,
        revision_id: str,
        resources: list[tuple[str, bytes]],
    ) -> list[StoredObject]:
        """写 tex 工程的 resources；``logical_path`` 必须是工程内 canonical 相对路径。"""

        directory = self.revision_dir(document_id=document_id, revision_id=revision_id, create=True)
        base = os.path.join(directory, RESOURCES_DIR)
        os.makedirs(base, exist_ok=True)
        written: list[StoredObject] = []
        for logical_path, payload in resources:
            normalized = normalize_relative_path(logical_path)
            target = resolve_relative_under_root(base, normalized)
            parent = os.path.dirname(target)
            os.makedirs(parent, exist_ok=True)
            if os.path.lexists(target):
                raise ImmutableContentError(
                    "revision 内该 resource 已存在：原件不可变",
                    details={"logical_path": normalized},
                )
            with open(target, "wb") as handle:
                handle.write(bytes(payload))
            written.append(
                StoredObject(
                    relative_path=self._relative(target),
                    content_hash=content_hash_of_chunks([bytes(payload)]),
                    size_bytes=len(payload),
                )
            )
        return written

    # ------------------------------------------------------------ artifacts
    def artifact_relative_path(
        self, *, document_id: str, revision_id: str, kind: str, filename: str
    ) -> str:
        """计算 artifacts 相对路径（不落盘），便于"先登记元数据再写文件"。"""

        segments = [
            ARTIFACTS_DIR,
            assert_safe_segment(document_id, what="document_id"),
            assert_safe_segment(revision_id, what="revision_id"),
            assert_safe_segment(kind, what="artifact kind"),
            assert_safe_segment(filename, what="artifact 文件名"),
        ]
        return "/".join(segments)

    def write_artifact(
        self,
        *,
        document_id: str,
        revision_id: str,
        kind: str,
        filename: str,
        data: bytes,
    ) -> StoredObject:
        relative = self.artifact_relative_path(
            document_id=document_id, revision_id=revision_id, kind=kind, filename=filename
        )
        target = resolve_relative_under_root(self._root, relative)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as handle:
            handle.write(bytes(data))
        return StoredObject(
            relative_path=relative,
            content_hash=content_hash_of_chunks([bytes(data)]),
            size_bytes=len(data),
        )

    # ------------------------------------------------------------ 读取
    def _resolve(self, relative_path: str) -> str:
        if not relative_path:
            raise PathOutsideRoot("相对路径不能为空")
        normalized = normalize_relative_path(relative_path)
        return resolve_relative_under_root(self._root, normalized)

    def exists(self, *, relative_path: str) -> bool:
        try:
            path = self._resolve(relative_path)
        except PathOutsideRoot:
            return False
        if not os.path.isfile(path):
            return False
        try:
            assert_regular_file(path, what="原件")
        except Exception:
            return False
        return True

    def read_source(self, *, relative_path: str) -> bytes:
        path = self._resolve(relative_path)
        try:
            return safe_open_read(path, what="原件")
        except PathOutsideRoot as exc:
            if "不存在" in exc.message:
                raise NotFound(f"原件不存在：{relative_path}") from None
            raise

    def open_source(self, *, relative_path: str) -> BinaryIO:
        path = self._resolve(relative_path)
        assert_regular_file(path, what="原件")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        return os.fdopen(os.open(path, flags), "rb")

    def iter_source(
        self, *, relative_path: str, start: int = 0, end: int | None = None
    ) -> Iterator[bytes]:
        """按字节区间迭代（Range 端点用）；越界由调用方判定为 416。"""

        path = self._resolve(relative_path)
        assert_regular_file(path, what="原件")
        if start < 0:
            raise PathOutsideRoot("Range 起点不能为负")
        with open(path, "rb") as handle:
            handle.seek(start)
            remaining = None if end is None else max(0, end - start + 1)
            while True:
                chunk = handle.read(CHUNK_SIZE if remaining is None else min(CHUNK_SIZE, remaining))
                if not chunk:
                    return
                yield chunk
                if remaining is not None:
                    remaining -= len(chunk)
                    if remaining <= 0:
                        return

    def source_size(self, *, relative_path: str) -> int:
        path = self._resolve(relative_path)
        info = assert_regular_file(path, what="原件")
        return int(info.st_size)

    # ------------------------------------------------------------ 内部
    def _relative(self, absolute_path: str) -> str:
        root_real = os.path.realpath(self._root)
        target = os.path.realpath(absolute_path)
        if not target.startswith(root_real + os.sep):
            raise PathOutsideRoot("路径越出数据根", details={"path": absolute_path})
        return target[len(root_real) + 1 :].replace(os.sep, "/")

    def ensure_roots(self) -> None:
        os.makedirs(self.sources_root, exist_ok=True)
        os.makedirs(self.artifacts_root, exist_ok=True)
        for name in (self.sources_root, self.artifacts_root):
            assert_directory(name, what="内容目录")


__all__ = ["LocalContentStore", "MAIN_STEM", "RESOURCES_DIR", "SOURCES_DIR", "ARTIFACTS_DIR"]
