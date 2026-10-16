"""基于 Docker SDK 的真实运行时适配器。

安全边界（见 docs/architecture.md「数据和权限」）：

* 只操作带 :meth:`RuntimeAdapter.list_managed` 标签的资源，从不枚举或删除
  服务器上其他项目的容器与卷；
* 卷名与容器名一律由配置前缀生成，不接受外部传入的任意名字；
* 每个容器独立卷、独立网络别名，``read_only`` 根文件系统 + ``cap_drop``
  + ``no-new-privileges``，与单实例 compose 保持同一套加固参数。

这个进程因此属于**高权限控制面**，绝不能挂进 instance-app 容器里。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from docker.types import Mount

from control_plane.domain.models import WorkloadState
from control_plane.errors import RuntimeUnavailable
from control_plane.ports import (
    InstanceSpec,
    ReadinessResult,
    RuntimeStatus,
    VolumeSpec,
)

logger = logging.getLogger(__name__)

#: 就绪探针路径，与 contracts/instance-runtime-v1.md 冻结的约定一致。
READY_PATH = "/health/ready"


def _map_state(attrs: dict[str, Any]) -> WorkloadState:
    state = (attrs.get("State") or {})
    status = str(state.get("Status") or "").lower()
    if status == "running":
        health = str((state.get("Health") or {}).get("Status") or "").lower()
        if health in {"unhealthy", "starting"}:
            return WorkloadState.STARTING
        return WorkloadState.READY
    if status in {"created"}:
        return WorkloadState.CREATED
    if status in {"exited", "dead"}:
        return WorkloadState.STOPPED
    if status in {"paused", "restarting", "removing"}:
        return WorkloadState.STOPPING
    return WorkloadState.STOPPED


class DockerRuntimeAdapter:
    """把控制面的容器操作翻译成 Docker API。"""

    name = "docker"

    def __init__(self, config: Any, *, client: Any | None = None) -> None:
        if client is not None:
            self._client = client
        else:
            try:
                import docker  # 延迟导入，方便无 Docker 环境下做静态检查
            except ImportError as exc:  # pragma: no cover
                raise RuntimeUnavailable("未安装 docker SDK") from exc
            try:
                self._client = docker.DockerClient(
                    base_url=config.docker_host, timeout=60
                )
                self._client.ping()
            except Exception as exc:
                raise RuntimeUnavailable(f"无法连接 Docker daemon：{exc}") from exc
        self._config = config
        self._owned_labels = config.labels()

    # ------------------------------------------------------------ 内部
    def _guard_owned(self, container: Any) -> None:
        """确认容器属于本方，拒绝触碰其他项目的资源。"""

        labels = (container.attrs.get("Config") or {}).get("Labels") or {}
        for key, value in self._owned_labels.items():
            if labels.get(key) != value:
                raise RuntimeUnavailable(
                    f"拒绝操作非本方容器 {container.name}（缺少归属标签）"
                )

    # ------------------------------------------------------------ volumes
    def ensure_volume(self, spec: VolumeSpec) -> bool:
        try:
            self._client.volumes.get(spec.name)
            return False
        except Exception:
            pass
        try:
            self._client.volumes.create(name=spec.name, labels=spec.labels)
            return True
        except Exception as exc:
            raise RuntimeUnavailable(f"创建数据卷失败 {spec.name}：{exc}") from exc

    def remove_volume(self, name: str) -> bool:
        try:
            volume = self._client.volumes.get(name)
        except Exception:
            return False
        labels = (volume.attrs or {}).get("Labels") or {}
        for key, value in self._owned_labels.items():
            if labels.get(key) != value:
                raise RuntimeUnavailable(f"拒绝删除非本方数据卷 {name}")
        try:
            volume.remove(force=True)
            return True
        except Exception as exc:
            raise RuntimeUnavailable(f"删除数据卷失败 {name}：{exc}") from exc

    # ------------------------------------------------------------ 生命周期
    def create(self, spec: InstanceSpec) -> RuntimeStatus:
        self.ensure_volume(VolumeSpec(name=spec.volume_name, labels=spec.labels))
        try:
            existing = self._client.containers.get(spec.container_name)
            self._guard_owned(existing)
            return self.status(spec.container_name)
        except RuntimeUnavailable:
            raise
        except Exception:
            pass

        docker_spec: dict[str, Any] = {
            "image": spec.image,
            "name": spec.container_name,
            "detach": True,
            "environment": dict(spec.env),
            "labels": dict(spec.labels),
            # 用显式 Mount 而不是 volumes 字典：Mount 的 source/target 语义
            # 无歧义，且保证挂的是**具名卷**（不会退化成匿名卷留下垃圾数据）。
            "mounts": [
                Mount(
                    target=spec.mount_path,
                    source=spec.volume_name,
                    type="volume",
                    read_only=False,
                )
            ],
            "read_only": True,
            "tmpfs": {"/tmp": f"size={spec.tmpfs_size or '128m'},mode=1777"},
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "init": True,
        }
        if spec.mem_limit:
            docker_spec["mem_limit"] = spec.mem_limit
        if spec.cpus:
            docker_spec["nano_cpus"] = int(float(spec.cpus) * 1_000_000_000)
        if spec.pids_limit:
            docker_spec["pids_limit"] = int(spec.pids_limit)
        if spec.network:
            docker_spec["network"] = spec.network
        try:
            container = self._client.containers.create(**docker_spec)
            if spec.network and hasattr(self._client, "networks"):
                # Keep the gateway target stable while generation-specific
                # container names change on every reset.
                network = self._client.networks.get(spec.network)
                # ``containers.create(network=...)`` attaches the container
                # without aliases. Reconnect before first start so the stable
                # slot alias is actually registered in Docker DNS.
                network.disconnect(container, force=True)
                network.connect(
                    container,
                    aliases=[f"{self._config.container_prefix}-s{spec.slot_id:02d}"],
                )
        except Exception as exc:
            raise RuntimeUnavailable(f"创建容器失败 {spec.container_name}：{exc}") from exc
        return self.status(spec.container_name)

    def start(self, container_name: str) -> RuntimeStatus:
        try:
            container = self._client.containers.get(container_name)
            self._guard_owned(container)
            container.start()
        except RuntimeUnavailable:
            raise
        except Exception as exc:
            raise RuntimeUnavailable(f"启动容器失败 {container_name}：{exc}") from exc
        return self.status(container_name)

    def stop(self, container_name: str, *, timeout: int) -> RuntimeStatus:
        try:
            container = self._client.containers.get(container_name)
        except Exception:
            return RuntimeStatus(exists=False, state=WorkloadState.ABSENT)
        self._guard_owned(container)
        try:
            container.stop(timeout=timeout)
        except Exception as exc:
            raise RuntimeUnavailable(f"停止容器失败 {container_name}：{exc}") from exc
        return self.status(container_name)

    def remove(self, container_name: str, *, remove_volumes: bool = False) -> bool:
        try:
            container = self._client.containers.get(container_name)
        except Exception:
            return False
        self._guard_owned(container)
        try:
            # 具名卷不会被 v=True 删除，回收由 remove_volume 显式负责。
            container.remove(force=True)
            return True
        except Exception as exc:
            raise RuntimeUnavailable(f"删除容器失败 {container_name}：{exc}") from exc

    def status(self, container_name: str) -> RuntimeStatus:
        try:
            container = self._client.containers.get(container_name)
        except Exception:
            return RuntimeStatus(exists=False, state=WorkloadState.ABSENT)
        attrs = container.attrs
        state = _map_state(attrs)
        labels = (attrs.get("Config") or {}).get("Labels") or {}
        mounts = attrs.get("Mounts") or []
        volume_exists = any(m.get("Type") == "volume" for m in mounts)
        return RuntimeStatus(
            exists=True,
            state=state,
            container_name=container_name,
            volume_exists=volume_exists,
            upstream=self._upstream(container_name),
            running=state in {WorkloadState.READY, WorkloadState.STARTING},
        )

    def _upstream(self, container_name: str) -> str:
        """容器内网地址。

        应用容器在同一个 Docker 网络里按容器名解析；端口用内部端口，
        绝不经过宿主机发布端口（避免绕过网关）。
        """

        return f"http://{container_name}:{self._config.instance_port}"

    # ------------------------------------------------------------ 就绪
    def probe(self, upstream: str) -> ReadinessResult:
        import httpx

        url = f"{upstream.rstrip('/')}{READY_PATH}"
        try:
            response = httpx.get(url, timeout=3.0)
        except Exception as exc:
            return ReadinessResult(ready=False, detail=f"探针请求失败：{exc}")
        if response.status_code != 200:
            return ReadinessResult(
                ready=False, detail=f"探针返回 {response.status_code}"
            )
        try:
            payload = response.json()
        except Exception:
            return ReadinessResult(ready=False, detail="探针响应不是 JSON")
        if str(payload.get("status")) != "ok":
            return ReadinessResult(ready=False, detail=f"status={payload.get('status')!r}")
        return ReadinessResult(ready=True, payload=payload)

    def inspect_labels(self, container_name: str) -> dict[str, str]:
        try:
            container = self._client.containers.get(container_name)
        except Exception as exc:
            raise RuntimeUnavailable(f"容器不存在：{container_name}") from exc
        return dict((container.attrs.get("Config") or {}).get("Labels") or {})

    def list_managed(self) -> list[RuntimeStatus]:
        try:
            containers = self._client.containers.list(
                all=True, filters={"label": f"{self._config.label_key}={self._config.label_value}"}
            )
        except Exception as exc:
            raise RuntimeUnavailable(f"枚举容器失败：{exc}") from exc
        return [self.status(c.name) for c in containers]

    def wait_ready(
        self, upstream: str, *, timeout: float, interval: float
    ) -> ReadinessResult:
        """轮询直到就绪或超时。返回最后一次探测结果。"""

        deadline = time.monotonic() + timeout
        last = ReadinessResult(ready=False, detail="未开始探测")
        while True:
            last = self.probe(upstream)
            if last.ready:
                return last
            if time.monotonic() >= deadline:
                return last
            time.sleep(interval)

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # pragma: no cover
            logger.debug("关闭 Docker 客户端失败", exc_info=True)
