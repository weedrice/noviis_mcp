from __future__ import annotations

import json
import logging
import sys
import unittest

from logging_utils import JsonFormatter, PlainFormatter, SensitiveDataFilter


class LoggingSafetyTest(unittest.TestCase):
    def record(self, message="failed", args=(), exc_info=None):
        return logging.LogRecord("test", logging.ERROR, __file__, 1, message, args, exc_info)

    def test_top_level_and_nested_secret_fields_are_masked(self):
        record = self.record()
        for key in ("agent_token", "secret", "password", "X-NoviIs-Internal-Secret", "Authorization"):
            setattr(record, key, "TEST_SENSITIVE_VALUE")
        record.context = {"headers": {"Authorization": "TEST_SENSITIVE_VALUE"}, "items": [{"api_key": "TEST_SENSITIVE_VALUE"}], "count": 7}
        SensitiveDataFilter().filter(record)
        for formatter in (JsonFormatter(), PlainFormatter("%(message)s")):
            output = formatter.format(record)
            self.assertNotIn("TEST_SENSITIVE_VALUE", output)
            self.assertIn("7", output)

    def test_chained_exceptions_and_cached_tracebacks_are_masked_in_both_formats(self):
        try:
            try:
                raise ValueError("noviis_agt_TEST_token-with-dashes")
            except ValueError as exc:
                raise RuntimeError("password='TEST PRIVATE VALUE'") from exc
        except RuntimeError:
            record = self.record(exc_info=sys.exc_info())
        SensitiveDataFilter().filter(record)
        # PlainFormatter caches exc_text; subsequent handlers must also mask it.
        for formatter in (PlainFormatter("%(message)s"), JsonFormatter(), PlainFormatter("%(message)s")):
            output = formatter.format(record)
            self.assertNotIn("TEST_token-with-dashes", output)
            self.assertNotIn("TEST PRIVATE VALUE", output)
            self.assertIn("RuntimeError", output)
            self.assertIn("ValueError", output)

    def test_interpolated_and_mapping_arguments_are_masked_after_formatting(self):
        records = [
            self.record("password=%s count=%d", ("TEST_SENSITIVE_VALUE", 7)),
            self.record("credential=%(password)s count=%(count)d", {"password": "TEST_SENSITIVE_VALUE", "count": 7}),
            self.record("headers={'X-NoviIs-Internal-Secret': 'TEST_SENSITIVE_VALUE'}"),
        ]
        for record in records:
            SensitiveDataFilter().filter(record)
            for formatter in (JsonFormatter(), PlainFormatter("%(message)s")):
                output = formatter.format(record)
                self.assertNotIn("TEST_SENSITIVE_VALUE", output)

    def test_formatters_mask_stack_and_extras_even_without_filter(self):
        record = self.record("Authorization: Bearer TEST_SENSITIVE_VALUE")
        record.stack_info = "stack password=TEST_SENSITIVE_VALUE"
        record.exc_text = "error secret=TEST_SENSITIVE_VALUE"
        record.password = "TEST_SENSITIVE_VALUE"
        for formatter in (JsonFormatter(), PlainFormatter("%(message)s")):
            self.assertNotIn("TEST_SENSITIVE_VALUE", formatter.format(record))
        json.loads(JsonFormatter().format(record))
