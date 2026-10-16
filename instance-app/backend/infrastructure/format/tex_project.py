"""TeX 工程输入校验（安全解压）：**纯检查，不落盘、不执行用户代码**。

拒绝：绝对路径、``..``、symlink、hardlink、zip bomb、超限大小/数量/深度，
以及不在允许列表内的扩展名（防止夹带可执行/脚本）。**禁止**网络 fetch 与任意 shell。

产物是一个**逻辑视图**（``TexProject`` + ``TexResource``），落盘与不可变原件
由 storage 层负责；本模块只回答"这个输入是否可以安全接受"。
"""

from __future__ import annotations

import io
import posixpath
import zipfile

from backend.application.format.contracts import TexLimits, TexProject, TexResource
from backend.domain.hashing import content_hash
from backend.application.format.errors import TexMainInvalid, TexProjectRejected

_TEX_ENTRYPOINT_SUFFIXES = (".tex", ".ltx")
_MAX_ZIP_ENTRIES_SCANNED = 100_000  # 先在中央目录层面挡住超大条目数的炸弹


def normalize_logical_path(raw: str) -> str:
    """把 zip 内条目名归一化为 canonical 相对路径；越界/非法即拒绝。"""

    if raw is None:
        raise TexProjectRejected("zip_slip", "zip 条目名为空")
    name = raw.replace("\\", "/")
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        raise TexProjectRejected("absolute_path", f"zip 含绝对路径条目：{raw!r}")
    if "\x00" in name:
        raise TexProjectRejected("illegal_charset", "zip 条目名含 NUL")
    normalized = posixpath.normpath(name)
    if normalized.startswith("../") or normalized == ".." or "/../" in normalized:
        raise TexProjectRejected("path_traversal", f"zip 条目越界：{raw!r}")
    if normalized.startswith("/"):
        raise TexProjectRejected("path_traversal", f"zip 条目越界：{raw!r}")
    return normalized.lstrip("./") or "."


def validate_tex_payload(
    data: bytes,
    *,
    main: str | None,
    limits: TexLimits | None = None,
) -> tuple[TexProject, dict[str, bytes]]:
    """校验并"逻辑解压" tex 输入。

    单文件 ``.tex``：``main`` 缺省即用该文件；给出 ``main`` 必须与之一致。
    zip 工程：**必须显式给出 main** 且校验存在。

    返回 ``(TexProject, {logical_path: bytes})``——字节只存在于调用方内存，
    本函数不写盘。
    """

    spec = limits or TexLimits()
    if data.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return _validate_zip(data, main=main, limits=spec)
    return _validate_single(data, main=main, limits=spec)


def _validate_single(data: bytes, *, main: str | None, limits: TexLimits) -> tuple[TexProject, dict[str, bytes]]:
    if len(data) > limits.max_file_bytes:
        raise TexProjectRejected("file_too_large", "tex 单文件超过上限")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TexProjectRejected("tex_not_text", "tex 源必须为 UTF-8 文本") from exc
    # 单文件上传：入口固定为规范名 main.tex；给了 main 就必须与之等价。
    logical = "main.tex"
    if main and normalize_logical_path(main) not in (logical,):
        raise TexMainInvalid(
            "单文件 tex 的 main 必须为 main.tex（工程多文件请用 zip）",
            details={"main": main},
        )
    resource = TexResource(
        logical_path=logical, size_bytes=len(data), content_hash=_sha256_hex(data)
    )
    return TexProject(main=logical, resources=(resource,)), {logical: data}


