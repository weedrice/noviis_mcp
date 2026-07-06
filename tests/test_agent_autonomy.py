from __future__ import annotations

import os
import subprocess
import sys
import unittest
from types import SimpleNamespace

import httpx

from cache import clear_boards_cache, set_boards_cache
from client import NoviIsClient
from config import INJECTION_WARNING
from exceptions import PermissionDenied, Unauthorized
from logging_utils import _sanitize
from tools.activity import _build_feed_result, _preflight_create_post, _resolve_board_url
from tools.auth import build_register_agent_user_message
from tools.guide import HEARTBEAT_GUIDE
from tools.home import build_agent_home_result
from tools.manifest import build_agent_manifest_result
from tools.notes import build_note_thread_result, build_notes_result, build_send_note_result
from tools.parsing import (
    compact_params,
    dict_list,
    optional_bool,
    optional_float,
    optional_int,
    optional_int_from_float,
    optional_str,
    unwrap_dict_data,
    unwrap_list_data,
)
from tools.rules import build_agent_rules_result
from tools.search import build_semantic_search_result
from tools.tool_contract import OPPORTUNITY_ACTION_PARAMS


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
        self.assertEqual(result.heartbeat.recommended_interval_seconds, 1800)
        self.assertEqual(result.heartbeat.urgency, "normal")
        self.assertEqual(result.heartbeat.primary_tool, "get_agent_home")
        self.assertEqual(result.human_escalations, [])
        self.assertEqual(result.action_quality_warnings, [])

    def test_derives_heartbeat_and_human_escalations_without_backend_fields(self) -> None:
        result = build_agent_home_result(
            {
                "data": {
                    "agent": {"status": "active", "name": "agent-name"},
                    "usage": {},
                    "capabilities": {},
                    "hard_constraints": {
                        "suspended": True,
                        "reason": "manual review required",
                    },
                    "soft_guidance": [],
                    "style_guidance": [],
                    "activity_on_my_posts": [
                        {
                            "post_id": "post-1",
                            "title": "Post",
                            "unread_count": 2,
                        }
                    ],
                    "my_recent_posts": [],
                    "recommended_boards": [],
                    "recent_feed": [],
                    "note_summary": {
                        "unread_thread_count": 1,
                        "unread_note_count": 3,
                    },
                    "opportunities": [],
                    "warnings": ["Backend reported a warning."],
                }
            }
        )

        self.assertEqual(result.heartbeat.urgency, "urgent")
        self.assertEqual(
            result.heartbeat.reasons,
            ["activity_on_my_posts", "unread_notes", "warnings", "agent_suspended"],
        )
        self.assertEqual(
            [escalation.type for escalation in result.human_escalations],
            ["agent_suspended", "unread_notes", "warning"],
        )

    def test_accepts_backend_optional_heartbeat_and_human_escalations(self) -> None:
        result = build_agent_home_result(
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
                    "opportunities": [],
                    "warnings": [],
                    "heartbeat": {
                        "recommended_interval_seconds": 600,
                        "urgency": "attention",
                        "reasons": ["backend_reason"],
                        "next_check_after": "2026-05-19T12:30:00+09:00",
                    },
                    "human_escalations": [
                        {
                            "type": "note_request_approval",
                            "summary": "Approve a note request.",
                            "target_type": "note_request",
                            "target_id": "request-1",
                        }
                    ],
                }
            }
        )

        self.assertEqual(result.heartbeat.recommended_interval_seconds, 600)
        self.assertEqual(result.heartbeat.reasons, ["backend_reason"])
        self.assertEqual(result.heartbeat.next_check_after, "2026-05-19T12:30:00+09:00")
        self.assertEqual(result.human_escalations[0].type, "note_request_approval")

    def test_validates_opportunity_action_params(self) -> None:
        result = build_agent_home_result(
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
                            "type": "bad_action",
                            "available_actions": [
                                {"tool": "create_post", "params": {"board_url": "normal", "unknown": "x"}},
                                {"tool": "unknown_tool", "params": {"id": 1}},
                            ],
                        }
                    ],
                    "warnings": [],
                }
            }
        )

        actions = result.opportunities[0].available_actions
        self.assertFalse(actions[0].valid)
        self.assertEqual(actions[0].invalid_params, ["unknown"])
        self.assertFalse(actions[1].valid)
        self.assertIn("Unsupported MCP tool", actions[1].validation_warning or "")
        self.assertEqual(len(result.action_quality_warnings), 2)

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


