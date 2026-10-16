"""Restricted SDK contexts for one workspace and one run; no storage roots or keys."""
import json
from types import SimpleNamespace

from backend.application.services.workspace import ExplanationService, LearningService, NodeService, ProjectService
from backend.domain.errors import InvalidInputError, ConflictError
from backend.plugin_contracts.validation import bounded_json
from backend.host.runs import CancelledRun


class Cancellation:
    def __init__(self, check):
        self._check = check

    @property
    def is_cancelled(self):
        return not self._check()

    def check(self):
        if self.is_cancelled:
            raise CancelledRun()


class PluginHostContext:
    name = 'TutorLM Host'

    def __init__(self, manifest):
        self.manifest = manifest

    def capability(self, name):
        raise KeyError('Capabilities are available only within a workflow')


class ScopedContext:
    def __init__(self, app, run, manifest):
        self._app = app
        self.session_id = run.session_id or ''
        self.run = SimpleNamespace(run_id=run.run_id, session_id=self.session_id, kind=run.kind,
                                   input=dict(run.payload), provider=app.provider.name, model=app.provider.model)
        self.valid = lambda: (not app.runs.is_cancelled(run.run_id)
                              and app.runs.session_valid(self.session_id))
        self.cancel = Cancellation(self.valid)
        self._grants = frozenset(manifest.capabilities)
        self._versions = {}
        projects = ProjectService(app)
        nodes = NodeService(app, self._observe)
        explanations = ExplanationService(app, self._observe, allow_generate='models.generate' in self._grants)
        services = {}
        if 'knowledge.read' in self._grants or 'documents.read' in self._grants:
            services['projects'] = projects
        if self._grants & {'knowledge.read', 'knowledge.nodes.read'}:
            services['nodes'] = nodes
            services['explanations'] = explanations
        if 'models.generate' in self._grants:
            services['provider'] = app.provider
        container = {'context': SimpleNamespace(**services)}
        if 'knowledge.write' in self._grants:
            container['explanations'] = explanations
        if 'knowledge.read' in self._grants:
            container['learning'] = LearningService(app, read_only=True)
        self.container = SimpleNamespace(**container)

    def guard(self):
        self.cancel.check()

    def _observe(self, node):
        self._versions.setdefault(node['id'], (node['project_id'], node['revision']))

    def precommit(self):
        self.guard()
        for node_id, version in self._versions.items():
            current = self._app.store.get_node(node_id)
            if current is None or (current['project_id'], current['revision']) != version:
                raise ConflictError('Plugin source changed during generation')

    def capability(self, name):
        if name not in self._grants:
            raise KeyError('Capability was not granted')
        if name == 'models.generate':
            return self._app.provider
        if name.startswith('documents.'):
            raise KeyError('Document capabilities must use the document service')
        if name.startswith('knowledge.'):
            return self.container.context
        raise KeyError('Capability is unavailable')

    def emit(self, event_type, payload=None):
        self.guard()
        if not isinstance(event_type, str) or not event_type or len(event_type) > 128:
            raise InvalidInputError('Invalid run event type')
        if payload is not None and not isinstance(payload, dict):
            raise InvalidInputError('Run event payload must be an object')
        data = bounded_json({} if payload is None else payload)
        with self._app.store.transaction() as conn:
            seq = conn.execute('SELECT COALESCE(MAX(seq),0)+1 FROM run_events WHERE run_id=?', (self.run.run_id,)).fetchone()[0]
            conn.execute('INSERT INTO run_events VALUES(?,?,?,?)',
                         (self.run.run_id, seq, event_type, json.dumps(data, allow_nan=False)))
