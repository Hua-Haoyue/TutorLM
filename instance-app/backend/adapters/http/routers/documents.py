"""Documents, immutable revisions, text content, originals and Tex compile.

Two separations are load-bearing (docs sections 14.2-14.4):

* ``/content`` returns **text only for md/txt**. A PDF/TeX binary is never
  disguised as utf-8; binaries are served by ``/source`` with Range/HEAD;
* every upload/replacement creates a **new immutable revision**; the previous
  original is never rewritten, and ``current_revision_id`` only ever moves
  forward.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from backend.host import errors as app_errors
from backend.host.state import AppState

from ..dependencies import get_state, idempotency_key, require_session
from ..multipart import MAX_BODY_BYTES, field, file_field, parse
from ..schemas import CompileRequest, DocumentCreate, DocumentPatch, ExtractRequest

router = APIRouter(prefix="/api", tags=["documents"])

#: Reject a declared Content-Length over the cap before reading a single byte.
_MAX_BODY_BYTES = MAX_BODY_BYTES


async def _read_multipart(request: Request):
    """Read a multipart body with the size cap applied *while streaming*.

    ``await request.body()`` buffers the whole payload before ``parse`` can
    reject it, so a chunked body with no ``Content-Length`` could pull far more
    than the documented cap into memory. This reads the stream in bounded chunks
    and aborts as soon as the accumulated size exceeds ``MAX_BODY_BYTES`` -- the
    same cap ``parse`` still re-checks -- so the limit bounds memory, not just
    the eventual part payloads. A client-declared ``Content-Length`` over the cap
    is rejected without reading at all.
    """
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > _MAX_BODY_BYTES:
                raise app_errors.PayloadTooLarge(details={"max_body_bytes": _MAX_BODY_BYTES})
        except ValueError:
            raise app_errors.BadRequest("Invalid Content-Length", code="invalid_multipart")

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        if not chunk:
            continue
        total += len(chunk)
        if total > _MAX_BODY_BYTES:
            # Stop pulling from the client the moment the cap is crossed.
            raise app_errors.PayloadTooLarge(details={"max_body_bytes": _MAX_BODY_BYTES})
        chunks.append(chunk)
    body = b"".join(chunks)
    return parse(body, request.headers.get("content-type", ""))


def _upload_of(parts, name: str = "file"):
    part = file_field(parts, name)
    if part is None:
        raise app_errors.BadRequest(f"Missing multipart file field {name!r}", code="missing_file_field")
    return part


def _bool_field(parts, name: str, default: bool = False) -> bool:
    part = field(parts, name)
    if part is None:
        return default
    return part.text().strip().lower() in ("1", "true", "yes", "on")


def _text_field(parts, name: str) -> str | None:
    part = field(parts, name)
    if part is None:
        return None
    value = part.text().strip()
    return value or None


# -- collection ----------------------------------------------------------
@router.get("/projects/{project_id}/documents")
def list_documents(
    project_id: str,
    session: dict = Depends(require_session),
    state: AppState = Depends(get_state),
) -> dict:
    return {"items": state.services.list_documents(project_id)}


@router.post("/projects/{project_id}/documents", status_code=201)
def create_document(
    project_id: str,
    payload: DocumentCreate,
    session: dict = Depends(require_session),
    state: AppState = Depends(get_state),
) -> dict:
    """Create a text document from pasted text (md/txt only)."""
    document, _ = state.services.create_text_document(
        project_id=project_id,
        title=payload.title,
        text=payload.text,
        derive_nodes=payload.derive_nodes,
    )
    # `derive_nodes` defaults to false by contract: nodes come from manual
    # selection anchoring, so nothing is auto-generated here.
    return {"document": state.services.document_public(document)}


@router.post("/projects/{project_id}/documents/upload", status_code=201)
async def upload_document(
    project_id: str,
    request: Request,
    session: dict = Depends(require_session),
    state: AppState = Depends(get_state),
) -> dict:
    """Upload an md/txt/pdf/tex original (or a tex project zip)."""
    parts = await _read_multipart(request)
    upload = _upload_of(parts)
    main = _text_field(parts, "main")
    # `derive_nodes` defaults to false and is accepted for compatibility only:
    # nodes are created from manual selection anchoring, never auto-generated.
    _bool_field(parts, "derive_nodes")
    document, revision = state.services.upload_document(
        project_id=project_id,
        filename=upload.filename or "upload.bin",
        payload=upload.data,
        declared_mime=upload.content_type,
        main=main,
    )
    return {
        "document": state.services.document_public(document),
        "revision": state.services.revision_public(revision),
    }


# -- single document -----------------------------------------------------
@router.get("/documents/{document_id}")
def get_document(
    document_id: str,
    session: dict = Depends(require_session),
    state: AppState = Depends(get_state),
) -> dict:
    return {"document": state.services.get_document(document_id)}


@router.patch("/documents/{document_id}")
def patch_document(
    document_id: str,
    payload: DocumentPatch,
    session: dict = Depends(require_session),
    state: AppState = Depends(get_state),
) -> dict:
    return {"document": state.services.update_document(document_id, title=payload.title)}


@router.delete("/documents/{document_id}", status_code=204)
def delete_document(
    document_id: str,
    session: dict = Depends(require_session),
    state: AppState = Depends(get_state),
) -> Response:
    state.services.delete_document(document_id)
    return Response(status_code=204)


@router.get("/documents/{document_id}/revisions")
def list_revisions(
    document_id: str,
    session: dict = Depends(require_session),
    state: AppState = Depends(get_state),
) -> dict:
    return {"items": state.services.list_revisions(document_id)}


@router.post("/documents/{document_id}/revisions", status_code=201)
async def add_revision(
    document_id: str,
    request: Request,
    session: dict = Depends(require_session),
    state: AppState = Depends(get_state),
) -> dict:
    """Append a new immutable revision from a replacement file."""
    parts = await _read_multipart(request)
    upload = _upload_of(parts)
    main = _text_field(parts, "main")
    document, revision = state.services.add_revision(
        document_id=document_id,
        filename=upload.filename or "upload.bin",
        payload=upload.data,
        declared_mime=upload.content_type,
        main=main,
    )
    return {
        "document": state.services.document_public(document),
        "revision": state.services.revision_public(revision),
    }


@router.get("/documents/{document_id}/content")
def document_content(
    document_id: str,
    session: dict = Depends(require_session),
    state: AppState = Depends(get_state),
) -> dict:
    """Plain-text body for md/txt. Never a disguised binary payload."""
    return {"text": state.services.read_text_content(document_id)}


@router.api_route("/documents/{document_id}/source", methods=["GET", "HEAD"])
def document_source(
    document_id: str,
    request: Request,
    revision_id: str | None = None,
    session: dict = Depends(require_session),
    state: AppState = Depends(get_state),
) -> Response:
    """Original bytes with ETag/Range/HEAD semantics (docs section 14.3)."""
    payload = state.services.source_payload(document_id, revision_id)
    return _binary_response(payload, request, inline=False)


# -- extraction / builds -------------------------------------------------
@router.post("/documents/{document_id}/extract", status_code=202)
def extract_document(
    document_id: str,
    payload: ExtractRequest,
    session: dict = Depends(require_session),
    state: AppState = Depends(get_state),
    key: str | None = Depends(idempotency_key),
) -> dict:
    """Queue a text-extraction run (its own workload class, not the model queue)."""
    run = state.services.submit_extraction(
        document_id=document_id,
        revision_id=payload.revision_id,
        force=payload.force,
        idempotency_key=key,
        session_id=str(session["id"]),
    )
    return run


@router.get("/documents/{document_id}/extractions")
def list_extractions(
    document_id: str,
    revision_id: str | None = None,
    extractor_version: str | None = None,
    session: dict = Depends(require_session),
    state: AppState = Depends(get_state),
) -> dict:
    return {
        "items": state.services.list_extractions(
            document_id, revision_id=revision_id, extractor_version=extractor_version
        )
    }


@router.get("/documents/{document_id}/text")
def document_text(
    document_id: str,
    revision_id: str | None = None,
    extractor_version: str | None = None,
    session: dict = Depends(require_session),
    state: AppState = Depends(get_state),
) -> dict:
    """Structured extracted text (docs section 4b).

    Distinct from ``/content`` (whole original text for md/txt only) and from
    ``/source`` (raw bytes): this returns the *extracted* text plus the
    ``extractor_version`` that produced it, so a reference can be validated
    against ``revision_id + extractor_version``. A binary original never comes
    back as text here without a ready extraction.
    """
    return state.services.structured_text(
        document_id, revision_id=revision_id, extractor_version=extractor_version
    )


@router.post("/documents/{document_id}/compile", status_code=202)
def compile_document(
    document_id: str,
    payload: CompileRequest,
    session: dict = Depends(require_session),
    state: AppState = Depends(get_state),
    key: str | None = Depends(idempotency_key),
) -> dict:
    """TeX compile.

    This deployment has no safe compiler sandbox, so the documented default
    applies: **503 tex_compile_unavailable**, never enqueued and never retried
    through an unsafe in-process fallback (docs section 16).
    """
    return state.services.submit_compile(
        document_id=document_id,
        revision_id=payload.revision_id,
        main=payload.main,
        idempotency_key=key,
        session_id=str(session["id"]),
    )


@router.get("/documents/{document_id}/builds")
def list_builds(
    document_id: str,
    revision_id: str | None = None,
    session: dict = Depends(require_session),
    state: AppState = Depends(get_state),
) -> dict:
    return {"items": state.services.list_builds(document_id, revision_id=revision_id)}


@router.get("/documents/{document_id}/builds/{build_id}")
def get_build(
    document_id: str,
    build_id: str,
    session: dict = Depends(require_session),
    state: AppState = Depends(get_state),
) -> dict:
    return state.services.get_build(document_id, build_id)


# -- artifacts -----------------------------------------------------------
@router.get("/documents/{document_id}/artifacts")
def list_artifacts(
    document_id: str,
    revision_id: str | None = None,
    session: dict = Depends(require_session),
    state: AppState = Depends(get_state),
) -> dict:
    return {"items": state.services.list_artifacts(document_id, revision_id)}


@router.get("/documents/{document_id}/artifacts/{artifact_id}")
def get_artifact(
    document_id: str,
    artifact_id: str,
    session: dict = Depends(require_session),
    state: AppState = Depends(get_state),
) -> dict:
    return state.services.get_artifact(document_id, artifact_id)


@router.api_route("/documents/{document_id}/artifacts/{artifact_id}/source", methods=["GET", "HEAD"])
def artifact_source(
    document_id: str,
    artifact_id: str,
    request: Request,
    session: dict = Depends(require_session),
    state: AppState = Depends(get_state),
) -> Response:
    payload = state.services.artifact_source(document_id, artifact_id)
    return _binary_response(payload, request, inline=True)


# -- shared binary plumbing ----------------------------------------------
def _binary_response(payload, request: Request, *, inline: bool) -> Response:
    """Serve stored bytes with conditional-request, range and HEAD support.

    A single code path serves both originals and artifacts so a conditional-request
    or range bug cannot exist in one but not the other. HEAD never reads the file
    body; it only reports the same headers (``Content-Length`` included).
    """
    head_only = request.method.upper() == "HEAD"
    base_headers = {
        "ETag": payload.etag,
        "Accept-Ranges": "bytes",
    }
    disposition = "inline" if inline else "attachment"
    headers = {
        **base_headers,
        "Content-Length": str(payload.size_bytes),
        "Content-Disposition": f'{disposition}; filename="{payload.filename}"',
    }

    if_none_match = request.headers.get("if-none-match")
    if if_none_match and _etag_matches(if_none_match, payload.etag):
        # A validating conditional request: no body, no file read.
        return Response(status_code=304, headers=base_headers)

    range_header = request.headers.get("range")
    if range_header:
        parsed = _parse_range(range_header, payload.size_bytes)
        if parsed is None:
            headers["Content-Range"] = f"bytes */{payload.size_bytes}"
            return Response(status_code=416, headers=headers)
        start, end = parsed
        length = end - start + 1
        headers["Content-Range"] = f"bytes {start}-{end}/{payload.size_bytes}"
        headers["Content-Length"] = str(length)
        if head_only:
            return Response(status_code=206, media_type=payload.media_type, headers=headers)
        return Response(
            content=_read_range(payload.path, start, length),
            status_code=206,
            media_type=payload.media_type,
            headers=headers,
        )

    if head_only:
        return Response(status_code=200, media_type=payload.media_type, headers=headers)
    return Response(
        content=payload.path.read_bytes(),
        status_code=200,
        media_type=payload.media_type,
        headers=headers,
    )


def _read_range(path, start: int, length: int) -> bytes:
    """Read exactly one byte range without loading the whole original."""
    with path.open("rb") as handle:
        handle.seek(start)
        return handle.read(length)


def _etag_matches(header_value: str, etag: str) -> bool:
    candidates = [part.strip() for part in header_value.split(",")]
    for candidate in candidates:
        if candidate in ("*", etag):
            return True
        if candidate.startswith("W/"):
            candidate = candidate[2:]
        if candidate == etag:
            return True
    return False


def _parse_range(value: str, size: int) -> tuple[int, int] | None:
    """Parse a single ``bytes=`` range. Multiple ranges are not offered."""
    if not value.startswith("bytes="):
        return None
    spec = value[len("bytes=") :].strip()
    if "," in spec:
        return None
    start_text, _, end_text = spec.partition("-")
    try:
        if not start_text:
            # Suffix range: last N bytes.
            length = int(end_text)
            if length <= 0 or size == 0:
                return None
            start = max(0, size - length)
            return start, size - 1
        start = int(start_text)
        end = int(end_text) if end_text else size - 1
    except ValueError:
        return None
    if start < 0 or start >= size or end < start:
        return None
    return start, min(end, size - 1)


__all__ = ["router"]
