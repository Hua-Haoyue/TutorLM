"""Error rows -> the single documented JSON envelope.

Frontend contract (``docs/instance-app-api.md``):

    {"error": {"code": str, "message": str, "details"?: object}}

Two error families reach the adapter: Host/app errors
(``backend.host.errors``) and foundation domain errors
(``backend.domain.errors``). Both are mapped by their own ``code`` /
``status_code`` so this module never invents a code of its own, and stack traces
or internal paths are never echoed to the browser.
"""
from __future__ import annotations

from typing import Any, Mapping

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.host import errors as app_errors

#: Starlette's default status -> our code vocabulary (only what we can hit).
_STATUS_CODES: Mapping[int, str] = {
    400: "invalid_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    410: "gone",
    413: "payload_too_large",
    415: "unsupported_media",
    422: "invalid_request",
    429: "rate_limited",
    500: "internal_error",
    501: "not_implemented",
    503: "unavailable",
}


def envelope(code: str, message: str, details: Mapping[str, Any] | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = dict(details)
    return {"error": error}


def error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    details: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=envelope(code, message, details),
        headers=dict(headers) if headers else None,
    )


def _domain_error_payload(exc: Exception) -> tuple[int, str, str, dict[str, Any]] | None:
    """Map a foundation ``DomainError`` without importing it at module import time."""
    code = getattr(exc, "code", None)
    status = getattr(exc, "http_status", None)
    if not isinstance(status, int) or not isinstance(code, str):
        return None
    details = getattr(exc, "details", None) or {}
    return status, code, str(exc), dict(details)


def install_error_handlers(app: FastAPI) -> None:
    """Register handlers so *every* failure leaves as the same envelope."""

    @app.exception_handler(app_errors.AppError)
    async def _host_error(_: Request, exc: app_errors.AppError) -> JSONResponse:
        status = exc.status_code
        headers: dict[str, str] = dict(exc.headers)
        if status in (429, 503) and "Retry-After" not in headers:
            # Never leave a saturated/limited instance without a retry hint.
            headers["Retry-After"] = "1"
        return error_response(status, exc.code, exc.message_text, details=exc.details, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"loc": [str(part) for part in item.get("loc", ())], "msg": item.get("msg", ""), "type": item.get("type", "")}
            for item in exc.errors()
        ]
        return error_response(422, "invalid_request", "Request body failed validation", details={"errors": errors})

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _STATUS_CODES.get(exc.status_code, "http_error")
        detail = exc.detail if isinstance(exc.detail, str) else None
        return error_response(exc.status_code, code, detail or "Request failed", headers=getattr(exc, "headers", None))

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        mapped = _domain_error_payload(exc)
        if mapped is not None:
            status, code, message, details = mapped
            headers = {"Retry-After": "1"} if status in (429, 503) else None
            return error_response(status, code, message, details=details, headers=headers)
        # Last resort: keep the envelope, leak nothing.
        return error_response(500, "internal_error", "Internal error")


__all__ = ["envelope", "error_response", "install_error_handlers"]