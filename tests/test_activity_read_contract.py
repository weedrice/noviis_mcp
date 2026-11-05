from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import unittest

import httpx

from client import NoviIsClient
from scripts.sync_backend_contract import check_local
from tools.tool_contract import tool_contracts


class ActivityReadContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_generated_backend_observed_read_contract(self):
        check_local("activity-read")
        fixture = json.loads((Path(__file__).parent / "fixtures/backend/agent-activity-read-contract.json").read_text(encoding="utf-8"))
        exchange = fixture["observed"]
        requests = []

        def respond(request):
            requests.append(request)
            self.assertEqual(request.method, exchange["request"]["method"])
            self.assertEqual(request.url.path, exchange["request"]["path"])
            self.assertEqual(json.loads(request.content), exchange["request"]["body"])
            return httpx.Response(200, json=exchange["response"])

        async with httpx.AsyncClient(base_url="https://fixture.invalid/api/v1", transport=httpx.MockTransport(respond)) as http:
            backend = NoviIsClient(client=http, internal_secret="synthetic-secret")
            ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=SimpleNamespace(client=backend)))
            result = await tool_contracts()["mark_post_activity_read"].run(
                {"post_id": 170, "observed_comment_ids": ["1001", 1002], "agent_token": "synthetic-agent"}, context=ctx)
        self.assertEqual(len(requests), 1)
        self.assertTrue(result.observed_boundary_applied)
        self.assertEqual(result.remaining_unread_count, 1)
        self.assertEqual(result.marked_read_at, exchange["response"]["data"]["marked_read_at"])
