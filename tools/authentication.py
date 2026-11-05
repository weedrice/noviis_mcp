"""Resolve per-call credentials without storing an agent identity on the server."""
from __future__ import annotations

import hmac
import re

from mcp.server.fastmcp import Context

from tools.input_validation import validate_tool_inputs


def resolve_agent_token(ctx: Context, agent_token: str | None, *, required: bool = True) -> str | None:
    """Accept legacy tool arguments and the current HTTP request's Bearer token.

    Reading the SDK request directly also works with its stateless task groups;
    credentials never depend on a process-wide default or a previous request.
    """
    validate_tool_inputs({"agent_token": agent_token})
    request = getattr(ctx.request_context, "request", None)
    headers = request.headers.getlist("authorization") if request is not None else []
    header_token = None
    if headers:
        if len(headers) != 1:
            raise ValueError("Send exactly one Authorization header.")
        match = re.fullmatch(r"Bearer +([A-Za-z0-9._~+/-]+=*)", headers[0], re.IGNORECASE | re.ASCII)
        if match is None or len(match[1]) > 4096:
            raise ValueError("Authorization must contain one Bearer agent token of at most 4096 characters.")
        header_token = match[1]
    if header_token is not None and agent_token is not None:
        if not hmac.compare_digest(header_token.encode("utf-8"), agent_token.encode("utf-8")):
            raise ValueError("Authorization and agent_token identify different credentials; supply only one or matching values.")
    resolved = header_token if header_token is not None else agent_token
    if resolved is None and required:
        raise ValueError("Authentication required: configure Authorization: Bearer <agent token> or pass agent_token.")
    return resolved
