"""Reading actions on a card during a generation must not discard the result;
changing what the generation read still must."""
import json

import pytest

from backend.tests.test_causal_rewriting import output, tree
from backend.tests.test_plugin_sdk import execute, node


def touch(state, node_id, **fields):
    current = state.store.get_node(node_id)
    metadata = json.loads(current['metadata_json'] or '{}')
    changes = {}
    if 'metadata' in fields:
        changes['metadata_json'] = json.dumps({**metadata, **fields.pop('metadata')})
    state.store.update_node(node_id, expected_revision=None, **changes, **fields)


PRESENTATION = [
    {'metadata': {'position': {'x': 900, 'y': 40}, 'layout_scale': 1.6}},   # drag / enlarge
    {'metadata': {'pages': ['x'], 'page': 0}},                              # page through versions
    {'metadata': {'marks': [{'e': 'x', 'q': 'word', 's': 'hl'}]}},          # highlight a word
    {'metadata': {'status': 'mastered', 'retelling_view': {'original': True}}},
]


@pytest.mark.parametrize('change', PRESENTATION)
def test_explanation_survives_presentation_changes_on_its_node_and_parent(
        client, session_headers, project_id, text_document, wait_for_run, monkeypatch, change):
    state = client.app.state.tutorlm
    parent = node(client, session_headers, project_id, text_document['id'])
    assert execute(client, session_headers, wait_for_run, 'explanation', node_id=parent)['state'] == 'completed'
    child = node(client, session_headers, project_id, text_document['id'], parent)
    provider, original = state.services.provider, state.services.provider.explain

    def explain(request):
        # The user keeps reading while the child generates.
        for key in (parent, child):
            touch(state, key, **json.loads(json.dumps(change)))
        return original(request)
    monkeypatch.setattr(provider, 'explain', explain)
    run = execute(client, session_headers, wait_for_run, 'explanation', node_id=child)
    assert run['state'] == 'completed', run


@pytest.mark.parametrize('change', [{'title': 'Renamed'}, {'content': 'Other material'},
                                    {'metadata': {'source': {'quote': 'different'}}}])
def test_explanation_still_conflicts_when_its_input_changes(
        client, session_headers, project_id, text_document, wait_for_run, monkeypatch, change):
    state = client.app.state.tutorlm
    target = node(client, session_headers, project_id, text_document['id'])
    provider, original = state.services.provider, state.services.provider.explain

    def explain(request):
        touch(state, target, **json.loads(json.dumps(change)))
        return original(request)
    monkeypatch.setattr(provider, 'explain', explain)
    run = execute(client, session_headers, wait_for_run, 'explanation', node_id=target)
    assert run['state'] == 'failed' and run['error']['code'] == 'conflict', run


def test_retelling_survives_moving_cards_of_the_document(client, session_headers, project_id, text_document,
                                                        wait_for_run, monkeypatch):
    parent, child = tree(client, session_headers, project_id, text_document['id'], wait_for_run)
    other = node(client, session_headers, project_id, text_document['id'])
    state = client.app.state.tutorlm

    def rewrite(request):
        for key in (parent, child, other):
            touch(state, key, metadata={'position': {'x': 10, 'y': 20}, 'layout_routes': []})
        return json.dumps(output('通过**减少变化**保留重点。', [child]))
    monkeypatch.setattr(state.services.provider, 'retell', rewrite)
    run = execute(client, session_headers, wait_for_run, 'retelling', node_id=parent)
    assert run['state'] == 'completed', run
