from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from challenge import ChallengeManager
from exceptions import BackendContractError
from tools.activity import register_activity_tools, _preflight_create_post
from tools.notes import register_note_tools
from tools.search import register_search_tools
from tools.constraint_contract import validate_hard_constraints
from test_runtime_safety import ToolRegistry


class BoundaryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.registry = ToolRegistry()
        for register in (register_activity_tools, register_note_tools, register_search_tools):
            register(self.registry)
        self.client = AsyncMock()
        self.manager = ChallengeManager()
        self.runtime = SimpleNamespace(client=self.client, challenge_manager=self.manager)
        self.ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=self.runtime))

    async def test_invalid_inputs_never_authenticate_or_allocate_challenges(self):
        cases = [
            ("create_comment", {"post_id": "../wrong", "content": "sample"}),
            ("create_reply", {"comment_id": "1", "content": "  "}),
            ("create_post", {"board_url": "normal", "title": "", "content": "sample"}),
            ("send_note", {"recipient_agent_name": " ", "content": "sample"}),
            ("get_feed", {"size": 101}), ("get_notes", {"page": -1}),
            ("get_notes", {"box": "invalid"}), ("search_content", {"query": ""}),
        ]
        for name, args in cases:
            with self.subTest(name=name), self.assertRaises(ValueError):
                await self.registry.functions[name](ctx=self.ctx, agent_token="test-token", **args)
        self.assertEqual(self.client.mock_calls, [])
        self.assertEqual(self.manager._challenges, {})
        self.assertEqual(self.manager._owners, {})

    async def test_noncreation_writes_do_not_invent_success(self):
        cases = [("delete_post", {"post_id": "1"}), ("mark_post_activity_read", {"post_id": "1"}),
                 ("mark_note_read", {"note_thread_id": "1"}), ("like_post", {"post_id": "1"}),
                 ("like_comment", {"comment_id": "1"})]
        for name, args in cases:
            getattr(self.client, name).return_value = {"data": {}}
            with self.subTest(name=name), self.assertRaises(BackendContractError):
                await self.registry.functions[name](ctx=self.ctx, agent_token="test-token", **args)
        self.client.delete_post.return_value = {"data": {"deleted": False}}
        result = await self.registry.functions["delete_post"](ctx=self.ctx, agent_token="test-token", post_id="1")
        self.assertEqual(result.status, "not_deleted")
        self.client.delete_post.return_value = {"_http_no_content": True}
        result = await self.registry.functions["delete_post"](ctx=self.ctx, agent_token="test-token", post_id="1")
        self.assertTrue(result.deleted)

    async def test_malformed_constraints_block_post_and_home_uses_same_validator(self):
        valid = {"suspended": False, "can_create_post": True, "posts_remaining": 2,
                 "can_create_comment": True, "comments_remaining": 2}
        malformed = [{"posts_remaining": "unknown"}, {**valid, "can_create_post": "yes"},
                     {**valid, "posts_remaining": True}, {**valid, "posts_remaining": -1},
                     {**valid, "canCreatePost": False}]
        for constraints in malformed:
            self.client.get_agent_home.return_value = {"hard_constraints": constraints}
            self.assertEqual((await _preflight_create_post(self.runtime, "test-token")).error, "agent_home_contract_invalid")
            with self.assertRaises(BackendContractError):
                validate_hard_constraints(constraints)
        self.assertEqual(validate_hard_constraints(valid), valid)
