"""Static plugin registry (docs/instance-app-design.md section 7).

Plugins are declarative here: they describe `kind`/`workflow` capabilities that
the frontend discovers through GET /api/runtime, and they name the workload the
RunManager must schedule. Model/transport logic lives behind the provider port,
never inside a plugin.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Workflow:
    kind: str
    workload: str = "model"
    title: str = ""

    def public(self) -> dict[str, str]:
        return {"kind": self.kind}


@dataclass(frozen=True)
class Plugin:
    name: str
    workflows: tuple[Workflow, ...] = ()
    enabled: bool = True
    version: str = "0.1.0"
    description: str = ""

    def public(self) -> dict[str, object]:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "version": self.version,
            "workflows": [wf.public() for wf in self.workflows],
        }

    def workflow(self, kind: str) -> Workflow | None:
        for wf in self.workflows:
            if wf.kind == kind:
                return wf
        return None


EXPLANATION = Plugin(
    name="explanation",
    workflows=(Workflow(kind="explanation", workload="model", title="解释"),),
    description="Answer a question anchored to one knowledge node",
)

NARRATION = Plugin(
    name="narration",
    workflows=(Workflow(kind="narration", workload="model", title="串讲"),),
    description="Compose an ordered narration over selected nodes",
)

#: Static plugin set shipped by the backend image.
KNOWN_PLUGINS: tuple[Plugin, ...] = (EXPLANATION, NARRATION)


@dataclass
class PluginRegistry:
    """Runtime view over the static plugins, filtered by configuration."""

    plugins: tuple[Plugin, ...] = KNOWN_PLUGINS
    enabled_names: tuple[str, ...] = ("explanation", "narration")
    _extra: dict[str, Workflow] = field(default_factory=dict)

    def enabled(self) -> tuple[Plugin, ...]:
        return tuple(p for p in self.plugins if p.enabled and p.name in self.enabled_names)

    def public(self) -> list[dict[str, object]]:
        return [p.public() for p in self.plugins if p.name in self.enabled_names]

    def workflow(self, kind: str) -> Workflow | None:
        for plugin in self.enabled():
            wf = plugin.workflow(kind)
            if wf is not None:
                return wf
        return None

    def supports(self, kind: str) -> bool:
        return self.workflow(kind) is not None


def build_registry(enabled_names: tuple[str, ...]) -> PluginRegistry:
    return PluginRegistry(plugins=KNOWN_PLUGINS, enabled_names=enabled_names)


__all__ = [
    "EXPLANATION",
    "KNOWN_PLUGINS",
    "NARRATION",
    "Plugin",
    "PluginRegistry",
    "Workflow",
    "build_registry",
]