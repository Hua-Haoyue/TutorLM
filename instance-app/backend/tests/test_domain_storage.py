"""focused 测试：domain 规则 + SQLite 存储 + 安全路径。

覆盖任务点：路径穿越、符号链接、owner 绑定（含 fail closed）、版本哈希
（不可变 revision）、事务边界、并发访问。不依赖 Docker、不联网、不用真实模型。
"""

from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from pathlib import Path

import pytest

from backend.application.ports.provider import GenerateRequest
from backend.domain.enums import (
    ArtifactKind,
    DocumentFormat,
    DocumentStatus,
    RevisionStatus,
    RunKind,
    RunState,
    RunWorkload,
    StorageLayout,
)
from backend.domain.errors import (
    ConfigInvalid,
    Conflict,
    ImmutableContentError,
    PathOutsideRoot,
    StorageNotInitialized,
    SymlinkRejected,
    WorkspaceBindingConflict,
    WorkspaceRebindForbidden,
)
from backend.domain.hashing import content_hash, etag_for_hash, if_none_match_matches, is_content_hash
from backend.domain.models import (
    Document,
    DocumentArtifact,
    DocumentRevision,
    Node,
    Project,
    Run,
)
from backend.domain.paths import normalize_relative_path
from backend.domain.run import (
    interrupt_in_flight,
    may_commit_result,
    resolve_idempotent_create,
)
from backend.domain.storage_layout import (
    InventoryEntry,
    StorageProofFacts,
    LAYOUT_MARKER_FILENAME,
    evaluate_storage_layout,
)
from backend.domain.text import (
    codepoint_offset_to_utf16,
    decode_utf8_strict,
    utf16_offset_to_codepoint,
)
from backend.domain.volume import validate_workspace_subdirectory
from backend.domain.workspace_binding import BindingAction, decide_workspace_binding
from backend.infrastructure.providers.fake import FakeProvider
from backend.infrastructure.settings import ModelSettings, Settings
from backend.infrastructure.sqlite.database import open_database
from backend.infrastructure.sqlite.migrations import migrate
from backend.infrastructure.sqlite.unit_of_work import SqliteUnitOfWorkFactory

NOW = 1_700_000_000.0


def _project(workspace_id: str, project_id: str | None = None) -> Project:
    return Project(
        id=project_id or str(uuid.uuid4()),
        workspace_id=workspace_id,
        title="Demo",
        created_at=NOW,
        updated_at=NOW,
    )


def _document(project_id: str, document_id: str | None = None) -> Document:
    return Document(
        id=document_id or str(uuid.uuid4()),
        project_id=project_id,
        title="Doc",
        format=DocumentFormat.MD,
        status=DocumentStatus.READY,
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.fixture()
def database(tmp_path: Path):
    db = open_database(str(tmp_path / "business.sqlite3"))
    migrate(db)
    try:
        yield db
    finally:
        db.close()


@pytest.fixture()
def uow_factory(database):
    return SqliteUnitOfWorkFactory(database)


# ===================================================================== WAL/迁移
def test_wal_is_enabled_and_schema_tables_exist(database) -> None:
    assert database.journal_mode() == "wal"
    tables = database.table_names()
    for expected in (
        "projects",
        "documents",
        "document_revisions",
        "document_resources",
        "nodes",
        "edges",
        "explanations",
        "narrations",
        "narration_segments",
        "runs",
        "run_events",
        "workspace_binding",
    ):
        assert expected in tables


def test_migration_is_idempotent(database) -> None:
    assert migrate(database) == 1
    assert migrate(database) == 1


def test_foreign_database_is_rejected_not_clobbered(tmp_path: Path) -> None:
    """host/local/store 那套临时 schema 不能被本 foundation 静默接管。"""

    legacy = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(str(legacy))
    conn.execute("CREATE TABLE workspace_binding (id INTEGER PRIMARY KEY, current_workspace_id TEXT)")
    conn.execute("CREATE TABLE runs (id TEXT PRIMARY KEY, state TEXT)")
    conn.commit()
    conn.close()

    db = open_database(str(legacy))
    try:
        with pytest.raises(StorageNotInitialized) as excinfo:
            migrate(db)
        assert "foreign_tables" in excinfo.value.details
    finally:
        db.close()


# ============================================================ 路径穿越 / symlink
@pytest.mark.parametrize(
    "value",
    ["../etc/passwd", "/etc/passwd", "a/../../b", "a//b", "a/./b", "..", "C:/x", "a\\b", "", "a:b"],
)
def test_normalize_relative_path_rejects_traversal(value: str) -> None:
    with pytest.raises(PathOutsideRoot):
        normalize_relative_path(value)


def test_normalize_relative_path_accepts_canonical(value: str = "sections/intro.tex") -> None:
    assert normalize_relative_path(value) == "sections/intro.tex"


def test_resolve_under_root_rejects_parent_segment() -> None:
    from backend.infrastructure.storage.paths import resolve_under_root

    with pytest.raises(PathOutsideRoot):
        resolve_under_root("/tmp", "..")
    with pytest.raises(PathOutsideRoot):
        resolve_under_root("/tmp", "a/b")


def test_symlink_file_is_rejected(tmp_path: Path) -> None:
    from backend.infrastructure.storage.local import LocalContentStore

    root = tmp_path / "data"
    (root / "sources" / "d1" / "r1").mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = root / "sources" / "d1" / "r1" / "main.md"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):  # pragma: no cover - Windows 无权限时跳过
        pytest.skip("当前环境不允许创建符号链接")

    store = LocalContentStore(str(root))
    with pytest.raises(SymlinkRejected):
        store.read_source(relative_path="sources/d1/r1/main.md")
    assert store.exists(relative_path="sources/d1/r1/main.md") is False


def test_symlink_escape_out_of_root_is_rejected(tmp_path: Path) -> None:
    from backend.infrastructure.storage.paths import resolve_relative_under_root

    root = tmp_path / "data"
    (root / "sources").mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    link = root / "sources" / "link"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover
        pytest.skip("当前环境不允许创建符号链接")

    with pytest.raises(SymlinkRejected):
        resolve_relative_under_root(str(root), "sources/link/evil.txt")


def test_immutable_revision_write_refuses_overwrite(tmp_path: Path) -> None:
    from backend.infrastructure.storage.local import LocalContentStore

    store = LocalContentStore(str(tmp_path / "data"))
    first = store.write_source(document_id="d1", revision_id="r1", filename="main.md", data=b"hello")
    assert first.content_hash == content_hash(b"hello")
    assert first.size_bytes == 5

    with pytest.raises(ImmutableContentError):
        store.write_source(document_id="d1", revision_id="r1", filename="main.md", data=b"changed")

    # 原件内容保持不变。
    assert store.read_source(relative_path=first.relative_path) == b"hello"


def test_artifact_metadata_path_is_canonical(tmp_path: Path) -> None:
    from backend.infrastructure.storage.local import LocalContentStore

    store = LocalContentStore(str(tmp_path / "data"))
    written = store.write_artifact(
        document_id="d1", revision_id="r1", kind="extracted_text", filename="extracted.txt", data=b"body"
    )
    assert written.relative_path == "artifacts/d1/r1/extracted_text/extracted.txt"
    assert store.read_source(relative_path=written.relative_path) == b"body"


