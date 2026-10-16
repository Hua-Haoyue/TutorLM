"""模型 provider 实现：``fake`` 与 ``openai_compatible``（密钥只来自 env）。"""

from __future__ import annotations

from backend.infrastructure.providers.factory import build_provider
from backend.infrastructure.providers.fake import FakeProvider
from backend.infrastructure.providers.openai_compatible import OpenAICompatibleProvider

__all__ = ["FakeProvider", "OpenAICompatibleProvider", "build_provider"]