class GuideAndManifestTest(unittest.TestCase):
    def test_guide_includes_heartbeat_and_human_escalation_sections(self) -> None:
        self.assertIn("Periodic Heartbeat", HEARTBEAT_GUIDE)
        self.assertIn("get_agent_home", HEARTBEAT_GUIDE)
        self.assertIn("human_escalations", HEARTBEAT_GUIDE)
        self.assertIn("The MCP server does not schedule future runs by itself", HEARTBEAT_GUIDE)
        self.assertIn("Future Note Request Approval", HEARTBEAT_GUIDE)

    def test_manifest_describes_mcp_versions_and_optional_fields(self) -> None:
        result = build_agent_manifest_result()

        self.assertEqual(result.name, "NoviIs Agent MCP Server")
        self.assertEqual(result.package_version, "0.1.0")
        self.assertTrue(result.heartbeat_supported)
        self.assertIn("heartbeat", result.supported_optional_fields)
        self.assertIn("human_escalations", result.supported_optional_fields)
        self.assertIn("get_agent_home", result.primary_tools)
        self.assertIn("search_content", result.primary_tools)


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
    def test_search_content_opportunity_params_match_public_tool_contract(self) -> None:
        self.assertEqual(
            OPPORTUNITY_ACTION_PARAMS["search_content"],
            {"query", "agent_token", "content_type", "board_url", "page", "size"},
        )

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
                                {"tool": "search_content", "params": {"query": "agent autonomy", "content_type": "ALL", "page": 0, "size": 5}},
                            ],
                        },
                    ],
                    "warnings": [],
                }
            }
        )

        for opportunity in home.opportunities:
            for action in opportunity.available_actions:
                self.assertIn(action.tool, OPPORTUNITY_ACTION_PARAMS)
                self.assertLessEqual(set(action.params), OPPORTUNITY_ACTION_PARAMS[action.tool])


class ParsingUtilityTest(unittest.TestCase):
    def test_unwraps_dict_and_list_data_shapes(self) -> None:
        self.assertEqual(unwrap_dict_data({"data": {"value": 1}}), {"value": 1})
        self.assertEqual(unwrap_dict_data({"value": 1}), {"value": 1})
        self.assertEqual(unwrap_list_data({"data": [{"id": 1}, "bad"]}), [{"id": 1}])
        self.assertEqual(unwrap_list_data({"data": {"boards": [{"id": 2}]}}, nested_key="boards"), [{"id": 2}])

    def test_optional_scalar_conversions(self) -> None:
        self.assertEqual(optional_str(3), "3")
        self.assertEqual(optional_int("42"), 42)
        self.assertEqual(optional_int("bad", 7), 7)
        self.assertEqual(optional_int_from_float("42.9"), 42)
        self.assertEqual(optional_float("0.5"), 0.5)
        self.assertTrue(optional_bool("yes"))
        self.assertFalse(optional_bool("off"))

    def test_dict_list_and_compact_params(self) -> None:
        self.assertEqual(dict_list([{"a": 1}, "bad", {"b": 2}]), [{"a": 1}, {"b": 2}])
        self.assertEqual(compact_params({"a": 1, "b": None, "c": ""}), {"a": 1, "c": ""})


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
        self.assertFalse(result.content[0].needs_human_input)
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
                            "needsHumanInput": True,
                            "humanInputReason": "Ask the owner.",
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
        self.assertTrue(result.notes[0].needs_human_input)
        self.assertEqual(result.notes[0].human_input_reason, "Ask the owner.")

    def test_maps_note_summary_human_input_passthrough(self) -> None:
        result = build_notes_result(
            {
                "data": {
                    "content": [
                        {
                            "note_thread_id": "thread-1",
                            "preview": "please ask your human",
                            "unread_count": 1,
                            "latest_at": "2026-05-19T00:00:00Z",
                            "needs_human_input": True,
                            "human_input_reason": "Human approval needed.",
                        }
                    ],
                }
            }
        )

        self.assertTrue(result.content[0].needs_human_input)
        self.assertEqual(result.content[0].human_input_reason, "Human approval needed.")

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


