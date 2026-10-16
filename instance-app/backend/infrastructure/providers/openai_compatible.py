"""``openai_compatible`` provider：OpenAI 风格 ``/chat/completions``。

约束（设计 §7/§10）：

- 密钥**只**来自 ``TUTORLM_MODEL_API_KEY``（经 ``Settings.model`` 注入），
  不落库、不写卷、不入镜像、不复制 ``.env``；
- 不把密钥或 Authorization 头写进日志/异常消息；
- 失败统一抛 ``ProviderUnavailable``（调用方据此把 run 置 ``failed``，
  且因为"模型调用在事务外"，无需回滚业务写入）。

本模块**只**依赖标准库 ``urllib``，不引入额外运行时依赖；测试用**注入的
transport**（见 ``build_transport``）替代真实网络，绝不联网。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping

from backend.application.ports.provider import GenerateRequest, GenerateResult, ProviderInfo
from backend.domain.errors import ProviderUnavailable

#: transport(request) -> (status_code, body_bytes)；可注入以便测试离线运行。
Transport = Callable[[urllib.request.Request, float], "tuple[int, bytes]"]

CHAT_COMPLETIONS_PATH = "/chat/completions"


def build_transport(*, timeout: float) -> Transport:
    """默认 transport：真实 HTTP（生产路径）。

    这里不自己拼 URL 查询串、不重试：重试策略属于 RunManager，
    本层保持"一次调用、如实报错"。
    """

    def transport(request: urllib.request.Request, effective_timeout: float) -> tuple[int, bytes]:
        try:
            with urllib.request.urlopen(request, timeout=effective_timeout) as response:
                return int(response.status), response.read()
        except urllib.error.HTTPError as exc:
            return int(exc.code), exc.read()
        except urllib.error.URLError:
            raise ProviderUnavailable('Model service is unreachable') from None

    return transport


class OpenAICompatibleProvider:
    """OpenAI 兼容的 chat completions 客户端。"""

    __slots__ = ("_api_base", "_api_key", "_model", "_timeout", "_transport", "_max_tokens")

    def __init__(
        self,
        *,
        api_base: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 30.0,
        max_tokens: int = 1024,
        transport: Transport | None = None,
    ) -> None:
        if not api_base or not api_key or not model:
            raise ProviderUnavailable("openai_compatible 需要 api_base、api_key 与 model")
        self._api_base = api_base.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = float(timeout_seconds)
        self._max_tokens = int(max_tokens)
        self._transport = transport or build_transport(timeout=self._timeout)

    @property
    def info(self) -> ProviderInfo:
        return ProviderInfo(name="openai_compatible", model=self._model)

    def generate(self, request: GenerateRequest) -> GenerateResult:
        body = self._build_body(request)
        http_request = urllib.request.Request(
            url=self._endpoint(),
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        timeout = float(request.timeout_seconds or self._timeout)
        status, payload = self._transport(http_request, timeout)
        if status != 200:
            raise ProviderUnavailable(
                f"模型服务返回 HTTP {status}",
                details={"status": status},
            )
        return self._parse(payload, request)

    def close(self) -> None:
        return None

    # ------------------------------------------------------------ 内部
    def _endpoint(self) -> str:
        if self._api_base.endswith(CHAT_COMPLETIONS_PATH):
            return self._api_base
        return f"{self._api_base}{CHAT_COMPLETIONS_PATH}"

    def _headers(self) -> dict[str, str]:
        # 密钥**只**出现在这个头里；异常与返回体都不回显它。
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _build_body(self, request: GenerateRequest) -> dict[str, Any]:
        messages: list[dict[str, str]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        content = request.prompt
        if request.context:
            joined = "\n\n".join(request.context)
            content = f"{joined}\n\n{content}"
        messages.append({"role": "user", "content": content})
        return {
            "model": self._model,
            "messages": messages,
            "max_tokens": int(request.max_tokens or self._max_tokens),
            "temperature": 0,
        }

    def _parse(self, payload: bytes, request: GenerateRequest) -> GenerateResult:
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ProviderUnavailable("模型服务返回的不是合法 JSON") from None
        if not isinstance(decoded, Mapping):
            raise ProviderUnavailable("模型服务返回的不是 JSON 对象")
        choices = decoded.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderUnavailable("模型服务返回缺少 choices")
        first = choices[0]
        message = first.get("message") if isinstance(first, Mapping) else None
        text = ""
        if isinstance(message, Mapping):
            text = str(message.get("content") or "")
        if not text:
            raise ProviderUnavailable("模型服务返回的 message.content 为空")
        usage = decoded.get("usage") if isinstance(decoded.get("usage"), Mapping) else {}
        return GenerateResult(
            text=text,
            provider="openai_compatible",
            model=str(decoded.get("model") or self._model),
            payload={"kind": request.kind, "answer": text, "question": request.prompt},
            prompt_tokens=_as_int(usage.get("prompt_tokens")),
            completion_tokens=_as_int(usage.get("completion_tokens")),
        )


def _as_int(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


__all__ = ["OpenAICompatibleProvider", "Transport", "build_transport"]
