"""邀请码：系统码与分享码的生成、校验与访客权限判定。

一句话边界：这里只做"道具（invitation）"，不做调度。但它是唯一能决定
"这个请求能不能进、以什么优先级进"的地方。

安全不变量：

* 系统码（``TLM-SYS-``）与分享码（``TLM-SHARE-``）前缀、密码空间与域隔离都独立，
  分享码**永远**不会产生最高优先级；
* 明文只在签发响应里出现一次，DB 只存 ``HMAC(pepper, "invitation:kind:code")``；
* 验证失败一律拔同一个错误码，不把"码是否存在"变成枚举 Oracle；
* 任何日志/事件都不得出现明文码。
"""

from __future__ import annotations

import re
import secrets
import uuid
from dataclasses import dataclass
from typing import Any

from control_plane.domain.models import Invitation, InvitationKind, InvitationState
from control_plane.errors import (
    ConfigError,
    Forbidden,
    InvalidInvitation,
    InvitationExpired,
    InvitationRequired,
    NotFound,
)
from control_plane.security import hash_token
from control_plane.store import ControlPlaneStore

#: 系统码前缀。独立于分享码，不可互相提升。
SYSTEM_CODE_PREFIX = "TLM-SYS"
#: 分享码前缀。用于项目分享导入，只授予普通 trial。
SHARE_CODE_PREFIX = "TLM-SHARE"
#: 本模块签发的码格式版本。未来可演进，但不可复用旧版本语义。
CODE_VERSION = "v1"

#: 43 个 base64url 字符 == 32 字节 == 256 bit 熵。低于这个长度的人造
#: 字符串不能作为系统码，否则最高优先级就可被暴力猜测。
_ENTROPY_CHARS = 43
_PAYLOAD_RE = r"[A-Za-z0-9_-]{%d}" % _ENTROPY_CHARS
#: 本模块签发的带版本、至少 256 bit 的格式。
SYSTEM_CODE_RE = re.compile(rf"^{SYSTEM_CODE_PREFIX}-v(\d+)-({_PAYLOAD_RE})$")
SHARE_CODE_RE = re.compile(rf"^{SHARE_CODE_PREFIX}-v(\d+)-({_PAYLOAD_RE})$")

#: 系统码入场后的服务端优先级。客户端提交的任何 priority 都被忽略。
SYSTEM_PRIORITY = 100
#: 普通访客（包括分享导入后的普通 trial）。
DEFAULT_PRIORITY = 0

#: 分享码默认有效期：4 小时。
DEFAULT_SHARE_TTL_SECONDS = 4 * 60 * 60

#: 访客填写邀请码的请求字段上限。
MAX_CODE_LENGTH = 128


def is_system_code_format(code: str) -> bool:
    """是否符合系统码格式。

    全仓只有一个系统码格式：``TLM-SYS-v<digits>-<43 位 base64url>``。
    config 与 CLI 都使用同一个模式，不存在兼容分支。
    """

    return SYSTEM_CODE_RE.match(code) is not None


@dataclass(frozen=True, slots=True)
class AccessPolicy:
    """从配置解析出来的访问策略。

    字段名与 config worker 的 ``ControlPlaneConfig`` 保持一致；当字段尚未落地时
    退化到 ``CONTROL_PLANE_*`` 环境变量，避免两侧合并前出现"开关失效"。
    """

    #: ``True`` 时只有有效邀请码才能申请计算资源。
    invitation_only: bool = False
    #: 运维提前配置的系统码。启动时种入（仅接受合法格式）。
    system_invitation_codes: tuple[str, ...] = ()
    #: 卷复制助手镜像；空则回退到应用镜像。
    copy_helper_image: str = ""
    #: 分享码有效期。
    share_ttl_seconds: int = DEFAULT_SHARE_TTL_SECONDS

    @classmethod
    def from_config(cls, config: Any) -> "AccessPolicy":
        invitation_only = bool(getattr(config, "invitation_only", False))
        raw_codes = getattr(config, "system_invitation_codes", ()) or ()
        if isinstance(raw_codes, str):
            # 单个字符串很可能是误写：宁可报错也不静默把它当成一个码。
            raise ConfigError(
                "system_invitation_codes 必须是列表/元组，而不是单个字符串"
            )
        resolver = getattr(config, "resolved_copy_helper_image", None)
        if callable(resolver):
            copy_helper = str(resolver() or "")
        else:
            copy_helper = str(getattr(config, "copy_helper_image", "") or "")
            instance_image = str(getattr(config, "instance_image", "") or "")
            copy_helper = copy_helper.strip() or instance_image
        share_ttl = int(
            getattr(config, "share_code_ttl_seconds", DEFAULT_SHARE_TTL_SECONDS)
            or DEFAULT_SHARE_TTL_SECONDS
        )
        return cls(
            invitation_only=invitation_only,
            system_invitation_codes=tuple(
                str(item).strip() for item in raw_codes if str(item).strip()
            ),
            copy_helper_image=copy_helper.strip(),
            share_ttl_seconds=share_ttl,
        )

    def helper_image(self, instance_image: str) -> str:
        return self.copy_helper_image or instance_image

    def validate_system_codes(self) -> None:
        """启动时 fail closed：任何不符合系统码格式的配置都直接拒绝启动。"""

        for code in self.system_invitation_codes:
            if not is_system_code_format(code):
                raise ConfigError(
                    "配置中的系统邀请码不符合合法格式"
                    "（TLM-SYS-...），已拒绝启动（不接受弱人工字符串作为"
                    "系统优先级凭证）"
                )


