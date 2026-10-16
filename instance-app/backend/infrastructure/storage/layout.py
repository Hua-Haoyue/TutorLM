"""布局证据采集与初始化（把 IO 事实喂给 ``domain.storage_layout``）。

分工：**判定逻辑在 domain**（``evaluate_storage_layout`` 是纯函数），本模块只负责
"把本机事实采集出来"——顶层目录项、marker 原文、可写性、真实路径关系。
因此测试可以用假事实穷举反例，而无需构造真实文件系统。

就绪/分享的关键：空卷或新建卷**绝不**静默声明 ``content_only``。
"""

from __future__ import annotations

import os
import stat
from typing import Any, Iterable

from backend.domain.enums import StorageLayout
from backend.domain.paths import is_within
from backend.domain.storage_layout import (
    BUSINESS_DB_FILENAME,
    LAYOUT_MARKER_FILENAME,
    InventoryEntry,
    LayoutDecision,
    StorageProofFacts,
    build_marker,
    evaluate_storage_layout,
)
from backend.domain.volume import is_legacy_layout
from backend.infrastructure.storage.paths import realpath


def probe_writable(directory: str) -> tuple[bool, str | None]:
    """用**实际创建并删除一个临时文件**探测可写性。

    只查 ``os.access`` 不够：容器里 uid 10001 面对 root 属主的卷根时，
    ``access`` 与真实 ``open`` 可能给出不同结论（ACL/只读挂载）。
    """

    if not os.path.isdir(directory):
        return False, "目录不存在"
    candidate = os.path.join(directory, ".tutorlm-write-probe")
    try:
        with open(candidate, "wb") as handle:
            handle.write(b"probe")
    except OSError as exc:
        return False, f"{exc.__class__.__name__}: {exc.strerror or exc}"
    try:
        os.unlink(candidate)
    except OSError:  # pragma: no cover - 删不掉不影响"可写"结论
        pass
    return True, None


def inventory(data_root: str, *, max_entries: int = 512) -> tuple[InventoryEntry, ...]:
    """数据根**顶层**目录项（不递归；不跟随符号链接）。"""

    try:
        names = sorted(os.listdir(data_root))
    except OSError:
        return ()
    entries: list[InventoryEntry] = []
    for name in names[:max_entries]:
        path = os.path.join(data_root, name)
        try:
            info = os.lstat(path)
        except OSError:
            continue
        entries.append(
            InventoryEntry(
                name=name,
                is_dir=stat.S_ISDIR(info.st_mode),
                is_file=stat.S_ISREG(info.st_mode),
                is_symlink=stat.S_ISLNK(info.st_mode),
            )
        )
    return tuple(entries)


def read_marker(data_root: str) -> bytes | None:
    """读 marker 原文（不做解析：解析在 domain，且必须严格）。"""

    path = os.path.join(data_root, LAYOUT_MARKER_FILENAME)
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        return None


def collect_proof_facts(
    *,
    workspace_id: str,
    owner_id: str,
    subdirectory: str,
    data_dir: str,
    session_data_dir: str,
) -> StorageProofFacts:
    """采集判定 ``content_only`` 所需的全部本机事实。"""

    data_real = realpath(data_dir)
    session_real = realpath(session_data_dir)
    literal_data = os.path.abspath(data_dir)
    writable, _reason = probe_writable(data_real)
    business_db = os.path.join(data_real, BUSINESS_DB_FILENAME)
    return StorageProofFacts(
        workspace_id=workspace_id,
        owner_id=owner_id,
        subdirectory=subdirectory,
        data_root=data_real,
        session_dir=session_real,
        data_root_writable=writable,
        entries=inventory(data_real),
        marker_raw=read_marker(data_real),
        business_db_present=os.path.isfile(business_db),
        data_root_is_symlink=os.path.normpath(literal_data) != os.path.normpath(data_real),
    )


def evaluate(
    *,
    workspace_id: str,
    owner_id: str,
    subdirectory: str,
    data_dir: str,
    session_data_dir: str,
) -> LayoutDecision:
    """采集事实并交给 domain 判定（host 就绪与绑定校验都用这一条路径）。"""

    return evaluate_storage_layout(
        collect_proof_facts(
            workspace_id=workspace_id,
            owner_id=owner_id,
            subdirectory=subdirectory,
            data_dir=data_dir,
            session_data_dir=session_data_dir,
        )
    )


