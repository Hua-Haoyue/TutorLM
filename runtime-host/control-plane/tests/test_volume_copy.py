"""持久项目卷复制适配器：用 stub 与 fake 聚焦验证，不接触真实 Docker。

覆盖三类契约：

* ``VolumeCopySpec`` 的路径/归属校验（严格相对、无 ``..``、源目标都需标签）；
* ``FakeRuntimeAdapter.copy_volume`` 的内存语义（按目录映射复制成独立 bytes、
  排除路径、拒绝覆盖、故障注入）；
* ``DockerRuntimeAdapter.copy_volume`` 的 Docker 调用契约与安全护栏
  （helper 只回补 CHOWN、源只读目标可写、禁网、无特权、不拉镜像、
  拒绝在线源、清理与回滚必须暴露失败）。
"""

from __future__ import annotations

from pathlib import Path
import io
import json
import posixpath
import stat
from types import SimpleNamespace

import pytest

from control_plane.config import ControlPlaneConfig
from control_plane.errors import RuntimeUnavailable
from control_plane.ports import VolumeCopySpec, VolumeSpec
from control_plane.runtime.docker_adapter import DockerRuntimeAdapter
from control_plane.runtime.fake import FakeRuntimeAdapter

LABELS = {"app": "tutorlm-instance"}


def _source(**overrides) -> VolumeSpec:
    values = {"name": "vol-source", "labels": dict(LABELS)}
    values.update(overrides)
    return VolumeSpec(**values)


def _destination(**overrides) -> VolumeSpec:
    values = {"name": "vol-destination", "labels": dict(LABELS)}
    values.update(overrides)
    return VolumeSpec(**values)


def _copy_spec(**overrides) -> VolumeCopySpec:
    values = {
        "source": _source(),
        "destination": _destination(),
        "source_directory": "users/src-user/workspaces/src-workspace",
        "destination_directory": "users/recipient/workspaces/new-workspace",
    }
    values.update(overrides)
    return VolumeCopySpec(**values)


# =====================================================================  规格
def test_spec_normalizes_relative_directories_and_excludes():
    spec = _copy_spec(
        source_directory="./users/u1/workspaces/w1/",
        destination_directory="users/u2/workspaces/w2",
        exclude_paths=("./secrets/", "session.json"),
    )
    assert spec.source_directory == "users/u1/workspaces/w1"
    assert spec.destination_directory == "users/u2/workspaces/w2"
    assert spec.exclude_paths == ("secrets", "session.json")


def test_spec_defaults_represent_the_whole_volume():
    spec = _copy_spec(source_directory=".", destination_directory=".")
    assert spec.source_directory == "."
    assert spec.destination_directory == "."
    assert spec.exclude_paths == ()
    assert spec.exclude_names == ()


@pytest.mark.parametrize('bad', ['', ' ', '.', '..', '/auth', 'sub/sessions',
                                 'sub\\sessions', 'C:auth', 'auth\x00', None, 42])
def test_spec_rejects_invalid_exclude_names(bad):
    with pytest.raises(RuntimeUnavailable, match='exclude_names'):
        _copy_spec(exclude_names=(bad,))


def test_spec_keeps_exclude_names_separate_from_relative_paths():
    spec = _copy_spec(exclude_names=('sessions', 'token.json'),
                      exclude_paths=('private/config',))
    assert spec.exclude_names == ('sessions', 'token.json')
    assert spec.exclude_paths == ('private/config',)


@pytest.mark.parametrize('depth', ['', 'sub/', 'sub/deeper/'])
def test_fake_excludes_reserved_names_at_every_depth(fake, depth):
    _seed_source(fake)
    prefix = 'users/src-user/workspaces/src-workspace/'
    for name in ('sessions/secret', 'auth/token', 'session', 'token.json'):
        fake.write_volume('vol-source', prefix + depth + name, b'credential')
    fake.write_volume('vol-source', prefix + depth + 'sessions-notes.txt', b'business')
    fake.copy_volume(_copy_spec(
        exclude_names=('sessions', 'auth', 'session', 'token.json'),
        exclude_paths=('session.json',),
    ))
    copied = fake._volume_files['vol-destination']
    target = 'users/recipient/workspaces/new-workspace/'
    assert copied == {
        target + 'project.sqlite3': b'business-data',
        target + 'sources/notes.md': b'# notes',
        target + depth + 'sessions-notes.txt': b'business',
    }
    assert fake.read_volume('vol-source', prefix + depth + 'sessions/secret') == b'credential'


