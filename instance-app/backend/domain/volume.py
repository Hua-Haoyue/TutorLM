"""数据根布局：``/data`` 根与 v3 子目录的区分（无 IO 的纯判定）。

v3（``contracts/instance-runtime-v3.md``）：

- 旧项目：``TUTORLM_WORKSPACE_SUBDIRECTORY == "."``，``TUTORLM_DATA_DIR == mount``。
- 新项目：``users/<owner_uuid>/workspaces/<workspace_uuid>``。

子目录只能由控制面用 UUID 推导；应用**不得**接受客户端提交的任意路径。
本模块只做形状校验，不创建目录、不做 chown。
"""

from __future__ import annotations

import re

from backend.domain.errors import ConfigInvalid, StorageLayoutError
from backend.domain.ids import canonical_uuid

LEGACY_SUBDIRECTORY = "."
_DATA_DIRECTORY_RE = re.compile(
    r"^users/(?P<user>[0-9a-fA-F-]{36})/workspaces/(?P<workspace>[0-9a-fA-F-]{36})$"
)


def canonical_data_directory(owner_id: str, workspace_id: str) -> str:
    """规范的 UUID 专属子目录字符串。"""

    return f"users/{canonical_uuid(owner_id)}/workspaces/{canonical_uuid(workspace_id)}"


def validate_workspace_subdirectory(
    value: str | None,
    *,
    owner_id: str | None = None,
    workspace_id: str | None = None,
) -> str:
    """校验 ``TUTORLM_WORKSPACE_SUBDIRECTORY`` 必须是 ``.`` 或规范 UUID 子目录。

    与控制面 ``validate_data_directory`` 的**取值域一致**（两侧各自实现）。
    非规范写法（大写 UUID、缺连字符、绝对路径、``..``）一律拒绝。
    """

    raw = (value or "").strip()
    if raw == LEGACY_SUBDIRECTORY:
        return LEGACY_SUBDIRECTORY
    match = _DATA_DIRECTORY_RE.match(raw)
    if match is None:
        raise ConfigInvalid(f"非法的 TUTORLM_WORKSPACE_SUBDIRECTORY：{raw!r}")
    try:
        canonical = canonical_data_directory(match.group("user"), match.group("workspace"))
    except ValueError:
        raise ConfigInvalid(f"非法的 TUTORLM_WORKSPACE_SUBDIRECTORY：{raw!r}") from None
    if raw != canonical:
        raise ConfigInvalid(f"TUTORLM_WORKSPACE_SUBDIRECTORY 必须是规范形式：{raw!r}")
    if owner_id is not None and match.group("user") != canonical_uuid(owner_id):
        raise ConfigInvalid("TUTORLM_WORKSPACE_SUBDIRECTORY 与 TUTORLM_OWNER_ID 不一致")
    if workspace_id is not None and match.group("workspace") != canonical_uuid(workspace_id):
        raise ConfigInvalid("TUTORLM_WORKSPACE_SUBDIRECTORY 与 TUTORLM_WORKSPACE_ID 不一致")
    return raw


def is_legacy_layout(subdirectory: str) -> bool:
    return (subdirectory or "").strip() == LEGACY_SUBDIRECTORY


def expected_data_dir(mount_path: str, subdirectory: str) -> str:
    """由挂载点与子目录推导应当一致的 ``TUTORLM_DATA_DIR``（纯字符串）。"""

    base = str(mount_path).replace("\\", "/").rstrip("/")
    if is_legacy_layout(subdirectory):
        return base
    return f"{base}/{subdirectory}"


def assert_data_dir_matches(mount_path: str, subdirectory: str, data_dir: str) -> str:
    """``TUTORLM_DATA_DIR`` 必须与挂载点 + 子目录推导结果一致。"""

    expected = expected_data_dir(mount_path, subdirectory)
    actual = str(data_dir).replace("\\", "/").rstrip("/")
    if actual != expected:
        raise StorageLayoutError(
            "TUTORLM_DATA_DIR 与挂载点 + TUTORLM_WORKSPACE_SUBDIRECTORY 推导结果不一致",
            details={"expected": expected, "actual": actual},
        )
    return actual
