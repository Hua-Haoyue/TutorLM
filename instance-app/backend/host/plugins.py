"""The Host uses the plugin SDK registry directly."""
from backend.host.registry import PluginRegistry, build_default_registry
from backend.plugin_contracts import PluginContractError


def build_registry(enabled_names: tuple[str, ...]) -> PluginRegistry:
    registry = PluginRegistry().register_builtins()
    selected = set(enabled_names)
    if selected - {item.id for item in registry}:
        raise PluginContractError('Unknown enabled plugin ID')
    for item in registry:
        item.enabled = item.id in selected
    registry.freeze()
    return registry


__all__ = ['PluginRegistry', 'build_default_registry', 'build_registry']
