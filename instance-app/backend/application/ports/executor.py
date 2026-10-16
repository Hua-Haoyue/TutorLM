"""执行器端口：workload class 各自独立额度，**编译不占模型队列**（设计 §5）。

本层只定义接口：host 侧用 bounded ThreadExecutor 实现，队列满时快速失败
（不无限排队、不拖垮容器）。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from backend.domain.enums import RunWorkload


@runtime_checkable
class Executor(Protocol):
    """提交一个已持久化的 run 去执行。"""

    @property
    def workload(self) -> RunWorkload:
        """本执行器服务的 workload class。"""

        ...

    @property
    def capacity(self) -> int:
        """并发上限（有界队列长度由实现决定并暴露给配额逻辑）。"""

        ...

    def submit(self, run_id: str) -> bool:
        """入队；队列满返回 ``False``（调用方据此快速失败，不阻塞）。"""

        ...

    def cancel(self, run_id: str) -> bool:
        """请求取消；已结束或未入队返回 ``False``。"""

        ...

    def shutdown(self, *, wait: bool = True) -> None:
        ...


@runtime_checkable
class ExecutorRegistry(Protocol):
    """按 workload class 取执行器；缺失即为不可用（如无安全编译器）。"""

    def get(self, workload: RunWorkload) -> Executor | None:
        ...

    def available_workloads(self) -> frozenset[RunWorkload]:
        ...
