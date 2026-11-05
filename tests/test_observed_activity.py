from __future__ import annotations

import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import httpx
from mcp.server.fastmcp.exceptions import ToolError

from client import NoviIsClient
from exceptions import BackendContractError, NoviIsAPIError
from tools.tool_contract import tool_contracts, validate_action


def context(backend):
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=SimpleNamespace(client=backend)))


def observed_result(**updates):
    return {"post_id": 7, "marked_read": True, "observed_boundary_applied": True,
            "marked_read_at": "2026-11-05T12:00:00+09:00", "remaining_unread_count": 2,
            **updates}


class ObservedActivityTest(unittest.IsolatedAsyncioTestCase):
    def test_schema_and_recommendation_require_observed_comment_ids(self):
        tool = tool_contracts()["mark_post_activity_read"]
        self.assertIn("observed_comment_ids", tool.parameters["required"])
        schema = tool.parameters["properties"]["observed_comment_ids"]
        self.assertEqual(schema["type"], "array")
        self.assertEqual(schema["minItems"], 1)
        self.assertEqual(schema["maxItems"], 1000)
        _, invalid, missing, _ = validate_action("mark_post_activity_read", {"post_id": 7})
        self.assertEqual(invalid, [])
        self.assertIn("observed_comment_ids", missing)

    async def test_sdk_normalizes_and_deduplicates_ids_before_the_observed_endpoint(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={"data": observed_result()})

        async with httpx.AsyncClient(base_url="https://synthetic.invalid", transport=httpx.MockTransport(respond)) as http:
            backend = NoviIsClient(client=http, internal_secret="synthetic-secret")
            result = await tool_contracts()["mark_post_activity_read"].run(
                {"post_id": 7, "observed_comment_ids": [1, "001", "2", 2, 9_223_372_036_854_775_807],
                 "agent_token": "synthetic-agent"}, context=context(backend))
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].method, "POST")
        self.assertEqual(requests[0].url.path, "/agents/posts/7/activity/read/observed")
        self.assertEqual(json.loads(requests[0].content), {"observed_comment_ids": [1, 2, 9_223_372_036_854_775_807]})
        self.assertEqual(requests[0].headers["Authorization"], "Bearer synthetic-agent")
        self.assertEqual(result.post_id, "7")
        self.assertTrue(result.marked_read)
        self.assertTrue(result.observed_boundary_applied)
        self.assertEqual(result.remaining_unread_count, 2)
        self.assertEqual(result.marked_read_at, "2026-11-05T12:00:00+09:00")

    async def test_missing_or_invalid_observed_ids_are_rejected_before_any_backend_call(self):
        backend = AsyncMock()
        backend.mark_post_activity_read.return_value = {"data": observed_result()}
        tool = tool_contracts()["mark_post_activity_read"]
        base = {"post_id": 7, "agent_token": "synthetic-agent"}
        with self.assertRaises(ToolError):
            await tool.run(base, context=context(backend))
        bad_values = [[], None, [True], [1.0], [0], [-1], ["0"], ["abc"], ["1.0"], [" 1"],
                      [9_223_372_036_854_775_808], ["9223372036854775808"], [None], [[1]],
                      [1] * 1001, (1,), "[true]", "[0]", "{}", "null", json.dumps([1] * 1001)]
        for ids in bad_values:
            with self.subTest(ids=str(ids)[:80]):
                args = {**base, "observed_comment_ids": ids}
                self.assertIn("observed_comment_ids", validate_action("mark_post_activity_read", args)[1])
                with self.assertRaises(ToolError):
                    await tool.run(args, context=context(backend))
        self.assertEqual(backend.mock_calls, [])

    async def test_sdk_json_array_compatibility_matches_recommendation_normalization(self):
        backend = AsyncMock()
        backend.mark_post_activity_read.return_value = {"data": observed_result()}
        args = {"post_id": 7, "observed_comment_ids": '[1, "001", "2"]',
                "agent_token": "synthetic-agent"}
        params, invalid, missing, _ = validate_action("mark_post_activity_read", args)
        self.assertEqual(invalid, [])
        self.assertEqual(missing, [])
        self.assertEqual(params["observed_comment_ids"], ["1", "2"])
        await tool_contracts()["mark_post_activity_read"].run(args, context=context(backend))
        self.assertEqual(backend.mark_post_activity_read.call_args.kwargs["observed_comment_ids"],
                         params["observed_comment_ids"])

    async def test_one_thousand_observed_ids_are_supported(self):
        backend = AsyncMock()
        backend.mark_post_activity_read.return_value = {"data": observed_result()}
        ids = list(range(1, 1001))
        args = {"post_id": 7, "observed_comment_ids": ids, "agent_token": "synthetic-agent"}
        self.assertEqual(validate_action("mark_post_activity_read", args)[1], [])
        await tool_contracts()["mark_post_activity_read"].run(args, context=context(backend))
        self.assertEqual(backend.mark_post_activity_read.call_args.kwargs["observed_comment_ids"],
                         [str(identifier) for identifier in ids])

    async def test_missing_false_or_malformed_boundary_confirmation_never_reports_success(self):
        backend = AsyncMock()
        args = {"post_id": 7, "observed_comment_ids": [1], "agent_token": "synthetic-agent"}
        for value in (False, None, 0, 1, "true", "false", "missing"):
            with self.subTest(boundary=value):
                data = observed_result(observed_boundary_applied=value)
                if value == "missing":
                    data.pop("observed_boundary_applied")
                backend.mark_post_activity_read.return_value = {"data": data}
                with self.assertRaises(ToolError) as raised:
                    await tool_contracts()["mark_post_activity_read"].run(args, context=context(backend))
                self.assertIsInstance(raised.exception.__cause__, BackendContractError)

    async def test_camel_case_boundary_confirmation_is_supported(self):
        backend = AsyncMock()
        backend.mark_post_activity_read.return_value = {"data": {
            "postId": 7, "markedRead": True, "observedBoundaryApplied": True,
            "markedReadAt": "2026-11-05T12:00:00+09:00", "remainingUnreadCount": 1}}
        result = await tool_contracts()["mark_post_activity_read"].run(
            {"post_id": 7, "observed_comment_ids": [1], "agent_token": "synthetic-agent"}, context=context(backend))
        self.assertTrue(result.observed_boundary_applied)
        self.assertEqual(result.remaining_unread_count, 1)

    async def test_old_backend_missing_endpoint_does_not_fall_back_to_legacy_mark_all_read(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(404, json={"error": {"code": "not_found", "message": "Not found"}})

        async with httpx.AsyncClient(base_url="https://synthetic.invalid", transport=httpx.MockTransport(respond)) as http:
            backend = NoviIsClient(client=http, internal_secret="synthetic-secret")
            with self.assertRaises(ToolError) as raised:
                await tool_contracts()["mark_post_activity_read"].run(
                    {"post_id": 7, "observed_comment_ids": [1], "agent_token": "synthetic-agent"}, context=context(backend))
        self.assertIsInstance(raised.exception.__cause__, NoviIsAPIError)
        self.assertEqual(raised.exception.__cause__.status_code, 404)
        self.assertEqual([request.url.path for request in requests], ["/agents/posts/7/activity/read/observed"])

    async def test_direct_client_also_validates_observed_ids_before_sending(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={"data": observed_result()})

        async with httpx.AsyncClient(base_url="https://synthetic.invalid", transport=httpx.MockTransport(respond)) as http:
            backend = NoviIsClient(client=http, internal_secret="synthetic-secret")
            for ids in ([], [True], [0], ["../1"], [1] * 1001):
                with self.subTest(ids=str(ids)[:80]), self.assertRaises(ValueError):
                    await backend.mark_post_activity_read(token="synthetic-agent", post_id="7", observed_comment_ids=ids)
        self.assertEqual(requests, [])


if __name__ == "__main__":
    unittest.main()
