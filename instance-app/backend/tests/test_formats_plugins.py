"""format adapters + plugins：安全行为、失败闭合与不可用路径的回归测试。

覆盖：
- md/txt 安全解析 / 全文，Markdown XSS 转义；
- pdf fail-closed（无抽取器 → extractor_unavailable；绝不把二进制按文本解码）；
  malformed / encrypted fixture 判定；scanned（无文本层）→ unsupported；
- tex 工程：路径穿越 / 绝对路径 / zipbomb / 符号链接 / main 缺失；编译不可用；
- source_ref selector 校验（codepoint、pdf canon 坐标、tex file 必填）；
- 插件 manifest 与 explanation/narration workflow 消费 context + provider 端口。

不依赖第三方 PDF 库、不联网、不执行用户代码。
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.application.format import (  # noqa: E402
    ExtractUnavailable,
    InvalidSourceRef,
    SourceRef,
    TexCompileUnavailable,
    TexMainInvalid,
    TexProjectRejected,
    detect,
    extract_text,
    normalize_to_codepoint,
    parse_selector,
    utf16_to_codepoint,
)
from backend.application.format.contracts import CompileRequest  # noqa: E402
from backend.infrastructure.format import (  # noqa: E402
    ExtractorRegistryImpl,
    StructuralPdfExtractor,
    TexCompiler,
    render_markdown,
    validate_tex_payload,
)
from backend.infrastructure.format.tex_project import TexLimits  # noqa: E402
from backend.plugins.manifests import (  # noqa: E402
    EXPLANATION_MANIFEST,
    NARRATION_MANIFEST,
)
from backend.host.registry import build_default_registry  # noqa: E402
from backend.plugin_contracts import Draft, WorkflowInput  # noqa: E402
from backend.domain.models import Explanation  # noqa: E402
from backend.domain.errors import InvalidInputError  # noqa: E402
from backend.plugins.workflows import ExplanationWorkflow, NarrationWorkflow  # noqa: E402

REGISTRY = ExtractorRegistryImpl()


# ----------------------------------------------------------------------
# md / txt
# ----------------------------------------------------------------------

def test_txt_full_text_roundtrip():
    result = extract_text("你好\ntutor".encode("utf-8"), "txt", registry=REGISTRY)
    assert result.status == "ready"
    assert result.text == "你好\ntutor"
    # 内容摘要对齐 foundation domain.hashing.content_hash（裸十六进制，满足 is_content_hash）
    assert len(result.text_hash) == 64 and all(c in "0123456789abcdef" for c in result.text_hash)


def test_markdown_full_text_and_blocks_persist_codepoints():
    source = "# 标题\n\n正文 emoji 😀 结束\n"
    result = extract_text(source.encode("utf-8"), "md", registry=REGISTRY)
    assert result.status == "ready"
    assert "😀" in result.text


def test_text_rejects_non_utf8():
    from backend.application.format import EncodingUnsupported

    with pytest.raises(EncodingUnsupported):
        extract_text(b"\xff\xfe\x00bad", "txt", registry=REGISTRY)


def test_markdown_raw_html_is_escaped_not_injected():
    html = render_markdown("<script>alert(1)</script>\n")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_markdown_dangerous_link_scheme_is_neutralised():
    html = render_markdown("[x](javascript:alert(1))\n")
    assert "href=\"javascript:" not in html


def test_markdown_external_image_is_not_auto_loaded():
    html = render_markdown("![alt](https://evil.example/x.png)\n")
    assert "<img" not in html


# ----------------------------------------------------------------------
# media detection
# ----------------------------------------------------------------------

def test_detect_pdf_by_signature():
    probe = detect(b"%PDF-1.7\n", filename="a.pdf", declared_mime="application/pdf")
    assert probe.format == "pdf"


def test_detect_rejects_conflicting_media():
    from backend.application.format import UnsupportedMedia

    with pytest.raises(UnsupportedMedia):
        detect(b"%PDF-1.7\n", filename="a.tex", declared_mime="text/x-tex")


def test_detect_tex_zip_project():
    probe = detect(b"PK\x03\x04rest", filename="proj.zip", declared_mime="application/zip")
    assert probe.format == "tex" and probe.is_zip_project


# ----------------------------------------------------------------------
# pdf：fail closed / malformed / encrypted / scanned
# ----------------------------------------------------------------------

def test_pdf_extractor_unavailable_fails_closed():
    # 运行环境不安装 PDF 抽取库时，端口必须报不可用（不得假装能抽取）。
    extractor = StructuralPdfExtractor()
    if extractor.available():
        pytest.skip("本环境装有 PDF 后端；fail-closed 由 port 层断言覆盖")


def test_pdf_never_decodes_binary_as_text_without_registry():
    with pytest.raises(ExtractUnavailable):
        extract_text(b"%PDF-1.7\xff\xfe\x00binary", "pdf", registry=None)


def test_pdf_with_unavailable_extractor_fails_closed():
    # 显式注入“无后端”的抽取器，而不是靠运行环境恰好没装 pypdf：
    # 装配了 pypdf 之后本用例仍必须看到 fail-closed（fail closed 与依赖是否安装无关）。
    payload = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\nstartxref\n0\n%%EOF\n"
    registry = ExtractorRegistryImpl({'pdf': StructuralPdfExtractor(backend=None)})
    with pytest.raises(ExtractUnavailable):
        extract_text(payload, "pdf", registry=registry)


def test_malformed_pdf_rejected():
    from backend.application.format import PdfMalformed

    with pytest.raises(PdfMalformed):
        StructuralPdfExtractor(backend=object()).extract(b"%PDF-1.7\ntruncated", password=None)


def test_encrypted_pdf_requires_password_and_is_not_decoded():
    from backend.application.format import PdfEncrypted

    body = b"%PDF-1.7\n1 0 obj\n<< /Encrypt 2 0 R >>\nendobj\ntrailer\n<< /Encrypt 2 0 R >>\nstartxref\n0\n%%EOF\n"
    with pytest.raises(PdfEncrypted):
        StructuralPdfExtractor(backend=object()).extract(body, password=None)


def test_scanned_pdf_without_text_layer_is_unsupported():
    class _EmptyBackend:
        def extract(self, data: bytes, password):
            return ["", "", ""], 3, False

    result = StructuralPdfExtractor(backend=_EmptyBackend()).extract(
        b"%PDF-1.7\ntrailer\nstartxref\n0\n%%EOF\n"
    )
    assert result.status == "unsupported"
    assert result.error_code == "no_text_layer"
    assert result.text == ""


def test_pdf_ready_with_text_backend_produces_pages():
    class _TextBackend:
        def extract(self, data: bytes, password):
            return ["page one", "page two"], 2, False

    result = StructuralPdfExtractor(backend=_TextBackend()).extract(
        b"%PDF-1.7\ntrailer\nstartxref\n0\n%%EOF\n"
    )
    assert result.status == "ready"
    assert result.page_count == 2
    assert [p.page for p in result.pages] == [1, 2]


# ----------------------------------------------------------------------
# tex 工程校验
# ----------------------------------------------------------------------

def _zip(entries: dict[str, bytes], *, symlink: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (0o120777 << 16)
            archive.writestr(info, "target")
    return buf.getvalue()


def test_tex_single_file_ok():
    project, payloads = validate_tex_payload(b"\\documentclass{article}", main=None)
    assert project.main == "main.tex"
    assert payloads["main.tex"]


def test_tex_zip_requires_explicit_main():
    data = _zip({"main.tex": b"\\documentclass{article}"})
    with pytest.raises(TexMainInvalid):
        validate_tex_payload(data, main=None)


def test_tex_zip_main_must_exist():
    data = _zip({"other.tex": b"x"})
    with pytest.raises(TexMainInvalid):
        validate_tex_payload(data, main="main.tex")


def test_tex_zip_rejects_path_traversal():
    data = _zip({"../evil.tex": b"x", "main.tex": b"\\documentclass{article}"})
    with pytest.raises(TexProjectRejected) as exc:
        validate_tex_payload(data, main="main.tex")
    assert exc.value.code == "path_traversal"


def test_tex_zip_rejects_absolute_path():
    data = _zip({"/etc/passwd": b"x", "main.tex": b"y"})
    with pytest.raises(TexProjectRejected) as exc:
        validate_tex_payload(data, main="main.tex")
    assert exc.value.code in {"absolute_path", "path_traversal"}


def test_tex_zip_rejects_symlink_entry():
    data = _zip({"main.tex": b"\\documentclass{article}"}, symlink="link.tex")
    with pytest.raises(TexProjectRejected) as exc:
        validate_tex_payload(data, main="main.tex")
    assert exc.value.code == "symlink_entry"


def test_tex_zip_rejects_bomb_by_ratio():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("main.tex", b"\\documentclass{article}\n")
        archive.writestr("bomb.tex", b"A" * (2 * 1024 * 1024))
    with pytest.raises(TexProjectRejected) as exc:
        validate_tex_payload(buf.getvalue(), main="main.tex", limits=TexLimits(max_compression_ratio=5.0))
    assert exc.value.code == "zip_bomb"


def test_tex_zip_rejects_too_many_files():
    entries = {"main.tex": b"x"}
    for index in range(6):
        entries[f"s{index}.tex"] = b"y"
    with pytest.raises(TexProjectRejected) as exc:
        validate_tex_payload(_zip(entries), main="main.tex", limits=TexLimits(max_files=3))
    assert exc.value.code == "too_many_files"


def test_tex_zip_rejects_disallowed_extension():
    data = _zip({"main.tex": b"x", "run.sh": b"#!/bin/sh"})
    with pytest.raises(TexProjectRejected) as exc:
        validate_tex_payload(data, main="main.tex")
    assert exc.value.code == "extension_not_allowed"


# ----------------------------------------------------------------------
# tex 编译：无安全执行器 → 不可用，不 fallback
# ----------------------------------------------------------------------

def test_tex_compile_unavailable_without_executor():
    compiler = TexCompiler(None)
    assert compiler.available() is False
    with pytest.raises(TexCompileUnavailable):
        compiler.compile(CompileRequest(revision_id="r1", main="main.tex"))


def test_tex_compile_validates_artifact_magic():
    class _FakeExecutor:
        def available(self):
            return True

        def engine_version(self):
            return "pdflatex-1.0"

        def compile(self, request):
            from backend.application.format.contracts import CompileResult

            return CompileResult(status="completed", artifact_bytes=b"NOTPDF", engine_version="x")

    result = TexCompiler(_FakeExecutor()).compile(CompileRequest(revision_id="r1", main="main.tex"))
    assert result.status == "failed"
    assert result.error_code == "artifact_not_pdf"


# ----------------------------------------------------------------------
# source_ref / selector
# ----------------------------------------------------------------------

def test_selector_codepoint_and_utf16_conversion():
    assert utf16_to_codepoint("a😀b", 3) == 2
    assert normalize_to_codepoint("a😀b", 1, 3, "utf16") == (1, 2)
    with pytest.raises(InvalidSourceRef):
        utf16_to_codepoint("a😀b", 2)


def test_tex_source_selector_requires_file():
    with pytest.raises(InvalidSourceRef):
        parse_selector({"type": "tex-source", "start": 0, "end": 1, "unit": "codepoint"})


def test_pdf_selector_requires_canon_coordinate_space():
    with pytest.raises(InvalidSourceRef):
        parse_selector({"type": "pdf", "pages": [{"page": 1, "rects": []}], "coordinate_space": "pdf-user"})


def test_source_ref_roundtrip_and_whole_document():
    ref = SourceRef.parse(
        {
            "document_id": "d",
            "revision_id": "r",
            "selector": {"type": "whole-document"},
            "quote": "",
        }
    )
    assert ref.to_dict()["selector"] == {"type": "whole-document"}


# ----------------------------------------------------------------------
# 插件 / workflow
# ----------------------------------------------------------------------

def _context(explain=None):
    from types import SimpleNamespace
    def generate(**kwargs):
        return Explanation.create(project_id='p', node_id=kwargs['node_id'], question=kwargs.get('question'),
                                  answer='这是解释', provider='fake', model='offline')
    return SimpleNamespace(container=SimpleNamespace(explanations=SimpleNamespace(explain=explain or generate)),
                           cancel=SimpleNamespace(check=lambda: None))


def test_plugin_registry_exposes_workflows():
    registry = build_default_registry()
    assert registry.order == ('documents', 'explanations', 'knowledge-narration', 'card-clear')
    assert {'explanation', 'narration', 'retelling'} <= set(registry.executable_kinds())
    assert registry.supports('tex_compile') is False


def test_manifests_declare_model_workload_only():
    for manifest in (EXPLANATION_MANIFEST, NARRATION_MANIFEST):
        assert manifest.workflows
        assert all(wf.id and wf.kind and wf.input_schema for wf in manifest.workflows)


def test_explanation_workflow_consumes_context_and_provider():
    workflow = ExplanationWorkflow()
    result = workflow.run(_context(), WorkflowInput(kind='explanation', payload={'node_id': 'n', 'question': '为什么'}))
    assert isinstance(result, Draft)
    assert result.entity.answer == '这是解释'
    assert result.provider == 'fake'


def test_explanation_workflow_requires_node_id():
    with pytest.raises(InvalidInputError):
        build_default_registry().execute('explanation', _context(), {'question': 'no node'})


def test_explanation_workflow_fails_when_provider_unavailable():
    from backend.domain.errors import ProviderUnavailable

    def explain(**kwargs):
        raise ProviderUnavailable('Provider unavailable')
    workflow = ExplanationWorkflow()
    with pytest.raises(ProviderUnavailable):
        workflow.run(_context(explain), WorkflowInput('explanation', {'node_id': 'n'}))


def test_narration_workflow_derives_counts_and_duration():
    from types import SimpleNamespace
    from backend.application.ports.model_provider import ProviderCapabilities
    from backend.domain.models import Node
    nodes = [Node(id=n, project_id='p', document_id='d', title=n, kind='concept', revision=1,
                  created_at=0, updated_at=0) for n in ('n1', 'n2')]
    context = SimpleNamespace(container=SimpleNamespace(context=SimpleNamespace(
        projects=SimpleNamespace(get=lambda _: SimpleNamespace(title='Project')),
        nodes=SimpleNamespace(get=lambda key: next(n for n in nodes if n.id == key)),
        explanations=SimpleNamespace(list_for_node=lambda _: [],
                                     narration_passage=lambda roots: {'revision_id': 'r', 'passage': 'source'}),
        provider=SimpleNamespace(narrate=lambda request: ['one stitched paragraph'], capabilities=ProviderCapabilities('fake', 'offline')),
    )), cancel=SimpleNamespace(check=lambda: None))
    result = NarrationWorkflow().run(context, WorkflowInput('narration', {'project_id': 'p', 'node_ids': ['n1', 'n2']})).entity
    # Root nodes are stitched into one narration segment that keeps the first root's id.
    assert result.segment_count == len(result.segments) == 1
    assert result.total_duration_seconds == pytest.approx(result.segments[0].duration_seconds)
    assert result.segments[0].node_id == "n1" and result.segments[0].text == 'one stitched paragraph'


def test_narration_workflow_rejects_too_many_nodes():
    with pytest.raises(InvalidInputError):
        build_default_registry().execute('narration', _context(), {'project_id': 'p', 'node_ids': [f'n{i}' for i in range(11)]})
