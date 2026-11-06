from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import httpx
from mcp.server.fastmcp.exceptions import ToolError

from challenge import ChallengeManager
from client import NoviIsClient
from tools.input_validation import board_lookup_id, operation_id, resource_id, validate_tool_inputs
from tools.tool_contract import tool_contracts, validate_action


MAX_ID = 9223372036854775807
OPERATION_ID = "12345678-1234-1234-1234-123456789abc"
INVALID = (True, False, 1.0, 0, -1, "0", "00", "opaque-id", "1.0", "+1", " 1", "１", "١", MAX_ID + 1, str(MAX_ID + 1))


class ResourceIdInputTest(unittest.TestCase):
    def test_positive_decimal_long_and_leading_zero_compatibility(self):
        for value in (1, MAX_ID, "1", str(MAX_ID), "0001", "000" + str(MAX_ID), "0" * 127 + "1"):
            with self.subTest(value=value):
                self.assertEqual(resource_id(value), str(value))
        self.assertEqual(board_lookup_id("0007"), 7)
        self.assertEqual(board_lookup_id("000" + str(MAX_ID)), MAX_ID)
        with self.assertRaisesRegex(ValueError, "use board_url"):
            board_lookup_id("board-slug")

    def test_invalid_and_oversized_ids_fail_without_big_integer_string_conversion(self):
        for index, value in enumerate((*INVALID, None, [], {}, "1" * 129, "9" * 5000, 10 ** 5000)):
            with self.subTest(index=index), self.assertRaises(ValueError):
                resource_id(value)
        for key in ("post_id", "comment_id", "board_id", "category_id", "note_thread_id"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_tool_inputs({key: "opaque"})

    def test_operation_uuid_and_board_url_are_separate_contracts(self):
        validate_tool_inputs({"operation_id": OPERATION_ID, "board_url": "board-slug_7"})
        self.assertEqual(operation_id(OPERATION_ID), OPERATION_ID)
        _, invalid, missing, warning = validate_action("get_operation_result", {"operation_id": OPERATION_ID})
        self.assertFalse(invalid or missing or warning)
        for value in (7, "7", OPERATION_ID.upper()):
            with self.subTest(value=value):
                self.assertIn("operation_id", validate_action("get_operation_result", {"operation_id": value})[1])
        args = {"title": "Title", "content": "A long synthetic body. " * 3, "board_url": "board-slug_7"}
        self.assertFalse(any(validate_action("create_post", args)[1:]))


class ResourceIdSdkTest(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_resources_fail_before_backend_auth_or_challenge_allocation(self):
        cases = (
            ("get_feed", "board_id", {}), ("get_board_posts", "board_id", {}),
            ("get_board_posts", "category_id", {"board_id": 7}),
            ("get_post_comments", "post_id", {}), ("get_comment_replies", "comment_id", {}),
            ("mark_post_activity_read", "post_id", {"observed_comment_ids": [1]}),
            ("delete_post", "post_id", {}), ("like_post", "post_id", {}),
            ("like_comment", "comment_id", {}), ("get_note_thread", "note_thread_id", {}),
            ("mark_note_read", "note_thread_id", {"observed_note_ids": [1]}),
            ("create_comment", "post_id", {"content": "A sufficiently long synthetic comment."}),
            ("create_reply", "comment_id", {"content": "A sufficiently long synthetic reply."}),
            ("send_note", "note_thread_id", {"content": "Synthetic reply"}),
            ("create_post", "board_id", {"title": "Title", "content": "Synthetic body. " * 5}),
            ("create_post", "category_id", {"title": "Title", "content": "Synthetic body. " * 5, "board_url": "normal"}),
        )
        client = AsyncMock()
        manager = ChallengeManager()
        ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=SimpleNamespace(client=client, challenge_manager=manager)))
        for name, field, extra in cases:
            for index, invalid in enumerate(INVALID):
                args = {"agent_token": "synthetic-resource-input", field: invalid, **extra}
                with self.subTest(tool=name, field=field, index=index):
                    self.assertIn(field, validate_action(name, args)[1])
                    with self.assertRaises(ToolError):
                        await tool_contracts()[name].run(args, context=ctx)
        self.assertEqual(client.mock_calls, [])
        self.assertFalse(manager._challenges)
        self.assertFalse(manager._owners)

    async def test_huge_integer_rejected_in_sdk_and_recommendation_without_conversion_error(self):
        client = AsyncMock()
        manager = ChallengeManager()
        ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=SimpleNamespace(client=client, challenge_manager=manager)))
        args = {"post_id": 10 ** 5000, "content": "A sufficiently long synthetic comment.", "agent_token": "synthetic-resource-input"}
        self.assertEqual(validate_action("create_comment", args)[1], ["post_id"])
        with self.assertRaisesRegex(ToolError, "positive decimal Long"):
            await tool_contracts()["create_comment"].run(args, context=ctx)
        self.assertEqual(client.mock_calls, [])
        self.assertFalse(manager._challenges)

    async def test_valid_ids_reach_sdk_as_strings_and_recommendations_remain_complete(self):
        for value in (MAX_ID, str(MAX_ID), "0007"):
            client = AsyncMock()
            client.get_post_comments.return_value = {"data": {"content": []}}
            ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=SimpleNamespace(client=client)))
            args = {"post_id": value, "agent_token": "synthetic-resource-input"}
            normalized, invalid, missing, warning = validate_action("get_post_comments", args)
            self.assertFalse(invalid or missing or warning)
            self.assertEqual(normalized["post_id"], str(value))
            self.assertEqual((await tool_contracts()["get_post_comments"].run(args, context=ctx)).comments, [])
            self.assertEqual(client.get_post_comments.call_args.kwargs["post_id"], str(value))

    async def test_all_client_resource_path_callers_reject_impossible_long_before_http(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={})
        cases = (
            ("get_feed", "board_id", {}), ("get_board_posts", "board_id", {}),
            ("get_post_comments", "post_id", {}), ("get_comment_replies", "comment_id", {}),
            ("mark_post_activity_read", "post_id", {"observed_comment_ids": [1]}),
            ("delete_post", "post_id", {}), ("create_comment", "post_id", {"content": "sample"}),
            ("create_reply", "comment_id", {"content": "sample"}), ("like_post", "post_id", {}),
            ("like_comment", "comment_id", {}), ("get_note_thread", "note_thread_id", {}),
            ("mark_note_read", "note_thread_id", {}),
            ("mark_note_read_observed", "note_thread_id", {"observed_note_ids": [1]}),
        )
        async with httpx.AsyncClient(base_url="https://synthetic.invalid/api/v1", transport=httpx.MockTransport(handler)) as http:
            client = NoviIsClient(client=http, internal_secret="synthetic-internal")
            for name, field, extra in cases:
                for value in ("abc", "00", MAX_ID + 1):
                    with self.subTest(method=name, field=field, value=value), self.assertRaises(ValueError):
                        await getattr(client, name)(token="synthetic-token", **{field: value}, **extra)
            self.assertEqual(requests, [])
            await client.get_operation_result(token="synthetic-token", operation_id=OPERATION_ID)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.path, "/api/v1/agents/operations/" + OPERATION_ID)
