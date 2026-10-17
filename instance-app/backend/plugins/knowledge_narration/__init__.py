"""Knowledge-narration plugin: node snapshot -> narrated walkthrough draft."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from backend.application.ports.model_provider import NarrationRequest, NarrationStyle
from backend.domain.errors import InvalidInputError, NotFoundError, ProviderError
from backend.domain.models import Narration
from backend.domain.narration import build_segments
from backend.plugin_contracts import Draft, PanelDeclaration, PluginManifest, WorkflowDeclaration, WorkflowInput
from backend.plugin_contracts.validation import checkpoint
from backend.application.services import retelling_service
from backend.application.services.model_options import MODEL_OPTIONS_SCHEMA

MAX_RUN_NODES = 10
INPUT_SCHEMA = {
    "type": "object", "required": ["project_id"], "additionalProperties": False,
    "properties": {
        "model": MODEL_OPTIONS_SCHEMA,
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

    @staticmethod
    def _is_root(node) -> bool:
        source = (node.metadata or {}).get("source") or {}
        return not (source.get("fromNode") or node.parent_id)

    @staticmethod
    def _latest_explanation(ports, node):
        explanations = getattr(ports, "explanations", None)
        if explanations is None:
            return None
        history = explanations.list_for_node(node.id)
        return history[0] if history else None

    @classmethod
    def _stitched_root(cls, ports, roots):
        chunks = []
        root_data = []
        for index, node in enumerate(roots, 1):
            explanation = cls._latest_explanation(ports, node)
            source = (node.content or node.summary or "").strip()
            answer = (getattr(explanation, "answer", "") or "").strip()
            root_data.append({'node_id': node.id, 'title': node.title,
                              'source': source, 'explanation': answer,
                              'explanation_id': getattr(explanation, 'id', None)})
            chunks.append(
                f"根节点 {index}：{node.title}\n"
                f"原文片段：{source[:2400]}\n"
                + (f"最新解释（如已优化则使用优化版）：{answer[:4200]}" if answer
                   else "暂无解释：请直接结合根节点名称和原文串讲，不要求先生成解释。")
            )
        original_context = ports.explanations.narration_passage(roots)
        first = roots[0]
        return replace(first, title="根节点串讲", content=(
            "请按根节点顺序组织一条因果或递进逻辑链；只使用这些根节点，忽略所有子节点。\n"
            + "\n\n".join(chunks)
        ), summary="根节点之间的因果/递进串讲",
            metadata={'global_narration': {'roots': root_data, 'original_context': original_context}})

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
        nodes = [node for node in nodes if self._is_root(node)]
        if not nodes or len(nodes) > MAX_RUN_NODES:
            raise InvalidInputError("Narration requires between 1 and 10 nodes")
        if any(node.project_id != project_id for node in nodes):
            raise InvalidInputError("Narration nodes must belong to the project")
        if len({node.id for node in nodes}) != len(nodes):
            raise InvalidInputError('串讲不能重复选择同一个根节点。')
        if len({node.document_id for node in nodes}) != 1:
            raise InvalidInputError('串讲只能使用同一文档的根节点。')
        checkpoint(context)
        stitched = self._stitched_root(ports, nodes)
        paragraphs = list(ports.provider.narrate(NarrationRequest(
            project_title=project.title, nodes=[stitched],
            style=NarrationStyle(language=data.get("language", "zh"), tone=data.get("tone", "teacher")),
        )))
        checkpoint(context)
        if len(paragraphs) != 1 or not paragraphs[0].strip():
            raise ProviderError("Provider must return one nonempty stitched narration")
        entity = Narration.create(
            project_id=project_id, title=(data.get("title") or "").strip() or project.title + " narration",
            segments=build_segments([stitched], paragraphs),
            provider=ports.provider.capabilities.name, model=ports.provider.capabilities.model,
        )
        def validate():
            if self._stitched_root(ports, nodes).metadata != stitched.metadata:
                from backend.domain.errors import ConflictError
                raise ConflictError('根节点解释在串讲生成期间已更新，请重新生成串讲。')
        return Draft(entity=entity, result=entity.to_dict(), provider=entity.provider,
                     model=entity.model, artifact_type="narration", precommit=validate)


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
                          "properties": {"node_id": {"type": "string", "minLength": 1, "maxLength": 128}, "model": MODEL_OPTIONS_SCHEMA}},
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
