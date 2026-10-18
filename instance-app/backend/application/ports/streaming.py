"""Live partial text of a running generation.

The run host installs a *recorder* for each run; a service that wants its model call
streamed wraps the call in :func:`stream`, giving a transform from the raw model text
to what the user should see (for a JSON answer: the answer string so far). A provider
that can stream calls :func:`sink` and, when it returns a callable, feeds it the raw
text accumulated so far. Nothing here is persisted: the partial text is only polled
with the run while it is running.
"""
from __future__ import annotations

import json
import re
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Iterator

RECORDER: ContextVar[Callable[[str], None] | None] = ContextVar('tutorlm_partial_recorder', default=None)
_SINK: ContextVar[Callable[[str], None] | None] = ContextVar('tutorlm_stream_sink', default=None)


def sink() -> Callable[[str], None] | None:
    """The callable a streaming provider feeds with the raw text so far, or None."""
    return _SINK.get()


@contextmanager
def stream(transform: Callable[[str], str]) -> Iterator[None]:
    """Stream the model calls inside this block to the current run, if there is one."""
    record = RECORDER.get()

    def feed(raw: str) -> None:
        try:
            text = transform(raw)
        except Exception:  # a preview must never break the generation
            return
        if text:
            record(text)
    token = _SINK.set(feed if record is not None else None)
    try:
        yield
    finally:
        _SINK.reset(token)


_ESCAPES = {'"': '"', '\\': '\\', '/': '/', 'b': '\b', 'f': '\f', 'n': '\n', 'r': '\r', 't': '\t'}


def json_string_prefix(raw: str, key: str) -> str:
    """Decoded prefix of the string value of `key` in a JSON object still being written."""
    match = re.search(r'"' + re.escape(key) + r'"\s*:\s*"', raw)
    if not match:
        return ''
    out, i = [], match.end()
    while i < len(raw):
        ch = raw[i]
        if ch == '"':
            break
        if ch != '\\':
            out.append(ch)
            i += 1
            continue
        if i + 1 >= len(raw):
            break  # escape split across chunks
        code = raw[i + 1]
        if code == 'u':
            digits = raw[i + 2:i + 6]
            if len(digits) < 4:
                break
            try:
                out.append(json.loads('"\\u' + digits + '"'))
            except ValueError:
                pass
            i += 6
            continue
        out.append(_ESCAPES.get(code, code))
        i += 2
    return ''.join(out)
