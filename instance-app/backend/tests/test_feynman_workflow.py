import json
import threading

import pytest


SOURCE = '计算机图形学从虚拟模型出发，通过光栅化得到像素图像。摄影则用摄像头记录真实物体。'


@pytest.fixture
def optimized(client, session_headers, project_id, wait_for_run):
    doc = client.post(f'/api/projects/{project_id}/documents', headers=session_headers,
                     json={'title': '图形学招募', 'text': SOURCE}).json()['document']
    node = client.post(f'/api/projects/{project_id}/nodes', headers=session_headers,
        json={'document_id': doc['id'], 'title': '计算机图形学', 'content': SOURCE,
              'metadata': {'source': {'quote': SOURCE}}}).json()
    response = client.post('/api/runs', headers=session_headers, json={'kind': 'explanation',
        'input': {'node_id': node['id'], 'purpose': 'optimize'}})
    run = wait_for_run(response.json()['id'])
    assert run['state'] == 'completed', run
    exp = run['result']['explanation']
    assert exp['learning']['feynman_eligible'] is True
    return node, exp


def generate(client, headers, wait, node, exp, previous_id=None):
    response = client.post('/api/runs', headers=headers, json={'kind': 'feynman', 'input': {
        'node_id': node['id'], 'explanation_id': exp['id'],
        **({'previous_id': previous_id} if previous_id else {})}})
    assert response.status_code == 202, response.text
    return wait(response.json()['id'])


def current(client, headers, node, exp):
    return client.get(f'/api/nodes/{node["id"]}/feynman', headers=headers,
                      params={'explanation_id': exp['id']})


def answer(client, headers, node, quiz, option):
    return client.post(f'/api/nodes/{node["id"]}/feynman/{quiz["id"]}/answer', headers=headers,
                       json={'option_id': option})


def test_optimize_only_unlocks_and_answers_are_private(client, session_headers, project_id, optimized, wait_for_run):
    node, exp = optimized
    assert current(client, session_headers, node, exp).json() == {'quiz': None}
    runs = client.get('/api/runs', headers=session_headers).json()['items']
    assert all(r['kind'] != 'feynman' for r in runs)
    run = generate(client, session_headers, wait_for_run, node, exp)
    assert run['state'] == 'completed', run
    quiz = run['result']['quiz']
    assert len(quiz['choices']) == 2
    for response in (run, current(client, session_headers, node, exp).json()):
        raw = json.dumps(response)
        assert all(secret not in raw for secret in ('correct_id', 'correct_index', 'correction', 'source_quote', 'flaw'))
    before = len(client.get('/api/runs', headers=session_headers).json()['items'])
    correct = next(o['id'] for o in quiz['choices'] if o['label'].startswith('前提'))
    result = answer(client, session_headers, node, quiz, correct)
    assert result.status_code == 200, result.text
    assert result.json()['quiz']['state'] == 'correct'
    assert 'correction' in result.json()['quiz']
    assert len(client.get('/api/runs', headers=session_headers).json()['items']) == before
    assert current(client, session_headers, node, exp).json()['quiz']['state'] == 'correct'
    # Passing a single relation never changes the node's mastery status.
    saved = client.get(f'/api/nodes/{node["id"]}', headers=session_headers)
    if saved.status_code == 200:
        assert saved.json()['metadata'].get('status') != 'mastered'


