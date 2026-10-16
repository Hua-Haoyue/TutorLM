"""控制面配置：全部从 ``CONTROL_PLANE_*`` 环境变量读取。

设计原则与 instance-app 的 ``HostConfig`` 一致：字段显式、可覆盖、非法配置
立刻抛错，而不是带着默认值继续跑。区别是这里的策略属于**容器外**：
槽位数量、镜像与卷前缀、就绪超时、租约 TTL、管理令牌。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

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


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(f"{ENV_PREFIX}{name}", default)


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"环境变量 {ENV_PREFIX}{name} 必须是整数，收到 {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"环境变量 {ENV_PREFIX}{name} 必须是数字，收到 {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


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

    # --- 租约 ---------------------------------------------------------------
    lease_ttl_seconds: int = 3600
    lease_idle_ttl_seconds: int = 0
    reclaim_on_idle: bool = False

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
        self.validate()

    def validate(self) -> None:
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

    # ------------------------------------------------------------------ names
    def volume_name(self, slot_id: int) -> str:
        """槽位专属数据卷名。"""

        return f"{self.volume_prefix}-s{int(slot_id):02d}"

    def container_name(self, slot_id: int, generation: int) -> str:
        """容器名带代次，便于日志与孤儿资源排查。"""

        return f"{self.container_prefix}-s{int(slot_id):02d}-g{int(generation):04d}"

    def labels(self) -> dict[str, str]:
        return {self.label_key: self.label_value}

    def is_admin_token_configured(self) -> bool:
        return bool(self.admin_token.strip())

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

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None, **overrides: Any) -> "ControlPlaneConfig":
        """从环境变量构建；``environ`` 仅用于测试注入。"""

        source = dict(os.environ if environ is None else environ)
        get = lambda name, default=None: source.get(f"{ENV_PREFIX}{name}", default)  # noqa: E731

        def as_int(name: str, default: int) -> int:
            raw = get(name)
            if raw is None or not str(raw).strip():
                return default
            try:
                return int(str(raw).strip())
            except ValueError as exc:
                raise ConfigError(f"环境变量 {ENV_PREFIX}{name} 必须是整数") from exc

        def as_float(name: str, default: float) -> float:
            raw = get(name)
            if raw is None or not str(raw).strip():
                return default
            try:
                return float(str(raw).strip())
            except ValueError as exc:
                raise ConfigError(f"环境变量 {ENV_PREFIX}{name} 必须是数字") from exc

        def as_bool(name: str, default: bool) -> bool:
            raw = get(name)
            if raw is None:
                return default
            return str(raw).strip().lower() in {"1", "true", "yes", "on"}

        raw_db = get("DB_PATH")
        db_path = Path(raw_db).expanduser() if raw_db else _default_db_path()

        raw_cpus = get("CPUS")
        cpus = float(raw_cpus) if raw_cpus not in (None, "") else 2.0

        network = get("NETWORK")
        if network is not None and not network.strip():
            network = None

        config = cls(
            db_path=db_path,
            slots=as_int("SLOTS", 3),
            instance_image=get("INSTANCE_IMAGE") or "tutorlm-host:0.2.0-single",
            instance_port=as_int("INSTANCE_PORT", 8000),
            instance_mount_path=get("INSTANCE_MOUNT_PATH") or "/data",
            network=network,
            container_prefix=get("CONTAINER_PREFIX") or "tutorlm-instance",
            volume_prefix=get("VOLUME_PREFIX") or "tutorlm-instance-data",
            label_key=get("LABEL_KEY") or "app",
            label_value=get("LABEL_VALUE") or "tutorlm-instance",
            docker_host=get("DOCKER_HOST") or None,
            lease_ttl_seconds=as_int("LEASE_TTL_SECONDS", 3600),
            lease_idle_ttl_seconds=as_int("LEASE_IDLE_TTL_SECONDS", 0),
            reclaim_on_idle=as_bool("RECLAIM_ON_IDLE", False),
            adapter=(get("ADAPTER") or ADAPTER_DOCKER).strip().lower(),
            ready_timeout_seconds=as_float("READY_TIMEOUT_SECONDS", 60.0),
            ready_poll_interval_seconds=as_float("READY_POLL_INTERVAL_SECONDS", 1.0),
            stop_grace_seconds=as_int("STOP_GRACE_SECONDS", 30),
            mem_limit=get("MEM_LIMIT") or None,
            cpus=cpus,
            pids_limit=as_int("PIDS_LIMIT", 256),
            tmpfs_size=get("TMPFS_SIZE") or None,
            gc_enabled=as_bool("GC_ENABLED", True),
            gc_interval_seconds=as_int("GC_INTERVAL_SECONDS", 30),
            admin_token=get("ADMIN_TOKEN") or "",
            token_pepper=get("TOKEN_PEPPER") or "",
            lease_cookie_name=(get("LEASE_COOKIE_NAME") or "tutorlm_lease").strip(),
            lease_cookie_secure=as_bool("LEASE_COOKIE_SECURE", False),
            lease_cookie_samesite=(
                get("LEASE_COOKIE_SAMESITE") or "lax"
            ).strip().lower(),
            lease_cookie_max_age_seconds=(
                as_int("LEASE_COOKIE_MAX_AGE_SECONDS", 0) or None
            ),
            public_base_url=(get("PUBLIC_BASE_URL") or "").rstrip("/"),
            public_port=as_int("PUBLIC_PORT", 47823),
            instance_env=instance_env_from_environ(source),
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
            "ready_timeout_seconds": self.ready_timeout_seconds,
            "public_port": self.public_port,
            "admin_token_configured": self.is_admin_token_configured(),
            "lease_cookie_name": self.lease_cookie_name,
            "lease_cookie_secure": self.lease_cookie_secure,
            "lease_cookie_samesite": self.lease_cookie_samesite,
            "lease_cookie_max_age_seconds": self.lease_cookie_max_age_seconds,
            "instance_env_keys": sorted(self.instance_env),
        }
