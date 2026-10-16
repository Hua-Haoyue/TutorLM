"""Independent security regression tests for the invitation / share-copy work.

Owner: reviewer. These lock in the five P1 defects that were independently
reproduced during the read-only security review and have since been fixed. They
are written to FAIL if the corresponding defect is reintroduced, and they do not
depend on any other test module.

Scope:
1. system-code format domain isolation (config regex == core regex).
2. validation-error responses must not echo submitted secrets (422 `input`).
3. share copy must pin the source volume BEFORE releasing source compute, so a
   concurrent GC can never delete a volume that is about to be copied.
4. config provenance errors must not print system codes in plaintext / must not
   leak a traceback through the CLI.
5. session/credential subtrees must be excluded by basename at ANY depth, not
   only at the top of the content directory.
"""

from __future__ import annotations

import io
import json

import pytest
from fastapi.testclient import TestClient

from control_plane import cli as cli_module
from control_plane import config as config_module
from control_plane import invitations as invitations_module
from control_plane.api.app import create_app
from control_plane.config import ControlPlaneConfig
from control_plane.domain.models import STORAGE_LAYOUT_CONTENT_ONLY
from control_plane.errors import ConfigError
from control_plane.invitations import generate_system_code
from control_plane.ports import VolumeCopySpec, VolumeSpec
from control_plane.runtime.fake import FakeRuntimeAdapter
from control_plane.sharing import SESSION_SUBTREES

USER_ID = "11111111-1111-4111-8111-111111111111"


def _app(tmp_path, **overrides):
    config = ControlPlaneConfig.for_tests(tmp_path, **overrides)
    adapter = FakeRuntimeAdapter()
    app = create_app(config, adapter=adapter, run_startup_tasks=False)
    return app, adapter, config


def _shareable_source(state, config, adapter, workspace_id, user_id=USER_ID):
    data_directory = f"users/{user_id}/workspaces/{workspace_id}"
    workspace = state.store.create_workspace(
        workspace_id,
        user_id,
        "Source",
        config.workspace_volume_name(workspace_id),
        data_directory=data_directory,
        storage_layout=STORAGE_LAYOUT_CONTENT_ONLY,
    )
    labels = dict(config.labels())
    labels.update({"tutorlm.workspace_id": workspace_id, "tutorlm.user_id": user_id})
    adapter.ensure_volume(VolumeSpec(name=workspace.volume_name, labels=labels))
    return workspace, data_directory


# --------------------------------------------------------------------------- #
# 1. system-code format domain isolation
# --------------------------------------------------------------------------- #


def test_system_code_regex_is_identical_across_config_and_core():
    # The two modules deliberately do not import each other, so the only thing
    # keeping them in sync is this test. The patterns are not byte-identical
    # (the core captures version/payload groups), so compare behavior over a
    # corpus that exercises length, charset and version edges.
    assert (
        config_module.SYSTEM_INVITATION_CODE_RE.flags
        == invitations_module.SYSTEM_CODE_RE.flags
    )
    corpus = [
        generate_system_code(),
        "TLM-SYS-v1-" + "A" * 43,
        "TLM-SYS-v9-" + "_-0" * 14 + "x",
        "TLM-SYS-v1",
        "TLM-SYS-v1-" + "A" * 42,
        "TLM-SYS-v1-" + "A" * 44,
        "TLM-SYS-v1-" + "A" * 43 + "=",
        "TLM-SYS-V1-" + "A" * 43,
        "tlm-sys-v1-" + "A" * 43,
        "TLM-SYS-4Q2M-9Z7P-K3XR-T8NV",
        "TLM-SYS-v-" + "A" * 43,
        "TLM-SYS-v1x-" + "A" * 43,
    ]
    for code in corpus:
        assert config_module.is_valid_system_invitation_code(code) == (
            invitations_module.SYSTEM_CODE_RE.match(code) is not None
        ), code


@pytest.mark.parametrize(
    "code",
    [
        generate_system_code(),
        "TLM-SYS-v1-" + "A" * 43,
        "TLM-SYS-v1",
        "TLM-SYS-v1-" + "A" * 42,
        "TLM-SYS-v1-" + "A" * 44,
        # Crockford-style hand-written code must never be a system credential.
        "TLM-SYS-4Q2M-9Z7P-K3XR-T8NV",
        "TLM-SYS-V1-" + "A" * 43,
        "tlm-sys-v1-" + "A" * 43,
    ],
)
def test_system_code_acceptance_agrees_on_both_sides(code):
    assert config_module.is_valid_system_invitation_code(code) == (
        invitations_module.SYSTEM_CODE_RE.match(code) is not None
    )


