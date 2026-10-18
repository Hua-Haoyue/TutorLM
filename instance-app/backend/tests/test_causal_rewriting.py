"""Incremental local retelling, natural prose and atomic publication."""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from backend.application.ports.model_provider import RetellRequest, CausalExtractionRequest
from backend.application.services.causal_rewriting import (
    parse_payload, retell_schema, retell_system, rewrite_feedback, rewrite_policy,
    validate_rewrite, visible_text,
)
from backend.domain.errors import ProviderError
from backend.host.local.provider import OpenAIProvider
from backend.tests.test_plugin_sdk import execute, node, profile


def output(answer, child_ids=(), terms=()):
    return {'rewritten_explanation': answer,
            'bridges': [{'node_id': key, 'connection': '通过减少变化保留关键差别'} for key in child_ids],
            'causal_payload': {'causal_bridge': '通过减少变化保留关键差别', 'causal_reason': '避免偶然细节',
                               'analogy_core': '', 'terms': list(terms)}}


@pytest.mark.parametrize('depth,tier,target,repair_limit', [
    (0, 'anchor', '40-65', 78), (1, 'mechanism', '78-104', 125),
    (2, 'intuition', '130-195', 234), (4, 'intuition', '130-195', 234),
])
def test_depth_targets_are_soft_and_have_no_minimum(depth, tier, target, repair_limit):
    policy = rewrite_policy(depth)
    assert (policy['tier'], policy['target_chars'], policy['repair_above_chars']) == (tier, target, repair_limit)
    request = RetellRequest('title', 'question', 'old', (), {}, rewrite_policy=policy)
    answer, _ = validate_rewrite(output('通过**减少变化**，保留重点。'), request)
    assert rewrite_feedback(answer, request) == []


@pytest.mark.parametrize('depth,length,needs_repair', [
    (0, 65, False), (0, 66, False), (0, 78, False), (0, 79, True),
    (1, 105, False), (1, 125, False), (1, 126, True),
    (2, 196, False), (2, 234, False), (2, 235, True),
])
def test_only_significantly_overlong_answers_need_compression(depth, length, needs_repair):
    request = RetellRequest('title', 'q', 'old', (), {}, rewrite_policy=rewrite_policy(depth))
    text = '**因**' + '果' * (length - 2) + '。'
    answer, _ = validate_rewrite(output(text), request)
    assert len(visible_text(answer)) == length
    assert any('明显超长' in f for f in rewrite_feedback(answer, request)) == needs_repair


@pytest.mark.parametrize('bad', ['', '**', '【保留差别】（即原词）', '说明(即 原词)', '<br>正文', '```json\n{}\n```', '**未闭合'])
def test_bad_body_formats_are_rejected(bad):
    request = RetellRequest('title', 'q', 'old', (), {}, rewrite_policy=rewrite_policy(0))
    with pytest.raises(ProviderError):
        validate_rewrite(output(bad), request)


def test_natural_terms_without_binding_closure_or_forced_analogy():
    request = RetellRequest('频谱偏置', 'q', 'old', (), {}, rewrite_policy=rewrite_policy(2),
                           required_terms=('频谱偏置', '位置编码'))
    data = output('神经网络**容易学平缓变化，却常漏掉突然变化的细节**，所以需要位置编码。', terms=('频谱偏置', '位置编码'))
    assert validate_rewrite(data, request)[0] == data['rewritten_explanation']
    assert not {'term_bindings', 'closure', 'analogy_phrase'} & retell_schema(request)['properties'].keys()


def test_locality_and_bold_guidance_are_soft():
    request = RetellRequest('title', 'q', '这里说明卡点。作者随后增加位置编码，以便保留输入中的高频细节。',
                           ({'selection': '卡点'},), {}, rewrite_policy=rewrite_policy(0))
    feedback = rewrite_feedback('**网络喜欢平缓变化。**', request)
    assert any('未追问' in f for f in feedback)
    assert any('不标整段' in f for f in feedback)
    answer = '这里说明**网络容易学平缓变化**。作者随后增加位置编码，以便保留输入中的高频细节。'
    assert rewrite_feedback(answer, request) == []


def test_payload_still_rejects_long_story_and_invented_terms():
    payload = output('原词')['causal_payload'] | {'terms': ['原词']}
    assert parse_payload(payload, '原词')['terms'] == ['原词']
    for bad in (payload | {'causal_bridge': '字' * 33}, payload | {'analogy_core': '字' * 11},
                payload | {'terms': ['不存在']}, payload | {'causal_reason': ''}):
        with pytest.raises(ProviderError):
            parse_payload(bad, '原词')


