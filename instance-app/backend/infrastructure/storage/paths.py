"""真实路径解析与符号链接防护（唯一做 IO 的路径模块）。

安全要求（设计 §10）：

- 打开/读取文档时**拒绝符号链接与非常规文件**；
- 路径解析后必须仍在数据根或会话目录之下；
- 文件名/目录名由服务端生成的 id 决定，不使用用户文件名。
"""

from __future__ import annotations

import errno
import os
import stat

from backend.domain.errors import PathOutsideRoot, SymlinkRejected
from backend.domain.paths import assert_safe_segment, is_within, normalize_relative_path


def realpath(path: str | os.PathLike[str]) -> str:
    """``os.path.realpath`` 的显式封装（归约 symlink 后再比较包含关系）。"""

    return os.path.realpath(os.fspath(path))


def resolve_under_root(root: str, *segments: str) -> str:
    """把已校验的相对段解析到 ``root`` 下的真实路径并断言仍在根内。

    分两步：先按**字面**拼出候选路径拒绝越界，再 ``realpath`` 后重新断言。
    这样即使中途某段是符号链接指到根外，也会被第二步拦下。
    """

    root_real = realpath(root)
    candidate = os.path.join(root_real, *[assert_safe_segment(seg) for seg in segments])
    literal = os.path.normpath(candidate)
    if not is_within(root_real, literal):
        raise PathOutsideRoot("目标路径越出数据根", details={"path": literal})
    resolved = realpath(literal)
    if not is_within(root_real, resolved):
        raise SymlinkRejected(
            "路径经符号链接解析后越出数据根：拒绝",
            details={"path": literal, "resolved": resolved, "root": root_real},
        )
    return resolved


def resolve_relative_under_root(root: str, relative_path: str) -> str:
    """``relative_path`` 是 canonical posix 相对路径（如 tex 的 ``sections/intro.tex``）。"""

    normalized = normalize_relative_path(relative_path)
    return resolve_under_root(root, *normalized.split("/"))


def assert_regular_file(path: str, *, what: str = "文件") -> os.stat_result:
    """``lstat`` 断言：必须是常规文件，且不是符号链接。

    用 ``lstat`` 而不是 ``stat``：我们要判定的正是"这个目录项本身是不是链接"，
    先 ``stat`` 会把链接目标的类型当成目录项的类型。
    """

    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise PathOutsideRoot(f"{what}不存在", details={"path": path}) from None
    if stat.S_ISLNK(info.st_mode):
        raise SymlinkRejected(f"拒绝符号链接：{what}", details={"path": path})
    if not stat.S_ISREG(info.st_mode):
        raise SymlinkRejected(
            f"拒绝非常规文件（非普通文件）：{what}",
            details={"path": path, "mode": oct(info.st_mode)},
        )
    return info


def assert_directory(path: str, *, what: str = "目录") -> os.stat_result:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise PathOutsideRoot(f"{what}不存在", details={"path": path}) from None
    if stat.S_ISLNK(info.st_mode):
        raise SymlinkRejected(f"拒绝符号链接目录：{what}", details={"path": path})
    if not stat.S_ISDIR(info.st_mode):
        raise SymlinkRejected(
            f"期望目录但得到其它类型：{what}", details={"path": path, "mode": oct(info.st_mode)}
        )
    return info


def safe_open_read(path: str, *, what: str = "文件") -> bytes:
    """安全读取：拒绝符号链接/非常规文件，并做 ``O_NOFOLLOW`` 二次防护。"""

    assert_regular_file(path, what=what)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise SymlinkRejected(f"拒绝符号链接：{what}", details={"path": path}) from None
        raise
    try:
        with os.fdopen(fd, "rb") as handle:
            return handle.read()
    except OSError:  # pragma: no cover - 读取期错误按原样抛出
        raise
