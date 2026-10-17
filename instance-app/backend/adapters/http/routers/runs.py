"""Runs: submit, poll, cancel, plus explanation/narration history.

The frontend polls ``GET /api/runs/{id}`` (~250ms growing to ~1s) until the run
reaches a terminal state; SSE is an optional later addition, so nothing here
depends on streaming (``docs/instance-app-api.md`` section 8).

Cancellation is a hard "do not commit": the worker re-checks state before writing
its result, so a cancelled run never persists an answer.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from backend.domain.errors import NotFoundError

from backend.host.state import AppState

from ..dependencies import get_state, idempotency_key, require_session
from ..schemas import RunCreate

router = APIRouter(prefix="/api", tags=["runs"])


@router.post("/runs", status_code=202)
def create_run(
    payload: RunCreate,
    session: dict = Depends(require_session),
    state: AppState = Depends(get_state),
    key: str | None = Depends(idempotency_key),
) -> dict:
    """Queue an explanation/narration run.

    ``Idempotency-Key`` is scoped to this workspace, so a retry from a fresh
    session still maps to the original run; the same key with a different body is
    a 409 (handled by the RunManager).
    """
    return state.services.submit_run(
        kind=payload.kind,
        input_payload=payload.input,
        idempotency_key=key,
        session_id=str(session["id"]),
    )


@router.get("/runs")
def list_runs(
    session: dict = Depends(require_session),
    state: AppState = Depends(get_state),
) -> dict:
    items = state.services.list_runs()
    return {"items": [run for run in items if run['kind'] != 'workspace_chat'
                      or (state.store.get_run(run['id']) or {}).get('session_id') == str(session['id'])]}


@router.get("/runs/{run_id}")
def get_run(
    run_id: str,
    session: dict = Depends(require_session),
    state: AppState = Depends(get_state),
) -> dict:
    row = state.store.get_run(run_id)
    if row and row['kind'] == 'workspace_chat' and row['session_id'] != str(session['id']):
        raise NotFoundError('Run not found')
    return state.services.get_run(run_id)


@router.post("/runs/{run_id}/cancel", status_code=202)
def cancel_run(
    run_id: str,
    session: dict = Depends(require_session),
    state: AppState = Depends(get_state),
) -> dict:
    """Cancel a queued/running run. Its result is discarded, never committed."""
    row = state.store.get_run(run_id)
    if row and row['kind'] == 'workspace_chat' and row['session_id'] != str(session['id']):
        raise NotFoundError('Run not found')
    return state.services.cancel_run(run_id)


@router.get("/runs/{run_id}/events")
def run_events(
    run_id: str,
    session: dict = Depends(require_session),
    state: AppState = Depends(get_state),
) -> Response:
    """Optional SSE channel -- deliberately not implemented.

    The frontend contract is polling, so an unimplemented stream must be an
    explicit 501 rather than a hanging connection that looks like a stall.
    """
    state.services.get_run(run_id)  # 404 for unknown runs rather than a bare 501
    return Response(
        status_code=501,
        content=(
            '{"error":{"code":"not_implemented","message":'
            '"Run events are not streamed by this instance; poll GET /api/runs/{id}"}}'
        ),
        media_type="application/json",
        headers={"Retry-After": "1"},
    )


# -- history -------------------------------------------------------------
@router.get("/projects/{project_id}/explanations")
def list_explanations(
    project_id: str,
    session: dict = Depends(require_session),
    state: AppState = Depends(get_state),
) -> dict:
    return {"items": state.services.list_explanations(project_id)}


@router.get("/projects/{project_id}/narrations")
def list_narrations(
    project_id: str,
    session: dict = Depends(require_session),
    state: AppState = Depends(get_state),
) -> dict:
    return {"items": state.services.list_narrations(project_id)}


__all__ = ["router"]
