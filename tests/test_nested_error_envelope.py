from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

import httpx
from mcp.server.fastmcp.exceptions import ToolError

from client import NoviIsClient
from exceptions import BackendContractError
from tools.activity import _build_feed_result
from tools.home import build_agent_home_result
from tools.notes import build_notes_result, build_send_note_result
from tools.parsing import read_collection, unwrap_dict_data, unwrap_list_data, validate_response_envelope
from tools.rules import build_agent_rules_result
from tools.tool_contract import tool_contracts


PRIVATE_DIAGNOSTIC = "synthetic-private-backend-diagnostic"
HOME = json.loads((Path(__file__).parent / "fixtures/backend/agent-home-board-contract.json").read_text(encoding="utf-8"))["home"]["response"]
MARKERS = ({"success": False}, {"error": {"code": PRIVATE_DIAGNOSTIC}}, {"error": PRIVATE_DIAGNOSTIC})


class NestedEnvelopeBuildersTest(unittest.TestCase):
    def test_common_unwrappers_reject_root_and_direct_data_errors(self):
        for marker in MARKERS:
            for payload in ({"content": [], **marker}, {"success": True, "data": {"content": [], **marker}}):
                for consumer in (validate_response_envelope, unwrap_dict_data,
                                 lambda value: unwrap_list_data(value, nested_key="content"),
                                 lambda value: read_collection(value, "content")):
                    with self.subTest(marker=marker, payload=payload, consumer=consumer), self.assertRaises(BackendContractError) as caught:
                        consumer(payload)
                    self.assertNotIn(PRIVATE_DIAGNOSTIC, str(caught.exception))

    def test_direct_read_and_write_builders_cannot_accept_nested_error(self):
        for marker in MARKERS:
            for builder in (_build_feed_result, build_notes_result, build_send_note_result):
                payload = {"success": True, "data": {"content": [], "note_thread_id": 12,
                                                      "note_id": 34, "status": "sent", **marker}}
                with self.subTest(builder=builder.__name__, marker=marker), self.assertRaises(BackendContractError):
                    builder(payload)

    def test_home_and_rules_direct_builders_reject_errors(self):
        for marker in MARKERS:
            payload = deepcopy(HOME)
            payload["data"].update(marker)
            with self.subTest(marker=marker), self.assertRaises(BackendContractError):
                build_agent_home_result(payload)
            with self.assertRaises(BackendContractError):
                build_agent_rules_result({"data": {"hard_constraints": [], **marker}})

    def test_user_content_replies_and_operation_result_are_not_envelopes(self):
        item = {"error": "user quoted an error", "success": False,
                "replies": [{"error": "another quoted error"}]}
        data = {"content": [item], "result": {"success": False, "error": "operation result"}}
        payload = {"success": True, "data": data}
        original = deepcopy(payload)
        validate_response_envelope(payload)
        self.assertEqual(read_collection(payload, "content")[1], [item])
        self.assertIs(unwrap_dict_data(payload), data)
        self.assertEqual(unwrap_list_data(payload, nested_key="content"), [item])
        self.assertEqual(payload, original)
        # Only the immediate data object is a transport envelope.
        validate_response_envelope({"data": {"data": {"success": False, "error": "domain data"}}})

    def test_legacy_direct_payloads_empty_error_and_list_envelopes_remain_supported(self):
        for empty_error in (None, False, "", {}):
            payload = {"success": True, "error": empty_error, "data": {"content": [], "error": empty_error}}
            self.assertEqual(read_collection(payload, "content")[1], [])
        self.assertEqual(read_collection({"content": []}, "content")[1], [])
        self.assertEqual(unwrap_list_data({"data": [{"id": 1}]}), [{"id": 1}])
        self.assertEqual(unwrap_dict_data({"status": "sent"}), {"status": "sent"})


class NestedEnvelopeClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_nested_error_http_2xx_is_not_retried_and_has_no_private_diagnostic(self):
        for method in ("GET", "POST"):
            for marker in MARKERS:
                requests = []
                def handler(request):
                    requests.append(request)
                    return httpx.Response(200, json={"success": True, "data": {"content": [], **marker}})
                async with httpx.AsyncClient(base_url="https://synthetic.invalid/api/v1", transport=httpx.MockTransport(handler)) as http:
                    api = NoviIsClient(client=http, internal_secret="synthetic-internal")
                    with self.subTest(method=method, marker=marker), self.assertRaises(BackendContractError) as caught:
                        await api.request_json(method, "/synthetic", token="synthetic-token")
                self.assertEqual(len(requests), 1)
                self.assertNotIn(PRIVATE_DIAGNOSTIC, str(caught.exception))
                self.assertIn("outcome is unconfirmed", str(caught.exception))

    async def test_sdk_returns_error_instead_of_empty_notes(self):
        for marker in MARKERS:
            requests = []
            def handler(request):
                requests.append(request)
                return httpx.Response(200, json={"data": {"content": [], **marker}})
            async with httpx.AsyncClient(base_url="https://synthetic.invalid/api/v1", transport=httpx.MockTransport(handler)) as http:
                api = NoviIsClient(client=http, internal_secret="synthetic-internal")
                ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=SimpleNamespace(client=api)))
                with self.subTest(marker=marker), self.assertRaisesRegex(ToolError, "backend_contract_error") as caught:
                    await tool_contracts()["get_notes"].run({"agent_token": "synthetic-token"}, context=ctx)
            self.assertEqual(len(requests), 1)
            self.assertNotIn(PRIVATE_DIAGNOSTIC, str(caught.exception))

    async def test_successful_raw_lists_integer_scalars_and_delete_204_remain_supported(self):
        for value in ([{"error": "domain content", "success": False}], 0, 7):
            async with httpx.AsyncClient(base_url="https://synthetic.invalid", transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=value))) as http:
                api = NoviIsClient(client=http, internal_secret="synthetic-internal")
                result = await api.request_json("GET", "/synthetic")
                self.assertEqual(result["result"], value)
        async with httpx.AsyncClient(base_url="https://synthetic.invalid", transport=httpx.MockTransport(
            lambda request: httpx.Response(204))) as http:
            api = NoviIsClient(client=http, internal_secret="synthetic-internal")
            self.assertTrue((await api.request_json("DELETE", "/synthetic"))["_http_no_content"])

    async def test_http_success_preserves_nested_domain_error_values(self):
        payload = {"data": {"content": [{"error": "user data", "success": False}],
                            "result": {"error": "operation error", "success": False}}}
        async with httpx.AsyncClient(base_url="https://synthetic.invalid", transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=payload))) as http:
            api = NoviIsClient(client=http, internal_secret="synthetic-internal")
            self.assertEqual((await api.request_json("GET", "/synthetic"))["data"], payload["data"])
