"""内存版运行时适配器，用于全部单元测试与本地演示。

它精确模拟真实适配器的**可观测行为**（而不是假装 Docker 存在）：

* 容器需要若干次 ``probe`` 才会就绪，模拟冷启动；
* 可以注入启动失败、停止失败、探针拒绝，用来测试隔离与回滚路径；
* 卷只有在 ``remove_volume`` 之后才消失，因此能验证"reset 才释放数据"。

线程安全：测试里会并发分配，所以内部用锁保护状态字典。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from control_plane.domain.models import WorkloadState
from control_plane.errors import RuntimeUnavailable
from control_plane.ports import (
    InstanceSpec,
    ReadinessResult,
    RuntimeStatus,
    VolumeCopySpec,
    VolumeSpec,
)


@dataclass
class _FakeContainer:
    name: str
    spec: InstanceSpec
    running: bool = False
    probes: int = 0
    labels: dict[str, str] = field(default_factory=dict)


class FakeRuntimeAdapter:
    """可控的内存适配器。"""

    name = "fake"

    def __init__(self, *, ready_after_probes: int = 1, upstream_template: str = "http://{name}:{port}") -> None:
        self.ready_after_probes = max(1, int(ready_after_probes))
        self.upstream_template = upstream_template
        self._lock = threading.RLock()
        self._containers: dict[str, _FakeContainer] = {}
        self._volumes: dict[str, VolumeSpec] = {}
        self._volume_files: dict[str, dict[str, bytes]] = {}
        # --- 故障注入钩子（测试用） -------------------------------------
        self.fail_create: set[str] = set()
        self.fail_start: set[str] = set()
        self.fail_stop: set[str] = set()
        self.fail_remove_volume: set[str] = set()
        #: 卷复制故障注入：命中源卷或目标卷名即让 ``copy_volume`` 失败。
        self.fail_copy: set[str] = set()
        self.never_ready: set[str] = set()
        # --- 调用记录（断言用） -----------------------------------------
        self.calls: list[tuple[str, str]] = []

    # ------------------------------------------------------------ 内部
    def _record(self, op: str, target: str) -> None:
        self.calls.append((op, target))

    def _upstream(self, name: str, port: int) -> str:
        return self.upstream_template.format(name=name, port=port)

    # ------------------------------------------------------------ volumes
    def ensure_volume(self, spec: VolumeSpec) -> bool:
        with self._lock:
            self._record("ensure_volume", spec.name)
            if spec.name in self._volumes:
                if self._volumes[spec.name].labels != spec.labels:
                    raise RuntimeUnavailable('Volume labels do not match')
                return False
            if spec.must_exist:
                raise RuntimeUnavailable('Persistent workspace volume is missing')
            self._volumes[spec.name] = spec
            self._volume_files[spec.name] = {}
            return True

    def remove_volume(self, name: str) -> bool:
        with self._lock:
            self._record("remove_volume", name)
            if name in self.fail_remove_volume:
                raise RuntimeUnavailable(f"注入的卷删除失败：{name}")
            if any(c.spec.volume_name == name for c in self._containers.values()):
                raise RuntimeUnavailable('Volume is still mounted')
            self._volume_files.pop(name, None)
            return self._volumes.pop(name, None) is not None

    def volume_exists(self, name: str) -> bool:
        with self._lock:
            return name in self._volumes

    def copy_volume(self, spec: VolumeCopySpec) -> None:
        """在内存里把源卷的目录子树复制成独立的新卷内容。

        语义与真实适配器保持**可观测一致**：

        * 源卷必须存在且标签完全匹配，否则报错（不拿 source_uuid 当令牌）；
        * 目标卷**必须不存在**，绝不覆盖既有项目数据；
        * 内容按 ``source_directory`` -> ``destination_directory`` 做目录映射，
          逐条复制成**独立**的 bytes 字典，改源卷不影响副本；
        * ``exclude_paths``（相对源子树的路径）被跳过；
        * ``exclude_names`` match reserved basenames at every subtree depth;
        * ``fail_copy`` 注入可让这次复制失败，便于后端测试回滚路径。
        """

        with self._lock:
            self._record('copy_volume', spec.destination.name)
            source = self._volumes.get(spec.source.name)
            if source is None:
                raise RuntimeUnavailable(f'源卷 {spec.source.name} 不存在，拒绝复制')
            if not spec.source.labels:
                raise RuntimeUnavailable('源卷必须带归属标签')
            if source.labels != spec.source.labels:
                raise RuntimeUnavailable('源卷归属标签不匹配')
            if spec.destination.name in self._volumes:
                raise RuntimeUnavailable(
                    f'拒绝覆盖已存在的目标卷 {spec.destination.name}；卷复制只允许写全新卷'
                )
            if spec.destination.name in self.fail_copy or spec.source.name in self.fail_copy:
                raise RuntimeUnavailable(f'注入的卷复制失败：{spec.destination.name}')
            if not spec.destination.labels:
                raise RuntimeUnavailable('目标卷必须带归属标签')
            if not self.ensure_volume(spec.destination):
                raise RuntimeUnavailable(f'目标卷 {spec.destination.name} 已存在，拒绝继续复制')

            source_files = self._volume_files.setdefault(spec.source.name, {})
            destination_files: dict[str, bytes] = {}
            self._volume_files[spec.destination.name] = destination_files
            source_prefix = '' if spec.source_directory == '.' else spec.source_directory + '/'
            destination_prefix = (
                '' if spec.destination_directory == '.' else spec.destination_directory + '/'
            )
            excludes = tuple(spec.exclude_paths)
            for key, data in source_files.items():
                if source_prefix and not key.startswith(source_prefix):
                    continue
                relative = key[len(source_prefix):] if source_prefix else key
                if any(part in spec.exclude_names for part in relative.split('/')):
                    continue
                if any(relative == item or relative.startswith(item + '/') for item in excludes):
                    continue
                destination_files[destination_prefix + relative] = bytes(data)

    # ------------------------------------------------------------ 生命周期
    def create(self, spec: InstanceSpec) -> RuntimeStatus:
        with self._lock:
            self._record("create", spec.container_name)
            if spec.container_name in self.fail_create:
                raise RuntimeUnavailable(f"注入的创建失败：{spec.container_name}")
            # 与真实 docker 适配器保持一致的**可观测行为**：创建容器时
            # 会确保专属数据卷存在（真实实现由 Docker 自动创建具名卷）。
            # 否则测试就会漏掉"回收是否真的删了卷"这条不变量。
            self.ensure_volume(VolumeSpec(name=spec.volume_name, labels=spec.labels, must_exist=spec.volume_must_exist))
            existing = self._containers.get(spec.container_name)
            if existing is not None:
                return self._status_of(existing)
            container = _FakeContainer(
                name=spec.container_name, spec=spec, labels=dict(spec.labels)
            )
            self._containers[spec.container_name] = container
            return self._status_of(container)

    def start(self, container_name: str) -> RuntimeStatus:
        with self._lock:
            self._record("start", container_name)
            if container_name in self.fail_start:
                raise RuntimeUnavailable(f"注入的启动失败：{container_name}")
            container = self._containers.get(container_name)
            if container is None:
                raise RuntimeUnavailable(f"容器不存在：{container_name}")
            container.running = True
            return self._status_of(container)

    def stop(self, container_name: str, *, timeout: int) -> RuntimeStatus:
        with self._lock:
            self._record("stop", container_name)
            if container_name in self.fail_stop:
                raise RuntimeUnavailable(f"注入的停止失败：{container_name}")
            container = self._containers.get(container_name)
            if container is None:
                return RuntimeStatus(exists=False, state=WorkloadState.ABSENT)
            container.running = False
            return self._status_of(container)

    def remove(self, container_name: str, *, remove_volumes: bool = False) -> bool:
        with self._lock:
            self._record("remove", container_name)
            container = self._containers.pop(container_name, None)
            if remove_volumes and container is not None:
                self._volumes.pop(container.spec.volume_name, None)
            return container is not None

    def status(self, container_name: str) -> RuntimeStatus:
        with self._lock:
            container = self._containers.get(container_name)
            if container is None:
                return RuntimeStatus(exists=False, state=WorkloadState.ABSENT)
            return self._status_of(container)

    def _status_of(self, container: _FakeContainer) -> RuntimeStatus:
        if container.running:
            state = (
                WorkloadState.READY
                if container.probes >= self.ready_after_probes
                else WorkloadState.STARTING
            )
        else:
            state = WorkloadState.STOPPED
        return RuntimeStatus(
            exists=True,
            state=state,
            container_name=container.name,
            volume_exists=container.spec.volume_name in self._volumes,
            upstream=self._upstream(container.name, container.spec.internal_port),
            running=container.running,
        )

    # ------------------------------------------------------------ 就绪
    def probe(self, upstream: str) -> ReadinessResult:
        with self._lock:
            self._record("probe", upstream)
            for container in self._containers.values():
                candidate = self._upstream(container.name, container.spec.internal_port)
                if candidate != upstream:
                    continue
                if not container.running:
                    return ReadinessResult(ready=False, detail="容器未运行")
                if container.name in self.never_ready:
                    return ReadinessResult(ready=False, detail="注入：永不就绪")
                container.probes += 1
                if container.probes >= self.ready_after_probes:
                    payload = {'status': 'ok', 'mode': 'demo'}
                    if container.spec.env.get('TUTORLM_WORKSPACE_ID'):
                        payload.update({
                            'workspace_id': container.spec.env['TUTORLM_WORKSPACE_ID'],
                            'storage_mode': 'persistent',
                        })
                    return ReadinessResult(
                        ready=True,
                        payload=payload,
                    )
                return ReadinessResult(ready=False, detail="冷启动中")
            return ReadinessResult(ready=False, detail="未找到对应容器")

    def inspect_labels(self, container_name: str) -> dict[str, str]:
        with self._lock:
            container = self._containers.get(container_name)
            if container is None:
                raise RuntimeUnavailable(f"容器不存在：{container_name}")
            return dict(container.labels)

    def list_managed(self) -> list[RuntimeStatus]:
        with self._lock:
            return [self._status_of(c) for c in self._containers.values()]

    def close(self) -> None:  # pragma: no cover - fake 无需释放
        return None

    # ------------------------------------------------------------ 测试助手
    def write_volume(self, name: str, key: str, data: bytes) -> None:
        with self._lock:
            self._volume_files[name][key] = data

    def read_volume(self, name: str, key: str) -> bytes:
        with self._lock:
            return self._volume_files[name][key]

    def snapshot(self) -> dict[str, Any]:
        """便于断言内部实况。"""

        with self._lock:
            return {
                "containers": {
                    name: {
                        "running": c.running,
                        "probes": c.probes,
                        "volume": c.spec.volume_name,
                        "labels": dict(c.labels),
                    }
                    for name, c in self._containers.items()
                },
                "volumes": sorted(self._volumes),
            }
