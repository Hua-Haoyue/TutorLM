"""格式适配器端口层（application/format）。

本包只定义**契约与纯逻辑**（选择器校验、媒体探测、偏移换算、抽取用例）；
具体解析 / 抽取 / 编译实现在 ``infrastructure/format``。

对齐（对齐审查后）：
- 偏移换算 / UTF-8 解码 / 内容摘要的单一真值在 ``backend.domain``；
  本层只做**格式层的校验外壳**（把领域错误换成 ``InvalidSourceRef`` 等）。
- 错误分类复用 foundation ``domain.errors``；run 生命周期错误不在此层定义。
- provider / 引用解析契约在 foundation ``application.ports`` 与 host 侧。

pdf 抽取器缺失必须 fail closed；无受信执行器时编译不可用。
"""

from __future__ import annotations

from .contracts import (
    CompileRequest,
    CompileResult,
    ExtractionResult,
    Extractor,
    ExtractorRegistry,
    FormatAdapters,
    PageText,
    TexCompileExecutor,
    TexLimits,
    TexProject,
    TexResource,
)
from .errors import (
    BinaryContentNotText,
    EncodingUnsupported,
    ExtractUnavailable,
    FormatError,
    InvalidRunInput,
    InvalidSourceRef,
    PdfEncrypted,
    PdfMalformed,
    PdfNoTextLayer,
    ProviderUnavailable,
    TexCompileUnavailable,
    TexMainInvalid,
    TexProjectRejected,
    UnsupportedMedia,
)
from .extraction import extract_text
from .media import FORMATS, MD, PDF, TEX, TXT, MediaProbe, detect
from .selectors import (
    CANON_PDF_COORDINATE_SPACE,
    PdfPageAnchor,
    PdfSelector,
    SourceRef,
    TexSourceSelector,
    TextSelector,
    WholeDocumentSelector,
    codepoint_to_utf16,
    normalize_to_codepoint,
    parse_selector,
    utf16_to_codepoint,
)

__all__ = [
    "CANON_PDF_COORDINATE_SPACE",
    "BinaryContentNotText",
    "CompileRequest",
    "CompileResult",
    "EncodingUnsupported",
    "ExtractUnavailable",
    "ExtractionResult",
    "Extractor",
    "ExtractorRegistry",
    "FORMATS",
    "FormatAdapters",
    "FormatError",
    "InvalidRunInput",
    "InvalidSourceRef",
    "MD",
    "MediaProbe",
    "PDF",
    "PageText",
    "PdfEncrypted",
    "PdfMalformed",
    "PdfNoTextLayer",
    "PdfPageAnchor",
    "PdfSelector",
    "ProviderUnavailable",
    "SourceRef",
    "TEX",
    "TXT",
    "TexCompileExecutor",
    "TexCompileUnavailable",
    "TexLimits",
    "TexMainInvalid",
    "TexProject",
    "TexProjectRejected",
    "TexResource",
    "TexSourceSelector",
    "TextSelector",
    "UnsupportedMedia",
    "WholeDocumentSelector",
    "codepoint_to_utf16",
    "detect",
    "extract_text",
    "normalize_to_codepoint",
    "parse_selector",
    "utf16_to_codepoint",
]
