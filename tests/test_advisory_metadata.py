from types import SimpleNamespace
import unittest

import httpx

from client import NoviIsClient
from tools.metadata import build_rate_limit_info
from tools.tool_contract import tool_contracts


class AdvisoryMetadataTest(unittest.IsolatedAsyncioTestCase):
    async def test_bad_optional_headers_do_not_hide_registration_token_or_retry_registration(self):
        for value in ("1e309", "inf", "NaN", "-1", "invalid", "9223372036854775808"):
            calls = []

            def backend(request):
                calls.append(request)
                return httpx.Response(200, json={"data": {"agent_token": "synthetic-registration-token"}},
                                      headers={"X-RateLimit-Limit": value, "X-RateLimit-Remaining": "0"})

            async with httpx.AsyncClient(base_url="https://synthetic.invalid/api/v1", transport=httpx.MockTransport(backend)) as http:
                client = NoviIsClient(client=http, internal_secret="synthetic")
                ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=SimpleNamespace(client=client)))
                with self.subTest(value=value):
                    result = await tool_contracts()["register_agent"].run(
                        {"name": "synthetic", "description": "local test"}, context=ctx)
                    self.assertEqual(result.agent_token, "synthetic-registration-token")
                    self.assertIn(result.agent_token, result.user_message)
                    self.assertIsNone(result.rate_limit.limit)
                    self.assertEqual(result.rate_limit.remaining, 0)
                    self.assertEqual(len(calls), 1)

    def test_header_values_are_finite_nonnegative_integers_without_float_rounding(self):
        for value in (True, -1, 1.5, float("inf"), float("nan"), {}, [], "1_000", "+1", "1.5", "9" * 5000):
            with self.subTest(value_type=type(value).__name__):
                result = build_rate_limit_info({"_rate_limit": {key: value for key in ("limit", "remaining", "reset", "retry_after")}})
                self.assertTrue(all(getattr(result, key) is None for key in ("limit", "remaining", "reset", "retry_after")))
        result = build_rate_limit_info({"_rate_limit": {"limit": "9223372036854775807", "remaining": " 0 ", "reset": "1779270000", "retry_after": 12}})
        self.assertEqual((result.limit, result.remaining, result.reset, result.retry_after),
                         (9223372036854775807, 0, 1779270000, 12))

    def test_absent_optional_metadata_remains_absent(self):
        for payload in ({}, {"_rate_limit": None}, {"_rate_limit": {}}, {"_rate_limit": {"limit": None}}):
            self.assertIsNone(build_rate_limit_info(payload))
