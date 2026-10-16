"""HTTP 层：把分配器能力暴露成控制面 API。"""

from control_plane.api.app import create_app

__all__ = ["create_app"]