def test_configured_crockford_code_is_rejected_at_admission(tmp_path):
    # Even if some future config layer emits a Crockford code, admission must
    # not silently drop it into "invitation invalid" while config thinks it is
    # fine. Both sides must agree it is invalid.
    crockford = "TLM-SYS-4Q2M-9Z7P-K3XR-T8NV"
    assert config_module.is_valid_system_invitation_code(crockford) is False
    assert invitations_module.is_system_code_format(crockford) is False


# --------------------------------------------------------------------------- #
# 2. validation errors must not echo secrets
# --------------------------------------------------------------------------- #


def test_422_does_not_echo_oversized_invitation_code(tmp_path):
    app, _adapter, _config = _app(tmp_path, invitation_only=True)
    client = TestClient(app)
    submitted = "TLM-SYS-v1-" + "B" * 200
    response = client.post("/control/leases", json={"invitation_code": submitted})
    assert response.status_code == 422
    assert submitted not in response.text
    # The per-error ``input`` key must be gone. The error *code* string
    # ``invalid_input`` legitimately contains the substring "input", so check
    # the structured error entries instead of the raw body.
    for entry in response.json()["error"]["details"]["errors"]:
        assert "input" not in entry
        assert "ctx" not in entry
        assert submitted[:60] not in json.dumps(entry)


def test_422_does_not_echo_wrong_type_invitation_code(tmp_path):
    app, _adapter, _config = _app(tmp_path)
    client = TestClient(app)
    secret_number = 123456789
    response = client.post("/control/leases", json={"invitation_code": secret_number})
    assert response.status_code == 422
    assert str(secret_number) not in response.text


def test_422_does_not_echo_user_recovery_token(tmp_path):
    app, _adapter, _config = _app(tmp_path)
    client = TestClient(app)
    secret = "S" * 400
    response = client.post(
        "/control/users/session",
        json={"user_id": USER_ID, "recovery_token": secret},
    )
    assert response.status_code == 422
    assert secret not in response.text
    for entry in response.json()["error"]["details"]["errors"]:
        assert "input" not in entry


# --------------------------------------------------------------------------- #
# 3. source pin must precede source compute teardown
# --------------------------------------------------------------------------- #


def test_source_is_pinned_before_compute_is_stopped(tmp_path):
    app, adapter, config = _app(tmp_path)
    state = app.state
    state.store.create_user(USER_ID, "h")
    workspace_id = "33333333-3333-4333-8333-333333333333"
    source, source_dir = _shareable_source(state, config, adapter, workspace_id)
    adapter.write_volume(source.volume_name, f"{source_dir}/notes.md", b"precious")
    _invitation, code = state.invitations.issue_share_code(workspace=source, label="s")

    observed = {}
    original = state.sharing._stop_source_compute

    def spy(src):
        with state.store._lock:
            observed["pinned"] = state.store._workspace_pinned(
                state.store._conn, src.workspace_id
            )
            observed["has_compute"] = state.store._workspace_has_compute(
                state.store._conn, src.workspace_id
            )
        original(src)

    state.sharing._stop_source_compute = spy
    try:
        state.sharing.import_share(user_id=USER_ID, code=code)
    finally:
        state.sharing._stop_source_compute = original

    # At the moment we stop compute, the source must already be pinned.
    assert observed.get("pinned") is True


def test_concurrent_gc_between_quiesce_and_copy_cannot_delete_source(tmp_path):
    # Regression for the data-loss race: worst case is a full GC cycle running
    # while the copy is being prepared. With the pin taken first, the source
    # volume must survive.
    app, adapter, config = _app(tmp_path, workspace_idle_ttl_seconds=3600)
    state = app.state
    state.store.create_user(USER_ID, "h")
    workspace_id = "44444444-4444-4444-8444-444444444444"
    source, source_dir = _shareable_source(state, config, adapter, workspace_id)
    adapter.write_volume(source.volume_name, f"{source_dir}/notes.md", b"precious")
    _invitation, code = state.invitations.issue_share_code(workspace=source, label="s")

    original = state.sharing._stop_source_compute
    deleted = []

    def spy(src):
        # Force a GC pass that would reap an unpinned, idle source right here.
        state.store._clock = (lambda base: (lambda: base))(state.store._clock() + 10**7)
        deleted.extend(state.workspaces.gc_once()["deleted"])
        original(src)

    state.sharing._stop_source_compute = spy
    try:
        result = state.sharing.import_share(user_id=USER_ID, code=code)
    finally:
        state.sharing._stop_source_compute = original

    assert workspace_id not in deleted
    assert adapter.volume_exists(source.volume_name)
    assert adapter.read_volume(source.volume_name, f"{source_dir}/notes.md") == b"precious"
    assert result.workspace.state == "active"


# --------------------------------------------------------------------------- #
# 4. config provenance errors must not leak secrets or tracebacks
# --------------------------------------------------------------------------- #