def tree(client, headers, project_id, document_id, wait, depth=0):
    profile(client, headers)
    parent = None
    for _ in range(depth + 1):
        parent = node(client, headers, project_id, document_id, parent)
    child = node(client, headers, project_id, document_id, parent)
    for key in (parent, child):
        assert execute(client, headers, wait, 'explanation', node_id=key)['state'] == 'completed'
    return parent, child


@pytest.mark.parametrize('depth', [0, 1, 2, 3])
def test_compact_targeted_children_and_original_baseline(client, session_headers, project_id, text_document,
                                                       wait_for_run, monkeypatch, depth):
    parent, child = tree(client, session_headers, project_id, text_document['id'], wait_for_run, depth)
    provider = client.app.state.tutorlm.services.provider
    original, requests = provider.retell, []
    def capture(request):
        requests.append(request)
        return original(request)
    monkeypatch.setattr(provider, 'retell', capture)
    run = execute(client, session_headers, wait_for_run, 'retelling', node_id=parent)
    assert run['state'] == 'completed', run
    request = requests[0]
    assert request.explanation_depth == depth
    assert request.parent_original_text == request.parent_answer
    assert request.children[0]['node_id'] == child
    assert request.children[0]['child_term'] and request.children[0]['selection']
    assert 'answer' not in request.children[0]
    saved = run['result']['explanation']['learning']['retelling']
    assert set(saved['child_fingerprints']) == {child}
    assert saved['source_explanation_id'] == saved['original_explanation_id']


def test_incremental_only_changed_direct_children_and_original_toggle(client, session_headers, project_id,
                                                                   text_document, wait_for_run, monkeypatch):
    parent, first = tree(client, session_headers, project_id, text_document['id'], wait_for_run)
    state = client.app.state.tutorlm
    requests = []
    def rewrite(request):
        requests.append(request)
        answer = ('网络**喜欢平缓变化**。位置编码用于补充高频信息。' if len(requests) == 1 else
                  '网络喜欢平缓变化。位置编码**放大输入细节的差别**。')
        return json.dumps(output(answer, [c['node_id'] for c in request.children]))
    monkeypatch.setattr(state.services.provider, 'retell', rewrite)
    first_run = execute(client, session_headers, wait_for_run, 'retelling', node_id=parent)
    assert first_run['state'] == 'completed', first_run
    first_exp = first_run['result']['explanation']
    state.services.update_node(parent, metadata={'retelling_view': {'explanation_id': first_exp['id'], 'original': True}})
    repeated = execute(client, session_headers, wait_for_run, 'retelling', node_id=parent)
    assert repeated['state'] == 'completed', repeated
    assert repeated['result']['status'] == 'complete'
    assert repeated['result']['explanation']['id'] != first_exp['id']
    assert len(requests) == 2
    assert [c['node_id'] for c in requests[-1].children] == [first]
    assert requests[-1].parent_answer == first_exp['answer']
    repeated_exp = repeated['result']['explanation']
    second = node(client, session_headers, project_id, text_document['id'], parent)
    assert execute(client, session_headers, wait_for_run, 'explanation', node_id=second)['state'] == 'completed'
    second_run = execute(client, session_headers, wait_for_run, 'retelling', node_id=parent)
    assert second_run['state'] == 'completed', second_run
    assert [c['node_id'] for c in requests[-1].children] == [second]
    assert requests[-1].parent_answer == repeated_exp['answer']
    assert requests[-1].parent_original_text == requests[0].parent_original_text
    metadata = second_run['result']['explanation']['learning']['retelling']
    assert metadata['base_explanation_id'] == repeated_exp['id']
    assert metadata['original_explanation_id'] == first_exp['learning']['retelling']['original_explanation_id']
    assert set(metadata['child_fingerprints']) == {first, second}
    assert second_run['result']['updated_child_ids'] == [second]
    grandchild = node(client, session_headers, project_id, text_document['id'], first)
    assert execute(client, session_headers, wait_for_run, 'explanation', node_id=grandchild)['state'] == 'completed'
    repeat = execute(client, session_headers, wait_for_run, 'retelling', node_id=parent)
    assert repeat['state'] == 'completed', repeat
    assert {c['node_id'] for c in requests[-1].children} == {first, second}
    assert execute(client, session_headers, wait_for_run, 'explanation', node_id=first)['state'] == 'completed'
    updated = execute(client, session_headers, wait_for_run, 'retelling', node_id=parent)
    assert updated['state'] == 'completed', updated
    assert [c['node_id'] for c in requests[-1].children] == [first]


