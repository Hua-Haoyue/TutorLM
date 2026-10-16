"""内容存储：安全路径、不可变原件、artifacts、布局证据采集。"""

from __future__ import annotations

from backend.infrastructure.storage.layout import (
    collect_proof_facts,
    ensure_content_layout,
    write_layout_marker,
)
from backend.infrastructure.storage.local import LocalContentStore

__all__ = [
    "LocalContentStore",
    "collect_proof_facts",
    "ensure_content_layout",
    "write_layout_marker",
]
