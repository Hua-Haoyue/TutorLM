"""安全路径解析（纯函数，不做 IO；``os.path.realpath`` 只用于字符串归约时显式传入）。

规则（设计 §10）：

- 目录名/文件名由服务端生成的 id 决定，**不使用**用户文件名或路径。
- 拒绝绝对路径、``..`` 段、空段与 ``\\`` 分隔歧义。
- 解析后的路径必须仍在数据根之下；符号链接与非规格文件由存储层用 ``lstat`` 拒绝。
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from backend.domain.errors import PathOutsideRoot

#: 单个路径段允许的字符集合（canonical、可跨平台、无歧义）。
SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
#: 显式拒绝这些名字：它们要么与卷复制排除项冲突，要么是与会话/凭证等价的子树。
FORBIDDEN_SEGMENTS: frozenset[str] = frozenset(
    {".", "..", "sessions", "session", "auth", "credentials"}
)


def normalize_relative_path(raw: str) -> str:
    """把外部提供的相对路径规范化成 canonical 形式。

    接受 ``a/b/c.tex``，拒绝 ``/abs``、``C:/x``、``a/../../b``、``a//b``、
    ``a/./b``、反斜杠以及非法字符。返回 ``posix`` 风格、无前后斜杠的路径。
    """

    if raw is None:
        raise PathOutsideRoot("路径不能为空")
    text = str(raw).strip()
    if not text:
        raise PathOutsideRoot("路径不能为空")
    if "\\" in text:
        raise PathOutsideRoot("路径必须使用 '/' 分隔，且不允许反斜杠")
    if text.startswith("/"):
        raise PathOutsideRoot("不允许绝对路径")
    if re.match(r"^[A-Za-z]:", text):
        raise PathOutsideRoot("不允许盘符路径")
    parts: list[str] = []
    for segment in text.split("/"):
        if segment == "":
            raise PathOutsideRoot("路径不允许空段或重复分隔符")
        if segment in (".", ".."):
            raise PathOutsideRoot("路径不允许 '.' 或 '..' 段")
        if not SAFE_SEGMENT_RE.match(segment):
            raise PathOutsideRoot(f"路径段含非法字符：{segment!r}")
        parts.append(segment)
    return "/".join(parts)


def assert_safe_segment(segment: str, *, what: str = "路径段") -> str:
    """用于服务端自造的目录名（如 ``<document_id>``）——同样显式校验。"""

    if not isinstance(segment, str) or not SAFE_SEGMENT_RE.match(segment):
        raise PathOutsideRoot(f"{what}非法：{segment!r}")
    if segment in FORBIDDEN_SEGMENTS:
        raise PathOutsideRoot(f"{what}是保留名：{segment!r}")
    return segment


def is_within(root: str, candidate: str) -> bool:
    """``candidate`` 是否位于 ``root`` 之下（含等于 ``root``）。

    纯字符串归约，不解析 symlink：调用方必须先用 ``os.path.realpath``
    把两侧归约到真实路径，再调用本函数比较。
    """

    root_norm = _norm(root)
    cand_norm = _norm(candidate)
    if cand_norm == root_norm:
        return True
    return cand_norm.startswith(root_norm + "/")


def _norm(path: str) -> str:
    text = str(path).replace("\\", "/").rstrip("/")
    if text == "":
        return "/"
    return text


def relative_to_root(root: str, candidate: str) -> str:
    """返回 ``candidate`` 相对 ``root`` 的 posix 相对路径；越界抛错。"""

    root_norm = _norm(root)
    cand_norm = _norm(candidate)
    if cand_norm == root_norm:
        return ""
    if not cand_norm.startswith(root_norm + "/"):
        raise PathOutsideRoot("路径不在数据根之下")
    return cand_norm[len(root_norm) + 1 :]


def join_relative(root: str, *segments: str) -> str:
    """把已校验的相对段拼到 ``root`` 下，并再次断言仍在根内。"""

    normalized = [assert_safe_segment(seg) for seg in segments]
    candidate = str(PurePosixPath(_norm(root), *normalized))
    if not is_within(root, candidate):
        raise PathOutsideRoot("拼接后的路径越出数据根")
    return candidate


def sources_dir_name() -> str:
    return "sources"


def artifacts_dir_name() -> str:
    return "artifacts"
