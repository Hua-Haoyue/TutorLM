"""Application error taxonomy and the HTTP error envelope.

Pure module: no framework imports, so the Host services, the RunManager and the
HTTP adapter all raise the same errors.

Envelope (docs/instance-app-api.md "common conventions"):

    {"error": {"code": str, "message": str, "details"?: object}}

Messages never echo credentials, tokens or internal filesystem paths.
"""
from __future__ import annotations

from typing import Any, Mapping


class AppError(Exception):
    """Base class for every error the API surfaces to a client."""

    status_code = 500
    code = "internal_error"
    message = "Internal error"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message or self.message)
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        self.details: dict[str, Any] | None = dict(details) if details else None
        self.headers: dict[str, str] = dict(headers) if headers else {}

    @property
    def message_text(self) -> str:
        return str(self)

    def to_envelope(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message_text}
        if self.details:
            error["details"] = self.details
        return {"error": error}


class BadRequest(AppError):
    status_code = 400
    code = "invalid_request"
    message = "Invalid request"


class UnsupportedMedia(BadRequest):
    code = "unsupported_media"
    message = "Unsupported media type"


class EncodingUnsupported(BadRequest):
    code = "encoding_unsupported"
    message = "Text payload is not valid UTF-8"


class Unauthorized(AppError):
    status_code = 401
    code = "unauthorized"
    message = "Missing or invalid application session"


class InviteRequired(Unauthorized):
    code = "invite_required"
    message = "An invite code is required to start an application session"


class Forbidden(AppError):
    status_code = 403
    code = "forbidden"
    message = "Forbidden"


class NotFound(AppError):
    status_code = 404
    code = "not_found"
    message = "Not found"


class Conflict(AppError):
    status_code = 409
    code = "conflict"
    message = "Conflicting state"


class Gone(AppError):
    status_code = 410
    code = "gone"
    message = "Resource is gone"


class SessionExpired(Gone):
    code = "session_expired"
    message = "Application session expired"


class PayloadTooLarge(AppError):
    status_code = 413
    code = "payload_too_large"
    message = "Payload exceeds the configured limit"


class Unprocessable(AppError):
    status_code = 422
    code = "unprocessable"
    message = "Request cannot be processed"


class TooManyRequests(AppError):
    status_code = 429
    code = "rate_limited"
    message = "Quota exhausted"


class ServiceUnavailable(AppError):
    status_code = 503
    code = "unavailable"
    message = "Service temporarily unavailable"


class NotImplementedYet(AppError):
    status_code = 501
    code = "not_implemented"
    message = "Not implemented"


class IdempotencyConflict(Conflict):
    code = "idempotency_conflict"
    message = "Idempotency-Key was already used with a different request body"


class RevisionConflict(Conflict):
    code = "revision_conflict"
    message = "Optimistic concurrency check failed"


class WorkflowDisabled(Conflict):
    code = "workflow_disabled"
    message = "Workflow is not enabled in this instance"


class ProjectMismatch(BadRequest):
    code = "project_mismatch"
    message = "Body project_id does not match the workspace-bound project"


class NodeNotFound(NotFound):
    code = "node_not_found"
    message = "Node not found"


class DocumentNotFound(NotFound):
    code = "document_not_found"
    message = "Document not found"


class RunNotFound(NotFound):
    code = "run_not_found"
    message = "Run not found"


class RevisionGone(Gone):
    code = "revision_gone"
    message = "Revision no longer exists"


class ArtifactStale(Conflict):
    code = "artifact_stale"
    message = "Referenced artifact or build is stale"


class ContentNotAvailable(Conflict):
    code = "content_not_available"
    message = "Full text content is only available for text documents; use /text, /extractions or /source"


class PdfEncrypted(Conflict):
    code = "pdf_encrypted"
    message = "Encrypted PDF files are not supported"


class PdfMalformed(Unprocessable):
    code = "pdf_malformed"
    message = "Malformed PDF file"


class TexMainInvalid(Unprocessable):
    code = "tex_main_invalid"
    message = "TeX project entry file is missing or ambiguous"


class TexCompileUnavailable(ServiceUnavailable):
    code = "tex_compile_unavailable"
    message = "No safe TeX compilation executor is available in this deployment"


class RunQueueFull(ServiceUnavailable):
    code = "run_queue_full"
    message = "Run queue is full; retry later"


class RunLimitReached(TooManyRequests):
    code = "run_limit_reached"
    message = "Run quota for this session is exhausted"


class DocumentLimitReached(TooManyRequests):
    code = "document_limit_reached"
    message = "Document quota for this project is exhausted"


class SessionDirInvalid(AppError):
    """Configuration/deployment defect: sessions must live outside the volume."""

    status_code = 500
    code = "session_dir_invalid"
    message = "Application session storage is not configured outside the workspace volume"


class WorkspaceBindingFailed(AppError):
    """Clone/rebind could not be trusted; fail closed (docs section 4.5)."""

    status_code = 409
    code = "workspace_binding_failed"
    message = "Workspace binding could not be verified"


class StorageUnavailable(AppError):
    status_code = 503
    code = "storage_unavailable"
    message = "Workspace storage is not ready"


class ShareNotSupported(Conflict):
    """Sharing belongs to the control plane, never to the in-container app."""

    code = "share_not_supported"
    message = (
        "Project sharing is a control-plane concern; call "
        "POST /control/workspaces/{workspace_id}/invitations through the gateway instead"
    )


class ShareRedeemNotSupported(Gone):
    code = "share_redeem_not_supported"
    message = (
        "Share redemption is a control-plane concern; call POST /control/workspace-imports "
        "through the gateway instead"
    )


class MultiProjectNotSupported(Conflict):
    """One container serves exactly one workspace-bound project."""

    code = "multi_project_not_supported"
    message = "This instance serves a single workspace-bound project; cross-project operations are not supported"


__all__ = [
    "AppError",
    "ArtifactStale",
    "BadRequest",
    "Conflict",
    "ContentNotAvailable",
    "DocumentLimitReached",
    "DocumentNotFound",
    "EncodingUnsupported",
    "Forbidden",
    "Gone",
    "IdempotencyConflict",
    "InviteRequired",
    "MultiProjectNotSupported",
    "NodeNotFound",
    "NotFound",
    "NotImplementedYet",
    "PayloadTooLarge",
    "PdfEncrypted",
    "PdfMalformed",
    "ProjectMismatch",
    "RevisionConflict",
    "RevisionGone",
    "RunLimitReached",
    "RunNotFound",
    "RunQueueFull",
    "ServiceUnavailable",
    "SessionDirInvalid",
    "SessionExpired",
    "ShareNotSupported",
    "ShareRedeemNotSupported",
    "StorageUnavailable",
    "TexCompileUnavailable",
    "TexMainInvalid",
    "TooManyRequests",
    "Unauthorized",
    "Unprocessable",
    "UnsupportedMedia",
    "WorkflowDisabled",
    "WorkspaceBindingFailed",
]