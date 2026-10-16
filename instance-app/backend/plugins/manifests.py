"""插件 manifest 与注册表。

插件契约：``kind``、``workflow``、输入/输出 schema、``enabled``（设计 §7）。
静态插件 ``explanation`` / ``narration`` 只做编排，不含模型逻辑。

本模块**不**读环境、**不**装配 provider；只描述与校验 manifest 形状，
供 host 的插件生命周期与 ``/api/runtime`` 发现使用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

VALID_KINDS = ("explanation", "narration")

_EXPLANATION_INPUT = {
    "type": "object",
    "properties": {
        "refs": {"type": "array", "minItems": 1, "items": {"type": "object"}},
        "node_id": {"type": "string"},
        "question": {"type": "string"},
        "budget": {"type": "object"},
    },
    "required": ["refs"],
    "additionalProperties": False,
}

_EXPLANATION_OUTPUT = {
    "type": "object",
    "properties": {
        "explanation": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string"},
                "question": {"type": "string"},
                "answer": {"type": "string"},
                "provider": {"type": "string"},
                "model": {"type": "string"},
                "created_at": {"type": "string"},
            },
            "required": ["answer", "provider", "model"],
        }
    },
    "required": ["explanation"],
}

_NARRATION_INPUT = {
    "type": "object",
    "properties": {
        "refs": {"type": "array", "minItems": 1, "items": {"type": "object"}},
        "node_ids": {"type": "array", "maxItems": 10, "items": {"type": "string"}},
        "title": {"type": "string"},
        "budget": {"type": "object"},
    },
    "required": ["refs", "node_ids"],
    "additionalProperties": False,
}

_NARRATION_OUTPUT = {
    "type": "object",
    "properties": {
        "narration": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "title": {"type": "string"},
                "segments": {"type": "array", "items": {"type": "object"}},
                "segment_count": {"type": "integer"},
                "total_duration_seconds": {"type": "number"},
                "provider": {"type": "string"},
                "model": {"type": "string"},
                "created_at": {"type": "string"},
            },
            "required": ["segments", "segment_count", "total_duration_seconds"],
        }
    },
    "required": ["narration"],
}


@dataclass(frozen=True, slots=True)
class WorkflowSpec:
    kind: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    workload: str = "model"


@dataclass(frozen=True, slots=True)
class PluginManifest:
    name: str
    version: str
    kind: str
    workflows: tuple[WorkflowSpec, ...]
    enabled: bool = True
    description: str = ""

    def to_runtime_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "workflows": [{"kind": wf.kind} for wf in self.workflows],
        }


def _validate_manifest(manifest: PluginManifest) -> None:
    if manifest.kind not in VALID_KINDS:
        raise ValueError(f"未知插件 kind：{manifest.kind!r}")
    if not manifest.workflows:
        raise ValueError(f"插件 {manifest.name!r} 至少需要一个 workflow")
    for wf in manifest.workflows:
        if wf.kind not in VALID_KINDS:
            raise ValueError(f"未知 workflow kind：{wf.kind!r}")
        if wf.workload != "model":
            raise ValueError("explanation/narration workflow 的 workload 必须是 model")


EXPLANATION_MANIFEST = PluginManifest(
    name="explanation",
    version="0.1.0",
    kind="explanation",
    description="划词解释：取 SourceRef context → 调 provider → 返回 explanation",
    workflows=(
        WorkflowSpec(
            kind="explanation",
            input_schema=_EXPLANATION_INPUT,
            output_schema=_EXPLANATION_OUTPUT,
            workload="model",
        ),
    ),
)

NARRATION_MANIFEST = PluginManifest(
    name="narration",
    version="0.1.0",
    kind="narration",
    description="串讲：取多个 SourceRef context → 逐段调 provider → 返回 narration",
    workflows=(
        WorkflowSpec(
            kind="narration",
            input_schema=_NARRATION_INPUT,
            output_schema=_NARRATION_OUTPUT,
            workload="model",
        ),
    ),
)


class PluginRegistry:
    """插件注册与生命周期（启用/禁用、kind/workflow 发现）。"""

    def __init__(self, manifests: tuple[PluginManifest, ...] | None = None) -> None:
        self._plugins: dict[str, PluginManifest] = {}
        for manifest in manifests or (EXPLANATION_MANIFEST, NARRATION_MANIFEST):
            self.register(manifest)

    def register(self, manifest: PluginManifest) -> None:
        _validate_manifest(manifest)
        self._plugins[manifest.name] = manifest

    def get(self, name: str) -> PluginManifest | None:
        return self._plugins.get(name)

    def enabled(self) -> tuple[PluginManifest, ...]:
        return tuple(p for p in self._plugins.values() if p.enabled)

    def set_enabled(self, name: str, enabled: bool) -> None:
        manifest = self._plugins[name]
        self._plugins[name] = PluginManifest(
            name=manifest.name,
            version=manifest.version,
            kind=manifest.kind,
            description=manifest.description,
            enabled=enabled,
            workflows=manifest.workflows,
        )

    def workflow_for_kind(self, kind: str) -> WorkflowSpec | None:
        for manifest in self._plugins.values():
            if not manifest.enabled:
                continue
            for wf in manifest.workflows:
                if wf.kind == kind:
                    return wf
        return None

    def runtime_view(self) -> list[dict[str, Any]]:
        return [p.to_runtime_dict() for p in self._plugins.values()]
