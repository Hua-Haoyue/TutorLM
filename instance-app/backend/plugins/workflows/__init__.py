"""静态插件 workflow：explanation / narration。

只做编排：**输入校验 → 取 context（已由 RefResolver 解析）→ 调 provider → 校验输出**。
不含模型逻辑、不渲染、不管理文件生命周期、不直接读库。
"""

from __future__ import annotations

from backend.plugins.explanations import ExplanationWorkflow
from backend.plugins.knowledge_narration import NarrationWorkflow, RetellingWorkflow

__all__ = ['ExplanationWorkflow', 'NarrationWorkflow', 'RetellingWorkflow']
