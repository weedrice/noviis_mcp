from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

import anyio
import httpx

from client import NoviIsClient
from exceptions import NoviIsAPIError, ServerError


class RetryDeadlineTest(unittest.IsolatedAsyncioTestCase):
    async def test_every_429_returns_immediately_without_automatic_replay(self):
        for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            for kind in ("coded", "uncoded", "proxy"):
                for retry_after in ("0", "90"):
                    with self.subTest(method=method, kind=kind, retry_after=retry_after):
                        seen = []

                        def handler(request):
                            seen.append(request)
                            headers = {"Retry-After": retry_after}
                            if kind == "proxy":
                                return httpx.Response(429, headers=headers, text="<html>Rate limited</html>")
                            body = {"message": "Slow down"}
                            if kind == "coded":
                                body["code"] = "quota_limited"
                            return httpx.Response(429, headers=headers, json=body)

                        async with httpx.AsyncClient(base_url="https://synthetic.invalid", transport=httpx.MockTransport(handler)) as http:
                            client = NoviIsClient(client=http, internal_secret="synthetic")
                            with patch("client.asyncio.sleep", new_callable=AsyncMock) as sleep:
                                with self.assertRaises(NoviIsAPIError) as failure:
                                    await client.request_json(method, "/limited")
                                sleep.assert_not_awaited()
                        self.assertEqual(len(seen), 1)
                        self.assertEqual(failure.exception.code, "quota_limited" if kind == "coded" else "rate_limited")
                        self.assertEqual(failure.exception.retry_after_seconds, int(retry_after))
                        self.assertEqual(failure.exception.details["retry_after_seconds"], int(retry_after))

    async def test_uncoded_429_preserves_body_delay_when_header_is_absent(self):
        response = httpx.Response(429, json={"message": "Slow down", "details": {"retryAfterSeconds": 0}})
        async with httpx.AsyncClient(base_url="https://synthetic.invalid", transport=httpx.MockTransport(lambda _: response)) as http:
            client = NoviIsClient(client=http, internal_secret="synthetic")
            with self.assertRaises(NoviIsAPIError) as failure:
                await client.request_json("POST", "/limited")
        self.assertEqual(failure.exception.code, "rate_limited")
        self.assertEqual(failure.exception.retry_after_seconds, 0)

    async def test_safe_read_skips_retry_when_local_deadline_cannot_cover_backoff(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(503, json={"message": "Backend unavailable"})

        async with httpx.AsyncClient(base_url="https://synthetic.invalid", transport=httpx.MockTransport(handler)) as http:
            client = NoviIsClient(client=http, internal_secret="synthetic")
            with patch("client.MCP_REQUEST_TIMEOUT_SECONDS", 15), patch("client.asyncio.sleep", new_callable=AsyncMock) as sleep:
                with self.assertRaisesRegex(ServerError, "insufficient request deadline budget"):
                    await client.request_json("GET", "/feed")
                sleep.assert_not_awaited()
        self.assertEqual(len(seen), 1)

    async def test_enclosing_deadline_takes_precedence_over_local_budget(self):
        response = httpx.Response(503, json={"message": "Backend unavailable"})
        async with httpx.AsyncClient(base_url="https://synthetic.invalid", transport=httpx.MockTransport(lambda _: response)) as http:
            client = NoviIsClient(client=http, internal_secret="synthetic")
            with anyio.CancelScope(deadline=anyio.current_time() + 5):
                with patch("client.MCP_REQUEST_TIMEOUT_SECONDS", 120), patch("client.asyncio.sleep", new_callable=AsyncMock) as sleep:
                    with self.assertRaisesRegex(ServerError, "insufficient request deadline budget"):
                        await client.request_json("GET", "/feed")
                    sleep.assert_not_awaited()

    async def test_retry_is_not_sent_if_delay_exhausts_remaining_budget(self):
        clock = [100.0]
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(503, json={"message": "Backend unavailable"})

        async def delayed_sleep(_):
            clock[0] = 161.0

        async with httpx.AsyncClient(base_url="https://synthetic.invalid", transport=httpx.MockTransport(handler)) as http:
            client = NoviIsClient(client=http, internal_secret="synthetic")
            with patch("client.anyio.current_time", side_effect=lambda: clock[0]), patch("client.asyncio.sleep", side_effect=delayed_sleep):
                with self.assertRaisesRegex(ServerError, "no further request was sent"):
                    await client.request_json("GET", "/feed")
        self.assertEqual(len(seen), 1)

    async def test_local_deadline_bounds_total_http_time_and_preserves_unknown_write_outcome(self):
        seen = []

        async def handler(request):
            seen.append(request)
            await anyio.sleep(5)
            return httpx.Response(200, json={"data": {}})

        async with httpx.AsyncClient(base_url="https://synthetic.invalid", transport=httpx.MockTransport(handler)) as http:
            client = NoviIsClient(client=http, internal_secret="synthetic")
            with patch("client.MCP_REQUEST_TIMEOUT_SECONDS", 0.02):
                with self.assertRaisesRegex(ServerError, "Write outcome is unknown"):
                    await client.request_json("POST", "/posts")
        self.assertEqual(len(seen), 1)

    async def test_transport_error_does_not_replay_or_hide_unknown_write_outcome(self):
        seen = []

        def handler(request):
            seen.append(request)
            raise httpx.ReadTimeout("synthetic lost response")

        async with httpx.AsyncClient(base_url="https://synthetic.invalid", transport=httpx.MockTransport(handler)) as http:
            client = NoviIsClient(client=http, internal_secret="synthetic")
            with patch("client.asyncio.sleep", new_callable=AsyncMock) as sleep:
                with self.assertRaisesRegex(ServerError, "Write outcome is unknown"):
                    await client.request_json("POST", "/posts")
                sleep.assert_not_awaited()
        self.assertEqual(len(seen), 1)