@pytest.mark.parametrize('failure', ['malformed', 'long', 'bridge'])
def test_one_failed_repair_preserves_answer_and_fingerprints(client, session_headers, project_id, text_document,
                                                           wait_for_run, monkeypatch, failure):
    parent, child = tree(client, session_headers, project_id, text_document['id'], wait_for_run)
    state = client.app.state.tutorlm
    requests = []
    def invalid(request):
        requests.append(request)
        if failure == 'malformed':
            return 'malformed'
        answer = '**因**' + '果' * 100 if failure == 'long' else '通过**减少变化**保留重点。'
        return json.dumps(output(answer, ['wrong'] if failure == 'bridge' else [child]))
    monkeypatch.setattr(state.services.provider, 'retell', invalid)
    before = state.store.list_explanations(project_id)
    run = execute(client, session_headers, wait_for_run, 'retelling', node_id=parent)
    assert run['state'] == 'failed', run
    assert len(requests) == 2
    assert requests[1].previous_response and requests[1].retry_feedback
    assert '当前解释已保留' in run['error']['message']
    assert state.store.list_explanations(project_id) == before


def test_slightly_long_rewrite_saves_without_retry(client, session_headers, project_id, text_document,
                                                wait_for_run, monkeypatch):
    parent, child = tree(client, session_headers, project_id, text_document['id'], wait_for_run)
    requests = []
    def rewrite(request):
        requests.append(request)
        return json.dumps(output('**因**' + '果' * 68 + '。', [child]))
    monkeypatch.setattr(client.app.state.tutorlm.services.provider, 'retell', rewrite)
    run = execute(client, session_headers, wait_for_run, 'retelling', node_id=parent)
    assert run['state'] == 'completed', run
    assert len(requests) == 1
    assert len(visible_text(run['result']['answer'])) == 70


def test_significantly_long_rewrite_gets_one_targeted_compression(client, session_headers, project_id, text_document,
                                                               wait_for_run, monkeypatch):
    parent, child = tree(client, session_headers, project_id, text_document['id'], wait_for_run)
    requests = []
    def rewrite(request):
        requests.append(request)
        return json.dumps(output('**因**' + '果' * 100 if len(requests) == 1 else '通过**减少变化**保留重点。', [child]))
    monkeypatch.setattr(client.app.state.tutorlm.services.provider, 'retell', rewrite)
    run = execute(client, session_headers, wait_for_run, 'retelling', node_id=parent)
    assert run['state'] == 'completed', run
    assert len(requests) == 2
    assert '明显超长' in requests[1].retry_feedback


def test_legacy_payloads_and_legacy_child_ids(client, session_headers, project_id, text_document,
                                            wait_for_run, monkeypatch):
    parent, child = tree(client, session_headers, project_id, text_document['id'], wait_for_run)
    state = client.app.state.tutorlm
    with state.store.transaction() as conn:
        conn.execute('UPDATE explanations SET learning_json=? WHERE node_id IN (?,?)', ('{}', parent, child))
    rewrite_requests, extraction_requests = [], []
    extract = state.services.provider.extract_causal
    def extract_capture(request):
        extraction_requests.append(request)
        return extract(request)
    def rewrite(request):
        rewrite_requests.append(request)
        return json.dumps(output('通过**减少变化**保留重点。', [child]))
    monkeypatch.setattr(state.services.provider, 'extract_causal', extract_capture)
    monkeypatch.setattr(state.services.provider, 'retell', rewrite)
    run = execute(client, session_headers, wait_for_run, 'retelling', node_id=parent)
    assert run['state'] == 'completed', run
    assert [i['node_id'] for i in extraction_requests[0].items] == [parent, child]
    metadata = run['result']['explanation']['learning']
    metadata['retelling'].pop('child_fingerprints')
    with state.store.transaction() as conn:
        conn.execute('UPDATE explanations SET learning_json=? WHERE id=?', (json.dumps(metadata), run['result']['explanation']['id']))
    again = execute(client, session_headers, wait_for_run, 'retelling', node_id=parent)
    assert again['state'] == 'completed', again
    assert again['result']['status'] == 'complete'
    assert again['result']['explanation']['id'] != run['result']['explanation']['id']
    assert len(rewrite_requests) == 2


