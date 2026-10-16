"""统一引用模型（source_ref / selector）与偏移单位换算。

顶层字段与 `document-readers.md` / `instance-app-api.md` 共用**只有一套**定义：

    source_ref = {document_id, revision_id, artifact_id?, build_id?, selector, quote,
                  prefix?, suffix?, extractor_version?}

selector 判别联合：
  - text        {type:'text', start, end, unit:'codepoint'}
  - pdf         {type:'pdf', pages:[{page, rects?|quads?}],
                 coordinate_space:'normalized-cropbox-unrotated-top-left'}
  - tex-source  {type:'tex-source', file, start, end, unit:'codepoint'}   # file 必填
  - whole-document {type:'whole-document'}                               # 显式降级占位

本模块只做**结构校验与单位换算**，不接触存储、不做 IO。
持久层恒为 ``codepoint``；浏览器 ``utf16`` 进入时必须显式换算，
**不得**在同一条 anchor 里混存两种单位。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.domain.errors import DomainError
from backend.domain.text import (
    codepoint_offset_to_utf16 as _cp_to_utf16,
    utf16_offset_to_codepoint as _utf16_to_cp,
    validate_span,
)

from .errors import InvalidSourceRef

CANON_PDF_COORDINATE_SPACE = "normalized-cropbox-unrotated-top-left"
CODEPOINT = "codepoint"
UTF16 = "utf16"
_OFFSET_UNITS = (CODEPOINT, UTF16)


# --------------------------------------------------------------------------
# 偏移单位换算（BMP 外字符 1 码点 = 2 code unit）
# --------------------------------------------------------------------------

def utf16_to_codepoint(text: str, offset: int) -> int:
    """UTF-16 code unit 偏移 -> 码点偏移。

    单一真值在 ``domain.text``；本函数只是把领域错误换成格式层的
    ``InvalidSourceRef``（供 URL/JSON 校验路径统一映射）。
    """

    try:
        return _utf16_to_cp(text, offset)
    except DomainError as exc:
        raise InvalidSourceRef(exc.message, details=exc.details) from None


def codepoint_to_utf16(text: str, offset: int) -> int:
    """码点偏移 -> UTF-16 code unit 偏移（委托 ``domain.text``）。"""

    try:
        return _cp_to_utf16(text, offset)
    except DomainError as exc:
        raise InvalidSourceRef(exc.message, details=exc.details) from None


def normalize_to_codepoint(text: str, start: int, end: int, unit: str) -> tuple[int, int]:
    """把 (start, end, unit) 归一化为持久层使用的码点偏移。"""

    if unit not in _OFFSET_UNITS:
        raise InvalidSourceRef("未知偏移单位", details={"unit": unit})
    if start > end:
        raise InvalidSourceRef("start 不得大于 end", details={"start": start, "end": end})
    if unit == CODEPOINT:
        try:
            validate_span(text, start, end)
        except DomainError as exc:
            raise InvalidSourceRef(exc.message, details=exc.details) from None
        return start, end
    return utf16_to_codepoint(text, start), utf16_to_codepoint(text, end)


# --------------------------------------------------------------------------
# selector 结构
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class TextSelector:
    start: int
    end: int
    unit: str = CODEPOINT

    type: str = field(default="text", init=False)


@dataclass(frozen=True, slots=True)
class TexSourceSelector:
    file: str
    start: int
    end: int
    unit: str = CODEPOINT

    type: str = field(default="tex-source", init=False)


@dataclass(frozen=True, slots=True)
class PdfPageAnchor:
    page: int
    rects: tuple[dict[str, float], ...] = ()
    quads: tuple[dict[str, float], ...] = ()


@dataclass(frozen=True, slots=True)
class PdfSelector:
    pages: tuple[PdfPageAnchor, ...]
    coordinate_space: str = CANON_PDF_COORDINATE_SPACE
    rotation: int | None = None
    cropbox_transform: tuple[float, ...] | None = None

    type: str = field(default="pdf", init=False)


@dataclass(frozen=True, slots=True)
class WholeDocumentSelector:
    type: str = field(default="whole-document", init=False)


Selector = TextSelector | TexSourceSelector | PdfSelector | WholeDocumentSelector


def _require(condition: bool, message: str, **details: Any) -> None:
    if not condition:
        raise InvalidSourceRef(message, details=details or None)


def parse_selector(raw: Any) -> Selector:
    """把外部（JSON）selector 校验并转成内部 dataclass。

    无法可靠表达的形态一律**拒绝**，不猜测、不降级成 whole-document。
    """

    _require(isinstance(raw, dict), "selector 必须是对象")
    kind = raw.get("type")
    if kind == "text":
        unit = raw.get("unit", CODEPOINT)
        start, end = int(raw.get("start", -1)), int(raw.get("end", -1))
        _require(start >= 0 and end >= start, "text selector 偏移非法", start=start, end=end)
        _require(unit in _OFFSET_UNITS, "text selector unit 非法", unit=unit)
        return TextSelector(start=start, end=end, unit=unit)
    if kind == "tex-source":
        file = raw.get("file")
        _require(isinstance(file, str) and file.strip() != "", "tex-source selector 必须带 file")
        _require(not file.startswith(("/", "\\")) and ".." not in file.split("/"),
                 "tex-source file 必须是工程内 canonical 相对路径", file=file)
        unit = raw.get("unit", CODEPOINT)
        _require(unit in _OFFSET_UNITS, "tex-source selector unit 非法", unit=unit)
        return TexSourceSelector(
            file=file, start=int(raw.get("start", -1)), end=int(raw.get("end", -1)), unit=unit
        )
    if kind == "pdf":
        space = raw.get("coordinate_space")
        _require(space == CANON_PDF_COORDINATE_SPACE,
                 "pdf selector 只接受唯一 canon 坐标", coordinate_space=space)
        pages_raw = raw.get("pages")
        _require(isinstance(pages_raw, list) and len(pages_raw) > 0, "pdf selector 必须带非空 pages[]")
        pages: list[PdfPageAnchor] = []
        for item in pages_raw:
            _require(isinstance(item, dict), "pdf pages 项必须是对象")
            page = int(item.get("page", 0))
            _require(page >= 1, "pdf page 从 1 开始", page=page)
            pages.append(
                PdfPageAnchor(
                    page=page,
                    rects=tuple(dict(r) for r in item.get("rects", []) or []),
                    quads=tuple(dict(q) for q in item.get("quads", []) or []),
                )
            )
        return PdfSelector(pages=tuple(pages))
    if kind == "whole-document":
        return WholeDocumentSelector()
    raise InvalidSourceRef("未知 selector type", details={"type": kind})


@dataclass(frozen=True, slots=True)
class SourceRef:
    """解析后的统一引用（顶层字段名与专文共用）。"""

    document_id: str
    revision_id: str
    selector: Selector
    quote: str = ""
    prefix: str = ""
    suffix: str = ""
    artifact_id: str | None = None
    build_id: str | None = None
    extractor_version: str | None = None

    @classmethod
    def parse(cls, raw: Any) -> "SourceRef":
        _require(isinstance(raw, dict), "source_ref 必须是对象")
        document_id = raw.get("document_id")
        revision_id = raw.get("revision_id")
        _require(isinstance(document_id, str) and document_id != "", "source_ref 缺 document_id")
        _require(isinstance(revision_id, str) and revision_id != "", "source_ref 缺 revision_id")
        selector = parse_selector(raw.get("selector"))
        return cls(
            document_id=document_id,
            revision_id=revision_id,
            selector=selector,
            quote=str(raw.get("quote", "") or ""),
            prefix=str(raw.get("prefix", "") or ""),
            suffix=str(raw.get("suffix", "") or ""),
            artifact_id=raw.get("artifact_id") or None,
            build_id=raw.get("build_id") or None,
            extractor_version=raw.get("extractor_version") or None,
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "document_id": self.document_id,
            "revision_id": self.revision_id,
            "selector": _selector_to_dict(self.selector),
            "quote": self.quote,
        }
        if self.prefix:
            payload["prefix"] = self.prefix
        if self.suffix:
            payload["suffix"] = self.suffix
        if self.artifact_id:
            payload["artifact_id"] = self.artifact_id
        if self.build_id:
            payload["build_id"] = self.build_id
        if self.extractor_version:
            payload["extractor_version"] = self.extractor_version
        return payload


def _selector_to_dict(selector: Selector) -> dict[str, Any]:
    if isinstance(selector, TextSelector):
        return {"type": "text", "start": selector.start, "end": selector.end, "unit": selector.unit}
    if isinstance(selector, TexSourceSelector):
        return {
            "type": "tex-source",
            "file": selector.file,
            "start": selector.start,
            "end": selector.end,
            "unit": selector.unit,
        }
    if isinstance(selector, PdfSelector):
        return {
            "type": "pdf",
            "pages": [
                {"page": p.page, "rects": [dict(r) for r in p.rects], "quads": [dict(q) for q in p.quads]}
                for p in selector.pages
            ],
            "coordinate_space": selector.coordinate_space,
        }
    return {"type": "whole-document"}
