"""Disposable local fake provider for the graph layout browser regression."""
import json
import os

from backend.adapters.http.app import create_app
from backend.host.local.provider import FakeProvider


class RelationFixtureProvider(FakeProvider):
    def organize_relations(self, payload):
        edge = next((edge for edge in payload['edges'] if edge['relation'] == 'related_to'), None)
        suggestions = [] if edge is None else [{
            'action': 'update', 'edge_id': edge['id'],
            'source_node_id': edge['source_node_id'], 'target_node_id': edge['target_node_id'],
            'relation': 'supports', 'reason': 'Offline fixture: explicit directed support relation',
        }]
        return json.dumps({'suggestions': suggestions})


def build_app():
    if os.environ.get('TUTORLM_PROVIDER') != 'fake' or os.environ.get('TUTORLM_RUN_MODE') != 'demo':
        raise RuntimeError('Use a disposable loopback demo instance with the fake provider')
    return create_app(provider=RelationFixtureProvider())
