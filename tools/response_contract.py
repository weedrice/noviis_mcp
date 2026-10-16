from __future__ import annotations

from typing import Any

from exceptions import BackendContractError


def require_bool(data: dict[str, Any], *keys: str) -> bool:
    values = [data[key] for key in keys if key in data]
    if not values or any(type(value) is not bool or value != values[0] for value in values):
        raise BackendContractError(f"Response is missing a valid, consistent boolean {keys[0]}.")
    return values[0]


def require_count(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise BackendContractError(f"Response is missing a nonnegative integer {name}.")
    return value


def require_response_id(data: dict[str, Any], *keys: str) -> str:
    """Accept numeric and opaque IDs, but never silently turn missing IDs into success."""
    for key in keys:
        value = data.get(key)
        if type(value) is int and value > 0:
            return str(value)
        if isinstance(value, str) and value.strip() and value.strip().lower() not in {"null", "none"}:
            value = value.strip()
            if value.lstrip("+-").isdecimal() and int(value) <= 0:
                continue
            return value
    raise BackendContractError(f"Response is missing a valid {keys[0]}.")