@pytest.mark.parametrize('child_parent', [False, True])
def test_retelling_unlocks_only_root(client, session_headers, project_id, optimized, wait_for_run, child_parent):
    root, _ = optimized
    parent = root
    if child_parent:
        parent = client.post(f'/api/projects/{project_id}/nodes', headers=session_headers,
            json={'document_id': root['document_id'], 'title': '子节点', 'content': SOURCE,
                  'parent_id': root['id']}).json()
        run = client.post('/api/runs', headers=session_headers,
            json={'kind': 'explanation', 'input': {'node_id': parent['id']}}).json()
        assert wait_for_run(run['id'])['state'] == 'completed'
    child = client.post(f'/api/projects/{project_id}/nodes', headers=session_headers,
        json={'document_id': root['document_id'], 'title': '光栅化', 'content': SOURCE,
              'parent_id': parent['id']}).json()
    run = client.post('/api/runs', headers=session_headers,
        json={'kind': 'explanation', 'input': {'node_id': child['id']}}).json()
    assert wait_for_run(run['id'])['state'] == 'completed'
    response = client.put('/api/learning/profile', headers=session_headers,
        json={'expected_revision': 0, 'profile': {'education_stage': 'undergraduate'}})
    assert response.status_code == 200, response.text
    run = client.post('/api/runs', headers=session_headers,
        json={'kind': 'retelling', 'input': {'node_id': parent['id']}}).json()
    result = wait_for_run(run['id'])
    assert result['state'] == 'completed', json.dumps(result, ensure_ascii=True)
    exp = result['result']['explanation']
    assert bool(exp['learning'].get('feynman_eligible')) is not child_parent
    response = current(client, session_headers, parent, exp)
    assert response.status_code == (422 if child_parent else 200), response.text
    assert all(run['kind'] != 'feynman' for run in client.get('/api/runs', headers=session_headers).json()['items'])
    if not child_parent:
        result = generate(client, session_headers, wait_for_run, parent, exp)
        assert result['state'] == 'completed', result


def test_wrong_answer_correction_then_distinct_question(client, session_headers, optimized, wait_for_run):
    node, exp = optimized
    quiz = generate(client, session_headers, wait_for_run, node, exp)['result']['quiz']
    wrong = next(o['id'] for o in quiz['choices'] if o['label'].startswith('可以'))
    response = answer(client, session_headers, node, quiz, wrong)
    assert response.json()['quiz']['state'] == 'incorrect'
    assert response.json()['quiz']['correction']
    # A wrong attempt cannot be converted into a pass by clicking the other option.
    other = next(o['id'] for o in quiz['choices'] if o['id'] != wrong)
    assert answer(client, session_headers, node, quiz, other).status_code == 409
    assert answer(client, session_headers, node, quiz, wrong).json() == response.json()
    new = generate(client, session_headers, wait_for_run, node, exp, quiz['id'])
    assert new['state'] == 'completed', new
    assert new['result']['quiz']['id'] != quiz['id']
    assert new['result']['quiz']['statement'] != quiz['statement']
    restored = current(client, session_headers, node, exp).json()
    assert restored['quiz']['id'] == new['result']['quiz']['id']
    assert restored['previous_feedback']['state'] == 'incorrect'


def test_root_and_optimization_enforced(client, session_headers, project_id, optimized, wait_for_run):
    node, exp = optimized
    child = client.post(f'/api/projects/{project_id}/nodes', headers=session_headers,
        json={'document_id': node['document_id'], 'title': '子节点', 'content': SOURCE, 'parent_id': node['id']}).json()
    response = client.post('/api/runs', headers=session_headers, json={'kind': 'explanation',
        'input': {'node_id': child['id'], 'purpose': 'optimize'}})
    child_exp = wait_for_run(response.json()['id'])['result']['explanation']
    assert 'feynman_eligible' not in child_exp['learning']
    assert current(client, session_headers, child, child_exp).status_code == 422
    normal = client.post('/api/runs', headers=session_headers,
        json={'kind': 'explanation', 'input': {'node_id': node['id']}})
    normal_exp = wait_for_run(normal.json()['id'])['result']['explanation']
    assert current(client, session_headers, node, normal_exp).status_code == 422
    assert current(client, session_headers, node, child_exp).status_code == 422
    assert current(client, {}, node, exp).status_code == 401
    assert current(client, session_headers, {'id': 'foreign-node'}, exp).status_code == 404


def test_duplicate_generation_reuses_pending_question(client, session_headers, optimized, wait_for_run, monkeypatch):
    node, exp = optimized
    first = generate(client, session_headers, wait_for_run, node, exp)['result']['quiz']
    monkeypatch.setattr(client.app.state.tutorlm.services.provider, 'feynman', lambda _: pytest.fail('Repeated paid call'))
    second = generate(client, session_headers, wait_for_run, node, exp)['result']['quiz']
    assert first == second


