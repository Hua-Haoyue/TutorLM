"""共享测试夹具。

每个测试使用 ``tmp_path`` 下的独立 SQLite 与独立 fake adapter，
因此测试之间不共享状态，也永远不接触真实 Docker。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 让 ``import control_plane`` 无论从哪个目录运行都成立。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from control_plane.api.app import build_components, create_app  # noqa: E402
from control_plane.config import ControlPlaneConfig  # noqa: E402
from control_plane.runtime.fake import FakeRuntimeAdapter  # noqa: E402

#: 与配置里的测试管理令牌保持一致。
ADMIN_HEADERS = {"X-Admin-Token": "test-admin-token"}


@pytest.fixture()
def config(tmp_path: Path) -> ControlPlaneConfig:
    return ControlPlaneConfig.for_tests(tmp_path)


@pytest.fixture()
def adapter() -> FakeRuntimeAdapter:
    return FakeRuntimeAdapter()


@pytest.fixture()
def components(config: ControlPlaneConfig, adapter: FakeRuntimeAdapter):
    """不走 HTTP 的核心对象，用于纯逻辑测试。"""

    built = build_components(config, adapter=adapter)
    try:
        yield built
    finally:
        built["store"].close()


@pytest.fixture()
def client(config: ControlPlaneConfig, adapter: FakeRuntimeAdapter):
    """带 fake adapter 的测试客户端；不启动后台 GC 线程。"""

    app = create_app(config, adapter=adapter, run_startup_tasks=False)
    with TestClient(app) as test_client:
        test_client.fake_adapter = adapter  # type: ignore[attr-defined]
        yield test_client


@pytest.fixture()
def visitor_factory(client: TestClient):
    """Independent cookie jars share the app owned by the main client's lifespan."""

    visitors = []

    def create():
        visitor = TestClient(client.app)
        visitors.append(visitor)
        return visitor

    yield create
    for visitor in visitors:
        visitor.close()


@pytest.fixture()
def lease(client: TestClient) -> dict:
    """分配一个租约，返回响应体（含一次性 token）。"""

    response = client.post("/control/leases", json={"label": "pytest"})
    assert response.status_code == 201, response.text
    return response.json()


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
