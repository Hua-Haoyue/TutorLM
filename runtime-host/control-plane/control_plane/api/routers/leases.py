"""访客租约接口与网关准入（admission）。

``POST /control/leases`` 是申请计算资源的入口；这里
不信任任何客户端传入的槽位/上游地址：槽位由分配器选，upstream 由控制面
在容器就绪后回填。

匿名身份由 **租约令牌** 承载，两个通道等价：

* ``Cookie: <lease_cookie_name>`` —— 浏览器默认通道，``HttpOnly``，前端 JS
  读不到；
* ``Authorization: Bearer <token>`` —— 兼容脚本、测试与旧客户端。

两者同时出现时以 cookie 为准（cookie 是服务端下发的可信副本，而 Bearer
可能来自更早的手工调用）。令牌只在签发响应里出现一次，数据库只存哈希。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Header, Request, Response, status

from control_plane.allocator import Allocator
from control_plane.api.dependencies import get_allocator, get_config
from control_plane.api.schemas import (
    LeaseCreateRequest,
    LeaseCreateResponse,
    LeaseCurrentResponse,
)
from control_plane.config import ControlPlaneConfig
from control_plane.errors import (
    ControlPlaneError,
    InvalidToken,
    UntrustedUpstream,
)
from control_plane.security import bearer_token
from control_plane.api.routers.workspaces import current_user
from control_plane.errors import SlotBusy

router = APIRouter(prefix="/control/leases", tags=["leases"])
admission_router = APIRouter(prefix="/control", tags=["admission"])


def _credential(
    request: Request, config: ControlPlaneConfig, authorization: str | None
) -> str | None:
    """按 cookie -> Bearer 的顺序取访客凭据。

    cookie 名可配置，所以这里读 ``request.cookies`` 而不是用 ``Cookie(...)``
    参数——后者在路由定义时就把名字固定死了。
    """

    cookie = request.cookies.get(config.lease_cookie_name)
    if cookie and cookie.strip():
        return cookie.strip()
    return bearer_token(authorization)


def _set_lease_cookie(
    response: Response, config: ControlPlaneConfig, token: str
) -> None:
    """下发/刷新租约 cookie。

    刻意不带 ``domain``：只作用于当前主机，避免把租约泄漏给部署在同一
    父域下的其他站点。``path=/`` 让工作台、业务 API、SSE 与上传都能带上它。
    """

    if not token:
        return
    response.set_cookie(
        key=config.lease_cookie_name,
        value=token,
        max_age=config.cookie_max_age(),
        httponly=True,
        secure=config.lease_cookie_secure,
        samesite=config.lease_cookie_samesite,
        path="/",
    )


def _clear_lease_cookie(response: Response, config: ControlPlaneConfig) -> None:
    response.delete_cookie(
        key=config.lease_cookie_name,
        path="/",
        secure=config.lease_cookie_secure,
        httponly=True,
        samesite=config.lease_cookie_samesite,
    )


def _instance_url(config: ControlPlaneConfig) -> str:
    """浏览器应当访问的入口。

    未配置 ``public_base_url`` 时返回相对路径 ``/``，让同源网关沿用当前
    主机；一旦配置就以配置为准（例如 https://host:47823）。
    """

    if config.public_base_url:
        return config.public_base_url
    return "/"


def _assert_controlled_upstream(config: ControlPlaneConfig, lease, upstream: str) -> str:
    """确认 upstream 确实是本槽位本代次的受控地址。

    网关会无条件信任 admission 返回的 upstream，所以这里必须挡住"数据库里
    被写入了一个外部地址"这种污染：只接受
    ``http://<container_prefix>-s<slot:02d>-g<generation:04d>:<instance_port>``。
    """

    expected = f"http://{config.container_name(lease.slot_id, lease.generation)}:{config.instance_port}"
    if upstream != expected:
        # 绝不把可疑地址交给网关转发。这里 fail closed 而不是回退。
        raise UntrustedUpstream(
            "租约 upstream 与受控实例地址不一致，已拒绝转发",
            details={"lease_id": lease.lease_id, "slot_id": lease.slot_id},
        )
    return expected


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=LeaseCreateResponse,
    summary="申请一次匿名试用（分配槽位并启动实例）",
)
def create_lease(
    request: Request,
    response: Response,
    payload: LeaseCreateRequest | None = None,
    authorization: str | None = Header(default=None),
    allocator: Allocator = Depends(get_allocator),
    config: ControlPlaneConfig = Depends(get_config),
) -> dict:
    """申请试用，并在成功时把令牌写进 HttpOnly cookie。

    幂等性（验收项 T1.2）：如果请求已带一个有效的访客凭据（cookie 或
    Bearer），直接返回该租约的当前状态，不再占用新槽位。响应里的 ``token``
    字段为兼容既有客户端而保留；对 cookie 流程来说它等于客户端自己刚发来
    的值，没有新增泄漏面。
    """

    label = payload.label if payload is not None else ""
    invitation_code = payload.invitation_code if payload is not None else None
    workspace_id = str(payload.workspace_id) if payload is not None and payload.workspace_id is not None else None
    user_id = None
    if workspace_id is not None:
        service = request.app.state.workspaces
        user_id = current_user(request, config, service)['user_id']
        service.get(workspace_id, user_id)
    credential = _credential(request, config, authorization)

    # 入场判定统一在这里：resume 也不能绕过 invitation-only，否则一个旧
    # Cookie 就能无限续用而不出示邀请码。显式提供的无效 code 必须被拒绝，
    # 不能静默退化成"无码入场"。
    admission = allocator.admit(invitation_code, workspace_id=workspace_id, user_id=user_id)

    # 先看是否已有可复用租约。如果有，就地**提升优先级**（CAS）后返回，
    # 绝不新占槽位。否则一个浏览器在已有普通租约时再出示系统码，
    # 会同时占两个槽位而且旧租约仍是低优先级。
    existing = allocator.resume(credential)
    if existing is not None:
        if workspace_id is not None and existing.workspace_id != workspace_id:
            raise SlotBusy('Release the current compute lease before switching workspaces')
        if admission.priority > existing.priority:
            existing = allocator.promote(existing.lease_id, admission.priority)
        _set_lease_cookie(response, config, credential or "")
        return {
            "lease": existing.to_dict(),
            "token": credential or "",
            "instance_url": _instance_url(config),
            "token_type": "Bearer",
            "resumed": True,
        }

    result = allocator.allocate(
        label=label, workspace_id=workspace_id, user_id=user_id, priority=admission.priority
    )
    _set_lease_cookie(response, config, result.token)
    return {
        "lease": result.lease.to_dict(),
        "token": result.token,
        "instance_url": _instance_url(config),
        "token_type": "Bearer",
        "resumed": False,
    }


@router.get(
    "/current",
    response_model=LeaseCurrentResponse,
    summary="查询当前租约状态",
)
def current_lease(
    request: Request,
    response: Response,
    authorization: str | None = Header(default=None),
    allocator: Allocator = Depends(get_allocator),
    config: ControlPlaneConfig = Depends(get_config),
) -> dict:
    credential = _credential(request, config, authorization)
    lease = allocator.authenticate(credential)
    # 每次成功访问都续期浏览器 cookie，让活跃用户不会因为客户端计时先到期
    # 而丢掉身份；服务端 TTL 仍是唯一的权威判定。
    _set_lease_cookie(response, config, credential or "")
    return {"lease": lease.to_dict(), "instance_url": _instance_url(config)}


@router.delete(
    "/current",
    status_code=status.HTTP_202_ACCEPTED,
    summary="主动结束试用（撤销 + 回收槽位）",
)
def release_current_lease(
    request: Request,
    authorization: str | None = Header(default=None),
    allocator: Allocator = Depends(get_allocator),
    config: ControlPlaneConfig = Depends(get_config),
) -> Response:
    credential = _credential(request, config, authorization)
    lease = allocator.authenticate(credential)
    final = allocator.release(lease.lease_id, reason="visitor_released")
    response = Response(
        status_code=status.HTTP_202_ACCEPTED,
        content=json.dumps({"lease_id": final.lease_id, "state": str(final.state)}),
        media_type="application/json",
    )
    # 服务端此刻已经撤销租约，清 cookie 只是顺手把浏览器的残留状态也抹掉。
    _clear_lease_cookie(response, config)
    return response


@admission_router.get(
    "/admission",
    include_in_schema=False,
    summary="网关准入（Nginx auth_request 调用，只返回受控路由头）",
)
def admission(
    request: Request,
    authorization: str | None = Header(default=None),
    allocator: Allocator = Depends(get_allocator),
    config: ControlPlaneConfig = Depends(get_config),
) -> Response:
    """回答"这个请求该转发到哪个实例"。

    客户端只能提供凭据；``slot``/``generation``/``upstream`` 一律由服务端
    从租约推导，因此伪造请求无法把流量指向别人的容器。

    无效、过期、已撤销、仍在启动中的租约统一返回 **401**——网关侧只需要
    "放行/拒绝"两种结果，让状态差异留在 ``/control/leases/current``。
    """

    credential = _credential(request, config, authorization)
    try:
        lease, upstream = allocator.resolve_target(credential)
    except ControlPlaneError as exc:
        raise InvalidToken(exc.message or "访客凭据无效") from exc

    upstream = _assert_controlled_upstream(config, lease, upstream)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.headers["X-TutorLM-Lease"] = lease.lease_id
    response.headers["X-TutorLM-Slot"] = str(lease.slot_id)
    response.headers["X-TutorLM-Generation"] = str(lease.generation)
    response.headers["X-TutorLM-Upstream"] = upstream
    return response
