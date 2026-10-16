"""explanation workflow：单个 SourceRef context → provider → Explanation。

provider 契约复用 foundation ``application.ports.provider.ModelProvider``
（``GenerateRequest``/``GenerateResult``），**不**自定义 provider 协议。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.application.format.errors import InvalidRunInput
from backend.application.ports.provider import GenerateRequest, ModelProvider
from backend.domain.errors import ProviderUnavailable
from backend.plugins.ports import WorkflowContext


@dataclass(frozen=True, slots=True)
class ExplanationResult:
    node_id: str | None
    question: str | None
    answer: str
    provider: str
    model: str


class ExplanationWorkflow:
    kind = "explanation"
    workload = "model"

    def __init__(self, provider: ModelProvider, *, max_context_chars: int = 8000) -> None:
        self._provider = provider
        self._max_context_chars = max_context_chars

    def validate_input(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise InvalidRunInput("explanation 输入必须是对象")
        refs = payload.get("refs")
        if not isinstance(refs, list) or not refs:
            raise InvalidRunInput("explanation 需要至少一个 SourceRef（refs）")
        question = payload.get("question")
        if question is not None and not isinstance(question, str):
            raise InvalidRunInput("question 必须是字符串")
        node_id = payload.get("node_id")
        if node_id is not None and not isinstance(node_id, str):
            raise InvalidRunInput("node_id 必须是字符串")
        return {
            "refs": refs,
            "question": question,
            "node_id": node_id,
            "budget": payload.get("budget") or {},
        }

    def run(self, context: WorkflowContext, payload: dict[str, Any]) -> ExplanationResult:
        prompt = self._build_prompt(context, payload.get("question"))
        response = self._provider.generate(
            GenerateRequest(kind=self.kind, prompt=prompt, system=_SYSTEM_PROMPT)
        )
        answer = (response.text or "").strip()
        if not answer:
            raise ProviderUnavailable(
                "provider 返回空结果", details={"provider": response.provider}
            )
        return ExplanationResult(
            node_id=payload.get("node_id"),
            question=payload.get("question"),
            answer=answer,
            provider=response.provider,
            model=response.model,
        )

    def _build_prompt(self, context: WorkflowContext, question: str | None) -> str:
        body = context.text[: self._max_context_chars]
        ask = question or "请解释这段内容。"
        return f"问题：{ask}\n\n引用内容：\n{body}"


_SYSTEM_PROMPT = "你是学习助手，只依据给定引用内容解释，不编造引用之外的事实。"
