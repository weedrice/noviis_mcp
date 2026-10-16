from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from challenge import ChallengeManager, _calculate_answer, _normalize_answer
from exceptions import ChallengeExpired, ChallengeFailed, ChallengeRateLimited, ChallengeSuspended, ChallengeUsed
from tools.activity import CreateCommentResult, CreatePostResult
from tools.challenge_flow import issue_challenge_result, verify_or_reissue_challenge
from tools.notes import SendNoteResult


class ChallengeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 1000.0
        self.manager = ChallengeManager(clock=lambda: self.now)
        self.payload = {"content": "sample", "image_file_id": "12", "image_alt": "image"}
        settings = patch.multiple(
            "challenge", CHALLENGE_EXPIRES_SECONDS=120, CHALLENGE_FAIL_LIMIT=3,
            CHALLENGE_FAILURE_WINDOW_SECONDS=300, CHALLENGE_SUSPEND_SECONDS=60,
            CHALLENGE_MAX_PENDING=3, CHALLENGE_ISSUE_LIMIT=10,
            CHALLENGE_ISSUE_WINDOW_SECONDS=60, CHALLENGE_RETENTION_SECONDS=300,
        )
        settings.start()
        self.addCleanup(settings.stop)
        expression = patch("challenge._random_expression", return_value=(8, 3, "divide"))
        expression.start()
        self.addCleanup(expression.stop)

    def issue(self, owner: str = "owner"):
        return self.manager.issue_challenge(owner_key=owner, action="create_post", payload=self.payload)

    def verify(self, prompt, answer: str = "2.67", **overrides):
        values = dict(owner_key="owner", action="create_post", payload=self.payload,
                      challenge_id=prompt.challenge_id, answer=answer)
        values.update(overrides)
        return self.manager.verify_challenge(**values)

    def flow(self, prompt, answer="2.67", factory=CreatePostResult):
        return verify_or_reissue_challenge(
            runtime=SimpleNamespace(challenge_manager=self.manager), result_factory=factory,
            owner_key="owner", action="create_post", payload=self.payload,
            challenge_id=prompt.challenge_id, answer=answer,
        )

    def test_correct_answer_is_single_use_and_expiry_is_explicit(self):
        prompt = self.issue()
        self.assertEqual(prompt.expires_in_seconds, 120)
        self.assertIn("ROUND_HALF_UP", prompt.answer_format)
        self.now += 119
        self.verify(prompt)
        with self.assertRaises(ChallengeUsed):
            self.verify(prompt)

    def test_expiry_at_deadline_does_not_suspend_and_reissues(self):
        for _ in range(4):
            prompt = self.issue()
            self.now += 120
            with self.assertRaises(ChallengeExpired):
                self.verify(prompt)
        result = self.flow(prompt)
        self.assertEqual(result.error, "challenge_expired")
        self.assertTrue(result.retryable)
        self.verify(result.challenge)

    def test_wrong_owner_does_not_consume_original_challenge(self):
        prompt = self.issue()
        with self.assertRaises(ChallengeFailed):
            self.verify(prompt, owner_key="other-owner")
        self.verify(prompt)

    def test_action_and_payload_binding(self):
        variants = [
            {"action": "create_reply"},
            {"payload": dict(self.payload, content="changed")},
            {"payload": dict(self.payload, image_file_id="13")},
            {"payload": dict(self.payload, image_alt="changed")},
        ]
        for values in variants:
            with self.subTest(values=values):
                self.now += 301
                prompt = self.issue()
                with self.assertRaises(ChallengeFailed):
                    self.verify(prompt, **values)
                with self.assertRaises(ChallengeFailed):
                    self.verify(prompt)

    def test_invalid_numbers_are_counted_and_never_escape_as_decimal_errors(self):
        for answer in ["abc", "", "Infinity", "NaN", "sNaN", "1e999999", "9" * 10000]:
            with self.subTest(answer=answer[:20]):
                self.now += 301
                for attempt in range(3):
                    prompt = self.issue()
                    expected = ChallengeSuspended if attempt == 2 else ChallengeFailed
                    with self.assertRaises(expected):
                        self.verify(prompt, answer)

    def test_failed_answer_cannot_be_corrected_using_old_challenge(self):
        prompt = self.issue()
        with self.assertRaises(ChallengeFailed):
            self.verify(prompt, "0")
        with self.assertRaises(ChallengeFailed):
            self.verify(prompt)

    def test_success_does_not_reset_rolling_failure_window(self):
        for _ in range(2):
            with self.assertRaises(ChallengeFailed):
                self.verify(self.issue(), "0")
        self.verify(self.issue())
        with self.assertRaises(ChallengeSuspended):
            self.verify(self.issue(), "0")

    def test_failures_expire_and_suspension_recovers_at_deadline(self):
        for _ in range(2):
            with self.assertRaises(ChallengeFailed):
                self.verify(self.issue(), "0")
        self.now += 300
        for _ in range(2):
            with self.assertRaises(ChallengeFailed):
                self.verify(self.issue(), "0")
        with self.assertRaises(ChallengeSuspended):
            self.verify(self.issue(), "0")
        self.now += 59.1
        with self.assertRaises(ChallengeSuspended) as caught:
            self.issue()
        self.assertEqual(caught.exception.retry_after, 1)
        self.now += 0.9
        self.verify(self.issue())

    def test_pending_limit_is_per_owner_and_frees_on_completion(self):
        pending = [self.issue() for _ in range(3)]
        with self.assertRaises(ChallengeRateLimited) as caught:
            self.issue()
        self.assertEqual(caught.exception.code, "challenge_pending_limit")
        self.assertEqual(caught.exception.retry_after, 120)
        self.issue("another-owner")
        self.verify(pending[0])
        self.issue()

    def test_issue_rate_limit_applies_even_when_answers_are_correct(self):
        for _ in range(10):
            self.verify(self.issue())
        with self.assertRaises(ChallengeRateLimited) as caught:
            self.issue()
        self.assertEqual(caught.exception.code, "challenge_issue_limit")
        self.now += 60
        self.verify(self.issue())

    def test_terminal_records_and_idle_owners_are_eventually_removed(self):
        used = self.issue()
        self.verify(used)
        expired = self.issue("expiring-owner")
        with self.assertRaises(ChallengeFailed):
            self.verify(self.issue("failing-owner"), "0", owner_key="failing-owner")
        self.now += 421
        self.issue("new-owner")
        self.assertNotIn(used.challenge_id, self.manager._challenges)
        self.assertNotIn(expired.challenge_id, self.manager._challenges)
        self.assertEqual(len(self.manager._owners), 1)
        self.assertNotIn("new-owner", self.manager._owners)

    def test_payload_is_copied_at_issuance(self):
        self.payload["nested"] = {"value": "original"}
        prompt = self.issue()
        self.payload["nested"]["value"] = "changed"
        with self.assertRaises(ChallengeFailed):
            self.verify(prompt)

    def test_used_challenge_does_not_automatically_issue_a_new_write(self):
        prompt = self.issue()
        self.verify(prompt)
        result = self.flow(prompt)
        self.assertEqual(result.error, "challenge_used")
        self.assertFalse(result.retryable)
        self.assertIsNone(result.challenge)

    def test_all_write_result_types_support_structured_suspension(self):
        for _ in range(2):
            result = self.flow(self.issue(), "not-a-number")
            self.assertEqual(result.error, "challenge_failed")
        pending = result.challenge
        result = self.flow(pending, "not-a-number")
        self.assertEqual(result.status, "challenge_suspended")
        for factory in (CreatePostResult, CreateCommentResult, SendNoteResult):
            for result in (
                self.flow(pending, factory=factory),
                issue_challenge_result(
                    runtime=SimpleNamespace(challenge_manager=self.manager), result_factory=factory,
                    owner_key="owner", action="create_post", payload=self.payload,
                ),
            ):
                self.assertEqual(result.status, "challenge_suspended")
                self.assertTrue(result.retryable)
                self.assertEqual(result.retry_after_seconds, 60)

    def test_reissue_rate_limit_is_structured_too(self):
        for _ in range(9):
            self.verify(self.issue())
        result = self.flow(self.issue(), "wrong")
        self.assertEqual(result.status, "challenge_rate_limited")
        self.assertEqual(result.error, "challenge_issue_limit")
        self.assertTrue(result.retryable)
        self.assertEqual(result.retry_after_seconds, 60)

    def test_pending_limit_response_is_structured(self):
        for _ in range(3):
            self.issue()
        result = issue_challenge_result(
            runtime=SimpleNamespace(challenge_manager=self.manager), result_factory=CreateCommentResult,
            owner_key="owner", action="create_comment", payload=self.payload,
        )
        self.assertEqual(result.status, "challenge_rate_limited")
        self.assertEqual(result.error, "challenge_pending_limit")

    def test_decimal_rounding_and_signed_zero(self):
        self.assertEqual(_normalize_answer(" 2.665 "), "2.67")
        self.assertEqual(_normalize_answer("-0.00"), "0.00")
        self.assertEqual(_calculate_answer(1, 8, "divide"), "0.13")
        self.assertEqual(_calculate_answer(1, 99, "subtract"), "-98.00")


if __name__ == "__main__":
    unittest.main()
