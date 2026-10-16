"""``/health`` and ``/health/ready``.

The control plane calls ``/health/ready`` on the Docker network **without** an
application Bearer, so these routes are unauthenticated by contract
(``contracts/instance-runtime-v1.md``, docs section 8). They expose no business
data: only the liveness flag, the run mode and the project-binding declaration.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from backend.host.state import AppState

from ..dependencies import get_state

router = APIRouter(tags=["health"])


@router.get("/health")
def health(state: AppState = Depends(get_state)) -> dict[str, str]:
    """Liveness only: being alive is not the same as being ready."""
    return state.health.live()


@router.get("/health/ready")
def ready(response: Response, state: AppState = Depends(get_state)) -> dict:
    """Readiness handshake.

    Persistent instances must declare ``storage_mode=persistent`` +
    ``workspace_id``; ``storage_layout=content_only`` is only present once the
    domain proved the volume carries business content alone. When readiness is
    not ok the HTTP status is 503 so the control plane never admits a visitor.
    """
    report = state.health.ready()
    response.status_code = report.http_status
    return report.to_payload()


__all__ = ["router"]