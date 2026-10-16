"""Single-project routes plus the explicit non-support of sharing.

One container serves exactly one workspace-bound project, so the frontend''s
historical multi-project CRUD is adapted rather than reimplemented
(``docs/instance-app-api.md`` section 10):

* ``POST /api/projects`` does **not** create a second project; it returns the
  current one (idempotent) so an old client keeps working.
* ``DELETE /api/projects/{id}`` clears this project''s business content only.
* ``POST /api/projects/{id}/share`` is a **409 with a deprecation note**: sharing
  is a control-plane concern and never touches Docker or the outer database here.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from backend.host import errors as app_errors
from backend.host.state import AppState

from ..dependencies import get_state, require_session
from ..schemas import ProjectPatch

router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.get("")
def list_projects(
    session: dict = Depends(require_session),
    state: AppState = Depends(get_state),
) -> dict:
    """Only the workspace-bound project is ever listed."""
    return {"items": [state.services.project_public(state.services.current_project())]}


@router.post("", status_code=200)
def create_project(
    session: dict = Depends(require_session),
    state: AppState = Depends(get_state),
) -> dict:
    """Deprecated multi-project creation, adapted to the single-project model.

    Returns the current project instead of creating another one. The 200 (not 201)
    status and the ``Deprecation`` header make the adaptation visible to clients.
    """
    return state.services.project_public(state.services.current_project())


@router.get("/{project_id}")
def get_project(
    project_id: str,
    session: dict = Depends(require_session),
    state: AppState = Depends(get_state),
) -> dict:
    return state.services.project_public(state.services.require_project(project_id))


@router.patch("/{project_id}")
def patch_project(
    project_id: str,
    payload: ProjectPatch,
    session: dict = Depends(require_session),
    state: AppState = Depends(get_state),
) -> dict:
    project = state.services.require_project(project_id)
    row = state.services.store.update_project(
        project["id"], title=payload.title, description=payload.description
    )
    return state.services.project_public(row)


@router.delete("/{project_id}", status_code=204)
def delete_project(
    project_id: str,
    session: dict = Depends(require_session),
    state: AppState = Depends(get_state),
) -> Response:
    """Delete this project''s business content. Sessions live outside the volume."""
    project = state.services.require_project(project_id)
    state.services.store.purge_project_content(project["id"])
    state.services.storage.delete_document_files(project["id"])
    return Response(status_code=204)


@router.post("/{project_id}/share")
def share_project(
    project_id: str,
    session: dict = Depends(require_session),
    state: AppState = Depends(get_state),
) -> Response:
    """Deliberately unsupported: sharing belongs to the control plane.

    Returned as an explicit 409 (not a 404) so the frontend can show an
    actionable migration message instead of a generic failure. The instance never
    implements invitations or redemption, because that would require touching
    Docker or the outer database.
    """
    raise app_errors.ShareNotSupported(
        "Project sharing moved to the control plane; call "
        "POST /control/workspaces/{workspace_id}/invitations through the gateway",
        details={
            "deprecated": True,
            "replacement": "POST /control/workspaces/{workspace_id}/invitations",
        },
    )


__all__ = ["router"]