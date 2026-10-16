from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tools.activity import _build_feed_result, _to_comment, register_activity_tools
from tools.home import build_agent_home_result
from tools.notes import build_notes_result
from test_runtime_safety import ToolRegistry


class ReadContextTest(unittest.IsolatedAsyncioTestCase):
    async def test_feed_rejects_unsupported_or_conflicting_pagination_before_io(self):
        registry = ToolRegistry()
        register_activity_tools(registry)
        client = AsyncMock()
        ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=SimpleNamespace(client=client)))
        for args in ({"cursor": "next"}, {"limit": 10, "size": 20}):
            with self.assertRaises(ValueError):
                await registry.functions["get_feed"](ctx, "test-token", **args)
        client.get_feed.assert_not_awaited()
        client.get_feed.return_value = {"data": {"content": []}}
        await registry.functions["get_feed"](ctx, "test-token", limit=7)
        self.assertEqual(client.get_feed.call_args.kwargs, {"token": "test-token", "board_id": None, "page": 0, "size": 7})

    def test_full_content_is_not_lost_when_preview_is_short(self):
        body = "본문" * 1000
        result = _build_feed_result({"data": {"content": [{"postId": 1, "content": body}]}})
        self.assertEqual(result.posts[0].content, body)
        self.assertEqual(len(result.posts[0].content_preview), 500)
        missing = _build_feed_result({"data": {"content": [{"postId": 2, "summary": "preview"}]}})
        self.assertIsNone(missing.posts[0].content)
        self.assertEqual(missing.posts[0].content_preview, "preview")

    def test_hidden_or_unknown_comment_status_does_not_expose_body(self):
        for status in ("DELETED", "BLINDED", "BLOCKED_AUTHOR", "FUTURE_HIDDEN_STATE"):
            comment = _to_comment({"commentId": 1, "status": status, "content": "hidden body"})
            self.assertEqual(comment.status, status)
            self.assertEqual(comment.content, "")

    def test_unread_notes_request_attention_without_human_escalation(self):
        data = {key: [] for key in ("soft_guidance", "style_guidance", "activity_on_my_posts", "my_recent_posts",
                                    "recommended_boards", "recent_feed", "opportunities", "warnings")}
        data.update(agent={"status": "active", "name": "test"}, usage={}, capabilities={},
                    hard_constraints={"suspended": False, "can_create_post": True, "can_create_comment": True,
                                      "posts_remaining": 3, "comments_remaining": 3},
                    note_summary={"unread_thread_count": 1, "unread_note_count": 2})
        result = build_agent_home_result({"data": data})
        self.assertEqual(result.human_escalations, [])
        self.assertIn("unread_notes", result.heartbeat.reasons)
        self.assertEqual(result.heartbeat.urgency, "attention")
        data["human_escalations"] = [{"type": "note_request_approval", "summary": "Explicit request"}]
        result = build_agent_home_result({"data": data})
        self.assertEqual(result.human_escalations[0].type, "note_request_approval")
        notes = build_notes_result({"data": {"content": [{"note_thread_id": 1, "needs_human_input": True}]}})
        self.assertTrue(notes.content[0].needs_human_input)
