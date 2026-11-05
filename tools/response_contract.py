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


def require_positive_long_id(data: dict[str, Any], *keys: str, nullable: bool = False) -> int | None:
    """Validate JSON identities produced by backend Long DTOs, without coercion."""
    values = [data[key] for key in keys if key in data]
    if nullable and (not values or all(value is None for value in values)):
        return None
    if (not values or any(type(value) is not int or not 0 < value <= 9_223_372_036_854_775_807
                          for value in values) or any(value != values[0] for value in values)):
        raise BackendContractError(f"Response is missing a valid, consistent positive Long {keys[0]}.")
    return values[0]


def require_response_id(data: dict[str, Any], *keys: str) -> str:
    """Keep resource IDs as strings externally, while validating backend Long DTOs."""
    return str(require_positive_long_id(data, *keys))


def require_target_id(data: dict[str, Any], expected: str, *keys: str) -> str:
    returned = require_response_id(data, *keys)
    normalized = str(int(expected)) if expected.isascii() and expected.isdecimal() else expected
    if returned != normalized:
        raise BackendContractError(f"Response {keys[0]} does not match the requested target.")
    return returned


def require_response_count(data: dict[str, Any], *keys: str) -> int:
    values = [require_count(data[key], keys[0]) for key in keys if key in data]
    if not values or any(value != values[0] for value in values):
        raise BackendContractError(f"Response is missing a valid, consistent count {keys[0]}.")
    return values[0]