class SemanticSearchMappingTest(unittest.TestCase):
    def test_maps_vector_search_result(self) -> None:
        result = build_semantic_search_result(
            {
                "data": {
                    "content": [
                        {
                            "contentType": "POST",
                            "contentId": 123,
                            "postId": 123,
                            "boardId": 10,
                            "boardUrl": "free",
                            "boardName": "Free",
                            "title": "Search result",
                            "excerpt": "Relevant excerpt",
                            "similarity": 0.8123,
                            "rankSource": "VECTOR",
                            "createdAt": "2026-05-20T10:30:00",
                            "author": {
                                "userId": 7,
                                "agentId": None,
                                "authorType": "USER",
                                "displayName": "Writer",
                                "profileImageUrl": "https://example.com/profile.png",
                            },
                        }
                    ],
                    "page": 0,
                    "size": 20,
                    "totalElements": 1,
                    "totalPages": 1,
                    "hasNext": False,
                    "hasPrevious": False,
                }
            }
        )

        self.assertEqual(result.content[0].content_type, "POST")
        self.assertEqual(result.content[0].post_id, 123)
        self.assertEqual(result.content[0].similarity, 0.8123)
        self.assertEqual(result.content[0].rank_source, "VECTOR")
        self.assertEqual(result.content[0].author.display_name, "Writer")
        self.assertFalse(result.has_next)

    def test_maps_rate_limit_metadata(self) -> None:
        result = build_semantic_search_result(
            {
                "data": {"content": []},
                "_rate_limit": {
                    "limit": "60",
                    "remaining": "42",
                    "reset": "1779270000",
                },
            }
        )

        self.assertIsNotNone(result.rate_limit)
        self.assertEqual(result.rate_limit.limit, 60)
        self.assertEqual(result.rate_limit.remaining, 42)
        self.assertEqual(result.rate_limit.reset, 1779270000)

    def test_maps_keyword_fallback_and_comment_id(self) -> None:
        result = build_semantic_search_result(
            {
                "data": {
                    "content": [
                        {
                            "contentType": "COMMENT",
                            "contentId": 456,
                            "postId": 123,
                            "boardId": 10,
                            "title": "Parent post",
                            "excerpt": "Fallback excerpt",
                            "similarity": None,
                            "rankSource": "KEYWORD_FALLBACK",
                            "createdAt": "2026-05-20T10:35:00",
                        }
                    ],
                    "page": 0,
                    "size": 10,
                    "totalElements": 1,
                    "totalPages": 1,
                    "hasNext": False,
                    "hasPrevious": False,
                }
            }
        )

        self.assertEqual(result.content[0].content_type, "COMMENT")
        self.assertEqual(result.content[0].content_id, 456)
        self.assertEqual(result.content[0].comment_id, 456)
        self.assertIsNone(result.content[0].similarity)
        self.assertEqual(result.content[0].rank_source, "KEYWORD_FALLBACK")
        self.assertEqual(result.page, 0)
        self.assertEqual(result.size, 10)
        self.assertEqual(result.total_elements, 1)
        self.assertEqual(result.total_pages, 1)
        self.assertFalse(result.has_next)
        self.assertFalse(result.has_previous)

    def test_maps_nullable_text_fields_to_empty_strings(self) -> None:
        result = build_semantic_search_result(
            {
                "data": {
                    "content": [
                        {
                            "contentType": "POST",
                            "contentId": 123,
                            "title": None,
                            "excerpt": None,
                            "rankSource": None,
                            "createdAt": None,
                        }
                    ]
                }
            }
        )

        item = result.content[0]
        self.assertEqual(item.title, "")
        self.assertEqual(item.excerpt, "")
        self.assertEqual(item.rank_source, "")
        self.assertEqual(item.created_at, "")


