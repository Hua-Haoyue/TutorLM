"""Knowledge-narration plugin: node snapshot -> narrated walkthrough draft."""

from __future__ import annotations

from typing import Any

from backend.application.ports.model_provider import NarrationRequest, NarrationStyle
from backend.domain.errors import InvalidInputError, NotFoundError, ProviderError
from backend.domain.models import Narration
from backend.domain.narration import build_segments
from backend.plugin_contracts import Draft, PanelDeclaration, PluginManifest, WorkflowDeclaration, WorkflowInput
from backend.plugin_contracts.validation import checkpoint
from backend.application.services import retelling_service

MAX_RUN_NODES = 10
INPUT_SCHEMA = {
    "type": "object", "required": ["project_id"], "additionalProperties": False,
    "properties": {
        "project_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "node_ids": {"type": ["array", "null"], "maxItems": MAX_RUN_NODES,
                     "items": {"type": "string", "minLength": 1, "maxLength": 128}},
        "title": {"type": ["string", "null"], "maxLength": 200},
        "document_id": {"type": ["string", "null"], "maxLength": 128},
        "language": {"type": "string", "minLength": 1, "maxLength": 10},
        "tone": {"type": "string", "enum": ["teacher", "casual"]},
    },
}


class NarrationWorkflow:
    id = "narration.v1"
    kind = "narration"

    def run(self, context: Any, data: WorkflowInput) -> Draft:
        checkpoint(context)
        ports = context.container.context
        project_id = data.get("project_id")
        project = ports.projects.get(project_id)
        if project is None:
            raise NotFoundError("Project not found")
        selected = data.get("node_ids")
        if selected:
            nodes = [ports.nodes.get(node_id) for node_id in selected]
            if any(node is None for node in nodes):
                raise NotFoundError("Node not found")
        else:
            nodes = list(ports.nodes.list_for_project(project_id, document_id=data.get("document_id")))
        if not nodes or len(nodes) > MAX_RUN_NODES:
            raise InvalidInputError("Narration requires between 1 and 10 nodes")
        if any(node.project_id != project_id for node in nodes):
            raise InvalidInputError("Narration nodes must belong to the project")
        checkpoint(context)
        paragraphs = list(ports.provider.narrate(NarrationRequest(
            project_title=project.title, nodes=nodes,
            style=NarrationStyle(language=data.get("language", "zh"), tone=data.get("tone", "teacher")),
        )))
        checkpoint(context)
        if len(paragraphs) != len(nodes) or any(not isinstance(p, str) or not p.strip() for p in paragraphs):
            raise ProviderError("Provider must return one nonempty paragraph per node")
        entity = Narration.create(
            project_id=project_id, title=(data.get("title") or "").strip() or project.title + " narration",
            segments=build_segments(nodes, paragraphs),
            provider=ports.provider.capabilities.name, model=ports.provider.capabilities.model,
        )
        return Draft(entity=entity, result=entity.to_dict(), provider=entity.provider,
                     model=entity.model, artifact_type="narration")


class RetellingWorkflow:
    id = "retelling.v1"
    kind = "retelling"

    def run(self, context: Any, data: WorkflowInput) -> Draft:
        checkpoint(context)
        return retelling_service.run(context, data.get('node_id'))

MANIFEST = PluginManifest.build(
    "knowledge-narration",
    "0.2.0",
    title="知识与串讲",
    description="消费节点快照生成串讲草稿。",
    depends_on=("documents", "explanations"),
    capabilities=["knowledge.read", "knowledge.write", "knowledge.graph.read", "knowledge.nodes.read", "knowledge.edges.read", "models.generate"],
    workflows=[
        WorkflowDeclaration(
            id="narration.v1",
            kind="narration",
            title="生成串讲",
            input_schema=INPUT_SCHEMA,
            required_capabilities=["knowledge.write", "models.generate"],
        ),
        WorkflowDeclaration(
            id="retelling.v1", kind="retelling", title="回讲父节点",
            input_schema={"type": "object", "required": ["node_id"], "additionalProperties": False,
                          "properties": {"node_id": {"type": "string", "minLength": 1, "maxLength": 128}}},
            required_capabilities=["knowledge.read", "knowledge.write", "models.generate"],
        ),
    ],
    panels=[PanelDeclaration(id="knowledge-narration", title="串讲", slot="right", order=30)],
)


class KnowledgeNarrationPlugin:
    manifest = MANIFEST

    def __init__(self) -> None:
        workflows = [NarrationWorkflow(), RetellingWorkflow()]
        self.workflows = {workflow.id: workflow for workflow in workflows}

    def register_services(self, host: Any) -> None:  # pragma: no cover - no-op
        return None

    def register_routes(self, router: Any, host: Any) -> None:  # pragma: no cover
        return None

    async def startup(self, host: Any) -> None:  # pragma: no cover - no-op
        return None

    async def shutdown(self, host: Any) -> None:  # pragma: no cover - no-op
        return None


def build() -> KnowledgeNarrationPlugin:
    return KnowledgeNarrationPlugin()


__all__ = ["MANIFEST", "NarrationWorkflow", "KnowledgeNarrationPlugin", "build"]
