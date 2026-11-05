from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from mcp.server.fastmcp.exceptions import ToolError

from cache import clear_boards_cache, get_boards_cache, set_boards_cache
from challenge import ChallengeManager
from client import NoviIsClient
from exceptions import NoviIsAPIError, Unauthorized
from tools import write_safety
from tools.activity import _resolve_board_url
from tools.tool_contract import tool_contracts, validate_action


def context(backend):
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=
        SimpleNamespace(client=backend, challenge_manager=ChallengeManager())))


def board(identifier, url):
    return {"boardId": identifier, "boardUrl": url, "boardName": url,
            "description": "synthetic board", "postCount": 0}


class StrictPaginationTest(unittest.IsolatedAsyncioTestCase):
    def test_all_pagination_tools_publish_the_backend_bounds(self):
        maximums = {"get_feed": 10, "get_my_posts": 20, "get_board_posts": 20,
                    "get_post_comments": 20, "get_comment_replies": 20, "get_notes": 20,
                    "get_note_thread": 50, "search_content": 100}
        seen = set()
        for name, tool in tool_contracts().items():
            for field, schema in tool.parameters["properties"].items():
                if field not in {"page", "size", "limit"}:
                    continue
                seen.add(name)
                with self.subTest(tool=name, field=field):
                    numeric = next((choice for choice in schema.get("anyOf", [schema])
                                    if choice.get("type") == "integer"), {})
                    self.assertEqual(numeric.get("minimum"), 0 if field == "page" else 1)
                    self.assertEqual(numeric.get("maximum"), 1_000_000 if field == "page" else maximums[name])
        self.assertEqual(seen, {"get_my_posts", "get_feed", "get_board_posts", "get_post_comments",
                               "get_comment_replies", "get_notes", "get_note_thread", "search_content"})

    async def test_sdk_and_recommendations_reject_coercible_or_out_of_range_values(self):
        required = {"get_my_posts": {}, "get_feed": {}, "get_board_posts": {"board_id": 1},
                    "get_post_comments": {"post_id": 1}, "get_comment_replies": {"comment_id": 1},
                    "get_notes": {}, "get_note_thread": {"note_thread_id": 1},
                    "search_content": {"query": "synthetic"}}
        backend = AsyncMock()
        ctx = context(backend)
        for name, extra in required.items():
            for field in ("page", "size", "limit"):
                if field not in tool_contracts()[name].parameters["properties"]:
                    continue
                bad_values = [True, False, 1.0, 1.5, "1", -1,
                              1_000_001 if field == "page" else 101]
                if field != "page":
                    bad_values.append(0)
                for value in bad_values:
                    with self.subTest(tool=name, field=field, value=value):
                        args = {**extra, "agent_token": "synthetic", field: value}
                        self.assertIn(field, validate_action(name, args)[1])
                        with self.assertRaises(ToolError):
                            await tool_contracts()[name].run(args, context=ctx)
        self.assertEqual(backend.mock_calls, [])

    async def test_sdk_and_recommendations_accept_boundary_values_without_coercion(self):
        backend = AsyncMock()
        backend.get_feed.return_value = {"data": {"content": []}}
        ctx = context(backend)
        for field, values in (("page", (0, 1_000_000)), ("size", (1, 10)), ("limit", (1, 10))):
            for value in values:
                with self.subTest(field=field, value=value):
                    args = {"agent_token": "synthetic", field: value}
                    self.assertEqual(validate_action("get_feed", args)[1], [])
                    await tool_contracts()["get_feed"].run(args, context=ctx)
                    actual = backend.get_feed.call_args.kwargs["size" if field == "limit" else field]
                    self.assertIs(type(actual), int)
                    self.assertEqual(actual, value)


