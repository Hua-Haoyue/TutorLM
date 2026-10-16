"""Volume-external application session store.

Hard rule (contracts/instance-runtime-v3.md, docs/instance-app-design.md 4.1):
application sessions and credentials live only under
`TUTORLM_SESSION_DATA_DIR`, never inside `TUTORLM_DATA_DIR`. Sharing/cloning
copies the volume tree, so a session inside it would become the receiver's
identity.

Tokens are stored as salted hashes; the plaintext token exists only in the
response body of `POST /api/sessions`. Nothing here is logged.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from ..errors import SessionDirInvalid, SessionExpired, Unauthorized

SESSION_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  token_hash TEXT NOT NULL UNIQUE,
  workspace_id TEXT NOT NULL,
  owner_id TEXT,
  issued_at TEXT NOT NULL,
  expires_at INTEGER NOT NULL,
  revoked INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sessions_workspace ON sessions(workspace_id);
"""

TOKEN_BYTES = 32
SESSION_ID_BYTES = 16
HASH_ROUNDS = 60000


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _hash_token(token: str, salt: bytes) -> str:
    """PBKDF2-HMAC-SHA256; stdlib only, no credentials in the workspace volume."""
    digest = hashlib.pbkdf2_hmac("sha256", token.encode("utf-8"), salt, HASH_ROUNDS)
    return salt.hex() + "$" + digest.hex()


def _verify_token(token: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split("$", 1)
    except ValueError:
        return False
    try:
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    candidate = _hash_token(token, salt)
    return hmac.compare_digest(candidate, stored)


class SessionStore:
    """Issues and validates application sessions (distinct from compute leases)."""

    def __init__(
        self,
        db_path: Path,
        *,
        workspace_id: str,
        owner_id: str | None = None,
        data_dir: Path | None = None,
        ttl_seconds: int = 3600,
    ) -> None:
        self.db_path = Path(db_path)
        self.workspace_id = workspace_id
        self.owner_id = owner_id
        self.ttl_seconds = int(ttl_seconds)
        self._lock = threading.Lock()

        # Defense in depth: refuse to run with sessions inside the volume even if
        # a caller wires it that way.
        if data_dir is not None:
            self._assert_outside_volume(data_dir)

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.executescript(SESSION_SCHEMA)
        finally:
            conn.close()

    @staticmethod
    def _assert_outside_volume(data_dir: Path) -> None:
        session_dir = Path(os.environ.get("TUTORLM_SESSION_DATA_DIR") or "").resolve()
        volume = Path(data_dir).resolve()
        if not session_dir:
            return
        if session_dir == volume or volume in session_dir.parents:
            raise SessionDirInvalid(
                "TUTORLM_SESSION_DATA_DIR must be outside TUTORLM_DATA_DIR; sessions are never copied with the volume"
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    # -- issue / validate ------------------------------------------------
    def issue(self, *, workspace_id: str | None = None, ttl_seconds: int | None = None) -> tuple[str, dict[str, Any]]:
        target = workspace_id or self.workspace_id
        ttl = int(ttl_seconds or self.ttl_seconds)
        now = _utcnow()
        expires = now + timedelta(seconds=ttl)
        token = secrets.token_urlsafe(TOKEN_BYTES)
        session_id = secrets.token_hex(SESSION_ID_BYTES)
        salt = secrets.token_bytes(16)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO sessions (id, token_hash, workspace_id, owner_id, issued_at, expires_at, revoked)"
                    " VALUES (?, ?, ?, ?, ?, ?, 0)",
                    (
                        session_id,
                        _hash_token(token, salt),
                        target,
                        self.owner_id,
                        now.isoformat(timespec="seconds").replace("+00:00", "Z"),
                        int(expires.timestamp()),
                    ),
                )
                conn.execute("COMMIT")
            except sqlite3.Error:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()
        session = {
            "id": session_id,
            "workspace_id": target,
            "expires_at": expires.isoformat(timespec="seconds").replace("+00:00", "Z"),
        }
        return token, session

    def validate(self, token: str | None) -> dict[str, Any]:
        """Return the session record or raise 401/410 with the documented codes."""
        if not token:
            raise Unauthorized("Missing application session token")
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute("SELECT * FROM sessions WHERE revoked = 0").fetchall()
            finally:
                conn.close()
        matched: dict[str, Any] | None = None
        for row in rows:
            if _verify_token(token, str(row["token_hash"])):
                matched = dict(row)
                break
        if matched is None:
            raise Unauthorized("Application session token is not valid")
        if int(matched["expires_at"]) <= int(_utcnow().timestamp()):
            raise SessionExpired("Application session expired; request a new session")
        return matched

    def get(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def revoke(self, session_id: str) -> None:
        """Mark a session for cleanup; project content is never deleted here."""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("UPDATE sessions SET revoked = 1 WHERE id = ?", (session_id,))
                conn.execute("COMMIT")
            except sqlite3.Error:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    def gc(self) -> int:
        """Drop revoked/expired session rows only. Never touches sources/ or artifacts/."""
        now = int(_utcnow().timestamp())
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                cur = conn.execute("DELETE FROM sessions WHERE revoked = 1 OR expires_at <= ?", (now,))
                changed = cur.rowcount or 0
                conn.execute("COMMIT")
            except sqlite3.Error:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()
        return changed

    def public(self, record: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": record.get("id"),
            "workspace_id": record.get("workspace_id"),
            "expires_at": _epoch_to_iso(record.get("expires_at")),
        }


def _epoch_to_iso(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (TypeError, ValueError):
        return None


__all__ = ["SessionStore", "SESSION_SCHEMA"]
