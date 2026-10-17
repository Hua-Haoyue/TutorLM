"""Lease-authorized session bootstrap; never accepts an upstream from a client."""
import httpx
from fastapi import APIRouter, Depends, Header, Request, Response

from control_plane.api.dependencies import get_allocator, get_config
from control_plane.api.routers.leases import _credential, _assert_controlled_upstream
from control_plane.errors import RuntimeUnavailable, InvalidToken
from control_plane.security import app_bootstrap_token

router = APIRouter(prefix='/control')


def issue_at_instance(upstream, token):
    try:
        with httpx.Client(trust_env=False, timeout=10, follow_redirects=False) as client:
            result = client.post(upstream + '/internal/app-session',
                                 headers={'X-App-Bootstrap-Token': token}, json={})
        if result.status_code != 201:
            raise RuntimeUnavailable('Application session bootstrap failed')
        data = result.json()
        if not isinstance(data, dict) or not isinstance(data.get('access_token'), str) or not data['access_token']:
            raise ValueError('Invalid session response')
        if not isinstance(data.get('session'), dict):
            raise ValueError('Missing session binding')
        return data
    except (httpx.HTTPError, ValueError):
        raise RuntimeUnavailable('Application session bootstrap unavailable') from None


@router.post('/app-session', status_code=201)
def create(request: Request, response: Response, authorization: str | None = Header(default=None),
           allocator=Depends(get_allocator), config=Depends(get_config)):
    credential = _credential(request, config, authorization)
    lease, upstream = allocator.resolve_target(credential)
    upstream = _assert_controlled_upstream(config, lease, upstream)
    token = app_bootstrap_token(config, request.app.state.store, lease)
    data = issue_at_instance(upstream, token)
    latest, latest_upstream = allocator.resolve_target(credential)
    if latest.lease_id != lease.lease_id or latest.generation != lease.generation or latest_upstream != upstream:
        raise InvalidToken('Lease changed during session bootstrap')
    expected_workspace = lease.workspace_id or 'local'
    if data['session'].get('workspace_id') != expected_workspace:
        raise RuntimeUnavailable('Application returned a mismatched workspace session')
    response.headers['Cache-Control'] = 'no-store'
    return {'access_token': data['access_token'], 'session': data['session']}
