from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import httpx
from mcp.types import JSONRPCMessage
from pydantic import ValidationError

import test_mcp_sessions as sessions


SYNTHETIC_CREDENTIAL = "synthetic-envelope-private-value"


class RPCEnvelopePrivacyTest(unittest.TestCase):
    def setUp(self):
        self.fixture = sessions.MCPSessionTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.headers = self.fixture.session()
        self.body_limiter = self.fixture.http.app.middleware_stack.app.app

    def post(self, payload, **kwargs):
        return self.fixture.http.post("/mcp", headers=self.headers, json=payload, **kwargs)

    def test_invalid_envelopes_hide_nested_inputs_before_sdk_runtime_allocation(self):
        credential = {"agent_token": SYNTHETIC_CREDENTIAL,
                      "nested": {"credentials": [SYNTHETIC_CREDENTIAL]}}
        cases = (
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": [credential]},
            {"jsonrpc": "2.0", "id": 1, "method": credential, "params": {}},
            {"jsonrpc": "2.0", "id": 1, "result": [credential]},
            {"params": {"arguments": credential}},
            [credential],
        )
        clients_before = len(self.fixture.clients)
        for index, payload in enumerate(cases):
            with self.subTest(case=index):
                with self.assertRaises(ValidationError):
                    JSONRPCMessage.model_validate(payload)
                response = self.post(payload)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json(), {
                    "jsonrpc": "2.0", "id": "server-error",
                    "error": {"code": -32602, "message": "Invalid JSON-RPC message."},
                })
                self.assertNotIn(SYNTHETIC_CREDENTIAL, response.text)
                self.assertNotIn("input_value", response.text)
        self.assertEqual(len(self.fixture.clients), clients_before)
        self.assertEqual(self.fixture.backend_requests, [])

    def test_security_media_routing_and_method_statuses_match_sdk(self):
        invalid = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": []}
        cases = (
            ("POST", "/mcp", {"Content-Type": "text/plain"}, 400),
            ("POST", "/mcp", {"Content-Type": "application/json-other"}, 415),
            ("POST", "/mcp", {"Accept": "text/plain"}, 406),
            ("POST", "/mcp", {"Host": "untrusted.invalid"}, 421),
            ("POST", "/mcp", {"Origin": "https://untrusted.invalid"}, 403),
            ("POST", "/missing", {}, 404),
            ("POST", "/mcp/", {}, 307),
            ("PUT", "/mcp", {}, 405),
            ("PATCH", "/mcp", {}, 405),
        )
        for method, path, overrides, expected in cases:
            with self.subTest(method=method, path=path, headers=overrides):
                headers = {**self.headers, "Content-Type": "application/json", **overrides}
                kwargs = dict(headers=headers, content=json.dumps(invalid), follow_redirects=False)
                response = self.fixture.http.request(method, path, **kwargs)
                # Disable only our adapter to compare the installed SDK's policy.
                with patch.object(self.body_limiter, "rpc_path", None):
                    baseline = self.fixture.http.request(method, path, **kwargs)
                self.assertEqual(response.status_code, expected)
                self.assertEqual(response.status_code, baseline.status_code)
                self.assertEqual(response.content, baseline.content)
        self.assertEqual(self.fixture.backend_requests, [])

    def test_root_path_uses_the_same_endpoint_as_the_sdk_router(self):
        invalid = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": [{"agent_token": SYNTHETIC_CREDENTIAL}]}
        with patch.object(self.fixture.http._transport, "root_path", "/gateway"):
            # ASGI servers may provide either the prefixed or stripped path.
            for path in ("/gateway/mcp", "/mcp"):
                with self.subTest(path=path):
                    response = self.fixture.http.post(path, headers=self.headers, json=invalid)
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.json()["error"]["message"], "Invalid JSON-RPC message.")
                    self.assertNotIn(SYNTHETIC_CREDENTIAL, response.text)
                    valid = self.fixture.http.post(path, headers=self.headers, json={
                        "jsonrpc": "2.0", "id": 1, "method": "ping"})
                    self.assertEqual(valid.status_code, 200)

    def test_json_syntax_encoding_and_protocol_errors_keep_sdk_behavior(self):
        for body in (b"{", b"\xff"):
            with self.subTest(body=body):
                headers = {**self.headers, "Content-Type": "application/json"}
                response = self.fixture.http.post("/mcp", headers=headers, content=body)
                with patch.object(self.body_limiter, "rpc_path", None):
                    baseline = self.fixture.http.post("/mcp", headers=headers, content=body)
                self.assertEqual(response.status_code, baseline.status_code)
                self.assertEqual(response.content, baseline.content)
        headers = {**self.headers, "mcp-protocol-version": "unsupported"}
        payload = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        response = self.fixture.http.post("/mcp", headers=headers, json=payload)
        with patch.object(self.body_limiter, "rpc_path", None):
            baseline = self.fixture.http.post("/mcp", headers=headers, json=payload)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.content, baseline.content)

    def test_valid_notifications_responses_and_initialization_are_accepted(self):
        for payload in (
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 1, "result": {"answer": "ok"}},
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -32603, "message": "synthetic"}},
        ):
            with self.subTest(kind=next(key for key in ("method", "result", "error") if key in payload)):
                response = self.post(payload)
                self.assertEqual(response.status_code, 202)
                self.assertEqual(response.content, b"")
        self.fixture.session()  # Actual initialize + initialized notification exchange.
        self.assertEqual(self.post({"jsonrpc": "2.0", "id": 2, "method": "ping"}).status_code, 200)

    def test_body_limit_still_precedes_envelope_validation(self):
        with patch.object(self.body_limiter, "max_body_bytes", 8):
            response = self.post({"params": [{"agent_token": SYNTHETIC_CREDENTIAL}]})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json(), {"error": "request_too_large"})

    def test_successful_tools_and_registration_keep_results(self):
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


if __name__ == "__main__":
    unittest.main()
