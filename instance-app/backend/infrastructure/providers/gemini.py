"""Native Gemini generateContent transport; credentials stay in headers."""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

from backend.application.ports import streaming
from backend.application.ports.provider import GenerateRequest, GenerateResult, ProviderInfo
from backend.domain.errors import ProviderUnavailable
from backend.infrastructure.providers.openai_compatible import Transport, build_stream_transport, build_transport


def gemini_schema(schema):
    """Copy of a JSON schema that Gemini's ``responseSchema`` accepts.

    Gemini only supports string ``enum`` values; a numeric enum (for example an
    answer index ``[0, 1]``) makes the whole request fail with HTTP 400. Such
    enums are dropped here. The schema is a generation hint only: callers keep
    validating the returned values themselves, so the constraint is not lost.
    """
    if isinstance(schema, dict):
        return {key: gemini_schema(value) for key, value in schema.items()
                if not (key == 'enum' and not (isinstance(value, list) and all(isinstance(v, str) for v in value)))}
    if isinstance(schema, list):
        return [gemini_schema(item) for item in schema]
    return schema


class GeminiProvider:
    def __init__(self, *, api_base: str, api_key: str, model: str,
                 timeout_seconds: float = 60, max_tokens: int = 2048,
                 transport: Transport | None = None, stream_transport=None):
        if not api_base or not api_key or not model:
            raise ProviderUnavailable('Gemini requires API base, key and model')
        self._base = api_base.rstrip('/')
        self._key = api_key
        self._model = model
        self._timeout = timeout_seconds
        self._max_tokens = max_tokens
        self._transport = transport or build_transport(timeout=timeout_seconds)
        self._stream_transport = stream_transport or build_stream_transport()

    @property
    def info(self):
        return ProviderInfo(name='gemini', model=self._model)

    def generate(self, request: GenerateRequest) -> GenerateResult:
        base = self._base if self._base.endswith(('/v1beta', '/v1')) else self._base + '/v1beta'
        model = urllib.parse.quote(self._model.removeprefix('models/'), safe='')
        prompt = '\n\n'.join([*request.context, request.prompt])
        body = {'contents': [{'role': 'user', 'parts': [{'text': prompt}]}],
                'generationConfig': {'maxOutputTokens': request.max_tokens or self._max_tokens,
                                     'temperature': 0}}
        if request.system:
            body['systemInstruction'] = {'parts': [{'text': request.system}]}
        if request.reasoning_effort != 'auto':
            body['generationConfig']['maxOutputTokens'] = max(body['generationConfig']['maxOutputTokens'],
                {'low': 4096, 'medium': 8192, 'high': 16384}[request.reasoning_effort])
            if self._model.startswith('gemini-3'):
                level = request.reasoning_effort
                if 'pro' in self._model and level == 'medium':
                    level = 'high'
                body['generationConfig']['thinkingConfig'] = {'thinkingLevel': level}
            elif self._model.startswith('gemini-2.5'):
                body['generationConfig']['thinkingConfig'] = {'thinkingBudget': {'low': 1024, 'medium': 4096, 'high': 8192}[request.reasoning_effort]}
            else:
                raise ProviderUnavailable('This Gemini model does not declare configurable thinking')
        if request.web_search:
            body['tools'] = [{'google_search': {}}]
        if request.json_object:
            body['generationConfig']['responseMimeType'] = 'application/json'
            if request.json_schema:
                body['generationConfig']['responseSchema'] = gemini_schema(request.json_schema)
        feed = streaming.sink()
        if feed is not None:
            status, raw = self._stream(base + '/models/' + model + ':streamGenerateContent?alt=sse', body,
                                       request.timeout_seconds or self._timeout, feed)
        else:
            http_request = urllib.request.Request(
                base + '/models/' + model + ':generateContent',
                data=json.dumps(body, ensure_ascii=False).encode('utf-8'), method='POST',
                headers={'x-goog-api-key': self._key, 'Content-Type': 'application/json',
                         'Accept': 'application/json'},
            )
            status, raw = self._transport(http_request, request.timeout_seconds or self._timeout)
        if status != 200:
            reason = {401: 'authentication failed', 403: 'access denied',
                      404: 'model or API endpoint not found', 429: 'rate limit exceeded',
                      503: 'no available upstream capacity'}.get(status, 'upstream request failed')
            raise ProviderUnavailable(f'Gemini HTTP {status}: {reason}', details={'status': status})
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise ProviderUnavailable('Gemini endpoint returned non-JSON data; check the API base URL') from None
        if not isinstance(data, dict):
            raise ProviderUnavailable('Gemini returned an invalid response object')
        candidates = data.get('candidates')
        candidate = candidates[0] if isinstance(candidates, list) and candidates else None
        if not isinstance(candidate, dict):
            raise ProviderUnavailable('Gemini returned no candidates (possibly blocked)')
        if candidate.get('finishReason') not in (None, 'STOP'):
            finish = candidate.get('finishReason')
            safe_finish = finish if finish in {'MAX_TOKENS', 'SAFETY', 'RECITATION', 'OTHER'} else 'unspecified'
            message = '验证题生成达到输出上限，请重试' if request.kind == 'feynman' and finish == 'MAX_TOKENS' else 'Gemini generation did not finish successfully'
            raise ProviderUnavailable(message, details={'finish_reason': safe_finish})
        content = candidate.get('content')
        parts = content.get('parts') if isinstance(content, dict) else None
        if not isinstance(parts, list):
            raise ProviderUnavailable('Gemini returned no text parts')
        text = ''.join(p['text'] for p in parts if isinstance(p, dict)
                       and not p.get('thought') and isinstance(p.get('text'), str))
        if not text.strip():
            raise ProviderUnavailable('Gemini returned no answer text')
        usage = data.get('usageMetadata') or {}
        return GenerateResult(text=text, provider='gemini', model=self._model,
                              payload={'finish_reason': candidate.get('finishReason') if candidate.get('finishReason') in {'STOP', 'MAX_TOKENS', 'SAFETY', 'RECITATION', 'OTHER'} else None,
                                       'sources': [item['web'] for item in (candidate.get('groundingMetadata') or {}).get('groundingChunks', [])
                                                   if isinstance(item, dict) and isinstance(item.get('web'), dict)]},
                              prompt_tokens=usage.get('promptTokenCount'),
                              completion_tokens=usage.get('candidatesTokenCount'))

    def _stream(self, url, body, timeout, feed):
        """streamGenerateContent (SSE): feed the text so far after every chunk, then return
        the chunks merged into one generateContent-shaped response for the usual checks."""
        texts, last = [], {}

        def on_line(line):
            line = line.decode('utf-8', 'replace').strip()
            if not line.startswith('data:'):
                return
            try:
                chunk = json.loads(line[5:])
            except ValueError:
                return
            if not isinstance(chunk, dict):
                return
            last['seen'] = True
            candidates = chunk.get('candidates')
            candidate = candidates[0] if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict) else {}
            parts = (candidate.get('content') or {}).get('parts') if isinstance(candidate.get('content'), dict) else None
            for part in parts if isinstance(parts, list) else []:
                if isinstance(part, dict) and not part.get('thought') and isinstance(part.get('text'), str):
                    texts.append(part['text'])
            for key in ('finishReason', 'groundingMetadata'):
                if candidate.get(key):
                    last[key] = candidate[key]
            if isinstance(chunk.get('usageMetadata'), dict):
                last['usageMetadata'] = chunk['usageMetadata']
            if texts:
                feed(''.join(texts))

        http_request = urllib.request.Request(
            url, data=json.dumps(body, ensure_ascii=False).encode('utf-8'), method='POST',
            headers={'x-goog-api-key': self._key, 'Content-Type': 'application/json', 'Accept': 'text/event-stream'},
        )
        status, error = self._stream_transport(http_request, timeout, on_line)
        if status != 200:
            return status, error
        if not last.get('seen'):
            return 200, b'{}'
        candidate = {'content': {'parts': [{'text': ''.join(texts)}]}, 'finishReason': last.get('finishReason')}
        if last.get('groundingMetadata'):
            candidate['groundingMetadata'] = last['groundingMetadata']
        return 200, json.dumps({'candidates': [candidate], 'usageMetadata': last.get('usageMetadata') or {}}).encode('utf-8')

    def close(self):
        pass
