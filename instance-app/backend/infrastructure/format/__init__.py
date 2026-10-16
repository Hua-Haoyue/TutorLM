"""格式适配器实现（infrastructure/format）：md/txt/pdf 解析与抽取、tex 工程校验与受限编译。

实现**纯粹**：不执行用户代码、不联网、不读写卷外文件。
pdf 抽取器缺失即 fail closed；tex 编译只经受信 executor 端口。
"""

from __future__ import annotations

from .markdown import MarkdownBlock, MarkdownDocument, parse_markdown, render_markdown
from .pdf import EXTRACTOR_VERSION, PdfStructure, StructuralPdfExtractor, inspect, open_pdf
from .registry import ExtractorRegistryImpl, TextExtractor, TEXT_EXTRACTOR_VERSION
from .tex_compile import TexCompiler, artifact_hash
from .tex_project import normalize_logical_path, validate_tex_payload

__all__ = [
    "EXTRACTOR_VERSION",
    "ExtractorRegistryImpl",
    "TEXT_EXTRACTOR_VERSION",
    "TextExtractor",
    "MarkdownBlock",
    "MarkdownDocument",
    "PdfStructure",
    "StructuralPdfExtractor",
    "TexCompiler",
    "artifact_hash",
    "inspect",
    "normalize_logical_path",
    "open_pdf",
    "parse_markdown",
    "render_markdown",
    "validate_tex_payload",
]
