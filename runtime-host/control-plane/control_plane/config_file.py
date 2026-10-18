"""Control-plane config file loading (TOML only).

This module owns exactly one job: turning a path into a validated mapping of
control-plane settings. It never touches the process environment for anything
other than an explicit ``environ`` argument and it never falls back to a
"default path" - the caller must decide whether a file is in play.

Layout (TOML)::

    invitationonly = true            # root-level legacy spelling, optional

    [control_plane]
    invitationonly = true
    slots = 4
    system_invitation_codes = ["TLM-SYS-v1-..."]   # core-generated, or a named code such as "TUTORLM-DONGNIYISI"

    [instance_env]
    RUN_MODE = "demo"                # -> TUTORLM_RUN_MODE in the container

Only TOML is supported: the CLI, ``CONTROL_PLANE_CONFIG`` entry point and
``docs/configuration.md`` describe a single format, so there is no second
parser to drift out of sync. Unknown keys are rejected instead of silently
ignored; a typo in a security switch must not be mistaken for "feature off".
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Mapping

from control_plane.errors import ConfigError

#: Environment variable that points at the config file when no explicit path is
#: passed. Reading it is the *only* implicit lookup in this module.
CONFIG_PATH_ENV = "CONTROL_PLANE_CONFIG"
CONFIG_PATH_ENV_ALIAS = "TUTORLM_CONTROL_PLANE_CONFIG"

CONTROL_PLANE_TABLE = "control_plane"
INSTANCE_ENV_TABLE = "instance_env"
#: Root-level spellings accepted for convenience. Only unambiguous booleans are
#: allowed here; everything else must live in ``[control_plane]``.
ROOT_INVITATION_KEY = "invitationonly"
ROOT_INVITATION_KEY_PYTHON = "invitation_only"

TABLES: tuple[str, ...] = (CONTROL_PLANE_TABLE, INSTANCE_ENV_TABLE)


def resolve_config_path(
    explicit: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path | None:
    """Return the config path to load, or ``None`` when no file is configured.

    ``explicit`` always wins. Otherwise ``CONTROL_PLANE_CONFIG`` (with the
    deployment-friendly ``TUTORLM_CONTROL_PLANE_CONFIG`` alias) is consulted.
    A configured-but-missing path is an error: starting a control plane with a
    typo'd path while believing the file took effect is worse than refusing.
    """

    raw: Any = explicit
    source = environ if environ is not None else None
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        lookup = source if source is not None else _os_environ()
        raw = lookup.get(CONFIG_PATH_ENV) or lookup.get(CONFIG_PATH_ENV_ALIAS)
        raw = raw.strip() if isinstance(raw, str) else raw
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if isinstance(raw, Path):
        path = raw
    elif isinstance(raw, str):
        path = Path(raw)
    else:  # pragma: no cover - defensive, callers pass str/Path
        raise ConfigError(f"{CONFIG_PATH_ENV} 必须是路径字符串，收到 {type(raw).__name__}")
    path = path.expanduser()
    if not path.is_file():
        raise ConfigError(f"{CONFIG_PATH_ENV} 指向的文件不存在：{path}")
    return path


def _os_environ() -> Mapping[str, str]:
    import os

    return os.environ


def load_config_file(path: str | Path) -> dict[str, Any]:
    """Read *path* as TOML and return the raw document (no validation yet)."""

    file_path = Path(path).expanduser()
    try:
        data = file_path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件 {file_path}：{exc}") from exc
    if file_path.suffix.lower() == ".json":
        raise ConfigError(
            f"配置文件 {file_path} 是 JSON；当前只支持 TOML（见 docs/configuration.md）"
        )
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ConfigError(f"配置文件 {file_path} 不是 UTF-8 文本") from None
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        # Parser diagnostics may contain source tokens, including credentials.
        raise ConfigError(f"配置文件 {file_path} 不是合法 TOML") from None
    if not isinstance(document, dict):
        raise ConfigError(f"配置文件 {file_path} 顶层必须是对象/表")
    return document


def parse_config_document(
    document: Mapping[str, Any],
    *,
    source: str = "<config>",
) -> tuple[dict[str, Any], dict[str, str]]:
    """Split a raw document into ``(control_plane, instance_env)``.

    Returns plain values: ``control_plane`` holds TOML-native Python objects
    (bool/int/float/str/list) exactly as written, so the caller can apply strict
    type checks knowing whether a value came from a file rather than a string.
    """

    if not isinstance(document, Mapping):
        raise ConfigError(f"{source} 顶层必须是对象/表")
    allowed = set(TABLES)
    unknown = sorted(set(document) - allowed)
    root_invitation: Any = _MISSING
    for key in (ROOT_INVITATION_KEY, ROOT_INVITATION_KEY_PYTHON, 'inviteonly'):
        if key in document:
            if not isinstance(document[key], bool):
                raise ConfigError(
                    f"{source} 根级 {key} 必须是布尔值（放 [control_plane] 里可以写别的字段）"
                )
            if root_invitation is not _MISSING and root_invitation != document[key]:
                raise ConfigError(f"{source} 根级 invitationonly 与 invitation_only 取值冲突")
            root_invitation = document[key]
    unknown = [key for key in unknown if key not in {ROOT_INVITATION_KEY, ROOT_INVITATION_KEY_PYTHON, 'inviteonly'}]
    if unknown:
        raise ConfigError(
            f"{source} 未知的顶层配置段 {unknown}；只允许 {sorted(allowed)} 和根级 {ROOT_INVITATION_KEY}"
        )

    control_plane: dict[str, Any] = {}
    table = document.get(CONTROL_PLANE_TABLE)
    if table is not None:
        if not isinstance(table, Mapping):
            raise ConfigError(f"{source} [{CONTROL_PLANE_TABLE}] 必须是表")
        control_plane.update(_convert_table(table, f"[{CONTROL_PLANE_TABLE}]", source))
    if root_invitation is not _MISSING:
        existing = control_plane.get(ROOT_INVITATION_KEY)
        if existing is not None and existing != root_invitation:
            raise ConfigError(
                f"{source} 根级 {ROOT_INVITATION_KEY} 与 [{CONTROL_PLANE_TABLE}] 中的取值冲突；"
                "请只保留一处，避免含糊"
            )
        control_plane[ROOT_INVITATION_KEY] = root_invitation

    instance_env_raw = document.get(INSTANCE_ENV_TABLE)
    instance_env: dict[str, str] = {}
    if instance_env_raw is not None:
        if not isinstance(instance_env_raw, Mapping):
            raise ConfigError(f"{source} [{INSTANCE_ENV_TABLE}] 必须是表")
        for key, value in instance_env_raw.items():
            name = _instance_env_key(str(key), source)
            instance_env[name] = _instance_env_value(value, name, source)
    return control_plane, instance_env


class _Missing:
    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return "<missing>"


_MISSING = _Missing()


def _convert_table(table: Mapping[str, Any], label: str, source: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in table.items():
        name = str(key).strip()
        if not name:
            raise ConfigError(f"{source} {label} 出现空键名")
        if name != str(key):
            raise ConfigError(f"{source} {label} 键名 {key!r} 含多余空白")
        out[name] = value
    return out


def _instance_env_key(key: str, source: str) -> str:
    name = key.strip()
    if not name:
        raise ConfigError(f"{source} [{INSTANCE_ENV_TABLE}] 出现空键名")
    if name.upper().startswith("TUTORLM_"):
        return name.upper()
    return f"TUTORLM_{name.upper()}"


def _instance_env_value(value: Any, name: str, source: str) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, str)):
        return str(value)
    raise ConfigError(
        f"{source} [{INSTANCE_ENV_TABLE}] {name} 的值必须是字符串/数字/布尔，收到 {type(value).__name__}"
    )
