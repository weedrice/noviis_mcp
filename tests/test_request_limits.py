from __future__ import annotations

import asyncio
import unittest

from transport import RequestLimitMiddleware, RequestSizeLimitMiddleware, current_request


class RequestLimitTest(unittest.IsolatedAsyncioTestCase):
    def middleware(self, app, **overrides):
        return RequestLimitMiddleware(app, **dict(max_concurrent=2, request_limit=10,
                                      window_seconds=60, timeout_seconds=5, **overrides))

    async def invoke(self, app, receive=None):
        sent = []
        async def default_receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        async def send(message):
            sent.append(message)
        await app({"type": "http", "method": "POST", "headers": []}, receive or default_receive, send)
        return sent

    async def test_concurrency_rejects_without_queueing_and_releases_after_success(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        count = 0
        async def app(scope, receive, send):
            nonlocal count
            count += 1
            if count == 2:
                entered.set()
            await release.wait()
            await send({"type": "http.response.start", "status": 200, "headers": []})
        middleware = self.middleware(app)
        first = asyncio.create_task(self.invoke(middleware))
        second = asyncio.create_task(self.invoke(middleware))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            rejected = await self.invoke(middleware)
            self.assertEqual(rejected[0]["status"], 429)
            self.assertEqual(count, 2)
        finally:
            release.set()
            await asyncio.gather(first, second)
        self.assertEqual(middleware.active, 0)
        self.assertEqual((await self.invoke(middleware))[0]["status"], 200)

    async def test_rolling_rate_window_is_bounded_and_expires(self):
        now = 1000.0
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
        middleware = RequestLimitMiddleware(app, max_concurrent=2, request_limit=2,
                                            window_seconds=60, timeout_seconds=5, clock=lambda: now)
        await self.invoke(middleware)
        await self.invoke(middleware)
        for _ in range(10):
            self.assertEqual((await self.invoke(middleware))[0]["status"], 429)
        self.assertEqual(len(middleware.started), 2)
        now += 60
        self.assertEqual((await self.invoke(middleware))[0]["status"], 200)
        self.assertEqual(len(middleware.started), 1)

    async def test_deadline_covers_slow_body_before_sdk_dispatch(self):
        async def never_called(scope, receive, send):
            self.fail("Slow body must not reach SDK")
        async def slow_receive():
            await asyncio.Event().wait()
        middleware = RequestLimitMiddleware(RequestSizeLimitMiddleware(never_called, 10), max_concurrent=1,
                                            request_limit=10, window_seconds=60, timeout_seconds=0.01)
        self.assertEqual((await self.invoke(middleware, slow_receive))[0]["status"], 504)
        self.assertEqual(middleware.active, 0)

    async def test_timeout_finishes_started_response_without_second_headers(self):
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"partial", "more_body": True})
            await asyncio.Event().wait()
        middleware = RequestLimitMiddleware(app, max_concurrent=1, request_limit=10,
                                            window_seconds=60, timeout_seconds=0.01)
        messages = await self.invoke(middleware)
        self.assertEqual(sum(item["type"] == "http.response.start" for item in messages), 1)
        self.assertFalse(messages[-1]["more_body"])
        self.assertEqual(middleware.active, 0)

    async def test_exception_and_external_cancellation_release_request_lifetime(self):
        cancellations = []
        async def broken(scope, receive, send):
            current_request.get().cancel = lambda: cancellations.append("closed")
            raise RuntimeError("disconnect")
        middleware = self.middleware(broken)
        with self.assertRaises(RuntimeError):
            await self.invoke(middleware)
        self.assertEqual(middleware.active, 0)
        self.assertEqual(cancellations, ["closed"])
        self.assertIsNone(current_request.get())

        entered = asyncio.Event()
        async def waiting(scope, receive, send):
            current_request.get().cancel = lambda: cancellations.append("cancelled")
            entered.set()
            await asyncio.Event().wait()
        middleware = self.middleware(waiting)
        task = asyncio.create_task(self.invoke(middleware))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(middleware.active, 0)
        self.assertEqual(cancellations, ["closed", "cancelled"])
