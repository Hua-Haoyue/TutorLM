"""``fake`` provider：确定性离线输出。

用途是测试与演示：**不联网、不读密钥、同输入必得同输出**。
响应中明确标识 ``provider.name=fake``，前端据此标识演示模型（设计 §7）。
"""

from __future__ import annotations

import hashlib

from backend.application.ports.provider import GenerateRequest, GenerateResult, ProviderInfo

#: 每次生成的确定性"句子"数量上限，避免输出长度随输入无界增长。
MAX_SENTENCES = 4


class FakeProvider:
    """确定性 provider：以输入摘要派生输出，因此相同输入必得相同输出。"""

    __slots__ = ("_model",)

    def __init__(self, model: str = "offline-demo") -> None:
        self._model = model or "offline-demo"

    @property
    def info(self) -> ProviderInfo:
        return ProviderInfo(name="fake", model=self._model)

    def generate(self, request: GenerateRequest) -> GenerateResult:
        material = "\n".join([request.kind, request.system or "", request.prompt, *request.context])
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
        text = self._compose(request, digest)
        payload = self._payload(request, digest, text)
        return GenerateResult(
            text=text,
            provider="fake",
            model=self._model,
            payload=payload,
            prompt_tokens=len(request.prompt.split()),
            completion_tokens=len(text.split()),
        )

    def close(self) -> None:
        return None

    # ------------------------------------------------------------ 内部
    def _compose(self, request: GenerateRequest, digest: str) -> str:
        """确定性文本：包含 kind 与摘要前缀，便于断言可重复性。"""

        sentences = [f"[fake:{request.kind}] 离线确定性输出 {digest[:12]}"]
        for index, chunk in enumerate(request.context[:MAX_SENTENCES]):
            head = " ".join(chunk.split())[:80]
            sentences.append(f"参考资料 {index + 1}：{head}")
        if not request.context:
            sentences.append("未提供引用上下文，本回复仅用于演示。")
        return "\n".join(sentences)

    def _payload(self, request: GenerateRequest, digest: str, text: str) -> dict[str, object]:
        return {
            "kind": request.kind,
            "deterministic": True,
            "input_digest": digest,
            "answer": text,
            "question": request.prompt,
            "citations": list(request.context),
        }

    def describe(self) -> dict[str, object]:
        return {"provider": "fake", "model": self._model, "offline": True}

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"FakeProvider({self._model!r})"
