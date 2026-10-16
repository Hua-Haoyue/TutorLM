"""``storage_layout`` 判定：``content_only`` 的**严格证明**与布局标记（marker）。

契约要求（``contracts/instance-runtime-v3.md`` 与设计 §4.4）：

- 只有**确认卷内无 session/凭证**时才声明 ``content_only``；
- 旧 ``legacy_root``（``TUTORLM_WORKSPACE_SUBDIRECTORY == "."``）与 v3 子目录必须**区分**；
- 无法证明隔离的项目一律按 legacy 处理（**不返回** ``storage_layout``），不无条件宣布 share safe；
- 已初始化（``storage_initialized=1``）的卷缺失时必须报错，**不得静默生成空卷冒充原项目**。

本模块是**纯函数**：所有事实（目录项、marker 内容、可写性）由
``infrastructure/storage`` 用 ``lstat``/``open`` 采集后传入。判定逻辑不碰 IO，
因此可以在单元测试里穷举反例。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from backend.domain.enums import StorageLayout
from backend.domain.errors import StorageNotInitialized
from backend.domain.volume import is_legacy_layout

#: 数据根内的布局标记文件：证明"是应用按 v3 初始化过的内容布局"，
#: 而不是"看起来恰好没有 session 目录"。
LAYOUT_MARKER_FILENAME = ".tutorlm-layout.json"
#: marker 结构版本；未来结构变化必须显式迁移，不做兼容猜测。
LAYOUT_MARKER_VERSION = 1

#: 明确等同于"会话/凭证"的目录名（大小写不敏感）。任何一个出现即不满足
#: content_only —— 卷复制是整树复制，卷内任何 session 都会变成接收者身份。
FORBIDDEN_CONTENT_NAMES: frozenset[str] = frozenset(
    {"session", "sessions", "auth", "credentials", "credential", "tokens", ".env"}
)

#: 这些名字属于**已知且允许**的内容布局；出现未知顶层项不影响判定
#: （内容目录由应用自己创建），但**符号链接**一律视为不可证明。
KNOWN_CONTENT_DIRS: frozenset[str] = frozenset({"sources", "artifacts"})
KNOWN_CONTENT_FILES: frozenset[str] = frozenset(
    {
        "business.sqlite3",
        "business.sqlite3-wal",
        "business.sqlite3-shm",
        LAYOUT_MARKER_FILENAME,
    }
)

#: 业务库主文件名（卷内唯一业务库）。
BUSINESS_DB_FILENAME = "business.sqlite3"


@dataclass(frozen=True, slots=True)
class InventoryEntry:
    """数据根顶层的一项（由存储层 ``lstat`` 得出，不递归）。"""

    name: str
    is_dir: bool
    is_file: bool
    is_symlink: bool = False

    @property
    def is_regular(self) -> bool:
        """非常规文件（FIFO/socket/device）与符号链接都不是 regular。"""

        return self.is_file and not self.is_symlink


@dataclass(frozen=True, slots=True)
class StorageProofFacts:
    """证明 ``content_only`` 所需的全部事实（全部来自本机，绝不来自客户端）。"""

    workspace_id: str
    owner_id: str
    #: ``TUTORLM_WORKSPACE_SUBDIRECTORY`` 原样值（``"."`` 表示 legacy）。
    subdirectory: str
    #: 真实路径（已 realpath）的数据根与卷外会话目录。
    data_root: str
    session_dir: str
    #: 数据根是否可写（由实际创建临时文件探测，见 storage 层）。
    data_root_writable: bool
    #: 数据根顶层目录项。
    entries: Sequence[InventoryEntry] = ()
    #: marker 原文（bytes，None 表示不存在）。
    marker_raw: bytes | None = None
    #: 业务库是否存在于数据根。
    business_db_present: bool = False
    #: 数据根是否为符号链接本身（真实路径与字面路径不一致即为真）。
    data_root_is_symlink: bool = False


@dataclass(frozen=True, slots=True)
class LayoutDecision:
    """判定结果：布局 + 是否可对外声明 + 可操作原因。"""

    layout: StorageLayout
    declarable: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)
    marker_workspace_id: str | None = None
    initialized: bool = False

    @property
    def readiness_error(self) -> StorageNotInitialized | None:
        """卷被标记已初始化却缺失/不一致时的阻塞错误（就绪必须非 ok）。"""

        if self.initialized and not self.declarable:
            return StorageNotInitialized(
                "卷内布局标记表明该数据根已初始化，但当前证据不满足 v3 content_only："
                "拒绝静默生成空卷冒充原项目",
                details={"reasons": list(self.reasons)},
            )
        return None


@dataclass(frozen=True, slots=True)
class LayoutMarker:
    """``.tutorlm-layout.json`` 的已解析内容。"""

    marker_version: int
    storage_layout: StorageLayout
    workspace_id: str
    owner_id: str
    subdirectory: str
    initialized: bool
    created_at: str

    def to_json_bytes(self) -> bytes:
        return (
            json.dumps(
                {
                    "marker_version": self.marker_version,
                    "storage_layout": str(self.storage_layout),
                    "workspace_id": self.workspace_id,
                    "owner_id": self.owner_id,
                    "subdirectory": self.subdirectory,
                    "initialized": self.initialized,
                    "created_at": self.created_at,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")


def build_marker(
    *,
    workspace_id: str,
    owner_id: str,
    subdirectory: str,
    created_at: str,
) -> LayoutMarker:
    """应用初始化时写入的 marker：显式声明 ``content_only`` + 已初始化。"""

    return LayoutMarker(
        marker_version=LAYOUT_MARKER_VERSION,
        storage_layout=StorageLayout.CONTENT_ONLY,
        workspace_id=workspace_id,
        owner_id=owner_id,
        subdirectory=subdirectory,
        initialized=True,
        created_at=created_at,
    )


def parse_marker(raw: bytes | None) -> tuple[LayoutMarker | None, str | None]:
    """严格解析 marker；返回 ``(marker, reason)``。

    任何缺字段/版本不符/取值非法都返回 ``(None, reason)``，**不**做默认值填充：
    marker 是安全证据，猜测它等于伪造证据。
    """

    if raw is None:
        return None, "缺少布局标记文件"
    if len(raw) > 64 * 1024:
        return None, "布局标记文件过大，拒绝解析"
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "布局标记文件不是合法 UTF-8 JSON"
    if not isinstance(decoded, Mapping):
        return None, "布局标记必须是 JSON 对象"
    version = decoded.get("marker_version")
    if version != LAYOUT_MARKER_VERSION:
        return None, f"布局标记版本不受支持：{version!r}"
    layout_raw = decoded.get("storage_layout")
    try:
        layout = StorageLayout(str(layout_raw))
    except ValueError:
        return None, f"布局标记的 storage_layout 非法：{layout_raw!r}"
    workspace_id = decoded.get("workspace_id")
    owner_id = decoded.get("owner_id")
    subdirectory = decoded.get("subdirectory")
    initialized = decoded.get("initialized")
    created_at = decoded.get("created_at")
    if not isinstance(workspace_id, str) or not workspace_id:
        return None, "布局标记缺少 workspace_id"
    if not isinstance(owner_id, str) or not owner_id:
        return None, "布局标记缺少 owner_id"
    if not isinstance(subdirectory, str) or not subdirectory:
        return None, "布局标记缺少 subdirectory"
    if not isinstance(initialized, bool):
        return None, "布局标记的 initialized 必须是布尔值"
    if not isinstance(created_at, str) or not created_at:
        return None, "布局标记缺少 created_at"
    return (
        LayoutMarker(
            marker_version=int(version),
            storage_layout=layout,
            workspace_id=workspace_id,
            owner_id=owner_id,
            subdirectory=subdirectory,
            initialized=initialized,
            created_at=created_at,
        ),
        None,
    )


def _is_outside(child: str, parent: str) -> bool:
    c = str(child).replace("\\", "/").rstrip("/")
    p = str(parent).replace("\\", "/").rstrip("/")
    return not (c == p or c.startswith(p + "/"))


def evaluate_storage_layout(facts: StorageProofFacts) -> LayoutDecision:
    """严格落实 v3 布局判定。

    声明 ``content_only`` 需要**同时**满足（任一不满足即按 legacy / 非 ok）：

    1. 非 legacy 子目录，且 ``TUTORLM_WORKSPACE_SUBDIRECTORY`` 与数据根一致；
    2. 数据根不是符号链接，且会话目录在数据根之外、两者互不包含；
    3. 数据根可写；
    4. 顶层不存在会话/凭证等价命名的目录，也不存在任何符号链接；
    5. 存在 marker，且 marker 声明 ``content_only`` + 已初始化 + 与当前
       ``workspace_id`` / ``owner_id`` / ``subdirectory`` 全部一致。
    """

    reasons: list[str] = []

    if is_legacy_layout(facts.subdirectory):
        reasons.append("legacy_root：TUTORLM_WORKSPACE_SUBDIRECTORY 为 '.'，无法证明卷内无会话")

    if facts.data_root_is_symlink:
        reasons.append("数据根本身是符号链接，拒绝据此证明布局")

    if not facts.data_root_writable:
        reasons.append("数据根不可写：就绪不得声明 ok")

    if not _is_outside(facts.session_dir, facts.data_root):
        reasons.append("会话目录位于数据根之内：session 会被卷复制带走")
    if not _is_outside(facts.data_root, facts.session_dir):
        reasons.append("数据根位于会话目录之内：布局不可证明")
    if not str(facts.data_root).strip() or not str(facts.session_dir).strip():
        reasons.append("数据根或会话目录为空")

    seen_lower: set[str] = set()
    for entry in facts.entries:
        name = str(entry.name)
        lowered = name.lower()
        if lowered in FORBIDDEN_CONTENT_NAMES:
            reasons.append(f"顶层存在会话/凭证等价目录：{name}")
        if entry.is_symlink:
            reasons.append(f"顶层存在符号链接，布局不可证明：{name}")
        elif not (entry.is_dir or entry.is_file):
            reasons.append(f"顶层存在非常规文件：{name}")
        if lowered in seen_lower:
            reasons.append(f"顶层存在仅大小写不同的重复项：{name}")
        seen_lower.add(lowered)

    marker, marker_reason = parse_marker(facts.marker_raw)
    marker_workspace_id: str | None = None
    initialized = False
    if marker is None:
        reasons.append(marker_reason or "缺少布局标记")
    else:
        marker_workspace_id = marker.workspace_id
        initialized = bool(marker.initialized)
        if marker.storage_layout is not StorageLayout.CONTENT_ONLY:
            reasons.append("布局标记未声明 content_only")
        if marker.workspace_id != facts.workspace_id:
            reasons.append("布局标记的 workspace_id 与当前注入不一致")
        if marker.owner_id != facts.owner_id:
            reasons.append("布局标记的 owner_id 与当前注入不一致")
        if marker.subdirectory != facts.subdirectory:
            reasons.append("布局标记的 subdirectory 与当前注入不一致")
        if not marker.initialized:
            reasons.append("布局标记未标记 initialized")

    # 独立判定：卷看起来已初始化（marker 声明 initialized 或业务库存在），
    # 但当前证据不足以声明 content_only -> 就绪必须非 ok，而不是当作空卷重来。
    looks_initialized = initialized or facts.business_db_present

    declarable = not reasons
    layout = StorageLayout.CONTENT_ONLY if declarable else StorageLayout.LEGACY_ROOT
    return LayoutDecision(
        layout=layout,
        declarable=declarable,
        reasons=tuple(reasons),
        marker_workspace_id=marker_workspace_id,
        initialized=looks_initialized and not declarable,
    )


def marker_is_missing_but_volume_initialized(facts: StorageProofFacts) -> bool:
    """marker 缺失但业务库存在：典型"已初始化卷内容被清空/挂错"的情形。"""

    if facts.marker_raw is not None:
        return False
    return facts.business_db_present
