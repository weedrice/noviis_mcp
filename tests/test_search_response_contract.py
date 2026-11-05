from __future__ import annotations

import unittest

from exceptions import BackendContractError
from tools.search import build_semantic_search_result


def search_item(kind="POST"):
    return {"contentType": kind, "contentId": 123 if kind == "POST" else 456,
            "postId": 123, "boardId": 10, "boardUrl": "general", "boardName": "General",
            "title": "Sample", "excerpt": "Context", "similarity": None,
            "rankSource": "KEYWORD_FALLBACK", "createdAt": "2026-11-05T10:30:00",
            "author": {"userId": 7, "agentId": None, "authorType": "USER",
                       "displayName": "Writer", "profileImageUrl": None}}


class SearchResponseContractTest(unittest.TestCase):
    def parse(self, item):
        return build_semantic_search_result({"data": {"content": [item]}}).content[0]

    def test_backend_post_and_comment_shapes_keep_numeric_identity(self):
        for kind in ("POST", "COMMENT"):
            with self.subTest(kind=kind):
                result = self.parse(search_item(kind))
                self.assertIs(type(result.content_id), int)
                self.assertEqual(result.post_id, 123)
                self.assertEqual(result.board_id, 10)
                self.assertEqual(result.comment_id, 456 if kind == "COMMENT" else None)
                self.assertIsNone(result.similarity)
                self.assertIsNone(result.author.agent_id)
                self.assertIsNone(result.author.profile_image_url)

    def test_ids_cannot_be_coerced_or_silently_dropped(self):
        for field in ("contentId", "postId", "boardId"):
            for value in (None, True, False, 0, -1, 1.5, "123", 9_223_372_036_854_775_808):
                with self.subTest(field=field, value=value):
                    item = search_item()
                    item[field] = value
                    with self.assertRaises(BackendContractError):
                        self.parse(item)
            item = search_item()
            del item[field]
            with self.assertRaises(BackendContractError):
                self.parse(item)

    def test_id_alias_conflicts_and_wrong_content_relationships_fail(self):
        for changes in ({"content_id": 124}, {"post_id": 999}, {"board_id": 11},
                        {"postId": 456}, {"commentId": 456}, {"contentType": "post"},
                        {"contentType": "UNKNOWN"}, {"content_type": "COMMENT"}):
            with self.subTest(changes=changes), self.assertRaises(BackendContractError):
                self.parse({**search_item(), **changes})
        for changes in ({"commentId": 457}, {"commentId": True}, {"commentId": 456, "comment_id": 457}):
            with self.subTest(changes=changes), self.assertRaises(BackendContractError):
                self.parse({**search_item("COMMENT"), **changes})
        self.assertEqual(self.parse({**search_item("COMMENT"), "commentId": 456}).comment_id, 456)

    def test_snake_case_aliases_and_maximum_long_remain_exact(self):
        item = search_item()
        for camel, snake in (("contentType", "content_type"), ("contentId", "content_id"),
                             ("postId", "post_id"), ("boardId", "board_id")):
            item[snake] = item.pop(camel)
        item["content_id"] = item["post_id"] = 9_223_372_036_854_775_807
        self.assertEqual(self.parse(item).post_id, 9_223_372_036_854_775_807)

    def test_author_ids_and_type_are_consistent(self):
        for changes in ({"userId": True}, {"userId": None}, {"user_id": 8}, {"agentId": 2},
                        {"authorType": "AGENT"}, {"authorType": "BOT"}, {"agentId": 1.5}):
            item = search_item()
            item["author"].update(changes)
            with self.subTest(changes=changes), self.assertRaises(BackendContractError):
                self.parse(item)
        item = search_item()
        item["author"].update({"authorType": "AGENT", "agentId": 5})
        self.assertEqual(self.parse(item).author.agent_id, 5)
        for value in ([], "writer", 7):
            item["author"] = value
            with self.assertRaises(BackendContractError):
                self.parse(item)

    def test_nullable_presentation_fields_keep_existing_output_contract(self):
        item = search_item()
        for field in ("title", "excerpt", "rankSource", "createdAt", "boardUrl", "boardName", "author"):
            item[field] = None
        result = self.parse(item)
        self.assertEqual((result.title, result.excerpt, result.rank_source, result.created_at), ("", "", "", ""))
        self.assertIsNone(result.author)
        self.assertIsNone(result.board_url)

    def test_malformed_collections_do_not_appear_as_empty_searches(self):
        for payload in ({}, {"data": None}, {"data": []}, {"data": {"content": None}},
                        {"data": {"content": {}}}, {"data": {"content": [search_item(), None]}},
                        {"success": False, "data": {"content": []}}):
            with self.subTest(payload=payload), self.assertRaises(BackendContractError):
                build_semantic_search_result(payload)
        self.assertEqual(build_semantic_search_result({"data": {"content": []}}).content, [])

    def test_malformed_presentation_fields_do_not_get_stringified(self):
        for field, value in (("title", {}), ("createdAt", 1), ("similarity", True),
                             ("similarity", "0.5"), ("similarity", float("nan")), ("similarity", float("inf"))):
            item = search_item()
            item[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(BackendContractError):
                self.parse(item)
