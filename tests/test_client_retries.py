from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

import httpx

from client import NoviIsClient
from exceptions import ServerError


class RetrySafetyTest(unittest.IsolatedAsyncioTestCase):
    async def test_writes_are_not_replayed_after_server_error(self):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                seen = []

                def handler(request):
                    seen.append(request)
                    return httpx.Response(500 if len(seen) == 1 else 200, json={"message": "failed"})

                async with httpx.AsyncClient(base_url="https://noviis.test/api/v1", transport=httpx.MockTransport(handler)) as http:
                    client = NoviIsClient(client=http, internal_secret="test-secret")
                    with patch("client.asyncio.sleep", new_callable=AsyncMock) as sleep:
                        with self.assertRaisesRegex(ServerError, "outcome is unknown"):
                            await client.request_json(method, "/agents/posts", token="test-token", json_body={"content": "sample"})
                        sleep.assert_not_awaited()
                self.assertEqual(len(seen), 1)

    async def test_get_retries_one_server_error_then_succeeds(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(503 if len(seen) == 1 else 200, json={"data": []})

        async with httpx.AsyncClient(base_url="https://noviis.test/api/v1", transport=httpx.MockTransport(handler)) as http:
            client = NoviIsClient(client=http, internal_secret="test-secret")
            with patch("client.asyncio.sleep", new_callable=AsyncMock) as sleep:
                result = await client.request_json("GET", "/agents/feed")
                sleep.assert_awaited_once()
        self.assertEqual(result["data"], [])
        self.assertEqual(len(seen), 2)

    async def test_get_stops_after_second_server_error(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(503, json={"message": "failed"})

        async with httpx.AsyncClient(base_url="https://noviis.test/api/v1", transport=httpx.MockTransport(handler)) as http:
            client = NoviIsClient(client=http, internal_secret="test-secret")
            with patch("client.asyncio.sleep", new_callable=AsyncMock):
                with self.assertRaises(ServerError):
                    await client.request_json("GET", "/agents/feed")
        self.assertEqual(len(seen), 2)



if __name__ == "__main__":
    unittest.main()
