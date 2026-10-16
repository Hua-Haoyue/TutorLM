"""HTTP host contract tests (``docs/instance-app-api.md``).

These run the real ASGI app through ``TestClient`` against a local Host graph:
temporary volume, volume-external sessions, ``fake`` provider. No Docker, no
network, no control plane and no paid model call is involved.

Coverage map:

* ``/health`` and ``/health/ready`` -- liveness vs. the persistent binding
  declaration (including the ``content_only`` proof and its absence);
* sessions -- implicit-session default deny, invite code, ``/me``, expiry-ish
  invalid token, logout and the "sessions must be outside the volume" rule;
* projects -- single-project adaptation, share/redeem non-support;
* documents -- creation, immutable revisions, ``/content`` text-only semantics;
* originals -- ``ETag`` / ``Range`` / 206+416 / ``HEAD`` / conditional 304 and
  Authorization enforcement;
* runs -- ``Idempotency-Key`` scoping and conflict, polling to a terminal state,
  cancel-before-commit, and the disabled TeX compiler answering 503;
* envelope/fallback -- the single error shape and the ``/api/*`` 404.
"""
from __future__ import annotations


import time

import pytest

# --------------------------------------------------------------------------
# health
# --------------------------------------------------------------------------


def test_health_is_liveness_only(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_non_persistent_reports_mode_without_binding(client):
    response = client.get("/health/ready")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["mode"] == "demo"
    # A non-persistent instance must not claim a workspace binding.
    assert "workspace_id" not in payload
    assert "storage_layout" not in payload


def test_ready_persistent_declares_binding_and_content_only(persistent_env):
    from backend.adapters.http.app import create_app
    from backend.host.config import HostConfig
    from fastapi.testclient import TestClient

    config = HostConfig.from_env(persistent_env)
    with TestClient(create_app(config)) as test_client:
        response = test_client.get("/health/ready")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["storage_mode"] == "persistent"
    assert payload["workspace_id"] == persistent_env["TUTORLM_WORKSPACE_ID"]
    assert payload["storage_layout"] == "content_only"


def test_ready_persistent_legacy_layout_omits_storage_layout(persistent_env, tmp_path):
    from backend.adapters.http.app import create_app
    from backend.host.config import HostConfig
    from fastapi.testclient import TestClient

    # A legacy mount root cannot be proven session-free, so the instance must not
    # declare `content_only` (the control plane then records `legacy_root`).
    legacy_root = tmp_path / "legacy"
    legacy_root.mkdir(parents=True, exist_ok=True)
    env = {
        **persistent_env,
        "TUTORLM_MOUNT_PATH": str(legacy_root),
        "TUTORLM_DATA_DIR": str(legacy_root),
        "TUTORLM_WORKSPACE_SUBDIRECTORY": ".",
    }
    with TestClient(create_app(HostConfig.from_env(env))) as test_client:
        response = test_client.get("/health/ready")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["storage_mode"] == "persistent"
    assert payload["workspace_id"] == env["TUTORLM_WORKSPACE_ID"]
    assert "storage_layout" not in payload or payload["storage_layout"] != "content_only"


def test_ready_is_not_ok_when_data_root_is_not_writable(persistent_env, tmp_path, monkeypatch):
    from backend.adapters.http.app import create_app
    from backend.host.config import HostConfig
    from fastapi.testclient import TestClient

    # Simulate an unwritable volume: the probe reports failure and readiness must
    # refuse rather than silently writing somewhere else.
    import backend.host.storage_probe as probe

    monkeypatch.setattr(probe, "ensure_data_root", lambda path: (False, "data root is not writable"))
    monkeypatch.setattr(probe, "_probe_writable", lambda path: False)

    with TestClient(create_app(HostConfig.from_env(persistent_env))) as test_client:
        response = test_client.get("/health/ready")

    assert response.status_code == 503, response.text
    payload = response.json()
    assert payload["status"] != "ok"
    assert "storage_layout" not in payload


# --------------------------------------------------------------------------
# runtime discovery
# --------------------------------------------------------------------------


def test_runtime_is_unauthenticated_and_never_exposes_a_key(client, env):
    response = client.get("/api/runtime")
    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] in ("local", "demo", "managed")
    assert payload["provider"]["name"] == "fake"
    assert "api_key" not in response.text.lower()
    kinds = {wf["kind"] for plugin in payload["plugins"] for wf in plugin["workflows"]}
    assert {"explanation", "narration"} <= kinds
    assert set(payload["limits"]) >= {"max_documents_per_session", "max_bytes_per_file", "max_runs_per_session"}


