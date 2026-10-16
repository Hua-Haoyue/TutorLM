"""Canonical workspace service interfaces. Persistence stays behind the Host."""
from datetime import datetime
import json
from types import SimpleNamespace

from backend.application.ports.model_provider import ExplanationRequest
from backend.domain.errors import ConflictError, InvalidInputError, NotFoundError
from backend.domain.models import Explanation, Node, Project
from backend.plugin_contracts.learning import LearnerProfile, LearningStateWrite


def epoch(value):
    return float(value) if isinstance(value, (float, int)) else datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()


def node_entity(row):
    return Node(
        id=row['id'], project_id=row['project_id'], document_id=row.get('document_id'),
        title=row['title'], kind=row['kind'], revision=row.get('revision', 1),
        created_at=epoch(row['created_at']), updated_at=epoch(row['updated_at']),
        content=row.get('content'), summary=row.get('summary'), order_index=row.get('order_index', 0),
        parent_id=row.get('parent_id'), metadata=json.loads(row.get('metadata_json') or '{}'),
    )


def explanation_entity(row):
    return Explanation(
        id=row['id'], project_id=row['project_id'], node_id=row['node_id'],
        question=row.get('question'), answer=row['answer'], provider=row['provider'], model=row['model'],
        created_at=epoch(row['created_at']), learning=json.loads(row.get('learning_json') or '{}'),
    )


class ProjectService:
    def __init__(self, app):
        self._app = app

    def get(self, project_id):
        row = self._app.current_project()
        if row['id'] != project_id:
            return None
        return Project(id=row['id'], workspace_id=row['workspace_id'], title=row['title'],
                       description=row.get('description'), created_at=epoch(row['created_at']), updated_at=epoch(row['updated_at']))


class NodeService:
    def __init__(self, app, observe=None):
        self._app = app
        self._observe = observe or (lambda row: None)

    def get(self, node_id):
        row = self._app.store.get_node(node_id)
        if row is None or row['project_id'] != self._app.current_project()['id']:
            return None
        self._observe(row)
        return node_entity(row)

    def list_for_project(self, project_id, document_id=None):
        if project_id != self._app.current_project()['id']:
            raise NotFoundError('Project not found')
        rows = [row for row in self._app.store.list_nodes(project_id)
                if document_id is None or row.get('document_id') == document_id]
        for row in rows:
            self._observe(row)
        return [node_entity(row) for row in rows]


class ExplanationService:
    def __init__(self, app, observe=None, *, allow_generate=True):
        self._app = app
        self._observe = observe
        self._allow_generate = allow_generate

    def explain(self, *, node_id, question=None, persist=False):
        if not self._allow_generate:
            raise InvalidInputError('Plugin has no models.generate capability')
        if persist:
            raise InvalidInputError('Workflow results must be committed by the Host')
        node = NodeService(self._app, self._observe).get(node_id)
        if node is None:
            raise NotFoundError('Node not found')
        source = node.metadata.get('source') or {}
        if source.get('document_id') and source.get('revision_id'):
            resolved = self._app.resolve_ref(source)
            if not resolved.get('resolved'):
                raise InvalidInputError('Node source no longer resolves')
            node.content = resolved['text']
        elif isinstance(source.get('quote'), str):
            node.content = source['quote']
        question = question or '请解释这个知识点。'
        request = ExplanationRequest(node=node, question=question)
        answer = self._app.provider.explain(request)
        if not isinstance(answer, str) or not answer.strip():
            raise InvalidInputError('Provider returned an empty explanation')
        capabilities = self._app.provider.capabilities
        return Explanation.create(project_id=node.project_id, node_id=node.id, question=question,
                                  answer=answer, provider=capabilities.name, model=capabilities.model)

    def list_for_node(self, node_id):
        if NodeService(self._app).get(node_id) is None:
            raise NotFoundError('Node not found')
        return [explanation_entity(row) for row in self._app.store.list_explanations(self._app.current_project()['id'])
                if row['node_id'] == node_id]


class LearningService:
    def __init__(self, app, *, read_only=False):
        self._app = app
        self._read_only = read_only
        # The SDK exposes a synchronization surface, never a raw connection.
        self.store = SimpleNamespace(db=SimpleNamespace(lock=app.store.lock))

    def get_profile(self):
        project_id = self._app.current_project()['id']
        with self._app.store.transaction() as conn:
            row = conn.execute('SELECT profile_json,revision FROM learner_profiles WHERE project_id=?', (project_id,)).fetchone()
        if row is None:
            return {**LearnerProfile().model_dump(), 'revision': 0}
        return {**json.loads(row['profile_json']), 'revision': row['revision']}

    def set_profile(self, profile, expected_revision):
        if self._read_only:
            raise InvalidInputError('Workflow learning data is read-only')
        value = LearnerProfile.model_validate(profile).model_dump()
        project_id = self._app.current_project()['id']
        with self._app.store.transaction() as conn:
            current = self.get_profile()
            if current['revision'] != expected_revision:
                raise ConflictError('Learner profile revision changed')
            revision = expected_revision + 1
            conn.execute('INSERT INTO learner_profiles VALUES(?,?,?) ON CONFLICT(project_id) DO UPDATE '
                         'SET profile_json=excluded.profile_json,revision=excluded.revision',
                         (project_id, json.dumps(value, allow_nan=False), revision))
        return {**value, 'revision': revision}

    def get_state(self, node_id):
        if NodeService(self._app).get(node_id) is None:
            raise NotFoundError('Node not found')
        with self._app.store.transaction() as conn:
            row = conn.execute('SELECT state_json,revision FROM node_learning WHERE node_id=?', (node_id,)).fetchone()
        if row is None:
            return {'node_id': node_id, 'understanding': 'unresolved', 'note': '', 'revision': 0}
        return {'node_id': node_id, **json.loads(row['state_json']), 'revision': row['revision']}

    def set_state(self, node_id, payload):
        if self._read_only:
            raise InvalidInputError('Workflow learning data is read-only')
        value = LearningStateWrite.model_validate(payload)
        with self._app.store.transaction() as conn:
            current = self.get_state(node_id)
            if current['revision'] != value.expected_revision:
                raise ConflictError('Node learning revision changed')
            state = value.model_dump(exclude={'expected_revision'})
            conn.execute('INSERT INTO node_learning VALUES(?,?,?) ON CONFLICT(node_id) DO UPDATE '
                         'SET state_json=excluded.state_json,revision=excluded.revision',
                         (node_id, json.dumps(state), value.expected_revision + 1))
        return self.get_state(node_id)
