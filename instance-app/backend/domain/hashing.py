"""内容摘要（原件/artifacts 的版本哈希）。

``content_hash`` 是原件字节摘要：revision 不可变的判据、ETag 的派生来源。
只用标准库 ``hashlib``，无 IO。
"""

from __future__ import annotations

import hashlib
import re
from typing import Iterable

CONTENT_HASH_ALGO = "sha256"
_CONTENT_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
CHUNK_SIZE = 1024 * 256


def content_hash(data: bytes) -> str:
    """返回 64 位小写十六进制的 SHA-256。"""

    return hashlib.sha256(data).hexdigest()


def content_hash_of_chunks(chunks: Iterable[bytes]) -> str:
    """流式摘要：大文件不整块读进内存。"""

    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(chunk)
    return digest.hexdigest()


def is_content_hash(value: str) -> bool:
    return isinstance(value, str) and _CONTENT_HASH_RE.match(value) is not None


def etag_for_hash(value: str) -> str:
    """由 ``content_hash`` 派生强 ETag（原件端点的条件请求）。"""

    return f'"{value}"'


def if_none_match_matches(header_value: str | None, etag: str) -> bool:
    """``If-None-Match`` 是否命中该 ETag（支持 ``*`` 与逗号列表）。"""

    if not header_value:
        return False
    candidates = [item.strip() for item in header_value.split(",")]
    if "*" in candidates:
        return True
    return etag in candidates
