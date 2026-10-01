"""Slack native stream rotation (vercel/chat d4a1f03a, #884; issue #208).

Port of the ``native stream rotation`` describe in upstream
``packages/adapter-slack/src/index.test.ts`` (chat@4.41.1), plus
Python-specific guards. Upstream's ``withClock(tick)`` (a mocked
``Date.now``) becomes a fake monotonic clock patched into the adapter and
advanced inside the source generator; nothing sleeps.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from chat_sdk.adapters.slack import adapter as slack_adapter_module
from chat_sdk.adapters.slack.adapter import SlackAdapter
from chat_sdk.adapters.slack.types import SlackAdapterConfig
from chat_sdk.types import StreamOptions

THREAD = "slack:D123:1234567890.000000"
TOKEN = "xoxb-test-token"
# Max age 100ms; the fixed 30s grace window applies on top of it.
MAX_AGE = 100
PAST_GRACE = MAX_AGE + 30_001


class _ExpiredStreamError(Exception):
    """Shape of slack_sdk's ``SlackApiError`` for an expired stream."""

    def __init__(self) -> None:
        super().__init__("message_not_in_streaming_state")
        self.response = {"ok": False, "error": "message_not_in_streaming_state"}


class _Setup:
    def __init__(self, adapter: SlackAdapter, segments: list[MagicMock], chat_stream: AsyncMock, logger: MagicMock):
        self.adapter = adapter
        self.segments = segments
        self.chat_stream = chat_stream
        self.logger = logger


def _setup(segment_count: int = 3, **config: Any) -> _Setup:
    logger = MagicMock()
    config.setdefault("stream_segment_max_age_ms", MAX_AGE)
    adapter = SlackAdapter(
        SlackAdapterConfig(bot_token=TOKEN, signing_secret="test-signing-secret", logger=logger, **config)
    )
    segments: list[MagicMock] = []
    for index in range(segment_count):
        segment = MagicMock()
        # Real ``chat.startStream`` / ``appendStream`` responses carry the
        # message ts; slack_sdk keeps it only privately, so the adapter
        # records it from the first response.
        segment.append = AsyncMock(return_value={"ok": True, "ts": f"1234567890.{index}"})
        segment.stop = AsyncMock(return_value={"ok": True, "ts": f"1234567890.{index}"})
        segments.append(segment)
    chat_stream = AsyncMock(side_effect=lambda **_: segments[chat_stream.call_count - 1])
    client = MagicMock()
    client.chat_stream = chat_stream
    adapter._get_client = lambda token=None: client  # type: ignore[assignment]
    return _Setup(adapter, segments, chat_stream, logger)


@pytest.fixture
def tick(monkeypatch: pytest.MonkeyPatch) -> Callable[[float], None]:
    """Controllable adapter clock (upstream ``withClock``); ``tick(ms)`` sets it."""
    now = [0.0]
    monkeypatch.setattr(slack_adapter_module, "_monotonic_ms", lambda: now[0])

    def set_now(ms: float) -> None:
        now[0] = ms

    return set_now


