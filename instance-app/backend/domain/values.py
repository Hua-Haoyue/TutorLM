"""取值校验与小工具（无 IO）。"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from backend.domain.enums import (
    DocumentFormat,
    DocumentStatus,
    EdgeRelation,
    ExtractionStatus,
    NodeKind,
    RevisionStatus,
    RunKind,
    RunState,
    RunWorkload,
)
from backend.domain.errors import ConfigInvalid, DomainError

_TRUE = frozenset({"1", "true", "yes", "on", "y", "t"})
_FALSE = frozenset({"0", "false", "no", "off", "n", "f", ""})


def parse_bool(raw: Any, *, field: str, default: bool | None = None) -> bool:
    """严格布尔解析：不认识的取值直接报错，不做 truthy 猜测。"""

    if raw is None:
        if default is None:
            raise ConfigInvalid(f"{field} 缺失")
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int):
        if raw in (0, 1):
            return bool(raw)
        raise ConfigInvalid(f"{field} 必须是布尔值：{raw!r}")
    text = str(raw).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ConfigInvalid(f"{field} 必须是布尔值：{raw!r}")


def parse_int(
    raw: Any,
    *,
    field: str,
    default: int,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        value = default
    else:
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            raise ConfigInvalid(f"{field} 必须是整数：{raw!r}") from None
    if minimum is not None and value < minimum:
        raise ConfigInvalid(f"{field} 不能小于 {minimum}：{value}")
    if maximum is not None and value > maximum:
        raise ConfigInvalid(f"{field} 不能大于 {maximum}：{value}")
    return value


def parse_float(
    raw: Any,
    *,
    field: str,
    default: float,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        value = default
    else:
        try:
            value = float(str(raw).strip())
        except (TypeError, ValueError):
            raise ConfigInvalid(f"{field} 必须是数字：{raw!r}") from None
    if minimum is not None and value < minimum:
        raise ConfigInvalid(f"{field} 不能小于 {minimum}：{value}")
    if maximum is not None and value > maximum:
        raise ConfigInvalid(f"{field} 不能大于 {maximum}：{value}")
    return value


def require_text(raw: Any, *, field: str) -> str:
    if raw is None:
        raise ConfigInvalid(f"{field} 缺失")
    value = str(raw).strip()
    if not value:
        raise ConfigInvalid(f"{field} 不能为空")
    return value


def optional_text(raw: Any) -> str | None:
    if raw is None:
        return None
    value = str(raw).strip()
    return value or None


def require_choice(raw: Any, *, field: str, allowed: Iterable[str]) -> str:
    """严格枚举：把取值限制在已冻结的字面量集合内。"""

    value = require_text(raw, field=field)
    allowed_set = {str(item) for item in allowed}
    if value not in allowed_set:
        raise ConfigInvalid(f"{field} 取值非法：{value!r}；允许：{sorted(allowed_set)}")
    return value


def is_valid_kind(value: str | None, allowed: type) -> bool:
    if value is None:
        return False
    try:
        allowed(value)
    except ValueError:
        return False
    return True


def as_dict(pairs: Mapping[str, Any] | None) -> dict[str, Any]:
    if not pairs:
        return {}
    if not isinstance(pairs, Mapping):
        raise DomainError("metadata 必须是对象", code="invalid_metadata", http_status=400)
    return dict(pairs)


#: 需要显式校验的枚举类型（供 repository / schema 层复用）。
ENUM_TYPES: tuple[type, ...] = (
    DocumentFormat,
    DocumentStatus,
    RevisionStatus,
    ExtractionStatus,
    NodeKind,
    EdgeRelation,
    RunKind,
    RunState,
    RunWorkload,
)
