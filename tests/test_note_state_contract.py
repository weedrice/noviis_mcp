from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from mcp.server.fastmcp.exceptions import ToolError

from exceptions import BackendContractError
from tools.notes import build_note_thread_result, build_notes_result
from tools.tool_contract import tool_contracts


def summary(**fields):
    return build_notes_result({"data": {"content": [{"note_thread_id": 12, **fields}]}}).content[0]


def note(**fields):
    return build_note_thread_result({"data": {"note_thread_id": 12, "notes": [
        {"note_id": 34, "content": "Review this note", **fields}
    ]}}).notes[0]


class NoteStateContractTest(unittest.TestCase):
    def test_unread_count_requires_nonnegative_long_integer(self):
        for value in (None, True, False, "0", "unknown", -1, 0.0, 1.5, [], {}, 9223372036854775808):
            for key in ("unread_count", "unreadCount"):
                with self.subTest(value=value, key=key), self.assertRaises(BackendContractError):
                    summary(**{key: value})
        for value in (0, 1, 9223372036854775807):
            self.assertEqual(summary(unread_count=value).unread_count, value)
            self.assertEqual(summary(unreadCount=value).unread_count, value)

    def test_unread_count_aliases_must_agree_in_value_and_type(self):
        for fields in ({"unread_count": 0, "unreadCount": 1},
                       {"unread_count": 0, "unreadCount": False},
                       {"unread_count": 0, "unreadCount": None}):
            with self.subTest(fields=fields), self.assertRaises(BackendContractError):
                summary(**fields)
        self.assertEqual(summary(unread_count=0, unreadCount=0).unread_count, 0)

    def test_read_and_human_attention_flags_require_real_json_boolean(self):
        for builder, keys in ((note, ("is_read", "isRead", "needs_human_input", "needsHumanInput")),
                              (summary, ("needs_human_input", "needsHumanInput"))):
            for key in keys:
                for value in (None, 0, 1, 0.0, "false", "unread", "pending", [], {}, {"read": False}):
                    with self.subTest(builder=builder.__name__, key=key, value=value), self.assertRaises(BackendContractError):
                        builder(**{key: value})

    def test_boolean_aliases_must_agree(self):
        for builder, snake, camel in ((note, "is_read", "isRead"),
                                     (note, "needs_human_input", "needsHumanInput"),
                                     (summary, "needs_human_input", "needsHumanInput")):
            for other in (True, 0, None):
                with self.subTest(builder=builder.__name__, key=snake, other=other), self.assertRaises(BackendContractError):
                    builder(**{snake: False, camel: other})
            self.assertIs(getattr(builder(**{snake: False, camel: False}), snake), False)
            self.assertIs(getattr(builder(**{camel: True}), snake), True)

    def test_omitted_legacy_metadata_preserves_defaults(self):
        self.assertEqual(summary().unread_count, 0)
        for result in (summary(), note()):
            self.assertIs(result.needs_human_input, False)
            self.assertIsNone(result.human_input_reason)
        self.assertIs(note().is_read, False)

    def test_human_reason_is_nullable_text_with_consistent_aliases(self):
        for builder in (summary, note):
            for value in (False, 1, [], {}):
                with self.subTest(builder=builder.__name__, value=value), self.assertRaises(BackendContractError):
                    builder(human_input_reason=value)
            for left, right in (("approve", "reject"), (None, "approve"), ("approve", False)):
                with self.subTest(left=left, right=right), self.assertRaises(BackendContractError):
                    builder(human_input_reason=left, humanInputReason=right)
            self.assertIsNone(builder(human_input_reason=None, humanInputReason=None).human_input_reason)
            self.assertEqual(builder(humanInputReason="Approve request").human_input_reason, "Approve request")


class NoteStateSdkContractTest(unittest.IsolatedAsyncioTestCase):
    async def call_tool(self, name, data):
        client = SimpleNamespace(**{name: AsyncMock(return_value={"data": data})})
        ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=SimpleNamespace(client=client)))
        args = {"agent_token": "synthetic-note-state-token"}
        if name == "get_note_thread":
            args["note_thread_id"] = 12
        return await tool_contracts()[name].run(args, context=ctx)

    async def test_sdk_rejects_corrupt_read_and_attention_state(self):
        for name, data in (
            ("get_notes", {"content": [{"note_thread_id": 12, "unread_count": "unknown"}]}),
            ("get_notes", {"content": [{"note_thread_id": 12, "needs_human_input": []}]}),
            ("get_note_thread", {"note_thread_id": 12, "notes": [{"note_id": 34, "is_read": "unread"}]}),
            ("get_note_thread", {"note_thread_id": 12, "notes": [{"note_id": 34, "needs_human_input": {}}]}),
        ):
            with self.subTest(tool=name, data=data), self.assertRaisesRegex(ToolError, "backend_contract_error"):
                await self.call_tool(name, data)

    async def test_sdk_preserves_zero_false_and_attention_true(self):
        result = await self.call_tool("get_notes", {"content": [{"note_thread_id": 12,
            "unread_count": 0, "needs_human_input": False}]})
        self.assertEqual(result.content[0].unread_count, 0)
        self.assertIs(result.content[0].needs_human_input, False)
        result = await self.call_tool("get_note_thread", {"note_thread_id": 12, "notes": [
            {"note_id": 34, "is_read": False, "needs_human_input": True,
             "human_input_reason": "Approval required"}]})
        self.assertIs(result.notes[0].is_read, False)
        self.assertIs(result.notes[0].needs_human_input, True)
        self.assertEqual(result.notes[0].human_input_reason, "Approval required")