def test_fake_exclude_paths_remain_root_relative(fake):
    _seed_source(fake)
    prefix = 'users/src-user/workspaces/src-workspace/'
    fake.write_volume('vol-source', prefix + 'sessions/secret', b'root')
    fake.write_volume('vol-source', prefix + 'sub/sessions/notes', b'nested')
    fake.copy_volume(_copy_spec(exclude_paths=('sessions',)))
    copied = fake._volume_files['vol-destination']
    assert 'users/recipient/workspaces/new-workspace/sessions/secret' not in copied
    assert copied['users/recipient/workspaces/new-workspace/sub/sessions/notes'] == b'nested'


@pytest.mark.parametrize('depth', ['', 'sub/', 'sub/deeper/'])
def test_helper_excludes_reserved_names_without_entering_directories(depth):
    from control_plane.runtime.docker_adapter import _COPY_SCRIPT

    # Execute the real helper traversal against an in-memory filesystem only.
    namespace = {}
    exec(compile(_COPY_SCRIPT.rsplit('raise SystemExit(main())', 1)[0],
                 '<copy-helper>', 'exec'), namespace)
    files = {
        depth + 'sessions/secret': b'secret',
        depth + 'auth/token': b'secret',
        depth + 'session': b'secret',
        depth + 'token.json': b'secret',
        depth + 'sessions-notes.txt': b'business',
        'root-only/private': b'secret',
        'sub/root-only/notes': b'business',
    }
    directories = {'/copy/source'}
    for key in files:
        parent = posixpath.dirname('/copy/source/' + key)
        while parent.startswith('/copy/source'):
            directories.add(parent)
            parent = posixpath.dirname(parent)
    visited = []

    def scandir(path):
        visited.append(path)
        children = set(directories) | {'/copy/source/' + key for key in files}
        return [SimpleNamespace(
            name=posixpath.basename(child), path=child,
            stat=lambda follow_symlinks=False, child=child: SimpleNamespace(
                st_mode=stat.S_IFDIR if child in directories else stat.S_IFREG),
        ) for child in sorted(children) if posixpath.dirname(child) == path]

    copied = {}
    namespace['os'] = SimpleNamespace(
        path=SimpleNamespace(join=posixpath.join, islink=lambda path: False,
                             isdir=lambda path: path in directories),
        scandir=scandir, makedirs=lambda *args, **kwargs: None,
    )
    namespace['sys'] = SimpleNamespace(
        argv=['helper', '.', '.', '10001', '10001', '120',
              json.dumps(['sessions', 'auth', 'session', 'token.json']), 'root-only'],
        stdout=io.StringIO(), stderr=io.StringIO(),
    )
    namespace['fix'] = lambda path, is_dir: None
    namespace['copy_file'] = lambda source, target, deadline: copied.update({
        target.removeprefix('/copy/target/'): files[source.removeprefix('/copy/source/')],
    })
    assert namespace['main']() == 0
    assert copied == {depth + 'sessions-notes.txt': b'business',
                      'sub/root-only/notes': b'business'}
    assert not any(posixpath.basename(path) in {'sessions', 'auth'} for path in visited)


def test_docker_transports_exclude_names_separately(docker_adapter, stub):
    _seed_owned_source(stub)
    docker_adapter.copy_volume(_copy_spec(
        exclude_names=('sessions', 'auth', 'token.json'),
        exclude_paths=('root-only/private',),
    ))
    command = _helper_kwargs(stub)['entrypoint']
    assert json.loads(command[8]) == ['sessions', 'auth', 'token.json']
    assert command[9:] == ['root-only/private']


@pytest.mark.parametrize("bad", ["/abs/path", "\\\\abs\\\\path", "C:\\\\windows", "../up", "a/../b", "  "])
def test_spec_rejects_non_relative_or_escaping_directories(bad):
    with pytest.raises(RuntimeUnavailable):
        _copy_spec(source_directory=bad)


