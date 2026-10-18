"""Live partial text: Gemini SSE -> streaming sink -> run.partial while running."""
import json
import threading

from backend.application.ports import streaming
from backend.application.ports.provider import GenerateRequest
from backend.infrastructure.providers.gemini import GeminiProvider


def test_json_string_prefix_decodes_a_partial_answer():
    assert streaming.json_string_prefix('{"kind":"concept","answer":"第一行\\n$\\\\alpha$ 与 \\"引号', 'answer') == '第一行\n$\\alpha$ 与 "引号'
    assert streaming.json_string_prefix('{"answer":"ok","diagnostic":null}', 'answer') == 'ok'
    assert streaming.json_string_prefix('{"answer":"a\\', 'answer') == 'a'          # escape split across chunks
    assert streaming.json_string_prefix('{"answer":"\\u4e2', 'answer') == ''          # partial \\u escape
    assert streaming.json_string_prefix('{"answer":"\\u4e2d文', 'answer') == '中文'
    assert streaming.json_string_prefix('{"kind":"con', 'answer') == ''


def sse(*chunks):
    return [b'data: ' + json.dumps(chunk, ensure_ascii=False).encode() + b'\r\n' for chunk in chunks] + [b'\r\n']


def provider(lines, status=200, calls=None):
    def stream_transport(request, timeout, on_line):
        (calls if calls is not None else []).append((request.full_url, timeout))
        for line in lines:
            on_line(line)
        return status, b'' if status == 200 else b'{"error":{}}'
    def transport(request, timeout):
        (calls if calls is not None else []).append((request.full_url, timeout))
        return 200, json.dumps({'candidates': [{'content': {'parts': [{'text': 'whole'}]}, 'finishReason': 'STOP'}]}).encode()
    return GeminiProvider(api_base='https://example.invalid', api_key='k', model='gemini-3-flash-preview',
                          transport=transport, stream_transport=stream_transport)


def test_gemini_streams_text_to_the_run_and_returns_the_merged_answer():
    seen, calls = [], []
    gemini = provider(sse(
        {'candidates': [{'content': {'parts': [{'text': '思考', 'thought': True}]}}]},
        {'candidates': [{'content': {'parts': [{'text': '{"answer":"第一'}]}}]},
        {'candidates': [{'content': {'parts': [{'text': '段"}'}]}, 'finishReason': 'STOP'}],
         'usageMetadata': {'promptTokenCount': 3, 'candidatesTokenCount': 4}},
    ), calls=calls)
    token = streaming.RECORDER.set(seen.append)
    try:
        with streaming.stream(lambda raw: streaming.json_string_prefix(raw, 'answer')):
            result = gemini.generate(GenerateRequest(kind='explanation', prompt='p', timeout_seconds=180))
    finally:
        streaming.RECORDER.reset(token)
    assert result.text == '{"answer":"第一段"}' and result.completion_tokens == 4
    assert seen == ['第一', '第一段']                     # thoughts are never shown
    assert calls == [('https://example.invalid/v1beta/models/gemini-3-flash-preview:streamGenerateContent?alt=sse', 180)]


def test_without_a_run_or_a_streaming_service_gemini_does_not_stream():
    calls = []
    gemini = provider([], calls=calls)
    assert gemini.generate(GenerateRequest(kind='x', prompt='p')).text == 'whole'
    with streaming.stream(lambda raw: raw):                 # no run recorder installed
        assert gemini.generate(GenerateRequest(kind='x', prompt='p')).text == 'whole'
    assert all(':generateContent' in url for url, _ in calls)


def test_stream_errors_and_truncation_still_fail_like_before():
    import pytest
    from backend.domain.errors import ProviderUnavailable
    token = streaming.RECORDER.set(lambda text: None)
    try:
        with streaming.stream(lambda raw: raw):
            with pytest.raises(ProviderUnavailable):
                provider([], status=429).generate(GenerateRequest(kind='x', prompt='p'))
            with pytest.raises(ProviderUnavailable):
                provider(sse({'candidates': [{'content': {'parts': [{'text': 'cut'}]}, 'finishReason': 'MAX_TOKENS'}]})
                         ).generate(GenerateRequest(kind='x', prompt='p'))
    finally:
        streaming.RECORDER.reset(token)


