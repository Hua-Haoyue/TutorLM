"""管理员邀请码接口：系统码的签发/列表/撤销。

全部要求 ``X-Admin-Token``。明文码**只在签发响应里出现一次**；
列表与撤销响应都不包含明文，也不包含可用于重建的哈希。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from control_plane.api.dependencies import get_invitations, get_store
from control_plane.api.routers.admin import require_admin
from control_plane.api.schemas import InvitationCreateRequest
from control_plane.domain.models import InvitationKind
from control_plane.invitations import InvitationService
from control_plane.store import ControlPlaneStore

router = APIRouter(prefix='/control/admin/invitations', tags=['invitations'])


@router.get('', dependencies=[Depends(require_admin)], summary='列出系统邀请码（不返回明文）')
def list_invitations(
    service: InvitationService = Depends(get_invitations),
    store: ControlPlaneStore = Depends(get_store),
) -> dict:
    now = store._clock()
    items = [
        invitation.to_dict(now=now)
        for invitation in store.list_invitations(kind=str(InvitationKind.SYSTEM))
    ]
    return {'items': items}


@router.post(
    '',
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
    summary='签发一个新的系统邀请码（明文仅此一次）',
)
def create_invitation(
    payload: InvitationCreateRequest | None = None,
    service: InvitationService = Depends(get_invitations),
) -> dict:
    label = payload.label if payload is not None else ''
    invitation, code = service.issue_system_code(label=label)
    return {
        'invitation': invitation.to_dict(now=service._store._clock()),
        'code': code,
    }


@router.delete(
    '/{invitation_id}',
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(require_admin)],
    summary='撤销系统邀请码（写入持久 tombstone）',
)
def revoke_invitation(
    invitation_id: str,
    service: InvitationService = Depends(get_invitations),
) -> dict:
    invitation = service.revoke(invitation_id)
    return {'invitation': invitation.to_dict(now=service._store._clock())}