def test_spec_requires_labels_and_distinct_volumes():
    with pytest.raises(RuntimeUnavailable, match="归属标签"):
        _copy_spec(source=VolumeSpec(name="vol-source"))
    with pytest.raises(RuntimeUnavailable, match="归属标签"):
        _copy_spec(destination=VolumeSpec(name="vol-destination"))
    with pytest.raises(RuntimeUnavailable, match="不能是同一个卷"):
        _copy_spec(destination=_source())


def test_spec_rejects_non_positive_timeout():
    with pytest.raises(RuntimeUnavailable, match="timeout_seconds"):
        _copy_spec(timeout_seconds=0)


# ====================================================================  fake
@pytest.fixture()
def fake() -> FakeRuntimeAdapter:
    return FakeRuntimeAdapter()


def _seed_source(fake: FakeRuntimeAdapter, name: str = "vol-source") -> None:
    assert fake.ensure_volume(VolumeSpec(name=name, labels=dict(LABELS))) is True
    fake.write_volume(name, "users/src-user/workspaces/src-workspace/project.sqlite3", b"business-data")
    fake.write_volume(name, "users/src-user/workspaces/src-workspace/sources/notes.md", b"# notes")
    fake.write_volume(name, "users/src-user/workspaces/src-workspace/session.json", b"secret-token")
    fake.write_volume(name, "users/src-user/other/unrelated.txt", b"outside")


def test_fake_copies_directory_subtree_as_independent_bytes(fake):
    _seed_source(fake)
    fake.copy_volume(_copy_spec(exclude_paths=("session.json",)))

    copied = fake._volume_files["vol-destination"]
    assert copied == {
        "users/recipient/workspaces/new-workspace/project.sqlite3": b"business-data",
        "users/recipient/workspaces/new-workspace/sources/notes.md": b"# notes",
    }
    assert copied is not fake._volume_files["vol-source"]

    # 副本必须是独立 bytes：改动源卷不影响目标卷。
    fake.write_volume("vol-source", "users/src-user/workspaces/src-workspace/project.sqlite3", b"MUTATED")
    assert fake._volume_files["vol-destination"][
        "users/recipient/workspaces/new-workspace/project.sqlite3"
    ] == b"business-data"


def test_fake_default_directories_copy_whole_volume(fake):
    _seed_source(fake)
    fake.copy_volume(_copy_spec(source_directory=".", destination_directory="."))
    assert set(fake._volume_files["vol-destination"]) == set(fake._volume_files["vol-source"])


def test_fake_requires_source_volume_to_exist(fake):
    with pytest.raises(RuntimeUnavailable, match="不存在"):
        fake.copy_volume(_copy_spec())


def test_fake_refuses_mismatched_source_ownership(fake):
    _seed_source(fake)
    with pytest.raises(RuntimeUnavailable, match="归属标签"):
        fake.copy_volume(_copy_spec(source=_source(labels={"app": "other-project"})))
    assert not fake.volume_exists("vol-destination")


def test_fake_refuses_to_overwrite_existing_destination(fake):
    _seed_source(fake)
    assert fake.ensure_volume(VolumeSpec(name="vol-destination", labels=dict(LABELS))) is True
    fake.write_volume("vol-destination", "keep.txt", b"existing")
    with pytest.raises(RuntimeUnavailable, match="覆盖"):
        fake.copy_volume(_copy_spec())
    assert fake.read_volume("vol-destination", "keep.txt") == b"existing"


def test_fake_fault_injection_blocks_copy(fake):
    _seed_source(fake)
    fake.fail_copy.add("vol-destination")
    with pytest.raises(RuntimeUnavailable, match="注入"):
        fake.copy_volume(_copy_spec())
    assert not fake.volume_exists("vol-destination")


def test_fake_records_the_copy_call(fake):
    _seed_source(fake)
    fake.copy_volume(_copy_spec())
    assert ("copy_volume", "vol-destination") in fake.calls


