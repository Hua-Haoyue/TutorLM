import json
import threading

import pytest

from backend.tests.test_plugin_sdk import node, execute


def edge(src, dst, relation='depends_on'):
    return dict(source_node_id=src, target_node_id=dst, relation=relation, reason='理解源节点需要目标知识')


def model(monkeypatch, client, fn):
    provider = client.app.state.tutorlm.services.provider
    monkeypatch.setattr(provider, 'auto_relations', lambda payload: json.dumps({'relations': fn(payload)}))


def graph(client, headers, project_id):
    return client.get(f'/api/projects/{project_id}/graph', headers=headers).json()


def test_new_then_delete_then_manual_restore(client, session_headers, project_id, text_document, wait_for_run, monkeypatch):
    d = text_document['id']
    a, b = [node(client, session_headers, project_id, d) for _ in range(2)]
    model(monkeypatch, client, lambda p: [edge(b, a)] if p['focus']['id'] in (a, b) else [])
    result = execute(client, session_headers, wait_for_run, 'auto_relations', document_id=d, node_id=b)
    assert result['state'] == 'completed', result
    assert result['result']['added'] == 1
    saved = graph(client, session_headers, project_id)['edges'][0]
    assert saved['source_node_id'] == b and saved['target_node_id'] == a
    assert saved['metadata']['auto_relations'] is True
    client.delete(f'/api/projects/{project_id}/graph/edges/{saved["id"]}', headers=session_headers)
    # Editing and retrying the same new-node run must not restore a deleted edge.
    client.patch(f'/api/nodes/{b}', headers=session_headers, json={'metadata': {'position': {'x': 1, 'y': 2}}})
    result = execute(client, session_headers, wait_for_run, 'auto_relations', document_id=d, node_id=b)
    assert result['result']['added'] == 0
    assert graph(client, session_headers, project_id)['edges'] == []
    c = node(client, session_headers, project_id, d)
    model(monkeypatch, client, lambda p: [])
    execute(client, session_headers, wait_for_run, 'auto_relations', document_id=d, node_id=c)
    assert graph(client, session_headers, project_id)['edges'] == []
    result = execute(client, session_headers, wait_for_run, 'auto_relations', document_id=d)
    assert result['state'] == 'completed', result
    assert result['result'] == {'added': 1, 'restored': 1}
    execute(client, session_headers, wait_for_run, 'auto_relations', document_id=d)
    assert len(graph(client, session_headers, project_id)['edges']) == 1


@pytest.mark.parametrize('kind', ['garbage', 'outside', 'self', 'type', 'direction', 'empty_reason'])
def test_invalid_model_is_atomic(kind, client, session_headers, project_id, text_document, wait_for_run, monkeypatch):
    d = text_document['id']
    a, b = [node(client, session_headers, project_id, d) for _ in range(2)]
    bad = {'outside': [edge(b, 'outside')], 'self': [edge(b, b)],
           'type': [edge(b, a, 'related_to')], 'direction': [edge(b, a), edge(a, b)],
           'empty_reason': [{**edge(b, a), 'reason': ''}]}.get(kind)
    monkeypatch.setattr(client.app.state.tutorlm.services.provider, 'auto_relations',
                        lambda _: 'no JSON' if kind == 'garbage' else json.dumps({'relations': bad}))
    before = graph(client, session_headers, project_id)
    result = execute(client, session_headers, wait_for_run, 'auto_relations', document_id=d, node_id=b)
    assert result['state'] == 'failed', result
    assert graph(client, session_headers, project_id) == before


