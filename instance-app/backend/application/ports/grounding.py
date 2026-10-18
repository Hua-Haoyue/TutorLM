"""Web pages a generation actually used (search grounding), collected per call.

A service wraps its model call in :func:`collect`; a provider that searched the web
reports the pages with :func:`add`. Only http(s) links with a short title are kept.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_PAGES: ContextVar[list | None] = ContextVar('tutorlm_grounding_pages', default=None)
MAX_PAGES = 10


@contextmanager
def collect() -> Iterator[list[dict]]:
    pages: list[dict] = []
    token = _PAGES.set(pages)
    try:
        yield pages
    finally:
        _PAGES.reset(token)


def add(sources) -> None:
    pages = _PAGES.get()
    if pages is None or not isinstance(sources, list):
        return
    for item in sources:
        if not isinstance(item, dict) or len(pages) >= MAX_PAGES:
            continue
        uri, title = item.get('uri') or item.get('url'), item.get('title')
        if not isinstance(uri, str) or len(uri) > 2000 or not uri.lower().startswith(('https://', 'http://')):
            continue
        if any(page['uri'] == uri for page in pages):
            continue
        title = title.strip()[:200] if isinstance(title, str) and title.strip() else uri.split('/')[2][:200]
        pages.append({'uri': uri, 'title': title})
