"""统一错误信封，与 instance-app 保持相同的 JSON 形状。

这里有一个**安全约束**：校验失败不能回显输入值。FastAPI 默认吧
``input`` 也写进错误安全结构，而请求体里可能包含邀请码、恢复凭据等敏感字段。
所以这里只保留安全的 ``loc``/``type``/``msg``/约束字段，并将 ``msg`` 中的
原始值替换为占位符。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from control_plane.errors import ControlPlaneError

#: 这些字段的值绝不能出现在响应里（即使在 loc 里也只保留名字）。
SENSITIVE_FIELDS = frozenset(
    {"invitation_code", "recovery_token", "token", "password", "secret", "authorization"}
)

#: 只保留这些安全字段；``input``/``url``/``ctx`` 一律丢弃。
_SAFE_KEYS = ("type", "loc", "msg")


def _safe_location(loc: Any) -> list[Any]:
    if not isinstance(loc, (list, tuple)):
        return [str(loc)]
    return [str(part) for part in loc]


def sanitize_validation_errors(raw_errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 FastAPI/ Pydantic 的 validation error 剥成不含输入值的安全形式。"""

    safe: list[dict[str, Any]] = []
    for error in raw_errors:
        item: dict[str, Any] = {}
        for key in _SAFE_KEYS:
            if key not in error:
                continue
            value = error[key]
            if key == "loc":
                item["loc"] = _safe_location(value)
            elif key == "msg":
                # pydantic 有时会把原值拼进 msg；只保留类型描述。
                text = str(value)
                if any(field in text for field in SENSITIVE_FIELDS):
                    text = "输入不符合约束"
                item["msg"] = text
            else:
                item[key] = str(value)
        safe.append(item)
    return safe


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
                    "details": {"errors": sanitize_validation_errors(list(error.errors()))},
                }
            },
            status_code=422,
        )
