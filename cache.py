from __future__ import annotations

import hashlib
import time
from collections.abc import Awaitable, Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

import anyio

from config import BOARDS_CACHE_TTL_SECONDS

# Bound the number of cached identities and avoid retaining raw bearer tokens as keys.
_MAX_CACHE_ENTRIES = 256
_boards_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


@dataclass(eq=False)
class BoardsLoadTicket:
    key: str
    valid: bool = True
    done: anyio.Event = field(default_factory=anyio.Event)
    result: list[dict[str, Any]] | None = None
    error: Exception | None = None


# Only outstanding loads retain tickets; no unbounded per-identity epoch history.
_pending_loads: dict[str, set[BoardsLoadTicket]] = {}
_inflight: dict[str, BoardsLoadTicket] = {}


async def load_boards_cached(
    agent_token: str, loader: Callable[[], Awaitable[list[dict[str, Any]]]], *, force_refresh: bool = False,
) -> list[dict[str, Any]]:
    """Share ordinary misses without creating tasks that outlive their HTTP client.

    The owner uses its own request deadline. If it is cancelled, waiters retry
    with their own client/deadline. Cancelling a waiter never cancels the owner.
    Refresh/invalidation detach old loads so later callers cannot join them.
    """
    if force_refresh:
        clear_boards_cache(agent_token)
    key = _cache_key(agent_token)
    while True:
        cached = get_boards_cache(agent_token)
        if cached is not None:
            return cached
        ticket = _inflight.get(key)
        if ticket is not None:
            await ticket.done.wait()
            if ticket.error is not None:
                raise ticket.error
            if ticket.result is not None:
                return deepcopy(ticket.result)
            # Cancelled owners publish no result. Elect another waiting request.
            continue
        ticket = begin_boards_load(agent_token)
        _inflight[key] = ticket
        try:
            boards = await loader()
            set_boards_cache(boards, agent_token=agent_token, load_ticket=ticket)
            ticket.result = deepcopy(boards)
            return boards
        except Exception as exc:
            ticket.error = exc
            raise
        finally:
            if _inflight.get(key) is ticket:
                del _inflight[key]
            finish_boards_load(ticket)
            ticket.done.set()


def begin_boards_load(agent_token: str) -> BoardsLoadTicket:
    ticket = BoardsLoadTicket(_cache_key(agent_token))
    _pending_loads.setdefault(ticket.key, set()).add(ticket)
    return ticket


def finish_boards_load(ticket: BoardsLoadTicket) -> None:
    pending = _pending_loads.get(ticket.key)
    if pending is not None:
        pending.discard(ticket)
        if not pending:
            _pending_loads.pop(ticket.key, None)
    ticket.valid = False


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


def set_boards_cache(boards: Sequence[dict[str, Any]], *, agent_token: str,
                     load_ticket: BoardsLoadTicket | None = None) -> None:
    key = _cache_key(agent_token)
    if load_ticket is not None and (not load_ticket.valid or load_ticket.key != key):
        return
    now = time.monotonic()
    _expire_entries(now)
    _boards_cache.pop(key, None)
    if len(_boards_cache) >= _MAX_CACHE_ENTRIES:
        _boards_cache.pop(next(iter(_boards_cache)))
    _boards_cache[key] = (now, deepcopy(list(boards)))


def clear_boards_cache(agent_token: str | None = None) -> None:
    if agent_token is None:
        _boards_cache.clear()
        _inflight.clear()
        pending = [ticket for tickets in _pending_loads.values() for ticket in tickets]
        _pending_loads.clear()
    else:
        key = _cache_key(agent_token)
        _boards_cache.pop(key, None)
        _inflight.pop(key, None)
        pending = _pending_loads.pop(key, set())
    for ticket in pending:
        ticket.valid = False
