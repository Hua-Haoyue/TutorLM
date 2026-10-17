import io
import json
import stat
import threading
import zipfile

import pytest

from backend.application.ports.provider import GenerateRequest
from backend.domain.errors import InvalidInputError
from backend.infrastructure.providers.gemini import GeminiProvider
from backend.infrastructure.providers.openai_compatible import OpenAICompatibleProvider


def put(client, headers, path, text, revision=None):
    return client.put('/api/code/file', headers=headers, json={'path': path, 'text': text, 'expected_revision': revision})


def start_chat(client, headers):
    return client.post('/api/chat/threads', headers=headers, json={}).json()['id']


def chat(client, headers, wait, thread, message='Read my project', **extra):
    response = client.post('/api/runs', headers=headers, json={'kind': 'workspace_chat',
        'input': {'thread_id': thread, 'message': message, **extra}})
    assert response.status_code == 202, response.text
    return wait(response.json()['id'])


def output(answer='Done', tool=None, arguments=None, changes=None):
    return json.dumps({'answer': answer, 'tool': tool, 'arguments': arguments or {}, 'changes': changes or []})


def test_code_upload_index_search_update_and_revision_conflict(client, session_headers):
    upload = client.post('/api/projects/' + client.get('/api/projects', headers=session_headers).json()['items'][0]['id'] + '/documents/upload',
        headers=session_headers, files={'file': ('helper.py', b'def compute_total(items):\n    return sum(items)\n', 'text/x-python')})
    assert upload.status_code == 201, upload.text
    doc = upload.json()['document']
    assert doc['code_path'] == 'helper.py'
    files = client.get('/api/code/files', headers=session_headers).json()['items']
    assert files[0]['symbols'][0]['name'] == 'compute_total'
    hits = client.get('/api/code/search', params={'query': 'compute_total'}, headers=session_headers).json()['items']
    assert hits[0]['path'] == 'helper.py' and hits[0]['line'] == 1
    assert put(client, session_headers, 'helper.py', 'print(1)').status_code == 409
    response = put(client, session_headers, 'helper.py', 'def new_name():\n    return 42\n', doc['current_revision_id'])
    assert response.status_code == 200, response.text
    assert not client.get('/api/code/search', params={'query': 'compute_total'}, headers=session_headers).json()['items']
    assert client.get('/api/code/search', params={'query': 'new_name'}, headers=session_headers).json()['items']
    assert put(client, session_headers, 'helper.py', '', doc['current_revision_id']).status_code == 409


@pytest.mark.parametrize('path', ['../outside.py', '/etc/file.py', 'C:/secret.py', 'src/../../x.py', '.env',
    'src/.git/config.py', 'credentials/key.py', 'private_key.py', 'a\\b.py', 'main.exe'])
def test_code_paths_never_reach_host_filesystem(client, session_headers, path):
    assert put(client, session_headers, path, 'test').status_code == 422


def archive(entries):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as z:
        for path, value in entries:
            z.writestr(path, value)
    return stream.getvalue()


def test_code_zip_import_indexes_multiple_files_atomically(client, session_headers):
    raw = archive([('src/main.py', 'from helpers import twice\nprint(twice(2))'), ('src/helpers.py', 'def twice(x):\n    return x * 2')])
    response = client.post('/api/code/import', headers=session_headers, files={'file': ('source.zip', raw, 'application/zip')})
    assert response.status_code == 201, response.text
    assert len(response.json()['items']) == 2
    bad = archive([('good.py', 'pass'), ('../bad.py', 'oops')])
    assert client.post('/api/code/import', headers=session_headers, files={'file': ('bad.zip', bad)}).status_code == 422
    paths = [f['path'] for f in client.get('/api/code/files', headers=session_headers).json()['items']]
    assert paths == ['src/helpers.py', 'src/main.py']
    assert client.post('/api/code/import', headers=session_headers, files={'file': ('again.zip', raw)}).status_code == 409
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as z:
        info = zipfile.ZipInfo('link.py'); info.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(info, '/etc/passwd')
    assert client.post('/api/code/import', headers=session_headers, files={'file': ('link.zip', stream.getvalue())}).status_code == 422


