import json
import logging

import pytest

from backend.application.services.feynman import FeynmanService, QuestionText, source_fragments
from backend.host.errors import Unprocessable
from test_feynman_workflow import optimized, generate, SOURCE


def valid():
    return {'statement': 'So the conclusion always proves its premise?',
            'choices': ['The direction is reversed', 'Yes, that follows'], 'correct_index': 0,
            'correction': 'The premise leads to the conclusion.',
            'source_fragment_id': 's1', 'flaw': 'Reversed implication'}


@pytest.mark.parametrize('field,value,reason', [
    ('statement', 'x', 'text'), ('correction', None, 'text'),
    ('source_fragment_id', 'missing', 'quote'), ('source_fragment_id', True, 'quote'),
    ('source_fragment_id', [], 'quote'), ('flaw', 'x'*301, 'text'),
    ('correct_index', '0', 'index'), ('correct_index', True, 'index'),
    ('correct_index', 2, 'index'), ('choices', [], 'choices'),
    ('choices', ['same', 'same'], 'choices'), ('choices', ['valid', 1], 'choices'),
    ('choices', ['valid', 'x'*161], 'choices'),
])
def test_exact_rule_without_content(field, value, reason):
    data = valid()
    data[field] = value
    with pytest.raises(Unprocessable) as error:
        FeynmanService.parse(json.dumps(data), SOURCE, [])
    assert error.value.details['reason'] == reason
    assert set(error.value.details) <= {'reason', 'field', 'actual_type', 'actual_length'}
    assert SOURCE not in str(error.value.details)


@pytest.mark.parametrize('raw,previous,reason', [
    ('{invalid', [], 'json'), ('[]', [], 'fields'),
    (json.dumps({**valid(), 'extra': 'secret'}), [], 'fields'),
    (json.dumps(valid()), [valid()['statement']], 'duplicate'),
])
def test_other_rules(raw, previous, reason):
    with pytest.raises(Unprocessable) as error:
        FeynmanService.parse(raw, SOURCE, previous)
    assert error.value.details['reason'] == reason


def test_validation_log_correlates_without_leaking(client, session_headers, optimized, wait_for_run, monkeypatch, caplog):
    node, exp = optimized
    data = valid()
    data['source_fragment_id'] = 'PRIVATE INCORRECT QUOTATION'
    monkeypatch.setattr(client.app.state.tutorlm.services.provider, 'feynman',
        lambda payload: QuestionText(json.dumps(data), 'STOP'))
    with caplog.at_level(logging.WARNING):
        run = generate(client, session_headers, wait_for_run, node, exp)
    assert run['state'] == 'failed'
    assert run['error']['code'] == 'feynman_invalid_question'
    assert '引用' in run['error']['message']
    record = next(r.message for r in caplog.records if r.message.startswith('feynman_validation_failed '))
    diagnostic = json.loads(record.split(' ', 1)[1])
    assert diagnostic['run_id'] == run['id']
    assert diagnostic['reason'] == 'quote'
    assert diagnostic['actual_type'] == 'str'
    assert diagnostic['finish_reason'] == 'STOP'
    assert SOURCE not in record
    assert 'PRIVATE INCORRECT QUOTATION' not in record
    assert 'correct_index' not in json.dumps(run)


@pytest.mark.parametrize('source', ['中文第一句。第二句！\n第三句？', 'A sentence. Another sentence!',
                                   'x' * 2500, '短句。', '标点、 空格\n保持原样。'])
def test_fragment_evidence_is_exact(source):
    fragments = source_fragments(source)
    assert ''.join(item['text'] for item in fragments) == source
    assert len({item['id'] for item in fragments}) == len(fragments)
    for item in fragments:
        data = valid()
        data['source_fragment_id'] = item['id']
        parsed = FeynmanService.parse(json.dumps(data), source, [])
        assert parsed['source_quote'] == item['text']
        assert parsed['source_quote'] in source
        assert len(parsed['source_quote']) <= 1200
        assert 'source_fragment_id' not in parsed


def test_model_gets_numbered_evidence(client, session_headers, optimized, wait_for_run, monkeypatch):
    node, exp = optimized
    provider = client.app.state.tutorlm.services.provider
    original = provider.feynman
    captured = []
    def capture(payload):
        captured.append(payload)
        return original(payload)
    monkeypatch.setattr(provider, 'feynman', capture)
    run = generate(client, session_headers, wait_for_run, node, exp)
    assert run['state'] == 'completed'
    assert captured[0]['source_fragments']
    assert 'source' not in captured[0]


def test_gemini_numeric_enum_is_normalized():
    from backend.infrastructure.providers.gemini import gemini_schema
    original = {'type': 'object', 'properties': {'index': {'type': 'integer', 'enum': [0, 1]},
                                              'mode': {'type': 'string', 'enum': ['one', 'two']}}}
    result = gemini_schema(original)
    assert 'enum' not in result['properties']['index']
    assert result['properties']['mode']['enum'] == ['one', 'two']
    assert original['properties']['index']['enum'] == [0, 1]


def test_provider_failure_logs_finish_without_response(client, session_headers, optimized, wait_for_run, monkeypatch, caplog):
    from backend.domain.errors import ProviderUnavailable
    node, exp = optimized
    def truncated(payload):
        raise ProviderUnavailable('PRIVATE UPSTREAM ERROR', details={'finish_reason': 'MAX_TOKENS'})
    monkeypatch.setattr(client.app.state.tutorlm.services.provider, 'feynman', truncated)
    with caplog.at_level(logging.WARNING):
        run = generate(client, session_headers, wait_for_run, node, exp)
    diagnostic = next(r.message for r in caplog.records if r.message.startswith('feynman_provider_failed '))
    assert run['id'] in diagnostic
    assert 'MAX_TOKENS' in diagnostic
    assert 'PRIVATE UPSTREAM ERROR' not in diagnostic


def test_gemini_truncation_has_a_safe_finish_reason():
    from backend.infrastructure.providers.gemini import GeminiProvider
    from backend.application.ports.provider import GenerateRequest
    from backend.domain.errors import ProviderUnavailable
    provider = GeminiProvider(api_base='https://example.test', api_key='SECRET', model='offline',
        transport=lambda *args: (200, b'{"candidates":[{"finishReason":"MAX_TOKENS"}]}'))
    with pytest.raises(ProviderUnavailable) as error:
        provider.generate(GenerateRequest(kind='feynman', prompt='test'))
    assert error.value.details['finish_reason'] == 'MAX_TOKENS'
    assert 'SECRET' not in str(error.value)
