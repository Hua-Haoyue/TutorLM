"""Static plugin registry (docs/plugin-sdk.md).

Plugins are registered at build time; this is *not* a dynamic marketplace or a
security sandbox. The registry is the single authority that validates each
manifest (id/version/host API/capabilities), resolves dependencies with a
topological sort and exposes the public descriptors that ``GET /api/plugins``
and ``GET /api/runtime`` return.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Iterable, Iterator, Mapping

from backend.domain.errors import InvalidInputError, NotFoundError
from backend.plugin_contracts import Draft, PluginManifest, WorkflowDeclaration, WorkflowInput
from backend.plugin_contracts.manifest import HOST_API_VERSION, PluginContractError
from backend.plugin_contracts.validation import bounded_json, check_schema, checkpoint, validate_value

PluginFactory = Callable[[], Any]


@dataclass(frozen=True, slots=True)
class RegisteredWorkflow:
    plugin_id: str
    declaration: WorkflowDeclaration
    instance: Any

    @property
    def kind(self) -> str:
        return self.declaration.kind

    @property
    def workload(self) -> str:
        return 'model'

#: Builtin plugins are mandatory: if one fails to import the Host is not
#: correctly assembled and startup must fail; plugins may be disabled explicitly.
BUILTIN_PLUGINS: tuple[tuple[str, str], ...] = (
    ("backend.plugins.documents", "build"),
    ("backend.plugins.explanations", "build"),
    ("backend.plugins.knowledge_narration", "build"),
)


@dataclass(slots=True)
class RegisteredPlugin:
    """A manifest plus its (optional) instance and ordering metadata."""

    manifest: PluginManifest
    instance: Any = None
    enabled: bool = True
    order: int = 0

    @property
    def id(self) -> str:
        return self.manifest.id

    @property
    def version(self) -> str:
        return self.manifest.version

    def descriptor(self) -> dict[str, Any]:
        payload = self.manifest.to_dict()
        payload["enabled"] = bool(self.enabled)
        return payload


class PluginRegistry:
    """Validates, orders and exposes the statically registered plugins."""

    def __init__(self) -> None:
        self._items: dict[str, RegisteredPlugin] = {}
        self._ordered: list[str] = []
        self._topology: list[str] = []
        self._frozen = False

    # -------------------------------------------------------------- registration
    def register(
        self,
        manifest: PluginManifest,
        *,
        instance: Any = None,
        factory: PluginFactory | None = None,
        enabled: bool = True,
        replace: bool = False,
    ) -> RegisteredPlugin:
        """Register one plugin.

        ``instance`` is stored directly; otherwise ``factory`` is called to
        build the plugin class. Both are optional so a manifest-only plugin
        (e.g. a builtin with no code yet) can be registered.
        """

        self._check_frozen()
        previous = self._items.get(manifest.id) if isinstance(manifest, PluginManifest) else None
        if not isinstance(manifest, PluginManifest):
            raise PluginContractError("register 需要 PluginManifest")
        manifest.validate()
        if manifest.id in self._items and not replace:
            raise PluginContractError(
                f"插件重复注册: {manifest.id}",
                details={"plugin_id": manifest.id},
            )
        resolved = instance
        if resolved is None and factory is not None:
            resolved = factory()
        item = RegisteredPlugin(
            manifest=manifest,
            instance=resolved,
            enabled=bool(enabled),
            order=len(self._ordered),
        )
        self._items[manifest.id] = item
        try:
            self._rebuild()
            self._workflow_map(require_instances=False)
        except Exception:
            if previous is None:
                self._items.pop(manifest.id, None)
            else:
                self._items[manifest.id] = previous
            self._rebuild()
            raise
        return item

    def register_builtins(self) -> "PluginRegistry":
        """Load and register the three mandatory builtin plugins.

        A failing import or a missing :data:`MANIFEST` is fatal: builtins are
        required plugins, so startup/readiness must surface the failure instead
        of silently dropping the plugin.
        """

        self._check_frozen()
        for module_name, factory_name in BUILTIN_PLUGINS:
            try:
                module = __import__(module_name, fromlist=[factory_name, "MANIFEST"])
            except Exception as exc:  # noqa: BLE001 - re-raised as contract error
                raise PluginContractError(
                    f"必要内置插件加载失败: {module_name}",
                    details={"plugin_module": module_name, "error": repr(exc)},
                ) from exc
            manifest = getattr(module, "MANIFEST", None)
            if not isinstance(manifest, PluginManifest):
                raise PluginContractError(
                    f"内置插件缺少有效的 MANIFEST: {module_name}",
                    details={"plugin_module": module_name},
                )
            factory = getattr(module, factory_name, None)
            if not callable(factory):
                raise PluginContractError(
                    f"内置插件缺少 build() 构造器: {module_name}",
                    details={"plugin_module": module_name, "factory": factory_name},
                )
            self.register(manifest, factory=factory, replace=True)
        return self

    def freeze(self) -> "PluginRegistry":
        """Validate the finalised set, then seal it against later registration."""

        self.validate()
        self._frozen = True
        return self

    def _check_frozen(self) -> None:
        if self._frozen:
            raise PluginContractError("插件注册表已冻结")

    # ---------------------------------------------------------------- validation
    def _rebuild(self) -> None:
        """Recompute the dependency topology; raise on a dependency cycle.

        Registration order does not matter: only edges to *registered* plugins
        are traversed here, and a missing dependency is reported by
        :meth:`validate` (run at freeze / startup) rather than blocking an
        out-of-order ``register`` call.
        """

        ids = list(self._items)
        ordered: list[str] = []
        seen: set[str] = set()
        visiting: set[str] = set()
        order_index = sorted(range(len(ids)), key=lambda i: self._items[ids[i]].order)

        def visit(plugin_id: str, trail: tuple[str, ...]) -> None:
            if plugin_id in seen:
                return
            if plugin_id in visiting:
                cycle = " -> ".join((*trail, plugin_id))
                raise PluginContractError(
                    f"检测到插件依赖环: {cycle}",
                    details={"cycle": list((*trail, plugin_id))},
                )
            visiting.add(plugin_id)
            item = self._items[plugin_id]
            for dependency in item.manifest.depends_on:
                if dependency in self._items:
                    visit(dependency, (*trail, plugin_id))
            visiting.discard(plugin_id)
            seen.add(plugin_id)
            ordered.append(plugin_id)

        for index in order_index:
            visit(ids[index], ())
        self._topology = ordered
        self._ordered = [pid for pid in ordered if self._items[pid].enabled]

    # ------------------------------------------------------------------- access
    def _workflow_map(self, *, require_instances: bool) -> dict[str, RegisteredWorkflow]:
        workflows: dict[str, RegisteredWorkflow] = {}
        for plugin_id in self._ordered:
            item = self._items[plugin_id]
            implementations = getattr(item.instance, "workflows", {})
            if not isinstance(implementations, Mapping):
                raise PluginContractError("Plugin workflows must be an id-to-instance mapping")
            declared = set(item.manifest.workflow_ids)
            if len(declared) != len(item.manifest.workflows):
                raise PluginContractError("Duplicate workflow declaration id")
            if set(implementations) - declared:
                raise PluginContractError("Plugin implements an undeclared workflow")
            for spec in item.manifest.workflows:
                if spec.kind in workflows:
                    raise PluginContractError(f"Duplicate workflow kind: {spec.kind}")
                for schema in (spec.input_schema, spec.output_schema):
                    if schema is not None:
                        check_schema(schema)
                instance = implementations.get(spec.id)
                if instance is None:
                    if require_instances:
                        raise PluginContractError(f"Workflow implementation missing: {spec.id}")
                elif (
                    getattr(instance, "id", None) != spec.id
                    or getattr(instance, "kind", None) != spec.kind
                    or not callable(getattr(instance, "run", None))
                ):
                    raise PluginContractError(f"Workflow implementation mismatch: {spec.id}")
                workflows[spec.kind] = RegisteredWorkflow(plugin_id, spec, instance)
        return workflows

    def workflow(self, kind: str) -> RegisteredWorkflow:
        """Resolve an enabled, declared and implemented workflow by run kind."""
        self.validate()
        workflow = self._workflow_map(require_instances=True).get(kind)
        if workflow is None:
            raise NotFoundError("Workflow is not available", details={"kind": kind})
        return workflow

    def executable_kinds(self) -> tuple[str, ...]:
        """Enabled workflow kinds in dependency order; validate implementations first."""
        self.validate()
        return tuple(self._workflow_map(require_instances=True))

    def supports(self, kind: str) -> bool:
        return kind in self.executable_kinds()

    def public(self) -> list[dict[str, Any]]:
        return self.descriptors()

    def execute(self, kind: str, scoped_context: Any, payload: Mapping[str, Any]) -> Draft:
        """Validate and execute a trusted static workflow; never commit its draft."""
        entry = self.workflow(kind)
        if not isinstance(payload, Mapping):
            raise InvalidInputError("Workflow input must be an object")
        snapshot = bounded_json(dict(payload))
        spec = entry.declaration
        if spec.input_schema is not None:
            validate_value(snapshot, spec.input_schema)
        checkpoint(scoped_context)
        run = getattr(scoped_context, "run", scoped_context)
        data = WorkflowInput(
            kind=kind, payload=MappingProxyType(snapshot),
            session_id=getattr(scoped_context, "session_id", ""),
            run_id=getattr(run, "run_id", ""),
        )
        draft = entry.instance.run(scoped_context, data)
        checkpoint(scoped_context)
        if not isinstance(draft, Draft) or not isinstance(draft.result, Mapping):
            raise PluginContractError("Workflow must return Draft with an object result")
        draft.result = bounded_json(dict(draft.result))
        if spec.output_schema is not None:
            validate_value(draft.result, spec.output_schema, "result")
        return draft

    def get(self, plugin_id: str) -> RegisteredPlugin:
        item = self._items.get(plugin_id)
        if item is None:
            raise NotFoundError(
                f"插件不存在: {plugin_id}", details={"plugin_id": plugin_id}
            )
        return item

    def has(self, plugin_id: str) -> bool:
        return plugin_id in self._items

    def __contains__(self, plugin_id: object) -> bool:
        return isinstance(plugin_id, str) and plugin_id in self._items

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[RegisteredPlugin]:
        for plugin_id in self._ordered:
            yield self._items[plugin_id]

    @property
    def order(self) -> tuple[str, ...]:
        """Topologically sorted plugin ids (dependencies first)."""

        return tuple(self._topology)

    @property
    def host_api_version(self) -> int:
        return HOST_API_VERSION

    def instances(self) -> list[tuple[str, Any]]:
        """Enabled plugins that have a concrete instance, in dependency order."""

        return [
            (plugin_id, self._items[plugin_id].instance)
            for plugin_id in self._ordered
            if self._items[plugin_id].instance is not None
        ]

    # ---------------------------------------------------------------- descriptors
    def descriptor(self, plugin_id: str) -> dict[str, Any]:
        return self.get(plugin_id).descriptor()

    def descriptors(self, *, enabled_only: bool = True) -> list[dict[str, Any]]:
        """Public descriptors for ``GET /api/plugins`` (no server config)."""

        plugins: Iterable[str] = self._ordered if enabled_only else self._topology
        return [self._items[plugin_id].descriptor() for plugin_id in plugins]

    def summary(self) -> dict[str, Any]:
        """Aggregate view used by ``GET /api/runtime``."""

        capabilities: list[str] = []
        workflows: list[dict[str, Any]] = []
        panels: list[dict[str, Any]] = []
        for plugin_id in self._ordered:
            manifest = self._items[plugin_id].manifest
            capabilities.extend(manifest.capabilities)
            workflows.extend(manifest.to_dict()["workflows"])
            panels.extend(manifest.to_dict()["panels"])
        return {
            "host_api_version": HOST_API_VERSION,
            "count": len(self._ordered),
            "order": list(self._ordered),
            "capabilities": sorted(set(capabilities)),
            "workflows": workflows,
            "panels": panels,
            "plugins": self.descriptors(),
        }

    def validate(self) -> list[str]:
        """Re-run all manifest + dependency checks and return the load order.

        Used by startup/readiness; raises :class:`PluginContractError` on the
        first problem so a broken *required* plugin blocks readiness.
        """

        for plugin_id in list(self._items):
            item = self._items[plugin_id]
            for dependency in item.manifest.depends_on:
                if dependency not in self._items:
                    raise PluginContractError(
                        f"插件 {plugin_id} 依赖的 {dependency} 未注册",
                        details={"plugin_id": plugin_id, "missing": dependency},
                    )
        for plugin_id in self._topology:
            self._items[plugin_id].manifest.validate()
        self._rebuild()
        for plugin_id in self._ordered:
            if any(not self._items[dep].enabled for dep in self._items[plugin_id].manifest.depends_on):
                raise PluginContractError("Enabled plugin depends on a disabled plugin")
        self._workflow_map(require_instances=True)
        return list(self._topology)

    @property
    def missing_dependencies(self) -> list[tuple[str, str]]:
        """``(plugin_id, dependency)`` pairs whose dependency is not registered."""

        return [
            (plugin_id, dependency)
            for plugin_id, item in self._items.items()
            for dependency in item.manifest.depends_on
            if dependency not in self._items
        ]


def build_default_registry() -> PluginRegistry:
    """Registry preloaded with the built-in plugins, in dependency order."""

    registry = PluginRegistry()
    registry.register_builtins()
    registry.freeze()
    return registry


__all__ = [
    "BUILTIN_PLUGINS",
    "PluginFactory",
    "PluginRegistry",
    "RegisteredPlugin",
    "RegisteredWorkflow",
    "build_default_registry",
]
