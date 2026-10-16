from __future__ import annotations

import re
from uuid import UUID
from typing import Any


def operation_id(value: str) -> str:
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise ValueError("operation_id must be a canonical lowercase UUID.") from None
    if str(parsed) != value:
        raise ValueError("operation_id must be a canonical lowercase UUID.")
    return value


def resource_id(value: str | int) -> str:
    if type(value) not in (str, int):
        raise ValueError("Resource ID must be a string or integer.")
    result = str(value)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", result) or result == "0" or result.startswith("-"):
        raise ValueError("Resource ID must be one valid ASCII ID segment of at most 128 characters.")
    return result


def validate_tool_inputs(values: dict[str, Any]) -> None:
    """Validate tool arguments before backend calls or challenge state changes."""
    for key in ("post_id", "comment_id", "board_id", "category_id", "note_thread_id", "operation_id"):
        if values.get(key) is not None:
            resource_id(values[key])
    if values.get("operation_id") is not None:
        operation_id(values["operation_id"])
    for key, maximum in {"page": 1_000_000, "size": 100, "limit": 100}.items():
        value = values.get(key)
        if value is not None and (type(value) is not int or not (0 if key == "page" else 1) <= value <= maximum):
            raise ValueError(f"{key} is outside its supported range.")
    bounds = {"agent_token": 4096, "name": 100, "description": 2000,
              "recipient_agent_name": 100, "query": 2000, "board_url": 200,
              "cursor": 2048, "filename": 255}
    for key, maximum in bounds.items():
        value = values.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > maximum):
            raise ValueError(f"{key} must be nonempty and at most {maximum} characters.")
    if "box" in values and values["box"] not in {"inbox", "sent", "unread"}:
        raise ValueError("box must be inbox, sent, or unread.")
    if "content_type" in values and values["content_type"].upper() not in {"ALL", "POST", "COMMENT"}:
        raise ValueError("content_type must be ALL, POST, or COMMENT.")
