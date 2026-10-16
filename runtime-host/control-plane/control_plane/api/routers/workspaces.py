"""User recovery, durable project metadata and project sharing."""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response

from control_plane.api.dependencies import get_config
from control_plane.api.schemas import (
    AccessPolicyResponse,
    ShareInvitationCreateRequest,
    UserLoginRequest,
    WorkspaceCreateRequest,
    WorkspaceImportRequest,
)
from control_plane.config import ControlPlaneConfig
from control_plane.errors import InvalidToken
from control_plane.invitations import InvitationService
from control_plane.sharing import ShareService
from control_plane.workspaces import WorkspaceService
from control_plane.security import bearer_token


def no_store(response: Response):
    response.headers['Cache-Control'] = 'no-store'


router = APIRouter(prefix='/control', tags=['workspaces'], dependencies=[Depends(no_store)])


def get_workspaces(request: Request) -> WorkspaceService:
    return request.app.state.workspaces


def get_invitations(request: Request) -> InvitationService:
    return request.app.state.invitations


def get_sharing(request: Request) -> ShareService:
    return request.app.state.sharing


def user_cookie_name(config: ControlPlaneConfig) -> str:
    return config.lease_cookie_name + '_user'


def set_user_cookie(response: Response, config: ControlPlaneConfig, user_id: str, token: str):
    response.set_cookie(
        user_cookie_name(config), f'{user_id}.{token}', httponly=True,
        secure=config.lease_cookie_secure, samesite=config.lease_cookie_samesite,
        max_age=30 * 24 * 60 * 60, path='/control',
    )


def current_user(
    request: Request,
    config: ControlPlaneConfig = Depends(get_config),
    service: WorkspaceService = Depends(get_workspaces),
) -> dict:
    credential = request.cookies.get(user_cookie_name(config))
    if credential:
        user_id, _, token = credential.partition('.')
    else:
        user_id = request.headers.get('X-User-ID', '')
        token = request.headers.get('X-User-Token', '')
    return service.authenticate(user_id, token)


def detach_other_compute(request: Request, response: Response, service: WorkspaceService, user_id: str):
    config = service.config
    token = request.cookies.get(config.lease_cookie_name)
    try:
        lease = request.app.state.allocator.authenticate(token)
    except InvalidToken:
        lease = None
    if lease is not None:
        if lease.workspace_id and service.store.require_workspace(lease.workspace_id).user_id == user_id:
            return
        request.app.state.allocator.release(lease.lease_id, reason='user_identity_changed')
    response.delete_cookie(
        config.lease_cookie_name, path='/', httponly=True,
        secure=config.lease_cookie_secure, samesite=config.lease_cookie_samesite,
    )


@router.post('/users', status_code=201)
def register_user(request: Request, response: Response, service=Depends(get_workspaces), config=Depends(get_config)):
    result = service.register()
    detach_other_compute(request, response, service, result['user']['user_id'])
    set_user_cookie(response, config, result['user']['user_id'], result['recovery_token'])
    return result


@router.post('/users/session')
def login(payload: UserLoginRequest, request: Request, response: Response, service=Depends(get_workspaces), config=Depends(get_config)):
    user = service.authenticate(str(payload.user_id), payload.recovery_token)
    detach_other_compute(request, response, service, user['user_id'])
    set_user_cookie(response, config, user['user_id'], payload.recovery_token)
    return {'user': user}


@router.get('/users/me')
def me(user=Depends(current_user)):
    return {'user': user}


@router.delete('/users/session', status_code=204)
def logout(request: Request, response: Response, user=Depends(current_user), service=Depends(get_workspaces)):
    config = service.config
    token = request.cookies.get(config.lease_cookie_name) or bearer_token(request.headers.get('Authorization'))
    try:
        lease = request.app.state.allocator.authenticate(token)
    except InvalidToken:
        lease = None
    if lease and lease.workspace_id:
        workspace = service.store.require_workspace(lease.workspace_id)
        if workspace.user_id == user['user_id']:
            request.app.state.allocator.release(lease.lease_id, reason='user_logout')
    for name, path in ((user_cookie_name(config), '/control'), (config.lease_cookie_name, '/')):
        response.delete_cookie(name, path=path, httponly=True, secure=config.lease_cookie_secure, samesite=config.lease_cookie_samesite)


