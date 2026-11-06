from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from mcp.server.fastmcp.exceptions import ToolError
from tools.tool_contract import tool_contracts

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


class SearchPaginationContractTest(unittest.IsolatedAsyncioTestCase):
    async def search(self, metadata, *, page=3, size=10):
        backend = AsyncMock()
        backend.search_semantic.return_value = {"data": {"content": [], **metadata}}
        ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=SimpleNamespace(client=backend)))
        result = await tool_contracts()["search_content"].run(
            {"query": "sample", "page": page, "size": size}, context=ctx)
        backend.search_semantic.assert_awaited_once_with(
            query="sample", token=None, content_type="ALL", board_url=None, page=page, size=size)
        return result

    async def test_search_checks_requested_page_and_size_at_tool_boundary(self):
        for metadata in ({"page": 0}, {"size": 20}, {"number": 0}, {"pageSize": 20}):
            with self.subTest(metadata=metadata), self.assertRaisesRegex(ToolError, "backend_contract_error"):
                await self.search(metadata)
        result = await self.search({"page": 3, "size": 10, "totalElements": 41,
                                    "totalPages": 5, "hasNext": True, "hasPrevious": True})
        self.assertEqual((result.page, result.size, result.total_pages), (3, 10, 5))
        self.assertTrue(result.has_next)
        self.assertTrue(result.has_previous)

    async def test_search_rejects_malformed_or_conflicting_page_metadata(self):
        for metadata in ({"hasNext": "unknown", "has_next": False}, {"hasNext": "false"},
                         {"page": 3, "number": 0}, {"hasPrevious": True, "has_previous": False},
                         {"page": 3, "totalPages": 2, "hasNext": True},
                         {"size": 10, "totalElements": 21, "totalPages": 2},
                         {"size": None}, {"totalElements": True}):
            with self.subTest(metadata=metadata), self.assertRaisesRegex(ToolError, "backend_contract_error"):
                await self.search(metadata)

    async def test_previous_flag_uses_strict_boolean_and_page_consistency(self):
        for key in ("hasPrevious", "has_previous"):
            for value in (None, "false", 0, 1, {}, []):
                with self.subTest(key=key, value=value), self.assertRaisesRegex(ToolError, "backend_contract_error"):
                    await self.search({key: value})
        for page, previous in ((0, True), (3, False)):
            with self.subTest(page=page), self.assertRaisesRegex(ToolError, "backend_contract_error"):
                await self.search({"hasPrevious": previous}, page=page)
        first = await self.search({"has_previous": False}, page=0)
        self.assertFalse(first.has_previous)
        self.assertIsNone(first.page)

    async def test_missing_metadata_remains_unknown_and_out_of_range_pages_are_supported(self):
        result = await self.search({})
        for name in ("page", "size", "total_elements", "total_pages", "has_next", "has_previous"):
            self.assertIsNone(getattr(result, name))
        result = await self.search({"page": 3, "size": 10, "totalElements": 1,
                                    "totalPages": 1, "last": True, "hasPrevious": True})
        self.assertFalse(result.has_next)
        self.assertTrue(result.has_previous)
