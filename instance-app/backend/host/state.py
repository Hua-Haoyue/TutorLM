"""AppState: the assembled Host graph handed to the HTTP adapter.

This is the single composition point. ``create_app`` builds it (or accepts one
for tests), routers receive it via ``app.state``, and nothing else constructs
storage/session/run objects on the fly.

Why it lives in ``host/``: assembling ports + adapters is a composition-root
concern (``docs/instance-app-design.md`` section 2). It imports foundation
``domain`` values but **no** control-plane or Docker module.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time

from backend.domain.enums import StorageLayout

from .binding import verify_and_persist
from .config import HostConfig
from .health import HealthService
from .local.provider import FakeProvider, build_host_provider
from .local.sessions import SessionStore
from .local.store import LocalStorage, LocalStore
from .plugins import build_registry, PluginRegistry
from .runs import RunManager
from .services import AppServices
from .storage_probe import ensure_data_root


@dataclass
class AppState:
    """Everything a request may need, wired once at startup."""

    config: HostConfig
    store: LocalStore
    storage: LocalStorage
    sessions: SessionStore
    runs: RunManager
    plugins: PluginRegistry
    services: AppServices
    health: HealthService
    binding_reasons: tuple[str, ...] = ()


def build_state(
    config: HostConfig,
    *,
    store: LocalStore | None = None,
    storage: LocalStorage | None = None,
    sessions: SessionStore | None = None,
    runs: RunManager | None = None,
    plugins: PluginRegistry | None = None,
    provider: FakeProvider | None = None,
    initialize: bool = True,
) -> AppState:
    """Assemble the Host graph, applying the documented startup rules.

    ``initialize=False`` skips the volume mutations (marker write, binding insert,
    interrupted-run sweep) so a caller can inspect readiness without side effects.
    """
    # Readiness reports the real reason; this call only makes sure a fresh volume
    # exists so the marker/binding checks below have something to inspect.
    config.validate_workspace_path()
    ensure_data_root(Path(config.data_dir))

    storage = storage or LocalStorage(config.data_dir)
    storage.ensure_roots()

    store = store or LocalStore(config.business_db_path)
    plugins = plugins or build_registry(config.enabled_plugins)

    session_db = config.session_db_path()
    if sessions is None:
        if session_db is None:
            # Sessions must live outside the volume; without an explicit
            # directory we fail closed rather than writing inside the project.
            from .errors import SessionDirInvalid

            raise SessionDirInvalid(
                "TUTORLM_SESSION_DATA_DIR is required: application sessions must never live inside the workspace volume"
            )
        sessions = SessionStore(
            session_db,
            workspace_id=config.workspace_id or "unbound",
            owner_id=config.owner_id,
            data_dir=Path(config.data_dir),
            ttl_seconds=config.session_ttl_seconds,
        )

    runs = runs or RunManager(
        store,
        max_runs_per_session=config.limits.max_runs_per_session,
        executors={
            "model": (config.run_workers, config.run_queue_size),
            "extract": (config.extract_workers, config.extract_queue_size),
            "tex_compile": (config.compile_workers, config.compile_queue_size),
        },
    )
    def session_valid(session_id: str) -> bool:
        record = sessions.get(session_id)
        return bool(record and not record.get('revoked') and record['expires_at'] > time.time())

    runs.session_valid = session_valid

    binding_reasons: tuple[str, ...] = ()
    if initialize and config.requires_workspace_binding:
        layout = StorageLayout.CONTENT_ONLY if not _is_legacy(config.workspace_subdirectory) else StorageLayout.LEGACY_ROOT
        _, binding_reasons = verify_and_persist(config, store, storage_layout=layout)
        # A restart must not replay paid calls; close bookkeeping once.
        runs.reclaim_interrupted()

    if provider is None:
        provider = build_host_provider(config.provider)

    services = AppServices(
        config,
        store=store,
        storage=storage,
        sessions=sessions,
        runs=runs,
        plugins=plugins,
        provider=provider,
    )
    health = HealthService(config)

    return AppState(
        config=config,
        store=store,
        storage=storage,
        sessions=sessions,
        runs=runs,
        plugins=plugins,
        services=services,
        health=health,
        binding_reasons=binding_reasons,
    )


def _is_legacy(subdirectory: str) -> bool:
    return (subdirectory or "").strip() in ("", ".")


__all__ = ["AppState", "build_state"]
