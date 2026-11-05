from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from mcp.server.fastmcp.exceptions import ToolError

from tools.activity import _build_comments_result, _build_feed_result
from tools.tool_contract import tool_contracts


def post(**changes):
    return {"postId": 11, "boardId": 7, "content": "reviewed post", **changes}


def comment(**changes):
    return {"commentId": 21, "postId": 11, "parentId": None,
            "status": "ACTIVE", "content": "reviewed comment", **changes}


async def run_read(name, items, args=None, metadata=None):
    backend = AsyncMock()
    getattr(backend, name).return_value = {"data": {"content": items, **(metadata or {})}}
    ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=SimpleNamespace(client=backend)))
    result = await tool_contracts()[name].run({"agent_token": "synthetic", **(args or {})}, context=ctx)
    getattr(backend, name).assert_awaited_once()
    return result


class ReadResourceContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_all_post_lists_require_positive_long_core_ids(self):
        for name, args in (("get_feed", {}), ("get_my_posts", {}), ("get_board_posts", {"board_id": 7})):
            for key in ("postId", "boardId"):
                for value in (None, True, 0, -1, 1.5, "7", "opaque", 9223372036854775808, "missing"):
                    item = post(**{key: value})
                    if value == "missing":
                        del item[key]
                    with self.subTest(tool=name, key=key, value=value), self.assertRaisesRegex(ToolError, "backend_contract_error"):
                        await run_read(name, [item], args)

    async def test_feed_aliases_and_requested_board_must_agree(self):
        for name, args in (("get_feed", {"board_id": 7}), ("get_board_posts", {"board_id": 7})):
            for item in (post(boardId=8), post(post_id=12), post(board_id=8), post(board_id="7")):
                with self.subTest(tool=name, item=item), self.assertRaisesRegex(ToolError, "backend_contract_error"):
                    await run_read(name, [item], args)
            result = await run_read(name, [{"post_id": 9223372036854775807, "board_id": 7}], {"board_id": "007"})
            self.assertEqual(result.posts[0].post_id, "9223372036854775807")
            self.assertEqual(result.posts[0].board_id, "7")
        result = await run_read("get_feed", [post(boardId=7), post(postId=12, boardId=8)])
        self.assertEqual([item.board_id for item in result.posts], ["7", "8"])

    async def test_comment_core_ids_and_nullable_parent_aliases_are_strict(self):
        for key in ("commentId", "postId", "parentId"):
            for value in (True, 0, -1, 1.5, "11", "opaque", 9223372036854775808):
                with self.subTest(key=key, value=value), self.assertRaisesRegex(ToolError, "backend_contract_error"):
                    await run_read("get_post_comments", [comment(**{key: value})], {"post_id": 11})
        for item in (comment(comment_id=22), comment(post_id=12), comment(parent_id=1)):
            with self.subTest(item=item), self.assertRaisesRegex(ToolError, "backend_contract_error"):
                await run_read("get_post_comments", [item], {"post_id": 11})
        for key in ("commentId", "postId"):
            item = comment()
            del item[key]
            with self.subTest(missing=key), self.assertRaisesRegex(ToolError, "backend_contract_error"):
                await run_read("get_post_comments", [item], {"post_id": 11})

    async def test_comment_and_direct_reply_endpoints_enforce_requested_relationships(self):
        for item in (comment(postId=12), comment(parentId=20)):
            with self.subTest(item=item), self.assertRaisesRegex(ToolError, "backend_contract_error"):
                await run_read("get_post_comments", [item], {"post_id": 11})
        for parent in (None, 20):
            with self.subTest(parent=parent), self.assertRaisesRegex(ToolError, "backend_contract_error"):
                await run_read("get_comment_replies", [comment(commentId=22, parentId=parent)], {"comment_id": 21})
        result = await run_read("get_comment_replies", [comment(commentId=22, parentId=21)], {"comment_id": "021"})
        self.assertEqual(result.comments[0].parent_comment_id, "21")
        with self.assertRaisesRegex(ToolError, "backend_contract_error"):
            await run_read("get_comment_replies", [comment(commentId=22, parentId=21),
                                                    comment(commentId=23, parentId=21, postId=12)], {"comment_id": 21})

    async def test_nested_replies_must_match_parent_and_post_at_every_level(self):
        root = comment(children=[comment(commentId=22, parentId=21,
                                       children=[comment(commentId=23, parentId=22)])])
        good = await run_read("get_post_comments", [root], {"post_id": 11})
        self.assertEqual(good.comments[0].replies[0].replies[0].comment_id, "23")
        for changes in ({"postId": 12}, {"parentId": 99}, {"parentId": None}, {"commentId": 22}):
            malformed = deepcopy(root)
            malformed["children"][0]["children"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaisesRegex(ToolError, "backend_contract_error"):
                await run_read("get_post_comments", [malformed], {"post_id": 11})
        conflicting = {**root, "replies": []}
        with self.assertRaisesRegex(ToolError, "backend_contract_error"):
            await run_read("get_post_comments", [conflicting], {"post_id": 11})

    async def test_hidden_placeholders_keep_identity_and_descendants_without_content(self):
        for status in ("DELETED", "BLINDED", "BLOCKED_AUTHOR"):
            root = comment(status=status, content="must stay hidden", author=None,
                           children=[comment(commentId=22, parentId=21, content="visible reply")])
            result = await run_read("get_post_comments", [root], {"post_id": 11})
            with self.subTest(status=status):
                self.assertEqual(result.comments[0].status, status)
                self.assertEqual(result.comments[0].content, "")
                self.assertEqual(result.comments[0].comment_id, "21")
                self.assertEqual(result.comments[0].replies[0].content, "visible reply")
                self.assertEqual(result.filtered_count, 0)

    async def test_snake_aliases_and_optional_legacy_page_metadata_remain_supported(self):
        item = {"comment_id": 21, "post_id": 11, "parent_comment_id": None,
                "content": "parent", "replies": [
                    {"comment_id": 22, "post_id": 11, "parent_comment_id": 21, "content": "child"}]}
        result = await run_read("get_post_comments", [item], {"post_id": "011"})
        self.assertEqual(result.comments[0].replies[0].post_id, "11")
        self.assertIsNone(result.page_number)
        self.assertIsNone(result.has_next)
        self.assertEqual(_build_feed_result({"posts": []}).posts, [])
        self.assertEqual(_build_comments_result({"comments": []}).comments, [])

    async def test_every_read_tool_passes_requested_pagination_to_shared_validator(self):
        cases = (("get_feed", {}, [post()], 10), ("get_my_posts", {}, [post()], 10),
                 ("get_board_posts", {"board_id": 7}, [post()], 10),
                 ("get_post_comments", {"post_id": 11}, [comment()], 20),
                 ("get_comment_replies", {"comment_id": 20}, [comment(parentId=20)], 20))
        for name, args, items, size in cases:
            for metadata in ({"page": 1}, {"size": size - 1}, {"hasNext": "false"},
                             {"page": 0, "number": 1}, {"hasNext": True, "has_next": False}):
                with self.subTest(tool=name, metadata=metadata), self.assertRaisesRegex(ToolError, "backend_contract_error"):
                    await run_read(name, items, args, metadata)
            result = await run_read(name, items, args,
                                    {"page": 0, "size": size, "totalElements": 1, "totalPages": 1, "hasNext": False})
            self.assertEqual(result.page_number, 0)
            self.assertFalse(result.has_next)
