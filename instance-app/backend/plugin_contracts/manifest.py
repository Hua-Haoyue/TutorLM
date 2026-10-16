"""Static plugin manifest contract (Host plugin SDK v1).

A manifest is the *only* thing the Host needs in order to accept a build-time
plugin: it declares the plugin identity, the Host API version it was compiled
against, its dependencies, the capabilities it may use and the workflows /
panels it contributes (docs/plugin-sdk.md).

Manifests are declarative data. They carry no secrets, no filesystem paths and
no callables, so they serialise straight into ``GET /api/plugins`` and the
``contracts/host/v1`` snapshot without leaking server configuration.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from backend.domain.errors import InvalidInputError

#: The Host API generation this SDK implements. Plugins that were built against
#: a different major version are rejected at registration time.
HOST_API_VERSION = 1

#: The capability vocabulary. A manifest may only request capabilities from
#: this closed set; unknown strings are rejected.
KNOWN_CAPABILITIES: frozenset[str] = frozenset(
    {
        "documents.read",
        "documents.write",
        "knowledge.read",
        "knowledge.nodes.read",
        "knowledge.nodes.write",
        "knowledge.edges.read",
        "knowledge.edges.write",
        "knowledge.graph.read",
        "knowledge.graph.write",
        "knowledge.write",
        "models.generate",
        "runs.submit",
        "storage.read",
        "storage.write",
        "panels.register",
        "workflows.register",
    }
)

_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_VERSION_RE = re.compile(r"^\d+\.\d+(?:\.\d+)?(?:[-+][0-9A-Za-z.-]+)?$")
_ID_MAX = 64
_VERSION_MAX = 32


class PluginContractError(InvalidInputError):
    """A manifest violates the static plugin contract.

    Subclasses :class:`~backend.domain.errors.InvalidInputError` so the shared
    HTTP error handler maps it to ``422 invalid_input`` without new plumbing.
    """

    code = "invalid_input"


def is_valid_plugin_id(value: str) -> bool:
    """``True`` when ``value`` is an acceptable plugin identifier."""

    return bool(value) and len(value) <= _ID_MAX and bool(_ID_RE.match(value))


def is_valid_version(value: str) -> bool:
    """``True`` when ``value`` is an acceptable ``major.minor[.patch]`` version."""

    return bool(value) and len(value) <= _VERSION_MAX and bool(_VERSION_RE.match(value))


def _clean_tuple(values: Iterable[str] | None) -> tuple[str, ...]:
    """De-duplicate while preserving order (manifests are order-sensitive)."""

    seen: list[str] = []
    for value in values or ():
        text = str(value).strip()
        if text and text not in seen:
            seen.append(text)
    return tuple(seen)


@dataclass(frozen=True, slots=True)
class WorkflowDeclaration:
    """A workflow a plugin contributes."""

    id: str
    kind: str
    title: str = ""
    description: str = ""
    input_schema: Mapping[str, Any] | None = None
    output_schema: Mapping[str, Any] | None = None
    required_capabilities: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "description": self.description,
            "required_capabilities": list(self.required_capabilities),
        }
        if self.input_schema is not None:
            payload["input_schema"] = dict(self.input_schema)
        if self.output_schema is not None:
            payload["output_schema"] = dict(self.output_schema)
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WorkflowDeclaration":
        return cls(
            id=str(data.get("id", "")).strip(),
            kind=str(data.get("kind", "")).strip(),
            title=str(data.get("title", "")),
            description=str(data.get("description", "")),
            input_schema=dict(data["input_schema"]) if data.get("input_schema") else None,
            output_schema=dict(data["output_schema"]) if data.get("output_schema") else None,
            required_capabilities=_clean_tuple(data.get("required_capabilities")),
        )


@dataclass(frozen=True, slots=True)
class PanelDeclaration:
    """A frontend panel a plugin contributes."""

    id: str
    title: str = ""
    slot: str = "workspace"
    order: int = 0
    workflow_ids: tuple[str, ...] = ()
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "slot": self.slot,
            "order": int(self.order),
            "workflow_ids": list(self.workflow_ids),
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PanelDeclaration":
        return cls(
            id=str(data.get("id", "")).strip(),
            title=str(data.get("title", "")),
            slot=str(data.get("slot", "workspace")),
            order=int(data.get("order", 0) or 0),
            workflow_ids=_clean_tuple(data.get("workflow_ids")),
            description=str(data.get("description", "")),
        )


@dataclass(frozen=True, slots=True)
class PluginManifest:
    """Immutable description of one statically registered plugin."""

    id: str
    version: str
    host_api_version: int = field(default=HOST_API_VERSION)
    title: str = ""
    description: str = ""
    depends_on: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    workflows: tuple[WorkflowDeclaration, ...] = ()
    panels: tuple[PanelDeclaration, ...] = ()
    builtin: bool = True

    # --------------------------------------------------------------- builders
    @classmethod
    def build(
        cls,
        id: str,
        version: str,
        *,
        host_api_version: int = HOST_API_VERSION,
        title: str = "",
        description: str = "",
        depends_on: Iterable[str] = (),
        capabilities: Iterable[str] = (),
        workflows: Iterable[WorkflowDeclaration] = (),
        panels: Iterable[PanelDeclaration] = (),
        builtin: bool = True,
    ) -> "PluginManifest":
        """Construct a manifest, normalising and de-duplicating the sequences."""

        return cls(
            id=str(id).strip(),
            version=str(version).strip(),
            host_api_version=int(host_api_version),
            title=str(title),
            description=str(description),
            depends_on=_clean_tuple(depends_on),
            capabilities=_clean_tuple(capabilities),
            workflows=tuple(workflows),
            panels=tuple(panels),
            builtin=bool(builtin),
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PluginManifest":
        """Rehydrate a manifest (used by the hello template and fixtures)."""

        return cls.build(
            id=str(data.get("id", "")),
            version=str(data.get("version", "")),
            host_api_version=int(data.get("host_api_version", HOST_API_VERSION)),
            title=str(data.get("title", "")),
            description=str(data.get("description", "")),
            depends_on=data.get("depends_on") or (),
            capabilities=data.get("capabilities") or (),
            workflows=[
                WorkflowDeclaration.from_dict(item)
                for item in (data.get("workflows") or ())
            ],
            panels=[
                PanelDeclaration.from_dict(item)
                for item in (data.get("panels") or ())
            ],
            builtin=bool(data.get("builtin", True)),
        )

    # -------------------------------------------------------------- accessors
    @property
    def workflow_ids(self) -> tuple[str, ...]:
        return tuple(workflow.id for workflow in self.workflows)

    @property
    def panel_ids(self) -> tuple[str, ...]:
        return tuple(panel.id for panel in self.panels)

    def supports_capability(self, capability: str) -> bool:
        return capability in self.capabilities

    # ------------------------------------------------------------- validation
    def validate(self) -> None:
        """Raise :class:`PluginContractError` if this manifest is not loadable."""

        if not is_valid_plugin_id(self.id):
            raise PluginContractError(
                f"非法插件 id: {self.id!r}",
                details={"field": "id", "value": self.id},
            )
        if not is_valid_version(self.version):
            raise PluginContractError(
                f"非法插件版本: {self.version!r}（期望 major.minor[.patch]）",
                details={"field": "version", "value": self.version, "plugin_id": self.id},
            )
        if int(self.host_api_version) != HOST_API_VERSION:
            raise PluginContractError(
                f"插件 {self.id} 的 host_api_version={self.host_api_version} "
                f"与 Host API v{HOST_API_VERSION} 不匹配",
                details={
                    "field": "host_api_version",
                    "expected": HOST_API_VERSION,
                    "actual": int(self.host_api_version),
                    "plugin_id": self.id,
                },
            )
        if self.id in self.depends_on:
            raise PluginContractError(
                f"插件 {self.id} 不能依赖自身",
                details={"field": "depends_on", "plugin_id": self.id},
            )
        for dependency in self.depends_on:
            if not is_valid_plugin_id(dependency):
                raise PluginContractError(
                    f"插件 {self.id} 的依赖 id 非法: {dependency!r}",
                    details={"field": "depends_on", "value": dependency, "plugin_id": self.id},
                )
        unknown = sorted(set(self.capabilities) - KNOWN_CAPABILITIES)
        if unknown:
            raise PluginContractError(
                f"插件 {self.id} 申请了未定义的 capability: {', '.join(unknown)}",
                details={
                    "field": "capabilities",
                    "unknown": unknown,
                    "known": sorted(KNOWN_CAPABILITIES),
                    "plugin_id": self.id,
                },
            )
        for workflow in self.workflows:
            if not workflow.id or not workflow.kind:
                raise PluginContractError(
                    f"插件 {self.id} 的 workflow 缺少 id/kind",
                    details={"field": "workflows", "plugin_id": self.id},
                )
            missing = sorted(set(workflow.required_capabilities) - set(self.capabilities))
            if missing:
                raise PluginContractError(
                    f"插件 {self.id} 的 workflow {workflow.id} 需要未声明的 capability: "
                    f"{', '.join(missing)}",
                    details={
                        "field": "workflows",
                        "workflow_id": workflow.id,
                        "missing": missing,
                        "plugin_id": self.id,
                    },
                )

    # ------------------------------------------------------------ serialisation
    def to_dict(self) -> dict[str, Any]:
        """Public descriptor - safe to expose over ``GET /api/plugins``."""

        return {
            "id": self.id,
            "version": self.version,
            "host_api_version": int(self.host_api_version),
            "title": self.title,
            "description": self.description,
            "depends_on": list(self.depends_on),
            "capabilities": list(self.capabilities),
            "workflows": [workflow.to_dict() for workflow in self.workflows],
            "panels": [panel.to_dict() for panel in self.panels],
            "builtin": bool(self.builtin),
        }


def manifest_from_dict(data: Mapping[str, Any]) -> PluginManifest:
    """Module-level convenience mirroring :meth:`PluginManifest.from_dict`."""

    return PluginManifest.from_dict(data)


__all__ = [
    "HOST_API_VERSION",
    "KNOWN_CAPABILITIES",
    "PanelDeclaration",
    "PluginContractError",
    "PluginManifest",
    "WorkflowDeclaration",
    "is_valid_plugin_id",
    "is_valid_version",
    "manifest_from_dict",
]