def test_source_parent_controls_depth_and_next_step(client, session_headers, project_id, text_document, wait_for_run):
    parent, child = tree(client, session_headers, project_id, text_document['id'], wait_for_run, 1)
    state = client.app.state.tutorlm
    grandparent = state.store.get_node(parent)['parent_id']
    with state.store.transaction() as conn:
        conn.execute('UPDATE nodes SET parent_id=NULL WHERE id IN (?,?)', (parent, child))
    state.services.update_node(parent, metadata={'source': {'fromNode': grandparent}})
    state.services.update_node(child, metadata={'source': {'fromNode': parent}})
    run = execute(client, session_headers, wait_for_run, 'retelling', node_id=parent)
    assert run['state'] == 'completed', run
    assert run['result']['next_parent_node_id'] == grandparent
    assert run['result']['explanation']['learning']['retelling']['explanation_depth'] == 1


def test_broken_ancestry_rejected_without_new_answer(client, session_headers, project_id, text_document, wait_for_run):
    parent, child = tree(client, session_headers, project_id, text_document['id'], wait_for_run)
    state = client.app.state.tutorlm
    state.services.update_node(parent, metadata={'source': {'fromNode': child}})
    before = state.store.list_explanations(project_id)
    assert execute(client, session_headers, wait_for_run, 'retelling', node_id=parent)['state'] == 'failed'
    assert state.store.list_explanations(project_id) == before


def test_transport_receives_natural_local_prompt_and_new_schema():
    calls = []
    provider = OpenAIProvider(SimpleNamespace(info=SimpleNamespace(name='test', model='test'),
                                             generate=lambda request: calls.append(request) or SimpleNamespace(text='{}')))
    request = RetellRequest('title', 'q', 'current', (), {}, rewrite_policy=rewrite_policy(0), parent_original_text='original')
    provider.retell(request)
    provider.extract_causal(CausalExtractionRequest(items=()))
    assert json.loads(calls[0].prompt)['parent_original_text'] == 'original'
    assert '最小语句范围' in calls[0].system and '不判断' in calls[0].system
    assert calls[0].json_schema['required'] == ['rewritten_explanation', 'bridges', 'causal_payload']
    assert calls[1].kind == 'causal_extraction'
    repair = replace(request, retry_feedback='明显超长', previous_response=json.dumps(output('字' * 100)))
    assert '唯一一次定向修正' in retell_system(repair)
    assert '不要从长篇父解释重新扩写' in retell_system(repair)


def test_child_changes_during_generation_discard_parent_draft(client, session_headers, project_id, text_document,
                                                            wait_for_run, monkeypatch):
    parent, child = tree(client, session_headers, project_id, text_document['id'], wait_for_run)
    state = client.app.state.tutorlm
    before = [e for e in state.store.list_explanations(project_id) if e['node_id'] == parent]
    def rewrite(request):
        with state.store.transaction() as conn:
            conn.execute('UPDATE explanations SET answer=? WHERE node_id=?', ('子解释已更新。', child))
        return json.dumps(output('通过**减少变化**保留重点。', [child]))
    monkeypatch.setattr(state.services.provider, 'retell', rewrite)
    run = execute(client, session_headers, wait_for_run, 'retelling', node_id=parent)
    assert run['state'] == 'failed' and run['error']['code'] == 'conflict', run
    assert [e for e in state.store.list_explanations(project_id) if e['node_id'] == parent] == before


def test_original_snapshot_survives_pruned_history_and_multiple_updates(client, session_headers, project_id,
                                                                     text_document, wait_for_run, monkeypatch):
    from backend.application.services.workspace import LearningService
    parent, first = tree(client, session_headers, project_id, text_document['id'], wait_for_run)
    state = client.app.state.tutorlm
    requests = []
    def rewrite(request):
        requests.append(request)
        return json.dumps(output('通过**减少变化**保留重点。', [c['node_id'] for c in request.children]))
    monkeypatch.setattr(state.services.provider, 'retell', rewrite)
    monkeypatch.setattr(LearningService, 'get_state', lambda *_: pytest.fail('Retelling must not inspect understanding'))
    initial = execute(client, session_headers, wait_for_run, 'retelling', node_id=parent)
    assert initial['state'] == 'completed', initial
    original_id = initial['result']['explanation']['learning']['retelling']['original_explanation_id']
    with state.store.transaction() as conn:
        conn.execute('DELETE FROM explanations WHERE id=?', (original_id,))
    second = node(client, session_headers, project_id, text_document['id'], parent)
    assert execute(client, session_headers, wait_for_run, 'explanation', node_id=second)['state'] == 'completed'
    with state.store.transaction() as conn:
        conn.execute('UPDATE explanations SET answer=? WHERE node_id=?', ('同一条记录里的解释也可以更新。', first))
    next_run = execute(client, session_headers, wait_for_run, 'retelling', node_id=parent)
    assert next_run['state'] == 'completed', next_run
    assert {c['node_id'] for c in requests[-1].children} == {first, second}
    assert requests[-1].parent_original_text == requests[0].parent_original_text
    assert next_run['result']['explanation']['learning']['retelling']['original_explanation_id'] == original_id


