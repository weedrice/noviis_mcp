from copy import deepcopy
import json
import re
import traceback
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import quote

import httpx
from mcp.server.fastmcp.exceptions import ToolError

from client import NoviIsClient
from exceptions import AgentSuspended, NoviIsAPIError, PermissionDenied, ServerError, Unauthorized
from logging_utils import sanitize_error_data
from tools.tool_contract import tool_contracts


SECRET = "synthetic-internal+/private"
TOKEN = "synthetic-agent+/private"
MESSAGE = f"Proxy diagnosis {SECRET}; Authorization: Bearer {TOKEN}"


class ErrorRedactionTest(unittest.IsolatedAsyncioTestCase):
    def assert_redacted(self, value):
        text = str(value)
        for secret in (SECRET, TOKEN, quote(SECRET, safe=""), quote(TOKEN, safe="")):
            self.assertNotIn(secret, text)

    async def test_typed_http_errors_preserve_recovery_fields_without_credentials(self):
        for status in (400, 403, 409, 422, 429):
            payload = {"error": {"code": "quota_denied", "message": MESSAGE,
                "details": {"notes_remaining": 0, "can_send_note": False,
                            "reset_at": "2026-11-05T15:00:00Z", "debug": [SECRET, {"extra": TOKEN}],
                            "authorization": "other-private-value"}}}
            original = deepcopy(payload)
            def handler(request):
                return httpx.Response(status, json=payload, headers={"Retry-After": "7"})
            async with httpx.AsyncClient(base_url="https://synthetic.invalid", transport=httpx.MockTransport(handler)) as http:
                client = NoviIsClient(client=http, internal_secret=SECRET)
                with self.subTest(status=status), self.assertRaises(NoviIsAPIError) as caught:
                    await client.request_json("POST", "/agents/notes", token=TOKEN)
            error = caught.exception
            self.assert_redacted(str(error))
            self.assert_redacted(error.details)
            self.assertNotIn("other-private-value", str(error.details))
            self.assertEqual(error.code, "quota_denied")
            self.assertEqual(error.details["notes_remaining"], 0)
            self.assertIs(error.details["can_send_note"], False)
            self.assertEqual(error.details["reset_at"], "2026-11-05T15:00:00Z")
            if status == 429:
                self.assertEqual(error.retry_after_seconds, 7)
            self.assertEqual(payload, original)

    async def test_unauthorized_suspended_and_server_error_paths_are_sanitized(self):
        for status, payload, kind in (
            (401, {"message": MESSAGE}, Unauthorized),
            (403, {"status": "suspended", "message": MESSAGE}, AgentSuspended),
            (500, {"message": MESSAGE}, ServerError),
        ):
            async with httpx.AsyncClient(base_url="https://synthetic.invalid", transport=httpx.MockTransport(
                lambda request: httpx.Response(status, json=payload))) as http:
                client = NoviIsClient(client=http, internal_secret=SECRET)
                with self.assertRaises(kind) as caught:
                    await client.request_json("POST", "/agents/posts", token=TOKEN)
            self.assert_redacted(caught.exception)
            if status == 500:
                self.assertIn("outcome is unknown", str(caught.exception))

    async def test_non_json_and_short_read_budget_errors_are_sanitized(self):
        text = f"Proxy error {SECRET} {quote(TOKEN, safe='')}"
        async with httpx.AsyncClient(base_url="https://synthetic.invalid", transport=httpx.MockTransport(
            lambda request: httpx.Response(503, text=text))) as http:
            client = NoviIsClient(client=http, internal_secret=SECRET)
            with patch("client.MCP_REQUEST_TIMEOUT_SECONDS", 1), self.assertRaises(ServerError) as caught:
                await client.request_json("GET", "/agents/status", token=TOKEN)
        self.assert_redacted(caught.exception)
        self.assertIn("insufficient request deadline budget", str(caught.exception))

    async def test_transport_and_uncoded_status_error_tracebacks_hide_credentials(self):
        def transport_failure(request):
            raise httpx.ReadError(MESSAGE, request=request)
        for handler in (transport_failure, lambda request: httpx.Response(400, text="bad request")):
            async with httpx.AsyncClient(base_url="https://synthetic.invalid", transport=httpx.MockTransport(handler)) as http:
                client = NoviIsClient(client=http, internal_secret=SECRET)
                try:
                    await client.request_json("POST", "/agents/posts?echo=" + quote(TOKEN, safe=""), token=TOKEN)
                except (ServerError, httpx.HTTPStatusError) as error:
                    self.assert_redacted("".join(traceback.format_exception(error)))
                else:
                    self.fail("Expected a sanitized error")

    async def test_sdk_tool_error_does_not_expose_server_or_agent_credentials(self):
        async with httpx.AsyncClient(base_url="https://synthetic.invalid", transport=httpx.MockTransport(
            lambda request: httpx.Response(403, json={"error": {"code": "proxy_debug_error", "message": MESSAGE}}))) as http:
            api = NoviIsClient(client=http, internal_secret=SECRET)
            ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=SimpleNamespace(client=api)))
            with self.assertRaises(ToolError) as caught:
                await tool_contracts()["get_agent_status"].run({"agent_token": TOKEN}, context=ctx)
            self.assert_redacted(caught.exception)

    async def test_successful_registration_token_is_not_redacted(self):
        async with httpx.AsyncClient(base_url="https://synthetic.invalid", transport=httpx.MockTransport(
            lambda request: httpx.Response(201, json={"agentToken": TOKEN}))) as http:
            api = NoviIsClient(client=http, internal_secret=SECRET)
            ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=SimpleNamespace(client=api)))
            result = await tool_contracts()["register_agent"].run({"description": "sample agent"}, context=ctx)
            self.assertEqual(result.agent_token, TOKEN)
            self.assertIn(TOKEN, result.user_message)

    def test_sanitizer_handles_structured_keys_and_escaped_values_without_mutation(self):
        source = {"debug": [SECRET, quote(SECRET, safe=""), TOKEN],
                  "nested": {"api_key": "unrelated-key", SECRET: TOKEN}, "count": 7}
        original = deepcopy(source)
        result = sanitize_error_data(source, known_secrets=(SECRET, TOKEN))
        self.assert_redacted(result)
        self.assertNotIn("unrelated-key", str(result))
        self.assertEqual(result["count"], 7)
        self.assertEqual(source, original)
        self.assertNotIn("unlabeled-secret", sanitize_error_data(
            "unlabeled-secret", known_secrets=("unlabeled-secret",)))

    async def test_fallback_body_masks_solidus_and_mixed_case_percent_escapes(self):
        escaped_values = ["".join("\\u%04x" % ord(char) for char in SECRET), SECRET.replace("/", "\\/"),
                          quote(SECRET, safe="").replace("%2B", "%2b"),
                          quote(SECRET, safe="").replace("%2F", "%2f"),
                          re.sub(r"%[0-9A-F]{2}", lambda match: match[0].lower(), quote(TOKEN, safe=""))]
        for escaped in escaped_values:
            for status in (401, 403, 500):
                async with httpx.AsyncClient(base_url="https://synthetic.invalid", transport=httpx.MockTransport(
                    lambda request: httpx.Response(status, text='{"debug":"' + escaped + '"}'))) as http:
                    api = NoviIsClient(client=http, internal_secret=SECRET)
                    with self.subTest(status=status, escaped=escaped), self.assertRaises((Unauthorized, ServerError, PermissionDenied)) as caught:
                        await api.request_json("POST", "/agents/posts", token=TOKEN)
                self.assertNotIn(escaped, str(caught.exception))
                self.assert_redacted(caught.exception)
