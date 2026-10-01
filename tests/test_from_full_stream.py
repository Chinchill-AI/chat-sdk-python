"""Tests for from_full_stream: text streams, StreamChunk objects, AI SDK event streams.

Port of from-full-stream.ts tests.
"""

from __future__ import annotations

import enum
from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from chat_sdk.from_full_stream import from_full_stream
from chat_sdk.testing import create_mock_adapter, create_mock_state
from chat_sdk.thread import ThreadImpl, _ThreadImplConfig
from chat_sdk.types import PostableMarkdown, RawMessage, StreamChunk, ThinkingChunk

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


# ---------------------------------------------------------------------------
# Fidelity aliases -- map TS test names to existing Python tests
# ---------------------------------------------------------------------------


class TestFidelityAliases:
    """Fidelity aliases matching TS it() names to existing test logic."""

    async def test_extracts_textdelta_values(self):
        events = [{"type": "text-delta", "textDelta": "Hello"}, {"type": "text-delta", "textDelta": " World"}]
        assert await _collect(from_full_stream(_async_iter(events))) == ["Hello", " World"]

    async def test_does_not_add_trailing_separator_after_final_finishstep(self):
        events = [{"type": "text-delta", "textDelta": "A"}, {"type": "finish-step"}]
        result = await _collect(from_full_stream(_async_iter(events)))
        assert result == ["A"]

    async def test_skips_toolcall_and_other_nontext_events(self):
        events = [{"type": "tool-call", "name": "x"}, {"type": "text-delta", "textDelta": "ok"}]
        assert await _collect(from_full_stream(_async_iter(events))) == ["ok"]

    async def test_handles_consecutive_finishstep_events(self):
        events = [
            {"type": "text-delta", "textDelta": "A"},
            {"type": "finish-step"},
            {"type": "finish-step"},
            {"type": "text-delta", "textDelta": "B"},
        ]
        assert await _collect(from_full_stream(_async_iter(events))) == ["A", "\n\n", "B"]

    async def test_does_not_inject_separator_when_finishstep_comes_before_any_text(self):
        events = [{"type": "finish-step"}, {"type": "text-delta", "textDelta": "First"}]
        assert await _collect(from_full_stream(_async_iter(events))) == ["First"]

    async def test_ignores_textdelta_with_nonstring_textdelta(self):
        events = [{"type": "text-delta", "textDelta": 42}, {"type": "text-delta", "textDelta": None}]
        assert await _collect(from_full_stream(_async_iter(events))) == []

    async def test_extracts_textdelta_with_text_property_ai_sdk_v6(self):
        events = [{"type": "text-delta", "text": "v6 text"}]
        assert await _collect(from_full_stream(_async_iter(events))) == ["v6 text"]

    async def test_injects_separator_between_steps_with_text_property(self):
        events = [
            {"type": "text-delta", "text": "A"},
            {"type": "finish-step"},
            {"type": "text-delta", "text": "B"},
        ]
        assert await _collect(from_full_stream(_async_iter(events))) == ["A", "\n\n", "B"]

    async def test_prefers_text_over_textdelta_when_both_present(self):
        events = [{"type": "text-delta", "text": "preferred", "textDelta": "fallback"}]
        assert await _collect(from_full_stream(_async_iter(events))) == ["preferred"]

    async def test_ignores_invalid_events_null_primitives_missing_type(self):
        events: list = [None, 42, True, {"no_type": 1}, {"type": "text-delta", "textDelta": "ok"}]
        assert await _collect(from_full_stream(_async_iter(events))) == ["ok"]


# ---------------------------------------------------------------------------
# ThinkingChunk opt-in (Python-only divergence, default-off == upstream)
# ---------------------------------------------------------------------------


