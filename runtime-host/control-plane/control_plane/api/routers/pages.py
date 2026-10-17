"""Document presence is bounded by, and never extends, the compute lease TTL."""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response

from control_plane.api.dependencies import get_allocator, get_config, get_store
from control_plane.api.routers.leases import _credential
from control_plane.api.schemas import LeasePageRequest
from control_plane.errors import InvalidToken


def no_store(response: Response):
    response.headers['Cache-Control'] = 'no-store'


router = APIRouter(prefix='/control/leases', tags=['lease-pages'], dependencies=[Depends(no_store)])


def current_page_lease(lease_id: str, request: Request, allocator=Depends(get_allocator), config=Depends(get_config)):
    credential = _credential(request, config, request.headers.get('Authorization'))
    lease = allocator.authenticate(credential)
    if lease.lease_id != lease_id:
        raise InvalidToken('Page does not belong to the current compute lease')
    return lease


@router.post('/{lease_id}/pages', status_code=201)
def attach_page(payload: LeasePageRequest, lease=Depends(current_page_lease), store=Depends(get_store), config=Depends(get_config)):
    page_id = str(payload.page_id)
    store.register_page(lease.lease_id, page_id)
    return {
        'lease_id': lease.lease_id, 'page_id': page_id, 'expires_at': lease.expires_at,
        'heartbeat_interval_ms': max(100, min(20000, config.page_heartbeat_timeout_seconds * 1000 // 3)),
    }


@router.post('/{lease_id}/pages/{page_id}/heartbeat')
def heartbeat_page(page_id: UUID, lease=Depends(current_page_lease), store=Depends(get_store)):
    store.heartbeat_page(lease.lease_id, str(page_id))
    return {'status': 'ok', 'expires_at': lease.expires_at}


@router.post('/{lease_id}/pages/{page_id}/close', status_code=202)
def close_page(page_id: UUID, lease=Depends(current_page_lease), store=Depends(get_store), allocator=Depends(get_allocator), config=Depends(get_config)):
    store.close_page(lease.lease_id, str(page_id))
    # A confirmed final page close releases compute immediately. A lost pagehide
    # still follows the heartbeat timeout in GC, and a different live page keeps
    # the shared lease alive.
    deadline = store.page_deadline(lease.lease_id, config.page_heartbeat_timeout_seconds, 0)
    released = deadline is not None and deadline <= allocator._clock()
    if released:
        final = allocator.release(lease.lease_id, reason='last_page_closed')
        return {'lease_id': final.lease_id, 'page_id': str(page_id), 'status': str(final.state)}
    return {'lease_id': lease.lease_id, 'page_id': str(page_id), 'status': 'closing'}