def ensure_content_layout(*, data_dir: str, legacy: bool) -> None:
    """创建内容布局目录（``sources/``、``artifacts/``）。

    新建 v3 子目录布局时会先补建父目录链（``users/<owner>/workspaces/<ws>``）：
    挂载点属 root 时这**会** ``EACCES``，应用不做 ``chown``、也不降级写到容器可写层——
    由外层 helper 交出属主（design §9.4）。这里只把失败讲清楚。
    """

    if legacy:
        os.makedirs(data_dir, exist_ok=True)
    else:
        os.makedirs(data_dir, exist_ok=True)
    for name in ("sources", "artifacts"):
        os.makedirs(os.path.join(data_dir, name), exist_ok=True)


def write_layout_marker(
    *,
    data_dir: str,
    workspace_id: str,
    owner_id: str,
    subdirectory: str,
    created_at: str,
) -> str:
    """原子写入 marker（先写同目录临时文件再 ``os.replace``）。

    用同目录临时文件而不是 ``/tmp``：``os.replace`` 只在同一文件系统内原子，
    而 ``/tmp`` 在容器里通常是另一个 tmpfs。
    """

    marker = build_marker(
        workspace_id=workspace_id,
        owner_id=owner_id,
        subdirectory=subdirectory,
        created_at=created_at,
    )
    target = os.path.join(realpath(data_dir), LAYOUT_MARKER_FILENAME)
    tmp = f"{target}.tmp-{os.getpid()}"
    with open(tmp, "wb") as handle:
        handle.write(marker.to_json_bytes())
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)
    return target


def prepare_workspace_layout(
    *,
    workspace_id: str,
    owner_id: str,
    subdirectory: str,
    data_dir: str,
    session_data_dir: str,
    created_at: str,
    allow_initialize: bool,
) -> LayoutDecision:
    """就绪前的一次性布局准备。

    - 已经是可声明的 ``content_only``：直接返回（幂等）；
    - ``legacy`` 布局：只做可写性探测，**不写 marker**（legacy 永远不可声明 content_only）；
    - 全新 v3 子目录且允许初始化：建内容目录 + 写 marker，然后**重新采集事实**判定；
    - 其它（marker 缺失但卷看起来已初始化、owner/workspace 不符等）：
      返回非可声明的判定，由调用方把就绪置为非 ``ok``。
    """

    decision = evaluate(
        workspace_id=workspace_id,
        owner_id=owner_id,
        subdirectory=subdirectory,
        data_dir=data_dir,
        session_data_dir=session_data_dir,
    )
    if decision.declarable or is_legacy_layout(subdirectory):
        return decision
    if not allow_initialize:
        return decision

    # 只有在"数据根里既没有 marker 也没有业务库"（真正空的新卷）时才初始化。
    facts = collect_proof_facts(
        workspace_id=workspace_id,
        owner_id=owner_id,
        subdirectory=subdirectory,
        data_dir=data_dir,
        session_data_dir=session_data_dir,
    )
    if facts.marker_raw is not None or facts.business_db_present:
        return decision
    if not facts.data_root_writable:
        return decision
    if not is_within(facts.session_dir, facts.data_root) and not is_within(
        facts.data_root, facts.session_dir
    ):
        pass
    ensure_content_layout(data_dir=data_dir, legacy=False)
    write_layout_marker(
        data_dir=data_dir,
        workspace_id=workspace_id,
        owner_id=owner_id,
        subdirectory=subdirectory,
        created_at=created_at,
    )
    return evaluate(
        workspace_id=workspace_id,
        owner_id=owner_id,
        subdirectory=subdirectory,
        data_dir=data_dir,
        session_data_dir=session_data_dir,
    )


def describe_inventory(entries: Iterable[InventoryEntry]) -> list[dict[str, Any]]:
    """把顶层目录项变成可日志/可返回的摘要（**不含**文件内容）。"""

    return [
        {"name": entry.name, "kind": "dir" if entry.is_dir else "file", "symlink": entry.is_symlink}
        for entry in entries
    ]


__all__ = [
    "collect_proof_facts",
    "describe_inventory",
    "ensure_content_layout",
    "evaluate",
    "inventory",
    "prepare_workspace_layout",
    "probe_writable",
    "read_marker",
    "write_layout_marker",
    "StorageLayout",
]
