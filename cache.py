from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from typing import Any


_boards_cache: list[dict[str, Any]] | None = None


def get_boards_cache() -> list[dict[str, Any]] | None:
    if _boards_cache is None:
        return None
    return deepcopy(_boards_cache)


def set_boards_cache(boards: Sequence[dict[str, Any]]) -> None:
    global _boards_cache
    _boards_cache = deepcopy(list(boards))


def clear_boards_cache() -> None:
    global _boards_cache
    _boards_cache = None
