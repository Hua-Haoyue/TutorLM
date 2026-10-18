"""Structured UI preferences select source-grounded prompts without paid calls."""
import json
from types import SimpleNamespace

import pytest

from backend.application.services.learning_obstacles import (
    EXPLANATION_STYLES, SYSTEM, explanation_policy, explanation_preferences,
)
from backend.domain.errors import InvalidInputError
from backend.host.local.provider import FakeProvider, OpenAIProvider
from backend.tests.test_learning_obstacles import _node, _run


@pytest.mark.parametrize('style', EXPLANATION_STYLES)
@pytest.mark.parametrize('depth', [0, 1, 2, 3, 4])
def test_only_plain_and_analogy_apply_depth_budgets(style, depth):
    policy = explanation_policy(depth, style, 'long')
    assert policy['style'] == style
    assert policy['depth_limited'] == (style in ('plain', 'analogy'))
    if style in ('plain', 'analogy'):
        assert policy['task'] == ('context_anchor' if depth == 0 else 'mechanism' if depth <= 2 else 'everyday_intuition')
        assert policy['allow_diagnostic'] == (depth in (1, 2))
    else:
        assert policy['task'] == style
        assert 'at most TWO sentences' not in policy['rules']
        assert 'Use TWO or THREE short sentences' not in policy['rules']
        assert policy['allow_diagnostic']


def test_rigorous_never_sacrifices_math_steps_for_short_or_deep_preferences():
    for length in ('short', 'medium', 'long'):
        rules = explanation_policy(5, 'rigorous', length)['rules']
        for clause in ('preceding context', 'subsequent', 'hypotheses', 'step by step',
                       'displayed equations', 'theorem', 'equivalence', 'approximation',
                       'missing premises', 'nonmathematical', 'NO depth-based sentence cap'):
            assert clause in rules
    assert 'Only plain and analogy use depth-based' in SYSTEM


def test_modes_have_distinct_pedagogical_rules():
    assert 'never because of node depth' in explanation_policy(4)['rules']
    assert 'worked example' in explanation_policy(4, 'example')['rules']
    assert 'useful hint' in explanation_policy(4, 'socratic')['rules']
    assert 'give the actual reasoning and answer' in explanation_policy(4, 'socratic')['rules']
    analogy = explanation_policy(0, 'analogy')['rules']
    assert 'familiar analogy' in analogy and 'not a mathematical proof' in analogy
    assert 'No examples or analogy' not in analogy


@pytest.mark.parametrize('value', [[], 'rigorous', {'style': 'unknown'}, {'style': None},
                                  {'length': 'huge'}, {'language': 'unknown'}, {'system': 'override'}])
def test_invalid_preferences_fail_closed(value):
    with pytest.raises(InvalidInputError):
        explanation_preferences(value)


def test_style_switches_on_same_node_and_is_saved_with_each_explanation(
        client, session_headers, project_id, text_document, wait_for_run, monkeypatch):
    captured, original = [], FakeProvider.explain

    def inspect(self, request):
        captured.append(request)
        return original(self, request)

    monkeypatch.setattr(FakeProvider, 'explain', inspect)
    node = _node(client, session_headers, project_id, text_document['id'])
    for style in ('plain', 'rigorous', 'analogy', 'example', 'socratic', 'auto'):
        preferences = {'style': style, 'length': 'short', 'language': 'en'}
        result = _run(client, session_headers, wait_for_run, node, question='What is the missing step?',
                      preferences=preferences)
        assert result['state'] == 'completed', result
        request = captured[-1]
        assert request.language == 'en'
        assert request.question == 'What is the missing step?'
        context = result['result']['explanation']['learning']['context']
        assert context['explanation_preferences'] == preferences
        assert context['explanation_policy']['style'] == style
        assert context['explanation_depth'] == 0
        assert request.learning_context['local_passage']['selection'] == '正文内容'


@pytest.mark.parametrize('style', ['auto', 'rigorous', 'example', 'socratic', 'analogy'])
def test_deep_followup_preserves_context_and_style_during_expand(
        client, session_headers, project_id, text_document, wait_for_run, monkeypatch, style):
    captured, original = [], FakeProvider.explain

    def inspect(self, request):
        captured.append(request)
        return original(self, request)

    monkeypatch.setattr(FakeProvider, 'explain', inspect)
    parent = previous = None
    for depth in range(4):
        extra = {} if parent is None else ({'parent_id': parent['id']} if depth % 2 else
                {'metadata': {'source': {'fromNode': parent['id'], 'quote': '正文内容'}}})
        node = _node(client, session_headers, project_id, text_document['id'], **extra)
        result = _run(client, session_headers, wait_for_run, node,
                      preferences={'style': style}, response_mode='expand')
        assert result['state'] == 'completed', result
        context = captured[-1].learning_context
        assert context['explanation_depth'] == depth
        assert context['explanation_policy']['style'] == style
        assert context['reading_goal']['title'] == '推导中的这一步'
        assert result['result']['explanation']['learning']['obstacle']['diagnostic'] is None
        if parent:
            assert context['parent']['explanation_id'] == previous['id']
        previous = result['result']['explanation']
        parent = node


