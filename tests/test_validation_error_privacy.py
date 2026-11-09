from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import httpx
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp.tools.base import Tool

import test_mcp_sessions as sessions
from tools.tool_contract import tool_contracts


SYNTHETIC_CREDENTIAL = "noviis_agt_synthetic_validation_credential"


def invalid_calls():
    return (
        ("get_post_comments", {"agent_token": SYNTHETIC_CREDENTIAL}),
        ("get_post_comments", {"post_id": 1, "agent_token": [SYNTHETIC_CREDENTIAL]}),
        ("get_post_comments", {"post_id": 1, "agent_token": {"nested": [SYNTHETIC_CREDENTIAL]}}),
        ("get_post_comments", {"post_id": {"private": SYNTHETIC_CREDENTIAL}, "agent_token": SYNTHETIC_CREDENTIAL}),
        ("mark_note_read", {"note_thread_id": 1, "observed_note_ids": [SYNTHETIC_CREDENTIAL],
                            "agent_token": SYNTHETIC_CREDENTIAL}),
        ("register_agent", {"description": {"nested": SYNTHETIC_CREDENTIAL}}),
    )


class ValidationErrorPrivacyTest(unittest.IsolatedAsyncioTestCase):
    async def test_direct_tool_run_hides_missing_type_and_custom_validator_inputs(self):
        client = AsyncMock()
        ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=SimpleNamespace(client=client)))
        for name, arguments in invalid_calls():
            with self.subTest(tool=name, fields=list(arguments)), self.assertRaises(ToolError) as caught:
                await tool_contracts()[name].run(arguments, context=ctx)
            message = str(caught.exception)
            self.assertNotIn(SYNTHETIC_CREDENTIAL, message)
            self.assertNotIn("input_value", message)
            self.assertNotIn("input_type", message)
            self.assertIn("validation error", message)
            self.assertNotIn(SYNTHETIC_CREDENTIAL, str(caught.exception.__cause__))
        self.assertEqual(client.mock_calls, [])

    def test_input_and_output_schemas_remain_identical_to_sdk_defaults(self):
        for name, protected in tool_contracts().items():
            baseline = Tool.from_function(protected.fn)
            with self.subTest(tool=name):
                self.assertEqual(protected.parameters, baseline.parameters)
                self.assertEqual(protected.fn_metadata.arg_model.model_json_schema(),
                                 baseline.fn_metadata.arg_model.model_json_schema())
                self.assertEqual(protected.fn_metadata.output_schema, baseline.fn_metadata.output_schema)
                self.assertFalse(baseline.fn_metadata.arg_model.model_config.get("hide_input_in_errors", False))

    async def test_successful_registration_still_returns_its_token_unchanged(self):
        client = AsyncMock()
        client.register_agent.return_value = {"data": {"agent_token": SYNTHETIC_CREDENTIAL}}
        ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=SimpleNamespace(client=client)))
        result = await tool_contracts()["register_agent"].run({"description": "synthetic registration"}, context=ctx)
        self.assertEqual(result.agent_token, SYNTHETIC_CREDENTIAL)
        self.assertIn(SYNTHETIC_CREDENTIAL, result.user_message)


class HTTPValidationErrorPrivacyTest(unittest.TestCase):
    def setUp(self):
        self.fixture = sessions.MCPSessionTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.headers = self.fixture.session()

    def test_actual_http_tool_errors_never_include_validation_inputs(self):
        for name, arguments in invalid_calls():
            with self.subTest(tool=name, fields=list(arguments)):
                result = self.fixture.rpc(self.headers, "tools/call", {"name": name, "arguments": arguments})
                self.assertTrue(result["isError"])
                message = result["content"][0]["text"]
                self.assertNotIn(SYNTHETIC_CREDENTIAL, message)
                self.assertNotIn("input_value", message)
                self.assertNotIn("input_type", message)
                self.assertIn("validation error", message)
        self.assertEqual(self.fixture.backend_requests, [])

    def test_actual_http_success_keeps_status_and_registration_results(self):
        result = self.fixture.rpc(self.headers, "tools/call", {
            "name": "get_agent_status", "arguments": {"agent_token": "valid"}})
        self.assertFalse(result.get("isError"))
        self.assertEqual(result["structuredContent"]["name"], "valid")

        original_backend = self.fixture.backend

        def backend(request):
            if request.url.path.endswith("/register"):
                return httpx.Response(200, json={"data": {"agent_token": SYNTHETIC_CREDENTIAL}})
            return original_backend(request)

        self.fixture.backend = backend
        result = self.fixture.rpc(self.headers, "tools/call", {
            "name": "register_agent", "arguments": {"description": "synthetic registration"}})
        self.assertFalse(result.get("isError"))
        self.assertEqual(result["structuredContent"]["agent_token"], SYNTHETIC_CREDENTIAL)
        self.assertIn(SYNTHETIC_CREDENTIAL, result["structuredContent"]["user_message"])


if __name__ == "__main__":
    unittest.main()
