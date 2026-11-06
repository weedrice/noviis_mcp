from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from mcp.server.fastmcp.exceptions import ToolError
from exceptions import BackendContractError
from tools.activity import _build_comments_result, _build_feed_result
from tools.notes import build_notes_result, build_note_thread_result
from tools.pagination import read_pagination
from tools.search import build_semantic_search_result
from tools.tool_contract import tool_contracts


def search_item(kind="POST", board="general"):
    return {"contentType": kind, "contentId": 1 if kind == "POST" else 2,
            "postId": 1, "boardId": 7, "boardUrl": board}


class PaginationBoundsTest(unittest.TestCase):
    def test_request_context_checks_omitted_page_and_size_without_filling_output(self):
        for data in ({"totalPages": 1, "hasNext": True},
                     {"totalPages": 1, "last": False},
                     {"totalElements": 21, "totalPages": 2}):
            with self.subTest(data=data), self.assertRaises(BackendContractError):
                read_pagination(data, requested_page=3, requested_size=10)
        result = read_pagination({"totalElements": 41, "totalPages": 5, "hasNext": True},
                                 requested_page=3, requested_size=10, item_count=1)
        self.assertIsNone(result["page"])
        self.assertIsNone(result["size"])
        self.assertTrue(result["has_next"])

    def test_nonempty_page_cannot_exceed_count_or_page_bounds(self):
        for data, page, size, count in (
            ({}, 0, 1, 2), ({"totalElements": 0}, None, None, 1),
            ({"totalPages": 0}, None, None, 1), ({"totalPages": 1}, 1, 10, 1),
            ({"totalElements": 21}, 2, 10, 2),
        ):
            with self.subTest(data=data, page=page, size=size), self.assertRaises(BackendContractError):
                read_pagination(data, requested_page=page, requested_size=size, item_count=count)

    def test_short_pages_and_empty_out_of_range_pages_remain_legal(self):
        read_pagination({"totalElements": 41, "totalPages": 5, "hasNext": True},
                        requested_page=0, requested_size=10, item_count=1)
        result = read_pagination({"totalElements": 1, "totalPages": 1, "last": True},
                                 requested_page=99, requested_size=10, item_count=0)
        self.assertFalse(result["has_next"])
        result = read_pagination({}, requested_page=99, requested_size=10, item_count=0)
        self.assertTrue(all(value is None for value in result.values()))

    def test_all_read_builders_check_actual_raw_collection_and_aliases(self):
        builders = (
            (_build_feed_result, "posts", {"postId": 1, "boardId": 7}, {}),
            (_build_comments_result, "comments", {"commentId": 2, "postId": 1, "content": "text"}, {}),
            (build_notes_result, "threads", {"note_thread_id": 3}, {}),
            (build_note_thread_result, "notes", {"note_id": 4, "content": "text", "sender_agent_name": "sender"}, {"note_thread_id": 3}),
            (build_semantic_search_result, "content", search_item(), {}),
        )
        for builder, key, item, extra in builders:
            data = {key: [item], **extra}
            # A valid item prevents unrelated identity validation from hiding a failure.
            builder({"data": data}, requested_page=0, requested_size=1)
            for metadata in ({"totalElements": 0}, {"totalPages": 0}):
                with self.subTest(builder=builder.__name__, metadata=metadata), self.assertRaises(BackendContractError):
                    builder({"data": {**data, **metadata}}, requested_page=0, requested_size=1)
            with self.subTest(builder=builder.__name__, size=True), self.assertRaises(BackendContractError):
                builder({"data": {**data, key: [item, item]}}, requested_page=0, requested_size=1)

    def test_nested_replies_are_not_counted_as_top_level_page_items(self):
        result = _build_comments_result({"data": {"content": [
            {"commentId": 1, "postId": 7, "status": "ACTIVE", "children": [
                {"commentId": 2, "postId": 7, "parentId": 1, "status": "ACTIVE"}]}
        ], "totalElements": 1, "totalPages": 1}}, requested_page=0, requested_size=1)
        self.assertEqual(len(result.comments), 1)


class SearchFilterTest(unittest.IsolatedAsyncioTestCase):
    async def search(self, items, **args):
        backend = AsyncMock()
        backend.search_semantic.return_value = {"data": {"content": items}}
        ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=SimpleNamespace(client=backend)))
        return await tool_contracts()["search_content"].run({"query": "sample", **args}, context=ctx)

    async def test_explicit_type_filter_rejects_other_type(self):
        for expected, actual in (("POST", "COMMENT"), ("comment", "POST")):
            with self.subTest(expected=expected), self.assertRaisesRegex(ToolError, "requested content type"):
                await self.search([search_item(actual)], content_type=expected)
        result = await self.search([search_item()], content_type="post")
        self.assertEqual(result.content[0].content_type, "POST")

    async def test_board_filter_rejects_conflicting_returned_slug(self):
        for item in (search_item(board="other"), {**search_item(), "board_url": "other"}):
            with self.subTest(item=item), self.assertRaisesRegex(ToolError, "backend_contract_error"):
                await self.search([item], board_url="general")
        result = await self.search([search_item()], board_url="general")
        self.assertEqual(result.content[0].board_url, "general")

    async def test_all_types_unfiltered_boards_and_legacy_optional_slug_are_supported(self):
        result = await self.search([search_item(), search_item("COMMENT", "other")])
        self.assertEqual(len(result.content), 2)
        for item in (search_item(board=None), {k:v for k,v in search_item().items() if k != "boardUrl"}):
            result = await self.search([item], board_url="general")
            self.assertIsNone(result.content[0].board_url)
        self.assertEqual((await self.search([], content_type="POST", board_url="general")).content, [])
