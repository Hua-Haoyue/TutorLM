"""领域枚举与状态机常量（无 IO）。

取值冻结自 ``docs/instance-app-design.md`` §4.3/§5 与 ``docs/instance-app-api.md``。
``StorageLayout`` 的字面量必须与控制面 ``control_plane/domain/models.py`` 的
``STORAGE_LAYOUT_*`` **逐字符一致**；两侧各自定义，不共享模块。
"""

from __future__ import annotations

from enum import StrEnum


class DocumentFormat(StrEnum):
    """``documents.format``。由服务端探测得出，不盲信客户端声明。"""

    MD = "md"
    TXT = "txt"
    PDF = "pdf"
    TEX = "tex"


#: 只有这些格式有"纯文本全文"语义（``document/content`` 只对它们返回 ``text``）。
TEXT_FORMATS: frozenset["DocumentFormat"] = frozenset({DocumentFormat.MD, DocumentFormat.TXT})


class DocumentStatus(StrEnum):
    """``documents.status``（草案枚举，待实现冻结）。"""

    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"


class RevisionStatus(StrEnum):
    """``document_revisions.status``。"""

    READY = "ready"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"


class ExtractionStatus(StrEnum):
    """``document_extractions.status``。"""

    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"


class ArtifactKind(StrEnum):
    """``document_artifacts.kind``。"""

    EXTRACTED_TEXT = "extracted_text"
    COMPILED_PDF = "compiled_pdf"
    BUILD_LOG = "build_log"


class BuildStatus(StrEnum):
    """``document_builds.status``（与 run 状态机同形）。"""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class RunState(StrEnum):
    """``runs.state``：``queued -> running -> completed|failed|cancelled|interrupted``。"""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class RunWorkload(StrEnum):
    """workload class：各自独立 executor 与额度，编译不占模型队列。"""

    MODEL = "model"
    EXTRACT = "extract"
    TEX_COMPILE = "tex_compile"


class RunKind(StrEnum):
    """当前静态插件只编排这两种 kind。"""

    EXPLANATION = "explanation"
    NARRATION = "narration"


class StorageLayout(StrEnum):
    """就绪声明里的 ``storage_layout``。

    - ``CONTENT_ONLY``：已证明数据根内只有项目业务内容，session/凭证在卷外。
    - ``LEGACY_ROOT``：无法证明隔离（旧项目、或证据不足）——不对外声明，
      控制面按 legacy 记录且拒绝作为分享源。
    """

    CONTENT_ONLY = "content_only"
    LEGACY_ROOT = "legacy_root"


class NodeKind(StrEnum):
    SECTION = "section"
    CONCEPT = "concept"
    EXAMPLE = "example"
    EXERCISE = "exercise"
    SUMMARY = "summary"


class EdgeRelation(StrEnum):
    PARENT_OF = "parent_of"
    EXPLAINS = "explains"
    SUPPORTS = "supports"
    DEPENDS_ON = "depends_on"
    PREREQUISITE_OF = "prerequisite_of"
    CONTRADICTS = "contradicts"
    EXAMPLE_OF = "example_of"
    SUMMARIZES = "summarizes"
    DERIVED_FROM = "derived_from"
    REFERENCES = "references"
    RELATED_TO = "related_to"


class LearnStatus(StrEnum):
    """``nodes.metadata.status``。"""

    LEARNING = "learning"
    MASTERED = "mastered"


#: 终态：run/build 到达后不再迁移。
RUN_TERMINAL_STATES: frozenset[RunState] = frozenset(
    {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED, RunState.INTERRUPTED}
)

#: 合法迁移 ``from -> {to, ...}``；未列出的一律拒绝。
RUN_ALLOWED_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.QUEUED: frozenset(
        {RunState.RUNNING, RunState.CANCELLED, RunState.FAILED, RunState.INTERRUPTED}
    ),
    RunState.RUNNING: frozenset(
        {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED, RunState.INTERRUPTED}
    ),
    RunState.COMPLETED: frozenset(),
    RunState.FAILED: frozenset(),
    RunState.CANCELLED: frozenset(),
    RunState.INTERRUPTED: frozenset(),
}


def is_run_terminal(state: RunState) -> bool:
    return state in RUN_TERMINAL_STATES


def can_transition_run(current: RunState, target: RunState) -> bool:
    """同状态视为幂等返回 ``True``；其余按表判定。"""

    if current == target:
        return True
    return target in RUN_ALLOWED_TRANSITIONS.get(current, frozenset())
