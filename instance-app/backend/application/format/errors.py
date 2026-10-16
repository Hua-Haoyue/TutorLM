"""格式/媒体专属错误：**继承 foundation ``domain.errors.DomainError``**。

对齐（对齐审查后）：错误分类的单一真值在 ``backend.domain.errors``。凡是 foundation
已拥有语义的码位都**不在这里重定义**，而是从 ``domain.errors`` 直接复用（本模块
只是把它们再导出，类对象同一）：

- ``unsupported_media`` / ``encoding_unsupported``：媒体探测与文本解码。
- ``tex_compile_unavailable``：无受信编译执行器。
- ``provider_unavailable``：provider 端口不可用。

run 生命周期错误（``run_not_found``/``idempotency_conflict``/队列满等）**不属本层**，
由 foundation ``domain.run`` / host 负责，本模块不再定义，避免两套真值。

本模块只保留**格式适配器特有**的错误，并显式使用 ``domain.errors.DomainError`` 的
``code``/``http_status``/``details`` 契约，以便 host/http 用同一信封映射。
"""

from __future__ import annotations

from typing import Any

from backend.domain.errors import (
    DomainError,
    EncodingUnsupported,
    ProviderUnavailable,
    TexCompileUnavailable,
    UnsupportedMedia,
)

__all__ = [
    "BinaryContentNotText",
    "DomainError",
    "EncodingUnsupported",
    "ExtractUnavailable",
    "FormatError",
    "InvalidRunInput",
    "InvalidSourceRef",
    "PdfEncrypted",
    "PdfMalformed",
    "PdfNoTextLayer",
    "ProviderUnavailable",
    "TexCompileUnavailable",
    "TexMainInvalid",
    "TexProjectRejected",
    "UnsupportedMedia",
]


class FormatError(DomainError):
    """格式适配器错误基类（``DomainError`` 的格式层别名）。"""

    code = "format_error"
    http_status = 400

    def to_error(self) -> dict[str, Any]:
        """向后兼容别名：与 ``DomainError.to_dict`` 同形。"""

        return self.to_dict()


class InvalidSourceRef(FormatError):
    """source_ref / selector 结构非法（缺字段、字段冲突、半代理切分等）。"""

    code = "invalid_source_ref"
    http_status = 400


class BinaryContentNotText(FormatError):
    """对非纯文本类（pdf / tex）请求全文文本：原件端点与抽取端点分离。

    ``code`` 与 foundation host 的 ``content_not_available`` 对齐，避免两套命名。
    """

    code = "content_not_available"
    http_status = 409


class PdfMalformed(FormatError):
    code = "pdf_malformed"
    http_status = 422


class PdfEncrypted(FormatError):
    """有密码的 pdf：密码只在请求内使用，不落库、不写日志。"""

    code = "pdf_encrypted"
    http_status = 409


class PdfNoTextLayer(FormatError):
    """扫描 / 无文本层：明确 unsupported，不编造文本，也不用 OCR 伪造。"""

    code = "no_text_layer"
    http_status = 422


class ExtractUnavailable(FormatError):
    """抽取器 / 抽取执行器不可用：fail closed，绝不把二进制当文本解码。"""

    code = "extractor_unavailable"
    http_status = 503


class TexMainInvalid(FormatError):
    code = "tex_main_invalid"
    http_status = 422


class TexProjectRejected(FormatError):
    """tex 工程（含 zip）不通过安全校验：越界 / 符号链接 / 炸弹 / 超限。"""

    code = "tex_project_rejected"
    http_status = 422

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=code, details=details)


class InvalidRunInput(FormatError):
    """workflow 入参非法（格式/插件层校验）。"""

    code = "invalid_run_input"
    http_status = 422
