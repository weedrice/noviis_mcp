from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from exceptions import BackendContractError
from mcp.server.fastmcp.exceptions import ToolError
from tools.notes import build_note_thread_result, build_notes_result
from tools.tool_contract import tool_contracts


def thread_data(**updates):
    return {"note_thread_id": 7, "notes": [{"note_id": 11, "content": "hello"}], **updates}


class NoteReadIdentityTests(unittest.TestCase):
    def test_required_id_fields_are_positive_long_json_integers(self):
        for value in (None, True, False, "1", 1.0, 0, -1, 9223372036854775808):
            with self.subTest(value=value):
                for payload, builder in (({"content": [{"note_thread_id": value}]}, build_notes_result),
                                         (thread_data(note_thread_id=value), build_note_thread_result),
                                         (thread_data(notes=[{"note_id": value}]), build_note_thread_result)):
                    with self.assertRaises(BackendContractError):
                        builder(payload)
        for payload, builder in (({"content": [{}]}, build_notes_result),
                                 ({"notes": []}, build_note_thread_result),
                                 (thread_data(notes=[{}]), build_note_thread_result)):
            with self.assertRaises(BackendContractError):
                builder(payload)

    def test_identity_aliases_must_agree(self):
        for payload, builder in (({"content": [{"id": 2, "note_thread_id": 1}]}, build_notes_result),
                                 (thread_data(noteThreadId=8), build_note_thread_result),
                                 (thread_data(notes=[{"note_id": 11, "id": 12}]), build_note_thread_result)):
            with self.subTest(payload=payload), self.assertRaises(BackendContractError):
                builder(payload)
        result = build_note_thread_result({"note_thread_id": 7, "noteThreadId": 7,
                                           "notes": [{"note_id": 11, "noteId": 11, "id": 11}]})
        self.assertEqual(result.notes[0].note_id, "11")

    def test_latest_note_id_is_optional_nullable_but_never_coerced(self):
        for latest in ({}, {"latest_note_id": None}, {"latest_note_id": None, "latestNoteId": None}):
            self.assertIsNone(build_notes_result({"content": [{"note_thread_id": 7, **latest}]}).content[0].latest_note_id)
        self.assertEqual(build_notes_result({"content": [{"note_thread_id": 7, "latest_note_id": 9}]}).content[0].latest_note_id, "9")
        for latest in ({"latest_note_id": "9"}, {"latest_note_id": 1.9}, {"latest_note_id": True},
                       {"latest_note_id": 9, "latestNoteId": 10}, {"latest_note_id": None, "latestNoteId": 9}):
            with self.subTest(latest=latest), self.assertRaises(BackendContractError):
                build_notes_result({"content": [{"note_thread_id": 7, **latest}]})

    def test_requested_thread_and_optional_item_thread_must_match(self):
        with self.assertRaises(BackendContractError):
            build_note_thread_result(thread_data(), expected_thread_id="8")
        self.assertEqual(build_note_thread_result(thread_data(), expected_thread_id="007").note_thread_id, "7")
        for item in ({"note_id": 11, "note_thread_id": 8},
                     {"note_id": 11, "note_thread_id": 7, "noteThreadId": 8},
                     {"note_id": 11, "note_thread_id": "7"}):
            with self.subTest(item=item), self.assertRaises(BackendContractError):
                build_note_thread_result(thread_data(notes=[item]))
        self.assertEqual(build_note_thread_result(thread_data(notes=[{"note_id": 11, "noteThreadId": 7}])).notes[0].note_id, "11")

    def test_note_builders_use_strict_pagination_without_requiring_optional_metadata(self):
        self.assertIsNone(build_note_thread_result(thread_data(notes=[])).page)
        self.assertIsNone(build_notes_result({"content": []}).page)
        for builder, payload in ((build_notes_result, {"content": []}), (build_note_thread_result, thread_data(notes=[]))):
            for extra in ({"has_next": "false"}, {"page": "0"}, {"page": 1}, {"size": 10}):
                with self.subTest(builder=builder.__name__, extra=extra), self.assertRaises(BackendContractError):
                    builder({**payload, **extra}, requested_page=0, requested_size=20)
            self.assertFalse(builder({**payload, "last": True}).has_next)


class NoteReadToolContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_note_thread_tool_checks_the_requested_target(self):
        backend = AsyncMock()
        backend.get_note_thread.return_value = {"data": thread_data(note_thread_id=8)}
        ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=SimpleNamespace(client=backend)))
        with self.assertRaises((BackendContractError, ToolError)):
            await tool_contracts()["get_note_thread"].run({"note_thread_id": 7, "agent_token": "synthetic"}, context=ctx)

    async def test_note_tools_pass_requested_page_and_size_to_response_validation(self):
        for tool_name, args, method, payload in (
            ("get_notes", {}, "get_notes", {"content": [], "page": 0, "size": 20}),
            ("get_note_thread", {"note_thread_id": 7}, "get_note_thread", thread_data(page=0, size=50)),
        ):
            backend = AsyncMock()
            getattr(backend, method).return_value = {"data": payload}
            ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=SimpleNamespace(client=backend)))
            with self.subTest(tool=tool_name), self.assertRaises((BackendContractError, ToolError)):
                await tool_contracts()[tool_name].run({"agent_token": "synthetic", "page": 1, **args}, context=ctx)


if __name__ == "__main__":
    unittest.main()
