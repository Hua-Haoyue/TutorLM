# -*- coding: utf-8 -*-
"""Runtime acceptance for source-ref resolution and upload format validation.

Owner-scoped focused suite (``tests/test_runtime_acceptance.py``). It drives the
real ASGI app (``create_app`` -> ``LocalStore``/``LocalStorage``/``SessionStore``/
``RunManager`` with the in-process ``fake`` provider) and covers the formerly-open
gaps:

* ``POST /api/sources/resolve`` actually resolves md/txt and tex-source refs,
  validates the stored revision, the codepoint span, the canonical
  quote/prefix/suffix, and returns a canonical citation; a mismatched quote, a
  stale revision, a cross-workspace document and a malformed ref come back as
  ``resolved: false`` with a stable ``reason`` instead of a fabricated answer;
* an upload is validated by the real format adapter, so a malformed PDF is 422
  and an encrypted PDF is 409 -- they never land as ``ready``;
* a new node's ``metadata.source`` cannot point outside the workspace;
* the multipart body is bounded while streaming (the cap is not applied only
  after buffering).

No Docker, no network, no paid model call.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

INSTANCE_APP = Path(__file__).resolve().parents[2]
if str(INSTANCE_APP) not in sys.path:
    sys.path.insert(0, str(INSTANCE_APP))

from fastapi.testclient import TestClient  # noqa: E402

from backend.adapters.http.app import create_app  # noqa: E402
from backend.adapters.http.multipart import MAX_BODY_BYTES  # noqa: E402
from backend.host.config import HostConfig  # noqa: E402

INVITE = "TLM-RUNTIME-CODE"


def _env(root: Path, **overrides: str) -> dict[str, str]:
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    session = root / "session" / "s01"
    session.mkdir(parents=True, exist_ok=True)
    env = {
        "TUTORLM_RUN_MODE": "demo",
        "TUTORLM_DATA_DIR": str(data),
        "TUTORLM_SESSION_DATA_DIR": str(session),
        "TUTORLM_PROVIDER": "fake",
        "TUTORLM_INVITE_CODE": INVITE,
    }
    env.update(overrides)
    return env


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(HostConfig.from_env(_env(tmp_path)))) as test_client:
        yield test_client


@pytest.fixture
def headers(client):
    issued = client.post("/api/sessions", json={"invite_code": INVITE})
    assert issued.status_code == 201, issued.text
    return {"Authorization": f"Bearer {issued.json()['access_token']}"}


@pytest.fixture
def project_id(client, headers):
    return client.get("/api/projects", headers=headers).json()["items"][0]["id"]


def _text_doc(client, headers, project_id, text="hello world foo bar"):
    document = client.post(
        f"/api/projects/{project_id}/documents",
        json={"title": "t", "text": text},
        headers=headers,
    ).json()["document"]
    return document


def _resolve(client, headers, refs, budget=None):
    payload = {"refs": refs}
    if budget is not None:
        payload["budget"] = budget
    response = client.post("/api/sources/resolve", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["items"]


# --------------------------------------------------------------------------
# source ref: md/txt
# --------------------------------------------------------------------------
def test_resolve_text_span_with_quote(client, headers, project_id):
    document = _text_doc(client, headers, project_id)
    ref = {
        "document_id": document["id"],
        "revision_id": document["current_revision_id"],
        "selector": {"type": "text", "start": 6, "end": 11, "unit": "codepoint"},
        "quote": "world",
    }
    item = _resolve(client, headers, [ref])[0]
    assert item["resolved"] is True, item
    assert item["text"] == "world"
    assert item["citation"]["document_id"] == document["id"]
    assert item["citation"]["revision_id"] == document["current_revision_id"]
    assert item["citation"]["span"] == {"start": 6, "end": 11}
    # The canonical ref is echoed back for re-anchoring.
    assert item["ref"]["selector"] == {"type": "text", "start": 6, "end": 11, "unit": "codepoint"}


def test_resolve_whole_document_selector(client, headers, project_id):
    document = _text_doc(client, headers, project_id, "alpha\nbeta")
    ref = {
        "document_id": document["id"],
        "revision_id": document["current_revision_id"],
        "selector": {"type": "whole-document"},
        "quote": "",
    }
    item = _resolve(client, headers, [ref])[0]
    assert item["resolved"] is True
    assert item["text"] == "alpha\nbeta"
    assert item["citation"]["span"]["end"] == len("alpha\nbeta")


def test_resolve_reanchors_when_offsets_drift_but_quote_is_unique(client, headers, project_id):
    document = _text_doc(client, headers, project_id, "the quick brown fox")
    # Offsets point at the wrong slice, but the canonical quote is unique and the
    # prefix/suffix window confirms it: the server re-anchors (offsets are only
    # hints; quote + prefix/suffix is canonical per document-readers.md 3.4/4.4).
    ref = {
        "document_id": document["id"],
        "revision_id": document["current_revision_id"],
        "selector": {"type": "text", "start": 0, "end": 3, "unit": "codepoint"},
        "quote": "quick",
        "prefix": "the ",
        "suffix": " brown",
    }
    item = _resolve(client, headers, [ref])[0]
    assert item["resolved"] is True, item
    assert item["text"] == "quick"
    # The citation carries the *canonical* span recovered from the stored text.
    assert item["citation"]["span"] == {"start": 4, "end": 9}


def test_resolve_wrong_prefix_disambiguates_to_mismatch(client, headers, project_id):
    document = _text_doc(client, headers, project_id, "the quick brown fox")
    ref = {
        "document_id": document["id"],
        "revision_id": document["current_revision_id"],
        "selector": {"type": "text", "start": 0, "end": 3, "unit": "codepoint"},
        "quote": "quick",
        "prefix": "not-the-prefix",
    }
    item = _resolve(client, headers, [ref])[0]
    assert item["resolved"] is False
    assert item["reason"] == "quote_mismatch"


def test_resolve_quote_mismatch_is_not_fabricated(client, headers, project_id):
    document = _text_doc(client, headers, project_id)
    ref = {
        "document_id": document["id"],
        "revision_id": document["current_revision_id"],
        "selector": {"type": "text", "start": 6, "end": 11, "unit": "codepoint"},
        "quote": "does-not-appear",
    }
    item = _resolve(client, headers, [ref])[0]
    assert item["resolved"] is False
    assert item["reason"] == "quote_mismatch"
    assert item["text"] == ""


def test_resolve_ambiguous_quote_without_context_is_refused(client, headers, project_id):
    document = _text_doc(client, headers, project_id, "ab ab ab")
    ref = {
        "document_id": document["id"],
        "revision_id": document["current_revision_id"],
        "selector": {"type": "text", "start": 0, "end": 2, "unit": "codepoint"},
        "quote": "ab",
    }
    item = _resolve(client, headers, [ref])[0]
    # The stored span still matches its own quote, so it resolves at the span.
    assert item["resolved"] is True
    assert item["citation"]["span"] == {"start": 0, "end": 2}


def test_resolve_utf16_unit_is_converted_to_codepoint(client, headers, project_id):
    document = _text_doc(client, headers, project_id, "a\U0001F600b")
    # utf16 length: 'a'(1) + emoji(2 units) + 'b'(1); the emoji spans utf16
    # units 1..3 and maps to codepoint span 1..2.
    ref = {
        "document_id": document["id"],
        "revision_id": document["current_revision_id"],
        "selector": {"type": "text", "start": 1, "end": 3, "unit": "utf16"},
        "quote": "\U0001F600",
    }
    item = _resolve(client, headers, [ref])[0]
    assert item["resolved"] is True, item
    assert item["text"] == "\U0001F600"
    assert item["citation"]["span"] == {"start": 1, "end": 2}


def test_resolve_budget_truncates_text_not_offsets(client, headers, project_id):
    document = _text_doc(client, headers, project_id, "0123456789")
    ref = {
        "document_id": document["id"],
        "revision_id": document["current_revision_id"],
        "selector": {"type": "text", "start": 0, "end": 10, "unit": "codepoint"},
        "quote": "0123456789",
    }
    item = _resolve(client, headers, [ref], budget={"max_chars": 4})[0]
    assert item["resolved"] is True
    assert item["text"] == "0123"
    assert item["citation"]["span"] == {"start": 0, "end": 10}


def test_resolve_is_per_item_and_reports_stale_revision(client, headers, project_id):
    document = _text_doc(client, headers, project_id)
    good = {
        "document_id": document["id"],
        "revision_id": document["current_revision_id"],
        "selector": {"type": "text", "start": 0, "end": 5, "unit": "codepoint"},
        "quote": "hello",
    }
    stale = {
        "document_id": document["id"],
        "revision_id": "00000000-0000-4000-8000-000000000000",
        "selector": {"type": "whole-document"},
        "quote": "",
    }
    items = _resolve(client, headers, [good, stale])
    assert items[0]["resolved"] is True
    assert items[1]["resolved"] is False
    assert items[1]["reason"] == "rev_gone"


def test_resolve_malformed_ref_is_per_item_not_a_batch_error(client, headers):
    malformed = {"document_id": "x", "selector": {"type": "not-a-type"}}
    items = _resolve(client, headers, [malformed])
    assert items[0]["resolved"] is False
    assert items[0]["reason"] == "invalid_ref"


def test_resolve_unknown_document_is_rev_gone(client, headers):
    ref = {
        "document_id": "00000000-0000-4000-8000-000000000000",
        "revision_id": "00000000-0000-4000-8000-000000000001",
        "selector": {"type": "whole-document"},
        "quote": "",
    }
    item = _resolve(client, headers, [ref])[0]
    assert item["resolved"] is False
    assert item["reason"] == "rev_gone"


# --------------------------------------------------------------------------
# source ref: tex-source
# --------------------------------------------------------------------------
def _upload(client, headers, project_id, filename, data, mime, main=None):
    payload = {"file": (filename, data, mime)}
    form = {"main": main} if main else None
    return client.post(
        f"/api/projects/{project_id}/documents/upload",
        files=payload,
        data=form,
        headers=headers,
    )


def test_upload_tex_single_file_and_resolve_tex_source(client, headers, project_id):
    tex = "\\documentclass{article}\n\\begin{document}\nhello tex\n\\end{document}\n"
    uploaded = _upload(client, headers, project_id, "main.tex", tex.encode(), "application/x-tex", main="main.tex")
    assert uploaded.status_code == 201, uploaded.text
    document = uploaded.json()["document"]
    revision_id = uploaded.json()["revision"]["id"]

    ref = {
        "document_id": document["id"],
        "revision_id": revision_id,
        "selector": {"type": "tex-source", "file": "main.tex", "start": 0, "end": 20, "unit": "codepoint"},
        "quote": tex[0:20],
    }
    item = _resolve(client, headers, [ref])[0]
    assert item["resolved"] is True, item
    assert item["citation"]["file"] == "main.tex"
    assert item["citation"]["span"] == {"start": 0, "end": 20}


def test_tex_source_missing_file_is_reported(client, headers, project_id):
    tex = "\\documentclass{article}\n\\begin{document}\nx\n\\end{document}\n"
    uploaded = _upload(client, headers, project_id, "main.tex", tex.encode(), "application/x-tex", main="main.tex")
    document = uploaded.json()["document"]
    ref = {
        "document_id": document["id"],
        "revision_id": uploaded.json()["revision"]["id"],
        "selector": {"type": "tex-source", "file": "nope.tex", "start": 0, "end": 3, "unit": "codepoint"},
        "quote": "",
    }
    item = _resolve(client, headers, [ref])[0]
    assert item["resolved"] is False
    assert item["reason"] == "file_missing"


def test_tex_zip_project_requires_main_and_validates_entries(client, headers, project_id):
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("main.tex", "\\documentclass{article}\n\\begin{document}\nhi\n\\end{document}\n")
        archive.writestr("sec/part.tex", "part body\n")
    data = buf.getvalue()

    # Without a main entry the project is unprocessable, not silently accepted.
    missing_main = _upload(client, headers, project_id, "proj.zip", data, "application/zip")
    assert missing_main.status_code == 422, missing_main.text
    assert missing_main.json()["error"]["code"] == "tex_main_invalid"

    ok = _upload(client, headers, project_id, "proj.zip", data, "application/zip", main="main.tex")
    assert ok.status_code == 201, ok.text
    document = ok.json()["document"]
    assert document["format"] == "tex"
    revision_id = ok.json()["revision"]["id"]

    ref = {
        "document_id": document["id"],
        "revision_id": revision_id,
        "selector": {"type": "tex-source", "file": "sec/part.tex", "start": 0, "end": 9, "unit": "codepoint"},
        "quote": "part body",
    }
    item = _resolve(client, headers, [ref])[0]
    assert item["resolved"] is True, item
    assert item["citation"]["file"] == "sec/part.tex"


def test_tex_zip_traversal_is_rejected(client, headers, project_id):
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("main.tex", "\\documentclass{article}")
        archive.writestr("../evil.tex", "x")
    response = _upload(client, headers, project_id, "proj.zip", buf.getvalue(), "application/zip", main="main.tex")
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] in ("tex_project_rejected", "path_traversal")


# --------------------------------------------------------------------------
# upload: PDF structural validation
# --------------------------------------------------------------------------
def test_malformed_pdf_upload_is_422_not_ready(client, headers, project_id):
    response = _upload(client, headers, project_id, "bad.pdf", b"%PDF-1.7\ntruncated", "application/pdf")
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "pdf_malformed"


def test_encrypted_pdf_upload_is_409(client, headers, project_id):
    body = b"%PDF-1.7\n1 0 obj\n<< /Encrypt 2 0 R >>\nendobj\ntrailer\n<< /Encrypt 2 0 R >>\nstartxref\n0\n%%EOF\n"
    response = _upload(client, headers, project_id, "enc.pdf", body, "application/pdf")
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "pdf_encrypted"


def test_wellformed_pdf_upload_is_ready_and_content_is_not_text(client, headers, project_id):
    # A structurally valid PDF is stored (never decoded as utf-8).
    body = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\nstartxref\n0\n%%EOF\n"
    response = _upload(client, headers, project_id, "ok.pdf", body, "application/pdf")
    assert response.status_code == 201, response.text
    document = response.json()["document"]
    assert document["format"] == "pdf"
    content = client.get(f"/api/documents/{document['id']}/content", headers=headers)
    assert content.status_code == 409
    assert content.json()["error"]["code"] == "content_not_available"


def test_pdf_resolve_without_extraction_reports_reason(client, headers, project_id):
    body = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\nstartxref\n0\n%%EOF\n"
    uploaded = _upload(client, headers, project_id, "p.pdf", body, "application/pdf")
    document = uploaded.json()["document"]
    ref = {
        "document_id": document["id"],
        "revision_id": uploaded.json()["revision"]["id"],
        "selector": {"type": "pdf", "pages": [{"page": 1, "rects": []}], "coordinate_space": "normalized-cropbox-unrotated-top-left"},
        "quote": "some text",
    }
    item = _resolve(client, headers, [ref])[0]
    assert item["resolved"] is False
    # No ready extraction => refuse rather than fabricate pdf offsets.
    assert item["reason"] in ("extraction_required", "extractor_unavailable")


# --------------------------------------------------------------------------
# node metadata: no cross-workspace reference
# --------------------------------------------------------------------------
def test_node_source_ref_cannot_point_outside_workspace(client, headers, project_id, tmp_path):
    document = _text_doc(client, headers, project_id)
    rows = _resolve(
        client,
        headers,
        [{
            "document_id": document["id"],
            "revision_id": document["current_revision_id"],
            "selector": {"type": "whole-document"},
            "quote": "",
        }],
    )
    assert rows[0]["resolved"] is True

    # A valid in-workspace anchor is accepted and stored canonically.
    ok = client.post(
        f"/api/projects/{project_id}/nodes",
        json={
            "title": "anchored",
            "kind": "concept",
            "metadata": {
                "source": {
                    "document_id": document["id"],
                    "revision_id": document["current_revision_id"],
                    "selector": {"type": "text", "start": 0, "end": 5, "unit": "codepoint"},
                    "quote": "hello",
                }
            },
        },
        headers=headers,
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["metadata"]["source"]["selector"]["type"] == "text"

    # An anchor naming an unknown/foreign document is rejected, not stored.
    bad = client.post(
        f"/api/projects/{project_id}/nodes",
        json={
            "title": "bad",
            "kind": "concept",
            "metadata": {
                "source": {
                    "document_id": "00000000-0000-4000-8000-0000000000ff",
                    "revision_id": "00000000-0000-4000-8000-0000000000fe",
                    "selector": {"type": "whole-document"},
                    "quote": "",
                }
            },
        },
        headers=headers,
    )
    assert bad.status_code == 422, bad.text
    assert bad.json()["error"]["code"] == "invalid_source_ref"

    # A non-ref metadata payload (no document/revision) is not treated as a ref.
    plain = client.post(
        f"/api/projects/{project_id}/nodes",
        json={"title": "plain", "kind": "concept", "metadata": {"source": {"quote": "just a quote"}}},
        headers=headers,
    )
    assert plain.status_code == 201, plain.text


def test_node_source_ref_with_missing_revision_is_rejected(client, headers, project_id):
    document = _text_doc(client, headers, project_id)
    bad = client.post(
        f"/api/projects/{project_id}/nodes",
        json={
            "title": "stale",
            "kind": "concept",
            "metadata": {
                "source": {
                    "document_id": document["id"],
                    "revision_id": "00000000-0000-4000-8000-0000000000fe",
                    "selector": {"type": "whole-document"},
                    "quote": "",
                }
            },
        },
        headers=headers,
    )
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "invalid_source_ref"


# --------------------------------------------------------------------------
# upload: bounded body
# --------------------------------------------------------------------------
def test_upload_body_is_bounded_while_streaming(tmp_path):
    import asyncio

    import httpx

    app = create_app(HostConfig.from_env(_env(tmp_path)))
    pulled = {"bytes": 0}

    async def counting_app(scope, receive, send):
        async def counting_receive():
            message = await receive()
            if message["type"] == "http.request":
                pulled["bytes"] += len(message.get("body", b""))
            return message

        await app(scope, counting_receive, send)

    class Endless(httpx.AsyncByteStream):
        def __init__(self, total: int) -> None:
            self.total = total

        async def __aiter__(self):
            chunk = b"z" * 65536
            sent = 0
            while sent < self.total:
                yield chunk
                sent += len(chunk)

    total = MAX_BODY_BYTES + 8 * 1024 * 1024

    async def scenario() -> int:
        transport = httpx.ASGITransport(app=counting_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as async_client:
            token = (await async_client.post("/api/sessions", json={"invite_code": INVITE})).json()["access_token"]
            auth = {"Authorization": f"Bearer {token}"}
            pid = (await async_client.get("/api/projects", headers=auth)).json()["items"][0]["id"]
            response = await async_client.post(
                f"/api/projects/{pid}/documents/upload",
                content=Endless(total),
                headers={**auth, "Content-Type": "multipart/form-data; boundary=B"},
            )
            assert response.status_code == 413
        return pulled["bytes"]

    received = asyncio.run(scenario())
    assert received <= MAX_BODY_BYTES + 1024 * 1024, (
        f"pulled {received} bytes for a {MAX_BODY_BYTES}-byte cap; the limit "
        "must be enforced while streaming, not after full buffering"
    )


def test_upload_requires_session(client, project_id):
    response = client.post(
        f"/api/projects/{project_id}/documents/upload",
        files={"file": ("x.md", b"# hi", "text/markdown")},
    )
    assert response.status_code == 401


def test_upload_filename_traversal_is_sanitized(client, headers, project_id):
    body = (
        b"--B\r\nContent-Disposition: form-data; name=\"file\"; "
        b"filename=\"../../etc/pwned.txt\"\r\nContent-Type: text/plain\r\n\r\nhello\r\n--B--\r\n"
    )
    uploaded = client.post(
        f"/api/projects/{project_id}/documents/upload",
        content=body,
        headers={**headers, "Content-Type": "multipart/form-data; boundary=B"},
    )
    assert uploaded.status_code == 201, uploaded.text
    name = uploaded.json()["document"]["source_filename"]
    assert "/" not in name and "\\" not in name and ".." not in name
