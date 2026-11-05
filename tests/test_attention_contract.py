from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from mcp.server.fastmcp.exceptions import ToolError

from exceptions import BackendContractError
from tools.home import build_agent_home_result
from tools.rules import build_agent_rules_result
from tools.tool_contract import tool_contracts


HOME = json.loads((Path(__file__).parent / "fixtures/backend/agent-home-board-contract.json").read_text(encoding="utf-8"))["home"]["response"]


def home(**fields):
    payload = deepcopy(HOME)
    payload["data"].update(fields)
    return payload


class AttentionContractTest(unittest.TestCase):
    def test_actual_wire_fixture_still_maps(self):
        result = build_agent_home_result(deepcopy(HOME))
        self.assertEqual(result.note_summary.unread_note_count, 0)
        self.assertIsInstance(result.heartbeat.reasons, list)

    def test_warnings_require_a_list_of_nonempty_strings(self):
        for value in (None, "warning", {}, True, [1], [None], [""], ["  "]):
            with self.subTest(value=value), self.assertRaises(BackendContractError):
                build_agent_home_result(home(warnings=value))
        result = build_agent_home_result(home(warnings=["Needs attention"]))
        self.assertEqual(result.heartbeat.urgency, "urgent")
        self.assertIn("Needs attention", [item.summary for item in result.human_escalations])

    def test_optional_absent_and_null_sections_preserve_legacy_defaults(self):
        payload = home(warnings=[], activity_on_my_posts=[], heartbeat=None,
                       human_escalations=None, note_summary=None)
        result = build_agent_home_result(payload)
        self.assertIsNone(result.note_summary)
        self.assertEqual(result.human_escalations, [])
        self.assertEqual(result.heartbeat.urgency, "normal")
        self.assertEqual(result.heartbeat.recommended_interval_seconds, 1800)
        for key in ("heartbeat", "human_escalations", "note_summary"):
            payload["data"].pop(key)
        self.assertEqual(build_agent_home_result(payload).heartbeat.urgency, "normal")

    def test_note_summary_rejects_invalid_counts_and_shapes(self):
        for value in ([], "notes", False, {}, {"unread_note_count": 1}):
            with self.subTest(value=value), self.assertRaises(BackendContractError):
                build_agent_home_result(home(note_summary=value))
        for key in ("unread_note_count", "unread_thread_count"):
            for value in (None, "1", True, -1, 1.1, [], 9223372036854775808):
                with self.subTest(key=key, value=value), self.assertRaises(BackendContractError):
                    build_agent_home_result(home(note_summary={"unread_note_count": 0, "unread_thread_count": 0, key: value}))
        result = build_agent_home_result(home(note_summary={"unreadNoteCount": 2, "unreadThreadCount": 1}))
        self.assertIn("unread_notes", result.heartbeat.reasons)

    def test_activity_unread_metadata_cannot_silently_erase_attention(self):
        for value in (None, "activity", {}, [None], [{"unread_count": None}], [{"commentCount": None}], [{"unread_count": "2"}],
                      [{"newCommentCount": False}], [{"comment_count": -1}]):
            with self.subTest(value=value), self.assertRaises(BackendContractError):
                build_agent_home_result(home(activity_on_my_posts=value))
        result = build_agent_home_result(home(activity_on_my_posts=[{"post_id": "1", "newCommentCount": 2}]))
        self.assertIn("activity_on_my_posts", result.heartbeat.reasons)

    def test_section_and_count_alias_conflicts_are_rejected(self):
        for fields in (
            {"note_summary": {}, "noteSummary": {"unread_note_count": 1}},
            {"human_escalations": [], "humanEscalations": [{}]},
            {"activity_on_my_posts": [], "activityOnMyPosts": [{}]},
            {"note_summary": {"unread_note_count": 1, "unreadNoteCount": 2}},
            {"activity_on_my_posts": [{"unread_count": 1, "newCommentCount": 2}]},
        ):
            with self.subTest(fields=fields), self.assertRaises(BackendContractError):
                build_agent_home_result(home(**fields))

    def test_escalations_require_objects_and_required_text(self):
        for value in ("approval", {}, [None], [{}], [{"type": "approval"}],
                      [{"type": [], "summary": "Approve"}], [{"type": "approval", "summary": ""}]):
            with self.subTest(value=value), self.assertRaises(BackendContractError):
                build_agent_home_result(home(human_escalations=value))

    def test_escalation_optional_fields_reject_wrong_kinds(self):
        for key in ("reason", "severity", "target_type", "target_id"):
            for value in (True, [], {}):
                with self.subTest(key=key, value=value), self.assertRaises(BackendContractError):
                    build_agent_home_result(home(human_escalations=[{"type": "approval", "summary": "Approve", key: value}]))
        for value in (-1, 0, 9223372036854775808):
            with self.subTest(value=value), self.assertRaises(BackendContractError):
                build_agent_home_result(home(human_escalations=[{"type": "approval", "summary": "Approve", "target_id": value}]))
        item = build_agent_home_result(home(human_escalations=[{"type": "approval", "summary": "Approve", "targetId": 12, "reason": None}])).human_escalations[0]
        self.assertEqual(item.target_id, "12")
        self.assertEqual(item.severity, "medium")

    def test_heartbeat_wrong_shape_and_fields_are_rejected(self):
        for value in ([], "normal", False):
            with self.subTest(value=value), self.assertRaises(BackendContractError):
                build_agent_home_result(home(heartbeat=value))
        for fields in ({"urgency": True}, {"reasons": "alert"}, {"reasons": [1]},
                       {"recommended_interval_seconds": "600"}, {"recommended_interval_seconds": 0},
                       {"recommended_interval_seconds": -1}, {"recommended_interval_seconds": True},
                       {"primaryTool": {}}, {"reportStyle": []}, {"nextCheckAfter": "invalid"},
                       {"recommended_interval_seconds": 600, "recommendedIntervalSeconds": 1800}):
            with self.subTest(fields=fields), self.assertRaises(BackendContractError):
                build_agent_home_result(home(heartbeat=fields))

    def test_valid_camel_case_heartbeat_is_preserved(self):
        result = build_agent_home_result(home(heartbeat={"recommendedIntervalSeconds": 600,
            "urgency": "attention", "reasons": ["backend_reason"], "primaryTool": "get_notes",
            "reportStyle": "short", "nextCheckAfter": "2026-11-05T12:00:00+09:00"}))
        self.assertEqual(result.heartbeat.recommended_interval_seconds, 600)
        self.assertEqual(result.heartbeat.primary_tool, "get_notes")
        self.assertIn("backend_reason", result.heartbeat.reasons)

    def test_rules_reject_malformed_sections_and_items(self):
        for key in ("hard_constraints", "soft_guidance", "style_guidance"):
            for value in (None, "rule", {}, False, [None], [{}], [{"code": "x", "description": 1}],
                          [{"code": "", "description": "x"}], [{"code": "x", "description": "x", "severity": []}]):
                with self.subTest(key=key, value=value), self.assertRaises(BackendContractError):
                    build_agent_rules_result({key: value})

    def test_rules_optional_legacy_fields_and_valid_rules(self):
        self.assertEqual(build_agent_rules_result({}).hard_constraints, [])
        rule = {"code": "limit", "description": "Respect quota", "severity": None}
        result = build_agent_rules_result({"hardConstraints": [rule], "hard_constraints": [rule]})
        self.assertEqual(result.hard_constraints[0].code, "limit")
        with self.assertRaises(BackendContractError):
            build_agent_rules_result({"hardConstraints": [rule], "hard_constraints": []})

    def test_wrong_envelopes_and_rule_metadata_are_rejected(self):
        for value in (None, [], "invalid"):
            for builder in (build_agent_home_result, build_agent_rules_result):
                with self.subTest(value=value, builder=builder.__name__), self.assertRaises(BackendContractError):
                    builder({"data": value})
        for key in ("title", "version"):
            with self.subTest(key=key), self.assertRaises(BackendContractError):
                build_agent_rules_result({key: 1})


class AttentionSdkContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_reports_contract_error_instead_of_normal_state(self):
        cases = [("get_agent_home", home(warnings="needs attention")),
                 ("get_agent_home", home(note_summary={"unread_note_count": "2"})),
                 ("get_agent_home", home(human_escalations="approval")),
                 ("get_agent_rules", {"data": {"hard_constraints": "must stop"}})]
        for name, payload in cases:
            client = SimpleNamespace(**{name: AsyncMock(return_value=payload)})
            ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=SimpleNamespace(client=client)))
            with self.subTest(tool=name, payload=payload), self.assertRaisesRegex(ToolError, "backend_contract_error"):
                await tool_contracts()[name].run({"agent_token": "synthetic-attention-token"}, context=ctx)
            getattr(client, name).assert_awaited_once()
