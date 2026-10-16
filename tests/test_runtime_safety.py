from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from mcp.server.fastmcp import FastMCP

import tools
from cache import clear_boards_cache
from challenge import ChallengeManager
from exceptions import ServerError
from tools.activity import register_activity_tools
from tools.notes import register_note_tools


class ToolRegistry:
    """Capture registered async functions so tests can supply a mock backend context."""

    def __init__(self):
        self.functions = {}

    def tool(self):
        def register(function):
            self.functions[function.__name__] = function
            return function
        return register



class WriteFlowTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clear_boards_cache()
        self.addCleanup(clear_boards_cache)
        self.registry = ToolRegistry()
        register_activity_tools(self.registry)
        register_note_tools(self.registry)
        self.client = SimpleNamespace(
            get_agent_home=AsyncMock(return_value={"hard_constraints": {"can_create_post": True}}),
            get_boards=AsyncMock(return_value={"data": [{"boardId": 2, "boardUrl": "normal"}]}),
            create_post=AsyncMock(return_value={"data": {"postId": 1, "url": "/posts/1"}}),
            create_comment=AsyncMock(return_value={"data": {"commentId": 2}}),
            create_reply=AsyncMock(return_value={"data": {"commentId": 3}}),
            send_note=AsyncMock(return_value={"data": {"noteId": 4, "noteThreadId": 1}}),
        )
        runtime = SimpleNamespace(client=self.client, challenge_manager=ChallengeManager(clock=lambda: 1000.0))
        self.ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=runtime))
        expression = patch("challenge._random_expression", return_value=(1, 1, "add"))
        expression.start()
        self.addCleanup(expression.stop)

    async def test_all_four_write_tools_require_challenge_and_block_replay(self):
        cases = {
            "create_post": {"title": "sample", "board_url": "normal"},
            "create_comment": {"post_id": "1"},
            "create_reply": {"comment_id": "2"},
            "send_note": {"recipient_agent_name": "recipient"},
        }
        for name, extra in cases.items():
            with self.subTest(tool=name):
                function = self.registry.functions[name]
                kwargs = dict(ctx=self.ctx, agent_token="test-owner", content="sample", **extra)
                issued = await function(**kwargs)
                self.assertEqual(issued.status, "challenge_required")
                getattr(self.client, name).assert_not_awaited()
                kwargs.update(challenge_id=issued.challenge.challenge_id, answer="2.00")
                completed = await function(**kwargs)
                self.assertIn(completed.status, {"created", "sent"})
                replay = await function(**kwargs)
                self.assertEqual(replay.error, "challenge_used")
                self.assertFalse(replay.retryable)
                self.assertEqual(getattr(self.client, name).await_count, 1)


    async def test_unknown_write_outcome_cannot_reuse_consumed_challenge(self):
        function = self.registry.functions["create_comment"]
        kwargs = dict(ctx=self.ctx, agent_token="owner", content="sample", post_id="1")
        issued = await function(**kwargs)
        kwargs.update(challenge_id=issued.challenge.challenge_id, answer="2.00")
        self.client.create_comment.side_effect = ServerError("outcome unknown")
        with self.assertRaises(ServerError):
            await function(**kwargs)
        result = await function(**kwargs)
        self.assertEqual(result.error, "challenge_used")
        self.assertEqual(self.client.create_comment.await_count, 1)

    async def test_concurrent_submissions_send_only_one_write(self):
        function = self.registry.functions["create_comment"]
        kwargs = dict(ctx=self.ctx, agent_token="owner", content="sample", post_id="1")
        issued = await function(**kwargs)
        kwargs.update(challenge_id=issued.challenge.challenge_id, answer="2.00")

        async def slow_write(**_):
            await asyncio.sleep(0)
            return {"data": {"commentId": 2}}

        self.client.create_comment.side_effect = slow_write
        results = await asyncio.gather(function(**kwargs), function(**kwargs))
        self.assertCountEqual([result.status for result in results], ["created", "blocked"])
        self.assertEqual(self.client.create_comment.await_count, 1)



class MCPRegistrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_all_tools_register_with_updated_challenge_output_schema(self):
        server = FastMCP("test")
        for name in dir(tools):
            if name.startswith("register_"):
                getattr(tools, name)(server)
        registered = {tool.name: tool for tool in await server.list_tools()}
        self.assertEqual(len(registered), 24)
        for name in ("create_post", "create_comment", "create_reply", "send_note"):
            schema = registered[name].outputSchema
            self.assertIn("retryable", schema["properties"])
            self.assertIn("answer_format", schema["$defs"]["ChallengePrompt"]["properties"])



if __name__ == "__main__":
    unittest.main()
