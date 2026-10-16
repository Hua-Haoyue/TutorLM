"""基础设施层：sqlite、storage 与 providers 的具体实现。

只有 ``host`` 组合根实例化这些实现；``domain`` 与 ``application`` 不 import 本包。
"""

from __future__ import annotations

__all__ = ["providers", "settings", "sqlite", "storage"]