# ============================================================ settings 严格校验
def test_persistent_mode_requires_all_bindings() -> None:
    with pytest.raises(ConfigInvalid) as excinfo:
        Settings.from_env(
            {
                "TUTORLM_RUN_MODE": "managed",
                "TUTORLM_PERSISTENT_WORKSPACE": "true",
                "TUTORLM_WORKSPACE_ID": "11111111-1111-4111-8111-111111111111",
            }
        )
    assert excinfo.value.details["missing"]


def test_persistent_mode_rejects_session_dir_inside_data_root() -> None:
    with pytest.raises(ConfigInvalid) as excinfo:
        Settings.from_env(
            {
                "TUTORLM_RUN_MODE": "managed",
                "TUTORLM_PERSISTENT_WORKSPACE": "true",
                "TUTORLM_WORKSPACE_ID": "11111111-1111-4111-8111-111111111111",
                "TUTORLM_OWNER_ID": "22222222-2222-4222-8222-222222222222",
                "TUTORLM_MOUNT_PATH": "/data",
                "TUTORLM_DATA_DIR": "/data/users/22222222-2222-4222-8222-222222222222/workspaces/11111111-1111-4111-8111-111111111111",
                "TUTORLM_WORKSPACE_SUBDIRECTORY": "users/22222222-2222-4222-8222-222222222222/workspaces/11111111-1111-4111-8111-111111111111",
                "TUTORLM_SESSION_DATA_DIR": "/data/users/22222222-2222-4222-8222-222222222222/workspaces/11111111-1111-4111-8111-111111111111/sessions",
            }
        )
    assert "TUTORLM_SESSION_DATA_DIR" in excinfo.value.message


def test_persistent_mode_accepts_canonical_v3_layout() -> None:
    settings = Settings.from_env(
        {
            "TUTORLM_RUN_MODE": "managed",
            "TUTORLM_PERSISTENT_WORKSPACE": "true",
            "TUTORLM_WORKSPACE_ID": "11111111-1111-4111-8111-111111111111",
            "TUTORLM_OWNER_ID": "22222222-2222-4222-8222-222222222222",
            "TUTORLM_MOUNT_PATH": "/data",
            "TUTORLM_DATA_DIR": "/data/users/22222222-2222-4222-8222-222222222222/workspaces/11111111-1111-4111-8111-111111111111",
            "TUTORLM_WORKSPACE_SUBDIRECTORY": "users/22222222-2222-4222-8222-222222222222/workspaces/11111111-1111-4111-8111-111111111111",
            "TUTORLM_SESSION_DATA_DIR": "/tmp/tutorlm-session/s01",
        }
    )
    assert settings.persistent_workspace is True
    assert settings.is_legacy_layout is False
    assert settings.declared_layout_hint() is StorageLayout.CONTENT_ONLY


@pytest.mark.parametrize(
    "provider_env",
    [
        {"TUTORLM_PROVIDER": "openai_compatible"},
        {"TUTORLM_PROVIDER": "does-not-exist"},
        {"TUTORLM_RUN_MODE": "not-a-mode"},
        {"TUTORLM_PERSISTENT_WORKSPACE": "maybe"},
    ],
)
def test_invalid_config_values_fail_loudly(provider_env: dict[str, str]) -> None:
    with pytest.raises(ConfigInvalid):
        Settings.from_env(provider_env)


def test_api_key_is_never_in_describe() -> None:
    settings = Settings.from_env(
        {
            "TUTORLM_RUN_MODE": "managed",
            "TUTORLM_PERSISTENT_WORKSPACE": "true",
            "TUTORLM_WORKSPACE_ID": "11111111-1111-4111-8111-111111111111",
            "TUTORLM_OWNER_ID": "22222222-2222-4222-8222-222222222222",
            "TUTORLM_MOUNT_PATH": "/data",
            "TUTORLM_DATA_DIR": "/data/users/22222222-2222-4222-8222-222222222222/workspaces/11111111-1111-4111-8111-111111111111",
            "TUTORLM_WORKSPACE_SUBDIRECTORY": "users/22222222-2222-4222-8222-222222222222/workspaces/11111111-1111-4111-8111-111111111111",
            "TUTORLM_SESSION_DATA_DIR": "/tmp/tutorlm-session/s01",
            "TUTORLM_PROVIDER": "openai_compatible",
            "TUTORLM_MODEL_API_BASE": "https://example.invalid/v1",
            "TUTORLM_MODEL_API_KEY": "sk-super-secret",
            "TUTORLM_MODEL_NAME": "demo",
        }
    )
    assert "sk-super-secret" not in repr(settings.describe())
    assert settings.describe()["model"]["api_key_configured"] is True


@pytest.mark.parametrize(
    "value,expected",
    [
        (".", "."),
        (
            "users/22222222-2222-4222-8222-222222222222/workspaces/11111111-1111-4111-8111-111111111111",
            "users/22222222-2222-4222-8222-222222222222/workspaces/11111111-1111-4111-8111-111111111111",
        ),
    ],
)
def test_validate_workspace_subdirectory_accepts_canonical(value: str, expected: str) -> None:
    assert validate_workspace_subdirectory(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "users/22222222-2222-4222-8222-222222222222/workspaces/not-a-uuid",
        "/data/users/x",
        "../users/x",
        "users/22222222-2222-4222-8222-222222222222",  # 缺后半段
        "Users/22222222-2222-4222-8222-222222222222/workspaces/11111111-1111-4111-8111-111111111111",
    ],
)
def test_validate_workspace_subdirectory_rejects_non_canonical(value: str) -> None:
    with pytest.raises(ConfigInvalid):
        validate_workspace_subdirectory(value)


# ================================================== content_only 严格证明 + marker
def _facts(**overrides) -> StorageProofFacts:
    base = dict(
        workspace_id="11111111-1111-4111-8111-111111111111",
        owner_id="22222222-2222-4222-8222-222222222222",
        subdirectory="users/22222222-2222-4222-8222-222222222222/workspaces/11111111-1111-4111-8111-111111111111",
        data_root="/data/users/o/workspaces/w",
        session_dir="/tmp/tutorlm-session/s01",
        data_root_writable=True,
        entries=(InventoryEntry(name="sources", is_dir=True, is_file=False),),
        marker_raw=None,
        business_db_present=False,
        data_root_is_symlink=False,
    )
    base.update(overrides)
    return StorageProofFacts(**base)


def _marker_bytes(**overrides) -> bytes:
    from backend.domain.storage_layout import build_marker

    payload = dict(
        workspace_id="11111111-1111-4111-8111-111111111111",
        owner_id="22222222-2222-4222-8222-222222222222",
        subdirectory="users/22222222-2222-4222-8222-222222222222/workspaces/11111111-1111-4111-8111-111111111111",
        created_at="2026-01-01T00:00:00Z",
    )
    payload.update(overrides)
    return build_marker(**payload).to_json_bytes()


