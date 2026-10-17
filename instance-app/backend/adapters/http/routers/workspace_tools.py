"""Session-bound source-project and agent API. No shell or raw filesystem routes."""
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from backend.domain.errors import InvalidInputError
from ..dependencies import get_state, require_session
from .documents import _read_multipart, _upload_of

router = APIRouter(prefix='/api', dependencies=[Depends(require_session)])


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class FileWrite(Strict):
    path: str = Field(min_length=1, max_length=240)
    text: str = Field(max_length=2 * 1024 * 1024)
    expected_revision: str | None = None


class Decision(Strict):
    approve: StrictBool


@router.get('/code/files')
def files(state=Depends(get_state)):
    return {'items': state.services.agent.code.files()}


@router.get('/code/search')
def search(query: str, state=Depends(get_state)):
    return {'items': state.services.agent.code.search(query)}


@router.get('/code/file')
def read(path: str, start_line: int = 1, end_line: int = 200, state=Depends(get_state)):
    return state.services.agent.code.read(path, start_line, end_line)


@router.put('/code/file')
def write(payload: FileWrite, state=Depends(get_state)):
    return {'document': state.services.agent.code.write(payload.path, payload.text, payload.expected_revision)}


@router.post('/code/import', status_code=201)
async def import_code(request: Request, state=Depends(get_state)):
    parts = await _read_multipart(request)
    file = _upload_of(parts)
    if (file.filename or '').lower().endswith('.zip'):
        return {'items': state.services.agent.code.import_archive(file.data)}
    try:
        text = file.data.decode('utf-8')
    except UnicodeError as exc:
        raise InvalidInputError('Source file must be UTF-8') from exc
    path_part = next((p for p in parts if p.name == 'path'), None)
    path = path_part.text() if path_part else file.filename
    return {'items': [state.services.agent.code.write(path, text)]}


@router.post('/chat/threads', status_code=201)
def create_chat(session=Depends(require_session), state=Depends(get_state)):
    return state.services.agent.create(str(session['id']))


@router.get('/chat/threads')
def list_chats(session=Depends(require_session), state=Depends(get_state)):
    return {'items': state.services.agent.threads(str(session['id']))}


@router.get('/chat/threads/{thread_id}')
def history(thread_id: str, session=Depends(require_session), state=Depends(get_state)):
    return state.services.agent.history(thread_id, str(session['id']))


@router.post('/chat/proposals/{proposal_id}')
def decide(proposal_id: str, payload: Decision, session=Depends(require_session), state=Depends(get_state)):
    return state.services.agent.decide(proposal_id, str(session['id']), payload.approve)
