from dataclasses import asdict
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from challenge import ChallengeManager
from exceptions import ChallengeUnknown, NoviIsAPIError
from tools.activity import CreateCommentResult
from tools.challenge_flow import verify_or_reissue_challenge
from tools.tool_contract import tool_contracts


CASES = {
    "create_post": ({"title": "sample", "board_url": "normal"}, {"postId": 1}),
    "create_comment": ({"post_id": 1}, {"commentId": 2}),
    "create_reply": ({"comment_id": 2}, {"commentId": 3}),
    "send_note": ({"recipient_agent_name": "recipient"}, {"noteId": 4, "noteThreadId": 1}),
}


class ReplayOperationIdentityTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 1000.0
        self.client = AsyncMock()
        self.client.get_agent_status.return_value = {"status": "active"}
        self.client.get_agent_home.return_value = {"hard_constraints": {
            "suspended": False, "can_create_post": True, "posts_remaining": 1}}
        for name, (_, response) in CASES.items():
            getattr(self.client, name).return_value = response
        self.runtime = SimpleNamespace(client=self.client, challenge_manager=ChallengeManager(clock=lambda: self.now))
        self.ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=self.runtime))
        for setting in (patch("tools.operations.NOVIIS_OPERATION_SUPPORT", True),
                        patch("challenge._random_expression", return_value=(1, 1, "add")),
                        patch("challenge.CHALLENGE_RETENTION_SECONDS", 300),
                        patch("challenge.CHALLENGE_EXPIRES_SECONDS", 120)):
            setting.start()
            self.addCleanup(setting.stop)

    def args(self, name):
        return {"agent_token": "owner", "content": "sample " * 10, **CASES[name][0]}

    async def call(self, name, args):
        return await tool_contracts()[name].run(args, context=self.ctx)

    async def issue(self, name, original):
        args = self.args(name)
        if original is not None:
            args["operation_id"] = original
        return await self.call(name, args)

    def answer(self, name, prompt, operation=None):
        args = {**self.args(name), "challenge_id": prompt.challenge.challenge_id, "answer": "2.00"}
        if operation is not None:
            args["operation_id"] = operation
        return args

    async def test_used_replay_always_returns_owned_original_key_for_all_writes(self):
        for name in CASES:
            for explicit in (False, True):
                with self.subTest(tool=name, explicit=explicit):
                    self.runtime.challenge_manager = ChallengeManager(clock=lambda: self.now)
                    getattr(self.client, name).reset_mock()
                    prompt = await self.issue(name, str(uuid4()) if explicit else None)
                    await self.call(name, self.answer(name, prompt, prompt.operation_id))
                    for supplied in (None, str(uuid4()), prompt.operation_id):
                        result = await self.call(name, self.answer(name, prompt, supplied))
                        self.assertEqual((result.error, result.retryable), ("challenge_used", False))
                        self.assertEqual(result.operation_id, prompt.operation_id)
                        self.assertIsNone(result.challenge)
                    getattr(self.client, name).assert_awaited_once()

    async def test_recovery_lookup_and_explicit_retry_keep_backend_idempotency(self):
        saved = {}

        async def create(**kwargs):
            key = kwargs["operation_id"]
            if key not in saved:
                saved[key] = {"commentId": len(saved) + 1}
            return saved[key]

        async def lookup(**kwargs):
            key = kwargs["operation_id"]
            if key not in saved:
                raise NoviIsAPIError(status_code=404, code="operation_not_found", message="not found")
            return {"operation_id": key, "status": "succeeded", "action": "create_comment",
                    "result": saved[key], "completed_at": "2026-11-06T00:00:00Z"}

        self.client.create_comment.side_effect = create
        self.client.get_operation_result.side_effect = lookup
        original = str(uuid4())
        prompt = await self.issue("create_comment", original)
        await self.call("create_comment", self.answer("create_comment", prompt, original))
        replay = await self.call("create_comment", self.answer("create_comment", prompt))
        found = await self.call("get_operation_result", {"agent_token": "owner", "operation_id": replay.operation_id})
        self.assertEqual(found.status, "succeeded")
        fresh = await self.issue("create_comment", replay.operation_id)
        await self.call("create_comment", self.answer("create_comment", fresh, fresh.operation_id))
        self.assertEqual(len(saved), 1)

    async def test_expired_failed_and_pending_mismatch_replacements_retain_original_key(self):
        for state in ("pending", "expired", "failed"):
            for supplied in (None, str(uuid4())):
                with self.subTest(state=state, supplied=supplied is not None):
                    self.runtime.challenge_manager = ChallengeManager(clock=lambda: self.now)
                    self.client.create_comment.reset_mock()
                    original = str(uuid4())
                    prompt = await self.issue("create_comment", original)
                    if state == "expired":
                        self.now += 120
                    elif state == "failed":
                        incorrect = self.answer("create_comment", prompt, original)
                        incorrect["answer"] = "0"
                        await self.call("create_comment", incorrect)
                    replacement = await self.call("create_comment", self.answer("create_comment", prompt, supplied))
                    self.assertEqual(replacement.status, "challenge_required")
                    self.assertEqual(replacement.operation_id, original)
                    self.client.create_comment.assert_not_awaited()
                    completed = await self.call("create_comment", self.answer("create_comment", replacement, original))
                    self.assertEqual(completed.status, "created")
                    self.assertEqual(self.client.create_comment.call_args.kwargs["operation_id"], original)

    async def test_pending_prerequisite_failures_return_original_key_without_consuming(self):
        for name in CASES:
            for supplied in (None, str(uuid4())):
                with self.subTest(tool=name, supplied=supplied is not None):
                    self.runtime.challenge_manager = ChallengeManager(clock=lambda: self.now)
                    self.client.get_agent_status.side_effect = None
                    original = str(uuid4())
                    prompt = await self.issue(name, original)
                    self.client.get_agent_status.side_effect = NoviIsAPIError(status_code=429, code="rate_limited", message="wait")
                    result = await self.call(name, self.answer(name, prompt, supplied))
                    self.assertEqual(result.operation_id, original)
                    self.assertEqual(result.error, "rate_limited")
                    self.assertEqual(self.runtime.challenge_manager._challenges[prompt.challenge.challenge_id].status, "pending")
        self.client.get_agent_status.side_effect = None
        prompt = await self.issue("create_post", str(uuid4()))
        self.client.get_agent_home.return_value = {"hard_constraints": {
            "suspended": False, "can_create_post": False, "posts_remaining": 0}}
        result = await self.call("create_post", self.answer("create_post", prompt))
        self.assertEqual(result.operation_id, prompt.operation_id)
        self.assertEqual(result.error, "post_daily_limit_exceeded")

    async def test_unknown_and_foreign_replays_never_invent_or_disclose_keys(self):
        original = str(uuid4())
        prompt = await self.issue("create_comment", original)
        for foreign in (False, True):
            for supplied in (None, str(uuid4())):
                args = self.answer("create_comment", prompt, supplied)
                if foreign:
                    args["agent_token"] = "other-owner"
                else:
                    args["challenge_id"] = str(uuid4())
                result = await self.call("create_comment", args)
                self.assertEqual(result.error, "challenge_unknown")
                self.assertEqual(result.operation_id, supplied)
                self.assertNotIn(original, result.message)
        with self.assertRaises(ChallengeUnknown):
            self.runtime.challenge_manager.recovery_operation_id(owner_key="other-owner", challenge_id=prompt.challenge.challenge_id)

    async def test_restart_and_retention_require_a_saved_key_instead_of_synthesizing_one(self):
        for loss in ("restart", "retention"):
            self.runtime.challenge_manager = ChallengeManager(clock=lambda: self.now)
            prompt = await self.issue("create_comment", str(uuid4()))
            await self.call("create_comment", self.answer("create_comment", prompt, prompt.operation_id))
            if loss == "restart":
                self.runtime.challenge_manager = ChallengeManager(clock=lambda: self.now)
            else:
                self.now += 301
            missing = await self.call("create_comment", self.answer("create_comment", prompt))
            saved = await self.call("create_comment", self.answer("create_comment", prompt, prompt.operation_id))
            self.assertIsNone(missing.operation_id)
            self.assertEqual(saved.operation_id, prompt.operation_id)
            self.assertFalse(missing.retryable or saved.retryable)

    async def test_disabled_mode_retains_no_operation_metadata(self):
        with patch("tools.operations.NOVIIS_OPERATION_SUPPORT", False):
            prompt = await self.issue("create_comment", None)
            await self.call("create_comment", self.answer("create_comment", prompt))
            result = await self.call("create_comment", self.answer("create_comment", prompt))
        self.assertIsNone(result.operation_id)
        self.assertIsNone(self.runtime.challenge_manager._challenges[prompt.challenge.challenge_id].operation_id)

    async def test_retention_boundary_during_prerequisites_preserves_only_supplied_key(self):
        for supplied in (False, True):
            self.runtime.challenge_manager = ChallengeManager(clock=lambda: self.now)
            self.client.get_agent_status.side_effect = None
            prompt = await self.issue("create_comment", str(uuid4()))
            self.now += 120

            async def delayed_status(**kwargs):
                self.now += 301
                return {"status": "active"}

            self.client.get_agent_status.side_effect = delayed_status
            result = await self.call("create_comment", self.answer(
                "create_comment", prompt, prompt.operation_id if supplied else None))
            self.assertEqual(result.error, "challenge_unknown")
            self.assertFalse(result.retryable)
            self.assertEqual(result.operation_id, prompt.operation_id if supplied else None)
        self.client.create_comment.assert_not_awaited()

    def test_retention_boundary_after_metadata_lookup_keeps_already_verified_key(self):
        original = str(uuid4())
        manager = ChallengeManager(clock=lambda: self.now)
        payload = {"operation_id": original}
        prompt = manager.issue_challenge(owner_key="owner", action="create_comment", payload=payload)
        self.now += 120
        real_verify = manager.verify_challenge

        def delayed_verify(**kwargs):
            self.now += 301
            return real_verify(**kwargs)

        with patch.object(manager, "verify_challenge", side_effect=delayed_verify):
            result = verify_or_reissue_challenge(runtime=SimpleNamespace(challenge_manager=manager),
                result_factory=CreateCommentResult, owner_key="owner", action="create_comment", payload=payload,
                challenge_id=prompt.challenge_id, answer="2.00")
        self.assertEqual(result.error, "challenge_unknown")
        self.assertEqual(result.operation_id, original)
        self.assertFalse(result.retryable)

    async def test_metadata_is_only_a_valid_uuid_and_existing_digests(self):
        prompt = await self.issue("create_comment", str(uuid4()))
        record = asdict(self.runtime.challenge_manager._challenges[prompt.challenge.challenge_id])
        self.assertEqual(record["operation_id"], prompt.operation_id)
        self.assertNotIn("owner", record.values())
        self.assertNotIn(self.args("create_comment")["content"], record.values())
        self.assertNotIn("payload", record)
        for value in ("opaque-sensitive-value", True, {}, 1):
            with self.assertRaises(ValueError):
                self.runtime.challenge_manager.issue_challenge(owner_key="owner", action="create_comment", payload={"operation_id": value})

    async def test_post_context_mismatch_and_verification_limits_preserve_original_key(self):
        original = str(uuid4())
        prompt = await self.issue("create_post", original)
        result = await self.call("create_post", self.answer("create_post", prompt, str(uuid4())))
        self.assertEqual(result.error, "challenge_context_mismatch")
        self.assertEqual(result.operation_id, original)
        self.assertIsNone(result.challenge)
        self.client.create_post.assert_not_awaited()
        payload = {"operation_id": original}
        manager = ChallengeManager(clock=lambda: self.now)
        with patch("challenge.CHALLENGE_FAIL_LIMIT", 1):
            challenge = manager.issue_challenge(owner_key="owner", action="create_comment", payload=payload)
            limited = verify_or_reissue_challenge(runtime=SimpleNamespace(challenge_manager=manager), result_factory=CreateCommentResult,
                owner_key="owner", action="create_comment", payload={"operation_id": str(uuid4())},
                challenge_id=challenge.challenge_id, answer="2.00")
        self.assertEqual(limited.error, "challenge_suspended")
        self.assertEqual(limited.operation_id, original)


if __name__ == "__main__":
    unittest.main()