def test_config_duplicate_code_error_has_no_plaintext(tmp_path):
    code = generate_system_code()
    with pytest.raises(ConfigError) as excinfo:
        ControlPlaneConfig(adapter="fake", system_invitation_codes=(code, code))
    assert code not in str(excinfo.value)


def test_config_invalid_code_error_has_no_plaintext(tmp_path):
    bogus = "TLM-SYS-v1-" + "Z" * 43  # syntactically fine but... force invalid below
    with pytest.raises(ConfigError) as excinfo:
        ControlPlaneConfig(adapter="fake", system_invitation_codes=("tlm-sys-v1-" + "A" * 43,))
    text = str(excinfo.value)
    assert "tlm-sys-v1-" + "A" * 43 not in text
    assert bogus not in text


def test_cli_serve_reports_config_error_without_traceback_or_plaintext(tmp_path, capsys):
    code = generate_system_code()
    path = tmp_path / "serve.toml"
    path.write_text(
        '[control_plane]\nsystem_invitation_codes = ["%s", "%s"]\n' % (code, code),
        encoding="utf-8",
    )
    out, err = io.StringIO(), io.StringIO()
    rc = cli_module.main(
        ["serve", "--config", str(path)],
        environ={},
        stdout=out,
        stderr=err,
        serve_runner=lambda *a, **k: 0,
    )
    assert rc == 1
    assert code not in err.getvalue()
    assert "Traceback" not in err.getvalue()


# --------------------------------------------------------------------------- #
# 5. session subtrees excluded by basename at any depth
# --------------------------------------------------------------------------- #


def test_spec_exclude_names_reject_multi_segment_values():
    for bad in ("sub/sessions", "sessions/secret", "./auth", "/auth", ""):
        with pytest.raises(Exception):
            VolumeCopySpec(
                source=VolumeSpec(name="s", labels={"app": "x"}),
                destination=VolumeSpec(name="d", labels={"app": "x"}),
                exclude_names=(bad,),
            )


def test_nested_session_subtree_is_not_cloned(tmp_path):
    app, adapter, config = _app(tmp_path)
    state = app.state
    state.store.create_user(USER_ID, "h")
    workspace_id = "55555555-5555-4555-8555-555555555555"
    source, source_dir = _shareable_source(state, config, adapter, workspace_id)
    adapter.write_volume(source.volume_name, f"{source_dir}/notes.md", b"Notes")
    for name in SESSION_SUBTREES:
        # Top level AND nested: both must be dropped.
        adapter.write_volume(source.volume_name, f"{source_dir}/{name}/top", b"TOP")
        adapter.write_volume(
            source.volume_name, f"{source_dir}/sub/{name}/nested", b"NESTED"
        )

    _invitation, code = state.invitations.issue_share_code(workspace=source, label="s")
    destination = state.sharing.import_share(user_id=USER_ID, code=code).workspace

    assert (
        adapter.read_volume(
            destination.volume_name, f"{destination.data_directory}/notes.md"
        )
        == b"Notes"
    )
    for name in SESSION_SUBTREES:
        for suffix in ("top", "nested"):
            key = (
                f"{destination.data_directory}/{name}/{suffix}"
                if suffix == "top"
                else f"{destination.data_directory}/sub/{name}/{suffix}"
            )
            with pytest.raises(Exception):
                adapter.read_volume(destination.volume_name, key)


def test_copy_spec_carries_exclude_names_not_exclude_paths(tmp_path):
    app, adapter, config = _app(tmp_path)
    state = app.state
    state.store.create_user(USER_ID, "h")
    workspace_id = "66666666-6666-4666-8666-666666666666"
    source, source_dir = _shareable_source(state, config, adapter, workspace_id)
    adapter.write_volume(source.volume_name, f"{source_dir}/notes.md", b"Notes")

    captured = {}
    original = adapter.copy_volume

    def capture(spec):
        captured["spec"] = spec
        return original(spec)

    adapter.copy_volume = capture
    try:
        _invitation, code = state.invitations.issue_share_code(workspace=source, label="s")
        state.sharing.import_share(user_id=USER_ID, code=code)
    finally:
        adapter.copy_volume = original

    spec = captured["spec"]
    assert set(SESSION_SUBTREES).issubset(set(spec.exclude_names))
    # Session subtrees must be enforced by basename, not by a root-relative path.
    assert spec.exclude_paths == ()


# --------------------------------------------------------------------------- #
# 6. resume must PROMOTE in place, SHARE codes must not grant a direct lease
# --------------------------------------------------------------------------- #


