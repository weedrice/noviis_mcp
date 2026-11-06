import json
from types import SimpleNamespace
import unittest

import httpx
from mcp.server.fastmcp.exceptions import ToolError

from client import NoviIsClient
from exceptions import BackendContractError
from tools.notes import build_note_thread_result, build_notes_result
from tools.tool_contract import tool_contracts


def note(**fields):
    return {"note_id": 9, "sender_agent_name": "sender", "content": "Review this note", **fields}


def thread(notes=None, **fields):
    return {"note_thread_id": 7, "notes": [note()] if notes is None else notes, **fields}


class NoteContentContractTest(unittest.TestCase):
    def test_content_and_sender_are_required_nonblank_strings(self):
        for key in ("content", "sender_agent_name"):
            missing = note()
            del missing[key]
            for item in [missing, *(note(**{key: value}) for value in (None, {}, [], True, 1, "", " \n\t"))]:
                with self.subTest(key=key, item=item), self.assertRaisesRegex(BackendContractError, key):
                    build_note_thread_result(thread([item]))

    def test_sender_aliases_must_agree_without_coercion(self):
        for value in (None, {}, True, "other", ""):
            with self.subTest(value=value), self.assertRaises(BackendContractError):
                build_note_thread_result(thread([note(senderAgentName=value)]))
        item = note(senderAgentName="sender")
        self.assertEqual(build_note_thread_result(thread([item])).notes[0].sender_agent_name, "sender")
        del item["sender_agent_name"]
        self.assertEqual(build_note_thread_result(thread([item])).notes[0].sender_agent_name, "sender")

    def test_valid_text_is_preserved_exactly(self):
        content = "  안녕하세요.\n確認 요청입니다. 😀  "
        result = build_note_thread_result(thread([note(content=content, sender_agent_name="Ângela")]))
        self.assertEqual(result.notes[0].content, content)
        self.assertEqual(result.notes[0].sender_agent_name, "Ângela")

    def build_counterparts(self, **fields):
        return (
            lambda: build_note_thread_result(thread([], **fields)).counterpart_agent,
            lambda: build_notes_result({"content": [{"note_thread_id": 7, **fields}]}).content[0].counterpart_agent,
        )

    def test_counterpart_is_optional_and_nullable(self):
        for fields in ({}, {"counterpart_agent": None}, {"counterpartAgent": None},
                       {"counterpart_agent": None, "counterpartAgent": None}):
            for build in self.build_counterparts(**fields):
                self.assertIsNone(build())

    def test_present_counterpart_requires_object_and_nonblank_name(self):
        for value in (True, 1, "sender", [], {}, {"name": None}, {"name": []}, {"name": 5}, {"name": ""}, {"name": " \t"}):
            for build in self.build_counterparts(counterpart_agent=value):
                with self.subTest(value=value), self.assertRaises(BackendContractError):
                    build()

    def test_counterpart_and_display_name_aliases_are_strict(self):
        for fields in (
            {"counterpart_agent": {"name": "sender"}, "counterpartAgent": {"name": "other"}},
            {"counterpart_agent": None, "counterpartAgent": {"name": "sender"}},
            {"counterpart_agent": {"name": "sender", "display_name": "One", "displayName": "Two"}},
            *({"counterpart_agent": {"name": "sender", "display_name": value}} for value in ({}, [], True, 1)),
        ):
            for build in self.build_counterparts(**fields):
                with self.subTest(fields=fields), self.assertRaises(BackendContractError):
                    build()
        counterpart = {"name": "sender", "display_name": "Sender", "displayName": "Sender"}
        for build in self.build_counterparts(counterpart_agent=counterpart, counterpartAgent=counterpart):
            self.assertEqual(build().display_name, "Sender")
        for fields in ({"name": "sender"}, {"name": "sender", "displayName": None}):
            for build in self.build_counterparts(counterpartAgent=fields):
                self.assertIsNone(build().display_name)


class NoteContentSDKContractTest(unittest.IsolatedAsyncioTestCase):
    async def run_read_then_ack(self, notes):
        requests = []

        def backend(request):
            requests.append(request)
            data = thread(notes) if request.method == "GET" else {
                "note_thread_id": 7, "marked_read": True, "observed_boundary_applied": True, "remaining_unread_count": 0}
            return httpx.Response(200, json={"data": data})

        async with httpx.AsyncClient(base_url="https://mock.invalid/api/v1", transport=httpx.MockTransport(backend)) as http:
            runtime = SimpleNamespace(client=NoviIsClient(client=http, internal_secret="synthetic"))
            ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=runtime))
            try:
                result = await tool_contracts()["get_note_thread"].run({"agent_token": "synthetic", "note_thread_id": 7}, context=ctx)
                acknowledgement = await tool_contracts()["mark_note_read"].run({
                    "agent_token": "synthetic", "note_thread_id": 7,
                    "observed_note_ids": [item.note_id for item in result.notes]}, context=ctx)
            except ToolError:
                self.assertEqual([request.method for request in requests], ["GET"])
                raise
        return result, acknowledgement, requests

    async def test_corrupt_note_fails_sdk_read_before_observed_acknowledgement(self):
        for fields in ({"content": None}, {"content": {}}, {"content": ""},
                       {"sender_agent_name": None}, {"senderAgentName": "different"}):
            with self.subTest(fields=fields), self.assertRaisesRegex(ToolError, "backend_contract_error"):
                await self.run_read_then_ack([note(note_id=8), note(**fields)])
        missing = note()
        del missing["content"]
        with self.assertRaisesRegex(ToolError, "backend_contract_error"):
            await self.run_read_then_ack([missing])

    async def test_valid_note_can_be_observed_and_acknowledged(self):
        result, acknowledged, requests = await self.run_read_then_ack([note(content="실제로 읽은 본문")])
        self.assertEqual(result.notes[0].content, "실제로 읽은 본문")
        self.assertTrue(acknowledged.marked_read)
        self.assertEqual(json.loads(requests[-1].content), {"observed_note_ids": [9]})


if __name__ == "__main__":
    unittest.main()
