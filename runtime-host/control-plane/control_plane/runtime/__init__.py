"""运行时适配器：fake 用于测试，docker 用于真实部署。"""

from control_plane.runtime.fake import FakeRuntimeAdapter

__all__ = ["FakeRuntimeAdapter", "build_adapter"]


def build_adapter(config, *, store=None):
    """按配置构建适配器。``docker`` 延迟导入，缺依赖时给出明确提示。"""

    from control_plane.config import ADAPTER_DOCKER, ADAPTER_FAKE

    if config.adapter == ADAPTER_FAKE:
        return FakeRuntimeAdapter()
    if config.adapter == ADAPTER_DOCKER:
        try:
            from control_plane.runtime.docker_adapter import DockerRuntimeAdapter
        except ImportError as exc:  # pragma: no cover - 依赖缺失时的提示
            raise RuntimeError(
                "缺少 docker SDK；请安装 requirements.txt 或改用 "
                "CONTROL_PLANE_ADAPTER=fake"
            ) from exc
        return DockerRuntimeAdapter(config)
    raise RuntimeError(f"未知的适配器：{config.adapter}")
