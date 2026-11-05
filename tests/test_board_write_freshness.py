from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from mcp.server.fastmcp.exceptions import ToolError

from cache import clear_boards_cache, get_boards_cache, set_boards_cache
from challenge import ChallengeManager
from client import NoviIsClient
from tools.tool_contract import tool_contracts


class BoardWriteFreshnessTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clear_boards_cache()
        self.addCleanup(clear_boards_cache)
        self.args = {"agent_token": "synthetic", "title": "sample", "content": "sample " * 10, "board_id": 7}

    @asynccontextmanager
    async def backend(self, *, supported=False):
        state = SimpleNamespace(boards=[{"boardId": 7, "boardUrl": "old"}], reads=0, writes=[], fail=False)

        def handler(request):
            if request.url.path.endswith("/home"):
                data = {"hard_constraints": {"suspended": False, "can_create_post": True, "posts_remaining": 3}}
            elif request.url.path.endswith("/status"):
                data = {"status": "active"}
            elif request.url.path.endswith("/boards"):
                state.reads += 1
                if state.fail:
                    return httpx.Response(200, json={"data": None})
                data = state.boards
            elif request.method == "POST":
                body = json.loads(request.content)
                actual = next(board["boardId"] for board in state.boards if board["boardUrl"] == body["boardUrl"])
                state.writes.append((body, actual))
                data = {"postId": 123}
            else:
                raise AssertionError(request.url)
            return httpx.Response(200, json={"data": data})

        async with httpx.AsyncClient(base_url="https://mock.invalid/api/v1", transport=httpx.MockTransport(handler)) as http:
            runtime = SimpleNamespace(client=NoviIsClient(client=http, internal_secret="synthetic"), challenge_manager=ChallengeManager())
            state.runtime = runtime
            state.ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=runtime))
            with patch("tools.operations.NOVIIS_OPERATION_SUPPORT", supported), patch("challenge._random_expression", return_value=(1, 1, "add")):
                yield state

    async def call(self, state, args=None, issued=None, answer="2.00"):
        values = dict(self.args if args is None else args)
        if issued:
            values.update(challenge_id=issued.challenge.challenge_id, answer=answer)
            if issued.operation_id:
                values["operation_id"] = issued.operation_id
        return await tool_contracts()["create_post"].run(values, context=state.ctx)

    async def test_slug_rename_and_reuse_blocks_original_challenge_without_replacement(self):
        for supported in (False, True):
            with self.subTest(operation_support=supported):
                clear_boards_cache()
                async with self.backend(supported=supported) as state:
                    issued = await self.call(state)
                    state.boards = [{"boardId": 7, "boardUrl": "new"}, {"boardId": 8, "boardUrl": "old"}]
                    result = await self.call(state, issued=issued)
                    self.assertEqual((result.status, result.error, result.retryable), ("blocked", "challenge_context_mismatch", False))
                    self.assertIsNone(result.challenge)
                    self.assertEqual(result.operation_id, issued.operation_id)
                    self.assertEqual(state.reads, 2)
                    self.assertEqual(state.writes, [])
                    self.assertEqual(len(state.runtime.challenge_manager._challenges), 1)

    async def test_stale_cached_target_is_ignored_on_issuance_and_consumption(self):
        async with self.backend() as state:
            state.boards = [{"boardId": 7, "boardUrl": "new"}, {"boardId": 8, "boardUrl": "old"}]
            stale = [{"boardId": 7, "boardUrl": "old"}]
            set_boards_cache(stale, agent_token="synthetic")
            issued = await self.call(state)
            set_boards_cache(stale, agent_token="synthetic")
            result = await self.call(state, issued=issued)
            self.assertEqual(result.status, "created")
            self.assertEqual(state.reads, 2)
            self.assertEqual(state.writes[0][0]["boardUrl"], "new")
            self.assertEqual(state.writes[0][1], 7)

    async def test_explicit_slug_retains_its_meaning_without_id_lookup(self):
        async with self.backend() as state:
            args = {key: value for key, value in self.args.items() if key != "board_id"}
            args["board_url"] = "old"
            issued = await self.call(state, args)
            result = await self.call(state, args, issued)
            self.assertEqual(result.status, "created")
            self.assertEqual(state.reads, 0)
            self.assertEqual(state.writes[0][0]["boardUrl"], "old")

    async def test_malformed_fresh_response_does_not_use_cache_or_consume_challenge(self):
        async with self.backend() as state:
            issued = await self.call(state)
            state.fail = True
            with self.assertRaises(ToolError):
                await self.call(state, issued=issued)
            self.assertIsNone(get_boards_cache("synthetic"))
            self.assertEqual(state.writes, [])
            self.assertEqual(state.runtime.challenge_manager._challenges[issued.challenge.challenge_id].status, "pending")
            state.fail = False
            result = await self.call(state, issued=issued)
            self.assertEqual(result.status, "created")
            self.assertEqual(len(state.writes), 1)

    async def test_used_and_unknown_challenges_remain_blocked_after_target_change(self):
        async with self.backend() as state:
            issued = await self.call(state)
            await self.call(state, issued=issued)
            state.boards = [{"boardId": 7, "boardUrl": "new"}]
            used = await self.call(state, issued=issued)
            self.assertEqual(used.error, "challenge_used")
            self.assertFalse(used.retryable)
            state.runtime.challenge_manager = ChallengeManager()
            unknown = await self.call(state, issued=issued)
            self.assertEqual(unknown.error, "challenge_unknown")
            self.assertFalse(unknown.retryable)
            self.assertEqual(len(state.writes), 1)

    async def test_incorrect_answer_with_unchanged_target_still_reissues(self):
        async with self.backend() as state:
            issued = await self.call(state)
            incorrect = await self.call(state, issued=issued, answer="0")
            self.assertEqual(incorrect.error, "challenge_failed")
            self.assertTrue(incorrect.retryable)
            result = await self.call(state, issued=incorrect)
            self.assertEqual(result.status, "created")
            self.assertEqual(len(state.writes), 1)


if __name__ == "__main__":
    unittest.main()
