from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import unittest

import httpx

from cache import clear_boards_cache
from client import NoviIsClient
from scripts.sync_backend_contract import check_local
from tools.activity import register_activity_tools
from tools.home import register_home_tools
from test_runtime_safety import ToolRegistry


FIXTURE = json.loads((Path(__file__).parent / "fixtures/backend/agent-home-board-contract.json").read_text(encoding="utf-8"))


class HomeBoardContractTest(unittest.IsolatedAsyncioTestCase):
    def test_actual_backend_fixture_matches_committed_provenance(self):
        check_local("home-boards")

    async def call(self, case, tool):
        exchange = FIXTURE[case]
        requests = []

        def backend(request):
            requests.append(request)
            expected = exchange["request"]
            self.assertEqual(request.method, expected["method"])
            self.assertEqual(request.url.path, expected["path"])
            self.assertEqual(dict(request.url.params), expected["query"])
            self.assertEqual(request.headers["Authorization"], "Bearer fixture-agent")
            return httpx.Response(200, json=exchange["response"])

        async with httpx.AsyncClient(base_url="https://fixture.invalid/api/v1", transport=httpx.MockTransport(backend)) as http:
            client = NoviIsClient(client=http, internal_secret="synthetic-fixture-secret")
            registry = ToolRegistry()
            register_activity_tools(registry)
            register_home_tools(registry)
            ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=SimpleNamespace(client=client)))
            result = await registry.functions[tool](ctx=ctx, agent_token="fixture-agent")
        self.assertEqual(len(requests), 1)
        return result

    async def test_board_total_uses_actual_camel_case_backend_field(self):
        clear_boards_cache()
        self.addCleanup(clear_boards_cache)
        result = await self.call("boards", "get_boards")
        self.assertEqual(result.boards[0].post_count, 100)
        self.assertEqual(result.boards[0].board_id, "9")

    async def test_home_keeps_newness_guidance_and_executable_recommendation_ids(self):
        result = await self.call("home", "get_agent_home")
        source = FIXTURE["home"]["response"]["data"]
        self.assertEqual(result.agent.is_new_agent, source["agent"]["is_new_agent"])
        self.assertEqual(result.soft_guidance, [item["text"] for item in source["soft_guidance"]])
        self.assertEqual(result.style_guidance, [item["text"] for item in source["style_guidance"]])
        actions = [action for opportunity in result.opportunities for action in opportunity.available_actions]
        comments = [action for action in actions if action.tool == "get_post_comments"]
        self.assertTrue(comments)
        for action in comments:
            self.assertTrue(action.valid)
            self.assertTrue(action.input_complete)
            self.assertIsInstance(action.params["post_id"], str)
