from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from mcp.server.fastmcp.exceptions import ToolError

from cache import clear_boards_cache
from challenge import ChallengeManager
from tools.tool_contract import tool_contracts, validate_action


class BoardTargetContractTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clear_boards_cache()
        self.addCleanup(clear_boards_cache)
        self.client = AsyncMock()
        self.client.get_agent_status.return_value = {"status": "active"}
        self.client.get_agent_home.return_value = {"hard_constraints": {
            "suspended": False, "can_create_post": True, "posts_remaining": 3}}
        self.client.get_boards.return_value = {"data": [{"boardId": 9, "boardUrl": "normal"}]}
        self.client.create_post.return_value = {"data": {"postId": 123}}
        self.manager = ChallengeManager()
        self.ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=SimpleNamespace(
            client=self.client, challenge_manager=self.manager)))
        self.base = {"agent_token": "synthetic", "title": "test", "content": "valid body " * 6}

    async def test_ambiguous_targets_fail_before_authentication_or_challenge(self):
        for target in ({}, {"board_id": 9, "board_url": "normal"}, {"board_id": 9, "board_url": "different-board"}):
            with self.subTest(target=target), self.assertRaises(ToolError):
                await tool_contracts()["create_post"].run({**self.base, **target}, context=self.ctx)
            _, invalid, missing, _ = validate_action("create_post", {**self.base, **target})
            self.assertTrue(invalid or missing)
        self.assertEqual(self.client.mock_calls, [])
        self.assertFalse(self.manager._challenges)

    async def test_each_single_target_supports_the_existing_two_step_write(self):
        for target in ({"board_id": 9}, {"board_url": "normal"}):
            with self.subTest(target=target), patch("challenge._random_expression", return_value=(1, 1, "add")):
                args = {**self.base, **target}
                issued = await tool_contracts()["create_post"].run(args, context=self.ctx)
                result = await tool_contracts()["create_post"].run(
                    {**args, "challenge_id": issued.challenge.challenge_id, "answer": "2.00"}, context=self.ctx)
                self.assertEqual(result.status, "created")
                self.assertEqual(self.client.create_post.call_args.kwargs["board_url"], "normal")
                _, invalid, missing, error = validate_action("create_post", args)
                self.assertFalse(invalid or missing or error)
        self.assertEqual(self.client.create_post.await_count, 2)
