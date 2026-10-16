"""Markdown 安全解析（白名单）：**不执行**脚本、**不**注入原始 HTML。

设计要求（设计 §10 / document-readers §3.1）：

- raw HTML 一律**转义为纯文本**（本实现选择"关闭 raw HTML 渲染"这一允许方案）；
- 危险链接 scheme（``javascript:`` / ``data:`` / ``vbscript:`` 等）被中和；
- 外链图片**不自动加载**（默认不产出可加载的 ``<img src>``）；
- 输出是**结构化块**（不只是字符串），供 Reader 做偏移/锚点，块自带码点偏移。

本模块纯函数、无 IO，不依赖第三方 Markdown 库。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import escape as _html_escape
from urllib.parse import urlsplit

_SAFE_LINK_SCHEMES = ("http", "https", "mailto")
_SAFE_IMAGE_SCHEMES = ("http", "https")  # 但仍默认不自动加载

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_FENCE = re.compile(r"^(\`\`\`|~~~)\s*([\w.+-]*)\s*$")
_LIST_ITEM = re.compile(r"^\s*([-*+]|\d+[.)])\s+(.*)$")


@dataclass(frozen=True, slots=True)
class MarkdownBlock:
    """解析后的一个块：``text`` 为规范化文本，``start``/``end`` 为码点偏移。"""

    kind: str
    text: str
    start: int
    end: int
    level: int = 0
    language: str | None = None
    html: str = ""


@dataclass(frozen=True, slots=True)
class MarkdownDocument:
    blocks: tuple[MarkdownBlock, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def text(self) -> str:
        return "\n\n".join(block.text for block in self.blocks)


def parse_markdown(source: str) -> MarkdownDocument:
    """解析 Markdown 为安全块序列；raw HTML 被当作纯文本。"""

    blocks: list[MarkdownBlock] = []
    warnings: list[str] = []
    lines = source.splitlines(keepends=True)

    offset = 0
    index = 0
    paragraph: list[str] = []
    para_start = 0

    def flush_paragraph() -> None:
        nonlocal paragraph
        if not paragraph:
            return
        raw = "".join(paragraph).strip("\n")
        block = _text_block("paragraph", raw, para_start)
        blocks.append(block)
        paragraph = []

    while index < len(lines):
        line = lines[index]
        line_start = offset
        offset += len(line)
        stripped = line.rstrip("\n")

        fence = _FENCE.match(stripped.strip())
        if fence:
            flush_paragraph()
            marker = fence.group(1)
            language = fence.group(2) or None
            body: list[str] = []
            body_offset = offset
            index += 1
            while index < len(lines):
                inner = lines[index]
                offset += len(inner)
                if inner.strip() in (marker, marker + " "):
                    index += 1
                    break
                body.append(inner)
                index += 1
            code = "".join(body)
            blocks.append(
                MarkdownBlock(
                    kind="code",
                    text=code,
                    start=body_offset,
                    end=body_offset + len(code),
                    language=language,
                    html=f"<pre><code>{_html_escape(code)}</code></pre>",
                )
            )
            continue

        heading = _HEADING.match(stripped)
        if heading:
            flush_paragraph()
            level = len(heading.group(1))
            text = heading.group(2).strip()
            blocks.append(
                MarkdownBlock(
                    kind="heading",
                    text=text,
                    start=line_start,
                    end=line_start + len(stripped),
                    level=level,
                    html=f"<h{level}>{_render_inline(text)[0]}</h{level}>",
                )
            )
            index += 1
            continue

        item = _LIST_ITEM.match(stripped)
        if item:
            flush_paragraph()
            text = item.group(2).strip()
            rendered, _ = _render_inline(text)
            blocks.append(
                MarkdownBlock(
                    kind="list-item",
                    text=text,
                    start=line_start,
                    end=line_start + len(stripped),
                    html=f"<li>{rendered}</li>",
                )
            )
            index += 1
            continue

        if stripped.strip() == "":
            flush_paragraph()
            index += 1
            continue

        if not paragraph:
            para_start = line_start
        paragraph.append(line)
        index += 1

    flush_paragraph()
    if "<" in source and ">" in source:
        warnings.append("raw_html_escaped")
    return MarkdownDocument(blocks=tuple(blocks), warnings=tuple(warnings))


def render_markdown(source: str) -> str:
    """把 Markdown 渲染为**安全** HTML 片段（无脚本、无 raw HTML）。"""

    document = parse_markdown(source)
    return "\n".join(block.html for block in document.blocks)


def _text_block(kind: str, text: str, start: int) -> MarkdownBlock:
    rendered, _ = _render_inline(text)
    return MarkdownBlock(
        kind=kind, text=text, start=start, end=start + len(text), html=f"<p>{rendered}</p>"
    )


def _render_inline(text: str) -> tuple[str, bool]:
    """渲染行内元素：先转义 HTML，再处理链接/图片；返回 (html, 是否含危险内容)。"""

    dangerous = False
    out: list[str] = []
    i = 0
    length = len(text)
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            out.append(_html_escape("".join(buffer)))
            buffer.clear()

    while i < length:
        ch = text[i]
        if ch == "`":
            end = text.find("`", i + 1)
            if end != -1:
                flush()
                out.append(f"<code>{_html_escape(text[i + 1:end])}</code>")
                i = end + 1
                continue
        if ch == "!" and i + 1 < length and text[i + 1] == "[":
            end = text.find("](", i + 2)
            if end != -1:
                close = text.find(")", end + 2)
                if close != -1:
                    flush()
                    alt = text[i + 2:end]
                    url = text[end + 2:close].strip()
                    # 图片**默认不自动加载**：只输出占位，不产出可加载 <img src>。
                    if _is_dangerous_url(url, _SAFE_IMAGE_SCHEMES):
                        dangerous = True
                    out.append(f'<span class="md-image" data-alt="{_html_escape(alt)}"></span>')
                    i = close + 1
                    continue
        if ch == "[":
            end = text.find("](", i + 1)
            if end != -1:
                close = text.find(")", end + 2)
                if close != -1:
                    flush()
                    label = text[i + 1:end]
                    url = text[end + 2:close].strip()
                    if _is_dangerous_url(url, _SAFE_LINK_SCHEMES):
                        dangerous = True
                        out.append(_html_escape(text[i:close + 1]))
                    else:
                        out.append(
                            f'<a href="{_html_escape(url, quote=True)}" rel="noopener noreferrer nofollow" '
                            f'target="_blank">{_html_escape(label)}</a>'
                        )
                    i = close + 1
                    continue
        if ch == "<":
            # raw HTML：转义为纯文本，绝不透传。
            close = text.find(">", i + 1)
            if close != -1:
                dangerous = True
        buffer.append(ch)
        i += 1

    flush()
    return "".join(out), dangerous


def _is_dangerous_url(url: str, allowed_schemes: tuple[str, ...]) -> bool:
    if not url:
        return True
    candidate = url.strip()
    if candidate.startswith("//"):
        return True
    scheme = urlsplit(candidate).scheme.lower()
    if scheme == "":
        # 无 scheme 视为相对路径，保守允许（同源资源由服务端另行校验）。
        return False
    return scheme not in allowed_schemes
