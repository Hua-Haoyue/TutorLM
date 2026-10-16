"""控制面的端口（Port）定义：运行时适配器契约。

``RuntimeAdapter`` 是控制面唯一接触"容器"的地方。fake 实现让全部调度/
租约/回收逻辑可以在没有 Docker 的机器和 CI 上跑完，docker 实现只负责把
这些调用翻译成 Docker API。这样"调度正确性"与"能否连上 Docker"解耦。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from control_plane.domain.models import WorkloadState


@dataclass(frozen=True, slots=True)
class VolumeSpec:
    """槽位专属数据卷。"""

    name: str
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class InstanceSpec:
    """创建一个实例容器所需的全部信息。"""

    slot_id: int
    generation: int
    container_name: str
    image: str
    volume_name: str
    mount_path: str
    internal_port: int
    env: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    network: str | None = None
    mem_limit: str | None = None
    cpus: float | None = None
    pids_limit: int | None = None
    tmpfs_size: str | None = None
    stop_grace_seconds: int = 30


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    """适配器对某个槽位当前实况的观测。"""

    exists: bool
    state: WorkloadState
    container_name: str | None = None
    volume_exists: bool = False
    #: 容器内网可达地址；不存在时为 ``None``。
    upstream: str | None = None
    running: bool = False
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ReadinessResult:
    """一次就绪探测的结果。"""

    ready: bool
    detail: str | None = None
    payload: dict[str, Any] | None = None


@runtime_checkable
class RuntimeAdapter(Protocol):
    """容器运行时适配器。

    实现必须满足：

    * 所有方法都要幂等——重复创建/停止/删除同一槽位不得报错；
    * 只操作带本方标签的资源，绝不枚举或删除服务器上其他项目的容器；
    * 失败时抛 :class:`control_plane.errors.RuntimeUnavailable`，
      而不是静默返回成功。
    """

    name: str

    def ensure_volume(self, spec: VolumeSpec) -> bool:
        """确保卷存在；返回 ``True`` 表示本次新建。"""

    def remove_volume(self, name: str) -> bool:
        """删除卷；返回 ``True`` 表示本次确实删除了。幂等。"""

    def create(self, spec: InstanceSpec) -> RuntimeStatus:
        """创建容器（不启动）。"""

    def start(self, container_name: str) -> RuntimeStatus:
        """启动容器。"""

    def stop(self, container_name: str, *, timeout: int) -> RuntimeStatus:
        """停止容器（SIGTERM -> 超时后 SIGKILL）。"""

    def remove(self, container_name: str, *, remove_volumes: bool = False) -> bool:
        """删除容器。"""

    def status(self, container_name: str) -> RuntimeStatus:
        """查询单个容器的实况。"""

    def probe(self, upstream: str) -> ReadinessResult:
        """对 ``GET /health/ready`` 做一次探测。"""

    def inspect_labels(self, container_name: str) -> dict[str, str]:
        """读取容器标签，用于 reconcile 时确认归属。"""

    def list_managed(self) -> list[RuntimeStatus]:
        """列出带本方标签的容器（不包含其他项目的资源）。"""

    def close(self) -> None:
        """释放适配器持有的连接。"""
