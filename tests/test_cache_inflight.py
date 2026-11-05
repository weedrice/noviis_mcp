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
