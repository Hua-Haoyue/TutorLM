"""Pure narration helpers: pacing estimation and segment assembly.

Deliberately dependency-free: the domain layer must not import the application
or port layers, so this module works on plain :class:`Node` sequences.
"""

from __future__ import annotations

import re
from typing import Sequence

from backend.domain.models import NarrationSegment, Node

# Roughly 4 CJK characters per second, ~15 Latin characters per second.
_CJK = re.compile(r"[\u4e00-\u9fff]")
MIN_SEGMENT_SECONDS = 1.5


def estimate_duration_seconds(text: str) -> float:
    """Estimate spoken duration for a paragraph of mixed CJK/Latin text."""

    text = text or ""
    cjk_count = len(_CJK.findall(text))
    latin_count = max(len(text) - cjk_count, 0)
    seconds = cjk_count / 4.0 + latin_count / 15.0
    return round(max(seconds, MIN_SEGMENT_SECONDS), 2)


def build_segments(nodes: Sequence[Node], paragraphs: Sequence[str]) -> list[NarrationSegment]:
    """Pair provider output paragraphs with their source nodes, in order."""

    segments: list[NarrationSegment] = []
    for index, node in enumerate(nodes):
        text = paragraphs[index] if index < len(paragraphs) else ""
        text = text.strip() if text else ""
        if not text:
            text = f"（{node.title} 暂无串讲内容）"
        segments.append(
            NarrationSegment(
                id='', narration_id='', order_index=index,
                node_id=node.id,
                title=node.title,
                text=text,
                duration_seconds=estimate_duration_seconds(text),
            )
        )
    return segments
