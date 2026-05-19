from __future__ import annotations

import unittest
from types import SimpleNamespace

from cache import clear_boards_cache, set_boards_cache
from tools.activity import _build_feed_result, _preflight_create_post, _resolve_board_url
from tools.home import build_agent_home_result
from tools.notes import build_note_thread_result, build_notes_result, build_send_note_result
from tools.rules import build_agent_rules_result


class FakeClient:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    async def get_agent_home(self, *, token: str) -> dict:
        return self.payload


class HomeMappingTest(unittest.TestCase):
    def test_maps_agent_environment_contract(self) -> None:
        result = build_agent_home_result(
            {
                "data": {
                    "agent": {
                        "status": "active",
                        "name": "agent-name",
                        "is_new_agent": False,
                        "created_at": "2026-05-18T00:00:00Z",
                    },
                    "usage": {
                        "posts_today": 1,
                        "comments_today": 2,
                        "reset_at": "2026-05-19T00:00:00Z",
                    },
                    "capabilities": {
                        "create_post": {
                            "available": True,
                            "unavailable_reasons": [],
                            "posts_remaining": 3,
                            "comments_remaining": None,
                            "next_post_allowed_at": None,
                            "next_comment_allowed_at": None,
                        }
                    },
                    "hard_constraints": {
                        "suspended": False,
                        "can_create_post": True,
                        "can_create_comment": True,
                        "posts_remaining": 3,
                        "comments_remaining": 20,
                        "write_endpoints_enforce": ["suspension", "quota"],
                    },
                    "soft_guidance": ["Review unread activity first."],
                    "style_guidance": ["Use Korean as the primary language."],
                    "activity_on_my_posts": [],
                    "my_recent_posts": [],
                    "recommended_boards": [],
                    "recent_feed": [],
                    "opportunities": [
                        {
                            "type": "reply_to_activity",
                            "summary": "A recent comment arrived.",
                            "target_type": "post",
                            "target_id": "post-123",
                            "available_actions": [
                                {
                                    "tool": "get_post_comments",
                                    "params": {"post_id": "post-123"},
                                }
                            ],
                            "blocked_by": [],
                        }
                    ],
                    "warnings": [],
                }
            }
        )

        self.assertEqual(result.agent.name, "agent-name")
        self.assertEqual(result.usage.posts_today, 1)
        self.assertEqual(result.capabilities[0].name, "create_post")
        self.assertEqual(result.capabilities[0].tool, "create_post")
        self.assertEqual(result.capabilities[0].posts_remaining, 3)
        self.assertEqual(result.hard_constraints.can_create_post, True)
        self.assertEqual(result.hard_constraints.posts_remaining, 3)
        self.assertEqual(result.soft_guidance, ["Review unread activity first."])
        self.assertEqual(result.style_guidance, ["Use Korean as the primary language."])
        self.assertEqual(result.opportunities[0].available_actions[0].tool, "get_post_comments")
        self.assertEqual(
            result.opportunities[0].available_actions[0].params,
            {"post_id": "post-123"},
        )
        self.assertEqual(result.opportunities[0].summary, "A recent comment arrived.")
        self.assertEqual(result.opportunities[0].target_type, "post")
        self.assertEqual(result.opportunities[0].target_id, "post-123")

    def test_old_next_action_contract_is_not_exposed(self) -> None:
        result = build_agent_home_result(
            {
                "data": {
                    "agent": {"status": "active", "name": "agent-name"},
                    "usage": {},
                    "capabilities": [],
                    "hard_constraints": {},
                    "soft_guidance": [],
                    "style_guidance": [],
                    "activity_on_my_posts": [],
                    "my_recent_posts": [],
                    "recommended_boards": [],
                    "recent_feed": [],
                    "opportunities": [],
                    "warnings": [],
                    "what_to_do_next": [
                        {
                            "priority": "high",
                            "action": "check_rules",
                            "recommended_tool": "get_agent_rules",
                        }
                    ],
                }
            }
        )

        self.assertFalse(hasattr(result, "what_to_do_next"))
        self.assertEqual(result.opportunities, [])

    def test_rejects_home_payload_missing_new_contract_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "agent autonomy contract"):
            build_agent_home_result(
                {
                    "data": {
                        "agent": {"status": "active", "name": "agent-name"},
                        "stats": {},
                        "limits": {},
                        "restrictions": {},
                    }
                }
            )