def test_empty_new_volume_never_declares_content_only() -> None:
    """空卷/新建卷：marker 缺失时**绝不**声明 content_only。"""

    decision = evaluate_storage_layout(_facts())
    assert decision.declarable is False
    assert decision.layout is StorageLayout.LEGACY_ROOT
    assert any("布局标记" in reason for reason in decision.reasons)


def test_content_only_requires_marker_and_clean_inventory() -> None:
    decision = evaluate_storage_layout(_facts(marker_raw=_marker_bytes()))
    assert decision.declarable is True
    assert decision.layout is StorageLayout.CONTENT_ONLY


@pytest.mark.parametrize("name", ["sessions", "session", "auth", "credentials", ".env"])
def test_session_like_directory_blocks_content_only(name: str) -> None:
    facts = _facts(
        marker_raw=_marker_bytes(),
        entries=(
            InventoryEntry(name="sources", is_dir=True, is_file=False),
            InventoryEntry(name=name, is_dir=True, is_file=False),
        ),
    )
    decision = evaluate_storage_layout(facts)
    assert decision.declarable is False
    assert any(name in reason for reason in decision.reasons)


def test_symlink_entry_blocks_content_only() -> None:
    facts = _facts(
        marker_raw=_marker_bytes(),
        entries=(
            InventoryEntry(name="sources", is_dir=True, is_file=False),
            InventoryEntry(name="shim", is_dir=False, is_file=False, is_symlink=True),
        ),
    )
    decision = evaluate_storage_layout(facts)
    assert decision.declarable is False
    assert any("符号链接" in reason for reason in decision.reasons)


def test_legacy_layout_is_never_declarable() -> None:
    decision = evaluate_storage_layout(
        _facts(subdirectory=".", marker_raw=_marker_bytes(subdirectory="."))
    )
    assert decision.declarable is False
    assert any("legacy_root" in reason for reason in decision.reasons)


def test_marker_mismatch_on_workspace_blocks_content_only() -> None:
    decision = evaluate_storage_layout(
        _facts(marker_raw=_marker_bytes(workspace_id="99999999-9999-4999-8999-999999999999"))
    )
    assert decision.declarable is False
    assert any("workspace_id" in reason for reason in decision.reasons)


def test_initialized_volume_without_marker_raises_storage_not_initialized() -> None:
    """已初始化的卷缺 marker：就绪必须非 ok，不得静默当空卷。"""

    decision = evaluate_storage_layout(_facts(business_db_present=True))
    error = decision.readiness_error
    assert error is not None
    assert isinstance(error, StorageNotInitialized)


def test_unwritable_data_root_blocks_readiness() -> None:
    decision = evaluate_storage_layout(_facts(marker_raw=_marker_bytes(), data_root_writable=False))
    assert decision.declarable is False
    assert any("可写" in reason for reason in decision.reasons)


def test_prepare_workspace_layout_initializes_only_clean_empty_volume(
    tmp_path: Path, workspace_id: str, owner_id: str
) -> None:
    from backend.infrastructure.storage.layout import prepare_workspace_layout

    settings = Settings.for_persistent_tests(tmp_path, workspace_id=workspace_id, owner_id=owner_id)
    Path(settings.data_dir).mkdir(parents=True, exist_ok=True)

    decision = prepare_workspace_layout(
        workspace_id=workspace_id,
        owner_id=owner_id,
        subdirectory=settings.workspace_subdirectory,
        data_dir=settings.data_dir,
        session_data_dir=settings.session_data_dir or str(tmp_path / "session"),
        created_at="2026-01-01T00:00:00Z",
        allow_initialize=True,
    )
    assert decision.declarable is True
    assert Path(settings.data_dir, LAYOUT_MARKER_FILENAME).is_file()

    # 第二次调用幂等：不会重写/破坏既有 marker。
    again = prepare_workspace_layout(
        workspace_id=workspace_id,
        owner_id=owner_id,
        subdirectory=settings.workspace_subdirectory,
        data_dir=settings.data_dir,
        session_data_dir=settings.session_data_dir or str(tmp_path / "session"),
        created_at="2026-01-02T00:00:00Z",
        allow_initialize=True,
    )
    assert again.declarable is True


def test_prepare_workspace_layout_does_not_initialize_when_volume_looks_used(
    tmp_path: Path, workspace_id: str, owner_id: str
) -> None:
    from backend.infrastructure.storage.layout import prepare_workspace_layout

    settings = Settings.for_persistent_tests(tmp_path, workspace_id=workspace_id, owner_id=owner_id)
    data = Path(settings.data_dir)
    data.mkdir(parents=True, exist_ok=True)
    (data / "business.sqlite3").write_bytes(b"SQLite format 3\0")

    decision = prepare_workspace_layout(
        workspace_id=workspace_id,
        owner_id=owner_id,
        subdirectory=settings.workspace_subdirectory,
        data_dir=settings.data_dir,
        session_data_dir=settings.session_data_dir or str(tmp_path / "session"),
        created_at="2026-01-01T00:00:00Z",
        allow_initialize=True,
    )
    assert decision.declarable is False
    assert decision.readiness_error is not None
    assert not Path(data, LAYOUT_MARKER_FILENAME).exists()


# ============================================================ ownership 绑定
WS = "11111111-1111-4111-8111-111111111111"
OWNER = "22222222-2222-4222-8222-222222222222"
OTHER_WS = "33333333-3333-4333-8333-333333333333"
OTHER_OWNER = "44444444-4444-4444-8444-444444444444"
SOURCE_WS = "55555555-5555-4555-8555-555555555555"
COPY_ID = "66666666-6666-4666-8666-666666666666"


def test_binding_created_when_absent() -> None:
    decision = decide_workspace_binding(
        None, workspace_id=WS, owner_id=OWNER, source_workspace_id=None, copy_id=None, now=NOW
    )
    assert decision.action is BindingAction.CREATED
    assert decision.binding.current_workspace_id == WS
    assert decision.binding.origin_workspace_id == WS


def test_binding_idempotent_when_owner_matches() -> None:
    from backend.domain.workspace_binding import WorkspaceBinding

    existing = WorkspaceBinding(current_workspace_id=WS, origin_workspace_id=WS, owner_id=OWNER, updated_at=NOW)
    decision = decide_workspace_binding(
        existing, workspace_id=WS, owner_id=OWNER, source_workspace_id=None, copy_id=None, now=NOW
    )
    assert decision.action is BindingAction.UNCHANGED


def test_binding_owner_conflict_fails_closed() -> None:
    from backend.domain.workspace_binding import WorkspaceBinding

    existing = WorkspaceBinding(current_workspace_id=WS, origin_workspace_id=WS, owner_id=OWNER, updated_at=NOW)
    with pytest.raises(WorkspaceBindingConflict):
        decide_workspace_binding(
            existing,
            workspace_id=WS,
            owner_id=OTHER_OWNER,
            source_workspace_id=None,
            copy_id=None,
            now=NOW,
        )


def test_suspected_clone_without_trusted_identifiers_fails_closed() -> None:
    """缺 TUTORLM_WORKSPACE_SOURCE_ID：fail closed，不盲写 owner。"""

    from backend.domain.workspace_binding import WorkspaceBinding

    existing = WorkspaceBinding(
        current_workspace_id=SOURCE_WS, origin_workspace_id=SOURCE_WS, owner_id=OTHER_OWNER, updated_at=NOW
    )
    with pytest.raises(WorkspaceRebindForbidden):
        decide_workspace_binding(
            existing, workspace_id=WS, owner_id=OWNER, source_workspace_id=None, copy_id=None, now=NOW
        )


