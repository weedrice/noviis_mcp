from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tools.parsing import optional_int_from_float as _optional_int


@dataclass
class RateLimitInfo:
    limit: int | None = None
    remaining: int | None = None
    reset: int | None = None
    retry_after: int | None = None


def build_rate_limit_info(payload: dict[str, Any]) -> RateLimitInfo | None:
    value = payload.get("_rate_limit")
    if not isinstance(value, dict):
        return None
    if not any(value.get(key) is not None for key in ("limit", "remaining", "reset", "retry_after")):
        return None
    return RateLimitInfo(
        limit=_optional_int(value.get("limit")),
        remaining=_optional_int(value.get("remaining")),
        reset=_optional_int(value.get("reset")),
        retry_after=_optional_int(value.get("retry_after")),
    )
