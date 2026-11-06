import gzip
import unittest
from unittest.mock import patch

import httpx

from cache import clear_boards_cache, get_boards_cache, set_boards_cache
from client import NoviIsClient
from exceptions import BackendContractError, ServerError, NoviIsAPIError


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.reads = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    async def aclose(self):
        self.closed = True


class ResponseBudgetTest(unittest.IsolatedAsyncioTestCase):
    async def request(self, stream, *, limit=16, headers=None, status=200, method="GET", token=None):
        calls = []
        def backend(request):
            calls.append(request)
            return httpx.Response(status, headers=headers, stream=stream)
        async with httpx.AsyncClient(base_url="https://synthetic.invalid", transport=httpx.MockTransport(backend)) as http:
            api = NoviIsClient(client=http, internal_secret="synthetic")
            with patch("client.MCP_MAX_RESPONSE_BYTES", limit):
                try:
                    return await api.request_json(method, "/agents/feed", token=token)
                finally:
                    self.assertEqual(len(calls), 1)
                    self.assertTrue(stream.closed)

    async def test_chunked_oversize_response_stops_and_closes_without_reading_remainder(self):
        for method in ("GET", "POST"):
            stream = ChunkStream([b"x" * 10, b"x" * 10, b"not consumed"])
            with self.subTest(method=method), self.assertRaisesRegex(BackendContractError, "byte limit") as caught:
                await self.request(stream, method=method)
            self.assertEqual(stream.reads, 2)
            if method == "POST":
                self.assertIn("outcome is unconfirmed", str(caught.exception))

    async def test_content_length_cannot_bypass_decoded_body_limit(self):
        for headers in ({}, {"Content-Length": "1"}, {"Transfer-Encoding": "chunked"}):
            stream = ChunkStream([b"x" * 17])
            with self.subTest(headers=headers), self.assertRaises(BackendContractError):
                await self.request(stream, headers=headers)

    async def test_gzip_limit_counts_decompressed_bytes_and_preserves_valid_gzip(self):
        raw = b'{"message":"' + b'x' * 4000 + b'"}'
        packed = gzip.compress(raw)
        self.assertLess(len(packed), 128)
        with self.assertRaises(BackendContractError):
            await self.request(ChunkStream([packed]), limit=128, headers={"Content-Encoding": "gzip"})
        result = await self.request(ChunkStream([packed]), limit=len(raw), headers={"Content-Encoding": "gzip"})
        self.assertEqual(result["message"], "x" * 4000)

    async def test_exact_limit_and_advisory_headers_preserve_success(self):
        raw = b'{"data": {"id": 1}}'
        result = await self.request(ChunkStream([raw[:5], raw[5:]]), limit=len(raw),
                                    headers={"X-RateLimit-Remaining": "7"})
        self.assertEqual(result["data"], {"id": 1})
        self.assertEqual(result["_rate_limit"]["remaining"], "7")

    async def test_error_bodies_are_bounded_without_retrying(self):
        for status in (401, 429, 500, 503):
            with self.subTest(status=status), self.assertRaises(BackendContractError):
                await self.request(ChunkStream([b"x" * 17]), status=status)
        with self.assertRaises(NoviIsAPIError) as caught:
            await self.request(ChunkStream([b'{"message":"wait"}']), limit=64,
                               status=429, headers={"Retry-After": "3"})
        self.assertEqual(caught.exception.retry_after_seconds, 3)

    async def test_json_parse_cannot_return_success_after_local_or_enclosing_deadline(self):
        import anyio
        original_parse = NoviIsClient._parse_json
        for method in ("GET", "POST"):
            for enclosing in (False, True):
                clock = [100.0]
                stream = ChunkStream([b'{"data":{"value":7}}'])
                def slow_parse(response, **kwargs):
                    result = original_parse(response, **kwargs)
                    clock[0] = 106.0 if enclosing else 161.0
                    return result
                async with httpx.AsyncClient(base_url="https://synthetic.invalid", transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, stream=stream))) as http:
                    api = NoviIsClient(client=http, internal_secret="synthetic")
                    with patch("client.anyio.current_time", side_effect=lambda: clock[0]), \
                            patch("client.anyio.current_effective_deadline", return_value=105.0 if enclosing else float("inf")), \
                            patch.object(NoviIsClient, "_parse_json", side_effect=slow_parse):
                        with self.subTest(method=method, enclosing=enclosing), self.assertRaisesRegex(ServerError, "deadline exhausted") as caught:
                            await api.request_json(method, "/agents/posts")
                        if method == "POST":
                            self.assertIn("Write outcome is unknown", str(caught.exception))
                self.assertTrue(stream.closed)

    async def test_excessive_json_nesting_reports_contract_error(self):
        raw = b'[' * 2000 + b'0' + b']' * 2000
        with self.assertRaisesRegex(BackendContractError, "Expected a JSON"):
            await self.request(ChunkStream([raw]), limit=len(raw))

    async def test_empty_delete_response_retains_confirmation(self):
        result = await self.request(ChunkStream([]), method="DELETE", status=204)
        self.assertTrue(result["_http_no_content"])

    async def test_gzip_decoder_allocation_is_bounded_before_rejecting_expansion(self):
        import zlib
        original = zlib.decompressobj
        output_sizes = []
        class ObservedDecoder:
            def __init__(self, *args):
                self.inner = original(*args)
            def decompress(self, data, max_length=0):
                self_test.assertGreater(max_length, 0)
                result = self.inner.decompress(data, max_length)
                output_sizes.append(len(result))
                return result
            def __getattr__(self, name):
                return getattr(self.inner, name)
        self_test = self
        packed = gzip.compress(b'x' * (1024 * 1024))
        with patch('client.zlib.decompressobj', ObservedDecoder), self.assertRaises(BackendContractError):
            await self.request(ChunkStream([packed]), limit=128, headers={'Content-Encoding': 'gzip'})
        self.assertEqual(output_sizes, [129])

    async def test_access_failure_invalidates_cache_before_body_consumption(self):
        token = 'synthetic-cache-token'
        for status in (401, 403, 404):
            set_boards_cache([{'id': 7}], agent_token=token)
            with self.subTest(status=status), self.assertRaises(BackendContractError):
                await self.request(ChunkStream([b'x' * 17]), status=status, token=token)
            self.assertIsNone(get_boards_cache(token))
        class ObservedStream(ChunkStream):
            async def __aiter__(self):
                self_test.assertIsNone(get_boards_cache(token))
                async for chunk in super().__aiter__():
                    yield chunk
        self_test = self
        set_boards_cache([{'id': 7}], agent_token=token)
        try:
            with self.assertRaises(BackendContractError):
                await self.request(ObservedStream([b'bad gzip']), status=401, token=token,
                                   headers={'Content-Encoding': 'gzip'})
        finally:
            clear_boards_cache(token)

    async def test_gzip_chunks_members_and_invalid_encoding(self):
        headers = {'Content-Encoding': 'gzip'}
        packed = gzip.compress(b'{"data":') + gzip.compress(b'7}')
        result = await self.request(ChunkStream([packed[i:i+3] for i in range(0, len(packed), 3)]),
                                    headers=headers, limit=64)
        self.assertEqual(result['data'], 7)
        for wire, encoding in ((packed[:-4], 'gzip'), (b'bad gzip', 'gzip'), (b'{}', 'br')):
            with self.subTest(encoding=encoding, wire=wire), self.assertRaises(BackendContractError):
                await self.request(ChunkStream([wire]), headers={'Content-Encoding': encoding}, limit=64)
