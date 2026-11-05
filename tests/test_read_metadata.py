from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from mcp.server.fastmcp.exceptions import ToolError

from exceptions import BackendContractError
from tools.read_metadata import (
    POST_COUNT_FIELDS, POST_FLAG_FIELDS, POST_TEXT_FIELDS,
    read_author, read_post_metadata,
)
from tools.tool_contract import tool_contracts


FIXTURE = json.loads((Path(__file__).parent / "fixtures/backend/agent-mcp-contract.json").read_text(encoding="utf-8"))


def camel(name):
    head, *tail = name.split("_")
    return head + "".join(part.title() for part in tail)


class ReadMetadataHelpersTest(unittest.TestCase):
    def test_missing_and_null_post_metadata_remain_unknown(self):
        self.assertTrue(all(value is None for value in read_post_metadata({}).values()))
        fields = POST_COUNT_FIELDS + POST_FLAG_FIELDS + POST_TEXT_FIELDS
        self.assertTrue(all(value is None for value in read_post_metadata(dict.fromkeys(fields)).values()))

    def test_zero_and_false_are_preserved(self):
        fields = {**dict.fromkeys(POST_COUNT_FIELDS, 0), **dict.fromkeys(POST_FLAG_FIELDS, False)}
        result = read_post_metadata(fields)
        for key in POST_COUNT_FIELDS:
            self.assertEqual(result[key], 0)
            self.assertIs(type(result[key]), int)
        for key in POST_FLAG_FIELDS:
            self.assertIs(result[key], False)

    def test_counts_require_nonnegative_long_and_consistent_aliases(self):
        for key in POST_COUNT_FIELDS:
            for bad in (False, "0", 0.1, -1, [], 9223372036854775808):
                with self.subTest(key=key, bad=bad), self.assertRaises(BackendContractError):
                    read_post_metadata({key: bad})
            with self.assertRaises(BackendContractError):
                read_post_metadata({key: 0, camel(key): 1})
            self.assertEqual(read_post_metadata({camel(key): 9223372036854775807})[key], 9223372036854775807)

    def test_flags_require_json_boolean_and_consistent_aliases(self):
        for key in POST_FLAG_FIELDS:
            for bad in (0, "false", [], {}):
                with self.subTest(key=key, bad=bad), self.assertRaises(BackendContractError):
                    read_post_metadata({key: bad})
            with self.assertRaises(BackendContractError):
                read_post_metadata({key: None, camel(key): False})
            self.assertIs(read_post_metadata({key: False, camel(key): False})[key], False)

    def test_optional_text_requires_text_and_consistent_aliases(self):
        for key in POST_TEXT_FIELDS:
            with self.subTest(key=key), self.assertRaises(BackendContractError):
                read_post_metadata({key: {}})
            if camel(key) != key:
                with self.assertRaises(BackendContractError):
                    read_post_metadata({key: "first", camel(key): "second"})
            self.assertEqual(read_post_metadata({camel(key): "value"})[key], "value")

    def test_actual_author_identity_is_retained(self):
        raw = FIXTURE["feed"]["response"]["data"]["content"][0]["author"]
        author = read_author(raw)
        self.assertEqual((author.user_id, author.agent_id, author.author_type, author.display_name),
                         (raw["userId"], raw["agentId"], raw["authorType"], raw["displayName"]))
        self.assertIsNone(read_author(None))

    def test_legacy_partial_author_does_not_invent_identity(self):
        for raw in ("Writer", {"name": "Writer"}, {"displayName": "Writer"}):
            self.assertEqual(asdict(read_author(raw)), {"user_id": None, "agent_id": None,
                "author_type": None, "display_name": "Writer"})
        self.assertIsNone(read_author({"authorType": "AGENT"}).agent_id)
        self.assertIsNone(read_author({"agentId": 7}).author_type)

    def test_author_identity_and_alias_conflicts_are_rejected(self):
        for raw in ([], False, {"userId": True}, {"agentId": "7"}, {"userId": 0},
                    {"agentId": 9223372036854775808}, {"authorType": "USER", "agentId": 7},
                    {"authorType": "AGENT", "agentId": None}, {"authorType": "OTHER"},
                    {"authorType": "USER", "author_type": "AGENT"},
                    {"userId": 7, "user_id": 8}, {"displayName": "A", "display_name": "B"}):
            with self.subTest(raw=raw), self.assertRaises(BackendContractError):
                read_author(raw)
        self.assertEqual(read_author({"userId": 7, "user_id": 7, "authorType": "USER", "agentId": None}).user_id, 7)


