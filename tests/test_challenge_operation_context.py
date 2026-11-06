from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from challenge import ChallengeManager
from exceptions import ChallengeExpired, ChallengeRateLimited, ChallengeSuspended
from tools.activity import CreateCommentResult
from tools.challenge_flow import issue_challenge_result, verify_or_reissue_challenge
from tools.tool_contract import tool_contracts


def limited():
    return ChallengeRateLimited(code="challenge_issue_limit", retry_after=30, message="wait")


class ChallengeOperationContextTest(unittest.TestCase):
    def test_issue_limits_preserve_supplied_operation_but_never_return_a_fresh_key(self):
        for supported in (True, False):
            for operation in (str(uuid4()), None):
                for error in (limited(), ChallengeSuspended(30)):
                    manager = Mock()
                    manager.issue_challenge.side_effect = error
                    with patch("tools.operations.NOVIIS_OPERATION_SUPPORT", supported):
                        result = issue_challenge_result(
                            runtime=SimpleNamespace(challenge_manager=manager), result_factory=CreateCommentResult,
                            owner_key="synthetic", action="create_comment", payload={"operation_id": operation})
                    self.assertEqual(result.operation_id, operation if supported else None)
                    self.assertIsNone(result.challenge)
                    self.assertEqual(result.retry_after_seconds, 30)

    def test_verification_limits_keep_original_operation_without_replacement_challenge(self):
        operation = str(uuid4())
        challenge = str(uuid4())
        for error in (limited(), ChallengeSuspended(30)):
            manager = Mock()
            manager.verify_challenge.side_effect = error
            manager.recovery_operation_id.return_value = operation
            with patch("tools.operations.NOVIIS_OPERATION_SUPPORT", True):
                result = verify_or_reissue_challenge(
                    runtime=SimpleNamespace(challenge_manager=manager), result_factory=CreateCommentResult,
                    owner_key="synthetic", action="create_comment", payload={"operation_id": operation},
                    challenge_id=challenge, answer="2.00")
            self.assertEqual(result.operation_id, operation)
            self.assertIsNone(result.challenge)
            manager.issue_challenge.assert_not_called()

    def test_expired_challenge_reissue_limit_preserves_the_existing_key(self):
        operation = str(uuid4())
        manager = Mock()
        manager.verify_challenge.side_effect = ChallengeExpired("expired")
        manager.recovery_operation_id.return_value = operation
        manager.issue_challenge.side_effect = limited()
        with patch("tools.operations.NOVIIS_OPERATION_SUPPORT", True):
            result = verify_or_reissue_challenge(
                runtime=SimpleNamespace(challenge_manager=manager), result_factory=CreateCommentResult,
                owner_key="synthetic", action="create_comment", payload={"operation_id": operation},
                challenge_id=str(uuid4()), answer="2.00")
        self.assertEqual(result.operation_id, operation)
        self.assertIsNone(result.challenge)


class SDKChallengeOperationContextTest(unittest.IsolatedAsyncioTestCase):
    async def test_used_challenge_retains_explicit_or_generated_recovery_key(self):
        for supplied in (str(uuid4()), None):
            backend = AsyncMock()
            backend.get_agent_status.return_value = {"status": "active"}
            backend.create_comment.return_value = {"commentId": 7}
            runtime = SimpleNamespace(client=backend, challenge_manager=ChallengeManager())
            ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=runtime))
            args = {"agent_token": "synthetic", "post_id": 7, "content": "sample " * 10}
            if supplied:
                args["operation_id"] = supplied
            with patch("tools.operations.NOVIIS_OPERATION_SUPPORT", True), patch("challenge._random_expression", return_value=(1, 1, "add")):
                issued = await tool_contracts()["create_comment"].run(args, context=ctx)
                final_args = {**args, "operation_id": issued.operation_id,
                              "challenge_id": issued.challenge.challenge_id, "answer": "2.00"}
                await tool_contracts()["create_comment"].run(final_args, context=ctx)
                used = await tool_contracts()["create_comment"].run(final_args, context=ctx)
            self.assertEqual(used.error, "challenge_used")
            self.assertEqual(used.operation_id, issued.operation_id)
            self.assertFalse(used.retryable)
            self.assertIsNone(used.challenge)
            backend.create_comment.assert_awaited_once()

    async def test_disabled_operation_support_does_not_invent_recovery_keys(self):
        backend = AsyncMock()
        backend.get_agent_status.return_value = {"status": "active"}
        backend.create_comment.return_value = {"commentId": 7}
        runtime = SimpleNamespace(client=backend, challenge_manager=ChallengeManager())
        ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=runtime))
        args = {"agent_token": "synthetic", "post_id": 7, "content": "sample " * 10}
        with patch("tools.operations.NOVIIS_OPERATION_SUPPORT", False), patch("challenge._random_expression", return_value=(1, 1, "add")):
            issued = await tool_contracts()["create_comment"].run(args, context=ctx)
            final_args = {**args, "challenge_id": issued.challenge.challenge_id, "answer": "2.00"}
            await tool_contracts()["create_comment"].run(final_args, context=ctx)
            used = await tool_contracts()["create_comment"].run(final_args, context=ctx)
        self.assertIsNone(used.operation_id)