def test_clone_rebinds_only_with_matching_trusted_identifiers() -> None:
    from backend.domain.workspace_binding import WorkspaceBinding

    existing = WorkspaceBinding(
        current_workspace_id=SOURCE_WS, origin_workspace_id=SOURCE_WS, owner_id=OTHER_OWNER, updated_at=NOW
    )
    decision = decide_workspace_binding(
        existing,
        workspace_id=WS,
        owner_id=OWNER,
        source_workspace_id=SOURCE_WS,
        copy_id=COPY_ID,
        now=NOW,
    )
    assert decision.action is BindingAction.REBOUND
    assert decision.binding.current_workspace_id == WS
    assert decision.binding.owner_id == OWNER
    # 业务内容身份（origin）保持不变，只有访问上下文被重绑。
    assert decision.binding.origin_workspace_id == SOURCE_WS


def test_clone_with_mismatched_source_id_fails_closed() -> None:
    from backend.domain.workspace_binding import WorkspaceBinding

    existing = WorkspaceBinding(
        current_workspace_id=SOURCE_WS, origin_workspace_id=SOURCE_WS, owner_id=OTHER_OWNER, updated_at=NOW
    )
    with pytest.raises(WorkspaceRebindForbidden):
        decide_workspace_binding(
            existing,
            workspace_id=WS,
            owner_id=OWNER,
            source_workspace_id=OTHER_WS,
            copy_id=None,
            now=NOW,
        )


def test_binding_repository_roundtrip_and_single_row(uow_factory) -> None:
    from backend.domain.workspace_binding import WorkspaceBinding

    binding = WorkspaceBinding(
        current_workspace_id=WS,
        origin_workspace_id=WS,
        owner_id=OWNER,
        updated_at=NOW,
        storage_layout=StorageLayout.CONTENT_ONLY,
    )
    with uow_factory() as uow:
        uow.repos.binding.save(binding)
        uow.commit()

    with uow_factory() as uow:
        loaded = uow.repos.binding.get()
        uow.commit()
    assert loaded is not None
    assert loaded.current_workspace_id == WS
    assert loaded.storage_layout is StorageLayout.CONTENT_ONLY

    # 第二次保存覆盖同一行，而不是插入第二行。
    rebinding = WorkspaceBinding(
        current_workspace_id="77777777-7777-4777-8777-777777777777",
        origin_workspace_id=WS,
        owner_id=OWNER,
        updated_at=NOW + 1,
        copy_id=COPY_ID,
    )
    with uow_factory() as uow:
        uow.repos.binding.rebind(rebinding, event_payload={"old": WS, "new": rebinding.current_workspace_id})
        uow.commit()

    with uow_factory() as uow:
        rows = uow.connection.execute("SELECT COUNT(*) AS n FROM workspace_binding").fetchone()["n"]
        events = uow.connection.execute(
            "SELECT type, payload_json FROM workspace_events ORDER BY id"
        ).fetchall()
        uow.commit()
    assert rows == 1
    assert any(row["type"] == "workspace_rebound" for row in events)


# ============================================================ 版本哈希 / 事务
def test_content_hash_and_etag_helpers() -> None:
    digest = content_hash(b"hello")
    assert is_content_hash(digest)
    assert not is_content_hash("ABC")
    etag = etag_for_hash(digest)
    assert if_none_match_matches(etag, etag) is True
    assert if_none_match_matches("*", etag) is True
    assert if_none_match_matches('"other"', etag) is False
    assert if_none_match_matches(None, etag) is False


def test_revision_rejects_bad_hash_and_revision_no() -> None:
    from backend.domain.errors import DomainError

    with pytest.raises(DomainError):
        DocumentRevision(
            id="r1",
            document_id="d1",
            revision_no=1,
            content_hash="not-a-hash",
            size_bytes=1,
            status=RevisionStatus.READY,
            created_at=NOW,
        )
    with pytest.raises(DomainError):
        DocumentRevision(
            id="r1",
            document_id="d1",
            revision_no=0,
            content_hash=content_hash(b""),
            size_bytes=0,
            status=RevisionStatus.READY,
            created_at=NOW,
        )


def test_revision_no_is_monotonic_and_contents_are_immutable(uow_factory) -> None:
    project = _project(WS)
    document = _document(project.id)
    with uow_factory() as uow:
        uow.repos.projects.save(project)
        uow.repos.documents.add(document)
        uow.commit()

    # 同一内容重复添加仍得到新 revision_no（每次上传 = 新不可变原件）。
    for payload in (b"v1", b"v1", b"v2"):
        with uow_factory() as uow:
            number = uow.repos.revisions.next_revision_no(document.id)
            revision = DocumentRevision(
                id=str(uuid.uuid4()),
                document_id=document.id,
                revision_no=number,
                content_hash=content_hash(payload),
                size_bytes=len(payload),
                status=RevisionStatus.READY,
                created_at=NOW,
            )
            uow.repos.revisions.add(revision)
            uow.repos.documents.set_current_revision(
                document.id, revision.id, size_bytes=revision.size_bytes, updated_at=NOW
            )
            uow.commit()

    with uow_factory() as uow:
        revisions = uow.repos.revisions.list_for_document(document.id)
        current = uow.repos.documents.get(document.id)
        uow.commit()
    assert [item.revision_no for item in revisions] == [1, 2, 3]
    assert current is not None and current.current_revision_id == revisions[-1].id
    # 旧 revision 仍可查（不被覆盖）。
    assert revisions[0].content_hash == content_hash(b"v1")


def test_rollback_discards_writes(uow_factory) -> None:
    project = _project(WS)
    with uow_factory() as uow:
        uow.repos.projects.save(project)
        # 不 commit -> 退出即回滚。
    with uow_factory() as uow:
        assert uow.repos.projects.get(project.id) is None
        uow.commit()


def test_exception_rolls_back_and_leaves_no_partial_row(uow_factory) -> None:
    project = _project(WS)
    document = _document(project.id)
    with pytest.raises(RuntimeError):
        with uow_factory() as uow:
            uow.repos.projects.save(project)
            uow.repos.documents.add(document)
            raise RuntimeError("boom")
    with uow_factory() as uow:
        assert uow.repos.projects.get(project.id) is None
        assert uow.repos.documents.get(document.id) is None
        uow.commit()


def test_commit_is_explicit_and_single_use(uow_factory) -> None:
    project = _project(WS)
    with uow_factory() as uow:
        uow.repos.projects.save(project)
        uow.commit()
        with pytest.raises(Exception):
            uow.commit()
    with uow_factory() as uow:
        assert uow.repos.projects.get(project.id) is not None
        uow.commit()


