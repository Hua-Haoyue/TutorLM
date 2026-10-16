"""ID 生成与校验。

服务端生成目录名与文件名：**绝不使用用户提供的文件名或路径**（设计 §10）。
"""

from __future__ import annotations

import uuid


def new_id() -> str:
    """新 UUID4 的规范字符串形式。"""

    return str(uuid.uuid4())


def canonical_uuid(value: str | None) -> str:
    """规范化 UUID 字符串；非法输入抛 ``ValueError``（由调用方转成领域错误）。"""

    return str(uuid.UUID(str(value).strip()))


def is_uuid(value: str | None) -> bool:
    if not value:
        return False
    try:
        uuid.UUID(str(value).strip())
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def is_canonical_uuid(value: str | None) -> bool:
    """必须是规范形式（小写、带连字符），避免同一 UUID 出现两种写法。"""

    if not is_uuid(value):
        return False
    return canonical_uuid(value) == str(value).strip()


def revision_dir_name(revision_no: int) -> str:
    """原件目录名由服务端从 ``revision_no`` 生成，不接受外部字符串。"""

    if not isinstance(revision_no, int) or isinstance(revision_no, bool) or revision_no < 1:
        raise ValueError(f"revision_no 必须为正整数：{revision_no!r}")
    return f"r{revision_no:06d}"
