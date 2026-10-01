"""Channel-level history.

Python port of ``history/channel.ts``.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

from chat_sdk.errors import ChatError, ChatNotImplementedError
from chat_sdk.history.resolve_adapter import optional_method, persists_history, require_adapter
from chat_sdk.history.thread import ThreadHistoryCacheLike
from chat_sdk.types import (
    AdapterResolver,
    FetchOptions,
    FetchResult,
    ListThreadsOptions,
    ListThreadsResult,
    ListThreadsWithMessagesResult,
    ThreadSummary,
    ThreadWithMessages,
)

DEFAULT_MAX_THREADS = 5

# Upper bound on concurrent per-thread fetches in list_threads_with_messages.
THREAD_FETCH_CONCURRENCY = 4


class _ThreadLister(Protocol):
    async def list(self, thread_id: str, options: FetchOptions | None = None) -> FetchResult: ...


class ChannelHistoryApiImpl:
    """Channel-level history implementation.

    ``list_messages()`` and ``list_threads()`` delegate to the matching
    adapter methods; ``list_threads_with_messages()`` lists threads together
    with a page of messages each.

    Adapter resolution: the adapter name is the channel ID prefix
    (``{adapter}:{channel}``).

    Capability rule: a method is absent when the attribute is ``None`` or is
    the unoverridden :class:`~chat_sdk.types.BaseAdapter` stub. A call that
    still raises :class:`~chat_sdk.errors.ChatNotImplementedError` is
    re-raised as the same capability error (no cache retry).
    """

    def __init__(
        self,
        get_adapter: AdapterResolver,
        thread_history: _ThreadLister,
        cache: ThreadHistoryCacheLike | None = None,
    ) -> None:
        self._get_adapter = get_adapter
        self._thread_history = thread_history
        self._cache = cache

    async def list_messages(self, channel_id: str, options: FetchOptions | None = None) -> FetchResult:
        """Fetch top-level messages in a channel (not thread replies).

        Uses ``adapter.fetch_channel_messages`` when available. Adapters that
        persist history in the SDK-side store are served from the
        channel-keyed cache instead (``Chat`` appends inbound messages under
        the channel ID for exactly this purpose). Every other adapter raises
        a capability error rather than guessing.
        """
        adapter = require_adapter(self._get_adapter, channel_id, "history.channel")
        unsupported = (
            f'history.channel.listMessages: adapter "{adapter.name}" does not support fetching channel messages'
        )

        fetch = optional_method(adapter, "fetch_channel_messages")
        if fetch is not None:
            try:
                return await fetch(channel_id, options)
            except ChatNotImplementedError as exc:
                raise ChatError(unsupported) from exc

        if self._cache is not None and persists_history(adapter):
            all_messages = await self._cache.get_messages(channel_id)
            limit = options.limit if options is not None else None
            messages = all_messages
            if limit is not None:
                if options is not None and options.direction == "forward":
                    messages = all_messages[:limit]
                else:
                    messages = all_messages[max(0, len(all_messages) - limit) :]
            return FetchResult(messages=messages, next_cursor=None)

        raise ChatError(unsupported)

    async def list_threads(self, channel_id: str, options: ListThreadsOptions | None = None) -> ListThreadsResult:
        """List threads in a channel via ``adapter.list_threads``.

        Raises when the adapter does not implement ``list_threads``.
        """
        adapter = require_adapter(self._get_adapter, channel_id, "history.channel")
        unsupported = f'history.channel.listThreads: adapter "{adapter.name}" does not implement listThreads'

        list_threads = optional_method(adapter, "list_threads")
        if list_threads is None:
            raise ChatError(unsupported)
        try:
            # Keyword form: adapters with a ``**kwargs`` signature (e.g. the
            # mock adapter) reject a second positional argument.
            return await list_threads(channel_id, options=options)
        except ChatNotImplementedError as exc:
            raise ChatError(unsupported) from exc

    async def list_threads_with_messages(
        self,
        channel_id: str,
        *,
        max_threads: int | None = None,
        messages_per_thread: int | None = None,
        cursor: str | None = None,
    ) -> ListThreadsWithMessagesResult:
        """List threads and fetch a page of messages for each.

        Fetches up to ``max_threads`` (default 5) threads, then retrieves
        ``messages_per_thread`` messages for each through
        ``history.thread.list`` (so per-thread reads share its cache-fallback
        semantics), at most :data:`THREAD_FETCH_CONCURRENCY` threads at a
        time to stay inside platform rate limits. Order is preserved.
        """
        resolved_max = max_threads if max_threads is not None else DEFAULT_MAX_THREADS
        threads_result = await self.list_threads(channel_id, ListThreadsOptions(cursor=cursor, limit=resolved_max))

        async def fetch_one(summary: ThreadSummary) -> ThreadWithMessages:
            result = await self._thread_history.list(
                summary.id,
                FetchOptions(limit=messages_per_thread) if messages_per_thread is not None else None,
            )
            return ThreadWithMessages(thread_id=summary.id, messages=result.messages)

        summaries = threads_result.threads
        threads: list[ThreadWithMessages] = []
        for i in range(0, len(summaries), THREAD_FETCH_CONCURRENCY):
            batch = summaries[i : i + THREAD_FETCH_CONCURRENCY]
            # Upstream parity (`Promise.all` per batch, history/channel.ts):
            # the first failure propagates and the rest of the batch is not
            # cancelled; gather still retrieves their exceptions.
            threads.extend(await asyncio.gather(*(fetch_one(summary) for summary in batch)))

        return ListThreadsWithMessagesResult(threads=threads, next_cursor=threads_result.next_cursor)
