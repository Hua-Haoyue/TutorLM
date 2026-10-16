"""Unified source references (``POST /api/sources/resolve``).

Refs are validated against the **stored revision** and never silently re-pointed
to the newest one (``docs/instance-app-api.md`` section 6). Resolution is real,
per format:

* md/txt: a codepoint/utf16 ``text`` span (or ``whole-document``), re-checked
  against the canonical ``quote``/``prefix``/``suffix``;
* tex: a ``tex-source`` span resolved inside the validated project (no compiler);
* pdf: text comes only from a **ready extraction** -- a missing extractor or a
  missing extraction is reported, never fabricated.

Anything that cannot be proven -- a missing revision, a stale
``artifact_id``/``build_id``, a cross-workspace document, an unusable selector, a
quote that does not match, or an unavailable PDF extractor -- comes back as
``resolved: false`` with a machine-readable ``reason`` from the service layer.
Each ref is resolved independently, so one malformed ref cannot fail the batch.
"""
from __future__ import annotations

from typing import Any, Mapping

from fastapi import APIRouter, Depends

from backend.host.state import AppState

from ..dependencies import get_state, require_session
from ..schemas import SourceResolveRequest

router = APIRouter(prefix="/api/sources", tags=["sources"])


@router.post("/resolve")
def resolve_sources(
    payload: SourceResolveRequest,
    session: dict = Depends(require_session),
    state: AppState = Depends(get_state),
) -> dict:
    budget = payload.budget or {}
    max_chars = _budget_int(budget, "max_chars")
    items = [state.services.resolve_ref(ref, max_chars=max_chars) for ref in payload.refs]
    return {"items": items}


def _budget_int(budget: Mapping[str, Any], key: str) -> int | None:
    value = budget.get(key)
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


__all__ = ["router"]
