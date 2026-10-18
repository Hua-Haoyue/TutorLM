"""Verifier-owned black-box acceptance tests for the invitation / preemption /
share-copy work.

These tests drive real code paths, not simulations:

* the real FastAPI app built by ``control_plane.api.app.create_app`` (in-process
  ASGI via Starlette's ``TestClient``), and
* the real CLI entry point ``control_plane.cli.main`` talking to a live loopback
  uvicorn server over real HTTP.

Nothing here presents a mock transport as the core API. Where a helper seam is
required (declaring an app-side ``content_only`` layout, which the in-container
app has not shipped yet) the test says so explicitly.

Docker is *never* exercised here. The real-adapter integration suite is opt-in
via ``CONTROL_PLANE_DOCKER_IT=1`` and needs a local Docker daemon plus matching
local images; this host has no Docker CLI, so those tests remain skipped.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from control_plane import cli as cli_module
from control_plane.api.app import build_components, create_app
from control_plane.config import (
    ControlPlaneConfig,
    is_valid_system_invitation_code,
)
from control_plane.domain.models import STORAGE_LAYOUT_CONTENT_ONLY
from control_plane.errors import ConfigError
from control_plane.invitations import SYSTEM_CODE_RE, InvitationService, AccessPolicy
from control_plane.ports import VolumeSpec
from control_plane.runtime.fake import FakeRuntimeAdapter
from control_plane.sharing import SESSION_SUBTREES, ShareService

ADMIN = {"X-Admin-Token": "test-admin-token"}


# --------------------------------------------------------------------------- #
# config: defaults < file < environment < explicit
# --------------------------------------------------------------------------- #


def _write_toml(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_toml_precedence_default_file_env_explicit(tmp_path: Path):
    toml = _write_toml(
        tmp_path / "cp.toml",
        "[control_plane]\nslots = 4\ninstance_image = \"from-file\"\n",
    )
    # file only
    from_file = ControlPlaneConfig.from_env({}, config_path=str(toml))
    assert from_file.slots == 4 and from_file.instance_image == "from-file"
    # defaults still apply where the file is silent
    assert from_file.lease_ttl_seconds == 3600
    # environment wins over file
    from_envv = ControlPlaneConfig.from_env(
        {"CONTROL_PLANE_SLOTS": "7"}, config_path=str(toml)
    )
    assert from_envv.slots == 7 and from_envv.instance_image == "from-file"
    # explicit override wins over environment
    explicit = ControlPlaneConfig.from_env(
        {"CONTROL_PLANE_SLOTS": "7"}, config_path=str(toml), slots=9
    )
    assert explicit.slots == 9


def test_config_env_var_points_at_file_and_missing_file_is_a_clean_error(tmp_path: Path):
    toml = _write_toml(tmp_path / "cp.toml", "[control_plane]\nslots = 6\n")
    via_env = ControlPlaneConfig.from_env({"CONTROL_PLANE_CONFIG": str(toml)})
    assert via_env.slots == 6
    with pytest.raises(ConfigError):
        ControlPlaneConfig.from_env(
            {"CONTROL_PLANE_CONFIG": str(tmp_path / "does-not-exist.toml")}
        )


def test_invitation_only_root_and_table_spellings_and_conflict(tmp_path: Path):
    root = ControlPlaneConfig.from_env(
        {}, config_path=str(_write_toml(tmp_path / "root.toml", "invitationonly = true\n"))
    )
    assert root.invitation_only is True

    table = ControlPlaneConfig.from_env(
        {},
        config_path=str(_write_toml(tmp_path / "t.toml", "[control_plane]\ninvitationonly = true\n")),
    )
    assert table.invitation_only is True

    plain = ControlPlaneConfig.from_env(
        {}, config_path=str(_write_toml(tmp_path / "f.toml", "invitationonly = false\n"))
    )
    assert plain.invitation_only is False

    conflict = _write_toml(
        tmp_path / "bad.toml",
        "invitationonly = true\n[control_plane]\ninvitationonly = false\n",
    )
    with pytest.raises(ConfigError):
        ControlPlaneConfig.from_env({}, config_path=str(conflict))

    # a non-boolean switch must fail loudly, not silently read as "off"
    wrong = _write_toml(tmp_path / "wrong.toml", "invitationonly = \"yes\"\n")
    with pytest.raises(ConfigError):
        ControlPlaneConfig.from_env({}, config_path=str(wrong))


# --------------------------------------------------------------------------- #
# system invitation code format
# --------------------------------------------------------------------------- #


def test_generated_system_code_is_256_bit_and_accepted_by_config(tmp_path: Path):
    config = ControlPlaneConfig.for_tests(tmp_path)
    service = InvitationService(
        build_components(config)["store"],
        pepper="p",
        policy=AccessPolicy(invitation_only=True),
    )
    try:
        _invitation, code = service.issue_system_code(label="t")
    finally:
        build_components  # keep the helper import obviously used
    assert SYSTEM_CODE_RE.match(code), code
    assert code.startswith("TLM-SYS-v1-")
    payload = code[len("TLM-SYS-v1-"):]
    assert len(payload) == 43, payload  # 32 bytes base64url == 256 bit
    assert is_valid_system_invitation_code(code)


# --------------------------------------------------------------------------- #
# CLI HTTP client must not honour ambient proxies
# --------------------------------------------------------------------------- #


def test_cli_http_client_ignores_ambient_proxy_environment(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    client = cli_module.ControlPlaneHTTP("http://127.0.0.1:1")
    try:
        assert client._client.trust_env is False, (
            "CLI must set trust_env=False so loopback admin calls never route "
            "through ambient HTTP_PROXY"
        )
    finally:
        client.close()


# --------------------------------------------------------------------------- #
# real CLI + real loopback server (no fake control plane)
# --------------------------------------------------------------------------- #


def _free_port() -> int:
    with contextlib.closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextlib.contextmanager
def _live_control_plane(tmp_path: Path):
    """Run a real control-plane over real HTTP; skip if sockets are denied."""

    import uvicorn

    port = _free_port()
    toml = _write_toml(
        tmp_path / "serve.toml",
        "[control_plane]\n"
        "slots = 2\n"
        "adapter = \"fake\"\n"
        "admin_token = \"test-admin-token\"\n"
        "token_pepper = \"test-pepper\"\n"
        f"db_path = {(tmp_path / 'serve.sqlite3').as_posix()!r}\n"
        "gc_enabled = false\n"
        "instance_image = \"tutorlm-host:test\"\n"
        "ready_timeout_seconds = 2.0\n"
        "ready_poll_interval_seconds = 0.01\n",
    )
    config = ControlPlaneConfig.from_env({}, config_path=str(toml))
    app = create_app(config, adapter=FakeRuntimeAdapter(), run_startup_tasks=False)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = threading.Event()
    for _ in range(200):
        if getattr(server, "started", False):
            break
        if deadline.wait(0.05):
            break
    else:  # pragma: no cover - defensive
        server.should_exit = True
        thread.join(timeout=5)
        pytest.skip("loopback server did not start")
    try:
        yield f"http://127.0.0.1:{port}", config
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_real_cli_manages_system_invitation_codes_end_to_end(tmp_path: Path):
    with _live_control_plane(tmp_path) as (url, config):
        def run(args):
            out, err = [], []
            code = cli_module.main(
                ["--config", str(config.db_path.parent / "serve.toml"), "--url", url, *args],
                environ={},
                stdout=_Collector(out),
                stderr=_Collector(err),
            )
            return code, "".join(out), "".join(err)

        rc, out, err = run(["invitations", "create", "--label", "acceptance"])
        assert rc == 0, err
        body = json.loads(out)
        code = body["code"]
        invitation_id = body["invitation"]["invitation_id"]
        assert code.startswith("TLM-SYS-v1-")
        assert out.count(code) == 1 and code not in err

        ok, listed, _ = run(["invitations", "list"])
        assert ok == 0, listed
        assert code not in listed and "code_hash" not in listed
        assert json.loads(listed)["items"][0]["invitation_id"] == invitation_id

        ok, revoked, _ = run(["invitations", "delete", invitation_id])
        assert ok == 0, revoked
        assert json.loads(revoked)["invitation"]["state"] == "revoked"

        ok, described, _ = run(["config"])
        assert ok == 0
        summary = json.loads(described)
        assert summary["admin_token_configured"] is True
        assert "test-admin-token" not in described


class _Collector:
    def __init__(self, sink: list):
        self._sink = sink

    def write(self, text: str) -> int:
        self._sink.append(text)
        return len(text)

    def flush(self) -> None:  # pragma: no cover - trivial
        return None


# --------------------------------------------------------------------------- #
# admission / preemption acceptance through the real API
# --------------------------------------------------------------------------- #


def _app_with_admin(tmp_path: Path, **overrides):
    config = ControlPlaneConfig.for_tests(tmp_path, slots=2, **overrides)
    adapter = FakeRuntimeAdapter()
    app = create_app(config, adapter=adapter, run_startup_tasks=False)
    return app, adapter


def _register(client: TestClient) -> str:
    response = client.post("/control/users")
    assert response.status_code == 201, response.text
    return response.json()["user"]["user_id"]


def _issue_system_code(client: TestClient) -> str:
    response = client.post("/control/admin/invitations", json={"label": "t"}, headers=ADMIN)
    assert response.status_code == 201, response.text
    return response.json()["code"]


def _active_leases(client: TestClient) -> list[dict]:
    response = client.get("/control/admin/leases", headers=ADMIN)
    assert response.status_code == 200, response.text
    return [item for item in response.json()["items"] if item["state"] == "active"]


def test_system_code_preempts_exactly_one_low_priority_lease(tmp_path: Path):
    app, adapter = _app_with_admin(tmp_path)
    with TestClient(app) as admin:
        # two ordinary visitors hold both slots (priority 0)
        a, b = TestClient(app), TestClient(app)
        assert a.post("/control/leases", json={"label": "a"}).status_code == 201
        assert b.post("/control/leases", json={"label": "b"}).status_code == 201
        assert len(_active_leases(admin)) == 2

        code = _issue_system_code(admin)
        c = TestClient(app)
        response = c.post("/control/leases", json={"label": "c", "invitation_code": code})
        assert response.status_code == 201, response.text
        assert response.json()["lease"]["priority"] > 0

        active = _active_leases(admin)
        assert len(active) == 2, "preemption must take exactly one slot"
        # the oldest ordinary visitor was displaced, the other still serves
        assert a.get("/control/leases/current").status_code == 401
        assert b.get("/control/leases/current").status_code == 200


def test_ordinary_visitor_cannot_preempt_when_slots_are_full(tmp_path: Path):
    app, _adapter = _app_with_admin(tmp_path)
    with TestClient(app) as admin:
        a, b = TestClient(app), TestClient(app)
        assert a.post("/control/leases", json={"label": "a"}).status_code == 201
        assert b.post("/control/leases", json={"label": "b"}).status_code == 201
        c = TestClient(app)
        response = c.post("/control/leases", json={"label": "c"})
        assert response.status_code == 503, response.text
        assert len(_active_leases(admin)) == 2
        assert a.get("/control/leases/current").status_code == 200
        assert b.get("/control/leases/current").status_code == 200


def test_preemption_keeps_the_victims_project_and_volume(tmp_path: Path):
    app, adapter = _app_with_admin(tmp_path)
    with TestClient(app) as admin:
        victim = TestClient(app)
        _register(victim)
        workspace = victim.post("/control/workspaces", json={"title": "keep"}).json()["workspace"]
        opened = victim.post("/control/leases", json={"workspace_id": workspace["workspace_id"]})
        assert opened.status_code == 201, opened.text
        stored = app.state.store.require_workspace(workspace["workspace_id"])
        adapter.ensure_volume(VolumeSpec(name=stored.volume_name, labels=stored and {
            **app.state.config.labels(),
            "tutorlm.workspace_id": stored.workspace_id,
            "tutorlm.user_id": stored.user_id,
        }))

        other = TestClient(app)
        assert other.post("/control/leases", json={"label": "other"}).status_code == 201

        code = _issue_system_code(admin)
        assert TestClient(app).post(
            "/control/leases", json={"label": "sys", "invitation_code": code}
        ).status_code == 201

        # the victim lost compute, not data
        assert victim.get("/control/leases/current").status_code == 401
        assert app.state.store.require_workspace(workspace["workspace_id"]).state == "active"
        assert adapter.volume_exists(stored.volume_name)


def test_valid_system_code_promotes_an_existing_lease_in_place(tmp_path: Path):
    """An already-running ordinary lease + a valid system code must upgrade it.

    Acceptance requirement: submitting a valid system code while already
    holding an ordinary lease must raise that lease to the highest priority
    *in place* -- reuse the same lease/slot/token, never consume a second slot.
    """

    app, _adapter = _app_with_admin(tmp_path)
    with TestClient(app) as admin:
        code = _issue_system_code(admin)
        visitor = TestClient(app)
        first = visitor.post("/control/leases", json={"label": "ordinary"})
        assert first.status_code == 201, first.text
        original = first.json()
        assert original["lease"]["priority"] == 0
        original_id = original["lease"]["lease_id"]
        original_slot = original["lease"]["slot_id"]
        original_token = original["token"]

        upgraded = visitor.post(
            "/control/leases", json={"label": "with-code", "invitation_code": code}
        )
        assert upgraded.status_code == 201, upgraded.text
        body = upgraded.json()
        assert body["resumed"] is True
        assert body["lease"]["lease_id"] == original_id, "must reuse the same lease"
        assert body["lease"]["slot_id"] == original_slot, "must not consume a new slot"
        assert body["lease"]["priority"] > 0, "priority must be raised"
        assert body["token"] == original_token, "the lease token must not change"

        # exactly one live lease, and the untouched token still authenticates
        assert len(_active_leases(admin)) == 1

        bearer = TestClient(app)
        bearer.headers.update({"Authorization": f"Bearer {original_token}"})
        assert bearer.get("/control/leases/current").status_code == 200


def test_share_code_is_rejected_as_a_direct_lease_invitation(tmp_path: Path):
    """A share code may only be used via /control/workspace-imports.

    Submitting it directly to POST /control/leases must be refused -- it is not
    an admission token and must never grant compute by itself.
    """

    app, adapter = _app_with_admin(tmp_path)
    with TestClient(app) as admin:
        store = app.state.store
        config = app.state.config
        owner = TestClient(app)
        user_id = _register(owner)
        workspace_id = "22222222-2222-4222-8222-222222222222"
        directory = f"users/{user_id}/workspaces/{workspace_id}"
        store.create_workspace(
            workspace_id,
            user_id,
            "source",
            config.workspace_volume_name(workspace_id),
            data_directory=directory,
            storage_layout=STORAGE_LAYOUT_CONTENT_ONLY,
        )
        _invitation, share_code = app.state.invitations.issue_share_code(
            workspace=store.require_workspace(workspace_id), label="s"
        )

        visitor = TestClient(app)
        rejected = visitor.post(
            "/control/leases", json={"label": "share", "invitation_code": share_code}
        )
        assert rejected.status_code == 403, rejected.text
        assert rejected.json()["error"]["code"] == "invalid_invitation"
        # nothing was admitted
        assert _active_leases(admin) == []


def test_imported_copy_opens_without_a_code_after_import(tmp_path: Path):
    """After a share import the recipient may open the copy with no code.

    With invitation_only=false an ordinary no-code lease still works; the import
    records a normal (never system) trial grant so the copy can be opened.
    """

    app, adapter = _app_with_admin(tmp_path)
    with TestClient(app) as admin:
        store = app.state.store
        config = app.state.config
        owner = TestClient(app)
        owner_id = _register(owner)
        workspace_id = "44444444-4444-4444-8444-444444444444"
        directory = f"users/{owner_id}/workspaces/{workspace_id}"
        source = store.create_workspace(
            workspace_id,
            owner_id,
            "source",
            config.workspace_volume_name(workspace_id),
            data_directory=directory,
            storage_layout=STORAGE_LAYOUT_CONTENT_ONLY,
        )
        labels = {
            **config.labels(),
            "tutorlm.workspace_id": workspace_id,
            "tutorlm.user_id": owner_id,
        }
        adapter.ensure_volume(VolumeSpec(name=source.volume_name, labels=labels))
        adapter.write_volume(source.volume_name, f"{directory}/notes.md", b"hello")

        _invitation, share_code = app.state.invitations.issue_share_code(
            workspace=source, label="s"
        )
        recipient = TestClient(app)
        _register(recipient)
        imported = recipient.post(
            "/control/workspace-imports", json={"invitation_code": share_code}
        )
        assert imported.status_code == 200, imported.text
        copy = imported.json()["workspace"]
        assert copy["workspace_id"] != workspace_id

        opened = recipient.post(
            "/control/leases", json={"workspace_id": copy["workspace_id"]}
        )
        assert opened.status_code == 201, opened.text
        assert opened.json()["lease"]["priority"] == 0, "a share import grants an ordinary trial, never system priority"


def test_revoked_system_code_cannot_start_anything_new(tmp_path: Path):
    app, _adapter = _app_with_admin(tmp_path)
    with TestClient(app) as admin:
        holder = TestClient(app)
        code = _issue_system_code(admin)
        assert holder.post(
            "/control/leases", json={"label": "h", "invitation_code": code}
        ).status_code == 201

        invitation_id = admin.get("/control/admin/invitations", headers=ADMIN).json()["items"][0][
            "invitation_id"
        ]
        assert admin.delete(
            f"/control/admin/invitations/{invitation_id}", headers=ADMIN
        ).status_code == 200

        # the already-running lease is not force-killed, but nothing new starts
        assert holder.get("/control/leases/current").status_code == 200
        fresh = TestClient(app)
        rejected = fresh.post("/control/leases", json={"label": "x", "invitation_code": code})
        assert rejected.status_code == 403, rejected.text
        assert rejected.json()["error"]["code"] == "invalid_invitation"


def test_invitation_only_flip_blocks_old_cookie_resume(tmp_path: Path):
    open_config = ControlPlaneConfig.for_tests(tmp_path, slots=2, invitation_only=False)
    adapter = FakeRuntimeAdapter()
    app_open = create_app(open_config, adapter=adapter, run_startup_tasks=False)
    with TestClient(app_open) as first:
        obtained = first.post("/control/leases", json={"label": "ephemeral"})
        assert obtained.status_code == 201, obtained.text
        token = obtained.json()["token"]

    gated_config = open_config.with_overrides(invitation_only=True)
    app_gated = create_app(gated_config, adapter=adapter, run_startup_tasks=False)
    with TestClient(app_gated) as gated:
        gated.cookies.set(gated_config.lease_cookie_name, token)
        blocked = gated.post("/control/leases", json={"label": "again"})
        assert blocked.status_code == 403, blocked.text
        assert blocked.json()["error"]["code"] == "invitation_required"


# --------------------------------------------------------------------------- #
# share copy acceptance
# --------------------------------------------------------------------------- #


@pytest.fixture()
def sharing(tmp_path: Path):
    config = ControlPlaneConfig.for_tests(tmp_path, slots=2)
    components = build_components(config, adapter=FakeRuntimeAdapter())
    store = components["store"]
    service = ShareService(
        config,
        store,
        components["lifecycle"],
        components["adapter"],
        InvitationService(store, pepper="p", policy=AccessPolicy(invitation_only=False)),
    )
    try:
        yield components, service
    finally:
        store.close()


def _shareable_workspace(components, adapter, user_id: str, *, workspace_id: str):
    """Declare an app-side ``content_only`` layout and mount the source volume.

    The in-container app owns this declaration; until its ready handshake ships,
    tests set it through the documented store/service seam.
    """

    config = components["config"]
    directory = f"users/{user_id}/workspaces/{workspace_id}"
    workspace = components["store"].create_workspace(
        workspace_id,
        user_id,
        "source",
        config.workspace_volume_name(workspace_id),
        data_directory=directory,
        storage_layout=STORAGE_LAYOUT_CONTENT_ONLY,
    )
    labels = {
        **config.labels(),
        "tutorlm.workspace_id": workspace_id,
        "tutorlm.user_id": user_id,
    }
    adapter.ensure_volume(VolumeSpec(name=workspace.volume_name, labels=labels))
    return workspace, directory


def test_share_copy_is_isolated_and_drops_session_subtrees(sharing):
    components, service = sharing
    adapter, store = components["adapter"], components["store"]
    user_id = "11111111-1111-4111-8111-111111111111"
    store.create_user(user_id, "h")
    source, source_dir = _shareable_workspace(
        components, adapter, user_id, workspace_id="22222222-2222-4222-8222-222222222222"
    )
    adapter.write_volume(source.volume_name, f"{source_dir}/notes.md", b"Notes")
    for subtree in SESSION_SUBTREES:
        adapter.write_volume(source.volume_name, f"{source_dir}/{subtree}/secret", b"SECRET")

    _invitation, code = service._invitations.issue_share_code(workspace=source, label="s")
    result = service.import_share(user_id=user_id, code=code)
    destination = result.workspace

    assert destination.data_directory == (
        f"users/{user_id}/workspaces/{destination.workspace_id}"
    )
    assert destination.state == "active"
    assert adapter.read_volume(destination.volume_name, f"{destination.data_directory}/notes.md") == b"Notes"
    for subtree in SESSION_SUBTREES:
        with pytest.raises(Exception):
            adapter.read_volume(destination.volume_name, f"{destination.data_directory}/{subtree}/secret")

    # independent bytes: writing the copy never touches the source
    adapter.write_volume(destination.volume_name, f"{destination.data_directory}/notes.md", b"CHANGED")
    assert adapter.read_volume(source.volume_name, f"{source_dir}/notes.md") == b"Notes"


def test_source_is_gc_pinned_while_a_copy_is_in_flight(sharing):
    """An in-flight copy must keep its source volume un-claimable by storage GC."""

    components, service = sharing
    adapter, store = components["adapter"], components["store"]
    config = components["config"]
    user_id = "11111111-1111-4111-8111-111111111111"
    store.create_user(user_id, "h")
    source, _source_dir = _shareable_workspace(
        components, adapter, user_id, workspace_id="33333333-3333-4333-8333-333333333333"
    )
    destination_id = "44444444-4444-4444-8444-444444444444"

    # Durable pin written *before* any volume work (mirrors a crash mid-copy).
    store.begin_share_copy(
        copy_id="pin-1",
        source_workspace_id=source.workspace_id,
        destination_workspace_id=destination_id,
        recipient_user_id=user_id,
        destination_volume_name=config.workspace_volume_name(destination_id),
        title="copy",
        invitation_id=None,
        idempotency_key=None,
        data_directory=f"users/{user_id}/workspaces/{destination_id}",
    )

    # Even with an idle threshold of zero the pinned source must not be claimed.
    claimed = {w.workspace_id for w in store.claim_expired_workspaces(0)}
    assert source.workspace_id not in claimed, "GC claimed a source volume that is pinned"

    store.unpin_workspace_copy("pin-1", "failed", error="test")
    claimed_after = {w.workspace_id for w in store.claim_expired_workspaces(0)}
    assert source.workspace_id in claimed_after, "pin release must unblock storage GC"


def test_failed_copy_is_not_published_and_source_is_untouched(sharing):
    components, service = sharing
    adapter, store = components["adapter"], components["store"]
    user_id = "11111111-1111-4111-8111-111111111111"
    store.create_user(user_id, "h")
    source, source_dir = _shareable_workspace(
        components, adapter, user_id, workspace_id="55555555-5555-4555-8555-555555555555"
    )
    adapter.write_volume(source.volume_name, f"{source_dir}/notes.md", b"Notes")
    _invitation, code = service._invitations.issue_share_code(workspace=source, label="s")

    adapter.fail_copy.add(source.volume_name)
    with pytest.raises(Exception):
        service.import_share(user_id=user_id, code=code)

    assert adapter.read_volume(source.volume_name, f"{source_dir}/notes.md") == b"Notes"
    assert store.require_workspace(source.workspace_id).state == "active"
    assert adapter.volume_exists(source.volume_name)

    copies = store.list_workspace_copies()
    assert len(copies) == 1 and copies[0].state == "failed"
    assert store.require_workspace(copies[0].destination_workspace_id).state != "active"

    # copying is retryable once the injected failure is removed
    adapter.fail_copy.clear()
    retried = service.import_share(user_id=user_id, code=code)
    assert retried.workspace.state == "active"


# ------------------------------------------------------------------ 命名系统码
def test_named_system_code_from_config_grants_priority_case_insensitively(tmp_path: Path):
    config = ControlPlaneConfig.for_tests(tmp_path, slots=2, system_invitation_codes=("TUTORLM-DONGNIYISI",))
    app = create_app(config, adapter=FakeRuntimeAdapter(), run_startup_tasks=True)   # seeds configured codes
    with TestClient(app) as admin:
        a, b = TestClient(app), TestClient(app)
        assert a.post("/control/leases", json={"label": "a"}).status_code == 201
        assert b.post("/control/leases", json={"label": "b"}).status_code == 201
        c = TestClient(app)
        # People type it: lower case and spaces around it still work.
        response = c.post("/control/leases", json={"label": "c", "invitation_code": "  tutorlm-dongniyisi "})
        assert response.status_code == 201, response.text
        assert response.json()["lease"]["priority"] > 0
        assert len(_active_leases(admin)) == 2
        listed = admin.get("/control/admin/invitations", headers=ADMIN).json()["items"]
        assert any(item.get("code_prefix") == "NAMED" and item.get("label") == "configured" for item in listed), listed
        assert "TUTORLM-DONGNIYISI" not in str(listed), "the plaintext code is never listed"


def test_admin_can_issue_a_named_code_once(tmp_path: Path):
    app, _adapter = _app_with_admin(tmp_path)
    with TestClient(app) as admin:
        created = admin.post("/control/admin/invitations", headers=ADMIN, json={"label": "roadshow", "code": "TutorLM-Roadshow-2026"})
        assert created.status_code == 201, created.text
        assert created.json()["code"] == "TUTORLM-ROADSHOW-2026"
        again = admin.post("/control/admin/invitations", headers=ADMIN, json={"code": "TUTORLM-ROADSHOW-2026"})
        assert again.status_code == 409, again.text
        visitor = TestClient(app)
        assert visitor.post("/control/leases", json={"invitation_code": "TUTORLM-ROADSHOW-2026"}).status_code == 201
        # Revoking makes it stop working, and it cannot be issued again.
        item = next(i for i in admin.get("/control/admin/invitations", headers=ADMIN).json()["items"] if i["label"] == "roadshow")
        assert admin.delete(f"/control/admin/invitations/{item['invitation_id']}", headers=ADMIN).status_code in (200, 204)
        late = TestClient(app)
        assert late.post("/control/leases", json={"invitation_code": "TUTORLM-ROADSHOW-2026"}).status_code in (401, 403)
        assert admin.post("/control/admin/invitations", headers=ADMIN, json={"code": "TUTORLM-ROADSHOW-2026"}).status_code == 409


@pytest.mark.parametrize("code", ["SHORT-CODE", "tutorlm", "TLM-SYS-ROADSHOW-2026", "TLM-SHARE-ROADSHOW-1",
                                  "TUTORLM DONGNIYISI", "TUTORLM_DONGNIYISI", "-TUTORLM-DONGNIYISI", "A" * 70 + "-B"])
def test_unsafe_or_ambiguous_named_codes_are_rejected(tmp_path: Path, code: str):
    app, _adapter = _app_with_admin(tmp_path)
    with TestClient(app) as admin:
        response = admin.post("/control/admin/invitations", headers=ADMIN, json={"code": code})
        assert response.status_code in (400, 401, 403, 422), (code, response.text)
    # Configuration is just as strict (codes there are written as-is).
    with pytest.raises(ConfigError):
        ControlPlaneConfig.for_tests(tmp_path, system_invitation_codes=(code,))
