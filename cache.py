from __future__ import annotations

import hashlib
import time
from collections.abc import Sequence
from copy import deepcopy
from typing import Any

from config import BOARDS_CACHE_TTL_SECONDS

# Bound the number of cached identities and avoid retaining raw bearer tokens as keys.
_MAX_CACHE_ENTRIES = 256
_boards_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _cache_key(agent_token: str) -> str:
    return hashlib.sha256(agent_token.encode("utf-8")).hexdigest()


def _expire_entries(now: float) -> None:
    for key, (created_at, _) in list(_boards_cache.items()):
        if now - created_at >= BOARDS_CACHE_TTL_SECONDS:
            del _boards_cache[key]


def get_boards_cache(agent_token: str) -> list[dict[str, Any]] | None:
    _expire_entries(time.monotonic())
    entry = _boards_cache.get(_cache_key(agent_token))
    return deepcopy(entry[1]) if entry is not None else None


def set_boards_cache(boards: Sequence[dict[str, Any]], *, agent_token: str) -> None:
    now = time.monotonic()
    _expire_entries(now)
    key = _cache_key(agent_token)
    _boards_cache.pop(key, None)
    if len(_boards_cache) >= _MAX_CACHE_ENTRIES:
        _boards_cache.pop(next(iter(_boards_cache)))
    _boards_cache[key] = (now, deepcopy(list(boards)))


def clear_boards_cache(agent_token: str | None = None) -> None:
    if agent_token is None:
        _boards_cache.clear()
    else:
        _boards_cache.pop(_cache_key(agent_token), None)
