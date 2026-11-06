from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from tools.home import build_agent_home_result
from tools.tool_contract import tool_contracts


HOME = json.loads((Path(__file__).parent / "fixtures/backend/agent-home-board-contract.json").read_text(encoding="utf-8"))["home"]["response"]


def home(**updates):
    payload = deepcopy(HOME)
    payload["data"].update(activity_on_my_posts=[], warnings=[],
                           note_summary={"unread_thread_count": 0, "unread_note_count": 0})
    payload["data"].update(updates)
    return payload


def escalation(severity="medium"):
    return {"type": "approval_required", "summary": "Human approval is required", "severity": severity}


class HeartbeatEscalationsTest(unittest.TestCase):
    def test_low_medium_and_default_escalations_require_attention(self):
        for item in (escalation("low"), escalation("medium"),
                     {"type": "approval_required", "summary": "Human approval is required"}):
            result = build_agent_home_result(home(human_escalations=[item]))
            self.assertEqual(result.heartbeat.urgency, "attention")
            self.assertEqual(result.heartbeat.reasons, ["human_escalations"])
            self.assertEqual(result.human_escalations[0].summary, item["summary"])

    def test_high_and_critical_escalations_require_urgent(self):
        for severity in ("high", "critical", "HIGH"):
            result = build_agent_home_result(home(human_escalations=[escalation(severity)]))
            self.assertEqual(result.heartbeat.urgency, "urgent")
            self.assertEqual(result.human_escalations[0].severity, severity)
        mixed = build_agent_home_result(home(human_escalations=[escalation("low"), escalation("high")]))
        self.assertEqual(mixed.heartbeat.urgency, "urgent")
        self.assertEqual(mixed.heartbeat.reasons.count("human_escalations"), 1)

    def test_explicit_normal_cannot_lower_escalation_and_higher_urgency_is_preserved(self):
        for severity, minimum in (("low", "attention"), ("medium", "attention"), ("high", "urgent")):
            result = build_agent_home_result(home(human_escalations=[escalation(severity)], heartbeat={"urgency": "normal"}))
            self.assertEqual(result.heartbeat.urgency, minimum)
            result = build_agent_home_result(home(human_escalations=[escalation(severity)], heartbeat={"urgency": "urgent"}))
            self.assertEqual(result.heartbeat.urgency, "urgent")

    def test_unknown_urgency_normalized_only_when_escalation_exists(self):
        for severity, minimum in (("medium", "attention"), ("critical", "urgent")):
            result = build_agent_home_result(home(human_escalations=[escalation(severity)], heartbeat={"urgency": "custom_code"}))
            self.assertEqual(result.heartbeat.urgency, minimum)
        self.assertEqual(build_agent_home_result(home(heartbeat={"urgency": "custom_code"})).heartbeat.urgency, "custom_code")
        self.assertEqual(build_agent_home_result(home(human_escalations=[escalation("custom_severity")])).heartbeat.urgency, "attention")

    def test_no_escalations_preserve_normal_state_and_optional_defaults(self):
        for fields in ({}, {"human_escalations": None}, {"human_escalations": []}):
            result = build_agent_home_result(home(**fields))
            self.assertEqual(result.heartbeat.urgency, "normal")
            self.assertEqual(result.heartbeat.reasons, [])
            self.assertEqual(result.heartbeat.recommended_interval_seconds, 1800)
        result = build_agent_home_result(home(heartbeat={"urgency": "urgent", "reasons": ["backend_reason"]}))
        self.assertEqual(result.heartbeat.urgency, "urgent")
        self.assertEqual(result.heartbeat.reasons, ["backend_reason"])

    def test_warning_and_suspension_escalations_preserve_existing_urgent_defaults(self):
        warning = build_agent_home_result(home(warnings=["Operational warning"]))
        self.assertEqual(warning.heartbeat.urgency, "urgent")
        self.assertEqual(warning.heartbeat.reasons, ["warnings", "human_escalations"])
        payload = home()
        payload["data"]["hard_constraints"].update(suspended=True, can_create_post=False,
                                                  can_create_comment=False, can_send_note=False)
        suspended = build_agent_home_result(payload)
        self.assertEqual(suspended.heartbeat.urgency, "urgent")
        self.assertIn("agent_suspended", suspended.heartbeat.reasons)
        self.assertIn("human_escalations", suspended.heartbeat.reasons)
        # Keep an existing explicit warning urgency if it meets the medium
        # escalation minimum; do not expand other warning override semantics.
        explicit = build_agent_home_result(home(warnings=["Warning"], heartbeat={"urgency": "attention"}))
        self.assertEqual(explicit.heartbeat.urgency, "attention")

    def test_reason_is_deduplicated_and_backend_schedule_fields_preserved(self):
        result = build_agent_home_result(home(human_escalations=[escalation()], heartbeat={
            "urgency": "normal", "reasons": ["backend_reason", "human_escalations"],
            "recommended_interval_seconds": 600, "primary_tool": "get_notes",
            "report_style": "short_summary", "next_check_after": "2026-11-06T10:00:00+09:00"}))
        self.assertEqual(result.heartbeat.reasons, ["backend_reason", "human_escalations"])
        self.assertEqual(result.heartbeat.recommended_interval_seconds, 600)
        self.assertEqual(result.heartbeat.primary_tool, "get_notes")
        self.assertEqual(result.heartbeat.report_style, "short_summary")
        self.assertEqual(result.heartbeat.next_check_after, "2026-11-06T10:00:00+09:00")


class HeartbeatEscalationsSdkTest(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_exposes_escalation_and_matching_heartbeat(self):
        for severity, expected in (("low", "attention"), ("high", "urgent")):
            client = SimpleNamespace(get_agent_home=AsyncMock(return_value=home(
                human_escalations=[escalation(severity)], heartbeat={"urgency": "normal"})))
            ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=SimpleNamespace(client=client)))
            result = await tool_contracts()["get_agent_home"].run({"agent_token": "synthetic-heartbeat"}, context=ctx)
            self.assertEqual(result.heartbeat.urgency, expected)
            self.assertIn("human_escalations", result.heartbeat.reasons)
            self.assertEqual(result.human_escalations[0].type, "approval_required")
            client.get_agent_home.assert_awaited_once()
