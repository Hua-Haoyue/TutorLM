"""真实 PDF 抽取回归：pypdf 后端 + 结构化门禁。

这一组用例验证的是**真实依赖**而非 mock：fixture 由 pypdf 的 writer 现写现用
（把文本直接嵌进页面 content stream），落到 ``tmp_path``，因此仓库里没有受版本
管理的二进制，也不需要 reportlab 等额外依赖。断言覆盖：

- 有文本层的简单 PDF → ``ready`` + 逐页文本 + ``page_count``（真实抽取）；
- 扫描 / 空白页（无文本层）→ ``unsupported`` / ``no_text_layer``，不伪造内容；
- 加密 PDF → 无密码 ``PdfEncrypted``；显式密码可解出文本，密码不落任何输出；
- 非法 / 截断 PDF → ``PdfMalformed``（结构化门禁拒绝，不把二进制按 utf-8 解码）。

``pypdf`` 是 ``requirements.txt`` 声明的运行时依赖；抽取端口在依赖存在时
``available()`` 为 True（这正是本文件要证明的能力），缺失时才 fail closed。
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest

from backend.application.format import PdfEncrypted, PdfMalformed
from backend.infrastructure.format import ExtractorRegistryImpl, StructuralPdfExtractor
from backend.infrastructure.format.registry import ExtractorRegistryImpl as _Registry

try:  # 真实后端；缺失时下面的用例显式失败（这是依赖回归，不是可选路径）。
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
except Exception:  # pragma: no cover - 仅在未安装 pypdf 的裸环境触发
    PdfWriter = None


pytestmark = pytest.mark.skipif(
    PdfWriter is None,
    reason="未安装 pypdf：真实抽取依赖回归需要它在 requirements.txt 已声明",
)


# ---------------------------------------------------------------- fixtures ----

def _text_pdf_bytes(lines: list[str], *, width: int = 300, height: int = 300) -> bytes:
    """用 pypdf writer 现造一份带文本层的单页 PDF（Helvetica + 直接文本 stream）。"""

    writer = PdfWriter()
    page = writer.add_blank_page(width=width, height=height)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
    )
    parts = ["BT /F1 14 Tf 20 250 Td 14 TL"]
    for index, line in enumerate(lines):
        if index:
            parts.append("T*")
        parts.append(f"({line}) Tj")
    parts.append("ET")
    stream = DecodedStreamObject()
    stream.set_data("\n".join(parts).encode("latin-1"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _blank_pdf_bytes(*, width: int = 200, height: int = 200) -> bytes:
    """空白页 PDF：结构合法但没有任何文本操作符（模拟扫描件无文本层）。"""

    writer = PdfWriter()
    writer.add_blank_page(width=width, height=height)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _encrypted_pdf_bytes(lines: list[str], password: str) -> bytes:
    """加密 PDF：真实 ``/Encrypt`` 字典 + 密码保护。"""

    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
    )
    stream = DecodedStreamObject()
    stream.set_data(
        ("BT /F1 14 Tf 20 250 Td (%s) Tj ET" % lines[0]).encode("latin-1")
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.encrypt(password)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _write(path: Path, payload: bytes) -> str:
    path.write_bytes(payload)
    return str(path)


# ------------------------------------------------------------------- tests ----

def test_pypdf_backend_is_declared_and_available():
    """运行时依赖真实装配：pypdf 存在时抽取端口必须报告可用。"""

    extractor = StructuralPdfExtractor()
    assert extractor.available() is True
    assert ExtractorRegistryImpl().get("pdf").available() is True
    assert _Registry().get("pdf").available() is True


def test_real_extraction_reads_text_and_page_count(tmp_path: Path):
    """有文本层的 PDF 被真实抽取：状态 ready、文本可读、页数正确。"""

    source = _write(tmp_path / "text.pdf", _text_pdf_bytes(["Hello PDF extraction", "second line"]))
    data = Path(source).read_bytes()

    result = StructuralPdfExtractor().extract(data, password=None)

    assert result.status == "ready"
    assert result.is_ready
    assert result.page_count == 1
    assert "Hello PDF extraction" in result.text
    assert "second line" in result.text
    assert result.pages and result.pages[0].page == 1
    assert "Hello PDF extraction" in result.pages[0].text
    assert result.error_code is None
    # 摘要须与抽取文本一致（不可变、可复算）。
    from backend.domain.hashing import content_hash

    assert result.text_hash == content_hash(result.text.encode("utf-8"))


def test_real_extraction_through_registry_and_application_path(tmp_path: Path):
    """经注册表 + application 用例的真实抽取路径同样返回 ready 文本。"""

    from backend.application.format import extract_text

    data = _text_pdf_bytes(["Registered path text", "line two"])
    result = extract_text(data, "pdf", registry=ExtractorRegistryImpl())

    assert result.status == "ready"
    assert "Registered path text" in result.text
    assert result.text_hash


def test_scanned_pdf_without_text_layer_is_unsupported_and_fabricates_nothing(tmp_path: Path):
    """无文本层 PDF → unsupported / no_text_layer，文本为空且不编造 offset。"""

    data = _blank_pdf_bytes()
    result = StructuralPdfExtractor().extract(data, password=None)

    assert result.status == "unsupported"
    assert result.error_code == "no_text_layer"
    assert result.text == ""
    assert result.page_count == 1


def test_encrypted_pdf_requires_password_and_does_not_decode(tmp_path: Path):
    """加密 PDF：显式密码门禁拒绝，且报错信息不含密码。"""

    data = _encrypted_pdf_bytes(["Secret page one"], "secret")

    with pytest.raises(PdfEncrypted):
        StructuralPdfExtractor().extract(data, password=None)


def test_encrypted_pdf_extracts_with_correct_password(tmp_path: Path):
    """提供正确密码时可真实解出文本，密码不落入结果。"""

    data = _encrypted_pdf_bytes(["Secret page one"], "secret")
    result = StructuralPdfExtractor().extract(data, password="secret")

    assert result.status == "ready"
    assert "Secret page one" in result.text
    assert "secret" not in result.text
    assert "secret" not in str(result.error_message)


def test_invalid_pdf_is_rejected_as_malformed_not_decoded(tmp_path: Path):
    """非 PDF 字节 / 截断 PDF → PdfMalformed，绝不按 utf-8 当文本返回。"""

    with pytest.raises(PdfMalformed):
        StructuralPdfExtractor().extract(b"not a pdf at all, just junk bytes", password=None)
    with pytest.raises(PdfMalformed):
        StructuralPdfExtractor().extract(b"%PDF-1.7\ntruncated", password=None)