def test_runtime_provider_defaults_to_fake_without_configuration(tmp_path, monkeypatch):
    # The documented default provider is `fake`; a bare environment must not
    # require any model credentials.
    from backend.host.config import HostConfig

    monkeypatch.setenv("TUTORLM_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("TUTORLM_PROVIDER", raising=False)
    assert HostConfig.from_env().provider.name == "fake"


# --------------------------------------------------------------------------
# sessions
# --------------------------------------------------------------------------


def test_implicit_session_is_denied_by_default(client):
    response = client.post("/api/sessions", json={})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invite_required"


def test_implicit_session_denied_even_when_opted_in_without_persistent_binding(env, monkeypatch):
    # Stage A requires the explicit flag *and* a persistent, bound workspace. A
    # demo instance that merely sets the flag must still refuse.
    from backend.adapters.http.app import create_app
    from backend.host.config import HostConfig
    from fastapi.testclient import TestClient

    with TestClient(create_app(HostConfig.from_env({**env, "TUTORLM_ALLOW_IMPLICIT_SESSION": "true"}))) as test_client:
        response = test_client.post("/api/sessions", json={})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invite_required"


def test_wrong_invite_code_is_rejected(client):
    response = client.post("/api/sessions", json={"invite_code": "not-the-code"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invite_required"


def test_session_lifecycle_and_bearer_enforcement(client):
    issued = client.post("/api/sessions", json={"invite_code": "TLM-DEMO-TEST"})
    assert issued.status_code == 201
    body = issued.json()
    assert body["access_token"]
    assert body["session"]["id"]
    assert body["session"]["expires_at"]
    headers = {"Authorization": f"Bearer {body['access_token']}"}

    me = client.get("/api/sessions/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["session"]["id"] == body["session"]["id"]
    assert "max_bytes_per_file" in me.json()["limits"]

    assert client.get("/api/sessions/me").status_code == 401
    assert client.get("/api/sessions/me", headers={"Authorization": "Bearer nonsense"}).status_code == 401
    # A non-bearer scheme is not a credential either.
    assert client.get("/api/sessions/me", headers={"Authorization": "Basic abc"}).status_code == 401

    logout = client.delete("/api/sessions/me", headers=headers)
    assert logout.status_code == 202
    # The session is revoked; project content is untouched by design.
    assert client.get("/api/sessions/me", headers=headers).status_code == 401


def test_client_supplied_owner_and_workspace_headers_are_ignored(client):
    issued = client.post("/api/sessions", json={"invite_code": "TLM-DEMO-TEST"})
    token = issued.json()["access_token"]
    # The session workspace comes from the server, never from these headers.
    headers = {
        "Authorization": f"Bearer {token}",
        "X-User-ID": "attacker",
        "X-Workspace-ID": "attacker-workspace",
        "X-User-Token": "attacker-token",
    }
    me = client.get("/api/sessions/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["session"]["workspace_id"] != "attacker-workspace"


def test_sessions_cannot_be_configured_inside_the_volume(env, tmp_path, monkeypatch):
    # Fail closed rather than writing a session into the project volume, because
    # sharing/cloning copies the volume tree and a session inside it would become
    # the receiver's identity.
    from backend.host import errors as app_errors
    from backend.host.local.sessions import SessionStore

    data_dir = tmp_path / "volume"
    data_dir.mkdir(parents=True, exist_ok=True)
    # Point the session directory *inside* the volume: the guard must refuse.
    monkeypatch.setenv("TUTORLM_SESSION_DATA_DIR", str(data_dir / "sessions"))

    with pytest.raises(app_errors.SessionDirInvalid):
        SessionStore(data_dir / "sessions" / "sessions.sqlite3", workspace_id="local", data_dir=data_dir)


def test_startup_refuses_a_missing_session_directory(tmp_path, monkeypatch):
    # Without an explicit volume-external session directory the app must not
    # silently fall back to writing sessions inside the project volume.
    from backend.adapters.http.app import create_app
    from backend.host import errors as app_errors
    from backend.host.config import HostConfig

    monkeypatch.delenv("TUTORLM_SESSION_DATA_DIR", raising=False)
    config = HostConfig.from_env(
        {"TUTORLM_RUN_MODE": "demo", "TUTORLM_DATA_DIR": str(tmp_path / "volume")}
    )
    with pytest.raises(app_errors.SessionDirInvalid):
        create_app(config)

# --------------------------------------------------------------------------
# projects
# --------------------------------------------------------------------------


def test_projects_requires_a_session(client):
    response = client.get("/api/projects")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_projects_returns_exactly_one_workspace_project(client, session_headers):
    response = client.get("/api/projects", headers=session_headers)
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["workspace_id"]


def test_legacy_project_creation_is_adapted_to_the_current_project(client, session_headers):
    # The historical multi-project create is adapted: it returns the current
    # project instead of creating a second one, and the response says so.
    before = client.get("/api/projects", headers=session_headers).json()["items"][0]
    created = client.post("/api/projects", headers=session_headers)
    assert created.status_code == 200
    assert created.json()["id"] == before["id"]
    after = client.get("/api/projects", headers=session_headers).json()["items"]
    assert len(after) == 1


def test_project_patch_and_unknown_project(client, session_headers, project_id):
    patched = client.patch(f"/api/projects/{project_id}", json={"title": "重命名"}, headers=session_headers)
    assert patched.status_code == 200
    assert patched.json()["title"] == "重命名"

    missing = client.get("/api/projects/00000000-0000-4000-8000-000000000000", headers=session_headers)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "project_not_found"


def test_sharing_and_redemption_are_explicitly_not_supported(client, session_headers, project_id):
    # Sharing belongs to the control plane. The instance must answer with an
    # explicit migration signal instead of a generic 404 or by touching Docker.
    share = client.post(f"/api/projects/{project_id}/share", headers=session_headers)
    assert share.status_code == 409
    assert share.json()["error"]["code"] == "share_not_supported"

    redeem = client.post("/api/shares/TLM-SHARE-abc/redeem", headers=session_headers)
    assert redeem.status_code == 410
    assert redeem.json()["error"]["code"] == "share_redeem_not_supported"


# --------------------------------------------------------------------------
# documents
# --------------------------------------------------------------------------


def test_document_lifecycle_and_content(client, session_headers, project_id, text_document):
    document = text_document
    assert document["format"] == "md"
    assert document["status"] == "ready"
    assert document["current_revision_id"]
    assert document["size_bytes"] > 0

    fetched = client.get(f"/api/documents/{document['id']}", headers=session_headers)
    assert fetched.status_code == 200
    assert fetched.json()["document"]["id"] == document["id"]

    content = client.get(f"/api/documents/{document['id']}/content", headers=session_headers)
    assert content.status_code == 200
    assert content.json()["text"].startswith("# 标题")

    revisions = client.get(f"/api/documents/{document['id']}/revisions", headers=session_headers)
    assert [rev["revision_no"] for rev in revisions.json()["items"]] == [1]

    listed = client.get(f"/api/projects/{project_id}/documents", headers=session_headers)
    assert [item["id"] for item in listed.json()["items"]] == [document["id"]]


def test_document_creation_requires_a_session(client, project_id):
    response = client.post(
        f"/api/projects/{project_id}/documents", json={"title": "X", "text": "body"}
    )
    assert response.status_code == 401


def test_document_upload_creates_an_immutable_revision(client, session_headers, project_id):
    files = {"file": ("notes.md", b"# Uploaded\n\nbody", "text/markdown")}
    uploaded = client.post(
        f"/api/projects/{project_id}/documents/upload", files=files, headers=session_headers
    )
    assert uploaded.status_code == 201, uploaded.text
    document = uploaded.json()["document"]
    assert document["format"] == "md"
    assert uploaded.json()["revision"]["revision_no"] == 1

    # Replacing the source appends a revision and never rewrites the original.
    replacement = {"file": ("notes.md", b"# Uploaded v2", "text/markdown")}
    second = client.post(
        f"/api/documents/{document['id']}/revisions", files=replacement, headers=session_headers
    )
    assert second.status_code == 201, second.text
    assert second.json()["revision"]["revision_no"] == 2
    assert second.json()["document"]["current_revision_id"] == second.json()["revision"]["id"]

    revisions = client.get(f"/api/documents/{document['id']}/revisions", headers=session_headers).json()["items"]
    assert sorted(rev["revision_no"] for rev in revisions) == [1, 2]


def test_document_upload_rejects_mismatched_media(client, session_headers, project_id):
    # A PDF signature with a .md extension must be rejected, not guessed.
    files = {"file": ("fake.md", b"%PDF-1.4 not really markdown", "text/markdown")}
    response = client.post(
        f"/api/projects/{project_id}/documents/upload", files=files, headers=session_headers
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_media"


def test_content_endpoint_refuses_binary_formats(client, session_headers, project_id):
    # A PDF binary must never be served as utf-8 text by /content.
    pdf = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
    files = {"file": ("paper.pdf", pdf, "application/pdf")}
    uploaded = client.post(
        f"/api/projects/{project_id}/documents/upload", files=files, headers=session_headers
    )
    assert uploaded.status_code == 201, uploaded.text
    document_id = uploaded.json()["document"]["id"]

    content = client.get(f"/api/documents/{document_id}/content", headers=session_headers)
    assert content.status_code == 409
    assert content.json()["error"]["code"] == "content_not_available"

    # The original is still available from /source.
    assert client.get(f"/api/documents/{document_id}/source", headers=session_headers).status_code == 200


def test_document_deletion_removes_content(client, session_headers, project_id, text_document):
    document_id = text_document["id"]
    deleted = client.delete(f"/api/documents/{document_id}", headers=session_headers)
    assert deleted.status_code == 204
    assert client.get(f"/api/documents/{document_id}", headers=session_headers).status_code == 404
# --------------------------------------------------------------------------
# originals: Range / HEAD / ETag / auth
# --------------------------------------------------------------------------


def test_source_requires_authorization(client, text_document):
    response = client.get(f"/api/documents/{text_document['id']}/source")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_source_range_conditional_and_head(client, session_headers, text_document):
    url = f"/api/documents/{text_document['id']}/source"

    full = client.get(url, headers=session_headers)
    assert full.status_code == 200
    assert full.headers["Accept-Ranges"] == "bytes"
    assert int(full.headers["Content-Length"]) == len(full.content)
    etag = full.headers["ETag"]
    assert etag
    assert "attachment" in full.headers["Content-Disposition"]

    # A conditional request with the current validator must not resend the body.
    conditional = client.get(url, headers={**session_headers, "If-None-Match": etag})
    assert conditional.status_code == 304
    assert conditional.content == b""

    # An explicit byte range returns the requested slice and a Content-Range.
    ranged = client.get(url, headers={**session_headers, "Range": "bytes=0-4"})
    assert ranged.status_code == 206
    assert ranged.content == full.content[:5]
    assert ranged.headers["Content-Range"] == f"bytes 0-4/{len(full.content)}"

    # A suffix range asks for the last N bytes.
    suffix = client.get(url, headers={**session_headers, "Range": "bytes=-3"})
    assert suffix.status_code == 206
    assert suffix.content == full.content[-3:]

    # An unsatisfiable range is a 416 with the total size, not a silent full body.
    bad = client.get(url, headers={**session_headers, "Range": f"bytes={len(full.content) + 10}-"})
    assert bad.status_code == 416
    assert bad.headers["Content-Range"] == f"bytes */{len(full.content)}"

    # HEAD reports the same metadata with no body.
    head = client.head(url, headers=session_headers)
    assert head.status_code == 200
    assert head.content == b""
    assert head.headers["ETag"] == etag
    assert int(head.headers["Content-Length"]) == len(full.content)

    head_range = client.head(url, headers={**session_headers, "Range": "bytes=1-3"})
    assert head_range.status_code == 206
    assert head_range.content == b""
    assert head_range.headers["Content-Range"] == f"bytes 1-3/{len(full.content)}"


def test_source_stale_revision_is_gone_not_repointed(client, session_headers, text_document):
    # A ref bound to a revision that no longer exists must fail with 410 rather
    # than silently resolving to the current revision.
    url = f"/api/documents/{text_document['id']}/source"
    response = client.get(f"{url}?revision_id=00000000-0000-4000-8000-000000000000", headers=session_headers)
    assert response.status_code == 410
    assert response.json()["error"]["code"] in ("rev_gone", "revision_gone")


def test_source_unknown_document_is_not_found(client, session_headers):
    response = client.get(
        "/api/documents/00000000-0000-4000-8000-000000000000/source", headers=session_headers
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------
# runs: idempotency / cancel / tex 503
# --------------------------------------------------------------------------


def _make_node(client, session_headers, project_id, document_id) -> str:
    response = client.post(
        f"/api/projects/{project_id}/nodes",
        json={
            "document_id": document_id,
            "title": "锚点节点",
            "kind": "concept",
            "metadata": {"source": {"quote": "正文内容"}},
        },
        headers=session_headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_explanation_run_completes_and_persists_history(
    client, session_headers, project_id, text_document, wait_for_run
):
    node_id = _make_node(client, session_headers, project_id, text_document["id"])
    key = "run-key-1"
    created = client.post(
        "/api/runs",
        json={"kind": "explanation", "input": {"node_id": node_id, "question": "为什么？"}},
        headers={**session_headers, "Idempotency-Key": key},
    )
    assert created.status_code == 202, created.text
    run = created.json()
    assert run["state"] in ("queued", "running")

    finished = wait_for_run(run["id"])
    assert finished["state"] == "completed"
    assert finished["result"]["explanation"]["answer"]
    assert finished["result"]["explanation"]["provider"] == "fake"

    history = client.get(f"/api/projects/{project_id}/explanations", headers=session_headers)
    assert history.status_code == 200
    assert history.json()["items"][0]["node_id"] == node_id


def test_idempotency_key_is_scoped_to_the_workspace(client, session_headers, project_id, text_document, wait_for_run):
    node_id = _make_node(client, session_headers, project_id, text_document["id"])
    key = "same-key"

    first = client.post(
        "/api/runs",
        json={"kind": "explanation", "input": {"node_id": node_id}},
        headers={**session_headers, "Idempotency-Key": key},
    )
    assert first.status_code == 202

    # The same key with the identical body returns the original run, even from a
    # brand-new application session (the scope is the workspace identity).
    second_session = client.post("/api/sessions", json={"invite_code": "TLM-DEMO-TEST"}).json()["access_token"]
    replay = client.post(
        "/api/runs",
        json={"kind": "explanation", "input": {"node_id": node_id}},
        headers={"Authorization": f"Bearer {second_session}", "Idempotency-Key": key},
    )
    assert replay.status_code == 202
    assert replay.json()["id"] == first.json()["id"]

    # The same key with a different body is a conflict, never a silent reuse.
    conflict = client.post(
        "/api/runs",
        json={"kind": "explanation", "input": {"node_id": node_id, "question": "different"}},
        headers={**session_headers, "Idempotency-Key": key},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"


def _gated_client(env, gate):
    """Build the app with a provider whose call blocks until `gate` is released.

    Cancellation is only observable while a run is in flight, so the provider call
    is gated explicitly instead of relying on scheduler timing.
    """
    from backend.adapters.http.app import create_app
    from backend.host.config import HostConfig
    from backend.host.local.provider import FakeProvider
    from fastapi.testclient import TestClient

    class GatedProvider(FakeProvider):
        def explain(self, request):
            gate.wait(timeout=10)
            return super().explain(request)

    config = HostConfig.from_env(env)
    return TestClient(create_app(config, provider=GatedProvider(config.provider.model)))


def test_cancel_prevents_commit(env, wait_for_run):
    """Cancel while the provider call is in flight: the result must not commit."""
    import threading

    gate = threading.Event()
    with _gated_client(env, gate) as client:
        # The gated app has its own session store; start a fresh session on it.
        token = client.post("/api/sessions", json={"invite_code": "TLM-DEMO-TEST"}).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        pid = client.get("/api/projects", headers=headers).json()["items"][0]["id"]
        document = client.post(
            f"/api/projects/{pid}/documents",
            json={"title": "gate", "text": "正文内容"},
            headers=headers,
        ).json()["document"]
        node = client.post(
            f"/api/projects/{pid}/nodes",
            json={"document_id": document["id"], "title": "N", "kind": "concept"},
            headers=headers,
        ).json()

        created = client.post(
            "/api/runs",
            json={"kind": "explanation", "input": {"node_id": node["id"]}},
            headers={**headers, "Idempotency-Key": "cancel-key"},
        )
        assert created.status_code == 202, created.text
        run_id = created.json()["id"]

        # Wait until the run is actually running (inside the gated provider call).
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = client.get(f"/api/runs/{run_id}", headers=headers).json()["state"]
            if state == "running":
                break
            time.sleep(0.02)
        else:
            raise AssertionError("run never reached the running state")

        cancelled = client.post(f"/api/runs/{run_id}/cancel", headers=headers)
        assert cancelled.status_code == 202
        assert cancelled.json()["state"] == "cancelled"

        # Now let the cancelled provider call finish; its result must be dropped.
        gate.set()

        terminal = wait_for_run(run_id)
        assert terminal["state"] == "cancelled"
        assert "result" not in terminal

        # Nothing was persisted to explanation history for the cancelled run.
        assert client.get(f"/api/projects/{pid}/explanations", headers=headers).json()["items"] == []

        # Cancelling again is idempotent rather than an error.
        assert client.post(f"/api/runs/{run_id}/cancel", headers=headers).json()["state"] == "cancelled"


def test_cancel_of_a_finished_run_keeps_its_result(
    client, session_headers, project_id, text_document, wait_for_run
):
    """A terminal run is not retroactively cancelled (no result is discarded)."""
    node_id = _make_node(client, session_headers, project_id, text_document["id"])
    created = client.post(
        "/api/runs",
        json={"kind": "explanation", "input": {"node_id": node_id}},
        headers={**session_headers, "Idempotency-Key": "already-done"},
    )
    run_id = created.json()["id"]
    finished = wait_for_run(run_id)
    assert finished["state"] == "completed"

    after = client.post(f"/api/runs/{run_id}/cancel", headers=session_headers)
    assert after.status_code == 202
    # The completed run keeps its committed result.
    assert after.json()["state"] == "completed"
    assert client.get(f"/api/runs/{run_id}", headers=session_headers).json().get("result")


def test_narration_run_derives_segment_totals(client, session_headers, project_id, text_document, wait_for_run):
    node_ids = [
        _make_node(client, session_headers, project_id, text_document["id"]),
        _make_node(client, session_headers, project_id, text_document["id"]),
    ]
    created = client.post(
        "/api/runs",
        json={"kind": "narration", "input": {"project_id": project_id, "node_ids": node_ids}},
        headers={**session_headers, "Idempotency-Key": "narr-1"},
    )
    assert created.status_code == 202, created.text

    finished = wait_for_run(created.json()["id"])
    assert finished["state"] == "completed"
    narration = finished["result"]["narration"]
    assert narration["segment_count"] == len(narration["segments"]) == 2
    # The total is derived server-side from the segments, not supplied by client.
    assert narration["total_duration_seconds"] == pytest.approx(
        sum(seg["duration_seconds"] for seg in narration["segments"])
    )


def test_narration_rejects_too_many_nodes(client, session_headers, project_id, text_document, wait_for_run):
    node_id = _make_node(client, session_headers, project_id, text_document["id"])
    created = client.post(
        "/api/runs",
        json={"kind": "narration", "input": {"project_id": project_id, "node_ids": [node_id] * 11}},
        headers={**session_headers, "Idempotency-Key": "too-many"},
    )
    assert created.status_code == 202
    finished = wait_for_run(created.json()["id"])
    assert finished["state"] == "failed"
    assert finished['error']['code'] == 'invalid_input'


def test_run_listing_and_unknown_run(client, session_headers):
    assert client.get("/api/runs", headers=session_headers).status_code == 200
    missing = client.get("/api/runs/00000000-0000-4000-8000-000000000000", headers=session_headers)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "run_not_found"


def test_run_events_are_explicitly_not_streamed(client, session_headers, project_id, text_document):
    node_id = _make_node(client, session_headers, project_id, text_document["id"])
    created = client.post(
        "/api/runs",
        json={"kind": "explanation", "input": {"node_id": node_id}},
        headers={**session_headers, "Idempotency-Key": "events-1"},
    )
    response = client.get(f"/api/runs/{created.json()['id']}/events", headers=session_headers)
    # Polling is the contract; an unimplemented stream must say so instead of
    # opening a connection that looks like a stall.
    assert response.status_code == 501
    assert response.json()["error"]["code"] == "not_implemented"


def test_tex_compile_is_unavailable_by_default(client, session_headers, project_id):
    # No safe compiler sandbox exists, so the documented default is a 503 that is
    # never enqueued. The request body is irrelevant at that point.
    files = {"file": ("main.tex", b"\\documentclass{article}\\begin{document}x\\end{document}", "application/x-tex")}
    uploaded = client.post(
        f"/api/projects/{project_id}/documents/upload",
        files=files,
        data={"main": "main.tex"},
        headers=session_headers,
    )
    assert uploaded.status_code == 201, uploaded.text
    revision_id = uploaded.json()["revision"]["id"]

    response = client.post(
        f"/api/documents/{uploaded.json()['document']['id']}/compile",
        json={"revision_id": revision_id, "main": "main.tex"},
        headers={**session_headers, "Idempotency-Key": "tex-1"},
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "tex_compile_unavailable"
    # Nothing was queued for the unavailable capability.
    runs = client.get("/api/runs", headers=session_headers).json()["items"]
    assert not [run for run in runs if run["kind"] == "tex_compile"]


def test_extraction_run_reports_ready_with_an_artifact(
    client, session_headers, project_id, text_document, wait_for_run
):
    created = client.post(
        f"/api/documents/{text_document['id']}/extract",
        json={},
        headers={**session_headers, "Idempotency-Key": "extract-1"},
    )
    assert created.status_code == 202, created.text
    finished = wait_for_run(created.json()["id"])
    assert finished["state"] == "completed"
    assert finished["result"]["extraction"]["status"] == "ready"

    listed = client.get(f"/api/documents/{text_document['id']}/extractions", headers=session_headers)
    assert listed.status_code == 200
    assert listed.json()["items"][0]["extractor_version"]

    artifacts = client.get(f"/api/documents/{text_document['id']}/artifacts", headers=session_headers)
    assert artifacts.status_code == 200
    assert artifacts.json()["items"]
    artifact_id = artifacts.json()["items"][0]["id"]
    fetched = client.get(
        f"/api/documents/{text_document['id']}/artifacts/{artifact_id}/source", headers=session_headers
    )
    assert fetched.status_code == 200
    assert fetched.headers["Accept-Ranges"] == "bytes"


def test_structured_text_requires_a_session(client, text_document):
    response = client.get(f"/api/documents/{text_document['id']}/text")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_structured_text_of_a_text_document_falls_back_to_the_original(
    client, session_headers, text_document
):
    # No extraction has run yet: a text document reports its immutable original
    # (never a binary, never a fabricated page map).
    response = client.get(f"/api/documents/{text_document['id']}/text", headers=session_headers)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["revision_id"] == text_document["current_revision_id"]
    assert payload["extractor_version"]
    assert "hello world" in payload["text"]
    assert "page_count" not in payload


def test_structured_text_after_extraction_reports_the_extractor_version(
    client, session_headers, project_id, text_document, wait_for_run
):
    created = client.post(
        f"/api/documents/{text_document['id']}/extract",
        json={},
        headers={**session_headers, "Idempotency-Key": "text-extract-1"},
    )
    assert created.status_code == 202, created.text
    finished = wait_for_run(created.json()["id"])
    extractor_version = finished["result"]["extraction"]["extractor_version"]

    response = client.get(f"/api/documents/{text_document['id']}/text", headers=session_headers)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["revision_id"] == text_document["current_revision_id"]
    # The endpoint reports the version that actually produced the stored text,
    # so a source_ref can be validated against revision_id + extractor_version.
    assert payload["extractor_version"] == extractor_version
    assert "hello world" in payload["text"]


def test_structured_text_for_binary_without_extraction_is_unavailable(
    client, session_headers, project_id
):
    # A PDF original must never be returned as text by this endpoint unless a
    # ready extraction exists; it must fail closed instead of guessing bytes.
    pdf = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
    files = {"file": ("paper.pdf", pdf, "application/pdf")}
    uploaded = client.post(
        f"/api/projects/{project_id}/documents/upload", files=files, headers=session_headers
    )
    assert uploaded.status_code == 201, uploaded.text
    document_id = uploaded.json()["document"]["id"]

    response = client.get(f"/api/documents/{document_id}/text", headers=session_headers)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "content_not_available"


def test_structured_text_unknown_document_is_not_found(client, session_headers):
    response = client.get("/api/documents/00000000-0000-4000-8000-000000000000/text", headers=session_headers)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "document_not_found"


# --------------------------------------------------------------------------
# graph and error envelope
# --------------------------------------------------------------------------


def test_node_and_edge_roundtrip(client, session_headers, project_id, text_document):
    parent = client.post(
        f"/api/projects/{project_id}/nodes",
        json={"document_id": text_document["id"], "title": "父", "kind": "section"},
        headers=session_headers,
    ).json()
    child = client.post(
        f"/api/projects/{project_id}/nodes",
        json={"document_id": text_document["id"], "title": "子", "kind": "concept", "parent_id": parent["id"]},
        headers=session_headers,
    ).json()

    graph = client.get(f"/api/projects/{project_id}/graph", headers=session_headers).json()
    assert {node["id"] for node in graph["nodes"]} == {parent["id"], child["id"]}

    edge = client.post(
        f"/api/projects/{project_id}/graph/edges",
        json={"source_node_id": parent["id"], "target_node_id": child["id"], "relation": "explains"},
        headers=session_headers,
    )
    assert edge.status_code == 201
    assert client.get(f"/api/projects/{project_id}/graph", headers=session_headers).json()["edges"]

    # Deleting the parent promotes the child instead of cascading.
    assert client.delete(f"/api/nodes/{parent['id']}", headers=session_headers).status_code == 204
    remaining = client.get(f"/api/projects/{project_id}/graph", headers=session_headers).json()
    assert [node["id"] for node in remaining["nodes"]] == [child["id"]]
    assert remaining["nodes"][0]["parent_id"] is None


def test_node_patch_optimistic_concurrency(client, session_headers, project_id, text_document):
    node = client.post(
        f"/api/projects/{project_id}/nodes",
        json={"document_id": text_document["id"], "title": "N", "kind": "concept"},
        headers=session_headers,
    ).json()

    ok = client.patch(f"/api/nodes/{node['id']}", json={"title": "N2", "revision": node["revision"]}, headers=session_headers)
    assert ok.status_code == 200
    assert ok.json()["revision"] == node["revision"] + 1

    # A stale revision must not overwrite the newer state.
    stale = client.patch(f"/api/nodes/{node['id']}", json={"title": "N3", "revision": node["revision"]}, headers=session_headers)
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "revision_conflict"


def test_unknown_api_route_returns_json_404(client):
    response = client.get("/api/does-not-exist")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_error_envelope_shape_is_uniform(client, session_headers, project_id):
    # Every failure path -- auth, not found, conflict -- shares one envelope.
    for response in (
        client.get("/api/projects"),
        client.get("/api/documents/00000000-0000-4000-8000-000000000000", headers=session_headers),
        client.post(f"/api/projects/{project_id}/share", headers=session_headers),
    ):
        payload = response.json()
        assert set(payload) == {"error"}
        assert set(payload["error"]) >= {"code", "message"}
        assert isinstance(payload["error"]["code"], str)
        assert isinstance(payload["error"]["message"], str)


def test_retry_after_is_present_on_saturated_or_unavailable(client, session_headers, project_id):
    files = {"file": ("main.tex", b"\\documentclass{article}", "application/x-tex")}
    uploaded = client.post(
        f"/api/projects/{project_id}/documents/upload", files=files, headers=session_headers
    ).json()
    response = client.post(
        f"/api/documents/{uploaded['document']['id']}/compile",
        json={"revision_id": uploaded["revision"]["id"], "main": "main.tex"},
        headers=session_headers,
    )
    assert response.status_code == 503
    assert response.headers.get("Retry-After") == "1"


def test_security_headers_are_set(client):
    response = client.get("/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
