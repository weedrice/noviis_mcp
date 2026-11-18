"""Exercise the new protocol over HTTP alongside the existing legacy suite."""
from __future__ import annotations

import unittest
from unittest.mock import patch

import anyio
import httpx
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

import test_mcp_sessions as sessions
from client import NoviIsClient


MODERN_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": "2026-07-28",
}


class ModernProtocolTest(unittest.TestCase):
    def setUp(self):
        self.fixture = sessions.MCPSessionTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_discovery_and_all_tool_schemas_match_legacy_without_initialize(self):
        fixture = self.fixture
        self.assertEqual(fixture.clients, [])
        discover = fixture.rpc(MODERN_HEADERS, "server/discover", {})
        self.assertIn("2026-07-28", discover["supportedVersions"])
        self.assertIn("tools", discover["capabilities"])
        modern = fixture.rpc(MODERN_HEADERS, "tools/list", {})
        legacy = fixture.rpc(fixture.session(), "tools/list", {})
        self.assertEqual(len(modern["tools"]), 26)
        self.assertEqual(modern["tools"], legacy["tools"])
        self.assertEqual(fixture.backend_requests, [])
        self.assertGreater(len(fixture.clients), 1)
        self.assertEqual(len({id(client) for client in fixture.clients}), len(fixture.clients))
        for closed, client in zip(fixture.client_closed, fixture.clients):
            self.assertTrue(closed.wait(timeout=5))
            self.assertTrue(client.is_closed)

    def test_bearer_authentication_is_request_local_and_missing_auth_is_actionable(self):
        fixture = self.fixture
        for token in ("valid", "other"):
            result = fixture.rpc({**MODERN_HEADERS, "Authorization": f"Bearer {token}"},
                                 "tools/call", {"name": "get_agent_status", "arguments": {}})
            self.assertFalse(result.get("isError"))
            self.assertEqual(result["structuredContent"]["name"], token)
        result = fixture.rpc(MODERN_HEADERS, "tools/call", {"name": "get_agent_status", "arguments": {}})
        self.assertTrue(result["isError"])
        self.assertIn("Authentication required", result["content"][0]["text"])
        self.assertEqual([token for _, token in fixture.backend_requests], ["valid", "other"])

    def test_challenges_cross_protocols_and_cannot_replay(self):
        fixture = self.fixture
        issued = fixture.structured(MODERN_HEADERS)
        answer = {"challenge_id": issued["challenge"]["challenge_id"], "answer": "2.00"}
        self.assertEqual(fixture.structured(fixture.session(), **answer)["status"], "created")
        self.assertEqual(fixture.structured(MODERN_HEADERS, **answer)["error"], "challenge_used")
        self.assertEqual(len(fixture.writes), 1)

    def test_modern_timeout_closes_client_and_blocks_write_replay(self):
        self.fixture.session = lambda: dict(MODERN_HEADERS)
        self.fixture.test_timeout_closes_backend_client_and_consumed_challenge_cannot_replay()

    def test_unexpected_value_error_does_not_become_public_tool_guidance(self):
        def broken_backend(request):
            raise ValueError("opaque-private-crash-value")

        self.fixture.backend = broken_backend
        result = self.fixture.rpc(MODERN_HEADERS, "tools/call", {
            "name": "get_agent_status", "arguments": {"agent_token": "valid"}})
        self.assertTrue(result["isError"])
        self.assertNotIn("opaque-private-crash-value", str(result))
        self.assertIn("Error executing tool get_agent_status", result["content"][0]["text"])


class ModernOfficialClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_modern_official_client_uses_http_and_completes_challenge(self):
        writes = []
        closed = []

        def backend(request):
            if request.url.path.endswith("/status"):
                return httpx.Response(200, json={"data": {"status": "active"}})
            writes.append(request.url.path)
            return httpx.Response(201, json={"data": {"commentId": 321}})

        def backend_client():
            instance = NoviIsClient(client=httpx.AsyncClient(
                base_url="https://mock.invalid/api/v1", transport=httpx.MockTransport(backend)),
                internal_secret="synthetic")
            instance._owns_client = True
            closed.append(instance)
            return instance

        with patch.object(sessions.main, "NoviIsClient", backend_client), \
                patch.object(sessions.main, "MCP_SERVER_HOST", "127.0.0.1"), \
                patch("challenge._random_expression", return_value=(1, 1, "add")):
            app = sessions.main.create_app()
            with anyio.fail_after(10):
                async with app.router.lifespan_context(app):
                    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app)) as http:
                        transport = streamable_http_client("http://127.0.0.1:8001/mcp", http_client=http)
                        async with Client(transport, mode="2026-07-28") as client:
                            self.assertEqual(client.protocol_version, "2026-07-28")
                            self.assertEqual(len((await client.list_tools()).tools), 26)
                            args = {"agent_token": "synthetic", "post_id": "1", "content": "sample" * 10}
                            issued = await client.call_tool("create_comment", args)
                            args.update(challenge_id=issued.structured_content["challenge"]["challenge_id"], answer="2.00")
                            created = await client.call_tool("create_comment", args)
                            self.assertEqual(created.structured_content["comment_id"], "321")
                            replay = await client.call_tool("create_comment", args)
                            self.assertEqual(replay.structured_content["error"], "challenge_used")
        self.assertEqual(len(writes), 1)
        self.assertTrue(closed)
        self.assertTrue(all(client._client.is_closed for client in closed))
