"""格式适配器端口（Protocol）与值对象。

这一层只是**契约**：md/txt/pdf 解析、抽取、tex 工程校验与受限编译。
实现落在 ``infrastructure/format``。foundation（domain/ports/infrastructure）
未就绪时，这里先给出稳定接口，避免上层依赖具体实现。

关键立场（与设计 §16 / tex 专文一致）：

- pdf 抽取器**不可用**时 **fail closed**（``ExtractUnavailable``），
  **绝不**把 pdf 二进制当 utf-8 文本解码；扫描件无文本层 → ``unsupported``。
- tex 编译**只经受信 executor 端口**完成；没有该端口就是
  ``TexCompileUnavailable``，**不做**同 App subprocess fallback。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


# --------------------------------------------------------------------------
# 抽取（md/txt/pdf）
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class PageText:
    """pdf 抽取的单页文本（page 从 1 开始）+ 该页的规范化文本。"""

    page: int
    text: str


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """抽取器输出：不可变、可复算。

    ``status`` 取 ``pending|ready|failed|unsupported``：
    - ``ready``       正常抽取，``text``/``pages`` 可用。
    - ``unsupported`` 如扫描件无文本层；``error_code='no_text_layer'``，文本为空，
                      **不伪造**内容、不用 OCR 编造。
    """

    status: str
    extractor_version: str
    text: str = ""
    text_hash: str = ""
    page_count: int | None = None
    pages: tuple[PageText, ...] = ()
    error_code: str | None = None
    error_message: str | None = None

    @property
    def is_ready(self) -> bool:
        return self.status == "ready"


@runtime_checkable
class Extractor(Protocol):
    """单个 format 的文本抽取端口（md/txt/pdf）。"""

    format: str
    version: str

    def available(self) -> bool:
        """底层抽取后端是否可用；不可用必须让上层 fail closed。"""
        ...

    def extract(self, data: bytes, *, password: str | None = None) -> ExtractionResult:
        """抽取文本。``password`` 只在本次调用内使用，不落库、不写日志。"""
        ...


@runtime_checkable
class ExtractorRegistry(Protocol):
    """按 format 取抽取器；缺失/不可用时由调用方决定 fail-closed 行为。"""

    def get(self, fmt: str) -> Extractor | None:
        ...


# --------------------------------------------------------------------------
# tex 工程（安全解压后的逻辑视图）
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class TexResource:
    """工程内一个文件：``logical_path`` 是 canonical 相对路径，非磁盘绝对路径。"""

    logical_path: str
    size_bytes: int
    content_hash: str
    is_binary: bool = False


@dataclass(frozen=True, slots=True)
class TexProject:
    """tex 工程：一个 revision 下的多 resources。"""

    main: str
    resources: tuple[TexResource, ...] = ()

    def paths(self) -> tuple[str, ...]:
        return tuple(r.logical_path for r in self.resources)


@dataclass(frozen=True, slots=True)
class TexLimits:
    """tex 工程安全上限（默认保守；调用方可收紧，不得放宽到不安全）。"""

    max_files: int = 200
    max_total_bytes: int = 32 * 1024 * 1024
    max_file_bytes: int = 8 * 1024 * 1024
    max_depth: int = 8
    max_compression_ratio: float = 100.0
    allowed_extensions: tuple[str, ...] = (
        ".tex", ".sty", ".cls", ".bib", ".bst", ".def", ".cfg", ".clo",
        ".png", ".jpg", ".jpeg", ".pdf", ".eps", ".svg", ".tikz", ".ltx", ".txt",
    )


# --------------------------------------------------------------------------
# 受限编译（受信执行器）
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class CompileRequest:
    """声明式受限任务：只有 inputs + 期望产物，**不含** shell / 可执行文件 / 命令行。"""

    revision_id: str
    main: str
    sources: tuple[TexResource, ...] = ()
    engine: str = "pdflatex"
    timeout_seconds: int = 120


@dataclass(frozen=True, slots=True)
class CompileResult:
    """编译产物（仍不可信，需 size/magic 校验后入库）。"""

    status: str
    artifact_bytes: bytes | None = None
    engine_version: str | None = None
    log: str = ""
    source_map: dict | None = None
    error_code: str | None = None
    error_message: str | None = None


@runtime_checkable
class TexCompileExecutor(Protocol):
    """受信编译执行器端口（独立基础设施，非本 App 进程内）。

    实现**不得**：挂 Docker socket、访问网络、读取卷外文件、执行任意 shell。
    """

    def available(self) -> bool:
        """执行器是否已部署且就绪；``False`` 时上层必须返回 503，不入队。"""
        ...

    def engine_version(self) -> str | None:
        ...

    def compile(self, request: CompileRequest) -> CompileResult:  # pragma: no cover - 接口
        ...


# --------------------------------------------------------------------------
# 适配器端口聚合
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class FormatAdapters:
    """一次装配里可用的格式能力集合（缺失项显式为空）。"""

    extractors: ExtractorRegistry | None = None
    tex_compiler: TexCompileExecutor | None = None
    extra: dict = field(default_factory=dict)
