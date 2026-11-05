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


def guidance_texts(value: Any) -> list[str]:
    """Keep the string-list contract without rendering backend objects as repr."""
    from exceptions import BackendContractError

    if not isinstance(value, list):
        raise BackendContractError("Guidance must be a list of strings or text objects.")
    result = []
    for item in value:
        if isinstance(item, str):
            result.append(item)
            continue
        if isinstance(item, dict):
            text = item.get("text")
            if not isinstance(text, str) or not text.strip():
                text = item.get("description")
            if isinstance(text, str) and text.strip():
                result.append(text)
                continue
        raise BackendContractError("Guidance entry must provide text or description.")
    return result


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
        return optional_str(value.get("displayName", value.get("display_name", value.get("name", value.get("nickname", value.get("agentName"))))))
    return optional_str(value)


def unwrap_dict_data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    return payload


def read_collection(payload: Any, *keys: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Require an explicit collection; missing or corrupt data is not an empty page."""
    from exceptions import BackendContractError

    if not isinstance(payload, dict) or payload.get("success") is False or payload.get("error") not in (None, False, "", {}):
        raise BackendContractError("Read response must be a successful object.")
    data = payload["data"] if "data" in payload else payload
    if not isinstance(data, dict):
        raise BackendContractError("Read response data must be an object.")
    for key in keys:
        if key in data:
            return data, require_object_list(data[key], key)
    raise BackendContractError("Read response is missing its collection.")


def require_object_list(value: Any, field: str) -> list[dict[str, Any]]:
    from exceptions import BackendContractError

    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise BackendContractError(f"Read response {field} must be a list of objects.")
    return value


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


def post_content(item: dict[str, Any]) -> str | None:
    value = item.get("content")
    return value if isinstance(value, str) else None


def post_preview(item: dict[str, Any]) -> str:
    for key in ("content_preview", "contentPreview", "contentsExcerpt", "summary"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    return (post_content(item) or "")[:500]