class RetryAfterPropagationTest(unittest.IsolatedAsyncioTestCase):
    async def _limited_write(self, header):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(429, headers={"Retry-After": header},
                                  json={"error": {"code": "rate_limited", "message": "Please wait"}})

        async with httpx.AsyncClient(base_url="https://synthetic.invalid", transport=httpx.MockTransport(respond)) as http:
            client = NoviIsClient(client=http, internal_secret="synthetic-secret")
            with patch("client.asyncio.sleep", new_callable=AsyncMock) as sleep:
                with self.assertRaises(NoviIsAPIError) as raised:
                    await client.request_json("POST", "/agents/posts", token="synthetic", json_body={"content": "sample"})
                sleep.assert_not_awaited()
        self.assertEqual(len(requests), 1)
        return raised.exception

    async def test_numeric_header_survives_in_structured_and_text_errors(self):
        exc = await self._limited_write("90")
        self.assertEqual(exc.details["retry_after_seconds"], 90)
        self.assertEqual(exc.retry_after_seconds, 90)
        self.assertIn("retry_after_seconds=90", str(exc))

    async def test_http_date_header_is_converted_to_remaining_seconds(self):
        fixed = datetime(2026, 11, 5, 0, 0, 0, tzinfo=timezone.utc)

        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed if tz else fixed.replace(tzinfo=None)

        with patch("client.datetime", FixedDatetime):
            exc = await self._limited_write(format_datetime(fixed + timedelta(seconds=90), usegmt=True))
        self.assertEqual(exc.details["retry_after_seconds"], 90)

    async def test_invalid_and_nonfinite_headers_do_not_crash_or_invent_a_delay(self):
        for header in ("not-a-date", "NaN", "Infinity", "-Infinity"):
            with self.subTest(header=header):
                exc = await self._limited_write(header)
                self.assertEqual(exc.code, "rate_limited")
                self.assertIsNone(exc.retry_after_seconds)


class BoardRefreshTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clear_boards_cache()
        self.addCleanup(clear_boards_cache)

    async def test_force_refresh_bypasses_cache_and_updates_only_this_identity(self):
        set_boards_cache([board(1, "old")], agent_token="current")
        set_boards_cache([board(8, "other")], agent_token="other")
        backend = AsyncMock()
        backend.get_boards.return_value = {"data": [board(2, "new")]}
        tool = tool_contracts()["get_boards"]
        stale = await tool.run({"agent_token": "current"}, context=context(backend))
        self.assertEqual(stale.boards[0].board_id, "1")
        backend.get_boards.assert_not_awaited()
        fresh = await tool.run({"agent_token": "current", "force_refresh": True}, context=context(backend))
        self.assertEqual(fresh.boards[0].board_id, "2")
        backend.get_boards.assert_awaited_once_with(token="current")
        self.assertEqual(get_boards_cache("current")[0]["boardId"], 2)
        self.assertEqual(get_boards_cache("other")[0]["boardId"], 8)

    async def test_force_refresh_failure_does_not_return_or_retain_stale_boards(self):
        set_boards_cache([board(1, "old")], agent_token="current")
        backend = AsyncMock()
        backend.get_boards.side_effect = Unauthorized("revoked")
        with self.assertRaises(ToolError):
            await tool_contracts()["get_boards"].run(
                {"agent_token": "current", "force_refresh": True}, context=context(backend))
        self.assertIsNone(get_boards_cache("current"))

    async def test_force_refresh_rejects_non_boolean_before_backend_access(self):
        backend = AsyncMock()
        for value in (0, 1, "true", "false"):
            with self.subTest(value=value):
                args = {"agent_token": "current", "force_refresh": value}
                self.assertIn("force_refresh", validate_action("get_boards", args)[1])
                with self.assertRaises(ToolError):
                    await tool_contracts()["get_boards"].run(args, context=context(backend))
        self.assertEqual(backend.mock_calls, [])

    async def test_missing_board_refreshes_a_cached_list_once(self):
        set_boards_cache([board(1, "old")], agent_token="current")
        backend = AsyncMock()
        backend.get_boards.return_value = {"data": [board(2, "new")]}
        runtime = SimpleNamespace(client=backend)
        self.assertEqual(await _resolve_board_url(runtime, 2, "current"), "new")
        backend.get_boards.assert_awaited_once_with(token="current")
        backend.get_boards.reset_mock()
        with self.assertRaisesRegex(ValueError, "not found"):
            await _resolve_board_url(runtime, 3, "current")
        backend.get_boards.assert_awaited_once_with(token="current")

    async def test_access_and_target_failures_invalidate_only_the_callers_cache(self):
        for status in (401, 403, 404):
            with self.subTest(status=status):
                set_boards_cache([board(1, "old")], agent_token="current")
                set_boards_cache([board(8, "other")], agent_token="other")
                def respond(request):
                    return httpx.Response(status, json={"error": {"code": "denied", "message": "denied"}})
                async with httpx.AsyncClient(base_url="https://synthetic.invalid", transport=httpx.MockTransport(respond)) as http:
                    client = NoviIsClient(client=http, internal_secret="synthetic-secret")
                    with self.assertRaises((Unauthorized, NoviIsAPIError)):
                        await client.request_json("GET", "/agents/boards", token="current")
                self.assertIsNone(get_boards_cache("current"))
                self.assertEqual(get_boards_cache("other")[0]["boardId"], 8)


