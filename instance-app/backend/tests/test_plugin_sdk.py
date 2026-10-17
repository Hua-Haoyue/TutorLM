"""Exercise the public SDK through the real Host, without model network calls."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from backend.domain.models import Explanation
from backend.domain.errors import InvalidInputError
from backend.host.plugins import build_default_registry
from backend.host.registry import PluginRegistry
from backend.plugin_contracts import Draft, PluginManifest, WorkflowDeclaration
from backend.plugin_contracts.manifest import PluginContractError


def node(client, headers, project_id, document_id, parent_id=None):
    response = client.post(f'/api/projects/{project_id}/nodes', headers=headers, json={
        'document_id': document_id, 'title': 'SDK node', 'kind': 'concept',
        'parent_id': parent_id, 'content': 'Learning material',
    })
    assert response.status_code == 201, response.text
    return response.json()['id']


def execute(client, headers, wait, kind, **payload):
    response = client.post('/api/runs', headers=headers, json={'kind': kind, 'input': payload})
    assert response.status_code == 202, response.text
    return wait(response.json()['id'])


def profile(client, headers, revision=0):
    return client.put('/api/learning/profile', headers=headers, json={
        'expected_revision': revision, 'profile': {'education_stage': 'undergraduate'},
    })


def test_builtin_sdk_discovery(client):
    registry = client.app.state.tutorlm.plugins
    assert registry.order == ('documents', 'explanations', 'knowledge-narration', 'card-clear')
    assert registry.workflow('explanation').instance.__class__.__module__ == 'backend.plugins.explanations'
    assert registry.workflow('retelling').declaration.id == 'retelling.v1'
    public = client.get('/api/plugins').json()
    assert public['count'] == 4
    assert {item['kind'] for item in public['workflows']} == {'explanation', 'narration', 'retelling', 'graph_relations'}
    assert public['host_api_version'] == 1


@pytest.mark.parametrize('change', [
    {'host_api_version': 999}, {'capabilities': ('unknown.capability',)},
])
def test_manifest_rejects_incompatible_contract(change):
    manifest = replace(PluginManifest.build('sample', '1.0.0'), **change)
    with pytest.raises(PluginContractError):
        PluginRegistry().register(manifest)


@pytest.mark.parametrize('disabled', [False, True])
def test_dependency_must_be_available(disabled):
    registry = PluginRegistry()
    if disabled:
        registry.register(PluginManifest.build('dependency', '1.0.0'), enabled=False)
    registry.register(PluginManifest.build('consumer', '1.0.0', depends_on=('dependency',)))
    with pytest.raises(PluginContractError):
        registry.freeze()


def test_workflow_requires_implementation():
    registry = PluginRegistry()
    registry.register(PluginManifest.build('sample', '1.0.0', workflows=[WorkflowDeclaration('test.v1', 'test')]))
    with pytest.raises(PluginContractError):
        registry.freeze()


@pytest.mark.parametrize('result', [{'not': 'a draft'}, Draft(result={'value': float('nan')}), Draft(result={'value': 'wrong'})])
def test_registry_validates_workflow_output(result):
    workflow = SimpleNamespace(id='test.v1', kind='test', run=lambda context, data: result)
    manifest = PluginManifest.build('sample', '1.0.0', workflows=[WorkflowDeclaration(
        'test.v1', 'test', output_schema={'type': 'object', 'required': ['value'],
                                        'properties': {'value': {'type': 'integer'}}},
    )])
    registry = PluginRegistry()
    registry.register(manifest, instance=SimpleNamespace(workflows={'test.v1': workflow}))
    with pytest.raises(InvalidInputError):
        registry.execute('test', SimpleNamespace(valid=lambda: True), {})


def test_learning_routes_revision_auth_and_scope(client, session_headers):
    assert client.get('/api/learning/profile').status_code == 401
    assert client.get('/api/learning/profile', headers=session_headers).json()['revision'] == 0
    assert profile(client, session_headers).json()['revision'] == 1
    assert profile(client, session_headers).status_code == 409
    assert client.get('/api/nodes/missing/learning', headers=session_headers).status_code == 404


def test_retelling_blocked_partial_and_complete(client, session_headers, project_id, text_document, wait_for_run):
    parent = node(client, session_headers, project_id, text_document['id'])
    children = [node(client, session_headers, project_id, text_document['id'], parent) for _ in range(2)]
    assert execute(client, session_headers, wait_for_run, 'retelling', node_id=parent)['state'] == 'failed'
    assert profile(client, session_headers).status_code == 200
    assert execute(client, session_headers, wait_for_run, 'explanation', node_id=parent)['state'] == 'completed'
    blocked = execute(client, session_headers, wait_for_run, 'retelling', node_id=parent)
    assert blocked['state'] == 'completed'
    assert blocked['result']['status'] == 'blocked'
    assert blocked['result']['explanation'] is None
    for index, child in enumerate(children):
        assert execute(client, session_headers, wait_for_run, 'explanation', node_id=child)['state'] == 'completed'
        run = execute(client, session_headers, wait_for_run, 'retelling', node_id=parent)
        assert run['state'] == 'completed', run
        result = run['result']
        assert result['status'] == ('partial' if index == 0 else 'complete')
        assert set(result['included_child_ids']) == set(children[:index + 1])
        learning = result['explanation']['learning']
        assert learning['structure']['profile_revision'] == 1
        assert learning['retelling']['source_explanation_id']
    stored = client.get(f'/api/projects/{project_id}/explanations', headers=session_headers).json()['items']
    assert stored[0]['id'] == result['explanation']['id']
    assert stored[0]['learning'] == learning


def test_publication_failure_rolls_back_entity(client, session_headers, project_id, text_document, wait_for_run, monkeypatch):
    target = node(client, session_headers, project_id, text_document['id'])
    store = client.app.state.tutorlm.store
    original = store.mark_run_state

    def fail_completion(run_id, state, **fields):
        if state == 'completed':
            raise RuntimeError('Simulated completion failure')
        return original(run_id, state, **fields)

    monkeypatch.setattr(store, 'mark_run_state', fail_completion)
    run = execute(client, session_headers, wait_for_run, 'explanation', node_id=target)
    assert run['state'] == 'failed'
    assert store.list_explanations(project_id) == []


@pytest.mark.parametrize('kind,mutation', [('explanation', 'node'), ('retelling', 'profile'), ('retelling', 'tree')])
def test_changed_sources_discard_draft(client, session_headers, project_id, text_document, wait_for_run, monkeypatch, kind, mutation):
    state = client.app.state.tutorlm
    target = node(client, session_headers, project_id, text_document['id'])
    if kind == 'retelling':
        child = node(client, session_headers, project_id, text_document['id'], target)
        profile(client, session_headers)
        for key in (target, child):
            assert execute(client, session_headers, wait_for_run, 'explanation', node_id=key)['state'] == 'completed'
    before = len(state.store.list_explanations(project_id))
    method = 'explain' if kind == 'explanation' else 'retell'
    original = getattr(state.services.provider, method)

    def generate(request):
        result = original(request)
        if mutation == 'node':
            state.services.update_node(target, content='Changed input')
        elif mutation == 'profile':
            from backend.application.services.workspace import LearningService
            LearningService(state.services).set_profile({'education_stage': 'graduate'}, 1)
        else:
            state.services.store.create_node(project_id=project_id, document_id=text_document['id'],
                                             title='New child', kind='concept', parent_id=target,
                                             content=None, summary=None, metadata={})
        return result

    monkeypatch.setattr(state.services.provider, method, generate)
    run = execute(client, session_headers, wait_for_run, kind, node_id=target)
    assert run['state'] == 'failed', run
    assert run['error']['code'] == 'conflict', run
    assert len(state.store.list_explanations(project_id)) == before


def test_publication_requires_write_capability(client, session_headers, project_id, text_document, wait_for_run, monkeypatch):
    target = node(client, session_headers, project_id, text_document['id'])
    state = client.app.state.tutorlm
    entry = state.plugins.get('explanations')
    # Trusted plugins are not sandboxed; even a misbehaving read-only plugin
    # cannot accidentally publish an entity through the Host commit interface.
    entry.manifest = replace(entry.manifest, capabilities=(), workflows=tuple(
        replace(workflow, required_capabilities=()) for workflow in entry.manifest.workflows))
    workflow = entry.instance.workflows['explanation.v1']
    monkeypatch.setattr(workflow, 'run', lambda context, data: Draft(
        entity=Explanation.create(project_id=project_id, node_id=target, question='Q', answer='A', provider='fake', model='test'),
        artifact_type='explanation',
    ))
    run = execute(client, session_headers, wait_for_run, 'explanation', node_id=target)
    assert run['state'] == 'failed'
    assert state.store.list_explanations(project_id) == []


def test_scoped_services_and_event_contract(client, session_headers, project_id, text_document, wait_for_run, monkeypatch):
    from backend.host.context import ScopedContext

    target = node(client, session_headers, project_id, text_document['id'])
    state = client.app.state.tutorlm
    workflow = state.plugins.workflow('explanation').instance
    original = workflow.run

    def run(context, data):
        assert isinstance(context, ScopedContext)
        assert not hasattr(context.container.context, 'store')
        assert not hasattr(context.container.learning.store.db, 'execute')
        assert context.container.context.projects.get('another-project') is None
        assert context.container.context.nodes.get('missing') is None
        with pytest.raises(KeyError):
            context.capability('storage.write')
        with pytest.raises(InvalidInputError):
            context.container.learning.set_profile({}, 0)
        with pytest.raises(InvalidInputError):
            context.emit('progress', {'value': float('nan')})
        with pytest.raises(InvalidInputError):
            context.emit('progress', [])
        context.emit('progress', {'value': 1})
        context.emit('progress', {'value': 2})
        return original(context, data)

    monkeypatch.setattr(workflow, 'run', run)
    result = execute(client, session_headers, wait_for_run, 'explanation', node_id=target)
    assert result['state'] == 'completed', result
    with state.store.transaction() as conn:
        rows = conn.execute('SELECT seq,payload_json FROM run_events WHERE run_id=? ORDER BY seq', (result['id'],)).fetchall()
    assert [row['seq'] for row in rows] == [1, 2]


def test_purge_clears_learning_data(client, session_headers, project_id, text_document):
    target = node(client, session_headers, project_id, text_document['id'])
    assert profile(client, session_headers).status_code == 200
    saved = client.put(f'/api/nodes/{target}/learning', headers=session_headers, json={
        'expected_revision': 0, 'understanding': 'resolved', 'note': 'Known',
    })
    assert saved.status_code == 200
    assert saved.json()['revision'] == 1
    assert client.put(f'/api/nodes/{target}/learning', headers=session_headers, json={
        'expected_revision': 0, 'understanding': 'resolved',
    }).status_code == 409
    assert client.delete(f'/api/projects/{project_id}', headers=session_headers).status_code == 204
    assert client.get('/api/learning/profile', headers=session_headers).json()['revision'] == 0
    with client.app.state.tutorlm.store.transaction() as conn:
        assert conn.execute('SELECT COUNT(*) FROM node_learning').fetchone()[0] == 0


@pytest.mark.parametrize('fail', [False, True])
def test_lifecycle_order_and_authenticated_routes(env, fail):
    from backend.adapters.http.app import create_app
    from backend.host.config import HostConfig
    from fastapi.testclient import TestClient

    calls = []
    registry = build_default_registry()
    # A fresh mutable registry preserves builtin dependencies.
    custom = PluginRegistry()
    for item in registry:
        custom.register(item.manifest, instance=item.instance)

    class Plugin:
        workflows = {}

        def register_services(self, context):
            calls.append('services')

        def register_routes(self, router, context):
            router.add_api_route('/probe', lambda: {'ok': True})

        async def startup(self, context):
            calls.append('start')
            if fail:
                raise RuntimeError('Startup rejected')

        async def shutdown(self, context):
            calls.append('stop')

    custom.register(PluginManifest.build('probe', '1.0.0', depends_on=('documents',)), instance=Plugin())
    app = create_app(HostConfig.from_env(env), plugins=custom)
    if fail:
        with pytest.raises(RuntimeError, match='Startup rejected'), TestClient(app):
            pass
    else:
        with TestClient(app) as client:
            assert calls == ['services', 'start']
            assert client.get('/api/plugins/probe/probe').status_code == 401
            issued = client.post('/api/sessions', json={'invite_code': 'TLM-DEMO-TEST'}).json()
            assert client.get('/api/plugins/probe/probe', headers={'Authorization': 'Bearer ' + issued['access_token']}).json() == {'ok': True}
    assert calls == ['services', 'start', 'stop']
