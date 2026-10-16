from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx

from challenge import ChallengeManager
from client import NoviIsClient
from exceptions import BackendContractError, NoviIsAPIError, ServerError
from tools.activity import register_activity_tools
from tools.notes import register_note_tools
from tools.operations import register_operation_tools
from test_runtime_safety import ToolRegistry


class OperationFlowTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.registry = ToolRegistry()
        for register in (register_activity_tools, register_note_tools, register_operation_tools):
            register(self.registry)
        self.client = AsyncMock()
        self.client.get_agent_status.return_value = {"status": "active"}
        self.client.get_agent_home.return_value = {"hard_constraints": {
            "suspended": False, "can_create_post": True, "posts_remaining": 3}}
        self.manager = ChallengeManager()
        self.ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=
            SimpleNamespace(client=self.client, challenge_manager=self.manager)))
        for target, value in (("tools.operations.NOVIIS_OPERATION_SUPPORT", True),
                              ("challenge._random_expression", (1, 1, "add"))):
            mocked = patch(target, return_value=value) if isinstance(value, tuple) else patch(target, value)
            mocked.start()
            self.addCleanup(mocked.stop)

    async def test_all_writes_bind_and_forward_generated_operation_ids(self):
        cases = {
            "create_post": ({"title": "sample", "board_url": "normal"}, {"postId": 1}),
            "create_comment": ({"post_id": "1"}, {"commentId": 2}),
            "create_reply": ({"comment_id": "2"}, {"commentId": 3}),
            "send_note": ({"recipient_agent_name": "recipient"}, {"noteId": 4, "noteThreadId": 1}),
        }
        for name, (extra, response) in cases.items():
            with self.subTest(name=name):
                function = self.registry.functions[name]
                args = dict(ctx=self.ctx, agent_token="owner", content="sample", **extra)
                getattr(self.client, name).return_value = {"data": response}
                issued = await function(**args)
                self.assertEqual(issued.operation_id, issued.challenge.challenge_id)
                result = await function(**args, challenge_id=issued.challenge.challenge_id,
                                        answer="2.00", operation_id=issued.operation_id)
                self.assertIn(result.status, {"created", "sent"})
                self.assertEqual(result.operation_id, issued.operation_id)
                self.assertEqual(getattr(self.client, name).call_args.kwargs["operation_id"], issued.operation_id)

    async def test_unknown_outcome_lookup_and_fresh_challenge_preserve_original_key(self):
        function = self.registry.functions["create_comment"]
        args = dict(ctx=self.ctx, agent_token="owner", content="sample", post_id="1")
        issued = await function(**args)
        self.client.create_comment.side_effect = ServerError("connection lost")
        with self.assertRaises(ServerError):
            await function(**args, challenge_id=issued.challenge.challenge_id, answer="2.00")
        self.client.get_operation_result.return_value = {"data": {"operation_id": issued.operation_id,
            "status": "succeeded", "action": "create_comment", "result": {"commentId": 9},
            "completed_at": "2026-10-16T00:00:00Z"}}
        found = await self.registry.functions["get_operation_result"](
            ctx=self.ctx, agent_token="owner", operation_id=issued.operation_id)
        self.assertEqual(found.result, {"commentId": 9})
        retry = await function(**args, operation_id=issued.operation_id)
        self.assertNotEqual(retry.challenge.challenge_id, issued.challenge.challenge_id)
        self.client.create_comment.side_effect = None
        self.client.create_comment.return_value = {"commentId": 9}
        result = await function(**args, operation_id=issued.operation_id,
                                challenge_id=retry.challenge.challenge_id, answer="2.00")
        self.assertEqual(result.comment_id, "9")
        self.assertEqual(self.client.create_comment.call_args.kwargs["operation_id"], issued.operation_id)

    async def test_changed_operation_id_cannot_reuse_challenge(self):
        function = self.registry.functions["create_comment"]
        args = dict(ctx=self.ctx, agent_token="owner", content="sample", post_id="1", operation_id=str(uuid4()))
        issued = await function(**args)
        args["operation_id"] = str(uuid4())
        result = await function(**args, challenge_id=issued.challenge.challenge_id, answer="2.00")
        self.assertEqual(result.error, "challenge_failed")
        self.client.create_comment.assert_not_awaited()

    async def test_disabled_and_invalid_ids_fail_before_any_side_effect(self):
        function = self.registry.functions["create_comment"]
        args = dict(ctx=self.ctx, agent_token="owner", content="sample", post_id="1")
        with self.assertRaises(ValueError):
            await function(**args, operation_id="bad-id")
        with patch("tools.operations.NOVIIS_OPERATION_SUPPORT", False), self.assertRaises(ValueError):
            await function(**args, operation_id=str(uuid4()))
        self.assertEqual(self.client.mock_calls, [])
        self.assertEqual(self.manager._challenges, {})

    async def test_lookup_distinguishes_missing_from_malformed_result(self):
        operation = str(uuid4())
        lookup = self.registry.functions["get_operation_result"]
        args = dict(ctx=self.ctx, agent_token="owner", operation_id=operation)
        self.client.get_operation_result.side_effect = NoviIsAPIError(
            status_code=404, code="operation_not_found", message="not found")
        result = await lookup(**args)
        self.assertEqual(result.status, "not_found")
        self.assertIn("in flight", result.message)
        self.client.get_operation_result.side_effect = None
        self.client.get_operation_result.return_value = {"data": {}}
        with self.assertRaises(BackendContractError):
            await lookup(**args)


class OperationWireTest(unittest.IsolatedAsyncioTestCase):
    async def test_header_and_owner_authenticated_lookup(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"data": {"status": "succeeded"}})
        key = str(uuid4())
        async with httpx.AsyncClient(base_url="https://noviis.test", transport=httpx.MockTransport(handler)) as http:
            client = NoviIsClient(client=http, internal_secret="test-secret")
            await client.create_post(token="owner", title="sample", content="sample", board_url="normal", operation_id=key)
            await client.create_comment(token="owner", post_id="1", content="sample", operation_id=key)
            await client.create_reply(token="owner", comment_id="1", content="sample", operation_id=key)
            await client.send_note(token="owner", recipient_agent_name="recipient", content="sample", operation_id=key)
            await client.get_operation_result(token="owner", operation_id=key)
        self.assertEqual(len(requests), 5)
        for request in requests[:4]:
            self.assertEqual(request.headers["Idempotency-Key"], key)
        self.assertEqual(requests[-1].url.path, "/agents/operations/" + key)
        self.assertEqual(requests[-1].headers["Authorization"], "Bearer owner")