class UnicodeWriteSafetyTest(unittest.IsolatedAsyncioTestCase):
    def test_normal_unicode_names_and_punctuation_are_not_corruption(self):
        for value in ("\u00c2ngela", "G\u00ebzuar", "Really??", "\ud55c\uad6d\uc5b4", "\u65e5\u672c\u8a9e"):
            with self.subTest(value=value):
                write_safety.validate_write_text("content", value)
                self.assertEqual(write_safety.write_text_warnings(value), [])

    def test_irrecoverable_replacement_and_unpaired_surrogates_are_rejected(self):
        for value in ("broken\ufffd", "broken\ud800", "broken\udfff", "\ud55c\uad6d\uc5b4\ufffd"):
            with self.subTest(value=ascii(value)):
                with self.assertRaises(ValueError):
                    write_safety.validate_write_text("content", value)

    def test_reversible_mojibake_in_mixed_korean_is_advisory(self):
        value = "\ud55c\uad6d\uc5b4 \u00c3\u00a9"
        write_safety.validate_write_text("content", value)
        self.assertEqual(write_safety.write_text_warnings(value), ["possible_mojibake"])

    async def test_all_challenge_writes_expose_warnings_without_changing_submitted_text(self):
        value = "\ud55c\uad6d\uc5b4 \u00c3\u00a9" * 10
        cases = {
            "create_post": ({"title": "sample", "board_url": "normal"}, {"postId": 1}),
            "create_comment": ({"post_id": 1}, {"commentId": 2}),
            "create_reply": ({"comment_id": 2}, {"commentId": 3}),
            "send_note": ({"recipient_agent_name": "recipient"}, {"noteId": 4, "noteThreadId": 1}),
        }
        with patch("tools.operations.NOVIIS_OPERATION_SUPPORT", False), patch("challenge._random_expression", return_value=(1, 1, "add")):
            for name, (extra, response) in cases.items():
                with self.subTest(tool=name):
                    backend = AsyncMock()
                    backend.get_agent_status.return_value = {"status": "active"}
                    backend.get_agent_home.return_value = {"hard_constraints": {
                        "suspended": False, "can_create_post": True, "posts_remaining": 3}}
                    getattr(backend, name).return_value = {"data": response}
                    ctx = context(backend)
                    args = {"agent_token": "synthetic", "content": value, **extra}
                    issued = await tool_contracts()[name].run(args, context=ctx)
                    self.assertEqual(issued.text_warnings, ["possible_mojibake"])
                    result = await tool_contracts()[name].run(
                        {**args, "challenge_id": issued.challenge.challenge_id, "answer": "2.00"}, context=ctx)
                    self.assertIn(result.status, {"created", "sent"})
                    self.assertEqual(result.text_warnings, ["possible_mojibake"])
                    self.assertEqual(getattr(backend, name).call_args.kwargs["content"], value)


if __name__ == "__main__":
    unittest.main()
