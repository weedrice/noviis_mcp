from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from mcp.server.fastmcp.exceptions import ToolError

from exceptions import BackendContractError
from tools.constraint_contract import validate_hard_constraints
from tools.home import build_agent_home_result
from tools.tool_contract import tool_contracts


RESET = "2030-01-01T00:00:00+09:00"


def status_payload():
    return {"status": "active", "name": "synthetic",
            "stats": {"posts_today": 2, "comments_today": 3, "reset_at": RESET},
            "limits": {"max_posts_per_day": 50, "max_comments_per_day": 100,
                       "posts_remaining": 48, "comments_remaining": 97,
                       "max_notes_per_day": 20, "notes_remaining": 0,
                       "next_note_allowed_at": RESET},
            "restrictions": {"can_post": True, "can_comment": True,
                             "is_suspended": False, "can_send_note": False}}


def home_payload():
    fixture = Path(__file__).parent / "fixtures/backend/agent-home-board-contract.json"
    return json.loads(fixture.read_text(encoding="utf-8"))["home"]["response"]


async def run_status(payload):
    backend = AsyncMock()
    backend.get_agent_status.return_value = {"data": payload}
    ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=SimpleNamespace(client=backend)))
    return await tool_contracts()["get_agent_status"].run({"agent_token": "synthetic"}, context=ctx)


class StatusPolicyContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_preserves_exhausted_note_quota_and_denied_permission(self):
        result = await run_status(status_payload())
        self.assertEqual(result.limits.max_notes_per_day, 20)
        self.assertEqual(result.limits.notes_remaining, 0)
        self.assertEqual(result.limits.next_note_allowed_at, RESET)
        self.assertIs(result.restrictions.can_send_note, False)

    async def test_missing_core_contract_never_invents_quota_or_permission(self):
        for path in (("stats",), ("limits",), ("restrictions",), ("status",), ("name",),
                     ("stats", "posts_today"), ("stats", "comments_today"), ("stats", "reset_at"),
                     ("limits", "max_posts_per_day"), ("limits", "max_comments_per_day"),
                     ("limits", "posts_remaining"), ("limits", "comments_remaining"),
                     ("restrictions", "can_post"), ("restrictions", "can_comment"), ("restrictions", "is_suspended")):
            payload = status_payload()
            target = payload if len(path) == 1 else payload[path[0]]
            del target[path[-1]]
            with self.subTest(path=path), self.assertRaisesRegex(ToolError, "backend_contract_error"):
                await run_status(payload)

    async def test_invalid_policy_types_values_and_timestamps_are_rejected(self):
        for section, key, values in (
            (None, "status", (None, "", "pending_claim", "unexpected", 1)),
            (None, "name", (None, "", 1)),
            (None, "stats", (None, [], "bad")),
            (None, "limits", (None, [], "bad")),
            (None, "restrictions", (None, [], "bad")),
            ("stats", "posts_today", (True, 1.5, "2", -1)),
            ("stats", "reset_at", (None, "", "tomorrow", "2030-01-01", 1)),
            ("limits", "posts_remaining", (True, 1.5, "2", -1, 51)),
            ("limits", "notes_remaining", (None, True, 1.5, "2", -1, 21)),
            ("restrictions", "can_post", (None, 1, "true")),
            ("restrictions", "can_send_note", (None, 1, "false")),
        ):
            for value in values:
                payload = status_payload()
                (payload if section is None else payload[section])[key] = value
                with self.subTest(section=section, key=key, value=value), self.assertRaisesRegex(ToolError, "backend_contract_error"):
                    await run_status(payload)

    async def test_aliases_are_supported_but_conflicts_rejected(self):
        payload = status_payload()
        for section in ("stats", "limits", "restrictions"):
            payload[section] = {key.split("_")[0] + "".join(x.title() for x in key.split("_")[1:]): value
                                for key, value in payload[section].items()}
        self.assertEqual((await run_status(payload)).limits.notes_remaining, 0)
        for section, key, value in (("limits", "notes_remaining", 1), ("restrictions", "can_send_note", True),
                                    ("stats", "posts_today", True), ("limits", "next_note_allowed_at", None)):
            conflicting = deepcopy(payload)
            conflicting[section][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ToolError, "backend_contract_error"):
                await run_status(conflicting)

    async def test_suspension_and_quota_contradictions_fail_closed(self):
        for section, key, value in ((None, "status", "suspended"), ("restrictions", "is_suspended", True),
                                    ("limits", "posts_remaining", 0), ("limits", "comments_remaining", 0),
                                    ("restrictions", "can_send_note", True)):
            payload = status_payload()
            (payload if section is None else payload[section])[key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ToolError, "backend_contract_error"):
                await run_status(payload)
        payload = status_payload()
        payload["restrictions"].update(is_suspended=True, can_post=False, can_comment=False)
        for status in ("active", "suspended", "claimed"):
            payload["status"] = status
            self.assertTrue((await run_status(payload)).restrictions.is_suspended)

    async def test_older_backend_missing_notes_is_unknown_never_allowed(self):
        payload = status_payload()
        for name in ("max_notes_per_day", "notes_remaining", "next_note_allowed_at"):
            del payload["limits"][name]
        del payload["restrictions"]["can_send_note"]
        result = await run_status(payload)
        self.assertIsNone(result.limits.notes_remaining)
        self.assertIsNone(result.limits.max_notes_per_day)
        self.assertIsNone(result.restrictions.can_send_note)


