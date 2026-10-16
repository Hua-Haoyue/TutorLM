"""Shared fixtures for the in-container backend tests.

Every test runs against a temporary data root and a volume-external session
directory, with the ``fake`` provider. No Docker, no network, no control plane:
the HTTP tests exercise the real routers through ``TestClient`` against a fully
local Host graph.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

#: The application package is imported as ``backend.*`` (see pyproject), so the
#: repository's ``instance-app`` directory must be importable.
INSTANCE_APP = Path(__file__).resolve().parents[2]
if str(INSTANCE_APP) not in sys.path:
    sys.path.insert(0, str(INSTANCE_APP))


#: Canonical UUIDs shared by the persistent-mode fixtures below.
WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
OWNER_ID = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def workspace_id() -> str:
    """Canonical workspace UUID (used by persistent-layout tests)."""
    return WORKSPACE_ID


@pytest.fixture
def owner_id() -> str:
    """Canonical owner UUID (used by persistent-layout tests)."""
    return OWNER_ID


@pytest.fixture(autouse=True)
def _isolated_session_dir(tmp_path, monkeypatch):
    """Sessions are always outside the volume, mirroring the deployment contract."""
    session_root = tmp_path / "session" / "s01"
    session_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TUTORLM_SESSION_DATA_DIR", str(session_root))
    return session_root


@pytest.fixture
def env(tmp_path, _isolated_session_dir):
    """Environment for a local (unbound, non-persistent) instance."""
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    return {
        "TUTORLM_RUN_MODE": "demo",
        "TUTORLM_DATA_DIR": str(data),
        "TUTORLM_SESSION_DATA_DIR": str(_isolated_session_dir),
        "TUTORLM_PROVIDER": "fake",
        "TUTORLM_INVITE_CODE": "TLM-DEMO-TEST",
    }


@pytest.fixture
def persistent_env(tmp_path, _isolated_session_dir):
    """Environment for a persistent v3 instance (subdirectory layout)."""
    workspace_id = WORKSPACE_ID
    owner_id = OWNER_ID
    subdirectory = f"users/{owner_id}/workspaces/{workspace_id}"
    mount = tmp_path / "data"
    data = mount / subdirectory
    data.mkdir(parents=True, exist_ok=True)
    return {
        "TUTORLM_RUN_MODE": "managed",
        "TUTORLM_PERSISTENT_WORKSPACE": "true",
        "TUTORLM_WORKSPACE_ID": workspace_id,
        "TUTORLM_OWNER_ID": owner_id,
        "TUTORLM_MOUNT_PATH": str(mount),
        "TUTORLM_DATA_DIR": str(data),
        "TUTORLM_WORKSPACE_SUBDIRECTORY": subdirectory,
        "TUTORLM_SESSION_DATA_DIR": str(_isolated_session_dir),
        "TUTORLM_PROVIDER": "fake",
        "TUTORLM_INVITE_CODE": "TLM-DEMO-TEST",
    }


@pytest.fixture
def client(env):
    """A TestClient over the assembled ASGI app (local mode)."""
    from backend.adapters.http.app import create_app
    from backend.host.config import HostConfig
    from fastapi.testclient import TestClient

    config = HostConfig.from_env(env)
    with TestClient(create_app(config)) as test_client:
        yield test_client


@pytest.fixture
def session_headers(client):
    """Start an application session and return its Authorization header."""
    response = client.post("/api/sessions", json={"invite_code": "TLM-DEMO-TEST"})
    assert response.status_code == 201, response.text
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def project_id(client, session_headers):
    response = client.get("/api/projects", headers=session_headers)
    assert response.status_code == 200, response.text
    return response.json()["items"][0]["id"]


@pytest.fixture
def text_document(client, session_headers, project_id):
    response = client.post(
        f"/api/projects/{project_id}/documents",
        json={"title": "示例文档", "text": "# 标题\n\n正文内容 hello world"},
        headers=session_headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["document"]


@pytest.fixture
def wait_for_run(client, session_headers):
    """Poll a run to a terminal state, mirroring the frontend's polling loop."""
    import time

    def _wait(run_id: str, *, timeout: float = 10.0) -> dict:
        deadline = time.monotonic() + timeout
        payload = {}
        while time.monotonic() < deadline:
            response = client.get(f"/api/runs/{run_id}", headers=session_headers)
            assert response.status_code == 200, response.text
            payload = response.json()
            if payload["state"] in ("completed", "failed", "cancelled", "interrupted"):
                return payload
            time.sleep(0.02)
        raise AssertionError(f"run {run_id} did not reach a terminal state: {payload}")

    return _wait
