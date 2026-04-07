"""Extended tests for ChannelImpl.

Ported from TS channel.test.ts to cover categories missing from test_channel.py:
- state management (get, set, merge, replace, key prefix)
- messages iterator (newest first, pagination, empty, break early)
- threads iterator (pagination, empty when not supported)
- fetchMetadata (sets name, basic info without fetchChannelInfo)
- post with different message formats (raw, markdown, card, streaming)
- post error cases (threadId override, SentMessage capabilities, edit/delete/reaction)
- postEphemeral (native, Author object, DM fallback, no fallback, no openDM)
- startTyping (delegates to adapter, with status)
- mentionUser (various formats)
- serialization (toJSON/fromJSON roundtrip, DM, external visibility)
- schedule() (delegation, error handling, return shape, cancel, message formats)
- deriveChannelId
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest
from chat_sdk.channel import ChannelImpl, _ChannelImplConfigWithAdapter, derive_channel_id
from chat_sdk.errors import ChatNotImplementedError
from chat_sdk.testing import (
    MockAdapter,
    MockStateAdapter,
    create_mock_adapter,
    create_mock_state,
    create_test_message,
)
from chat_sdk.types import (
    Author,
    ChannelInfo,
    FetchResult,
    ListThreadsResult,
    Message,
    PostableCard,
    PostableMarkdown,
    PostableRaw,
    PostEphemeralOptions,
    RawMessage,
    ScheduledMessage,
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
# State management (extended)
# ============================================================================


class TestChannelStateExtended:
    """Extended state management tests."""

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
    async def test_overwrite_existing_keys_when_merging(self, mock_adapter, mock_state):
        channel = _make_channel(mock_adapter, mock_state)
        await channel.set_state({"topic": "general", "count": 1})
        await channel.set_state({"count": 10})
        state = await channel.get_state()
        assert state == {"topic": "general", "count": 10}

    @pytest.mark.asyncio
    async def test_replace_state_when_option_set(self, mock_adapter, mock_state):
        channel = _make_channel(mock_adapter, mock_state)
        await channel.set_state({"topic": "general", "count": 5})
        await channel.set_state({"count": 10}, replace=True)
        state = await channel.get_state()
        assert state == {"count": 10}
        assert "topic" not in state

    @pytest.mark.asyncio
    async def test_correct_key_prefix(self, mock_adapter, mock_state):
        channel = _make_channel(mock_adapter, mock_state)
        await channel.set_state({"topic": "general"})
        assert "channel-state:slack:C123" in mock_state.cache

    @pytest.mark.asyncio
    async def test_get_with_correct_key(self, mock_adapter, mock_state):
        mock_state.cache["channel-state:slack:C123"] = {"pre_existing": True}
        channel = _make_channel(mock_adapter, mock_state)
        state = await channel.get_state()
        assert state == {"pre_existing": True}


# ============================================================================
# Messages iterator (newest first) -- extended
# ============================================================================


class TestChannelMessagesExtended:
    """Extended tests for channel.messages() -- newest-first iteration."""

    @pytest.mark.asyncio
    async def test_use_fetch_channel_messages_with_backward_direction(self, mock_adapter, mock_state):
        msgs = [
            create_test_message("msg-1", "Oldest"),
            create_test_message("msg-2", "Newest"),
        ]
        fetch_calls: list[tuple[str, Any]] = []

        async def mock_fetch(channel_id: str, options: Any = None) -> FetchResult:
            fetch_calls.append((channel_id, options))
            return FetchResult(messages=msgs, next_cursor=None)

        mock_adapter.fetch_channel_messages = mock_fetch
        channel = _make_channel(mock_adapter, mock_state)

        collected: list[Message] = []
        async for msg in channel.messages():
            collected.append(msg)

        assert len(collected) == 2
        assert collected[0].text == "Newest"
        assert collected[1].text == "Oldest"
        assert fetch_calls[0][1].direction == "backward"

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
    async def test_auto_paginate_through_pages(self, mock_adapter, mock_state):
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
        async def mock_fetch(channel_id: str, options: Any = None) -> FetchResult:
            return FetchResult(
                messages=[
                    create_test_message("msg-1", "First"),
                    create_test_message("msg-2", "Second"),
                    create_test_message("msg-3", "Third"),
                ],
                next_cursor="more",
            )

        mock_adapter.fetch_channel_messages = mock_fetch
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
# Threads iterator (extended)
# ============================================================================


class TestChannelThreadsExtended:
    """Extended threads iterator tests."""

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
        mock_adapter.list_threads = AsyncMock(
            return_value=ListThreadsResult(threads=thread_summaries, next_cursor=None)
        )
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

        async def mock_list(channel_id: str, options: Any = None, **kwargs: Any) -> ListThreadsResult:
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
# Metadata (extended)
# ============================================================================


class TestChannelMetadataExtended:
    """Extended metadata tests."""

    @pytest.mark.asyncio
    async def test_fetch_channel_info_and_set_name(self, mock_adapter, mock_state):
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
# Post with different message formats (extended)
# ============================================================================


class TestChannelPostExtended:
    """Extended post tests covering all message formats."""

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
    async def test_post_card_message(self, mock_adapter, mock_state):
        channel = _make_channel(mock_adapter, mock_state)
        card = PostableCard(card={"type": "card", "title": "Test"}, fallback_text="card fallback")
        result = await channel.post(card)
        assert result.text == "card fallback"

    @pytest.mark.asyncio
    async def test_streaming_accumulates_text(self, mock_adapter, mock_state):
        channel = _make_channel(mock_adapter, mock_state)

        posted_messages: list[Any] = []

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
    async def test_fallback_to_post_message_when_no_channel_post(self, mock_adapter, mock_state):
        mock_adapter.post_channel_message = None
        channel = _make_channel(mock_adapter, mock_state)

        await channel.post("Hello!")

        assert len(mock_adapter._post_calls) == 1
        assert mock_adapter._post_calls[0] == ("slack:C123", "Hello!")


# ============================================================================
# Post error cases (SentMessage capabilities)
# ============================================================================


class TestChannelPostCapabilities:
    """Tests for SentMessage capabilities from channel.post()."""

    @pytest.mark.asyncio
    async def test_thread_id_override_from_response(self, mock_adapter, mock_state):
        async def custom_post(channel_id: str, message: Any) -> RawMessage:
            return RawMessage(id="msg-2", thread_id="slack:C123:new-thread", raw={})

        mock_adapter.post_channel_message = custom_post
        channel = _make_channel(mock_adapter, mock_state)

        result = await channel.post("Hello!")
        assert result.thread_id == "slack:C123:new-thread"

    @pytest.mark.asyncio
    async def test_sent_message_has_capabilities(self, mock_adapter, mock_state):
        channel = _make_channel(mock_adapter, mock_state)
        result = await channel.post("Hello!")

        assert callable(result.edit)
        assert callable(result.delete)
        assert callable(result.add_reaction)
        assert callable(result.remove_reaction)

    @pytest.mark.asyncio
    async def test_edit_sent_message(self, mock_adapter, mock_state):
        channel = _make_channel(mock_adapter, mock_state)
        result = await channel.post("Hello!")
        await result.edit("Updated!")

        assert len(mock_adapter._edit_calls) == 1
        assert mock_adapter._edit_calls[0][0] == "slack:C123"
        assert mock_adapter._edit_calls[0][1] == "msg-1"
        assert mock_adapter._edit_calls[0][2] == "Updated!"

    @pytest.mark.asyncio
    async def test_delete_sent_message(self, mock_adapter, mock_state):
        channel = _make_channel(mock_adapter, mock_state)
        result = await channel.post("Hello!")
        await result.delete()

        assert len(mock_adapter._delete_calls) == 1
        assert mock_adapter._delete_calls[0] == ("slack:C123", "msg-1")

    @pytest.mark.asyncio
    async def test_add_reaction_to_sent_message(self, mock_adapter, mock_state):
        channel = _make_channel(mock_adapter, mock_state)
        result = await channel.post("Hello!")
        await result.add_reaction("thumbsup")

        assert len(mock_adapter._add_reaction_calls) == 1
        assert mock_adapter._add_reaction_calls[0] == ("slack:C123", "msg-1", "thumbsup")

    @pytest.mark.asyncio
    async def test_remove_reaction_from_sent_message(self, mock_adapter, mock_state):
        channel = _make_channel(mock_adapter, mock_state)
        result = await channel.post("Hello!")
        await result.remove_reaction("thumbsup")

        assert len(mock_adapter._remove_reaction_calls) == 1
        assert mock_adapter._remove_reaction_calls[0] == ("slack:C123", "msg-1", "thumbsup")

    @pytest.mark.asyncio
    async def test_sent_message_author(self, mock_adapter, mock_state):
        channel = _make_channel(mock_adapter, mock_state)
        result = await channel.post("Hello")

        assert result.author.is_bot is True
        assert result.author.is_me is True
        assert result.author.user_id == "self"


# ============================================================================
# postEphemeral
# ============================================================================


class TestChannelPostEphemeral:
    """Tests for channel.post_ephemeral()."""

    @pytest.mark.asyncio
    async def test_use_adapter_post_ephemeral_when_available(self, mock_adapter, mock_state):
        ephemeral_calls: list[tuple[str, str, Any]] = []

        async def mock_post_ephemeral(channel_id: str, user_id: str, message: Any) -> Any:
            ephemeral_calls.append((channel_id, user_id, message))
            return {
                "id": "eph-1",
                "thread_id": "slack:C123",
                "used_fallback": False,
                "raw": {},
            }

        mock_adapter.post_ephemeral = mock_post_ephemeral
        channel = _make_channel(mock_adapter, mock_state)

        result = await channel.post_ephemeral(
            "U456",
            "Secret!",
            PostEphemeralOptions(fallback_to_dm=True),
        )

        assert len(ephemeral_calls) == 1
        assert ephemeral_calls[0] == ("slack:C123", "U456", "Secret!")

    @pytest.mark.asyncio
    async def test_extract_user_id_from_author(self, mock_adapter, mock_state):
        ephemeral_calls: list[tuple[str, str, Any]] = []

        async def mock_post_ephemeral(channel_id: str, user_id: str, message: Any) -> Any:
            ephemeral_calls.append((channel_id, user_id, message))
            return {
                "id": "eph-1",
                "thread_id": "slack:C123",
                "used_fallback": False,
                "raw": {},
            }

        mock_adapter.post_ephemeral = mock_post_ephemeral
        channel = _make_channel(mock_adapter, mock_state)

        author = Author(
            user_id="U789",
            user_name="testuser",
            full_name="Test User",
            is_bot=False,
            is_me=False,
        )

        await channel.post_ephemeral(
            author,
            "Hello!",
            PostEphemeralOptions(fallback_to_dm=False),
        )

        assert ephemeral_calls[0][1] == "U789"

    @pytest.mark.asyncio
    async def test_return_none_when_no_ephemeral_and_no_fallback(self, mock_adapter, mock_state):
        mock_adapter.post_ephemeral = None
        channel = _make_channel(mock_adapter, mock_state)

        result = await channel.post_ephemeral(
            "U456",
            "Secret!",
            PostEphemeralOptions(fallback_to_dm=False),
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_fallback_to_dm(self, mock_adapter, mock_state):
        mock_adapter.post_ephemeral = None
        channel = _make_channel(mock_adapter, mock_state)

        result = await channel.post_ephemeral(
            "U456",
            "Secret!",
            PostEphemeralOptions(fallback_to_dm=True),
        )

        assert result is not None
        assert result.id == "msg-1"
        assert result.thread_id == "slack:DU456:"
        assert result.used_fallback is True

    @pytest.mark.asyncio
    async def test_return_none_when_no_ephemeral_no_open_dm(self, mock_adapter, mock_state):
        mock_adapter.post_ephemeral = None
        mock_adapter.open_dm = None
        channel = _make_channel(mock_adapter, mock_state)

        result = await channel.post_ephemeral(
            "U456",
            "Secret!",
            PostEphemeralOptions(fallback_to_dm=True),
        )

        assert result is None


# ============================================================================
# startTyping
# ============================================================================


class TestChannelStartTyping:
    """Tests for channel.start_typing()."""

    @pytest.mark.asyncio
    async def test_call_adapter_start_typing(self, mock_adapter, mock_state):
        channel = _make_channel(mock_adapter, mock_state)
        await channel.start_typing()

        assert len(mock_adapter._start_typing_calls) == 1
        assert mock_adapter._start_typing_calls[0] == ("slack:C123", None)

    @pytest.mark.asyncio
    async def test_pass_status_string(self, mock_adapter, mock_state):
        channel = _make_channel(mock_adapter, mock_state)
        await channel.start_typing("thinking...")

        assert mock_adapter._start_typing_calls[0] == ("slack:C123", "thinking...")


# ============================================================================
# mentionUser (extended)
# ============================================================================


class TestChannelMentionUserExtended:
    """Extended mentionUser tests."""

    def test_return_formatted_mention(self, mock_adapter, mock_state):
        channel = _make_channel(mock_adapter, mock_state)
        assert channel.mention_user("U456") == "<@U456>"

    def test_handle_different_user_id_formats(self, mock_adapter, mock_state):
        channel = _make_channel(mock_adapter, mock_state)
        assert channel.mention_user("UABC123DEF") == "<@UABC123DEF>"
        assert channel.mention_user("bot-user") == "<@bot-user>"


# ============================================================================
# Serialization (extended)
# ============================================================================


class TestChannelSerializationExtended:
    """Extended serialization tests."""

    def test_serialize_with_correct_type_tag(self, mock_adapter, mock_state):
        channel = _make_channel(mock_adapter, mock_state)
        data = channel.to_json()

        assert data == {
            "_type": "chat:Channel",
            "id": "slack:C123",
            "adapter_name": "slack",
            "channel_visibility": "unknown",
            "is_dm": False,
        }

    def test_serialize_dm_channel(self, mock_adapter, mock_state):
        channel = _make_channel(mock_adapter, mock_state, is_dm=True)
        data = channel.to_json()
        assert data["is_dm"] is True

    def test_serialize_external_visibility(self, mock_adapter, mock_state):
        channel = _make_channel(mock_adapter, mock_state, channel_visibility="external")
        data = channel.to_json()
        assert data["channel_visibility"] == "external"

    def test_deserialize_from_json(self, mock_adapter, mock_state):
        data = {
            "_type": "chat:Channel",
            "id": "slack:C123",
            "adapter_name": "slack",
            "is_dm": False,
        }
        channel = ChannelImpl.from_json(data, adapter=mock_adapter)

        assert channel.id == "slack:C123"
        assert channel.is_dm is False
        assert channel.adapter is mock_adapter

    def test_round_trip(self, mock_adapter, mock_state):
        original = _make_channel(mock_adapter, mock_state, is_dm=True, channel_visibility="external")
        data = original.to_json()
        restored = ChannelImpl.from_json(data, adapter=mock_adapter)

        assert restored.id == original.id
        assert restored.is_dm == original.is_dm
        assert restored.channel_visibility == original.channel_visibility
        assert restored.adapter.name == original.adapter.name

    def test_json_serializable(self, mock_adapter, mock_state):
        channel = _make_channel(mock_adapter, mock_state)
        data = channel.to_json()
        stringified = json.dumps(data)
        parsed = json.loads(stringified)
        assert parsed == data

    def test_default_channel_visibility_on_deserialize(self, mock_adapter, mock_state):
        data = {
            "_type": "chat:Channel",
            "id": "slack:C123",
            "adapter_name": "slack",
            "is_dm": False,
        }
        channel = ChannelImpl.from_json(data, adapter=mock_adapter)
        assert channel.channel_visibility == "unknown"

    def test_round_trip_private_visibility(self, mock_adapter, mock_state):
        original = _make_channel(mock_adapter, mock_state, channel_visibility="private")
        data = original.to_json()
        restored = ChannelImpl.from_json(data, adapter=mock_adapter)
        assert restored.channel_visibility == "private"

    def test_round_trip_workspace_visibility(self, mock_adapter, mock_state):
        original = _make_channel(mock_adapter, mock_state, channel_visibility="workspace")
        data = original.to_json()
        restored = ChannelImpl.from_json(data, adapter=mock_adapter)
        assert restored.channel_visibility == "workspace"


# ============================================================================
# schedule()
# ============================================================================


class TestChannelSchedule:
    """Tests for channel.schedule()."""

    FUTURE_DATE = datetime(2030, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    def _mock_schedule_result(self, **overrides: Any) -> ScheduledMessage:
        defaults = {
            "scheduled_message_id": "Q123",
            "channel_id": "C123",
            "post_at": self.FUTURE_DATE,
            "raw": {"ok": True},
        }
        defaults.update(overrides)
        return ScheduledMessage(**defaults)

    # ---- Error handling ----

    @pytest.mark.asyncio
    async def test_throw_not_implemented_when_no_schedule_message(self, mock_adapter, mock_state):
        channel = _make_channel(mock_adapter, mock_state)

        with pytest.raises(ChatNotImplementedError):
            await channel.schedule("Hello", post_at=self.FUTURE_DATE)

    @pytest.mark.asyncio
    async def test_not_implemented_includes_scheduling_method(self, mock_adapter, mock_state):
        channel = _make_channel(mock_adapter, mock_state)

        with pytest.raises(ChatNotImplementedError) as exc_info:
            await channel.schedule("Hello", post_at=self.FUTURE_DATE)

        assert exc_info.value.method == "scheduling"

    # ---- Basic delegation ----

    @pytest.mark.asyncio
    async def test_delegate_to_adapter_schedule_message(self, mock_adapter, mock_state):
        schedule_calls: list[tuple[str, Any, Any]] = []

        async def mock_schedule(channel_id: str, message: Any, options: Any) -> ScheduledMessage:
            schedule_calls.append((channel_id, message, options))
            return self._mock_schedule_result()

        mock_adapter.schedule_message = mock_schedule
        channel = _make_channel(mock_adapter, mock_state)

        await channel.schedule("Hello", post_at=self.FUTURE_DATE)

        assert len(schedule_calls) == 1
        assert schedule_calls[0][0] == "slack:C123"
        assert schedule_calls[0][1] == "Hello"

    @pytest.mark.asyncio
    async def test_return_scheduled_message(self, mock_adapter, mock_state):
        expected = self._mock_schedule_result()

        async def mock_schedule(channel_id: str, message: Any, options: Any) -> ScheduledMessage:
            return expected

        mock_adapter.schedule_message = mock_schedule
        channel = _make_channel(mock_adapter, mock_state)

        result = await channel.schedule("Hello", post_at=self.FUTURE_DATE)
        assert result is expected

    # ---- Message formats ----

    @pytest.mark.asyncio
    async def test_pass_string_message(self, mock_adapter, mock_state):
        schedule_calls: list[tuple[str, Any, Any]] = []

        async def mock_schedule(channel_id: str, message: Any, options: Any) -> ScheduledMessage:
            schedule_calls.append((channel_id, message, options))
            return self._mock_schedule_result()

        mock_adapter.schedule_message = mock_schedule
        channel = _make_channel(mock_adapter, mock_state)

        await channel.schedule("Plain text", post_at=self.FUTURE_DATE)
        assert schedule_calls[0][1] == "Plain text"

    @pytest.mark.asyncio
    async def test_pass_markdown_message(self, mock_adapter, mock_state):
        schedule_calls: list[tuple[str, Any, Any]] = []

        async def mock_schedule(channel_id: str, message: Any, options: Any) -> ScheduledMessage:
            schedule_calls.append((channel_id, message, options))
            return self._mock_schedule_result()

        mock_adapter.schedule_message = mock_schedule
        channel = _make_channel(mock_adapter, mock_state)

        md_msg = PostableMarkdown(markdown="**bold** text")
        await channel.schedule(md_msg, post_at=self.FUTURE_DATE)
        assert schedule_calls[0][1] is md_msg

    # ---- Cancel ----

    @pytest.mark.asyncio
    async def test_cancel_scheduled_message(self, mock_adapter, mock_state):
        cancel_called = False

        async def cancel_fn() -> None:
            nonlocal cancel_called
            cancel_called = True

        async def mock_schedule(channel_id: str, message: Any, options: Any) -> ScheduledMessage:
            return self._mock_schedule_result(_cancel=cancel_fn)

        mock_adapter.schedule_message = mock_schedule
        channel = _make_channel(mock_adapter, mock_state)

        result = await channel.schedule("Hello", post_at=self.FUTURE_DATE)
        await result.cancel()

        assert cancel_called

    # ---- Error propagation ----

    @pytest.mark.asyncio
    async def test_propagate_adapter_errors(self, mock_adapter, mock_state):
        async def mock_schedule(channel_id: str, message: Any, options: Any) -> ScheduledMessage:
            raise Exception("API failure")

        mock_adapter.schedule_message = mock_schedule
        channel = _make_channel(mock_adapter, mock_state)

        with pytest.raises(Exception, match="API failure"):
            await channel.schedule("Hello", post_at=self.FUTURE_DATE)

    @pytest.mark.asyncio
    async def test_not_call_post_message_when_scheduling(self, mock_adapter, mock_state):
        async def mock_schedule(channel_id: str, message: Any, options: Any) -> ScheduledMessage:
            return self._mock_schedule_result()

        mock_adapter.schedule_message = mock_schedule
        channel = _make_channel(mock_adapter, mock_state)

        await channel.schedule("Hello", post_at=self.FUTURE_DATE)

        assert len(mock_adapter._post_calls) == 0


# ============================================================================
# deriveChannelId
# ============================================================================


class TestDeriveChannelId:
    """Tests for deriveChannelId utility."""

    def test_use_adapter_channel_id_from_thread_id(self, mock_adapter, mock_state):
        channel_id = derive_channel_id(mock_adapter, "slack:C123:1234.5678")
        assert channel_id == "slack:C123"

    def test_work_with_different_adapters(self):
        adapter = create_mock_adapter("gchat")
        channel_id = derive_channel_id(adapter, "gchat:spaces/ABC123:dGhyZWFk")
        assert channel_id == "gchat:spaces/ABC123"
