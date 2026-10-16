"""Documents plugin: import / read / selection."""

from __future__ import annotations

from typing import Any

from backend.plugin_contracts import PanelDeclaration, PluginManifest

MANIFEST = PluginManifest.build(
    "documents",
    "0.1.0",
    title="文档",
    description="导入文档、派生知识树并提供选区 DTO。",
    capabilities=["documents.read", "documents.write", "knowledge.write", "knowledge.nodes.read", "knowledge.nodes.write", "storage.write"],
    workflows=(),
    panels=[PanelDeclaration(id="documents", title="文档", slot="left", order=10)],
)


class DocumentsPlugin:
    """Minimal static plugin; real behaviour still lives in the core services."""

    manifest = MANIFEST

    def register_services(self, host: Any) -> None:  # pragma: no cover - no-op
        return None

    def register_routes(self, router: Any, host: Any) -> None:  # pragma: no cover
        return None

    async def startup(self, host: Any) -> None:  # pragma: no cover - no-op
        return None

    async def shutdown(self, host: Any) -> None:  # pragma: no cover - no-op
        return None


def build() -> DocumentsPlugin:
    return DocumentsPlugin()


__all__ = ["MANIFEST", "DocumentsPlugin", "build"]
