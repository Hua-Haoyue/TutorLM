"""Host-facing service facade used by ``adapters/http``.

Why this exists: the foundation workstream owns ``domain`` and ``application``
(pure models, use cases, ports), and the HTTP layer must not talk to SQLite rows
directly. This module is the composition seam -- it turns repository rows into
the exact JSON envelopes the frontend expects (``docs/instance-app-api.md``) and
enforces the Host-level rules (workspace binding, quotas, immutable revisions,
cancel-aware runs).

Design constraints obeyed here:

* no Docker/control-plane imports; no client-supplied owner/workspace is trusted;
* content only ever read from ``TUTORLM_DATA_DIR``; sessions only from
  ``TUTORLM_SESSION_DATA_DIR``;
* ``document/content`` returns text **only** for md/txt -- pdf/tex binaries are
  served exclusively by ``/source`` (Range/HEAD);
* every write path re-checks the workspace-bound project before mutating.

Replacement path: when ``infrastructure/sqlite`` repositories and
``application/services`` land, ``AppServices`` is constructed with those instead;
the HTTP routers do not change.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from backend.application.format.errors import (
    InvalidSourceRef,
    PdfEncrypted as FormatPdfEncrypted,
    PdfMalformed as FormatPdfMalformed,
    TexMainInvalid as FormatTexMainInvalid,
    TexProjectRejected as FormatTexProjectRejected,
)
from backend.application.format.selectors import (
    PdfSelector,
    SourceRef,
    TexSourceSelector,
    TextSelector,
    WholeDocumentSelector,
    normalize_to_codepoint,
)
from backend.domain.enums import TEXT_FORMATS, DocumentStatus, RevisionStatus
from backend.domain.errors import DomainError, UnsupportedMedia as DomainUnsupportedMedia
from backend.domain.text import decode_utf8_strict

from . import errors as app_errors
from .config import HostConfig
from .local.provider import FakeProvider
from .local.sessions import SessionStore
from .local.store import (
    LocalStorage,
    LocalStore,
    PathEscape,
    content_hash,
    new_id as store_new_id,
    safe_relative_path,
    sanitize_filename,
)
from .plugins import PluginRegistry
from .runs import CancelledRun, RunContext, RunDraft, RunManager, WORKLOAD_EXTRACT
#: Identity used by a local/demo instance that has no injected workspace UUID.
#: It is a server-side constant, never taken from a request.
LOCAL_WORKSPACE_ID = "local"

#: Extension -> canonical stored filename. Server-generated, never user text.
_MAIN_FILENAME = {
    "md": "main.md",
    "txt": "main.txt",
    "pdf": "main.pdf",
    "tex": "main.tex",
}

#: Media types used for ``/source`` responses (never used to fake text content).
_MEDIA_TYPES = {
    "md": "text/markdown; charset=utf-8",
    "txt": "text/plain; charset=utf-8",
    "pdf": "application/pdf",
    "tex": "application/x-tex",
}


def _iso(epoch: Any) -> str | None:
    """Render a stored epoch as an ISO-8601 UTC string (frontend contract)."""
    if epoch in (None, ""):
        return None
    try:
        value = float(epoch)
    except (TypeError, ValueError):
        return str(epoch)
    from datetime import datetime, timezone

    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _rows_to_iso(value: Any) -> str:
    if isinstance(value, (int, float)):
        return _iso(value) or ""
    return str(value or "")


def _json_or(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except ValueError:
        return fallback


def _validate_medium(probe: Any, payload: bytes, *, main: str | None) -> dict[str, Any]:
    """Validate a probed upload with the real format adapter (no side effects).

    The media probe only *classifies* the bytes; a payload that claims to be a
    PDF is then structurally inspected and a TeX project/zip is checked with the
    safe-extraction rules. This is what keeps a malformed PDF (-> 422) or an
    encrypted PDF (-> 409) from ever landing as a ``ready`` document, and stops
    a tex zip that escapes the documented limits from being stored. No compiler
    is invoked and nothing is written here.

    Returns the extra metadata to persist on the document (``main`` for TeX,
    ``is_zip_project`` for zip projects) -- server-derived, never client text.
    """
    from backend.application.format.media import PDF, TEX

    fmt = str(getattr(probe, "format", ""))
    if fmt == PDF:
        from backend.infrastructure.format.pdf import open_pdf

        try:
            open_pdf(payload)
        except FormatPdfMalformed as exc:
            raise app_errors.PdfMalformed(str(exc) or "Malformed PDF file") from exc
        except FormatPdfEncrypted as exc:
            raise app_errors.PdfEncrypted(str(exc) or "Encrypted PDF files are not supported") from exc
        return {}

    if fmt == TEX:
        from backend.infrastructure.format.tex_project import validate_tex_payload

        try:
            project, _payloads = validate_tex_payload(payload, main=main)
        except FormatTexMainInvalid as exc:
            raise app_errors.TexMainInvalid(str(exc) or "TeX project entry file is missing") from exc
        except FormatTexProjectRejected as exc:
            # A rejected project is unprocessable content, not a bad request:
            # the safe-extraction rules (traversal, bomb, extension, ...) failed.
            raise app_errors.Unprocessable(
                str(exc) or "TeX project failed validation", code=getattr(exc, "code", "tex_project_rejected")
            ) from exc
        except DomainError as exc:
            raise app_errors.Unprocessable(str(exc)) from exc
        extra: dict[str, Any] = {"main": project.main}
        if bool(getattr(probe, "is_zip_project", False)):
            extra["is_zip_project"] = True
        return extra

    # md/txt: the probe already required strict UTF-8 decoding.
    return {}




class _TexResourceMissing(Exception):
    def __init__(self, logical_path):
        super().__init__(logical_path)
        self.logical_path = logical_path


@dataclass(frozen=True)
class SourcePayload:
    """Streaming descriptor for ``GET/HEAD /api/documents/{id}/source``."""

    path: Path
    size_bytes: int
    etag: str
    media_type: str
    filename: str


class AppServices:
    """One workspace-bound service object; every router call goes through it."""

    def __init__(
        self,
        config: HostConfig,
        *,
        store: LocalStore,
        storage: LocalStorage,
        sessions: SessionStore,
        runs: RunManager,
        plugins: PluginRegistry,
        provider: FakeProvider | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.storage = storage
        self.sessions = sessions
        self.runs = runs
        self.plugins = plugins
        self.provider = provider or FakeProvider(config.provider.model)
        self._register_handlers()

    # -- identity --------------------------------------------------------
    @property
    def workspace_id(self) -> str:
        """The env-bound workspace.

        Persistent/managed instances *must* be bound, so a missing id is an error
        there (fail closed). A local/demo instance has no injected workspace, so it
        uses the same stable local identity the session store issues -- never a
        client-supplied value.
        """
        if self.config.requires_workspace_binding:
            return self.config.require_workspace()
        return self.config.workspace_id or LOCAL_WORKSPACE_ID

    def current_project(self) -> dict[str, Any]:
        """Return the workspace-bound project, creating it once if needed.

        One container serves exactly one workspace, so this is the only project
        the API ever exposes (``docs/instance-app-api.md`` section 3).
        """
        project = self.store.get_current_project(self.workspace_id)
        if project is None:
            project = self.store.create_project(self.workspace_id, "我的工作区")
        return project

    def require_project(self, project_id: str | None) -> dict[str, Any]:
        """Cross-project access is impossible by construction; verify anyway."""
        project = self.current_project()
        if project_id and str(project_id) != project["id"]:
            raise app_errors.NotFound("Project not found in this workspace", code="project_not_found")
        return project

    # -- serialization ---------------------------------------------------
    def project_public(self, row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "title": row["title"],
            "description": row.get("description"),
            "workspace_id": row["workspace_id"],
            "created_at": _rows_to_iso(row.get("created_at")),
            "updated_at": _rows_to_iso(row.get("updated_at")),
        }

    def document_public(self, row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "title": row["title"],
            "source_filename": row.get("source_filename"),
            "format": row.get("format"),
            "status": row.get("status"),
            "current_revision_id": row.get("current_revision_id"),
            "size_bytes": int(row.get("size_bytes") or 0),
            "order_index": int(row.get("order_index") or 0),
            "created_at": _rows_to_iso(row.get("created_at")),
            "updated_at": _rows_to_iso(row.get("updated_at")),
        }

    def revision_public(self, row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "document_id": row["document_id"],
            "revision_no": int(row["revision_no"]),
            "content_hash": row["content_hash"],
            "size_bytes": int(row["size_bytes"]),
            "status": row["status"],
            "created_at": _rows_to_iso(row.get("created_at")),
        }

    def node_public(self, row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "document_id": row.get("document_id"),
            "title": row["title"],
            "kind": row["kind"],
            "summary": row.get("summary"),
            "content": row.get("content"),
            "order_index": int(row.get("order_index") or 0),
            "parent_id": row.get("parent_id"),
            "metadata": _json_or(row.get("metadata_json"), {}),
            "revision": int(row.get("revision") or 1),
            "created_at": _rows_to_iso(row.get("created_at")),
            "updated_at": _rows_to_iso(row.get("updated_at")),
        }

    def edge_public(self, row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "source_node_id": row["source_node_id"],
            "target_node_id": row["target_node_id"],
            "relation": row["relation"],
            "metadata": _json_or(row.get("metadata_json"), {}),
            "created_at": _rows_to_iso(row.get("created_at")),
        }

    def explanation_public(self, row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "node_id": row["node_id"],
            "question": row.get("question"),
            "answer": row["answer"],
            "provider": row["provider"],
            "model": row["model"],
            "created_at": _rows_to_iso(row.get("created_at")),
        }

    def narration_public(self, row: Mapping[str, Any]) -> dict[str, Any]:
        segments = _json_or(row.get("segments"), [])
        return {
            "id": row["id"],
            "title": row["title"],
            "segments": segments,
            "segment_count": int(row.get("segment_count") or len(segments)),
            "total_duration_seconds": float(row.get("total_duration_seconds") or 0.0),
            "provider": row["provider"],
            "model": row["model"],
            "created_at": _rows_to_iso(row.get("created_at")),
        }

    def extraction_public(self, row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "revision_id": row["revision_id"],
            "extractor_version": row["extractor_version"],
            "status": row["status"],
            "text_hash": row.get("text_hash"),
            "page_count": row.get("page_count"),
            "error_code": row.get("error_code"),
            "created_at": _rows_to_iso(row.get("created_at")),
        }

    def artifact_public(self, row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "document_id": row["document_id"],
            "revision_id": row["revision_id"],
            "build_id": row.get("build_id"),
            "kind": row["kind"],
            "content_hash": row.get("content_hash"),
            "size_bytes": int(row.get("size_bytes") or 0),
            "created_at": _rows_to_iso(row.get("created_at")),
        }

    def build_public(self, row: Mapping[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": row["id"],
            "revision_id": row["revision_id"],
            "status": row["status"],
            "engine_version": row.get("engine_version"),
            "source_map": _json_or(row.get("source_map_json"), None),
            "created_at": _rows_to_iso(row.get("created_at")),
            "updated_at": _rows_to_iso(row.get("updated_at")),
        }
        if row.get("artifact_id"):
            payload["artifact_id"] = row["artifact_id"]
        if row.get("error_code"):
            payload["error"] = {"code": row["error_code"], "message": row.get("error_message") or ""}
        return payload

    def run_public(self, row: Mapping[str, Any]) -> dict[str, Any]:
        state = row.get("state")
        payload: dict[str, Any] = {
            "id": row["id"],
            "kind": row["kind"],
            "state": state,
            "created_at": _rows_to_iso(row.get("created_at")),
            "updated_at": _rows_to_iso(row.get("updated_at")),
        }
        if row.get("progress") is not None:
            payload["progress"] = row["progress"]
        input_payload = _json_or(row.get("input_json"), {})
        if input_payload:
            payload["input"] = input_payload
        result = _json_or(row.get("result_json"), None)
        if state == "completed" and result is not None:
            payload["result"] = result
        if row.get("error_code"):
            payload["error"] = {"code": row["error_code"], "message": row.get("error_message") or ""}
        return payload
    # -- runtime discovery ------------------------------------------------
    def runtime_public(self) -> dict[str, Any]:
        return {
            "mode": self.config.run_mode,
            "provider": {"name": self.config.provider.name, "model": self.config.provider.model},
            "plugins": self.plugins.public(),
            "limits": self.config.limits.public(),
        }

    # -- documents --------------------------------------------------------
    def list_documents(self, project_id: str) -> list[dict[str, Any]]:
        project = self.require_project(project_id)
        return [self.document_public(row) for row in self.store.list_documents(project["id"])]

    def get_document(self, document_id: str) -> dict[str, Any]:
        return self.document_public(self.require_document(document_id))

    def require_document(self, document_id: str) -> dict[str, Any]:
        row = self.store.get_document(document_id)
        if row is None:
            raise app_errors.DocumentNotFound()
        # A document from another workspace cannot exist in this volume, but the
        # project link is still verified so a stale id can never leak rows.
        project = self.current_project()
        if row["project_id"] != project["id"]:
            raise app_errors.DocumentNotFound()
        return row

    def require_revision(self, document: Mapping[str, Any], revision_id: str | None) -> dict[str, Any]:
        target = revision_id or document.get("current_revision_id")
        if not target:
            raise app_errors.RevisionGone("Document has no revision yet")
        row = self.store.get_revision(str(target))
        if row is None or row["document_id"] != document["id"]:
            # Never silently re-point a stale reference at the newest revision.
            raise app_errors.RevisionGone("Revision no longer exists for this document")
        return row

    def create_text_document(
        self,
        *,
        project_id: str,
        title: str,
        text: str,
        derive_nodes: bool = False,
        fmt: str = "md",
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        project = self.require_project(project_id)
        self._assert_document_quota(project["id"])
        if fmt not in TEXT_FORMATS:
            raise app_errors.UnsupportedMedia("Text creation only supports md/txt")
        payload = text.encode("utf-8")
        self._assert_file_size(payload)
        return self._store_new_document(
            project_id=project["id"],
            title=title or "未命名文档",
            fmt=fmt,
            source_filename=None,
            payload=payload,
            resources=(),
            status=DocumentStatus.READY.value,
        )

    def upload_document(
        self,
        *,
        project_id: str,
        filename: str,
        payload: bytes,
        declared_mime: str | None,
        main: str | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Probe media, validate the format, store an immutable original.

        The stored format is decided from signatures, never from a client
        declaration, and the payload is then checked by the *real* format
        adapter: a PDF is structurally inspected (malformed -> 422, encrypted ->
        409) and a TeX project/zip is validated with the safe-extraction rules
        (no compiler is invoked). The declared media type alone is never trusted.
        """
        from backend.application.format.media import detect  # foundation port

        project = self.require_project(project_id)
        self._assert_document_quota(project["id"])
        self._assert_file_size(payload)

        try:
            probe = detect(payload, filename=filename, declared_mime=declared_mime)
        except DomainUnsupportedMedia as exc:
            raise app_errors.UnsupportedMedia(str(exc)) from exc

        extra = _validate_medium(probe, payload, main=main)

        title = sanitize_filename(filename, "未命名文档").rsplit(".", 1)[0] or "未命名文档"
        return self._store_new_document(
            project_id=project["id"],
            title=title,
            fmt=probe.format,
            source_filename=sanitize_filename(filename, "upload.bin"),
            payload=payload,
            resources=(),
            status=DocumentStatus.READY.value,
            extra=extra,
        )

    def add_revision(
        self,
        *,
        document_id: str,
        filename: str,
        payload: bytes,
        declared_mime: str | None,
        main: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Append an immutable revision; the previous original is never touched.

        The replacement is validated exactly like an upload (not just probed):
        a malformed/encrypted PDF or an invalid TeX project is rejected here too,
        so a revision can never carry a format the reader cannot serve.
        """
        from backend.application.format.media import detect

        document = self.require_document(document_id)
        self._assert_file_size(payload)
        try:
            probe = detect(payload, filename=filename, declared_mime=declared_mime)
        except DomainUnsupportedMedia as exc:
            raise app_errors.UnsupportedMedia(str(exc)) from exc

        extra = _validate_medium(probe, payload, main=main)

        revision_no = self.store.next_revision_no(document["id"])
        revision_id = store_new_id()
        relative = _MAIN_FILENAME.get(probe.format, "main.bin")
        rel_path = self.storage.write_revision_file(
            document["id"],
            revision_id,
            relative_path=relative,
            payload=payload,
        )
        revision = {
            "id": revision_id,
            "revision_no": revision_no,
            "content_hash": content_hash(payload),
            "size_bytes": len(payload),
            "status": RevisionStatus.READY.value,
            "main_rel_path": rel_path,
            "resources": [],
        }
        updated, created = self.store.add_revision(
            document_id=document["id"],
            revision=revision,
            size_bytes=len(payload),
            status=DocumentStatus.READY.value,
            fmt=probe.format,
            source_filename=sanitize_filename(filename, "upload.bin"),
            extra=extra or None,
        )
        return updated, created

    def update_document(self, document_id: str, *, title: str | None) -> dict[str, Any]:
        self.require_document(document_id)
        fields = {}
        if title is not None:
            fields["title"] = title
        row = self.store.update_document(document_id, **fields) if fields else self.require_document(document_id)
        return self.document_public(row)

    def delete_document(self, document_id: str) -> None:
        self.require_document(document_id)
        self.store.delete_document(document_id)
        # Files are removed after the rows commit; artifacts are regenerable, but
        # removing them together keeps the volume free of orphaned binaries.
        self.storage.delete_document_files(document_id)

    def list_revisions(self, document_id: str) -> list[dict[str, Any]]:
        document = self.require_document(document_id)
        return [self.revision_public(row) for row in self.store.list_revisions(document["id"])]

    def read_text_content(self, document_id: str) -> str:
        """Full text for md/txt only; binaries are never disguised as utf-8."""
        document = self.require_document(document_id)
        if document["format"] not in TEXT_FORMATS:
            raise app_errors.ContentNotAvailable(
                "Full text is only available for text documents; use /text, /extractions or /source"
            )
        revision = self.require_revision(document, None)
        raw = self.storage.read_source(document["id"], revision)
        try:
            return decode_utf8_strict(raw)
        except DomainError as exc:
            raise app_errors.EncodingUnsupported(str(exc)) from exc

    def source_payload(self, document_id: str, revision_id: str | None) -> SourcePayload:
        """Descriptor for the original bytes (md/txt/pdf/tex or tex zip)."""
        document = self.require_document(document_id)
        revision = self.require_revision(document, revision_id)
        path = self.storage.source_path(document["id"], revision)
        if path.is_symlink() or not path.is_file():
            raise app_errors.NotFound("Source file is missing", code="source_missing")
        size = path.stat().st_size
        digest = str(revision.get("content_hash") or "")
        return SourcePayload(
            path=path,
            size_bytes=size,
            etag=_etag(digest),
            media_type=_MEDIA_TYPES.get(str(document["format"]), "application/octet-stream"),
            filename=str(document.get("source_filename") or path.name),
        )

    # -- artifacts / extractions / builds ---------------------------------
    def list_artifacts(self, document_id: str, revision_id: str | None) -> list[dict[str, Any]]:
        document = self.require_document(document_id)
        return [
            self.artifact_public(row)
            for row in self.store.list_artifacts(document_id=document["id"], revision_id=revision_id)
        ]

    def get_artifact(self, document_id: str, artifact_id: str) -> dict[str, Any]:
        document = self.require_document(document_id)
        row = self.store.get_artifact(artifact_id)
        if row is None or row["document_id"] != document["id"]:
            raise app_errors.NotFound("Artifact not found", code="artifact_not_found")
        return self.artifact_public(row)

    def artifact_source(self, document_id: str, artifact_id: str) -> SourcePayload:
        document = self.require_document(document_id)
        row = self.store.get_artifact(artifact_id)
        if row is None or row["document_id"] != document["id"]:
            raise app_errors.NotFound("Artifact not found", code="artifact_not_found")
        path = self.storage.artifact_path(row)
        if path.is_symlink() or not path.is_file():
            raise app_errors.NotFound("Artifact file is missing", code="artifact_missing")
        media_type = "application/pdf" if row["kind"] == "compiled_pdf" else "text/plain; charset=utf-8"
        return SourcePayload(
            path=path,
            size_bytes=path.stat().st_size,
            etag=_etag(str(row.get("content_hash") or "")),
            media_type=media_type,
            filename=path.name,
        )

    def list_extractions(
        self, document_id: str, *, revision_id: str | None, extractor_version: str | None
    ) -> list[dict[str, Any]]:
        document = self.require_document(document_id)
        target = revision_id or document.get("current_revision_id")
        rows = self.store.list_extractions(
            document_id=document["id"],
            revision_id=str(target) if target else None,
            extractor_version=extractor_version,
        )
        return [self.extraction_public(row) for row in rows]

    def structured_text(
        self, document_id: str, *, revision_id: str | None, extractor_version: str | None
    ) -> dict[str, Any]:
        """Structured text for ``GET /api/documents/{id}/text`` (docs section 4b).

        Never returns a binary payload and never fabricates page offsets: when a
        ready extraction exists its stored text is served with the extractor
        version that produced it; a text document with no extraction yet falls
        back to its immutable original. A binary document without a ready
        extraction is reported as unavailable rather than guessed at.
        """
        document = self.require_document(document_id)
        revision = self.require_revision(document, revision_id)
        target_version = extractor_version or self.config.extractor_version

        rows = self.store.list_extractions(
            document_id=document["id"],
            revision_id=str(revision["id"]),
            extractor_version=extractor_version,
        )
        ready = next((row for row in rows if row.get("status") == "ready" and row.get("artifact_id")), None)

        payload: dict[str, Any] = {
            "revision_id": revision["id"],
            "extractor_version": str(ready["extractor_version"]) if ready else target_version,
        }
        if ready is not None:
            artifact = self.store.get_artifact(str(ready["artifact_id"]))
            if artifact is None:
                raise app_errors.NotFound("Extraction artifact is missing", code="artifact_missing")
            try:
                payload["text"] = decode_utf8_strict(self.storage.read_artifact(artifact))
            except DomainError as exc:
                raise app_errors.EncodingUnsupported(str(exc)) from exc
            if ready.get("page_count"):
                # Only reported when a real extractor produced page granularity.
                payload["page_count"] = int(ready["page_count"])
            return payload

        if str(document["format"]) not in TEXT_FORMATS:
            raise app_errors.ContentNotAvailable(
                "Structured text is not available yet; run POST /api/documents/{id}/extract first"
            )
        try:
            payload["text"] = decode_utf8_strict(self.storage.read_source(document["id"], revision))
        except DomainError as exc:
            raise app_errors.EncodingUnsupported(str(exc)) from exc
        return payload

    def list_builds(self, document_id: str, *, revision_id: str | None) -> list[dict[str, Any]]:
        document = self.require_document(document_id)
        return [
            self.build_public(row)
            for row in self.store.list_builds(document_id=document["id"], revision_id=revision_id)
        ]

    def get_build(self, document_id: str, build_id: str) -> dict[str, Any]:
        document = self.require_document(document_id)
        row = self.store.get_build(build_id)
        if row is None or row["document_id"] != document["id"]:
            raise app_errors.NotFound("Build not found", code="build_not_found")
        return self.build_public(row)

    # -- nodes / edges ----------------------------------------------------
    def list_nodes(self, project_id: str) -> list[dict[str, Any]]:
        project = self.require_project(project_id)
        return [self.node_public(row) for row in self.store.list_nodes(project["id"])]

    def _validated_node_metadata(
        self, metadata: Mapping[str, Any] | None, *, document_id: str | None = None
    ) -> dict[str, Any]:
        """Validate a node's metadata before it is written.

        A unified ``SourceRef`` anchor (``metadata.source``) is a *reference*, not
        an authorization: the referenced ``document_id``/``revision_id`` must
        belong to this workspace-bound project, and the selector must parse under
        the shared rules. A ref that points at another workspace, a deleted
        revision or a structurally invalid selector is rejected here rather than
        stored, so a node can never smuggle a cross-workspace read primitive
        (``docs/document-readers.md`` section 2, ``docs/instance-app-api.md``
        section 6). Unrelated metadata keys pass through untouched.
        """
        payload = dict(metadata or {})
        source = payload.get("source")
        # A *full* SourceRef carries both document_id and revision_id. A legacy
        # or partial anchor (e.g. only a ``quote``) is not a resolvable reference
        # and is stored as-is; only a full ref is validated against the workspace.
        looks_like_ref = isinstance(source, Mapping) and (
            source.get("document_id") is not None or source.get("revision_id") is not None
        )
        if looks_like_ref:
            try:
                ref = SourceRef.parse(dict(source))
            except InvalidSourceRef as exc:
                raise app_errors.Unprocessable(
                    "Node source reference is invalid", code="invalid_source_ref"
                ) from exc
            document = self.store.get_document(ref.document_id)
            project = self.current_project()
            if document is None or document["project_id"] != project["id"]:
                # Cross-workspace or unknown document: never resolvable here.
                raise app_errors.Unprocessable(
                    "Node source reference points outside this workspace",
                    code="invalid_source_ref",
                    details={"document_id": ref.document_id},
                )
            revision = self.store.get_revision(ref.revision_id)
            if revision is None or revision["document_id"] != document["id"]:
                raise app_errors.Unprocessable(
                    "Node source reference names a missing revision",
                    code="invalid_source_ref",
                    details={"revision_id": ref.revision_id},
                )
            # Normalize to the canonical shape before storage (codepoint units,
            # canonical selector dict) so stored anchors are always consistent.
            payload["source"] = ref.to_dict()
        return payload

    def create_node(
        self,
        *,
        project_id: str,
        document_id: str | None,
        title: str,
        kind: str,
        content: str | None,
        parent_id: str | None,
        metadata: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        project = self.require_project(project_id)
        if document_id:
            self.require_document(document_id)
        safe_metadata = self._validated_node_metadata(metadata, document_id=document_id)
        row = self.store.create_node(
            project_id=project["id"],
            document_id=document_id,
            title=title,
            kind=kind,
            content=content,
            summary=None,
            parent_id=parent_id,
            metadata=safe_metadata,
        )
        return self.node_public(row)

    def update_node(
        self,
        node_id: str,
        *,
        title: str | None = None,
        kind: str | None = None,
        content: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        revision: int | None = None,
    ) -> dict[str, Any]:
        self.require_node(node_id)
        fields: dict[str, Any] = {}
        if title is not None:
            fields["title"] = title
        if kind is not None:
            fields["kind"] = kind
        if content is not None:
            fields["content"] = content
        if metadata is not None:
            safe_metadata = self._validated_node_metadata(metadata)
            fields["metadata_json"] = json.dumps(safe_metadata, ensure_ascii=False)
        row = self.store.update_node(node_id, expected_revision=revision, **fields)
        return self.node_public(row)

    def require_node(self, node_id: str) -> dict[str, Any]:
        row = self.store.get_node(node_id)
        if row is None:
            raise app_errors.NodeNotFound()
        project = self.current_project()
        if row["project_id"] != project["id"]:
            raise app_errors.NodeNotFound()
        return row

    def delete_node(self, node_id: str) -> None:
        self.require_node(node_id)
        self.store.delete_node(node_id)

    def graph(self, project_id: str) -> dict[str, list[dict[str, Any]]]:
        project = self.require_project(project_id)
        return {
            "nodes": [self.node_public(row) for row in self.store.list_nodes(project["id"])],
            "edges": [self.edge_public(row) for row in self.store.list_edges(project["id"])],
        }

    def create_edge(
        self,
        *,
        project_id: str,
        source_node_id: str,
        target_node_id: str,
        relation: str,
        metadata: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        project = self.require_project(project_id)
        row = self.store.create_edge(
            project_id=project["id"],
            source_node_id=source_node_id,
            target_node_id=target_node_id,
            relation=relation,
            metadata=dict(metadata or {}),
        )
        return self.edge_public(row)

    def delete_edge(self, project_id: str, edge_id: str) -> None:
        project = self.require_project(project_id)
        self.store.delete_edge(project["id"], edge_id)

    # -- explanations / narrations ----------------------------------------
    def list_explanations(self, project_id: str) -> list[dict[str, Any]]:
        project = self.require_project(project_id)
        return [self.explanation_public(row) for row in self.store.list_explanations(project["id"])]

    def list_narrations(self, project_id: str) -> list[dict[str, Any]]:
        project = self.require_project(project_id)
        return [self.narration_public(row) for row in self.store.list_narrations(project["id"])]

    # -- runs -------------------------------------------------------------
    def list_runs(self) -> list[dict[str, Any]]:
        rows = self.store.list_runs(session_id=None, project_id=self.current_project()["id"])
        return [self.run_public(row) for row in rows]

    def get_run(self, run_id: str) -> dict[str, Any]:
        row = self.store.get_run(run_id)
        if row is None:
            raise app_errors.RunNotFound()
        return self.run_public(row)

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        row = self.runs.cancel(run_id)
        return self.run_public(row)


    # -- source references -------------------------------------------------
    def resolve_ref(self, ref, *, max_chars=None):
        """Resolve one unified ``SourceRef`` to text, or explain why it cannot.

        Resolution is verified against the **stored revision** -- it is never
        re-pointed at the newest revision and never fabricated. A missing
        revision, a stale ``artifact_id``/``build_id``, a cross-workspace
        document, a selector the format cannot express, a canonical quote that
        does not match the stored text, or a PDF whose extractor is unavailable
        all come back as ``resolved: false`` with a machine-readable ``reason``
        (``docs/instance-app-api.md`` section 6). The returned ``citation`` is the
        canonical span (codepoint offsets, plus ``file`` for tex-source) computed
        from the stored bytes -- never from a client-supplied offset -- so a
        consumer can both render and re-anchor.

        Per-item reasons are stable and additive: ``rev_gone``, ``unauthorized``,
        ``artifact_stale``, ``invalid_ref``, ``unsupported`` (shape has no text
        mapping -- e.g. a pdf selector with no quote), ``quote_mismatch``,
        ``extraction_required`` (pdf text needs a ready extraction),
        ``extractor_unavailable`` (no pdf backend installed) and ``file_missing``
        (tex-source ``file`` not present in the project). A malformed ref is a
        per-item failure, never a 500 for the batch.
        """
        raw = ref if isinstance(ref, Mapping) else {}
        base = {
            "ref": dict(raw),
            "text": "",
            "citation": {
                "document_id": str(raw.get("document_id") or ""),
                "revision_id": str(raw.get("revision_id") or ""),
            },
            "resolved": False,
        }

        try:
            source_ref = SourceRef.parse(dict(raw))
        except InvalidSourceRef:
            # Structural problems are reported per item so one bad ref cannot
            # fail (or leak) the whole batch.
            return {**base, "reason": "invalid_ref"}

        document = self.store.get_document(source_ref.document_id)
        if document is None:
            return {**base, "reason": "rev_gone"}
        project = self.current_project()
        if document["project_id"] != project["id"]:
            # A ref must never become a cross-workspace read primitive.
            return {**base, "reason": "unauthorized"}

        revision = self.store.get_revision(source_ref.revision_id)
        if revision is None or revision["document_id"] != document["id"]:
            # A stale ref is reported, never silently re-pointed.
            return {**base, "reason": "rev_gone"}

        # A compiled-artifact reference must still describe *this* revision.
        if self._artifact_binding_stale(source_ref, revision):
            return {**base, "reason": "artifact_stale"}

        selector = source_ref.selector
        fmt = str(document["format"])

        if isinstance(selector, PdfSelector):
            return self._resolve_pdf_ref(source_ref, document, revision, base, max_chars=max_chars)
        if fmt == "tex":
            return self._resolve_tex_ref(source_ref, document, revision, base, max_chars=max_chars)
        if fmt not in TEXT_FORMATS:
            return {**base, "reason": "unsupported"}

        try:
            text = decode_utf8_strict(self.storage.read_source(document["id"], revision))
        except (DomainError, OSError):
            return {**base, "reason": "unsupported"}

        span = _normalize_span(selector, text)
        if span is None:
            return {**base, "reason": "unsupported"}
        located = _locate_span(text, span, source_ref)
        if located is None:
            return {**base, "reason": "quote_mismatch"}
        return self._resolved(base, text, located[0], located[1], max_chars=max_chars)

    # -- resolve helpers --------------------------------------------------
    def _artifact_binding_stale(self, source_ref: "SourceRef", revision: Mapping[str, Any]) -> bool:
        """True when a ``build_id``/``artifact_id`` binding no longer matches."""
        build_id = source_ref.build_id
        artifact_id = source_ref.artifact_id
        if build_id:
            build = self.store.get_build(str(build_id))
            if build is None or build["revision_id"] != revision["id"]:
                return True
            if artifact_id and str(build.get("artifact_id") or "") != str(artifact_id):
                return True
            return False
        if artifact_id:
            artifact = self.store.get_artifact(str(artifact_id))
            if artifact is None or artifact["revision_id"] != revision["id"]:
                return True
        return False

    def _resolve_pdf_ref(
        self,
        source_ref: "SourceRef",
        document: Mapping[str, Any],
        revision: Mapping[str, Any],
        base: dict[str, Any],
        *,
        max_chars: int | None,
    ) -> dict[str, Any]:
        """PDF text comes only from a **ready extraction** -- never raw bytes."""
        ready = next(
            (
                row
                for row in self.store.list_extractions(
                    document_id=document["id"], revision_id=str(revision["id"])
                )
                if row.get("status") == "ready" and row.get("artifact_id")
            ),
            None,
        )
        if ready is not None:
            # ``revision_id + extractor_version`` is the reference identity; a
            # version that no longer matches invalidates the anchor.
            if source_ref.extractor_version and str(source_ref.extractor_version) != str(
                ready["extractor_version"]
            ):
                return {**base, "reason": "extraction_required"}
            artifact = self.store.get_artifact(str(ready["artifact_id"]))
            if artifact is None:
                return {**base, "reason": "unsupported"}
            try:
                text = decode_utf8_strict(self.storage.read_artifact(artifact))
            except (DomainError, OSError):
                return {**base, "reason": "unsupported"}
            if not source_ref.quote:
                # A pdf selector with no canonical quote cannot be mapped to an
                # offset without trusting the reader's ordering -- refuse.
                return {**base, "reason": "unsupported"}
            located = _search_anchor(text, source_ref.quote, source_ref.prefix, source_ref.suffix)
            if located is None:
                return {**base, "reason": "quote_mismatch"}
            citation = {**base["citation"], "span": {"start": located[0], "end": located[1]}}
            return self._resolved(
                base, text, located[0], located[1], max_chars=max_chars, citation=citation
            )

        # No ready text: refuse rather than fabricate. Distinguish a missing
        # extractor backend from "extraction has not been run yet".
        from backend.infrastructure.format.pdf import StructuralPdfExtractor

        try:
            extractor_available = StructuralPdfExtractor().available()
        except Exception:  # pragma: no cover - defensive
            extractor_available = False
        reason = "extraction_required" if extractor_available else "extractor_unavailable"
        return {**base, "reason": reason}

    def _resolve_tex_ref(
        self,
        source_ref: "SourceRef",
        document: Mapping[str, Any],
        revision: Mapping[str, Any],
        base: dict[str, Any],
        *,
        max_chars: int | None,
    ) -> dict[str, Any]:
        """Resolve a ``tex-source`` (or whole-document) ref inside the project.

        The project is re-validated with the same safe-extraction rules used at
        upload (no compiler is invoked); a ``file`` that is not part of the
        project is reported as ``file_missing`` rather than guessed at.
        """
        from backend.infrastructure.format.tex_project import validate_tex_payload

        raw_bytes = self.storage.read_source(document["id"], revision)
        try:
            project, payloads = validate_tex_payload(raw_bytes, main=_stored_tex_main(document))
        except (FormatTexProjectRejected, FormatTexMainInvalid, DomainError, OSError):
            return {**base, "reason": "unsupported"}

        selector = source_ref.selector
        if isinstance(selector, TexSourceSelector):
            target = selector.file
        elif isinstance(selector, WholeDocumentSelector):
            target = project.main
        else:
            # A text/pdf selector has no meaning for a tex project.
            return {**base, "reason": "unsupported"}

        blob = payloads.get(target)
        if blob is None:
            return {**base, "reason": "file_missing"}
        try:
            text = decode_utf8_strict(blob)
        except DomainError:
            return {**base, "reason": "unsupported"}

        span = _normalize_span(selector, text)
        if span is None:
            return {**base, "reason": "unsupported"}
        located = _locate_span(text, span, source_ref)
        if located is None:
            return {**base, "reason": "quote_mismatch"}
        citation = {
            **base["citation"],
            "file": target,
            "span": {"start": located[0], "end": located[1]},
        }
        return self._resolved(
            base, text, located[0], located[1], max_chars=max_chars, citation=citation
        )

    def _resolved(
        self,
        base: dict[str, Any],
        text: str,
        start: int,
        end: int,
        *,
        max_chars: int | None,
        citation: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build the resolved envelope; budgets truncate ``text``, not offsets."""
        snippet = text[start:end]
        if max_chars is not None and len(snippet) > max_chars:
            snippet = snippet[:max_chars]
        canonical = dict(citation) if citation is not None else {
            **base["citation"],
            "span": {"start": start, "end": end},
        }
        return {"ref": base["ref"], "text": snippet, "citation": canonical, "resolved": True}

    def submit_run(
        self,
        *,
        kind: str,
        input_payload: Mapping[str, Any],
        idempotency_key: str | None,
        session_id: str,
    ) -> dict[str, Any]:
        """Queue an explanation/narration run for the workspace-bound project."""
        workflow = self.plugins.workflow(kind)
        if workflow is None:
            raise app_errors.WorkflowDisabled(f"Workflow {kind!r} is not enabled in this instance")
        project = self.current_project()
        payload = dict(input_payload)
        # Idempotency scope is the workspace identity, not the session: the same
        # key from a fresh session must still be idempotent for this project.
        scope = f"workspace:{self.workspace_id}"
        run = self.runs.submit(
            kind=kind,
            workload=workflow.workload,
            input_payload=payload,
            project_id=project["id"],
            session_id=session_id,
            idempotency_key=idempotency_key,
            idempotency_scope=scope,
        )
        return self.run_public(run)

    def submit_extraction(
        self,
        *,
        document_id: str,
        revision_id: str | None,
        force: bool,
        idempotency_key: str | None,
        session_id: str,
    ) -> dict[str, Any]:
        document = self.require_document(document_id)
        revision = self.require_revision(document, revision_id)
        project = self.current_project()
        payload = {"document_id": document["id"], "revision_id": revision["id"], "force": bool(force)}
        run = self.runs.submit(
            kind="extract",
            workload=WORKLOAD_EXTRACT,
            input_payload=payload,
            project_id=project["id"],
            session_id=session_id,
            idempotency_key=idempotency_key,
            idempotency_scope=f"workspace:{self.workspace_id}",
            target_id=document["id"],
        )
        return self.run_public(run)

    def submit_compile(
        self,
        *,
        document_id: str,
        revision_id: str,
        main: str,
        idempotency_key: str | None,
        session_id: str,
    ) -> dict[str, Any]:
        """TeX compile.

        Availability is checked *first*: with no safe sandbox the capability does
        not exist for this deployment, so the endpoint answers 503 without even
        validating the request, and never enqueues work. There is deliberately no
        fallback to an in-process subprocess (clearing env vars is not a sandbox).
        """
        if not self.config.tex_compiler_available:
            raise app_errors.TexCompileUnavailable()
        document = self.require_document(document_id)
        self.require_revision(document, revision_id)
        # Reached only when a deployment wires a real, sandboxed executor.
        raise app_errors.TexCompileUnavailable(
            "A safe TeX compilation executor is not part of this deployment"
        )

    # -- run handlers ------------------------------------------------------
    def _register_handlers(self) -> None:
        self.runs.register("explanation", self._handle_explanation)
        self.runs.register("narration", self._handle_narration)
        self.runs.register("extract", self._handle_extract)

    def _handle_explanation(self, ctx: RunContext) -> Mapping[str, Any]:
        node_id = str(ctx.payload.get("node_id") or "")
        question = ctx.payload.get("question")
        node = self.store.get_node(node_id)
        if node is None:
            raise app_errors.NodeNotFound()
        if ctx.project_id and node["project_id"] != ctx.project_id:
            raise app_errors.NodeNotFound()

        # Provider call happens with no transaction held (docs section 4.3).
        result = self.provider.explain(
            node_title=str(node["title"]),
            quote=_quote_of(node),
            question=str(question) if question else None,
        )
        if self.runs.is_cancelled(ctx.run_id):
            raise CancelledRun()

        def publish():
            record = self.store.add_explanation(
                project_id=node['project_id'], node_id=node_id,
                question=str(question) if question else None, answer=result.text,
                provider=result.provider, model=result.model,
            )
            return {'explanation': self.explanation_public(record)}
        return RunDraft(publish)

    def _handle_narration(self, ctx: RunContext) -> Mapping[str, Any]:
        node_ids = [str(value) for value in (ctx.payload.get("node_ids") or [])]
        limit = self.config.limits.max_narration_nodes
        if not node_ids:
            raise app_errors.Unprocessable("node_ids must not be empty", code="invalid_run_input")
        if len(node_ids) > limit:
            raise app_errors.Unprocessable(
                f"node_ids is limited to {limit} entries", code="invalid_run_input"
            )
        project = self.current_project()
        nodes: list[dict[str, Any]] = []
        for node_id in node_ids:
            node = self.store.get_node(node_id)
            if node is None or node["project_id"] != project["id"]:
                raise app_errors.NodeNotFound(f"Node {node_id} not found in this project")
            nodes.append(node)

        segments = self.provider.narrate(
            title=str(ctx.payload.get("title") or ""),
            nodes=nodes,
        )
        if self.runs.is_cancelled(ctx.run_id):
            raise CancelledRun()

        title = str(ctx.payload.get("title") or "").strip() or f"串讲 · {len(segments)} 段"
        def publish():
            record = self.store.add_narration(
                project_id=project['id'], title=title, segments=segments,
                provider=self.provider.name, model=self.provider.model,
            )
            return {'narration': self.narration_public(record)}
        return RunDraft(publish)

    def _handle_extract(self, ctx: RunContext) -> Mapping[str, Any]:
        """Extract structured text via the format adapters (registry port).

        md/txt are decoded strictly; PDF goes through the real extractor port
        (``StructuralPdfExtractor``). A missing backend, an encrypted/malformed
        PDF or a scan with no text layer is recorded as a non-ready extraction
        with an explicit ``error_code`` -- never as fabricated text. A binary is
        never decoded as utf-8 here.
        """
        from backend.application.format.errors import (
            PdfEncrypted as _FmtPdfEncrypted,
            PdfMalformed as _FmtPdfMalformed,
        )
        from backend.application.format.extraction import extract_text
        from backend.infrastructure.format.registry import ExtractorRegistryImpl

        document_id = str(ctx.payload.get("document_id") or "")
        revision_id = str(ctx.payload.get("revision_id") or "")
        document = self.store.get_document(document_id)
        if document is None:
            raise app_errors.DocumentNotFound()
        revision = self.store.get_revision(revision_id)
        if revision is None or revision["document_id"] != document["id"]:
            raise app_errors.RevisionGone()

        raw = self.storage.read_source(document["id"], revision)
        fmt = str(document["format"])
        extractor_version = self.config.extractor_version
        page_count: int | None = None

        if fmt in TEXT_FORMATS:
            try:
                text = decode_utf8_strict(raw)
            except DomainError as exc:
                text = ""
                status = "unsupported"
                error_code = getattr(exc, "code", "unsupported")
            else:
                status = "ready"
                error_code = None
        else:
            # pdf/tex: through the extractor registry (tex has no text extractor,
            # so it reports `unsupported` rather than decoding bytes).
            registry = ExtractorRegistryImpl()
            try:
                result = extract_text(raw, fmt, registry=registry, extractor_version=None)
            except _FmtPdfMalformed:
                text, status, error_code = "", "failed", "pdf_malformed"
            except _FmtPdfEncrypted:
                text, status, error_code = "", "failed", "pdf_encrypted"
            except DomainError as exc:
                # extractor_unavailable / unsupported / content-not-text.
                text, status, error_code = "", "unsupported", getattr(exc, "code", "unsupported")
            except Exception:
                # A backend that still cannot parse the bytes (e.g. a PDF library
                # raising its own read error) is recorded as a failed extraction
                # with an explicit code -- never surfaced as a bare run_failed.
                text, status, error_code = "", "failed", "extractor_error"
            else:
                text = result.text
                status = result.status
                error_code = result.error_code
                page_count = result.page_count
                if result.extractor_version:
                    extractor_version = result.extractor_version

        if self.runs.is_cancelled(ctx.run_id):
            raise CancelledRun()

        def publish():
            artifact_id = None
            text_hash = None
            if status == 'ready':
                payload_bytes = text.encode('utf-8')
                text_hash = content_hash(payload_bytes)
                rel_path, _ = self.storage.write_artifact(
                    document['id'], revision_id, 'extracted.txt', payload_bytes,
                )
                artifact = self.store.add_artifact(
                    document_id=document['id'], revision_id=revision_id,
                    kind='extracted_text', rel_path=rel_path, payload=payload_bytes,
                )
                artifact_id = artifact['id']
            extraction = self.store.upsert_extraction(
                document_id=document['id'], revision_id=revision_id,
                extractor_version=extractor_version, status=status,
                text_hash=text_hash, page_count=page_count, error_code=error_code,
                artifact_id=artifact_id,
            )
            return {'extraction': self.extraction_public(extraction)}
        return RunDraft(publish)
    # -- internals --------------------------------------------------------
    def _assert_file_size(self, payload: bytes) -> None:
        limit = self.config.limits.max_bytes_per_file
        if len(payload) > limit:
            raise app_errors.PayloadTooLarge(
                f"File exceeds max_bytes_per_file ({limit} bytes)",
                details={"max_bytes_per_file": limit, "size_bytes": len(payload)},
            )

    def _assert_document_quota(self, project_id: str) -> None:
        limit = self.config.limits.max_documents_per_session
        if limit and self.store.count_documents(project_id) >= limit:
            raise app_errors.DocumentLimitReached(details={"max_documents_per_session": limit})

    def _store_new_document(
        self,
        *,
        project_id: str,
        title: str,
        fmt: str,
        source_filename: str | None,
        payload: bytes,
        resources: Sequence[tuple[str, bytes]],
        status: str,
        extra: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Create the row pair first, then write the immutable original.

        Ordering matters: the file path is derived from the *server-generated*
        document id, so the rows are created first and the bytes land under
        ``sources/<document_id>/<revision_id>/``. A failure while writing leaves
        a ``failed`` revision rather than a document pointing at nothing.
        """
        revision_id = store_new_id()
        relative = _MAIN_FILENAME.get(fmt, "main.bin")
        revision = {
            "id": revision_id,
            "revision_no": 1,
            "content_hash": content_hash(payload),
            "size_bytes": len(payload),
            "status": RevisionStatus.READY.value,
            "main_rel_path": relative,
            "resources": [],
        }
        document, created = self.store.create_document(
            project_id=project_id,
            title=title,
            fmt=fmt,
            source_filename=source_filename,
            status=status,
            size_bytes=len(payload),
            revision=revision,
            extra=dict(extra) if extra else None,
        )
        try:
            self.storage.write_revision_file(
                document["id"],
                revision_id,
                relative_path=relative,
                payload=payload,
            )
            for logical_path, blob in resources:
                safe = safe_relative_path(logical_path)
                self.storage.write_revision_file(
                    document["id"],
                    revision_id,
                    relative_path=f"resources/{safe}",
                    payload=blob,
                )
        except (PathEscape, DomainError, OSError) as exc:
            self.store.update_document(document["id"], status=DocumentStatus.FAILED.value)
            raise app_errors.Unprocessable(
                "The original could not be stored", code="storage_write_failed", details={"reason": type(exc).__name__}
            ) from exc
        return document, created


def _stored_tex_main(document: Mapping[str, Any]) -> str | None:
    """The declared tex entry file, read from the stored document extra blob."""
    extra = _json_or(document.get("extra_json"), {})
    if isinstance(extra, Mapping):
        main = extra.get("main") or extra.get("tex_main")
        if isinstance(main, str) and main.strip():
            return main.strip()
    return None


def _normalize_span(selector: Any, text: str) -> tuple[int, int] | None:
    """Normalize a parsed selector to *codepoint* offsets in the stored text.

    Only a codepoint/utf16 ``text`` span, a ``tex-source`` span or an explicit
    ``whole-document`` can be expressed as an offset range. A DOM ``utf16`` unit
    is converted (and a half-surrogate split rejected) by the shared selector
    helper. ``None`` means "this selector has no provable offset mapping".
    """
    if isinstance(selector, WholeDocumentSelector):
        return 0, len(text)
    if isinstance(selector, TextSelector):
        start, end, unit = selector.start, selector.end, selector.unit
    elif isinstance(selector, TexSourceSelector):
        start, end, unit = selector.start, selector.end, selector.unit
    else:
        # pdf selectors carry no text offsets.
        return None
    try:
        return normalize_to_codepoint(text, int(start), int(end), unit)
    except (InvalidSourceRef, DomainError, TypeError, ValueError):
        return None


def _locate_span(text: str, span: tuple[int, int], source_ref: "SourceRef") -> tuple[int, int] | None:
    """Verify a stored span against the canonical quote/prefix/suffix.

    When the ref carries a quote we require it to match at the span (and, when
    present, the prefix/suffix windows to match too) -- this catches an anchor
    whose offsets drifted under an edited/served-differently revision, exactly
    the "explicit re-anchor" case the reader contract requires. Refs without a
    quote fall back to the stored span.
    """
    start, end = span
    quote = source_ref.quote
    if not quote:
        return start, end
    if text[start:end] == quote:
        return start, end
    # Offsets drifted; re-anchor on the unique quote + prefix/suffix window.
    return _search_anchor(text, quote, source_ref.prefix, source_ref.suffix)


def _search_anchor(
    text: str, quote: str, prefix: str = "", suffix: str = ""
) -> tuple[int, int] | None:
    """Find a unique occurrence of ``quote`` (validated by prefix/suffix).

    Returns ``None`` when the quote is absent or ambiguous -- an ambiguous quote
    (multiple candidates the context cannot disambiguate) is *not* resolved, so
    we never guess the wrong location.
    """
    if not quote:
        return None
    candidates: list[int] = []
    cursor = text.find(quote)
    while cursor != -1:
        candidates.append(cursor)
        cursor = text.find(quote, cursor + 1)
        if len(candidates) > 64:  # bounded: an over-common quote is unusable
            return None
    if not candidates:
        return None

    matches: list[tuple[int, int]] = []
    for start in candidates:
        end = start + len(quote)
        if prefix and text[max(0, start - len(prefix)) : start] != prefix:
            continue
        if suffix and text[end : end + len(suffix)] != suffix:
            continue
        matches.append((start, end))
    if len(matches) != 1:
        return None
    return matches[0]


def _quote_of(node: Mapping[str, Any]) -> str | None:
    """Read the anchored quote from ``node.metadata.source`` (unified SourceRef)."""
    metadata = _json_or(node.get("metadata_json"), {})
    source = metadata.get("source") if isinstance(metadata, dict) else None
    if isinstance(source, dict):
        quote = source.get("quote")
        if isinstance(quote, str) and quote.strip():
            return quote
    return None


def _etag(content_digest: str) -> str:
    """Strong-ish ETag derived from the stored immutable content hash."""
    digest = content_digest.strip()
    if not digest:
        return '"unknown"'
    if digest.startswith("sha256:"):
        digest = digest.split(":", 1)[1]
    return f'"{digest}"'


__all__ = ["AppServices", "SourcePayload"]
