from __future__ import annotations

import unittest

import httpx

from client import NoviIsClient
from exceptions import BackendContractError, PermissionDenied, Unauthorized
from tools.response_contract import require_response_id


class ResponseContractTest(unittest.IsolatedAsyncioTestCase):
    async def request(self, response, method="POST"):
        calls = []

        def handler(request):
            calls.append(request)
            return response

        async with httpx.AsyncClient(base_url="https://noviis.test", transport=httpx.MockTransport(handler)) as http:
            client = NoviIsClient(client=http, internal_secret="test-secret")
            try:
                return await client.request_json(method, "/agents/posts", token="test-token")
            finally:
                self.assertEqual(len(calls), 1)

    async def test_malformed_success_responses_fail_without_retry(self):
        for body in (b"<html>proxy</html>", b'{"data":', b"", b"null", b'"ok"', b"true", b"1.5", b'{"success":false}'):
            with self.subTest(body=body):
                with self.assertRaisesRegex(BackendContractError, "outcome is unconfirmed"):
                    await self.request(httpx.Response(200, content=body))

    async def test_existing_list_and_numeric_contracts_are_preserved(self):
        for value in ([{"boardId": 1}], 5, 0):
            with self.subTest(value=value):
                result = await self.request(httpx.Response(200, json=value), "GET")
                self.assertEqual(result["result"], value)

    async def test_empty_delete_204_is_valid_but_empty_create_is_not(self):
        result = await self.request(httpx.Response(204), "DELETE")
        self.assertEqual({key: value for key, value in result.items() if key != "_rate_limit"}, {})
        with self.assertRaises(BackendContractError):
            await self.request(httpx.Response(204))

    async def test_auth_errors_remain_auth_errors_even_with_html(self):
        for status, error in ((401, Unauthorized), (403, PermissionDenied)):
            with self.subTest(status=status), self.assertRaises(error):
                await self.request(httpx.Response(status, text="<html>denied</html>"))


class ResponseIdTest(unittest.TestCase):
    def test_missing_and_invalid_ids_never_report_success(self):
        for value in (None, "", " ", 0, -1, "0", "-1", True, False, 1.5, [], {}, "null", "None"):
            with self.subTest(value=value), self.assertRaises(BackendContractError):
                require_response_id({"postId": value}, "postId", "id")

    def test_numeric_opaque_and_alternate_ids_are_supported(self):
        for value in (1, "123", "thread-1"):
            with self.subTest(value=value):
                self.assertEqual(require_response_id({"id": value}, "postId", "id"), str(value))


if __name__ == "__main__":
    unittest.main()
