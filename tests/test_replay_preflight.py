import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from mcp.server.fastmcp.exceptions import ToolError

from challenge import ChallengeManager
from exceptions import ChallengeUnknown, NoviIsAPIError, ServerError, Unauthorized
from tools.tool_contract import tool_contracts


def quota(remaining):
    return {"hard_constraints": {"suspended": False, "can_create_post": remaining > 0,
                                 "posts_remaining": remaining}}


def limited():
    return NoviIsAPIError(status_code=429, code="rate_limited", message="wait", details={"retry_after_seconds": 1})


CASES = {
    "create_post": ({"title": "sample", "board_url": "normal"}, {"postId": 1}),
    "create_comment": ({"post_id": 1}, {"commentId": 2}),
    "create_reply": ({"comment_id": 2}, {"commentId": 3}),
    "send_note": ({"recipient_agent_name": "recipient"}, {"noteId": 4, "noteThreadId": 1}),
}


class ReplayPreflightSDKTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 1000.0
        self.client = AsyncMock()
        self.client.get_agent_status.return_value = {"status": "active"}
        self.client.get_agent_home.return_value = quota(1)
        self.runtime = SimpleNamespace(client=self.client, challenge_manager=ChallengeManager(clock=lambda: self.now))
        self.ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=self.runtime))
        for name, (_, result) in CASES.items():
            getattr(self.client, name).return_value = result
        for setting in (patch("challenge._random_expression", return_value=(1, 1, "add")),
                        patch("challenge.CHALLENGE_RETENTION_SECONDS", 300),
                        patch("tools.operations.NOVIIS_OPERATION_SUPPORT", False)):
            setting.start()
            self.addCleanup(setting.stop)

    def args(self, name):
        return {"agent_token": "owner", "content": "sample " * 10, **CASES[name][0]}

    async def call(self, name, args):
        return await tool_contracts()[name].run(args, context=self.ctx)

    async def issued_args(self, name, operation=None):
        args = self.args(name)
        if operation is not None:
            args["operation_id"] = operation
        issued = await self.call(name, args)
        final = {**args, "challenge_id": issued.challenge.challenge_id, "answer": "2.00"}
        if issued.operation_id:
            final["operation_id"] = issued.operation_id
        return final

    async def test_last_quota_commit_with_lost_response_keeps_used_outcome(self):
        final = await self.issued_args("create_post")
        self.client.create_post.side_effect = ServerError("Synthetic reply lost after commit")
        with self.assertRaises(ToolError):
            await self.call("create_post", final)
        self.client.get_agent_home.return_value = quota(0)
        reads = self.client.get_agent_home.await_count
        result = await self.call("create_post", final)
        self.assertEqual((result.error, result.retryable), ("challenge_used", False))
        self.assertIn("previous write result", result.message)
        self.assertEqual(self.client.get_agent_home.await_count, reads)
        self.client.create_post.assert_awaited_once()

    async def test_terminal_replays_precede_quota_auth_limits_and_revocation_for_all_writes(self):
        for name in CASES:
            for history in ("used", "retention", "restart"):
                for supported in (False, True):
                    for error in (limited(), Unauthorized("revoked")):
                        with self.subTest(tool=name, history=history, support=supported, error=type(error).__name__):
                            self.runtime.challenge_manager = ChallengeManager(clock=lambda: self.now)
                            self.client.get_agent_status.side_effect = None
                            self.client.get_agent_home.return_value = quota(1)
                            method = getattr(self.client, name)
                            method.reset_mock()
                            with patch("tools.operations.NOVIIS_OPERATION_SUPPORT", supported):
                                final = await self.issued_args(name, str(uuid4()) if supported else None)
                                await self.call(name, final)
                                if history == "retention":
                                    self.now += 301
                                elif history == "restart":
                                    self.runtime.challenge_manager = ChallengeManager(clock=lambda: self.now)
                                self.client.get_agent_home.return_value = quota(0)
                                self.client.get_agent_status.side_effect = error
                                before = len(self.client.mock_calls)
                                result = await self.call(name, final)
                            self.assertEqual(result.error, "challenge_used" if history == "used" else "challenge_unknown")
                            self.assertFalse(result.retryable)
                            self.assertIsNone(result.challenge)
                            self.assertEqual(result.operation_id, final.get("operation_id"))
                            self.assertEqual(len(self.client.mock_calls), before)
                            method.assert_awaited_once()

    async def test_generated_operation_key_survives_replay(self):
        with patch("tools.operations.NOVIIS_OPERATION_SUPPORT", True):
            final = await self.issued_args("create_comment")
            await self.call("create_comment", final)
            self.client.get_agent_status.side_effect = limited()
            result = await self.call("create_comment", final)
        self.assertEqual(result.operation_id, final["challenge_id"])
        self.assertFalse(result.retryable)

    async def test_foreign_and_absent_ids_are_indistinguishable_and_never_mutate_original(self):
        for name in CASES:
            for used in (False, True):
                self.runtime.challenge_manager = ChallengeManager(clock=lambda: self.now)
                final = await self.issued_args(name)
                if used:
                    await self.call(name, final)
                before = len(self.client.mock_calls)
                foreign = await self.call(name, {**final, "agent_token": "other"})
                absent = await self.call(name, {**final, "agent_token": "other", "challenge_id": str(uuid4())})
                self.assertEqual(foreign, absent)
                self.assertEqual(foreign.error, "challenge_unknown")
                self.assertEqual(len(self.client.mock_calls), before)
                self.assertEqual(len(self.runtime.challenge_manager._owners), 1)
                record = self.runtime.challenge_manager._challenges[final["challenge_id"]]
                self.assertEqual(record.status, "used" if used else "pending")
                if not used:
                    completed = await self.call(name, final)
                    self.assertIn(completed.status, {"created", "sent"})

    async def test_pending_auth_failure_never_consumes_and_remains_reusable(self):
        for name in CASES:
            for error in (limited(), Unauthorized("revoked")):
                with self.subTest(tool=name, error=type(error).__name__):
                    self.runtime.challenge_manager = ChallengeManager(clock=lambda: self.now)
                    self.client.get_agent_status.side_effect = None
                    getattr(self.client, name).reset_mock()
                    final = await self.issued_args(name)
                    self.client.get_agent_status.side_effect = error
                    if isinstance(error, Unauthorized):
                        with self.assertRaises(ToolError):
                            await self.call(name, final)
                    else:
                        self.assertEqual((await self.call(name, final)).error, "rate_limited")
                    self.assertEqual(self.runtime.challenge_manager._challenges[final["challenge_id"]].status, "pending")
                    getattr(self.client, name).assert_not_awaited()
                    self.client.get_agent_status.side_effect = None
                    self.assertIn((await self.call(name, final)).status, {"created", "sent"})
                    getattr(self.client, name).assert_awaited_once()

    async def test_pending_post_quota_failure_remains_reusable(self):
        final = await self.issued_args("create_post")
        self.client.get_agent_home.return_value = quota(0)
        self.assertEqual((await self.call("create_post", final)).error, "post_daily_limit_exceeded")
        self.assertEqual(self.runtime.challenge_manager._challenges[final["challenge_id"]].status, "pending")
        self.client.create_post.assert_not_awaited()
        self.client.get_agent_home.return_value = quota(1)
        self.assertEqual((await self.call("create_post", final)).status, "created")

    async def test_concurrent_auth_limit_cannot_hide_another_requests_consumption(self):
        for name in CASES:
            for supported in (False, True):
                with self.subTest(tool=name, operation_support=supported):
                    self.runtime.challenge_manager = ChallengeManager(clock=lambda: self.now)
                    self.client.get_agent_status.side_effect = None
                    getattr(self.client, name).reset_mock()
                    entered, release = asyncio.Event(), asyncio.Event()
                    calls = 0

                    async def status(**kwargs):
                        nonlocal calls
                        calls += 1
                        if calls == 1:
                            entered.set()
                            await release.wait()
                            raise limited()
                        return {"status": "active"}

                    with patch("tools.operations.NOVIIS_OPERATION_SUPPORT", supported):
                        final = await self.issued_args(name)
                        self.client.get_agent_status.side_effect = status
                        slow = asyncio.create_task(self.call(name, final))
                        try:
                            await asyncio.wait_for(entered.wait(), 3)
                            completed = await self.call(name, final)
                        finally:
                            release.set()
                            replay = await asyncio.wait_for(slow, 3)
                    self.assertIn(completed.status, {"created", "sent"})
                    self.assertEqual((replay.error, replay.retryable), ("challenge_used", False))
                    self.assertEqual(replay.operation_id, final.get("operation_id"))
                    self.assertIsNone(replay.challenge)
                    self.assertEqual(calls, 2)
                    getattr(self.client, name).assert_awaited_once()

    async def test_concurrent_quota_failure_cannot_hide_another_posts_consumption(self):
        for supported in (False, True):
            with self.subTest(operation_support=supported):
                self.runtime.challenge_manager = ChallengeManager(clock=lambda: self.now)
                self.client.get_agent_home.side_effect = None
                self.client.create_post.reset_mock()
                entered, release = asyncio.Event(), asyncio.Event()
                calls = 0

                async def home(**kwargs):
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        entered.set()
                        await release.wait()
                        return quota(0)
                    return quota(1)

                with patch("tools.operations.NOVIIS_OPERATION_SUPPORT", supported):
                    final = await self.issued_args("create_post")
                    self.client.get_agent_home.side_effect = home
                    slow = asyncio.create_task(self.call("create_post", final))
                    try:
                        await asyncio.wait_for(entered.wait(), 3)
                        completed = await self.call("create_post", final)
                    finally:
                        release.set()
                        replay = await asyncio.wait_for(slow, 3)
                self.assertEqual(completed.status, "created")
                self.assertEqual((replay.error, replay.retryable), ("challenge_used", False))
                self.assertEqual(replay.operation_id, final.get("operation_id"))
                self.assertIsNone(replay.challenge)
                self.assertEqual(calls, 2)
                self.client.create_post.assert_awaited_once()

    async def test_new_challenge_still_requires_authentication(self):
        self.client.get_agent_status.side_effect = Unauthorized("revoked")
        for name in CASES:
            with self.subTest(tool=name), self.assertRaises(ToolError):
                await self.call(name, self.args(name))
            getattr(self.client, name).assert_not_awaited()
        self.assertEqual(self.runtime.challenge_manager._challenges, {})
        self.assertEqual(self.runtime.challenge_manager._owners, {})

    def test_replay_inspection_never_allocates_unknown_owner_state(self):
        for _ in range(10):
            with self.assertRaises(ChallengeUnknown):
                self.runtime.challenge_manager.check_replay(owner_key=str(uuid4()), challenge_id=str(uuid4()))
        self.assertEqual(self.runtime.challenge_manager._owners, {})
        self.assertEqual(self.runtime.challenge_manager._challenges, {})


if __name__ == "__main__":
    unittest.main()
