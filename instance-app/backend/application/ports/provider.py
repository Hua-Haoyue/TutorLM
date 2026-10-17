"""模型 provider 端口。

密钥只从 env 读取（``TUTORLM_MODEL_API_KEY``），**不写项目卷、不入库、不入镜像**。
调用方负责"模型调用在事务外"：读输入 -> 关事务 -> 调 provider -> 重开事务再写入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ProviderInfo:
    """``/api/runtime`` 里 ``provider`` 字段的来源。"""

    name: str
    model: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "model": self.model}


@dataclass(frozen=True, slots=True)
class GenerateRequest:
    """一次生成请求；``kind`` 决定输出 schema。"""

    kind: str
    prompt: str
    system: str | None = None
    context: list[str] = field(default_factory=list)
    max_tokens: int | None = None
    timeout_seconds: float | None = None
    json_object: bool = False
    json_schema: dict[str, Any] | None = None
    reasoning_effort: str = 'auto'
    web_search: bool = False


@dataclass(frozen=True, slots=True)
class GenerateResult:
    """provider 输出；``text`` + 结构化 ``payload``。"""

    text: str
    provider: str
    model: str
    payload: dict[str, Any] = field(default_factory=dict)
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


@runtime_checkable
class ModelProvider(Protocol):
    @property
    def info(self) -> ProviderInfo:
        ...

    def generate(self, request: GenerateRequest) -> GenerateResult:
        """同步生成；失败抛 ``ProviderUnavailable``（调用方据此置 run ``failed``）。"""

        ...

    def close(self) -> None:
        ...
