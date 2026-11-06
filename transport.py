from __future__ import annotations

from collections import deque
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
import json
import math
import time

import anyio
from mcp.server.transport_security import TransportSecurityMiddleware, TransportSecuritySettings
from mcp.types import INVALID_PARAMS, JSONRPCMessage
from pydantic import ValidationError
from starlette._utils import get_route_path
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


@dataclass
class RequestLifetime:
    cancel: Callable[[], None] | None = None
    deadline: float = math.inf


current_request: ContextVar[RequestLifetime | None] = ContextVar("mcp_request_lifetime", default=None)


class RequestLimitMiddleware:
    """Reject overload before reading bodies or allocating SDK/client resources."""

    def __init__(self, app: ASGIApp, *, max_concurrent: int, request_limit: int,
                 window_seconds: int, timeout_seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.app = app
        self.max_concurrent = max_concurrent
        self.request_limit = request_limit
        self.window_seconds = window_seconds
        self.timeout_seconds = timeout_seconds
        self.clock = clock
        self.active = 0
        self.started: deque[float] = deque()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        now = self.clock()
        while self.started and self.started[0] <= now - self.window_seconds:
            self.started.popleft()
        if self.active >= self.max_concurrent or len(self.started) >= self.request_limit:
            retry_after = 1 if len(self.started) < self.request_limit else max(1, math.ceil(self.started[0] + self.window_seconds - now))
            await JSONResponse({"error": "mcp_request_limit"}, status_code=429,
                               headers={"Retry-After": str(retry_after)})(scope, receive, send)
            return

        # No await between checking limits and reserving capacity.
        self.active += 1
        self.started.append(now)
        lifetime = RequestLifetime()
        token = current_request.set(lifetime)
        response_started = False
        response_complete = False

        async def track_send(message: Message) -> None:
            nonlocal response_started, response_complete
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                response_complete = True

        try:
            with anyio.move_on_after(self.timeout_seconds) as deadline:
                # SDK tasks run in its application task group, outside this
                # cancel scope. ContextVar inheritance carries the absolute
                # deadline, including body-read time, into the SDK lifespan.
                lifetime.deadline = anyio.current_effective_deadline()
                await self.app(scope, receive, track_send)
            if deadline.cancel_called:
                if lifetime.cancel is not None:
                    lifetime.cancel()
                # A slow reader must not indefinitely hold an admission slot
                # while we send the timeout or finish an existing response.
                with anyio.move_on_after(1):
                    if not response_started:
                        await JSONResponse({"error": "mcp_request_timeout", "message": "Request timed out; inspect activity before retrying a write."}, status_code=504)(scope, receive, send)
                    elif not response_complete:
                        await send({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            # SDK server tasks live in its application task group. Explicitly
            # cancel this request's lifespan even on disconnect or timeout.
            if lifetime.cancel is not None:
                lifetime.cancel()
            current_request.reset(token)
            self.active -= 1


class RequestSizeLimitMiddleware:
    """Bound HTTP bodies before MCP parses JSON, including chunked requests."""

    def __init__(self, app: ASGIApp, max_body_bytes: int, *, rpc_path: str | None = None,
                 transport_security: TransportSecuritySettings | None = None) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.rpc_path = rpc_path
        self.rpc_security = TransportSecurityMiddleware(transport_security)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH"}:
            await self.app(scope, receive, send)
            return
        for key, value in scope.get("headers", []):
            if key.lower() == b"content-length":
                try:
                    length = int(value)
                    if length < 0:
                        raise ValueError
                except ValueError:
                    await JSONResponse({"error": "invalid_content_length"}, status_code=400)(scope, receive, send)
                    return
                if length > self.max_body_bytes:
                    await self._too_large(scope, receive, send)
                    return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > self.max_body_bytes:
                await self._too_large(scope, receive, send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break

        if await self._reject_invalid_rpc_envelope(scope, body, receive, send):
            return

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)

    async def _reject_invalid_rpc_envelope(
        self, scope: Scope, body: bytearray, receive: Receive, send: Send,
    ) -> bool:
        # Match the router's handling of mounted apps and ASGI root_path.
        if scope["method"] != "POST" or self.rpc_path is None or get_route_path(scope) != self.rpc_path:
            return False
        try:
            raw_message = json.loads(body)
        except (ValueError, UnicodeError, RecursionError):
            # Keep the SDK's existing JSON syntax/encoding error behavior.
            return False
        try:
            JSONRPCMessage.model_validate(raw_message)
        except ValidationError:
            pass
        else:
            return False

        # The SDK checks security and media headers before validating its body.
        # Reuse its security policy so malformed envelopes do not mask those
        # responses; this adapter applies to our JSON-response endpoint only.
        request = Request(scope)
        security_error = await self.rpc_security.validate_request(request, is_post=True)
        if security_error is not None:
            await security_error(scope, receive, send)
            return True
        accept_types = (part.strip() for part in request.headers.get("accept", "").split(","))
        if not any(part.startswith("application/json") for part in accept_types):
            return False
        content_types = (part.strip() for part in request.headers.get("content-type", "").split(";")[0].split(","))
        if "application/json" not in content_types:
            return False

        # Never stringify the exception: union validation errors can embed
        # credentials anywhere in the input, including nested params.
        await JSONResponse({
            "jsonrpc": "2.0", "id": "server-error",
            "error": {"code": INVALID_PARAMS, "message": "Invalid JSON-RPC message."},
        }, status_code=400)(scope, receive, send)
        return True

    async def _too_large(self, scope: Scope, receive: Receive, send: Send) -> None:
        await JSONResponse({"error": "request_too_large"}, status_code=413)(scope, receive, send)
