"""CLI tests: mock transport only.

Every test drives ``control_plane.cli`` through an injected ``httpx`` transport
or injected collaborators. No test contacts a real control plane, remote host
or the worker database.
"""

from __future__ import annotations

import io
import json

import httpx
import pytest

from control_plane import cli
from control_plane.cli import (
    CONFIG_ENV,
    ControlPlaneHTTP,
    HTTPError,
    load_config,
    main,
    resolve_config_path,
)


def json_response(request: httpx.Request, status: int, body):
    if body is None:
        return httpx.Response(status, request=request)
    return httpx.Response(status, json=body, request=request)


def mock_client(routes, **kwargs) -> ControlPlaneHTTP:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        for (method, path), result in routes.items():
            if request.method == method and request.url.path == path:
                if callable(result):
                    return result(request)
                status, body = result
                return json_response(request, status, body)
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    client = ControlPlaneHTTP("http://cp.test", transport=httpx.MockTransport(handler), **kwargs)
    client.seen = seen  # type: ignore[attr-defined]
    return client


def run(argv, client, environ=None):
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, client=client, environ=environ or {}, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


# --- serve / legacy compatibility -------------------------------------------


def test_bare_invocation_still_starts_server_with_legacy_flags():
    calls = {}

    def serve_runner(config, *, host, port, log_level, run_startup_tasks):
        calls.update(config=config, host=host, port=port, log_level=log_level, run_startup_tasks=run_startup_tasks)
        return 0

    sentinel = object()
    code = main(
        ["--host", "0.0.0.0", "--port", "9000", "--log-level", "debug", "--no-startup-tasks"],
        environ={},
        config_loader=lambda path, env: sentinel,
        serve_runner=serve_runner,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )
    assert code == 0
    assert calls == {
        "config": sentinel,
        "host": "0.0.0.0",
        "port": 9000,
        "log_level": "debug",
        "run_startup_tasks": False,
    }


def test_explicit_serve_subcommand_accepts_config():
    calls = {}

    def serve_runner(config, *, host, port, log_level, run_startup_tasks):
        calls.update(config=config, host=host, port=port, log_level=log_level)
        return 0

    code = main(
        ["serve", "--config", "cp.toml", "--port", "8123"],
        environ={},
        config_loader=lambda path, env: ("loaded", path, env),
        serve_runner=serve_runner,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )
    assert code == 0
    assert calls["config"] == ("loaded", "cp.toml", {})
    assert calls["host"] == "127.0.0.1"
    assert calls["port"] == 8123


def test_serve_config_resolved_from_control_plane_config_env():
    seen = {}

    def serve_runner(config, **kwargs):
        seen["config"] = config
        return 0

    main(
        ["serve"],
        environ={CONFIG_ENV: "from-env.toml"},
        config_loader=lambda path, env: ("loaded", path),
        serve_runner=serve_runner,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )
    assert seen["config"] == ("loaded", "from-env.toml")


# --- configuration entry point ----------------------------------------------


def test_resolve_config_path_prefers_explicit_over_env():
    assert resolve_config_path("explicit.toml", {CONFIG_ENV: "env.toml"}) == "explicit.toml"
    assert resolve_config_path(None, {CONFIG_ENV: "env.toml"}) == "env.toml"
    assert resolve_config_path(None, {}) is None


def test_load_config_delegates_to_from_env_with_config_path(monkeypatch):
    recorded = {}

    def fake_from_env(cls, environ=None, **overrides):
        recorded["environ"] = environ
        recorded["overrides"] = overrides
        return "CONFIG"

    monkeypatch.setattr(cli.ControlPlaneConfig, "from_env", classmethod(fake_from_env))
    # A path that does not exist on disk still works: the CLI never reads it.
    result = load_config("does-not-exist.toml", {"CONTROL_PLANE_ADMIN_TOKEN": "t"})
    assert result == "CONFIG"
    assert recorded["overrides"] == {"config_path": "does-not-exist.toml"}
    assert recorded["environ"] == {"CONTROL_PLANE_ADMIN_TOKEN": "t"}


