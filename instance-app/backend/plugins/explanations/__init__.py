"""Explanations plugin: node question -> explanation draft."""

from __future__ import annotations

from typing import Any

from backend.plugin_contracts import Draft, PanelDeclaration, PluginManifest, WorkflowDeclaration, WorkflowInput
from backend.plugin_contracts.validation import checkpoint

INPUT_SCHEMA = {
    "type": "object", "required": ["node_id"], "additionalProperties": False,
    "properties": {
        "node_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "question": {"type": ["string", "null"], "maxLength": 1000},
        "response_mode": {"type": "string", "enum": ["auto", "expand"]},
    },
}


class ExplanationWorkflow:
    id = "explanation.v1"
    kind = "explanation"

    def run(self, context: Any, data: WorkflowInput) -> Draft:
        checkpoint(context)
        entity = context.container.explanations.explain(
            node_id=data.get("node_id"), question=data.get("question"), persist=False,
            response_mode=data.get("response_mode", "auto"),
        )
        checkpoint(context)
        return Draft(
            entity=entity, result=entity.to_dict(), provider=entity.provider,
            model=entity.model, artifact_type="explanation",
        )

MANIFEST = PluginManifest.build(
    "explanations",
    "0.1.0",
    title="解释",
    description="针对节点生成解释草稿（调用模型能力）。",
    depends_on=("documents",),
    capabilities=[
        "documents.read",
        "knowledge.read",
        "knowledge.write",
        "knowledge.nodes.read",
        "knowledge.nodes.write",
        "knowledge.edges.read",
        "knowledge.edges.write",
        "models.generate",
    ],
    workflows=[
        WorkflowDeclaration(
            id="explanation.v1",
            kind="explanation",
            title="解释节点",
            input_schema=INPUT_SCHEMA,
            required_capabilities=["knowledge.write", "models.generate"],
        )
    ],
    panels=[PanelDeclaration(id="explanation", title="解释", slot="right", order=20)],
)


class ExplanationsPlugin:
    manifest = MANIFEST

    def __init__(self) -> None:
        workflow = ExplanationWorkflow()
        self.workflows = {workflow.id: workflow}

    def register_services(self, host: Any) -> None:  # pragma: no cover - no-op
        return None

    def register_routes(self, router: Any, host: Any) -> None:  # pragma: no cover
        return None

    async def startup(self, host: Any) -> None:  # pragma: no cover - no-op
        return None

    async def shutdown(self, host: Any) -> None:  # pragma: no cover - no-op
        return None


def build() -> ExplanationsPlugin:
    return ExplanationsPlugin()


__all__ = ["MANIFEST", "ExplanationWorkflow", "ExplanationsPlugin", "build"]