class ReadMetadataSdkTest(unittest.IsolatedAsyncioTestCase):
    async def call(self, case, payload=None):
        name = "get_feed" if case == "feed" else "get_post_comments"
        payload = deepcopy(FIXTURE[case]["response"]) if payload is None else payload
        client = SimpleNamespace(**{name: AsyncMock(return_value=payload)})
        ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=SimpleNamespace(client=client)))
        args = {"agent_token": "synthetic-read-metadata"}
        args.update({"board_id": "11", "page": 1, "size": 1} if case == "feed" else {"post_id": "101", "page": 0, "size": 4})
        result = await tool_contracts()[name].run(args, context=ctx)
        return result.posts if case == "feed" else result.comments

    async def test_sdk_actual_backend_author_ids_and_type_survive(self):
        for case in ("feed", "comments"):
            items = await self.call(case)
            raw = FIXTURE[case]["response"]["data"]["content"][0]["author"]
            self.assertEqual(items[0].author_name, raw["displayName"])
            self.assertEqual(items[0].author.user_id, raw["userId"])
            self.assertEqual(items[0].author.agent_id, raw["agentId"])
            self.assertEqual(items[0].author.author_type, "AGENT")
            self.assertTrue(items[0].untrusted_content)
            if case == "comments":
                self.assertIsNone(items[1].author)

    async def test_sdk_same_name_human_and_agent_remain_distinguishable(self):
        for case in ("feed", "comments"):
            agent = (await self.call(case))[0]
            payload = deepcopy(FIXTURE[case]["response"])
            payload["data"]["content"][0]["author"].update(userId=71, agentId=None, authorType="USER")
            human = (await self.call(case, payload))[0]
            self.assertEqual(human.author_name, agent.author_name)
            self.assertNotEqual(human.author, agent.author)

    async def test_sdk_actual_feed_absent_metadata_is_unknown(self):
        post = (await self.call("feed"))[0]
        for field in ("view_count", "is_nsfw", "is_spoiler", "is_liked", "is_scrapped", "is_subscribed", "inquiry_answered", "has_image"):
            with self.subTest(field=field):
                self.assertIsNone(getattr(post, field))
        self.assertFalse(post.is_notice)
        self.assertFalse(post.is_secret)
        self.assertTrue(post.has_my_comment)
        self.assertEqual(post.like_count, 3)

    async def test_sdk_preserves_present_zero_false_and_legacy_name(self):
        payload = deepcopy(FIXTURE["feed"]["response"])
        raw = payload["data"]["content"][0]
        raw.update(viewCount=0, isNsfw=False, isLiked=False, author={"name": "Legacy"})
        post = (await self.call("feed", payload))[0]
        self.assertEqual(post.view_count, 0)
        self.assertIs(post.is_nsfw, False)
        self.assertIs(post.is_liked, False)
        self.assertEqual(post.author_name, "Legacy")
        self.assertEqual(post.author.display_name, "Legacy")
        self.assertIsNone(post.author.agent_id)

    async def test_sdk_rejects_conflicting_or_malformed_present_metadata(self):
        for updates in ({"viewCount": True}, {"isNsfw": "false"},
                        {"isLiked": False, "is_liked": True},
                        {"author": {"authorType": "USER", "agentId": 7}},
                        {"author": {"agentId": True}}):
            payload = deepcopy(FIXTURE["feed"]["response"])
            payload["data"]["content"][0].update(updates)
            with self.subTest(updates=updates), self.assertRaisesRegex(ToolError, "backend_contract_error"):
                await self.call("feed", payload)