def test_load_config_without_path_omits_config_path(monkeypatch):
    recorded = {}

    def fake_from_env(cls, environ=None, **overrides):
        recorded["overrides"] = overrides
        return "CONFIG"

    monkeypatch.setattr(cli.ControlPlaneConfig, "from_env", classmethod(fake_from_env))
    assert load_config(None, {}) == "CONFIG"
    assert recorded["overrides"] == {}


# --- management wiring ------------------------------------------------------


def test_management_builds_client_from_config_and_env(monkeypatch):
    captured = {}

    class Recorder:
        def __init__(self, base_url, admin_token="", user_id="", user_token="", transport=None):
            captured.update(base_url=base_url, admin_token=admin_token, user_id=user_id, user_token=user_token)

        def request(self, method, path, *, payload=None):
            return {"ok": True}

        def close(self):
            pass

    monkeypatch.setattr(cli, "ControlPlaneHTTP", Recorder)
    config = cli.ControlPlaneConfig(admin_token="admin-secret")
    out = io.StringIO()
    code = main(
        ["config"],
        environ={"TUTORLM_USER_ID": "u1", "TUTORLM_USER_TOKEN": "ut"},
        config_loader=lambda path, env: config,
        stdout=out,
        stderr=io.StringIO(),
    )
    assert code == 0
    assert captured == {
        "base_url": "http://127.0.0.1:8090",
        "admin_token": "admin-secret",
        "user_id": "u1",
        "user_token": "ut",
    }


def test_injected_client_skips_config_loading():
    def explode(path, env):
        raise AssertionError("config loader must not run when a client is injected")

    client = mock_client({("GET", "/control/admin/config"): (200, {"slots": 3})})
    code, out, _ = run(["config"], client=client, environ={})
    assert code == 0
    assert json.loads(out) == {"slots": 3}


# --- status / admin commands ------------------------------------------------


def test_status_combines_health_and_capacity_with_admin_header():
    client = mock_client(
        {
            ("GET", "/control/health"): (200, {"status": "ok"}),
            ("GET", "/control/capacity"): (200, {"slots_free": 2}),
        },
        admin_token="secret",
    )
    code, out, _ = run(["status"], client=client)
    assert code == 0
    assert json.loads(out) == {"health": {"status": "ok"}, "capacity": {"slots_free": 2}}
    for request in client.seen:  # type: ignore[attr-defined]
        assert request.headers.get("X-Admin-Token") == "secret"
        assert "secret" not in str(request.url)


def test_slots_leases_events_reconcile_gc_config_paths():
    routes = {
        ("GET", "/control/admin/slots"): (200, {"items": []}),
        ("POST", "/control/admin/slots/2/reclaim"): (200, {"slot": {"slot_id": 2}}),
        ("POST", "/control/admin/slots/2/drain"): (200, {"slot": {"slot_id": 2}}),
        ("POST", "/control/admin/slots/2/release-quarantine"): (200, {"slot": {"slot_id": 2}}),
        ("GET", "/control/admin/leases"): (200, {"items": []}),
        ("GET", "/control/admin/events"): (200, {"items": []}),
        ("POST", "/control/admin/reconcile"): (200, {"report": {}}),
        ("POST", "/control/admin/gc"): (200, {"report": {}}),
        ("GET", "/control/admin/config"): (200, {"slots": 3}),
    }
    client = mock_client(routes)
    for argv in (
        ["slots", "list"],
        ["slots", "reclaim", "2"],
        ["slots", "drain", "2"],
        ["slots", "release-quarantine", "2"],
        ["leases"],
        ["events"],
        ["reconcile"],
        ["gc"],
        ["config"],
    ):
        code, _, err = run(argv, client=client)
        assert code == 0, (argv, err)


def test_leases_and_events_pass_limit_query():
    client = mock_client(
        {
            ("GET", "/control/admin/leases"): (200, {"items": []}),
            ("GET", "/control/admin/events"): (200, {"items": []}),
        }
    )
    run(["leases", "--limit", "7"], client=client)
    run(["events", "--limit", "9"], client=client)
    urls = [str(r.url) for r in client.seen]  # type: ignore[attr-defined]
    assert any("limit=7" in u for u in urls)
    assert any("limit=9" in u for u in urls)


