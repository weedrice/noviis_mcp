from __future__ import annotations

from contextlib import contextmanager
import io
import logging
import unittest
from unittest.mock import patch

import httpx

import test_mcp_sessions as sessions
from logging_utils import JsonFormatter, PlainFormatter, SensitiveDataFilter


SYNTHETIC_CREDENTIAL = "opaque-private-method-validation-value"


class RPCMethodLogPrivacyTest(unittest.TestCase):
    def setUp(self):
        self.fixture = sessions.MCPSessionTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.headers = self.fixture.session()

    @contextmanager
    def capture(self, formatter):
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        handler.addFilter(SensitiveDataFilter())
        handler.setFormatter(formatter)
        previous_disable = logging.root.manager.disable
        with patch.object(logging.getLogger(), "handlers", [handler]), \
                patch.object(logging.getLogger(), "level", logging.DEBUG):
            logging.disable(logging.NOTSET)
            try:
                yield output
            finally:
                logging.disable(previous_disable)
                handler.close()

    def post(self, payload):
        response = self.fixture.http.post("/mcp", headers=self.headers, json=payload)
        # Notifications return 202 before SDK validation; observe completed
        # per-request cleanup before inspecting their asynchronously written log.
        self.assertTrue(self.fixture.client_closed[-1].wait(timeout=5))
        return response

    def test_method_validation_warning_and_debug_logs_hide_all_inputs(self):
        cases = (
            {"name": "get_agent_status", "arguments": [SYNTHETIC_CREDENTIAL]},
            {"name": {"private": SYNTHETIC_CREDENTIAL}, "arguments": {}},
            {"name": "get_agent_status", "arguments": {},
             "_meta": {"progressToken": [SYNTHETIC_CREDENTIAL]}},
        )
        for formatter in (JsonFormatter(), PlainFormatter("%(levelname)s %(message)s")):
            for index, params in enumerate(cases):
                with self.subTest(format=type(formatter).__name__, case=index), self.capture(formatter) as output:
                    response = self.post({"jsonrpc": "2.0", "id": 10, "method": "tools/call", "params": params})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()["error"]["code"], -32602)
                    self.assertNotIn(SYNTHETIC_CREDENTIAL, response.text)
                    log = output.getvalue()
                    self.assertIn("Failed to validate request: input omitted.", log)
                    self.assertIn("Message that failed validation: input omitted.", log)
                    self.assertNotIn(SYNTHETIC_CREDENTIAL, log)
                    self.assertNotIn("input_value", log)
        self.assertEqual(self.fixture.backend_requests, [])

    def test_notification_validation_logs_hide_exception_and_raw_message(self):
        cases = (
            {"method": "notifications/progress", "params": {
                "progressToken": [SYNTHETIC_CREDENTIAL], "progress": 1}},
            {"method": "notifications/cancelled", "params": {
                "requestId": {"private": SYNTHETIC_CREDENTIAL}}},
        )
        for formatter in (JsonFormatter(), PlainFormatter("%(levelname)s %(message)s")):
            for index, payload in enumerate(cases):
                with self.subTest(format=type(formatter).__name__, case=index), self.capture(formatter) as output:
                    response = self.post({"jsonrpc": "2.0", **payload})
                    self.assertEqual(response.status_code, 202)
                    self.assertEqual(response.content, b"")
                    log = output.getvalue()
                    self.assertIn("Failed to validate notification: input omitted.", log)
                    self.assertNotIn(SYNTHETIC_CREDENTIAL, log)
                    self.assertNotIn("input_value", log)
        self.assertEqual(self.fixture.backend_requests, [])

    def test_successful_results_registration_and_normal_diagnostics_are_preserved(self):
        original_backend = self.fixture.backend

        def backend(request):
            if request.url.path.endswith("/register"):
                return httpx.Response(200, json={"data": {"agent_token": SYNTHETIC_CREDENTIAL}})
            return original_backend(request)

        self.fixture.backend = backend
        with self.capture(JsonFormatter()) as output:
            logging.warning("ordinary runtime diagnostic")
            response = self.post({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "get_agent_status", "arguments": {"agent_token": "valid"}}})
            self.assertEqual(response.json()["result"]["structuredContent"]["name"], "valid")
            response = self.post({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "register_agent", "arguments": {"description": "synthetic registration"}}})
            self.assertEqual(response.json()["result"]["structuredContent"]["agent_token"], SYNTHETIC_CREDENTIAL)
            self.assertIn("ordinary runtime diagnostic", output.getvalue())
            self.assertIn("noviis_api_response", output.getvalue())

    def test_source_adapter_does_not_replace_unrelated_messages(self):
        filter_ = SensitiveDataFilter()
        for pathname, function, message in (
            ("application/session.py", "_receive_loop", "Failed to validate request: ordinary diagnostic"),
            ("/site-packages/mcp/shared/session.py", "other_function", "Failed to validate request: ordinary diagnostic"),
            ("/site-packages/mcp/shared/session.py", "_receive_loop", "Read stream closed by client"),
        ):
            with self.subTest(pathname=pathname, function=function):
                record = logging.LogRecord("root", logging.WARNING, pathname, 1, message, (), None, function)
                filter_.filter(record)
                self.assertEqual(record.getMessage(), message)


if __name__ == "__main__":
    unittest.main()
