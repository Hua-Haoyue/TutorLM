"""Request schemas. Responses are rendered by the Host service serializers.

Only shapes the frontend actually sends are accepted; unknown fields are rejected
so a typo cannot silently become a different operation
(``docs/instance-app-api.md``).
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# -- sessions ------------------------------------------------------------
class SessionCreate(StrictModel):
    invite_code: str | None = Field(default=None, max_length=256)


# -- projects ------------------------------------------------------------
class ProjectPatch(StrictModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


# -- documents -----------------------------------------------------------
class DocumentCreate(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1)
    derive_nodes: bool = False


class DocumentPatch(StrictModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)


class ExtractRequest(StrictModel):
    revision_id: str | None = None
    force: bool = False


class CompileRequest(StrictModel):
    revision_id: str
    main: str = Field(min_length=1, max_length=400)


# -- nodes / edges -------------------------------------------------------
class NodeCreate(StrictModel):
    document_id: str | None = None
    title: str = Field(min_length=1, max_length=200)
    kind: str = "concept"
    content: str | None = Field(default=None, max_length=20000)
    parent_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class NodePatch(StrictModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    kind: str | None = None
    content: str | None = Field(default=None, max_length=20000)
    metadata: dict[str, Any] | None = None
    revision: int | None = None


class EdgeCreate(StrictModel):
    source_node_id: str
    target_node_id: str
    relation: str
    metadata: dict[str, Any] = Field(default_factory=dict)


# -- runs ----------------------------------------------------------------
class RunCreate(StrictModel):
    kind: str
    input: dict[str, Any] = Field(default_factory=dict)


# -- sources -------------------------------------------------------------
class SourceResolveRequest(StrictModel):
    refs: list[dict[str, Any]] = Field(default_factory=list, max_length=64)
    budget: dict[str, Any] | None = None


__all__ = [
    "CompileRequest",
    "DocumentCreate",
    "DocumentPatch",
    "EdgeCreate",
    "ExtractRequest",
    "NodeCreate",
    "NodePatch",
    "ProjectPatch",
    "RunCreate",
    "SessionCreate",
    "SourceResolveRequest",
    "StrictModel",
]