# --- invitations ------------------------------------------------------------


def test_invitations_list_create_delete():
    captured = {}

    def create(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            201,
            json={"invitation": {"invitation_id": "i1"}, "code": "TLM-SYS-ABCD"},
            request=request,
        )

    client = mock_client(
        {
            ("GET", "/control/admin/invitations"): (200, {"items": [{"invitation_id": "i1"}]}),
            ("POST", "/control/admin/invitations"): create,
            ("DELETE", "/control/admin/invitations/i1"): (204, None),
        },
        admin_token="secret",
    )
    code, out, _ = run(["invitations", "list"], client=client)
    assert code == 0 and json.loads(out)["items"][0]["invitation_id"] == "i1"

    code, out, _ = run(["invitations", "create", "--label", "demo"], client=client)
    assert code == 0
    assert captured["body"] == {"label": "demo"}
    assert out.count("TLM-SYS-ABCD") == 1  # plaintext code shown exactly once

    code, out, _ = run(["invitations", "delete", "i1"], client=client)
    assert code == 0
    assert json.loads(out) == {"status": "ok"}


# --- shares -----------------------------------------------------------------


def test_shares_create_omits_label_when_absent_and_sends_when_present():
    bodies = []

    def create(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={"invitation": {"invitation_id": "s1"}, "code": "TLM-SHARE-1"}, request=request)

    client = mock_client(
        {
            ("POST", "/control/workspaces/w1/invitations"): create,
            ("POST", "/control/workspaces/w2/invitations"): create,
            ("DELETE", "/control/workspaces/w1/invitations/s1"): (204, None),
        },
        user_id="u1",
        user_token="ut",
    )
    assert run(["shares", "create", "w1"], client=client)[0] == 0
    assert run(["shares", "create", "w2", "--label", "spring"], client=client)[0] == 0
    assert run(["shares", "revoke", "w1", "s1"], client=client)[0] == 0
    assert bodies == [{}, {"label": "spring"}]
    for request in client.seen:  # type: ignore[attr-defined]
        assert request.headers.get("X-User-ID") == "u1"
        assert request.headers.get("X-User-Token") == "ut"


def test_shares_import_sends_code_and_optional_title():
    captured = {}

    def import_workspace(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(202, json={"workspace": {"workspace_id": "w9"}}, request=request)

    client = mock_client({("POST", "/control/workspace-imports"): import_workspace}, user_id="u1", user_token="ut")
    code, out, _ = run(["shares", "import", "CODE-1", "--title", "Imported"], client=client)
    assert code == 0
    assert captured["body"] == {"invitation_code": "CODE-1", "title": "Imported"}
    assert json.loads(out) == {"workspace": {"workspace_id": "w9"}}

    run(["shares", "import", "CODE-2"], client=client)
    assert captured["body"] == {"invitation_code": "CODE-2"}


def test_generic_http_output_does_not_depend_on_status_shape():
    # 200 with an unexpected body, and 201 without the documented envelope, both pass through.
    client = mock_client(
        {
            ("POST", "/control/workspace-imports"): (200, {"workspace_id": "w1", "title": "x"}),
            ("POST", "/control/admin/invitations"): (201, {"invitation_id": "i9", "code": "C"}),
        }
    )
    code, out, _ = run(["shares", "import", "CODE"], client=client)
    assert code == 0 and json.loads(out) == {"workspace_id": "w1", "title": "x"}
    code, out, _ = run(["invitations", "create"], client=client)
    assert code == 0 and json.loads(out) == {"invitation_id": "i9", "code": "C"}


# --- user commands ----------------------------------------------------------


def test_user_register_and_login():
    seen = {}

    def login(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"user": {"user_id": "u1"}}, request=request)

    client = mock_client(
        {
            ("POST", "/control/users"): (201, {"user": {"user_id": "u1"}, "recovery_token": "RT-1"}),
            ("POST", "/control/users/session"): login,
        }
    )
    code, out, _ = run(["user", "register"], client=client)
    assert code == 0 and json.loads(out)["recovery_token"] == "RT-1"
    code, _, _ = run(["user", "login", "--user-id", "u1", "--recovery-token", "RT-1"], client=client)
    assert code == 0
    assert seen["body"] == {"user_id": "u1", "recovery_token": "RT-1"}