def test_code_explanation_search_default_and_pdf_isolation(client, session_headers, project_id, wait_for_run, monkeypatch):
    app = client.app.state.tutorlm.services
    source = put(client, session_headers, 'math.py', 'def double_value(x):\n    return x * 2').json()['document']
    put(client, session_headers, 'main.py', 'from math import double_value\nresult = double_value(3)')
    node = client.post(f'/api/projects/{project_id}/nodes', headers=session_headers, json={
        'title': 'double_value', 'document_id': source['id'], 'content': 'double_value'}).json()
    calls = []; original = app.provider.explain
    monkeypatch.setattr(app.provider, 'explain', lambda request: calls.append(request) or original(request))
    response = client.post('/api/runs', headers=session_headers, json={'kind': 'explanation', 'input': {'node_id': node['id']}})
    assert wait_for_run(response.json()['id'])['state'] == 'completed'
    assert {hit['path'] for hit in calls[-1].learning_context['project_code']} == {'math.py', 'main.py'}
    # The code source defaults on, but an explicit off must remove all retrieval.
    response = client.post('/api/runs', headers=session_headers, json={'kind': 'explanation',
        'input': {'node_id': node['id'], 'preferences': {'code_context': False}}})
    assert wait_for_run(response.json()['id'])['state'] == 'completed'
    assert 'project_code' not in calls[-1].learning_context
    # A real PDF's extracted text does not implicitly enable project retrieval.
    from test_pdf_engine import _text_pdf_bytes
    uploaded = client.post(f'/api/projects/{project_id}/documents/upload', headers=session_headers,
        files={'file': ('reading.pdf', _text_pdf_bytes(['double_value']), 'application/pdf')})
    assert uploaded.status_code == 201, uploaded.text
    ordinary = uploaded.json()['document']
    other = client.post(f'/api/projects/{project_id}/nodes', headers=session_headers,
        json={'title': 'double_value', 'document_id': ordinary['id'], 'content': 'double_value'}).json()
    response = client.post('/api/runs', headers=session_headers, json={'kind':'explanation','input':{'node_id':other['id']}})
    assert wait_for_run(response.json()['id'])['state'] == 'completed'
    assert 'project_code' not in calls[-1].learning_context


def test_chat_runs_are_private_to_app_session(client, session_headers, wait_for_run, monkeypatch):
    app = client.app.state.tutorlm.services
    thread = start_chat(client, session_headers)
    monkeypatch.setattr(app.provider, 'agent_step', lambda *_: output('Private reply'))
    run = chat(client, session_headers, wait_for_run, thread)
    token = client.post('/api/sessions', json={'invite_code':'TLM-DEMO-TEST'}).json()['access_token']
    other = {'Authorization':'Bearer '+token}
    assert client.get('/api/runs/'+run['id'], headers=other).status_code == 404
    assert client.post('/api/runs/'+run['id']+'/cancel', headers=other).status_code == 404
    assert all(item['id'] != run['id'] for item in client.get('/api/runs', headers=other).json()['items'])


def test_approved_batch_rolls_back_all_changes_on_conflict(client, session_headers, wait_for_run, monkeypatch):
    app = client.app.state.tutorlm.services
    thread = start_chat(client, session_headers)
    old = put(client, session_headers, 'existing.py', 'old=1').json()['document']
    monkeypatch.setattr(app.provider, 'agent_step', lambda *_: output('Two edits', changes=[
        {'kind':'file','path':'new.py','text':'new=1'},
        {'kind':'file','path':'existing.py','text':'old=2'}]))
    run = chat(client, session_headers, wait_for_run, thread)
    put(client, session_headers, 'existing.py', 'other=3', old['current_revision_id'])
    proposal = run['result']['message']['proposal_id']
    assert client.post('/api/chat/proposals/'+proposal, headers=session_headers, json={'approve':True}).status_code == 409
    assert [f['path'] for f in app.agent.code.files()] == ['existing.py']
    assert client.get('/api/chat/threads/'+thread, headers=session_headers).json()['proposals'][0]['state'] == 'pending'


