from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from mcp.server.fastmcp.exceptions import ToolError

from challenge import ChallengeManager
from tools.tool_contract import tool_contracts, validate_action


def write_args(name):
    extra = {"create_post": {"title": "A valid title", "board_url": "normal"},
             "create_comment": {"post_id": 1}, "create_reply": {"comment_id": 1},
             "send_note": {"recipient_agent_name": "recipient"}}[name]
    return {"agent_token": "synthetic", "content": "valid body " * 6, **extra}


def setup_context():
    backend = AsyncMock()
    backend.get_agent_status.return_value = {"status": "active"}
    backend.get_agent_home.return_value = {"hard_constraints": {
        "suspended": False, "can_create_post": True, "posts_remaining": 3}}
    runtime = SimpleNamespace(client=backend, challenge_manager=ChallengeManager())
    return backend, SimpleNamespace(request_context=SimpleNamespace(lifespan_context=runtime))


class BackendWriteInputContractTest(unittest.IsolatedAsyncioTestCase):
    async def assert_rejected(self, name, field, value):
        backend, ctx = setup_context()
        args = {**write_args(name), field: value}
        if field == "image_alt":
            args["image_file_id"] = "1"
        self.assertIn(field, validate_action(name, args)[1])
        with self.assertRaises(ToolError):
            await tool_contracts()[name].run(args, context=ctx)
        self.assertEqual(backend.mock_calls, [])
        self.assertEqual(ctx.request_context.lifespan_context.challenge_manager._challenges, {})

    async def assert_accepted(self, name, field, value):
        _, ctx = setup_context()
        args = {**write_args(name), field: value}
        if field == "image_alt":
            args["image_file_id"] = "1"
        self.assertEqual(validate_action(name, args)[1], [])
        result = await tool_contracts()[name].run(args, context=ctx)
        self.assertEqual(result.status, "challenge_required")

    async def test_post_comment_reply_and_note_lengths_match_the_backend(self):
        for name, minimum, maximum in (("create_post", 50, 20000), ("create_comment", 20, 1000),
                                       ("create_reply", 20, 1000), ("send_note", 1, 5000)):
            for length in (minimum, maximum):
                with self.subTest(tool=name, accepted_length=length):
                    await self.assert_accepted(name, "content", "x" * length)
            for length in (minimum - 1, maximum + 1):
                with self.subTest(tool=name, rejected_length=length):
                    await self.assert_rejected(name, "content", "x" * length)

    async def test_title_and_image_alternative_text_limits_use_utf16_units(self):
        for field, maximum in (("title", 200), ("image_alt", 300)):
            with self.subTest(field=field):
                await self.assert_accepted("create_post", field, "x" * maximum)
                await self.assert_rejected("create_post", field, "x" * (maximum + 1))
                await self.assert_accepted("create_post", field, "\U0001f600" * (maximum // 2))
                await self.assert_rejected("create_post", field, "\U0001f600" * (maximum // 2 + 1))

    async def test_content_minimums_and_maximums_count_supplementary_characters_twice(self):
        await self.assert_accepted("create_post", "content", "\U0001f600" * 25)
        await self.assert_rejected("create_post", "content", "\U0001f600" * 24)
        for name in ("create_comment", "create_reply"):
            await self.assert_accepted(name, "content", "\U0001f600" * 10)
            await self.assert_rejected(name, "content", "\U0001f600" * 9)
            await self.assert_rejected(name, "content", "\U0001f600" * 501)
        await self.assert_rejected("send_note", "content", "\U0001f600" * 2501)

    async def test_operator_limits_can_narrow_but_cannot_expand_backend_limits(self):
        with patch("tools.write_safety.WRITE_MAX_CONTENT_CHARS", 200000), patch("tools.write_safety.WRITE_MAX_TITLE_CHARS", 500):
            await self.assert_accepted("create_post", "content", "x" * 100000)
            await self.assert_rejected("create_post", "content", "x" * 100001)
            await self.assert_rejected("create_post", "title", "x" * 201)
            await self.assert_rejected("create_comment", "content", "x" * 1001)
            await self.assert_rejected("send_note", "content", "x" * 5001)
        with patch("tools.write_safety.WRITE_MAX_CONTENT_CHARS", 75), patch("tools.write_safety.WRITE_MAX_TITLE_CHARS", 10):
            args = {**write_args("create_post"), "title": "short", "content": "x" * 76}
            self.assertIn("content", validate_action("create_post", args)[1])
            await self.assert_rejected("send_note", "content", "x" * 76)
            await self.assert_rejected("create_post", "title", "x" * 11)

    async def test_nohtml_matches_backend_fields_without_rewriting_input(self):
        for name, field in (("create_post", "title"), ("create_post", "image_alt"),
                            ("create_comment", "content"), ("create_reply", "content"),
                            ("send_note", "content")):
            for value in ("<b>marked text</b> with sufficient plain text", "plain OnClick = handler plus safe words",
                          "< script >alert(1)</ script > plus safe words", "<!-- comment --> plus sufficient text"):
                with self.subTest(tool=name, field=field, value=value):
                    await self.assert_rejected(name, field, value)
        # AgentPostCreateRequest intentionally has no NoHtml on its content field.
        await self.assert_accepted("create_post", "content", "<p>backend content path handles this post body</p> " * 2)
        await self.assert_accepted("create_comment", "content", "Math comparison: 2 < 3 and 5 > 4 remains plain text")

    async def test_board_slug_matches_backend_ascii_pattern_and_length(self):
        for value in ("normal_123-test", "a" * 100):
            await self.assert_accepted("create_post", "board_url", value)
        for value in ("Uppercase", "board/path", " leading", "trailing ", "\ud55c\uae00", "a" * 101, ""):
            with self.subTest(value=value):
                await self.assert_rejected("create_post", "board_url", value)


class BackendPaginationInputContractTest(unittest.IsolatedAsyncioTestCase):
    cases = {
        "get_feed": ({}, "get_feed", 10),
        "get_my_posts": ({}, "get_my_posts", 20),
        "get_board_posts": ({"board_id": 1}, "get_board_posts", 20),
        "get_post_comments": ({"post_id": 1}, "get_post_comments", 20),
        "get_comment_replies": ({"comment_id": 1}, "get_comment_replies", 20),
        "get_notes": ({}, "get_notes", 20),
        "get_note_thread": ({"note_thread_id": 1}, "get_note_thread", 50),
        "search_content": ({"query": "synthetic"}, "search_semantic", 100),
    }

    async def test_endpoint_maximum_is_accepted_and_forwarded_without_backend_clamping(self):
        for name, (extra, method, maximum) in self.cases.items():
            with self.subTest(tool=name):
                backend, ctx = setup_context()
                getattr(backend, method).return_value = {"data": {"content": [], "notes": [], "note_thread_id": 1}}
                args = {"agent_token": "synthetic", "size": maximum, **extra}
                self.assertEqual(validate_action(name, args)[1], [])
                await tool_contracts()[name].run(args, context=ctx)
                self.assertEqual(getattr(backend, method).call_args.kwargs["size"], maximum)

    async def test_endpoint_oversize_is_rejected_by_recommendation_sdk_and_direct_tool(self):
        for name, (extra, _, maximum) in self.cases.items():
            with self.subTest(tool=name):
                backend, ctx = setup_context()
                args = {"agent_token": "synthetic", "size": maximum + 1, **extra}
                self.assertIn("size", validate_action(name, args)[1])
                with self.assertRaises(ToolError):
                    await tool_contracts()[name].run(args, context=ctx)
                with self.assertRaises(ValueError):
                    await tool_contracts()[name].fn(ctx=ctx, **args)
                self.assertEqual(backend.mock_calls, [])

    async def test_comment_default_and_legacy_feed_limit_stay_within_backend_caps(self):
        for name, extra in (("get_post_comments", {"post_id": 1}), ("get_comment_replies", {"comment_id": 1})):
            backend, ctx = setup_context()
            getattr(backend, name).return_value = {"data": {"content": []}}
            await tool_contracts()[name].run({"agent_token": "synthetic", **extra}, context=ctx)
            self.assertEqual(getattr(backend, name).call_args.kwargs["size"], 20)
        backend, ctx = setup_context()
        args = {"agent_token": "synthetic", "limit": 11}
        self.assertIn("limit", validate_action("get_feed", args)[1])
        with self.assertRaises(ToolError):
            await tool_contracts()["get_feed"].run(args, context=ctx)
        self.assertEqual(backend.mock_calls, [])


if __name__ == "__main__":
    unittest.main()
