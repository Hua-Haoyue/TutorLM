"""Workspace binding adapter over ``backend.domain.workspace_binding``.

The verification algorithm (fail closed on untrusted clones, never rewrite
business stable ids) is a **pure domain decision**. This module is only the Host
adapter: read the stored row, call the domain function, persist the result, and
write the layout marker used by the readiness proof.

Marker: ``.tutorlm-layout.json`` at the data root, owned by
``domain/storage_layout.build_marker`` -- the single canonical format.
"""
from __future__ import annotations

import os
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from backend.domain.enums import StorageLayout
from backend.domain.volume import is_legacy_layout
from backend.domain.errors import (
    ConfigInvalid,
    StorageNotInitialized,
    WorkspaceBindingConflict,
    WorkspaceRebindForbidden,
)
from backend.domain.storage_layout import (
    LAYOUT_MARKER_FILENAME,
    LayoutMarker,
    build_marker,
    parse_marker,
)
from backend.domain.workspace_binding import BindingAction, WorkspaceBinding, decide_workspace_binding
from backend.domain.ids import new_id

from .config import HostConfig


def _iso_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

#: Marker format version owned by the domain module.
MARKER_FORMAT = 1


def _now() -> float:
    import time

    return time.time()


def _binding_timestamp(value: Any) -> float:
    try:
        result = float(value)
    except (ValueError, TypeError):
        try:
            parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                raise ValueError('Missing timezone')
            result = parsed.astimezone(timezone.utc).timestamp()
        except (ValueError, TypeError, OverflowError):
            raise ConfigInvalid('Stored workspace binding timestamp is invalid') from None
    if not math.isfinite(result) or result < 0:
        raise ConfigInvalid('Stored workspace binding timestamp is invalid')
    return result


class MarkerStore:
    """Reads/writes the domain-owned layout marker at the data root."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def read(self) -> bytes | None:
        try:
            if self.path.is_symlink() or not self.path.is_file():
                return None
            return self.path.read_bytes()
        except OSError:
            return None

    def parsed(self) -> tuple[LayoutMarker | None, str | None]:
        return parse_marker(self.read())

    def write(self, marker: LayoutMarker) -> None:
        payload = marker.to_json_bytes()
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_bytes(payload)
        os.replace(tmp, self.path)


def marker_path(config: HostConfig) -> Path:
    return Path(config.data_dir) / LAYOUT_MARKER_FILENAME


def read_binding_row(store: Any) -> WorkspaceBinding | None:
    """Adapt the fallback store row into the domain value object."""
    row: Mapping[str, Any] | None = None
    loader = getattr(store, "read_binding", None)
    if callable(loader):
        row = loader()
    if not row:
        return None
    current = row.get("current_workspace_id")
    origin = row.get("origin_workspace_id") or current
    owner = row.get("owner_id")
    if not current or not origin or not owner:
        return None
    try:
        layout = StorageLayout(str(row.get("storage_layout") or StorageLayout.LEGACY_ROOT))
    except ValueError:
        layout = StorageLayout.LEGACY_ROOT
    return WorkspaceBinding(
        current_workspace_id=str(current),
        origin_workspace_id=str(origin),
        owner_id=str(owner),
        updated_at=_binding_timestamp(row.get('updated_at', 0.0)),
        copy_id=(str(row["copy_id"]) if row.get("copy_id") else None),
        storage_layout=layout,
    )


def verify_and_persist(
    config: HostConfig,
    store: Any,
    *,
    storage_layout: StorageLayout = StorageLayout.LEGACY_ROOT,
) -> tuple[WorkspaceBinding | None, tuple[str, ...]]:
    """Run the domain decision and persist only what the domain allowed.

    Raises ``ConfigInvalid`` / ``WorkspaceBindingConflict`` /
    ``WorkspaceRebindForbidden`` -- the same fail-closed errors the domain raises,
    which the HTTP layer maps to the documented status codes.
    """
    if not config.requires_workspace_binding:
        return None, ()

    workspace_id = config.workspace_id
    owner_id = config.owner_id
    if not workspace_id or not owner_id:
        raise ConfigInvalid("持久模式必须提供 TUTORLM_WORKSPACE_ID 与 TUTORLM_OWNER_ID")

    existing = read_binding_row(store)
    decision = decide_workspace_binding(
        existing,
        workspace_id=workspace_id,
        owner_id=owner_id,
        source_workspace_id=config.workspace_source_id,
        copy_id=config.workspace_copy_id,
        now=_now(),
        storage_layout=storage_layout,
    )

    if decision.action is BindingAction.CREATED:
        _save(store, decision.binding)
    elif decision.action is BindingAction.REBOUND:
        _rebind(store, decision.binding, existing=existing)

    # The marker only ever declares `content_only`, so it is written exclusively
    # for a v3 subdirectory layout. A legacy mount root stays unmarked and is
    # therefore reported as `legacy_root` (never share-safe).
    if not is_legacy_layout(config.workspace_subdirectory):
        marker = MarkerStore(marker_path(config))
        parsed, _ = marker.parsed()
        if parsed is None or parsed.workspace_id != decision.binding.current_workspace_id:
            marker.write(
                build_marker(
                    workspace_id=decision.binding.current_workspace_id,
                    owner_id=decision.binding.owner_id,
                    subdirectory=config.workspace_subdirectory,
                    created_at=_iso_now(),
                )
            )
    return decision.binding, decision.reasons


def _save(store: Any, binding: WorkspaceBinding) -> None:
    saver = getattr(store, "save_binding", None)
    if callable(saver):
        saver(binding)
        return
    store.ensure_binding(binding.current_workspace_id, binding.owner_id)


def _rebind(store: Any, binding: WorkspaceBinding, *, existing: WorkspaceBinding | None) -> None:
    payload = {
        "from_workspace_id": existing.current_workspace_id if existing else None,
        "to_workspace_id": binding.current_workspace_id,
        "event_id": new_id(),
    }
    rebinder = getattr(store, "rebind_workspace", None)
    if callable(rebinder):
        try:
            rebinder(
                workspace_id=binding.current_workspace_id,
                owner_id=binding.owner_id,
                source_id=binding.origin_workspace_id,
                copy_id=binding.copy_id,
                event_payload=payload,
            )
        except TypeError:
            rebinder(
                workspace_id=binding.current_workspace_id,
                owner_id=binding.owner_id,
                source_id=binding.origin_workspace_id,
                copy_id=binding.copy_id,
            )


def readiness_blocker(config: HostConfig, store: Any) -> str | None:
    """Non-fatal probe used by ``/health/ready`` without mutating the volume."""
    if not config.requires_workspace_binding:
        return None
    try:
        existing = read_binding_row(store)
        decide_workspace_binding(
            existing,
            workspace_id=config.require_workspace(),
            owner_id=config.owner_id or "",
            source_workspace_id=config.workspace_source_id,
            copy_id=config.workspace_copy_id,
            now=_now(),
            storage_layout=StorageLayout.LEGACY_ROOT,
        )
    except (ConfigInvalid, WorkspaceBindingConflict, WorkspaceRebindForbidden, StorageNotInitialized) as exc:
        return str(exc)
    return None


__all__ = [
    "MARKER_FORMAT",
    "MarkerStore",
    "marker_path",
    "read_binding_row",
    "readiness_blocker",
    "verify_and_persist",
]
