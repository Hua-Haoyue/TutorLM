"""HostConfig: explicit environment parsing and validation.

Every value is read from the control-plane-injected environment; nothing is read
from the client. Unknown/invalid values fail loudly instead of silently falling
back to a weaker mode.

Reference: contracts/instance-runtime-v1.md ("config passthrough"),
v2/v3 (persistent workspace), docs/instance-app-design.md sections 3.2, 4.4, 8.
"""
from __future__ import annotations

import os
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .errors import BadRequest

TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off", ""}

RUN_MODES = ("local", "demo", "managed")
PROVIDERS = ("fake", "openai_compatible", "gemini")

#: Subtrees that must never live inside the workspace volume. Copying a volume
#: tree that contains any of these would hand the receiver somebody else's
#: session, so `content_only` is only declared when none of them exist.
SESSION_SUBTREE_NAMES = ("sessions", "session", "auth", "credentials", "tokens", "secrets")

STORAGE_MARKER_NAME = ".tutorlm-storage.json"


def _flag(environ: Mapping[str, str], name: str, default: bool = False) -> bool:
    raw = environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    raise BadRequest(f"{name} must be a boolean", code="invalid_configuration")


def _int(environ: Mapping[str, str], name: str, default: int) -> int:
    raw = environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise BadRequest(f"{name} must be an integer", code="invalid_configuration") from exc


def _float(environ: Mapping[str, str], name: str, default: float) -> float:
    raw = environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise BadRequest(f"{name} must be a number", code="invalid_configuration") from exc


def _optional(environ: Mapping[str, str], name: str) -> str | None:
    raw = environ.get(name)
    if raw is None:
        return None
    value = raw.strip()
    return value or None


def _uuid_or_none(environ: Mapping[str, str], name: str) -> str | None:
    value = _optional(environ, name)
    if value is None:
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise BadRequest(f"{name} must be a UUID", code="invalid_configuration") from exc


@dataclass(frozen=True)
class ProviderConfig:
    """Model provider settings. The API key never leaves this object."""

    name: str = "fake"
    model: str = "offline-demo"
    api_base: str | None = None
    max_tokens: int = 512
    timeout_seconds: float = 30.0
    has_api_key: bool = False
    api_key: str = field(default='', repr=False, compare=False)

    @property
    def available(self) -> bool:
        if self.name == "fake":
            return True
        return self.name in ("openai_compatible", "gemini") and bool(self.api_base) and self.has_api_key

    def public(self) -> dict[str, object]:
        """Discovery payload for GET /api/runtime (never includes the key)."""
        return {"name": self.name, "model": self.model}


@dataclass(frozen=True)
class Limits:
    """Quotas reported to the frontend and enforced by the Host."""

    max_documents_per_session: int = 50
    max_bytes_per_file: int = 2 * 1024 * 1024
    max_runs_per_session: int = 20
    max_narration_nodes: int = 10
    max_nodes_per_project: int = 2000
    page_size: int = 200

    def public(self) -> dict[str, int]:
        return {
            "max_documents_per_session": self.max_documents_per_session,
            "max_bytes_per_file": self.max_bytes_per_file,
            "max_runs_per_session": self.max_runs_per_session,
        }


