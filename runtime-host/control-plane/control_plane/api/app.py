"""控制面 FastAPI 应用组装。

启动顺序刻意做成"先对账、再开 GC"：进程曾在回收中途被杀掉时，必须先
把槽位实况看清楚，再让自动收割介入，否则会基于脏状态做决定。
"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from control_plane.allocator import Allocator
from control_plane.api.errors import register_error_handlers
from control_plane.api.routers import admin, health, leases
from control_plane.config import ControlPlaneConfig
from control_plane.lifecycle import SlotLifecycle
from control_plane.runtime import build_adapter
from control_plane.store import ControlPlaneStore

logger = logging.getLogger(__name__)


def build_components(
    config: ControlPlaneConfig,
    *,
    adapter: Any | None = None,
    activity_probe: Any | None = None,
    clock: Any | None = None,
) -> dict[str, Any]:
    """按配置构建 store/adapter/lifecycle/allocator。

    抽成函数是为了让测试可以注入 fake adapter 与假时钟，而不必启动 HTTP。
    """

    store = (
        ControlPlaneStore(config.resolved_db_path(), clock=clock)
        if clock is not None
        else ControlPlaneStore(config.resolved_db_path())
    )
    store.ensure_slots(config.slots)
    runtime = adapter if adapter is not None else build_adapter(config)
    lifecycle = SlotLifecycle(config, store, runtime)
    allocator = Allocator(
        config,
        store,
        lifecycle,
        activity_probe=activity_probe,
        **({"clock": clock} if clock is not None else {}),
    )
    return {
        "config": config,
        "store": store,
        "adapter": runtime,
        "lifecycle": lifecycle,
        "allocator": allocator,
    }


def create_app(
    config: ControlPlaneConfig | None = None,
    *,
    adapter: Any | None = None,
    activity_probe: Any | None = None,
    run_startup_tasks: bool = True,
) -> FastAPI:
    resolved = config or ControlPlaneConfig.from_env()
    components = build_components(
        resolved, adapter=adapter, activity_probe=activity_probe
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stop_event = threading.Event()
        gc_thread: threading.Thread | None = None

        if run_startup_tasks:
            # 先对账：把"上次没走完的回收"暴露出来，而不是假装干净。
            try:
                report = components["lifecycle"].reconcile()
                if report.get("quarantined"):
                    logger.warning("对账发现并隔离了槽位：%s", report["quarantined"])
            except Exception:  # noqa: BLE001 - 对账失败不应阻止启动
                logger.exception("启动对账失败")

            if resolved.gc_enabled:
                gc_thread = threading.Thread(
                    target=_gc_loop,
                    args=(components["allocator"], stop_event, resolved.gc_interval_seconds),
                    name="control-plane-gc",
                    daemon=True,
                )
                gc_thread.start()

        try:
            yield
        finally:
            stop_event.set()
            if gc_thread is not None:
                gc_thread.join(timeout=5)
            try:
                components["adapter"].close()
            except Exception:  # noqa: BLE001
                logger.debug("关闭适配器失败", exc_info=True)
            components["store"].close()

    app = FastAPI(
        title="TutorLM Control Plane",
        version="0.1.0",
        description="容器外调度控制面：匿名试用租约、固定槽位分配、实例生命周期与回收",
        lifespan=lifespan,
    )
    for key, value in components.items():
        setattr(app.state, key, value)

    register_error_handlers(app)
    app.include_router(health.router)
    app.include_router(leases.router)
    app.include_router(leases.admission_router)
    app.include_router(admin.router)
    return app


def _gc_loop(allocator: Allocator, stop_event: threading.Event, interval: int) -> None:
    """后台收割循环。异常不终止线程，否则后续回收会静默失效。"""

    while not stop_event.wait(max(1, int(interval))):
        try:
            report = allocator.gc_once()
            if any(report.get(key) for key in ("expired", "reclaimed", "failed")):
                logger.info("GC 结果：%s", report)
        except Exception:  # noqa: BLE001
            logger.exception("GC 轮次失败")