# --- error handling ---------------------------------------------------------


def test_non_2xx_goes_to_stderr_without_echoing_credentials():
    client = mock_client(
        {("POST", "/control/users/session"): (403, {"detail": "invalid credentials"})},
        admin_token="admin-secret",
    )
    code, out, err = run(["user", "login", "--user-id", "u1", "--recovery-token", "RT-SECRET"], client=client)
    assert code == 1
    assert out == ""
    assert "HTTP 403" in err and "invalid credentials" in err
    assert "RT-SECRET" not in err
    assert "admin-secret" not in err


def test_import_error_does_not_echo_invitation_code():
    client = mock_client({("POST", "/control/workspace-imports"): (400, {"detail": "bad code"})})
    code, out, err = run(["shares", "import", "TLM-SHARE-SECRET"], client=client)
    assert code == 1
    assert out == ""
    assert "TLM-SHARE-SECRET" not in err


def test_transport_failure_is_reported_as_cli_error():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    client = ControlPlaneHTTP("http://cp.test", transport=httpx.MockTransport(handler))
    code, out, err = run(["status"], client=client)
    assert code == 1
    assert out == ""
    assert "request failed" in err


def test_http_error_helper_parses_error_envelope():
    request = httpx.Request("GET", "http://cp.test/x")
    response = httpx.Response(400, json={"error": {"message": "nope"}}, request=request)
    assert cli._error_detail(response) == "nope"


@pytest.mark.parametrize("command", [["serve"], ["config"], ["shares", "import", "CODE"], ["status"]])
def test_config_loader_type_error_is_not_hidden(command):
    def rejecting(path, env):
        raise TypeError("configuration loader programming error")

    err = io.StringIO()
    with pytest.raises(TypeError, match="programming error"):
        main(
            [*command, "--config", "cp.toml"],
            environ={},
            config_loader=rejecting,
            serve_runner=lambda *a, **k: 0,
            stdout=io.StringIO(),
            stderr=err,
        )
    assert err.getvalue() == ""


@pytest.mark.parametrize(
    "command", [[], ["serve"], ["config"], ["invitations", "list"], ["shares", "import", "submitted-code"], ["status"]]
)
@pytest.mark.parametrize("failure", ["malformed", "duplicate", "missing"])
def test_config_failures_are_safe_for_every_command(command, failure, tmp_path, monkeypatch, capsys, caplog):
    from control_plane.invitations import generate_system_code

    invitation_code = generate_system_code()
    secrets = [invitation_code, "admin-config-secret", "pepper-config-secret", "submitted-code"]
    path = tmp_path / "control-plane.toml"
    text = (
        '[control_plane]\nadmin_token = "admin-config-secret"\n'
        'token_pepper = "pepper-config-secret"\n'
        f"system_invitation_codes = {json.dumps([invitation_code, invitation_code])}\n"
    )
    if failure == "malformed":
        text += 'broken = ["admin-config-secret"\n'
    if failure != "missing":
        path.write_text(text, encoding="utf-8")

    def unexpected(*args, **kwargs):
        pytest.fail("invalid configuration must fail before starting HTTP or serving")

    monkeypatch.setattr(cli, "ControlPlaneHTTP", unexpected)
    out, err = io.StringIO(), io.StringIO()
    result = main(
        [*command, "--config", str(path)],
        environ={"CONTROL_PLANE_ADMIN_TOKEN": secrets[1], "CONTROL_PLANE_TOKEN_PEPPER": secrets[2]},
        serve_runner=unexpected,
        stdout=out,
        stderr=err,
    )
    assert result == 1
    assert out.getvalue() == ""
    assert "configuration could not be loaded" in err.getvalue()
    captured = capsys.readouterr()
    diagnostics = out.getvalue() + err.getvalue() + captured.out + captured.err + caplog.text
    assert "Traceback" not in diagnostics
    assert all(secret not in diagnostics for secret in secrets)


