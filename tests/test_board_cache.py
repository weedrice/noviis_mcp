from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import cache
from copy import deepcopy

from cache import clear_boards_cache, get_boards_cache, set_boards_cache
from tools.activity import _resolve_board_url, _load_boards


class BoardCacheTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clear_boards_cache()
        self.addCleanup(clear_boards_cache)

    def test_isolation_expiry_and_copying(self):
        with patch("cache.time.monotonic", return_value=100), patch("cache.BOARDS_CACHE_TTL_SECONDS", 300):
            original = [{"postCount": 0, "boardId": 2, "boardUrl": "private-a"}]
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

    async def test_write_resolution_always_fetches_only_current_agents_boards(self):
        fetch = AsyncMock(return_value={"data": [{"postCount": 0, "board_id": 2, "board_url": "own-board"}]})
        runtime = SimpleNamespace(client=SimpleNamespace(get_boards=fetch))
        set_boards_cache([{"postCount": 0, "boardId": 2, "boardUrl": "other-private-board"}], agent_token="other")
        self.assertEqual(await _resolve_board_url(runtime, 2, "current"), "own-board")
        self.assertEqual(await _resolve_board_url(runtime, 2, "current"), "own-board")
        self.assertEqual(fetch.await_count, 2)
        fetch.assert_awaited_with(token="current")

    async def test_unknown_board_does_not_fall_back_to_numeric_url(self):
        fetch = AsyncMock(return_value={"data": []})
        with self.assertRaisesRegex(ValueError, "not found"):
            await _resolve_board_url(SimpleNamespace(client=SimpleNamespace(get_boards=fetch)), 999, "current")

    def test_byte_budget_evicts_old_entries_without_cross_identity_leaks(self):
        boards = [{"boardId": 1, "description": "x" * 1000}]
        size = cache._payload_bytes(deepcopy(boards), 100000)
        with patch("cache.BOARDS_CACHE_MAX_BYTES", size * 2):
            for token in ("a", "b", "c"):
                set_boards_cache(boards, agent_token=token)
            self.assertIsNone(get_boards_cache("a"))
            self.assertEqual(get_boards_cache("b"), boards)
            self.assertEqual(get_boards_cache("c"), boards)
            self.assertLessEqual(sum(cache._entry_bytes.values()), size * 2)
        self.assertEqual(set(cache._entry_bytes), set(cache._boards_cache))

    def test_replacement_invalidation_and_expiry_release_accounting(self):
        boards = [{"boardId": 1}]
        with patch("cache.time.monotonic", return_value=100):
            set_boards_cache(boards, agent_token="a")
            set_boards_cache([], agent_token="a")
            self.assertEqual(sum(cache._entry_bytes.values()), cache._payload_bytes([], 1000))
            set_boards_cache([], agent_token="b")
            clear_boards_cache("a")
            self.assertEqual(len(cache._entry_bytes), 1)
        with patch("cache.time.monotonic", return_value=400), patch("cache.BOARDS_CACHE_TTL_SECONDS", 300):
            self.assertIsNone(get_boards_cache("b"))
            self.assertEqual(cache._entry_bytes, {})
        set_boards_cache([], agent_token="a")
        clear_boards_cache()
        self.assertEqual(cache._entry_bytes, {})

    async def test_oversized_response_is_returned_but_not_cached(self):
        large = [{"boardId": 2, "boardUrl": "general", "postCount": 0, "description": "x" * 5000}]
        fetch = AsyncMock(return_value={"data": large})
        runtime = SimpleNamespace(client=SimpleNamespace(get_boards=fetch))
        with patch("cache.BOARDS_CACHE_MAX_BYTES", 1000):
            set_boards_cache([], agent_token="other")
            set_boards_cache([], agent_token="current")
            self.assertEqual(await _load_boards(runtime, "current", force_refresh=True), large)
            self.assertIsNone(get_boards_cache("current"))
            self.assertEqual(await _load_boards(runtime, "current"), large)
            self.assertEqual(fetch.await_count, 2)
            self.assertEqual(get_boards_cache("other"), [])
            self.assertLessEqual(sum(cache._entry_bytes.values()), 1000)

    def test_invalidated_inflight_result_does_not_consume_budget(self):
        ticket = cache.begin_boards_load("a")
        clear_boards_cache("a")
        set_boards_cache([{"description": "large" * 100}], agent_token="a", load_ticket=ticket)
        cache.finish_boards_load(ticket)
        self.assertEqual(cache._entry_bytes, {})
        self.assertIsNone(get_boards_cache("a"))

    def test_payload_size_is_bounded_and_handles_deep_or_shared_objects(self):
        value = "large" * 1000
        self.assertLess(cache._payload_bytes([value, value], 100000), 2 * len(value))
        self.assertGreater(cache._payload_bytes([value], 100), 100)
        nested = []
        for _ in range(2000):
            nested = [nested]
        self.assertGreater(cache._payload_bytes(nested, 1000000), 1000)



if __name__ == "__main__":
    unittest.main()
