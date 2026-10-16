"""统一错误信封，与 instance-app 保持相同的 JSON 形状。"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from control_plane.errors import ControlPlaneError


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ControlPlaneError)
    async def _control_plane_error(request: Request, error: ControlPlaneError) -> JSONResponse:
        headers = {}
        if error.http_status in {429, 503}:
            headers["Retry-After"] = "5"
        return JSONResponse(
            {"error": error.to_dict()}, status_code=error.http_status, headers=headers
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            {
                "error": {
                    "code": "invalid_input",
                    "message": "请求参数校验失败",
                    "details": {"errors": error.errors()},
                }
            },
            status_code=422,
        )
