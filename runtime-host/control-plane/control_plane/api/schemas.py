"""API 的请求/响应 DTO。"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class LeaseCreateRequest(BaseModel):
    """``POST /control/leases`` 的请求体。"""

    label: str = Field(default="", max_length=64)
    workspace_id: UUID | None = None
    #: 访客填写的邀请码。服务端只从中推导权限与优先级，
    #: **不会**接受任何客户端传入的 priority 字段。
    invitation_code: str | None = Field(default=None, max_length=128)


class UserLoginRequest(BaseModel):
    user_id: UUID
    recovery_token: str = Field(min_length=1, max_length=256)


class WorkspaceCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200, pattern=r'\S')


class LeasePageRequest(BaseModel):
    page_id: UUID


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


class AccessPolicyResponse(BaseModel):
    """公开的能力探针：只回答"需不需要邀请码"与哪些能力启用。

    不包含任何码、配置或内部计数，因此可以对未认证的前端公开。
    """

    invitation_only: bool
    system_invitation_supported: bool = True
    project_sharing_supported: bool = True


class InvitationCreateRequest(BaseModel):
    label: str = Field(default="", max_length=64)


class ShareInvitationCreateRequest(BaseModel):
    label: str | None = Field(default=None, max_length=64)


class WorkspaceImportRequest(BaseModel):
    invitation_code: str = Field(min_length=1, max_length=128)
    title: str | None = Field(default=None, max_length=200)


class InvitationListResponse(BaseModel):
    items: list[dict[str, Any]]


class WorkspaceImportResponse(BaseModel):
    workspace: dict[str, Any]
    #: 命中已有副本时为 ``True``（幂等或默认去重）。
    reused: bool = False
