"""控制面配置：默认值 < 配置文件 < ``CONTROL_PLANE_*`` 环境变量 < 显式覆盖。

设计原则与 instance-app 的 ``HostConfig`` 一致：字段显式、可覆盖、非法配置
立刻抛错，而不是带着默认值继续跑。区别是这里的策略属于**容器外**：
槽位数量、镜像与卷前缀、就绪超时、租约 TTL、管理令牌。

统一配置文件（TOML）由 ``CONTROL_PLANE_CONFIG`` 指定路径，
也可以显式传给 :meth:`ControlPlaneConfig.from_env` 的 ``config_path``。
文件格式、优先级与安全注意事项见 ``docs/configuration.md``。配置对象本身
从不探测默认路径，也不会在未显式指定时读取任何凭据文件。
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, ClassVar, Mapping

from control_plane.config_file import (
    load_config_file,
    parse_config_document,
    resolve_config_path,
)
from control_plane.errors import ConfigError

ENV_PREFIX = "CONTROL_PLANE_"
#: 前缀 ``CONTROL_PLANE_INSTANCE_ENV_`` 会被剥掉并换成 ``TUTORLM_``，
#: 例如 ``CONTROL_PLANE_INSTANCE_ENV_RUN_MODE=demo`` -> 容器内 ``TUTORLM_RUN_MODE=demo``。
INSTANCE_ENV_PREFIX = f"{ENV_PREFIX}INSTANCE_ENV_"
INSTANCE_ENV_TARGET_PREFIX = "TUTORLM_"

ADAPTER_FAKE = "fake"
ADAPTER_DOCKER = "docker"
VALID_ADAPTERS: tuple[str, ...] = (ADAPTER_FAKE, ADAPTER_DOCKER)

#: RFC 6265 允许的 cookie-name 字符集；非法名字会让 Starlette 直接抛错，
#: 所以在配置层就拒绝，而不是等到第一次签发租约时才 500。
COOKIE_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
#: ``SameSite`` 只允许这三个取值（小写），与 Starlette 的约束一致。
VALID_SAMESITE: tuple[str, ...] = ("lax", "strict", "none")

#: 配置文件里放控制面字段的表名（与 control_plane.config_file 保持一致）。
CONTROL_PLANE_FILE_TABLE = "control_plane"

#: 系统邀请码格式的**唯一**权威是 :mod:`control_plane.invitations`
#: （``SYSTEM_CODE_RE``：``TLM-SYS-v<version>-<43 位 base64url>``，即 32 随机
#: 字节 / 256 bit）。配置层只做同一格式的**纯校验**，绝不自己生成码，也不
#: import 邀请核心（那会形成 config -> invitations -> store 的环）。
SYSTEM_INVITATION_CODE_PREFIX = "TLM-SYS"
#: 当前只接受核心签发的版本；未来版本需要两侧一起改。
SYSTEM_INVITATION_CODE_VERSION = "v1"
#: 43 个 base64url 字符 == 32 字节 == 256 bit 熵。
SYSTEM_INVITATION_CODE_PAYLOAD_CHARS = 43
#: 与核心 ``SYSTEM_CODE_RE`` 完全一致（版本段是 ``v<digits>``，不是固定 v1）。
SYSTEM_INVITATION_CODE_RE = re.compile(
    rf"^{SYSTEM_INVITATION_CODE_PREFIX}-v\d+"
    rf"-[A-Za-z0-9_-]{{{SYSTEM_INVITATION_CODE_PAYLOAD_CHARS}}}$"
)
#: 供错误信息与文档引用的格式说明（不是第二套正则）。
SYSTEM_INVITATION_CODE_FORMAT = (
    f"{SYSTEM_INVITATION_CODE_PREFIX}-{SYSTEM_INVITATION_CODE_VERSION}"
    f"-<{SYSTEM_INVITATION_CODE_PAYLOAD_CHARS} 位 base64url 随机字符>"
)


def is_valid_system_invitation_code(value: str) -> bool:
    """严格校验系统邀请码格式（与邀请核心的 ``SYSTEM_CODE_RE`` 逐字符一致）。

    只做纯格式校验：不查数据库、不 import 邀请服务。生成邀请码的唯一入口是
    :func:`control_plane.invitations.generate_system_code`，配置层不生成、也不
    加比核心更严的额外规则（否则会出现"核心签发但配置拒绝"的分歧）。
    """

    return isinstance(value, str) and SYSTEM_INVITATION_CODE_RE.match(value) is not None


def parse_system_invitation_codes(raw: Any) -> tuple[str, ...]:
    """Parse a strict list of system invitation codes from config/env.

    Accepts an iterable of strings (TOML array) or a JSON array string such as
    ``["TLM-SYS-v1-...."]`` coming from ``CONTROL_PLANE_SYSTEM_INVITATION_CODES``.
    Blank entries, duplicates and any code that is not a canonical core-issued
    code are rejected: a weak hand-written string must not silently grant the
    highest admission priority.
    """

    if raw is None:
        return ()
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return ()
        try:
            decoded = json.loads(stripped)
        except json.JSONDecodeError:
            raise ConfigError(
                "CONTROL_PLANE_SYSTEM_INVITATION_CODES 必须是 JSON 字符串数组"
            ) from None
        if not isinstance(decoded, list):
            raise ConfigError(
                "CONTROL_PLANE_SYSTEM_INVITATION_CODES 必须是 JSON 字符串数组"
            )
        values: list[Any] = list(decoded)
    elif isinstance(raw, (list, tuple)):
        values = list(raw)
    else:
        raise ConfigError(
            "system_invitation_codes 必须是字符串数组，收到 " f"{type(raw).__name__}"
        )

    codes: list[str] = []
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, str):
            raise ConfigError("system_invitation_codes 只能包含字符串")
        code = item.strip()
        if not code:
            raise ConfigError("system_invitation_codes 不允许空字符串条目")
        if not is_valid_system_invitation_code(code):
            raise ConfigError(
                f"系统邀请码格式非法：必须形如 {SYSTEM_INVITATION_CODE_FORMAT}。"
                "请用 CLI/邀请核心生成，不要手写。"
            )
        if code in seen:
            raise ConfigError("system_invitation_codes 含重复条目")
        seen.add(code)
        codes.append(code)
    return tuple(codes)


def _default_db_path() -> Path:
    return Path("var") / "control-plane.sqlite3"


def instance_env_from_environ(environ: dict[str, str] | None = None) -> dict[str, str]:
    """收集需要透传给实例容器的 ``TUTORLM_*`` 变量。"""

    source = os.environ if environ is None else environ
    out: dict[str, str] = {}
    for key, value in source.items():
        if not key.startswith(INSTANCE_ENV_PREFIX):
            continue
        suffix = key[len(INSTANCE_ENV_PREFIX) :].strip()
        if not suffix:
            continue
        out[f"{INSTANCE_ENV_TARGET_PREFIX}{suffix}"] = value
    return out


#: 环境变量里允许的布尔写法。刻意保留历史集合（``1/true/yes/on``），
#: 但**拒绝**无法识别的字符串，而不是把它悄悄当成 False。
ENV_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
ENV_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def _file_scalar(value: Any, field: str, expected: type, source: str) -> Any:
    """Strictly type-check one value coming from a config file.

    ``bool`` is deliberately rejected for numeric fields and ``int`` for boolean
    fields, so ``gc_enabled = 1`` cannot be silently accepted.
    """

    if expected is bool:
        if not isinstance(value, bool):
            raise ConfigError(
                f"{source} [{CONTROL_PLANE_FILE_TABLE}] {field} 必须是布尔值 true/false，"
                f"收到类型 {type(value).__name__}"
            )
        return value
    if expected is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(
                f"{source} [{CONTROL_PLANE_FILE_TABLE}] {field} 必须是整数"
            )
        return value
    if expected is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(
                f"{source} [{CONTROL_PLANE_FILE_TABLE}] {field} 必须是数字"
            )
        return float(value)
    if expected is str:
        if not isinstance(value, str):
            raise ConfigError(
                f"{source} [{CONTROL_PLANE_FILE_TABLE}] {field} 必须是字符串"
            )
        return value
    raise AssertionError(f"unsupported file field type for {field}: {expected!r}")


class _FileLayer:
    """Validated ``[control_plane]`` + ``[instance_env]`` values from one file."""

    def __init__(
        self,
        control_plane: Mapping[str, Any],
        instance_env: Mapping[str, str],
        source: str,
        owner: type,
    ) -> None:
        self.source = source
        self.instance_env = dict(instance_env)
        self._values: dict[str, Any] = {}
        fields: Mapping[str, Any] = owner.__dataclass_fields__
        aliases = {**owner.FILE_KEY_ALIASES, "invitation_only": "invitation_only"}
        for key, value in control_plane.items():
            name = aliases.get(key, key)
            if name not in fields:
                raise ConfigError(
                    f"{source} [{CONTROL_PLANE_FILE_TABLE}] 未知字段 {key!r}；"
                    "未知字段会拼错配置而不报错，因此这里直接拒绝"
                )
            if name in self._values:
                raise ConfigError(
                    f"{source} [{CONTROL_PLANE_FILE_TABLE}] 字段 {name} 重复定义"
                    "（invitationonly / invitation_only 只能留一个）"
                )
            self._values[name] = value

    def has(self, name: str) -> bool:
        return name in self._values

    def strict(self, name: str, expected: type) -> Any:
        return _file_scalar(self._values[name], name, expected, self.source)

    def strict_list(self, name: str) -> list[str]:
        value = self._values[name]
        if isinstance(value, str) or not isinstance(value, (list, tuple)):
            raise ConfigError(
                f"{self.source} [{CONTROL_PLANE_FILE_TABLE}] {name} 必须是字符串数组"
            )
        for item in value:
            if not isinstance(item, str):
                raise ConfigError(
                    f"{self.source} [{CONTROL_PLANE_FILE_TABLE}] {name} 只能包含字符串，"
                    f"收到类型 {type(item).__name__}"
                )
        return [str(item) for item in value]


class _EnvLayer:
    """``CONTROL_PLANE_*`` environment values (already-normalised strings)."""

    def __init__(self, environ: Mapping[str, str]) -> None:
        self._environ = environ

    def has(self, env_name: str) -> bool:
        return f"{ENV_PREFIX}{env_name}" in self._environ

    def raw(self, env_name: str) -> str | None:
        return self._environ.get(f"{ENV_PREFIX}{env_name}")

    def mapping(self) -> Mapping[str, str]:
        return self._environ

    def text(self, env_name: str) -> str | None:
        """Return the stripped value, or ``None`` for missing/blank."""

        raw = self.raw(env_name)
        if raw is None:
            return None
        stripped = raw.strip()
        return stripped or None


class _Layers:
    """Resolution order: environment wins over file, defaults are caller-side."""

    def __init__(self, env: _EnvLayer, file_layer: _FileLayer | None) -> None:
        self._env = env
        self._file = file_layer

    # -------------------------------------------------------------- strings
    def raw_string(self, env_name: str, field: str, default: str | None) -> str | None:
        if self._env.has(env_name):
            return self._env.raw(env_name)
        if self._file is not None and self._file.has(field):
            return self._file.strict(field, str)
        return default

    def string(self, env_name: str, field: str, default: str, *, strip: bool = False) -> str:
        if self._env.has(env_name):
            raw = self._env.raw(env_name) or ""
            return raw.strip() if strip else raw
        if self._file is not None and self._file.has(field):
            value = self._file.strict(field, str)
            return value.strip() if strip else value
        return default

    def string_or_empty(self, env_name: str, field: str) -> str:
        """String field where blank is meaningful (``""`` = fall back)."""

        if self._env.has(env_name):
            return (self._env.raw(env_name) or "").strip()
        if self._file is not None and self._file.has(field):
            return self._file.strict(field, str).strip()
        return ""

    def optional_string(self, env_name: str, field: str) -> str | None:
        """Legacy-compatible: blank/whitespace means ``None`` (feature off)."""

        if self._env.has(env_name):
            raw = self._env.raw(env_name)
            stripped = (raw or "").strip()
            return stripped or None
        if self._file is not None and self._file.has(field):
            return self._file.strict(field, str).strip() or None
        return None

    # --------------------------------------------------------------- numbers
    def int(self, env_name: str, field: str, default: int) -> int:
        if self._env.has(env_name):
            raw = self._env.text(env_name)
            if raw is None:
                return default
            try:
                return int(raw)
            except ValueError as exc:
                raise ConfigError(
                    f"环境变量 {ENV_PREFIX}{env_name} 必须是整数，收到 {raw!r}"
                ) from exc
        if self._file is not None and self._file.has(field):
            return self._file.strict(field, int)
        return default

    def duration_or_inf(self, env_name: str, field: str, default: int | None) -> int | None:
        if self._env.has(env_name):
            raw = (self._env.text(env_name) or '').strip().lower()
            if raw == 'inf':
                return None
            try:
                return int(raw)
            except ValueError as exc:
                raise ConfigError(f"环境变量 {ENV_PREFIX}{env_name} 必须是正整数或 inf，收到 {raw!r}") from exc
        if self._file is not None and self._file.has(field):
            value = self._file._values[field]
            if isinstance(value, str) and value.strip().lower() == 'inf':
                return None
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigError(f"配置项 {field} 必须是正整数或字符串 inf")
            return value
        return default

    def optional_int(self, env_name: str, field: str, default: int | None) -> int | None:
        if self._env.has(env_name):
            raw = self._env.text(env_name)
            if raw is None:
                return default
            try:
                return int(raw)
            except ValueError as exc:
                raise ConfigError(
                    f"环境变量 {ENV_PREFIX}{env_name} 必须是整数，收到 {raw!r}"
                ) from exc
        if self._file is not None and self._file.has(field):
            return self._file.strict(field, int)
        return default

    def number(self, env_name: str, field: str, default: float) -> float:
        if self._env.has(env_name):
            raw = self._env.text(env_name)
            if raw is None:
                return default
            try:
                return float(raw)
            except ValueError as exc:
                raise ConfigError(
                    f"环境变量 {ENV_PREFIX}{env_name} 必须是数字，收到 {raw!r}"
                ) from exc
        if self._file is not None and self._file.has(field):
            return self._file.strict(field, float)
        return default

    def optional_number(self, env_name: str, field: str, default: float | None) -> float | None:
        if self._env.has(env_name):
            raw = self._env.text(env_name)
            if raw is None:
                return default
            try:
                return float(raw)
            except ValueError as exc:
                raise ConfigError(
                    f"环境变量 {ENV_PREFIX}{env_name} 必须是数字，收到 {raw!r}"
                ) from exc
        if self._file is not None and self._file.has(field):
            return self._file.strict(field, float)
        return default

    # --------------------------------------------------------------- flags
    def boolean(self, env_name: str, field: str, default: bool) -> bool:
        if self._env.has(env_name):
            raw = self._env.text(env_name)
            if raw is None:
                return default
            return _parse_env_bool(ENV_PREFIX + env_name, raw)
        if self._file is not None and self._file.has(field):
            return self._file.strict(field, bool)
        return default

    # ------------------------------------------------------------ specials
    def invitation_codes(self) -> tuple[str, ...]:
        """系统邀请码：环境变量（JSON 数组）优先于配置文件数组。"""

        if self._env.has("SYSTEM_INVITATION_CODES"):
            return parse_system_invitation_codes(self._env.raw("SYSTEM_INVITATION_CODES"))
        if self._file is not None and self._file.has("system_invitation_codes"):
            return parse_system_invitation_codes(
                self._file.strict_list("system_invitation_codes")
            )
        return ()

    def instance_env(self) -> dict[str, str]:
        """``[instance_env]`` 之后再用 ``CONTROL_PLANE_INSTANCE_ENV_*`` 覆盖。"""

        merged: dict[str, str] = dict(self._file.instance_env) if self._file else {}
        for key, value in instance_env_from_environ(dict(self._env.mapping())).items():
            merged[key] = value
        return merged


def _parse_env_bool(name: str, raw: str) -> bool:
    lowered = raw.strip().lower()
    if lowered in ENV_TRUE_VALUES:
        return True
    if lowered in ENV_FALSE_VALUES:
        return False
    raise ConfigError(
        f"环境变量 {name} 必须是布尔值（true/false/1/0/yes/no/on/off），收到 {raw!r}"
    )


@dataclass(frozen=True, slots=True)
class ControlPlaneConfig:
    """一份控制面进程的完整策略。"""

    # --- 存储 ---------------------------------------------------------------
    db_path: Path = field(default_factory=_default_db_path)

    # --- 实例池 -------------------------------------------------------------
    slots: int = 3
    instance_image: str = "tutorlm-host:0.2.0-single"
    instance_port: int = 8000
    instance_mount_path: str = "/data"
    network: str | None = None
    container_prefix: str = "tutorlm-instance"
    volume_prefix: str = "tutorlm-instance-data"
    label_key: str = "app"
    label_value: str = "tutorlm-instance"
    docker_host: str | None = None
    #: 分享复制 helper 使用的**本地既有**镜像。留空表示回退到
    #: :attr:`instance_image`；控制面只使用已存在的本地镜像，绝不拉取。
    copy_helper_image: str = ""

    # --- 租约 ---------------------------------------------------------------
    lease_ttl_seconds: int = 3600
    lease_idle_ttl_seconds: int = 0
    reclaim_on_idle: bool = False
    workspace_idle_ttl_seconds: int | None = 4 * 60 * 60
    workspace_permanent: bool = False
    page_heartbeat_timeout_seconds: int = 120
    page_close_grace_seconds: int = 5

    # --- 生命周期 -----------------------------------------------------------
    adapter: str = ADAPTER_DOCKER
    ready_timeout_seconds: float = 60.0
    ready_poll_interval_seconds: float = 1.0
    stop_grace_seconds: int = 30
    mem_limit: str | None = "2g"
    cpus: float | None = 2.0
    pids_limit: int | None = 256
    tmpfs_size: str | None = "128m"
    gc_enabled: bool = True
    gc_interval_seconds: int = 30

    # --- 鉴权 ---------------------------------------------------------------
    admin_token: str = ""
    token_pepper: str = ""
    lease_cookie_name: str = "tutorlm_lease"
    #: 默认 **False**：本地 HTTP 开发未配置 TLS。
    #: Secure=True 的 cookie 在纯 HTTP 下会被浏览器直接丢弃，表现为
    #: "能签发但每次请求都 401"，比明文风险更难排查。TLS 落地后必须改 true。
    lease_cookie_secure: bool = False
    lease_cookie_samesite: str = "lax"
    #: cookie 的 Max-Age；``None`` 表示跟随 ``lease_ttl_seconds``。
    #: 服务端过期判定始终以租约 TTL 为准，这里只影响浏览器侧留存时间。
    lease_cookie_max_age_seconds: int | None = None

    # --- 邀请 ---------------------------------------------------------------
    #: 仅邀请可用：为 True 时新访客需要有效邀请码（系统码或用户分享码）。
    #: 配置文件里写作 ``invitationonly``（用户指定拼写），内部字段是这个。
    invitation_only: bool = False
    #: 运维预置的系统邀请码。必须由 CLI/本模块的生成器产生，格式严格校验；
    #: 不要手写弱字符串。``describe()`` 只暴露数量，绝不回显明文。
    system_invitation_codes: tuple[str, ...] = ()
    #: 用户分享码的有效期（秒），默认 7 天。
    share_code_ttl_seconds: int = 7 * 24 * 60 * 60

    # --- 对外 ---------------------------------------------------------------
    public_base_url: str = ""
    public_port: int = 47823

    # --- 透传 ---------------------------------------------------------------
    instance_env: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # 冻结 dataclass 也要先归一化：环境变量里写 "Lax" 不该变成启动失败。
        normalized = (self.lease_cookie_samesite or "").strip().lower()
        if normalized != self.lease_cookie_samesite:
            object.__setattr__(self, "lease_cookie_samesite", normalized)
        codes = self.system_invitation_codes
        if isinstance(codes, list):
            object.__setattr__(self, "system_invitation_codes", tuple(codes))
        self.validate()

    def validate(self) -> None:
        self._validate_booleans()
        if self.slots < 1:
            raise ConfigError(f"CONTROL_PLANE_SLOTS 必须 >= 1，收到 {self.slots}")
        if self.adapter not in VALID_ADAPTERS:
            raise ConfigError(
                f"CONTROL_PLANE_ADAPTER 必须是 {VALID_ADAPTERS} 之一，收到 {self.adapter!r}"
            )
        if not (1 <= self.instance_port <= 65535):
            raise ConfigError(f"CONTROL_PLANE_INSTANCE_PORT 非法：{self.instance_port}")
        if not (1 <= self.public_port <= 65535):
            raise ConfigError(f"CONTROL_PLANE_PUBLIC_PORT 非法：{self.public_port}")
        if self.ready_timeout_seconds <= 0:
            raise ConfigError("CONTROL_PLANE_READY_TIMEOUT_SECONDS 必须 > 0")
        if self.ready_poll_interval_seconds <= 0:
            raise ConfigError("CONTROL_PLANE_READY_POLL_INTERVAL_SECONDS 必须 > 0")
        if self.lease_ttl_seconds <= 0:
            raise ConfigError("CONTROL_PLANE_LEASE_TTL_SECONDS 必须 > 0")
        if self.lease_idle_ttl_seconds < 0:
            raise ConfigError("CONTROL_PLANE_LEASE_IDLE_TTL_SECONDS 不能为负")
        if self.workspace_idle_ttl_seconds is not None and self.workspace_idle_ttl_seconds <= 0:
            raise ConfigError('CONTROL_PLANE_WORKSPACE_IDLE_TTL_SECONDS must be positive')
        if self.page_heartbeat_timeout_seconds <= 0:
            raise ConfigError('CONTROL_PLANE_PAGE_HEARTBEAT_TIMEOUT_SECONDS must be positive')
        if self.page_close_grace_seconds < 0:
            raise ConfigError('CONTROL_PLANE_PAGE_CLOSE_GRACE_SECONDS cannot be negative')
        if self.reclaim_on_idle and self.lease_idle_ttl_seconds <= 0:
            raise ConfigError(
                "CONTROL_PLANE_RECLAIM_ON_IDLE=true 需要同时设置 "
                "CONTROL_PLANE_LEASE_IDLE_TTL_SECONDS > 0"
            )
        if self.stop_grace_seconds < 0:
            raise ConfigError("CONTROL_PLANE_STOP_GRACE_SECONDS 不能为负")
        if not self.instance_mount_path.startswith("/"):
            raise ConfigError("CONTROL_PLANE_INSTANCE_MOUNT_PATH 必须是容器内绝对路径")
        if not self.container_prefix:
            raise ConfigError("CONTROL_PLANE_CONTAINER_PREFIX 不能为空")
        if not self.volume_prefix:
            raise ConfigError("CONTROL_PLANE_VOLUME_PREFIX 不能为空")
        if not COOKIE_NAME_RE.match(self.lease_cookie_name or ""):
            raise ConfigError(
                "CONTROL_PLANE_LEASE_COOKIE_NAME 必须是合法的 cookie 名"
                f"（RFC 6265 token），收到 {self.lease_cookie_name!r}"
            )
        if self.lease_cookie_samesite not in VALID_SAMESITE:
            raise ConfigError(
                "CONTROL_PLANE_LEASE_COOKIE_SAMESITE 必须是 "
                f"{VALID_SAMESITE} 之一，收到 {self.lease_cookie_samesite!r}"
            )
        if self.lease_cookie_samesite == "none" and not self.lease_cookie_secure:
            # 浏览器会直接丢弃 SameSite=None 但没有 Secure 的 cookie，
            # 静默失效比启动即报错更难排查，所以这里直接拒绝。
            raise ConfigError(
                "CONTROL_PLANE_LEASE_COOKIE_SAMESITE=none 必须同时 "
                "CONTROL_PLANE_LEASE_COOKIE_SECURE=true"
            )
        if self.lease_cookie_max_age_seconds is not None and (
            self.lease_cookie_max_age_seconds <= 0
        ):
            raise ConfigError(
                "CONTROL_PLANE_LEASE_COOKIE_MAX_AGE_SECONDS 必须 > 0（或留空跟随租约 TTL）"
            )
        if self.share_code_ttl_seconds <= 0:
            raise ConfigError("CONTROL_PLANE_SHARE_CODE_TTL_SECONDS 必须 > 0")
        if not isinstance(self.copy_helper_image, str):
            raise ConfigError("CONTROL_PLANE_COPY_HELPER_IMAGE 必须是字符串")
        self._validate_system_invitation_codes()

    def _validate_booleans(self) -> None:
        """布尔字段必须是真正的 ``bool``，不接受真值字符串。

        配置文件里的 ``"yes"``、``1``、``"false"`` 之类如果被当成真值处理，
        会让一个安全开关在运维不知情的情况下失效。这里选择直接拒绝。
        """

        for name in ("reclaim_on_idle", "gc_enabled", "lease_cookie_secure", "invitation_only"):
            value = getattr(self, name)
            if not isinstance(value, bool):
                raise ConfigError(
                    f"{name} 必须是布尔值（true/false），收到 {value!r}（{type(value).__name__}）"
                )

    def _validate_system_invitation_codes(self) -> None:
        """Re-validate the codes so a direct dataclass construction is safe too.

        The strict format check lives in the invitation core; this method only
        makes sure the *shape* is a sequence of non-empty unique strings and
        that each entry passes that same core check.
        """

        codes = self.system_invitation_codes
        if isinstance(codes, str):
            raise ConfigError(
                "system_invitation_codes 必须是字符串序列，而不是单个字符串"
            )
        if not isinstance(codes, (tuple, list)):
            raise ConfigError(
                "system_invitation_codes 必须是字符串序列，收到 "
                f"{type(codes).__name__}"
            )
        seen: set[str] = set()
        for code in codes:
            if not isinstance(code, str) or not code.strip():
                raise ConfigError("system_invitation_codes 含空/非字符串条目")
            if not is_valid_system_invitation_code(code):
                raise ConfigError(
                    "system_invitation_codes 含非法系统邀请码；"
                    f"必须形如 {SYSTEM_INVITATION_CODE_FORMAT} 且由 CLI 生成"
                )
            if code in seen:
                raise ConfigError("system_invitation_codes 含重复条目")
            seen.add(code)

    # ------------------------------------------------------------------ names
    def volume_name(self, slot_id: int) -> str:
        """槽位专属数据卷名。"""

        return f"{self.volume_prefix}-s{int(slot_id):02d}"

    def container_name(self, slot_id: int, generation: int) -> str:
        """容器名带代次，便于日志与孤儿资源排查。"""

        return f"{self.container_prefix}-s{int(slot_id):02d}-g{int(generation):04d}"

    def workspace_volume_name(self, workspace_id: str) -> str:
        return f'{self.volume_prefix.rstrip("-")}-workspace-{uuid.UUID(workspace_id).hex}'

    def labels(self) -> dict[str, str]:
        return {self.label_key: self.label_value}

    def is_admin_token_configured(self) -> bool:
        return bool(self.admin_token.strip())

    def has_system_invitation_codes(self) -> bool:
        return bool(self.system_invitation_codes)

    def resolved_copy_helper_image(self) -> str:
        """分享复制 helper 实际使用的镜像；留空则回退到实例镜像。

        永远返回本地镜像名，调用方不得据此拉取远程镜像。
        """

        return (self.copy_helper_image or "").strip() or self.instance_image

    def cookie_max_age(self) -> int:
        """浏览器侧 cookie 存活秒数；留空时跟随租约 TTL。"""

        if self.lease_cookie_max_age_seconds is not None:
            return int(self.lease_cookie_max_age_seconds)
        return int(self.lease_ttl_seconds)

    # ---------------------------------------------------------------- helpers
    def with_overrides(self, **overrides: Any) -> "ControlPlaneConfig":
        known = set(self.__dataclass_fields__)  # type: ignore[attr-defined]
        unknown = set(overrides) - known
        if unknown:
            raise TypeError(f"unknown control plane config fields: {sorted(unknown)}")
        return replace(self, **overrides)

    def resolved_db_path(self) -> Path:
        return Path(self.db_path).expanduser()

    #: TOML ``[control_plane]`` 键 -> dataclass 字段名。
    #: 只接受这些确切拼写（外加 ``invitationonly`` 别名），未知键直接报错。
    FILE_KEY_ALIASES: ClassVar[dict[str, str]] = {
        "inviteonly": "invitation_only",
        "invitationonly": "invitation_only",
    }

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        config_path: str | Path | None = None,
        **overrides: Any,
    ) -> "ControlPlaneConfig":
        """按 默认值 < 配置文件 < 环境变量 < 显式覆盖 的顺序构建配置。

        * ``environ``：测试/嵌入时注入的完整环境；``None`` 表示真实
          ``os.environ``。**不会**在其中之外再找变量。
        * ``config_path``：显式配置文件路径，优先级最高。留空时才读
          ``CONTROL_PLANE_CONFIG``（以及部署别名 ``TUTORLM_CONTROL_PLANE_CONFIG``）。
          没有配置就没有文件层，绝不猜测默认路径、也不静默读取别处的凭据。
        * ``overrides``：调用方已知的高优先级字段（如测试夹具），最后应用。
        """

        source = dict(os.environ if environ is None else environ)
        env = _EnvLayer(source)
        path = resolve_config_path(config_path, source)
        file_layer: _FileLayer | None = None
        if path is not None:
            document = load_config_file(path)
            control_plane, file_instance_env = parse_config_document(
                document, source=str(path)
            )
            file_layer = _FileLayer(control_plane, file_instance_env, str(path), cls)
        layers = _Layers(env, file_layer)

        raw_db = layers.raw_string("DB_PATH", "db_path", None)
        db_path = Path(raw_db).expanduser() if raw_db else _default_db_path()
        network = layers.optional_string("NETWORK", "network")
        mem_limit = layers.optional_string("MEM_LIMIT", "mem_limit")
        tmpfs_size = layers.optional_string("TMPFS_SIZE", "tmpfs_size")
        docker_host = layers.optional_string("DOCKER_HOST", "docker_host")

        config = cls(
            db_path=db_path,
            slots=layers.int("SLOTS", "slots", 3),
            instance_image=layers.string("INSTANCE_IMAGE", "instance_image", "tutorlm-host:0.2.0-single"),
            instance_port=layers.int("INSTANCE_PORT", "instance_port", 8000),
            instance_mount_path=layers.string("INSTANCE_MOUNT_PATH", "instance_mount_path", "/data"),
            network=network,
            container_prefix=layers.string("CONTAINER_PREFIX", "container_prefix", "tutorlm-instance"),
            volume_prefix=layers.string("VOLUME_PREFIX", "volume_prefix", "tutorlm-instance-data"),
            label_key=layers.string("LABEL_KEY", "label_key", "app"),
            label_value=layers.string("LABEL_VALUE", "label_value", "tutorlm-instance"),
            docker_host=docker_host,
            copy_helper_image=layers.string_or_empty("COPY_HELPER_IMAGE", "copy_helper_image"),
            lease_ttl_seconds=layers.int("LEASE_TTL_SECONDS", "lease_ttl_seconds", 3600),
            lease_idle_ttl_seconds=layers.int("LEASE_IDLE_TTL_SECONDS", "lease_idle_ttl_seconds", 0),
            reclaim_on_idle=layers.boolean("RECLAIM_ON_IDLE", "reclaim_on_idle", False),
            workspace_idle_ttl_seconds=layers.duration_or_inf("WORKSPACE_IDLE_TTL_SECONDS", "workspace_idle_ttl_seconds", 14400),
            workspace_permanent=layers.boolean("WORKSPACE_PERMANENT", "workspace_permanent", False),
            page_heartbeat_timeout_seconds=layers.int("PAGE_HEARTBEAT_TIMEOUT_SECONDS", "page_heartbeat_timeout_seconds", 120),
            page_close_grace_seconds=layers.int("PAGE_CLOSE_GRACE_SECONDS", "page_close_grace_seconds", 5),
            adapter=layers.string("ADAPTER", "adapter", ADAPTER_DOCKER, strip=True).lower(),
            ready_timeout_seconds=layers.number("READY_TIMEOUT_SECONDS", "ready_timeout_seconds", 60.0),
            ready_poll_interval_seconds=layers.number("READY_POLL_INTERVAL_SECONDS", "ready_poll_interval_seconds", 1.0),
            stop_grace_seconds=layers.int("STOP_GRACE_SECONDS", "stop_grace_seconds", 30),
            mem_limit=mem_limit,
            cpus=layers.optional_number("CPUS", "cpus", 2.0),
            pids_limit=layers.optional_int("PIDS_LIMIT", "pids_limit", 256),
            tmpfs_size=tmpfs_size,
            gc_enabled=layers.boolean("GC_ENABLED", "gc_enabled", True),
            gc_interval_seconds=layers.int("GC_INTERVAL_SECONDS", "gc_interval_seconds", 30),
            admin_token=layers.raw_string("ADMIN_TOKEN", "admin_token", "") or "",
            token_pepper=layers.raw_string("TOKEN_PEPPER", "token_pepper", "") or "",
            lease_cookie_name=layers.string("LEASE_COOKIE_NAME", "lease_cookie_name", "tutorlm_lease", strip=True),
            lease_cookie_secure=layers.boolean("LEASE_COOKIE_SECURE", "lease_cookie_secure", False),
            lease_cookie_samesite=layers.string("LEASE_COOKIE_SAMESITE", "lease_cookie_samesite", "lax", strip=True).lower(),
            lease_cookie_max_age_seconds=layers.optional_int("LEASE_COOKIE_MAX_AGE_SECONDS", "lease_cookie_max_age_seconds", None),
            invitation_only=layers.boolean("INVITATION_ONLY", "invitation_only", False),
            system_invitation_codes=layers.invitation_codes(),
            share_code_ttl_seconds=layers.int("SHARE_CODE_TTL_SECONDS", "share_code_ttl_seconds", 7 * 24 * 60 * 60),
            public_base_url=layers.string("PUBLIC_BASE_URL", "public_base_url", "").rstrip("/"),
            public_port=layers.int("PUBLIC_PORT", "public_port", 47823),
            instance_env=layers.instance_env(),
        )
        return config.with_overrides(**overrides) if overrides else config

    @classmethod
    def for_tests(cls, tmp_path: Path, **overrides: Any) -> "ControlPlaneConfig":
        """测试用隔离配置：fake adapter、无后台 GC、固定的令牌盐。"""

        values: dict[str, Any] = {
            "db_path": Path(tmp_path) / "control-plane.sqlite3",
            "slots": 2,
            "adapter": ADAPTER_FAKE,
            "admin_token": "test-admin-token",
            "token_pepper": "test-pepper",
            "gc_enabled": False,
            "workspace_permanent": False,
            "ready_timeout_seconds": 1.0,
            "ready_poll_interval_seconds": 0.01,
            "instance_image": "tutorlm-host:test",
            "instance_env": {"TUTORLM_RUN_MODE": "demo", "TUTORLM_INVITE_CODE": "test-invite"},
            "public_base_url": "",
        }
        values.update(overrides)
        return cls(**values)

    def describe(self) -> dict[str, Any]:
        """对外可见的配置摘要（不含任何令牌）。"""

        return {
            "slots": self.slots,
            "adapter": self.adapter,
            "instance_image": self.instance_image,
            "instance_port": self.instance_port,
            "network": self.network,
            "lease_ttl_seconds": self.lease_ttl_seconds,
            "lease_idle_ttl_seconds": self.lease_idle_ttl_seconds,
            "reclaim_on_idle": self.reclaim_on_idle,
            'workspace_idle_ttl_seconds': self.workspace_idle_ttl_seconds,
            'workspace_permanent': self.workspace_permanent,
            'page_heartbeat_timeout_seconds': self.page_heartbeat_timeout_seconds,
            'page_close_grace_seconds': self.page_close_grace_seconds,
            "ready_timeout_seconds": self.ready_timeout_seconds,
            "public_port": self.public_port,
            "admin_token_configured": self.is_admin_token_configured(),
            "invitation_only": self.invitation_only,
            "system_invitation_codes_count": len(self.system_invitation_codes),
            "share_code_ttl_seconds": self.share_code_ttl_seconds,
            "copy_helper_image_configured": bool((self.copy_helper_image or "").strip()),
            "lease_cookie_name": self.lease_cookie_name,
            "lease_cookie_secure": self.lease_cookie_secure,
            "lease_cookie_samesite": self.lease_cookie_samesite,
            "lease_cookie_max_age_seconds": self.lease_cookie_max_age_seconds,
            "instance_env_keys": sorted(self.instance_env),
        }
