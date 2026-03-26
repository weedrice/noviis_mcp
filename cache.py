from __future__ import annotations

import time
from collections.abc import Sequence
from copy import deepcopy
from typing import Any

from config import BOARDS_CACHE_TTL_SECONDS

_boards_cache: list[dict[str, Any]] | None = None
_boards_cache_set_at: float | None = None


def get_boards_cache() -> list[dict[str, Any]] | None:
    if _boards_cache is None or _boards_cache_set_at is None:
        return None
    if time.monotonic() - _boards_cache_set_at >= BOARDS_CACHE_TTL_SECONDS:
        clear_boards_cache()
        return None
    return deepcopy(_boards_cache)


def set_boards_cache(boards: Sequence[dict[str, Any]]) -> None:
    global _boards_cache, _boards_cache_set_at
    _boards_cache = deepcopy(list(boards))
    _boards_cache_set_at = time.monotonic()


def clear_boards_cache() -> None:
    global _boards_cache, _boards_cache_set_at
    _boards_cache = None
    _boards_cache_set_at = None