def generate_system_code() -> str:
    """生成一个系统码明文（CSPRNG，至少 256 bit）。

    这个函数也可以被运维离线调用，把结果填进
    ``CONTROL_PLANE_SYSTEM_INVITATION_CODES`` 提前配置。
    """

    return f"{SYSTEM_CODE_PREFIX}-{CODE_VERSION}-{secrets.token_urlsafe(32)}"


def generate_share_code() -> str:
    """生成一个分享码明文。密码空间与系统码完全独立。"""

    return f"{SHARE_CODE_PREFIX}-{CODE_VERSION}-{secrets.token_urlsafe(32)}"


def code_prefix_of(code: str) -> str:
    """只保留非敏感的版本前缀，用于审计与列表展示。"""

    match = SYSTEM_CODE_RE.match(code) or SHARE_CODE_RE.match(code)
    if match is None:
        return ""
    return f"{code.split('-v')[0]}-v{match.group(1)}"


def verification_hash(code: str, kind: str, pepper: str) -> str:
    """带域隔离的码哈希。同一个明文在两类码下得到不同哈希。"""

    return hash_token(f"invitation:{kind}:{code}", pepper)


@dataclass(frozen=True, slots=True)
class Admission:
    """一次入场判定的结果。"""

    allowed: bool
    priority: int = DEFAULT_PRIORITY
    #: 从哪个邀请码获得许可（仅存 id，不存明文）。
    invitation_id: str | None = None
    kind: str | None = None