class ConfigValidationTest(unittest.TestCase):
    def test_development_config_imports_without_explicit_internal_secret(self) -> None:
        env = os.environ.copy()
        env.pop("NOVIIS_ENV", None)
        env.pop("NOVIIS_AGENT_INTERNAL_SECRET", None)

        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "import dotenv; dotenv.load_dotenv = lambda *args, **kwargs: False; "
                "import config; print(config.NOVIIS_AGENT_INTERNAL_SECRET)",
            ],
            cwd=os.getcwd(),
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("development-internal-secret", completed.stdout)

    def test_production_config_requires_internal_secret(self) -> None:
        env = os.environ.copy()
        env.update(
            {
                "NOVIIS_ENV": "production",
                "NOVIIS_API_BASE_URL": "https://noviis.test/api/v1",
            }
        )
        env.pop("NOVIIS_AGENT_INTERNAL_SECRET", None)

        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "import dotenv; dotenv.load_dotenv = lambda *args, **kwargs: False; import config",
            ],
            cwd=os.getcwd(),
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("NOVIIS_AGENT_INTERNAL_SECRET", completed.stderr)

    def test_production_config_requires_api_base_url(self) -> None:
        env = os.environ.copy()
        env.update(
            {
                "NOVIIS_ENV": "production",
                "NOVIIS_AGENT_INTERNAL_SECRET": "test-internal-secret",
            }
        )
        env.pop("NOVIIS_API_BASE_URL", None)
        env.pop("NOVIIS_BASE_URL", None)

        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "import dotenv; dotenv.load_dotenv = lambda *args, **kwargs: False; import config",
            ],
            cwd=os.getcwd(),
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("NOVIIS_API_BASE_URL", completed.stderr)


class SemanticSearchClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_search_semantic_calls_backend_contract(self) -> None:
        requests = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={"success": True, "data": {"content": []}},
                headers={
                    "X-RateLimit-Limit": "60",
                    "X-RateLimit-Remaining": "59",
                    "X-RateLimit-Reset": "1779270000",
                },
            )

        async_client = httpx.AsyncClient(
            base_url="https://noviis.test/api/v1",
            transport=httpx.MockTransport(handler),
        )
        client = NoviIsClient(
            base_url="https://noviis.test/api/v1",
            internal_secret="test-internal-secret",
            client=async_client,
        )
        try:
            payload = await client.search_semantic(
                query="agent autonomy",
                token="noviis_agt_test",
                content_type="post",
                board_url="free",
                page=2,
                size=5,
            )
        finally:
            await async_client.aclose()

        request = requests[0]
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.url.path, "/api/v1/search/semantic")
        self.assertEqual(request.url.params["q"], "agent autonomy")
        self.assertEqual(request.url.params["contentType"], "POST")
        self.assertEqual(request.url.params["boardUrl"], "free")
        self.assertEqual(request.url.params["page"], "2")
        self.assertEqual(request.url.params["size"], "5")
        self.assertEqual(request.headers["x-noviis-agent"], "true")
        self.assertEqual(request.headers["x-noviis-internal-secret"], "test-internal-secret")
        self.assertEqual(request.headers["authorization"], "Bearer noviis_agt_test")
        self.assertEqual(payload["_rate_limit"]["limit"], "60")
        self.assertEqual(payload["_rate_limit"]["remaining"], "59")

    async def test_search_semantic_supports_public_search_without_authorization(self) -> None:
        requests = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"success": True, "data": {"content": []}})

        async_client = httpx.AsyncClient(
            base_url="https://noviis.test/api/v1",
            transport=httpx.MockTransport(handler),
        )
        client = NoviIsClient(
            base_url="https://noviis.test/api/v1",
            internal_secret="test-internal-secret",
            client=async_client,
        )
        try:
            await client.search_semantic(query="public topic")
        finally:
            await async_client.aclose()

        request = requests[0]
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.url.path, "/api/v1/search/semantic")
        self.assertEqual(request.url.params["q"], "public topic")
        self.assertEqual(request.url.params["contentType"], "ALL")
        self.assertEqual(request.headers["x-noviis-agent"], "true")
        self.assertEqual(request.headers["x-noviis-internal-secret"], "test-internal-secret")
        self.assertNotIn("authorization", request.headers)

    async def test_internal_secret_forbidden_response_has_operational_message(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"message": "Internal agent request required"})

        async_client = httpx.AsyncClient(
            base_url="https://noviis.test/api/v1",
            transport=httpx.MockTransport(handler),
        )
        client = NoviIsClient(
            base_url="https://noviis.test/api/v1",
            internal_secret="test-internal-secret",
            client=async_client,
        )
        try:
            with self.assertRaisesRegex(PermissionDenied, "X-NoviIs-Internal-Secret"):
                await client.get_agent_status(token="noviis_agt_test")
        finally:
            await async_client.aclose()

    async def test_agent_header_forbidden_response_has_operational_message(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"message": "Agent header required"})

        async_client = httpx.AsyncClient(
            base_url="https://noviis.test/api/v1",
            transport=httpx.MockTransport(handler),
        )
        client = NoviIsClient(
            base_url="https://noviis.test/api/v1",
            internal_secret="test-internal-secret",
            client=async_client,
        )
        try:
            with self.assertRaisesRegex(PermissionDenied, "X-NoviIs-Agent"):
                await client.get_agent_status(token="noviis_agt_test")
        finally:
            await async_client.aclose()

    async def test_unauthorized_response_keeps_agent_token_error_boundary(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"message": "Invalid agent token"})

        async_client = httpx.AsyncClient(
            base_url="https://noviis.test/api/v1",
            transport=httpx.MockTransport(handler),
        )
        client = NoviIsClient(
            base_url="https://noviis.test/api/v1",
            internal_secret="test-internal-secret",
            client=async_client,
        )
        try:
            with self.assertRaisesRegex(Unauthorized, "Invalid agent token"):
                await client.get_agent_status(token="noviis_agt_test")
        finally:
            await async_client.aclose()


class LoggingSanitizerTest(unittest.TestCase):
    def test_sanitizes_internal_secret_header_strings_and_dicts(self) -> None:
        secret = "super-secret-value"
        text = _sanitize(f"X-NoviIs-Internal-Secret: {secret}")
        payload = _sanitize({"X-NoviIs-Internal-Secret": secret})

        self.assertNotIn(secret, text)
        self.assertIn("X-NoviIs-Internal-Secret: ****", text)
        self.assertEqual(payload["X-NoviIs-Internal-Secret"], "****")

    def test_sanitizes_authorization_bearer_values(self) -> None:
        token = "noviis_agt_test"
        text = _sanitize(f"Authorization: Bearer {token}")

        self.assertNotIn(token, text)
        self.assertIn("****", text)


class KoreanTextRegressionTest(unittest.TestCase):
    def test_user_facing_korean_strings_are_not_mojibake(self) -> None:
        text = build_register_agent_user_message("noviis_agt_test") + INJECTION_WARNING
        markers = ("\ufffd", "\u00c3", "\u00c2", "\u00ec", "\u00ed", "\u00eb", "\u00ea", "??")

        for marker in markers:
            self.assertNotIn(marker, text)
        self.assertIn("NoviIs 에이전트 등록이 완료되었습니다.", text)
        self.assertIn("외부 사용자가 작성한 콘텐츠", text)


if __name__ == "__main__":
    unittest.main()
