# -*- coding: utf-8 -*-
"""Read-only security acceptance for the real instance-app HTTP surface.

Independent verifier file (single owner). It drives the *real* ASGI app through
``TestClient``/``httpx.ASGITransport`` against the real ``AppState`` graph
(``LocalStore`` + ``LocalStorage`` + ``SessionStore`` + ``RunManager`` + the
in-process provider) -- never domain-only unit helpers.

This file intentionally contains genuine RED reproductions: each P1/P2 case
asserts the SECURITY-CORRECT behaviour and fails against current code so an
owner can fix it. Do not edit production files from here.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

# The package is imported as ``backend.*``; make ``instance-app`` importable
# without touching the shared conftest.
INSTANCE_APP = Path(__file__).resolve().parents[2]
if str(INSTANCE_APP) not in sys.path:
    sys.path.insert(0, str(INSTANCE_APP))

from fastapi.testclient import TestClient  # noqa: E402

from backend.adapters.http.app import create_app  # noqa: E402
from backend.host.config import HostConfig  # noqa: E402

WS = "11111111-1111-4111-8111-111111111111"
OWNER = "22222222-2222-4222-8222-222222222222"
SUB = f"users/{OWNER}/workspaces/{WS}"


# ---------------------------------------------------------------- builders
def _demo_env(root: Path, **overrides: str) -> dict[str, str]:
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    session = root / "session" / "s01"
    session.mkdir(parents=True, exist_ok=True)
    env = {
        "TUTORLM_RUN_MODE": "demo",
        "TUTORLM_DATA_DIR": str(data),
        "TUTORLM_SESSION_DATA_DIR": str(session),
        "TUTORLM_PROVIDER": "fake",
        "TUTORLM_INVITE_CODE": "TLM-ACCEPT-CODE",
    }
    env.update(overrides)
    return env


def _persistent_env(root: Path, *, data_dir: Path, mount: Path, **overrides: str) -> dict[str, str]:
    session = root / "session" / "s01"
    session.mkdir(parents=True, exist_ok=True)
    env = {
        "TUTORLM_RUN_MODE": "managed",
        "TUTORLM_PERSISTENT_WORKSPACE": "true",
        "TUTORLM_WORKSPACE_ID": WS,
        "TUTORLM_OWNER_ID": OWNER,
        "TUTORLM_MOUNT_PATH": str(mount),
        "TUTORLM_DATA_DIR": str(data_dir),
        "TUTORLM_WORKSPACE_SUBDIRECTORY": SUB,
        "TUTORLM_SESSION_DATA_DIR": str(session),
        "TUTORLM_PROVIDER": "fake",
        "TUTORLM_INVITE_CODE": "TLM-ACCEPT-CODE",
    }
    env.update(overrides)
    return env


@pytest.fixture
def demo(tmp_path):
    """A fresh demo (unbound) app on an isolated tmp root."""
    return create_app(HostConfig.from_env(_demo_env(tmp_path)))


@pytest.fixture
def demo_client(demo):
    with TestClient(demo) as client:
        yield client


def _session_headers(client: TestClient) -> dict[str, str]:
    issued = client.post("/api/sessions", json={"invite_code": "TLM-ACCEPT-CODE"})
    assert issued.status_code == 201, issued.text
    return {"Authorization": f"Bearer {issued.json()['access_token']}"}


def _project_id(client: TestClient, headers: dict[str, str]) -> str:
    return client.get("/api/projects", headers=headers).json()["items"][0]["id"]


# =================================================================== P1 RED
def test_p1_persistent_instance_restart_must_not_crash(tmp_path):
    """P1 (RED): a persistent instance cannot survive a normal restart.

    ``host/local/store.py`` (``ensure_binding``/``save_binding``) writes
    ``updated_at`` as an ISO-8601 **string** (``utcnow()``), but
    ``host/binding.py:101`` re-reads it with ``float(row.get("updated_at") or 0.0)``.
    The second ``create_app`` therefore raises ``ValueError`` and the container
    never becomes ready. Reproduced via the real HTTP factory, no fakes.
    """
    mount = tmp_path / "data"
    (mount / SUB).mkdir(parents=True)
    env = _persistent_env(tmp_path, data_dir=mount / SUB, mount=mount)
    config = HostConfig.from_env(env)

    with TestClient(create_app(config)) as client:
        assert client.get("/health/ready").status_code == 200

    # Restart with the SAME binding: this is a plain container restart, not a clone.
    restarted = create_app(HostConfig.from_env(env))
    with TestClient(restarted) as client:
        ready = client.get("/health/ready")
    assert ready.status_code == 200, (
        "persistent restart must re-open the existing binding idempotently; "
        f"got {ready.status_code}: {ready.text[:200]}"
    )


def test_p1_persistent_restart_reads_binding_timestamp(tmp_path):
    """Narrow unit-level companion to the restart test above (real store rows)."""
    mount = tmp_path / "data"
    (mount / SUB).mkdir(parents=True)
    env = _persistent_env(tmp_path, data_dir=mount / SUB, mount=mount)
    create_app(HostConfig.from_env(env))  # first start writes the binding row

    from backend.host.binding import read_binding_row
    from backend.host.local.store import LocalStore

    store = LocalStore(mount / SUB / "business.sqlite3")
    row = store.read_binding()
    assert row is not None and isinstance(row["updated_at"], str)

    # This is the exact call the startup path makes; it must not explode.
    binding = read_binding_row(store)
    assert binding is not None and binding.current_workspace_id == WS
    assert binding.updated_at > 0


def test_p1_remote_provider_must_not_be_silently_faked(tmp_path, monkeypatch):
    """P1 (RED): configured ``openai_compatible`` is advertised but never used.

    ``/api/runtime`` reports ``provider.name == "openai_compatible"`` with an
    API key configured, yet ``host/state.py:107`` always builds the in-process
    ``FakeProvider``. A finished run is stored with ``provider="fake"``. That is
    exactly the "runtime must not claim remote while executing fake" rule.
    """
    import json
    from backend.infrastructure.providers import openai_compatible

    calls = []

    def transport(request, timeout):
        calls.append(request)
        return 200, json.dumps({'choices': [{'message': {'content': 'Remote answer'}}]}).encode()

    monkeypatch.setattr(openai_compatible, 'build_transport', lambda **kwargs: transport)
    env = _demo_env(
        tmp_path,
        TUTORLM_PROVIDER="openai_compatible",
        TUTORLM_MODEL_API_BASE="https://api.example.invalid/v1",
        TUTORLM_MODEL_API_KEY="placeholder-not-a-real-key",
        TUTORLM_MODEL_NAME="gpt-x",
    )
    with TestClient(create_app(HostConfig.from_env(env))) as client:
        runtime = client.get("/api/runtime").json()
        assert runtime["provider"]["name"] == "openai_compatible"

        headers = _session_headers(client)
        pid = _project_id(client, headers)
        doc = client.post(
            f"/api/projects/{pid}/documents",
            json={"title": "t", "text": "hello"},
            headers=headers,
        ).json()["document"]
        node = client.post(
            f"/api/projects/{pid}/nodes",
            json={"document_id": doc["id"], "title": "n"},
            headers=headers,
        ).json()
        run = client.post(
            "/api/runs",
            json={"kind": "explanation", "input": {"node_id": node["id"]}},
            headers=headers,
        ).json()
        for _ in range(200):
            state = client.get(f"/api/runs/{run['id']}", headers=headers).json()
            if state["state"] in ("completed", "failed", "cancelled", "interrupted"):
                break
            time.sleep(0.03)

    explanation = (state.get("result") or {}).get("explanation", {})
    assert state["state"] == "completed", state
    assert explanation.get("provider") != "fake", (
        "runtime advertises a remote provider with a key; the run result must not "
        f"be a fake-provider answer (got provider={explanation.get('provider')!r})"
    )
    assert len(calls) == 1
    assert calls[0].get_header('Authorization') == 'Bearer placeholder-not-a-real-key'


def test_p1_data_dir_must_equal_mount_plus_subdirectory(tmp_path):
    """P1 (RED): ``TUTORLM_DATA_DIR`` may escape the project subdirectory.

    ``host/config.py::from_env`` validates the *shape* of the subdirectory but
    never enforces ``data_dir == mount + subdirectory`` (``domain.volume``
    exposes ``assert_data_dir_matches`` which no Host path calls). A persistent
    instance can therefore accept the whole mount root as its data dir, write
    its marker/business DB at the shared root, and still declare
    ``content_only`` -- over a tree that also holds other owners' workspaces.
    """
    mount = tmp_path / "data"
    (mount / SUB).mkdir(parents=True)
    sibling = mount / "users" / "44444444-4444-4444-8444-444444444444" / "workspaces" / "55555555-5555-4555-8555-555555555555"
    sibling.mkdir(parents=True)
    (sibling / "business.sqlite3").write_bytes(b"OTHER-OWNER-DATA")

    env = _persistent_env(tmp_path, data_dir=mount, mount=mount)  # data_dir == mount root
    from backend.domain.errors import StorageLayoutError
    with pytest.raises(StorageLayoutError):
        HostConfig.from_env(env)
    assert not (mount / 'business.sqlite3').exists()


def test_p1_upload_body_is_bounded_before_buffering(tmp_path):
    """P1 (RED): the multipart cap is applied only *after* buffering the body.

    ``adapters/http/routers/documents.py:31`` does ``await request.body()`` and
    only then ``multipart.parse`` checks ``MAX_BODY_BYTES``. A streamed body with
    no ``Content-Length`` is therefore buffered in full before the cap can fire,
    so the limit cannot bound memory.

    Deterministic probe: drive the real ASGI app directly (no httpx pumping) with
    a finite body somewhat larger than the cap and count the bytes the app pulls
    from ``receive``. A streaming-bounded reader stops near the cap; the current
    reader drains the whole body before answering 413.
    """
    import httpx

    from backend.adapters.http.multipart import MAX_BODY_BYTES

    app = create_app(HostConfig.from_env(_demo_env(tmp_path)))
    chunk = b"z" * 65536
    total = MAX_BODY_BYTES + 4 * 1024 * 1024

    async def scenario() -> tuple[int, int | None]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            token = (await client.post("/api/sessions", json={"invite_code": "TLM-ACCEPT-CODE"})).json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            pid = (await client.get("/api/projects", headers=headers)).json()["items"][0]["id"]

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": f"/api/projects/{pid}/documents/upload",
            "raw_path": f"/api/projects/{pid}/documents/upload".encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"authorization", f"Bearer {token}".encode()),
                (b"content-type", b"multipart/form-data; boundary=B"),
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("t", 80),
        }

        pulled = {"bytes": 0}
        sent = {"n": 0}

        async def receive():
            if sent["n"] >= total:
                return {"type": "http.request", "body": b"", "more_body": False}
            sent["n"] += len(chunk)
            pulled["bytes"] += len(chunk)
            return {"type": "http.request", "body": chunk, "more_body": True}

        status: dict[str, int] = {}

        async def send(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]

        try:
            await asyncio.wait_for(app(scope, receive, send), timeout=20.0)
        except asyncio.TimeoutError:
            pass
        return pulled["bytes"], status.get("code")

    received, status_code = asyncio.run(scenario())
    assert status_code == 413, f"expected 413 for an over-cap streamed body, got {status_code}"
    assert received <= MAX_BODY_BYTES + 1024 * 1024, (
        f"app pulled {received} bytes for a {MAX_BODY_BYTES}-byte cap; the limit must "
        "bound the read while streaming, not run after full buffering"
    )


# ============================================================ GREEN regress
def test_session_expiry_is_410_and_missing_is_401(tmp_path):
    env = _demo_env(tmp_path, TUTORLM_SESSION_TTL_SECONDS="1")
    with TestClient(create_app(HostConfig.from_env(env))) as client:
        token = client.post("/api/sessions", json={"invite_code": "TLM-ACCEPT-CODE"}).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        assert client.get("/api/sessions/me", headers=headers).status_code == 200

        time.sleep(1.3)
        expired = client.get("/api/sessions/me", headers=headers)
        assert expired.status_code == 410
        assert expired.json()["error"]["code"] == "session_expired"

        assert client.get("/api/projects").status_code == 401
        assert client.get("/api/projects", headers={"Authorization": "Bearer nope"}).status_code == 401
        assert client.get("/api/projects", headers={"Authorization": "Basic abc"}).status_code == 401


def test_forged_identity_headers_and_cookie_are_rejected(demo_client):
    token = demo_client.post("/api/sessions", json={"invite_code": "TLM-ACCEPT-CODE"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    forged = {
        **headers,
        "X-User-ID": "attacker",
        "X-User-Token": "attacker",
        "X-Workspace-ID": "attacker-ws",
    }
    me = demo_client.get("/api/sessions/me", headers=forged)
    assert me.status_code == 200
    assert me.json()["session"]["workspace_id"] != "attacker-ws"

    # A cookie carrying the same token is NOT an accepted credential.
    cookie_only = demo_client.get("/api/projects", headers={"Cookie": f"session={token}"})
    assert cookie_only.status_code == 401
    assert cookie_only.json()["error"]["code"] == "unauthorized"


def test_only_one_project_and_cross_project_is_404(demo_client):
    headers = _session_headers(demo_client)
    items = demo_client.get("/api/projects", headers=headers).json()["items"]
    assert len(items) == 1

    other = demo_client.get(
        "/api/projects/00000000-0000-4000-8000-000000000000", headers=headers
    )
    assert other.status_code == 404
    assert other.json()["error"]["code"] == "project_not_found"


def test_source_requires_auth_and_honours_range_etag_head(demo_client):
    headers = _session_headers(demo_client)
    pid = _project_id(demo_client, headers)
    document = demo_client.post(
        f"/api/projects/{pid}/documents",
        json={"title": "t", "text": "abcdefghij"},
        headers=headers,
    ).json()["document"]
    src = f"/api/documents/{document['id']}/source"

    assert demo_client.get(src).status_code == 401

    full = demo_client.get(src, headers=headers)
    assert full.status_code == 200
    etag = full.headers["etag"]
    assert full.headers["accept-ranges"] == "bytes"

    part = demo_client.get(src, headers={**headers, "Range": "bytes=2-5"})
    assert part.status_code == 206
    assert part.content == b"cdef"
    assert part.headers["content-range"] == "bytes 2-5/10"

    assert demo_client.get(src, headers={**headers, "If-None-Match": etag}).status_code == 304

    head = demo_client.head(src, headers=headers)
    assert head.status_code == 200 and head.headers["content-length"] == "10"

    assert demo_client.get(src, headers={**headers, "Range": "bytes=999-1000"}).status_code == 416


def test_source_revision_must_belong_to_the_document(demo_client):
    headers = _session_headers(demo_client)
    pid = _project_id(demo_client, headers)
    first = demo_client.post(
        f"/api/projects/{pid}/documents", json={"title": "a", "text": "alpha"}, headers=headers
    ).json()["document"]
    second = demo_client.post(
        f"/api/projects/{pid}/documents", json={"title": "b", "text": "beta"}, headers=headers
    ).json()["document"]

    # The other document's revision must not resolve against this document.
    response = demo_client.get(
        f"/api/documents/{first['id']}/source?revision_id={second['current_revision_id']}",
        headers=headers,
    )
    assert response.status_code == 410
    assert response.json()["error"]["code"] == "revision_gone"


def test_session_gc_does_not_touch_persisted_content(tmp_path):
    env = _demo_env(tmp_path)
    data = Path(env["TUTORLM_DATA_DIR"])
    session_root = Path(env["TUTORLM_SESSION_DATA_DIR"])
    with TestClient(create_app(HostConfig.from_env(env))) as client:
        headers = _session_headers(client)
        pid = _project_id(client, headers)
        client.post(
            f"/api/projects/{pid}/documents",
            json={"title": "keep", "text": "keep me"},
            headers=headers,
        )
        assert client.delete("/api/sessions/me", headers=headers).status_code == 202

    # A fresh session still sees the durable content.
    with TestClient(create_app(HostConfig.from_env(env))) as client:
        headers = _session_headers(client)
        docs = client.get(f"/api/projects/{pid}/documents", headers=headers).json()["items"]
    assert [d["title"] for d in docs] == ["keep"]

    # Sessions live outside the volume; the data root holds business content only.
    assert (session_root / "sessions.sqlite3").exists()
    assert not (data / "sessions.sqlite3").exists()
    names = {p.name for p in data.iterdir()}
    assert "sessions" not in names and "session" not in names


def test_upload_filename_traversal_is_sanitized_server_side(demo_client):
    headers = _session_headers(demo_client)
    pid = _project_id(demo_client, headers)
    body = (
        b"--B\r\nContent-Disposition: form-data; name=\"file\"; "
        b"filename=\"../../etc/pwned.txt\"\r\nContent-Type: text/plain\r\n\r\nhello\r\n--B--\r\n"
    )
    uploaded = demo_client.post(
        f"/api/projects/{pid}/documents/upload",
        content=body,
        headers={**headers, "Content-Type": "multipart/form-data; boundary=B"},
    )
    assert uploaded.status_code == 201, uploaded.text
    document = uploaded.json()["document"]
    # The stored name is server-sanitized: no directory component, no traversal.
    assert "/" not in document["source_filename"] and "\\" not in document["source_filename"]
    assert ".." not in document["source_filename"]
    assert document["title"] == "pwned"

    sources_root = next(p for p in (Path(demo_client.app.state.tutorlm.config.data_dir)).iterdir() if p.name == "sources")
    leaked = [p for p in sources_root.rglob("*") if p.name == "etc" or p.name == "pwned.txt"]
    assert not leaked


def test_file_size_limit_is_413(tmp_path):
    env = _demo_env(tmp_path, TUTORLM_MAX_BYTES_PER_FILE="1024")
    with TestClient(create_app(HostConfig.from_env(env))) as client:
        headers = _session_headers(client)
        pid = _project_id(client, headers)
        too_big = client.post(
            f"/api/projects/{pid}/documents",
            json={"title": "t", "text": "x" * 2000},
            headers=headers,
        )
        assert too_big.status_code == 413
        assert too_big.json()["error"]["code"] == "payload_too_large"


def test_credentials_subtree_inside_volume_blocks_content_only(tmp_path):
    mount = tmp_path / "data"
    data = mount / SUB
    data.mkdir(parents=True)
    (data / "sessions").mkdir()  # a session-like subtree inside the project volume
    env = _persistent_env(tmp_path, data_dir=data, mount=mount)
    with TestClient(create_app(HostConfig.from_env(env))) as client:
        ready = client.get("/health/ready")
    assert ready.status_code == 503
    assert ready.json().get("storage_layout") != "content_only"
    assert ready.json()["status"] != "ok"