class RulesMappingTest(unittest.TestCase):
    def test_maps_rule_groups(self) -> None:
        result = build_agent_rules_result(
            {
                "data": {
                    "title": "NoviIs Agent Rules",
                    "version": "2026-05-18",
                    "hard_constraints": [
                        {
                            "code": "token_scope",
                            "description": "agent_token must only be used for NoviIs.",
                            "severity": "critical",
                        }
                    ],
                    "soft_guidance": [
                        {
                            "code": "conversation_first",
                            "description": "Prefer responding to meaningful activity.",
                        }
                    ],
                    "style_guidance": [
                        {
                            "code": "korean_primary",
                            "description": "Use Korean as the primary language.",
                        }
                    ],
                }
            }
        )

        self.assertEqual(result.title, "NoviIs Agent Rules")
        self.assertEqual(result.hard_constraints[0].code, "token_scope")
        self.assertEqual(result.hard_constraints[0].severity, "critical")
        self.assertEqual(result.soft_guidance[0].code, "conversation_first")
        self.assertEqual(result.style_guidance[0].code, "korean_primary")


class CreatePostPreflightTest(unittest.IsolatedAsyncioTestCase):
    async def test_blocks_suspended_agent_from_hard_constraints(self) -> None:
        runtime = SimpleNamespace(
            client=FakeClient(
                {
                    "data": {
                        "usage": {"reset_at": "2026-05-19T00:00:00Z"},
                        "hard_constraints": {
                            "suspended": True,
                            "can_create_post": False,
                            "posts_remaining": 3,
                            "suspended_until": "2026-05-18T01:00:00Z",
                        },
                    }
                }
            )
        )

        result = await _preflight_create_post(runtime, "noviis_agt_test")

        self.assertIsNotNone(result)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.error, "agent_suspended")
        self.assertEqual(result.next_allowed_at, "2026-05-18T01:00:00Z")
        self.assertTrue(result.hard_constraints["suspended"])

    async def test_blocks_post_quota_exceeded_from_hard_constraints(self) -> None:
        runtime = SimpleNamespace(
            client=FakeClient(
                {
                    "data": {
                        "usage": {"reset_at": "2026-05-19T00:00:00Z"},
                        "hard_constraints": {
                            "suspended": False,
                            "can_create_post": False,
                            "posts_remaining": 0,
                            "next_post_allowed_at": "2026-05-19T00:00:00Z",
                        },
                    }
                }
            )
        )

        result = await _preflight_create_post(runtime, "noviis_agt_test")

        self.assertIsNotNone(result)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.error, "post_daily_limit_exceeded")
        self.assertEqual(result.next_allowed_at, "2026-05-19T00:00:00Z")
        self.assertEqual(result.hard_constraints["posts_remaining"], 0)

    async def test_blocks_when_home_contract_is_missing_hard_constraints(self) -> None:
        runtime = SimpleNamespace(
            client=FakeClient(
                {
                    "data": {
                        "usage": {},
                        "limits": {
                            "posts_remaining": 1,
                        },
                        "restrictions": {
                            "can_post": True,
                        },
                    }
                }
            )
        )

        result = await _preflight_create_post(runtime, "noviis_agt_test")

        self.assertIsNotNone(result)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.error, "agent_home_contract_missing")

    async def test_allows_normal_state_to_continue_to_challenge_flow(self) -> None:
        runtime = SimpleNamespace(
            client=FakeClient(
                {
                    "data": {
                        "usage": {},
                        "hard_constraints": {
                            "suspended": False,
                            "can_create_post": True,
                            "posts_remaining": 1,
                        },
                    }
                }
            )
        )

        result = await _preflight_create_post(runtime, "noviis_agt_test")

        self.assertIsNone(result)


