"""FastAPI 依赖：从 ``app.state`` 取共享对象。"""

from __future__ import annotations

from typing import Any

from fastapi import Request

from control_plane.allocator import Allocator
from control_plane.config import ControlPlaneConfig
from control_plane.lifecycle import SlotLifecycle
from control_plane.ports import RuntimeAdapter
from control_plane.store import ControlPlaneStore


def get_config(request: Request) -> ControlPlaneConfig:
    return request.app.state.config


def get_store(request: Request) -> ControlPlaneStore:
    return request.app.state.store


def get_adapter(request: Request) -> RuntimeAdapter:
    return request.app.state.adapter


def get_lifecycle(request: Request) -> SlotLifecycle:
    return request.app.state.lifecycle


def get_allocator(request: Request) -> Allocator:
    return request.app.state.allocator


def get_state(request: Request) -> Any:
    return request.app.state