class TestThinkingOptIn:
    """``emit_thinking`` surfaces AI-SDK reasoning as ``ThinkingChunk``.

    Default-off must be byte-for-byte upstream: reasoning parts are dropped
    and no ``ThinkingChunk`` is ever produced. Opt-in turns them into chunks.
    """

    async def test_default_off_drops_reasoning_delta(self):
        # Upstream chat@4.31 drops `reasoning-delta`; default-off must match.
        events: list[Any] = [
            {"type": "reasoning-delta", "text": "thinking hard"},
            {"type": "text-delta", "text": "answer"},
        ]
        result = await _collect(from_full_stream(_async_iter(events)))
        assert result == ["answer"]
        assert all(not isinstance(c, ThinkingChunk) for c in result)

    async def test_default_off_drops_reasoning_part(self):
        events: list[Any] = [
            {"type": "reasoning", "text": "step 1"},
            {"type": "text-delta", "text": "out"},
        ]
        result = await _collect(from_full_stream(_async_iter(events)))
        assert result == ["out"]

    async def test_default_off_drops_pydantic_thinking_part(self):
        # pydantic-ai shape: type == "thinking", content carries the text.
        events: list[Any] = [
            {"type": "thinking", "content": "reasoning"},
            {"type": "text-delta", "text": "final"},
        ]
        result = await _collect(from_full_stream(_async_iter(events)))
        assert result == ["final"]

    async def test_default_off_byte_identical_to_no_reasoning(self):
        # Output with reasoning parts (default-off) must equal output of the
        # same stream with the reasoning parts removed entirely.
        with_reasoning: list[Any] = [
            {"type": "text-delta", "text": "A"},
            {"type": "reasoning-delta", "text": "secret thought"},
            {"type": "finish-step"},
            {"type": "reasoning", "text": "more thought"},
            {"type": "text-delta", "text": "B"},
        ]
        without_reasoning: list[Any] = [
            {"type": "text-delta", "text": "A"},
            {"type": "finish-step"},
            {"type": "text-delta", "text": "B"},
        ]
        assert await _collect(from_full_stream(_async_iter(with_reasoning))) == await _collect(
            from_full_stream(_async_iter(without_reasoning))
        )

    async def test_opt_in_yields_thinking_chunk_from_reasoning_delta(self):
        events: list[Any] = [{"type": "reasoning-delta", "text": "analyzing"}]
        result = await _collect(from_full_stream(_async_iter(events), emit_thinking=True))
        assert len(result) == 1
        chunk = result[0]
        assert isinstance(chunk, ThinkingChunk)
        assert chunk.type == "thinking"
        assert chunk.content == "analyzing"

    async def test_opt_in_yields_thinking_chunk_from_reasoning_part(self):
        events: list[Any] = [{"type": "reasoning", "text": "deliberating"}]
        result = await _collect(from_full_stream(_async_iter(events), emit_thinking=True))
        assert result == [ThinkingChunk(content="deliberating")]

    async def test_opt_in_yields_thinking_chunk_from_pydantic_content(self):
        # pydantic-ai ThinkingPart carries text in `content`, not `text`.
        events: list[Any] = [{"type": "thinking", "content": "pondering"}]
        result = await _collect(from_full_stream(_async_iter(events), emit_thinking=True))
        assert result == [ThinkingChunk(content="pondering")]

    async def test_opt_in_interleaves_thinking_and_text(self):
        events: list[Any] = [
            {"type": "reasoning-delta", "text": "let me think"},
            {"type": "text-delta", "text": "the answer is"},
            {"type": "reasoning", "text": "double-checking"},
            {"type": "text-delta", "text": " 42"},
        ]
        result = await _collect(from_full_stream(_async_iter(events), emit_thinking=True))
        assert result == [
            ThinkingChunk(content="let me think"),
            "the answer is",
            ThinkingChunk(content="double-checking"),
            " 42",
        ]

    async def test_opt_in_skips_empty_reasoning(self):
        # No content => nothing emitted even with opt-in on.
        events: list[Any] = [
            {"type": "reasoning-delta", "text": ""},
            {"type": "reasoning"},
            {"type": "text-delta", "text": "x"},
        ]
        result = await _collect(from_full_stream(_async_iter(events), emit_thinking=True))
        assert result == ["x"]

    async def test_opt_in_does_not_change_text_output(self):
        # The text path is identical with the flag on; only thinking is added.
        events: list[Any] = [
            {"type": "text-delta", "text": "A"},
            {"type": "finish-step"},
            {"type": "text-delta", "text": "B"},
        ]
        off = await _collect(from_full_stream(_async_iter(events)))
        on = await _collect(from_full_stream(_async_iter(events), emit_thinking=True))
        assert off == on == ["A", "\n\n", "B"]