class HomeNotePolicyContractTest(unittest.TestCase):
    def test_generated_backend_home_preserves_note_policy(self):
        payload = home_payload()
        result = build_agent_home_result(payload)
        raw = payload["data"]
        self.assertEqual(result.usage.max_notes_per_day, raw["usage"]["max_notes_per_day"])
        self.assertEqual(result.usage.notes_remaining, raw["usage"]["notes_remaining"])
        self.assertEqual(result.hard_constraints.can_send_note, raw["hard_constraints"]["can_send_note"])
        self.assertEqual(result.hard_constraints.note_daily_limit, raw["hard_constraints"]["note_daily_limit"])
        capability = next(c for c in result.capabilities if c.name == "send_note")
        self.assertEqual(capability.notes_remaining, 20)

    def test_exhausted_notes_and_camel_aliases_preserved_everywhere(self):
        payload = home_payload()
        raw = payload["data"]
        for section in (raw["usage"], raw["hard_constraints"], raw["capabilities"]["send_note"]):
            section.pop("notes_remaining", None)
            section.pop("next_note_allowed_at", None)
            section["notesRemaining"] = 0
            section["nextNoteAllowedAt"] = RESET
        raw["hard_constraints"]["can_send_note"] = False
        raw["capabilities"]["send_note"]["available"] = False
        result = build_agent_home_result(payload)
        capability = next(c for c in result.capabilities if c.name == "send_note")
        for section in (result.usage, result.hard_constraints, capability):
            self.assertEqual(section.notes_remaining, 0)
            self.assertEqual(section.next_note_allowed_at, RESET)
        self.assertFalse(result.hard_constraints.can_send_note)

    def test_present_malformed_or_conflicting_note_policy_fails(self):
        for section, name, value in (("usage", "notes_remaining", "20"), ("usage", "max_notes_per_day", True),
                                     ("hard_constraints", "can_send_note", "false"),
                                     ("hard_constraints", "notesRemaining", 0),
                                     ("hard_constraints", "next_note_allowed_at", "later")):
            payload = home_payload()
            payload["data"][section][name] = value
            with self.subTest(section=section, name=name), self.assertRaises(BackendContractError):
                build_agent_home_result(payload)
        payload = home_payload()
        payload["data"]["capabilities"]["send_note"]["notes_remaining"] = True
        with self.assertRaises(BackendContractError):
            build_agent_home_result(payload)

    def test_old_home_note_fields_are_unknown_and_contradictions_rejected(self):
        payload = home_payload()
        for section in (payload["data"]["usage"], payload["data"]["hard_constraints"], *payload["data"]["capabilities"].values()):
            for name in ("can_send_note", "notes_remaining", "note_daily_limit", "max_notes_per_day", "next_note_allowed_at"):
                section.pop(name, None)
        result = build_agent_home_result(payload)
        self.assertIsNone(result.hard_constraints.can_send_note)
        self.assertIsNone(result.usage.notes_remaining)
        self.assertTrue(all(c.notes_remaining is None for c in result.capabilities))
        for patch in ({"suspended": True}, {"posts_remaining": 0}, {"comments_remaining": 0}):
            constraints = {**payload["data"]["hard_constraints"], **patch}
            with self.subTest(patch=patch), self.assertRaises(BackendContractError):
                validate_hard_constraints(constraints)
