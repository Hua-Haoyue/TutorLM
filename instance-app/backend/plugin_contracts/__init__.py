"""插件**契约**：``kind`` / ``workflow`` / 输入输出 schema / ``enabled``。

单一真值约定（避免两套契约）：

- 插件 manifest 的**具体实现与 JSON schema** 在 ``backend.plugins.manifests``
  （静态插件 ``explanation`` / ``narration`` 的注册与生命周期）；
- ``kind`` 与 ``workload`` 的**取名真值**在 ``backend.domain.enums``
  （``RunKind`` / ``RunWorkload``），因为它们是 run 状态机的输入；
- 本包只做**契约形状的转发与校验**，不复制 schema、不自己定义第二套取值。

因此上层既可以 ``from backend.plugin_contracts import EXPLANATION_MANIFEST``，
也可以直接引用 ``backend.plugins.manifests``，两者指向**同一个对象**。
"""

from __future__ import annotations

from typing import Any

from backend.domain.enums import RunKind, RunWorkload

#: workflow 目前只编排这两种 kind（与 ``domain.enums.RunKind`` 同一真值）。
PLUGIN_KINDS: tuple[str, ...] = tuple(str(item) for item in RunKind)

#: 插件 workflow 的 workload 必须是 ``model``：抽取/编译各有独立 executor，
#: 不通过插件编排（设计 §5/§16）。
PLUGIN_WORKLOAD: str = str(RunWorkload.MODEL)


def __getattr__(name: str) -> Any:
    """按需转发 ``plugins.manifests`` 的契约对象。

    用惰性转发而不是顶层 import：``plugins`` 包会 import
    ``application.format`` 与 provider 端口，顶层导入会把契约包和适配器实现
    绑在一起（契约应当比实现更稳定）。
    """

    if name in {"EXPLANATION_MANIFEST", "NARRATION_MANIFEST", "PluginManifest", "WorkflowSpec", "PluginRegistry"}:
        from backend.plugins import manifests

        return getattr(manifests, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def validate_contract_shape(manifest: Any) -> None:
    """最小形状校验：转发给 ``plugins.manifests`` 的同一实现，不另立规则。"""

    from backend.plugins.manifests import _validate_manifest

    _validate_manifest(manifest)


__all__ = [
    "EXPLANATION_MANIFEST",
    "NARRATION_MANIFEST",
    "PLUGIN_KINDS",
    "PLUGIN_WORKLOAD",
    "PluginManifest",
    "PluginRegistry",
    "WorkflowSpec",
    "validate_contract_shape",
]
