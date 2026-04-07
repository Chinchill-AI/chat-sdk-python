"""Tests for ThreadImpl.

Covers: construction, post message, streaming (native + fallback),
message iteration (forward/backward), ephemeral messages, subscription,
state get/set, and serialization.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest
from chat_sdk.testing import (
    MockAdapter,
    MockStateAdapter,
    create_mock_adapter,
    create_mock_state,
    create_test_message,
)
from chat_sdk.thread import ThreadImpl, _ThreadImplConfig
from chat_sdk.types import (
    Author,
    FetchResult,
    Message,
    MessageMetadata,
    PostableMarkdown,
    PostableRaw,
    RawMessage,
    StreamChunk,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_thread(
    adapter: MockAdapter | None = None,
    state: MockStateAdapter | None = None,
    *,
    thread_id: str = "slack:C123:1234.5678",
    channel_id: str = "C123",
    is_dm: bool = False,
    channel_visibility: str = "unknown",
    current_message: Message | None = None,
    fallback_streaming_placeholder_text: str | None = "...",
    streaming_update_interval_ms: int = 500,
) -> ThreadImpl:
    adapter = adapter or create_mock_adapter()
    state = state or create_mock_state()
    return ThreadImpl(
        _ThreadImplConfig(
            id=thread_id,
            adapter=adapter,
            state_adapter=state,
            channel_id=channel_id,
            is_dm=is_dm,
            channel_visibility=channel_visibility,
            current_message=current_message,
            fallback_streaming_placeholder_text=fallback_streaming_placeholder_text,
            streaming_update_interval_ms=streaming_update_interval_ms,
        )
    )


async def _create_text_stream(chunks: list[str]) -> AsyncIterator[str]:
    for chunk in chunks:
        yield chunk


# ============================================================================
# Per-thread state
# ============================================================================


class TestThreadState:
    """Tests for ThreadImpl state get/set."""

    @pytest.mark.asyncio
    async def test_return_none_when_no_state(self, mock_adapter, mock_state):
        thread = _make_thread(mock_adapter, mock_state)
        state = await thread.get_state()
        assert state is None

    @pytest.mark.asyncio
    async def test_return_stored_state(self, mock_adapter, mock_state):
        mock_state.cache["thread-state:slack:C123:1234.5678"] = {"ai_mode": True}
        thread = _make_thread(mock_adapter, mock_state)
        state = await thread.get_state()
        assert state == {"ai_mode": True}

    @pytest.mark.asyncio
    async def test_set_state_and_retrieve(self, mock_adapter, mock_state):
        thread = _make_thread(mock_adapter, mock_state)
        await thread.set_state({"ai_mode": True})
        state = await thread.get_state()
        assert state == {"ai_mode": True}

    @pytest.mark.asyncio
    async def test_merge_state_by_default(self, mock_adapter, mock_state):
        thread = _make_thread(mock_adapter, mock_state)
        await thread.set_state({"ai_mode": True})
        await thread.set_state({"counter": 5})
        state = await thread.get_state()
        assert state == {"ai_mode": True, "counter": 5}

    @pytest.mark.asyncio
    async def test_overwrite_existing_keys(self, mock_adapter, mock_state):
        thread = _make_thread(mock_adapter, mock_state)
        await thread.set_state({"ai_mode": True, "counter": 1})
        await thread.set_state({"counter": 10})
        state = await thread.get_state()
        assert state == {"ai_mode": True, "counter": 10}

    @pytest.mark.asyncio
    async def test_replace_entire_state(self, mock_adapter, mock_state):
        thread = _make_thread(mock_adapter, mock_state)
        await thread.set_state({"ai_mode": True, "counter": 5})
        await thread.set_state({"counter": 10}, replace=True)
        state = await thread.get_state()
        assert state == {"counter": 10}
        assert "ai_mode" not in state

    @pytest.mark.asyncio
    async def test_correct_key_prefix(self, mock_adapter, mock_state):
        thread = _make_thread(mock_adapter, mock_state)
        await thread.set_state({"ai_mode": True})
        assert "thread-state:slack:C123:1234.5678" in mock_state.cache


# ============================================================================
# Post with different message formats
# ============================================================================


class TestThreadPost:
    """Tests for ThreadImpl.post() with various message formats."""

    @pytest.mark.asyncio
    async def test_post_string_message(self, mock_adapter, mock_state):
        thread = _make_thread(mock_adapter, mock_state)
        result = await thread.post("Hello world")

        assert len(mock_adapter._post_calls) == 1
        assert mock_adapter._post_calls[0] == ("slack:C123:1234.5678", "Hello world")
        assert result.text == "Hello world"
        assert result.id == "msg-1"

    @pytest.mark.asyncio
    async def test_post_raw_message(self, mock_adapter, mock_state):
        thread = _make_thread(mock_adapter, mock_state)
        result = await thread.post(PostableRaw(raw="raw text"))

        assert mock_adapter._post_calls[0][1].raw == "raw text"
        assert result.text == "raw text"

    @pytest.mark.asyncio
    async def test_post_markdown_message(self, mock_adapter, mock_state):
        thread = _make_thread(mock_adapter, mock_state)
        result = await thread.post(PostableMarkdown(markdown="**bold** text"))

        assert result.text == "**bold** text"

    @pytest.mark.asyncio
    async def test_correct_author_on_sent_message(self, mock_adapter, mock_state):
        thread = _make_thread(mock_adapter, mock_state)
        result = await thread.post("Hello")

        assert result.author.is_bot is True
        assert result.author.is_me is True
        assert result.author.user_id == "self"
        assert result.author.user_name == "slack-bot"

    @pytest.mark.asyncio
    async def test_thread_id_override_from_response(self, mock_adapter, mock_state):
        original_post = mock_adapter.post_message

        async def custom_post(thread_id: str, message: Any) -> RawMessage:
            mock_adapter._post_calls.append((thread_id, message))
            return RawMessage(id="msg-2", thread_id="slack:C123:new-thread-id", raw={})

        mock_adapter.post_message = custom_post
        thread = _make_thread(mock_adapter, mock_state)
        result = await thread.post("Hello")

        assert result.thread_id == "slack:C123:new-thread-id"

    @pytest.mark.asyncio
    async def test_sent_message_has_capabilities(self, mock_adapter, mock_state):
        thread = _make_thread(mock_adapter, mock_state)
        result = await thread.post("Hello")

        assert callable(result.edit)
        assert callable(result.delete)
        assert callable(result.add_reaction)
        assert callable(result.remove_reaction)


# ============================================================================
# Streaming
# ============================================================================


class TestThreadStreaming:
    """Tests for ThreadImpl streaming (native + fallback)."""

    @pytest.mark.asyncio
    async def test_native_streaming_when_available(self, mock_adapter, mock_state):
        stream_calls: list[Any] = []

        async def mock_stream(thread_id: str, text_stream: Any, options: Any) -> RawMessage:
            stream_calls.append((thread_id, text_stream, options))
            # Consume the stream
            async for _ in text_stream:
                pass
            return RawMessage(id="msg-stream", thread_id="t1", raw="Hello World")

        mock_adapter.stream = mock_stream
        thread = _make_thread(mock_adapter, mock_state)

        text_stream = _create_text_stream(["Hello", " ", "World"])
        await thread.post(text_stream)

        assert len(stream_calls) == 1
        assert stream_calls[0][0] == "slack:C123:1234.5678"
        # Should NOT call post_message for fallback
        assert len(mock_adapter._post_calls) == 0

    @pytest.mark.asyncio
    async def test_fallback_post_edit_when_no_native_streaming(self, mock_adapter, mock_state):
        mock_adapter.stream = None
        thread = _make_thread(mock_adapter, mock_state)

        text_stream = _create_text_stream(["Hello", " ", "World"])
        await thread.post(text_stream)

        # Should post initial placeholder
        assert len(mock_adapter._post_calls) >= 1
        assert mock_adapter._post_calls[0] == ("slack:C123:1234.5678", "...")
        # Should edit with final content wrapped as markdown
        assert len(mock_adapter._edit_calls) >= 1
        last_edit = mock_adapter._edit_calls[-1]
        assert last_edit[0] == "slack:C123:1234.5678"
        assert last_edit[1] == "msg-1"
        assert isinstance(last_edit[2], PostableMarkdown)
        assert last_edit[2].markdown == "Hello World"

    @pytest.mark.asyncio
    async def test_accumulate_text_chunks(self, mock_adapter, mock_state):
        mock_adapter.stream = None
        thread = _make_thread(mock_adapter, mock_state)

        text_stream = _create_text_stream(["This ", "is ", "a ", "test ", "message."])
        result = await thread.post(text_stream)

        last_edit = mock_adapter._edit_calls[-1]
        assert isinstance(last_edit[2], PostableMarkdown)
        assert last_edit[2].markdown == "This is a test message."
        assert result.text == "This is a test message."

    @pytest.mark.asyncio
    async def test_sent_message_capabilities_after_streaming(self, mock_adapter, mock_state):
        mock_adapter.stream = None
        thread = _make_thread(mock_adapter, mock_state)

        text_stream = _create_text_stream(["Hello"])
        result = await thread.post(text_stream)

        assert result.id == "msg-1"
        assert callable(result.edit)
        assert callable(result.delete)
        assert callable(result.add_reaction)
        assert callable(result.remove_reaction)

    @pytest.mark.asyncio
    async def test_empty_stream(self, mock_adapter, mock_state):
        mock_adapter.stream = None
        thread = _make_thread(mock_adapter, mock_state)

        text_stream = _create_text_stream([])
        await thread.post(text_stream)

        # Should post initial placeholder
        assert mock_adapter._post_calls[0] == ("slack:C123:1234.5678", "...")
        # Should edit with empty content wrapped as markdown
        last_edit = mock_adapter._edit_calls[-1]
        assert isinstance(last_edit[2], PostableMarkdown)
        assert last_edit[2].markdown == ""

    @pytest.mark.asyncio
    async def test_disabled_placeholder(self, mock_adapter, mock_state):
        mock_adapter.stream = None
        thread = _make_thread(
            mock_adapter,
            mock_state,
            fallback_streaming_placeholder_text=None,
        )

        text_stream = _create_text_stream(["H", "i"])
        await thread.post(text_stream)

        # Should NOT post "..."
        for call in mock_adapter._post_calls:
            assert call[1] != "..."

    @pytest.mark.asyncio
    async def test_empty_stream_with_disabled_placeholder(self, mock_adapter, mock_state):
        mock_adapter.stream = None
        thread = _make_thread(
            mock_adapter,
            mock_state,
            fallback_streaming_placeholder_text=None,
        )

        text_stream = _create_text_stream([])
        await thread.post(text_stream)

        # Should still post a message even with no chunks
        assert len(mock_adapter._post_calls) >= 1
        # Final post should be empty markdown
        posted = mock_adapter._post_calls[-1][1]
        assert isinstance(posted, PostableMarkdown)
        assert posted.markdown == ""

    @pytest.mark.asyncio
    async def test_preserve_newlines_native_path(self, mock_adapter, mock_state):
        captured_chunks: list[str] = []

        async def mock_stream(thread_id: str, text_stream: Any, options: Any) -> RawMessage:
            async for chunk in text_stream:
                if isinstance(chunk, str):
                    captured_chunks.append(chunk)
            return RawMessage(id="msg-stream", thread_id="t1", raw={})

        mock_adapter.stream = mock_stream
        thread = _make_thread(mock_adapter, mock_state)

        text_stream = _create_text_stream(["hello", ".", "\n", "how", " are", " you?"])
        result = await thread.post(text_stream)

        assert result.text == "hello.\nhow are you?"
        assert captured_chunks == ["hello", ".", "\n", "how", " are", " you?"]

    @pytest.mark.asyncio
    async def test_preserve_newlines_fallback_path(self, mock_adapter, mock_state):
        mock_adapter.stream = None
        thread = _make_thread(mock_adapter, mock_state)

        text_stream = _create_text_stream(["hello.", "\n", "how are you?"])
        result = await thread.post(text_stream)

        last_edit = mock_adapter._edit_calls[-1]
        assert isinstance(last_edit[2], PostableMarkdown)
        assert last_edit[2].markdown == "hello.\nhow are you?"
        assert result.text == "hello.\nhow are you?"

    @pytest.mark.asyncio
    async def test_stream_chunk_objects_pass_through(self, mock_adapter, mock_state):
        """StreamChunk dataclass objects should pass through to adapter.stream."""
        from chat_sdk.types import MarkdownTextChunk, TaskUpdateChunk

        captured_chunks: list[Any] = []

        async def mock_stream(thread_id: str, text_stream: Any, options: Any) -> RawMessage:
            async for chunk in text_stream:
                captured_chunks.append(chunk)
            return RawMessage(id="msg-stream", thread_id="t1", raw={})

        mock_adapter.stream = mock_stream
        thread = _make_thread(mock_adapter, mock_state)

        async def mixed_stream() -> AsyncIterator[Any]:
            yield "Hello "
            yield TaskUpdateChunk(type="task_update", id="tool-1", title="Running bash", status="in_progress")
            yield "world"
            yield TaskUpdateChunk(
                type="task_update", id="tool-1", title="Running bash", status="complete", output="Done"
            )

        result = await thread.post(mixed_stream())

        assert len(captured_chunks) == 4
        assert captured_chunks[0] == "Hello "
        assert captured_chunks[2] == "world"
        # task_update chunks should be passed through as objects
        assert captured_chunks[1].type == "task_update"
        assert captured_chunks[3].status == "complete"
        # Accumulated text should only include strings
        assert result.text == "Hello world"

    @pytest.mark.asyncio
    async def test_fallback_text_only_with_chunks(self, mock_adapter, mock_state):
        """In fallback mode, only text content is used, chunk objects are ignored."""
        mock_adapter.stream = None
        thread = _make_thread(mock_adapter, mock_state)

        async def mixed_stream() -> AsyncIterator[Any]:
            yield "Hello"
            yield {"type": "task_update", "id": "tool-1", "title": "Running", "status": "in_progress"}
            yield " World"

        await thread.post(mixed_stream())

        assert mock_adapter._post_calls[0] == ("slack:C123:1234.5678", "...")
        last_edit = mock_adapter._edit_calls[-1]
        assert isinstance(last_edit[2], PostableMarkdown)
        assert last_edit[2].markdown == "Hello World"


# ============================================================================
# Message iteration
# ============================================================================


class TestThreadMessages:
    """Tests for thread.messages() and thread.all_messages()."""

    @pytest.mark.asyncio
    async def test_messages_newest_first(self, mock_adapter, mock_state):
        msgs = [
            create_test_message("msg-1", "Oldest"),
            create_test_message("msg-2", "Middle"),
            create_test_message("msg-3", "Newest"),
        ]
        mock_adapter.fetch_messages = AsyncMock(return_value=FetchResult(messages=msgs, next_cursor=None))
        thread = _make_thread(mock_adapter, mock_state)

        collected: list[Message] = []
        async for msg in thread.messages():
            collected.append(msg)

        assert len(collected) == 3
        # Reversed: newest first
        assert collected[0].text == "Newest"
        assert collected[1].text == "Middle"
        assert collected[2].text == "Oldest"

    @pytest.mark.asyncio
    async def test_messages_backward_direction(self, mock_adapter, mock_state):
        mock_adapter.fetch_messages = AsyncMock(return_value=FetchResult(messages=[], next_cursor=None))
        thread = _make_thread(mock_adapter, mock_state)

        async for _ in thread.messages():
            pass

        call_args = mock_adapter.fetch_messages.call_args
        assert call_args[0][1].direction == "backward"

    @pytest.mark.asyncio
    async def test_all_messages_chronological_order(self, mock_adapter, mock_state):
        msgs = [
            create_test_message("msg-1", "First message"),
            create_test_message("msg-2", "Second message"),
            create_test_message("msg-3", "Third message"),
        ]
        mock_adapter.fetch_messages = AsyncMock(return_value=FetchResult(messages=msgs, next_cursor=None))
        thread = _make_thread(mock_adapter, mock_state)

        collected: list[Message] = []
        async for msg in thread.all_messages():
            collected.append(msg)

        assert len(collected) == 3
        assert collected[0].text == "First message"
        assert collected[1].text == "Second message"
        assert collected[2].text == "Third message"

    @pytest.mark.asyncio
    async def test_all_messages_forward_direction(self, mock_adapter, mock_state):
        mock_adapter.fetch_messages = AsyncMock(return_value=FetchResult(messages=[], next_cursor=None))
        thread = _make_thread(mock_adapter, mock_state)

        async for _ in thread.all_messages():
            pass

        call_args = mock_adapter.fetch_messages.call_args
        assert call_args[0][1].direction == "forward"
        assert call_args[0][1].limit == 100

    @pytest.mark.asyncio
    async def test_all_messages_pagination(self, mock_adapter, mock_state):
        page1 = [
            create_test_message("msg-1", "Page 1 - Message 1"),
            create_test_message("msg-2", "Page 1 - Message 2"),
        ]
        page2 = [
            create_test_message("msg-3", "Page 2 - Message 1"),
            create_test_message("msg-4", "Page 2 - Message 2"),
        ]
        page3 = [create_test_message("msg-5", "Page 3 - Message 1")]

        call_count = 0

        async def mock_fetch(thread_id: str, options: Any = None) -> FetchResult:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return FetchResult(messages=page1, next_cursor="cursor-1")
            if call_count == 2:
                return FetchResult(messages=page2, next_cursor="cursor-2")
            return FetchResult(messages=page3, next_cursor=None)

        mock_adapter.fetch_messages = mock_fetch
        thread = _make_thread(mock_adapter, mock_state)

        collected: list[Message] = []
        async for msg in thread.all_messages():
            collected.append(msg)

        assert len(collected) == 5
        assert [m.text for m in collected] == [
            "Page 1 - Message 1",
            "Page 1 - Message 2",
            "Page 2 - Message 1",
            "Page 2 - Message 2",
            "Page 3 - Message 1",
        ]
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_empty_thread(self, mock_adapter, mock_state):
        mock_adapter.fetch_messages = AsyncMock(return_value=FetchResult(messages=[], next_cursor=None))
        thread = _make_thread(mock_adapter, mock_state)

        collected: list[Message] = []
        async for msg in thread.all_messages():
            collected.append(msg)

        assert len(collected) == 0

    @pytest.mark.asyncio
    async def test_stop_on_empty_page_with_cursor(self, mock_adapter, mock_state):
        """Edge case: adapter returns cursor but no messages."""
        mock_adapter.fetch_messages = AsyncMock(return_value=FetchResult(messages=[], next_cursor="some-cursor"))
        thread = _make_thread(mock_adapter, mock_state)

        collected: list[Message] = []
        async for msg in thread.all_messages():
            collected.append(msg)

        assert len(collected) == 0

    @pytest.mark.asyncio
    async def test_break_early(self, mock_adapter, mock_state):
        page = [
            create_test_message("msg-1", "Message 1"),
            create_test_message("msg-2", "Message 2"),
        ]
        mock_adapter.fetch_messages = AsyncMock(return_value=FetchResult(messages=page, next_cursor="more-available"))
        thread = _make_thread(mock_adapter, mock_state)

        collected: list[Message] = []
        async for msg in thread.all_messages():
            collected.append(msg)
            if msg.id == "msg-1":
                break

        assert len(collected) == 1
        assert collected[0].id == "msg-1"


# ============================================================================
# Subscription
# ============================================================================


class TestThreadSubscription:
    """Tests for thread subscription management."""

    @pytest.mark.asyncio
    async def test_not_subscribed_by_default(self, mock_adapter, mock_state):
        thread = _make_thread(mock_adapter, mock_state)
        assert await thread.is_subscribed() is False

    @pytest.mark.asyncio
    async def test_subscribe(self, mock_adapter, mock_state):
        thread = _make_thread(mock_adapter, mock_state)
        await thread.subscribe()
        assert await thread.is_subscribed() is True

    @pytest.mark.asyncio
    async def test_unsubscribe(self, mock_adapter, mock_state):
        thread = _make_thread(mock_adapter, mock_state)
        await thread.subscribe()
        await thread.unsubscribe()
        assert await thread.is_subscribed() is False

    @pytest.mark.asyncio
    async def test_subscribed_context_always_true(self, mock_adapter, mock_state):
        thread = ThreadImpl(
            _ThreadImplConfig(
                id="slack:C123:1234.5678",
                adapter=mock_adapter,
                state_adapter=mock_state,
                is_subscribed_context=True,
            )
        )
        assert await thread.is_subscribed() is True


# ============================================================================
# Refresh
# ============================================================================


class TestThreadRefresh:
    """Tests for thread.refresh()."""

    @pytest.mark.asyncio
    async def test_updates_recent_messages(self, mock_adapter, mock_state):
        msgs = [
            create_test_message("msg-1", "Recent 1"),
            create_test_message("msg-2", "Recent 2"),
        ]
        mock_adapter.fetch_messages = AsyncMock(return_value=FetchResult(messages=msgs, next_cursor=None))
        thread = _make_thread(mock_adapter, mock_state)

        assert len(thread.recent_messages) == 0
        await thread.refresh()

        assert len(thread.recent_messages) == 2
        assert thread.recent_messages[0].text == "Recent 1"
        assert thread.recent_messages[1].text == "Recent 2"

    @pytest.mark.asyncio
    async def test_refresh_with_limit_50(self, mock_adapter, mock_state):
        mock_adapter.fetch_messages = AsyncMock(return_value=FetchResult(messages=[], next_cursor=None))
        thread = _make_thread(mock_adapter, mock_state)

        await thread.refresh()

        call_args = mock_adapter.fetch_messages.call_args
        assert call_args[0][1].limit == 50


# ============================================================================
# Serialization
# ============================================================================


class TestThreadSerialization:
    """Tests for ThreadImpl.to_json() and from_json()."""

    def test_serialize_with_correct_type_tag(self, mock_adapter, mock_state):
        thread = _make_thread(mock_adapter, mock_state)
        data = thread.to_json()

        assert data["_type"] == "chat:Thread"
        assert data["id"] == "slack:C123:1234.5678"
        assert data["channel_id"] == "C123"
        assert data["channel_visibility"] == "unknown"
        assert data["current_message"] is None
        assert data["is_dm"] is False
        assert data["adapter_name"] == "slack"

    def test_serialize_dm_thread(self, mock_adapter, mock_state):
        thread = _make_thread(mock_adapter, mock_state, is_dm=True, thread_id="slack:DU123:")
        data = thread.to_json()

        assert data["_type"] == "chat:Thread"
        assert data["is_dm"] is True

    def test_serialize_external_channel(self, mock_adapter, mock_state):
        thread = _make_thread(mock_adapter, mock_state, channel_visibility="external")
        data = thread.to_json()
        assert data["channel_visibility"] == "external"

    def test_serialize_private_channel(self, mock_adapter, mock_state):
        thread = _make_thread(mock_adapter, mock_state, channel_visibility="private")
        data = thread.to_json()
        assert data["channel_visibility"] == "private"

    def test_serialize_workspace_channel(self, mock_adapter, mock_state):
        thread = _make_thread(mock_adapter, mock_state, channel_visibility="workspace")
        data = thread.to_json()
        assert data["channel_visibility"] == "workspace"

    def test_json_serializable(self, mock_adapter, mock_state):
        import json

        adapter = create_mock_adapter("teams")
        thread = _make_thread(adapter, mock_state, thread_id="teams:channel123:thread456")
        data = thread.to_json()

        stringified = json.dumps(data)
        parsed = json.loads(stringified)
        assert parsed == data

    def test_from_json_reconstruct(self, mock_adapter, mock_state):
        data = {
            "_type": "chat:Thread",
            "id": "slack:C123:1234.5678",
            "channel_id": "C123",
            "is_dm": False,
            "adapter_name": "slack",
        }
        thread = ThreadImpl.from_json(data, adapter=mock_adapter)

        assert thread.id == "slack:C123:1234.5678"
        assert thread.channel_id == "C123"
        assert thread.is_dm is False
        assert thread.adapter.name == "slack"

    def test_from_json_dm_thread(self, mock_adapter, mock_state):
        data = {
            "_type": "chat:Thread",
            "id": "slack:DU456:",
            "channel_id": "DU456",
            "is_dm": True,
            "adapter_name": "slack",
        }
        thread = ThreadImpl.from_json(data, adapter=mock_adapter)
        assert thread.is_dm is True

    def test_round_trip(self, mock_adapter, mock_state):
        original = _make_thread(mock_adapter, mock_state, is_dm=True)
        data = original.to_json()
        restored = ThreadImpl.from_json(data, adapter=mock_adapter)

        assert restored.id == original.id
        assert restored.channel_id == original.channel_id
        assert restored.is_dm == original.is_dm
        assert restored.adapter.name == original.adapter.name

    def test_round_trip_channel_visibility(self, mock_adapter, mock_state):
        original = _make_thread(mock_adapter, mock_state, channel_visibility="external")
        data = original.to_json()
        restored = ThreadImpl.from_json(data, adapter=mock_adapter)
        assert restored.channel_visibility == "external"

    def test_default_channel_visibility_unknown(self, mock_adapter, mock_state):
        data = {
            "_type": "chat:Thread",
            "id": "slack:C123:1234.5678",
            "channel_id": "C123",
            "is_dm": False,
            "adapter_name": "slack",
        }
        thread = ThreadImpl.from_json(data, adapter=mock_adapter)
        assert thread.channel_visibility == "unknown"

    def test_serialize_current_message(self, mock_adapter, mock_state):
        current_message = create_test_message(
            "msg-1",
            "Hello",
            raw={"team_id": "T123"},
            author=Author(
                user_id="U456",
                user_name="user",
                full_name="Test User",
                is_bot=False,
                is_me=False,
            ),
        )
        thread = _make_thread(mock_adapter, mock_state, current_message=current_message)
        data = thread.to_json()

        assert data["current_message"] is not None
        assert data["current_message"]["_type"] == "chat:Message"
        assert data["current_message"]["author"]["user_id"] == "U456"
        assert data["current_message"]["raw"] == {"team_id": "T123"}

    def test_round_trip_with_current_message(self, mock_adapter, mock_state):
        current_message = create_test_message(
            "msg-1",
            "Hello",
            raw={"team_id": "T123"},
            author=Author(
                user_id="U456",
                user_name="user",
                full_name="Test User",
                is_bot=False,
                is_me=False,
            ),
        )
        original = _make_thread(mock_adapter, mock_state, current_message=current_message)
        data = original.to_json()
        restored = ThreadImpl.from_json(data, adapter=mock_adapter)

        assert restored.id == original.id
        assert restored.channel_id == original.channel_id


# ============================================================================
# Construction
# ============================================================================


class TestThreadConstruction:
    """Tests for basic ThreadImpl construction and properties."""

    def test_basic_properties(self, mock_adapter, mock_state):
        thread = _make_thread(mock_adapter, mock_state)

        assert thread.id == "slack:C123:1234.5678"
        assert thread.channel_id == "C123"
        assert thread.is_dm is False
        assert thread.channel_visibility == "unknown"
        assert thread.adapter.name == "slack"

    def test_dm_thread(self, mock_adapter, mock_state):
        thread = _make_thread(mock_adapter, mock_state, is_dm=True)
        assert thread.is_dm is True

    def test_initial_message_in_recent(self, mock_adapter, mock_state):
        msg = create_test_message("msg-init", "Initial")
        thread = ThreadImpl(
            _ThreadImplConfig(
                id="slack:C123:1234.5678",
                adapter=mock_adapter,
                state_adapter=mock_state,
                initial_message=msg,
            )
        )
        assert len(thread.recent_messages) == 1
        assert thread.recent_messages[0].text == "Initial"

    def test_mention_user(self, mock_adapter, mock_state):
        thread = _make_thread(mock_adapter, mock_state)
        assert thread.mention_user("U123") == "<@U123>"
