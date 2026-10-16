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
        # --- 故障注入钩子（测试用） -------------------------------------
        self.fail_create: set[str] = set()
        self.fail_start: set[str] = set()
        self.fail_stop: set[str] = set()
        self.fail_remove_volume: set[str] = set()
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
                return False
            self._volumes[spec.name] = spec
            return True

    def remove_volume(self, name: str) -> bool:
        with self._lock:
            self._record("remove_volume", name)
            if name in self.fail_remove_volume:
                raise RuntimeUnavailable(f"注入的卷删除失败：{name}")
            return self._volumes.pop(name, None) is not None

    def volume_exists(self, name: str) -> bool:
        with self._lock:
            return name in self._volumes

    # ------------------------------------------------------------ 生命周期
    def create(self, spec: InstanceSpec) -> RuntimeStatus:
        with self._lock:
            self._record("create", spec.container_name)
            if spec.container_name in self.fail_create:
                raise RuntimeUnavailable(f"注入的创建失败：{spec.container_name}")
            # 与真实 docker 适配器保持一致的**可观测行为**：创建容器时
            # 会确保专属数据卷存在（真实实现由 Docker 自动创建具名卷）。
            # 否则测试就会漏掉"回收是否真的删了卷"这条不变量。
            self.ensure_volume(VolumeSpec(name=spec.volume_name, labels=spec.labels))
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
                    return ReadinessResult(
                        ready=True,
                        payload={"status": "ok", "mode": "demo"},
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
