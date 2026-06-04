from __future__ import annotations

from typing import Any


def camel_case(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in tail)


def dict_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def optional_int(value: Any, default: int | None = None) -> int | None:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def optional_int_from_float(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def optional_bool(value: Any, default: bool | None = None) -> bool | None:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def id_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def optional_id(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def extract_name(value: Any) -> str | None:
    if isinstance(value, dict):
        return optional_str(value.get("name", value.get("nickname", value.get("agentName"))))
    return optional_str(value)


def unwrap_dict_data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    return payload


def unwrap_list_data(payload: dict[str, Any], *, nested_key: str | None = None) -> list[dict[str, Any]]:
    data = payload.get("data")
    if isinstance(data, list):
        return dict_list(data)
    if nested_key and isinstance(data, dict):
        nested = data.get(nested_key)
        if isinstance(nested, list):
            return dict_list(nested)
    if nested_key:
        nested = payload.get(nested_key)
        if isinstance(nested, list):
            return dict_list(nested)
    return []


def compact_params(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}
