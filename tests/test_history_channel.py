"""Faithful translation of history/channel.test.ts.

Tests for ChannelHistoryApiImpl (``chat.history.channel``): channel message
reads (adapter, channel-keyed cache, capability error), thread listing, and
``list_threads_with_messages`` with bounded per-thread concurrency.

TS file: packages/chat/src/history/channel.test.ts
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock

import pytest

from chat_sdk.errors import ChatError, ChatNotImplementedError
from chat_sdk.history import ChannelHistoryApiImpl, ThreadHistoryApiImpl
from chat_sdk.testing import create_mock_adapter, create_mock_state, create_test_message
from chat_sdk.thread_history import ThreadHistoryCache
from chat_sdk.types import (
    BaseAdapter,
    FetchOptions,
    FetchResult,
    ListThreadsOptions,
    ListThreadsResult,
    ThreadSummary,
)

LIST_THREADS_UNSUPPORTED_RE = r"does not implement listThreads"
CHANNEL_MESSAGES_UNSUPPORTED_RE = r"does not support fetching channel messages"
NO_ADAPTER_RE = r"no adapter registered"


class _Env:
    def __init__(self) -> None:
        self.mock_adapter = create_mock_adapter("slack")
        self.cache = ThreadHistoryCache(create_mock_state())
        self.api = self.build_api(lambda name: self.mock_adapter if name == "slack" else None)

    def build_api(self, resolver: Callable[[str], Any]) -> ChannelHistoryApiImpl:
        return ChannelHistoryApiImpl(resolver, ThreadHistoryApiImpl(resolver, self.cache), self.cache)


@pytest.fixture
def env() -> _Env:
    return _Env()


async def _seed_three(cache: ThreadHistoryCache, channel_id: str) -> None:
    for msg_id, text in (("c1", "one"), ("c2", "two"), ("c3", "three")):
        await cache.append(channel_id, create_test_message(msg_id, text))


class TestChannelHistoryApiImpl:
    # TS: "listMessages uses fetchChannelMessages when available"
    async def test_listmessages_uses_fetchchannelmessages_when_available(self, env: _Env):
        msg = create_test_message("m1", "channel msg")
        env.mock_adapter.fetch_channel_messages = AsyncMock(  # type: ignore[method-assign]
            return_value=FetchResult(messages=[msg], next_cursor="next")
        )
        options = FetchOptions(limit=10)

        result = await env.api.list_messages("slack:C123", options)

        env.mock_adapter.fetch_channel_messages.assert_awaited_once_with("slack:C123", options)
        assert result.messages == [msg]

    # TS: "listMessages throws when fetchChannelMessages is absent"
    async def test_listmessages_throws_when_fetchchannelmessages_is_absent(self, env: _Env):
        env.mock_adapter.fetch_channel_messages = None  # type: ignore[method-assign,assignment]

        with pytest.raises(ChatError, match=CHANNEL_MESSAGES_UNSUPPORTED_RE):
            await env.api.list_messages("slack:C123")
        assert env.mock_adapter._fetch_calls == []

    # TS: "listMessages throws for an unregistered adapter prefix"
    async def test_listmessages_throws_for_an_unregistered_adapter_prefix(self, env: _Env):
        with pytest.raises(ChatError, match=NO_ADAPTER_RE):
            await env.api.list_messages("github:owner/repo")

    # TS: "listMessages serves persisting adapters from the channel-keyed cache"
    async def test_listmessages_serves_persisting_adapters_from_the_channelkeyed_cache(self, env: _Env):
        persist_adapter = create_mock_adapter("whatsapp")
        persist_adapter.persist_thread_history = True
        persist_adapter.fetch_channel_messages = None  # type: ignore[method-assign,assignment]
        persist_api = env.build_api(lambda name: persist_adapter if name == "whatsapp" else None)
        await _seed_three(env.cache, "whatsapp:123")

        backward = await persist_api.list_messages("whatsapp:123", FetchOptions(limit=2))
        assert [m.text for m in backward.messages] == ["two", "three"]

        forward = await persist_api.list_messages("whatsapp:123", FetchOptions(limit=2, direction="forward"))
        assert [m.text for m in forward.messages] == ["one", "two"]

    # TS: "listThreads delegates to adapter.listThreads"
    async def test_listthreads_delegates_to_adapterlistthreads(self, env: _Env):
        root = create_test_message("root", "thread root")
        env.mock_adapter.list_threads = AsyncMock(  # type: ignore[method-assign]
            return_value=ListThreadsResult(
                threads=[ThreadSummary(id="slack:C123:1111.2222", root_message=root, reply_count=3)],
                next_cursor="t-cursor",
            )
        )
        options = ListThreadsOptions(limit=5)

        result = await env.api.list_threads("slack:C123", options)

        env.mock_adapter.list_threads.assert_awaited_once_with("slack:C123", options=options)
        assert [t.id for t in result.threads] == ["slack:C123:1111.2222"]
        assert result.next_cursor == "t-cursor"

    # TS: "listThreads throws when adapter does not implement listThreads"
    async def test_listthreads_throws_when_adapter_does_not_implement_listthreads(self, env: _Env):
        env.mock_adapter.list_threads = None  # type: ignore[method-assign,assignment]

        with pytest.raises(ChatError, match=LIST_THREADS_UNSUPPORTED_RE):
            await env.api.list_threads("slack:C123")

    # TS: "listThreadsWithMessages fetches messages for each thread"
    async def test_listthreadswithmessages_fetches_messages_for_each_thread(self, env: _Env):
        root = create_test_message("root", "root text")
        env.mock_adapter.list_threads = AsyncMock(  # type: ignore[method-assign]
            return_value=ListThreadsResult(
                threads=[
                    ThreadSummary(id="slack:C123:1111.2222", root_message=root),
                    ThreadSummary(id="slack:C123:3333.4444", root_message=root),
                ]
            )
        )

        async def fetch(thread_id: str, options: FetchOptions | None = None) -> FetchResult:
            return FetchResult(messages=[create_test_message(f"{thread_id}-r", f"reply for {thread_id}")])

        env.mock_adapter.fetch_messages = AsyncMock(side_effect=fetch)  # type: ignore[method-assign]

        result = await env.api.list_threads_with_messages("slack:C123", max_threads=2, messages_per_thread=1)

        assert [t.thread_id for t in result.threads] == ["slack:C123:1111.2222", "slack:C123:3333.4444"]
        assert "1111.2222" in result.threads[0].messages[0].text
        assert env.mock_adapter.list_threads.await_args.kwargs["options"].limit == 2
        assert env.mock_adapter.fetch_messages.await_args_list[0].args[1].limit == 1

    # TS: "listThreadsWithMessages bounds per-thread fetch concurrency"
    async def test_listthreadswithmessages_bounds_perthread_fetch_concurrency(self, env: _Env):
        root = create_test_message("root", "root text")
        env.mock_adapter.list_threads = AsyncMock(  # type: ignore[method-assign]
            return_value=ListThreadsResult(
                threads=[ThreadSummary(id=f"slack:C123:{i}.0", root_message=root) for i in range(10)]
            )
        )
        in_flight = 0
        max_in_flight = 0

        async def fetch(thread_id: str, options: FetchOptions | None = None) -> FetchResult:
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            await asyncio.sleep(0.001)
            in_flight -= 1
            return FetchResult(messages=[create_test_message(f"{thread_id}-r", "reply")])

        env.mock_adapter.fetch_messages = AsyncMock(side_effect=fetch)  # type: ignore[method-assign]

        result = await env.api.list_threads_with_messages("slack:C123", max_threads=10)

        assert len(result.threads) == 10
        # Order is preserved across batches.
        assert [t.thread_id for t in result.threads] == [f"slack:C123:{i}.0" for i in range(10)]
        # Bounded, and actually concurrent within a batch.
        assert max_in_flight == 4


class _StubOnlyAdapter(BaseAdapter):
    """A ``BaseAdapter`` subclass that does not override the optional
    ``fetch_channel_messages`` / ``list_threads`` stubs."""

    def __init__(self, name: str, *, persist: bool) -> None:
        self._name = name
        self._persist = persist
        self.fetch_messages = AsyncMock(return_value=FetchResult())

    @property
    def name(self) -> str:
        return self._name

    @property
    def persist_thread_history(self) -> bool | None:
        return self._persist


class TestChannelHistoryApiImplCapabilityRule:
    """Python-specific: an unoverridden ``BaseAdapter`` stub counts as absent
    (upstream tests ``if (adapter.fetchChannelMessages)``)."""

    async def test_unoverridden_stub_on_a_persisting_adapter_falls_back_to_the_channel_cache(self, env: _Env):
        adapter = _StubOnlyAdapter("whatsapp", persist=True)
        api = env.build_api(lambda name: adapter if name == "whatsapp" else None)
        await _seed_three(env.cache, "whatsapp:123")

        result = await api.list_messages("whatsapp:123")

        assert [m.text for m in result.messages] == ["one", "two", "three"]

    async def test_unoverridden_stubs_on_a_non_persisting_adapter_raise_capability_errors(self, env: _Env):
        adapter = _StubOnlyAdapter("acme", persist=False)
        api = env.build_api(lambda name: adapter if name == "acme" else None)

        with pytest.raises(ChatError, match=CHANNEL_MESSAGES_UNSUPPORTED_RE) as messages_exc:
            await api.list_messages("acme:C1")
        with pytest.raises(ChatError, match=LIST_THREADS_UNSUPPORTED_RE) as threads_exc:
            await api.list_threads("acme:C1")
        # Absent methods are never called, so there is no chained cause.
        assert messages_exc.value.__cause__ is None
        assert threads_exc.value.__cause__ is None

    async def test_not_implemented_from_an_override_is_a_capability_error_without_cache_retry(self, env: _Env):
        adapter = create_mock_adapter("whatsapp")
        adapter.persist_thread_history = True
        adapter.fetch_channel_messages = AsyncMock(  # type: ignore[method-assign]
            side_effect=ChatNotImplementedError("whatsapp", "fetchChannelMessages")
        )
        api = env.build_api(lambda name: adapter if name == "whatsapp" else None)
        await _seed_three(env.cache, "whatsapp:123")

        with pytest.raises(ChatError, match=CHANNEL_MESSAGES_UNSUPPORTED_RE) as exc_info:
            await api.list_messages("whatsapp:123")
        assert isinstance(exc_info.value.__cause__, ChatNotImplementedError)
