import json

import pytest

from backend.application.services.graph_relations import GraphRelationsService
from backend.host.local.provider import OpenAIProvider
from backend.tests.test_plugin_sdk import node, execute


def setup_graph(client, headers, project_id, document_id):
    ids = [node(client, headers, project_id, document_id) for _ in range(3)]
    edge = client.post(f'/api/projects/{project_id}/graph/edges', headers=headers, json={
        'source_node_id': ids[0], 'target_node_id': ids[1], 'relation': 'related_to',
        'metadata': {'user_note': 'keep me'},
    }).json()
    return ids, edge


def suggestion(action, src, dst, relation, edge_id=''):
    return dict(action=action, edge_id=edge_id, source_node_id=src,
                target_node_id=dst, relation=relation, reason='内容说明了前置依赖')


def test_preview_selective_apply_and_stale(client, session_headers, project_id, text_document, wait_for_run, monkeypatch):
    ids, edge = setup_graph(client, session_headers, project_id, text_document['id'])
    provider = client.app.state.tutorlm.services.provider
    proposals = [suggestion('update', ids[1], ids[0], 'depends_on', edge['id']),
                 suggestion('add', ids[1], ids[2], 'supports')]
    monkeypatch.setattr(provider, 'organize_relations', lambda _: json.dumps({'suggestions': proposals}))
    before = client.get(f'/api/projects/{project_id}/graph', headers=session_headers).json()
    run = execute(client, session_headers, wait_for_run, 'graph_relations', document_id=text_document['id'])
    assert run['state'] == 'completed', run
    assert client.get(f'/api/projects/{project_id}/graph', headers=session_headers).json() == before
    path = f'/api/projects/{project_id}/graph/relations/apply'
    # Invalid mixed selection cannot partially commit.
    assert client.post(path, headers=session_headers, json={'run_id': run['id'], 'suggestion_ids': ['s1', 'no']}).status_code == 422
    assert client.get(f'/api/projects/{project_id}/graph', headers=session_headers).json() == before
    assert client.post(path, headers=session_headers, json={'run_id': run['id'], 'suggestion_ids': ['s1']}).status_code == 200
    after = client.get(f'/api/projects/{project_id}/graph', headers=session_headers).json()
    assert len(after['edges']) == len(before['edges'])
    updated = next(e for e in after['edges'] if e['id'] == edge['id'])
    assert updated['relation'] == 'depends_on' and updated['source_node_id'] == ids[1]
    assert updated['metadata'] == {'user_note': 'keep me'}
    assert before['nodes'] == after['nodes']
    assert client.post(path, headers=session_headers, json={'run_id': run['id'], 'suggestion_ids': ['s2']}).status_code == 409
    assert client.post(path, json={'run_id': run['id'], 'suggestion_ids': ['s1']}).status_code == 401


def test_add_remove_and_document_scope(client, session_headers, project_id, text_document, wait_for_run, monkeypatch):
    ids, edge = setup_graph(client, session_headers, project_id, text_document['id'])
    proposals = [suggestion('remove', ids[0], ids[1], 'related_to', edge['id']),
                 suggestion('add', ids[0], ids[2], 'prerequisite_of')]
    monkeypatch.setattr(client.app.state.tutorlm.services.provider, 'organize_relations',
                        lambda _: json.dumps({'suggestions': proposals}))
    run = execute(client, session_headers, wait_for_run, 'graph_relations', document_id=text_document['id'])
    assert run['state'] == 'completed', run
    body = {'run_id': run['id'], 'suggestion_ids': ['s1', 's2']}
    assert client.post(f'/api/projects/other/graph/relations/apply', headers=session_headers, json=body).status_code == 404
    assert client.post(f'/api/projects/{project_id}/graph/relations/apply', headers=session_headers, json=body).status_code == 200
    edges = client.get(f'/api/projects/{project_id}/graph', headers=session_headers).json()['edges']
    assert not any(e['id'] == edge['id'] for e in edges)
    assert any(e['source_node_id'] == ids[0] and e['target_node_id'] == ids[2] for e in edges)


@pytest.mark.parametrize('bad', ['not json', '{"suggestions":[{}]}', '{"suggestions":null}'])
def test_invalid_model_preserves_graph(client, session_headers, project_id, text_document, wait_for_run, monkeypatch, bad):
    setup_graph(client, session_headers, project_id, text_document['id'])
    monkeypatch.setattr(client.app.state.tutorlm.services.provider, 'organize_relations', lambda _: bad)
    before = client.get(f'/api/projects/{project_id}/graph', headers=session_headers).json()
    run = execute(client, session_headers, wait_for_run, 'graph_relations', document_id=text_document['id'])
    assert run['state'] == 'failed'
    assert client.get(f'/api/projects/{project_id}/graph', headers=session_headers).json() == before


def test_reject_invented_nodes_parent_edges_duplicates():
    nodes = [{'id': 'a'}, {'id': 'b'}]
    edge = dict(id='e', source_node_id='a', target_node_id='b', relation='related_to')
    for item in [suggestion('add', 'a', 'missing', 'supports'), suggestion('add', 'a', 'b', 'parent_of'),
                 suggestion('add', 'a', 'b', 'related_to'), suggestion('update', 'a', 'b', 'supports', 'missing')]:
        with pytest.raises(ValueError):
            GraphRelationsService.validate({'suggestions': [item]}, nodes, [edge])


def test_remote_uses_schema_and_directions():
    from types import SimpleNamespace
    captured = []
    transport = SimpleNamespace(info=SimpleNamespace(name='gemini', model='test'),
                                generate=lambda req: captured.append(req) or SimpleNamespace(text='{"suggestions":[]}'))
    assert OpenAIProvider(transport).organize_relations({'nodes': []}) == '{"suggestions":[]}'
    req = captured[0]
    assert req.kind == 'graph_relations' and req.json_object and req.json_schema
    assert 'depends_on=A依赖B' in req.system
