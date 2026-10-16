"""``TUTORLM_*`` 配置解析与**严格校验**（env 是唯一来源）。

设计原则与控制面 ``ControlPlaneConfig`` 一致：字段显式、非法配置立刻抛
``ConfigInvalid``，而不是带着默认值继续跑。区别是本对象属于**容器内**：
数据根、卷外会话目录、workspace/owner 绑定、运行模式与 provider。

约束（``contracts/instance-runtime-v3.md`` 与设计 §9.1）：

- ``TUTORLM_DATA_DIR`` 是**当前项目**的数据根；``TUTORLM_SESSION_DATA_DIR`` 必须**在卷外**；
- 持久模式（``TUTORLM_PERSISTENT_WORKSPACE=true``）**必须**提供
  ``WORKSPACE_ID``/``OWNER_ID``/``DATA_DIR``/``SUBDIRECTORY``/``SESSION_DATA_DIR``；
- ``TUTORLM_WORKSPACE_SOURCE_ID`` / ``TUTORLM_WORKSPACE_COPY_ID`` 为 **planned**：
  控制面当前不注入，缺失即按"未提供"处理（重绑 fail closed）；
- 模型密钥只从 env 读，**不落库、不写卷、不入镜像**，也**不**出现在 ``describe()`` 里。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from typing import Any, ClassVar, Mapping

from backend.domain.enums import StorageLayout
from backend.domain.errors import ConfigInvalid
from backend.domain.paths import is_within, normalize_relative_path
from backend.domain.values import optional_text, parse_bool, parse_float, parse_int, require_choice
from backend.domain.volume import LEGACY_SUBDIRECTORY, is_legacy_layout, validate_workspace_subdirectory

ENV_PREFIX = "TUTORLM_"

#: 运行模式（``/api/runtime`` 的 ``mode``）。
RUN_MODE_LOCAL = "local"
RUN_MODE_DEMO = "demo"
RUN_MODE_MANAGED = "managed"
VALID_RUN_MODES: tuple[str, ...] = (RUN_MODE_LOCAL, RUN_MODE_DEMO, RUN_MODE_MANAGED)

#: provider 名称。
PROVIDER_FAKE = "fake"
PROVIDER_OPENAI_COMPATIBLE = "openai_compatible"
VALID_PROVIDERS: tuple[str, ...] = (PROVIDER_FAKE, PROVIDER_OPENAI_COMPATIBLE)

#: 数据挂在容器内的默认挂载点（v1）。
DEFAULT_MOUNT_PATH = "/data"

#: 会话数据目录名：卷内出现任何一个都等价于"卷内可能有会话"，禁止声明 content_only。
SESSION_SUBDIRECTORY_NAMES: tuple[str, ...] = ("session", "sessions", "auth", "credentials")

#: 数据根内不允许是会话目录（大小写不敏感比较用）。
_SECRET_NAME_RE = re.compile(r"^(session|sessions|auth|credentials?|tokens)$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ModelSettings:
    """模型 provider 配置。``api_key`` 只在内存中，不落库/不入日志。"""

    provider: str = PROVIDER_FAKE
    api_base: str | None = None
    api_key: str | None = None
    name: str = "offline-demo"
    max_tokens: int = 1024
    timeout_seconds: float = 30.0

    @property
    def is_fake(self) -> bool:
        return self.provider == PROVIDER_FAKE

    def describe(self) -> dict[str, Any]:
        """对外摘要：**绝不**包含 ``api_key``。"""

        return {
            "provider": self.provider,
            "model": self.name,
            "api_base": self.api_base,
            "max_tokens": self.max_tokens,
            "timeout_seconds": self.timeout_seconds,
            "api_key_configured": bool(self.api_key),
        }


@dataclass(frozen=True, slots=True)
class Settings:
    """应用配置的不可变快照。"""

    run_mode: str = RUN_MODE_LOCAL
    persistent_workspace: bool = False
    workspace_id: str | None = None
    owner_id: str | None = None
    data_dir: str = DEFAULT_MOUNT_PATH
    mount_path: str = DEFAULT_MOUNT_PATH
    workspace_subdirectory: str = LEGACY_SUBDIRECTORY
    session_data_dir: str | None = None
    workspace_source_id: str | None = None
    workspace_copy_id: str | None = None
    invite_code: str | None = None
    allow_implicit_session: bool = False
    model: ModelSettings = field(default_factory=ModelSettings)
    business_db_name: str = "business.sqlite3"
    #: 配额（按项目持久计量；超限 413/429）。
    max_documents: int = 50
    max_bytes_per_file: int = 2 * 1024 * 1024
    max_runs: int = 20

    ENV_MAP: ClassVar[Mapping[str, str]] = {
        "run_mode": f"{ENV_PREFIX}RUN_MODE",
        "persistent_workspace": f"{ENV_PREFIX}PERSISTENT_WORKSPACE",
        "workspace_id": f"{ENV_PREFIX}WORKSPACE_ID",
        "owner_id": f"{ENV_PREFIX}OWNER_ID",
        "data_dir": f"{ENV_PREFIX}DATA_DIR",
        "mount_path": f"{ENV_PREFIX}MOUNT_PATH",
        "workspace_subdirectory": f"{ENV_PREFIX}WORKSPACE_SUBDIRECTORY",
        "session_data_dir": f"{ENV_PREFIX}SESSION_DATA_DIR",
        "workspace_source_id": f"{ENV_PREFIX}WORKSPACE_SOURCE_ID",
        "workspace_copy_id": f"{ENV_PREFIX}WORKSPACE_COPY_ID",
        "invite_code": f"{ENV_PREFIX}INVITE_CODE",
        "allow_implicit_session": f"{ENV_PREFIX}ALLOW_IMPLICIT_SESSION",
        "provider": f"{ENV_PREFIX}PROVIDER",
        "model_api_base": f"{ENV_PREFIX}MODEL_API_BASE",
        "model_api_key": f"{ENV_PREFIX}MODEL_API_KEY",
        "model_name": f"{ENV_PREFIX}MODEL_NAME",
        "model_max_tokens": f"{ENV_PREFIX}MODEL_MAX_TOKENS",
        "model_timeout_seconds": f"{ENV_PREFIX}MODEL_TIMEOUT_SECONDS",
        "max_documents": f"{ENV_PREFIX}MAX_DOCUMENTS",
        "max_bytes_per_file": f"{ENV_PREFIX}MAX_BYTES_PER_FILE",
        "max_runs": f"{ENV_PREFIX}MAX_RUNS",
    }

    # ------------------------------------------------------------ 属性
    @property
    def is_legacy_layout(self) -> bool:
        return is_legacy_layout(self.workspace_subdirectory)

    @property
    def provider_name(self) -> str:
        return self.model.provider

    @property
    def business_db_path(self) -> str:
        return f"{self.data_dir.rstrip('/')}/{self.business_db_name}"

    # ------------------------------------------------------------ 解析
    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        overrides: Mapping[str, Any] | None = None,
    ) -> "Settings":
        """从 ``TUTORLM_*`` 构建配置并**严格校验**。"""

        env = dict(os.environ if environ is None else environ)
        raw = {key: env.get(name) for key, name in cls.ENV_MAP.items()}

        persistent = parse_bool(raw["persistent_workspace"], field=cls.ENV_MAP["persistent_workspace"], default=False)
        # run_mode 与外层 host config 一致：未提供时，持久默认 ``managed``、非持久默认 ``demo``；
        # 但**不接受**不认识的取值（仍然严格校验，不会惄惄降级）。
        run_mode = require_choice(
            raw["run_mode"] or (RUN_MODE_MANAGED if persistent else RUN_MODE_DEMO),
            field=cls.ENV_MAP["run_mode"],
            allowed=VALID_RUN_MODES,
        )

        mount_path = optional_text(raw["mount_path"]) or DEFAULT_MOUNT_PATH
        workspace_id = optional_text(raw["workspace_id"])
        owner_id = optional_text(raw["owner_id"])
        data_dir = optional_text(raw["data_dir"])
        subdirectory = optional_text(raw["workspace_subdirectory"])
        session_dir = optional_text(raw["session_data_dir"])

        if persistent:
            missing = [
                name
                for name, value in (
                    (cls.ENV_MAP["workspace_id"], workspace_id),
                    (cls.ENV_MAP["owner_id"], owner_id),
                    (cls.ENV_MAP["data_dir"], data_dir),
                    (cls.ENV_MAP["workspace_subdirectory"], subdirectory),
                    (cls.ENV_MAP["session_data_dir"], session_dir),
                )
                if not value
            ]
            if missing:
                raise ConfigInvalid(
                    "持久模式缺少必需的 TUTORLM_* 配置：" + ", ".join(missing),
                    details={"missing": missing},
                )
        else:
            data_dir = data_dir or mount_path
            subdirectory = subdirectory or LEGACY_SUBDIRECTORY

        assert data_dir is not None  # 非持久分支已兜底
        subdirectory = validate_workspace_subdirectory(
            subdirectory,
            owner_id=owner_id if persistent else None,
            workspace_id=workspace_id if persistent else None,
        )

        # TUTORLM_PROVIDER 未提供时默认 ``fake``（离线确定性演示模型，与 host config 与设计 §7 一致）；
        # 供应商名仍然只在已知集合内，写错立刻报错而不是静默回退到 fake。
        provider = require_choice(
            raw["provider"] or PROVIDER_FAKE,
            field=cls.ENV_MAP["provider"],
            allowed=VALID_PROVIDERS,
        )
        model = ModelSettings(
            provider=provider,
            api_base=optional_text(raw["model_api_base"]),
            api_key=optional_text(raw["model_api_key"]),
            name=optional_text(raw["model_name"]) or ("offline-demo" if provider == PROVIDER_FAKE else ""),
            max_tokens=parse_int(
                raw["model_max_tokens"],
                field=cls.ENV_MAP["model_max_tokens"],
                default=1024,
                minimum=1,
                maximum=1_000_000,
            ),
            timeout_seconds=parse_float(
                raw["model_timeout_seconds"],
                field=cls.ENV_MAP["model_timeout_seconds"],
                default=30.0,
                minimum=0.1,
                maximum=600.0,
            ),
        )
        if provider == PROVIDER_OPENAI_COMPATIBLE:
            missing_model = [
                name
                for name, value in (
                    (cls.ENV_MAP["model_api_base"], model.api_base),
                    (cls.ENV_MAP["model_api_key"], model.api_key),
                    (cls.ENV_MAP["model_name"], model.name),
                )
                if not value
            ]
            if missing_model:
                raise ConfigInvalid(
                    "openai_compatible provider 缺少必需配置：" + ", ".join(missing_model),
                    details={"missing": missing_model},
                )

        settings = cls(
            run_mode=run_mode,
            persistent_workspace=persistent,
            workspace_id=workspace_id,
            owner_id=owner_id,
            data_dir=cls._normalize_path(data_dir, field=cls.ENV_MAP["data_dir"]),
            mount_path=cls._normalize_path(mount_path, field=cls.ENV_MAP["mount_path"]),
            workspace_subdirectory=subdirectory,
            session_data_dir=(
                cls._normalize_path(session_dir, field=cls.ENV_MAP["session_data_dir"]) if session_dir else None
            ),
            workspace_source_id=optional_text(raw["workspace_source_id"]),
            workspace_copy_id=optional_text(raw["workspace_copy_id"]),
            invite_code=optional_text(raw["invite_code"]),
            allow_implicit_session=parse_bool(
                raw["allow_implicit_session"],
                field=cls.ENV_MAP["allow_implicit_session"],
                default=False,
            ),
            model=model,
            max_documents=parse_int(
                raw["max_documents"], field=cls.ENV_MAP["max_documents"], default=50, minimum=1
            ),
            max_bytes_per_file=parse_int(
                raw["max_bytes_per_file"],
                field=cls.ENV_MAP["max_bytes_per_file"],
                default=2 * 1024 * 1024,
                minimum=1,
            ),
            max_runs=parse_int(raw["max_runs"], field=cls.ENV_MAP["max_runs"], default=20, minimum=1),
        )
        settings.assert_consistent()
        return settings.with_overrides(**overrides) if overrides else settings

    @staticmethod
    def _normalize_path(value: str, *, field: str) -> str:
        """数据根必须是小写的绝对 posix 路径；不接受相对路径与 ``..``。"""

        text = str(value).strip().replace("\\", "/")
        if not text:
            raise ConfigInvalid(f"{field} 不能为空")
        if not text.startswith("/"):
            raise ConfigInvalid(f"{field} 必须是容器内绝对路径：{value!r}")
        if ".." in text.split("/"):
            raise ConfigInvalid(f"{field} 不允许 '..' 段：{value!r}")
        normalized = normalize_relative_path(text.lstrip("/"))
        return "/" + normalized

    # ------------------------------------------------------------ 校验
    def assert_consistent(self) -> None:
        """跨字段不变量：会话目录必须在卷外，数据根与子目录必须自洽。"""

        if not self.persistent_workspace:
            return
        if not self.session_data_dir:
            raise ConfigInvalid("持久模式缺少 TUTORLM_SESSION_DATA_DIR")
        session = self.session_data_dir.rstrip("/")
        data = self.data_dir.rstrip("/")
        if session == data or is_within(data, session):
            raise ConfigInvalid(
                "TUTORLM_SESSION_DATA_DIR 必须位于 TUTORLM_DATA_DIR 之外（session 会被卷复制带走）",
                details={"data_dir": data, "session_data_dir": session},
            )
        if is_within(session, data):
            raise ConfigInvalid(
                "TUTORLM_DATA_DIR 不能位于 TUTORLM_SESSION_DATA_DIR 之内",
                details={"data_dir": data, "session_data_dir": session},
            )
        if not self.is_legacy_layout:
            expected = f"{self.mount_path.rstrip('/')}/{self.workspace_subdirectory}"
            if data != expected:
                raise ConfigInvalid(
                    "TUTORLM_DATA_DIR 必须等于挂载点 + TUTORLM_WORKSPACE_SUBDIRECTORY",
                    details={"expected": expected, "actual": data},
                )
        for name in SESSION_SUBDIRECTORY_NAMES:
            if _SECRET_NAME_RE.match(name) and name in self.workspace_subdirectory.lower().split("/"):
                raise ConfigInvalid(f"数据根子目录不允许包含会话/凭证命名：{name}")

    def assert_session_dir_outside_data(self) -> None:
        """共享给就绪判定的重复断言（防止调用方绕过 ``from_env``）。"""

        self.assert_consistent()

    # ------------------------------------------------------------ 覆盖
    def with_overrides(self, **overrides: Any) -> "Settings":
        """显式覆盖（主要给测试）；覆盖后仍重新校验。"""

        unknown = set(overrides) - {f for f in self.__dataclass_fields__}
        if unknown:
            raise ConfigInvalid(f"未知的配置覆盖字段：{sorted(unknown)}")
        updated = replace(self, **overrides)
        updated.assert_consistent()
        return updated

    @classmethod
    def for_tests(cls, tmp_path: Any, **overrides: Any) -> "Settings":
        """测试用隔离配置：临时数据根与卷外会话目录。"""

        base = str(tmp_path).replace("\\", "/")
        values: dict[str, Any] = {
            "run_mode": RUN_MODE_DEMO,
            "persistent_workspace": False,
            "workspace_id": None,
            "owner_id": None,
            "data_dir": f"{base}/data",
            "mount_path": f"{base}/data",
            "workspace_subdirectory": LEGACY_SUBDIRECTORY,
            "session_data_dir": f"{base}/session",
            "model": ModelSettings(),
        }
        values.update(overrides)
        return cls(**values)

    @classmethod
    def for_persistent_tests(
        cls,
        tmp_path: Any,
        *,
        workspace_id: str,
        owner_id: str,
        **overrides: Any,
    ) -> "Settings":
        """持久模式测试配置：v3 子目录布局（可声明 content_only）。"""

        base = str(tmp_path).replace("\\", "/")
        subdirectory = f"users/{owner_id}/workspaces/{workspace_id}"
        values: dict[str, Any] = {
            "run_mode": RUN_MODE_MANAGED,
            "persistent_workspace": True,
            "workspace_id": workspace_id,
            "owner_id": owner_id,
            "mount_path": f"{base}/data",
            "data_dir": f"{base}/data/{subdirectory}",
            "workspace_subdirectory": subdirectory,
            "session_data_dir": f"{base}/session/s01",
            "model": ModelSettings(),
        }
        values.update(overrides)
        return cls(**values)

    def describe(self) -> dict[str, Any]:
        """对外可见摘要（**不含**模型密钥与体验码明文）。"""

        return {
            "run_mode": self.run_mode,
            "persistent_workspace": self.persistent_workspace,
            "workspace_id": self.workspace_id,
            "owner_id": self.owner_id,
            "data_dir": self.data_dir,
            "workspace_subdirectory": self.workspace_subdirectory,
            "session_data_dir": self.session_data_dir,
            "session_dir_outside_data": bool(
                self.session_data_dir
                and not is_within(self.data_dir.rstrip('/'), self.session_data_dir.rstrip('/'))
            ),
            "has_workspace_source_id": bool(self.workspace_source_id),
            "has_workspace_copy_id": bool(self.workspace_copy_id),
            "allow_implicit_session": self.allow_implicit_session,
            "model": self.model.describe(),
            "limits": self.limits(),
        }

    def limits(self) -> dict[str, int]:
        """``/api/runtime`` 的 ``limits`` 字段。"""

        return {
            "max_documents_per_session": self.max_documents,
            "max_bytes_per_file": self.max_bytes_per_file,
            "max_runs_per_session": self.max_runs,
        }

    def declared_layout_hint(self) -> StorageLayout | None:
        """从子目录形状给出的**上界**提示；真正判定仍走 storage 证据。"""

        if self.is_legacy_layout:
            return None
        return StorageLayout.CONTENT_ONLY
