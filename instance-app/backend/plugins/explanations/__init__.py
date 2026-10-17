"""Explanations plugin: node question -> explanation draft."""

from __future__ import annotations

from typing import Any

from backend.plugin_contracts import Draft, PanelDeclaration, PluginManifest, WorkflowDeclaration, WorkflowInput
from backend.plugin_contracts.validation import checkpoint
from backend.application.services.model_options import MODEL_OPTIONS_SCHEMA

INPUT_SCHEMA = {
    "type": "object", "required": ["node_id"], "additionalProperties": False,
    "properties": {
        "node_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "question": {"type": ["string", "null"], "maxLength": 1000},
        "response_mode": {"type": "string", "enum": ["auto", "expand"]},
        "preferences": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "style": {"type": "string", "enum": ["auto", "plain", "rigorous", "example", "analogy", "socratic"]},
                "length": {"type": "string", "enum": ["short", "medium", "long"]},
                "language": {"type": "string", "enum": ["auto", "zh", "en"]},
                "model": {"type": "object", "additionalProperties": False, "properties": {
                    "effort": {"type": "string", "enum": ["auto", "low", "medium", "high"]},
                    "web_search": {"type": "boolean"}}},
                "code_context": {"type": "boolean"},
            },
        },
        "purpose": {"type": "string", "enum": ["explain", "optimize"]},
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
            preferences=data.get("preferences"),
        )
        if data.get('purpose') == 'optimize':
            node = context.capability('knowledge.read').nodes.get(data.get('node_id'))
            if node and not node.parent_id:
                entity.learning['feynman_eligible'] = True
        checkpoint(context)
        return Draft(
            entity=entity, result=entity.to_dict(), provider=entity.provider,
            model=entity.model, artifact_type="explanation",
        )

class GraphRelationsWorkflow:
    id = 'graph_relations.v1'
    kind = 'graph_relations'

    def run(self, context: Any, data: WorkflowInput) -> Draft:
        checkpoint(context)
        result = context.container.context.graph_relations.preview(data.get('document_id'))
        checkpoint(context)
        return Draft(result=result)


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
        ),
        WorkflowDeclaration(
            id='graph_relations.v1', kind='graph_relations', title='AI 梳理关系',
            input_schema={'type': 'object', 'required': ['document_id'], 'additionalProperties': False,
                          'properties': {'document_id': {'type': 'string', 'minLength': 1, 'maxLength': 128}, 'model': MODEL_OPTIONS_SCHEMA}},
            required_capabilities=['knowledge.edges.read', 'models.generate'],
        ),
    ],
    panels=[PanelDeclaration(id="explanation", title="解释", slot="right", order=20)],
)


class ExplanationsPlugin:
    manifest = MANIFEST

    def __init__(self) -> None:
        workflow = ExplanationWorkflow()
        graph = GraphRelationsWorkflow()
        self.workflows = {workflow.id: workflow, graph.id: graph}

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
