import json

import pytest

from backend.application.ports.provider import GenerateRequest
from backend.domain.errors import ProviderUnavailable
from backend.host.config import HostConfig
from backend.host.local.provider import build_host_provider
from backend.infrastructure.providers.gemini import GeminiProvider


def test_native_request_and_text_response():
    def transport(request, timeout):
        assert request.full_url == 'https://example.test/v1beta/models/gemini-3.1-pro:generateContent'
        assert request.get_header('X-goog-api-key') == 'test-key'
        assert 'test-key' not in request.full_url
        body = json.loads(request.data)
        assert body['systemInstruction']['parts'][0]['text'] == 'System'
        assert body['contents'][0]['parts'][0]['text'] == 'Context\n\nQuestion'
        assert body['generationConfig']['maxOutputTokens'] == 123
        assert timeout == 10
        return 200, json.dumps({'candidates': [{'finishReason': 'STOP', 'content': {'parts': [
            {'text': 'private thought', 'thought': True}, {'text': 'Answer'}, {'text': ' text'}]}}]}).encode()
    provider = GeminiProvider(api_base='https://example.test', api_key='test-key', model='gemini-3.1-pro', transport=transport)
    result = provider.generate(GenerateRequest(kind='explanation', prompt='Question', context=['Context'], system='System', max_tokens=123, timeout_seconds=10))
    assert result.text == 'Answer text'
    assert result.provider == 'gemini'


@pytest.mark.parametrize('status,payload', [
    (404, b'{"error":"model not found"}'), (503, b'upstream down'),
    (200, b'<!doctype html><html>Website</html>'), (200, b'[]'),
    (200, b'{"candidates":[]}'),
    (200, b'{"candidates":[{"finishReason":"MAX_TOKENS"}]}'),
    (200, b'{"candidates":[{"content":{"parts":[{"thought":true,"text":"hidden"}]}}]}'),
])
def test_invalid_or_incomplete_output_fails_closed(status, payload):
    provider = GeminiProvider(api_base='https://example.test/v1beta', api_key='test-key', model='gemini-3.1-pro', transport=lambda *args: (status, payload))
    with pytest.raises(ProviderUnavailable) as error:
        provider.generate(GenerateRequest(kind='explanation', prompt='Question'))
    assert 'test-key' not in str(error.value)


def test_host_builds_native_provider(env):
    config = HostConfig.from_env({**env, 'TUTORLM_PROVIDER': 'gemini',
        'TUTORLM_MODEL_API_BASE': 'https://example.test', 'TUTORLM_MODEL_API_KEY': 'test-key',
        'TUTORLM_MODEL_NAME': 'gemini-3.1-pro'})
    assert config.provider.available
    provider = build_host_provider(config.provider)
    assert provider.capabilities.name == 'gemini'
    assert provider.capabilities.model == 'gemini-3.1-pro'


def test_structured_explanation_uses_native_json_mode():
    def transport(request, timeout):
        assert json.loads(request.data)['generationConfig']['responseMimeType'] == 'application/json'
        return 200, json.dumps({'candidates': [{'finishReason': 'STOP', 'content': {'parts': [
            {'text': '{"kind":"formula","answer":"x ≠ 0","diagnostic":null}'}]}}]}).encode()
    provider = GeminiProvider(api_base='https://example.test', api_key='test-key', model='gemini', transport=transport)
    result = provider.generate(GenerateRequest(kind='explanation', prompt='Explain as JSON', json_object=True))
    assert json.loads(result.text)['answer'] == 'x ≠ 0'


def test_numeric_enums_are_not_sent_to_gemini():
    """Gemini rejects non-string enums with HTTP 400; the caller still validates the value."""
    schema = {'type': 'object', 'required': ['kind', 'correct_index'], 'properties': {
        'kind': {'type': 'string', 'enum': ['a', 'b']},
        'correct_index': {'type': 'integer', 'enum': [0, 1]},
        'items': {'type': 'array', 'items': {'type': 'integer', 'enum': [1, 2, 3]}},
    }}
    sent = {}

    def transport(request, timeout):
        sent.update(json.loads(request.data)['generationConfig']['responseSchema'])
        return 200, json.dumps({'candidates': [{'finishReason': 'STOP', 'content': {'parts': [
            {'text': '{"kind":"a","correct_index":1}'}]}}]}).encode()
    provider = GeminiProvider(api_base='https://example.test', api_key='test-key', model='gemini', transport=transport)
    provider.generate(GenerateRequest(kind='feynman', prompt='Quiz', json_object=True, json_schema=schema))
    assert sent['properties']['kind']['enum'] == ['a', 'b']
    assert 'enum' not in sent['properties']['correct_index']
    assert 'enum' not in sent['properties']['items']['items']
    assert schema['properties']['correct_index']['enum'] == [0, 1], 'the caller schema is not modified'