# ===================================================================  docker
class _StubHelper:
    """最小 helper 容器替身，记录生命周期调用顺序。"""

    def __init__(self, name: str, labels: dict[str, str], wait_result, wait_error=None) -> None:
        self.name = name
        self.labels = labels
        self._wait_result = wait_result
        self._wait_error = wait_error
        self.events: list[str] = []
        self.wait_timeouts: list[int] = []

    def start(self):
        self.events.append("start")

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        self.events.append("wait")
        if self._wait_error is not None:
            raise self._wait_error
        return self._wait_result

    def logs(self, stdout=True, stderr=True):
        return b"helper output"

    def stop(self, timeout=None):
        self.events.append("stop")

    def kill(self):
        self.events.append("kill")

    def remove(self, force=False, v=False):
        self.events.append("remove")


class _StubContainer:
    def __init__(self, name: str, labels: dict[str, str], mounts=None, status: str = "running") -> None:
        self.name = name
        self.attrs = {
            "Config": {"Labels": labels},
            "State": {"Status": status},
            "Mounts": mounts or [],
        }


class _StubVolume:
    """卷替身：``remove`` 是真实方法，避免把回调错误地绑定成方法。"""

    def __init__(self, name: str, labels: dict[str, str], on_remove) -> None:
        self.name = name
        self.attrs = {"Labels": labels}
        self.removed_with: list[bool] = []
        self._on_remove = on_remove

    def remove(self, force: bool = False):
        self.removed_with.append(force)
        if self._on_remove is not None:
            self._on_remove(force)


class _StubVolumes:
    def __init__(self) -> None:
        self.store: dict[str, _StubVolume] = {}
        self.created: list[dict] = []
        #: 卷名 -> 删除时的回调（抛错即可模拟回滚失败）。
        self.on_remove: dict[str, object] = {}

    def get(self, name: str):
        if name not in self.store:
            raise KeyError(name)
        return self.store[name]

    def create(self, name: str, labels=None):
        labels = dict(labels or {})
        volume = _StubVolume(name, labels, self.on_remove.get(name))
        self.store[name] = volume
        self.created.append({"name": name, "labels": labels})
        return volume

    def put(self, name: str, labels: dict[str, str], on_remove=None):
        self.store[name] = _StubVolume(name, dict(labels), on_remove)
        return self.store[name]


class _StubImages:
    def __init__(self, present: set[str]) -> None:
        self.present = set(present)
        self.get_calls: list[str] = []
        self.pulls: list[str] = []

    def get(self, name: str):
        self.get_calls.append(name)
        if name not in self.present:
            from docker.errors import ImageNotFound

            raise ImageNotFound(name)
        return object()

    def pull(self, name: str):
        self.pulls.append(name)
        raise AssertionError("copy_volume 不得拉取镜像")


class _StubContainers:
    def __init__(self) -> None:
        self.created_kwargs: list[dict] = []
        self.helpers: list[_StubHelper] = []
        self.managed: list[_StubContainer] = []
        self.wait_result = {"StatusCode": 0}
        self.wait_error: Exception | None = None

    def create(self, **kwargs):
        helper = _StubHelper(kwargs["name"], dict(kwargs.get("labels") or {}), self.wait_result, self.wait_error)
        self.created_kwargs.append(kwargs)
        self.helpers.append(helper)
        return helper

    def list(self, all=False, filters=None):  # noqa: ARG002
        return list(self.managed)


class _StubClient:
    def __init__(self, images_present: set[str]) -> None:
        self.containers = _StubContainers()
        self.volumes = _StubVolumes()
        self.images = _StubImages(images_present)
        self.closed = False

    def ping(self):
        return True

    def close(self):
        self.closed = True


@pytest.fixture()
def stub() -> _StubClient:
    return _StubClient(images_present={"tutorlm-copy-helper:local"})


@pytest.fixture()
def docker_adapter(stub) -> DockerRuntimeAdapter:
    config = ControlPlaneConfig.for_tests(
        Path("var") / "cp-test",
        adapter="docker",
        container_prefix="inst",
        volume_prefix="vol",
        instance_image="tutorlm-copy-helper:local",
        instance_mount_path="/data",
        mem_limit="1g",
        cpus=1.5,
        pids_limit=128,
        tmpfs_size="64m",
    )
    return DockerRuntimeAdapter(config, client=stub)


def _seed_owned_source(stub: _StubClient, name: str = "vol-source") -> None:
    stub.volumes.put(name, LABELS)


