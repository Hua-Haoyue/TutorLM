"""ASGI application assembly (HTTP only).

Responsibilities kept deliberately narrow: build the Host graph, mount the
routers, install the single error envelope, add safety headers and provide the
``/api/*`` 404 fallback. No business rule, no storage access and no identity
decision lives in this module (``docs/instance-app-design.md`` sections 2 and 6).

Notably absent by design: Docker access, control-plane imports, upstream address
selection, and any trust in client-supplied owner/workspace headers.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio

from fastapi import FastAPI, Request, APIRouter, Depends
from fastapi.responses import JSONResponse, Response

from backend.host import errors as app_errors
from backend.host.config import HostConfig
from backend.host.state import AppState, build_state

from .errors import error_response, install_error_handlers
from .routers import documents, graph, health, learning, projects, runs, runtime, sessions, sources
from .dependencies import require_session
from backend.host.context import PluginHostContext

#: Sent on every response: the app is same-origin behind the gateway and must not
#: be framed, sniffed or allowed to leak a referrer.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def build_app(state: AppState) -> FastAPI:
    """Assemble the ASGI app around an already-built :class:`AppState`.

    Split from ``create_app`` so tests can construct a fully isolated Host graph
    (tmp dirs, fake provider) and pass it in.
    """
    @asynccontextmanager
    async def lifespan(app):
        started = []
        try:
            for plugin_id, instance in state.plugins.instances():
                context = PluginHostContext(state.plugins.get(plugin_id).manifest)
                started.append((instance, context))
                instance.register_services(context)
                await instance.startup(context)
            yield
        finally:
            try:
                for instance, context in reversed(started):
                    try:
                        await instance.shutdown(context)
                    except Exception:
                        import logging
                        logging.getLogger(__name__).error('Plugin shutdown failed')
            finally:
                await asyncio.to_thread(state.runs.shutdown, wait=True)
                close_provider = getattr(state.services.provider, 'close', None)
                if close_provider:
                    close_provider()

    app = FastAPI(
        title="TutorLM instance application",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.tutorlm = state

    install_error_handlers(app)

    @app.middleware("http")
    async def _security_headers(request: Request, call_next) -> Response:
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    app.include_router(health.router)
    app.include_router(runtime.router)
    app.include_router(sessions.router)
    app.include_router(projects.router)
    app.include_router(documents.router)
    app.include_router(graph.router)
    app.include_router(runs.router)
    app.include_router(sources.router)
    app.include_router(learning.router)
    for plugin_id, instance in state.plugins.instances():
        scoped_router = APIRouter(prefix=f'/api/plugins/{plugin_id}', dependencies=[Depends(require_session)])
        instance.register_routes(scoped_router, PluginHostContext(state.plugins.get(plugin_id).manifest))
        app.include_router(scoped_router)

    @app.exception_handler(404)
    async def _api_fallback(request: Request, _exc) -> Response:  # pragma: no cover - registered handler
        return JSONResponse(status_code=404, content={"error": {"code": "not_found", "message": "Unknown API route"}})

    @app.get("/api/{unmatched:path}", include_in_schema=False)
    async def _unmatched_api(unmatched: str) -> Response:
        """Distinguish "this API does not exist" from an SPA route.

        The gateway needs a JSON 404 for unknown ``/api/*`` paths so it does not
        serve ``index.html`` into a fetch() call.
        """
        return error_response(
            404,
            "not_found",
            "Unknown API route",
            details={"path": f"/api/{unmatched}"},
        )

    @app.post("/api/shares/{code}/redeem")
    async def _redeem_not_supported(code: str) -> Response:
        """Share redemption belongs to the control plane (explicit 410, not 404).

        An explicit signal lets the frontend show a migration hint instead of a
        generic failure; the application never touches Docker or the outer
        database to implement this.
        """
        raise app_errors.ShareRedeemNotSupported(
            "Share redemption moved to the control plane; call "
            "POST /control/workspace-imports through the gateway",
            details={"deprecated": True, "replacement": "POST /control/workspace-imports"},
        )

    return app


def create_app(config: HostConfig | None = None, **state_kwargs) -> FastAPI:
    """Build config, assemble the Host graph and return the ASGI app.

    ``state_kwargs`` are forwarded to :func:`build_state` (``store``, ``storage``,
    ``sessions``, ``runs``, ``plugins``, ``provider``, ``initialize``), which is
    what lets tests inject fakes without touching the routers.
    """
    resolved = config or HostConfig.from_env()
    state = build_state(resolved, **state_kwargs)
    return build_app(state)


__all__ = ["SECURITY_HEADERS", "build_app", "create_app"]
