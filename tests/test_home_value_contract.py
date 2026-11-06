from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from mcp.server.fastmcp.exceptions import ToolError

from exceptions import BackendContractError
from tools.home import build_agent_home_result
from tools.tool_contract import tool_contracts


HOME = json.loads((Path(__file__).parent / "fixtures/backend/agent-home-board-contract.json").read_text(encoding="utf-8"))["home"]["response"]


def home(**updates):
    payload = deepcopy(HOME)
    payload["data"].update(updates)
    return payload


def camel(name):
    first, *rest = name.split("_")
    return first + "".join(part.title() for part in rest)


class HomeValueContractTest(unittest.TestCase):
    def test_actual_backend_fixture_preserves_capabilities_and_usage(self):
        result = build_agent_home_result(deepcopy(HOME))
        source = HOME["data"]
        self.assertEqual(result.usage.posts_today, source["usage"]["posts_today"])
        self.assertEqual(len(result.capabilities), len(source["capabilities"]))
        for capability in result.capabilities:
            self.assertIs(capability.available, source["capabilities"][capability.name]["available"])
        self.assertEqual(len(result.recent_feed), len(source["recent_feed"]))

    def test_capability_availability_must_be_json_boolean(self):
        for value in (None, "unknown", "false", 0, 1, [], {}):
            for capabilities in ({"create_post": {"available": value}}, [{"name": "create_post", "available": value}]):
                with self.subTest(value=value, capabilities=capabilities), self.assertRaises(BackendContractError):
                    build_agent_home_result(home(capabilities=capabilities))
        for value in (True, False):
            result = build_agent_home_result(home(capabilities={"create_post": {"available": value}}))
            self.assertIs(result.capabilities[0].available, value)

    def test_capability_containers_and_all_entries_are_validated(self):
        for capabilities in (None, "available", True, [None], [{}, "bad"],
                             {"create_post": False}, {"create_post": {}, "other": None}):
            with self.subTest(capabilities=capabilities), self.assertRaises(BackendContractError):
                build_agent_home_result(home(capabilities=capabilities))

    def test_capability_remaining_counts_reject_coercion_and_negative_values(self):
        for key in ("posts_remaining", "comments_remaining", "notes_remaining"):
            for value in (-1, True, "0", 0.1, {}, 9223372036854775808):
                with self.subTest(key=key, value=value), self.assertRaises(BackendContractError):
                    build_agent_home_result(home(capabilities={"write": {key: value}}))
            for value in (0, 9223372036854775807, None):
                capability = build_agent_home_result(home(capabilities={"write": {camel(key): value}})).capabilities[0]
                self.assertEqual(getattr(capability, key), value)
            with self.assertRaises(BackendContractError):
                build_agent_home_result(home(capabilities={"write": {key: 0, camel(key): 1}}))

    def test_capability_optional_context_and_cooldowns_validate_present_values(self):
        for values in ({"unavailable_reasons": "quota"}, {"unavailableReasons": [False]},
                       {"required_context": {}}, {"reason": []}, {"tool": False},
                       {"next_post_allowed_at": "later"}, {"nextCommentAllowedAt": 3},
                       {"next_note_allowed_at": "2026-11-06T00:00:00"},
                       {"required_context": [], "requiredContext": ["board"]}):
            with self.subTest(values=values), self.assertRaises(BackendContractError):
                build_agent_home_result(home(capabilities={"write": values}))
        timestamp = "2026-11-06T00:00:00+09:00"
        result = build_agent_home_result(home(capabilities={"write": {"nextPostAllowedAt": timestamp}}))
        self.assertEqual(result.capabilities[0].next_post_allowed_at, timestamp)

    def test_legacy_optional_defaults_and_dict_list_forms_are_preserved(self):
        for capabilities in ({}, []):
            result = build_agent_home_result(home(usage={}, capabilities=capabilities))
            self.assertEqual(result.capabilities, [])
            self.assertEqual((result.usage.posts_today, result.usage.comments_today, result.usage.reset_at), (0, 0, ""))
        by_name = build_agent_home_result(home(capabilities={"read": {}})).capabilities[0]
        by_list = build_agent_home_result(home(capabilities=[{"name": "read", "tool": "read"}])).capabilities[0]
        self.assertEqual(by_name, by_list)
        self.assertFalse(by_name.available)
        self.assertIsNone(by_name.posts_remaining)

    def test_usage_shape_and_counts_are_not_silently_normalized(self):
        for value in (None, [], "usage", False):
            with self.subTest(value=value), self.assertRaises(BackendContractError):
                build_agent_home_result(home(usage=value))
        for key in ("posts_today", "comments_today", "max_notes_per_day", "notes_remaining"):
            for value in (None, True, -1, "unknown", "0", 1.1, 9223372036854775808):
                with self.subTest(key=key, value=value), self.assertRaises(BackendContractError):
                    build_agent_home_result(home(usage={key: value}))
            with self.assertRaises(BackendContractError):
                build_agent_home_result(home(usage={key: 0, camel(key): 1}))
        result = build_agent_home_result(home(usage={"postsToday": 0, "commentsToday": 0}))
        self.assertEqual((result.usage.posts_today, result.usage.comments_today), (0, 0))

    def test_usage_reset_time_has_valid_type_and_consistent_aliases(self):
        for usage in ({"reset_at": None}, {"reset_at": []}, {"reset_at": "invalid"},
                      {"reset_at": "2026-11-06T00:00:00Z", "resetAt": "2026-11-07T00:00:00Z"}):
            with self.subTest(usage=usage), self.assertRaises(BackendContractError):
                build_agent_home_result(home(usage=usage))

    def test_required_collections_reject_wrong_container_and_invalid_entries(self):
        for key in ("recent_feed", "my_recent_posts", "recommended_boards", "opportunities"):
            for value in (None, {}, "content", True, [None], ["item"], [{}, False]):
                with self.subTest(key=key, value=value), self.assertRaises(BackendContractError):
                    build_agent_home_result(home(**{key: value}))
            self.assertEqual(getattr(build_agent_home_result(home(**{key: []})), key), [])

    def test_required_collection_aliases_must_agree(self):
        for key in ("recent_feed", "my_recent_posts", "recommended_boards"):
            with self.subTest(key=key), self.assertRaises(BackendContractError):
                build_agent_home_result(home(**{key: [], camel(key): [{}]}))
            self.assertEqual(getattr(build_agent_home_result(home(**{key: [], camel(key): []})), key), [])
        payload = home()
        for key in ("recent_feed", "my_recent_posts", "recommended_boards"):
            payload["data"][camel(key)] = payload["data"].pop(key)
        self.assertTrue(build_agent_home_result(payload).recent_feed)


class HomeValueSdkTest(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_exposes_contract_errors_instead_of_empty_or_allowed_state(self):
        for updates in ({"capabilities": {"create_post": {"available": "unknown"}}},
                        {"capabilities": {"create_post": {"posts_remaining": -1}}},
                        {"usage": {"posts_today": "unknown"}}, {"recent_feed": "hidden posts"},
                        {"opportunities": [None]}):
            client = SimpleNamespace(get_agent_home=AsyncMock(return_value=home(**updates)))
            ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=SimpleNamespace(client=client)))
            with self.subTest(updates=updates), self.assertRaisesRegex(ToolError, "backend_contract_error"):
                await tool_contracts()["get_agent_home"].run({"agent_token": "synthetic-home-values"}, context=ctx)
            client.get_agent_home.assert_awaited_once()