def _helper_kwargs(stub: _StubClient) -> dict:
    assert len(stub.containers.created_kwargs) == 1
    return stub.containers.created_kwargs[0]


def test_docker_helper_is_hardened_and_mounts_are_scoped(docker_adapter, stub):
    _seed_owned_source(stub)
    docker_adapter.copy_volume(_copy_spec())

    kwargs = _helper_kwargs(stub)
    assert kwargs["cap_drop"] == ["ALL"]
    assert kwargs["cap_add"] == ["CHOWN"]
    assert kwargs["security_opt"] == ["no-new-privileges:true"]
    assert kwargs["user"] == "0:0"
    assert kwargs["network_disabled"] is True
    assert kwargs["read_only"] is True
    assert kwargs["init"] is True
    assert kwargs["mem_limit"] == "1g"
    assert kwargs["nano_cpus"] == 1_500_000_000
    assert kwargs["pids_limit"] == 128
    assert kwargs["tmpfs"] == {"/tmp": "size=64m,mode=1777"}
    assert "privileged" not in kwargs
    assert "ports" not in kwargs and "port_bindings" not in kwargs

    source_mount, target_mount = kwargs["mounts"]
    assert source_mount["Source"] == "vol-source"
    assert source_mount["Type"] == "volume"
    assert source_mount["ReadOnly"] is True
    assert target_mount["Source"] == "vol-destination"
    assert target_mount["ReadOnly"] is False


def test_docker_helper_is_owned_labeled_and_uniquely_named(docker_adapter, stub):
    _seed_owned_source(stub)
    docker_adapter.copy_volume(_copy_spec())
    kwargs = _helper_kwargs(stub)
    assert kwargs["labels"]["app"] == "tutorlm-instance"
    assert kwargs["labels"]["tutorlm.copy"] == "volume"
    assert kwargs["name"].startswith("inst-copy-")
    assert kwargs["entrypoint"][0] == "python3"


def test_helper_script_never_follows_symlinks_for_ownership(docker_adapter):
    from control_plane.runtime import docker_adapter as module

    script = module._COPY_SCRIPT
    assert "os.lchown(" in script
    assert "os.chown(" not in script
    assert "stat.S_ISLNK(os.lstat(" in script
    # 仍只拒绝 symlink，不静默跟随。
    assert "refusing symlink" in script


def test_docker_helper_receives_directories_and_excludes(docker_adapter, stub):
    _seed_owned_source(stub)
    docker_adapter.copy_volume(_copy_spec(exclude_paths=("secrets", "session.json")))
    entrypoint = _helper_kwargs(stub)["entrypoint"]
    assert "users/src-user/workspaces/src-workspace" in entrypoint
    assert "users/recipient/workspaces/new-workspace" in entrypoint
    assert entrypoint[-2:] == ["secrets", "session.json"]


def test_docker_refuses_missing_helper_image_without_pulling(docker_adapter, stub):
    _seed_owned_source(stub)
    with pytest.raises(RuntimeUnavailable, match="拒绝隐式拉取"):
        docker_adapter.copy_volume(_copy_spec(helper_image="not-built:latest"))
    assert stub.images.pulls == []
    assert stub.containers.created_kwargs == []
    assert not stub.volumes.created


def test_docker_falls_back_to_configured_image_when_unset(docker_adapter, stub):
    _seed_owned_source(stub)
    docker_adapter.copy_volume(_copy_spec())
    assert stub.images.get_calls == ["tutorlm-copy-helper:local"]
    assert _helper_kwargs(stub)["image"] == "tutorlm-copy-helper:local"


def test_docker_requires_source_volume_ownership(docker_adapter, stub):
    with pytest.raises(RuntimeUnavailable, match="不存在"):
        docker_adapter.copy_volume(_copy_spec())
    stub.volumes.put("vol-source", {"app": "other-project"})
    with pytest.raises(RuntimeUnavailable, match="非本方数据卷"):
        docker_adapter.copy_volume(_copy_spec())
    assert stub.containers.created_kwargs == []


