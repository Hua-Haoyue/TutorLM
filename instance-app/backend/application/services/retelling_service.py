"""Q15–Q16: user-triggered, dependency-checked return to one parent card."""
from __future__ import annotations

import json

from backend.application.ports.model_provider import RetellRequest
from backend.domain.errors import ConflictError, InvalidInputError, NotFoundError, ProviderError
from backend.domain.models import Explanation
from backend.plugin_contracts import Draft


def snapshot(container, node_id: str) -> dict:
    """Capture every descendant whose state can affect this parent's closure."""
    context, learning = container.context, container.learning
    with learning.store.db.lock:
        parent = context.nodes.get(node_id)
        if parent is None:
            raise NotFoundError('Parent node not found')
        nodes = context.nodes.list_for_project(parent.project_id, document_id=parent.document_id)
        by_parent: dict[str, list] = {}
        for node in nodes:
            if node.parent_id:
                by_parent.setdefault(node.parent_id, []).append(node)
        direct = by_parent.get(node_id, [])
        if not direct:
            raise InvalidInputError('This node has no children to retell')
        if len(direct) > 30:
            raise InvalidInputError('A retelling can include at most 30 direct children')
        visited, descendants = set(), []

        def visit(node, depth):
            if node.id in visited or depth > 100 or len(descendants) >= 300:
                raise InvalidInputError('Invalid or excessive learning tree')
            visited.add(node.id)
            explanation = next(iter(context.explanations.list_for_node(node.id)), None)
            descendants.append({'node': node.to_dict(), 'state': learning.get_state(node.id),
                                'explanation': explanation.to_dict() if explanation else None})
            for child in by_parent.get(node.id, []):
                visit(child, depth + 1)

        visit(parent, 0)
        if len(descendants) < 2:
            raise InvalidInputError('This node has no children to retell')
        return {'profile': learning.get_profile(), 'nodes': descendants,
                'direct_child_ids': [child.id for child in direct]}


def _parse(raw: str, child_ids: list[str]) -> dict:
    value = raw.strip()
    if value.startswith('```'):
        value = value.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
    try:
        data = json.loads(value)
    except (ValueError, TypeError) as exc:
        raise ProviderError('Retelling provider returned invalid JSON') from exc
    if not isinstance(data, dict) or not all(isinstance(data.get(k), str) and data[k].strip()
                                             for k in ('opening', 'closure')):
        raise ProviderError('Retelling requires an opening and a closure')
    bridges = data.get('bridges')
    if not isinstance(bridges, list) or len(bridges) != len(child_ids):
        raise ProviderError('Retelling must bridge every resolved child exactly once')
    if [b.get('node_id') for b in bridges if isinstance(b, dict)] != child_ids or any(
        not isinstance(b, dict) or not isinstance(b.get('connection'), str) or not b['connection'].strip()
        for b in bridges
    ):
        raise ProviderError('Retelling child bridges do not match the requested order')
    if any(len(s) > 3000 for s in [data['opening'], data['closure'], *(b['connection'] for b in bridges)]):
        raise ProviderError('Retelling section is too long')
    answer = '\n\n'.join([data['opening'].strip(), *(b['connection'].strip() for b in bridges),
                          data['closure'].strip()])
    return {'answer': answer, 'bridges': bridges, 'closure': data['closure'].strip()}


def run(context, node_id: str) -> Draft:
    container = context.container
    captured = snapshot(container, node_id)
    if captured['profile']['revision'] == 0:
        raise InvalidInputError('Set the learner profile before retelling')
    entries = {item['node']['id']: item for item in captured['nodes']}
    parent = entries[node_id]
    source = parent['explanation']
    if source is None:
        raise InvalidInputError('Explain the parent before retelling its children')
    ready_ids = [key for key in captured['direct_child_ids'] if entries[key]['explanation'] is not None]
    blocked_ids = [key for key in captured['direct_child_ids'] if key not in ready_ids]
    root = not bool((parent['node'].get('metadata', {}).get('source') or {}).get('fromNode'))
    if not ready_ids:
        return Draft(result={'status': 'blocked', 'parent_node_id': node_id,
                             'blocked_child_ids': blocked_ids, 'answer': None,
                             'explanation': None, 'reached_root': False})
    children = tuple({'node_id': key, 'title': entries[key]['node']['title'],
                      'question': entries[key]['explanation']['question'],
                      'answer': entries[key]['explanation']['answer'],
                      'analogy': ((entries[key]['explanation'].get('learning') or {}).get('structure') or {}).get('analogy')}
                     for key in ready_ids)
    question = source['question']
    if question == '请解释这个知识点。':
        question = parent['node']['title']
    request = RetellRequest(parent_title=parent['node']['title'],
                            parent_question=question, parent_answer=source['answer'],
                            children=children, profile=captured['profile'], partial=bool(blocked_ids))
    generate = getattr(container.context.provider, 'retell', None)
    if not callable(generate):
        raise ProviderError('This model provider does not support retelling')
    raw = generate(request)
    try:
        parsed = _parse(raw, ready_ids)
    except ProviderError:
        # One format retry. The generated text is never committed if still malformed.
        parsed = _parse(generate(request), ready_ids)
    context.guard()

    def validate():
        if snapshot(container, node_id) != captured:
            raise ConflictError('Learning tree changed during retelling; run it again')

    base = {'status': 'partial' if blocked_ids else 'complete', 'parent_node_id': node_id,
            'included_child_ids': ready_ids, 'blocked_child_ids': blocked_ids,
            'answer': parsed['answer'], 'bridges': parsed['bridges'],
            'closure': parsed['closure'], 'reached_root': root and not blocked_ids,
            'next_parent_node_id': None if root or blocked_ids else parent['node']['parent_id']}
    provider = container.context.provider.capabilities
    explanation = Explanation.create(project_id=parent['node']['project_id'], node_id=node_id,
                                     question=source['question'], answer=parsed['answer'],
                                     provider=provider.name, model=provider.model)
    explanation.learning = {'structure': {'treatment': 'mechanism', 'prerequisites': [], 'glosses': [],
                                          'analogy': None, 'profile_revision': captured['profile']['revision'],
                                          'strategy_version': 'retelling.v2'},
                            'profile_snapshot': captured['profile'],
                            'retelling': {'source_explanation_id': source['id'],
                                          'child_explanation_ids': [entries[key]['explanation']['id'] for key in ready_ids],
                                          'skipped_child_ids': blocked_ids,
                                          'bridges': parsed['bridges'], 'closure': parsed['closure']}}
    return Draft(entity=explanation, result={**base, 'explanation': explanation.to_dict()},
                 provider=provider.name, model=provider.model, artifact_type='explanation',
                 precommit=validate)