@router.post('/workspaces', status_code=201)
def create_workspace(payload: WorkspaceCreateRequest, user=Depends(current_user), service=Depends(get_workspaces)):
    workspace = service.create(user['user_id'], payload.title.strip())
    return {'workspace': workspace.to_dict(service.config.workspace_idle_ttl_seconds)}


@router.get('/workspaces')
def list_workspaces(user=Depends(current_user), service=Depends(get_workspaces)):
    return {'items': [w.to_dict(service.config.workspace_idle_ttl_seconds) for w in service.list(user['user_id'])]}


@router.get('/workspaces/{workspace_id}')
def get_workspace(workspace_id: UUID, user=Depends(current_user), service=Depends(get_workspaces)):
    workspace = service.get(str(workspace_id), user['user_id'])
    return {'workspace': workspace.to_dict(service.config.workspace_idle_ttl_seconds)}


@router.post('/workspaces/{workspace_id}/touch')
def touch_workspace(workspace_id: UUID, user=Depends(current_user), service=Depends(get_workspaces)):
    workspace = service.get(str(workspace_id), user['user_id'], touch=True)
    return {'workspace': workspace.to_dict(service.config.workspace_idle_ttl_seconds)}


@router.get('/access-policy', response_model=AccessPolicyResponse)
def access_policy(
    service: InvitationService = Depends(get_invitations),
) -> dict:
    """公开能力探针：前端据此决定是否展示邀请码输入框。

    只返回非敏感能力与开关，不泄露任何码、配置或内部计数。
    """

    return {
        'invitation_only': bool(service.policy.invitation_only),
        'system_invitation_supported': True,
        'project_sharing_supported': True,
    }


@router.post('/workspaces/{workspace_id}/invitations', status_code=201)
def create_share_invitation(
    workspace_id: UUID,
    payload: ShareInvitationCreateRequest | None = None,
    user=Depends(current_user),
    service: InvitationService = Depends(get_invitations),
    sharing: ShareService = Depends(get_sharing),
):
    """项目所有者为自己的项目签发分享码。

    只有声明了 v3 内容存储布局的项目才能签发；否则拒绝，
    避免把可能含应用 session 凭证的整个卷拷给接收者。
    """

    workspace = sharing.require_shareable_workspace(str(workspace_id), user['user_id'])
    label = (payload.label if payload is not None else None) or ''
    invitation, code = service.issue_share_code(workspace=workspace, label=label)
    return {
        'invitation': invitation.to_dict(now=service._store._clock()),
        'code': code,
        'workspace': workspace.to_dict(sharing.workspace_idle_seconds),
    }


@router.get('/workspaces/{workspace_id}/invitations')
def list_share_invitations(
    workspace_id: UUID,
    user=Depends(current_user),
    service: InvitationService = Depends(get_invitations),
    sharing: ShareService = Depends(get_sharing),
):
    workspace = sharing.require_shareable_workspace(str(workspace_id), user['user_id'])
    now = service._store._clock()
    return {
        'items': [
            invitation.to_dict(now=now)
            for invitation in service._store.list_invitations_for_workspace(workspace.workspace_id)
        ]
    }


@router.delete('/workspaces/{workspace_id}/invitations/{invitation_id}', status_code=200)
def revoke_share_invitation(
    workspace_id: UUID,
    invitation_id: str,
    user=Depends(current_user),
    service: InvitationService = Depends(get_invitations),
):
    invitation = service.revoke_workspace_share(
        invitation_id, workspace_id=str(workspace_id), user_id=user['user_id']
    )
    return {'invitation': invitation.to_dict(now=service._store._clock())}


@router.post('/workspace-imports', status_code=200)
def import_shared_workspace(
    payload: WorkspaceImportRequest,
    request: Request,
    user=Depends(current_user),
    sharing: ShareService = Depends(get_sharing),
    idempotency_key: str | None = Header(default=None, alias='Idempotency-Key'),
):
    """输入分享码，得到一个独立副本。

    幂等：带 ``Idempotency-Key`` 时同一用户重试返回同一副本；不带时，
    同一用户对同一源项目的重复导入也返回已有副本。
    """

    result = sharing.import_share(
        user_id=user['user_id'],
        code=payload.invitation_code,
        title=payload.title,
        idempotency_key=idempotency_key,
    )
    return {
        'workspace': result.workspace.to_dict(sharing.workspace_idle_seconds),
        'reused': result.reused,
    }
