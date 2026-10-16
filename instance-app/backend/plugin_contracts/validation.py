"""Small, fail-closed JSON Schema subset used by static workflow declarations."""

from __future__ import annotations

import json
from typing import Any, Mapping

from backend.domain.errors import InvalidInputError
from backend.plugin_contracts.manifest import PluginContractError

MAX_JSON_BYTES = 256 * 1024
_KEYWORDS = {
    "type", "properties", "required", "additionalProperties", "items", "enum",
    "minLength", "maxLength", "minItems", "maxItems", "minimum", "maximum",
    "title", "description",
}
_TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}


def check_schema(schema: Mapping[str, Any], depth: int = 0) -> None:
    if depth > 16 or not isinstance(schema, Mapping):
        raise PluginContractError("Invalid or excessively nested workflow schema")
    if set(schema) - _KEYWORDS:
        raise PluginContractError("Unsupported workflow schema keywords")
    types = schema.get("type", [])
    types = [types] if isinstance(types, str) else types
    if not isinstance(types, list) or not set(types) <= _TYPES:
        raise PluginContractError("Unsupported workflow schema type")
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        raise PluginContractError("Schema properties must be an object")
    required = schema.get("required", [])
    if not isinstance(required, list) or any(not isinstance(k, str) for k in required):
        raise PluginContractError("Schema required must be a string list")
    if not isinstance(schema.get("additionalProperties", True), bool):
        raise PluginContractError("additionalProperties must be boolean")
    if "enum" in schema and not isinstance(schema["enum"], list):
        raise PluginContractError("Schema enum must be a list")
    for child in properties.values():
        check_schema(child, depth + 1)
    if "items" in schema:
        check_schema(schema["items"], depth + 1)
    for key in ("minLength", "maxLength", "minItems", "maxItems", "minimum", "maximum"):
        if key in schema and type(schema[key]) not in (int, float):
            raise PluginContractError(f"Schema {key} must be numeric")


def bounded_json(value: Any) -> Any:
    """Return a detached JSON snapshot; reject coercions, NaN and oversized data."""
    try:
        def check_keys(item: Any, depth: int = 0) -> None:
            if depth > 32:
                raise ValueError("Workflow JSON nesting exceeds 32 levels")
            if isinstance(item, dict):
                if any(not isinstance(key, str) for key in item):
                    raise ValueError("JSON object keys must be strings")
                for child in item.values():
                    check_keys(child, depth + 1)
            elif isinstance(item, list):
                for child in item:
                    check_keys(child, depth + 1)
            elif type(item) not in (str, int, float, bool, type(None)):
                raise ValueError("Non-JSON value")

        check_keys(value)
        raw = json.dumps(value, allow_nan=False, ensure_ascii=True)
        if len(raw.encode("utf-8")) > MAX_JSON_BYTES:
            raise ValueError("Workflow JSON exceeds 256 KiB")
        return json.loads(raw)
    except (TypeError, ValueError, RecursionError) as exc:
        raise InvalidInputError("Workflow data must be bounded JSON") from exc


def validate_value(value: Any, schema: Mapping[str, Any], path: str = "input") -> None:
    matches = {
        "object": isinstance(value, dict), "array": isinstance(value, list),
        "string": isinstance(value, str), "integer": type(value) is int,
        "number": type(value) in (int, float), "boolean": type(value) is bool,
        "null": value is None,
    }
    types = schema.get("type", [])
    types = [types] if isinstance(types, str) else types
    if types and not any(matches[t] for t in types):
        raise InvalidInputError(f"{path}: incorrect type")
    if "enum" in schema and value not in schema["enum"]:
        raise InvalidInputError(f"{path}: value is not allowed")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        if any(key not in value for key in schema.get("required", [])):
            raise InvalidInputError(f"{path}: missing required field")
        if schema.get("additionalProperties") is False and set(value) - set(props):
            raise InvalidInputError(f"{path}: unknown field")
        for key, child in props.items():
            if key in value:
                validate_value(value[key], child, f"{path}.{key}")
    if isinstance(value, list) and "items" in schema:
        for index, child in enumerate(value):
            validate_value(child, schema["items"], f"{path}[{index}]")
    bounds = []
    if isinstance(value, str):
        bounds = [("minLength", len(value), True), ("maxLength", len(value), False)]
    elif isinstance(value, list):
        bounds = [("minItems", len(value), True), ("maxItems", len(value), False)]
    elif type(value) in (int, float):
        bounds = [("minimum", value, True), ("maximum", value, False)]
    for key, actual, lower in bounds:
        if key in schema and (actual < schema[key] if lower else actual > schema[key]):
            raise InvalidInputError(f"{path}: violates {key}")


def checkpoint(context: Any) -> None:
    """Check the SDK cooperative cancellation token."""
    token = getattr(context, "cancel", context)
    check = getattr(token, "check", None)
    if callable(check):
        check()
