"""Q15–Q16: user-triggered, dependency-checked return to one parent card."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace

from backend.application.ports.model_provider import CausalExtractionRequest, RetellRequest
from backend.application.services.causal_rewriting import (
    VERSION, parse_json, parse_payload, rewrite_feedback, rewrite_policy, validate_rewrite, visible_text,
)
from backend.domain.errors import ConflictError, InvalidInputError, NotFoundError, ProviderError
from backend.domain.models import Explanation
from backend.plugin_contracts import Draft


def _parent_id(node):
    return ((node.metadata or {}).get('source') or {}).get('fromNode') or node.parent_id


def snapshot(container, node_id: str) -> dict:
    """Capture the current parent, its direct children and the original baseline."""
    context, learning = container.context, container.learning
    with learning.store.db.lock:
        parent = context.nodes.get(node_id)
        if parent is None:
            raise NotFoundError('Parent node not found')
        nodes = context.nodes.list_for_project(parent.project_id, document_id=parent.document_id)
        by_id = {node.id: node for node in nodes}
        ancestors, seen = [], {node_id}
        current = parent
        while _parent_id(current):
            key = _parent_id(current)
            if key in seen or key not in by_id or len(ancestors) >= 100:
                raise InvalidInputError('Invalid learning ancestry')
            seen.add(key)
            current = by_id[key]
            ancestors.append(current.to_dict())
        by_parent: dict[str, list] = {}
        for node in nodes:
            if _parent_id(node):
                by_parent.setdefault(_parent_id(node), []).append(node)
        direct = by_parent.get(node_id, [])
        if not direct:
            raise InvalidInputError('This node has no children to retell')
        if len(direct) > 30:
            raise InvalidInputError('A retelling can include at most 30 direct children')
        history = [e.to_dict() for e in context.explanations.list_for_node(node_id)]
        entries = [{'node': parent.to_dict(), 'explanation': history[0] if history else None}]
        for child in direct:
            explanation = next(iter(context.explanations.list_for_node(child.id)), None)
            entries.append({'node': child.to_dict(),
                            'explanation': explanation.to_dict() if explanation else None})
        original = history[0] if history else None
        by_explanation = {e['id']: e for e in history}
        visited = set()
        while original and original['id'] not in visited:
            visited.add(original['id'])
            retelling = (original.get('learning') or {}).get('retelling') or {}
            source_id = retelling.get('original_explanation_id') or retelling.get('source_explanation_id')
            if not source_id:
                break
            previous = by_explanation.get(source_id) or retelling.get('original_explanation')
            if not previous:
                break
            original = previous
        return {'profile': learning.get_profile(), 'nodes': entries, 'original': original,
                'ancestors': ancestors, 'depth': len(ancestors),
                'next_parent_node_id': _parent_id(parent),
                'direct_child_ids': [child.id for child in direct]}


def _parse(raw: str, child_ids: list[str], request: RetellRequest) -> dict:
    data = parse_json(raw)
    answer, payload = validate_rewrite(data, request)
    bridges = data.get('bridges')
    if not isinstance(bridges, list) or len(bridges) != len(child_ids):
        raise ProviderError('Retelling must bridge every resolved child exactly once')
    if [b.get('node_id') for b in bridges if isinstance(b, dict)] != child_ids or any(
        not isinstance(b, dict) or not isinstance(b.get('connection'), str) or not b['connection'].strip()
        for b in bridges
    ):
        raise ProviderError('Retelling child bridges do not match the requested order')
    if any(len(visible_text(b['connection'])) > 32 for b in bridges):
        raise ProviderError('Each causal bridge must be at most 32 characters')
    return {'answer': answer, 'bridges': bridges, 'causal_payload': payload}


def _fingerprint(entry):
    explanation, node = entry['explanation'], entry['node']
    source = ((node.get('metadata') or {}).get('source') or {})
    value = [node['id'], node['title'], node.get('content'), source.get('quote'),
             explanation['id'], explanation['answer'], explanation.get('question'),
             (explanation.get('learning') or {}).get('causal_payload')]
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _source_text(entry):
    return '\n'.join(str(value or '') for value in (
        entry['node']['title'], entry['node'].get('content'), entry['explanation']['answer']))


def _payloads(provider, entries, keys, guard):
    payloads, missing = {}, []
    for key in keys:
        entry = entries[key]
        try:
            payloads[key] = parse_payload(
                (entry['explanation'].get('learning') or {}).get('causal_payload'), _source_text(entry))
        except ProviderError:
            missing.append({'node_id': key, 'source_text': _source_text(entry)})
    if not missing:
        return payloads
    extract = getattr(provider, 'extract_causal', None)
    if not callable(extract):
        raise ProviderError('This provider cannot compress legacy explanations')
    request = CausalExtractionRequest(items=tuple(missing))
    for attempt in range(2):
        guard()
        try:
            data = parse_json(extract(request))
            items = data.get('items')
            if not isinstance(items, list) or [item.get('node_id') if isinstance(item, dict) else None
                                             for item in items] != [item['node_id'] for item in missing]:
                raise ProviderError('Causal extraction must match source nodes in order')
            for item, original in zip(items, missing):
                payloads[item['node_id']] = parse_payload(item.get('causal_payload'), original['source_text'])
            return payloads
        except ProviderError as exc:
            if attempt:
                raise
            request = replace(request, retry_feedback=str(exc))
    raise ProviderError('Causal extraction failed')


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
    root = captured['next_parent_node_id'] is None
    if not ready_ids:
        return Draft(result={'status': 'blocked', 'parent_node_id': node_id,
                             'blocked_child_ids': blocked_ids, 'answer': None,
                             'explanation': None, 'reached_root': False})
    previous = (source.get('learning') or {}).get('retelling') or {}
    integrated = dict(previous.get('child_fingerprints') or {})
    # Older retellings recorded IDs, not fingerprints. Do not re-integrate their unchanged children.
    if 'child_fingerprints' not in previous:
        legacy_ids = set(previous.get('child_explanation_ids') or [])
        integrated.update({key: _fingerprint(entries[key]) for key in ready_ids
                           if entries[key]['explanation']['id'] in legacy_ids})
    updated_ids = [key for key in ready_ids if integrated.get(key) != _fingerprint(entries[key])]
    # Every explicit click generates; unchanged inputs reuse the ready direct children.
    if not updated_ids:
        updated_ids = ready_ids
    payloads = _payloads(container.context.provider, entries, [node_id, *updated_ids], context.guard)
    children = tuple({'node_id': key, 'child_term': entries[key]['node']['title'],
                      'selection': ((entries[key]['node'].get('metadata') or {}).get('source') or {}).get('quote')
                                   or entries[key]['node'].get('content') or entries[key]['node']['title'],
                      'question': entries[key]['explanation'].get('question'),
                      'causal_payload': payloads[key]} for key in updated_ids)
    required_terms = tuple(payloads[node_id]['terms'])
    question = source['question']
    if question == '请解释这个知识点。':
        question = parent['node']['title']
    request = RetellRequest(parent_title=parent['node']['title'],
                            parent_question=question, parent_answer=source['answer'],
                            children=children, profile=captured['profile'], partial=bool(blocked_ids),
                            explanation_depth=captured['depth'], rewrite_policy=rewrite_policy(captured['depth']),
                            required_terms=required_terms,
                            parent_original_text=captured['original']['answer'],
                            source_context={'node': parent['node'], 'ancestors': captured['ancestors'],
                                            'parent_causal_payload': payloads[node_id]})
    generate = getattr(container.context.provider, 'retell', None)
    if not callable(generate):
        raise ProviderError('This model provider does not support retelling')
    # Generate toward the requested style, then allow only one targeted repair.
    warnings = []
    for attempt in range(2):
        context.guard()
        raw = generate(request)
        try:
            parsed = _parse(raw, updated_ids, request)
            warnings = rewrite_feedback(parsed['answer'], request)
            if warnings and attempt == 0:
                request = replace(request, retry_feedback='; '.join(warnings), previous_response=raw)
                continue
            if len(visible_text(parsed['answer'])) > request.rewrite_policy['repair_above_chars']:
                raise ProviderError('; '.join(warnings))
            break
        except ProviderError as exc:
            if attempt == 1:
                raise ProviderError(
                    '回讲定向修正后仍未通过校验，当前解释已保留。',
                    details={'validation_error': str(exc)},
                ) from exc
            request = replace(request, retry_feedback=str(exc), previous_response=raw)
    context.guard()

    def validate():
        if snapshot(container, node_id) != captured:
            raise ConflictError('Learning tree changed during retelling; run it again')

    base = {'status': 'partial' if blocked_ids else 'complete', 'parent_node_id': node_id,
            'included_child_ids': ready_ids, 'updated_child_ids': updated_ids, 'blocked_child_ids': blocked_ids,
            'answer': parsed['answer'], 'bridges': parsed['bridges'],
            'reached_root': root and not blocked_ids,
            'next_parent_node_id': None if root or blocked_ids else captured['next_parent_node_id']}
    provider = container.context.provider.capabilities
    explanation = Explanation.create(project_id=parent['node']['project_id'], node_id=node_id,
                                     question=source['question'], answer=parsed['answer'],
                                     provider=provider.name, model=provider.model)
    integrated = {key: _fingerprint(entries[key]) for key in ready_ids}
    original = captured['original']
    explanation.learning = {'causal_payload': parsed['causal_payload'],
                            'structure': {'treatment': 'mechanism', 'prerequisites': [], 'glosses': [],
                                          'analogy': None, 'profile_revision': captured['profile']['revision'],
                                          'strategy_version': VERSION},
                            'profile_snapshot': captured['profile'],
                            'retelling': {'source_explanation_id': original['id'],
                                          'original_explanation_id': original['id'],
                                          'original_explanation': {key: original.get(key) for key in
                                                                   ('id', 'answer', 'question', 'created_at')},
                                          'base_explanation_id': source['id'],
                                          'child_fingerprints': integrated,
                                          'updated_child_ids': updated_ids,
                                          'explanation_depth': captured['depth'],
                                          'rewrite_policy': request.rewrite_policy,
                                          'visible_chars': len(visible_text(parsed['answer'])),
                                          'child_explanation_ids': [entries[key]['explanation']['id'] for key in ready_ids],
                                          'skipped_child_ids': blocked_ids,
                                          'bridges': parsed['bridges'], 'style_warnings': warnings}}
    return Draft(entity=explanation, result={**base, 'explanation': explanation.to_dict()},
                 provider=provider.name, model=provider.model, artifact_type='explanation',
                 precommit=validate)
