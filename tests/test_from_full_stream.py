"""Tests for from_full_stream: text streams, StreamChunk objects, AI SDK event streams.

Port of from-full-stream.ts tests.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from chat_sdk.from_full_stream import from_full_stream
from chat_sdk.types import StreamChunk

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _async_iter(items: list[Any]) -> AsyncIterator[Any]:
    """Create an async iterator from a list of items."""
    for item in items:
        yield item


async def _collect(stream: AsyncIterator[str | StreamChunk]) -> list[str | StreamChunk]:
    """Collect all items from an async iterator."""
    result: list[str | StreamChunk] = []
    async for item in stream:
        result.append(item)
    return result


# ---------------------------------------------------------------------------
# Plain text streams
# ---------------------------------------------------------------------------


class TestPlainTextStreams:
    @pytest.mark.asyncio
    async def test_passes_through_strings(self):
        items = ["Hello", " ", "World"]
        result = await _collect(from_full_stream(_async_iter(items)))
        assert result == ["Hello", " ", "World"]

    @pytest.mark.asyncio
    async def test_empty_stream(self):
        result = await _collect(from_full_stream(_async_iter([])))
        assert result == []

    @pytest.mark.asyncio
    async def test_single_string(self):
        result = await _collect(from_full_stream(_async_iter(["hello"])))
        assert result == ["hello"]


# ---------------------------------------------------------------------------
# StreamChunk passthrough
# ---------------------------------------------------------------------------


class TestStreamChunkPassthrough:
    @pytest.mark.asyncio
    async def test_markdown_text_chunk(self):
        chunk: StreamChunk = {"type": "markdown_text", "text": "# Hello"}
        result = await _collect(from_full_stream(_async_iter([chunk])))
        assert len(result) == 1
        assert result[0] == chunk

    @pytest.mark.asyncio
    async def test_task_update_chunk(self):
        chunk: StreamChunk = {"type": "task_update", "task_id": "t1", "status": "running"}
        result = await _collect(from_full_stream(_async_iter([chunk])))
        assert len(result) == 1
        assert result[0]["type"] == "task_update"

    @pytest.mark.asyncio
    async def test_plan_update_chunk(self):
        chunk: StreamChunk = {"type": "plan_update", "plan": "step1"}
        result = await _collect(from_full_stream(_async_iter([chunk])))
        assert len(result) == 1
        assert result[0]["type"] == "plan_update"

    @pytest.mark.asyncio
    async def test_mixed_text_and_chunks(self):
        items: list[Any] = [
            "plain text",
            {"type": "markdown_text", "text": "# Heading"},
            "more text",
        ]
        result = await _collect(from_full_stream(_async_iter(items)))
        assert len(result) == 3
        assert result[0] == "plain text"
        assert isinstance(result[1], dict)
        assert result[1]["type"] == "markdown_text"
        assert result[2] == "more text"


# ---------------------------------------------------------------------------
# AI SDK text-delta events
# ---------------------------------------------------------------------------


class TestTextDeltaEvents:
    @pytest.mark.asyncio
    async def test_extracts_text_from_text_delta_events(self):
        events: list[Any] = [
            {"type": "text-delta", "textDelta": "Hello"},
            {"type": "text-delta", "textDelta": " World"},
        ]
        result = await _collect(from_full_stream(_async_iter(events)))
        assert result == ["Hello", " World"]

    @pytest.mark.asyncio
    async def test_extracts_text_delta_v6_format(self):
        events: list[Any] = [
            {"type": "text-delta", "text_delta": "Hello"},
            {"type": "text-delta", "text_delta": " World"},
        ]
        result = await _collect(from_full_stream(_async_iter(events)))
        assert result == ["Hello", " World"]

    @pytest.mark.asyncio
    async def test_extracts_text_from_text_field(self):
        events: list[Any] = [
            {"type": "text-delta", "text": "Hello from text field"},
        ]
        result = await _collect(from_full_stream(_async_iter(events)))
        assert result == ["Hello from text field"]

    @pytest.mark.asyncio
    async def test_extracts_text_from_delta_field(self):
        events: list[Any] = [
            {"type": "text-delta", "delta": "Delta content"},
        ]
        result = await _collect(from_full_stream(_async_iter(events)))
        assert result == ["Delta content"]


# ---------------------------------------------------------------------------
# Step separators
# ---------------------------------------------------------------------------


class TestStepSeparators:
    @pytest.mark.asyncio
    async def test_inserts_separator_between_steps(self):
        events: list[Any] = [
            {"type": "text-delta", "textDelta": "Step 1"},
            {"type": "finish-step"},
            {"type": "text-delta", "textDelta": "Step 2"},
        ]
        result = await _collect(from_full_stream(_async_iter(events)))
        assert result == ["Step 1", "\n\n", "Step 2"]

    @pytest.mark.asyncio
    async def test_no_separator_before_first_text(self):
        events: list[Any] = [
            {"type": "finish-step"},
            {"type": "text-delta", "textDelta": "First text"},
        ]
        result = await _collect(from_full_stream(_async_iter(events)))
        assert result == ["First text"]

    @pytest.mark.asyncio
    async def test_multiple_steps(self):
        events: list[Any] = [
            {"type": "text-delta", "textDelta": "A"},
            {"type": "finish-step"},
            {"type": "text-delta", "textDelta": "B"},
            {"type": "finish-step"},
            {"type": "text-delta", "textDelta": "C"},
        ]
        result = await _collect(from_full_stream(_async_iter(events)))
        assert result == ["A", "\n\n", "B", "\n\n", "C"]

    @pytest.mark.asyncio
    async def test_consecutive_finish_steps_only_one_separator(self):
        events: list[Any] = [
            {"type": "text-delta", "textDelta": "A"},
            {"type": "finish-step"},
            {"type": "finish-step"},
            {"type": "finish-step"},
            {"type": "text-delta", "textDelta": "B"},
        ]
        result = await _collect(from_full_stream(_async_iter(events)))
        # Only one separator should be inserted regardless of how many finish-steps
        assert result == ["A", "\n\n", "B"]


# ---------------------------------------------------------------------------
# Skipped / ignored events
# ---------------------------------------------------------------------------


class TestSkippedEvents:
    @pytest.mark.asyncio
    async def test_skips_none_values(self):
        events: list[Any] = [None, "text", None]
        result = await _collect(from_full_stream(_async_iter(events)))
        assert result == ["text"]

    @pytest.mark.asyncio
    async def test_skips_non_dict_objects(self):
        events: list[Any] = [42, True, 3.14, "text"]
        result = await _collect(from_full_stream(_async_iter(events)))
        assert result == ["text"]

    @pytest.mark.asyncio
    async def test_skips_dicts_without_type(self):
        events: list[Any] = [
            {"data": "no type field"},
            {"type": "text-delta", "textDelta": "valid"},
        ]
        result = await _collect(from_full_stream(_async_iter(events)))
        assert result == ["valid"]

    @pytest.mark.asyncio
    async def test_skips_unknown_event_types(self):
        events: list[Any] = [
            {"type": "unknown-event", "data": "ignored"},
            {"type": "tool-call", "name": "search"},
            {"type": "text-delta", "textDelta": "visible"},
        ]
        result = await _collect(from_full_stream(_async_iter(events)))
        assert result == ["visible"]

    @pytest.mark.asyncio
    async def test_skips_text_delta_with_no_text_content(self):
        events: list[Any] = [
            {"type": "text-delta"},  # No text/textDelta/delta field
        ]
        result = await _collect(from_full_stream(_async_iter(events)))
        assert result == []

    @pytest.mark.asyncio
    async def test_skips_text_delta_with_non_string_content(self):
        events: list[Any] = [
            {"type": "text-delta", "textDelta": 42},
            {"type": "text-delta", "textDelta": None},
        ]
        result = await _collect(from_full_stream(_async_iter(events)))
        assert result == []


# ---------------------------------------------------------------------------
# Complex mixed streams
# ---------------------------------------------------------------------------


class TestComplexMixedStreams:
    @pytest.mark.asyncio
    async def test_full_agent_stream(self):
        """Simulate a multi-step agent stream with tools and text."""
        events: list[Any] = [
            # Step 1: tool call (ignored) + text
            {"type": "tool-call", "name": "search"},
            {"type": "text-delta", "textDelta": "Found "},
            {"type": "text-delta", "textDelta": "results."},
            {"type": "finish-step"},
            # Step 2: more text
            {"type": "text-delta", "textDelta": "Here is a summary."},
            {"type": "finish-step"},
            # Step 3: StreamChunk interleaved
            {"type": "task_update", "task_id": "t1", "status": "done"},
            {"type": "text-delta", "textDelta": "Done!"},
        ]
        result = await _collect(from_full_stream(_async_iter(events)))
        # StreamChunks pass through immediately; separator is emitted before
        # the next text-delta, not before a StreamChunk.
        assert result == [
            "Found ",
            "results.",
            "\n\n",
            "Here is a summary.",
            {"type": "task_update", "task_id": "t1", "status": "done"},
            "\n\n",
            "Done!",
        ]

    @pytest.mark.asyncio
    async def test_stream_with_only_stream_chunks(self):
        events: list[Any] = [
            {"type": "plan_update", "plan": "step1"},
            {"type": "task_update", "task_id": "t1", "status": "pending"},
            {"type": "markdown_text", "text": "Hello"},
        ]
        result = await _collect(from_full_stream(_async_iter(events)))
        assert len(result) == 3
        assert all(isinstance(r, dict) for r in result)

    @pytest.mark.asyncio
    async def test_stream_with_strings_and_events_mixed(self):
        events: list[Any] = [
            "plain string",
            {"type": "text-delta", "textDelta": "from event"},
            "another string",
        ]
        result = await _collect(from_full_stream(_async_iter(events)))
        assert result == ["plain string", "from event", "another string"]
