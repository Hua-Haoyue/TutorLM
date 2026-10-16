"""Application sessions (``/api/sessions*``).

Distinct from a compute lease and from the outer user identity
(``contracts/instance-runtime-v1.md``). Two rules are load-bearing:

1. **Implicit sessions are denied by default.** Without ``TUTORLM_INVITE_CODE``
   or an explicit ``TUTORLM_ALLOW_IMPLICIT_SESSION=true`` (docs section 3.2,
   planned opt-in) an unauthenticated ``POST /api/sessions`` is a 401
   ``invite_required``. The app never infers identity from the network topology
   and never accepts a client-supplied owner/workspace.
2. Sessions are stored only under ``TUTORLM_SESSION_DATA_DIR``; ``DELETE`` marks
   the row for cleanup and **never** deletes project content.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from backend.host import errors as app_errors
from backend.host.state import AppState

from ..dependencies import get_state, require_session
from ..schemas import SessionCreate

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def _implicit_allowed(state: AppState) -> bool:
    """Stage-A opt-in: explicit flag + persistent mode + bound workspace env."""
    config = state.config
    return bool(
        config.allow_implicit_session
        and config.persistent_workspace
        and config.run_mode in ("local", "managed")
        and config.workspace_id
        and config.owner_id
    )


@router.post("", status_code=201)
def create_session(
    payload: SessionCreate,
    request: Request,
    response: Response,
    state: AppState = Depends(get_state),
) -> dict:
    config = state.config
    supplied = payload.invite_code or None

    if config.invite_code:
        if not supplied or supplied != config.invite_code:
            raise app_errors.InviteRequired("The invite code is missing or incorrect")
    elif not _implicit_allowed(state):
        # Default deny. This is not a topology check: isolation is a deployment
        # constraint, never an application-verifiable security property.
        raise app_errors.InviteRequired(
            "This instance requires an invite code; implicit local sessions are disabled"
        )

    # The binding is always the env-provided workspace, never a request field.
    # A local/demo instance has none injected; the session then carries the same
    # stable local identity the Host uses for its single project.
    workspace_id = state.services.workspace_id
    if config.requires_workspace_binding and not config.workspace_id:
        raise app_errors.ServiceUnavailable("This instance is not bound to a workspace", code="workspace_unbound")

    token, session = state.sessions.issue(workspace_id=workspace_id)
    response.headers["Cache-Control"] = "no-store"
    return {
        "access_token": token,
        "session": {
            "id": session["id"],
            "workspace_id": session["workspace_id"],
            "expires_at": session["expires_at"],
        },
    }


@router.get("/me")
def read_session(
    session: dict = Depends(require_session),
    state: AppState = Depends(get_state),
) -> dict:
    current = state.sessions.get(str(session["id"])) or session
    return {
        "session": state.sessions.public(current),
        "limits": state.config.limits.public(),
    }


@router.delete("/me", status_code=202)
def delete_session(
    session: dict = Depends(require_session),
    state: AppState = Depends(get_state),
) -> Response:
    """Schedule session cleanup. Project content is untouched by design."""
    with state.runs.publication_guard():
        state.sessions.revoke(str(session['id']))
        state.sessions.gc()
    return Response(status_code=202)


__all__ = ["router"]
