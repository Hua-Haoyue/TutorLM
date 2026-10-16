"""UnitOfWork 端口：显式事务边界，repository 不裸开事务（设计 §4.3）。

用法：

```python
with uow_factory() as uow:          # 开始事务
    uow.projects.save(project)
    uow.commit()                    # 显式提交
# 未 commit 即退出 -> 回滚
```

**模型调用在事务外**：不要持有 ``uow`` 跨 provider 调用；读输入 -> 提交/关闭 ->
调 provider -> 重新开事务 -> 校验（绑定未变、run 仍 running）再写入。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from backend.application.ports.repositories import (
    DocumentRepository,
    EdgeRepository,
    ExplanationRepository,
    ExtractionRepository,
    NarrationRepository,
    NodeRepository,
    ProjectRepository,
    ResourceRepository,
    RevisionRepository,
    RunEventRepository,
    RunRepository,
    WorkspaceBindingRepository,
)
from backend.domain.enums import StorageLayout


@dataclass(frozen=True, slots=True)
class Repositories:
    """一个事务内的 repository 句柄集合。"""

    projects: ProjectRepository
    documents: DocumentRepository
    revisions: RevisionRepository
    resources: ResourceRepository
    extractions: ExtractionRepository
    nodes: NodeRepository
    edges: EdgeRepository
    explanations: ExplanationRepository
    narrations: NarrationRepository
    runs: RunRepository
    run_events: RunEventRepository
    binding: WorkspaceBindingRepository


@runtime_checkable
class UnitOfWork(Protocol):
    """单事务作用域。"""

    @property
    def repos(self) -> Repositories:
        ...

    def commit(self) -> None:
        ...

    def rollback(self) -> None:
        ...

    def __enter__(self) -> "UnitOfWork":
        ...

    def __exit__(self, exc_type, exc, tb) -> bool | None:
        ...


@runtime_checkable
class UnitOfWorkFactory(Protocol):
    def __call__(self) -> UnitOfWork:
        ...


#: 布局枚举在 UoW 层也被引用（绑定初始化需要落 storage_layout）。
__all__ = ["Repositories", "UnitOfWork", "UnitOfWorkFactory", "StorageLayout"]
