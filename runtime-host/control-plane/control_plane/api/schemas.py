"""API 的请求/响应 DTO。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class LeaseCreateRequest(BaseModel):
    """``POST /control/leases`` 的请求体。"""

    label: str = Field(default="", max_length=64)


class LeaseCreateResponse(BaseModel):
    """分配成功。``token`` 只在这里出现一次，之后无法再取回。"""

    lease: dict[str, Any]
    token: str
    #: 浏览器应当访问的入口；未配置 public_base_url 时为相对路径。
    instance_url: str
    #: 网关/前端把它放进 ``Authorization: Bearer`` 即可。
    token_type: str = "Bearer"
    #: ``True`` 表示复用了请求里已带的凭据，没有新占槽位（幂等）。
    resumed: bool = False


class LeaseCurrentResponse(BaseModel):
    lease: dict[str, Any]
    instance_url: str


class CapacityResponse(BaseModel):
    """容量概览。

    字段名与 ``REVIEW-CHECKLIST.md`` 冻结的建议 DTO 对齐（``slots_ready`` /
    ``slots_starting`` / ``leases_active``），同时额外给出更细的占用阶段，
    便于运维判断"卡在哪一步"。``slots_ready`` 与 ``slots_free`` 同义：
    现在就能分配给新访客的槽位数。
    """

    slots_total: int
    slots_free: int
    slots_ready: int
    slots_starting: int
    slots_active: int
    slots_draining: int
    slots_reclaiming: int
    slots_quarantined: int
    leases_live: int
    leases_active: int
    accepting: bool


class SlotListResponse(BaseModel):
    items: list[dict[str, Any]]


class AdminSlotResponse(BaseModel):
    slot: dict[str, Any]


class ReconcileResponse(BaseModel):
    report: dict[str, Any]


class GcResponse(BaseModel):
    report: dict[str, Any]


class HealthResponse(BaseModel):
    status: str
    adapter: str
    slots_total: int
    slots_free: int