@pytest.mark.parametrize("command", [["serve"], ["config"], ["shares", "import", "CODE"], ["status"]])
@pytest.mark.parametrize("failure_type", [PermissionError, UnicodeDecodeError])
def test_config_io_errors_are_safe(command, failure_type):
    def rejecting(path, env):
        if failure_type is UnicodeDecodeError:
            raise UnicodeDecodeError("utf-8", b"secret", 0, 1, "private-config-value")
        raise PermissionError("private-config-value")

    out, err = io.StringIO(), io.StringIO()
    assert main(
        command, environ={}, config_loader=rejecting, stdout=out, stderr=err
    ) == 1
    assert out.getvalue() == ""
    assert "configuration could not be loaded" in err.getvalue()
    assert "private-config-value" not in err.getvalue()


def test_missing_positional_argument_exits_two():
    with pytest.raises(SystemExit) as excinfo:
        main(["slots", "reclaim"], client=mock_client({}), stdout=io.StringIO(), stderr=io.StringIO())
    assert excinfo.value.code == 2


def test_http_error_dataclass_message():
    assert str(HTTPError(403, "forbidden")) == "HTTP 403: forbidden"
    assert str(HTTPError(500, "")) == "HTTP 500"


# --- real HTTP against a loopback fake control plane ------------------------


def _start_fake_control_plane():
    """Start a real loopback HTTP server; skip the test if sockets are denied."""

    try:
        import threading
        import time

        import uvicorn
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse, Response
        from starlette.routing import Route
    except Exception:  # pragma: no cover - environment without uvicorn
        pytest.skip("uvicorn/starlette unavailable")

    async def create_invitation(request):
        return JSONResponse(
            {"invitation": {"invitation_id": "inv-1"}, "code": "TLM-SYS-REAL"},
            status_code=201,
        )

    async def delete_invitation(request):
        invitation_id = request.path_params["invitation_id"]
        if invitation_id != "inv-1":
            return JSONResponse({"detail": "unknown"}, status_code=404)
        return Response(status_code=204)

    async def import_workspace(request):
        return JSONResponse({"workspace": {"workspace_id": "ws-1"}}, status_code=202)

    app = Starlette(
        routes=[
            Route("/control/admin/invitations", create_invitation, methods=["POST"]),
            Route("/control/admin/invitations/{invitation_id}", delete_invitation, methods=["DELETE"]),
            Route("/control/workspace-imports", import_workspace, methods=["POST"]),
        ]
    )
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    try:
        thread.start()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"cannot start loopback server: {exc}")
    for _ in range(200):
        if server.started and server.servers and server.servers[0].sockets:
            break
        time.sleep(0.02)
    else:  # pragma: no cover
        pytest.skip("loopback server did not start in time")
    port = server.servers[0].sockets[0].getsockname()[1]
    return server, f"http://127.0.0.1:{port}"


def test_real_http_invitation_and_share_commands():
    """Drive the real ``ControlPlaneHTTP`` over a loopback socket, not a mock."""

    server, url = _start_fake_control_plane()
    try:
        env = {"CONTROL_PLANE_ADMIN_TOKEN": "admin-tok"}

        code, out, err = run(["--url", url, "invitations", "create", "--label", "demo"], client=None, environ=env)
        assert code == 0, err
        body = json.loads(out)
        assert body["invitation"]["invitation_id"] == "inv-1"
        assert body["code"] == "TLM-SYS-REAL"
        assert out.count("TLM-SYS-REAL") == 1

        code, out, err = run(["invitations", "delete", "inv-1", "--url", url], client=None, environ=env)
        assert code == 0, err
        assert json.loads(out) == {"status": "ok"}

        code, out, err = run(["shares", "import", "CODE-1", "--title", "T", "--url", url], client=None, environ=env)
        assert code == 0, err
        assert json.loads(out) == {"workspace": {"workspace_id": "ws-1"}}
    finally:
        server.should_exit = True


