import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import cache
from tools.activity import _load_boards


class CacheInflightTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        cache.clear_boards_cache()
        self.addCleanup(cache.clear_boards_cache)

    async def start_old_load(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def fetch(**kwargs):
            started.set()
            await release.wait()
            return {"data": [{"boardId": 1, "boardUrl": "old"}]}

        runtime = SimpleNamespace(client=SimpleNamespace(get_boards=fetch))
        task = asyncio.create_task(_load_boards(runtime, "current"))
        self.addAsyncCleanup(self.finish_task, task, release)
        await asyncio.wait_for(started.wait(), 2)
        return task, release

    async def finish_task(self, task, release):
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_refresh_cannot_be_overwritten_by_an_older_fetch(self):
        task, release = await self.start_old_load()
        fresh = [{"boardId": 1, "boardUrl": "new"}]
        runtime = SimpleNamespace(client=SimpleNamespace(get_boards=AsyncMock(return_value={"data": fresh})))
        self.assertEqual(await _load_boards(runtime, "current", force_refresh=True), fresh)
        release.set()
        await asyncio.wait_for(task, 2)
        self.assertEqual(cache.get_boards_cache("current"), fresh)
        self.assertEqual(cache._pending_loads, {})

    async def test_invalidation_during_load_does_not_repopulate_cache(self):
        for token in ("current", None):
            with self.subTest(token=token):
                task, release = await self.start_old_load()
                cache.clear_boards_cache(token)
                release.set()
                await asyncio.wait_for(task, 2)
                self.assertIsNone(cache.get_boards_cache("current"))
                self.assertEqual(cache._pending_loads, {})

    async def test_other_identity_invalidation_does_not_discard_current_load(self):
        task, release = await self.start_old_load()
        cache.clear_boards_cache("other")
        release.set()
        await asyncio.wait_for(task, 2)
        self.assertEqual(cache.get_boards_cache("current")[0]["boardUrl"], "old")
        self.assertEqual(cache._pending_loads, {})

    async def test_failure_and_cancellation_release_tickets(self):
        runtime = SimpleNamespace(client=SimpleNamespace(get_boards=AsyncMock(side_effect=RuntimeError("offline"))))
        with self.assertRaises(RuntimeError):
            await _load_boards(runtime, "current")
        self.assertEqual(cache._pending_loads, {})
        task, _ = await self.start_old_load()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(cache._pending_loads, {})

    async def test_failed_refresh_does_not_allow_older_fetch_to_restore_stale_data(self):
        task, release = await self.start_old_load()
        runtime = SimpleNamespace(client=SimpleNamespace(get_boards=AsyncMock(side_effect=RuntimeError("offline"))))
        with self.assertRaises(RuntimeError):
            await _load_boards(runtime, "current", force_refresh=True)
        release.set()
        await asyncio.wait_for(task, 2)
        self.assertIsNone(cache.get_boards_cache("current"))
        self.assertEqual(cache._pending_loads, {})

    async def test_concurrent_misses_share_one_fetch_and_copy_results(self):
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def fetch(**kwargs):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return {"data": [{"boardId": 1, "boardUrl": "general"}]}

        runtime = SimpleNamespace(client=SimpleNamespace(get_boards=fetch))
        tasks = [asyncio.create_task(_load_boards(runtime, "current")) for _ in range(10)]
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.sleep(0)
        self.assertEqual(calls, 1)
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks), 2)
        results[0][0]["boardUrl"] = "changed"
        self.assertTrue(all(result[0]["boardUrl"] == "general" for result in results[1:]))
        self.assertEqual(cache.get_boards_cache("current")[0]["boardUrl"], "general")
        self.assertEqual(cache._inflight, {})

    async def test_waiter_cancellation_does_not_cancel_owner(self):
        owner, release = await self.start_old_load()
        client = SimpleNamespace(get_boards=AsyncMock())
        waiter = asyncio.create_task(_load_boards(SimpleNamespace(client=client), "current"))
        await asyncio.sleep(0)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertFalse(owner.done())
        release.set()
        await asyncio.wait_for(owner, 2)
        client.get_boards.assert_not_called()
        self.assertEqual(cache._inflight, {})

    async def test_owner_cancellation_retries_with_waiters_client(self):
        owner, _ = await self.start_old_load()
        fresh = [{"boardId": 1, "boardUrl": "new"}]
        client = SimpleNamespace(get_boards=AsyncMock(return_value={"data": fresh}))
        waiter = asyncio.create_task(_load_boards(SimpleNamespace(client=client), "current"))
        await asyncio.sleep(0)
        owner.cancel()
        self.assertEqual(await asyncio.wait_for(waiter, 2), fresh)
        client.get_boards.assert_awaited_once()
        self.assertEqual(cache._inflight, {})

    async def test_error_is_shared_and_a_later_request_can_retry(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def fetch(**kwargs):
            started.set()
            await release.wait()
            raise RuntimeError("offline")

        client = SimpleNamespace(get_boards=AsyncMock(side_effect=fetch))
        runtime = SimpleNamespace(client=client)
        owner = asyncio.create_task(_load_boards(runtime, "current"))
        await asyncio.wait_for(started.wait(), 2)
        waiter = asyncio.create_task(_load_boards(runtime, "current"))
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.wait_for(asyncio.gather(owner, waiter, return_exceptions=True), 2)
        self.assertTrue(all(isinstance(result, RuntimeError) for result in results))
        client.get_boards.assert_awaited_once()
        self.assertEqual(cache._inflight, {})
        client.get_boards.side_effect = None
        client.get_boards.return_value = {"data": []}
        self.assertEqual(await _load_boards(runtime, "current"), [])
        self.assertEqual(client.get_boards.await_count, 2)

    async def test_other_identity_and_invalidated_loads_are_not_shared(self):
        owner, release = await self.start_old_load()
        fresh = [{"boardId": 1, "boardUrl": "new"}]
        client = SimpleNamespace(get_boards=AsyncMock(return_value={"data": fresh}))
        runtime = SimpleNamespace(client=client)
        self.assertEqual(await _load_boards(runtime, "other"), fresh)
        cache.clear_boards_cache("current")
        self.assertEqual(await _load_boards(runtime, "current"), fresh)
        self.assertEqual(client.get_boards.await_count, 2)
        release.set()
        await asyncio.wait_for(owner, 2)
        self.assertEqual(cache.get_boards_cache("current"), fresh)