def test_project_row_is_unique_per_workspace(uow_factory) -> None:
    first = _project(WS)
    second = _project(WS)  # 同 workspace 的第二个 project 对象
    with uow_factory() as uow:
        uow.repos.projects.save(first)
        uow.repos.projects.save(second)
        uow.commit()
    with uow_factory() as uow:
        count = uow.connection.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"]
        loaded = uow.repos.projects.get_for_workspace(WS)
        uow.commit()
    assert count == 1
    assert loaded is not None and loaded.id == first.id


def test_delete_node_promotes_children_instead_of_cascading(uow_factory) -> None:
    project = _project(WS)
    document = _document(project.id)
    parent = Node(
        id=str(uuid.uuid4()),
        project_id=project.id,
        document_id=document.id,
        title="parent",
        kind="section",
        revision=1,
        created_at=NOW,
        updated_at=NOW,
    )
    child = Node(
        id=str(uuid.uuid4()),
        project_id=project.id,
        document_id=document.id,
        title="child",
        kind="concept",
        revision=1,
        created_at=NOW,
        updated_at=NOW,
        parent_id=parent.id,
    )
    with uow_factory() as uow:
        uow.repos.projects.save(project)
        uow.repos.documents.add(document)
        uow.repos.nodes.add(parent)
        uow.repos.nodes.add(child)
        uow.commit()

    with uow_factory() as uow:
        promoted = uow.repos.nodes.delete(parent.id)
        uow.commit()
    assert promoted == [child.id]

    with uow_factory() as uow:
        surviving = uow.repos.nodes.get(child.id)
        uow.commit()
    assert surviving is not None
    assert surviving.parent_id is None  # 提升为顶层，未被级联删除


# ============================================================ run 状态机 / 幂等
def test_run_state_machine_transitions() -> None:
    from backend.domain.enums import can_transition_run

    assert can_transition_run(RunState.QUEUED, RunState.RUNNING) is True
    assert can_transition_run(RunState.RUNNING, RunState.COMPLETED) is True
    assert can_transition_run(RunState.RUNNING, RunState.CANCELLED) is True
    # 同状态幂等；终态不可再迁移。
    assert can_transition_run(RunState.RUNNING, RunState.RUNNING) is True
    for terminal in (RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED, RunState.INTERRUPTED):
        assert can_transition_run(terminal, RunState.RUNNING) is False
    # 不允许跳过 running 直接完成。
    assert can_transition_run(RunState.QUEUED, RunState.COMPLETED) is False


def test_cancelled_run_must_not_commit_result() -> None:
    """取消后不 commit：只有仍在 running 的 run 才允许写入结果。"""

    assert may_commit_result(RunState.RUNNING) is True
    for state in (
        RunState.QUEUED,
        RunState.COMPLETED,
        RunState.FAILED,
        RunState.CANCELLED,
        RunState.INTERRUPTED,
    ):
        assert may_commit_result(state) is False


def test_interrupt_in_flight_is_idempotent_and_never_replays() -> None:
    assert interrupt_in_flight(RunState.RUNNING) is RunState.INTERRUPTED
    assert interrupt_in_flight(RunState.QUEUED) is RunState.INTERRUPTED
    # 终态保持不变（幂等），不会被"重启收尾"改写。
    for terminal in (RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED, RunState.INTERRUPTED):
        assert interrupt_in_flight(terminal) is terminal


def test_idempotency_same_key_same_input_reuses_run() -> None:
    from backend.domain.run import canonical_input_fingerprint

    fingerprint = canonical_input_fingerprint({"node_id": "n1", "question": "why"})
    reused = resolve_idempotent_create(
        existing_state=RunState.COMPLETED,
        existing_fingerprint=fingerprint,
        requested_fingerprint=canonical_input_fingerprint({"question": "why", "node_id": "n1"}),
        existing_run_id="run-1",
    )
    assert reused == "run-1"


def test_idempotency_same_key_different_input_conflicts() -> None:
    from backend.domain.run import canonical_input_fingerprint

    with pytest.raises(Conflict):
        resolve_idempotent_create(
            existing_state=RunState.RUNNING,
            existing_fingerprint=canonical_input_fingerprint({"node_id": "n1"}),
            requested_fingerprint=canonical_input_fingerprint({"node_id": "n2"}),
            existing_run_id="run-1",
        )


def test_idempotency_without_existing_run_creates() -> None:
    assert (
        resolve_idempotent_create(
            existing_state=None, existing_fingerprint=None, requested_fingerprint="x", existing_run_id=None
        )
        is None
    )


def _run(project_id: str, *, key: str | None = None, scope: str | None = None) -> Run:
    return Run(
        id=str(uuid.uuid4()),
        project_id=project_id,
        kind=RunKind.EXPLANATION,
        state=RunState.QUEUED,
        workload=RunWorkload.MODEL,
        input={"node_id": "n1"},
        idempotency_key=key,
        idempotency_scope=scope,
        created_at=NOW,
        updated_at=NOW,
    )


def test_run_repository_compare_and_set_state(uow_factory) -> None:
    project = _project(WS)
    run = _run(project.id)
    with uow_factory() as uow:
        uow.repos.projects.save(project)
        uow.repos.runs.add(run)
        uow.commit()

    with uow_factory() as uow:
        queued_to_running = uow.repos.runs.compare_and_set_state(
            run.id, expected=str(RunState.QUEUED), target=str(RunState.RUNNING), updated_at=NOW + 1
        )
        uow.commit()
    assert queued_to_running is True

    # 取消后，迟到的 provider 结果必须写不进去。
    with uow_factory() as uow:
        uow.repos.runs.compare_and_set_state(
            run.id, expected=str(RunState.RUNNING), target=str(RunState.CANCELLED), updated_at=NOW + 2
        )
        uow.commit()

    with uow_factory() as uow:
        late_commit = uow.repos.runs.compare_and_set_state(
            run.id,
            expected=str(RunState.RUNNING),
            target=str(RunState.COMPLETED),
            updated_at=NOW + 3,
            result={"explanation": {"answer": "late"}},
        )
        uow.commit()
    assert late_commit is False

    with uow_factory() as uow:
        final = uow.repos.runs.get(run.id)
        uow.commit()
    assert final is not None
    assert final.state is RunState.CANCELLED
    assert final.result is None


def test_run_repository_idempotency_lookup_is_workspace_scoped(uow_factory) -> None:
    project = _project(WS)
    run = _run(project.id, key="key-1", scope=WS)
    with uow_factory() as uow:
        uow.repos.projects.save(project)
        uow.repos.runs.add(run)
        uow.commit()

    with uow_factory() as uow:
        hit = uow.repos.runs.find_by_idempotency_key(WS, "key-1")
        other_scope = uow.repos.runs.find_by_idempotency_key(OTHER_WS, "key-1")
        missing = uow.repos.runs.find_by_idempotency_key(WS, "key-2")
        blank = uow.repos.runs.find_by_idempotency_key(WS, "")
        uow.commit()
    assert hit is not None and hit.id == run.id
    assert other_scope is None  # 同 key 不同 workspace 不幂等
    assert missing is None
    assert blank is None