def _validate_zip(data: bytes, *, main: str | None, limits: TexLimits) -> tuple[TexProject, dict[str, bytes]]:
    if main is None or main.strip() == "":
        raise TexMainInvalid("zip 工程必须显式指定 main 入口", details={"reason": "missing_main"})
    main_path = normalize_logical_path(main)
    if not main_path.endswith(_TEX_ENTRYPOINT_SUFFIXES):
        raise TexMainInvalid("main 必须是 .tex/.ltx", details={"main": main_path})

    payloads: dict[str, bytes] = {}
    resources: list[TexResource] = []
    total = 0
    seen_paths: set[str] = set()

    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise TexProjectRejected("bad_zip", "zip 无法读取") from exc

    with archive:
        infos = archive.infolist()
        if len(infos) > _MAX_ZIP_ENTRIES_SCANNED:
            raise TexProjectRejected("too_many_entries", "zip 条目数超过扫描上限")
        if len(infos) > limits.max_files:
            raise TexProjectRejected(
                "too_many_files", "zip 文件数超过上限", details={"count": len(infos), "limit": limits.max_files}
            )

        for info in infos:
            logical = normalize_logical_path(info.filename)
            if info.is_dir() or logical == ".":
                continue
            if _is_symlink_or_special(info):
                raise TexProjectRejected(
                    "symlink_entry", f"zip 含符号链接/特殊条目：{info.filename!r}",
                    details={"external_attr": info.external_attr},
                )
            if logical in seen_paths:
                raise TexProjectRejected("duplicate_entry", f"zip 含重复路径：{logical!r}")
            seen_paths.add(logical)

            depth = logical.count("/")
            if depth > limits.max_depth:
                raise TexProjectRejected(
                    "too_deep", f"zip 目录深度超过上限：{logical!r}", details={"depth": depth}
                )
            if info.file_size > limits.max_file_bytes:
                raise TexProjectRejected(
                    "file_too_large", f"zip 内文件超过上限：{logical!r}",
                    details={"size": info.file_size, "limit": limits.max_file_bytes},
                )
            ratio = info.file_size / max(info.compress_size, 1)
            if ratio > limits.max_compression_ratio:
                raise TexProjectRejected(
                    "zip_bomb", f"压缩比异常（疑似 zip bomb）：{logical!r}",
                    details={"ratio": round(ratio, 2), "limit": limits.max_compression_ratio},
                )
            suffix = posixpath.splitext(logical)[1].lower()
            if suffix and suffix not in limits.allowed_extensions:
                raise TexProjectRejected(
                    "extension_not_allowed", f"zip 含不允许的扩展名：{logical!r}",
                    details={"suffix": suffix},
                )

            total += info.file_size
            if total > limits.max_total_bytes:
                raise TexProjectRejected(
                    "project_too_large", "zip 解压后总量超过上限",
                    details={"total": total, "limit": limits.max_total_bytes},
                )

            content = _read_entry(archive, info, limits)
            payloads[logical] = content
            resources.append(
                TexResource(
                    logical_path=logical,
                    size_bytes=len(content),
                    content_hash=_sha256_hex(content),
                    is_binary=suffix not in (".tex", ".ltx", ".sty", ".cls", ".bib", ".bst", ".txt"),
                )
            )

    if main_path not in payloads:
        raise TexMainInvalid(
            "main 入口不在工程内（必须显式指定且存在）",
            details={"main": main_path, "paths": sorted(payloads)[:20]},
        )

    return TexProject(main=main_path, resources=tuple(resources)), payloads


def _read_entry(archive: zipfile.ZipFile, info: zipfile.ZipInfo, limits: TexLimits) -> bytes:
    """读取单个条目并**再次**校验实际大小（防止中央目录声明与实体不符）。"""

    try:
        with archive.open(info) as handle:
            content = handle.read(limits.max_file_bytes + 1)
    except (RuntimeError, zipfile.BadZipFile) as exc:
        raise TexProjectRejected("read_failed", f"zip 条目读取失败：{info.filename!r}") from exc
    if len(content) > limits.max_file_bytes:
        raise TexProjectRejected(
            "file_too_large", f"zip 条目实体超过上限：{info.filename!r}"
        )
    return content


def _is_symlink_or_special(info: zipfile.ZipInfo) -> bool:
    """UNIX 模式下按权限位识别 symlink/设备/管道等非常规文件。"""

    mode = (info.external_attr >> 16) & 0xFFFF
    if mode == 0:
        return False
    file_type = mode & 0o170000
    if file_type == 0o120000:  # symlink
        return True
    if file_type in (0o010000, 0o020000, 0o060000, 0o140000):  # fifo, chr, blk, socket
        return True
    return False


def _sha256_hex(payload: bytes) -> str:
    """内容摘要：委托 ``domain.hashing.content_hash``（裸十六进制）。"""

    return content_hash(payload)