# ---------------------------------------------------------------------------
# AG-UI streams (TanStack AI chat()) — vercel/chat#934
# ---------------------------------------------------------------------------


async def _text(events: list[Any]) -> str:
    return "".join(str(item) for item in await _collect(from_full_stream(_async_iter(events))))


class TestAGUIStreamsTanStackAIChat:
    async def test_extracts_text_message_content_deltas(self):
        events = [
            {"type": "RUN_STARTED", "threadId": "t1", "runId": "r1"},
            {"type": "TEXT_MESSAGE_START", "messageId": "m1", "role": "assistant"},
            {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "hello"},
            {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": " world"},
            {"type": "TEXT_MESSAGE_END", "messageId": "m1"},
            {"type": "RUN_FINISHED", "threadId": "t1", "runId": "r1"},
        ]
        assert await _text(events) == "hello world"

    async def test_injects_separator_between_text_messages_in_a_tool_loop(self):
        events = [
            {"type": "TEXT_MESSAGE_START", "messageId": "m1", "role": "assistant"},
            {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "Looking."},
            {"type": "TEXT_MESSAGE_END", "messageId": "m1"},
            {"type": "TOOL_CALL_START", "toolCallId": "c1", "toolCallName": "search"},
            {"type": "TOOL_CALL_ARGS", "toolCallId": "c1", "delta": '{"q":"x"}'},
            {"type": "TOOL_CALL_END", "toolCallId": "c1"},
            {"type": "TOOL_CALL_RESULT", "toolCallId": "c1", "content": "data"},
            {"type": "TEXT_MESSAGE_START", "messageId": "m2", "role": "assistant"},
            {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m2", "delta": "Found it."},
            {"type": "TEXT_MESSAGE_END", "messageId": "m2"},
        ]
        assert await _text(events) == "Looking.\n\nFound it."

    async def test_does_not_add_trailing_separator_after_final_text_message_end(self):
        events = [
            {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "done"},
            {"type": "TEXT_MESSAGE_END", "messageId": "m1"},
            {"type": "RUN_FINISHED", "threadId": "t1", "runId": "r1"},
        ]
        assert await _text(events) == "done"

    async def test_does_not_inject_separator_when_text_message_end_comes_before_any_text(self):
        events = [
            {"type": "TEXT_MESSAGE_START", "messageId": "m0", "role": "assistant"},
            {"type": "TEXT_MESSAGE_END", "messageId": "m0"},
            {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "first"},
        ]
        assert await _text(events) == "first"

    async def test_skips_tool_call_args_deltas_even_though_they_carry_a_delta_field(self):
        events = [
            {"type": "TOOL_CALL_ARGS", "toolCallId": "c1", "delta": '{"secret":1}'},
            {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "visible"},
        ]
        assert await _text(events) == "visible"

    async def test_skips_reasoning_and_run_lifecycle_events(self):
        events = [
            {"type": "REASONING_START", "messageId": "r1"},
            {"type": "REASONING_MESSAGE_CONTENT", "messageId": "r1", "delta": "hmm"},
            {"type": "REASONING_END", "messageId": "r1"},
            {"type": "STEP_STARTED", "stepName": "think"},
            {"type": "STEP_FINISHED", "stepName": "think"},
            {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "answer"},
            {"type": "RUN_ERROR", "message": "boom"},
        ]
        assert await _text(events) == "answer"

    async def test_skips_state_delta_even_though_its_delta_is_an_array(self):
        events = [
            {"type": "STATE_DELTA", "delta": [{"op": "add", "path": "/x", "value": 1}]},
            {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "text"},
        ]
        assert await _text(events) == "text"

    async def test_ignores_text_message_content_with_nonstring_delta(self):
        events = [
            {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": 42},
            {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1"},
            {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "ok"},
        ]
        assert await _text(events) == "ok"

    async def test_handles_ai_sdk_and_agui_events_in_the_same_stream(self):
        events = [
            {"type": "text-delta", "textDelta": "a"},
            {"type": "finish-step"},
            {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "b"},
            {"type": "TEXT_MESSAGE_END", "messageId": "m1"},
            {"type": "text-delta", "text": "c"},
        ]
        assert await _text(events) == "a\n\nb\n\nc"


class TestAGUIPythonEventModels:
    """Python AG-UI producers (``ag-ui-protocol``, pydantic-ai) emit model
    objects whose ``type`` is a ``str`` Enum and whose fields are snake_case."""

    async def test_str_enum_typed_attribute_events(self):
        class EventType(str, enum.Enum):  # noqa: UP042 — the ag-ui-protocol shape; StrEnum is 3.11+
            TEXT_MESSAGE_CONTENT = "TEXT_MESSAGE_CONTENT"
            TEXT_MESSAGE_END = "TEXT_MESSAGE_END"
            TOOL_CALL_ARGS = "TOOL_CALL_ARGS"

        @dataclass
        class Event:
            type: EventType
            message_id: str = ""
            delta: Any = None

        events = [
            Event(EventType.TEXT_MESSAGE_CONTENT, "m1", "Looking."),
            Event(EventType.TEXT_MESSAGE_END, "m1"),
            Event(EventType.TOOL_CALL_ARGS, "c1", '{"q":"x"}'),
            Event(EventType.TEXT_MESSAGE_CONTENT, "m2", "Found it."),
        ]
        assert await _collect(from_full_stream(_async_iter(events))) == ["Looking.", "\n\n", "Found it."]

    async def test_plain_enum_type_is_compared_by_value(self):
        class EventType(enum.Enum):
            TEXT_MESSAGE_CONTENT = "TEXT_MESSAGE_CONTENT"

        events = [SimpleNamespace(type=EventType.TEXT_MESSAGE_CONTENT, delta="hi")]
        assert await _collect(from_full_stream(_async_iter(events))) == ["hi"]

    async def test_unhashable_type_value_is_skipped(self):
        events = [{"type": ["TEXT_MESSAGE_CONTENT"], "delta": "x"}, {"type": "TEXT_MESSAGE_CONTENT", "delta": "y"}]
        assert await _collect(from_full_stream(_async_iter(events))) == ["y"]


class TestAGUIThroughThreadPost:
    """``thread.post()`` normalises through the same implementation."""

    @staticmethod
    def _thread(adapter: Any) -> ThreadImpl:
        return ThreadImpl(
            _ThreadImplConfig(
                id="slack:C123:1.2", adapter=adapter, state_adapter=create_mock_state(), channel_id="slack:C123"
            )
        )

    async def test_native_stream_receives_agui_text_with_separators(self):
        adapter = create_mock_adapter("slack")
        received: list[Any] = []

        async def _stream(thread_id: str, stream: Any, options: Any) -> RawMessage:
            async for chunk in stream:
                received.append(chunk)
            return RawMessage(id="sent", thread_id=thread_id, raw={})

        adapter.stream = AsyncMock(side_effect=_stream)  # type: ignore[attr-defined]
        events = [
            {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "Looking."},
            {"type": "TEXT_MESSAGE_END", "messageId": "m1"},
            {"type": "TOOL_CALL_ARGS", "toolCallId": "c1", "delta": '{"secret":1}'},
            {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m2", "delta": "Found it."},
        ]

        sent = await self._thread(adapter).post(_async_iter(events))

        assert received == ["Looking.", "\n\n", "Found it."]
        assert sent.text == "Looking.\n\nFound it."

    async def test_fallback_stream_posts_agui_text(self):
        adapter = create_mock_adapter("slack")
        events = [
            {"type": "STATE_DELTA", "delta": [{"op": "add", "path": "/x", "value": 1}]},
            {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "a"},
            {"type": "TEXT_MESSAGE_END", "messageId": "m1"},
            {"type": "text-delta", "text": "b"},
        ]

        await self._thread(adapter).post(_async_iter(events))

        assert adapter._post_calls == [("slack:C123:1.2", "...")]
        assert adapter._edit_calls[-1] == ("slack:C123:1.2", "msg-1", PostableMarkdown(markdown="a\n\nb"))
