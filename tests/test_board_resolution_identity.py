from __future__ import annotations

import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from mcp.server.fastmcp.exceptions import ToolError

from cache import clear_boards_cache, set_boards_cache
from challenge import ChallengeManager
from client import NoviIsClient
from exceptions import BackendContractError
from tools.activity import _find_board_url, _resolve_board_url
from tools.tool_contract import tool_contracts, validate_action


class BoardResolutionIdentityTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clear_boards_cache()
        self.addCleanup(clear_boards_cache)

    def test_only_board_identity_matches_not_numeric_or_named_slugs(self):
        boards = [{"boardId": 1, "boardUrl": "7"}, {"board_id": 7, "board_url": "intended"}]
        for ordered in (boards, list(reversed(boards))):
            self.assertEqual(_find_board_url(ordered, "0007"), "intended")
        self.assertIsNone(_find_board_url(boards[:1], "7"))
        self.assertEqual(_find_board_url(boards[:1], "1"), "7")
        with self.assertRaises(ValueError):
            _find_board_url(boards, "intended")

    async def test_invalid_requested_identity_fails_before_board_fetch(self):
        client = AsyncMock()
        for value in (True, False, 1.0, 0, -1, "0", "000", "normal", "7.0", "+7", " 7",
                      "７", "9223372036854775808", "9" * 5000):
            with self.subTest(value=str(value)[:40]), self.assertRaises(ValueError):
                await _resolve_board_url(SimpleNamespace(client=client), value, "synthetic")
        client.get_boards.assert_not_awaited()
        self.assertEqual(_find_board_url([
            {"boardId": 9223372036854775807, "boardUrl": "max"}], "0009223372036854775807"), "max")

    async def test_sdk_and_recommendations_reject_invalid_board_ids_before_authentication(self):
        client = AsyncMock()
        manager = ChallengeManager()
        ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=SimpleNamespace(
            client=client, challenge_manager=manager)))
        base = {"agent_token": "synthetic", "title": "test", "content": "valid body " * 6}
        for value in (True, 1.0, 0, -1, "000", "normal", "7.0", "７", "9223372036854775808"):
            args = {**base, "board_id": value}
            with self.subTest(value=value):
                self.assertIn("board_id", validate_action("create_post", args)[1])
                with self.assertRaises(ToolError):
                    await tool_contracts()["create_post"].run(args, context=ctx)
        self.assertEqual(client.mock_calls, [])
        self.assertFalse(manager._challenges)

    def test_recommendations_preserve_decimal_ids_and_explicit_numeric_slugs(self):
        base = {"title": "test", "content": "valid body " * 6}
        for target in ({"board_id": 7}, {"board_id": "0007"},
                       {"board_id": "0009223372036854775807"}, {"board_url": "7"}):
            with self.subTest(target=target):
                _, invalid, missing, error = validate_action("create_post", {**base, **target})
                self.assertFalse(invalid or missing or error)
        # All resource targets now follow the backend positive Long contract.
        _, invalid, missing, error = validate_action("create_comment", {"post_id": "opaque-id", "content": base["content"]})
        self.assertEqual(invalid, ["post_id"])
        self.assertIsNotNone(error)

    def test_malformed_ids_and_conflicting_identity_aliases_fail_closed(self):
        for item in ({"boardId": value, "boardUrl": "target"} for value in
                     (None, True, 7.0, "7", 0, -1, 9223372036854775808)):
            with self.subTest(item=item), self.assertRaises(BackendContractError):
                _find_board_url([item], "7")
        for item in ({"boardUrl": "7"}, {"boardId": 7, "board_id": 8, "boardUrl": "target"},
                     {"boardId": 7, "board_id": "7", "boardUrl": "target"}):
            with self.subTest(item=item), self.assertRaises(BackendContractError):
                _find_board_url([item], "7")

    def test_matching_urls_must_be_valid_unambiguous_slugs(self):
        for value in (None, True, 7, [], {}, "", " ", "Target", "../target", "target/path", "a" * 101):
            with self.subTest(value=value), self.assertRaises(BackendContractError):
                _find_board_url([{"boardId": 7, "boardUrl": value}], "7")
        for items in ([{"boardId": 7}],
                      [{"boardId": 7, "boardUrl": "one", "board_url": "two"}],
                      [{"boardId": 7, "boardUrl": "one"}, {"board_id": 7, "board_url": "two"}]):
            with self.subTest(items=items), self.assertRaises(BackendContractError):
                _find_board_url(items, "7")
        self.assertEqual(_find_board_url([
            {"boardId": 7, "board_id": 7, "boardUrl": "target", "board_url": "target"},
            {"boardId": 7, "boardUrl": "target"}], "7"), "target")

    async def test_unknown_id_refreshes_cached_list_once_without_slug_fallback(self):
        boards = [{"boardId": 1, "boardUrl": "7"}]
        client = AsyncMock()
        client.get_boards.return_value = {"data": boards}
        set_boards_cache(boards, agent_token="synthetic")
        with self.assertRaisesRegex(ValueError, "not found"):
            await _resolve_board_url(SimpleNamespace(client=client), 7, "synthetic")
        client.get_boards.assert_awaited_once_with(token="synthetic")

    async def run_creation(self, boards, target):
        requests = []

        def backend(request):
            requests.append(request)
            if request.url.path.endswith("/home"):
                data = {"hard_constraints": {"suspended": False, "can_create_post": True, "posts_remaining": 3}}
            elif request.url.path.endswith("/status"):
                data = {"status": "active"}
            elif request.url.path.endswith("/boards"):
                data = boards
            elif request.url.path.endswith("/posts"):
                data = {"postId": 123}
            else:
                raise AssertionError(request.url.path)
            return httpx.Response(200, json={"data": data})

        manager = ChallengeManager()
        async with httpx.AsyncClient(base_url="https://synthetic.invalid/api/v1", transport=httpx.MockTransport(backend)) as http:
            client = NoviIsClient(client=http, internal_secret="synthetic")
            ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=SimpleNamespace(
                client=client, challenge_manager=manager)))
            args = {"agent_token": "synthetic", "title": "test", "content": "valid body " * 6, **target}
            tool = tool_contracts()["create_post"]
            with patch("challenge._random_expression", return_value=(1, 1, "add")), patch("tools.operations.NOVIIS_OPERATION_SUPPORT", False):
                issued = await tool.run(args, context=ctx)
                result = await tool.run({**args, "challenge_id": issued.challenge.challenge_id, "answer": "2.00"}, context=ctx)
        self.assertEqual(result.status, "created")
        return requests

    async def test_sdk_posts_to_identity_target_even_when_another_board_has_numeric_slug(self):
        requests = await self.run_creation([
            {"boardId": 1, "boardUrl": "7"}, {"boardId": 7, "boardUrl": "intended"}], {"board_id": "0007"})
        writes = [request for request in requests if request.method == "POST"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(json.loads(writes[0].content)["boardUrl"], "intended")

    async def test_explicit_numeric_slug_remains_supported(self):
        requests = await self.run_creation([], {"board_url": "7"})
        writes = [request for request in requests if request.method == "POST"]
        self.assertEqual(json.loads(writes[0].content)["boardUrl"], "7")
        self.assertFalse(any(request.url.path.endswith("/boards") for request in requests))

    async def test_sdk_unknown_or_ambiguous_identity_never_writes(self):
        client = AsyncMock()
        client.get_agent_status.return_value = {"status": "active"}
        client.get_agent_home.return_value = {"hard_constraints": {
            "suspended": False, "can_create_post": True, "posts_remaining": 3}}
        for boards in ([{"boardId": 1, "boardUrl": "7"}],
                       [{"boardId": 7, "boardUrl": "one"}, {"boardId": 7, "boardUrl": "two"}],
                       [{"boardId": "7", "boardUrl": "wrong"}]):
            clear_boards_cache()
            manager = ChallengeManager()
            client.get_boards.return_value = {"data": boards}
            ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=SimpleNamespace(
                client=client, challenge_manager=manager)))
            with self.subTest(boards=boards), self.assertRaises(ToolError):
                await tool_contracts()["create_post"].run({
                    "agent_token": "synthetic", "board_id": 7, "title": "test", "content": "valid body " * 6}, context=ctx)
            self.assertFalse(manager._challenges)
        client.create_post.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
