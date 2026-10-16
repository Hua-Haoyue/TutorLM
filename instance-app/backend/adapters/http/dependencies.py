"""FastAPI dependencies: state access, bearer auth and idempotency.

Security notes enforced here:

* the application Bearer is the **only** accepted credential. No outer user
  cookie, ``X-User-ID`` or ``X-User-Token`` is read, and no client-supplied
  owner/workspace is trusted (``docs/instance-app-design.md`` sections 3 and 10);
* sessions live outside the volume (``SessionStore``), so a shared/cloned volume
  never carries an identity;
* idempotency keys are accepted only as a header and are never logged.
"""
from __future__ import annotations

from typing import Any

from fastapi import Depends, Header, Request

from backend.host import errors as app_errors
from backend.host.state import AppState

#: Header carrying the application session token.
AUTHORIZATION = "Authorization"
IDEMPOTENCY_KEY = "Idempotency-Key"
#: Max accepted idempotency key length; longer keys are rejected, not truncated.
MAX_IDEMPOTENCY_KEY = 200


def get_state(request: Request) -> AppState:
    state = getattr(request.app.state, "tutorlm", None)
    if state is None:  # pragma: no cover - only if the app was built incorrectly
        raise app_errors.ServiceUnavailable("Application state is not initialised", code="state_uninitialised")
    return state


def bearer_token(request: Request) -> str | None:
    """Extract the application Bearer token. Cookies and identity headers are ignored."""
    header = request.headers.get(AUTHORIZATION) or request.headers.get(AUTHORIZATION.lower())
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = value.strip()
    return token or None


def require_session(
    request: Request,
    state: AppState = Depends(get_state),
) -> dict[str, Any]:
    """Validate the Bearer against the volume-external session store.

    Raises 401 when absent/invalid and 410 when expired -- the documented split.
    """
    token = bearer_token(request)
    record = state.sessions.validate(token)
    return record


def optional_session(
    request: Request,
    state: AppState = Depends(get_state),
) -> dict[str, Any] | None:
    token = bearer_token(request)
    if not token:
        return None
    try:
        return state.sessions.validate(token)
    except app_errors.AppError:
        return None


def idempotency_key(
    state: AppState = Depends(get_state),
    key: str | None = Header(default=None, alias=IDEMPOTENCY_KEY),
) -> str | None:
    """Read the optional ``Idempotency-Key`` header without ever logging it."""
    if key is None:
        return None
    value = key.strip()
    if not value:
        return None
    if len(value) > MAX_IDEMPOTENCY_KEY:
        raise app_errors.BadRequest(
            f"Idempotency-Key must be at most {MAX_IDEMPOTENCY_KEY} characters",
            code="invalid_idempotency_key",
        )
    return value


__all__ = [
    "AUTHORIZATION",
    "IDEMPOTENCY_KEY",
    "MAX_IDEMPOTENCY_KEY",
    "bearer_token",
    "get_state",
    "idempotency_key",
    "optional_session",
    "require_session",
]