"""控制面路由集合。"""

from control_plane.api.routers import admin, health, invitations, leases

__all__ = ["admin", "health", "invitations", "leases"]