def test_docker_refuses_to_overwrite_existing_destination(docker_adapter, stub):
    _seed_owned_source(stub)
    stub.volumes.put("vol-destination", LABELS)
    with pytest.raises(RuntimeUnavailable, match="覆盖"):
        docker_adapter.copy_volume(_copy_spec())
    assert stub.containers.created_kwargs == []


def test_docker_refuses_online_source_writer(docker_adapter, stub):
    _seed_owned_source(stub)
    stub.containers.managed.append(
        _StubContainer(
            "inst-s00-g0001",
            dict(LABELS),
            mounts=[{"Type": "volume", "Name": "vol-source"}],
            status="running",
        )
    )
    with pytest.raises(RuntimeUnavailable, match="在线快照"):
        docker_adapter.copy_volume(_copy_spec())
    assert stub.containers.created_kwargs == []


def test_docker_refuses_foreign_writer_on_same_volume(docker_adapter, stub):
    """非本方容器挂在同一个源卷上时也必须被发现并拒绝，不能只看本方标签。"""

    _seed_owned_source(stub)
    stub.containers.managed.append(
        _StubContainer(
            "someone-elses-app",
            {"com.docker.compose.project": "other"},
            mounts=[{"Type": "volume", "Name": "vol-source"}],
            status="running",
        )
    )
    with pytest.raises(RuntimeUnavailable, match="在线快照"):
        docker_adapter.copy_volume(_copy_spec())
    assert stub.containers.created_kwargs == []
    # 只读扫描：不得因为发现外部写入者就去操作它。
    assert [c.name for c in stub.containers.managed] == ["someone-elses-app"]


def test_docker_scan_uses_volume_filter_not_owned_label(docker_adapter, stub):
    _seed_owned_source(stub)
    seen: list[dict] = []
    original = stub.containers.list

    def spy(all=False, filters=None):
        seen.append(dict(filters or {}))
        return original(all=all, filters=filters)

    stub.containers.list = spy  # type: ignore[assignment]
    docker_adapter.copy_volume(_copy_spec())
    assert seen == [{"volume": "vol-source"}]


def test_docker_ignores_stopped_writer(docker_adapter, stub):
    _seed_owned_source(stub)
    stub.containers.managed.append(
        _StubContainer(
            "inst-s00-g0001",
            dict(LABELS),
            mounts=[{"Type": "volume", "Name": "vol-source"}],
            status="exited",
        )
    )
    docker_adapter.copy_volume(_copy_spec())
    assert len(stub.containers.created_kwargs) == 1


def test_docker_success_is_bounded_and_helper_is_removed(docker_adapter, stub):
    _seed_owned_source(stub)
    docker_adapter.copy_volume(_copy_spec(timeout_seconds=45))
    helper = stub.containers.helpers[0]
    assert helper.wait_timeouts == [45 + 10]
    assert helper.events == ["start", "wait", "stop", "remove"]
    assert stub.volumes.created == [{"name": "vol-destination", "labels": dict(LABELS)}]


def test_docker_nonzero_exit_rolls_back_destination(docker_adapter, stub):
    _seed_owned_source(stub)
    stub.containers.wait_result = {"StatusCode": 3}
    stub.volumes.on_remove["vol-destination"] = lambda force: None
    with pytest.raises(RuntimeUnavailable, match="退出码 3"):
        docker_adapter.copy_volume(_copy_spec())
    helper = stub.containers.helpers[0]
    assert "remove" in helper.events
    assert stub.volumes.store["vol-destination"].removed_with == [False]
    assert "vol-destination" not in _ok_volumes(stub)


def test_docker_timeout_kills_helper_and_rolls_back(docker_adapter, stub):
    _seed_owned_source(stub)
    stub.containers.wait_error = TimeoutError("read timed out")
    stub.volumes.on_remove["vol-destination"] = lambda force: None
    with pytest.raises(RuntimeUnavailable, match="超时或被中断"):
        docker_adapter.copy_volume(_copy_spec())
    helper = stub.containers.helpers[0]
    assert "kill" in helper.events
    assert "remove" in helper.events
    assert stub.volumes.store["vol-destination"].removed_with == [False]


