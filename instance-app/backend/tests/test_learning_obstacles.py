import json

import pytest

from backend.application.services.learning_obstacles import local_slice, parse_answer
from backend.domain.errors import ProviderError


def test_slice_does_not_guess_repeated_selection():
    text = 'first: x = y\n\nsecond: x = y implies z'
    assert local_slice(text, 'x = y') is None
    assert local_slice(text, 'x = y', prefix='second: ')['after'] == ' implies z'
    assert local_slice(text, 'x = y', start=7)['before'] == 'first: '


@pytest.mark.parametrize('kind', ['concept', 'formula', 'reasoning', 'chart'])
def test_complete_markdown_preserved_and_expand_has_no_diagnostic(kind):
    answer = '## 先了解用途\n\n1. 参数不是零。\n\n摘要：这是正文的结论。\n\n$$a/b=c$$'
    raw = json.dumps({'kind': kind, 'answer': answer, 'diagnostic': {
        'question': '哪一步不清楚？', 'options': [
            {'label': '为什么能除', 'question': '解释除法的条件'},
            {'label': '为何相等', 'question': '解释等号两边的关系'}]}})
    parsed, metadata = parse_answer(raw, allow_diagnostic=False)
    assert parsed == answer
    assert metadata['diagnostic'] is None


@pytest.mark.parametrize('raw', ['not json', '{}', '{"kind":"formula","answer":""}',
    '{"kind":"chart","answer":"ok","diagnostic":{"question":"?","options":[]}}'])
def test_malformed_output_rejected(raw):
    with pytest.raises(ProviderError):
        parse_answer(raw, allow_diagnostic=True)


def _node(client, headers, project_id, document_id, **extra):
    response = client.post(f'/api/projects/{project_id}/nodes', headers=headers, json={
        'document_id': document_id, 'title': '推导中的这一步', 'content': '正文内容', **extra})
    assert response.status_code == 201, response.text
    return response.json()


def _run(client, headers, wait, node, **extra):
    response = client.post('/api/runs', headers=headers, json={
        'kind': 'explanation', 'input': {'node_id': node['id'], **extra}})
    assert response.status_code == 202, response.text
    return wait(response.json()['id'])


def test_diagnostic_once_and_parent_reference(client, session_headers, project_id, text_document, wait_for_run):
    node = _node(client, session_headers, project_id, text_document['id'])
    first = _run(client, session_headers, wait_for_run, node, question='没看懂')
    assert first['state'] == 'completed', first
    exp = first['result']['explanation']
    assert len(exp['learning']['obstacle']['diagnostic']['options']) == 2
    child = _node(client, session_headers, project_id, text_document['id'], parent_id=node['id'],
                  metadata={'source': {'fromNode': node['id'], 'fromExp': exp['id'], 'quote': '正文内容'}})
    result = _run(client, session_headers, wait_for_run, child)
    assert result['state'] == 'completed', result
    assert result['result']['explanation']['learning']['context']['parent_explanation_id'] == exp['id']
    second = _run(client, session_headers, wait_for_run, node, question='没看懂')
    assert second['state'] == 'completed', second
    assert second['result']['explanation']['learning']['obstacle']['diagnostic'] is None
    history = client.get(f'/api/projects/{project_id}/explanations', headers=session_headers).json()['items']
    assert len([e for e in history if e['node_id'] == node['id']]) == 2


def test_malformed_generation_keeps_previous_success(client, session_headers, project_id, text_document, wait_for_run, monkeypatch):
    from backend.host.local.provider import FakeProvider
    node = _node(client, session_headers, project_id, text_document['id'])
    first = _run(client, session_headers, wait_for_run, node)['result']['explanation']
    monkeypatch.setattr(FakeProvider, 'explain', lambda self, request: '{broken')
    failed = _run(client, session_headers, wait_for_run, node, response_mode='expand')
    assert failed['state'] == 'failed'
    history = client.get(f'/api/projects/{project_id}/explanations', headers=session_headers).json()['items']
    assert [e['id'] for e in history] == [first['id']]


def test_context_is_bounded_and_rejects_foreign_parent_explanation(client, session_headers, project_id, text_document, wait_for_run, monkeypatch):
    from backend.host.local.provider import FakeProvider
    captured, original = [], FakeProvider.explain
    def inspect(self, request):
        captured.append(request.learning_context)
        return original(self, request)
    monkeypatch.setattr(FakeProvider, 'explain', inspect)
    node = _node(client, session_headers, project_id, text_document['id'], metadata={'source': {'quote': '正文内容'}})
    assert _run(client, session_headers, wait_for_run, node)['state'] == 'completed'
    assert captured[0]['local_passage']['selection'] == '正文内容'
    assert captured[0]['document_title'] == '示例文档'
    child = _node(client, session_headers, project_id, text_document['id'], parent_id=node['id'],
                  metadata={'source': {'fromNode': node['id'], 'fromExp': 'missing', 'quote': '正文内容'}})
    assert _run(client, session_headers, wait_for_run, child)['state'] == 'failed'
    assert len(captured) == 1
