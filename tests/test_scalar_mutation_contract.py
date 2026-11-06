from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from mcp.server.fastmcp.exceptions import ToolError

from challenge import ChallengeManager
from exceptions import BackendContractError
from tools.response_contract import require_response_id
from tools.tool_contract import tool_contracts, validate_action


def context():
    backend = AsyncMock()
    backend.get_agent_status.return_value = {"status": "active"}
    backend.get_agent_home.return_value = {"hard_constraints": {
        "suspended": False, "can_create_post": True, "posts_remaining": 3}}
    runtime = SimpleNamespace(client=backend, challenge_manager=ChallengeManager())
    return backend, SimpleNamespace(request_context=SimpleNamespace(lifespan_context=runtime))


class ScalarMutationContractTest(unittest.IsolatedAsyncioTestCase):
    cases = {
        "mark_post_activity_read": ({"post_id": 7, "observed_comment_ids": [1]},
            {"post_id": 7, "marked_read": True, "observed_boundary_applied": True,
             "remaining_unread_count": 0}),
        "delete_post": ({"post_id": 7}, {"post_id": 7, "deleted": True, "already_deleted": False}),
        "like_post": ({"post_id": 7}, {"postId": 7, "isLiked": True, "likeCount": 0}),
        "like_comment": ({"comment_id": 7},
            {"comment_id": 7, "status": "liked", "already_liked": False, "like_count": 0}),
    }

    async def run_tool(self, name, response, args=None):
        backend, ctx = context()
        getattr(backend, name).return_value = {"data": response}
        result = await tool_contracts()[name].run(
            {"agent_token": "synthetic", **(args or self.cases[name][0])}, context=ctx)
        getattr(backend, name).assert_awaited_once()
        return result

    async def test_valid_response_preserves_explicit_confirmation_and_zero_counts(self):
        for name, (args, response) in self.cases.items():
            with self.subTest(tool=name):
                result = await self.run_tool(name, response, args)
                target = "comment_id" if name == "like_comment" else "post_id"
                self.assertEqual(getattr(result, target), "7")
        result = await self.run_tool("like_post", {"postId": 7, "isLiked": False, "likeCount": 0})
        self.assertEqual(result.status, "not_liked")
        result = await self.run_tool("delete_post", {"post_id": 7, "deleted": False})
        self.assertEqual(result.status, "not_deleted")

    async def test_missing_invalid_or_different_target_never_confirms_success(self):
        for name, (_, response) in self.cases.items():
            key = "comment_id" if name == "like_comment" else "postId" if name == "like_post" else "post_id"
            for value in (None, True, "7", 0, -1, 1.5, 8, 9223372036854775808, "missing"):
                bad = {**response, key: value}
                if value == "missing":
                    bad.pop(key)
                with self.subTest(tool=name, value=value), self.assertRaisesRegex(ToolError, "backend_contract_error"):
                    await self.run_tool(name, bad)

    async def test_aliases_must_agree_and_leading_zero_request_matches_numeric_target(self):
        for name, (args, response) in self.cases.items():
            conflicting = deepcopy(response)
            if name == "like_post":
                conflicting["post_id"] = 8
            else:
                conflicting["commentId" if name == "like_comment" else "postId"] = 8
            with self.subTest(tool=name), self.assertRaisesRegex(ToolError, "backend_contract_error"):
                await self.run_tool(name, conflicting)
            key = "comment_id" if name == "like_comment" else "post_id"
            self.assertEqual(getattr(await self.run_tool(name, response, {**args, key: "007"}), key), "7")
        result = await self.run_tool("mark_post_activity_read", {
            "postId": 7, "markedRead": True, "observedBoundaryApplied": True, "remainingUnreadCount": 0})
        self.assertEqual(result.remaining_unread_count, 0)

    async def test_required_flags_counts_and_status_are_strict(self):
        fields = {"mark_post_activity_read": ("marked_read", "observed_boundary_applied", "remaining_unread_count"),
                  "delete_post": ("deleted",), "like_post": ("isLiked", "likeCount"),
                  "like_comment": ("status", "already_liked", "like_count")}
        for name, keys in fields.items():
            for key in keys:
                for value in (None, "true", 1.5, [], "missing"):
                    bad = {**self.cases[name][1], key: value}
                    if value == "missing":
                        bad.pop(key)
                    with self.subTest(tool=name, key=key, value=value), self.assertRaisesRegex(ToolError, "backend_contract_error"):
                        await self.run_tool(name, bad)
        for key in ("marked_read", "observed_boundary_applied"):
            with self.assertRaisesRegex(ToolError, "backend_contract_error"):
                await self.run_tool("mark_post_activity_read", {**self.cases["mark_post_activity_read"][1], key: False})
        for name, key in (("mark_post_activity_read", "remaining_unread_count"),
                          ("like_post", "likeCount"), ("like_comment", "like_count")):
            for value in (-1, True):
                with self.subTest(tool=name, value=value), self.assertRaisesRegex(ToolError, "backend_contract_error"):
                    await self.run_tool(name, {**self.cases[name][1], key: value})
        for name, alias in (("mark_post_activity_read", "remainingUnreadCount"),
                            ("like_post", "like_count"), ("like_comment", "likeCount")):
            with self.subTest(tool=name), self.assertRaisesRegex(ToolError, "backend_contract_error"):
                await self.run_tool(name, {**self.cases[name][1], alias: 1})

    async def test_optional_delete_flag_cannot_lie_or_conflict(self):
        for fields in ({"already_deleted": "false"}, {"already_deleted": None},
                       {"already_deleted": False, "alreadyDeleted": True}):
            with self.subTest(fields=fields), self.assertRaisesRegex(ToolError, "backend_contract_error"):
                await self.run_tool("delete_post", {**self.cases["delete_post"][1], **fields})

    async def test_trusted_empty_delete_204_remains_supported(self):
        backend, ctx = context()
        backend.delete_post.return_value = {"_http_no_content": True}
        result = await tool_contracts()["delete_post"].run({"post_id": 7, "agent_token": "synthetic"}, context=ctx)
        self.assertTrue(result.deleted)
        self.assertEqual(result.post_id, "7")


class CreationIdentityContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_every_creation_rejects_invalid_and_conflicting_resource_ids(self):
        cases = {
            "create_post": ({"title": "Valid", "board_url": "normal"}, "postId", "post_id", {}),
            "create_comment": ({"post_id": 7}, "commentId", "comment_id", {}),
            "create_reply": ({"comment_id": 7}, "commentId", "comment_id", {}),
            "send_note": ({"recipient_agent_name": "recipient"}, "noteId", "note_id", {"noteThreadId": 7}),
        }
        for name, (extra, key, alias, other) in cases.items():
            for invalid in ({key: "opaque"}, {key: "7"}, {key: 9223372036854775808}, {key: 7, alias: 8}):
                backend, ctx = context()
                args = {"agent_token": "synthetic", "content": "valid body " * 6, **extra}
                getattr(backend, name).return_value = {"data": {**other, **invalid}}
                with patch("challenge._random_expression", return_value=(1, 1, "add")):
                    issued = await tool_contracts()[name].run(args, context=ctx)
                    with self.subTest(tool=name, invalid=invalid), self.assertRaisesRegex(ToolError, "backend_contract_error"):
                        await tool_contracts()[name].run({**args, "challenge_id": issued.challenge.challenge_id,
                                                        "answer": "2.00"}, context=ctx)
                getattr(backend, name).assert_awaited_once()

    async def test_upload_rejects_invalid_id_and_accepts_maximum_long(self):
        for value in ("opaque", "7", True, -1, 9223372036854775808, 9223372036854775807):
            backend, ctx = context()
            backend.upload_post_image.return_value = {"data": {"imageFileId": value}}
            args = {"agent_token": "synthetic", "filename": "image.png", "mime_type": "image/png", "image_base64": "eA=="}
            with self.subTest(value=value):
                if value == 9223372036854775807:
                    self.assertEqual((await tool_contracts()["upload_post_image"].run(args, context=ctx)).image_file_id, str(value))
                else:
                    with self.assertRaisesRegex(ToolError, "backend_contract_error"):
                        await tool_contracts()["upload_post_image"].run(args, context=ctx)

    async def test_operation_uuid_is_preserved_but_lookup_resource_ids_are_strict(self):
        operation = str(uuid4())
        for result_id in (7, "opaque", "7", 9223372036854775808):
            backend, ctx = context()
            backend.get_operation_result.return_value = {"data": {
                "operation_id": operation, "status": "succeeded", "action": "create_post",
                "completed_at": "2030-01-01T00:00:00Z", "result": {"postId": result_id}}}
            with patch("tools.operations.NOVIIS_OPERATION_SUPPORT", True), self.subTest(result_id=result_id):
                if result_id == 7:
                    result = await tool_contracts()["get_operation_result"].run({"agent_token": "synthetic", "operation_id": operation}, context=ctx)
                    self.assertEqual(result.operation_id, operation)
                else:
                    with self.assertRaisesRegex(ToolError, "backend_contract_error"):
                        await tool_contracts()["get_operation_result"].run({"agent_token": "synthetic", "operation_id": operation}, context=ctx)

    async def test_attachment_bounds_match_recommendation_before_authentication(self):
        for value in ("9223372036854775808", "0", "-1", "1.0", "+1", " 1", "1_0", "\u0661"):
            backend, ctx = context()
            args = {"agent_token": "synthetic", "board_url": "normal", "title": "Valid",
                    "content": "valid body " * 6, "image_file_id": value}
            self.assertIn("image_file_id", validate_action("create_post", args)[1])
            with self.subTest(value=value), self.assertRaises(ToolError):
                await tool_contracts()["create_post"].run(args, context=ctx)
            self.assertEqual(backend.mock_calls, [])
            self.assertEqual(ctx.request_context.lifespan_context.challenge_manager._challenges, {})
        for value in ("1", "001", "9223372036854775807"):
            backend, ctx = context()
            args = {"agent_token": "synthetic", "board_url": "normal", "title": "Valid",
                    "content": "valid body " * 6, "image_file_id": value}
            normalized, invalid, _, _ = validate_action("create_post", args)
            self.assertEqual(invalid, [])
            self.assertEqual(normalized["image_file_id"], str(int(value)))
            self.assertEqual((await tool_contracts()["create_post"].run(args, context=ctx)).status, "challenge_required")

    def test_resource_id_aliases_must_agree_even_if_one_is_invalid(self):
        for payload in ({"postId": 7, "post_id": 8}, {"postId": 7, "post_id": "7"},
                        {"postId": 7, "post_id": None}, {"postId": True, "post_id": 1}):
            with self.subTest(payload=payload), self.assertRaises(BackendContractError):
                require_response_id(payload, "postId", "post_id")
        self.assertEqual(require_response_id({"postId": 7, "post_id": 7}, "postId", "post_id"), "7")
