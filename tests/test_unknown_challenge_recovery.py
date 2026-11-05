from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from challenge import ChallengeManager
from exceptions import ChallengeFailed, ChallengeSuspended, ChallengeUnknown, ChallengeUsed
from tools.activity import CreateCommentResult
from tools.challenge_flow import verify_or_reissue_challenge
from tools.tool_contract import tool_contracts


class UnknownChallengeTest(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.manager = ChallengeManager(clock=lambda: self.now)
        settings = patch.multiple(
            "challenge", CHALLENGE_EXPIRES_SECONDS=120, CHALLENGE_RETENTION_SECONDS=300,
            CHALLENGE_FAIL_LIMIT=3, CHALLENGE_SUSPEND_SECONDS=60,
        )
        settings.start()
        self.addCleanup(settings.stop)
        expression = patch("challenge._random_expression", return_value=(1, 1, "add"))
        expression.start()
        self.addCleanup(expression.stop)

    def issue(self):
        return self.manager.issue_challenge(owner_key="owner", action="create_comment", payload={"content": "sample"})

    def verify(self, challenge_id, **changes):
        values = dict(owner_key="owner", action="create_comment", payload={"content": "sample"},
                      challenge_id=challenge_id, answer="2.00")
        values.update(changes)
        return self.manager.verify_challenge(**values)

    def flow(self, challenge_id, **changes):
        values = dict(runtime=SimpleNamespace(challenge_manager=self.manager), result_factory=CreateCommentResult,
                      owner_key="owner", action="create_comment", payload={"content": "sample"},
                      challenge_id=challenge_id, answer="2.00")
        values.update(changes)
        return verify_or_reissue_challenge(**values)

    def test_unknown_stays_nonretryable_while_failures_still_suspend_fresh_issuance(self):
        for _ in range(5):
            result = self.flow(str(uuid4()))
            self.assertEqual((result.status, result.error, result.retryable), ("blocked", "challenge_unknown", False))
            self.assertIn("outcome is unknown", result.message)
            self.assertIn("Inspect previous", result.message)
            self.assertIsNone(result.challenge)
        self.assertEqual(self.manager._challenges, {})
        with self.assertRaises(ChallengeSuspended):
            self.issue()
        self.now += 60
        self.verify(self.issue().challenge_id)

    def test_unknown_storage_pressure_does_not_allocate_or_displace_owner_state(self):
        pending = self.issue()
        with patch("challenge.CHALLENGE_MAX_RECORDS", 1):
            result = self.flow(str(uuid4()), owner_key="other")
        self.assertEqual(result.error, "challenge_unknown")
        self.assertFalse(result.retryable)
        self.assertEqual(len(self.manager._owners), 1)
        self.verify(pending.challenge_id)

    def test_used_record_cannot_be_reissued_by_changing_payload_or_action(self):
        used = self.issue()
        self.verify(used.challenge_id)
        for changes in ({"action": "send_note"}, {"payload": {"content": "changed"}}):
            result = self.flow(used.challenge_id, **changes)
            self.assertEqual((result.status, result.error, result.retryable), ("blocked", "challenge_used", False))
            self.assertIsNone(result.challenge)
        self.assertEqual(len(self.manager._challenges), 1)

    def test_used_record_remains_nonretryable_during_owner_suspension(self):
        used = self.issue()
        self.verify(used.challenge_id)
        for _ in range(3):
            with self.assertRaises(ChallengeUnknown):
                self.verify(str(uuid4()))
        with self.assertRaises(ChallengeUsed):
            self.verify(used.challenge_id, payload={"content": "changed"})

    def test_foreign_owner_cannot_disclose_or_consume_pending_or_used_state(self):
        pending = self.issue()
        with self.assertRaises(ChallengeFailed) as before:
            self.verify(pending.challenge_id, owner_key="other")
        self.assertEqual(self.manager._challenges[pending.challenge_id].status, "pending")
        self.verify(pending.challenge_id)
        with self.assertRaises(ChallengeFailed) as after:
            self.verify(pending.challenge_id, owner_key="other")
        self.assertEqual(str(before.exception), str(after.exception))
        self.assertEqual(self.manager._challenges[pending.challenge_id].status, "used")
        self.verify(self.issue().challenge_id)

    def test_known_expired_and_incorrect_pending_still_reissue(self):
        pending = self.issue()
        self.now += 120
        expired = self.flow(pending.challenge_id)
        self.assertEqual(expired.error, "challenge_expired")
        self.assertTrue(expired.retryable)
        self.assertIsNotNone(expired.challenge)
        incorrect = self.flow(expired.challenge.challenge_id, answer="0")
        self.assertEqual(incorrect.error, "challenge_failed")
        self.assertTrue(incorrect.retryable)
        self.verify(incorrect.challenge.challenge_id)

    def test_expired_record_after_retention_is_unknown_not_reissued(self):
        pending = self.issue()
        self.now += 421
        result = self.flow(pending.challenge_id)
        self.assertEqual(result.error, "challenge_unknown")
        self.assertFalse(result.retryable)
        self.assertEqual(self.manager._challenges, {})


class SDKUnknownChallengeTest(unittest.IsolatedAsyncioTestCase):
    async def test_all_creation_tools_block_replay_after_retention_or_restart(self):
        cases = {
            "create_post": ({"title": "sample", "board_url": "normal"}, {"postId": 1}),
            "create_comment": ({"post_id": "1"}, {"commentId": 2}),
            "create_reply": ({"comment_id": "2"}, {"commentId": 3}),
            "send_note": ({"recipient_agent_name": "recipient"}, {"noteId": 4, "noteThreadId": 1}),
        }
        for name, (extra, response) in cases.items():
            for supported in (False, True):
                for loss in ("retention", "restart"):
                    with self.subTest(tool=name, operation_support=supported, history_loss=loss):
                        now = [1000.0]
                        backend = AsyncMock()
                        backend.get_agent_status.return_value = {"status": "active"}
                        backend.get_agent_home.return_value = {"hard_constraints": {
                            "suspended": False, "can_create_post": True, "posts_remaining": 3}}
                        getattr(backend, name).return_value = response
                        runtime = SimpleNamespace(client=backend, challenge_manager=ChallengeManager(clock=lambda: now[0]))
                        ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=runtime))
                        args = {"agent_token": "owner", "content": "sample " * 10, **extra}
                        tool = tool_contracts()[name]
                        with patch("tools.operations.NOVIIS_OPERATION_SUPPORT", supported), \
                                patch("challenge.CHALLENGE_RETENTION_SECONDS", 300), \
                                patch("challenge._random_expression", return_value=(1, 1, "add")):
                            issued = await tool.run(args, context=ctx)
                            final_args = {**args, "challenge_id": issued.challenge.challenge_id, "answer": "2.00"}
                            if supported:
                                final_args["operation_id"] = issued.operation_id
                            completed = await tool.run(final_args, context=ctx)
                            self.assertIn(completed.status, {"created", "sent"})
                            if loss == "retention":
                                now[0] += 301
                            else:
                                runtime.challenge_manager = ChallengeManager(clock=lambda: now[0])
                            result = await tool.run(final_args, context=ctx)
                        self.assertEqual((result.status, result.error, result.retryable), ("blocked", "challenge_unknown", False))
                        self.assertIsNone(result.challenge)
                        self.assertEqual(result.operation_id, issued.operation_id if supported else None)
                        self.assertEqual(runtime.challenge_manager._challenges, {})
                        getattr(backend, name).assert_awaited_once()

    async def test_used_challenge_changed_payload_never_reaches_backend_twice(self):
        backend = AsyncMock()
        backend.get_agent_status.return_value = {"status": "active"}
        backend.create_comment.return_value = {"commentId": 2}
        runtime = SimpleNamespace(client=backend, challenge_manager=ChallengeManager())
        ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=runtime))
        args = {"agent_token": "owner", "post_id": "1", "content": "sample " * 10}
        tool = tool_contracts()["create_comment"]
        with patch("tools.operations.NOVIIS_OPERATION_SUPPORT", False), \
                patch("challenge._random_expression", return_value=(1, 1, "add")):
            issued = await tool.run(args, context=ctx)
            final_args = {**args, "challenge_id": issued.challenge.challenge_id, "answer": "2.00"}
            await tool.run(final_args, context=ctx)
            result = await tool.run({**final_args, "content": "changed " * 10}, context=ctx)
        self.assertEqual(result.error, "challenge_used")
        self.assertFalse(result.retryable)
        self.assertIsNone(result.challenge)
        backend.create_comment.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
