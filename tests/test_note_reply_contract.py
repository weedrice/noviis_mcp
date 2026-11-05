import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from challenge import ChallengeManager
from client import NoviIsClient
from exceptions import NoviIsAPIError
from mcp.server.fastmcp.exceptions import ToolError
from tools.tool_contract import tool_contracts, validate_action


class NoteReplyContractTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = AsyncMock()
        self.client.get_agent_status.return_value = {"status": "active"}
        self.client.send_note.return_value = {"data": {"note_thread_id": 12, "note_id": 34, "status": "sent"}}
        self.manager = ChallengeManager()
        self.ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=SimpleNamespace(
            client=self.client, challenge_manager=self.manager)))
        for target, value in (("tools.operations.NOVIIS_OPERATION_SUPPORT", False),
                              ("challenge._random_expression", (1, 1, "add"))):
            mocked = patch(target, return_value=value) if isinstance(value, tuple) else patch(target, value)
            mocked.start()
            self.addCleanup(mocked.stop)

    async def call(self, **params):
        return await tool_contracts()["send_note"].run(
            {"content": "reply body", "agent_token": "synthetic", **params}, context=self.ctx)

    async def answer(self, issued, **params):
        return await self.call(challenge_id=issued.challenge.challenge_id, answer="2.00", **params)

    async def test_reply_uses_thread_only_and_confirms_target(self):
        issued = await self.call(note_thread_id="00012")
        result = await self.answer(issued, note_thread_id=12)
        self.assertEqual(result.status, "sent")
        self.client.send_note.assert_awaited_once_with(token="synthetic", note_thread_id=12, content="reply body")

    async def test_wrong_response_thread_is_not_success(self):
        self.client.send_note.return_value["data"]["note_thread_id"] = 99
        issued = await self.call(note_thread_id=12)
        with self.assertRaisesRegex(ToolError, "different note thread"):
            await self.answer(issued, note_thread_id=12)

    async def test_changed_target_cannot_reuse_challenge(self):
        issued = await self.call(note_thread_id=12)
        result = await self.answer(issued, note_thread_id=13)
        self.assertNotEqual(result.status, "sent")
        self.client.send_note.assert_not_awaited()

    async def test_invalid_targets_fail_before_authentication_or_challenge(self):
        cases = [{}, {"recipient_agent_name": "recipient", "note_thread_id": 12}]
        cases += [{"note_thread_id": value} for value in (0, -1, True, 1.5, "opaque", "9223372036854775808")]
        for params in cases:
            with self.subTest(params=params), self.assertRaises(ToolError):
                await self.call(**params)
        self.client.get_agent_status.assert_not_awaited()
        self.assertFalse(self.manager._challenges)

    async def test_legacy_name_sending_is_preserved(self):
        issued = await self.call(recipient_agent_name="recipient")
        await self.answer(issued, recipient_agent_name="recipient")
        self.client.send_note.assert_awaited_once_with(token="synthetic", recipient_agent_name="recipient", content="reply body")

    def test_recommendations_validate_the_same_exclusive_target(self):
        for params in ({}, {"note_thread_id": "opaque"}, {"note_thread_id": 12, "recipient_agent_name": "recipient"}):
            _, invalid, missing, error = validate_action("send_note", {"content": "reply body", **params})
            self.assertTrue(invalid or missing or error)
        _, invalid, missing, error = validate_action("send_note", {"content": "reply body", "note_thread_id": 12})
        self.assertFalse(invalid or missing or error)

    async def test_legacy_server_rejection_never_falls_back_to_name(self):
        seen = []

        def legacy(request):
            seen.append(request)
            self.assertEqual(json.loads(request.content), {"note_thread_id": 12, "content": "reply body"})
            return httpx.Response(400, json={"code": "VALIDATION_FAILED", "message": "recipient required"})

        async with httpx.AsyncClient(base_url="https://synthetic.invalid/api/v1", transport=httpx.MockTransport(legacy)) as http:
            client = NoviIsClient(client=http, internal_secret="synthetic")
            with self.assertRaises(NoviIsAPIError):
                await client.send_note(token="synthetic", note_thread_id=12, content="reply body")
        self.assertEqual(len(seen), 1)
