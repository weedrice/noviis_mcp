from __future__ import annotations


class NoviIsMCPError(Exception):
    """Base exception for NoviIs MCP server errors."""


class Unauthorized(NoviIsMCPError):
    """Raised when the provided agent token is invalid or inactive."""


class AgentSuspended(NoviIsMCPError):
    """Raised when the agent is suspended."""


class PermissionDenied(NoviIsMCPError):
    """Raised when the API rejects the request for permission reasons."""


class RateLimited(NoviIsMCPError):
    """Raised when the API rate-limits the request."""

    def __init__(self, retry_after: int | None = None, message: str = "Rate limit exceeded") -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ServerError(NoviIsMCPError):
    """Raised when the upstream server returns a server error."""


class MaxRetryExceeded(NoviIsMCPError):
    """Raised when retry attempts are exhausted."""


class ActivityLimitExceeded(NoviIsMCPError):
    """Raised when daily activity limits are exceeded."""

    def __init__(self, reset_at: str, message: str = "Daily activity limit exceeded") -> None:
        super().__init__(message)
        self.reset_at = reset_at


class DuplicateInstanceError(NoviIsMCPError):
    """Raised when another MCP server instance already holds the lock."""
