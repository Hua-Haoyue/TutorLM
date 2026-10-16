"""按配置装配 provider（**只**用 ``Settings``，不看 ``os.environ``）。"""

from __future__ import annotations

from backend.application.ports.config import AppSettings
from backend.domain.errors import ConfigInvalid
from backend.infrastructure.providers.fake import FakeProvider
from backend.infrastructure.providers.openai_compatible import OpenAICompatibleProvider, Transport
from backend.infrastructure.settings import (
    PROVIDER_FAKE,
    PROVIDER_OPENAI_COMPATIBLE,
    Settings,
)


def build_provider(settings: Settings | AppSettings, *, transport: Transport | None = None):
    """按 ``settings.model`` 返回 provider 实例（密钥来自 settings，不读 env）。"""

    model = getattr(settings, "model", None)
    if model is None:  # pragma: no cover - 只有传错对象时发生
        raise ConfigInvalid("settings 缺少 model 配置")
    if model.provider == PROVIDER_FAKE:
        return FakeProvider(model=model.name or "offline-demo")
    if model.provider == PROVIDER_OPENAI_COMPATIBLE:
        return OpenAICompatibleProvider(
            api_base=model.api_base or "",
            api_key=model.api_key or "",
            model=model.name,
            timeout_seconds=model.timeout_seconds,
            max_tokens=model.max_tokens,
            transport=transport,
        )
    raise ConfigInvalid(f"未知 provider：{model.provider!r}")


__all__ = ["build_provider"]