@pytest.mark.parametrize('bad', ['not-json', '{}', 'wrong_quote', 'same_choices', 'no_flaw', 'invalid_index'])
def test_invalid_questions_do_not_publish(bad, client, session_headers, optimized, wait_for_run, monkeypatch):
    node, exp = optimized
    provider = client.app.state.tutorlm.services.provider
    original = provider.feynman
    def malformed(payload):
        if bad in ('not-json', '{}'):
            return bad
        data = json.loads(original(payload))
        if bad == 'wrong_quote': data['source_fragment_id'] = 'missing'
        if bad == 'same_choices': data['choices'] = ['相同选项', '相同选项']
        if bad == 'no_flaw': data['flaw'] = ''
        if bad == 'invalid_index': data['correct_index'] = True
        return json.dumps(data)
    monkeypatch.setattr(provider, 'feynman', malformed)
    result = generate(client, session_headers, wait_for_run, node, exp)
    assert result['state'] == 'failed', result
    assert current(client, session_headers, node, exp).json()['quiz'] is None


@pytest.mark.parametrize('action', ['cancel', 'edit', 'delete'])
def test_generation_revalidates_before_publish(action, client, session_headers, optimized, wait_for_run, monkeypatch):
    node, exp = optimized
    provider = client.app.state.tutorlm.services.provider
    original = provider.feynman
    entered, release = threading.Event(), threading.Event()
    def slow(payload):
        entered.set()
        assert release.wait(5)
        return original(payload)
    monkeypatch.setattr(provider, 'feynman', slow)
    response = client.post('/api/runs', headers=session_headers, json={'kind': 'feynman',
        'input': {'node_id': node['id'], 'explanation_id': exp['id']}})
    run_id = response.json()['id']
    assert entered.wait(5)
    try:
        if action == 'cancel': client.post(f'/api/runs/{run_id}/cancel', headers=session_headers)
        elif action == 'edit': client.patch(f'/api/nodes/{node["id"]}', headers=session_headers, json={'title': '内容变更'})
        else: client.delete(f'/api/nodes/{node["id"]}', headers=session_headers)
    finally:
        release.set()
    result = wait_for_run(run_id)
    assert result['state'] == ('cancelled' if action == 'cancel' else 'failed'), result


def test_answer_rejects_invalid_option_and_stale_content(client, session_headers, optimized, wait_for_run):
    node, exp = optimized
    quiz = generate(client, session_headers, wait_for_run, node, exp)['result']['quiz']
    assert answer(client, session_headers, node, quiz, 'forged').status_code == 422
    client.patch(f'/api/nodes/{node["id"]}', headers=session_headers, json={'content': 'changed'})
    assert answer(client, session_headers, node, quiz, quiz['choices'][0]['id']).status_code == 409


def test_repeated_question_is_rejected_on_retry(client, session_headers, optimized, wait_for_run, monkeypatch):
    node, exp = optimized
    provider = client.app.state.tutorlm.services.provider
    original = provider.feynman
    cached = []
    def repeated(payload):
        if not cached: cached.append(original(payload))
        return cached[0]
    monkeypatch.setattr(provider, 'feynman', repeated)
    quiz = generate(client, session_headers, wait_for_run, node, exp)['result']['quiz']
    wrong = next(c['id'] for c in quiz['choices'] if c['label'].startswith('可以'))
    answer(client, session_headers, node, quiz, wrong)
    run = generate(client, session_headers, wait_for_run, node, exp, quiz['id'])
    assert run['state'] == 'failed'
    assert current(client, session_headers, node, exp).json()['quiz']['state'] == 'incorrect'


def test_remote_provider_uses_grounded_schema_without_a_network_call():
    from backend.host.local.provider import OpenAIProvider
    from backend.application.ports.provider import ProviderInfo, GenerateResult
    class Transport:
        info = ProviderInfo(name='test', model='offline')
        def generate(self, request):
            assert request.kind == 'feynman' and request.json_object
            assert request.json_schema['properties']['choices']['maxItems'] == 2
            assert json.loads(request.prompt)['source'] == SOURCE
            assert '恰好包含一个关键逻辑漏洞' in request.system
            return GenerateResult(text='{}', provider='test', model='offline')
    assert OpenAIProvider(Transport()).feynman({'source': SOURCE}) == '{}'
