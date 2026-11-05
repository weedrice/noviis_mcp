"""One recovery contract for expected backend write rejections."""
from __future__ import annotations

from typing import Any

from exceptions import NoviIsAPIError


def write_error_fields(exc: NoviIsAPIError, *, operation_id: str | None = None) -> dict[str, Any]:
    """Preserve rejection metadata without converting unknown outcomes to rejection.

    Callers catch only NoviIsAPIError. Transport failures, HTTP 5xx and malformed
    success responses must still propagate so a committed write is not declared
    safely retryable. A 429 requires waiting and a fresh challenge, not replaying
    the already-consumed challenge.
    """
    if not 400 <= exc.status_code < 500:
        raise exc
    details = exc.details

    def optional_text(snake: str, camel: str) -> str | None:
        value = details.get(snake, details.get(camel))
        return value if isinstance(value, str) else None

    def mapping(snake: str, camel: str | None = None) -> dict[str, Any]:
        value = details.get(snake, details.get(camel) if camel else None)
        return dict(value) if isinstance(value, dict) else {}

    return {
        "status": "blocked",
        "http_status": exc.status_code,
        "error": exc.code,
        "message": str(exc),
        "retryable": exc.status_code == 429,
        "operation_id": operation_id,
        "retry_after_seconds": exc.retry_after_seconds,
        "reset_at": optional_text("reset_at", "resetAt"),
        "next_allowed_at": optional_text("next_allowed_at", "nextAllowedAt"),
        "limits": mapping("limits"),
        "restrictions": mapping("restrictions"),
        "hard_constraints": mapping("hard_constraints", "hardConstraints"),
    }
