from __future__ import annotations

import unittest

from exceptions import BackendContractError
from tools.activity import _build_comments_result, _build_feed_result
from tools.notes import build_notes_result, build_note_thread_result
from tools.search import build_semantic_search_result


class ReadResponseValidationTest(unittest.TestCase):
    builders = (_build_feed_result, _build_comments_result, build_notes_result,
                build_note_thread_result, build_semantic_search_result)

    def build(self, builder, data, **context):
        return builder({"data": {"content": [], "noteThreadId": 1, **data}}, **context)

    def test_malformed_collections_never_become_empty_success(self):
        for builder in self.builders:
            for content in (None, "invalid", {}, 5, [None], ["invalid"]):
                with self.subTest(builder=builder.__name__, content=content), self.assertRaises(BackendContractError):
                    self.build(builder, {"content": content})
            for payload in ({}, {"data": None}, {"data": []}, {"data": {"noteThreadId": 1}}):
                with self.subTest(builder=builder.__name__, payload=payload), self.assertRaises(BackendContractError):
                    builder(payload)

    def test_previous_page_flags_reject_coercions_and_conflicting_aliases(self):
        for builder in self.builders:
            for metadata in ({"hasPrevious": "false"}, {"has_previous": 0}, {"hasPrevious": None},
                             {"hasPrevious": False, "has_previous": True},
                             {"page": 0, "hasPrevious": True}, {"page": 1, "has_previous": False}):
                with self.subTest(builder=builder.__name__, metadata=metadata), self.assertRaises(BackendContractError):
                    self.build(builder, metadata)

    def test_requested_page_validates_previous_flag_without_response_page(self):
        for builder in self.builders:
            for page, invalid in ((0, True), (2, False)):
                with self.subTest(builder=builder.__name__, page=page):
                    with self.assertRaises(BackendContractError):
                        self.build(builder, {"has_previous": invalid}, requested_page=page)
                    result = self.build(builder, {"has_previous": not invalid}, requested_page=page)
                    actual = result.page_number if hasattr(result, "page_number") else result.page
                    self.assertIsNone(actual)

    def test_snake_case_page_aliases_are_mapped_and_checked(self):
        for builder in self.builders:
            with self.subTest(builder=builder.__name__):
                result = self.build(builder, {"page_number": 3, "page_size": 10, "total_elements": 1,
                                              "total_pages": 1, "is_last": True, "has_previous": True})
                page = result.page_number if hasattr(result, "page_number") else result.page
                size = result.page_size if hasattr(result, "page_size") else result.size
                self.assertEqual((page, size, result.has_next), (3, 10, False))
                for metadata in ({"page": 0, "page_number": 1}, {"size": 10, "page_size": 20},
                                 {"page_number": True}, {"page_size": "10"}, {"page_size": 0}):
                    with self.subTest(metadata=metadata), self.assertRaises(BackendContractError):
                        self.build(builder, metadata)

    def test_missing_metadata_and_explicit_empty_pages_remain_supported(self):
        for builder in self.builders:
            with self.subTest(builder=builder.__name__):
                result = self.build(builder, {}, requested_page=2, requested_size=10)
                self.assertIsNone(result.has_next)
                result = self.build(builder, {"page": 0, "size": 10, "totalElements": 0,
                                              "totalPages": 0, "last": True, "hasPrevious": False})
                self.assertFalse(result.has_next)
