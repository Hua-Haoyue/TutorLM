"""narration workflow：多个 node 的 SourceRef context → provider → Narration（含派生字段）。

provider 契约复用 foundation ``application.ports.provider.ModelProvider``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.application.format.errors import InvalidRunInput
from backend.application.ports.provider import GenerateRequest, ModelProvider
from backend.domain.errors import ProviderUnavailable
from backend.plugins.ports import WorkflowContext

MAX_NODE_IDS = 10


@dataclass(frozen=True, slots=True)
class NarrationSegment:
    node_id: str
    title: str
    text: str
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class NarrationResult:
    id: str
    title: str
    segments: tuple[NarrationSegment, ...]
    segment_count: int
    total_duration_seconds: float
    provider: str
    model: str


class NarrationWorkflow:
    kind = "narration"
    workload = "model"

    def __init__(self, provider: ModelProvider, *, max_context_chars: int = 12000,
                 seconds_per_100_chars: float = 4.0) -> None:
        self._provider = provider
        self._max_context_chars = max_context_chars
        self._seconds_per_100_chars = seconds_per_100_chars

    def validate_input(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise InvalidRunInput("narration 输入必须是对象")
        node_ids = payload.get("node_ids")
        if not isinstance(node_ids, list) or not node_ids:
            raise InvalidRunInput("narration 需要 node_ids")
        if len(node_ids) > MAX_NODE_IDS:
            raise InvalidRunInput(
                "narration 的 node_ids 超过上限",
                details={"count": len(node_ids), "limit": MAX_NODE_IDS},
            )
        if any(not isinstance(nid, str) for nid in node_ids):
            raise InvalidRunInput("node_ids 必须都是字符串")
        refs = payload.get("refs")
        if not isinstance(refs, list) or not refs:
            raise InvalidRunInput("narration 需要至少一个 SourceRef（refs）")
        title = payload.get("title")
        if title is not None and not isinstance(title, str):
            raise InvalidRunInput("title 必须是字符串")
        return {
            "refs": refs,
            "node_ids": node_ids,
            "title": title,
            "budget": payload.get("budget") or {},
        }

    def run(self, context: WorkflowContext, payload: dict[str, Any], *, narration_id: str) -> NarrationResult:
        node_ids: list[str] = payload["node_ids"]
        title = payload.get("title") or "串讲"
        per_node_chars = max(self._max_context_chars // max(len(node_ids), 1), 256)

        segments: list[NarrationSegment] = []
        provider_name = self._provider.info.name
        model = self._provider.info.model
        for index, node_id in enumerate(node_ids):
            span = self._slice_for_node(context, index, per_node_chars)
            prompt = f"请用口语化中文串讲第 {index + 1} 段：\n\n{span}"
            response = self._provider.generate(
                GenerateRequest(kind=self.kind, prompt=prompt, system=_SYSTEM_PROMPT)
            )
            text = (response.text or "").strip()
            if not text:
                raise ProviderUnavailable("provider 返回空段落", details={"node_id": node_id})
            provider_name = response.provider
            model = response.model
            segments.append(
                NarrationSegment(
                    node_id=node_id,
                    title=f"第 {index + 1} 段",
                    text=text,
                    duration_seconds=self._estimate_duration(text),
                )
            )

        return NarrationResult(
            id=narration_id,
            title=title,
            segments=tuple(segments),
            # 派生字段：服务端计算，前端不得重算/覆盖。
            segment_count=len(segments),
            total_duration_seconds=round(sum(s.duration_seconds for s in segments), 3),
            provider=provider_name,
            model=model,
        )

    def _slice_for_node(self, context: WorkflowContext, index: int, budget: int) -> str:
        if not context.items:
            return ""
        item = context.items[min(index, len(context.items) - 1)]
        return item.text[:budget]

    def _estimate_duration(self, text: str) -> float:
        return round(len(text) / 100 * self._seconds_per_100_chars, 3)


_SYSTEM_PROMPT = "你是串讲助手，只依据给定引用内容讲述，语言自然、口语化，不编造。"
