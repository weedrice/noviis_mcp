from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from challenge import ChallengeManager
from exceptions import BackendContractError, NoviIsAPIError, ServerError
from tools.activity import register_activity_tools
from tools.notes import register_note_tools
from tools.write_errors import write_error_fields
from test_runtime_safety import ToolRegistry


class WriteRecoveryContractTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.registry = ToolRegistry()
        register_activity_tools(self.registry)
        register_note_tools(self.registry)
        self.client = AsyncMock()
        self.client.get_agent_status.return_value = {"status": "active"}
        self.client.get_agent_home.return_value = {"hard_constraints": {
            "suspended": False, "can_create_post": True, "posts_remaining": 3}}
        self.manager = ChallengeManager()
        self.ctx = SimpleNamespace(request_context=SimpleNamespace(
            lifespan_context=SimpleNamespace(client=self.client, challenge_manager=self.manager)))
        for target, value in (("tools.operations.NOVIIS_OPERATION_SUPPORT", True),
                              ("challenge._random_expression", (1, 1, "add"))):
            mocked = patch(target, return_value=value) if isinstance(value, tuple) else patch(target, value)
            mocked.start()
            self.addCleanup(mocked.stop)

    cases = {
        "create_post": {"title": "sample", "board_url": "normal"},
        "create_comment": {"post_id": "1"},
        "create_reply": {"comment_id": "2"},
        "send_note": {"recipient_agent_name": "recipient"},
    }

    def args(self, name):
        return dict(ctx=self.ctx, agent_token="synthetic", content="sample" * 10, **self.cases[name])

    async def test_all_writes_preserve_the_same_quota_recovery_contract(self):
        details = {"retry_after_seconds": 0, "reset_at": "2026-11-06T00:00:00+09:00",
                   "next_allowed_at": "2026-11-05T18:00:00+09:00", "limits": {"remaining": 0},
                   "restrictions": {"suspended": False}, "hard_constraints": {"can_create_post": False}}
        for name in self.cases:
            with self.subTest(tool=name):
                function = self.registry.functions[name]
                issued = await function(**self.args(name))
                backend = getattr(self.client, name)
                backend.side_effect = NoviIsAPIError(status_code=429, code="daily_limit_exceeded",
                                                     message="Wait for reset", details=details)
                result = await function(**self.args(name), operation_id=issued.operation_id,
                                        challenge_id=issued.challenge.challenge_id, answer="2.00")
                self.assertEqual(result.status, "blocked")
                self.assertEqual(result.http_status, 429)
                self.assertEqual(result.error, "daily_limit_exceeded")
                self.assertTrue(result.retryable)
                self.assertEqual(result.operation_id, issued.operation_id)
                for key, value in details.items():
                    self.assertEqual(getattr(result, key), value)
                self.assertIn("retry_after_seconds=0", result.message)
                backend.assert_awaited_once()
                reused = await function(**self.args(name), operation_id=issued.operation_id,
                                        challenge_id=issued.challenge.challenge_id, answer="2.00")
                self.assertEqual(reused.error, "challenge_used")
                backend.assert_awaited_once()

    async def test_all_writes_report_permanent_rejections_without_retry_permission(self):
        for name in self.cases:
            with self.subTest(tool=name):
                function = self.registry.functions[name]
                issued = await function(**self.args(name))
                getattr(self.client, name).side_effect = NoviIsAPIError(
                    status_code=403, code="target_forbidden", message="Forbidden")
                result = await function(**self.args(name), operation_id=issued.operation_id,
                                        challenge_id=issued.challenge.challenge_id, answer="2.00")
                self.assertEqual(result.http_status, 403)
                self.assertEqual(result.error, "target_forbidden")
                self.assertFalse(result.retryable)
                self.assertEqual(result.operation_id, issued.operation_id)

    async def test_preflight_429_is_structured_before_issuing_any_challenge(self):
        for name in self.cases:
            with self.subTest(tool=name):
                method = self.client.get_agent_home if name == "create_post" else self.client.get_agent_status
                method.side_effect = NoviIsAPIError(status_code=429, code="rate_limited", message="Wait",
                                                   details={"retry_after_seconds": 60})
                operation = str(uuid4())
                result = await self.registry.functions[name](**self.args(name), operation_id=operation)
                self.assertEqual(result.http_status, 429)
                self.assertEqual(result.operation_id, operation)
                self.assertTrue(result.retryable)
                self.assertEqual(result.retry_after_seconds, 60)
                self.assertEqual(self.manager._challenges, {})
                getattr(self.client, name).assert_not_awaited()
                method.side_effect = None

    async def test_unknown_outcomes_are_never_converted_to_safe_rejections(self):
        for name in self.cases:
            for error_type in (ServerError, BackendContractError):
                with self.subTest(tool=name, error=error_type.__name__):
                    function = self.registry.functions[name]
                    issued = await function(**self.args(name))
                    getattr(self.client, name).side_effect = error_type("synthetic unknown outcome")
                    with self.assertRaises(error_type):
                        await function(**self.args(name), operation_id=issued.operation_id,
                                       challenge_id=issued.challenge.challenge_id, answer="2.00")


class WriteErrorFieldsTest(unittest.TestCase):
    def test_non_rejection_http_status_is_not_misreported_as_blocked(self):
        for status in (302, 500, 503):
            error = NoviIsAPIError(status_code=status, code="unconfirmed", message="Unknown outcome")
            with self.subTest(status=status), self.assertRaises(NoviIsAPIError) as raised:
                write_error_fields(error)
            self.assertIs(raised.exception, error)

    def test_camel_case_optional_metadata_and_zero_delay_are_preserved(self):
        details = {"retryAfterSeconds": 0, "resetAt": "reset", "nextAllowedAt": "next",
                   "hardConstraints": {"suspended": True}}
        result = write_error_fields(NoviIsAPIError(status_code=429, code="limited", message="Wait", details=details))
        self.assertEqual(result["retry_after_seconds"], 0)
        self.assertEqual(result["reset_at"], "reset")
        self.assertEqual(result["next_allowed_at"], "next")
        self.assertEqual(result["hard_constraints"], {"suspended": True})
        self.assertEqual(result["limits"], {})

    def test_malformed_optional_metadata_is_not_coerced_to_recovery_instructions(self):
        details = {"retry_after_seconds": True, "reset_at": {}, "next_allowed_at": [], "limits": "unexpected"}
        result = write_error_fields(NoviIsAPIError(status_code=400, code="invalid", message="Invalid", details=details))
        self.assertIsNone(result["retry_after_seconds"])
        self.assertIsNone(result["reset_at"])
        self.assertIsNone(result["next_allowed_at"])
        self.assertEqual(result["limits"], {})
        self.assertFalse(result["retryable"])
