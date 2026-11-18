import asyncio
import io
import json
import logging
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from client import NoviIsClient
from logging_utils import JsonFormatter, PlainFormatter, SensitiveDataFilter, request_log_context
from transport import RequestLimitMiddleware


class RequestObservabilityTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.handler = logging.StreamHandler(self.output)
        self.handler.setFormatter(JsonFormatter())
        self.handler.addFilter(SensitiveDataFilter())
        root = logging.getLogger()
        self.old_level = root.level
        old_handlers = root.handlers[:]
        root.setLevel(logging.INFO)
        root.handlers = [self.handler]
        self.addCleanup(setattr, root, "handlers", old_handlers)
        self.addCleanup(root.setLevel, self.old_level)

    def events(self, name):
        return [row for line in self.output.getvalue().splitlines()
                if (row := json.loads(line))["message"] == name]

    def middleware(self, app, **kwargs):
        return RequestLimitMiddleware(app, **{"max_concurrent": 4, "request_limit": 100,
            "window_seconds": 60, "timeout_seconds": 2, **kwargs})

    async def invoke(self, app, query="sample"):
        messages = []
        async def receive():
            return {"type": "http.disconnect"}
        async def send(message):
            messages.append(message)
        await app({"type": "http", "path": "/mcp", "method": "POST", "probe_query": query,
                   "headers": [(b"x-request-id", b"untrusted_client_id")],
                   "query_string": b"q=private_incoming_query"}, receive, send)
        return messages

    async def respond(self, send, status=200):
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def test_concurrent_child_tasks_keep_distinct_ids_through_backend_and_response(self):
        arrivals = []
        both = asyncio.Event()
        async def backend(request):
            arrivals.append((request.url.params["q"], request.headers["X-Request-ID"]))
            if len(arrivals) == 2:
                both.set()
            await both.wait()
            return httpx.Response(200, json={"data": {"content": []}})

        async with httpx.AsyncClient(base_url="https://mock.invalid", transport=httpx.MockTransport(backend)) as http:
            client = NoviIsClient(client=http, internal_secret="synthetic")
            async def app(scope, receive, send):
                await asyncio.create_task(client.search_semantic(query=scope["probe_query"]))
                await self.respond(send)
            middleware = self.middleware(app)
            replies = await asyncio.wait_for(asyncio.gather(self.invoke(middleware, "private_one"),
                                                            self.invoke(middleware, "private_two")), 3)
        ids = [dict(reply[0]["headers"])[b"x-request-id"].decode() for reply in replies]
        self.assertEqual(len(set(ids)), 2)
        self.assertEqual(dict(arrivals), dict(zip(("private_one", "private_two"), ids)))
        for name in ("mcp_request_complete", "noviis_api_complete", "httpx_request"):
            self.assertEqual({row["request_id"] for row in self.events(name)}, set(ids))
        for row in self.events("mcp_request_complete") + self.events("noviis_api_complete"):
            self.assertEqual(row["outcome"], "success")
            self.assertGreaterEqual(row["duration_ms"], 0)
        self.assertNotIn("private_", self.output.getvalue())
        self.assertNotIn("untrusted_client_id", self.output.getvalue())
        self.assertIsNone(request_log_context.get())

    async def test_error_echoes_are_redacted_after_client_returns_in_both_formats(self):
        for formatter in (JsonFormatter(), PlainFormatter("%(message)s")):
            self.output.seek(0)
            self.output.truncate(0)
            self.handler.setFormatter(formatter)
            async with httpx.AsyncClient(base_url="https://mock.invalid", transport=httpx.MockTransport(
                lambda request: httpx.Response(400, json={"code": "invalid", "message": request.url.params["q"]}))) as http:
                client = NoviIsClient(client=http, internal_secret="synthetic")
                async def app(scope, receive, send):
                    try:
                        await client.search_semantic(query="private_error_marker")
                    except Exception:
                        logging.getLogger("sdk-error-test").exception("Tool failed")
                    await self.respond(send)
                await self.invoke(self.middleware(app))
            self.assertNotIn("private_error_marker", self.output.getvalue())
            self.assertIn("Tool failed", self.output.getvalue())
            self.assertIsNone(request_log_context.get())

    async def test_terminal_outcomes_and_rejections_always_release_context(self):
        async def ok(scope, receive, send):
            await self.respond(send)
        middleware = self.middleware(ok, request_limit=1)
        await self.invoke(middleware)
        self.assertEqual((await self.invoke(middleware))[0]["status"], 429)
        self.assertEqual(self.events("mcp_request_complete")[-1]["outcome"], "overloaded")

        async def slow(scope, receive, send):
            await asyncio.Event().wait()
        self.assertEqual((await self.invoke(self.middleware(slow, timeout_seconds=0.01)))[0]["status"], 504)
        self.assertEqual(self.events("mcp_request_complete")[-1]["outcome"], "timeout")

        async def disconnected(scope, receive, send):
            await receive()
        await self.invoke(self.middleware(disconnected))
        self.assertEqual(self.events("mcp_request_complete")[-1]["outcome"], "disconnected")

        async def broken(scope, receive, send):
            raise RuntimeError("failure")
        with self.assertRaises(RuntimeError):
            await self.invoke(self.middleware(broken))
        self.assertEqual(self.events("mcp_request_complete")[-1]["outcome"], "error")

        entered = asyncio.Event()
        async def cancelled(scope, receive, send):
            entered.set()
            await asyncio.Event().wait()
        task = asyncio.create_task(self.invoke(self.middleware(cancelled)))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.events("mcp_request_complete")[-1]["outcome"], "cancelled")
        self.assertIsNone(request_log_context.get())

    async def test_backend_retry_and_error_classification(self):
        requests = []
        def backend(request):
            requests.append(request)
            return httpx.Response(503 if len(requests) == 1 else 200, json={"data": []})
        async with httpx.AsyncClient(base_url="https://mock.invalid", transport=httpx.MockTransport(backend)) as http:
            with patch("client.asyncio.sleep", new_callable=AsyncMock):
                await NoviIsClient(client=http, internal_secret="synthetic").request_json("GET", "/agents/boards")
        complete = self.events("noviis_api_complete")[-1]
        self.assertEqual((complete["attempts"], complete["status_code"], complete["outcome"]), (2, 200, "success"))
        self.assertEqual({request.headers["X-Request-ID"] for request in requests}, {complete["request_id"]})
        for kind in ("http_error", "contract_error", "transport_error", "timeout"):
            def fail(request):
                if kind == "http_error":
                    return httpx.Response(429, json={"message": "limited"})
                if kind == "contract_error":
                    return httpx.Response(200, text="not JSON")
                raise (httpx.ReadTimeout if kind == "timeout" else httpx.ConnectError)("synthetic")
            async with httpx.AsyncClient(base_url="https://mock.invalid", transport=httpx.MockTransport(fail)) as http:
                with self.assertRaises(Exception):
                    await NoviIsClient(client=http, internal_secret="synthetic").request_json("GET", "/agents/boards")
            self.assertEqual(self.events("noviis_api_complete")[-1]["outcome"], kind)
            self.assertIsNone(request_log_context.get())

    async def test_short_query_does_not_corrupt_correlation_or_event_names(self):
        async with httpx.AsyncClient(base_url="https://mock.invalid", transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": []}))) as http:
            await NoviIsClient(client=http, internal_secret="synthetic").search_semantic(query="a")
        row = self.events("noviis_api_complete")[-1]
        self.assertRegex(row["request_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(row["outcome"], "success")


class RequestProtocolObservabilityTest(unittest.TestCase):
    def test_real_mcp_calls_preserve_trace_and_hide_search_error_logs(self):
        import test_mcp_sessions as sessions

        fixture = sessions.MCPSessionTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        headers = fixture.session()
        backend_ids = []
        original = fixture.backend

        def backend(request):
            backend_ids.append(request.headers.get("X-Request-ID"))
            if request.url.params.get("q") == "private_mcp_error":
                return httpx.Response(400, json={"code": "invalid", "message": "private_mcp_error"})
            return original(request)

        fixture.backend = backend
        for formatter in (JsonFormatter(), PlainFormatter("%(levelname)s %(message)s")):
            output = io.StringIO()
            handler = logging.StreamHandler(output)
            handler.setFormatter(formatter)
            handler.addFilter(SensitiveDataFilter())
            previous_disable = logging.root.manager.disable
            with patch.object(logging.getLogger(), "handlers", [handler]), patch.object(logging.getLogger(), "level", logging.DEBUG):
                logging.disable(logging.NOTSET)
                try:
                    response = fixture.http.post("/mcp", headers={**headers, "X-Request-ID": "untrusted_client_id"}, json={
                        "jsonrpc": "2.0", "id": 10, "method": "tools/call", "params": {
                            "name": "search_content", "arguments": {"query": "private_mcp_error"}}})
                    self.assertTrue(fixture.client_closed[-1].wait(timeout=5))
                finally:
                    logging.disable(previous_disable)
            request_id = response.headers["X-Request-ID"]
            self.assertEqual(request_id, backend_ids[-1])
            self.assertRegex(request_id, r"^[0-9a-f]{32}$")
            self.assertTrue(response.json()["result"]["isError"])
            self.assertNotIn("private_mcp_error", output.getvalue())
            self.assertNotIn("untrusted_client_id", output.getvalue())
            self.assertIn(request_id, output.getvalue())
            self.assertIn("mcp_request_complete", output.getvalue())
            self.assertIn("noviis_api_complete", output.getvalue())