def test_resume_with_system_code_promotes_in_place(tmp_path):
    code = generate_system_code()
    config = ControlPlaneConfig.for_tests(
        tmp_path, slots=2, system_invitation_codes=(code,), invitation_only=False
    )
    app = create_app(config, adapter=FakeRuntimeAdapter(), run_startup_tasks=True)
    with TestClient(app) as visitor:
        first = visitor.post("/control/leases", json={"label": "anon"}).json()
        lease_a = first["lease"]
        assert lease_a["priority"] == 0

        second = visitor.post("/control/leases", json={"invitation_code": code}).json()
        lease_b = second["lease"]

        # Same lease, no new slot taken, priority bumped to the system level.
        assert second["resumed"] is True
        assert lease_b["lease_id"] == lease_a["lease_id"]
        assert lease_b["slot_id"] == lease_a["slot_id"]
        assert lease_b["priority"] == 100
        live = app.state.store.list_live_leases()
        assert len(live) == 1 and live[0].priority == 100


def test_system_code_priority_promotion_is_never_downgraded(tmp_path):
    code = generate_system_code()
    config = ControlPlaneConfig.for_tests(
        tmp_path, slots=2, system_invitation_codes=(code,), invitation_only=False
    )
    app = create_app(config, adapter=FakeRuntimeAdapter(), run_startup_tasks=True)
    with TestClient(app) as visitor:
        visitor.post("/control/leases", json={"invitation_code": code})
        # A second (unauthenticated) request must not lower the priority.
        again = visitor.post("/control/leases", json={}).json()
        assert again["lease"]["priority"] == 100


def test_share_code_cannot_be_used_as_a_direct_lease(tmp_path):
    app, adapter, config = _app(tmp_path)
    state = app.state
    state.store.create_user(USER_ID, "h")
    workspace_id = "88888888-8888-4888-8888-888888888888"
    source, _source_dir = _shareable_source(state, config, adapter, workspace_id)
    _invitation, share_code = state.invitations.issue_share_code(workspace=source, label="s")

    client = TestClient(app)
    response = client.post("/control/leases", json={"invitation_code": share_code})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "invalid_invitation"
    # No compute was handed out for the share code.
    assert state.store.list_live_leases() == []


# --------------------------------------------------------------------------- #
# 7. helper liveness: reconcile must not release the source pin
# --------------------------------------------------------------------------- #


def test_reconcile_keeps_pin_when_helper_may_still_be_running(tmp_path):
    app, adapter, config = _app(tmp_path)
    state = app.state
    state.store.create_user(USER_ID, "h")
    workspace_id = "99999999-9999-4999-8999-999999999999"
    source, source_dir = _shareable_source(state, config, adapter, workspace_id)
    adapter.write_volume(source.volume_name, f"{source_dir}/notes.md", b"original")

    destination_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    state.store.begin_share_copy(
        copy_id="inflight",
        source_workspace_id=workspace_id,
        destination_workspace_id=destination_id,
        recipient_user_id=USER_ID,
        destination_volume_name=config.workspace_volume_name(destination_id),
        title="half",
        invitation_id=None,
        idempotency_key=None,
        data_directory=f"users/{USER_ID}/workspaces/{destination_id}",
    )
    # Leftover helper still (nominally) running and mounting the source volume.
    from control_plane.ports import InstanceSpec

    adapter.create(
        InstanceSpec(
            slot_id=99, generation=0,
            container_name=f"{config.container_prefix}-copy-deadbeef",
            # The helper mounts the source read-only and the (new) destination;
            # use a scratch volume name so the fake adapter's own volume-label
            # guard does not reject the fixture before reconcile runs.
            image="helper", volume_name=f"{config.volume_prefix}-copy-scratch",
            mount_path="/data", internal_port=8000,
            labels={**config.labels(), "tutorlm.copy": "volume"},
        )
    )

    report = state.sharing.reconcile()
    assert report["manual_review"] == ["inflight"]
    assert report["cleaned"] == []
    assert state.store._workspace_pinned(state.store._conn, workspace_id) is True
    # Source content untouched and destination still invisible.
    assert adapter.read_volume(source.volume_name, f"{source_dir}/notes.md") == b"original"
    assert state.store.require_workspace(destination_id).state == "copying"


def test_reconcile_releases_pin_when_no_helper_remains(tmp_path):
    app, adapter, config = _app(tmp_path)
    state = app.state
    state.store.create_user(USER_ID, "h")
    workspace_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    source, _source_dir = _shareable_source(state, config, adapter, workspace_id)
    destination_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    state.store.begin_share_copy(
        copy_id="inflight2",
        source_workspace_id=workspace_id,
        destination_workspace_id=destination_id,
        recipient_user_id=USER_ID,
        destination_volume_name=config.workspace_volume_name(destination_id),
        title="half",
        invitation_id=None,
        idempotency_key=None,
        data_directory=f"users/{USER_ID}/workspaces/{destination_id}",
    )

    report = state.sharing.reconcile()
    assert report["cleaned"] == ["inflight2"]
    assert report["manual_review"] == []
    assert state.store._workspace_pinned(state.store._conn, workspace_id) is False
    assert state.store.require_workspace(destination_id).state == "failed"