def test_docker_cleanup_failure_is_surfaced_not_swallowed(docker_adapter, stub):
    _seed_owned_source(stub)

    def boom(force=False, v=False):
        raise RuntimeError("device busy")

    stub.containers.create = _failing_remove_factory(stub, boom)  # type: ignore[assignment]
    with pytest.raises(RuntimeUnavailable, match="helper 清理失败") as caught:
        docker_adapter.copy_volume(_copy_spec())
    details = caught.value.details
    # 复制本身成功，但 helper 残留；上游必须能据此发现残留状态。
    assert details["copy_completed"] is True
    assert details["helper_cleaned"] is False
    assert details["destination_rolled_back"] is True
    assert details["destination_may_contain_partial_data"] is False
    assert details["destination_volume"] == "vol-destination"
    assert "device busy" in details["cleanup_error"]


def test_docker_failure_details_report_uncleaned_destination(docker_adapter, stub):
    _seed_owned_source(stub)
    stub.containers.wait_result = {"StatusCode": 4}

    def boom(force):
        raise RuntimeError("cannot remove volume")

    stub.volumes.on_remove["vol-destination"] = boom
    with pytest.raises(RuntimeUnavailable, match="目标卷回滚失败") as caught:
        docker_adapter.copy_volume(_copy_spec())
    details = caught.value.details
    assert details["copy_completed"] is False
    assert details["helper_cleaned"] is True
    assert details["destination_rolled_back"] is False
    assert details["destination_may_contain_partial_data"] is True


def test_docker_rollback_failure_is_surfaced(docker_adapter, stub):
    _seed_owned_source(stub)
    stub.containers.wait_result = {"StatusCode": 5}

    def boom(force):
        raise RuntimeError("cannot remove volume")

    stub.volumes.on_remove["vol-destination"] = boom
    with pytest.raises(RuntimeUnavailable, match="目标卷回滚失败"):
        docker_adapter.copy_volume(_copy_spec())


def test_docker_rollback_refuses_foreign_destination_labels(docker_adapter, stub):
    """目标卷标签不匹配时，回滚必须拒绝自动删除（只清自己创建的卷）。"""

    stub.volumes.put("vol-destination", {"app": "someone-else"})
    error = docker_adapter._rollback_volume("vol-destination", dict(LABELS))
    assert error is not None and "归属标签不匹配" in error
    assert stub.volumes.store["vol-destination"].removed_with == []


def test_docker_rollback_missing_destination_is_not_an_error(docker_adapter):
    assert docker_adapter._rollback_volume("vol-ghost", dict(LABELS)) is None


def _ok_volumes(stub: _StubClient) -> set[str]:
    return {name for name, volume in stub.volumes.store.items() if volume.removed_with == []}


def test_copy_script_never_chmods_a_directory_after_handing_it_to_app(tmp_path, monkeypatch):
    import os
    import sys
    from control_plane.runtime.docker_adapter import _COPY_SCRIPT

    source, target = tmp_path / 'source', tmp_path / 'target'
    source.mkdir(); target.mkdir()
    (source / 'notes.md').write_text('Original content')
    handed_off = set()
    chmod = os.chmod
    def restricted_chmod(path, mode):
        assert str(path) not in handed_off, 'CAP_CHOWN does not permit chmod after chown'
        chmod(path, mode)
    monkeypatch.setattr(os, 'chmod', restricted_chmod)
    monkeypatch.setattr(os, 'lchown', lambda path, uid, gid: handed_off.add(str(path)), raising=False)
    monkeypatch.setattr(sys, 'argv', ['helper', '', 'users/owner/workspaces/copy', '10001', '10001', '30', '[]'])
    script = _COPY_SCRIPT.replace('SRC_ROOT = "/copy/source"', f'SRC_ROOT = {str(source)!r}').replace(
        'DST_ROOT = "/copy/target"', f'DST_ROOT = {str(target)!r}')
    with pytest.raises(SystemExit) as done:
        exec(compile(script, '<volume-copy-helper>', 'exec'), {})
    assert done.value.code == 0
    assert (target / 'users/owner/workspaces/copy/notes.md').read_text() == 'Original content'


def _failing_remove_factory(stub: _StubClient, remover):
    original = stub.containers.create

    def create(**kwargs):
        helper = original(**kwargs)
        helper.remove = remover  # type: ignore[assignment]
        return helper

    return create