def test_chat_document_edits_require_approval(client, session_headers, project_id, wait_for_run, monkeypatch):
    app = client.app.state.tutorlm.services
    doc = app.create_text_document(project_id=project_id, title='Notes', text='Before')[0]
    thread = start_chat(client, session_headers)
    monkeypatch.setattr(app.provider, 'agent_step', lambda *_: output('Edit notes', changes=[
        {'kind':'document','document_id':doc['id'],'text':'After'}]))
    run = chat(client, session_headers, wait_for_run, thread)
    assert app.read_text_content(doc['id']) == 'Before'
    assert client.post('/api/chat/proposals/'+run['result']['message']['proposal_id'], headers=session_headers,
        json={'approve':True}).status_code == 200
    assert app.read_text_content(doc['id']) == 'After'


def test_chat_reads_and_versions_displayed_explanations(client, session_headers, project_id, wait_for_run, monkeypatch):
    app = client.app.state.tutorlm.services
    node = client.post(f'/api/projects/{project_id}/nodes', headers=session_headers,
        json={'title':'Derivation','content':'Original source'}).json()
    old = app.store.add_explanation(project_id=project_id, node_id=node['id'], question='Why?',
        answer='Old answer', provider='fake', model='offline', learning={'context':{'source_status':'resolved'}})
    value = app.agent.tools('read_nodes', {'node_id':node['id']}, {})[0]
    assert value['content'] == 'Original source' and value['explanation']['answer'] == 'Old answer'
    thread = start_chat(client, session_headers)
    monkeypatch.setattr(app.provider, 'agent_step', lambda *_: output('Update answer', changes=[
        {'kind':'explanation','node_id':node['id'],'text':'New derivation'}]))
    run = chat(client, session_headers, wait_for_run, thread)
    assert app.list_explanations(project_id)[0]['id'] == old['id']
    proposal = run['result']['message']['proposal_id']
    assert client.post('/api/chat/proposals/'+proposal, headers=session_headers, json={'approve':True}).status_code == 200
    history = app.list_explanations(project_id)
    assert [item['answer'] for item in history] == ['New derivation', 'Old answer']
    assert history[0]['learning']['edited_from'] == old['id']
    assert app.require_node(node['id'])['content'] == 'Original source'
    # A regeneration between proposal and approval must not be overwritten.
    run = chat(client, session_headers, wait_for_run, thread)
    app.store.add_explanation(project_id=project_id, node_id=node['id'], question=None,
        answer='Concurrent answer', provider='fake', model='offline')
    assert client.post('/api/chat/proposals/'+run['result']['message']['proposal_id'], headers=session_headers,
        json={'approve':True}).status_code == 409


def test_chat_read_tools_denied_execution_and_approval_writes(client, session_headers, wait_for_run, monkeypatch):
    provider = client.app.state.tutorlm.services.provider
    thread = start_chat(client, session_headers)
    put(client, session_headers, 'old.py', 'def helper():\n    return 1')
    responses = iter([output(tool='run_code', arguments={'code': 'raise RuntimeError()'}),
                      output(tool='read_file', arguments={'path': 'old.py'}),
                      output('Proposed an edit', changes=[{'kind':'file','path':'old.py','text':'def helper():\n    return 2'}])])
    seen = []
    def step(payload, options):
        seen.append(json.loads(json.dumps(payload)))
        return next(responses)
    monkeypatch.setattr(provider, 'agent_step', step)
    run = chat(client, session_headers, wait_for_run, thread)
    assert run['state'] == 'completed', run
    assert 'denied' in run['result']['message']['tools'][0]['status']
    assert 'return 1' in seen[-1]['tool_results'][-1]['result']
    assert 'return 1' in client.get('/api/code/file', params={'path':'old.py'}, headers=session_headers).json()['text']
    proposal = run['result']['message']['proposal_id']
    path = '/api/chat/proposals/' + proposal
    assert client.post(path, headers=session_headers, json={'approve':True}).json()['state'] == 'applied'
    assert 'return 2' in client.get('/api/code/file', params={'path':'old.py'}, headers=session_headers).json()['text']
    assert client.post(path, headers=session_headers, json={'approve':True}).json()['state'] == 'applied'


