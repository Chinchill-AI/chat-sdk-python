"""Per-thread message history.

Python port of ``history/thread.ts``.
"""

from __future__ import annotations

import builtins
from collections.abc import AsyncIterator
from typing import Protocol

from chat_sdk.errors import ChatError
from chat_sdk.history.resolve_adapter import persists_history, require_adapter
from chat_sdk.types import AdapterResolver, FetchOptions, FetchResult, Message


class ThreadHistoryCacheLike(Protocol):
    """Minimal shape of the thread history cache this module needs."""

    async def append(self, thread_id: str, message: Message) -> None: ...

    async def get_messages(self, thread_id: str, limit: int | None = None) -> list[Message]: ...


class ThreadHistoryApiImpl:
    """Per-thread message history implementation.

    ``list()`` delegates to ``adapter.fetch_messages``. ``collect()`` is an
    async generator that paginates through every thread message, and
    ``append()`` writes to the SDK-side cache (for adapters with
    ``persist_thread_history``).

    Adapter resolution: the adapter name is the thread ID prefix
    (``{adapter}:{channel}:{thread}``). An unknown prefix raises rather than
    returning an empty result.

    Cache fallback: reserved for adapters whose history lives in the SDK-side
    store (``persist_thread_history`` / legacy ``persist_message_history``).
    For every other adapter the platform response is authoritative — an
    empty page is a real empty page, not a cue to substitute cached data.
    """

    def __init__(self, get_adapter: AdapterResolver, cache: ThreadHistoryCacheLike | None = None) -> None:
        self._get_adapter = get_adapter
        self._cache = cache

    async def list(self, thread_id: str, options: FetchOptions | None = None) -> FetchResult:
        """Fetch a single page of messages from a thread.

        Uses ``adapter.fetch_messages``, falling back to the SDK-side cache
        only for adapters that persist history there — and never on a
        continuation page (an empty page mid-pagination means the thread is
        exhausted).
        """
        adapter = require_adapter(self._get_adapter, thread_id, "history.thread")
        result = await adapter.fetch_messages(thread_id, options)

        if (
            len(result.messages) == 0
            and result.next_cursor is None
            and (options is None or options.cursor is None)
            and self._cache is not None
            and persists_history(adapter)
        ):
            messages = await self._read_cached_window(thread_id, options)
            return FetchResult(messages=messages, next_cursor=None)

        return result

    async def collect(self, thread_id: str, *, limit: int | None = None) -> AsyncIterator[Message]:
        """Yield every message in the thread in chronological order, handling
        pagination automatically. With ``limit``, yields the oldest N on both
        the adapter and cache paths.

        Falls back to the SDK-side cache for adapters that persist history
        there (e.g. Telegram, WhatsApp). Each page is fetched only when the
        previous one is exhausted, so breaking out early leaves no fetch
        pending.
        """
        adapter = require_adapter(self._get_adapter, thread_id, "history.thread")
        collected = 0

        if limit == 0:
            return

        cursor: str | None = None
        yielded_any = False
        while True:
            remaining = limit - collected if limit is not None else None
            if remaining is not None and remaining <= 0:
                return
            fetch_limit = max(1, min(100, remaining)) if remaining is not None else 100
            result = await adapter.fetch_messages(
                thread_id,
                FetchOptions(direction="forward", cursor=cursor, limit=fetch_limit),
            )
            for message in result.messages:
                yielded_any = True
                yield message
                collected += 1
                if limit is not None and collected >= limit:
                    return
            # Same guard as ThreadImpl.all_messages: an empty page ends
            # pagination even when the adapter echoes a cursor back, so a
            # misbehaving adapter cannot send us into an unbounded fetch loop.
            if not result.next_cursor or len(result.messages) == 0:
                break
            cursor = result.next_cursor

        if yielded_any or self._cache is None or not persists_history(adapter):
            return

        # Cache fallback for adapters whose history lives in the SDK-side
        # store. Slice from the front so a ``limit`` means "oldest N",
        # matching the adapter path above.
        all_messages = await self._cache.get_messages(thread_id)
        messages = all_messages[:limit] if limit is not None else all_messages
        for message in messages:
            yield message

    async def append(self, thread_id: str, message: Message) -> None:
        """Append a message to the SDK-side per-thread history cache.

        Only available when a cache was passed at construction time. Used by
        adapters that set ``persist_thread_history``.
        """
        if self._cache is None:
            raise ChatError("history.thread.append: no ThreadHistoryCache was provided at construction")
        await self._cache.append(thread_id, message)

    async def _read_cached_window(self, thread_id: str, options: FetchOptions | None) -> builtins.list[Message]:
        """Read a ``list()``-shaped window from the cache: ``direction="forward"``
        yields the oldest N, the default backward direction the newest N —
        the same windows the adapter path serves."""
        if self._cache is None:
            return []
        limit = options.limit if options is not None else None
        if options is not None and options.direction == "forward":
            all_messages = await self._cache.get_messages(thread_id)
            return all_messages[:limit] if limit is not None else all_messages
        return await self._cache.get_messages(thread_id, limit)
