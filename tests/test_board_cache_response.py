from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import httpx
from mcp.server.fastmcp.exceptions import ToolError

from cache import clear_boards_cache, get_boards_cache, set_boards_cache
from client import NoviIsClient
from exceptions import BackendContractError
from tools.activity import _load_boards
from tools.tool_contract import tool_contracts


class BoardCacheResponseTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clear_boards_cache()
        self.addCleanup(clear_boards_cache)

    async def test_malformed_collections_never_cache_an_empty_or_filtered_list(self):
        bad = [None, 1, "bad", {}, {"data": None}, {"data": {}}, {"data": {"boards": None}},
               {"boards": {}}, {"data": [None]}, {"data": [{"boardId": 7}, "bad"]},
               {"success": False, "data": []}, {"error": "failure", "boards": []},
               {"data": {"success": False, "boards": []}}, {"data": None, "boards": []}]
        for payload in bad:
            with self.subTest(payload=payload):
                clear_boards_cache()
                fetch = AsyncMock(side_effect=[payload, {"data": []}])
                runtime = SimpleNamespace(client=SimpleNamespace(get_boards=fetch))
                with self.assertRaises(BackendContractError):
                    await _load_boards(runtime, "synthetic")
                self.assertIsNone(get_boards_cache("synthetic"))
                self.assertEqual(await _load_boards(runtime, "synthetic"), [])
                self.assertEqual(fetch.await_count, 2)

    async def test_all_supported_wrappers_cache_valid_empty_and_nonempty_lists(self):
        for boards in ([], [{"boardId": 7, "boardUrl": "normal"}]):
            for payload in (boards, {"data": boards}, {"data": {"boards": boards}}, {"boards": boards}, {"result": boards}):
                with self.subTest(payload=payload):
                    clear_boards_cache()
                    fetch = AsyncMock(return_value=payload)
                    runtime = SimpleNamespace(client=SimpleNamespace(get_boards=fetch))
                    self.assertEqual(await _load_boards(runtime, "synthetic"), boards)
                    self.assertEqual(await _load_boards(runtime, "synthetic"), boards)
                    fetch.assert_awaited_once()

    async def test_failed_force_refresh_clears_only_current_identity(self):
        set_boards_cache([], agent_token="synthetic")
        set_boards_cache([], agent_token="other")
        runtime = SimpleNamespace(client=SimpleNamespace(get_boards=AsyncMock(return_value={"data": None})))
        with self.assertRaises(BackendContractError):
            await _load_boards(runtime, "synthetic", force_refresh=True)
        self.assertIsNone(get_boards_cache("synthetic"))
        self.assertEqual(get_boards_cache("other"), [])

    async def test_sdk_http_malformed_response_does_not_poison_next_read(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"data": None} if len(calls) == 1 else {"data": []})

        async with httpx.AsyncClient(base_url="https://mock.invalid/api/v1", transport=httpx.MockTransport(handler)) as http:
            runtime = SimpleNamespace(client=NoviIsClient(client=http, internal_secret="synthetic"))
            ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=runtime))
            tool = tool_contracts()["get_boards"]
            with self.assertRaises(ToolError):
                await tool.run({"agent_token": "synthetic"}, context=ctx)
            self.assertIsNone(get_boards_cache("synthetic"))
            result = await tool.run({"agent_token": "synthetic"}, context=ctx)
            self.assertEqual(result.boards, [])
            self.assertEqual(len(calls), 2)

    async def test_raw_http_array_is_supported_through_client_result_wrapper(self):
        async with httpx.AsyncClient(base_url="https://mock.invalid/api/v1", transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=[]))) as http:
            runtime = SimpleNamespace(client=NoviIsClient(client=http, internal_secret="synthetic"))
            self.assertEqual(await _load_boards(runtime, "synthetic"), [])
            self.assertEqual(get_boards_cache("synthetic"), [])


if __name__ == "__main__":
    unittest.main()