def test_chat_approvals_are_session_scoped_and_conflict_safe(client, session_headers, wait_for_run, monkeypatch, project_id):
    app = client.app.state.tutorlm.services
    thread = start_chat(client, session_headers)
    doc = put(client, session_headers, 'x.py', 'x=1').json()['document']
    monkeypatch.setattr(app.provider, 'agent_step', lambda *_: output('Change file', changes=[{'kind':'file','path':'x.py','text':'x=3'}]))
    run = chat(client, session_headers, wait_for_run, thread)
    proposal = run['result']['message']['proposal_id']
    another = client.post('/api/sessions', json={'invite_code':'TLM-DEMO-TEST'}).json()['access_token']
    other_headers = {'Authorization':'Bearer '+another}
    assert client.get('/api/chat/threads/'+thread, headers=other_headers).status_code == 404
    assert client.post('/api/chat/proposals/'+proposal, headers=other_headers, json={'approve':True}).status_code == 404
    put(client, session_headers, 'x.py', 'x=2', doc['current_revision_id'])
    assert client.post('/api/chat/proposals/'+proposal, headers=session_headers, json={'approve':True}).status_code == 409
    assert client.post('/api/chat/proposals/'+proposal, headers=session_headers, json={'approve':False}).json()['state'] == 'rejected'
    node = client.post(f'/api/projects/{project_id}/nodes', headers=session_headers, json={'title':'Existing','content':'Old'}).json()
    monkeypatch.setattr(app.provider, 'agent_step', lambda *_: output('Edit node', changes=[{'kind':'node','node_id':node['id'],'title':'Updated','content':'New'}]))
    run = chat(client, session_headers, wait_for_run, thread)
    assert app.require_node(node['id'])['title'] == 'Existing'
    assert client.post('/api/chat/proposals/'+run['result']['message']['proposal_id'], headers=session_headers, json={'approve':True}).status_code == 200
    assert app.require_node(node['id'])['title'] == 'Updated'


def test_agent_step_limit_and_cancellation_never_publish(client, session_headers, wait_for_run, monkeypatch):
    app = client.app.state.tutorlm.services
    thread = start_chat(client, session_headers)
    monkeypatch.setattr(app.provider, 'agent_step', lambda *_: output(tool='list_files'))
    run = chat(client, session_headers, wait_for_run, thread)
    assert run['state'] == 'failed'
    assert not client.get('/api/chat/threads/'+thread, headers=session_headers).json()['messages']
    entered, release = threading.Event(), threading.Event()
    def blocked(*_):
        entered.set(); release.wait(5)
        return output('No publication', changes=[{'kind':'file','path':'cancelled.py','text':'x=1'}])
    monkeypatch.setattr(app.provider, 'agent_step', blocked)
    response = client.post('/api/runs', headers=session_headers, json={'kind':'workspace_chat','input':{'thread_id':thread,'message':'test'}})
    assert entered.wait(5)
    client.post('/api/runs/'+response.json()['id']+'/cancel', headers=session_headers)
    release.set()
    assert wait_for_run(response.json()['id'])['state'] == 'cancelled'
    assert not client.get('/api/chat/threads/'+thread, headers=session_headers).json()['proposals']


