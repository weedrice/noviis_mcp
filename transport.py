from __future__ import annotations

from collections import deque
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
import math
import time

import anyio
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


@dataclass
class RequestLifetime:
    cancel: Callable[[], None] | None = None


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

    def __init__(self, app: ASGIApp, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

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

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)

    async def _too_large(self, scope: Scope, receive: Receive, send: Send) -> None:
        await JSONResponse({"error": "request_too_large"}, status_code=413)(scope, receive, send)
