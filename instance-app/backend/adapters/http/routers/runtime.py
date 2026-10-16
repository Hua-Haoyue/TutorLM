"""``GET /api/runtime`` -- capability discovery, unauthenticated by contract.

The frontend decides whether to enable the explain/narrate buttons from
``plugins[].workflows[].kind`` (docs section 7). No secret is ever included: only
the provider name/model, never the API key.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from backend.host.state import AppState

from ..dependencies import get_state

router = APIRouter(prefix="/api", tags=["runtime"])


@router.get("/runtime")
def runtime(state: AppState = Depends(get_state)) -> dict:
    return state.services.runtime_public()


@router.get('/plugins')
def plugins(state: AppState = Depends(get_state)) -> dict:
    return {'items': state.plugins.descriptors(), **state.plugins.summary()}


__all__ = ["router"]
