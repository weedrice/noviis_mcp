from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cache import clear_boards_cache, get_boards_cache, set_boards_cache
from tools.activity import _resolve_board_url


class BoardCacheTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clear_boards_cache()
        self.addCleanup(clear_boards_cache)

    def test_isolation_expiry_and_copying(self):
        with patch("cache.time.monotonic", return_value=100), patch("cache.BOARDS_CACHE_TTL_SECONDS", 300):
            original = [{"boardId": 2, "boardUrl": "private-a"}]
            set_boards_cache(original, agent_token="agent-a")
            original[0]["boardUrl"] = "mutated"
            self.assertIsNone(get_boards_cache("agent-b"))
            cached = get_boards_cache("agent-a")
            self.assertEqual(cached[0]["boardUrl"], "private-a")
            cached.clear()
            self.assertEqual(len(get_boards_cache("agent-a")), 1)
        with patch("cache.time.monotonic", return_value=400), patch("cache.BOARDS_CACHE_TTL_SECONDS", 300):
            self.assertIsNone(get_boards_cache("agent-a"))

    def test_empty_lists_are_cached_and_capacity_is_bounded(self):
        with patch("cache._MAX_CACHE_ENTRIES", 2):
            set_boards_cache([], agent_token="a")
            self.assertEqual(get_boards_cache("a"), [])
            set_boards_cache([], agent_token="b")
            set_boards_cache([], agent_token="c")
            self.assertIsNone(get_boards_cache("a"))
            self.assertEqual(get_boards_cache("b"), [])
            clear_boards_cache("b")
            self.assertIsNone(get_boards_cache("b"))
            self.assertEqual(get_boards_cache("c"), [])

    async def test_cache_miss_fetches_only_current_agents_boards(self):
        fetch = AsyncMock(return_value={"data": [{"board_id": 2, "board_url": "own-board"}]})
        runtime = SimpleNamespace(client=SimpleNamespace(get_boards=fetch))
        set_boards_cache([{"boardId": 2, "boardUrl": "other-private-board"}], agent_token="other")
        self.assertEqual(await _resolve_board_url(runtime, 2, "current"), "own-board")
        self.assertEqual(await _resolve_board_url(runtime, 2, "current"), "own-board")
        fetch.assert_awaited_once_with(token="current")

    async def test_unknown_board_does_not_fall_back_to_numeric_url(self):
        fetch = AsyncMock(return_value={"data": []})
        with self.assertRaisesRegex(ValueError, "not found"):
            await _resolve_board_url(SimpleNamespace(client=SimpleNamespace(get_boards=fetch)), 999, "current")



if __name__ == "__main__":
    unittest.main()
