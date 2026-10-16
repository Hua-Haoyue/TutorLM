"""抽取用例（application 层）：组装端口，不实现具体解析。

职责：
- md/txt：安全解析 + 全文（UTF-8，失败即 ``encoding_unsupported``）。
- pdf：经 ``Extractor`` 端口抽取；抽取器不可用或缺失 → **fail closed**
  （``extractor_unavailable``）。**绝不**把 pdf 二进制当文本解码。
- 抽取结果不可变（``ExtractionResult`` 带 ``extractor_version`` 与 ``text_hash``），
  供上层登记为不可变 revision/artifact。
"""

from __future__ import annotations

from backend.domain.hashing import content_hash as _content_hash

from .contracts import ExtractionResult, Extractor, ExtractorRegistry
from .errors import BinaryContentNotText, ExtractUnavailable, UnsupportedMedia
from .media import MD, PDF, TEX, TXT, decode_text


def extract_text(data: bytes, fmt: str, *, registry: ExtractorRegistry | None = None,
                 password: str | None = None, extractor_version: str | None = None) -> ExtractionResult:
    """对文本类（md/txt）做安全解析全文；对 pdf 走抽取器端口。"""

    if fmt in (MD, TXT):
        text = decode_text(data)
        version = extractor_version or (f"{fmt}-passthrough-v1")
        return ExtractionResult(
            status="ready",
            extractor_version=version,
            text=text,
            text_hash=_sha256_hex(text.encode("utf-8")),
            page_count=None,
        )
    if fmt == PDF:
        extractor = _require_extractor(registry, PDF)
        result = extractor.extract(data, password=password)
        return _with_hash(result)
    if fmt == TEX:
        # tex 文本消费走 tex-source selector（按 file），全文走 /content 只对 md/txt。
        raise BinaryContentNotText("tex 不走文本全文端点；请按 file 使用 tex-source 引用")
    raise UnsupportedMedia("未知格式", details={"format": fmt})


def _require_extractor(registry: ExtractorRegistry | None, fmt: str) -> Extractor:
    if registry is None:
        raise ExtractUnavailable(
            "抽取器注册表未装配：pdf 抽取 fail closed，不把二进制当文本解码",
            details={"format": fmt},
        )
    extractor = registry.get(fmt)
    if extractor is None:
        raise ExtractUnavailable(
            "该格式无抽取器：fail closed", details={"format": fmt}
        )
    if not extractor.available():
        raise ExtractUnavailable(
            "抽取后端不可用：fail closed，不降级为 utf-8 解码二进制",
            details={"format": fmt, "extractor_version": getattr(extractor, "version", None)},
        )
    return extractor


def _with_hash(result: ExtractionResult) -> ExtractionResult:
    if result.text_hash:
        return result
    return ExtractionResult(
        status=result.status,
        extractor_version=result.extractor_version,
        text=result.text,
        text_hash=_sha256_hex(result.text.encode("utf-8")),
        page_count=result.page_count,
        pages=result.pages,
        error_code=result.error_code,
        error_message=result.error_message,
    )


def _sha256_hex(payload: bytes) -> str:
    """内容摘要：委托 ``domain.hashing.content_hash``（裸十六进制，对齐 ``is_content_hash``）。"""

    return _content_hash(payload)