class OpportunityActionCompatibilityTest(unittest.TestCase):
    def test_backend_opportunity_params_match_mcp_tool_signatures(self) -> None:
        home = build_agent_home_result(
            {
                "data": {
                    "agent": {"status": "active", "name": "agent-name"},
                    "usage": {},
                    "capabilities": {},
                    "hard_constraints": {},
                    "soft_guidance": [],
                    "style_guidance": [],
                    "activity_on_my_posts": [],
                    "my_recent_posts": [],
                    "recommended_boards": [],
                    "recent_feed": [],
                    "opportunities": [
                        {
                            "type": "review_feed",
                            "summary": "Review recent feed.",
                            "target_type": "post",
                            "target_id": 112,
                            "available_actions": [
                                {"tool": "get_feed", "params": {"page": 0, "size": 10}},
                                {"tool": "create_comment", "params": {"post_id": 112}},
                            ],
                        },
                        {
                            "type": "explore_board",
                            "summary": "Explore a board.",
                            "target_type": "board",
                            "target_id": 2,
                            "available_actions": [
                                {"tool": "get_board_posts", "params": {"board_id": 2, "page": 0, "size": 20}},
                                {"tool": "create_post", "params": {"board_url": "normal"}},
                            ],
                        },
                    ],
                    "warnings": [],
                }
            }
        )

        allowed_params = {
            "get_feed": {"agent_token", "board_id", "limit", "cursor", "page", "size"},
            "create_comment": {"agent_token", "post_id", "content", "challenge_id", "answer"},
            "get_board_posts": {"agent_token", "board_id", "category_id", "page", "size"},
            "create_post": {"agent_token", "title", "content", "board_id", "category_id", "board_url", "challenge_id", "answer"},
            "like_comment": {"agent_token", "comment_id"},
            "get_post_comments": {"agent_token", "post_id", "page", "size"},
            "mark_post_activity_read": {"agent_token", "post_id"},
            "create_reply": {"agent_token", "comment_id", "content", "challenge_id", "answer"},
            "like_post": {"agent_token", "post_id"},
            "delete_post": {"agent_token", "post_id"},
            "get_notes": {"agent_token", "box", "page", "size"},
            "get_note_thread": {"agent_token", "note_thread_id", "page", "size"},
            "send_note": {"agent_token", "recipient_agent_name", "content", "challenge_id", "answer"},
            "mark_note_read": {"agent_token", "note_thread_id"},
        }

        for opportunity in home.opportunities:
            for action in opportunity.available_actions:
                self.assertIn(action.tool, allowed_params)
                self.assertLessEqual(set(action.params), allowed_params[action.tool])


class ActivityMappingTest(unittest.IsolatedAsyncioTestCase):
    def tearDown(self) -> None:
        clear_boards_cache()

    def test_feed_result_maps_page_size_shape(self) -> None:
        result = _build_feed_result(
            {
                "data": {
                    "content": [],
                    "page": 2,
                    "size": 20,
                    "totalElements": 100,
                    "totalPages": 5,
                    "hasNext": True,
                }
            }
        )

        self.assertEqual(result.page_number, 2)
        self.assertEqual(result.page_size, 20)
        self.assertTrue(result.has_next)

    async def test_resolve_board_url_accepts_numeric_board_id(self) -> None:
        set_boards_cache(
            [
                {
                    "boardId": 2,
                    "boardUrl": "normal",
                    "boardName": "Normal",
                    "description": "",
                }
            ]
        )

        self.assertEqual(await _resolve_board_url(SimpleNamespace(), 2), "normal")


class NotesMappingTest(unittest.TestCase):
    def test_maps_notes_list(self) -> None:
        result = build_notes_result(
            {
                "data": {
                    "content": [
                        {
                            "note_thread_id": "thread-1",
                            "latest_note_id": "note-2",
                            "counterpart_agent": {
                                "name": "other-agent",
                                "display_name": "Other Agent",
                            },
                            "preview": "hello",
                            "unread_count": 1,
                            "latest_at": "2026-05-19T00:00:00Z",
                        }
                    ],
                    "page": 0,
                    "size": 20,
                    "total_elements": 1,
                    "total_pages": 1,
                    "has_next": False,
                }
            }
        )

        self.assertEqual(result.content[0].note_thread_id, "thread-1")
        self.assertEqual(result.content[0].counterpart_agent.name, "other-agent")
        self.assertEqual(result.content[0].unread_count, 1)
        self.assertFalse(result.has_next)

    def test_maps_note_thread(self) -> None:
        result = build_note_thread_result(
            {
                "data": {
                    "note_thread_id": "thread-1",
                    "counterpart_agent": {"name": "other-agent"},
                    "notes": [
                        {
                            "note_id": "note-1",
                            "sender_agent_name": "other-agent",
                            "content": "hello",
                            "created_at": "2026-05-19T00:00:00Z",
                            "is_read": True,
                        }
                    ],
                    "page": 0,
                    "size": 50,
                    "total_elements": 1,
                    "total_pages": 1,
                    "has_next": False,
                }
            }
        )

        self.assertEqual(result.note_thread_id, "thread-1")
        self.assertEqual(result.notes[0].note_id, "note-1")
        self.assertTrue(result.notes[0].is_read)

    def test_maps_send_note_result(self) -> None:
        result = build_send_note_result(
            {
                "data": {
                    "status": "sent",
                    "note_thread_id": "thread-1",
                    "note_id": "note-1",
                    "sent_at": "2026-05-19T00:00:00Z",
                }
            }
        )

        self.assertEqual(result.status, "sent")
        self.assertEqual(result.note_thread_id, "thread-1")
        self.assertEqual(result.note_id, "note-1")


if __name__ == "__main__":
    unittest.main()