def test_native_model_controls_are_translated_without_paid_calls():
    calls = []
    def transport(request, timeout):
        calls.append(json.loads(request.data))
        return 200, b'{"candidates":[{"finishReason":"STOP","content":{"parts":[{"text":"answer"}]},"groundingMetadata":{"groundingChunks":[{"web":{"uri":"https://example.test","title":"source"}}]}}]}'
    provider = GeminiProvider(api_base='https://example.test', api_key='test', model='gemini-3-flash-preview', transport=transport)
    result = provider.generate(GenerateRequest(kind='web_research',prompt='question',reasoning_effort='high',web_search=True))
    assert calls[0]['generationConfig']['thinkingConfig'] == {'thinkingLevel':'high'}
    assert calls[0]['tools'] == [{'google_search':{}}]
    assert result.payload['sources'][0]['uri'] == 'https://example.test'
    provider.generate(GenerateRequest(kind='explanation',prompt='question'))
    assert 'tools' not in calls[-1] and 'thinkingConfig' not in calls[-1]['generationConfig']
    compatible = OpenAICompatibleProvider(api_base='https://example.test', api_key='test', model='gpt-5')
    body = compatible._build_body(GenerateRequest(kind='chat',prompt='q',reasoning_effort='medium'))
    assert body['reasoning_effort'] == 'medium' and 'max_completion_tokens' in body and 'temperature' not in body
    assert body['max_completion_tokens'] >= 8192


def test_model_options_are_isolated_and_reset_after_failure():
    from concurrent.futures import ThreadPoolExecutor
    from backend.application.services.model_options import CURRENT_OPTIONS, inference_options
    barrier = threading.Barrier(2)
    def work(effort):
        with inference_options({'model':{'effort':effort}}):
            barrier.wait(timeout=5)
            assert CURRENT_OPTIONS.get()['effort'] == effort
        assert CURRENT_OPTIONS.get() is None
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(work, ['low', 'high']))
    with pytest.raises(RuntimeError), inference_options({'model':{'web_search':True}}):
        raise RuntimeError('failed generation')
    assert CURRENT_OPTIONS.get() is None


def test_tools_require_session_and_search_respects_web_switch(client, session_headers, wait_for_run, monkeypatch):
    assert client.get('/api/code/files').status_code == 401
    app = client.app.state.tutorlm.services
    thread = start_chat(client, session_headers)
    with pytest.raises(InvalidInputError):
        app.agent.tools('web_search', {'query':'test'}, {'web_search':False})
    monkeypatch.setattr(app.provider, 'research', lambda *_: pytest.fail('Unexpected web access'))
    monkeypatch.setattr(app.provider, 'agent_step', lambda *_: output('offline'))
    assert chat(client, session_headers, wait_for_run, thread)['state'] == 'completed'


def test_chat_thread_list_is_titled_ordered_and_session_scoped(client, session_headers, wait_for_run, monkeypatch):
    app = client.app.state.tutorlm.services
    monkeypatch.setattr(app.provider, 'agent_step', lambda *_: output('Reply'))
    first = start_chat(client, session_headers)
    chat(client, session_headers, wait_for_run, first, message='  What does   the allocator do?  ')
    second = start_chat(client, session_headers)
    items = client.get('/api/chat/threads', headers=session_headers).json()['items']
    assert [item['id'] for item in items][:2] == [second, first]
    assert items[1]['title'] == 'What does the allocator do?' and items[1]['messages'] == 2
    assert items[0]['title'] == '' and items[0]['messages'] == 0
    chat(client, session_headers, wait_for_run, first, message='And the store?')
    assert client.get('/api/chat/threads', headers=session_headers).json()['items'][0]['id'] == first
    token = client.post('/api/sessions', json={'invite_code': 'TLM-DEMO-TEST'}).json()['access_token']
    assert client.get('/api/chat/threads', headers={'Authorization': 'Bearer ' + token}).json()['items'] == []


