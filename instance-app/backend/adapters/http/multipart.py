"""Minimal ``multipart/form-data`` parser (stdlib only).

Why hand-rolled: the documented deployment has no third-party runtime
dependencies (``backend/requirements.txt``), so the upload endpoints cannot rely
on ``python-multipart``. This parser is intentionally small and strict:

* only ``multipart/form-data`` with a ``boundary`` parameter is accepted;
* a missing terminator, a malformed part, an over-quota body or too many parts is
  rejected instead of being partially accepted;
* nothing is written to disk or to a temp file -- the caller decides what the
  bytes mean, and the same size cap applies to every upload entry point.
"""
from __future__ import annotations

from dataclasses import dataclass

from backend.host import errors as app_errors

#: Hard cap on the whole request body. The per-file quota is enforced separately.
MAX_BODY_BYTES = 64 * 1024 * 1024
#: Hard cap on part count: a tex project is the widest shape we accept.
MAX_PARTS = 512


@dataclass(frozen=True, slots=True)
class Part:
    """One parsed part: either a file field (``filename`` set) or a text field."""

    name: str
    data: bytes
    filename: str | None = None
    content_type: str | None = None

    def text(self) -> str:
        try:
            return self.data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise app_errors.EncodingUnsupported("Multipart field is not valid UTF-8") from exc


def boundary_of(content_type: str) -> str:
    """Extract the ``boundary`` parameter, or fail with a 400."""
    if not content_type or "multipart/form-data" not in content_type.lower():
        raise app_errors.BadRequest(
            "Expected multipart/form-data", code="unsupported_media"
        )
    for chunk in content_type.split(";")[1:]:
        key, _, value = chunk.partition("=")
        if key.strip().lower() == "boundary":
            boundary = value.strip().strip('"')
            if boundary:
                return boundary
    raise app_errors.BadRequest("multipart boundary is missing", code="unsupported_media")


def parse(body: bytes, content_type: str) -> list[Part]:
    """Parse a multipart body into parts, enforcing the global caps."""
    if len(body) > MAX_BODY_BYTES:
        raise app_errors.PayloadTooLarge(details={"max_body_bytes": MAX_BODY_BYTES})

    boundary = boundary_of(content_type).encode("utf-8")
    delimiter = b"--" + boundary
    if not body.startswith(delimiter):
        raise app_errors.BadRequest("Malformed multipart body", code="invalid_multipart")

    parts: list[Part] = []
    cursor = len(delimiter)
    while True:
        if body[cursor : cursor + 2] == b"--":
            break  # closing delimiter: the body ended cleanly
        if body[cursor : cursor + 2] == b"\r\n":
            cursor += 2
        header_end = body.find(b"\r\n\r\n", cursor)
        if header_end == -1:
            raise app_errors.BadRequest("Malformed multipart part headers", code="invalid_multipart")
        raw_headers = body[cursor:header_end].decode("latin-1")
        content_start = header_end + 4

        next_delimiter = body.find(b"\r\n" + delimiter, content_start)
        if next_delimiter == -1:
            raise app_errors.BadRequest("Multipart body is not terminated", code="invalid_multipart")

        parts.append(_build_part(raw_headers, body[content_start:next_delimiter]))
        if len(parts) > MAX_PARTS:
            raise app_errors.PayloadTooLarge(details={"max_parts": MAX_PARTS})
        cursor = next_delimiter + 2 + len(delimiter)

    return parts


def _build_part(raw_headers: str, data: bytes) -> Part:
    name: str | None = None
    filename: str | None = None
    content_type: str | None = None
    for line in raw_headers.split("\r\n"):
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key == "content-disposition":
            for token in value.split(";")[1:]:
                field, _, raw = token.partition("=")
                field = field.strip().lower()
                raw = raw.strip().strip('"')
                if field == "name":
                    name = raw
                elif field == "filename":
                    filename = raw
        elif key == "content-type":
            content_type = value

    if not name:
        raise app_errors.BadRequest("Multipart part is missing a name", code="invalid_multipart")
    return Part(name=name, data=data, filename=filename, content_type=content_type)


def field(parts: list[Part], name: str) -> Part | None:
    for part in parts:
        if part.name == name and part.filename is None:
            return part
    return None


def file_field(parts: list[Part], name: str) -> Part | None:
    for part in parts:
        if part.name == name and part.filename is not None:
            return part
    return None


__all__ = ["MAX_BODY_BYTES", "MAX_PARTS", "Part", "boundary_of", "field", "file_field", "parse"]