from __future__ import annotations

from types import SimpleNamespace
import unittest

import httpx

from client import NoviIsClient
from tools.activity import register_activity_tools
from tools.notes import register_note_tools
from tools.search import register_search_tools
from test_runtime_safety import ToolRegistry


class BackendReadLimitsTest(unittest.IsolatedAsyncioTestCase):
    async def test_default_and_boundary_sizes_match_backend_bounds_on_the_wire(self):
        # AgentQueryService caps feed at 10 and other reads at 20;
        # AgentNoteService caps summaries at 20 and threads at 50.
        cases = [
            ("get_feed", {}, 10, 10),
            ("get_my_posts", {}, 10, 20),
            ("get_board_posts", {"board_id": 1}, 10, 20),
            ("get_post_comments", {"post_id": 1}, 20, 20),
            ("get_comment_replies", {"comment_id": 1}, 20, 20),
            ("get_notes", {}, 20, 20),
            ("get_note_thread", {"note_thread_id": 1}, 50, 50),
            ("search_content", {"query": "test"}, 20, 100),
        ]
        registry = ToolRegistry()
        for register in (register_activity_tools, register_note_tools, register_search_tools):
            register(registry)

        for name, arguments, default, maximum in cases:
            for supplied, expected in (({}, default), ({"size": maximum}, maximum), ({"size": 7}, 7)):
                for page in (0, 2):
                    with self.subTest(tool=name, supplied=supplied, page=page):
                        requests = []

                        def handler(request):
                            requests.append(request)
                            self.assertEqual(int(request.url.params["size"]), expected)
                            self.assertEqual(int(request.url.params["page"]), page)
                            # Simulate the backend's bounded PageResponse, including
                            # a valid empty page beyond the last page.
                            size = min(int(request.url.params["size"]), maximum)
                            return httpx.Response(200, json={"data": {
                                "content": [], "noteThreadId": 1, "page": page, "size": size,
                                "totalElements": 0, "totalPages": 0,
                                "hasNext": False, "hasPrevious": page > 0,
                            }})

                        async with httpx.AsyncClient(base_url="https://contract.test/api/v1", transport=httpx.MockTransport(handler)) as http:
                            client = NoviIsClient(client=http, internal_secret="synthetic-secret")
                            ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=SimpleNamespace(client=client)))
                            result = await registry.functions[name](ctx=ctx, agent_token="synthetic-token", page=page, **arguments, **supplied)
                        actual = result.page_size if hasattr(result, "page_size") else result.size
                        self.assertEqual(actual, expected)
                        self.assertFalse(result.has_next)
                        self.assertEqual(len(requests), 1)

    async def test_legacy_feed_limit_respects_bounds_and_rejects_conflicts_before_io(self):
        from unittest.mock import AsyncMock

        registry = ToolRegistry()
        register_activity_tools(registry)
        client = SimpleNamespace(get_feed=AsyncMock(return_value={"data": {"content": [], "size": 10}}))
        ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=SimpleNamespace(client=client)))
        result = await registry.functions["get_feed"](ctx=ctx, agent_token="synthetic-token", limit=10)
        self.assertEqual(result.page_size, 10)
        self.assertEqual(client.get_feed.call_args.kwargs["size"], 10)
        client.get_feed.reset_mock()
        with self.assertRaises(ValueError):
            await registry.functions["get_feed"](ctx=ctx, agent_token="synthetic-token", limit=5, size=10)
        client.get_feed.assert_not_awaited()
