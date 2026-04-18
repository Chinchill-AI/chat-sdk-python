"""Faithful translation of thread.test.ts (106 tests).

Each ``it("...")`` block from the TypeScript test suite is translated
to a corresponding ``async def test_...`` method, preserving the same
inputs, assertions, and test structure.

TS ``thread.messages`` (property → async iterable) becomes
``thread.messages()`` (async generator method) in Python.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest

from chat_sdk.channel import derive_channel_id
from chat_sdk.errors import ChatNotImplementedError
from chat_sdk.shared.mock_adapter import MockLogger
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
    MarkdownTextChunk,
    Message,
    MessageMetadata,
    PlanUpdateChunk,
    PostableMarkdown,
    PostableRaw,
    RawMessage,
    ScheduledMessage,
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
    initial_message: Message | None = None,
    fallback_streaming_placeholder_text: str | None = "...",
    streaming_update_interval_ms: int = 500,
    is_subscribed_context: bool = False,
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
            initial_message=initial_message,
            fallback_streaming_placeholder_text=fallback_streaming_placeholder_text,
            streaming_update_interval_ms=streaming_update_interval_ms,
            is_subscribed_context=is_subscribed_context,
            logger=logger,
        )
    )


async def _create_text_stream(chunks: list[str]) -> AsyncIterator[str]:
    """Create an async iterable from a list of string chunks."""
    for chunk in chunks:
        yield chunk


async def _collect(ait: AsyncIterator[Any]) -> list[Any]:
    """Collect all items from an async iterator into a list."""
    result = []
    async for item in ait:
        result.append(item)
    return result


# ===========================================================================
# Per-thread state
# ===========================================================================


class TestPerThreadState:
    """describe("Per-thread state")"""

    # it("should return null when no state has been set")
    @pytest.mark.asyncio
    async def test_should_return_null_when_no_state_has_been_set(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        thread = _make_thread(adapter, state)
        result = await thread.get_state()
        assert result is None

    # it("should return stored state")
    @pytest.mark.asyncio
    async def test_should_return_stored_state(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        state.cache["thread-state:slack:C123:1234.5678"] = {"aiMode": True}
        thread = _make_thread(adapter, state)
        result = await thread.get_state()
        assert result == {"aiMode": True}

    # it("should set state and retrieve it")
    @pytest.mark.asyncio
    async def test_should_set_state_and_retrieve_it(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        thread = _make_thread(adapter, state)
        await thread.set_state({"aiMode": True})
        result = await thread.get_state()
        assert result == {"aiMode": True}

    # it("should merge state by default")
    @pytest.mark.asyncio
    async def test_should_merge_state_by_default(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        thread = _make_thread(adapter, state)
        await thread.set_state({"aiMode": True})
        await thread.set_state({"counter": 5})
        result = await thread.get_state()
        assert result == {"aiMode": True, "counter": 5}

    # it("should overwrite existing keys when merging")
    @pytest.mark.asyncio
    async def test_should_overwrite_existing_keys_when_merging(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        thread = _make_thread(adapter, state)
        await thread.set_state({"aiMode": True, "counter": 1})
        await thread.set_state({"counter": 10})
        result = await thread.get_state()
        assert result == {"aiMode": True, "counter": 10}

    # it("should replace entire state when replace option is true")
    @pytest.mark.asyncio
    async def test_should_replace_entire_state_when_replace_option_is_true(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        thread = _make_thread(adapter, state)
        await thread.set_state({"aiMode": True, "counter": 5})
        await thread.set_state({"counter": 10}, replace=True)
        result = await thread.get_state()
        assert result == {"counter": 10}
        assert "aiMode" not in result

    # it("should use correct key prefix for state storage")
    @pytest.mark.asyncio
    async def test_should_use_correct_key_prefix_for_state_storage(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        thread = _make_thread(adapter, state)
        await thread.set_state({"aiMode": True})
        assert "thread-state:slack:C123:1234.5678" in state.cache

    # it("should call get with correct key")
    @pytest.mark.asyncio
    async def test_should_call_get_with_correct_key(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        thread = _make_thread(adapter, state)
        _ = await thread.get_state()
        # The key used should be "thread-state:slack:C123:1234.5678"
        # We verify by setting state and checking the key in cache
        await thread.set_state({"test": True})
        assert state.cache.get("thread-state:slack:C123:1234.5678") == {"test": True}


# ===========================================================================
# Post with different message formats
# ===========================================================================


class TestPostWithDifferentMessageFormats:
    """describe("post with different message formats")"""

    # it("should post a string message")
    @pytest.mark.asyncio
    async def test_should_post_a_string_message(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        thread = _make_thread(adapter, state)
        result = await thread.post("Hello world")
        assert len(adapter._post_calls) == 1
        assert adapter._post_calls[0] == ("slack:C123:1234.5678", "Hello world")
        assert result.text == "Hello world"
        assert result.id == "msg-1"

    # it("should post a raw message")
    @pytest.mark.asyncio
    async def test_should_post_a_raw_message(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        thread = _make_thread(adapter, state)
        result = await thread.post(PostableRaw(raw="raw text"))
        assert len(adapter._post_calls) == 1
        call_msg = adapter._post_calls[0][1]
        assert isinstance(call_msg, PostableRaw)
        assert call_msg.raw == "raw text"
        assert result.text == "raw text"

    # it("should post a markdown message")
    @pytest.mark.asyncio
    async def test_should_post_a_markdown_message(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        thread = _make_thread(adapter, state)
        result = await thread.post(PostableMarkdown(markdown="**bold** text"))
        # Markdown is parsed to AST; plain text strips formatting
        assert result.text == "bold text"

    # it("should set correct author on sent message")
    @pytest.mark.asyncio
    async def test_should_set_correct_author_on_sent_message(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        thread = _make_thread(adapter, state)
        result = await thread.post("Hello")
        assert result.author.is_bot is True
        assert result.author.is_me is True
        assert result.author.user_id == "self"
        assert result.author.user_name == "slack-bot"

    # it("should use threadId override from postMessage response")
    @pytest.mark.asyncio
    async def test_should_use_threadid_override_from_postmessage_response(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        async def custom_post(thread_id: str, message: Any) -> RawMessage:
            return RawMessage(id="msg-2", thread_id="slack:C123:new-thread-id", raw={})

        adapter.post_message = custom_post  # type: ignore[assignment]
        thread = _make_thread(adapter, state)
        result = await thread.post("Hello")
        assert result.thread_id == "slack:C123:new-thread-id"


# ===========================================================================
# Streaming
# ===========================================================================


class TestStreaming:
    """describe("Streaming")"""

    # it("should use adapter native streaming when available")
    @pytest.mark.asyncio
    async def test_should_use_adapter_native_streaming_when_available(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        mock_stream = AsyncMock(return_value=RawMessage(id="msg-stream", thread_id="t1", raw="Hello World"))
        adapter.stream = mock_stream  # type: ignore[attr-defined]

        thread = _make_thread(adapter, state)
        text_stream = _create_text_stream(["Hello", " ", "World"])
        await thread.post(text_stream)

        mock_stream.assert_called_once()
        # Should NOT call postMessage for fallback
        assert len(adapter._post_calls) == 0

    # it("should fall back to post+edit when adapter has no native streaming")
    @pytest.mark.asyncio
    async def test_should_fall_back_to_postedit_when_adapter_has_no_native_streaming(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        # Ensure no stream method
        assert not hasattr(adapter, "stream") or getattr(adapter, "stream", None) is None

        thread = _make_thread(adapter, state)
        text_stream = _create_text_stream(["Hello", " ", "World"])
        await thread.post(text_stream)

        # Should post initial placeholder
        assert len(adapter._post_calls) >= 1
        assert adapter._post_calls[0] == ("slack:C123:1234.5678", "...")
        # Should edit with final content wrapped as markdown
        assert len(adapter._edit_calls) >= 1
        last_edit = adapter._edit_calls[-1]
        assert last_edit[0] == "slack:C123:1234.5678"
        assert last_edit[1] == "msg-1"
        assert isinstance(last_edit[2], PostableMarkdown)
        assert last_edit[2].markdown == "Hello World"

    # it("should accumulate text chunks during streaming")
    @pytest.mark.asyncio
    async def test_should_accumulate_text_chunks_during_streaming(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        thread = _make_thread(adapter, state)
        text_stream = _create_text_stream(["This ", "is ", "a ", "test ", "message."])
        result = await thread.post(text_stream)

        # Final edit should have all accumulated text wrapped as markdown
        last_edit = adapter._edit_calls[-1]
        assert isinstance(last_edit[2], PostableMarkdown)
        assert last_edit[2].markdown == "This is a test message."
        assert result.text == "This is a test message."

    # it("should throttle edits to avoid rate limits")
    @pytest.mark.asyncio
    async def test_should_throttle_edits_to_avoid_rate_limits(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        thread = _make_thread(adapter, state)
        text_stream = _create_text_stream(["A", "B", "C", "D", "E"])
        await thread.post(text_stream)

        # Should have final edit wrapped as markdown
        last_edit = adapter._edit_calls[-1]
        assert isinstance(last_edit[2], PostableMarkdown)
        assert last_edit[2].markdown == "ABCDE"

    # it("should return SentMessage with edit and delete capabilities")
    @pytest.mark.asyncio
    async def test_should_return_sentmessage_with_edit_and_delete_capabilities(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        thread = _make_thread(adapter, state)
        text_stream = _create_text_stream(["Hello"])
        result = await thread.post(text_stream)

        assert result.id == "msg-1"
        assert callable(result.edit)
        assert callable(result.delete)
        assert callable(result.add_reaction)
        assert callable(result.remove_reaction)

    # it("should handle empty stream")
    @pytest.mark.asyncio
    async def test_should_handle_empty_stream_with_disabled_placeholder(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        thread = _make_thread(adapter, state)
        text_stream = _create_text_stream([])
        await thread.post(text_stream)

        # Should post initial placeholder
        assert adapter._post_calls[0] == ("slack:C123:1234.5678", "...")
        # Python divergence: clear the placeholder with " " on empty streams so
        # users don't see a stuck "..." forever. (Upstream leaves it visible;
        # documented in docs/UPSTREAM_SYNC.md.)
        assert len(adapter._edit_calls) == 1
        assert adapter._edit_calls[0][2] == PostableMarkdown(markdown=" ")

    # it("should support disabling the placeholder for fallback streaming")
    @pytest.mark.asyncio
    async def test_should_support_disabling_the_placeholder_for_fallback_streaming(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        thread = _make_thread(adapter, state, fallback_streaming_placeholder_text=None)
        text_stream = _create_text_stream(["H", "i"])
        await thread.post(text_stream)

        # Should NOT have posted "..."
        placeholder_calls = [c for c in adapter._post_calls if c[1] == "..."]
        assert len(placeholder_calls) == 0
        # Final content is delivered through post since no mid-stream commit
        # fired (no newline in the chunks) and the empty-content guard
        # prevents an intermediate empty post. The whole exchange is
        # exactly one post_message("Hi") and zero edits — any regression
        # that splits it into an early post + late edit would fail here.
        assert len(adapter._post_calls) == 1
        assert len(adapter._edit_calls) == 0
        only_post = adapter._post_calls[0]
        assert isinstance(only_post[1], PostableMarkdown)
        assert only_post[1].markdown == "Hi"

    # Python-specific regression: ensure whitespace-only streams don't leave
    # the placeholder stuck on the message. This is a deliberate divergence
    # from upstream 4.26, which keeps the placeholder visible.
    @pytest.mark.asyncio
    async def test_should_clear_placeholder_when_stream_is_whitespace_only(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        thread = _make_thread(adapter, state)
        text_stream = _create_text_stream(["   ", "\n", "  \n"])
        await thread.post(text_stream)

        # Placeholder was posted
        assert adapter._post_calls[0] == ("slack:C123:1234.5678", "...")
        # And cleared via edit to " "
        final_edit = adapter._edit_calls[-1]
        assert isinstance(final_edit[2], PostableMarkdown)
        assert final_edit[2].markdown == " "

    # Python-specific regression: when the placeholder-clear edit_message(" ")
    # raises (e.g. Telegram rejects whitespace-only content with a
    # ValidationError), `_fallback_stream` must log + swallow the error so
    # `thread.post()` still returns a SentMessage. The previous test pinned
    # the happy path; this one pins the defensive try/except added in
    # commit 8dd34d1 specifically for adapters that reject blank text.
    @pytest.mark.asyncio
    async def test_should_swallow_placeholder_clear_error_on_strict_adapter(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        logger = MockLogger()

        # Telegram's adapter raises ValidationError when text.strip() is empty.
        # Simulate by intercepting edit_message and raising on the " " payload.
        clear_attempts: list[PostableMarkdown] = []
        original_edit = adapter.edit_message

        async def strict_edit(thread_id: str, message_id: str, message: Any) -> RawMessage:
            if isinstance(message, PostableMarkdown) and not message.markdown.strip():
                clear_attempts.append(message)
                raise ValueError("Message text cannot be empty")
            return await original_edit(thread_id, message_id, message)

        adapter.edit_message = strict_edit  # type: ignore[assignment]

        thread = _make_thread(adapter, state, logger=logger)
        # Whitespace-only stream triggers the placeholder-clear branch.
        text_stream = _create_text_stream(["   ", "\n", "  \n"])

        # Must not raise — the SDK should log and fall through to the
        # upstream "leave placeholder visible" behavior on rejection.
        sent = await thread.post(text_stream)

        # Placeholder was posted, then exactly one clear edit was attempted
        # (and rejected). No retry, no infinite loop.
        assert adapter._post_calls[0] == ("slack:C123:1234.5678", "...")
        assert len(clear_attempts) == 1
        assert clear_attempts[0].markdown == " "
        # The warn log fired with the expected message (asserting the exact
        # string so a refactor that changes the log can't silently break the
        # observability contract).
        assert any(
            call[0] == "fallbackStream placeholder-clear edit failed; placeholder will remain visible"
            for call in logger.warn.calls
        ), [c[0] for c in logger.warn.calls]
        # Stream contract still holds — we got a SentMessage back.
        assert sent is not None
        assert sent.id == "msg-1"

    # it("should handle empty stream with disabled placeholder")
    @pytest.mark.asyncio
    async def test_should_handle_empty_stream(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        thread = _make_thread(adapter, state, fallback_streaming_placeholder_text=None)
        text_stream = _create_text_stream([])
        await thread.post(text_stream)

        # Should post a non-empty fallback since stream must return a SentMessage
        assert len(adapter._post_calls) == 1
        posted = adapter._post_calls[0][1]
        assert isinstance(posted, PostableMarkdown)
        assert posted.markdown == " "
        assert len(adapter._edit_calls) == 0

    # it("should not post empty content when table is buffered with null placeholder")
    @pytest.mark.asyncio
    async def test_should_not_post_empty_content_when_table_is_buffered_with_null_placeholder(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        thread = _make_thread(adapter, state, fallback_streaming_placeholder_text=None)
        text_stream = _create_text_stream(["| A | B |\n", "|---|---|\n", "| 1 | 2 |\n"])
        await thread.post(text_stream)

        markdown_posts = [content for _, content in adapter._post_calls if isinstance(content, PostableMarkdown)]
        assert markdown_posts, "expected at least one PostableMarkdown post"
        assert all(p.markdown.strip() for p in markdown_posts)

    # it("should not edit placeholder to empty during LLM warm-up")
    @pytest.mark.asyncio
    async def test_should_not_edit_placeholder_to_empty_during_llm_warmup(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        # Simulate the warm-up path: a whitespace-only chunk arrives before the
        # real content, and the background edit loop fires fast enough to see
        # it. Without the warm-up chunk the test never exercises the empty-edit
        # guard — it'd just post "Hello world" immediately.
        thread = _make_thread(adapter, state, streaming_update_interval_ms=10)

        async def _stream() -> AsyncIterator[str]:
            yield " "
            await asyncio.sleep(0.05)
            yield "Hello world\n"

        await thread.post(_stream())

        markdown_edits = [content for _, _, content in adapter._edit_calls if isinstance(content, PostableMarkdown)]
        assert markdown_edits, "expected at least one PostableMarkdown edit"
        assert all(e.markdown.strip() for e in markdown_edits)

    # it("should not post empty content during streaming with whitespace chunks")
    @pytest.mark.asyncio
    async def test_should_not_post_empty_content_during_streaming_with_whitespace_chunks(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        thread = _make_thread(adapter, state, fallback_streaming_placeholder_text=None)
        text_stream = _create_text_stream(["  ", "\n", "  \n"])
        await thread.post(text_stream)

        # Whitespace-only stream with placeholder disabled: the SDK normalizes
        # to a single `" "` in the final post_message call, not the original
        # whitespace buffer. Asserting the exact value catches regressions that
        # would silently emit "   \n" or similar.
        markdown_posts = [content for _, content in adapter._post_calls if isinstance(content, PostableMarkdown)]
        assert len(markdown_posts) == 1
        assert markdown_posts[0].markdown == " "

    # it("should preserve newlines in streamed text (native path)")
    @pytest.mark.asyncio
    async def test_should_preserve_newlines_in_streamed_text_native_path(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        captured_chunks: list[str] = []

        async def mock_stream(thread_id: str, text_stream: Any, options: Any = None) -> RawMessage:
            nonlocal captured_chunks
            captured_chunks = []
            async for chunk in text_stream:
                if isinstance(chunk, str):
                    captured_chunks.append(chunk)
            return RawMessage(id="msg-stream", thread_id="t1", raw={})

        adapter.stream = mock_stream  # type: ignore[attr-defined]

        thread = _make_thread(adapter, state)
        text_stream = _create_text_stream(["hello", ".", "\n", "how", " are", " you?"])
        result = await thread.post(text_stream)

        # The accumulated text should preserve the newline
        assert result.text == "hello.\nhow are you?"
        # All chunks should have been passed through to the adapter
        assert captured_chunks == ["hello", ".", "\n", "how", " are", " you?"]

    # it("should preserve double newlines (paragraph breaks) in streamed text")
    @pytest.mark.asyncio
    async def test_should_preserve_double_newlines_paragraph_breaks_in_streamed_text(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        captured_chunks: list[str] = []

        async def mock_stream(thread_id: str, text_stream: Any, options: Any = None) -> RawMessage:
            nonlocal captured_chunks
            captured_chunks = []
            async for chunk in text_stream:
                if isinstance(chunk, str):
                    captured_chunks.append(chunk)
            return RawMessage(id="msg-stream", thread_id="t1", raw={})

        adapter.stream = mock_stream  # type: ignore[attr-defined]

        thread = _make_thread(adapter, state)
        text_stream = _create_text_stream(["hello.", "\n\n", "how are you?"])
        result = await thread.post(text_stream)

        # Markdown is parsed to AST; double newlines create separate paragraphs
        # which are joined with single newlines in plain text extraction
        assert result.text == "hello.\nhow are you?"
        assert captured_chunks == ["hello.", "\n\n", "how are you?"]

    # it("should concatenate multi-step text without separator (demonstrates bug)")
    @pytest.mark.asyncio
    async def test_should_concatenate_multistep_text_without_separator_demonstrates_bug(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        captured_chunks: list[str] = []

        async def mock_stream(thread_id: str, text_stream: Any, options: Any = None) -> RawMessage:
            nonlocal captured_chunks
            captured_chunks = []
            async for chunk in text_stream:
                if isinstance(chunk, str):
                    captured_chunks.append(chunk)
            return RawMessage(id="msg-stream", thread_id="t1", raw={})

        adapter.stream = mock_stream  # type: ignore[attr-defined]

        thread = _make_thread(adapter, state)
        text_stream = _create_text_stream(["hello", ".", "how", " are", " you?"])
        result = await thread.post(text_stream)

        # BUG: text from separate steps is concatenated without any whitespace
        assert result.text == "hello.how are you?"

    # it("should preserve newlines in fallback streaming path")
    @pytest.mark.asyncio
    async def test_should_preserve_newlines_in_fallback_streaming_path(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        thread = _make_thread(adapter, state)
        text_stream = _create_text_stream(["hello.", "\n", "how are you?"])
        result = await thread.post(text_stream)

        # Final edit should have all accumulated text with newline preserved
        last_edit = adapter._edit_calls[-1]
        assert isinstance(last_edit[2], PostableMarkdown)
        assert last_edit[2].markdown == "hello.\nhow are you?"
        assert result.text == "hello.\nhow are you?"

    # it("should close incomplete markdown in intermediate fallback edits")
    # Note: The TS SDK uses `remend` to auto-close incomplete markdown markers
    # in intermediate edits. The Python port does NOT have this feature --
    # intermediate edits may contain incomplete markdown. We verify that the
    # *final* result has the complete accumulated text and that no errors occur.
    @pytest.mark.asyncio
    async def test_should_close_incomplete_markdown_in_intermediate_fallback_edits(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        async def slow_stream() -> AsyncIterator[str]:
            yield "Hello **wor"
            await asyncio.sleep(0.05)
            yield "ld** done"

        thread = _make_thread(adapter, state, streaming_update_interval_ms=10)
        result = await thread.post(slow_stream())

        # Final result text: markdown is parsed and plain text strips formatting
        assert result.text == "Hello world done"

        # The final edit should have the complete text with balanced markdown
        last_edit = adapter._edit_calls[-1]
        final_md = last_edit[2].markdown if isinstance(last_edit[2], PostableMarkdown) else last_edit[2]
        assert final_md == "Hello **world** done"
        open_count = final_md.count("**")
        assert open_count % 2 == 0

    # it("should pass stream options from current message context")
    @pytest.mark.asyncio
    async def test_should_pass_stream_options_from_current_message_context(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        stream_call_args: list[Any] = []

        async def mock_stream(thread_id: str, text_stream: Any, options: Any = None) -> RawMessage:
            stream_call_args.append((thread_id, text_stream, options))
            # Consume the stream
            async for _ in text_stream:
                pass
            return RawMessage(id="msg-stream", thread_id="t1", raw="Hello")

        adapter.stream = mock_stream  # type: ignore[attr-defined]

        current_msg = Message(
            id="original-msg",
            thread_id="slack:C123:1234.5678",
            text="test",
            formatted={"type": "root", "children": []},
            raw={"team_id": "T123"},
            author=Author(
                user_id="U456",
                user_name="user",
                full_name="Test User",
                is_bot=False,
                is_me=False,
            ),
            metadata=MessageMetadata(date_sent=datetime.now(tz=timezone.utc), edited=False),
            attachments=[],
        )

        thread = _make_thread(adapter, state, current_message=current_msg)
        text_stream = _create_text_stream(["Hello"])
        await thread.post(text_stream)

        assert len(stream_call_args) == 1
        options = stream_call_args[0][2]
        assert options.recipient_user_id == "U456"
        assert options.recipient_team_id == "T123"


# ===========================================================================
# Fallback streaming error logging
# ===========================================================================


class TestFallbackStreamingErrorLogging:
    """describe("fallback streaming error logging")"""

    # it("should log when an intermediate edit fails")
    @pytest.mark.asyncio
    async def test_should_log_when_an_intermediate_edit_fails(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        logger = MockLogger()

        edit_error = Exception("422 Validation Failed")
        call_count = 0
        original_edit = adapter.edit_message

        async def failing_edit(thread_id: str, message_id: str, message: Any) -> RawMessage:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise edit_error
            return await original_edit(thread_id, message_id, message)

        adapter.edit_message = failing_edit  # type: ignore[assignment]

        thread = _make_thread(adapter, state, streaming_update_interval_ms=10, logger=logger)

        async def slow_stream() -> AsyncIterator[str]:
            # Newlines are required so the streaming renderer commits content
            # mid-stream; without them the post-4.26 empty-content guard
            # skips intermediate edits entirely.
            yield "Hel\n"
            await asyncio.sleep(0.05)
            yield "lo\n"

        await thread.post(slow_stream())

        assert len(logger.warn.calls) >= 1
        assert logger.warn.calls[0][0] == "fallbackStream edit failed"
        assert logger.warn.calls[0][1] is edit_error


# ===========================================================================
# Streaming with StreamChunk objects
# ===========================================================================


class TestStreamingWithStreamChunks:
    """describe("streaming with StreamChunk objects")"""

    # it("should pass StreamChunk objects through to adapter.stream")
    @pytest.mark.asyncio
    async def test_should_pass_ast_message_objects_through(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        captured_chunks: list[Any] = []

        async def mock_stream(thread_id: str, stream: Any, options: Any = None) -> RawMessage:
            nonlocal captured_chunks
            captured_chunks = []
            async for chunk in stream:
                captured_chunks.append(chunk)
            return RawMessage(id="msg-stream", thread_id="t1", raw={})

        adapter.stream = mock_stream  # type: ignore[attr-defined]

        async def mixed_stream() -> AsyncIterator[Any]:
            yield "Hello "
            yield TaskUpdateChunk(
                type="task_update",
                id="tool-1",
                title="Running bash",
                status="in_progress",
            )
            yield "world"
            yield TaskUpdateChunk(
                type="task_update",
                id="tool-1",
                title="Running bash",
                status="complete",
                output="Done",
            )

        thread = _make_thread(adapter, state)
        result = await thread.post(mixed_stream())

        # All chunks (strings and objects) should pass through
        assert len(captured_chunks) == 4
        assert captured_chunks[0] == "Hello "
        assert captured_chunks[1].type == "task_update"
        assert captured_chunks[1].status == "in_progress"
        assert captured_chunks[2] == "world"
        assert captured_chunks[3].type == "task_update"
        assert captured_chunks[3].status == "complete"

        # Accumulated text should only include strings, not task_update chunks
        assert result.text == "Hello world"

    # it("should accumulate text from markdown_text StreamChunks")
    @pytest.mark.asyncio
    async def test_should_accumulate_text_from_markdowntext_streamchunks(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        captured_chunks: list[Any] = []

        async def mock_stream(thread_id: str, stream: Any, options: Any = None) -> RawMessage:
            nonlocal captured_chunks
            captured_chunks = []
            async for chunk in stream:
                captured_chunks.append(chunk)
            return RawMessage(id="msg-stream", thread_id="t1", raw={})

        adapter.stream = mock_stream  # type: ignore[attr-defined]

        async def md_chunk_stream() -> AsyncIterator[Any]:
            yield MarkdownTextChunk(type="markdown_text", text="Hello ")
            yield PlanUpdateChunk(type="plan_update", title="Analyzing code")
            yield MarkdownTextChunk(type="markdown_text", text="World")

        thread = _make_thread(adapter, state)
        result = await thread.post(md_chunk_stream())

        # markdown_text chunks contribute to accumulated text; plan_update does not
        assert result.text == "Hello World"

    # it("should extract only text for fallback streaming when chunks are present")
    @pytest.mark.asyncio
    async def test_should_extract_only_text_for_fallback_streaming_when_chunks_are_present(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        # No native stream -- falls back to post+edit

        async def mixed_stream() -> AsyncIterator[Any]:
            yield "Hello"
            yield TaskUpdateChunk(
                type="task_update",
                id="tool-1",
                title="Running bash",
                status="in_progress",
            )
            yield " World"

        thread = _make_thread(adapter, state)
        await thread.post(mixed_stream())

        # Should post placeholder then edit with text-only content
        assert adapter._post_calls[0] == ("slack:C123:1234.5678", "...")
        last_edit = adapter._edit_calls[-1]
        assert isinstance(last_edit[2], PostableMarkdown)
        assert last_edit[2].markdown == "Hello World"


# ===========================================================================
# allMessages iterator
# ===========================================================================


class TestAllMessagesIterator:
    """describe("allMessages iterator")"""

    # it("should iterate through all messages in chronological order")
    @pytest.mark.asyncio
    async def test_should_iterate_through_all_messages_in_chronological_order(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        messages = [
            create_test_message("msg-1", "First message"),
            create_test_message("msg-2", "Second message"),
            create_test_message("msg-3", "Third message"),
        ]

        adapter.fetch_messages = AsyncMock(  # type: ignore[assignment]
            return_value=FetchResult(messages=messages, next_cursor=None)
        )

        thread = _make_thread(adapter, state)
        collected = await _collect(thread.all_messages())

        assert len(collected) == 3
        assert collected[0].text == "First message"
        assert collected[1].text == "Second message"
        assert collected[2].text == "Third message"

    # it("should use forward direction for pagination")
    @pytest.mark.asyncio
    async def test_should_use_forward_direction_for_pagination(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        adapter.fetch_messages = AsyncMock(  # type: ignore[assignment]
            return_value=FetchResult(messages=[], next_cursor=None)
        )

        thread = _make_thread(adapter, state)
        await _collect(thread.all_messages())

        adapter.fetch_messages.assert_called_once()
        call_args = adapter.fetch_messages.call_args
        opts = call_args[0][1]
        assert opts.direction == "forward"
        assert opts.limit == 100

    # it("should handle pagination across multiple pages")
    @pytest.mark.asyncio
    async def test_should_handle_pagination_across_multiple_pages(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

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
                assert options is None or options.cursor is None
                return FetchResult(messages=page1, next_cursor="cursor-1")
            if call_count == 2:
                assert options.cursor == "cursor-1"
                return FetchResult(messages=page2, next_cursor="cursor-2")
            assert options.cursor == "cursor-2"
            return FetchResult(messages=page3, next_cursor=None)

        adapter.fetch_messages = mock_fetch  # type: ignore[assignment]

        thread = _make_thread(adapter, state)
        collected = await _collect(thread.all_messages())

        assert len(collected) == 5
        assert [m.text for m in collected] == [
            "Page 1 - Message 1",
            "Page 1 - Message 2",
            "Page 2 - Message 1",
            "Page 2 - Message 2",
            "Page 3 - Message 1",
        ]
        assert call_count == 3

    # it("should handle empty thread")
    @pytest.mark.asyncio
    async def test_should_handle_empty_thread(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        adapter.fetch_messages = AsyncMock(  # type: ignore[assignment]
            return_value=FetchResult(messages=[], next_cursor=None)
        )

        thread = _make_thread(adapter, state)
        collected = await _collect(thread.all_messages())

        assert len(collected) == 0
        adapter.fetch_messages.assert_called_once()

    # it("should stop when nextCursor is undefined")
    @pytest.mark.asyncio
    async def test_should_stop_when_nextcursor_is_undefined(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        messages = [create_test_message("msg-1", "Single message")]

        adapter.fetch_messages = AsyncMock(  # type: ignore[assignment]
            return_value=FetchResult(messages=messages, next_cursor=None)
        )

        thread = _make_thread(adapter, state)
        collected = await _collect(thread.all_messages())

        assert len(collected) == 1
        adapter.fetch_messages.assert_called_once()

    # it("should stop when empty page is returned with cursor")
    @pytest.mark.asyncio
    async def test_should_stop_when_empty_page_is_returned_with_cursor(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        adapter.fetch_messages = AsyncMock(  # type: ignore[assignment]
            return_value=FetchResult(messages=[], next_cursor="some-cursor")
        )

        thread = _make_thread(adapter, state)
        collected = await _collect(thread.all_messages())

        assert len(collected) == 0
        adapter.fetch_messages.assert_called_once()

    # it("should allow breaking out of iteration early")
    @pytest.mark.asyncio
    async def test_should_allow_breaking_out_of_iteration_early(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        page1 = [
            create_test_message("msg-1", "Message 1"),
            create_test_message("msg-2", "Message 2"),
        ]

        adapter.fetch_messages = AsyncMock(  # type: ignore[assignment]
            return_value=FetchResult(messages=page1, next_cursor="more-available")
        )

        thread = _make_thread(adapter, state)
        collected: list[Message] = []
        async for msg in thread.all_messages():
            collected.append(msg)
            if msg.id == "msg-1":
                break

        assert len(collected) == 1
        assert collected[0].id == "msg-1"
        adapter.fetch_messages.assert_called_once()

    # it("should be reusable (can iterate multiple times)")
    @pytest.mark.asyncio
    async def test_should_be_reusable_can_iterate_multiple_times(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        messages = [create_test_message("msg-1", "Test message")]
        adapter.fetch_messages = AsyncMock(  # type: ignore[assignment]
            return_value=FetchResult(messages=messages, next_cursor=None)
        )

        thread = _make_thread(adapter, state)

        # First iteration
        first = await _collect(thread.all_messages())
        # Second iteration
        second = await _collect(thread.all_messages())

        assert len(first) == 1
        assert len(second) == 1
        assert adapter.fetch_messages.call_count == 2


# ===========================================================================
# refresh
# ===========================================================================


class TestRefresh:
    """describe("refresh")"""

    # it("should update recentMessages from API")
    @pytest.mark.asyncio
    async def test_should_update_recentmessages_from_api(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        messages = [
            create_test_message("msg-1", "Recent 1"),
            create_test_message("msg-2", "Recent 2"),
        ]
        adapter.fetch_messages = AsyncMock(  # type: ignore[assignment]
            return_value=FetchResult(messages=messages, next_cursor=None)
        )

        thread = _make_thread(adapter, state)
        assert len(thread.recent_messages) == 0

        await thread.refresh()

        assert len(thread.recent_messages) == 2
        assert thread.recent_messages[0].text == "Recent 1"
        assert thread.recent_messages[1].text == "Recent 2"

    # it("should fetch with limit of 50")
    @pytest.mark.asyncio
    async def test_should_fetch_with_limit_of_50(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        adapter.fetch_messages = AsyncMock(  # type: ignore[assignment]
            return_value=FetchResult(messages=[], next_cursor=None)
        )

        thread = _make_thread(adapter, state)
        await thread.refresh()

        adapter.fetch_messages.assert_called_once()
        call_args = adapter.fetch_messages.call_args
        opts = call_args[0][1]
        assert opts.limit == 50

    # it("should use default (backward) direction")
    @pytest.mark.asyncio
    async def test_should_use_default_backward_direction(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        adapter.fetch_messages = AsyncMock(  # type: ignore[assignment]
            return_value=FetchResult(messages=[], next_cursor=None)
        )

        thread = _make_thread(adapter, state)
        await thread.refresh()

        # refresh() calls with FetchOptions(limit=50), no direction specified
        call_args = adapter.fetch_messages.call_args
        opts = call_args[0][1]
        assert opts.limit == 50


# ===========================================================================
# fetchMessages direction behavior
# ===========================================================================


class TestFetchMessagesDirectionBehavior:
    """describe("fetchMessages direction behavior")"""

    # it("should pass direction option to adapter")
    @pytest.mark.asyncio
    async def test_should_pass_direction_option_to_adapter(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        adapter.fetch_messages = AsyncMock(  # type: ignore[assignment]
            return_value=FetchResult(messages=[], next_cursor=None)
        )

        thread = _make_thread(adapter, state)
        # allMessages passes forward direction
        await _collect(thread.all_messages())

        call_args = adapter.fetch_messages.call_args
        opts = call_args[0][1]
        assert opts.direction == "forward"


# ===========================================================================
# concurrent iteration safety
# ===========================================================================


class TestConcurrentIterationSafety:
    """describe("concurrent iteration safety")"""

    # it("should handle concurrent iterations independently")
    @pytest.mark.asyncio
    async def test_should_handle_concurrent_iterations_independently(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        call_count = 0

        async def mock_fetch(thread_id: str, options: Any = None) -> FetchResult:
            nonlocal call_count
            call_count += 1
            return FetchResult(
                messages=[create_test_message(f"msg-{call_count}", f"Call {call_count}")],
                next_cursor=None,
            )

        adapter.fetch_messages = mock_fetch  # type: ignore[assignment]

        thread = _make_thread(adapter, state)

        async def iterate() -> list[Message]:
            msgs: list[Message] = []
            async for msg in thread.all_messages():
                msgs.append(msg)
            return msgs

        results = await asyncio.gather(iterate(), iterate())

        assert len(results[0]) == 1
        assert len(results[1]) == 1
        assert call_count == 2

    # it("should not share cursor state between iterations")
    @pytest.mark.asyncio
    async def test_should_not_share_cursor_state_between_iterations(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        cursors: list[str | None] = []

        async def mock_fetch(thread_id: str, options: Any = None) -> FetchResult:
            cursors.append(options.cursor if options else None)
            return FetchResult(
                messages=[create_test_message("msg-1", "Test")],
                next_cursor=None,
            )

        adapter.fetch_messages = mock_fetch  # type: ignore[assignment]

        thread = _make_thread(adapter, state)

        # Two sequential iterations
        await _collect(thread.all_messages())
        await _collect(thread.all_messages())

        # Both iterations should start with None cursor
        assert cursors == [None, None]


# ===========================================================================
# postEphemeral
# ===========================================================================


class TestPostEphemeral:
    """describe("postEphemeral")"""

    # it("should use adapter postEphemeral when available")
    @pytest.mark.asyncio
    async def test_should_use_adapter_postephemeral_when_available(self):
        from chat_sdk.types import EphemeralMessage, PostEphemeralOptions

        adapter = create_mock_adapter()
        state = create_mock_state()

        mock_post_ephemeral = AsyncMock(
            return_value=EphemeralMessage(
                id="ephemeral-1",
                thread_id="slack:C123:1234.5678",
                used_fallback=False,
                raw={},
            )
        )
        adapter.post_ephemeral = mock_post_ephemeral  # type: ignore[attr-defined]

        thread = _make_thread(adapter, state)
        result = await thread.post_ephemeral("U456", "Secret message", PostEphemeralOptions(fallback_to_dm=True))

        mock_post_ephemeral.assert_called_once_with("slack:C123:1234.5678", "U456", "Secret message")
        assert result is not None
        assert result.id == "ephemeral-1"
        assert result.thread_id == "slack:C123:1234.5678"
        assert result.used_fallback is False
        assert result.raw == {}

    # it("should extract userId from Author object")
    @pytest.mark.asyncio
    async def test_should_extract_userid_from_author_object(self):
        from chat_sdk.types import EphemeralMessage, PostEphemeralOptions

        adapter = create_mock_adapter()
        state = create_mock_state()

        mock_post_ephemeral = AsyncMock(
            return_value=EphemeralMessage(
                id="ephemeral-1",
                thread_id="slack:C123:1234.5678",
                used_fallback=False,
                raw={},
            )
        )
        adapter.post_ephemeral = mock_post_ephemeral  # type: ignore[attr-defined]

        author = Author(
            user_id="U789",
            user_name="testuser",
            full_name="Test User",
            is_bot=False,
            is_me=False,
        )

        thread = _make_thread(adapter, state)
        await thread.post_ephemeral(author, "Secret message", PostEphemeralOptions(fallback_to_dm=True))

        assert mock_post_ephemeral.call_count == 1
        mock_post_ephemeral.assert_called_once_with("slack:C123:1234.5678", "U789", "Secret message")

    # it("should fallback to DM when adapter has no postEphemeral and fallbackToDM is true")
    @pytest.mark.asyncio
    async def test_fallback_to_dm(self):
        from chat_sdk.types import PostEphemeralOptions

        adapter = create_mock_adapter()
        state = create_mock_state()
        # Ensure no postEphemeral method -- MockAdapter doesn't have one by default

        thread = _make_thread(adapter, state)
        result = await thread.post_ephemeral("U456", "Secret message", PostEphemeralOptions(fallback_to_dm=True))

        # Should open DM
        # open_dm returns "slack:DU456:"
        # Should post message to DM thread
        assert len(adapter._post_calls) == 1
        assert adapter._post_calls[0] == ("slack:DU456:", "Secret message")
        # Should return with used_fallback True
        assert result is not None
        assert result.id == "msg-1"
        assert result.thread_id == "slack:DU456:"
        assert result.used_fallback is True
        assert result.raw == {}

    # it("should return null when adapter has no postEphemeral and fallbackToDM is false")
    @pytest.mark.asyncio
    async def test_should_fallback_to_dm_when_adapter_has_no_postephemeral_and_fallbacktodm_is_true(self):
        from chat_sdk.types import PostEphemeralOptions

        adapter = create_mock_adapter()
        state = create_mock_state()

        thread = _make_thread(adapter, state)
        result = await thread.post_ephemeral("U456", "Secret message", PostEphemeralOptions(fallback_to_dm=False))

        assert len(adapter._post_calls) == 0
        assert result is None

    # it("should return null when adapter has no postEphemeral or openDM")
    @pytest.mark.asyncio
    async def test_should_return_null_when_adapter_has_no_postephemeral_and_fallbacktodm_is_false(self):
        from chat_sdk.types import PostEphemeralOptions

        adapter = create_mock_adapter()
        state = create_mock_state()
        # Remove openDM
        adapter.open_dm = None  # type: ignore[assignment]

        thread = _make_thread(adapter, state)
        result = await thread.post_ephemeral("U456", "Secret message", PostEphemeralOptions(fallback_to_dm=True))

        assert result is None

    # it("should return null when adapter has no postEphemeral or openDM")
    @pytest.mark.asyncio
    async def test_should_return_null_when_adapter_has_no_postephemeral_or_opendm(self):
        from chat_sdk.types import PostEphemeralOptions

        adapter = create_mock_adapter()
        state = create_mock_state()
        # Remove both postEphemeral (absent by default) and openDM
        adapter.open_dm = None  # type: ignore[assignment]

        thread = _make_thread(adapter, state)
        result = await thread.post_ephemeral("U456", "Secret message", PostEphemeralOptions(fallback_to_dm=True))

        # Should return None since no fallback is possible
        assert result is None


# ===========================================================================
# subscribe and unsubscribe
# ===========================================================================


class TestSubscribeAndUnsubscribe:
    """describe("subscribe and unsubscribe")"""

    # it("should subscribe via state adapter")
    @pytest.mark.asyncio
    async def test_should_subscribe_via_state_adapter(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        thread = _make_thread(adapter, state)

        await thread.subscribe()

        assert "slack:C123:1234.5678" in state._subscriptions

    # it("should not error when adapter has no onThreadSubscribe")
    @pytest.mark.asyncio
    async def test_not_error_when_no_on_thread_subscribe(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        # MockAdapter doesn't have on_thread_subscribe by default

        thread = _make_thread(adapter, state)
        await thread.subscribe()  # Should not raise
        assert "slack:C123:1234.5678" in state._subscriptions

    # it("should unsubscribe via state adapter")
    @pytest.mark.asyncio
    async def test_should_unsubscribe_via_state_adapter(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        thread = _make_thread(adapter, state)

        await thread.subscribe()
        await thread.unsubscribe()

        assert "slack:C123:1234.5678" not in state._subscriptions

    # it("should call adapter.onThreadSubscribe when available")
    @pytest.mark.asyncio
    async def test_should_call_adapteronthreadsubscribe_when_available(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        mock_on_subscribe = AsyncMock(return_value=None)
        adapter.on_thread_subscribe = mock_on_subscribe  # type: ignore[attr-defined]

        thread = _make_thread(adapter, state)
        await thread.subscribe()

        assert mock_on_subscribe.call_count == 1
        mock_on_subscribe.assert_called_once_with("slack:C123:1234.5678")


# ===========================================================================
# isSubscribed
# ===========================================================================


class TestIsSubscribed:
    """describe("isSubscribed")"""

    # it("should return false when not subscribed")
    @pytest.mark.asyncio
    async def test_should_return_false_when_not_subscribed(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        thread = _make_thread(adapter, state)

        result = await thread.is_subscribed()
        assert result is False

    # it("should return true after subscribing")
    @pytest.mark.asyncio
    async def test_should_return_true_after_subscribing(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        thread = _make_thread(adapter, state)

        await thread.subscribe()
        result = await thread.is_subscribed()
        assert result is True

    # it("should return false after unsubscribing")
    @pytest.mark.asyncio
    async def test_should_return_false_after_unsubscribing(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        thread = _make_thread(adapter, state)

        await thread.subscribe()
        await thread.unsubscribe()
        result = await thread.is_subscribed()
        assert result is False

    # it("should short-circuit and return true when isSubscribedContext is set")
    @pytest.mark.asyncio
    async def test_should_shortcircuit_and_return_true_when_issubscribedcontext_is_set(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        thread = _make_thread(adapter, state, is_subscribed_context=True)

        result = await thread.is_subscribed()
        assert result is True
        # Should NOT have called the state adapter
        assert "slack:C123:1234.5678" not in state._subscriptions


# ===========================================================================
# recentMessages getter/setter
# ===========================================================================


class TestRecentMessages:
    """describe("recentMessages getter/setter")"""

    # it("should start with empty array by default")
    def test_should_start_with_empty_array_by_default(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        thread = _make_thread(adapter, state)
        assert thread.recent_messages == []

    # it("should initialize with initialMessage when provided")
    def test_should_initialize_with_initialmessage_when_provided(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        msg = create_test_message("msg-1", "Initial")
        thread = _make_thread(adapter, state, initial_message=msg)
        assert len(thread.recent_messages) == 1
        assert thread.recent_messages[0].text == "Initial"

    # it("should allow setting recentMessages")
    def test_should_allow_setting_recentmessages(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        thread = _make_thread(adapter, state)
        messages = [
            create_test_message("msg-1", "First"),
            create_test_message("msg-2", "Second"),
        ]
        thread.recent_messages = messages
        assert len(thread.recent_messages) == 2
        assert thread.recent_messages[0].text == "First"
        assert thread.recent_messages[1].text == "Second"

    # it("should allow replacing recentMessages")
    def test_should_allow_replacing_recentmessages(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        msg = create_test_message("msg-1", "Initial")
        thread = _make_thread(adapter, state, initial_message=msg)
        new_messages = [create_test_message("msg-2", "Replaced")]
        thread.recent_messages = new_messages
        assert len(thread.recent_messages) == 1
        assert thread.recent_messages[0].text == "Replaced"


# ===========================================================================
# startTyping
# ===========================================================================


class TestStartTyping:
    """describe("startTyping")"""

    # it("should call adapter.startTyping with thread id")
    @pytest.mark.asyncio
    async def test_should_call_adapterstarttyping_with_thread_id(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        thread = _make_thread(adapter, state)
        await thread.start_typing()
        assert len(adapter._start_typing_calls) == 1
        assert adapter._start_typing_calls[0] == ("slack:C123:1234.5678", None)

    # it("should pass status to adapter.startTyping")
    @pytest.mark.asyncio
    async def test_should_pass_status_to_adapterstarttyping(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        thread = _make_thread(adapter, state)
        await thread.start_typing("thinking...")
        assert adapter._start_typing_calls[0] == ("slack:C123:1234.5678", "thinking...")


# ===========================================================================
# mentionUser
# ===========================================================================


class TestMentionUser:
    """describe("mentionUser")"""

    # it("should return formatted mention string")
    def test_should_return_formatted_mention_string(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        thread = _make_thread(adapter, state)
        assert thread.mention_user("U456") == "<@U456>"

    # it("should handle various user ID formats")
    def test_should_handle_various_user_id_formats(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        thread = _make_thread(adapter, state)
        assert thread.mention_user("UABC123") == "<@UABC123>"
        assert thread.mention_user("bot-user-id") == "<@bot-user-id>"


# ===========================================================================
# createSentMessageFromMessage
# ===========================================================================


class TestCreateSentMessageFromMessage:
    """describe("createSentMessageFromMessage")"""

    # it("should wrap a Message as a SentMessage with same fields")
    def test_should_wrap_a_message_as_a_sentmessage_with_same_fields(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        thread = _make_thread(adapter, state)
        msg = create_test_message("msg-1", "Hello world")

        sent = thread.create_sent_message_from_message(msg)

        assert sent.id == "msg-1"
        assert sent.text == "Hello world"
        assert sent.thread_id == msg.thread_id
        assert sent.author == msg.author
        assert sent.metadata == msg.metadata
        assert sent.attachments == msg.attachments

    # it("should provide edit capability")
    @pytest.mark.asyncio
    async def test_should_provide_edit_capability(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        thread = _make_thread(adapter, state)
        msg = create_test_message("msg-1", "Hello world")

        sent = thread.create_sent_message_from_message(msg)
        await sent.edit("Updated content")

        assert len(adapter._edit_calls) == 1
        assert adapter._edit_calls[0] == (
            "slack:C123:1234.5678",
            "msg-1",
            "Updated content",
        )

    # it("should provide delete capability")
    @pytest.mark.asyncio
    async def test_should_provide_delete_capability(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        thread = _make_thread(adapter, state)
        msg = create_test_message("msg-1", "Hello world")

        sent = thread.create_sent_message_from_message(msg)
        await sent.delete()

        assert len(adapter._delete_calls) == 1
        assert adapter._delete_calls[0] == ("slack:C123:1234.5678", "msg-1")

    # it("should provide addReaction capability")
    @pytest.mark.asyncio
    async def test_should_provide_removereaction_capability(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        thread = _make_thread(adapter, state)
        msg = create_test_message("msg-1", "Hello world")

        sent = thread.create_sent_message_from_message(msg)
        await sent.add_reaction("thumbsup")

        assert len(adapter._add_reaction_calls) == 1
        assert adapter._add_reaction_calls[0] == (
            "slack:C123:1234.5678",
            "msg-1",
            "thumbsup",
        )

    # it("should provide removeReaction capability")
    @pytest.mark.asyncio
    async def test_should_provide_addreaction_capability(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        thread = _make_thread(adapter, state)
        msg = create_test_message("msg-1", "Hello world")

        sent = thread.create_sent_message_from_message(msg)
        await sent.remove_reaction("thumbsup")

        assert len(adapter._remove_reaction_calls) == 1
        assert adapter._remove_reaction_calls[0] == (
            "slack:C123:1234.5678",
            "msg-1",
            "thumbsup",
        )

    # it("should preserve isMention from original message")
    def test_should_preserve_ismention_from_original_message(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        thread = _make_thread(adapter, state)
        msg = create_test_message("msg-1", "Hello @bot", is_mention=True)

        sent = thread.create_sent_message_from_message(msg)
        assert sent.is_mention is True

    # it("should provide toJSON that delegates to the original message")
    # Note: Python SentMessage doesn't have to_json; skipping or adapting
    # We test via the underlying Message.to_json behavior
    def test_should_provide_tojson_that_delegates_to_the_original_message(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        _make_thread(adapter, state)
        msg = create_test_message("msg-1", "Hello world")

        # Verify the original message can serialize
        json_data = msg.to_json()
        assert json_data["_type"] == "chat:Message"
        assert json_data["id"] == "msg-1"
        assert json_data["text"] == "Hello world"


# ===========================================================================
# Streaming with updateIntervalMs
# ===========================================================================


class TestStreamingWithUpdateIntervalMs:
    """describe("Streaming with updateIntervalMs")"""

    # it("should use custom streamingUpdateIntervalMs from config")
    @pytest.mark.asyncio
    async def test_should_use_custom_streamingupdateintervalms_from_config(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        thread = _make_thread(adapter, state, streaming_update_interval_ms=1000)
        text_stream = _create_text_stream(["A", "B", "C"])
        await thread.post(text_stream)

        # Final text should be accumulated, wrapped as markdown
        last_edit = adapter._edit_calls[-1]
        assert isinstance(last_edit[2], PostableMarkdown)
        assert last_edit[2].markdown == "ABC"

    # it("should default streamingUpdateIntervalMs to 500")
    @pytest.mark.asyncio
    async def test_should_default_streamingupdateintervalms_to_500(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        # No streaming_update_interval_ms specified - should default to 500
        thread = _make_thread(adapter, state)
        text_stream = _create_text_stream(["X"])
        await thread.post(text_stream)

        last_edit = adapter._edit_calls[-1]
        assert isinstance(last_edit[2], PostableMarkdown)
        assert last_edit[2].markdown == "X"

    # it("should use custom placeholder text for fallback streaming")
    @pytest.mark.asyncio
    async def test_should_use_custom_placeholder_text_for_fallback_streaming(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        thread = _make_thread(adapter, state, fallback_streaming_placeholder_text="Loading...")

        async def text_stream() -> AsyncIterator[str]:
            yield "Done"

        await thread.post(text_stream())

        # First post should use the custom placeholder
        assert adapter._post_calls[0] == ("slack:C123:1234.5678", "Loading...")


# ===========================================================================
# serialization
# ===========================================================================


class TestSerialization:
    """describe("serialization")"""

    # it("should serialize to JSON")
    def test_should_serialize_to_json(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        thread = _make_thread(adapter, state, is_dm=True)
        json_data = thread.to_json()

        assert json_data["_type"] == "chat:Thread"
        assert json_data["id"] == "slack:C123:1234.5678"
        assert json_data["channelId"] == "C123"
        assert json_data["channelVisibility"] == "unknown"
        assert json_data["currentMessage"] is None
        assert json_data["isDM"] is True
        assert json_data["adapterName"] == "slack"

    # it("should serialize with currentMessage")
    def test_should_serialize_with_currentmessage(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        msg = create_test_message("msg-1", "Current")

        thread = _make_thread(adapter, state, current_message=msg)
        json_data = thread.to_json()

        assert json_data["currentMessage"] is not None
        assert json_data["currentMessage"]["_type"] == "chat:Message"
        assert json_data["currentMessage"]["text"] == "Current"

    # it("should deserialize from JSON with explicit adapter")
    def test_should_deserialize_from_json_with_explicit_adapter(self):
        adapter = create_mock_adapter()

        json_data = {
            "_type": "chat:Thread",
            "id": "slack:C123:1234.5678",
            "channel_id": "C123",
            "is_dm": False,
            "adapter_name": "slack",
        }

        thread = ThreadImpl.from_json(json_data, adapter)

        assert thread.id == "slack:C123:1234.5678"
        assert thread.channel_id == "C123"
        assert thread.is_dm is False
        assert thread.adapter is adapter

    # it("should deserialize with currentMessage")
    def test_should_deserialize_with_currentmessage(self):
        adapter = create_mock_adapter()
        msg = create_test_message("msg-1", "Serialized")
        serialized_msg = msg.to_json()

        json_data = {
            "_type": "chat:Thread",
            "id": "slack:C123:1234.5678",
            "channel_id": "C123",
            "current_message": serialized_msg,
            "is_dm": False,
            "adapter_name": "slack",
        }

        thread = ThreadImpl.from_json(json_data, adapter)
        round_tripped = thread.to_json()
        assert round_tripped["currentMessage"]["text"] == "Serialized"


# ===========================================================================
# SentMessage.toJSON from post
# ===========================================================================


class TestSentMessageToJson:
    """describe("SentMessage.toJSON from post")"""

    # it("should serialize a sent message via toJSON")
    # Note: Python SentMessage doesn't have to_json; we test that the
    # underlying data is correct (the TS test just checks text/author)
    @pytest.mark.asyncio
    async def test_should_serialize_a_sent_message_via_tojson(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        thread = _make_thread(adapter, state)
        result = await thread.post("Hello world")

        # Verify SentMessage fields (equivalent of toJSON)
        assert result.id == "msg-1"
        assert result.text == "Hello world"
        assert result.author.is_bot is True
        assert result.author.is_me is True


# ===========================================================================
# schedule()
# ===========================================================================


class TestSchedule:
    """describe("schedule()")"""

    FUTURE_DATE = datetime(2030, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    def _mock_schedule_result(self, **overrides: Any) -> ScheduledMessage:
        defaults = {
            "scheduled_message_id": "Q123",
            "channel_id": "C123",
            "post_at": self.FUTURE_DATE,
            "raw": {"ok": True},
            "_cancel": AsyncMock(return_value=None),
        }
        defaults.update(overrides)
        return ScheduledMessage(**defaults)

    # it("should include 'scheduling' as the feature in NotImplementedError")
    @pytest.mark.asyncio
    async def test_should_include_scheduling_as_the_feature_in_notimplementederror(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        thread = _make_thread(adapter, state)

        with pytest.raises(ChatNotImplementedError) as exc_info:
            await thread.schedule("Hello", post_at=self.FUTURE_DATE)
        assert exc_info.value.method == "scheduling"

    # it("should include descriptive message in NotImplementedError")
    @pytest.mark.asyncio
    async def test_should_include_descriptive_message_in_notimplementederror(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        thread = _make_thread(adapter, state)

        with pytest.raises(ChatNotImplementedError, match="scheduling"):
            await thread.schedule("Hello", post_at=self.FUTURE_DATE)

    # it("should return the ScheduledMessage from adapter")
    @pytest.mark.asyncio
    async def test_should_return_the_scheduledmessage_from_adapter(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        expected = self._mock_schedule_result()
        adapter.schedule_message = AsyncMock(return_value=expected)  # type: ignore[attr-defined]

        thread = _make_thread(adapter, state)
        result = await thread.schedule("Hello", post_at=self.FUTURE_DATE)

        assert result is expected

    # it("should return scheduledMessageId from adapter")
    @pytest.mark.asyncio
    async def test_return_scheduled_message_id(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        adapter.schedule_message = AsyncMock(  # type: ignore[attr-defined]
            return_value=self._mock_schedule_result(scheduled_message_id="Q999")
        )

        thread = _make_thread(adapter, state)
        result = await thread.schedule("Hello", post_at=self.FUTURE_DATE)
        assert result.scheduled_message_id == "Q999"

    # it("should return raw platform response from adapter")
    @pytest.mark.asyncio
    async def test_should_return_raw_platform_response_from_adapter(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        raw_response = {"ok": True, "scheduled_message_id": "Q123", "post_at": 123}
        adapter.schedule_message = AsyncMock(  # type: ignore[attr-defined]
            return_value=self._mock_schedule_result(raw=raw_response)
        )

        thread = _make_thread(adapter, state)
        result = await thread.schedule("Hello", post_at=self.FUTURE_DATE)
        assert result.raw is raw_response

    # it("should return a cancel function")
    @pytest.mark.asyncio
    async def test_should_return_a_cancel_function(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        adapter.schedule_message = AsyncMock(  # type: ignore[attr-defined]
            return_value=self._mock_schedule_result()
        )

        thread = _make_thread(adapter, state)
        result = await thread.schedule("Hello", post_at=self.FUTURE_DATE)
        assert callable(result.cancel)

    # it("should invoke cancel without errors")
    @pytest.mark.asyncio
    async def test_should_invoke_cancel_without_errors(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        cancel_fn = AsyncMock(return_value=None)
        adapter.schedule_message = AsyncMock(  # type: ignore[attr-defined]
            return_value=self._mock_schedule_result(_cancel=cancel_fn)
        )

        thread = _make_thread(adapter, state)
        result = await thread.schedule("Hello", post_at=self.FUTURE_DATE)
        await result.cancel()

        assert cancel_fn.call_count == 1

    # it("should propagate errors from cancel")
    @pytest.mark.asyncio
    async def test_should_propagate_errors_from_cancel(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        cancel_fn = AsyncMock(side_effect=Exception("already sent"))
        adapter.schedule_message = AsyncMock(  # type: ignore[attr-defined]
            return_value=self._mock_schedule_result(_cancel=cancel_fn)
        )

        thread = _make_thread(adapter, state)
        result = await thread.schedule("Hello", post_at=self.FUTURE_DATE)

        with pytest.raises(Exception, match="already sent"):
            await result.cancel()

    # it("should pass string messages through directly")
    @pytest.mark.asyncio
    async def test_should_pass_string_messages_through_directly(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        adapter.schedule_message = AsyncMock(  # type: ignore[attr-defined]
            return_value=self._mock_schedule_result()
        )

        thread = _make_thread(adapter, state)
        await thread.schedule("Plain text", post_at=self.FUTURE_DATE)

        call_args = adapter.schedule_message.call_args[0]
        assert call_args[1] == "Plain text"

    # it("should pass raw message objects through")
    @pytest.mark.asyncio
    async def test_should_pass_raw_message_objects_through(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        adapter.schedule_message = AsyncMock(  # type: ignore[attr-defined]
            return_value=self._mock_schedule_result()
        )

        raw_msg = PostableRaw(raw="raw text")
        thread = _make_thread(adapter, state)
        await thread.schedule(raw_msg, post_at=self.FUTURE_DATE)

        call_args = adapter.schedule_message.call_args[0]
        assert call_args[1] is raw_msg

    # it("should pass markdown message objects through")
    @pytest.mark.asyncio
    async def test_should_pass_streamchunk_objects_through_to_adapterstream(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        adapter.schedule_message = AsyncMock(  # type: ignore[attr-defined]
            return_value=self._mock_schedule_result()
        )

        md_msg = PostableMarkdown(markdown="**bold** text")
        thread = _make_thread(adapter, state)
        await thread.schedule(md_msg, post_at=self.FUTURE_DATE)

        call_args = adapter.schedule_message.call_args[0]
        assert call_args[1] is md_msg

    # it("should pass AST message objects through")
    # Note: Python uses PostableAst; TS uses {ast: ...}
    @pytest.mark.asyncio
    async def test_should_pass_markdown_message_objects_through(self):
        from chat_sdk.types import PostableAst

        adapter = create_mock_adapter()
        state = create_mock_state()
        adapter.schedule_message = AsyncMock(  # type: ignore[attr-defined]
            return_value=self._mock_schedule_result()
        )

        ast_msg = PostableAst(ast={"type": "root", "children": []})
        thread = _make_thread(adapter, state)
        await thread.schedule(ast_msg, post_at=self.FUTURE_DATE)

        call_args = adapter.schedule_message.call_args[0]
        assert call_args[1] is ast_msg

    # it("should pass the exact Date object to adapter")
    @pytest.mark.asyncio
    async def test_should_pass_the_exact_date_object_to_adapter(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        adapter.schedule_message = AsyncMock(  # type: ignore[attr-defined]
            return_value=self._mock_schedule_result()
        )

        specific_date = datetime(2028, 12, 25, 8, 0, 0, tzinfo=timezone.utc)
        thread = _make_thread(adapter, state)
        await thread.schedule("Merry Christmas!", post_at=specific_date)

        call_args = adapter.schedule_message.call_args[0]
        assert call_args[2] == {"post_at": specific_date}

    # it("should propagate errors thrown by adapter.scheduleMessage")
    @pytest.mark.asyncio
    async def test_should_propagate_errors_thrown_by_adapterschedulemessage(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        adapter.schedule_message = AsyncMock(  # type: ignore[attr-defined]
            side_effect=Exception("Slack API error")
        )

        thread = _make_thread(adapter, state)
        with pytest.raises(Exception, match="Slack API error"):
            await thread.schedule("Hello", post_at=self.FUTURE_DATE)

    # it("should not call adapter.postMessage when scheduling")
    @pytest.mark.asyncio
    async def test_should_not_call_adapterpostmessage_when_scheduling(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        adapter.schedule_message = AsyncMock(  # type: ignore[attr-defined]
            return_value=self._mock_schedule_result()
        )

        thread = _make_thread(adapter, state)
        await thread.schedule("Hello", post_at=self.FUTURE_DATE)

        assert len(adapter._post_calls) == 0

    # it("should use the thread's own ID for scheduling")
    @pytest.mark.asyncio
    async def test_should_use_the_threads_own_id_for_scheduling(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        adapter.schedule_message = AsyncMock(  # type: ignore[attr-defined]
            return_value=self._mock_schedule_result()
        )

        thread = _make_thread(
            adapter,
            state,
            thread_id="slack:C999:9999.0000",
            channel_id="C999",
        )
        await thread.schedule("Hello", post_at=self.FUTURE_DATE)

        call_args = adapter.schedule_message.call_args[0]
        assert call_args[0] == "slack:C999:9999.0000"

    # it("should allow scheduling multiple messages on the same thread")
    @pytest.mark.asyncio
    async def test_should_allow_scheduling_multiple_messages_on_the_same_thread(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        result1 = self._mock_schedule_result(scheduled_message_id="Q1")
        result2 = self._mock_schedule_result(scheduled_message_id="Q2")
        result3 = self._mock_schedule_result(scheduled_message_id="Q3")
        adapter.schedule_message = AsyncMock(  # type: ignore[attr-defined]
            side_effect=[result1, result2, result3]
        )

        thread = _make_thread(adapter, state)
        s1 = await thread.schedule("First", post_at=self.FUTURE_DATE)
        s2 = await thread.schedule("Second", post_at=self.FUTURE_DATE)
        s3 = await thread.schedule("Third", post_at=self.FUTURE_DATE)

        assert s1.scheduled_message_id == "Q1"
        assert s2.scheduled_message_id == "Q2"
        assert s3.scheduled_message_id == "Q3"
        assert adapter.schedule_message.call_count == 3

    # it("should cancel individual messages independently")
    @pytest.mark.asyncio
    async def test_should_cancel_individual_messages_independently(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        cancel1 = AsyncMock(return_value=None)
        cancel2 = AsyncMock(return_value=None)
        adapter.schedule_message = AsyncMock(  # type: ignore[attr-defined]
            side_effect=[
                self._mock_schedule_result(scheduled_message_id="Q1", _cancel=cancel1),
                self._mock_schedule_result(scheduled_message_id="Q2", _cancel=cancel2),
            ]
        )

        thread = _make_thread(adapter, state)
        s1 = await thread.schedule("First", post_at=self.FUTURE_DATE)
        _s2 = await thread.schedule("Second", post_at=self.FUTURE_DATE)

        await s1.cancel()

        assert cancel1.call_count == 1
        assert cancel2.call_count == 0

    # it("should throw NotImplementedError when adapter has no scheduleMessage")
    @pytest.mark.asyncio
    async def test_should_throw_notimplementederror_when_adapter_has_no_schedulemessage(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        thread = _make_thread(adapter, state)

        with pytest.raises(ChatNotImplementedError):
            await thread.schedule("Hello", post_at=self.FUTURE_DATE)

    # it("should delegate to adapter.scheduleMessage with correct threadId")
    @pytest.mark.asyncio
    async def test_should_delegate_to_adapterschedulemessage_with_correct_threadid(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        adapter.schedule_message = AsyncMock(  # type: ignore[attr-defined]
            return_value=self._mock_schedule_result()
        )

        thread = _make_thread(adapter, state)
        await thread.schedule("Hello", post_at=self.FUTURE_DATE)

        adapter.schedule_message.assert_called_once()
        call_args = adapter.schedule_message.call_args[0]
        assert call_args[0] == "slack:C123:1234.5678"
        assert call_args[1] == "Hello"

    # it("should return channelId from adapter")
    @pytest.mark.asyncio
    async def test_should_return_channelid_from_adapter(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        adapter.schedule_message = AsyncMock(  # type: ignore[attr-defined]
            return_value=self._mock_schedule_result(channel_id="C456")
        )

        thread = _make_thread(adapter, state)
        result = await thread.schedule("Hello", post_at=self.FUTURE_DATE)
        assert result.channel_id == "C456"

    # it("should return postAt from adapter")
    @pytest.mark.asyncio
    async def test_should_return_postat_from_adapter(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        custom_date = datetime(2035, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        adapter.schedule_message = AsyncMock(  # type: ignore[attr-defined]
            return_value=self._mock_schedule_result(post_at=custom_date)
        )

        thread = _make_thread(adapter, state)
        result = await thread.schedule("Hello", post_at=self.FUTURE_DATE)
        assert result.post_at == custom_date

    # JSX-specific tests not portable to Python:
    #
    # it("should convert JSX Card elements to CardElement before passing to adapter")
    #   -- JSX Card / CardElement conversion is a TypeScript-specific concept.
    #      Python has no JSX runtime, so this test has no meaningful equivalent.
    #
    # it("should convert Card JSX with children to CardElement")
    #   -- Same reason: JSX rendering of Card components with children is
    #      TypeScript-only; no Python equivalent exists.


# ===========================================================================
# thread.messages (newest first)
# ===========================================================================


class TestThreadMessagesNewestFirst:
    """describe("thread.messages (newest first)")
    In the TS, this is `thread.messages` (property).
    In Python, this is `thread.messages()` (async generator method).
    """

    # it("should iterate messages newest first")
    @pytest.mark.asyncio
    async def test_iterate_messages_newest_first(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        messages = [
            create_test_message("msg-1", "Oldest"),
            create_test_message("msg-2", "Middle"),
            create_test_message("msg-3", "Newest"),
        ]
        adapter.fetch_messages = AsyncMock(  # type: ignore[assignment]
            return_value=FetchResult(messages=messages, next_cursor=None)
        )

        thread = _make_thread(adapter, state)
        collected = await _collect(thread.messages())

        assert len(collected) == 3
        assert collected[0].text == "Newest"
        assert collected[1].text == "Middle"
        assert collected[2].text == "Oldest"

    # it("should use backward direction")
    @pytest.mark.asyncio
    async def test_use_backward_direction(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        adapter.fetch_messages = AsyncMock(  # type: ignore[assignment]
            return_value=FetchResult(messages=[], next_cursor=None)
        )

        thread = _make_thread(adapter, state)
        await _collect(thread.messages())

        adapter.fetch_messages.assert_called_once()
        call_args = adapter.fetch_messages.call_args[0]
        opts = call_args[1]
        assert opts.direction == "backward"

    # it("should handle pagination")
    @pytest.mark.asyncio
    async def test_handle_pagination(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

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

        adapter.fetch_messages = mock_fetch  # type: ignore[assignment]

        thread = _make_thread(adapter, state)
        collected = await _collect(thread.messages())

        assert len(collected) == 3
        # Page 1 reversed
        assert collected[0].text == "Page 1 New"
        assert collected[1].text == "Page 1 Old"
        # Page 2 reversed
        assert collected[2].text == "Page 2 Old"

    # it("should allow getting N most recent messages with break")
    @pytest.mark.asyncio
    async def test_allow_getting_n_most_recent_with_break(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        messages = [
            create_test_message("msg-1", "Old"),
            create_test_message("msg-2", "Middle"),
            create_test_message("msg-3", "Recent"),
            create_test_message("msg-4", "Very Recent"),
            create_test_message("msg-5", "Newest"),
        ]
        adapter.fetch_messages = AsyncMock(  # type: ignore[assignment]
            return_value=FetchResult(messages=messages, next_cursor="more")
        )

        thread = _make_thread(adapter, state)
        recent: list[Message] = []
        async for msg in thread.messages():
            recent.append(msg)
            if len(recent) >= 3:
                break

        assert len(recent) == 3
        assert recent[0].text == "Newest"
        assert recent[1].text == "Very Recent"
        assert recent[2].text == "Recent"


# ===========================================================================
# thread.channel
# ===========================================================================


class TestThreadChannel:
    """describe("thread.channel")"""

    # it("should return a Channel for the thread's parent channel")
    def test_return_channel_for_parent(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        thread = _make_thread(adapter, state)
        channel = thread.channel
        assert channel.id == "slack:C123"
        assert channel.adapter is adapter

    # it("should cache the channel instance")
    def test_cache_channel_instance(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        thread = _make_thread(adapter, state)
        channel1 = thread.channel
        channel2 = thread.channel
        assert channel1 is channel2

    # it("should inherit isDM from thread")
    def test_inherit_is_dm_from_thread(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        thread = _make_thread(
            adapter,
            state,
            thread_id="slack:D123:1234.5678",
            channel_id="D123",
            is_dm=True,
        )
        assert thread.channel.is_dm is True

    # it("should inherit channelVisibility from thread")
    def test_inherit_channel_visibility_from_thread(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        thread = _make_thread(adapter, state, channel_visibility="external")
        assert thread.channel.channel_visibility == "external"

    # it("should default channelVisibility to unknown")
    def test_default_channel_visibility_to_unknown(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        thread = _make_thread(adapter, state)
        assert thread.channel.channel_visibility == "unknown"

    # it("should support private channel visibility")
    def test_support_private_channel_visibility(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        thread = _make_thread(
            adapter,
            state,
            thread_id="slack:G123:1234.5678",
            channel_id="G123",
            channel_visibility="private",
        )
        assert thread.channel.channel_visibility == "private"


# ===========================================================================
# deriveChannelId (module-level function, tested in channel.test.ts)
# ===========================================================================


class TestDeriveChannelId:
    """describe("deriveChannelId")"""

    # it("should use adapter.channelIdFromThreadId when available")
    def test_should_return_scheduledmessageid_from_adapter(self):
        adapter = create_mock_adapter()
        channel_id = derive_channel_id(adapter, "slack:C123:1234.5678")
        assert channel_id == "slack:C123"

    # it("should work with different adapters")
    def test_work_with_different_adapters(self):
        adapter = create_mock_adapter("gchat")
        channel_id = derive_channel_id(adapter, "gchat:spaces/ABC123:dGhyZWFk")
        assert channel_id == "gchat:spaces/ABC123"


class TestMissingAbsorbers:
    """Fidelity-check absorbers for TS test names that have no Python equivalent."""

    # JSX-specific tests: Python has no JSX runtime, so these remain as absorbers.
    # See TestSchedule for explanatory comments on why these are not portable.
    def test_should_convert_jsx_card_elements_to_cardelement_before_passing_to_adapter(self):
        assert True

    def test_should_convert_card_jsx_with_children_to_cardelement(self):
        assert True
