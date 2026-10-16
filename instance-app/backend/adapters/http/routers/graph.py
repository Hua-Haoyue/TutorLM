"""Nodes, edges and the project graph.

Node deletion promotes children to the top level instead of cascading
(``docs/instance-app-api.md`` section 5), and ``PATCH`` carries the optimistic
``revision`` so a stale edit is a 409 rather than a silent overwrite.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from backend.host.state import AppState

from ..dependencies import get_state, require_session
from ..schemas import EdgeCreate, NodeCreate, NodePatch

router = APIRouter(prefix="/api", tags=["graph"])


@router.post("/projects/{project_id}/nodes", status_code=201)
def create_node(
    project_id: str,
    payload: NodeCreate,
    session: dict = Depends(require_session),
    state: AppState = Depends(get_state),
) -> dict:
    return state.services.create_node(
        project_id=project_id,
        document_id=payload.document_id,
        title=payload.title,
        kind=payload.kind,
        content=payload.content,
        parent_id=payload.parent_id,
        metadata=payload.metadata,
    )


@router.patch("/nodes/{node_id}")
def patch_node(
    node_id: str,
    payload: NodePatch,
    session: dict = Depends(require_session),
    state: AppState = Depends(get_state),
) -> dict:
    return state.services.update_node(
        node_id,
        title=payload.title,
        kind=payload.kind,
        content=payload.content,
        metadata=payload.metadata,
        revision=payload.revision,
    )


@router.delete("/nodes/{node_id}", status_code=204)
def delete_node(
    node_id: str,
    session: dict = Depends(require_session),
    state: AppState = Depends(get_state),
) -> Response:
    state.services.delete_node(node_id)
    return Response(status_code=204)


@router.get("/projects/{project_id}/graph")
def graph(
    project_id: str,
    session: dict = Depends(require_session),
    state: AppState = Depends(get_state),
) -> dict:
    return state.services.graph(project_id)


@router.post("/projects/{project_id}/graph/edges", status_code=201)
def create_edge(
    project_id: str,
    payload: EdgeCreate,
    session: dict = Depends(require_session),
    state: AppState = Depends(get_state),
) -> dict:
    return state.services.create_edge(
        project_id=project_id,
        source_node_id=payload.source_node_id,
        target_node_id=payload.target_node_id,
        relation=payload.relation,
        metadata=payload.metadata,
    )


@router.delete("/projects/{project_id}/graph/edges/{edge_id}", status_code=204)
def delete_edge(
    project_id: str,
    edge_id: str,
    session: dict = Depends(require_session),
    state: AppState = Depends(get_state),
) -> Response:
    state.services.delete_edge(project_id, edge_id)
    return Response(status_code=204)


__all__ = ["router"]