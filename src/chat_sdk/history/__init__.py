"""Unified History API: ``chat.history.user`` / ``.thread`` / ``.channel``.

Python port of ``packages/chat/src/history/``.
"""

from __future__ import annotations

from chat_sdk.errors import ChatError
from chat_sdk.history.channel import ChannelHistoryApiImpl
from chat_sdk.history.thread import ThreadHistoryApiImpl, ThreadHistoryCacheLike
from chat_sdk.history.to_prompt import PromptEntry, to_prompt_entries
from chat_sdk.history.user import UserHistoryApiImpl
from chat_sdk.types import (
    AdapterResolver,
    ListThreadsWithMessagesResult,
    ThreadWithMessages,
    UserHistoryApi,
    UserHistoryEntry,
    UserHistoryRole,
)

__all__ = [
    "AdapterResolver",
    "ChannelHistoryApiImpl",
    "HistoryApiImpl",
    "ListThreadsWithMessagesResult",
    "PromptEntry",
    "ThreadHistoryApiImpl",
    "ThreadHistoryCacheLike",
    "ThreadWithMessages",
    "UserHistoryApiImpl",
    "UserHistoryEntry",
    "UserHistoryRole",
    "to_prompt_entries",
]


class HistoryApiImpl:
    """Unified History API implementation.

    Composes :class:`UserHistoryApiImpl`, :class:`ThreadHistoryApiImpl` and
    :class:`ChannelHistoryApiImpl` into the ``HistoryApi`` facade.

    ``adapter_resolver`` maps an adapter name to its instance; ``Chat`` passes
    a closure over its adapter registry so adapters registered later are seen
    at call time. ``cache`` is the optional SDK-side thread history cache
    (enables the cache fallbacks and ``thread.append``). ``user`` is the
    per-user history API (upstream builds it from ``{config, state}``); when
    omitted, accessing ``.user`` raises.
    """

    def __init__(
        self,
        adapter_resolver: AdapterResolver,
        cache: ThreadHistoryCacheLike | None = None,
        user: UserHistoryApi | None = None,
    ) -> None:
        self._user = user
        self._thread = ThreadHistoryApiImpl(adapter_resolver, cache)
        self._channel = ChannelHistoryApiImpl(adapter_resolver, self._thread, cache)

    @property
    def thread(self) -> ThreadHistoryApiImpl:
        return self._thread

    @property
    def channel(self) -> ChannelHistoryApiImpl:
        return self._channel

    @property
    def user(self) -> UserHistoryApi:
        if self._user is None:
            raise ChatError(
                "chat.history.user is not configured — pass `history.user` (or the legacy "
                "`transcripts` + `identity`) to ChatConfig to enable it"
            )
        return self._user
