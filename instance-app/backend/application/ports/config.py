"""应用配置端口。

Host 的显式配置对象（``infrastructure/settings.py`` 的 ``Settings``）实现本协议。
端口只暴露用例真正需要读取的字段，避免 service 直接摸 ``os.environ``。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class AppSettings(Protocol):
    """只读配置视图。"""

    @property
    def persistent_workspace(self) -> bool:
        ...

    @property
    def workspace_id(self) -> str | None:
        ...

    @property
    def owner_id(self) -> str | None:
        ...

    @property
    def data_dir(self) -> str:
        ...

    @property
    def session_data_dir(self) -> str:
        ...

    @property
    def workspace_subdirectory(self) -> str:
        ...

    @property
    def provider_name(self) -> str:
        ...