def test_run_repository_marks_in_flight_runs_interrupted(uow_factory) -> None:
    project = _project(WS)
    queued = _run(project.id)
    running = Run(id=str(uuid.uuid4()), project_id=project.id, kind=RunKind.NARRATION,
                  state=RunState.RUNNING, workload=RunWorkload.MODEL, created_at=NOW, updated_at=NOW)
    done = Run(id=str(uuid.uuid4()), project_id=project.id, kind=RunKind.NARRATION,
               state=RunState.COMPLETED, workload=RunWorkload.MODEL, created_at=NOW, updated_at=NOW)
    with uow_factory() as uow:
        uow.repos.projects.save(project)
        for item in (queued, running, done):
            uow.repos.runs.add(item)
        uow.commit()

    with uow_factory() as uow:
        interrupted = uow.repos.runs.mark_in_flight_interrupted()
        uow.commit()
    assert sorted(interrupted) == sorted([queued.id, running.id])

    with uow_factory() as uow:
        after = {item.id: item.state for item in uow.repos.runs.list_for_project(project.id)}
        uow.commit()
    assert after[queued.id] is RunState.INTERRUPTED
    assert after[running.id] is RunState.INTERRUPTED
    assert after[done.id] is RunState.COMPLETED  # 终态不被改写（不重放）


def test_run_events_have_monotonic_seq(uow_factory) -> None:
    from backend.domain.models import RunEvent

    project = _project(WS)
    run = _run(project.id)
    with uow_factory() as uow:
        uow.repos.projects.save(project)
        uow.repos.runs.add(run)
        uow.commit()

    with uow_factory() as uow:
        for _ in range(3):
            seq = uow.repos.run_events.next_seq(run.id)
            uow.repos.run_events.append(
                RunEvent(run_id=run.id, seq=seq, type="progress", created_at=NOW, payload={"seq": seq})
            )
        uow.commit()

    with uow_factory() as uow:
        events = uow.repos.run_events.list_for_run(run.id)
        after_two = uow.repos.run_events.list_for_run(run.id, after_seq=2)
        uow.commit()
    assert [event.seq for event in events] == [1, 2, 3]
    assert [event.seq for event in after_two] == [3]  # 断点重放


