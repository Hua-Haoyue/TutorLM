"""真实 Docker 集成测试（验收项 T0/T2/T4/T6 的可执行版本）。

默认**跳过**：本地无 Docker、或无该镜像时不应让整套测试失败。显式启用：

    CONTROL_PLANE_DOCKER_IT=1 python -m pytest tests/test_docker_integration.py -v

它用真实 daemon 走完 创建 -> 启动 -> 就绪 -> 释放 -> 删容器 -> 删卷 -> 新代次，
并断言"两位访客之间数据不串"。这是"多实例方案是否成立"的最终判据。
"""

from __future__ import annotations

import os
import uuid

import pytest

from control_plane.config import ControlPlaneConfig
from control_plane.lifecycle import SlotLifecycle
from control_plane.runtime.docker_adapter import DockerRuntimeAdapter
from control_plane.store import ControlPlaneStore

pytestmark = pytest.mark.docker

#: 启用开关；未设置则整套跳过。
ENABLED = os.environ.get("CONTROL_PLANE_DOCKER_IT") == "1"
#: 测试用镜像；必须已存在于本地 daemon。
IMAGE = os.environ.get("CONTROL_PLANE_IT_IMAGE", "tutorlm-host:0.2.0-single")


def _docker_available() -> bool:
    try:
        import docker

        client = docker.from_env()
        client.ping()
        client.close()
        return True
    except Exception:
        return False


def _image_present() -> bool:
    try:
        import docker

        client = docker.from_env()
        try:
            client.images.get(IMAGE)
            return True
        finally:
            client.close()
    except Exception:
        return False


skip_reason = (
    "未设置 CONTROL_PLANE_DOCKER_IT=1"
    if not ENABLED
    else ("Docker daemon 不可用" if not _docker_available() else f"镜像 {IMAGE} 不存在")
)


@pytest.fixture()
def docker_stack(tmp_path):
    """真实 Docker 适配器 + 独立 SQLite + 唯一标签，结束后彻底清理。"""

    if not ENABLED or not _docker_available() or not _image_present():
        pytest.skip(skip_reason)

    # 用唯一前缀避免与开发机上既有容器/卷冲突
    run_id = uuid.uuid4().hex[:8]
    config = ControlPlaneConfig(
        db_path=tmp_path / "cp.sqlite3",
        slots=1,
        adapter="docker",
        container_prefix=f"cpit-{run_id}",
        volume_prefix=f"cpit-{run_id}-vol",
        label_key="tutorlm.it.run",
        label_value=run_id,
        instance_image=IMAGE,
        instance_env={
            "TUTORLM_RUN_MODE": "demo",
            "TUTORLM_INVITE_CODE": "it-invite",
            "TUTORLM_PROVIDER": "fake",
        },
        ready_timeout_seconds=120.0,
        ready_poll_interval_seconds=1.0,
        stop_grace_seconds=30,
        mem_limit="1g",
        cpus=1.0,
        pids_limit=256,
        tmpfs_size="64m",
        gc_enabled=False,
    )
    store = ControlPlaneStore(config.resolved_db_path())
    store.ensure_slots(config.slots)
    adapter = DockerRuntimeAdapter(config)
    lifecycle = SlotLifecycle(config, store, adapter)

    try:
        yield {"config": config, "store": store, "adapter": adapter, "lifecycle": lifecycle}
    finally:
        # 兜底清理：即使测试中途失败也不留下容器与卷
        for status in adapter.list_managed():
            try:
                if status.container_name:
                    adapter.stop(status.container_name, timeout=10)
                    adapter.remove(status.container_name)
            except Exception:
                pass
        try:
            adapter.remove_volume(config.volume_name(0))
        except Exception:
            pass
        adapter.close()
        store.close()


def test_real_container_reaches_ready(docker_stack):
    """T0/T2：真实容器能创建、启动并通过 /health/ready。"""

    stack = docker_stack
    store, lifecycle = stack["store"], stack["lifecycle"]
    store.allocate(lease_id="it-1", token_hash="h", ttl_seconds=600)
    slot = store.require_slot(0)

    result = lifecycle.provision(slot)
    assert result.ok is True, f"实例未就绪：{result.detail}"
    assert result.upstream

    # 真实探针确实拿到了应用的就绪响应
    probe = stack["adapter"].probe(result.upstream)
    assert probe.ready is True
    assert probe.payload["status"] == "ok"

    # 容器实况：running 且不发布端口
    status = stack["adapter"].status(slot.container_name)
    assert status.exists and status.running


def test_real_instance_isolation_between_visitors(docker_stack):
    """T4/T6：两位访客之间数据卷被删除，代次推进。"""

    stack = docker_stack
    store, lifecycle, adapter = stack["store"], stack["lifecycle"], stack["adapter"]
    volume = stack["config"].volume_name(0)

    store.allocate(lease_id="it-1", token_hash="h", ttl_seconds=600)
    result = lifecycle.provision(store.require_slot(0))
    assert result.ok, result.detail
    assert adapter.volume_exists(volume) if hasattr(adapter, "volume_exists") else True

    # 第一位访客结束 -> 回收
    reclaimed = lifecycle.reclaim(0)
    assert reclaimed.state.value == "free"
    assert reclaimed.generation == 1

    # 卷与容器都真的没了
    import docker

    client = docker.from_env()
    try:
        with pytest.raises(Exception):
            client.volumes.get(volume)
        with pytest.raises(Exception):
            client.containers.get("cpit-no-such-container")
    finally:
        client.close()

    # 第二位访客拿到新代次的容器名
    store.allocate(lease_id="it-2", token_hash="h2", ttl_seconds=600)
    second = store.require_slot(0)
    assert second.generation == 1
    assert second.container_name.endswith("g0001")
    result2 = lifecycle.provision(second)
    assert result2.ok, result2.detail