@dataclass(frozen=True)
class HostConfig:
    """Immutable Host configuration resolved once at startup."""

    run_mode: str = "demo"
    data_dir: Path = Path("/data")
    session_data_dir: Path | None = None
    workspace_id: str | None = None
    owner_id: str | None = None
    workspace_subdirectory: str = "."
    persistent_workspace: bool = False
    allow_implicit_session: bool = False
    invite_code: str | None = None
    workspace_source_id: str | None = None
    workspace_copy_id: str | None = None
    session_ttl_seconds: int = 3600
    provider: ProviderConfig = field(default_factory=ProviderConfig)
    limits: Limits = field(default_factory=Limits)
    enabled_plugins: tuple[str, ...] = ('documents', 'explanations', 'knowledge-narration')
    extractor_version: str = "host-fallback-1"
    tex_compiler_available: bool = False
    run_workers: int = 2
    run_queue_size: int = 32
    extract_workers: int = 1
    extract_queue_size: int = 8
    compile_workers: int = 1
    compile_queue_size: int = 4
    mount_path: Path | None = None

    def validate_workspace_path(self) -> None:
        if not self.requires_workspace_binding:
            return
        from backend.domain.volume import validate_workspace_subdirectory, assert_data_dir_matches

        subdirectory = validate_workspace_subdirectory(
            self.workspace_subdirectory, owner_id=self.owner_id, workspace_id=self.workspace_id,
        )
        data = Path(os.path.abspath(self.data_dir))
        if self.mount_path is not None:
            mount = Path(os.path.abspath(self.mount_path))
        elif subdirectory == '.':
            mount = data
        else:
            suffix = Path(subdirectory).parts
            if tuple(data.parts[-len(suffix):]) != suffix:
                raise BadRequest('Data directory does not match its workspace subdirectory', code='invalid_configuration')
            mount = data
            for _ in suffix:
                mount = mount.parent
        assert_data_dir_matches(str(mount), subdirectory, str(data))
        current = data
        while True:
            if current.is_symlink():
                raise BadRequest('Workspace paths cannot contain symbolic links', code='invalid_configuration')
            if current == current.parent:
                break
            current = current.parent

    # -- derived ---------------------------------------------------------
    @property
    def business_db_path(self) -> Path:
        return Path(self.data_dir) / "business.sqlite3"

    @property
    def sources_dir(self) -> Path:
        return Path(self.data_dir) / "sources"

    @property
    def artifacts_dir(self) -> Path:
        return Path(self.data_dir) / "artifacts"

    @property
    def storage_marker_path(self) -> Path:
        return Path(self.data_dir) / STORAGE_MARKER_NAME

    @property
    def requires_workspace_binding(self) -> bool:
        return self.persistent_workspace or self.run_mode == "managed"

    def session_db_path(self) -> Path | None:
        if self.session_data_dir is None:
            return None
        return Path(self.session_data_dir) / "sessions.sqlite3"

    def resolve(self, name: str) -> str:
        """Resolve a path for logs/messages without inventing directories."""
        return os.fspath(name)

    # -- construction ----------------------------------------------------
    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "HostConfig":
        env: Mapping[str, str] = os.environ if environ is None else environ

        persistent = _flag(env, "TUTORLM_PERSISTENT_WORKSPACE", False)
        run_mode = (_optional(env, "TUTORLM_RUN_MODE") or ("managed" if persistent else "demo")).lower()
        if run_mode not in RUN_MODES:
            raise BadRequest(
                "TUTORLM_RUN_MODE must be one of local/demo/managed",
                code="invalid_configuration",
            )

        data_dir = Path(_optional(env, "TUTORLM_DATA_DIR") or "/data")
        session_dir = _optional(env, "TUTORLM_SESSION_DATA_DIR")

        provider_name = (_optional(env, "TUTORLM_PROVIDER") or "fake").lower()
        if provider_name not in PROVIDERS:
            raise BadRequest("TUTORLM_PROVIDER must be fake, openai_compatible or gemini", code="invalid_configuration")

        provider = ProviderConfig(
            name=provider_name,
            model=_optional(env, "TUTORLM_MODEL_NAME") or "offline-demo",
            api_base=_optional(env, "TUTORLM_MODEL_API_BASE"),
            max_tokens=_int(env, "TUTORLM_MODEL_MAX_TOKENS", 512),
            timeout_seconds=_float(env, "TUTORLM_MODEL_TIMEOUT_SECONDS", 30.0),
            has_api_key=bool(_optional(env, "TUTORLM_MODEL_API_KEY")),
            api_key=_optional(env, 'TUTORLM_MODEL_API_KEY') or '',
        )

        limits = Limits(
            max_documents_per_session=_int(env, "TUTORLM_MAX_DOCUMENTS_PER_SESSION", 50),
            max_bytes_per_file=_int(env, "TUTORLM_MAX_BYTES_PER_FILE", 2 * 1024 * 1024),
            max_runs_per_session=_int(env, "TUTORLM_MAX_RUNS_PER_SESSION", 20),
        )

        config = cls(
            run_mode=run_mode,
            data_dir=data_dir,
            session_data_dir=Path(session_dir) if session_dir else None,
            workspace_id=_uuid_or_none(env, "TUTORLM_WORKSPACE_ID"),
            owner_id=_uuid_or_none(env, "TUTORLM_OWNER_ID"),
            workspace_subdirectory=_optional(env, "TUTORLM_WORKSPACE_SUBDIRECTORY") or ".",
            persistent_workspace=persistent,
            allow_implicit_session=_flag(env, "TUTORLM_ALLOW_IMPLICIT_SESSION", False),
            invite_code=_optional(env, "TUTORLM_INVITE_CODE"),
            workspace_source_id=_uuid_or_none(env, "TUTORLM_WORKSPACE_SOURCE_ID"),
            workspace_copy_id=_uuid_or_none(env, "TUTORLM_WORKSPACE_COPY_ID"),
            session_ttl_seconds=_int(env, "TUTORLM_SESSION_TTL_SECONDS", 3600),
            provider=provider,
            limits=limits,
            enabled_plugins=tuple(value.strip() for value in (env.get('TUTORLM_ENABLED_PLUGINS') or 'documents,explanations,knowledge-narration').split(',')),
            tex_compiler_available=_flag(env, "TUTORLM_TEX_COMPILER_AVAILABLE", False),
            run_workers=_int(env, "TUTORLM_RUN_WORKERS", 2),
            run_queue_size=_int(env, "TUTORLM_RUN_QUEUE_SIZE", 32),
            extract_workers=_int(env, "TUTORLM_EXTRACT_WORKERS", 1),
            extract_queue_size=_int(env, "TUTORLM_EXTRACT_QUEUE_SIZE", 8),
            mount_path=Path(env['TUTORLM_MOUNT_PATH']) if env.get('TUTORLM_MOUNT_PATH') else None,
        )
        config.validate_workspace_path()
        return config

    # -- storage layout --------------------------------------------------
    def declared_storage_layout(self, *, data_dir_writable: bool) -> str | None:
        """Return `content_only` only when isolation is actually verified.

        Anything we cannot prove (legacy mount root, missing session dir, a
        session/credential subtree inside the volume) stays undeclared, so the
        control plane records `legacy_root` and refuses to use it as a share
        source (docs/instance-app-design.md section 4.4).
        """
        if not self.persistent_workspace or not data_dir_writable:
            return None
        if self.session_data_dir is None:
            return None
        try:
            data = Path(self.data_dir).resolve()
            session = Path(self.session_data_dir).resolve()
        except OSError:
            return None
        if session == data or data in session.parents:
            # Session storage inside the volume would be copied on share/clone.
            return None
        try:
            if not session.is_dir():
                return None
        except OSError:
            return None
        if self._contains_session_subtrees(data):
            return None
        return "content_only"

    @staticmethod
    def _contains_session_subtrees(data_dir: Path) -> bool:
        for name in SESSION_SUBTREE_NAMES:
            candidate = data_dir / name
            try:
                if candidate.exists():
                    return True
            except OSError:
                return True
        return False

    # -- workspace binding ----------------------------------------------
    def require_workspace(self) -> str:
        if not self.workspace_id:
            raise BadRequest(
                "This instance has no bound workspace; TUTORLM_WORKSPACE_ID is missing",
                code="workspace_unbound",
            )
        return self.workspace_id

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def probe_writable(path: Path) -> tuple[bool, str | None]:
        """Non-destructive writability probe of the data root.

        Never falls back to writing into the container layer or /tmp: an
        unwritable mount must fail readiness with an actionable reason.
        """
        import errno

        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return False, f"data root is not creatable: {exc.strerror or exc.__class__.__name__}"
        try:
            with tempfile.NamedTemporaryFile(dir=str(path), prefix=".tutorlm-probe-", delete=True):
                pass
        except OSError as exc:
            reason = exc.strerror or exc.__class__.__name__
            if exc.errno in (errno.EACCES, errno.EPERM):
                return False, f"data root is not writable ({reason}); check volume ownership expectations"
            if exc.errno == errno.EROFS:
                return False, "data root is mounted read-only"
            return False, f"data root is not writable: {reason}"
        return True, None


__all__ = [
    "HostConfig",
    "Limits",
    "ProviderConfig",
    "SESSION_SUBTREE_NAMES",
    "STORAGE_MARKER_NAME",
]
