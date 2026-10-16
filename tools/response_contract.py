from __future__ import annotations

from typing import Any

from exceptions import BackendContractError


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
