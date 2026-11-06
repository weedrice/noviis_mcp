from __future__ import annotations

import time
import unittest
from unittest.mock import patch

import anyio
import httpx

import test_mcp_sessions as sessions


class RequestDeadlinePropagationTest(unittest.TestCase):
    def test_slow_body_consumes_same_budget_seen_by_sdk_read_retry(self):
        fixture = sessions.MCPSessionTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        headers = fixture.session()
        limiter = fixture.http.app.middleware_stack.app
        limiter.timeout_seconds = 2
        body_limiter = limiter.app

        async def slow_body(scope, receive, send):
            first = True

            async def delayed_receive():
                nonlocal first
                if first:
                    first = False
                    await anyio.sleep(1.6)
                return await receive()

            await body_limiter(scope, delayed_receive, send)

        limiter.app = slow_body
        remaining = []

        async def unavailable(request):
            remaining.append(anyio.current_effective_deadline() - anyio.current_time())
            return httpx.Response(503, json={"message": "synthetic unavailable"})

        fixture.backend = unavailable
        started = time.monotonic()
        with patch("client.MCP_REQUEST_TIMEOUT_SECONDS", 2), patch("client.SERVER_ERROR_WAIT", 0.5):
            response = fixture.http.post("/mcp", headers=headers, json={
                "jsonrpc": "2.0", "id": 999, "method": "tools/call",
                "params": {"name": "get_agent_status", "arguments": {"agent_token": "valid"}},
            })
        self.assertEqual(response.status_code, 200)
        result = response.json()["result"]
        self.assertTrue(result["isError"])
        self.assertIn("insufficient request deadline budget", result["content"][0]["text"])
        self.assertEqual(len(remaining), 1)
        self.assertGreater(remaining[0], 0)
        self.assertLess(remaining[0], 0.6)
        self.assertLess(time.monotonic() - started, 2)
        self.assertTrue(fixture.client_closed[-1].wait(timeout=5))
        self.assertTrue(fixture.clients[-1].is_closed)
        self.assertEqual(limiter.active, 0)


if __name__ == "__main__":
    unittest.main()
