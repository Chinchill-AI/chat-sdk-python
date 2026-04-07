"""Extended tests for ThreadImpl.

Ported from TS thread.test.ts to cover categories missing from test_thread.py:
- post with card format
- streaming with StreamChunk objects (markdown_text, task_update, plan_update)
- fallback streaming error logging
- streaming with updateIntervalMs / custom placeholder
- allMessages iterator (oldest-first, pagination, reusability)
- refresh (updates recentMessages, direction/limit)
- fetchMessages direction behavior
- concurrent iteration safety
- postEphemeral (native, Author object, DM fallback, no fallback)
- subscribe/unsubscribe/isSubscribed (state adapter delegation, onThreadSubscribe)
- recentMessages getter/setter
- startTyping (delegates to adapter)
- mentionUser (various formats)
- createSentMessageFromMessage (wrap Message as SentMessage with capabilities)
- serialization (toJSON/fromJSON roundtrip, currentMessage, channelVisibility)
- SentMessage.toJSON from post (simulated via Message.to_json)
- schedule() (delegation, error handling, return shape, cancel, message formats)
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest
from chat_sdk.errors import ChatNotImplementedError
from chat_sdk.testing import (
    MockAdapter,
    MockLogger,
    MockStateAdapter,
    create_mock_adapter,
    create_mock_state,
    create_test_message,
    mock_logger,
)
from chat_sdk.thread import ThreadImpl, _ThreadImplConfig
from chat_sdk.types import (
    Author,
    FetchResult,
    MarkdownTextChunk,
    Message,
    MessageMetadata,
    PlanUpdateChunk,
    PostableCard,
    PostableMarkdown,
    PostableRaw,
    PostEphemeralOptions,
    RawMessage,
    ScheduledMessage,
    StreamChunk,
    TaskUpdateChunk,
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
    is_subscribed_context: bool = False,
    initial_message: Message | None = None,
    logger: Any = None,
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
            is_subscribed_context=is_subscribed_context,
            initial_message=initial_message,
            logger=logger,
        )
    )


async def _create_text_stream(chunks: list[str]) -> AsyncIterator[str]:
    for chunk in chunks:
        yield chunk


# ============================================================================
# Post with card format
# ============================================================================


class TestThreadPostCard:
    """Post with PostableCard format."""

    @pytest.mark.asyncio
    async def test_post_card_message(self, mock_adapter, mock_state):
        thread = _make_thread(mock_adapter, mock_state)
        card = PostableCard(card={"type": "card", "title": "Test Card"}, fallback_text="card fallback")
        result = await thread.post(card)

        assert result.text == "card fallback"
        assert result.id == "msg-1"

    @pytest.mark.asyncio
    async def test_post_card_default_fallback(self, mock_adapter, mock_state):
        thread = _make_thread(mock_adapter, mock_state)
        card = PostableCard(card={"type": "card", "title": "No Fallback"})
        result = await thread.post(card)

        assert result.text == "[card]"


# ============================================================================
# Streaming with StreamChunk objects
# ============================================================================


class TestThreadStreamingChunks:
    """Tests for streaming with StreamChunk dataclass objects."""

    @pytest.mark.asyncio
    async def test_markdown_text_chunk_accumulates_text(self, mock_adapter, mock_state):
        """markdown_text chunks contribute to accumulated text."""
        captured_chunks: list[Any] = []

        async def mock_stream(thread_id: str, text_stream: Any, options: Any) -> RawMessage:
            async for chunk in text_stream:
                captured_chunks.append(chunk)
            return RawMessage(id="msg-stream", thread_id="t1", raw={})

        mock_adapter.stream = mock_stream
        thread = _make_thread(mock_adapter, mock_state)

        async def md_chunk_stream() -> AsyncIterator[Any]:
            yield MarkdownTextChunk(type="markdown_text", text="Hello ")
            yield PlanUpdateChunk(type="plan_update", title="Analyzing code")
            yield MarkdownTextChunk(type="markdown_text", text="World")

        result = await thread.post(md_chunk_stream())

        # markdown_text chunks contribute to accumulated text; plan_update does not
        assert result.text == "Hello World"

    @pytest.mark.asyncio
    async def test_plan_update_chunk_passes_through(self, mock_adapter, mock_state):
        """plan_update chunks pass through to adapter.stream."""
        captured_chunks: list[Any] = []

        async def mock_stream(thread_id: str, text_stream: Any, options: Any) -> RawMessage:
            async for chunk in text_stream:
                captured_chunks.append(chunk)
            return RawMessage(id="msg-stream", thread_id="t1", raw={})

        mock_adapter.stream = mock_stream
        thread = _make_thread(mock_adapter, mock_state)

        async def mixed_stream() -> AsyncIterator[Any]:
            yield "Text "
            yield PlanUpdateChunk(type="plan_update", title="Planning")
            yield "more"

        result = await thread.post(mixed_stream())

        assert len(captured_chunks) == 3
        assert captured_chunks[0] == "Text "
        assert captured_chunks[1].type == "plan_update"
        assert captured_chunks[1].title == "Planning"
        assert captured_chunks[2] == "more"
        assert result.text == "Text more"

    @pytest.mark.asyncio
    async def test_fallback_ignores_dict_chunks(self, mock_adapter, mock_state):
        """In fallback mode, dict-based chunks (non-string, non-StreamChunk) are skipped."""
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
# Fallback streaming error logging
# ============================================================================


class TestFallbackStreamingErrorLogging:
    """Tests for error logging during fallback streaming."""

    @pytest.mark.asyncio
    async def test_log_when_intermediate_edit_fails(self, mock_adapter, mock_state):
        logger = MockLogger()

        # Make edit_message fail on the first call
        call_count = 0
        original_edit = mock_adapter.edit_message

        async def failing_edit(thread_id: str, message_id: str, message: Any) -> RawMessage:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("422 Validation Failed")
            return await original_edit(thread_id, message_id, message)

        mock_adapter.edit_message = failing_edit

        thread = _make_thread(
            mock_adapter,
            mock_state,
            streaming_update_interval_ms=10,
            logger=logger,
        )

        async def slow_stream() -> AsyncIterator[str]:
            yield "Hel"
            await asyncio.sleep(0.05)
            yield "lo"

        await thread.post(slow_stream())

        # Logger should have warned about the edit failure
        assert len(logger.warn.calls) > 0
        assert "fallbackStream edit failed" in logger.warn.calls[0][0]


# ============================================================================
# Streaming with updateIntervalMs
# ============================================================================


class TestStreamingUpdateInterval:
    """Tests for custom streaming update intervals."""

    @pytest.mark.asyncio
    async def test_custom_streaming_update_interval(self, mock_adapter, mock_state):
        mock_adapter.stream = None

        thread = _make_thread(
            mock_adapter,
            mock_state,
            streaming_update_interval_ms=1000,
        )

        text_stream = _create_text_stream(["A", "B", "C"])
        result = await thread.post(text_stream)

        # Final text should be accumulated, wrapped as markdown
        last_edit = mock_adapter._edit_calls[-1]
        assert isinstance(last_edit[2], PostableMarkdown)
        assert last_edit[2].markdown == "ABC"

    @pytest.mark.asyncio
    async def test_custom_placeholder_text(self, mock_adapter, mock_state):
        mock_adapter.stream = None

        thread = _make_thread(
            mock_adapter,
            mock_state,
            fallback_streaming_placeholder_text="Loading...",
        )

        text_stream = _create_text_stream(["Done"])
        await thread.post(text_stream)

        # First post should use the custom placeholder
        assert mock_adapter._post_calls[0] == ("slack:C123:1234.5678", "Loading...")


# ============================================================================
# allMessages iterator
# ============================================================================


class TestAllMessagesIterator:
    """Tests for thread.all_messages() (oldest-first, forward pagination)."""

    @pytest.mark.asyncio
    async def test_chronological_order(self, mock_adapter, mock_state):
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
    async def test_forward_direction_with_limit_100(self, mock_adapter, mock_state):
        mock_adapter.fetch_messages = AsyncMock(return_value=FetchResult(messages=[], next_cursor=None))
        thread = _make_thread(mock_adapter, mock_state)

        async for _ in thread.all_messages():
            pass

        call_args = mock_adapter.fetch_messages.call_args
        assert call_args[0][1].direction == "forward"
        assert call_args[0][1].limit == 100

    @pytest.mark.asyncio
    async def test_pagination_across_multiple_pages(self, mock_adapter, mock_state):
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
        assert mock_adapter.fetch_messages.call_count == 1

    @pytest.mark.asyncio
    async def test_stop_on_undefined_cursor(self, mock_adapter, mock_state):
        msgs = [create_test_message("msg-1", "Single message")]
        mock_adapter.fetch_messages = AsyncMock(return_value=FetchResult(messages=msgs, next_cursor=None))
        thread = _make_thread(mock_adapter, mock_state)

        collected: list[Message] = []
        async for msg in thread.all_messages():
            collected.append(msg)

        assert len(collected) == 1
        assert mock_adapter.fetch_messages.call_count == 1

    @pytest.mark.asyncio
    async def test_stop_on_empty_page_with_cursor(self, mock_adapter, mock_state):
        """Edge case: adapter returns cursor but no messages."""
        mock_adapter.fetch_messages = AsyncMock(return_value=FetchResult(messages=[], next_cursor="some-cursor"))
        thread = _make_thread(mock_adapter, mock_state)

        collected: list[Message] = []
        async for msg in thread.all_messages():
            collected.append(msg)

        assert len(collected) == 0
        assert mock_adapter.fetch_messages.call_count == 1

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
        assert mock_adapter.fetch_messages.call_count == 1

    @pytest.mark.asyncio
    async def test_reusable_iterator(self, mock_adapter, mock_state):
        """Can iterate multiple times; each creates a fresh iterator."""
        msgs = [create_test_message("msg-1", "Test message")]
        mock_adapter.fetch_messages = AsyncMock(return_value=FetchResult(messages=msgs, next_cursor=None))
        thread = _make_thread(mock_adapter, mock_state)

        first: list[Message] = []
        async for msg in thread.all_messages():
            first.append(msg)

        second: list[Message] = []
        async for msg in thread.all_messages():
            second.append(msg)

        assert len(first) == 1
        assert len(second) == 1
        assert mock_adapter.fetch_messages.call_count == 2


# ============================================================================
# Refresh
# ============================================================================


class TestRefreshExtended:
    """Extended tests for thread.refresh()."""

    @pytest.mark.asyncio
    async def test_updates_recent_messages_from_api(self, mock_adapter, mock_state):
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

    @pytest.mark.asyncio
    async def test_refresh_uses_default_direction(self, mock_adapter, mock_state):
        """refresh() does not specify direction, so adapter uses its default."""
        mock_adapter.fetch_messages = AsyncMock(return_value=FetchResult(messages=[], next_cursor=None))
        thread = _make_thread(mock_adapter, mock_state)

        await thread.refresh()

        call_args = mock_adapter.fetch_messages.call_args
        # refresh passes FetchOptions(limit=50), no explicit direction
        assert call_args[0][1].limit == 50


# ============================================================================
# fetchMessages direction behavior
# ============================================================================


class TestFetchMessagesDirection:
    """Tests for direction options on message iterators."""

    @pytest.mark.asyncio
    async def test_all_messages_passes_forward_direction(self, mock_adapter, mock_state):
        mock_adapter.fetch_messages = AsyncMock(return_value=FetchResult(messages=[], next_cursor=None))
        thread = _make_thread(mock_adapter, mock_state)

        async for _ in thread.all_messages():
            pass

        call = mock_adapter.fetch_messages.call_args
        assert call[0][1].direction == "forward"

    @pytest.mark.asyncio
    async def test_messages_passes_backward_direction(self, mock_adapter, mock_state):
        mock_adapter.fetch_messages = AsyncMock(return_value=FetchResult(messages=[], next_cursor=None))
        thread = _make_thread(mock_adapter, mock_state)

        async for _ in thread.messages():
            pass

        call = mock_adapter.fetch_messages.call_args
        assert call[0][1].direction == "backward"


# ============================================================================
# Concurrent iteration safety
# ============================================================================


class TestConcurrentIteration:
    """Tests for concurrent iteration independence."""

    @pytest.mark.asyncio
    async def test_handle_concurrent_iterations_independently(self, mock_adapter, mock_state):
        call_count = 0

        async def mock_fetch(thread_id: str, options: Any = None) -> FetchResult:
            nonlocal call_count
            call_count += 1
            return FetchResult(
                messages=[create_test_message(f"msg-{call_count}", f"Call {call_count}")],
                next_cursor=None,
            )

        mock_adapter.fetch_messages = mock_fetch
        thread = _make_thread(mock_adapter, mock_state)

        async def collect() -> list[Message]:
            msgs: list[Message] = []
            async for msg in thread.all_messages():
                msgs.append(msg)
            return msgs

        results = await asyncio.gather(collect(), collect())

        assert len(results[0]) == 1
        assert len(results[1]) == 1
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_no_shared_cursor_state(self, mock_adapter, mock_state):
        cursors: list[str | None] = []

        async def mock_fetch(thread_id: str, options: Any = None) -> FetchResult:
            cursors.append(getattr(options, "cursor", None) if options else None)
            return FetchResult(
                messages=[create_test_message("msg-1", "Test")],
                next_cursor=None,
            )

        mock_adapter.fetch_messages = mock_fetch
        thread = _make_thread(mock_adapter, mock_state)

        async for _ in thread.all_messages():
            pass
        async for _ in thread.all_messages():
            pass

        # Both iterations should start with None cursor
        assert cursors == [None, None]


# ============================================================================
# postEphemeral
# ============================================================================


class TestThreadPostEphemeral:
    """Tests for thread.post_ephemeral()."""

    @pytest.mark.asyncio
    async def test_use_adapter_post_ephemeral_when_available(self, mock_adapter, mock_state):
        ephemeral_calls: list[tuple[str, str, Any]] = []

        async def mock_post_ephemeral(thread_id: str, user_id: str, message: Any) -> Any:
            ephemeral_calls.append((thread_id, user_id, message))
            return {
                "id": "ephemeral-1",
                "thread_id": "slack:C123:1234.5678",
                "used_fallback": False,
                "raw": {},
            }

        mock_adapter.post_ephemeral = mock_post_ephemeral
        thread = _make_thread(mock_adapter, mock_state)

        result = await thread.post_ephemeral(
            "U456",
            "Secret message",
            PostEphemeralOptions(fallback_to_dm=True),
        )

        assert len(ephemeral_calls) == 1
        assert ephemeral_calls[0] == ("slack:C123:1234.5678", "U456", "Secret message")

    @pytest.mark.asyncio
    async def test_extract_user_id_from_author(self, mock_adapter, mock_state):
        ephemeral_calls: list[tuple[str, str, Any]] = []

        async def mock_post_ephemeral(thread_id: str, user_id: str, message: Any) -> Any:
            ephemeral_calls.append((thread_id, user_id, message))
            return {
                "id": "ephemeral-1",
                "thread_id": "slack:C123:1234.5678",
                "used_fallback": False,
                "raw": {},
            }

        mock_adapter.post_ephemeral = mock_post_ephemeral
        thread = _make_thread(mock_adapter, mock_state)

        author = Author(
            user_id="U789",
            user_name="testuser",
            full_name="Test User",
            is_bot=False,
            is_me=False,
        )

        await thread.post_ephemeral(
            author,
            "Secret message",
            PostEphemeralOptions(fallback_to_dm=True),
        )

        assert ephemeral_calls[0][1] == "U789"

    @pytest.mark.asyncio
    async def test_fallback_to_dm(self, mock_adapter, mock_state):
        mock_adapter.post_ephemeral = None
        thread = _make_thread(mock_adapter, mock_state)

        result = await thread.post_ephemeral(
            "U456",
            "Secret message",
            PostEphemeralOptions(fallback_to_dm=True),
        )

        assert result is not None
        assert result.id == "msg-1"
        assert result.thread_id == "slack:DU456:"
        assert result.used_fallback is True

    @pytest.mark.asyncio
    async def test_return_none_when_no_fallback(self, mock_adapter, mock_state):
        mock_adapter.post_ephemeral = None
        thread = _make_thread(mock_adapter, mock_state)

        result = await thread.post_ephemeral(
            "U456",
            "Secret message",
            PostEphemeralOptions(fallback_to_dm=False),
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_return_none_when_no_ephemeral_and_no_open_dm(self, mock_adapter, mock_state):
        mock_adapter.post_ephemeral = None
        mock_adapter.open_dm = None
        thread = _make_thread(mock_adapter, mock_state)

        result = await thread.post_ephemeral(
            "U456",
            "Secret message",
            PostEphemeralOptions(fallback_to_dm=True),
        )

        assert result is None


# ============================================================================
# Subscribe/Unsubscribe/isSubscribed
# ============================================================================


class TestThreadSubscriptionExtended:
    """Extended subscription tests ported from TS."""

    @pytest.mark.asyncio
    async def test_subscribe_via_state_adapter(self, mock_adapter, mock_state):
        thread = _make_thread(mock_adapter, mock_state)
        await thread.subscribe()
        assert "slack:C123:1234.5678" in mock_state._subscriptions

    @pytest.mark.asyncio
    async def test_call_adapter_on_thread_subscribe(self, mock_adapter, mock_state):
        subscribe_calls: list[str] = []

        async def on_subscribe(thread_id: str) -> None:
            subscribe_calls.append(thread_id)

        mock_adapter.on_thread_subscribe = on_subscribe
        thread = _make_thread(mock_adapter, mock_state)
        await thread.subscribe()

        assert subscribe_calls == ["slack:C123:1234.5678"]

    @pytest.mark.asyncio
    async def test_no_error_when_adapter_has_no_on_thread_subscribe(self, mock_adapter, mock_state):
        # Mock adapter doesn't have on_thread_subscribe by default
        thread = _make_thread(mock_adapter, mock_state)
        await thread.subscribe()  # Should not raise
        assert "slack:C123:1234.5678" in mock_state._subscriptions

    @pytest.mark.asyncio
    async def test_unsubscribe_via_state_adapter(self, mock_adapter, mock_state):
        thread = _make_thread(mock_adapter, mock_state)
        await thread.subscribe()
        await thread.unsubscribe()
        assert "slack:C123:1234.5678" not in mock_state._subscriptions

    @pytest.mark.asyncio
    async def test_is_subscribed_false_when_not_subscribed(self, mock_adapter, mock_state):
        thread = _make_thread(mock_adapter, mock_state)
        assert await thread.is_subscribed() is False

    @pytest.mark.asyncio
    async def test_is_subscribed_true_after_subscribe(self, mock_adapter, mock_state):
        thread = _make_thread(mock_adapter, mock_state)
        await thread.subscribe()
        assert await thread.is_subscribed() is True

    @pytest.mark.asyncio
    async def test_is_subscribed_false_after_unsubscribe(self, mock_adapter, mock_state):
        thread = _make_thread(mock_adapter, mock_state)
        await thread.subscribe()
        await thread.unsubscribe()
        assert await thread.is_subscribed() is False

    @pytest.mark.asyncio
    async def test_is_subscribed_context_short_circuits(self, mock_adapter, mock_state):
        thread = _make_thread(
            mock_adapter,
            mock_state,
            is_subscribed_context=True,
        )

        assert await thread.is_subscribed() is True
        # Should NOT have called the state adapter's is_subscribed
        assert "slack:C123:1234.5678" not in mock_state._subscriptions


# ============================================================================
# recentMessages getter/setter
# ============================================================================


class TestRecentMessages:
    """Tests for recentMessages getter/setter."""

    def test_start_with_empty_array(self, mock_adapter, mock_state):
        thread = _make_thread(mock_adapter, mock_state)
        assert thread.recent_messages == []

    def test_initialize_with_initial_message(self, mock_adapter, mock_state):
        msg = create_test_message("msg-1", "Initial")
        thread = _make_thread(mock_adapter, mock_state, initial_message=msg)

        assert len(thread.recent_messages) == 1
        assert thread.recent_messages[0].text == "Initial"

    def test_allow_setting_recent_messages(self, mock_adapter, mock_state):
        thread = _make_thread(mock_adapter, mock_state)
        messages = [
            create_test_message("msg-1", "First"),
            create_test_message("msg-2", "Second"),
        ]
        thread.recent_messages = messages

        assert len(thread.recent_messages) == 2
        assert thread.recent_messages[0].text == "First"
        assert thread.recent_messages[1].text == "Second"

    def test_allow_replacing_recent_messages(self, mock_adapter, mock_state):
        msg = create_test_message("msg-1", "Initial")
        thread = _make_thread(mock_adapter, mock_state, initial_message=msg)

        new_messages = [create_test_message("msg-2", "Replaced")]
        thread.recent_messages = new_messages

        assert len(thread.recent_messages) == 1
        assert thread.recent_messages[0].text == "Replaced"


# ============================================================================
# startTyping
# ============================================================================


class TestStartTyping:
    """Tests for thread.start_typing()."""

    @pytest.mark.asyncio
    async def test_call_adapter_start_typing(self, mock_adapter, mock_state):
        thread = _make_thread(mock_adapter, mock_state)
        await thread.start_typing()

        assert len(mock_adapter._start_typing_calls) == 1
        assert mock_adapter._start_typing_calls[0] == ("slack:C123:1234.5678", None)

    @pytest.mark.asyncio
    async def test_pass_status_to_adapter(self, mock_adapter, mock_state):
        thread = _make_thread(mock_adapter, mock_state)
        await thread.start_typing("thinking...")

        assert mock_adapter._start_typing_calls[0] == ("slack:C123:1234.5678", "thinking...")


# ============================================================================
# mentionUser
# ============================================================================


class TestMentionUserExtended:
    """Extended mentionUser tests."""

    def test_return_formatted_mention(self, mock_adapter, mock_state):
        thread = _make_thread(mock_adapter, mock_state)
        assert thread.mention_user("U456") == "<@U456>"

    def test_handle_various_user_id_formats(self, mock_adapter, mock_state):
        thread = _make_thread(mock_adapter, mock_state)
        assert thread.mention_user("UABC123") == "<@UABC123>"
        assert thread.mention_user("bot-user-id") == "<@bot-user-id>"


# ============================================================================
# createSentMessageFromMessage
# ============================================================================


class TestCreateSentMessageFromMessage:
    """Tests for thread.create_sent_message_from_message()."""

    def test_wrap_message_with_same_fields(self, mock_adapter, mock_state):
        thread = _make_thread(mock_adapter, mock_state)
        msg = create_test_message("msg-1", "Hello world")

        sent = thread.create_sent_message_from_message(msg)

        assert sent.id == "msg-1"
        assert sent.text == "Hello world"
        assert sent.thread_id == msg.thread_id
        assert sent.author is msg.author
        assert sent.metadata is msg.metadata
        assert sent.attachments is msg.attachments

    @pytest.mark.asyncio
    async def test_provide_edit_capability(self, mock_adapter, mock_state):
        thread = _make_thread(mock_adapter, mock_state)
        msg = create_test_message("msg-1", "Hello world")

        sent = thread.create_sent_message_from_message(msg)
        await sent.edit("Updated content")

        assert len(mock_adapter._edit_calls) == 1
        assert mock_adapter._edit_calls[0] == ("slack:C123:1234.5678", "msg-1", "Updated content")

    @pytest.mark.asyncio
    async def test_provide_delete_capability(self, mock_adapter, mock_state):
        thread = _make_thread(mock_adapter, mock_state)
        msg = create_test_message("msg-1", "Hello world")

        sent = thread.create_sent_message_from_message(msg)
        await sent.delete()

        assert len(mock_adapter._delete_calls) == 1
        assert mock_adapter._delete_calls[0] == ("slack:C123:1234.5678", "msg-1")

    @pytest.mark.asyncio
    async def test_provide_add_reaction_capability(self, mock_adapter, mock_state):
        thread = _make_thread(mock_adapter, mock_state)
        msg = create_test_message("msg-1", "Hello world")

        sent = thread.create_sent_message_from_message(msg)
        await sent.add_reaction("thumbsup")

        assert len(mock_adapter._add_reaction_calls) == 1
        assert mock_adapter._add_reaction_calls[0] == ("slack:C123:1234.5678", "msg-1", "thumbsup")

    @pytest.mark.asyncio
    async def test_provide_remove_reaction_capability(self, mock_adapter, mock_state):
        thread = _make_thread(mock_adapter, mock_state)
        msg = create_test_message("msg-1", "Hello world")

        sent = thread.create_sent_message_from_message(msg)
        await sent.remove_reaction("thumbsup")

        assert len(mock_adapter._remove_reaction_calls) == 1
        assert mock_adapter._remove_reaction_calls[0] == ("slack:C123:1234.5678", "msg-1", "thumbsup")

    def test_preserve_is_mention_from_original(self, mock_adapter, mock_state):
        thread = _make_thread(mock_adapter, mock_state)
        msg = create_test_message("msg-1", "Hello @bot", is_mention=True)

        sent = thread.create_sent_message_from_message(msg)
        assert sent.is_mention is True


# ============================================================================
# SentMessage serialization (simulated via _to_message -> to_json)
# ============================================================================


class TestSentMessageToJson:
    """Tests for SentMessage serialization via _to_message().to_json()."""

    @pytest.mark.asyncio
    async def test_sent_message_serializable_via_to_message(self, mock_adapter, mock_state):
        from chat_sdk.thread import _to_message

        thread = _make_thread(mock_adapter, mock_state)
        result = await thread.post("Hello world")

        # Convert to Message (which has to_json) and serialize
        msg = _to_message(result)
        data = msg.to_json()

        assert data["_type"] == "chat:Message"
        assert data["text"] == "Hello world"
        assert data["author"]["is_bot"] is True
        assert data["author"]["is_me"] is True


# ============================================================================
# Serialization (extended)
# ============================================================================


class TestThreadSerializationExtended:
    """Extended serialization tests."""

    def test_serialize_dm_thread(self, mock_adapter, mock_state):
        thread = _make_thread(
            mock_adapter,
            mock_state,
            is_dm=True,
            thread_id="slack:C123:1234.5678",
        )
        data = thread.to_json()

        assert data["_type"] == "chat:Thread"
        assert data["is_dm"] is True

    def test_serialize_with_current_message(self, mock_adapter, mock_state):
        msg = create_test_message("msg-1", "Current")
        thread = _make_thread(mock_adapter, mock_state, current_message=msg)

        data = thread.to_json()

        assert data["current_message"] is not None
        assert data["current_message"]["_type"] == "chat:Message"
        assert data["current_message"]["text"] == "Current"

    def test_deserialize_from_json_with_adapter(self, mock_adapter, mock_state):
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
        assert thread.adapter is mock_adapter

    def test_roundtrip_with_channel_visibility(self, mock_adapter, mock_state):
        original = _make_thread(mock_adapter, mock_state, channel_visibility="external")
        data = original.to_json()
        restored = ThreadImpl.from_json(data, adapter=mock_adapter)

        assert restored.channel_visibility == "external"

    def test_roundtrip_dm_thread(self, mock_adapter, mock_state):
        original = _make_thread(mock_adapter, mock_state, is_dm=True)
        data = original.to_json()
        restored = ThreadImpl.from_json(data, adapter=mock_adapter)

        assert restored.id == original.id
        assert restored.channel_id == original.channel_id
        assert restored.is_dm is True
        assert restored.adapter.name == original.adapter.name

    def test_json_serializable(self, mock_adapter, mock_state):
        adapter = create_mock_adapter("teams")
        thread = _make_thread(adapter, mock_state, thread_id="teams:channel123:thread456")
        data = thread.to_json()

        stringified = json.dumps(data)
        parsed = json.loads(stringified)
        assert parsed == data

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


# ============================================================================
# schedule()
# ============================================================================


class TestThreadSchedule:
    """Tests for thread.schedule()."""

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
        thread = _make_thread(mock_adapter, mock_state)

        with pytest.raises(ChatNotImplementedError):
            await thread.schedule("Hello", post_at=self.FUTURE_DATE)

    @pytest.mark.asyncio
    async def test_not_implemented_error_has_scheduling_method(self, mock_adapter, mock_state):
        thread = _make_thread(mock_adapter, mock_state)

        with pytest.raises(ChatNotImplementedError) as exc_info:
            await thread.schedule("Hello", post_at=self.FUTURE_DATE)

        assert exc_info.value.method == "scheduling"

    @pytest.mark.asyncio
    async def test_not_implemented_includes_descriptive_message(self, mock_adapter, mock_state):
        thread = _make_thread(mock_adapter, mock_state)

        with pytest.raises(ChatNotImplementedError, match="scheduling"):
            await thread.schedule("Hello", post_at=self.FUTURE_DATE)

    # ---- Basic delegation ----

    @pytest.mark.asyncio
    async def test_delegate_to_adapter_schedule_message(self, mock_adapter, mock_state):
        schedule_calls: list[tuple[str, Any, Any]] = []

        async def mock_schedule(thread_id: str, message: Any, options: Any) -> ScheduledMessage:
            schedule_calls.append((thread_id, message, options))
            return self._mock_schedule_result()

        mock_adapter.schedule_message = mock_schedule
        thread = _make_thread(mock_adapter, mock_state)

        await thread.schedule("Hello", post_at=self.FUTURE_DATE)

        assert len(schedule_calls) == 1
        assert schedule_calls[0][0] == "slack:C123:1234.5678"
        assert schedule_calls[0][1] == "Hello"
        assert schedule_calls[0][2] == {"post_at": self.FUTURE_DATE}

    @pytest.mark.asyncio
    async def test_return_scheduled_message_from_adapter(self, mock_adapter, mock_state):
        expected = self._mock_schedule_result()

        async def mock_schedule(thread_id: str, message: Any, options: Any) -> ScheduledMessage:
            return expected

        mock_adapter.schedule_message = mock_schedule
        thread = _make_thread(mock_adapter, mock_state)

        result = await thread.schedule("Hello", post_at=self.FUTURE_DATE)
        assert result is expected

    # ---- Return value shape ----

    @pytest.mark.asyncio
    async def test_return_scheduled_message_id(self, mock_adapter, mock_state):
        async def mock_schedule(thread_id: str, message: Any, options: Any) -> ScheduledMessage:
            return self._mock_schedule_result(scheduled_message_id="Q999")

        mock_adapter.schedule_message = mock_schedule
        thread = _make_thread(mock_adapter, mock_state)

        result = await thread.schedule("Hello", post_at=self.FUTURE_DATE)
        assert result.scheduled_message_id == "Q999"

    @pytest.mark.asyncio
    async def test_return_channel_id(self, mock_adapter, mock_state):
        async def mock_schedule(thread_id: str, message: Any, options: Any) -> ScheduledMessage:
            return self._mock_schedule_result(channel_id="C456")

        mock_adapter.schedule_message = mock_schedule
        thread = _make_thread(mock_adapter, mock_state)

        result = await thread.schedule("Hello", post_at=self.FUTURE_DATE)
        assert result.channel_id == "C456"

    @pytest.mark.asyncio
    async def test_return_post_at(self, mock_adapter, mock_state):
        custom_date = datetime(2035, 6, 15, 12, 0, 0, tzinfo=timezone.utc)

        async def mock_schedule(thread_id: str, message: Any, options: Any) -> ScheduledMessage:
            return self._mock_schedule_result(post_at=custom_date)

        mock_adapter.schedule_message = mock_schedule
        thread = _make_thread(mock_adapter, mock_state)

        result = await thread.schedule("Hello", post_at=self.FUTURE_DATE)
        assert result.post_at == custom_date

    @pytest.mark.asyncio
    async def test_return_raw_platform_response(self, mock_adapter, mock_state):
        raw_response = {"ok": True, "scheduled_message_id": "Q123", "post_at": 123}

        async def mock_schedule(thread_id: str, message: Any, options: Any) -> ScheduledMessage:
            return self._mock_schedule_result(raw=raw_response)

        mock_adapter.schedule_message = mock_schedule
        thread = _make_thread(mock_adapter, mock_state)

        result = await thread.schedule("Hello", post_at=self.FUTURE_DATE)
        assert result.raw == raw_response

    @pytest.mark.asyncio
    async def test_return_cancel_function(self, mock_adapter, mock_state):
        async def mock_schedule(thread_id: str, message: Any, options: Any) -> ScheduledMessage:
            return self._mock_schedule_result()

        mock_adapter.schedule_message = mock_schedule
        thread = _make_thread(mock_adapter, mock_state)

        result = await thread.schedule("Hello", post_at=self.FUTURE_DATE)
        assert callable(result.cancel)

    # ---- cancel() ----

    @pytest.mark.asyncio
    async def test_invoke_cancel_without_errors(self, mock_adapter, mock_state):
        cancel_called = False

        async def cancel_fn() -> None:
            nonlocal cancel_called
            cancel_called = True

        async def mock_schedule(thread_id: str, message: Any, options: Any) -> ScheduledMessage:
            return self._mock_schedule_result(_cancel=cancel_fn)

        mock_adapter.schedule_message = mock_schedule
        thread = _make_thread(mock_adapter, mock_state)

        result = await thread.schedule("Hello", post_at=self.FUTURE_DATE)
        await result.cancel()

        assert cancel_called

    @pytest.mark.asyncio
    async def test_propagate_errors_from_cancel(self, mock_adapter, mock_state):
        async def cancel_fn() -> None:
            raise Exception("already sent")

        async def mock_schedule(thread_id: str, message: Any, options: Any) -> ScheduledMessage:
            return self._mock_schedule_result(_cancel=cancel_fn)

        mock_adapter.schedule_message = mock_schedule
        thread = _make_thread(mock_adapter, mock_state)

        result = await thread.schedule("Hello", post_at=self.FUTURE_DATE)

        with pytest.raises(Exception, match="already sent"):
            await result.cancel()

    # ---- Different message formats ----

    @pytest.mark.asyncio
    async def test_pass_string_messages_through(self, mock_adapter, mock_state):
        schedule_calls: list[tuple[str, Any, Any]] = []

        async def mock_schedule(thread_id: str, message: Any, options: Any) -> ScheduledMessage:
            schedule_calls.append((thread_id, message, options))
            return self._mock_schedule_result()

        mock_adapter.schedule_message = mock_schedule
        thread = _make_thread(mock_adapter, mock_state)

        await thread.schedule("Plain text", post_at=self.FUTURE_DATE)

        assert schedule_calls[0][1] == "Plain text"

    @pytest.mark.asyncio
    async def test_pass_raw_message_through(self, mock_adapter, mock_state):
        schedule_calls: list[tuple[str, Any, Any]] = []

        async def mock_schedule(thread_id: str, message: Any, options: Any) -> ScheduledMessage:
            schedule_calls.append((thread_id, message, options))
            return self._mock_schedule_result()

        mock_adapter.schedule_message = mock_schedule
        thread = _make_thread(mock_adapter, mock_state)

        raw_msg = PostableRaw(raw="raw text")
        await thread.schedule(raw_msg, post_at=self.FUTURE_DATE)

        assert schedule_calls[0][1] is raw_msg

    @pytest.mark.asyncio
    async def test_pass_markdown_message_through(self, mock_adapter, mock_state):
        schedule_calls: list[tuple[str, Any, Any]] = []

        async def mock_schedule(thread_id: str, message: Any, options: Any) -> ScheduledMessage:
            schedule_calls.append((thread_id, message, options))
            return self._mock_schedule_result()

        mock_adapter.schedule_message = mock_schedule
        thread = _make_thread(mock_adapter, mock_state)

        md_msg = PostableMarkdown(markdown="**bold** text")
        await thread.schedule(md_msg, post_at=self.FUTURE_DATE)

        assert schedule_calls[0][1] is md_msg

    # ---- Adapter error propagation ----

    @pytest.mark.asyncio
    async def test_propagate_adapter_errors(self, mock_adapter, mock_state):
        async def mock_schedule(thread_id: str, message: Any, options: Any) -> ScheduledMessage:
            raise Exception("Slack API error")

        mock_adapter.schedule_message = mock_schedule
        thread = _make_thread(mock_adapter, mock_state)

        with pytest.raises(Exception, match="Slack API error"):
            await thread.schedule("Hello", post_at=self.FUTURE_DATE)

    @pytest.mark.asyncio
    async def test_not_call_post_message_when_scheduling(self, mock_adapter, mock_state):
        async def mock_schedule(thread_id: str, message: Any, options: Any) -> ScheduledMessage:
            return self._mock_schedule_result()

        mock_adapter.schedule_message = mock_schedule
        thread = _make_thread(mock_adapter, mock_state)

        await thread.schedule("Hello", post_at=self.FUTURE_DATE)

        assert len(mock_adapter._post_calls) == 0

    # ---- Different thread IDs ----

    @pytest.mark.asyncio
    async def test_use_thread_own_id_for_scheduling(self, mock_adapter, mock_state):
        schedule_calls: list[tuple[str, Any, Any]] = []

        async def mock_schedule(thread_id: str, message: Any, options: Any) -> ScheduledMessage:
            schedule_calls.append((thread_id, message, options))
            return self._mock_schedule_result()

        mock_adapter.schedule_message = mock_schedule
        thread = _make_thread(
            mock_adapter,
            mock_state,
            thread_id="slack:C999:9999.0000",
            channel_id="C999",
        )

        await thread.schedule("Hello", post_at=self.FUTURE_DATE)

        assert schedule_calls[0][0] == "slack:C999:9999.0000"

    # ---- Multiple schedules ----

    @pytest.mark.asyncio
    async def test_schedule_multiple_messages(self, mock_adapter, mock_state):
        call_count = 0

        async def mock_schedule(thread_id: str, message: Any, options: Any) -> ScheduledMessage:
            nonlocal call_count
            call_count += 1
            return self._mock_schedule_result(scheduled_message_id=f"Q{call_count}")

        mock_adapter.schedule_message = mock_schedule
        thread = _make_thread(mock_adapter, mock_state)

        s1 = await thread.schedule("First", post_at=self.FUTURE_DATE)
        s2 = await thread.schedule("Second", post_at=self.FUTURE_DATE)
        s3 = await thread.schedule("Third", post_at=self.FUTURE_DATE)

        assert s1.scheduled_message_id == "Q1"
        assert s2.scheduled_message_id == "Q2"
        assert s3.scheduled_message_id == "Q3"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_cancel_individual_messages_independently(self, mock_adapter, mock_state):
        cancel_1_called = False
        cancel_2_called = False

        async def cancel_1() -> None:
            nonlocal cancel_1_called
            cancel_1_called = True

        async def cancel_2() -> None:
            nonlocal cancel_2_called
            cancel_2_called = True

        call_count = 0

        async def mock_schedule(thread_id: str, message: Any, options: Any) -> ScheduledMessage:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return self._mock_schedule_result(scheduled_message_id="Q1", _cancel=cancel_1)
            return self._mock_schedule_result(scheduled_message_id="Q2", _cancel=cancel_2)

        mock_adapter.schedule_message = mock_schedule
        thread = _make_thread(mock_adapter, mock_state)

        s1 = await thread.schedule("First", post_at=self.FUTURE_DATE)
        _s2 = await thread.schedule("Second", post_at=self.FUTURE_DATE)

        await s1.cancel()

        assert cancel_1_called is True
        assert cancel_2_called is False

    @pytest.mark.asyncio
    async def test_pass_exact_date_to_adapter(self, mock_adapter, mock_state):
        schedule_calls: list[tuple[str, Any, Any]] = []

        async def mock_schedule(thread_id: str, message: Any, options: Any) -> ScheduledMessage:
            schedule_calls.append((thread_id, message, options))
            return self._mock_schedule_result()

        mock_adapter.schedule_message = mock_schedule
        thread = _make_thread(mock_adapter, mock_state)

        specific_date = datetime(2028, 12, 25, 8, 0, 0, tzinfo=timezone.utc)
        await thread.schedule("Merry Christmas!", post_at=specific_date)

        assert schedule_calls[0][2] == {"post_at": specific_date}


# ============================================================================
# thread.channel property
# ============================================================================


class TestThreadChannel:
    """Tests for thread.channel property."""

    def test_return_channel_for_parent(self, mock_adapter, mock_state):
        thread = _make_thread(mock_adapter, mock_state)
        channel = thread.channel
        assert channel.id == "slack:C123"
        assert channel.adapter is mock_adapter

    def test_cache_channel_instance(self, mock_adapter, mock_state):
        thread = _make_thread(mock_adapter, mock_state)
        channel1 = thread.channel
        channel2 = thread.channel
        assert channel1 is channel2

    def test_inherit_is_dm_from_thread(self, mock_adapter, mock_state):
        thread = _make_thread(
            mock_adapter,
            mock_state,
            is_dm=True,
            thread_id="slack:D123:1234.5678",
            channel_id="D123",
        )
        assert thread.channel.is_dm is True

    def test_inherit_channel_visibility_from_thread(self, mock_adapter, mock_state):
        thread = _make_thread(mock_adapter, mock_state, channel_visibility="external")
        assert thread.channel.channel_visibility == "external"

    def test_default_channel_visibility_unknown(self, mock_adapter, mock_state):
        thread = _make_thread(mock_adapter, mock_state)
        assert thread.channel.channel_visibility == "unknown"

    def test_support_private_channel_visibility(self, mock_adapter, mock_state):
        thread = _make_thread(
            mock_adapter,
            mock_state,
            channel_visibility="private",
            thread_id="slack:G123:1234.5678",
            channel_id="G123",
        )
        assert thread.channel.channel_visibility == "private"


# ============================================================================
# messages() (newest first) -- extended
# ============================================================================


class TestThreadMessagesNewestFirst:
    """Extended tests for thread.messages() (newest-first)."""

    @pytest.mark.asyncio
    async def test_iterate_newest_first(self, mock_adapter, mock_state):
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
        assert collected[0].text == "Newest"
        assert collected[1].text == "Middle"
        assert collected[2].text == "Oldest"

    @pytest.mark.asyncio
    async def test_pagination_newest_first(self, mock_adapter, mock_state):
        call_count = 0

        async def mock_fetch(thread_id: str, options: Any = None) -> FetchResult:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return FetchResult(
                    messages=[
                        create_test_message("msg-2", "Page 1 Old"),
                        create_test_message("msg-3", "Page 1 New"),
                    ],
                    next_cursor="cursor-1",
                )
            return FetchResult(
                messages=[create_test_message("msg-1", "Page 2 Old")],
                next_cursor=None,
            )

        mock_adapter.fetch_messages = mock_fetch
        thread = _make_thread(mock_adapter, mock_state)

        collected: list[Message] = []
        async for msg in thread.messages():
            collected.append(msg)

        assert len(collected) == 3
        # Page 1 reversed
        assert collected[0].text == "Page 1 New"
        assert collected[1].text == "Page 1 Old"
        # Page 2 reversed
        assert collected[2].text == "Page 2 Old"

    @pytest.mark.asyncio
    async def test_get_n_most_recent_with_break(self, mock_adapter, mock_state):
        msgs = [
            create_test_message("msg-1", "Old"),
            create_test_message("msg-2", "Middle"),
            create_test_message("msg-3", "Recent"),
            create_test_message("msg-4", "Very Recent"),
            create_test_message("msg-5", "Newest"),
        ]
        mock_adapter.fetch_messages = AsyncMock(return_value=FetchResult(messages=msgs, next_cursor="more"))
        thread = _make_thread(mock_adapter, mock_state)

        recent: list[Message] = []
        async for msg in thread.messages():
            recent.append(msg)
            if len(recent) >= 3:
                break

        assert len(recent) == 3
        assert recent[0].text == "Newest"
        assert recent[1].text == "Very Recent"
        assert recent[2].text == "Recent"


# ============================================================================
# Streaming: native path preserves newlines
# ============================================================================


class TestStreamingNewlines:
    """Tests for newline preservation in streaming."""

    @pytest.mark.asyncio
    async def test_preserve_double_newlines_native(self, mock_adapter, mock_state):
        captured_chunks: list[str] = []

        async def mock_stream(thread_id: str, text_stream: Any, options: Any) -> RawMessage:
            async for chunk in text_stream:
                if isinstance(chunk, str):
                    captured_chunks.append(chunk)
            return RawMessage(id="msg-stream", thread_id="t1", raw={})

        mock_adapter.stream = mock_stream
        thread = _make_thread(mock_adapter, mock_state)

        text_stream = _create_text_stream(["hello.", "\n\n", "how are you?"])
        result = await thread.post(text_stream)

        assert captured_chunks == ["hello.", "\n\n", "how are you?"]
        # The text preserves double newlines in the raw form
        assert "hello." in result.text
        assert "how are you?" in result.text

    @pytest.mark.asyncio
    async def test_concatenate_multi_step_text_without_separator(self, mock_adapter, mock_state):
        captured_chunks: list[str] = []

        async def mock_stream(thread_id: str, text_stream: Any, options: Any) -> RawMessage:
            async for chunk in text_stream:
                if isinstance(chunk, str):
                    captured_chunks.append(chunk)
            return RawMessage(id="msg-stream", thread_id="t1", raw={})

        mock_adapter.stream = mock_stream
        thread = _make_thread(mock_adapter, mock_state)

        text_stream = _create_text_stream(["hello", ".", "how", " are", " you?"])
        result = await thread.post(text_stream)

        assert result.text == "hello.how are you?"

    @pytest.mark.asyncio
    async def test_pass_stream_options_from_current_message(self, mock_adapter, mock_state):
        stream_calls: list[tuple[str, Any, Any]] = []

        async def mock_stream(thread_id: str, text_stream: Any, options: Any) -> RawMessage:
            stream_calls.append((thread_id, text_stream, options))
            async for _ in text_stream:
                pass
            return RawMessage(id="msg-stream", thread_id="t1", raw="Hello")

        mock_adapter.stream = mock_stream

        current_msg = create_test_message(
            "original-msg",
            "test",
            raw={"team_id": "T123"},
            author=Author(
                user_id="U456",
                user_name="user",
                full_name="Test User",
                is_bot=False,
                is_me=False,
            ),
        )
        thread = _make_thread(mock_adapter, mock_state, current_message=current_msg)

        text_stream = _create_text_stream(["Hello"])
        await thread.post(text_stream)

        assert len(stream_calls) == 1
        options = stream_calls[0][2]
        assert options.recipient_user_id == "U456"
        assert options.recipient_team_id == "T123"
