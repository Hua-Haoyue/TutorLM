"""Collect local storage evidence for the ``content_only`` readiness proof.

``domain/storage_layout.evaluate_storage_layout`` is a **pure function**: it never
touches IO. This module is the thin Host-side collector that produces the facts it
wants (top-level ``lstat`` inventory, marker bytes, writability probe).

Ownership note: the canonical collector belongs to ``infrastructure/storage``
(foundation workstream, still an empty package). Until it lands, the Host needs
facts to feed the domain decision, so this collector lives in ``host/`` and is
injected into the readiness service. It deliberately contains **no policy**: all
"is this content_only?" judgement stays in ``domain/storage_layout``.
"""
from __future__ import annotations

import os
from pathlib import Path

from backend.domain.storage_layout import (
    LAYOUT_MARKER_FILENAME,
    BUSINESS_DB_FILENAME,
    InventoryEntry,
    StorageProofFacts,
)


def collect_facts(
    *,
    workspace_id: str,
    owner_id: str,
    subdirectory: str,
    data_dir: Path,
    session_dir: Path | None,
    business_db_name: str = BUSINESS_DB_FILENAME,
) -> StorageProofFacts:
    """Gather on-disk facts about the data root. Never mutates the volume."""
    literal = Path(data_dir)
    try:
        real = literal.resolve()
    except OSError:
        real = literal
    # "The data root itself is a symlink" -- deliberately NOT
    # "abspath != realpath": on Windows that comparison also fires for 8.3
    # short names (NAMENO~1), which would wrongly block a healthy volume.
    data_root_is_symlink = os.path.islink(os.fspath(literal))

    session_value = ""
    if session_dir is not None:
        try:
            session_value = os.fspath(Path(session_dir).resolve())
        except OSError:
            session_value = os.fspath(session_dir)

    entries = _inventory(real)
    marker_raw = _read_bytes(real / LAYOUT_MARKER_FILENAME)

    return StorageProofFacts(
        workspace_id=workspace_id,
        owner_id=owner_id,
        subdirectory=subdirectory,
        data_root=os.fspath(real),
        session_dir=session_value,
        data_root_writable=_probe_writable(real),
        entries=entries,
        marker_raw=marker_raw,
        business_db_present=(real / business_db_name).is_file(),
        data_root_is_symlink=data_root_is_symlink,
    )


def _inventory(root: Path) -> tuple[InventoryEntry, ...]:
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return ()
    entries: list[InventoryEntry] = []
    for name in names:
        target = root / name
        try:
            os.lstat(target)
        except OSError:
            continue
        is_link = os.path.islink(target)
        entries.append(
            InventoryEntry(
                name=name,
                is_dir=os.path.isdir(target) and not is_link,
                is_file=os.path.isfile(target) and not is_link,
                is_symlink=is_link,
            )
        )
    return tuple(entries)


def _read_bytes(path: Path) -> bytes | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        return path.read_bytes()
    except OSError:
        return None


def _probe_writable(root: Path) -> bool:
    """Create + delete a temp file. Never falls back to the container layer."""
    import tempfile

    try:
        if not root.is_dir():
            return False
        with tempfile.NamedTemporaryFile(dir=str(root), prefix=".tutorlm-probe-", delete=True):
            pass
        return True
    except OSError:
        return False


def ensure_data_root(data_dir: Path) -> tuple[bool, str | None]:
    """Create the data root if missing and report writability with a reason."""
    try:
        Path(data_dir).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"data root is not creatable: {exc.strerror or exc.__class__.__name__}"
    if _probe_writable(Path(data_dir)):
        return True, None
    return False, (
        "data root is not writable; the volume must be owned by the application user "
        "(the instance never writes to the container layer or /tmp instead)"
    )


__all__ = ["collect_facts", "ensure_data_root"]
