"""Docker 适配器：不连真实 daemon，用假 client 验证调用契约与安全护栏。

这些测试刻意**不**需要 Docker：用 stub client 断言"生成了哪些 Docker 参数"
以及"是否会拒绝操作非本方资源"。真实 daemon 的集成测试标记为 docker，
本地默认跳过。
"""

from __future__ import annotations

import pytest

from control_plane.config import ControlPlaneConfig
from control_plane.errors import RuntimeUnavailable
from control_plane.ports import InstanceSpec, VolumeSpec
from control_plane.runtime.docker_adapter import DockerRuntimeAdapter


def test_missing_persistent_volume_is_not_created(docker_adapter, stub):
    with pytest.raises(RuntimeUnavailable, match='missing'):
        docker_adapter.ensure_volume(VolumeSpec(name='missing-workspace', must_exist=True))
    assert stub.volumes.created == []


def test_existing_volume_ownership_is_checked_before_mount(docker_adapter, stub):
    stub.volumes.create('vol-s00', labels={'app': 'unrelated-pool'})
    with pytest.raises(RuntimeUnavailable, match='ownership'):
        docker_adapter.create(_spec())
    assert stub.containers.created_kwargs == []


def test_volume_inspection_failure_is_not_treated_as_absence(docker_adapter, monkeypatch):
    def fail(name):
        raise RuntimeError('permission denied')
    monkeypatch.setattr(docker_adapter._client.volumes, 'get', fail)
    with pytest.raises(RuntimeUnavailable, match='inspect'):
        docker_adapter.remove_volume('vol-s00')


def test_volume_removal_never_uses_force(docker_adapter, stub):
    calls = []
    volume = stub.volumes.create('vol-s00', labels={'app': 'tutorlm-instance'})
    volume.remove = lambda force=False: calls.append(force)
    assert docker_adapter.remove_volume('vol-s00') is True
    assert calls == [False]


class _StubContainer:
    def __init__(self, name: str, labels: dict[str, str] | None = None) -> None:
        self.name = name
        self.attrs = {
            "Config": {"Labels": labels if labels is not None else {}},
            "State": {"Status": "running", "Health": {"Status": "healthy"}},
            "Mounts": [{"Type": "volume"}],
        }
        self.started = False
        self.stopped_with: int | None = None
        self.removed: dict | None = None
        #: 由 _StubContainers 注入，remove 时把自身从父 store 摘掉，
        #: 以模拟真实 Docker 删除容器后 get() 会失败的行为。
        self.parent: "_StubContainers | None" = None

    def start(self):
        self.started = True

    def stop(self, timeout: int | None = None):
        self.stopped_with = timeout

    def remove(self, force: bool = False, v: bool = False):
        self.removed = {"force": force, "v": v}
        if self.parent is not None:
            self.parent.store.pop(self.name, None)


class _StubVolumes:
    def __init__(self) -> None:
        self.store: dict[str, object] = {}
        self.created: list[dict] = []

    def get(self, name: str):
        if name not in self.store:
            raise KeyError(name)
        return self.store[name]

    def create(self, name: str, labels: dict | None = None):
        obj = type("V", (), {"attrs": {"Labels": labels or {}}, "remove": lambda self, force=False: None})()
        self.store[name] = obj
        self.created.append({"name": name, "labels": labels})
        return obj


class _StubContainers:
    def __init__(self) -> None:
        self.store: dict[str, _StubContainer] = {}
        self.created_kwargs: list[dict] = []
        self.filters: list[dict] = []

    def get(self, name: str):
        if name not in self.store:
            raise KeyError(name)
        return self.store[name]

    def create(self, **kwargs):
        self.created_kwargs.append(kwargs)
        container = _StubContainer(kwargs["name"], kwargs.get("labels"))
        container.parent = self
        self.store[kwargs["name"]] = container
        return container

    def list(self, all: bool = False, filters: dict | None = None):
        self.filters.append(filters or {})
        wanted = None
        if filters and "label" in filters:
            key, _, value = filters["label"].partition("=")
            wanted = (key, value)
        out = []
        for container in self.store.values():
            labels = container.attrs["Config"]["Labels"]
            if wanted and labels.get(wanted[0]) != wanted[1]:
                continue
            out.append(container)
        return out


class _StubClient:
    def __init__(self) -> None:
        self.containers = _StubContainers()
        self.volumes = _StubVolumes()
        self.closed = False

    def ping(self):
        return True

    def close(self):
        self.closed = True


@pytest.fixture()
def stub() -> _StubClient:
    return _StubClient()


@pytest.fixture()
def docker_adapter(stub):
    config = ControlPlaneConfig.for_tests(
        __import__("pathlib").Path("/tmp"),
        adapter="docker",
        container_prefix="inst",
        volume_prefix="vol",
        instance_image="tutorlm-host:test",
        instance_mount_path="/data",
        mem_limit="1g",
        cpus=1.5,
        pids_limit=128,
        tmpfs_size="64m",
        network="tutorlm",
    )
    return DockerRuntimeAdapter(config, client=stub)


def _spec() -> InstanceSpec:
    return InstanceSpec(
        slot_id=0,
        generation=0,
        container_name="inst-s00-g0000",
        image="tutorlm-host:test",
        volume_name="vol-s00",
        mount_path="/data",
        internal_port=8000,
        env={"TUTORLM_RUN_MODE": "demo"},
        labels={"app": "tutorlm-instance"},
        network="tutorlm",
        mem_limit="1g",
        cpus=1.5,
        pids_limit=128,
        tmpfs_size="64m",
        stop_grace_seconds=30,
    )


