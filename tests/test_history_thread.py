"""Faithful translation of history/thread.test.ts.

Tests for ThreadHistoryApiImpl (``chat.history.thread``): adapter
delegation, the cache fallback for adapters that persist history in the
SDK-side store, ``collect`` pagination, and ``append``.

TS file: packages/chat/src/history/thread.test.ts
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from chat_sdk.errors import ChatError
from chat_sdk.history import ThreadHistoryApiImpl
from chat_sdk.testing import MockAdapter, create_mock_adapter, create_mock_state, create_test_message
from chat_sdk.thread_history import ThreadHistoryCache
from chat_sdk.types import FetchOptions, FetchResult

NO_THREAD_HISTORY_CACHE_RE = r"no ThreadHistoryCache"
NO_ADAPTER_RE = r"no adapter registered"


def create_persist_adapter(name: str = "telegram") -> MockAdapter:
    adapter = create_mock_adapter(name)
    adapter.persist_thread_history = True
    return adapter


class _Env:
    def __init__(self) -> None:
        self.mock_adapter = create_mock_adapter("slack")
        self.persist_adapter = create_persist_adapter()
        self.cache = ThreadHistoryCache(create_mock_state())
        self.api = ThreadHistoryApiImpl(self.resolver, self.cache)

    def resolver(self, name: str) -> MockAdapter | None:
        if name == "slack":
            return self.mock_adapter
        if name == "telegram":
            return self.persist_adapter
        return None


@pytest.fixture
def env() -> _Env:
    return _Env()


async def _collect_texts(api: ThreadHistoryApiImpl, thread_id: str, limit: int | None = None) -> list[str]:
    return [msg.text async for msg in api.collect(thread_id, limit=limit)]


async def _seed_three(cache: ThreadHistoryCache, thread_id: str) -> None:
    for msg_id, text in (("c1", "one"), ("c2", "two"), ("c3", "three")):
        await cache.append(thread_id, create_test_message(msg_id, text))


class TestThreadHistoryApiImpl:
    # TS: "list delegates to adapter.fetchMessages when messages exist"
    async def test_list_delegates_to_adapterfetchmessages_when_messages_exist(self, env: _Env):
        msg = create_test_message("m1", "hello")
        env.mock_adapter.fetch_messages = AsyncMock(  # type: ignore[method-assign]
            return_value=FetchResult(messages=[msg], next_cursor="cursor-1")
        )
        options = FetchOptions(limit=10)

        result = await env.api.list("slack:C123:1234.5678", options)

        env.mock_adapter.fetch_messages.assert_awaited_once_with("slack:C123:1234.5678", options)
        assert result.messages == [msg]
        assert result.next_cursor == "cursor-1"

    # TS: "list throws for an unregistered adapter prefix"
    async def test_list_throws_for_an_unregistered_adapter_prefix(self, env: _Env):
        with pytest.raises(ChatError, match=NO_ADAPTER_RE):
            await env.api.list("slcak:C123:1234.5678")

    # TS: "list returns the adapter's empty page for non-persisting adapters"
    async def test_list_returns_the_adapters_empty_page_for_nonpersisting_adapters(self, env: _Env):
        await env.cache.append("slack:C123:1234.5678", create_test_message("c1", "cached"))

        result = await env.api.list("slack:C123:1234.5678", FetchOptions(limit=5))

        assert result.messages == []

    # TS: "list falls back to cache for adapters with persistThreadHistory"
    async def test_list_falls_back_to_cache_for_adapters_with_persistthreadhistory(self, env: _Env):
        await env.cache.append("telegram:C123:42", create_test_message("c1", "cached"))

        result = await env.api.list("telegram:C123:42", FetchOptions(limit=5))

        assert [m.text for m in result.messages] == ["cached"]

    # TS: "list does not substitute the cache on a continuation page"
    async def test_list_does_not_substitute_the_cache_on_a_continuation_page(self, env: _Env):
        await env.cache.append("telegram:C123:42", create_test_message("c1", "cached"))

        result = await env.api.list("telegram:C123:42", FetchOptions(limit=5, cursor="page-2"))

        assert result.messages == []
        assert result.next_cursor is None

    # TS: "list cache fallback windows by direction: backward newest-N, forward oldest-N"
    async def test_list_cache_fallback_windows_by_direction_backward_newestn_forward_oldestn(self, env: _Env):
        await _seed_three(env.cache, "telegram:C123:42")

        backward = await env.api.list("telegram:C123:42", FetchOptions(limit=2))
        assert [m.text for m in backward.messages] == ["two", "three"]

        forward = await env.api.list("telegram:C123:42", FetchOptions(limit=2, direction="forward"))
        assert [m.text for m in forward.messages] == ["one", "two"]

    # TS: "collect yields adapter messages when available"
    async def test_collect_yields_adapter_messages_when_available(self, env: _Env):
        env.mock_adapter.fetch_messages = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                FetchResult(messages=[create_test_message("m1", "one")], next_cursor="c2"),
                FetchResult(messages=[create_test_message("m2", "two")]),
            ]
        )

        assert await _collect_texts(env.api, "slack:C123:1234.5678") == ["one", "two"]
        second_options = env.mock_adapter.fetch_messages.await_args_list[1].args[1]
        assert (second_options.direction, second_options.cursor, second_options.limit) == ("forward", "c2", 100)

    # TS: "collect throws for an unregistered adapter prefix"
    async def test_collect_throws_for_an_unregistered_adapter_prefix(self, env: _Env):
        with pytest.raises(ChatError, match=NO_ADAPTER_RE):
            await _collect_texts(env.api, "slcak:C123:1234.5678")

    # TS: "collect stops when a page is empty even if the adapter echoes a cursor"
    async def test_collect_stops_when_a_page_is_empty_even_if_the_adapter_echoes_a_cursor(self, env: _Env):
        pages = [FetchResult(messages=[create_test_message("m1", "one")], next_cursor="c2")]

        async def fetch(thread_id: str, options: FetchOptions | None = None) -> FetchResult:
            return pages.pop(0) if pages else FetchResult(messages=[], next_cursor="c2")

        env.mock_adapter.fetch_messages = AsyncMock(side_effect=fetch)  # type: ignore[method-assign]

        assert await _collect_texts(env.api, "slack:C123:1234.5678") == ["one"]
        assert env.mock_adapter.fetch_messages.await_count == 2

    # TS: "collect falls back to cache for adapters with persistThreadHistory"
    async def test_collect_falls_back_to_cache_for_adapters_with_persistthreadhistory(self, env: _Env):
        await env.cache.append("telegram:C123:42", create_test_message("c1", "from cache"))

        assert await _collect_texts(env.api, "telegram:C123:42") == ["from cache"]

    # TS: "collect does not fall back to cache for non-persisting adapters"
    async def test_collect_does_not_fall_back_to_cache_for_nonpersisting_adapters(self, env: _Env):
        await env.cache.append("slack:C123:1234.5678", create_test_message("c1", "cached"))

        assert await _collect_texts(env.api, "slack:C123:1234.5678") == []

    # TS: "collect cache fallback yields the oldest N, matching the adapter path"
    async def test_collect_cache_fallback_yields_the_oldest_n_matching_the_adapter_path(self, env: _Env):
        await _seed_three(env.cache, "telegram:C123:42")

        assert await _collect_texts(env.api, "telegram:C123:42", limit=2) == ["one", "two"]

    # TS: "append writes to the thread history cache"
    async def test_append_writes_to_the_thread_history_cache(self, env: _Env):
        await env.api.append("slack:C123:1234.5678", create_test_message("m1", "stored"))

        stored = await env.cache.get_messages("slack:C123:1234.5678")
        assert [m.text for m in stored] == ["stored"]

    # TS: "collect returns immediately when limit is 0"
    async def test_collect_returns_immediately_when_limit_is_0(self, env: _Env):
        assert await _collect_texts(env.api, "slack:C123:1234.5678", limit=0) == []
        assert env.mock_adapter._fetch_calls == []

    # TS: "append throws when no cache was provided"
    async def test_append_throws_when_no_cache_was_provided(self, env: _Env):
        no_cache_api = ThreadHistoryApiImpl(env.resolver)

        with pytest.raises(ChatError, match=NO_THREAD_HISTORY_CACHE_RE):
            await no_cache_api.append("slack:C123:1234.5678", create_test_message("m1", "x"))


class TestThreadHistoryApiImplPythonSpecific:
    async def test_collect_pages_by_the_remaining_limit(self, env: _Env):
        # Page size is max(1, min(100, remaining)), so a partial page asks
        # only for what is still needed.
        env.mock_adapter.fetch_messages = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                FetchResult(
                    messages=[create_test_message("m1", "one"), create_test_message("m2", "two")], next_cursor="c2"
                ),
                FetchResult(messages=[create_test_message("m3", "three")], next_cursor="c3"),
            ]
        )

        assert await _collect_texts(env.api, "slack:C123:1234.5678", limit=3) == ["one", "two", "three"]
        limits = [call.args[1].limit for call in env.mock_adapter.fetch_messages.await_args_list]
        assert limits == [3, 1]

    async def test_collect_early_break_leaves_no_fetch_pending(self, env: _Env):
        env.mock_adapter.fetch_messages = AsyncMock(  # type: ignore[method-assign]
            return_value=FetchResult(messages=[create_test_message("m1", "one")], next_cursor="c2")
        )

        async for _ in env.api.collect("slack:C123:1234.5678"):
            break

        assert env.mock_adapter.fetch_messages.await_count == 1
