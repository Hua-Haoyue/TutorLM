"""Regression checks for persistent restart and runtime publication boundaries."""
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from backend.adapters.http.app import create_app
from backend.host.binding import _binding_timestamp
from backend.domain.errors import ConfigInvalid
from backend.host.config import HostConfig
from backend.domain.errors import ProviderError
from backend.host.local.provider import FakeProvider, OpenAIProvider
from backend.application.ports.model_provider import NarrationRequest
from backend.application.ports.provider import GenerateResult, ProviderInfo


@pytest.mark.parametrize('value', ['2026-10-17T00:00:00Z', 1792195200.0, '1792195200.0'])
def test_binding_accepts_stored_iso_or_numeric_timestamp(value):
    assert _binding_timestamp(value) == 1792195200.0


@pytest.mark.parametrize('value', ['invalid', 'NaN', float('inf'), -1])
def test_binding_rejects_invalid_timestamps(value):
    with pytest.raises(ConfigInvalid):
        _binding_timestamp(value)


def test_remote_narration_validates_node_identity_and_duration():
    class Provider:
        info = ProviderInfo('openai_compatible', 'test')
        payload = {'segments': [{'node_id': 'node-1', 'text': 'A segment', 'duration_seconds': 3}]}

        def generate(self, request):
            return GenerateResult(json.dumps(self.payload), self.info.name, self.info.model)

    provider = Provider()
    from backend.domain.models import Node
    implementation = OpenAIProvider(provider, max_tokens=100, timeout_seconds=1)
    nodes = [Node(id='node-1', project_id='p', document_id='d', title='Title', kind='concept', revision=1, created_at=0, updated_at=0)]
    assert implementation.narrate(NarrationRequest('Narration', nodes)) == ['A segment']
    provider.payload['segments'][0]['node_id'] = 'foreign-node'
    with pytest.raises(ProviderError):
        implementation.narrate(NarrationRequest('Narration', nodes))


def test_revoked_session_cannot_publish_a_late_model_result(env):
    entered, release = threading.Event(), threading.Event()

    class Provider(FakeProvider):
        def explain(self, request):
            entered.set()
            assert release.wait(5)
            return super().explain(request)

    app = create_app(HostConfig.from_env(env), provider=Provider())
    with TestClient(app) as client:
        token = client.post('/api/sessions', json={'invite_code': 'TLM-DEMO-TEST'}).json()['access_token']
        headers = {'Authorization': f'Bearer {token}'}
        pid = client.get('/api/projects', headers=headers).json()['items'][0]['id']
        doc = client.post(f'/api/projects/{pid}/documents', headers=headers, json={'title': 'Source', 'text': 'Source'}).json()['document']
        node = client.post(f'/api/projects/{pid}/nodes', headers=headers, json={'document_id': doc['id'], 'title': 'Node'}).json()
        run = client.post('/api/runs', headers=headers, json={'kind': 'explanation', 'input': {'node_id': node['id']}}).json()
        try:
            assert entered.wait(3)
            assert client.delete('/api/sessions/me', headers=headers).status_code == 202
        finally:
            release.set()
        state = app.state.tutorlm
        deadline = time.monotonic() + 3
        while state.store.get_run(run['id'])['state'] == 'running' and time.monotonic() < deadline:
            time.sleep(.01)
        assert state.store.get_run(run['id'])['state'] == 'cancelled'
        assert not state.store.list_explanations(pid)


def test_terminal_run_cannot_be_resurrected(client, session_headers):
    store = client.app.state.tutorlm.store
    run, _ = store.create_run(
        kind='extract', workload='extract', project_id=None, input_payload={},
        session_id='test', target_id=None, idempotency_key=None, idempotency_scope='test',
    )
    store.mark_run_state(run['id'], 'cancelled')
    store.mark_run_state(run['id'], 'completed', result_json='{"unexpected":true}')
    assert store.get_run(run['id'])['state'] == 'cancelled'
    assert not store.get_run(run['id'])['result_json']
