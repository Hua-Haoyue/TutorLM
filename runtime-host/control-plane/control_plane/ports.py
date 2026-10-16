"""控制面的端口（Port）定义：运行时适配器契约。

``RuntimeAdapter`` 是控制面唯一接触"容器"的地方。fake 实现让全部调度/
租约/回收逻辑可以在没有 Docker 的机器和 CI 上跑完，docker 实现只负责把
这些调用翻译成 Docker API。这样"调度正确性"与"能否连上 Docker"解耦。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from control_plane.domain.models import WorkloadState
from control_plane.errors import RuntimeUnavailable


@dataclass(frozen=True, slots=True)
class VolumeSpec:
    """A durable workspace volume or an anonymous lease's temporary volume."""

    name: str
    labels: dict[str, str] = field(default_factory=dict)
    must_exist: bool = False


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
    volume_must_exist: bool = False


def _normalize_relative_path(value: str, *, field: str) -> str:
    """校验并规范化一个**严格相对**的卷内路径。

    ``'.'`` 表示卷根；绝对路径、Windows 盘符与 ``..`` 一律拒绝，否则复制
    就会越出被挂载的那棵子树，把整个卷当成隐含绑定。
    """

    if value is None:
        raise RuntimeUnavailable(f'{field} 不能为空（复制整个卷请使用 "."）')
    text = str(value).strip()
    if not text:
        raise RuntimeUnavailable(f'{field} 不能为空（复制整个卷请使用 "."）')
    if text.startswith('/') or text.startswith('\\') or ':' in text:
        raise RuntimeUnavailable(f'{field} 必须是严格相对路径，收到 {value!r}')
    if '\x00' in text:
        raise RuntimeUnavailable(f'{field} 含非法字符')
    parts = [part for part in text.replace('\\', '/').split('/') if part not in ('', '.')]
    if not parts:
        return '.'
    if any(part == '..' for part in parts):
        raise RuntimeUnavailable(f'{field} 不得包含 ".."，收到 {value!r}')
    return '/'.join(parts)


@dataclass(frozen=True, slots=True)
class VolumeCopySpec:
    """Copy one directory subtree from an existing volume into a brand-new volume.

    ``source`` must already exist and carry exactly the ownership labels the
    caller supplies; ``destination`` must **not** exist yet, because this
    operation never overwrites or merges into existing project data. Both
    directories are strictly relative paths inside their volumes (``'.'`` =
    the whole volume), so mounting a volume here does not implicitly bind the
    entire volume as the project root.

    ``exclude_paths`` are paths relative to ``source_directory`` that must not
    be copied (for example stored session or credential files that must never
    be cloned into the recipient's project).
    ``exclude_names`` match exact file or directory basenames at any depth
    inside that subtree; unlike ``exclude_paths``, they are not root-relative.
    """

    source: VolumeSpec
    destination: VolumeSpec
    source_directory: str = '.'
    destination_directory: str = '.'
    helper_image: str = ''
    timeout_seconds: int = 120
    exclude_paths: tuple[str, ...] = ()
    exclude_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, 'source_directory',
            _normalize_relative_path(self.source_directory, field='source_directory'),
        )
        object.__setattr__(
            self, 'destination_directory',
            _normalize_relative_path(self.destination_directory, field='destination_directory'),
        )
        object.__setattr__(
            self, 'exclude_paths',
            tuple(
                _normalize_relative_path(item, field='exclude_paths')
                for item in (self.exclude_paths or ())
            ),
        )
        names = tuple(self.exclude_names)
        for name in names:
            if (
                not isinstance(name, str) or not name.strip()
                or name in {'.', '..'}
                or any(char in name for char in ('/', '\\', ':', '\x00'))
            ):
                raise RuntimeUnavailable('exclude_names must contain nonempty single-segment names')
        object.__setattr__(self, 'exclude_names', names)
        if int(self.timeout_seconds) <= 0:
            raise RuntimeUnavailable('timeout_seconds 必须为正数')
        if not self.source.labels:
            raise RuntimeUnavailable('源卷必须带归属标签')
        if not self.destination.labels:
            raise RuntimeUnavailable('目标卷必须带归属标签')
        if self.source.name == self.destination.name:
            raise RuntimeUnavailable('源卷与目标卷不能是同一个卷')


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


    def copy_volume(self, spec: VolumeCopySpec) -> None:
        """把一个卷中的目录子树复制到一个**全新**卷；失败抛 RuntimeUnavailable。

        目标卷必须不存在（绝不覆盖既有项目数据）。复制在受限的临时 helper
        容器里完成，结束后一定被停止并删除；失败的回滚与清理结果都必须暴露
        出来，不得静默吞掉。
        """

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
