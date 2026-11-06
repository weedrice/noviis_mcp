"""Strict policy fields shared by status and home responses."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from exceptions import BackendContractError


def policy_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BackendContractError(f"{label} must be an object.")
    return value


def policy_field(payload: dict[str, Any], name: str, expected: type, *,
                 label: str, required: bool = True, nullable: bool = False,
                 aliases: tuple[str, ...] = (), timestamp: bool = False) -> Any:
    first, *rest = name.split("_")
    camel = first + "".join(part.title() for part in rest)
    keys = dict.fromkeys((name, camel, *aliases))
    present = [payload[key] for key in keys if key in payload]
    if not present:
        if not required:
            return None
        raise BackendContractError(f"{label}.{name} is missing.")
    for item in present:
        if item is None and nullable:
            continue
        valid = type(item) is expected
        if valid and expected is int:
            valid = 0 <= item <= 9223372036854775807
        if valid and expected is str and (required or timestamp):
            valid = bool(item.strip())
        if valid and timestamp:
            try:
                valid = datetime.fromisoformat(item.replace("Z", "+00:00")).utcoffset() is not None
            except ValueError:
                valid = False
        if not valid:
            raise BackendContractError(f"{label}.{name} is invalid.")
    if any(type(item) is not type(present[0]) or item != present[0] for item in present[1:]):
        raise BackendContractError(f"{label}.{name} has conflicting aliases.")
    return present[0]


def validate_policy_permission(*, label: str, suspended: bool,
                               allowed: bool | None, remaining: int | None) -> None:
    if allowed is True and (suspended or remaining == 0):
        raise BackendContractError(f"{label} conflicts with suspension or remaining quota.")


def validate_policy_quota(maximum: int | None, remaining: int | None, label: str) -> None:
    if maximum is not None and remaining is not None and remaining > maximum:
        raise BackendContractError(f"{label} exceeds the daily limit.")
