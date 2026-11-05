from __future__ import annotations

import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import httpx
from mcp.server.fastmcp.exceptions import ToolError

from client import NoviIsClient
from exceptions import BackendContractError
from tools.tool_contract import tool_contracts, validate_action


def context(backend):
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=SimpleNamespace(client=backend)))


def result_data(**updates):
    return {"note_thread_id": 7, "marked_read": True, "observed_boundary_applied": True,
            "remaining_unread_count": 2, **updates}


class ObservedNotesTest(unittest.IsolatedAsyncioTestCase):
    def test_schema_and_recommendations_require_observed_ids(self):
        tool = tool_contracts()["mark_note_read"]
        self.assertIn("observed_note_ids", tool.parameters["required"])
        schema = tool.parameters["properties"]["observed_note_ids"]
        self.assertEqual((schema["minItems"], schema["maxItems"]), (1, 1000))
        _, invalid, missing, _ = validate_action("mark_note_read", {"note_thread_id": 7})
        self.assertEqual(invalid, [])
        self.assertIn("observed_note_ids", missing)

    async def test_sdk_only_calls_observed_endpoint_with_normalized_unique_ids(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={"data": result_data()})

        async with httpx.AsyncClient(base_url="https://synthetic.invalid", transport=httpx.MockTransport(respond)) as http:
            backend = NoviIsClient(client=http, internal_secret="synthetic")
            result = await tool_contracts()["mark_note_read"].run(
                {"note_thread_id": 7, "agent_token": "synthetic", "observed_note_ids": [1, "001", "2", 9223372036854775807]},
                context=context(backend))
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.path, "/agents/notes/7/read/observed")
        self.assertEqual(json.loads(requests[0].content), {"observed_note_ids": [1, 2, 9223372036854775807]})
        self.assertEqual(result.note_thread_id, "7")
        self.assertEqual(result.remaining_unread_count, 2)
        self.assertTrue(result.observed_boundary_applied)

    async def test_invalid_or_missing_observed_ids_never_reach_backend(self):
        backend = AsyncMock()
        base = {"note_thread_id": 7, "agent_token": "synthetic"}
        with self.assertRaises(ToolError):
            await tool_contracts()["mark_note_read"].run(base, context=context(backend))
        for ids in ([], None, [True], [1.1], [0], [-1], ["bad"], ["1.0"], [" 1"],
                    [9223372036854775808], [None], [[1]], [1] * 1001, (1,), "[true]", "null"):
            with self.subTest(ids=str(ids)[:60]):
                args = {**base, "observed_note_ids": ids}
                self.assertIn("observed_note_ids", validate_action("mark_note_read", args)[1])
                with self.assertRaises(ToolError):
                    await tool_contracts()["mark_note_read"].run(args, context=context(backend))
        self.assertEqual(backend.mock_calls, [])

    async def test_sdk_json_array_compatibility_matches_recommendation(self):
        backend = AsyncMock()
        backend.mark_note_read_observed.return_value = {"data": result_data()}
        args = {"note_thread_id": 7, "agent_token": "synthetic", "observed_note_ids": '[1,"001","2"]'}
        params, invalid, missing, _ = validate_action("mark_note_read", args)
        self.assertEqual((invalid, missing), ([], []))
        await tool_contracts()["mark_note_read"].run(args, context=context(backend))
        self.assertEqual(backend.mark_note_read_observed.call_args.kwargs["observed_note_ids"], params["observed_note_ids"])

    async def test_one_thousand_ids_supported(self):
        backend = AsyncMock()
        backend.mark_note_read_observed.return_value = {"data": result_data()}
        await tool_contracts()["mark_note_read"].run(
            {"note_thread_id": 7, "agent_token": "synthetic", "observed_note_ids": list(range(1, 1001))}, context=context(backend))
        self.assertEqual(len(backend.mark_note_read_observed.call_args.kwargs["observed_note_ids"]), 1000)

    async def test_old_backend_404_never_falls_back_to_whole_thread(self):
        paths = []

        def respond(request):
            paths.append(request.url.path)
            return httpx.Response(404, json={"code": "not_found", "message": "Unsupported endpoint"})

        async with httpx.AsyncClient(base_url="https://synthetic.invalid", transport=httpx.MockTransport(respond)) as http:
            backend = NoviIsClient(client=http, internal_secret="synthetic")
            with self.assertRaises(ToolError):
                await tool_contracts()["mark_note_read"].run(
                    {"note_thread_id": 7, "agent_token": "synthetic", "observed_note_ids": [1]}, context=context(backend))
        self.assertEqual(paths, ["/agents/notes/7/read/observed"])

    async def test_missing_false_or_inconsistent_confirmation_is_not_success(self):
        backend = AsyncMock()
        invalid = [result_data(observed_boundary_applied=value) for value in (None, False, "true", 1)]
        invalid += [{key: value for key, value in result_data().items() if key != "observed_boundary_applied"},
                    result_data(note_thread_id=8), result_data(remaining_unread_count=True),
                    result_data(note_thread_id=True), result_data(note_thread_id=7.0),
                    result_data(note_thread_id="7"), result_data(noteThreadId=8),
                    result_data(remaining_unread_count=-1), result_data(marked_read="true"), result_data(marked_read=False),
                    result_data(observedBoundaryApplied=False)]
        for data in invalid:
            with self.subTest(data=data):
                backend.mark_note_read_observed.return_value = {"data": data}
                with self.assertRaises(ToolError) as raised:
                    await tool_contracts()["mark_note_read"].run(
                        {"note_thread_id": 7, "agent_token": "synthetic", "observed_note_ids": [1]}, context=context(backend))
                self.assertIsInstance(raised.exception.__cause__, BackendContractError)


if __name__ == "__main__":
    unittest.main()
