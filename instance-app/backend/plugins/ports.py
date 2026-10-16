"""workflow 层的**输入值对象**；provider/时钟/id 端口一律复用 foundation。

对齐（对齐审查后）：**不再自定义** provider / clock / id / ref-resolver 协议。
- provider 契约 = ``backend.application.ports.provider.ModelProvider``
  （``GenerateRequest`` / ``GenerateResult`` / ``ProviderInfo``）。
- 引用解析（``SourceRef -> 文本``）是 **host 侧**职责，不属本层；workflow 只接收
  已解析好的 ``WorkflowContext``，因此这里不定义 ``RefResolverPort``。

本模块只保留 workflow 真正需要的上下文值对象（纯数据、无 IO）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.application.ports.provider import (  # re-export：单一真值在 foundation
    GenerateRequest,
    GenerateResult,
    ModelProvider,
    ProviderInfo,
)

__all__ = [
    "ContextBudget",
    "ContextItem",
    "GenerateRequest",
    "GenerateResult",
    "ModelProvider",
    "ProviderInfo",
    "WorkflowContext",
]


@dataclass(frozen=True, slots=True)
class ContextBudget:
    """context 预算（由 host 的 resolve 施加；workflow 只读）。"""

    max_chars: int | None = None
    max_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ContextItem:
    """组装进 workflow 的一段上下文（保持可追溯）。"""

    document_id: str
    revision_id: str
    text: str
    citation: dict[str, Any] = field(default_factory=dict)
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class WorkflowContext:
    """workflow 的输入上下文：**已解析**的片段 + 预算信息。

    host 侧先经 ``source_ref`` 解析（校验 revision/artifact/build 与权限）再组装这里；
    workflow **不**接触存储、不做权限判定、不改绑定到最新 revision。
    """

    items: tuple[ContextItem, ...] = ()
    budget: ContextBudget = field(default_factory=ContextBudget)

    @property
    def text(self) -> str:
        return "\n\n".join(item.text for item in self.items)
