from types import SimpleNamespace
import json
import unittest
from unittest.mock import AsyncMock

import httpx
from mcp.server.fastmcp.exceptions import ToolError

from client import NoviIsClient
from tools.tool_contract import tool_contracts, validate_action


def context(backend):
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=SimpleNamespace(client=backend)))


class RegistrationContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_description_only_and_legacy_name_make_the_same_backend_request(self):
        requests = []

        def backend(request):
            requests.append(request)
            return httpx.Response(201, json={"data": {"agentToken": "synthetic-registration-token"}})

        tool = tool_contracts()["register_agent"]
        self.assertEqual(tool.parameters["required"], ["description"])
        async with httpx.AsyncClient(base_url="https://synthetic.invalid/api/v1", transport=httpx.MockTransport(backend)) as http:
            client = NoviIsClient(client=http, internal_secret="synthetic")
            for extra in ({}, {"name": "legacy name"}, {"name": ""}, {"name": "x" * 101}, {"name": None}):
                args = {"description": "description", **extra}
                self.assertEqual(validate_action("register_agent", args)[1:3], ([], []))
                result = await tool.run(args, context=context(client))
                self.assertEqual(result.agent_token, "synthetic-registration-token")
                self.assertIn(result.agent_token, result.user_message)
            await client.register_agent(name="legacy direct caller", description="description")
        self.assertEqual(len(requests), 6)
        self.assertTrue(all(json.loads(request.content) == {"description": "description"} for request in requests))

    async def test_backend_description_bounds_count_raw_utf16_units(self):
        tool = tool_contracts()["register_agent"]
        for value in ("x" * 3000, "x" * 5000, "\U0001f600" * 2500, "\u00a0", "plain < comparison"):
            backend = AsyncMock()
            backend.register_agent.return_value = {"data": {"agent_token": "synthetic-token"}}
            args = {"description": value}
            self.assertEqual(validate_action("register_agent", args)[1], [])
            await tool.run(args, context=context(backend))
            backend.register_agent.assert_awaited_once_with(description=value)
        for value in ("", " \t\n", "\u3000", "x" * 5001, "\U0001f600" * 2501,
                      " " + "x" * 5000, "\ud800", None):
            backend = AsyncMock()
            args = {"description": value}
            self.assertIn("description", validate_action("register_agent", args)[1])
            with self.subTest(length=len(value) if isinstance(value, str) else None), self.assertRaises(ToolError):
                await tool.run(args, context=context(backend))
            self.assertEqual(backend.mock_calls, [])

    async def test_nohtml_rejects_tags_scripts_and_handlers_before_backend(self):
        for description in ("<b>text</b>", "<!-- text -->", "<?xml text?>", "onclick=x",
                            "< script>bad</ script>"):
            backend = AsyncMock()
            args = {"description": description}
            self.assertIn("description", validate_action("register_agent", args)[1])
            with self.subTest(description=description), self.assertRaises(ToolError):
                await tool_contracts()["register_agent"].run(args, context=context(backend))
            self.assertEqual(backend.mock_calls, [])

    async def test_bad_registration_token_is_never_reported_as_success(self):
        for token in (None, True, 1, [], {}, "", " ", " token", "token ", "token\n", "token\x00",
                      "token\u0085", "한글", "x" * 4097):
            backend = AsyncMock()
            backend.register_agent.return_value = {"data": {"agentToken": token}}
            with self.subTest(type=type(token).__name__), self.assertRaisesRegex(ToolError, "backend_contract_error"):
                await tool_contracts()["register_agent"].run({"description": "description"}, context=context(backend))
            backend.register_agent.assert_awaited_once()
        for response in ({}, {"agent_token": "synthetic-one", "agentToken": "synthetic-two"},
                         {"agent_token": None, "agentToken": "synthetic-two"}):
            backend = AsyncMock()
            backend.register_agent.return_value = {"data": response}
            with self.assertRaisesRegex(ToolError, "backend_contract_error") as error:
                await tool_contracts()["register_agent"].run({"description": "description"}, context=context(backend))
            self.assertNotIn("synthetic-one", str(error.exception))
            self.assertNotIn("synthetic-two", str(error.exception))

    async def test_valid_tokens_and_matching_aliases_are_preserved_exactly(self):
        for token in ("noviis_agt_" + "a" * 32, "legacy-token", "abc+/==", "x" * 4096):
            for response in ({"agentToken": token}, {"agent_token": token}, {"agent_token": token, "agentToken": token}):
                backend = AsyncMock()
                backend.register_agent.return_value = {"data": response}
                result = await tool_contracts()["register_agent"].run({"description": "description"}, context=context(backend))
                self.assertEqual(result.agent_token, token)
