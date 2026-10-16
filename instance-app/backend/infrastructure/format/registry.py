"""抽取器注册表与文本类抽取器（md/txt passthrough）。

注册表是**装配点**：把可用/不可用的抽取器交给 application 层；调用方只问
``get(fmt)``，由它决定 fail-closed。缺失等于**没有能力**，不是"降级猜格式"。
"""

from __future__ import annotations

from backend.application.format.contracts import ExtractionResult, Extractor
from backend.application.format.media import MD, PDF, TXT
from backend.domain.hashing import content_hash
from backend.infrastructure.format.pdf import StructuralPdfExtractor

TEXT_EXTRACTOR_VERSION = "text-passthrough-v1"


class TextExtractor:
    """md/txt 抽取器：严格 UTF-8 解码后给出全文（无损失、无猜测）。"""

    def __init__(self, fmt: str) -> None:
        if fmt not in (MD, TXT):
            raise ValueError(f"TextExtractor 只支持 md/txt，收到 {fmt!r}")
        self.format = fmt
        self.version = TEXT_EXTRACTOR_VERSION

    def available(self) -> bool:
        return True

    def extract(self, data: bytes, *, password: str | None = None) -> ExtractionResult:
        if password is not None:
            raise ValueError("文本类不需要密码")
        text = data.decode("utf-8")  # 调用方应已用 media.decode_text 校验
        return ExtractionResult(
            status="ready",
            extractor_version=self.version,
            text=text,
            text_hash=content_hash(text.encode("utf-8")),
        )


class ExtractorRegistryImpl:
    """默认装配：md/txt 永远可用；pdf 取决于是否装到后端（缺则 fail closed）。"""

    def __init__(self, extractors: dict[str, Extractor] | None = None) -> None:
        if extractors is None:
            extractors = {
                MD: TextExtractor(MD),
                TXT: TextExtractor(TXT),
                PDF: StructuralPdfExtractor(),
            }
        self._extractors: dict[str, Extractor] = dict(extractors)

    def get(self, fmt: str) -> Extractor | None:
        return self._extractors.get(fmt)

    def register(self, extractor: Extractor) -> None:
        self._extractors[extractor.format] = extractor