class TestNativeStreamRotation:
    @pytest.mark.asyncio
    async def test_rotates_at_a_paragraph_break_past_the_max_age_and_carries_an_open_fence_over(self, tick):
        s = _setup()

        async def stream() -> AsyncIterator[str]:
            yield "```ts\nconst first = true;\n"
            tick(MAX_AGE + 1)
            yield "const second = true;\n\nconst third = true;\n"
            yield "```\n"

        result = await s.adapter.stream(THREAD, stream())

        assert s.chat_stream.await_count == 2
        # The text before the paragraph break and the fence closer travel
        # with the stop call; no extra newline since the text ends with one.
        s.segments[0].stop.assert_awaited_once_with(token=TOKEN, markdown_text="const second = true;\n\n```")
        # The new segment reopens the fence and flushes immediately.
        assert s.segments[1].append.await_args_list[0].kwargs == {
            "markdown_text": "```ts\nconst third = true;\n",
            "token": TOKEN,
            "chunks": [],
        }
        assert s.segments[1].append.await_args_list[1].kwargs == {"markdown_text": "```\n", "token": TOKEN}
        s.segments[1].stop.assert_awaited_once_with(token=TOKEN)
        assert result is not None
        assert result.id == "1234567890.1"

    @pytest.mark.asyncio
    async def test_waits_for_a_paragraph_break_within_the_grace_window_then_cuts_at_a_line_break(self, tick):
        s = _setup()

        async def stream() -> AsyncIterator[str]:
            yield "First line.\n"
            tick(MAX_AGE + 1)
            yield "Second line.\n"
            tick(PAST_GRACE)
            yield "Third line.\n"
            yield "Fourth line.\n"

        await s.adapter.stream(THREAD, stream())

        assert s.chat_stream.await_count == 2
        assert s.segments[0].append.await_count == 2
        s.segments[0].stop.assert_awaited_once_with(token=TOKEN, markdown_text="Third line.\n")
        assert s.segments[1].append.await_args_list[0].kwargs == {
            "markdown_text": "Fourth line.\n",
            "token": TOKEN,
            "chunks": [],
        }

    @pytest.mark.asyncio
    async def test_does_not_rotate_when_the_final_flush_has_nothing_new_to_send(self, tick):
        s = _setup()

        async def stream() -> AsyncIterator[str]:
            yield "hello\n"
            tick(PAST_GRACE)

        result = await s.adapter.stream(THREAD, stream())

        assert s.chat_stream.await_count == 1
        s.segments[0].stop.assert_awaited_once_with(token=TOKEN)
        assert result is not None
        assert result.id == "1234567890.0"

    @pytest.mark.asyncio
    async def test_starts_the_segment_clock_at_the_first_call_slack_accepts_not_at_construction(self, tick):
        s = _setup()
        # The first delta is only buffered locally: no Slack stream yet.
        s.segments[0].append.side_effect = [None, {"ok": True, "ts": "1234567890.0"}, {"ok": True}, {"ok": True}]
        counts_mid_stream: list[int] = []

        async def stream() -> AsyncIterator[str]:
            yield "a\n"
            tick(1000)
            yield "b\n"
            tick(1000 + MAX_AGE - 1)
            yield "c\n\nd\n"
            counts_mid_stream.append(s.chat_stream.await_count)
            tick(1000 + MAX_AGE + 1)
            yield "e\n\nf\n"

        await s.adapter.stream(THREAD, stream())

        assert counts_mid_stream == [1]
        assert s.chat_stream.await_count == 2

    @pytest.mark.asyncio
    async def test_continues_in_a_new_message_when_slack_expired_the_segment_before_rotation(self, tick):
        s = _setup()
        s.segments[0].append.side_effect = [{"ok": True, "ts": "1234567890.0"}, None]
        s.segments[0].stop.side_effect = _ExpiredStreamError()

        async def stream() -> AsyncIterator[str]:
            yield "first\n"
            # Buffered only: never confirmed by Slack.
            yield "second\n"
            tick(MAX_AGE + 1)
            yield "third\n\nfourth\n"

        result = await s.adapter.stream(THREAD, stream())

        # Everything after the last confirmed flush is resent.
        assert s.segments[1].append.await_args_list[0].kwargs == {
            "markdown_text": "second\nthird\n\nfourth\n",
            "token": TOKEN,
            "chunks": [],
        }
        assert result is not None
        assert result.id == "1234567890.1"
        warnings = [call.args[0] for call in s.logger.warn.call_args_list]
        assert any("expired before rotation" in message for message in warnings)

    @pytest.mark.asyncio
    async def test_delivers_unconfirmed_text_in_a_new_message_when_the_last_segment_expired_before_stop(self):
        s = _setup()
        s.segments[0].append.side_effect = [{"ok": True, "ts": "1234567890.0"}, None]
        s.segments[0].stop.side_effect = _ExpiredStreamError()

        async def stream() -> AsyncIterator[str]:
            yield "first\n"
            yield "second\n"

        result = await s.adapter.stream(THREAD, stream())

        s.segments[1].append.assert_not_awaited()
        s.segments[1].stop.assert_awaited_once_with(token=TOKEN, markdown_text="second\n")
        assert result is not None
        assert result.id == "1234567890.1"

    @pytest.mark.asyncio
    async def test_returns_the_finalized_message_when_the_last_segment_expired_with_everything_delivered(self):
        s = _setup(feedback_buttons=True)
        s.segments[0].stop.side_effect = _ExpiredStreamError()

        async def stream() -> AsyncIterator[str]:
            yield "first\n"

        result = await s.adapter.stream(THREAD, stream())

        # No empty message is posted just to carry the feedback buttons.
        assert s.chat_stream.await_count == 1
        assert result is not None
        assert result.id == "1234567890.0"
        assert result.raw == {"ts": "1234567890.0"}
        skipped = [
            call.args[1]
            for call in s.logger.warn.call_args_list
            if "stream-end blocks skipped" in call.args[0] and "expired" in call.args[0]
        ]
        assert len(skipped) == 1
        assert skipped[0]["skippedBlocks"] == 1

    @pytest.mark.asyncio
    async def test_still_propagates_non_expiry_failures_during_rotation(self, tick):
        s = _setup()
        s.segments[0].stop.side_effect = RuntimeError("rotate boom")

        async def stream() -> AsyncIterator[str]:
            yield "a\n"
            tick(MAX_AGE + 1)
            yield "b\n\nc\n"

        with pytest.raises(RuntimeError, match="rotate boom"):
            await s.adapter.stream(THREAD, stream())

    @pytest.mark.asyncio
    async def test_replays_the_plan_and_open_task_cards_into_the_new_segment(self, tick):
        s = _setup()
        plan = {"type": "plan_update", "title": "Plan"}
        one_in_progress = {"type": "task_update", "id": "t1", "title": "One", "status": "in_progress"}
        one_complete = {**one_in_progress, "status": "complete"}
        two_complete = {"type": "task_update", "id": "t2", "title": "Two", "status": "complete"}

        async def stream() -> AsyncIterator[dict[str, Any]]:
            yield plan
            yield one_in_progress
            yield two_complete
            tick(MAX_AGE + 1)
            # A structured chunk is a block boundary: rotate right away.
            yield one_complete

        await s.adapter.stream(THREAD, stream())

        s.segments[0].stop.assert_awaited_once_with(token=TOKEN)
        assert s.segments[1].append.await_args_list[0].kwargs == {
            "chunks": [plan, one_in_progress],
            "token": TOKEN,
        }
        assert s.segments[1].append.await_args_list[1].kwargs == {"chunks": [one_complete], "token": TOKEN}

    @pytest.mark.asyncio
    async def test_repeats_the_table_header_when_a_table_continues_in_the_new_segment(self, tick):
        s = _setup()

        async def stream() -> AsyncIterator[str]:
            yield "| a | b |\n|---|---|\n| 1 | 2 |\n"
            tick(PAST_GRACE)
            yield "| 3 | 4 |\n"
            yield "| 5 | 6 |\n"

        await s.adapter.stream(THREAD, stream())

        s.segments[0].stop.assert_awaited_once_with(token=TOKEN, markdown_text="| 3 | 4 |\n")
        assert s.segments[1].append.await_args_list[0].kwargs == {
            "markdown_text": "| a | b |\n|---|---|\n| 5 | 6 |\n",
            "token": TOKEN,
            "chunks": [],
        }

    @pytest.mark.asyncio
    async def test_does_not_repeat_the_table_header_when_the_new_segment_starts_with_prose(self, tick):
        s = _setup()

        async def stream() -> AsyncIterator[str]:
            yield "| a | b |\n|---|---|\n| 1 | 2 |\n"
            tick(PAST_GRACE)
            yield "| 3 | 4 |\n"
            yield "\nSummary.\n"

        await s.adapter.stream(THREAD, stream())

        assert s.segments[1].append.await_args_list[0].kwargs == {
            "markdown_text": "\nSummary.\n",
            "token": TOKEN,
            "chunks": [],
        }

    @pytest.mark.asyncio
    async def test_closes_a_tilde_fence_with_tildes_even_when_backtick_fences_appear_inside_it(self, tick):
        s = _setup()

        async def stream() -> AsyncIterator[str]:
            yield "~~~\n```js\nconst x = 1;\n```\n"
            tick(MAX_AGE + 1)
            yield "still literal\n\nmore\n"

        await s.adapter.stream(THREAD, stream())

        s.segments[0].stop.assert_awaited_once_with(token=TOKEN, markdown_text="still literal\n\n~~~")
        assert s.segments[1].append.await_args_list[0].kwargs == {
            "markdown_text": "~~~\nmore\n",
            "token": TOKEN,
            "chunks": [],
        }

    @pytest.mark.asyncio
    async def test_finishes_a_fenced_block_in_the_old_segment_when_the_pending_text_closes_it(self, tick):
        s = _setup()

        async def stream() -> AsyncIterator[str]:
            yield "```ts\nconst a = 1;\n"
            tick(MAX_AGE + 1)
            yield "```\n\nAfter.\n"

        await s.adapter.stream(THREAD, stream())

        s.segments[0].stop.assert_awaited_once_with(token=TOKEN, markdown_text="```\n\n")
        # No empty reopened block at the top of the new segment.
        assert s.segments[1].append.await_args_list[0].kwargs == {
            "markdown_text": "After.\n",
            "token": TOKEN,
            "chunks": [],
        }

    @pytest.mark.asyncio
    async def test_treats_a_pending_partial_closing_fence_as_the_blocks_end(self, tick):
        s = _setup()

        async def stream() -> AsyncIterator[str]:
            yield "```ts\nconst a = 1;\n"
            tick(PAST_GRACE)
            yield "```"
            yield "\nAfter.\n"

        await s.adapter.stream(THREAD, stream())

        s.segments[0].stop.assert_awaited_once_with(token=TOKEN, markdown_text="```")
        assert s.segments[1].append.await_args_list[0].kwargs == {
            "markdown_text": "\nAfter.\n",
            "token": TOKEN,
            "chunks": [],
        }

    @pytest.mark.asyncio
    async def test_does_not_rotate_a_segment_before_slack_has_started_it(self, tick):
        s = _setup()
        s.segments[0].append.return_value = None

        async def stream() -> AsyncIterator[str]:
            yield "a\n"
            tick(PAST_GRACE)
            yield "b\n\nc\n"

        await s.adapter.stream(THREAD, stream())

        assert s.chat_stream.await_count == 1
        assert s.segments[0].stop.await_count == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", [0, -5, math.nan])
    async def test_falls_back_to_the_default_max_age_for_stream_segment_max_age_ms(self, tick, value):
        s = _setup(stream_segment_max_age_ms=value)
        counts_mid_stream: list[int] = []

        async def stream() -> AsyncIterator[str]:
            yield "a\n"
            tick(239_999)
            yield "b\n\nc\n"
            counts_mid_stream.append(s.chat_stream.await_count)
            tick(240_000)
            yield "d\n\ne\n"

        await s.adapter.stream(THREAD, stream())

        assert counts_mid_stream == [1]
        assert s.chat_stream.await_count == 2

    @pytest.mark.asyncio
    async def test_never_rotates_when_stream_segment_max_age_ms_is_infinity(self, tick):
        s = _setup(stream_segment_max_age_ms=math.inf)

        async def stream() -> AsyncIterator[str]:
            yield "a\n"
            tick(10_000_000)
            yield "b\n\nc\n"

        await s.adapter.stream(THREAD, stream())

        assert s.chat_stream.await_count == 1


