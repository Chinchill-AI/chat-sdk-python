"""Tests for ThreadHistoryCache: append, get, TTL, and limit behavior.

Port of thread-history related tests from the Vercel Chat SDK
(thread-history.test.ts, renamed upstream from message-history.test.ts).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from chat_sdk.thread_history import (
    DEFAULT_MAX_MESSAGES,
    DEFAULT_TTL_MS,
    KEY_PREFIX,
    ThreadHistoryCache,
    ThreadHistoryConfig,
)
from chat_sdk.types import Attachment, Author, Message, MessageMetadata

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_message(
    text: str = "Hello",
    *,
    msg_id: str = "msg-1",
    thread_id: str = "thread-1",
    user_id: str = "U123",
) -> Message:
    return Message(
        id=msg_id,
        thread_id=thread_id,
        text=text,
        formatted={"type": "root", "children": []},
        author=Author(
            full_name="Test User",
            is_bot=False,
            is_me=False,
            user_id=user_id,
            user_name="testuser",
        ),
        metadata=MessageMetadata(date_sent=datetime(2024, 1, 1)),
    )


def _make_mock_state() -> MagicMock:
    """Mock state that actually stores appended lists in memory."""
    lists: dict[str, list[Any]] = {}
    store: dict[str, Any] = {}

    state = MagicMock()

    async def _append_to_list(
        key: str, value: Any, *, max_length: int | None = None, ttl_ms: int | None = None
    ) -> None:
        if key not in lists:
            lists[key] = []
        lists[key].append(value)
        if max_length and len(lists[key]) > max_length:
            lists[key] = lists[key][-max_length:]

    async def _get_list(key: str) -> list[Any]:
        return lists.get(key, [])

    async def _get(key: str) -> Any | None:
        return store.get(key)

    async def _set(key: str, value: Any, *a: Any, **kw: Any) -> None:
        store[key] = value

    state.append_to_list = AsyncMock(side_effect=_append_to_list)
    state.get_list = AsyncMock(side_effect=_get_list)
    state.get = AsyncMock(side_effect=_get)
    state.set = AsyncMock(side_effect=_set)
    state.delete = AsyncMock()
    state._lists = lists
    state._store = store

    return state


# ---------------------------------------------------------------------------
# Construction / defaults
# ---------------------------------------------------------------------------


class TestThreadHistoryConstruction:
    def test_default_config(self):
        state = _make_mock_state()
        cache = ThreadHistoryCache(state)
        assert cache._max_messages == DEFAULT_MAX_MESSAGES
        assert cache._ttl_ms == DEFAULT_TTL_MS

    def test_custom_config(self):
        state = _make_mock_state()
        config = ThreadHistoryConfig(max_messages=50, ttl_ms=3600000)
        cache = ThreadHistoryCache(state, config)
        assert cache._max_messages == 50
        assert cache._ttl_ms == 3600000


# ---------------------------------------------------------------------------
# Append and get
# ---------------------------------------------------------------------------


class TestAppendAndGet:
    # TS: "should use appendToList for atomic appends"
    @pytest.mark.asyncio
    async def test_should_use_appendtolist_for_atomic_appends(self):
        state = _make_mock_state()
        cache = ThreadHistoryCache(state)

        msg = _make_message("Hello world", msg_id="m1")
        await cache.append("thread-1", msg)

        state.append_to_list.assert_called_once()
        args, kwargs = state.append_to_list.call_args
        assert args[0] == f"{KEY_PREFIX}thread-1"
        assert args[1]["id"] == "m1"
        assert kwargs == {"max_length": 100, "ttl_ms": 7 * 24 * 60 * 60 * 1000}

    # TS: "should return empty array for unknown thread"
    @pytest.mark.asyncio
    async def test_should_return_empty_array_for_unknown_thread(self):
        state = _make_mock_state()
        cache = ThreadHistoryCache(state)

        messages = await cache.get_messages("nonexistent-thread")
        assert messages == []

    # TS: "should append and retrieve messages"
    @pytest.mark.asyncio
    async def test_should_append_and_retrieve_messages(self):
        state = _make_mock_state()
        cache = ThreadHistoryCache(state)

        msg1 = _make_message("First", msg_id="m1")
        msg2 = _make_message("Second", msg_id="m2")
        msg3 = _make_message("Third", msg_id="m3")

        await cache.append("thread-1", msg1)
        await cache.append("thread-1", msg2)
        await cache.append("thread-1", msg3)

        messages = await cache.get_messages("thread-1")
        assert len(messages) == 3
        assert messages[0].text == "First"
        assert messages[1].text == "Second"
        assert messages[2].text == "Third"

    # TS: "should strip raw field on storage"
    @pytest.mark.asyncio
    async def test_should_strip_raw_field_on_storage(self):
        state = _make_mock_state()
        cache = ThreadHistoryCache(state)

        msg = _make_message("With raw")
        msg.raw = {"some": "data"}
        await cache.append("thread-1", msg)

        # The serialized value should have raw set to None
        call_args = state.append_to_list.call_args[0]
        serialized = call_args[1]
        assert serialized.get("raw") is None


# ---------------------------------------------------------------------------
# Limit / slicing
# ---------------------------------------------------------------------------


class TestGetMessagesLimit:
    # TS: "should support limit parameter in getMessages"
    @pytest.mark.asyncio
    async def test_should_support_limit_parameter_in_getmessages(self):
        state = _make_mock_state()
        cache = ThreadHistoryCache(state)

        for i in range(5):
            await cache.append("thread-1", _make_message(f"msg-{i}", msg_id=f"m{i}"))

        messages = await cache.get_messages("thread-1", limit=2)
        assert len(messages) == 2
        assert messages[0].text == "msg-3"
        assert messages[1].text == "msg-4"

    @pytest.mark.asyncio
    async def test_limit_larger_than_stored(self):
        state = _make_mock_state()
        cache = ThreadHistoryCache(state)

        await cache.append("thread-1", _make_message("only one"))
        messages = await cache.get_messages("thread-1", limit=100)
        assert len(messages) == 1

    @pytest.mark.asyncio
    async def test_limit_none_returns_all(self):
        state = _make_mock_state()
        cache = ThreadHistoryCache(state)

        for i in range(10):
            await cache.append("thread-1", _make_message(f"msg-{i}", msg_id=f"m{i}"))

        messages = await cache.get_messages("thread-1")
        assert len(messages) == 10


# ---------------------------------------------------------------------------
# Max messages trimming
# ---------------------------------------------------------------------------


class TestMaxMessagesTrimming:
    # TS: "should trim to maxMessages, keeping newest"
    @pytest.mark.asyncio
    async def test_should_trim_to_maxmessages_keeping_newest(self):
        state = _make_mock_state()
        config = ThreadHistoryConfig(max_messages=3)
        cache = ThreadHistoryCache(state, config)

        for i in range(5):
            await cache.append("thread-1", _make_message(f"msg-{i}", msg_id=f"m{i}"))

        # append_to_list should have been called with max_length=3
        for call in state.append_to_list.call_args_list:
            assert call[1].get("max_length") == 3

        # After trimming, only last 3 messages should remain
        messages = await cache.get_messages("thread-1")
        assert len(messages) == 3
        assert messages[0].text == "msg-2"
        assert messages[1].text == "msg-3"
        assert messages[2].text == "msg-4"


# ---------------------------------------------------------------------------
# TTL propagation
# ---------------------------------------------------------------------------


class TestTTLPropagation:
    @pytest.mark.asyncio
    async def test_passes_ttl_to_state(self):
        state = _make_mock_state()
        config = ThreadHistoryConfig(ttl_ms=86400000)
        cache = ThreadHistoryCache(state, config)

        await cache.append("thread-1", _make_message("test"))

        call_kwargs = state.append_to_list.call_args[1]
        assert call_kwargs.get("ttl_ms") == 86400000


# ---------------------------------------------------------------------------
# Thread isolation
# ---------------------------------------------------------------------------


class TestThreadIsolation:
    # TS: "should keep threads isolated"
    @pytest.mark.asyncio
    async def test_should_keep_threads_isolated(self):
        state = _make_mock_state()
        cache = ThreadHistoryCache(state)

        await cache.append("thread-A", _make_message("msg A", msg_id="mA"))
        await cache.append("thread-B", _make_message("msg B", msg_id="mB"))

        messages_a = await cache.get_messages("thread-A")
        messages_b = await cache.get_messages("thread-B")

        assert len(messages_a) == 1
        assert messages_a[0].text == "msg A"
        assert len(messages_b) == 1
        assert messages_b[0].text == "msg B"


# ---------------------------------------------------------------------------
# Key prefix
# ---------------------------------------------------------------------------


class TestKeyPrefix:
    @pytest.mark.asyncio
    async def test_uses_correct_key_prefix(self):
        state = _make_mock_state()
        cache = ThreadHistoryCache(state)

        await cache.append("my-thread", _make_message("test"))

        key = state.append_to_list.call_args[0][0]
        assert key == "msg-history:my-thread"


# ---------------------------------------------------------------------------
# Message serialization roundtrip
# ---------------------------------------------------------------------------


class TestMessageSerializationRoundtrip:
    @pytest.mark.asyncio
    async def test_message_survives_roundtrip(self):
        state = _make_mock_state()
        cache = ThreadHistoryCache(state)

        original = _make_message("Round trip test", msg_id="m-rt", thread_id="t-rt")
        original.attachments = [
            Attachment(type="image", url="https://example.com/img.png", name="img.png"),
        ]
        await cache.append("t-rt", original)

        messages = await cache.get_messages("t-rt")
        assert len(messages) == 1
        restored = messages[0]
        assert restored.id == "m-rt"
        assert restored.text == "Round trip test"
        assert restored.author.user_id == "U123"
        assert len(restored.attachments) == 1
        assert restored.attachments[0].type == "image"
        assert restored.attachments[0].name == "img.png"


# ---------------------------------------------------------------------------
# MessageHistoryCache (deprecated alias)
# ---------------------------------------------------------------------------


class TestMessageHistoryCacheDeprecatedAlias:
    # TS: "re-exports ThreadHistoryCache under the old name"
    @pytest.mark.asyncio
    async def test_reexports_threadhistorycache_under_the_old_name(self):
        from chat_sdk.message_history import (
            KEY_PREFIX as OLD_KEY_PREFIX,
        )
        from chat_sdk.message_history import (
            MessageHistoryCache,
            MessageHistoryConfig,
        )

        assert MessageHistoryCache is ThreadHistoryCache
        assert MessageHistoryConfig is ThreadHistoryConfig
        # The storage key prefix is deliberately unchanged — renaming it
        # would silently orphan persisted data.
        assert OLD_KEY_PREFIX == "msg-history:"

        state = _make_mock_state()
        cache = MessageHistoryCache(state)
        await cache.append("t-1", _make_message("hello", msg_id="m1"))
        msgs = await cache.get_messages("t-1")
        assert len(msgs) == 1
        assert msgs[0].text == "hello"