def test_create_sends_hardened_spec(docker_adapter, stub):
    docker_adapter.create(_spec())
    kwargs = stub.containers.created_kwargs[0]
    assert kwargs["image"] == "tutorlm-host:test"
    assert kwargs["name"] == "inst-s00-g0000"
    assert kwargs["environment"] == {"TUTORLM_RUN_MODE": "demo"}
    assert kwargs["labels"] == {"app": "tutorlm-instance"}
    assert kwargs["read_only"] is True
    assert kwargs["cap_drop"] == ["ALL"]
    assert kwargs["security_opt"] == ["no-new-privileges:true"]
    assert kwargs["init"] is True
    assert kwargs["pids_limit"] == 128
    assert kwargs["nano_cpus"] == 1_500_000_000
    assert kwargs["mem_limit"] == "1g"
    assert kwargs["network"] == "tutorlm"
    # 挂载必须是具名卷（不会退化成匿名卷）
    mount = kwargs["mounts"][0]
    assert mount["Type"] == "volume"
    assert mount["Source"] == "vol-s00"
    assert mount["Target"] == "/data"


def test_create_ensures_named_volume(docker_adapter, stub):
    docker_adapter.create(_spec())
    assert "vol-s00" in stub.volumes.store
    assert stub.volumes.created == [{"name": "vol-s00", "labels": {"app": "tutorlm-instance"}}]


def test_create_is_idempotent(docker_adapter, stub):
    docker_adapter.create(_spec())
    docker_adapter.create(_spec())
    assert len(stub.containers.created_kwargs) == 1


def test_no_published_ports(docker_adapter, stub):
    """实例端口不能发布到宿主机，否则可以绕过网关直连。"""

    docker_adapter.create(_spec())
    kwargs = stub.containers.created_kwargs[0]
    assert "ports" not in kwargs
    assert "port_bindings" not in kwargs


def test_upstream_uses_internal_network(docker_adapter):
    assert docker_adapter._upstream("inst-s00-g0000") == "http://inst-s00-g0000:8000"


def test_start_stop_remove_flow(docker_adapter, stub):
    docker_adapter.create(_spec())
    docker_adapter.start("inst-s00-g0000")
    assert stub.containers.store["inst-s00-g0000"].started is True
    docker_adapter.stop("inst-s00-g0000", timeout=30)
    assert stub.containers.store["inst-s00-g0000"].stopped_with == 30
    assert docker_adapter.remove("inst-s00-g0000") is True
    assert "inst-s00-g0000" not in stub.containers.store


def test_stop_missing_container_is_not_an_error(docker_adapter):
    status = docker_adapter.stop("ghost", timeout=5)
    assert status.exists is False


def test_remove_missing_container_returns_false(docker_adapter):
    assert docker_adapter.remove("ghost") is False


def test_status_maps_docker_state(docker_adapter, stub):
    docker_adapter.create(_spec())
    status = docker_adapter.status("inst-s00-g0000")
    assert status.exists is True
    assert status.running is True
    assert status.upstream == "http://inst-s00-g0000:8000"


def test_refuses_to_touch_foreign_container(docker_adapter, stub):
    """缺少归属标签的容器必须拒绝操作——不得影响服务器上其他项目。"""

    foreign = _StubContainer(
        "someone-elses-app", {"com.docker.compose.project": "other"}
    )
    foreign.parent = stub.containers
    stub.containers.store["someone-elses-app"] = foreign
    with pytest.raises(RuntimeUnavailable):
        docker_adapter.start("someone-elses-app")
    with pytest.raises(RuntimeUnavailable):
        docker_adapter.remove("someone-elses-app")


def test_list_managed_filters_by_owned_label(docker_adapter, stub):
    docker_adapter.create(_spec())
    other = _StubContainer("other", {"app": "something-else"})
    other.parent = stub.containers
    stub.containers.store["other"] = other
    managed = docker_adapter.list_managed()
    assert [item.container_name for item in managed] == ["inst-s00-g0000"]
    assert stub.containers.filters[-1] == {"label": "app=tutorlm-instance"}


def test_remove_volume_requires_owned_label(docker_adapter, stub):
    stub.volumes.store["vol-foreign"] = type(
        "V", (), {"attrs": {"Labels": {"app": "other"}}, "remove": lambda self, force=False: None}
    )()
    with pytest.raises(RuntimeUnavailable):
        docker_adapter.remove_volume("vol-foreign")


def test_probe_reads_readiness_endpoint(docker_adapter, monkeypatch):
    captured: dict[str, str] = {}

    class _Response:
        status_code = 200

        @staticmethod
        def json() -> dict:
            return {"status": "ok"}

    def fake_get(url, timeout=None):  # noqa: ARG001
        captured["url"] = url
        return _Response()

    import httpx

    monkeypatch.setattr(httpx, "get", fake_get)
    result = docker_adapter.probe("http://inst-s00-g0000:8000")
    assert result.ready is True
    assert captured["url"] == "http://inst-s00-g0000:8000/health/ready"


def test_probe_rejects_non_ok_status(docker_adapter, monkeypatch):
    class _Response:
        status_code = 200

        @staticmethod
        def json() -> dict:
            return {"status": "degraded"}

    import httpx

    monkeypatch.setattr(httpx, "get", lambda url, timeout=None: _Response())
    result = docker_adapter.probe("http://inst:8000")
    assert result.ready is False


def test_probe_handles_unreachable_instance(docker_adapter, monkeypatch):
    import httpx

    def boom(url, timeout=None):  # noqa: ARG001
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "get", boom)
    result = docker_adapter.probe("http://inst:8000")
    assert result.ready is False
    assert "探针请求失败" in (result.detail or "")