def test_all_canvas_nodes_batched_and_scoped(client, session_headers, project_id, text_document, wait_for_run, monkeypatch):
    d = text_document['id']
    ids = [node(client, session_headers, project_id, d) for _ in range(27)]
    other = client.post(f'/api/projects/{project_id}/documents', headers=session_headers,
                        json={'title': 'Other', 'text': 'Other text'}).json()['document']['id']
    outsider = node(client, session_headers, project_id, other)
    seen = []
    def propose(p):
        seen.extend(n['id'] for n in p['candidates'])
        return [edge(p['focus']['id'], n['id']) for n in p['candidates']]
    model(monkeypatch, client, propose)
    result = execute(client, session_headers, wait_for_run, 'auto_relations', document_id=d, node_id=ids[-1])
    assert result['state'] == 'completed', result
    assert set(seen) == set(ids[:-1]) and outsider not in seen
    assert result['result']['added'] == 26
    # Auth and a mismatched document must be rejected before a model call.
    assert client.post('/api/runs', json={'kind': 'auto_relations', 'input': {'document_id': d}}).status_code == 401
    assert client.post('/api/runs', headers=session_headers, json={'kind': 'auto_relations',
        'input': {'document_id': d, 'node_id': outsider}}).status_code == 422


@pytest.mark.parametrize('action', ['cancel', 'edit', 'delete'])
def test_inflight_changes_do_not_publish(action, client, session_headers, project_id, text_document, wait_for_run, monkeypatch):
    d = text_document['id']
    a, b = [node(client, session_headers, project_id, d) for _ in range(2)]
    entered, release = threading.Event(), threading.Event()
    def delayed(_):
        entered.set()
        assert release.wait(5)
        return json.dumps({'relations': [edge(b, a)]})
    monkeypatch.setattr(client.app.state.tutorlm.services.provider, 'auto_relations', delayed)
    response = client.post('/api/runs', headers=session_headers, json={'kind': 'auto_relations',
        'input': {'document_id': d, 'node_id': b}})
    run_id = response.json()['id']
    assert entered.wait(5)
    try:
        if action == 'cancel':
            client.post(f'/api/runs/{run_id}/cancel', headers=session_headers)
        elif action == 'edit':
            client.patch(f'/api/nodes/{a}', headers=session_headers, json={'content': 'changed'})
        else:
            client.delete(f'/api/nodes/{a}', headers=session_headers)
    finally:
        release.set()
    result = wait_for_run(run_id)
    assert result['state'] == ('cancelled' if action == 'cancel' else 'failed'), result
    assert graph(client, session_headers, project_id)['edges'] == []


def test_manual_preserves_user_edges_and_checks_all_pairs(client, session_headers, project_id, text_document, wait_for_run, monkeypatch):
    d = text_document['id']
    ids = [node(client, session_headers, project_id, d) for _ in range(3)]
    client.post(f'/api/projects/{project_id}/graph/edges', headers=session_headers,
                json={**{k: v for k, v in edge(ids[0], ids[1], 'explains').items() if k != 'reason'}, 'metadata': {'user': True}})
    seen = set()
    def propose(p):
        seen.update(frozenset((p['focus']['id'], n['id'])) for n in p['candidates'])
        return [edge(p['focus']['id'], n['id'], 'explains') for n in p['candidates']]
    model(monkeypatch, client, propose)
    result = execute(client, session_headers, wait_for_run, 'auto_relations', document_id=d)
    assert result['state'] == 'completed', result
    assert len(seen) == 3
    edges = graph(client, session_headers, project_id)['edges']
    assert len(edges) == 3
    assert sum(e['metadata'].get('user') is True for e in edges) == 1


def test_provider_requests_two_relation_schema():
    from backend.application.ports.provider import GenerateResult, ProviderInfo
    from backend.host.local.provider import OpenAIProvider
    class Transport:
        info = ProviderInfo(name='test', model='offline')
        def generate(self, request):
            assert request.kind == 'auto_relations' and request.json_object
            relation = request.json_schema['properties']['relations']['items']['properties']['relation']
            assert relation['enum'] == ['explains', 'depends_on']
            assert request.max_tokens >= 4096
            assert '不可信数据' in request.system
            return GenerateResult(text='{"relations":[]}', provider='test', model='offline')
    assert OpenAIProvider(Transport()).auto_relations({'focus': {}, 'candidates': []}) == '{"relations":[]}'
