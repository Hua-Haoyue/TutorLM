"""文本偏移单位换算（无 IO）。

规范：后端 canonical 偏移一律 **Unicode code point**（``unit:'codepoint'``）。
浏览器 DOM 侧偏移是 UTF-16 code unit，**持久化前必须换算**，不得两种单位混存
（设计 §15）。这里提供双向换算与区间校验。
"""

from __future__ import annotations

from backend.domain.errors import DomainError, EncodingUnsupported

CODEPOINT_UNIT = "codepoint"
UTF16_UNIT = "utf16"


def decode_utf8_strict(data: bytes) -> str:
    """严格 UTF-8 解码；失败即为 ``encoding_unsupported``，不做有损替换。"""

    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EncodingUnsupported(
            "文本类内容必须是合法 UTF-8；不做有损替换",
            details={"offset": exc.start, "reason": exc.reason},
        ) from None


def utf16_offset_to_codepoint(text: str, utf16_offset: int) -> int:
    """把 UTF-16 code unit 偏移换算成 code point 偏移。

    落在代理对上（mid-surrogate）视为非法：多出来的半个代理对会让所有
    后续偏移静默漂移，因此宁可报错。
    """

    if utf16_offset < 0:
        raise DomainError("offset 不能为负", code="invalid_offset", http_status=400)
    consumed = 0
    for index, char in enumerate(text):
        if consumed == utf16_offset:
            return index
        consumed += 2 if ord(char) > 0xFFFF else 1
        if consumed > utf16_offset:
            raise DomainError(
                "offset 落在代理对中间，无法换算为 code point",
                code="invalid_offset",
                http_status=400,
                details={"utf16_offset": utf16_offset},
            )
    if consumed == utf16_offset:
        return len(text)
    raise DomainError(
        "offset 超出文本长度",
        code="invalid_offset",
        http_status=400,
        details={"utf16_offset": utf16_offset, "utf16_length": consumed},
    )


def codepoint_offset_to_utf16(text: str, codepoint_offset: int) -> int:
    """反向换算：code point 偏移 -> UTF-16 code unit 偏移。"""

    if codepoint_offset < 0:
        raise DomainError("offset 不能为负", code="invalid_offset", http_status=400)
    if codepoint_offset > len(text):
        raise DomainError(
            "offset 超出文本长度",
            code="invalid_offset",
            http_status=400,
            details={"codepoint_offset": codepoint_offset, "length": len(text)},
        )
    return sum(2 if ord(char) > 0xFFFF else 1 for char in text[:codepoint_offset])


def validate_span(text: str, start: int, end: int) -> tuple[int, int]:
    """``0 <= start <= end <= len(text)``；返回同一对值（便于链式使用）。"""

    if start < 0 or end < 0:
        raise DomainError("span 不能为负", code="invalid_offset", http_status=400)
    if end < start:
        raise DomainError("span 的 end 不能小于 start", code="invalid_offset", http_status=400)
    if end > len(text):
        raise DomainError(
            "span 超出文本长度",
            code="invalid_offset",
            http_status=400,
            details={"start": start, "end": end, "length": len(text)},
        )
    return start, end


def slice_span(text: str, start: int, end: int) -> str:
    start, end = validate_span(text, start, end)
    return text[start:end]
