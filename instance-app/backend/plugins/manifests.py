"""Public manifest exports; definitions live exclusively in plugin_contracts."""
from backend.plugin_contracts import PluginManifest, WorkflowDeclaration, PanelDeclaration
from backend.plugins.explanations import MANIFEST as EXPLANATION_MANIFEST
from backend.plugins.knowledge_narration import MANIFEST as NARRATION_MANIFEST

__all__ = ['PluginManifest', 'WorkflowDeclaration', 'PanelDeclaration', 'EXPLANATION_MANIFEST', 'NARRATION_MANIFEST']
