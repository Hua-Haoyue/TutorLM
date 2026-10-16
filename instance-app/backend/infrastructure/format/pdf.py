"""PDF 适配器：结构探测 + 文本抽取端口实现。

关键立场：

- **fail closed**：没有可用的 pdf 抽取库时，``available()`` 为 ``False``，
  上层必须返回 ``extractor_unavailable``；**绝不**把 pdf 二进制按 utf-8 解码当文本。
- **扫描件不编造**：无文本层 → ``status='unsupported'``、``error_code='no_text_layer'``，
  不用 OCR 猜测内容（OCR 为可选、默认关闭）。
- **密码不持久化**：``password`` 只在本文件内使用，不落库、不写日志。
- 结构探测（加密 / 畸形）不依赖第三方库：只看 trailer 关键字与基本结构，
  用于在抽取前给出 ``pdf_encrypted`` / ``pdf_malformed``。
- **解析器错误归一化**：真正的抽取后端（pypdf）抛出的 ``PyPdfError`` 家族
  （缺 header / 缺 Root / 坏 xref / 截断流 / 密码错误等）统一收敛为格式层
  ``PdfMalformed`` / ``PdfEncrypted``，**绝不**把 pypdf 的原始 traceback
  文本泄漏给上层或落库；非解析器的意外异常原样抛出，不被误报成畸形 pdf。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from backend.application.format.contracts import ExtractionResult, PageText
from backend.domain.hashing import content_hash
from backend.application.format.errors import DomainError, PdfEncrypted, PdfMalformed
from backend.application.format.media import PDF

EXTRACTOR_VERSION = "pdf-structural-v1"

#: 视为“密码问题”的解析器异常类型名（不与可选依赖强绑定 import）。
_PASSWORD_ERROR_NAMES = frozenset({"WrongPasswordError", "FileNotDecryptedError"})

#: 格式层错误元组：``extract`` 用它识别“已经规范化”的错误，避免二次包装
#: 显式密码门禁 / 后备实现主动抛出的格式错误。
_FORMAT_ERRORS = (PdfEncrypted, PdfMalformed)


def _parser_error_types() -> tuple[type[BaseException], ...]:
    """pypdf 解析器错误家族基类；``pypdf`` 是可选依赖，故**惰性**解析。

    未安装时返回空元组：此时本就落在我们自己的 fail-closed 路径上。
    """

    try:
        from pypdf.errors import PyPdfError  # type: ignore
    except Exception:  # pragma: no cover - 未安装 pypdf 的裸环境
        return ()
    return (PyPdfError,)


def _is_parser_error(exc: BaseException) -> bool:
    """``exc`` 是否属于 pypdf 解析器错误家族（未安装 pypdf 时为 False）。"""

    types = _parser_error_types()
    return bool(types) and isinstance(exc, types)


def _as_format_error(exc: BaseException) -> DomainError:
    """把解析器异常收敛为格式层错误。

    - 密码类解析器错误（``WrongPasswordError`` / ``FileNotDecryptedError``）→
      ``PdfEncrypted``（密码问题，不是结构损坏）；
    - 其它 pypdf ``PyPdfError``（缺 header / 缺 Root / 坏 xref / 截断流）→
      ``PdfMalformed``；``details`` 只放稳定类型名，**不**放解析器 message，
      避免文件内容片段随错误文本泄漏。
    """

    name = type(exc).__name__
    if name in _PASSWORD_ERROR_NAMES:
        return PdfEncrypted("pdf 已加密：密码缺失或错误，密码不落库、不写日志")
    return PdfMalformed("pdf 无法解析为有效结构", details={"parser_error": name})


_PDF_HEADER = re.compile(rb"%PDF-(\d\.\d)")
_ENCRYPT = re.compile(rb"/Encrypt\b")
_OBJSTREAM = re.compile(rb"/ObjStm\b")
_TRAILER = re.compile(rb"trailer\b")
_PAGE_TYPE = re.compile(rb"/Type\s*/Page\b")


@dataclass(frozen=True, slots=True)
class PdfStructure:
    version: str
    has_eof: bool
    has_xref: bool
    encrypted: bool
    page_markers: int
    size_bytes: int

    @property
    def malformed(self) -> bool:
        # 缺少 header 的调用方早已拒绝；这里判断明显不完整/不可解析的结构。
        return not self.has_eof and not self.has_xref


def inspect(data: bytes) -> PdfStructure:
    """无依赖结构探测：只做保守判断，不假装能完整解析。"""

    if not data.startswith(b"%PDF-"):
        raise PdfMalformed("缺少 %PDF- 签名，拒绝按 pdf 处理")
    version_match = _PDF_HEADER.search(data[:1024])
    version = version_match.group(1).decode("ascii") if version_match else "unknown"
    tail = data[-2048:]
    has_eof = b"%%EOF" in tail
    has_xref = b"startxref" in tail or b"xref" in tail
    encrypted = _ENCRYPT.search(data) is not None
    structure = PdfStructure(
        version=version,
        has_eof=has_eof,
        has_xref=has_xref,
        encrypted=encrypted,
        page_markers=len(_PAGE_TYPE.findall(data)),
        size_bytes=len(data),
    )
    if structure.malformed:
        raise PdfMalformed("pdf 结构不完整（缺 %%EOF/startxref）", details={"version": version})
    return structure


def open_pdf(data: bytes, *, password: str | None = None) -> PdfStructure:
    """抽取前的门禁：畸形 → ``pdf_malformed``；加密 → ``pdf_encrypted``。

    传了 ``password`` 不改变这里的结论——密码校验交给真正的抽取库；
    此处只保证"有加密就必须显式走密码路径，不静默产出空文本"。
    """

    structure = inspect(data)
    if structure.encrypted and not password:
        raise PdfEncrypted("pdf 已加密：需显式提供密码，密码不持久化")
    return structure


_AUTO_BACKEND = object()


class StructuralPdfExtractor:
    """抽取端口实现。

    ``available()`` 取决于是否装配了真正的文本抽取后端（例如 pypdf）；
    没有该依赖时**不抽取、不伪造**，由上层 fail closed。
    """

    format = PDF
    version = EXTRACTOR_VERSION

    def __init__(self, backend=_AUTO_BACKEND) -> None:
        # ``backend`` 需实现 ``extract(data, password) -> (pages, page_count, encrypted)``。
        self._backend = _load_default_backend() if backend is _AUTO_BACKEND else backend

    def available(self) -> bool:
        return self._backend is not None

    def extract(self, data: bytes, *, password: str | None = None) -> ExtractionResult:
        open_pdf(data, password=password)
        if self._backend is None:
            # 上层应在此之前拒绝；这里再失败一次，绝不返回伪造文本。
            from backend.application.format.errors import ExtractUnavailable

            raise ExtractUnavailable("无 pdf 文本抽取后端：fail closed")
        try:
            pages, page_count, used_password = self._backend.extract(data, password)
        except _FORMAT_ERRORS:
            # 已经是格式层错误（如显式密码门禁 / 后备实现主动抛的 PdfEncrypted）。
            raise
        except Exception as exc:
            # pypdf 家族（缺 header / 缺 Root / 坏 xref / 截断流 / 密码错误）
            # 收敛为格式层错误，绝不向上泄漏原始 traceback 文本；
            # 非解析器的意外异常（例如后端自身 bug）保持原样，不被误报成畸形 pdf。
            if _is_parser_error(exc):
                raise _as_format_error(exc) from None
            raise
        page_texts = tuple(PageText(page=index + 1, text=text) for index, text in enumerate(pages))
        joined = "\n".join(text for text in pages)
        if joined.strip() == "":
            return ExtractionResult(
                status="unsupported",
                extractor_version=self.version,
                text="",
                text_hash=_sha256_hex(b""),
                page_count=page_count,
                pages=page_texts,
                error_code="no_text_layer",
                error_message="该 pdf 无可提取文本层（扫描件？）；不编造内容，OCR 需显式启用",
            )
        return ExtractionResult(
            status="ready",
            extractor_version=self.version,
            text=joined,
            text_hash=_sha256_hex(joined.encode("utf-8")),
            page_count=page_count,
            pages=page_texts,
            error_code=None,
            error_message=None,
        )


def _load_default_backend():
    """懒加载可选的 pdf 后端；**未安装即返回 None**（fail closed，不装依赖）。"""

    try:
        import pypdf  # type: ignore
    except Exception:
        return None

    class _PypdfBackend:
        def extract(self, data: bytes, password: str | None):
            from io import BytesIO

            reader = pypdf.PdfReader(BytesIO(data))
            if reader.is_encrypted:
                if password is None:
                    from backend.application.format.errors import PdfEncrypted as _Enc

                    raise _Enc("pdf 已加密：需显式提供密码")
                # ``decrypt`` 以 falsy 结果表示“未能解密”（密码错误），不一定抛异常；
                # 两种表现都收敛为 ``PdfEncrypted``，绝不继续去抽空文本。
                if not reader.decrypt(password):
                    from backend.application.format.errors import PdfEncrypted as _Enc

                    raise _Enc("pdf 已加密：密码缺失或错误，密码不落库、不写日志")
            texts = []
            for page in reader.pages:
                texts.append(page.extract_text() or "")
            return texts, len(reader.pages), password is not None

    return _PypdfBackend()


def _sha256_hex(payload: bytes) -> str:
    """内容摘要：委托 ``domain.hashing.content_hash``（裸十六进制）。"""

    return content_hash(payload)