def test_a_long_parent_is_not_forced_below_its_own_length():
    # The reported case: a root card (anchor tier, 40-65 chars) whose answer is ~230 chars
    # with LaTeX. A local rewrite keeps the untouched sentences, so it must not fail at 78.
    from backend.application.services.causal_rewriting import length_limit, visible_length
    parent = (r'特征值是指对方阵 $A$ 和非零向量 $\vec{x}$，若 $A\vec{x} = \lambda\vec{x}$，则标量 $\lambda$ 称为特征值。'
              + '在 PageRank 中它刻画转移矩阵的谱特性，决定平稳分布的存在唯一性，并控制幂迭代的收敛速度。' * 3)
    request = RetellRequest('特征值', 'q', parent, ({'selection': '方阵'},), {}, rewrite_policy=rewrite_policy(0))
    assert length_limit(request) >= visible_length(parent) > 78
    rewrite = parent.replace('方阵', '**行列数相同的方阵**', 1)
    assert not any('明显超长' in f for f in rewrite_feedback(rewrite, request))
    # A formula counts by its rendered size, not its LaTeX source.
    assert visible_length(r'$A\vec{x} = \lambda\vec{x}$') < len(r'A\vec{x}=\lambda\vec{x}')


def test_repaired_draft_keeps_a_valid_body_despite_overlong_metadata(client, session_headers, project_id, text_document,
                                                                    wait_for_run, monkeypatch):
    parent, child = tree(client, session_headers, project_id, text_document['id'], wait_for_run)
    requests = []
    def rewrite(request):
        requests.append(request)
        data = output('通过**减少变化**保留重点。', [child], terms=('减少变化', '编造的术语'))
        data['bridges'][0]['connection'] = '这是一句明显超过三十二个可见字符的连接说明，用来模拟模型常见的超长输出情况'
        data['causal_payload']['causal_reason'] = '原因' * 20
        return json.dumps(data, ensure_ascii=False)
    monkeypatch.setattr(client.app.state.tutorlm.services.provider, 'retell', rewrite)
    run = execute(client, session_headers, wait_for_run, 'retelling', node_id=parent)
    assert run['state'] == 'completed', run
    assert len(requests) == 2 and requests[1].retry_feedback, 'the first draft still gets one repair'
    assert len(visible_text(run['result']['bridges'][0]['connection'])) == 32
    payload = run['result']['explanation']['learning']['causal_payload']
    assert payload['terms'] == ['减少变化'] and len(visible_text(payload['causal_reason'])) == 32


def test_failed_repair_reports_the_reason():
    # The error now carries why validation failed (it used to be dropped).
    from backend.application.services import retelling_service
    import inspect
    assert '原因：' in inspect.getsource(retelling_service.run)


def test_retelling_and_extraction_never_web_search_and_timeouts_are_provider_errors(monkeypatch):
    from backend.application.services.model_options import CURRENT_OPTIONS
    from backend.domain.errors import ProviderUnavailable
    from backend.infrastructure.providers import openai_compatible
    calls = []
    provider = OpenAIProvider(SimpleNamespace(info=SimpleNamespace(name='gemini', model='gemini-2.5-flash'),
                                             generate=lambda request: calls.append(request) or SimpleNamespace(text='{}', payload={})))
    monkeypatch.setattr(provider, 'research', lambda *a, **k: (_ for _ in ()).throw(AssertionError('no web search')))
    token = CURRENT_OPTIONS.set({'effort': 'auto', 'web_search': True})
    try:
        provider.retell(RetellRequest('t', 'q', 'a', (), {}, rewrite_policy=rewrite_policy(0)))
        provider.extract_causal(CausalExtractionRequest(items=()))
    finally:
        CURRENT_OPTIONS.reset(token)
    assert [c.kind for c in calls] == ['retelling', 'causal_extraction'] and not any(c.web_search for c in calls)
    for error in (TimeoutError('read timed out'), ConnectionResetError('reset')):
        def boom(*_a, **_k):
            raise error
        monkeypatch.setattr(openai_compatible.urllib.request, 'urlopen', boom)
        with pytest.raises(ProviderUnavailable):
            openai_compatible.build_transport(timeout=1)(openai_compatible.urllib.request.Request('http://x.invalid'), 1)
