"""Validated inference controls; defaults never enable external search."""
from backend.domain.errors import InvalidInputError
from contextlib import contextmanager
from contextvars import ContextVar

CURRENT_OPTIONS = ContextVar('tutorlm_model_options', default=None)


@contextmanager
def inference_options(payload):
    value = payload.get('model')
    if value is None and isinstance(payload.get('preferences'), dict):
        value = payload['preferences'].get('model')
    token = CURRENT_OPTIONS.set(model_options(value))
    try:
        yield
    finally:
        CURRENT_OPTIONS.reset(token)

MODEL_OPTIONS_SCHEMA = {'type': 'object', 'additionalProperties': False, 'properties': {
    'effort': {'type': 'string', 'enum': ['auto', 'low', 'medium', 'high']},
    'web_search': {'type': 'boolean'},
}}


def model_options(value=None):
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - {'effort', 'web_search'}:
        raise InvalidInputError('Invalid model options')
    result = {'effort': 'auto', 'web_search': False} | value
    if result['effort'] not in ('auto', 'low', 'medium', 'high') or type(result['web_search']) is not bool:
        raise InvalidInputError('Invalid reasoning effort or web search setting')
    return result