# ============================================================ 并发访问
def test_concurrent_writers_serialize_without_lost_updates(uow_factory) -> None:
    """多线程写同一项目：``BEGIN IMMEDIATE`` + busy_timeout 串行化，不丢更新。"""

    project = _project(WS)
    with uow_factory() as uow:
        uow.repos.projects.save(project)
        uow.commit()

    writers = 6
    per_writer = 3
    errors: list[BaseException] = []

    def writer() -> None:
        try:
            for _ in range(per_writer):
                with uow_factory() as uow:
                    order_index = uow.repos.documents.next_order_index(project.id)
                    document = _document(project.id)
                    document.order_index = order_index
                    uow.repos.documents.add(document)
                    uow.commit()
        except BaseException as exc:  # pragma: no cover - 失败时用于断言输出
            errors.append(exc)

    threads = [threading.Thread(target=writer) for _ in range(writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    with uow_factory() as uow:
        documents = uow.repos.documents.list_for_project(project.id)
        uow.commit()
    assert len(documents) == writers * per_writer
    # 每个 writer 在事务内读 max+1，串行化后 order_index 必须无重复。
    indices = [document.order_index for document in documents]
    assert len(set(indices)) == len(indices)


def test_concurrent_cas_has_exactly_one_winner(uow_factory) -> None:
    """两个"完成"竞争者只能有一个写入结果（取消/超时不被覆盖）。"""

    project = _project(WS)
    run = _run(project.id)
    with uow_factory() as uow:
        uow.repos.projects.save(project)
        uow.repos.runs.add(run)
        uow.repos.runs.compare_and_set_state(
            run.id, expected=str(RunState.QUEUED), target=str(RunState.RUNNING), updated_at=NOW + 1
        )
        uow.commit()

    results: list[bool] = []
    lock = threading.Lock()

    def complete() -> None:
        with uow_factory() as uow:
            won = uow.repos.runs.compare_and_set_state(
                run.id,
                expected=str(RunState.RUNNING),
                target=str(RunState.COMPLETED),
                updated_at=NOW + 2,
                result={"explanation": {"answer": "ok"}},
            )
            uow.commit()
        with lock:
            results.append(won)

    threads = [threading.Thread(target=complete) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(results) == [False, True]


def test_concurrent_readers_do_not_block_writers(uow_factory) -> None:
    """WAL 下读不阻塞写：读者看到的是一致快照，写者不被读锁挡住。"""

    project = _project(WS)
    with uow_factory() as uow:
        uow.repos.projects.save(project)
        uow.commit()

    reader_errors: list[BaseException] = []
    stop = threading.Event()

    def reader() -> None:
        try:
            while not stop.is_set():
                with uow_factory.reader() as uow:
                    uow.repos.documents.list_for_project(project.id)
                    uow.commit()
        except BaseException as exc:  # pragma: no cover
            reader_errors.append(exc)

    reader_thread = threading.Thread(target=reader)
    reader_thread.start()
    try:
        for _ in range(10):
            with uow_factory() as uow:
                document = _document(project.id)
                document.order_index = uow.repos.documents.next_order_index(project.id)
                uow.repos.documents.add(document)
                uow.commit()
    finally:
        stop.set()
        reader_thread.join(timeout=30)

    assert reader_errors == []
    with uow_factory() as uow:
        assert len(uow.repos.documents.list_for_project(project.id)) == 10
        uow.commit()


# ============================================================ providers
def test_fake_provider_is_deterministic_and_offline() -> None:
    provider = FakeProvider(model="offline-demo")
    assert provider.info.name == "fake"
    request = GenerateRequest(kind="explanation", prompt="什么是单调栈？", context=["片段A"])
    first = provider.generate(request)
    second = provider.generate(request)
    assert first.text == second.text
    assert first.payload["input_digest"] == second.payload["input_digest"]
    assert first.provider == "fake"
    assert first.model == "offline-demo"
    assert "片段A" in first.text


def test_fake_provider_output_changes_with_input() -> None:
    provider = FakeProvider()
    a = provider.generate(GenerateRequest(kind="explanation", prompt="A"))
    b = provider.generate(GenerateRequest(kind="explanation", prompt="B"))
    assert a.text != b.text


def test_build_provider_defaults_to_fake_without_api_key() -> None:
    from backend.infrastructure.providers import build_provider

    settings = Settings.for_tests(".")
    provider = build_provider(settings)
    assert provider.info.name == "fake"
    # 默认配置下不应持有任何密钥。
    assert settings.model.api_key is None


def test_openai_compatible_sends_key_in_header_only() -> None:
    from backend.infrastructure.providers import build_provider
    from backend.application.ports.provider import GenerateRequest

    calls: dict[str, object] = {}

    def transport(request, timeout):
        calls["url"] = request.full_url
        calls["auth"] = request.headers.get("Authorization")
        calls["body"] = request.data
        import json as _json

        return 200, _json.dumps(
            {"model": "m", "choices": [{"message": {"content": "hello"}}], "usage": {"prompt_tokens": 3, "completion_tokens": 4}}
        ).encode()

    settings = Settings.for_tests(
        ".",
        model=ModelSettings(
            provider="openai_compatible",
            api_base="https://example.invalid/v1",
            api_key="sk-secret",
            name="m",
        ),
    )
    provider = build_provider(settings, transport=transport)
    result = provider.generate(GenerateRequest(kind="explanation", prompt="q"))
    assert calls["url"] == "https://example.invalid/v1/chat/completions"
    assert calls["auth"] == "Bearer sk-secret"
    assert b"sk-secret" not in calls["body"]  # 密钥不进请求体
    assert result.text == "hello"
    assert result.provider == "openai_compatible"
    assert result.completion_tokens == 4


@pytest.mark.parametrize(
    "response",
    [
        b"not json",
        b'{"choices": []}',
        b'{"choices": [{"message": {"content": ""}}]}',
        b"[]",
    ],
)
def test_openai_compatible_fails_closed_on_bad_payload(response: bytes) -> None:
    from backend.domain.errors import ProviderUnavailable
    from backend.infrastructure.providers.openai_compatible import OpenAICompatibleProvider

    provider = OpenAICompatibleProvider(
        api_base="https://example.invalid/v1",
        api_key="sk-secret",
        model="m",
        transport=lambda request, timeout: (200, response),
    )
    with pytest.raises(ProviderUnavailable):
        provider.generate(GenerateRequest(kind="explanation", prompt="q"))


def test_openai_compatible_non_200_raises_without_leaking_key() -> None:
    from backend.domain.errors import ProviderUnavailable
    from backend.infrastructure.providers.openai_compatible import OpenAICompatibleProvider

    provider = OpenAICompatibleProvider(
        api_base="https://example.invalid/v1",
        api_key="sk-secret",
        model="m",
        transport=lambda request, timeout: (500, b"upstream error"),
    )
    with pytest.raises(ProviderUnavailable) as excinfo:
        provider.generate(GenerateRequest(kind="explanation", prompt="q"))
    assert "sk-secret" not in str(excinfo.value)
    assert "sk-secret" not in repr(excinfo.value.details)


def test_openai_compatible_requires_credentials() -> None:
    from backend.domain.errors import ProviderUnavailable
    from backend.infrastructure.providers.openai_compatible import OpenAICompatibleProvider

    with pytest.raises(ProviderUnavailable):
        OpenAICompatibleProvider(api_base="", api_key="", model="")
    with pytest.raises(ProviderUnavailable):
        OpenAICompatibleProvider(api_base="https://x.invalid", api_key="", model="m")


# ============================================================ narration 派生字段
def test_narration_derived_fields_match_segments() -> None:
    from backend.domain.models import Narration, NarrationSegment

    narration = Narration(
        id="n1", project_id="p1", title="串讲", provider="fake", model="offline-demo", created_at=NOW
    )
    narration.segments = [
        NarrationSegment(id="s1", narration_id="n1", order_index=0, node_id="a", title="A", text="t1", duration_seconds=1.5),
        NarrationSegment(id="s2", narration_id="n1", order_index=1, node_id="b", title="B", text="t2", duration_seconds=2.5),
    ]
    assert narration.segment_count == 2
    assert narration.total_duration_seconds == pytest.approx(4.0)
    payload = narration.to_dict()
    assert payload["segment_count"] == len(payload["segments"]) == 2
    assert payload["total_duration_seconds"] == pytest.approx(
        sum(segment["duration_seconds"] for segment in payload["segments"])
    )


def test_narration_repository_persists_and_orders_segments(uow_factory) -> None:
    from backend.domain.models import Narration, NarrationSegment

    project = _project(WS)
    document = _document(project.id)
    node_a = Node(id=str(uuid.uuid4()), project_id=project.id, document_id=document.id,
                  title="A", kind="section", revision=1, created_at=NOW, updated_at=NOW)
    node_b = Node(id=str(uuid.uuid4()), project_id=project.id, document_id=document.id,
                  title="B", kind="concept", revision=1, created_at=NOW, updated_at=NOW)
    narration = Narration(id=str(uuid.uuid4()), project_id=project.id, title="串讲",
                          provider="fake", model="offline-demo", created_at=NOW)
    # 故意乱序传入：repository 应按 order_index 落库。
    segments = [
        NarrationSegment(id=str(uuid.uuid4()), narration_id=narration.id, order_index=1,
                         node_id=node_b.id, title="B", text="second", duration_seconds=2.0),
        NarrationSegment(id=str(uuid.uuid4()), narration_id=narration.id, order_index=0,
                         node_id=node_a.id, title="A", text="first", duration_seconds=1.0),
    ]
    with uow_factory() as uow:
        uow.repos.projects.save(project)
        uow.repos.documents.add(document)
        uow.repos.nodes.add(node_a)
        uow.repos.nodes.add(node_b)
        uow.repos.narrations.add(narration, segments)
        uow.commit()

    with uow_factory() as uow:
        loaded = uow.repos.narrations.get(narration.id)
        uow.commit()
    assert loaded is not None
    assert [segment.order_index for segment in loaded.segments] == [0, 1]
    assert [segment.node_id for segment in loaded.segments] == [node_a.id, node_b.id]
    assert loaded.segment_count == 2
    assert loaded.total_duration_seconds == pytest.approx(3.0)

    # 派生值不是独立真值：库里不存 segment_count / total_duration_seconds 列。
    with uow_factory() as uow:
        columns = {
            row["name"]
            for row in uow.connection.execute("PRAGMA table_info(narrations)").fetchall()
        }
        uow.commit()
    assert "segment_count" not in columns
    assert "total_duration_seconds" not in columns


# ============================================================ artifacts / builds
def test_artifact_and_build_metadata_roundtrip(uow_factory) -> None:
    from backend.domain.models import DocumentBuild

    project = _project(WS)
    document = _document(project.id)
    revision = DocumentRevision(id=str(uuid.uuid4()), document_id=document.id, revision_no=1,
                                content_hash=content_hash(b"body"), size_bytes=4,
                                status=RevisionStatus.READY, created_at=NOW)
    artifact = DocumentArtifact(id=str(uuid.uuid4()), revision_id=revision.id,
                                kind=ArtifactKind.EXTRACTED_TEXT, path="artifacts/d/r/extracted_text/extracted.txt",
                                content_hash=content_hash(b"body"), size_bytes=4, created_at=NOW)
    build = DocumentBuild(id=str(uuid.uuid4()), revision_id=revision.id, status="completed",
                          created_at=NOW, updated_at=NOW)
    with uow_factory() as uow:
        uow.repos.projects.save(project)
        uow.repos.documents.add(document)
        uow.repos.revisions.add(revision)
        uow.artifacts.add(artifact)
        uow.artifacts.add_build(build)
        uow.commit()

    with uow_factory() as uow:
        loaded_artifact = uow.artifacts.latest_for_revision(revision.id, kind="extracted_text")
        builds = uow.artifacts.list_builds(revision.id)
        uow.commit()
    assert loaded_artifact is not None and loaded_artifact.path.endswith("extracted.txt")
    assert [item.id for item in builds] == [build.id]
    # source_map 为可选：没有真实 compiler 时如实为 None，不虚构。
    assert builds[0].source_map is None


def test_build_failure_keeps_previous_successful_artifact(uow_factory) -> None:
    """``partial``/``failed`` build 不覆盖旧的成功产物。"""

    from backend.domain.models import DocumentBuild

    project = _project(WS)
    document = _document(project.id)
    revision = DocumentRevision(id=str(uuid.uuid4()), document_id=document.id, revision_no=1,
                                content_hash=content_hash(b"tex"), size_bytes=3,
                                status=RevisionStatus.READY, created_at=NOW)
    good = DocumentArtifact(id=str(uuid.uuid4()), revision_id=revision.id,
                            kind=ArtifactKind.COMPILED_PDF, path="artifacts/d/r/compiled_pdf/main.pdf",
                            content_hash=content_hash(b"%PDF"), size_bytes=4, created_at=NOW)
    failed = DocumentBuild(id=str(uuid.uuid4()), revision_id=revision.id, status="failed",
                           error_code="compile_failed", error_message="boom",
                           created_at=NOW + 1, updated_at=NOW + 1)
    with uow_factory() as uow:
        uow.repos.projects.save(project)
        uow.repos.documents.add(document)
        uow.repos.revisions.add(revision)
        uow.artifacts.add(good)
        uow.artifacts.add_build(failed)
        uow.commit()

    with uow_factory() as uow:
        latest = uow.artifacts.latest_for_revision(revision.id, kind="compiled_pdf")
        builds = uow.artifacts.list_builds(revision.id)
        uow.commit()
    assert latest is not None and latest.id == good.id
    assert builds[0].status == "failed"
    assert builds[0].artifact_id is None


# ============================================================ 文本偏移 / 解码
def test_utf16_codepoint_roundtrip_with_astral_plane() -> None:
    text = "a\U0001F600b"  # 一个 BMP 字符 + 一个代理对字符 + 一个 BMP 字符
    assert utf16_offset_to_codepoint(text, 0) == 0
    assert utf16_offset_to_codepoint(text, 1) == 1
    assert utf16_offset_to_codepoint(text, 3) == 2  # 代理对占 2 个 utf16 code unit
    assert codepoint_offset_to_utf16(text, 2) == 3
    for codepoint_offset in range(len(text) + 1):
        utf16 = codepoint_offset_to_utf16(text, codepoint_offset)
        assert utf16_offset_to_codepoint(text, utf16) == codepoint_offset


def test_utf16_offset_inside_surrogate_pair_is_rejected() -> None:
    from backend.domain.errors import DomainError

    text = "\U0001F600"
    with pytest.raises(DomainError):
        utf16_offset_to_codepoint(text, 1)  # 落在代理对中间


def test_invalid_utf8_fails_closed_without_replacement() -> None:
    from backend.domain.errors import EncodingUnsupported

    with pytest.raises(EncodingUnsupported):
        decode_utf8_strict(b"\xff\xfe\x00")
    assert decode_utf8_strict("中文".encode("utf-8")) == "中文"


def test_model_metadata_invalid_json_is_reported_not_silently_empty(uow_factory) -> None:
    from backend.domain.errors import DomainError

    project = _project(WS)
    document = _document(project.id)
    node = Node(id=str(uuid.uuid4()), project_id=project.id, document_id=document.id,
                title="n", kind="section", revision=1, created_at=NOW, updated_at=NOW)
    with uow_factory() as uow:
        uow.repos.projects.save(project)
        uow.repos.documents.add(document)
        uow.repos.nodes.add(node)
        uow.commit()

    # 直接写坏 JSON：读取时应显式报错，而不是静默当空 metadata。
    with uow_factory() as uow:
        uow.connection.execute("UPDATE nodes SET metadata_json = ? WHERE id = ?", ("{not json", node.id))
        uow.commit()

    with pytest.raises(DomainError):
        with uow_factory() as uow:
            uow.repos.nodes.get(node.id)
            uow.commit()


# ============================== symlink 拒绝（monkeypatch：不依赖宿主是否允许建链接）
def test_assert_regular_file_rejects_symlink_via_lstat(monkeypatch, tmp_path: Path) -> None:
    """即使宿主不允许创建符号链接，也要覆盖"lstat 判定为链接即拒绝"这条逻辑。"""

    import stat as stat_module

    from backend.infrastructure.storage import paths as storage_paths

    target = tmp_path / "plain.txt"
    target.write_text("x", encoding="utf-8")
    real_lstat = os.lstat

    def fake_lstat(path, *args, **kwargs):
        if os.fspath(path) == os.fspath(target):
            info = real_lstat(path, *args, **kwargs)
            return os.stat_result(tuple([stat_module.S_IFLNK | 0o777] + list(info)[1:]))
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(storage_paths.os, "lstat", fake_lstat)
    with pytest.raises(SymlinkRejected):
        storage_paths.assert_regular_file(str(target))


def test_resolve_rejects_realpath_escaping_root(monkeypatch, tmp_path: Path) -> None:
    """路径经符号链接归约后越出根的判定（monkeypatch ``realpath`` 模拟链接）。"""

    from backend.infrastructure.storage import paths as storage_paths

    root = tmp_path / "data"
    (root / "sources").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()

    real_realpath = os.path.realpath

    def fake_realpath(path):
        text = os.fspath(path).replace("\\", "/")
        if "sources" in text and not text.endswith("/data"):
            return str(outside / "evil.txt")
        return real_realpath(path)

    monkeypatch.setattr(storage_paths, "realpath", fake_realpath)
    with pytest.raises(SymlinkRejected):
        storage_paths.resolve_relative_under_root(str(root), "sources/main.md")


def test_safe_open_read_uses_nofollow_and_rejects_symlink(monkeypatch, tmp_path: Path) -> None:
    import stat as stat_module

    from backend.infrastructure.storage import paths as storage_paths

    target = tmp_path / "f.txt"
    target.write_text("data", encoding="utf-8")
    real_lstat = os.lstat

    def fake_lstat(path, *args, **kwargs):
        info = real_lstat(path, *args, **kwargs)
        return os.stat_result(tuple([stat_module.S_IFLNK | 0o777] + list(info)[1:]))

    monkeypatch.setattr(storage_paths.os, "lstat", fake_lstat)
    with pytest.raises(SymlinkRejected):
        storage_paths.safe_open_read(str(target))


def test_local_content_store_rejects_symlinked_artifact(monkeypatch, tmp_path: Path) -> None:
    """存储层的读取路径同样必须拒绝链接（不是只有底层函数做校验）。"""

    import stat as stat_module

    from backend.infrastructure.storage import local as local_module

    store = local_module.LocalContentStore(str(tmp_path / "data"))
    store.write_artifact(
        document_id="d1", revision_id="r1", kind="extracted_text", filename="e.txt", data=b"body"
    )
    real_lstat = os.lstat

    def fake_lstat(path, *args, **kwargs):
        info = real_lstat(path, *args, **kwargs)
        if stat_module.S_ISREG(info.st_mode):
            return os.stat_result(tuple([stat_module.S_IFLNK | 0o777] + list(info)[1:]))
        return info

    monkeypatch.setattr(local_module.os, "lstat", fake_lstat)
    with pytest.raises(SymlinkRejected):
        store.read_source(relative_path="artifacts/d1/r1/extracted_text/e.txt")
