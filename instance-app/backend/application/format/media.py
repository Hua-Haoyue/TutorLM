"""媒体探测：signature + extension + 声明 mime 三者交叉判定。

**不盲信**任何单一来源；冲突或不可识别一律拒绝（``unsupported_media``），
不猜测、不按扩展名强行当文本读。文本类（md/txt）必须严格 UTF-8。

tex 用「工程入口 + resources 形状」判定：单文件 `.tex`，或 `.zip` 工程包
（解压前只按签名/扩展名归类，真正的结构校验在 ``infrastructure/format`` 里做）。
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import EncodingUnsupported, UnsupportedMedia

MD = "md"
TXT = "txt"
PDF = "pdf"
TEX = "tex"
FORMATS = (MD, TXT, PDF, TEX)

TEXT_FORMATS = (MD, TXT)
_ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_PDF_MAGIC = b"%PDF-"


@dataclass(frozen=True, slots=True)
class MediaProbe:
    """探测结论：``format`` 为落库枚举；``is_zip_project`` 表明 tex 是 zip 工程包。"""

    format: str
    declared_mime: str | None
    filename: str | None
    is_zip_project: bool = False

    @property
    def is_text(self) -> bool:
        return self.format in TEXT_FORMATS


_EXT_TO_FORMAT = {
    ".md": MD,
    ".markdown": MD,
    ".txt": TXT,
    ".text": TXT,
    ".pdf": PDF,
    ".tex": TEX,
    ".zip": TEX,  # tex 多文件工程包
}


def _extension(filename: str | None) -> str | None:
    if not filename:
        return None
    name = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].strip().lower()
    dot = name.rfind(".")
    return name[dot:] if dot > 0 else None


def _mime_format(declared_mime: str | None) -> str | None:
    if not declared_mime:
        return None
    mime = declared_mime.split(";", 1)[0].strip().lower()
    return {
        "text/markdown": MD,
        "text/x-markdown": MD,
        "text/plain": TXT,
        "application/pdf": PDF,
        "application/x-tex": TEX,
        "text/x-tex": TEX,
        "application/zip": TEX,
        "application/x-zip-compressed": TEX,
        "application/octet-stream": None,
    }.get(mime)


def detect(
    data: bytes,
    *,
    filename: str | None = None,
    declared_mime: str | None = None,
) -> MediaProbe:
    """交叉判定格式；无法可靠判定即拒绝。"""

    ext = _extension(filename)
    ext_format = _EXT_TO_FORMAT.get(ext) if ext else None
    mime_format = _mime_format(declared_mime)

    signature: str | None = None
    if data.startswith(_PDF_MAGIC):
        signature = PDF
    elif data.startswith(_ZIP_MAGIC):
        signature = TEX
    elif _looks_like_utf8_text(data):
        signature = None  # 文本没有可靠 magic，只能靠解码 + 扩展名

    # zip 包与 pdf 有明确 magic：声明与签名冲突就拒绝。
    if signature == PDF:
        if ext_format not in (None, PDF) or mime_format not in (None, PDF):
            raise UnsupportedMedia(
                "pdf 签名与扩展名/声明 mime 冲突",
                details={"extension": ext, "declared_mime": declared_mime},
            )
        return MediaProbe(PDF, declared_mime, filename)

    if signature == TEX:  # zip 工程包
        if ext is not None and ext != ".zip":
            raise UnsupportedMedia(
                "zip 签名与扩展名冲突", details={"extension": ext}
            )
        if mime_format not in (None, TEX):
            raise UnsupportedMedia(
                "zip 签名与声明 mime 冲突", details={"declared_mime": declared_mime}
            )
        return MediaProbe(TEX, declared_mime, filename, is_zip_project=True)

    # 到这里的候选只有文本（md/txt）或 tex 单文件；必须真的能 UTF-8 解码。
    _require_decodable(data)

    if ext_format == TEX or mime_format == TEX:
        return MediaProbe(TEX, declared_mime, filename)

    if ext_format in TEXT_FORMATS and (mime_format in (None, ext_format) or mime_format in TEXT_FORMATS):
        return MediaProbe(ext_format, declared_mime, filename)

    if ext_format is None and mime_format in TEXT_FORMATS:
        # 无扩展名但有可信文本 mime：按 txt 处理（仍要求 UTF-8 通过）。
        return MediaProbe(mime_format, declared_mime, filename)

    raise UnsupportedMedia(
        "无法判定媒体类型（签名/扩展名/声明 mime 不一致或缺失）",
        details={"extension": ext, "declared_mime": declared_mime},
    )


def _looks_like_utf8_text(data: bytes) -> bool:
    if b"\x00" in data:
        return False
    return True


def _require_decodable(data: bytes) -> None:
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EncodingUnsupported(
            "文本类必须为 UTF-8，解码失败即拒绝（不做有损替换）",
            details={"reason": exc.reason, "position": exc.start},
        ) from exc


def decode_text(data: bytes) -> str:
    """严格 UTF-8 解码（文本类专用）。"""

    _require_decodable(data)
    return data.decode("utf-8")
