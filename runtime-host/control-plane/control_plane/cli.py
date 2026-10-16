"""Command line interface for serving and operating the control plane.

Management commands are HTTP clients of a running control plane; they never
touch the database or import server internals. Configuration is TOML-only and
delegated to the worker entry point ``ControlPlaneConfig.from_env``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence, TextIO

import httpx

from control_plane.config import ControlPlaneConfig
from control_plane.errors import ConfigError

#: Environment entry point naming the TOML config file.
CONFIG_ENV = "CONTROL_PLANE_CONFIG"
DEFAULT_URL = "http://127.0.0.1:8090"


class CLIError(Exception):
    """An expected CLI or HTTP failure."""


@dataclass
class HTTPError(CLIError):
    status_code: int
    detail: str

    def __str__(self) -> str:
        return f"HTTP {self.status_code}: {self.detail}" if self.detail else f"HTTP {self.status_code}"


def resolve_config_path(explicit: str | None, environ: Mapping[str, str]) -> str | None:
    """Prefer an explicit ``--config`` over the ``CONTROL_PLANE_CONFIG`` entry."""

    if explicit:
        return explicit
    value = environ.get(CONFIG_ENV)
    return value or None


def load_config(
    config_path: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> ControlPlaneConfig:
    """Build worker configuration through the agreed ``from_env`` entry point.

    The CLI deliberately does not parse config files itself: the worker owns
    the TOML schema and reads ``config_path``. Shell environment values stay in
    ``environ`` so they keep precedence over file values inside the worker.
    """

    values = dict(os.environ if environ is None else environ)
    if config_path:
        return ControlPlaneConfig.from_env(values, config_path=config_path)
    return ControlPlaneConfig.from_env(values)


class ControlPlaneHTTP:
    """Small injectable HTTP client used by all management commands."""

    def __init__(
        self,
        base_url: str,
        admin_token: str = "",
        user_id: str = "",
        user_token: str = "",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        headers: dict[str, str] = {}
        # Credentials travel only in headers; never in the URL or logs.
        if admin_token:
            headers["X-Admin-Token"] = admin_token
        if user_id:
            headers["X-User-ID"] = user_id
        if user_token:
            headers["X-User-Token"] = user_token
        self._client = httpx.Client(
            base_url=self.base_url, headers=headers, transport=transport, trust_env=False
        )

    def close(self) -> None:
        self._client.close()

    def request(self, method: str, path: str, *, payload: Any = None) -> Any:
        try:
            response = self._client.request(method, path, json=payload)
        except httpx.HTTPError as exc:
            raise CLIError(f"request failed: {exc}") from exc
        if not 200 <= response.status_code < 300:
            raise HTTPError(response.status_code, _error_detail(response))
        # Any 2xx is success. Emit whatever JSON the server returned; a body-less
        # response (204, or a 202 with no payload) becomes a minimal ok marker.
        if response.status_code == 204 or not response.content:
            return {"status": "ok"}
        try:
            return response.json()
        except ValueError:
            return {"status": "ok", "body": response.text}


def _error_detail(response: httpx.Response) -> str:
    """Extract a short error message without echoing request credentials."""

    try:
        body = response.json()
    except ValueError:
        return response.text.strip()[:500]
    if isinstance(body, dict):
        for key in ("detail", "error", "message"):
            value = body.get(key)
            if isinstance(value, dict):
                value = value.get("message")
            if value:
                return str(value)[:500]
        return ""
    return str(body)[:500]


def _json_print(value: Any, stdout: TextIO) -> None:
    stdout.write(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TutorLM control-plane CLI")
    parser.add_argument("--url", default=DEFAULT_URL, help="control-plane base URL")
    parser.add_argument("--config", help="path to a TOML config file")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--log-level", default="info")
    parser.add_argument("--no-startup-tasks", action="store_true")
    sub = parser.add_subparsers(dest="command")

    def connection_options(command: argparse.ArgumentParser) -> None:
        # SUPPRESS keeps a bare global option authoritative while allowing the
        # same flag after the subcommand to override it.
        command.add_argument("--url", default=argparse.SUPPRESS)
        command.add_argument("--config", default=argparse.SUPPRESS)

    serve = sub.add_parser("serve", help="run the control-plane server")
    serve.add_argument("--host", default=argparse.SUPPRESS)
    serve.add_argument("--port", type=int, default=argparse.SUPPRESS)
    serve.add_argument("--log-level", default=argparse.SUPPRESS)
    serve.add_argument("--no-startup-tasks", action="store_true", default=argparse.SUPPRESS)
    serve.add_argument("--config", default=argparse.SUPPRESS)

    status = sub.add_parser("status", help="show health and capacity")
    connection_options(status)

    slots = sub.add_parser("slots", help="operate on runtime slots")
    connection_options(slots)
    slots_sub = slots.add_subparsers(dest="slots_command", required=True)
    slots_list = slots_sub.add_parser("list")
    connection_options(slots_list)
    for action in ("reclaim", "drain", "release-quarantine"):
        command = slots_sub.add_parser(action)
        connection_options(command)
        command.add_argument("slot_id", type=int)

    leases = sub.add_parser("leases", help="list live leases")
    connection_options(leases)
    leases.add_argument("--limit", type=int, default=50)

    events = sub.add_parser("events", help="list recent events")
    connection_options(events)
    events.add_argument("--limit", type=int, default=50)

    for name in ("reconcile", "gc", "config"):
        command = sub.add_parser(name)
        connection_options(command)

    invitations = sub.add_parser("invitations", help="manage system invitation codes")
    connection_options(invitations)
    invitation_sub = invitations.add_subparsers(dest="invitations_command", required=True)
    invitation_list = invitation_sub.add_parser("list")
    connection_options(invitation_list)
    invitation_create = invitation_sub.add_parser("create")
    connection_options(invitation_create)
    invitation_create.add_argument("--label", default="")
    invitation_delete = invitation_sub.add_parser("delete")
    connection_options(invitation_delete)
    invitation_delete.add_argument("invitation_id")

    shares = sub.add_parser("shares", help="workspace sharing and import")
    connection_options(shares)
    shares_sub = shares.add_subparsers(dest="shares_command", required=True)
    share_create = shares_sub.add_parser("create")
    connection_options(share_create)
    share_create.add_argument("workspace_id")
    share_create.add_argument("--label", default=None)
    share_revoke = shares_sub.add_parser("revoke")
    connection_options(share_revoke)
    share_revoke.add_argument("workspace_id")
    share_revoke.add_argument("invitation_id")
    share_import = shares_sub.add_parser("import")
    connection_options(share_import)
    share_import.add_argument("code")
    share_import.add_argument("--title")

    user = sub.add_parser("user", help="anonymous persistent identity")
    connection_options(user)
    user_sub = user.add_subparsers(dest="user_command", required=True)
    user_register = user_sub.add_parser("register")
    connection_options(user_register)
    user_login = user_sub.add_parser("login")
    connection_options(user_login)
    user_login.add_argument("--user-id", required=True)
    user_login.add_argument("--recovery-token", required=True)
    return parser


def _default_serve(
    config: ControlPlaneConfig,
    *,
    host: str,
    port: int,
    log_level: str,
    run_startup_tasks: bool,
) -> int:
    import logging

    import uvicorn

    from control_plane.api.app import create_app

    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    app = create_app(config, run_startup_tasks=run_startup_tasks)
    uvicorn.run(app, host=host, port=port, log_level=log_level)
    return 0


def _run_management(args: argparse.Namespace, client: ControlPlaneHTTP) -> Any:
    command = args.command
    if command == "status":
        return {
            "health": client.request("GET", "/control/health"),
            "capacity": client.request("GET", "/control/capacity"),
        }
    if command == "slots":
        if args.slots_command == "list":
            return client.request("GET", "/control/admin/slots")
        return client.request("POST", f"/control/admin/slots/{args.slot_id}/{args.slots_command}")
    if command == "leases":
        return client.request("GET", f"/control/admin/leases?limit={args.limit}")
    if command == "events":
        return client.request("GET", f"/control/admin/events?limit={args.limit}")
    if command in {"reconcile", "gc"}:
        return client.request("POST", f"/control/admin/{command}")
    if command == "config":
        return client.request("GET", "/control/admin/config")
    if command == "invitations":
        if args.invitations_command == "list":
            return client.request("GET", "/control/admin/invitations")
        if args.invitations_command == "create":
            return client.request("POST", "/control/admin/invitations", payload={"label": args.label})
        return client.request("DELETE", f"/control/admin/invitations/{args.invitation_id}")
    if command == "shares":
        if args.shares_command == "create":
            payload: dict[str, Any] = {}
            if args.label is not None:
                payload["label"] = args.label
            return client.request("POST", f"/control/workspaces/{args.workspace_id}/invitations", payload=payload)
        if args.shares_command == "revoke":
            return client.request("DELETE", f"/control/workspaces/{args.workspace_id}/invitations/{args.invitation_id}")
        payload = {"invitation_code": args.code}
        if args.title is not None:
            payload["title"] = args.title
        return client.request("POST", "/control/workspace-imports", payload=payload)
    if command == "user":
        if args.user_command == "register":
            return client.request("POST", "/control/users")
        return client.request(
            "POST",
            "/control/users/session",
            payload={"user_id": args.user_id, "recovery_token": args.recovery_token},
        )
    raise CLIError("a command is required")


def main(
    argv: Sequence[str] | None = None,
    *,
    client: ControlPlaneHTTP | None = None,
    environ: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    config_loader: Callable[[str | None, Mapping[str, str] | None], ControlPlaneConfig] | None = None,
    serve_runner: Callable[..., int] | None = None,
) -> int:
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    config_loader = config_loader or load_config
    serve_runner = serve_runner or _default_serve

    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    values = dict(os.environ if environ is None else environ)
    config_path = resolve_config_path(getattr(args, "config", None), values)

    serving = args.command in (None, "serve")
    if serving or client is None:
        try:
            config = config_loader(config_path, values)
        except (ConfigError, OSError, UnicodeError):
            # Parser/validation errors can contain credential values or source
            # lines. Never echo them, including for server startup failures.
            stderr.write(
                "error: configuration could not be loaded; "
                "check TOML and CONTROL_PLANE_* settings\n"
            )
            return 1

    if serving:
        return serve_runner(
            config,
            host=args.host,
            port=args.port,
            log_level=args.log_level,
            run_startup_tasks=not args.no_startup_tasks,
        )

    own_client = client is None
    http = client
    try:
        if http is None:
            http = ControlPlaneHTTP(
                getattr(args, "url", DEFAULT_URL),
                admin_token=config.admin_token,
                user_id=values.get("TUTORLM_USER_ID", ""),
                user_token=values.get("TUTORLM_USER_TOKEN", ""),
            )
        _json_print(_run_management(args, http), stdout)
        return 0
    except (CLIError, ValueError) as exc:
        stderr.write(f"error: {exc}\n")
        return 1
    finally:
        if own_client and http is not None:
            http.close()