def test_real_http_non_2xx_exits_nonzero_without_token_leak():
    server, url = _start_fake_control_plane()
    try:
        # A crash-on-config loader must NOT run: a real client is built for HTTP.
        code, out, err = run(
            ["invitations", "delete", "missing", "--url", url],
            client=None,
            environ={"CONTROL_PLANE_ADMIN_TOKEN": "admin-tok"},
        )
        assert code == 1
        assert out == ""
        assert "HTTP 404" in err
        assert "admin-tok" not in err
    finally:
        server.should_exit = True


def test_config_never_parses_dotenv_or_mutates_process_env(monkeypatch, tmp_path):
    """``--config`` is handed to the worker; the CLI parses nothing itself."""

    import os as _os

    env_file = tmp_path / "cp.env"
    env_file.write_text("CONTROL_PLANE_ADMIN_TOKEN=SHOULD-NOT-LOAD\n", encoding="utf-8")
    monkeypatch.delenv("CONTROL_PLANE_ADMIN_TOKEN", raising=False)

    received = []

    def recorder(path, env):
        received.append(path)
        return cli.ControlPlaneConfig(admin_token="from-worker")

    code = main(
        ["serve", "--config", str(env_file)],
        environ={},
        config_loader=recorder,
        serve_runner=lambda *a, **k: 0,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )
    assert code == 0
    # The path is passed through untouched; the CLI never read the file.
    assert received == [str(env_file)]
    assert "CONTROL_PLANE_ADMIN_TOKEN" not in _os.environ


def test_user_login_error_never_echoes_recovery_token():
    client = mock_client({("POST", "/control/users/session"): (401, {"detail": "invalid credentials"})})
    code, out, err = run(["user", "login", "--user-id", "u1", "--recovery-token", "RT-SECRET"], client=client)
    assert code == 1
    assert out == ""
    assert "RT-SECRET" not in err


@pytest.mark.parametrize("listening_proxy", [False, True], ids=["dead-proxy", "recording-proxy"])
def test_status_bypasses_ambient_proxies_without_leaking_admin_header(monkeypatch, listening_proxy):
    import socket
    import threading
    from contextlib import ExitStack
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    origin_requests = []
    proxy_requests = []

    class OriginHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            origin_requests.append((self.path, self.headers.get("X-Admin-Token")))
            body = json.dumps({"status": "ok"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    class ProxyHandler(OriginHandler):
        def do_GET(self):
            proxy_requests.append((self.path, dict(self.headers)))
            self.send_error(502)

    def start_server(stack, handler):
        server = stack.enter_context(ThreadingHTTPServer(("127.0.0.1", 0), handler))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        # Shut down before joining, then close the listener.
        stack.callback(thread.join, 5)
        stack.callback(server.shutdown)
        return server.server_address[1]

    with ExitStack() as stack:
        origin_port = start_server(stack, OriginHandler)
        if listening_proxy:
            proxy_port = start_server(stack, ProxyHandler)
        else:
            # Reserve a port without listening: deterministic connection refusal.
            dead_proxy = stack.enter_context(socket.socket())
            dead_proxy.bind(("127.0.0.1", 0))
            proxy_port = dead_proxy.getsockname()[1]
        proxy_url = f"http://127.0.0.1:{proxy_port}"
        for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
            monkeypatch.setenv(name, proxy_url)
        for name in ("NO_PROXY", "no_proxy"):
            monkeypatch.setenv(name, "")

        code, out, err = run(
            ["status", "--url", f"http://127.0.0.1:{origin_port}"],
            client=None,
            environ={"CONTROL_PLANE_ADMIN_TOKEN": "proxy-regression-secret"},
        )

    assert code == 0, err
    assert json.loads(out) == {"health": {"status": "ok"}, "capacity": {"status": "ok"}}
    assert origin_requests == [
        ("/control/health", "proxy-regression-secret"),
        ("/control/capacity", "proxy-regression-secret"),
    ]
    assert proxy_requests == []
    assert "proxy-regression-secret" not in out + err