def test_running_explanation_exposes_its_partial_answer(client, session_headers, project_id, text_document, wait_for_run,
                                                       monkeypatch):
    from backend.tests.test_plugin_sdk import node
    state = client.app.state.tutorlm
    target = node(client, session_headers, project_id, text_document['id'])
    entered, release = threading.Event(), threading.Event()
    original = state.services.provider.explain

    def explain(request):
        feed = streaming.sink()
        assert feed is not None
        feed('{"kind":"concept","answer":"正在写的解释')
        entered.set()
        assert release.wait(5)
        return original(request)
    monkeypatch.setattr(state.services.provider, 'explain', explain)
    run = client.post('/api/runs', headers=session_headers, json={'kind': 'explanation', 'input': {'node_id': target}}).json()
    assert entered.wait(5)
    live = client.get('/api/runs/' + run['id'], headers=session_headers).json()
    assert live['state'] == 'running' and live['partial'] == '正在写的解释'
    release.set()
    done = wait_for_run(run['id'])
    assert done['state'] == 'completed' and 'partial' not in done


def grounded_provider(model, fail_native=False):
    from types import SimpleNamespace
    from backend.application.ports.provider import GenerateResult
    from backend.domain.errors import ProviderUnavailable
    from backend.host.local.provider import OpenAIProvider
    calls = []

    def generate(request):
        calls.append(request)
        if request.web_search and fail_native and request.kind != 'web_research':
            raise ProviderUnavailable('Gemini HTTP 400: upstream request failed', details={'status': 400})
        sources = [{'uri': 'https://zh.wikipedia.org/wiki/特征值', 'title': 'wikipedia.org'},
                   {'uri': 'javascript:alert(1)', 'title': 'bad'}] if request.web_search else []
        return GenerateResult(text='{"kind":"concept","answer":"特征值是…","diagnostic":null}', provider='gemini',
                              model=model, payload={'sources': sources})
    name = 'gemini' if model.startswith('gemini') else 'openai_compatible'
    return OpenAIProvider(SimpleNamespace(info=SimpleNamespace(name=name, model=model), generate=generate)), calls


def test_gemini_3_decides_itself_whether_to_search_and_pages_are_collected():
    from backend.application.ports import grounding
    provider, calls = grounded_provider('gemini-3-flash-preview')
    with grounding.collect() as pages:
        provider._generate('explanation', 'p', 's', json_object=True, options={'web_search': True})
    assert [(c.kind, c.web_search) for c in calls] == [('explanation', True)]       # one call, tool offered
    assert pages == [{'uri': 'https://zh.wikipedia.org/wiki/特征值', 'title': 'wikipedia.org'}]
    calls.clear()
    with grounding.collect() as pages:
        provider._generate('explanation', 'p', 's', options={'web_search': False})
    assert [(c.kind, c.web_search) for c in calls] == [('explanation', False)] and pages == []


def test_endpoints_without_native_search_fall_back_to_searching_first():
    from backend.application.ports import grounding
    for provider, calls in (grounded_provider('gemini-3-flash-preview', fail_native=True), grounded_provider('gemini-2.5-flash')):
        with grounding.collect() as pages:
            provider._generate('explanation', 'p', 's', json_object=True, options={'web_search': True})
        assert [c.kind for c in calls][-2:] == ['web_research', 'explanation'] and calls[-1].web_search is False
        assert pages and pages[0]['uri'].startswith('https://zh.wikipedia.org')


def test_explanation_records_web_pages_only_when_the_model_used_them(client, session_headers, project_id, text_document,
                                                                    wait_for_run, monkeypatch):
    from backend.application.ports import grounding
    from backend.tests.test_plugin_sdk import execute, node
    state = client.app.state.tutorlm
    original = state.services.provider.explain
    def explain(request):
        assert request.learning_context.get('web_search_available') is True
        grounding.add([{'uri': 'https://en.wikipedia.org/wiki/Eigenvalue', 'title': 'wikipedia.org'}])
        return original(request)
    monkeypatch.setattr(state.services.provider, 'explain', explain)
    target = node(client, session_headers, project_id, text_document['id'])
    run = execute(client, session_headers, wait_for_run, 'explanation', node_id=target,
                  preferences={'model': {'effort': 'auto', 'web_search': True}})
    assert run['state'] == 'completed', run
    assert run['result']['explanation']['learning']['web_sources'] == [{'uri': 'https://en.wikipedia.org/wiki/Eigenvalue', 'title': 'wikipedia.org'}]
    monkeypatch.setattr(state.services.provider, 'explain', original)
    run = execute(client, session_headers, wait_for_run, 'explanation', node_id=target)
    assert 'web_sources' not in run['result']['explanation']['learning']
