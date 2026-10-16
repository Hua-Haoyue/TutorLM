"""Workspace learning data consumed by plugin workflows."""
from fastapi import APIRouter, Depends

from backend.application.services.workspace import LearningService
from backend.plugin_contracts.learning import ProfileWrite, LearningStateWrite
from ..dependencies import require_session, get_state

router = APIRouter(prefix='/api', tags=['learning'], dependencies=[Depends(require_session)])


@router.get('/learning/profile')
def profile(state=Depends(get_state)):
    return LearningService(state.services).get_profile()


@router.put('/learning/profile')
def update_profile(payload: ProfileWrite, state=Depends(get_state)):
    with state.runs.publication_guard():
        return LearningService(state.services).set_profile(payload.profile.model_dump(), payload.expected_revision)


@router.get('/nodes/{node_id}/learning')
def node_state(node_id: str, state=Depends(get_state)):
    return LearningService(state.services).get_state(node_id)


@router.put('/nodes/{node_id}/learning')
def update_state(node_id: str, payload: LearningStateWrite, state=Depends(get_state)):
    with state.runs.publication_guard():
        return LearningService(state.services).set_state(node_id, payload.model_dump())
