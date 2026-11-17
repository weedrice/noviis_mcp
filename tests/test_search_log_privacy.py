import io
import json
import logging
import unittest
from urllib.parse import quote_plus

import httpx

from client import NoviIsClient
from logging_utils import JsonFormatter, PlainFormatter, SensitiveDataFilter, safe_log_params


class SearchLogPrivacyTest(unittest.IsolatedAsyncioTestCase):
    async def test_real_search_logs_omit_query_in_both_formats(self):
        query = "private_marker + & = 한글"
        for formatter in (JsonFormatter(), PlainFormatter("%(message)s")):
            for use_filter in (True, False):
                with self.subTest(formatter=type(formatter).__name__, use_filter=use_filter):
                    output = io.StringIO()
                    handler = logging.StreamHandler(output)
                    handler.setFormatter(formatter)
                    if use_filter:
                        handler.addFilter(SensitiveDataFilter())
                    root = logging.getLogger()
                    old_level = root.level
                    old_handlers = root.handlers[:]
                    root.setLevel(logging.INFO)
                    root.handlers = [handler]
                    seen = []

                    def backend(request):
                        seen.append(request)
                        return httpx.Response(200, json={"data": {"content": []}})

                    try:
                        async with httpx.AsyncClient(base_url="https://mock.invalid", transport=httpx.MockTransport(backend)) as http:
                            await NoviIsClient(client=http, internal_secret="synthetic").search_semantic(
                                query=query, page=2, size=10, content_type="POST")
                    finally:
                        root.handlers = old_handlers
                        root.setLevel(old_level)
                    self.assertEqual(seen[0].url.params["q"], query)
                    logs = output.getvalue()
                    for value in (query, quote_plus(query), "private_marker"):
                        self.assertNotIn(value, logs)
                    self.assertIn("httpx_request", logs)
                    self.assertIn('"page": 2', logs)
                    self.assertIn('"size": 10', logs)

    def test_only_safe_typed_query_metadata_is_logged(self):
        self.assertEqual(safe_log_params({"q": "private", "boardUrl": "private", "page": 2,
                                         "size": True, "limit": "private", "contentType": "private"}), {"page": 2})

    def test_wire_headers_reason_phrases_and_exceptions_are_not_rendered(self):
        for name in ("httpx", "httpcore.http11"):
            for formatter in (JsonFormatter(), PlainFormatter("%(message)s")):
                record = logging.LogRecord(name, logging.DEBUG, __file__, 1,
                                           "headers %s", ({"echo": "private_marker"},), None)
                record.exc_text = "private_marker"
                record.stack_info = "private_marker"
                self.assertNotIn("private_marker", formatter.format(record))
