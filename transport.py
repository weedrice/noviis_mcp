from __future__ import annotations

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


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
