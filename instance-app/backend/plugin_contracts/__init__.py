"""Host plugin SDK: the stable static plugin contract (v1).

This package is deliberately dependency-light so both the Host and the
build-time plugins can import it without cycles. It defines manifests, the
restricted plugin contexts, the workflow/draft contract and the shared result
DTOs (docs/plugin-sdk.md).
"""

from backend.plugin_contracts.context import (
    Cancellation,
    HostContext,
    RunContext,
    ScopedContext,
)
from backend.plugin_contracts.manifest import (
    HOST_API_VERSION,
    KNOWN_CAPABILITIES,
    PanelDeclaration,
    PluginContractError,
    PluginManifest,
    WorkflowDeclaration,
    is_valid_plugin_id,
    is_valid_version,
    manifest_from_dict,
)
from backend.plugin_contracts.result import OperationResult, RunResult
from backend.plugin_contracts.workflow import Draft, Workflow, WorkflowInput

__all__ = [
    "Cancellation",
    "Draft",
    "HOST_API_VERSION",
    "HostContext",
    "KNOWN_CAPABILITIES",
    "OperationResult",
    "PanelDeclaration",
    "PluginContractError",
    "PluginManifest",
    "RunContext",
    "RunResult",
    "ScopedContext",
    "Workflow",
    "WorkflowDeclaration",
    "WorkflowInput",
    "is_valid_plugin_id",
    "is_valid_version",
    "manifest_from_dict",
]
