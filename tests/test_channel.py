"""Tests for ChannelImpl.

Covers: construction, post message, message iteration, thread listing,
metadata, state, and serialization.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest
from chat_sdk.channel import ChannelImpl, _ChannelImplConfigWithAdapter
from chat_sdk.testing import (
    MockAdapter,
    MockStateAdapter,
    create_mock_adapter,
    create_mock_state,
    create_test_message,
)
from chat_sdk.types import (
    ChannelInfo,
    FetchResult,
    ListThreadsResult,
    Message,
    PostableMarkdown,
    PostableRaw,
    RawMessage,
    ThreadSummary,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_channel(
    adapter: MockAdapter | None = None,
    state: MockStateAdapter | None = None,
    *,
    channel_id: str = "slack:C123",
    is_dm: bool = False,
    channel_visibility: str = "unknown",
) -> ChannelImpl:
    adapter = adapter or create_mock_adapter()
    state = state or create_mock_state()
    return ChannelImpl(
        _ChannelImplConfigWithAdapter(
            id=channel_id,
            adapter=adapter,
            state_adapter=state,
            is_dm=is_dm,
            channel_visibility=channel_visibility,
        )
    )


# ============================================================================
# Basic properties
# ============================================================================


class TestChannelBasicProperties:
    """Tests for basic ChannelImpl construction and properties."""

    def test_correct_id_and_adapter(self, mock_adapter, mock_state):
        channel = _make_channel(mock_adapter, mock_state)
        assert channel.id == "slack:C123"
        assert channel.adapter is mock_adapter
        assert channel.is_dm is False
        assert channel.name is None

    def test_is_dm_when_configured(self, mock_adapter, mock_state):
        channel = _make_channel(mock_adapter, mock_state, is_dm=True, channel_id="slack:D123")
        assert channel.is_dm is True

    def test_channel_visibility(self, mock_adapter, mock_state):
        channel = _make_channel(mock_adapter, mock_state, channel_visibility="private")
        assert channel.channel_visibility == "private"


# ============================================================================
# State management
# ============================================================================


class TestChannelState:
    """Tests for ChannelImpl state get/set."""

    @pytest.mark.asyncio
    async def test_return_none_when_no_state(self, mock_adapter, mock_state):
        channel = _make_channel(mock_adapter, mock_state)
        state = await channel.get_state()
        assert state is None

    @pytest.mark.asyncio
    async def test_set_and_retrieve_state(self, mock_adapter, mock_state):
        channel = _make_channel(mock_adapter, mock_state)
        await channel.set_state({"topic": "general"})
        state = await channel.get_state()
        assert state == {"topic": "general"}

    @pytest.mark.asyncio
    async def test_merge_state_by_default(self, mock_adapter, mock_state):
        channel = _make_channel(mock_adapter, mock_state)
        await channel.set_state({"topic": "general"})
        await channel.set_state({"count": 5})
        state = await channel.get_state()
        assert state == {"topic": "general", "count": 5}

    @pytest.mark.asyncio
    async def test_replace_state(self, mock_adapter, mock_state):
        channel = _make_channel(mock_adapter, mock_state)
        await channel.set_state({"topic": "general", "count": 5})
        await channel.set_state({"count": 10}, replace=True)
        state = await channel.get_state()
        assert state == {"count": 10}

    @pytest.mark.asyncio
    async def test_correct_key_prefix(self, mock_adapter, mock_state):
        channel = _make_channel(mock_adapter, mock_state)
        await channel.set_state({"topic": "general"})
        assert "channel-state:slack:C123" in mock_state.cache


# ============================================================================
# Messages iterator (newest first)
# ============================================================================


class TestChannelMessages:
    """Tests for channel.messages() -- newest-first iteration."""

    @pytest.mark.asyncio
    async def test_use_fetch_channel_messages(self, mock_adapter, mock_state):
        msgs = [
            create_test_message("msg-1", "Oldest"),
            create_test_message("msg-2", "Middle"),
            create_test_message("msg-3", "Newest"),
        ]
        mock_adapter.fetch_channel_messages = AsyncMock(return_value=FetchResult(messages=msgs, next_cursor=None))
        channel = _make_channel(mock_adapter, mock_state)

        collected: list[Message] = []
        async for msg in channel.messages():
            collected.append(msg)

        # Reversed: newest first
        assert len(collected) == 3
        assert collected[0].text == "Newest"
        assert collected[1].text == "Middle"
        assert collected[2].text == "Oldest"

    @pytest.mark.asyncio
    async def test_fallback_to_fetch_messages(self, mock_adapter, mock_state):
        mock_adapter.fetch_channel_messages = None
        msgs = [
            create_test_message("msg-1", "First"),
            create_test_message("msg-2", "Second"),
        ]
        mock_adapter.fetch_messages = AsyncMock(return_value=FetchResult(messages=msgs, next_cursor=None))
        channel = _make_channel(mock_adapter, mock_state)

        collected: list[Message] = []
        async for msg in channel.messages():
            collected.append(msg)

        assert len(collected) == 2
        assert collected[0].text == "Second"
        assert collected[1].text == "First"

    @pytest.mark.asyncio
    async def test_auto_paginate(self, mock_adapter, mock_state):
        call_count = 0

        async def mock_fetch(channel_id: str, options: Any = None) -> FetchResult:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return FetchResult(
                    messages=[
                        create_test_message("msg-3", "Page 1 Newest"),
                        create_test_message("msg-4", "Page 1 Older"),
                    ],
                    next_cursor="cursor-1",
                )
            return FetchResult(
                messages=[
                    create_test_message("msg-1", "Page 2 Newest"),
                    create_test_message("msg-2", "Page 2 Older"),
                ],
                next_cursor=None,
            )

        mock_adapter.fetch_channel_messages = mock_fetch
        channel = _make_channel(mock_adapter, mock_state)

        collected: list[Message] = []
        async for msg in channel.messages():
            collected.append(msg)

        assert len(collected) == 4
        # Each page is reversed internally
        assert collected[0].text == "Page 1 Older"
        assert collected[1].text == "Page 1 Newest"
        assert collected[2].text == "Page 2 Older"
        assert collected[3].text == "Page 2 Newest"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_break_early(self, mock_adapter, mock_state):
        mock_adapter.fetch_channel_messages = AsyncMock(
            return_value=FetchResult(
                messages=[
                    create_test_message("msg-1", "First"),
                    create_test_message("msg-2", "Second"),
                    create_test_message("msg-3", "Third"),
                ],
                next_cursor="more",
            )
        )
        channel = _make_channel(mock_adapter, mock_state)

        collected: list[Message] = []
        async for msg in channel.messages():
            collected.append(msg)
            if len(collected) >= 2:
                break

        assert len(collected) == 2

    @pytest.mark.asyncio
    async def test_empty_channel(self, mock_adapter, mock_state):
        mock_adapter.fetch_channel_messages = AsyncMock(return_value=FetchResult(messages=[], next_cursor=None))
        channel = _make_channel(mock_adapter, mock_state)

        collected: list[Message] = []
        async for msg in channel.messages():
            collected.append(msg)

        assert len(collected) == 0


# ============================================================================
# Threads iterator
# ============================================================================


class TestChannelThreads:
    """Tests for channel.threads() iterator."""

    @pytest.mark.asyncio
    async def test_iterate_threads(self, mock_adapter, mock_state):
        thread_summaries = [
            ThreadSummary(
                id="slack:C123:1234.5678",
                root_message=create_test_message("msg-1", "Thread 1"),
                reply_count=5,
            ),
            ThreadSummary(
                id="slack:C123:2345.6789",
                root_message=create_test_message("msg-2", "Thread 2"),
                reply_count=3,
            ),
        ]
        mock_adapter.list_threads = AsyncMock(return_value=ListThreadsResult(threads=thread_summaries, next_cursor=None))
        channel = _make_channel(mock_adapter, mock_state)

        collected: list[ThreadSummary] = []
        async for t in channel.threads():
            collected.append(t)

        assert len(collected) == 2
        assert collected[0].id == "slack:C123:1234.5678"
        assert collected[0].reply_count == 5
        assert collected[1].id == "slack:C123:2345.6789"

    @pytest.mark.asyncio
    async def test_empty_when_no_list_threads(self, mock_adapter, mock_state):
        mock_adapter.list_threads = None
        channel = _make_channel(mock_adapter, mock_state)

        collected: list[ThreadSummary] = []
        async for t in channel.threads():
            collected.append(t)

        assert len(collected) == 0

    @pytest.mark.asyncio
    async def test_auto_paginate_threads(self, mock_adapter, mock_state):
        call_count = 0

        async def mock_list(channel_id: str, options: Any = None) -> ListThreadsResult:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return ListThreadsResult(
                    threads=[
                        ThreadSummary(
                            id="slack:C123:1111",
                            root_message=create_test_message("msg-1", "T1"),
                            reply_count=2,
                        ),
                    ],
                    next_cursor="cursor-1",
                )
            return ListThreadsResult(
                threads=[
                    ThreadSummary(
                        id="slack:C123:2222",
                        root_message=create_test_message("msg-2", "T2"),
                        reply_count=1,
                    ),
                ],
                next_cursor=None,
            )

        mock_adapter.list_threads = mock_list
        channel = _make_channel(mock_adapter, mock_state)

        collected: list[ThreadSummary] = []
        async for t in channel.threads():
            collected.append(t)

        assert len(collected) == 2
        assert call_count == 2


# ============================================================================
# Metadata
# ============================================================================


class TestChannelMetadata:
    """Tests for channel.fetch_metadata()."""

    @pytest.mark.asyncio
    async def test_fetch_metadata_and_set_name(self, mock_adapter, mock_state):
        channel = _make_channel(mock_adapter, mock_state)
        assert channel.name is None

        info = await channel.fetch_metadata()

        assert info.id == "slack:C123"
        assert info.name == "#slack:C123"
        assert channel.name == "#slack:C123"

    @pytest.mark.asyncio
    async def test_basic_info_without_fetch_channel_info(self, mock_adapter, mock_state):
        mock_adapter.fetch_channel_info = None
        channel = _make_channel(mock_adapter, mock_state)

        info = await channel.fetch_metadata()

        assert info.id == "slack:C123"
        assert info.is_dm is False
        assert info.metadata == {}


# ============================================================================
# Post
# ============================================================================


class TestChannelPost:
    """Tests for channel.post() with various message formats."""

    @pytest.mark.asyncio
    async def test_post_string_message(self, mock_adapter, mock_state):
        channel = _make_channel(mock_adapter, mock_state)
        result = await channel.post("Hello channel!")
        assert result.text == "Hello channel!"

    @pytest.mark.asyncio
    async def test_post_raw_message(self, mock_adapter, mock_state):
        channel = _make_channel(mock_adapter, mock_state)
        result = await channel.post(PostableRaw(raw="raw text message"))
        assert result.text == "raw text message"

    @pytest.mark.asyncio
    async def test_post_markdown_message(self, mock_adapter, mock_state):
        channel = _make_channel(mock_adapter, mock_state)
        result = await channel.post(PostableMarkdown(markdown="**bold** text"))
        assert result.text == "**bold** text"

    @pytest.mark.asyncio
    async def test_fallback_to_post_message_when_no_channel_post(self, mock_adapter, mock_state):
        mock_adapter.post_channel_message = None
        channel = _make_channel(mock_adapter, mock_state)

        await channel.post("Hello!")

        assert len(mock_adapter._post_calls) == 1
        assert mock_adapter._post_calls[0] == ("slack:C123", "Hello!")

    @pytest.mark.asyncio
    async def test_streaming_accumulates_text(self, mock_adapter, mock_state):
        channel = _make_channel(mock_adapter, mock_state)

        # Replace post_channel_message to track what was posted
        posted_messages: list[Any] = []
        original_post_channel = mock_adapter.post_channel_message

        async def tracking_post(channel_id: str, message: Any) -> RawMessage:
            posted_messages.append(message)
            return RawMessage(id="msg-1", thread_id=None, raw={})

        mock_adapter.post_channel_message = tracking_post

        async def text_stream() -> AsyncIterator[str]:
            yield "Hello"
            yield " "
            yield "World"

        result = await channel.post(text_stream())

        assert len(posted_messages) == 1
        assert isinstance(posted_messages[0], PostableMarkdown)
        assert posted_messages[0].markdown == "Hello World"
        assert result.text == "Hello World"

    @pytest.mark.asyncio
    async def test_sent_message_author(self, mock_adapter, mock_state):
        channel = _make_channel(mock_adapter, mock_state)
        result = await channel.post("Hello")

        assert result.author.is_bot is True
        assert result.author.is_me is True
        assert result.author.user_id == "self"


# ============================================================================
# Serialization
# ============================================================================


class TestChannelSerialization:
    """Tests for ChannelImpl.to_json() and from_json()."""

    def test_serialize_with_correct_type_tag(self, mock_adapter, mock_state):
        channel = _make_channel(mock_adapter, mock_state)
        data = channel.to_json()

        assert data["_type"] == "chat:Channel"
        assert data["id"] == "slack:C123"
        assert data["adapter_name"] == "slack"
        assert data["channel_visibility"] == "unknown"
        assert data["is_dm"] is False

    def test_serialize_dm_channel(self, mock_adapter, mock_state):
        channel = _make_channel(mock_adapter, mock_state, is_dm=True)
        data = channel.to_json()
        assert data["is_dm"] is True

    def test_from_json_reconstruct(self, mock_adapter, mock_state):
        data = {
            "_type": "chat:Channel",
            "id": "slack:C123",
            "adapter_name": "slack",
            "channel_visibility": "private",
            "is_dm": False,
        }
        channel = ChannelImpl.from_json(data, adapter=mock_adapter)

        assert channel.id == "slack:C123"
        assert channel.channel_visibility == "private"
        assert channel.is_dm is False
        assert channel.adapter.name == "slack"

    def test_round_trip(self, mock_adapter, mock_state):
        original = _make_channel(mock_adapter, mock_state, is_dm=True, channel_visibility="external")
        data = original.to_json()
        restored = ChannelImpl.from_json(data, adapter=mock_adapter)

        assert restored.id == original.id
        assert restored.is_dm == original.is_dm
        assert restored.channel_visibility == original.channel_visibility
        assert restored.adapter.name == original.adapter.name

    def test_json_serializable(self, mock_adapter, mock_state):
        import json

        channel = _make_channel(mock_adapter, mock_state)
        data = channel.to_json()
        stringified = json.dumps(data)
        parsed = json.loads(stringified)
        assert parsed == data

    def test_mention_user(self, mock_adapter, mock_state):
        channel = _make_channel(mock_adapter, mock_state)
        assert channel.mention_user("U123") == "<@U123>"
