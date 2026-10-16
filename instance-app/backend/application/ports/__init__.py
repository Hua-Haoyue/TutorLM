"""抽象端口：repository / provider / clock / id / executor / storage / config。

这些是**协议**（``typing.Protocol``），实现只在 ``infrastructure`` 里出现。
用例（services）依赖端口，因此可以在没有 sqlite/网络/文件系统的测试里替换实现。
"""

from __future__ import annotations

from backend.application.ports.artifacts import ArtifactStore
from backend.application.ports.clock import Clock, IdGenerator, SystemClock, UuidGenerator
from backend.application.ports.config import AppSettings
from backend.application.ports.content import ContentStore, StoredObject
from backend.application.ports.executor import Executor, ExecutorRegistry
from backend.application.ports.provider import (
    GenerateRequest,
    GenerateResult,
    ModelProvider,
    ProviderInfo,
)
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
from backend.application.ports.unit_of_work import Repositories, UnitOfWork, UnitOfWorkFactory

__all__ = [
    "AppSettings",
    "ArtifactStore",
    "Clock",
    "ContentStore",
    "DocumentRepository",
    "EdgeRepository",
    "Executor",
    "ExecutorRegistry",
    "ExplanationRepository",
    "ExtractionRepository",
    "GenerateRequest",
    "GenerateResult",
    "IdGenerator",
    "ModelProvider",
    "NarrationRepository",
    "NodeRepository",
    "ProjectRepository",
    "ProviderInfo",
    "Repositories",
    "ResourceRepository",
    "RevisionRepository",
    "RunEventRepository",
    "RunRepository",
    "StoredObject",
    "SystemClock",
    "UnitOfWork",
    "UnitOfWorkFactory",
    "UuidGenerator",
    "WorkspaceBindingRepository",
]
