from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import unittest

import httpx

from client import NoviIsClient
from scripts.sync_backend_contract import check_local
from tools.tool_contract import tool_contracts


class NoteReadWireContractTest(unittest.IsolatedAsyncioTestCase):
    def test_generated_backend_fixture_has_verified_provenance(self):
        check_local("note-read")

    async def test_sdk_uses_actual_backend_request_and_response(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/backend/agent-note-read-contract.json").read_text(encoding="utf-8"))
        exchange = fixture["observed"]
        seen = []

        def backend_response(request):
            seen.append(request)
            self.assertEqual(request.method, exchange["request"]["method"])
            self.assertEqual(request.url.path, exchange["request"]["path"])
            self.assertEqual(json.loads(request.content), exchange["request"]["body"])
            return httpx.Response(200, json=exchange["response"])

        async with httpx.AsyncClient(base_url="https://synthetic.invalid/api/v1", transport=httpx.MockTransport(backend_response)) as http:
            backend = NoviIsClient(client=http, internal_secret="synthetic")
            ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=SimpleNamespace(client=backend)))
            result = await tool_contracts()["mark_note_read"].run(
                {"agent_token": "synthetic", "note_thread_id": 170, **exchange["request"]["body"]}, context=ctx)
        self.assertEqual(len(seen), 1)
        self.assertEqual(result.note_thread_id, "170")
        self.assertTrue(result.observed_boundary_applied)
        self.assertEqual(result.remaining_unread_count, exchange["response"]["data"]["remaining_unread_count"])
        self.assertFalse(fixture["legacy"]["response"]["data"]["observed_boundary_applied"])


if __name__ == "__main__":
    unittest.main()