def test_agent_step_accepts_fenced_or_partial_json_but_not_garbage():
    from backend.application.services.workspace_agent import parse_step
    assert parse_step('```json\n{"answer":"Hi","tool":null}\n```') == {'answer': 'Hi', 'tool': None, 'arguments': {}, 'changes': []}
    assert parse_step('{"answer":"","tool":"list_files","arguments":null,"changes":null,"thought":"x"}')['tool'] == 'list_files'
    assert parse_step('{"answer":"ok","tool":"null"}')['tool'] is None
    for bad in ['not json', '{"answer":"","tool":null}', '{"answer":"x","changes":"all"}', 'x' * 100001]:
        with pytest.raises((ValueError, TypeError)):
            parse_step(bad)


def test_chat_retries_once_after_malformed_model_output(client, session_headers, wait_for_run, monkeypatch):
    app = client.app.state.tutorlm.services
    thread = start_chat(client, session_headers)
    seen = []
    def step(payload, _options):
        seen.append(payload.get('retry_feedback'))
        return 'Sure! here is my answer' if len(seen) == 1 else output('Recovered')
    monkeypatch.setattr(app.provider, 'agent_step', step)
    run = chat(client, session_headers, wait_for_run, thread)
    assert run['state'] == 'completed' and seen[0] is None and 'JSON' in seen[1]
    messages = client.get('/api/chat/threads/' + thread, headers=session_headers).json()['messages']
    assert messages[-1]['body']['text'] == 'Recovered'


def test_gemini_chat_sends_schema_and_falls_back_when_rejected():
    from backend.host.local.provider import OpenAIProvider
    from backend.domain.errors import ProviderUnavailable
    from backend.application.ports.provider import GenerateResult
    from backend.infrastructure.providers.gemini import gemini_schema
    from backend.application.services.workspace_agent import AGENT_SCHEMA
    calls = []
    class Transport:
        info = type('I', (), {'name': 'gemini', 'model': 'gemini-2.5-flash'})()
        def generate(self, request):
            calls.append(request.json_schema)
            if request.json_schema:
                raise ProviderUnavailable('Gemini HTTP 400: upstream request failed', details={'status': 400})
            return GenerateResult(text=output('ok'), provider='gemini', model='m', payload={})
    assert json.loads(OpenAIProvider(Transport()).agent_step({'message': 'hi'}, {}))['answer'] == 'ok'
    assert calls == [AGENT_SCHEMA, None]
    assert gemini_schema(AGENT_SCHEMA)['properties']['tool']['enum'][0] == 'list_files'


def test_chat_web_search_sources_are_numbered_safe_links(client, session_headers, wait_for_run, monkeypatch):
    app = client.app.state.tutorlm.services
    thread = start_chat(client, session_headers)
    monkeypatch.setattr(app.provider, 'model_features', lambda: {'effort': False, 'web_search': True, 'offline': False})
    monkeypatch.setattr(app.provider, 'research', lambda query, options: {'text': 'evidence', 'sources': [
        {'uri': 'https://example.org/a', 'title': 'Page A'}, {'uri': 'javascript:alert(1)', 'title': 'bad'},
        {'uri': 'https://example.org/a', 'title': 'dup'}, {'uri': 'http://example.net/b', 'title': ''}]})
    seen = []
    def step(payload, _options):
        seen.append(payload['tool_results'])
        if len(seen) == 1:
            return output('', tool='web_search', arguments={'query': 'topic'})
        return output('See [1] and [2].')
    monkeypatch.setattr(app.provider, 'agent_step', step)
    run = chat(client, session_headers, wait_for_run, thread, model={'effort': 'auto', 'web_search': True})
    assert run['state'] == 'completed', run
    given = json.loads(seen[1][0]['result'])['sources']
    assert [s['n'] for s in given] == [1, 1, 2]
    body = client.get('/api/chat/threads/' + thread, headers=session_headers).json()['messages'][-1]['body']
    assert body['sources'] == [{'uri': 'https://example.org/a', 'title': 'Page A'}, {'uri': 'http://example.net/b', 'title': 'example.net'}]
