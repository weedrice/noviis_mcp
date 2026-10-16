from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from client import NoviIsClient
from scripts.sync_backend_contract import check_local
from tools.activity import register_activity_tools
from tools.operations import register_operation_tools
from test_runtime_safety import ToolRegistry


FIXTURE = json.loads((Path(__file__).parent / "fixtures/backend/agent-mcp-contract.json").read_text(encoding="utf-8"))


class BackendContractTest(unittest.IsolatedAsyncioTestCase):
    def test_generated_fixture_matches_recorded_provenance(self):
        check_local()

    async def call(self, case, tool, **arguments):
        exchange = FIXTURE[case]
        calls = []
        def handler(request):
            calls.append(request)
            expected = exchange["request"]
            self.assertEqual(request.method, expected["method"])
            self.assertEqual(request.url.path, expected["path"])
            self.assertEqual(dict(request.url.params), expected["query"])
            self.assertEqual(request.headers["Authorization"], "Bearer contract-test")
            return httpx.Response(200, json=exchange["response"])
        async with httpx.AsyncClient(base_url="https://contract.test/api/v1", transport=httpx.MockTransport(handler)) as http:
            client = NoviIsClient(client=http, internal_secret="synthetic-secret")
            registry = ToolRegistry()
            register_activity_tools(registry)
            register_operation_tools(registry)
            ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=SimpleNamespace(client=client)))
            result = await registry.functions[tool](ctx=ctx, agent_token="contract-test", **arguments)
        self.assertEqual(len(calls), 1)
        return result

    async def test_feed_keeps_actual_backend_body_author_and_pagination(self):
        params = FIXTURE["feed"]["request"]["query"]
        result = await self.call("feed", "get_feed", board_id=params["boardId"],
                                 page=int(params["page"]), size=int(params["size"]))
        source = FIXTURE["feed"]["response"]["data"]
        post = source["content"][0]
        self.assertEqual(result.posts[0].content, post["content"])
        self.assertEqual(result.posts[0].content_preview, post["content"][:500])
        self.assertEqual(result.posts[0].author_name, post["author"]["displayName"])
        self.assertTrue(result.posts[0].untrusted_content)
        self.assertEqual((result.page_number, result.page_size, result.has_next),
                         (source["page"], source["size"], source["hasNext"]))

    async def test_comments_keep_status_and_allow_following_replies(self):
        result = await self.call("comments", "get_post_comments", post_id="101", page=0, size=4)
        self.assertEqual([comment.status for comment in result.comments],
                         ["ACTIVE", "DELETED", "BLINDED", "BLOCKED_AUTHOR"])
        self.assertTrue(result.comments[1].is_deleted)
        self.assertTrue(all(comment.has_replies and comment.reply_count == 1 for comment in result.comments))
        self.assertTrue(all(comment.content == "" for comment in result.comments[1:]))
        self.assertEqual(result.page_number, 0)
        self.assertTrue(result.has_next)
        result = await self.call("replies", "get_comment_replies", comment_id="202", page=1, size=1)
        raw = FIXTURE["replies"]["response"]["data"]["content"][0]
        self.assertEqual(result.comments[0].parent_comment_id, str(raw["parentId"]))
        self.assertEqual(result.comments[0].content, raw["content"])

    async def test_operation_response_matches_backend_serialization(self):
        raw = FIXTURE["operation"]["response"]["data"]
        with patch("tools.operations.NOVIIS_OPERATION_SUPPORT", True):
            result = await self.call("operation", "get_operation_result", operation_id=raw["operation_id"])
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.result, raw["result"])
        self.assertEqual(result.completed_at, raw["completed_at"])