class InvitationService:
    """邀请码的唯一写入方。"""

    def __init__(self, store: ControlPlaneStore, *, pepper: str, policy: AccessPolicy) -> None:
        self._store = store
        self._pepper = pepper
        self.policy = policy

    # ------------------------------------------------------------ 初始化
    def seed_configured_system_codes(self) -> dict[str, Any]:
        """把配置中的系统码种入 DB。幂等，且不会复活已撤销的码。"""

        self.policy.validate_system_codes()
        report: dict[str, Any] = {"seeded": 0, "existing": 0, "revoked": 0}
        for code in self.policy.system_invitation_codes:
            digest = verification_hash(code, InvitationKind.SYSTEM, self._pepper)
            if self._store.invitation_tombstone_exists(digest):
                existing = self._store.find_invitation_by_hash(digest)
                if existing is not None and existing.state == InvitationState.REVOKED:
                    # 撤销是持久的：配置里还在也不重新生效。
                    report["revoked"] += 1
                else:
                    report["existing"] += 1
                continue
            self._store.create_invitation(
                invitation_id=str(uuid.uuid4()),
                kind=str(InvitationKind.SYSTEM),
                code_prefix=code_prefix_of(code),
                code_hash=digest,
                label="configured",
                origin="config",
            )
            report["seeded"] += 1
        return report

    # ------------------------------------------------------------ 签发
    def issue_system_code(self, *, label: str = "") -> tuple[Invitation, str]:
        """签发一个新的系统码。明文仅在这里返回一次。"""

        code = generate_system_code()
        invitation = self._store.create_invitation(
            invitation_id=str(uuid.uuid4()),
            kind=str(InvitationKind.SYSTEM),
            code_prefix=code_prefix_of(code),
            code_hash=verification_hash(code, InvitationKind.SYSTEM, self._pepper),
            label=label[:64],
            origin="api",
        )
        return invitation, code

    def issue_share_code(self, *, workspace, label: str = "") -> tuple[Invitation, str]:
        """为自己的项目签发分享码。源项目必须已声明 v3
        内容布局，否则拒绝而不是盲目整卷拷贝。
        """

        from control_plane.errors import ShareSourceUnsupported

        if not workspace.is_share_safe_source:
            raise ShareSourceUnsupported(
                "项目未声明 v3 内容存储布局，无法保证其卷内不包含应用 session 凭证，"
                "已拒绝签发分享码",
                details={"workspace_id": workspace.workspace_id, "storage_layout": workspace.storage_layout},
            )
        code = generate_share_code()
        invitation = self._store.create_invitation(
            invitation_id=str(uuid.uuid4()),
            kind=str(InvitationKind.SHARE),
            code_prefix=code_prefix_of(code),
            code_hash=verification_hash(code, InvitationKind.SHARE, self._pepper),
            label=label[:64],
            workspace_id=workspace.workspace_id,
            owner_user_id=workspace.user_id,
            expires_at=self._store._clock() + self.policy.share_ttl_seconds,
        )
        return invitation, code

    # ------------------------------------------------------------ 撤销
    def revoke(self, invitation_id: str) -> Invitation:
        return self._store.revoke_invitation(invitation_id)

    def revoke_for_workspace(self, workspace_id: str) -> int:
        return self._store.revoke_invitations_for_workspace(workspace_id)

    def revoke_workspace_share(self, invitation_id: str, *, workspace_id: str, user_id: str) -> Invitation:
        """项目所有者撤销自己项目的分享码。别人的码一律拒绝。

        先确认调用者拥有该项目（否则返回 404，不泄露他人项目是否存在），
        再校验码的归属。
        """

        # 拒绝操作别人项目下的码：拒绝理由与项目不可见保持一致。
        workspace = self._store.require_workspace(workspace_id)
        if workspace.state != 'active' or workspace.user_id != user_id:
            raise NotFound('项目不存在或不可用')
        invitation = self._store.require_invitation(invitation_id)
        if invitation.kind != InvitationKind.SHARE or invitation.workspace_id != workspace_id:
            raise NotFound('分享码不属于该项目')
        if invitation.owner_user_id not in (None, user_id):
            raise Forbidden('只有项目所有者可以撤销该分享码')
        return self._store.revoke_invitation(invitation_id)

    # ------------------------------------------------------------ 验证
    def lookup(self, code: str, *, expected_kind: str | None = None) -> Invitation:
        """用明文码定位邀请码记录（不判定是否可用）。

        两类码的哈希域隔离，所以必须按格式推断 kind；格式不合法直接拒绝。
        """

        if not code or len(code) > MAX_CODE_LENGTH:
            raise InvalidInvitation("邀请码无效")
        if SYSTEM_CODE_RE.match(code):
            kind = str(InvitationKind.SYSTEM)
        elif SHARE_CODE_RE.match(code):
            kind = str(InvitationKind.SHARE)
        else:
            raise InvalidInvitation("邀请码无效")
        if expected_kind is not None and kind != expected_kind:
            raise InvalidInvitation("邀请码类型不匹配")
        invitation = self._store.find_invitation_by_hash(
            verification_hash(code, kind, self._pepper)
        )
        if invitation is None:
            raise InvalidInvitation("邀请码无效")
        return invitation

    def verify_system(self, code: str) -> Invitation:
        invitation = self.lookup(code, expected_kind=str(InvitationKind.SYSTEM))
        if invitation.state != InvitationState.ACTIVE:
            raise InvalidInvitation("邀请码已被撤销")
        return invitation

    def verify_share(self, code: str, *, now: float | None = None) -> Invitation:
        """分享码必须同时满足：未撤销、未过期、源项目仍存在且归属未变。"""

        moment = self._store._clock() if now is None else now
        invitation = self.lookup(code, expected_kind=str(InvitationKind.SHARE))
        if invitation.state != InvitationState.ACTIVE:
            raise InvalidInvitation("分享码已被撤销")
        if invitation.is_expired(now=moment):
            raise InvitationExpired("分享码已过期")
        if not invitation.workspace_id:
            raise InvalidInvitation("分享码没有绑定源项目")
        try:
            workspace = self._store.require_workspace(invitation.workspace_id)
        except Exception as exc:  # noqa: BLE001 - 源被删就拒绝导入
            raise InvalidInvitation("源项目不可用，分享码已失效") from exc
        if workspace.state != 'active':
            raise InvalidInvitation("源项目已不可用，分享码已失效")
        if invitation.owner_user_id and workspace.user_id != invitation.owner_user_id:
            # 源归属变更后，旧代码不能继续授权。
            raise InvalidInvitation("源项目归属已变更，分享码已失效")
        return invitation

    # ------------------------------------------------------------ 入场判定
    def admit(self, code: str | None, *, workspace_id: str | None = None, user_id: str | None = None) -> Admission:
        """决定这次申请能不能入场，以及应当以什么优先级入场。

        * 提供了系统码：验证通过 -> 最高优先级；
        * 分享码仅用于复制，不能直接分配槽位；
        * 未提供码：仅开放模式允许，历史 trial grant 不能绕过邀请模式；
        * 提供了格式合法但无效的码：**显式拒绝**，不能退化为无码入场。
        """

        if code is not None and not str(code).strip():
            code = None
        if code is None:
            if not self.policy.invitation_only:
                return Admission(allowed=True, priority=DEFAULT_PRIORITY)
            raise InvitationRequired("当前仅开放邀请码入场，请填写有效邀请码")
        raw = str(code).strip()
        if SYSTEM_CODE_RE.match(raw):
            invitation = self.verify_system(raw)
            self._store.register_invitation_use(invitation.invitation_id)
            return Admission(
                allowed=True, priority=SYSTEM_PRIORITY,
                invitation_id=invitation.invitation_id, kind=str(InvitationKind.SYSTEM),
            )
        if SHARE_CODE_RE.match(raw):
            # 分享码**不能**直接申请计算资源（否则就变成直接挂载源项目的访问令牌）。
            # 它只能走 ``/control/workspace-imports`` 产生独立副本。
            # 这里仍验证码本身的有效性，
            # 再拒绝，以便区分"码无效" 与 "码有效但不能直接用"。
            self.verify_share(raw)
            raise InvalidInvitation(
                "分享码不能直接申请计算，请先导入项目"
            )
        raise InvalidInvitation("邀请码无效")