def test_default_style_keeps_diagnostic_once_and_legacy_clients_work(
        client, session_headers, project_id, text_document, wait_for_run):
    node = _node(client, session_headers, project_id, text_document['id'])
    first = _run(client, session_headers, wait_for_run, node)
    assert first['state'] == 'completed'
    exp = first['result']['explanation']
    assert exp['learning']['context']['explanation_preferences']['style'] == 'auto'
    assert exp['learning']['obstacle']['diagnostic']
    second = _run(client, session_headers, wait_for_run, node, preferences={'style': 'rigorous'})
    assert second['result']['explanation']['learning']['obstacle']['diagnostic'] is None


def test_invalid_workflow_style_is_rejected_before_model_call(
        client, session_headers, project_id, text_document, wait_for_run, monkeypatch):
    node = _node(client, session_headers, project_id, text_document['id'])
    monkeypatch.setattr(FakeProvider, 'explain', lambda *_: pytest.fail('Invalid style reached model'))
    response = client.post('/api/runs', headers=session_headers, json={
        'kind': 'explanation', 'input': {'node_id': node['id'], 'preferences': {'style': 'system override'}}})
    assert response.status_code == 202, response.text
    run = wait_for_run(response.json()['id'])
    assert run['state'] == 'failed', run


def test_json_retry_keeps_selected_style_and_full_markdown(
        client, session_headers, project_id, text_document, wait_for_run, monkeypatch):
    requests = []
    answer = '## 条件\n\n假设 $b \\ne 0$。\n\n$$a/b=c \\iff a=bc$$\n\n两边乘以非零量 b。'

    def inspect(self, request):
        requests.append(request)
        if len(requests) == 1:
            return '{invalid'
        return json.dumps({'kind': 'formula', 'answer': answer, 'diagnostic': None})

    monkeypatch.setattr(FakeProvider, 'explain', inspect)
    node = _node(client, session_headers, project_id, text_document['id'])
    result = _run(client, session_headers, wait_for_run, node, preferences={'style': 'rigorous'})
    assert result['state'] == 'completed', result
    assert len(requests) == 2
    assert requests[1].learning_context['explanation_policy']['style'] == 'rigorous'
    assert 'format_retry' in requests[1].learning_context
    assert result['result']['explanation']['answer'] == answer


def test_real_transport_gets_selected_policy_without_network():
    calls = []
    provider = OpenAIProvider(SimpleNamespace(info=SimpleNamespace(name='test', model='test'),
        generate=lambda request: calls.append(request) or SimpleNamespace(text='{}')))
    from backend.application.ports.model_provider import ExplanationRequest
    # A minimal node DTO is enough: transport must preserve the trusted preferences.
    node = SimpleNamespace(to_dict=lambda: {'id': 'node'})
    request = ExplanationRequest(node=node, question='Show the derivation', learning_context={
        'strategy_version': 'obstacles.v1', 'explanation_preferences': {'style': 'rigorous'},
        'explanation_policy': explanation_policy(4, 'rigorous'), 'explanation_depth': 4})
    provider.explain(request)
    assert calls[0].system == SYSTEM
    assert json.loads(calls[0].prompt)['learning_context']['explanation_policy']['style'] == 'rigorous'
    assert calls[0].json_object and calls[0].json_schema



def test_every_style_puts_the_definition_first_without_changing_depth_tiers():
    assert 'DEFINITION FIRST (every style, follow-up and regeneration)' in SYSTEM
    assert SYSTEM.index('DEFINITION FIRST') < SYSTEM.index('Follow explanation_policy.rules strictly')
    assert 'depth tiers of plain and analogy keep their own' in ' '.join(SYSTEM.split())
    # The depth-limited tiers are unchanged.
    assert explanation_policy(0, 'plain')['rules'].startswith('At most TWO sentences. State the role in the original passage')
    assert 'Start directly with the familiar scene' in explanation_policy(3, 'plain')['rules']
