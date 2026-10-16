"""TutorLM 多实例控制面。

本包只负责"容器外"的能力：访客租约、固定槽位分配、容器生命周期与回收。
它不解析文档、不执行 Prompt，也不 import instance-app 的任何私有模块；
与应用实例之间唯一的耦合是 HTTP 契约（见 ../contracts/instance-runtime-v1.md）。
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
