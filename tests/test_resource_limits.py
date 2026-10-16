from __future__ import annotations

from dataclasses import asdict
import unittest
from unittest.mock import patch

from challenge import ChallengeManager
from exceptions import ChallengeRateLimited, ChallengeUsed
from transport import RequestSizeLimitMiddleware


class ChallengeCapacityTest(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.manager = ChallengeManager(clock=lambda: self.now)
        self.expression = patch("challenge._random_expression", return_value=(1, 1, "add"))
        self.expression.start()
        self.addCleanup(self.expression.stop)

    def issue(self, owner="owner", payload=None):
        return self.manager.issue_challenge(owner_key=owner, action="create_post", payload=payload or {"content": "sample"})

    def verify(self, prompt, owner="owner"):
        self.manager.verify_challenge(owner_key=owner, action="create_post", payload={"content": "sample"},
                                      challenge_id=prompt.challenge_id, answer="2.00")

    def test_records_are_bounded_without_evicting_used_ids(self):
        with patch("challenge.CHALLENGE_MAX_RECORDS", 2):
            used = self.issue()
            self.verify(used)
            pending = self.issue()
            with self.assertRaises(ChallengeRateLimited):
                self.issue("another")
            with self.assertRaises(ChallengeUsed):
                self.verify(used)
            self.verify(pending)
            self.assertEqual(len(self.manager._challenges), 2)
            self.now += 1000
            self.issue("another")
            self.assertEqual(len(self.manager._challenges), 1)

    def test_owner_failure_state_is_globally_bounded(self):
        from exceptions import ChallengeFailed
        with patch("challenge.CHALLENGE_MAX_RECORDS", 2):
            for owner in ("a", "b"):
                with self.assertRaises(ChallengeFailed):
                    self.manager.verify_challenge(owner_key=owner, action="create_post", payload={}, challenge_id="missing", answer="0")
            with self.assertRaises(ChallengeRateLimited):
                self.manager.verify_challenge(owner_key="c", action="create_post", payload={}, challenge_id="missing", answer="0")
            self.assertEqual(len(self.manager._owners), 2)

    def test_payload_limit_applies_before_state_allocation(self):
        with patch("challenge.CHALLENGE_MAX_PAYLOAD_BYTES", 20):
            with self.assertRaises(ValueError):
                self.issue(payload={"content": "x" * 21})
        self.assertEqual(self.manager._challenges, {})
        self.assertEqual(self.manager._owners, {})

    def test_capacity_pressure_does_not_displace_pending_owner_state(self):
        with patch("challenge.CHALLENGE_MAX_RECORDS", 1):
            pending = self.issue()
            self.now += 61
            # An unknown-ID attempt must not take the existing owner's slot
            # after its issuance window ends but before its challenge expires.
            with self.assertRaises(ChallengeRateLimited):
                self.manager.verify_challenge(owner_key="another", action="create_post", payload={}, challenge_id="missing", answer="0")
            self.verify(pending)

    def test_records_keep_digests_instead_of_token_or_content(self):
        prompt = self.issue(owner="test-sensitive-token", payload={"content": "test-private-message"})
        record = asdict(self.manager._challenges[prompt.challenge_id])
        self.assertNotIn("test-sensitive-token", repr(record))
        self.assertNotIn("test-private-message", repr(record))
        self.assertNotIn("payload", record)
        self.assertEqual(len(record["payload_digest"]), 64)


class RequestSizeTest(unittest.IsolatedAsyncioTestCase):
    async def run_request(self, chunks, headers=(), limit=10):
        received = []
        sent = []
        iterator = iter(chunks)

        async def receive():
            return next(iterator)

        async def send(message):
            sent.append(message)

        async def application(scope, receive, send):
            received.append(await receive())
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        await RequestSizeLimitMiddleware(application, limit)(
            {"type": "http", "method": "POST", "headers": headers}, receive, send,
        )
        return received, sent

    async def test_content_length_over_limit_is_rejected_before_reading(self):
        received, sent = await self.run_request([], headers=[(b"content-length", b"11")])
        self.assertEqual(sent[0]["status"], 413)
        self.assertEqual(received, [])

    async def test_chunked_and_understated_lengths_are_still_limited(self):
        for headers in ([], [(b"content-length", b"1")]):
            received, sent = await self.run_request([
                {"type": "http.request", "body": b"123456", "more_body": True},
                {"type": "http.request", "body": b"78901", "more_body": False},
            ], headers=headers)
            self.assertEqual(sent[0]["status"], 413)
            self.assertEqual(received, [])

    async def test_body_at_limit_is_preserved(self):
        received, sent = await self.run_request([
            {"type": "http.request", "body": b"12345", "more_body": True},
            {"type": "http.request", "body": b"67890", "more_body": False},
        ])
        self.assertEqual(sent[0]["status"], 200)
        self.assertEqual(received[0]["body"], b"1234567890")

    async def test_invalid_length_and_disconnect_do_not_reach_application(self):
        for value in (b"-1", b"invalid"):
            received, sent = await self.run_request([], headers=[(b"content-length", value)])
            self.assertEqual(sent[0]["status"], 400)
            self.assertEqual(received, [])
        received, sent = await self.run_request([{"type": "http.disconnect"}])
        self.assertEqual(received, [])
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