class TestNativeStreamRotationPythonSpecific:
    @pytest.mark.asyncio
    async def test_every_segment_gets_the_grid_team_id(self, tick):
        # #95: Enterprise Grid ``chat.startStream`` fails with
        # ``team_not_found`` without ``team_id``, so the rotated segment's
        # ``chat_stream`` call needs it as much as the first one.
        s = _setup()

        async def stream() -> AsyncIterator[str]:
            yield "a\n"
            tick(MAX_AGE + 1)
            yield "b\n\nc\n"

        await s.adapter.stream(
            "slack:C123:1234567890.000000",
            stream(),
            StreamOptions(recipient_user_id="U1", recipient_team_id="T_GRID"),
        )

        assert s.chat_stream.await_count == 2
        for call in s.chat_stream.await_args_list:
            assert call.kwargs == {
                "channel": "C123",
                "thread_ts": "1234567890.000000",
                "recipient_user_id": "U1",
                "recipient_team_id": "T_GRID",
                "team_id": "T_GRID",
            }

    @pytest.mark.asyncio
    async def test_a_resolved_mention_straddling_the_rotation_cut_is_not_duplicated(self, tick):
        # Rotation cuts the SL1 resolved buffer, never the raw renderer text:
        # a mention split across source chunks right where the segment
        # rotates is sent once, resolved, on exactly one side of the cut.
        from chat_sdk.state.memory import MemoryStateAdapter

        state = MemoryStateAdapter()
        await state.connect()
        await state.append_to_list("slack:user-by-name:alice", "U_ALICE_1")
        s = _setup()
        chat = MagicMock()
        chat.get_state = MagicMock(return_value=state)
        s.adapter._chat = chat  # type: ignore[assignment]

        async def stream() -> AsyncIterator[str]:
            yield "first\n"
            tick(MAX_AGE + 1)
            yield "ping @ali"
            yield "ce\n\nafter @alice\n"

        await s.adapter.stream(THREAD, stream())

        assert s.chat_stream.await_count == 2
        sent: list[str] = []
        for segment in s.segments[:2]:
            sent += [c.kwargs["markdown_text"] for c in segment.append.await_args_list if "markdown_text" in c.kwargs]
            sent += [c.kwargs["markdown_text"] for c in segment.stop.await_args_list if "markdown_text" in c.kwargs]
        assert s.segments[0].stop.await_args.kwargs["markdown_text"] == "ping <@U_ALICE_1>\n\n"
        assert "".join(sent) == "first\nping <@U_ALICE_1>\n\nafter <@U_ALICE_1>\n"

    @pytest.mark.asyncio
    async def test_cancellation_from_the_source_stream_mid_rotation_propagates(self, tick):
        s = _setup()

        async def stream() -> AsyncIterator[str]:
            yield "a\n"
            tick(MAX_AGE + 1)
            yield "b\n\nc\n"
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await s.adapter.stream(THREAD, stream())

        # The old segment was rotated; the new one is never stopped.
        assert s.chat_stream.await_count == 2
        s.segments[1].stop.assert_not_awaited()


class TestFenceTrackerPythonSpecific:
    def test_a_shorter_run_or_an_info_string_does_not_close_the_fence(self):
        from chat_sdk.adapters.slack.adapter import _open_fence_in

        # CommonMark: the closer must be at least as long as the opener and
        # carry no info string, so both inner lines are literal content.
        fence = _open_fence_in("````md\n```\n````js\ninner\n")
        assert fence is not None
        assert (fence.marker, fence.opening) == ("````", "````md")
        assert _open_fence_in("````md\n```\n`````\n") is None

    def test_a_backtick_info_string_with_a_backtick_is_not_a_fence(self):
        from chat_sdk.adapters.slack.adapter import _open_fence_in

        # Inline code such as ```a`b``` at line start never opens a block.
        assert _open_fence_in("```a`b```\ntext\n") is None
        # A CRLF line is not a fence line, as with JS ``.`` / ``$``.
        assert _open_fence_in("```ts\r\ncode\n") is None
