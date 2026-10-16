from __future__ import annotations

from contextlib import ExitStack
import logging
from threading import Event
import unittest
from unittest.mock import patch

import httpx
from starlette.testclient import TestClient

from challenge import ChallengeManager
from client import NoviIsClient

# Importing the entry point must not install file logging in the test process.
with patch("logging_utils.configure_logging"), patch("mcp.server.fastmcp.server.configure_logging"):
    import main


class MCPSessionTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.callback(logging.disable, logging.root.manager.disable)
        logging.disable(logging.CRITICAL)
        self.now = 1000.0
        self.managers = []
        self.clients = []
        self.client_closed = []
        self.writes = []
        self.status_calls = 0
        self.statuses = {"valid": "active", "other": "claimed", "third": "active"}
        self.malformed_write = False
        self.counter = 0

        def manager():
            instance = ChallengeManager(clock=lambda: self.now)
            self.managers.append(instance)
            return instance

        def client():
            closed = Event()

            class TrackedClient(NoviIsClient):
                async def aclose(self):
                    await super().aclose()
                    closed.set()

            http = httpx.AsyncClient(base_url="https://noviis.test/api/v1", transport=httpx.MockTransport(self.backend))
            instance = TrackedClient(client=http, internal_secret="test-secret")
            instance._owns_client = True
            self.clients.append(http)
            self.client_closed.append(closed)
            return instance

        self.stack.enter_context(patch.object(main, "ChallengeManager", manager))
        self.stack.enter_context(patch.object(main, "NoviIsClient", client))
        self.stack.enter_context(patch.object(main, "LOG_LEVEL", "ERROR"))
        self.stack.enter_context(patch.object(main, "MCP_SERVER_HOST", "127.0.0.1"))
        self.stack.enter_context(patch("challenge._random_expression", return_value=(1, 1, "add")))
        self.stack.enter_context(patch.multiple(
            "challenge", CHALLENGE_MAX_PENDING=3, CHALLENGE_MAX_TOTAL_PENDING=1000,
            CHALLENGE_MAX_RECORDS=10000, CHALLENGE_ISSUE_LIMIT=10,
            CHALLENGE_FAIL_LIMIT=3, CHALLENGE_SUSPEND_SECONDS=60,
        ))
        self.http = self.stack.enter_context(TestClient(main.create_app(), base_url="http://127.0.0.1:8001"))

    def backend(self, request):
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if token not in self.statuses:
            return httpx.Response(401, json={"message": "invalid agent"})
        if request.url.path.endswith("/status"):
            self.status_calls += 1
            return httpx.Response(200, json={"data": {"status": self.statuses[token]}})
        if request.url.path.endswith("/home"):
            return httpx.Response(200, json={"data": {"hard_constraints": {"can_create_post": True}}})
        self.assertEqual(request.method, "POST", request.url.path)
        self.writes.append(request.url.path)
        if self.malformed_write:
            return httpx.Response(200, text="<html>unexpected proxy page</html>")
        return httpx.Response(201, json={"data": {"postId": 1, "commentId": 2, "noteId": 3, "noteThreadId": 4}})

    def session(self):
        headers = {"Accept": "application/json, text/event-stream"}
        response = self.http.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "regression-test", "version": "1"}},
        })
        self.assertEqual(response.status_code, 200, response.text)
        headers.update({"mcp-session-id": response.headers["mcp-session-id"], "mcp-protocol-version": "2025-03-26"})
        initialized = self.http.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(initialized.status_code, 202)
        return headers

    def call(self, session, tool="create_comment", token="valid", **extra):
        self.counter += 1
        target = {
            "create_post": {"title": "sample", "board_url": "normal"},
            "create_comment": {"post_id": "1"},
            "create_reply": {"comment_id": "2"},
            "send_note": {"recipient_agent_name": "recipient"},
        }[tool]
        response = self.http.post("/mcp", headers=session, json={
            "jsonrpc": "2.0", "id": self.counter, "method": "tools/call",
            "params": {"name": tool, "arguments": {"agent_token": token, "content": "sample", **target, **extra}},
        })
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["result"]

    def structured(self, *args, **kwargs):
        result = self.call(*args, **kwargs)
        self.assertFalse(result.get("isError"), result)
        return result["structuredContent"]

    def test_pending_limit_is_shared_across_sessions_and_tools(self):
        first, second = self.session(), self.session()
        for tool in ("create_post", "create_reply", "send_note"):
            self.assertEqual(self.structured(first, tool)["status"], "challenge_required")
        limited = self.structured(second)
        self.assertEqual(limited["error"], "challenge_pending_limit")
        self.assertEqual(self.structured(second, token="other")["status"], "challenge_required")
        self.assertEqual(len(self.managers), 1)
        self.assertEqual(self.writes, [])

    def test_challenge_survives_session_close_and_cannot_be_replayed(self):
        first = self.session()
        issued = self.structured(first)
        deleted = self.http.delete("/mcp", headers=first)
        self.assertEqual(deleted.status_code, 200)
        second = self.session()
        answer = {"challenge_id": issued["challenge"]["challenge_id"], "answer": "2.00"}
        self.assertEqual(self.structured(second, **answer)["status"], "created")
        self.assertEqual(self.structured(second, **answer)["error"], "challenge_used")
        self.assertEqual(len(self.writes), 1)

    def test_issuance_window_is_shared_after_successful_writes(self):
        with patch("challenge.CHALLENGE_ISSUE_LIMIT", 2):
            for _ in range(2):
                session = self.session()
                issued = self.structured(session)
                completed = self.structured(session, challenge_id=issued["challenge"]["challenge_id"], answer="2.00")
                self.assertEqual(completed["status"], "created")
            self.assertEqual(self.structured(self.session())["error"], "challenge_issue_limit")
            self.assertEqual(len(self.writes), 2)

    def test_suspension_is_shared_and_expires_without_reconnecting(self):
        first, second = self.session(), self.session()
        issued = self.structured(first)
        for _ in range(3):
            issued = self.structured(first, challenge_id=issued["challenge"]["challenge_id"], answer="0")
        self.assertEqual(issued["status"], "challenge_suspended")
        self.assertEqual(self.structured(second)["status"], "challenge_suspended")
        self.now += 60
        self.assertEqual(self.structured(second)["status"], "challenge_required")

    def test_invalid_and_inactive_tokens_allocate_no_state(self):
        session = self.session()
        self.statuses["pending"] = "pending"
        self.statuses["malformed"] = None
        for token in ("invalid", "pending", "malformed"):
            for tool in ("create_post", "create_comment", "create_reply", "send_note"):
                with self.subTest(token=token, tool=tool):
                    self.assertTrue(self.call(session, tool, token=token)["isError"])
        self.assertEqual(self.managers[0]._challenges, {})
        self.assertEqual(self.managers[0]._owners, {})
        self.assertEqual(self.writes, [])

    def test_revoked_token_does_not_consume_existing_challenge(self):
        session = self.session()
        issued = self.structured(session)
        answer = {"challenge_id": issued["challenge"]["challenge_id"], "answer": "2.00"}
        del self.statuses["valid"]
        self.assertTrue(self.call(session, **answer)["isError"])
        self.assertEqual(self.writes, [])
        self.statuses["valid"] = "active"
        self.assertEqual(self.structured(session, **answer)["status"], "created")

    def test_global_pending_limit_applies_to_distinct_authenticated_agents(self):
        with patch("challenge.CHALLENGE_MAX_TOTAL_PENDING", 2):
            session = self.session()
            self.structured(session, token="valid")
            self.structured(session, token="other")
            limited = self.structured(self.session(), token="third")
            self.assertEqual(limited["error"], "challenge_global_pending_limit")
            self.assertEqual(len(self.managers[0]._challenges), 2)

    def test_large_content_is_rejected_before_authentication_or_allocation(self):
        with patch("tools.write_safety.WRITE_MAX_CONTENT_CHARS", 10):
            self.assertTrue(self.call(self.session(), content="x" * 11)["isError"])
        self.assertEqual(self.status_calls, 0)
        self.assertEqual(self.managers[0]._challenges, {})

    def test_malformed_backend_write_response_is_not_success(self):
        session = self.session()
        issued = self.structured(session)
        self.malformed_write = True
        result = self.call(session, challenge_id=issued["challenge"]["challenge_id"], answer="2.00")
        self.assertTrue(result["isError"])
        self.assertIn("backend_contract_error", result["content"][0]["text"])
        self.assertEqual(len(self.writes), 1)

    def test_new_application_has_independent_state_and_session_clients_close(self):
        session = self.session()
        self.structured(session)
        self.http.delete("/mcp", headers=session)
        # DELETE acknowledges termination before the session task finishes cleanup.
        self.assertTrue(self.client_closed[0].wait(timeout=5))
        self.assertTrue(self.clients[0].is_closed)
        main.create_mcp_server()
        self.assertEqual(len(self.managers), 2)
        self.assertEqual(self.managers[1]._challenges, {})


if __name__ == "__main__":
    unittest.main()
