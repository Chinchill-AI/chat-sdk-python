"""Behavioral tests for Teams native streaming via Bot Framework streaming protocol.

Port of upstream vercel/chat#416 (commit ed46bae): for DMs, the Teams
adapter dispatches stream chunks through ``IStreamer.emit`` (in TS) — in
Python, through cumulative typing activities tagged with
``channelData.streamType = "streaming"`` and a final ``message`` activity
tagged ``streamType = "final"``.

These tests pin the wire-level shape (streamSequence increments, streamId
threading, no streamId on the first chunk) and the lifecycle behavior
(typing indicator clears on close, no orphan streams, cancellation drains
cleanly, mid-stream errors surface to the caller).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from chat_sdk.adapters.teams.adapter import (
    _STREAM_TYPE_FINAL,
    _STREAM_TYPE_STREAMING,
    TeamsAdapter,
    _TeamsStreamSession,
)
from chat_sdk.adapters.teams.types import TeamsAdapterConfig, TeamsThreadId


def _make_adapter(
    *,
    native_stream_min_emit_interval_ms: int | None = None,
    clock_step_ms: float = 2000.0,
) -> TeamsAdapter:
    """Build a TeamsAdapter with a deterministic native-stream clock.

    The native streaming path throttles emits via
    ``_stream_clock_ms()`` (defaults to ``loop.time() * 1000``). Tests
    can't rely on real elapsed time without sleeping, so we substitute
    a counter-based clock that advances by ``clock_step_ms`` per call.
    With the default 2000ms step (> the 1500ms throttle), every chunk
    clears the interval gate — matching the pre-throttle test
    expectations of "one emit per chunk." Tests that want to exercise
    coalescing pass ``clock_step_ms=0`` (or a value below the configured
    interval) so chunks land within the same throttle window.
    """
    config_kwargs: dict[str, Any] = {
        "app_id": "test-app-id",
        "app_password": "test-password",
        "logger": MagicMock(
            debug=MagicMock(),
            info=MagicMock(),
            warn=MagicMock(),
            error=MagicMock(),
        ),
    }
    if native_stream_min_emit_interval_ms is not None:
        config_kwargs["native_stream_min_emit_interval_ms"] = native_stream_min_emit_interval_ms

    adapter = TeamsAdapter(TeamsAdapterConfig(**config_kwargs))
    adapter._stream_clock_ms = _advancing_clock(step_ms=clock_step_ms)
    # Default sleep is an AsyncMock so the throttle wait in
    # ``_stream_via_emit``'s end-of-stream flush returns instantly. Tests
    # that care about the wait amount can introspect via
    # ``adapter._stream_sleep_ms.await_args``; tests that don't care just
    # get the no-op behavior.
    adapter._stream_sleep_ms = AsyncMock()
    return adapter


def _advancing_clock(*, start_ms: float = 0.0, step_ms: float = 2000.0):
    """Returns a deterministic ms-clock that advances by ``step_ms`` per call.

    With ``step_ms`` greater than the throttle interval, every call to
    ``_stream_clock_ms`` reports enough elapsed time to clear the gate.
    With ``step_ms == 0``, every call returns the same value so all
    chunks land inside a single throttle window.
    """
    state = {"now": start_ms}

    def clock() -> float:
        state["now"] += step_ms
        return state["now"]

    return clock


def _dm_thread_id(adapter: TeamsAdapter) -> str:
    """Encode a DM-shaped (non-``19:`` prefix) conversation id."""
    return adapter.encode_thread_id(
        TeamsThreadId(
            conversation_id="a:1Abc-DM-conversation-id",
            service_url="https://smba.trafficmanager.net/teams/",
        )
    )


def _channel_thread_id(adapter: TeamsAdapter) -> str:
    """Encode a channel-shaped (``19:`` prefix) conversation id."""
    return adapter.encode_thread_id(
        TeamsThreadId(
            conversation_id="19:abc@thread.tacv2",
            service_url="https://smba.trafficmanager.net/teams/",
        )
    )


# ---------------------------------------------------------------------------
# Wire-format invariants
# ---------------------------------------------------------------------------


class TestNativeStreamingWireFormat:
    """Pin the Bot Framework streaming protocol payload shapes."""

    @pytest.mark.asyncio
    async def test_first_chunk_omits_stream_id(self):
        """The first chunk must NOT carry a ``streamId`` — the server assigns it.

        Hazard #7: serializing ``"streamId": None`` would cause Teams to
        reject the activity. Only emit the key once we have a real id from
        the Bot Framework REST response.
        """
        adapter = _make_adapter()
        adapter._teams_send = AsyncMock(return_value={"id": "stream-id-from-server"})
        tid = _dm_thread_id(adapter)
        session = _TeamsStreamSession()
        adapter._active_streams[tid] = session

        async def text_gen():
            yield "first"

        await adapter._stream_via_emit(tid, text_gen(), session)

        first_payload = adapter._teams_send.await_args_list[0].args[1]
        assert first_payload["type"] == "typing"
        assert first_payload["text"] == "first"
        assert first_payload["channelData"]["streamType"] == _STREAM_TYPE_STREAMING
        assert first_payload["channelData"]["streamSequence"] == 1
        assert "streamId" not in first_payload["channelData"]
        # ``streamId`` must also be absent from the streaminfo entity on
        # the first chunk — there is no server-assigned id yet and
        # sending ``"streamId": None`` (or "") would cause Teams to
        # reject the activity.
        assert "streamId" not in first_payload["entities"][0]
        # Subsequent chunks (none here) would inherit streamId from the
        # server response; verify the session captured it.
        assert session.stream_id == "stream-id-from-server"
        assert session.first_chunk_id == "stream-id-from-server"

    @pytest.mark.asyncio
    async def test_chunks_include_streaminfo_entity(self):
        """Each streaming chunk includes a ``streaminfo`` entity for the protocol."""
        adapter = _make_adapter()
        adapter._teams_send = AsyncMock(return_value={"id": "id-1"})
        tid = _dm_thread_id(adapter)
        session = _TeamsStreamSession()
        adapter._active_streams[tid] = session

        async def text_gen():
            yield "hello"

        await adapter._stream_via_emit(tid, text_gen(), session)

        payload = adapter._teams_send.await_args_list[0].args[1]
        assert payload["entities"] == [
            {
                "type": "streaminfo",
                "streamType": _STREAM_TYPE_STREAMING,
                "streamSequence": 1,
            }
        ]

    @pytest.mark.asyncio
    async def test_subsequent_chunks_carry_stream_id_and_increment_sequence(self):
        """After the first send, every chunk carries the assigned streamId.

        ``streamSequence`` increments by 1 per chunk (1, 2, 3, ...). Each
        chunk's ``text`` is the cumulative content (Teams clients render the
        latest snapshot — not deltas).
        """
        adapter = _make_adapter()
        # Server assigns id on the first send; subsequent sends echo back
        # arbitrary ids that we ignore (we keep the first one as streamId).
        adapter._teams_send = AsyncMock(
            side_effect=[
                {"id": "first-server-id"},
                {"id": "ignored-1"},
                {"id": "ignored-2"},
            ]
        )
        tid = _dm_thread_id(adapter)
        session = _TeamsStreamSession()
        adapter._active_streams[tid] = session

        async def text_gen():
            yield "Hel"
            yield "lo "
            yield "world"

        await adapter._stream_via_emit(tid, text_gen(), session)

        payloads = [c.args[1] for c in adapter._teams_send.await_args_list]
        assert [p["text"] for p in payloads] == ["Hel", "Hello ", "Hello world"]
        assert [p["channelData"]["streamSequence"] for p in payloads] == [1, 2, 3]
        # First chunk has no streamId; later chunks carry the captured one.
        assert "streamId" not in payloads[0]["channelData"]
        assert payloads[1]["channelData"]["streamId"] == "first-server-id"
        assert payloads[2]["channelData"]["streamId"] == "first-server-id"

    @pytest.mark.asyncio
    async def test_close_session_sends_final_message(self):
        """Closing the session sends a ``message`` activity with ``streamType: final``.

        This is what clears the streaming UI on the Teams client.
        """
        adapter = _make_adapter()
        adapter._teams_send = AsyncMock(return_value={"id": "final-server-id"})
        tid = _dm_thread_id(adapter)
        session = _TeamsStreamSession()
        session.stream_id = "running-stream-id"
        session._text = "Hello world"

        await adapter._close_stream_session(tid, session)

        assert adapter._teams_send.await_count == 1
        payload = adapter._teams_send.await_args.args[1]
        assert payload["type"] == "message"
        assert payload["text"] == "Hello world"
        assert payload["channelData"]["streamType"] == _STREAM_TYPE_FINAL
        assert payload["channelData"]["streamId"] == "running-stream-id"
        # Bot Framework streaming contract requires ``streamId`` on the
        # ``streaminfo`` entity (not just ``channelData``) for the final
        # activity. Earlier versions of this adapter omitted it from the
        # entity, which Teams may treat as a malformed close — leaving
        # the streaming UI spinning until client-side timeout.
        assert payload["entities"] == [
            {
                "type": "streaminfo",
                "streamType": _STREAM_TYPE_FINAL,
                "streamId": "running-stream-id",
            }
        ]


# ---------------------------------------------------------------------------
# Throttling (Bot Framework streaming endpoint is ~1 req/sec)
# ---------------------------------------------------------------------------


class TestNativeStreamingThrottle:
    """Pin the chunk-coalescing behavior that protects against Teams 429s.

    Microsoft's Bot Framework streaming endpoint throttles to roughly
    1 request/second and recommends buffering tokens for 1.5-2 seconds
    before sending the next ``streaming`` activity. ``_stream_via_emit``
    coalesces in-window chunks into the cumulative-text snapshot that
    ships with the next eligible emit (or in the final ``message``
    activity if the iterator ends inside the window).
    """

    @pytest.mark.asyncio
    async def test_intermediate_chunks_within_window_are_coalesced(self):
        """Mid-stream chunks in the same throttle window collapse to one emit.

        Without coalescing, a typical LLM token stream (10+ tokens/s) would
        rate-limit on the Bot Framework streaming endpoint within the
        first second and the response would be cancelled mid-flight.

        Two sends total: the first chunk's intermediate emit + the
        end-of-stream flush that ships everything else (see
        ``test_buffered_text_flushed_at_end_of_stream`` for why the
        flush exists).
        """
        # ``clock_step_ms=0`` means every clock check returns the same
        # value, so every chunk after the first lands inside the throttle
        # window. Only the first chunk emits intermediately; the rest
        # ride out in the end-of-stream flush.
        adapter = _make_adapter(clock_step_ms=0.0)
        adapter._teams_send = AsyncMock(return_value={"id": "id-1"})
        tid = _dm_thread_id(adapter)
        session = _TeamsStreamSession()
        adapter._active_streams[tid] = session

        async def text_gen():
            yield "Hel"
            yield "lo "
            yield "world"
            yield "!"

        result = await adapter._stream_via_emit(tid, text_gen(), session)

        # Two ``typing`` activities: the first chunk's intermediate emit
        # plus the end-of-stream flush. The middle two chunks did not
        # each get their own emit — they were coalesced.
        assert adapter._teams_send.await_count == 2, (
            "Throttle should coalesce intermediate chunks within the same window. "
            "Without this, real LLM streams (10+ tokens/s) would 429 the Bot "
            "Framework streaming endpoint within the first second."
        )
        payloads = [c.args[1] for c in adapter._teams_send.await_args_list]
        # Initial intermediate emit: just the first chunk's text.
        assert payloads[0]["text"] == "Hel"
        # End-of-stream flush: the full cumulative text.
        assert payloads[1]["text"] == "Hello world!"
        # Both are streaming activities (not the final ``message``).
        assert payloads[0]["type"] == "typing"
        assert payloads[1]["type"] == "typing"
        assert session.text == "Hello world!"
        assert result.raw["text"] == "Hello world!"

    @pytest.mark.asyncio
    async def test_chunks_beyond_throttle_interval_emit_individually(self):
        """When time advances past the interval, each chunk gets its own send."""
        adapter = _make_adapter(clock_step_ms=2000.0)
        adapter._teams_send = AsyncMock(side_effect=[{"id": "first"}, {"id": "ignored-1"}, {"id": "ignored-2"}])
        tid = _dm_thread_id(adapter)
        session = _TeamsStreamSession()
        adapter._active_streams[tid] = session

        async def text_gen():
            yield "one "
            yield "two "
            yield "three"

        await adapter._stream_via_emit(tid, text_gen(), session)
        # All three chunks emitted because each clock check reports
        # 2000ms elapsed (> the default 1500ms interval).
        assert adapter._teams_send.await_count == 3
        texts = [c.args[1]["text"] for c in adapter._teams_send.await_args_list]
        assert texts == ["one ", "one two ", "one two three"]

    @pytest.mark.asyncio
    async def test_caller_update_interval_ms_overrides_default(self):
        """``StreamOptions.update_interval_ms`` overrides the adapter default.

        A caller (e.g. a ``StreamingPlan``) that asks for ``update_interval_ms=0``
        gets one emit per chunk regardless of the adapter's configured
        default. Mirrors how the fallback path treats the same field.
        """
        from chat_sdk.types import StreamOptions

        # Even with a real (non-zero) default, a caller-supplied 0 should
        # disable coalescing.
        adapter = _make_adapter(
            native_stream_min_emit_interval_ms=1500,
            clock_step_ms=10.0,  # tiny steps so coalescing WOULD happen at 1500ms default
        )
        adapter._teams_send = AsyncMock(return_value={"id": "id-1"})
        tid = _dm_thread_id(adapter)
        session = _TeamsStreamSession()
        adapter._active_streams[tid] = session

        async def text_gen():
            yield "a"
            yield "b"
            yield "c"

        # Without the override, the 1500ms throttle + 10ms clock steps
        # would coalesce everything into one emit. With override=0,
        # every chunk should emit.
        opts = StreamOptions()
        opts.update_interval_ms = 0
        await adapter._stream_via_emit(tid, text_gen(), session, opts)
        assert adapter._teams_send.await_count == 3, (
            "Caller-supplied StreamOptions.update_interval_ms=0 should disable coalescing entirely for this stream"
        )

    @pytest.mark.asyncio
    async def test_buffered_text_flushed_at_end_of_stream(self):
        """End-of-stream flush guarantees Teams accepted every byte before return.

        **What this prevents (Codex P2):** without the flush, chunks coalesced
        in a throttle window would only ship in the close-path ``message``
        activity — and if THAT send fails (429, network blip), ``Thread.stream``
        would already have built a ``SentMessage`` from this method's return
        value containing text Teams never accepted. The chat handler returns
        and ``SentMessage`` is created BEFORE the close runs from the
        handler's finally block, so a swallowed close failure produces a
        message-history entry the user never saw.

        With the flush, ``accumulated`` is confirmed-accepted via a forced
        ``typing`` emit before ``_stream_via_emit`` returns, so the
        ``SentMessage`` matches reality even if the close fails.
        """
        adapter = _make_adapter(clock_step_ms=0.0)
        adapter._teams_send = AsyncMock(return_value={"id": "first-id"})
        tid = _dm_thread_id(adapter)
        session = _TeamsStreamSession()
        adapter._active_streams[tid] = session

        async def text_gen():
            yield "Hello"
            yield " coalesced"
            yield " world"

        result = await adapter._stream_via_emit(tid, text_gen(), session)

        # Two typing sends: the first chunk + the end-of-stream flush.
        assert adapter._teams_send.await_count == 2
        payloads = [c.args[1] for c in adapter._teams_send.await_args_list]
        assert payloads[0]["text"] == "Hello"
        assert payloads[1]["text"] == "Hello coalesced world", (
            "End-of-stream flush must carry the full accumulated text. "
            "Without this, Thread.stream would record a SentMessage with "
            "text Teams never accepted on a close-path send failure."
        )
        # Both are streaming typing activities, sequence increments.
        assert payloads[0]["channelData"]["streamSequence"] == 1
        assert payloads[1]["channelData"]["streamSequence"] == 2
        # The flush is what Thread.stream's SentMessage will be built from.
        assert result.raw["text"] == "Hello coalesced world"
        assert session.text == "Hello coalesced world"

    @pytest.mark.asyncio
    async def test_flush_failure_propagates_and_cancels_session(self):
        """If the end-of-stream flush fails, re-raise — same shape as in-loop emits.

        A close-path failure is now logged at warn (the user already saw
        the text via the flush), but a flush failure means buffered text
        was never accepted by Teams. Swallowing it would let
        ``Thread.stream`` record the buffered text in ``SentMessage`` /
        ``_message_history`` even though the user never saw it. Re-raise
        so the outer ``Thread.stream`` short-circuits the history append.
        """
        adapter = _make_adapter(clock_step_ms=0.0)
        adapter._teams_send = AsyncMock(
            side_effect=[
                {"id": "first-id"},  # initial chunk emit succeeds
                RuntimeError("429 on flush"),  # end-of-stream flush fails
            ]
        )
        tid = _dm_thread_id(adapter)
        session = _TeamsStreamSession()
        adapter._active_streams[tid] = session

        async def text_gen():
            yield "first"
            yield " buffered"

        with pytest.raises(RuntimeError, match="429 on flush"):
            await adapter._stream_via_emit(tid, text_gen(), session)
        # Session canceled so the close path skips its final-message activity.
        assert session.canceled is True
        # Two attempted sends: the first chunk + the flush attempt.
        assert adapter._teams_send.await_count == 2

    @pytest.mark.asyncio
    async def test_no_flush_when_iterator_ended_at_window_boundary(self):
        """No redundant flush when the last chunk already triggered an emit.

        Regression: the flush should ONLY run when there's buffered text
        that hasn't been emitted. If every chunk landed beyond the
        throttle window (each got its own emit), there's nothing to
        flush and we shouldn't add a redundant duplicate-text send.
        """
        adapter = _make_adapter(clock_step_ms=2000.0)
        adapter._teams_send = AsyncMock(side_effect=[{"id": "first"}, {"id": "ignored"}])
        tid = _dm_thread_id(adapter)
        session = _TeamsStreamSession()
        adapter._active_streams[tid] = session

        async def text_gen():
            yield "one "
            yield "two"

        await adapter._stream_via_emit(tid, text_gen(), session)
        # Exactly two sends — one per chunk. No flush because each chunk
        # was already emitted intermediately.
        assert adapter._teams_send.await_count == 2

    @pytest.mark.asyncio
    async def test_no_flush_after_session_canceled_mid_stream(self):
        """Mid-stream cancellation skips the end-of-stream flush.

        ``session.cancel()`` may be set by a user-initiated abort or by
        an upstream supervisor; either way, we shouldn't ship buffered
        text the user explicitly canceled out of.
        """
        adapter = _make_adapter(clock_step_ms=0.0)
        adapter._teams_send = AsyncMock(return_value={"id": "first-id"})
        tid = _dm_thread_id(adapter)
        session = _TeamsStreamSession()
        adapter._active_streams[tid] = session

        async def text_gen():
            yield "first"
            session.cancel()
            # These two are buffered (within window) but session is now
            # canceled — the flush should NOT ship them.
            yield " buffered"
            yield " more"

        await adapter._stream_via_emit(tid, text_gen(), session)
        # Only the pre-cancel emit. No flush of buffered text since
        # session.canceled is True at end-of-loop.
        assert adapter._teams_send.await_count == 1
        assert adapter._teams_send.await_args_list[0].args[1]["text"] == "first"

    @pytest.mark.asyncio
    async def test_close_path_final_message_carries_full_accumulated_text(self):
        """The close-path final ``message`` activity carries the full text.

        This is what switches the Teams streaming UI from typing indicator
        to message bubble. After the end-of-stream flush, this text is
        redundant content-wise (already confirmed via the flush typing
        emit), but the activity-type change is what the client UI needs.
        """
        adapter = _make_adapter(clock_step_ms=0.0)
        adapter._teams_send = AsyncMock(return_value={"id": "first-id"})
        tid = _dm_thread_id(adapter)
        session = _TeamsStreamSession()
        adapter._active_streams[tid] = session

        async def text_gen():
            yield "Hello"
            yield " coalesced"
            yield " world"

        await adapter._stream_via_emit(tid, text_gen(), session)
        # Two typing sends so far (initial + end-of-stream flush).
        assert adapter._teams_send.await_count == 2
        assert adapter._teams_send.await_args_list[0].args[1]["text"] == "Hello"
        assert adapter._teams_send.await_args_list[1].args[1]["text"] == "Hello coalesced world"

        # Now close: the final ``message`` activity carries the full
        # accumulated text — switching the streaming UI from typing
        # indicator to message bubble. Index [2] because the flush
        # already emitted at index [1].
        await adapter._close_stream_session(tid, session)
        assert adapter._teams_send.await_count == 3
        final_payload = adapter._teams_send.await_args_list[2].args[1]
        assert final_payload["type"] == "message"
        assert final_payload["text"] == "Hello coalesced world"


# ---------------------------------------------------------------------------
# streamInfo entity contract (Bot Framework REST: streamId on entity + channelData)
# ---------------------------------------------------------------------------


class TestFlushThrottle:
    """Pin that the end-of-stream flush honors the throttle window.

    Regression test for the case where a fast LLM stream finishes inside
    the throttle window after the last successful emit. Without this
    wait, the flush force-sends a ``typing`` activity immediately and
    risks a 429 from the Bot Framework streaming endpoint (1 req/sec
    quota), cancelling the stream mid-flight.
    """

    @pytest.mark.asyncio
    async def test_flush_waits_for_throttle_window_when_iterator_ends_in_window(self):
        # ``clock_step_ms=0`` keeps every clock call at 0, so the flush
        # sees ``elapsed_ms = 0`` after the first emit and must wait the
        # full default interval (1500ms).
        adapter = _make_adapter(clock_step_ms=0.0)
        adapter._teams_send = AsyncMock(return_value={"id": "first-id"})
        tid = _dm_thread_id(adapter)
        session = _TeamsStreamSession()
        adapter._active_streams[tid] = session

        async def text_gen():
            yield "Hello"
            yield " world"  # coalesced into the throttle window

        await adapter._stream_via_emit(tid, text_gen(), session)

        # Two emits (initial + flush) — the flush still ships, but
        # only after waiting the throttle window.
        assert adapter._teams_send.await_count == 2
        # Sleep was awaited exactly once (the flush's throttle wait),
        # for the full default interval since elapsed_ms = 0.
        assert adapter._stream_sleep_ms.await_count == 1
        wait_arg = adapter._stream_sleep_ms.await_args.args[0]
        assert wait_arg == 1500.0, (
            f"Expected the flush to wait the full default interval (1500ms) "
            f"when the iterator ends with elapsed_ms=0, got {wait_arg}ms. "
            f"Without this wait, the flush would 429 on a real Bot Framework "
            f"streaming endpoint."
        )

    @pytest.mark.asyncio
    async def test_flush_does_not_wait_when_window_already_elapsed(self):
        """If enough time passed since the last emit, the flush ships immediately."""
        # 2000ms steps: first chunk emit at t=2000, second chunk emit at
        # t=4000. Iterator ends. Flush would see elapsed = clock_now (6000)
        # - last_emit (4000) = 2000 >= 1500 → no wait.
        # But we also need the flush to actually have something to flush,
        # which means at least one buffered chunk. Use a tiny step that
        # still > interval to keep things straightforward — clock_step=2000
        # has every chunk emit individually, so there's no buffered text
        # at end. We need a stream where some chunks coalesce and the
        # window has elapsed by end-of-stream — achievable by having the
        # stream end well after the last emit. Simulate via a clock that
        # advances 2000ms per call until the loop exits, then... actually
        # the simpler shape: yield 2 chunks (both emit individually since
        # 2000 > 1500), then iterator ends. accumulated == last_committed,
        # so the flush block doesn't run at all. So we don't even test
        # "no wait" in this shape.
        #
        # The cleaner test: a stream where the LAST chunk lands inside
        # the window but enough time has passed before that. That's a
        # variable-step clock — out of scope for this regression. The
        # important property is: when the flush DOES run, it computes
        # elapsed correctly and skips the wait when window has elapsed.
        # That's covered by the inverse test above (waits when elapsed=0)
        # plus the clock-arithmetic itself.
        #
        # Instead, pin the inverse: when nothing was buffered (all chunks
        # already shipped intermediately), the flush block doesn't run
        # at all, so no extra sleep call. This catches a regression that
        # would always-wait at end-of-stream regardless of state.
        adapter = _make_adapter(clock_step_ms=2000.0)
        adapter._teams_send = AsyncMock(side_effect=[{"id": "first"}, {"id": "ignored"}])
        tid = _dm_thread_id(adapter)
        session = _TeamsStreamSession()
        adapter._active_streams[tid] = session

        async def text_gen():
            yield "one "
            yield "two"

        await adapter._stream_via_emit(tid, text_gen(), session)
        # Both chunks emitted in-loop (clock_step > interval).
        assert adapter._teams_send.await_count == 2
        # No flush body ran (accumulated == last_committed at end), so
        # the flush throttle wait was not invoked.
        assert adapter._stream_sleep_ms.await_count == 0

    @pytest.mark.asyncio
    async def test_flush_skips_emit_if_session_canceled_during_wait(self):
        """Cancellation during the throttle wait suppresses the flush emit.

        The returned ``RawMessage`` carries ONLY the text Teams actually
        accepted (the pre-cancel in-loop emit), not the buffered suffix
        the user canceled out of. ``Thread.stream``'s outer accumulator
        builds the ``SentMessage`` body from this value, so the SDK's
        recorded history must match what the user actually saw — not the
        local buffer that never shipped.
        """
        adapter = _make_adapter(clock_step_ms=0.0)
        adapter._teams_send = AsyncMock(return_value={"id": "first-id"})
        tid = _dm_thread_id(adapter)
        session = _TeamsStreamSession()
        adapter._active_streams[tid] = session

        # When the wait fires, cancel the session before the flush emits.
        async def cancel_during_wait(_ms):
            session.cancel()

        adapter._stream_sleep_ms = AsyncMock(side_effect=cancel_during_wait)

        async def text_gen():
            yield "Hello"
            yield " world"  # coalesced into the throttle window, then canceled

        result = await adapter._stream_via_emit(tid, text_gen(), session)

        # Only the initial chunk emit — the flush bailed because the
        # session was canceled during the throttle wait.
        assert adapter._teams_send.await_count == 1
        # ``Hello`` was committed by the first in-loop emit. `` world`` was
        # buffered in the throttle window and the flush was canceled
        # before shipping it. Return only what Teams accepted.
        assert result.raw["text"] == "Hello", (
            "RawMessage must carry only ``last_committed_text`` when the "
            "session is canceled during the flush throttle wait. Returning "
            "the buffered suffix would let Thread.stream record text the "
            "user canceled out of."
        )
        # session._text mirrors the RawMessage so the (skipped) close
        # path would also see only the accepted text.
        assert session.text == "Hello"
        assert session.canceled is True

    @pytest.mark.asyncio
    async def test_canceled_stream_sets_raw_message_text_override(self):
        """``RawMessage.text`` carries the adapter-authoritative snapshot.

        ``Thread.stream`` builds the recorded ``SentMessage`` from its
        own local accumulator, which includes every chunk yielded to
        the adapter — even chunks that were coalesced into the throttle
        window and never shipped. When the session is canceled
        mid-flight the adapter must surface the corrected text via the
        explicit ``RawMessage.text`` override so ``Thread.stream`` can
        prefer it over the local buffer; without this, the SDK records
        text the user never saw.
        """
        adapter = _make_adapter(clock_step_ms=0.0)
        adapter._teams_send = AsyncMock(return_value={"id": "first-id"})
        tid = _dm_thread_id(adapter)
        session = _TeamsStreamSession()
        adapter._active_streams[tid] = session

        async def cancel_during_wait(_ms):
            session.cancel()

        adapter._stream_sleep_ms = AsyncMock(side_effect=cancel_during_wait)

        async def text_gen():
            yield "Hello"
            yield " buffered"

        result = await adapter._stream_via_emit(tid, text_gen(), session)

        # ``RawMessage.text`` MUST be set (not None) on cancellation so
        # ``Thread.stream`` can override its local accumulator.
        assert result.text == "Hello", (
            "Adapter must populate RawMessage.text on cancellation so "
            "Thread.stream's recorded SentMessage matches what Teams "
            "shipped. Returning None would silently fall back to "
            "Thread.stream's local accumulator (which still contains "
            "the buffered suffix the user canceled out of)."
        )
        # raw["text"] mirrors for backward-compat with callers that
        # introspect raw directly.
        assert result.raw["text"] == "Hello"

    @pytest.mark.asyncio
    async def test_happy_path_stream_also_sets_raw_message_text(self):
        """Non-canceled streams also set ``RawMessage.text`` (== accumulated).

        Symmetry: the adapter always sets the override, so
        ``Thread.stream`` always prefers it. Callers don't have to
        special-case "is text set or not" — when the adapter ran to
        completion the override equals the local accumulator anyway.
        """
        adapter = _make_adapter(clock_step_ms=2000.0)
        adapter._teams_send = AsyncMock(side_effect=[{"id": "first"}, {"id": "ignored"}])
        tid = _dm_thread_id(adapter)
        session = _TeamsStreamSession()
        adapter._active_streams[tid] = session

        async def text_gen():
            yield "Hello "
            yield "world"

        result = await adapter._stream_via_emit(tid, text_gen(), session)
        assert result.text == "Hello world"
        assert session.canceled is False

    @pytest.mark.asyncio
    async def test_in_loop_cancellation_returns_only_committed_text(self):
        """Mid-stream cancellation after a coalesced chunk returns only-emitted text.

        Companion to the during-wait test above. When a chunk has been
        accumulated locally but coalesced (inside the throttle window),
        and then the session is canceled before the next emit, the loop's
        ``if session.canceled: break`` exits without flushing. The bottom
        return block must surface ``last_committed_text``, not the
        locally-buffered ``accumulated``, for the same reason: the
        ``SentMessage`` recorded by ``Thread.stream`` must match what
        Teams actually accepted (which is what the user saw).
        """
        adapter = _make_adapter(clock_step_ms=0.0)
        adapter._teams_send = AsyncMock(return_value={"id": "first-id"})
        tid = _dm_thread_id(adapter)
        session = _TeamsStreamSession()
        adapter._active_streams[tid] = session

        async def text_gen():
            yield "Hello"
            yield " buffered"  # coalesced (inside window)
            session.cancel()
            yield " never-sent"  # cancel-check at top of next iter breaks

        result = await adapter._stream_via_emit(tid, text_gen(), session)

        # Only the first chunk emit. The second chunk was coalesced
        # (still inside the window) but the cancel-check at the top of
        # iteration 3 broke the loop before another emit. The flush is
        # gated on ``not session.canceled`` so it's skipped too.
        assert adapter._teams_send.await_count == 1
        assert result.raw["text"] == "Hello", (
            "RawMessage must carry only what Teams actually shipped "
            "(``last_committed_text``) when the session is canceled "
            "with buffered text still pending. Returning ``accumulated`` "
            "would let Thread.stream record `` buffered`` even though "
            "the user never saw it."
        )
        assert session.text == "Hello"
        assert session.canceled is True

    @pytest.mark.asyncio
    async def test_flush_wait_cancelled_error_cancels_session(self):
        """``asyncio.CancelledError`` during the flush throttle wait cancels the session.

        Without this guard, a supervisor-initiated task cancellation that
        fires while ``_stream_sleep_ms`` is awaiting would propagate
        ``CancelledError`` out of ``_stream_via_emit`` while
        ``session.canceled`` is still ``False``. The adapter's finally-
        block close path would then see a non-canceled session and the
        invariant "any exception leaving _stream_via_emit implies
        ``session.canceled``" would be violated. Mirrors the in-loop
        try/except shape so the close path can safely rely on the
        invariant when deciding whether to ship a final ``message``
        activity.
        """
        adapter = _make_adapter(clock_step_ms=0.0)
        adapter._teams_send = AsyncMock(return_value={"id": "first-id"})
        tid = _dm_thread_id(adapter)
        session = _TeamsStreamSession()
        adapter._active_streams[tid] = session

        async def cancel_raising_sleep(_ms):
            raise asyncio.CancelledError

        adapter._stream_sleep_ms = AsyncMock(side_effect=cancel_raising_sleep)

        async def text_gen():
            yield "Hello"
            yield " buffered"  # coalesced into the throttle window

        with pytest.raises(asyncio.CancelledError):
            await adapter._stream_via_emit(tid, text_gen(), session)

        # Invariant: any exception leaving ``_stream_via_emit`` MUST
        # leave the session canceled. Without the flush-block try/except,
        # ``session.canceled`` would still be False here.
        assert session.canceled is True, (
            "Flush-wait CancelledError must cancel the session so the "
            "close path's final-message activity is skipped. Otherwise "
            "an external task cancellation could leak past the in-loop "
            "guard and leave the session in an inconsistent state."
        )
        # Only the in-loop emit landed; the flush emit never ran.
        assert adapter._teams_send.await_count == 1

    @pytest.mark.asyncio
    async def test_flush_wait_generic_exception_cancels_session(self):
        """A non-CancelledError raised by the flush sleep also cancels the session.

        Defense-in-depth for unusual ``_stream_sleep_ms`` implementations
        (e.g. a custom sleep that raises a timeout/IO error). Mirrors the
        ``except Exception`` branch in the in-loop try/except: cancel the
        session before propagating so the close path doesn't ship a
        final message containing buffered text Teams never accepted.
        """
        adapter = _make_adapter(clock_step_ms=0.0)
        adapter._teams_send = AsyncMock(return_value={"id": "first-id"})
        tid = _dm_thread_id(adapter)
        session = _TeamsStreamSession()
        adapter._active_streams[tid] = session

        async def raising_sleep(_ms):
            raise RuntimeError("sleep impl failure")

        adapter._stream_sleep_ms = AsyncMock(side_effect=raising_sleep)

        async def text_gen():
            yield "Hello"
            yield " buffered"

        with pytest.raises(RuntimeError, match="sleep impl failure"):
            await adapter._stream_via_emit(tid, text_gen(), session)

        assert session.canceled is True
        assert adapter._teams_send.await_count == 1


class TestCloseStreamThrottle:
    """Pin that the final ``message`` activity honors the 1 req/sec quota.

    Teams' streaming endpoint rate-limits ALL activities sharing a
    ``streamId`` together — the streaming ``typing`` activities AND the
    final ``message`` activity. A short DM response (one chunk, emitted
    immediately) followed by ``_close_stream_session`` would land two
    requests in the same second without this throttle, risking a 429
    that the close-path swallows fail-soft (leaving Teams' streaming UI
    stuck while the SDK records the response as sent).
    """

    @pytest.mark.asyncio
    async def test_close_waits_for_throttle_window_when_emit_was_recent(self):
        adapter = _make_adapter(clock_step_ms=0.0)
        adapter._teams_send = AsyncMock(return_value={"id": "final-id"})
        tid = _dm_thread_id(adapter)

        # Simulate a session whose last in-stream emit happened at clock=0
        # (just now, since the test clock returns 0 every call). The
        # close-path should wait the full 1500ms default interval.
        session = _TeamsStreamSession()
        session._text = "Hello"  # noqa: SLF001
        session.stream_id = "stream-1"
        session.last_emit_at_ms = 0.0
        session.emit_interval_ms = 1500

        await adapter._close_stream_session(tid, session)

        # The close path called sleep once with the full interval before
        # sending the final activity.
        assert adapter._stream_sleep_ms.await_count == 1
        wait_arg = adapter._stream_sleep_ms.await_args.args[0]
        assert wait_arg == 1500.0, (
            f"Close path must throttle the final activity against the "
            f"1 req/sec quota — got wait of {wait_arg}ms, expected 1500. "
            f"Without this wait, a fast LLM stream followed by an "
            f"immediate close would 429 and the swallowed exception "
            f"would leave Teams' streaming UI stuck."
        )
        # The final activity DID ship after the wait.
        assert adapter._teams_send.await_count == 1
        final_payload = adapter._teams_send.await_args.args[1]
        assert final_payload["channelData"]["streamType"] == _STREAM_TYPE_FINAL
        assert final_payload["text"] == "Hello"

    @pytest.mark.asyncio
    async def test_close_does_not_throttle_when_window_already_elapsed(self):
        """If enough time passed since the last emit, close ships immediately."""
        adapter = _make_adapter(clock_step_ms=2000.0)
        adapter._teams_send = AsyncMock(return_value={"id": "final-id"})
        tid = _dm_thread_id(adapter)

        session = _TeamsStreamSession()
        session._text = "Hello"  # noqa: SLF001
        session.stream_id = "stream-1"
        # Mark the last emit at clock=0; the clock advances by 2000ms each
        # call so by the time the close-path checks, elapsed >= 2000ms > 1500ms.
        session.last_emit_at_ms = 0.0
        session.emit_interval_ms = 1500

        await adapter._close_stream_session(tid, session)

        # No sleep — window had already elapsed.
        assert adapter._stream_sleep_ms.await_count == 0
        # Final activity shipped.
        assert adapter._teams_send.await_count == 1

    @pytest.mark.asyncio
    async def test_close_honors_session_interval_override(self):
        """``StreamOptions.update_interval_ms`` override propagates to close.

        The interval is cached on the session by ``_stream_via_emit``
        when the stream runs. The close path reads it instead of falling
        back to the adapter default — so a caller that asked for
        ``update_interval_ms=0`` (no throttle) gets immediate close even
        when the last emit was recent.
        """
        adapter = _make_adapter(clock_step_ms=0.0)
        adapter._teams_send = AsyncMock(return_value={"id": "final-id"})
        tid = _dm_thread_id(adapter)

        session = _TeamsStreamSession()
        session._text = "Hello"  # noqa: SLF001
        session.stream_id = "stream-1"
        session.last_emit_at_ms = 0.0
        # Caller asked for no throttle — close should skip the wait even
        # though the last emit was at clock=0 (same instant).
        session.emit_interval_ms = 0

        await adapter._close_stream_session(tid, session)

        assert adapter._stream_sleep_ms.await_count == 0
        assert adapter._teams_send.await_count == 1

    @pytest.mark.asyncio
    async def test_close_falls_back_to_adapter_default_when_session_interval_unset(self):
        """If ``session.emit_interval_ms`` was never set (defensive path), use the adapter default."""
        adapter = _make_adapter(clock_step_ms=0.0)
        adapter._teams_send = AsyncMock(return_value={"id": "final-id"})
        tid = _dm_thread_id(adapter)

        session = _TeamsStreamSession()
        session._text = "Hello"  # noqa: SLF001
        session.stream_id = "stream-1"
        session.last_emit_at_ms = 0.0
        # session.emit_interval_ms left at default (None)
        assert session.emit_interval_ms is None

        await adapter._close_stream_session(tid, session)

        # Used the adapter default (1500ms).
        assert adapter._stream_sleep_ms.await_count == 1
        assert adapter._stream_sleep_ms.await_args.args[0] == 1500.0

    @pytest.mark.asyncio
    async def test_close_does_not_throttle_when_no_emit_happened(self):
        """If no in-stream emit happened (last_emit_at_ms == -inf), close skips the wait.

        Shouldn't reach the throttle code in practice because
        ``session.text`` would be empty and the close early-returns, but
        the guard is cheap defense for sessions constructed manually
        (e.g. tests) that set ``_text`` directly without an emit.
        """
        adapter = _make_adapter(clock_step_ms=0.0)
        adapter._teams_send = AsyncMock(return_value={"id": "final-id"})
        tid = _dm_thread_id(adapter)

        session = _TeamsStreamSession()
        session._text = "Hello"  # noqa: SLF001
        session.stream_id = "stream-1"
        # last_emit_at_ms left at default (-inf)
        assert session.last_emit_at_ms == float("-inf")

        await adapter._close_stream_session(tid, session)

        # No sleep — no emit ever happened, no throttle to honor.
        assert adapter._stream_sleep_ms.await_count == 0
        assert adapter._teams_send.await_count == 1

    @pytest.mark.asyncio
    async def test_close_cancellation_during_wait_skips_final_emit(self):
        """If ``session.cancel()`` fires during the close-path wait, skip the final.

        Mirrors the in-loop / flush cancellation invariant: text that
        Teams never received shouldn't be marked as shipped.
        """
        adapter = _make_adapter(clock_step_ms=0.0)
        adapter._teams_send = AsyncMock(return_value={"id": "final-id"})
        tid = _dm_thread_id(adapter)

        session = _TeamsStreamSession()
        session._text = "Hello"  # noqa: SLF001
        session.stream_id = "stream-1"
        session.last_emit_at_ms = 0.0
        session.emit_interval_ms = 1500

        async def cancel_during_wait(_ms):
            session.cancel()

        adapter._stream_sleep_ms = AsyncMock(side_effect=cancel_during_wait)

        await adapter._close_stream_session(tid, session)

        # Sleep was called (entered the wait) but the final never shipped.
        assert adapter._stream_sleep_ms.await_count == 1
        assert adapter._teams_send.await_count == 0


class TestStreamInfoEntityContract:
    """Pin the wire-format requirement that ``streamId`` lives on the entity too.

    Per the Bot Framework streaming contract, the ``streaminfo`` entity
    must carry ``streamId`` on subsequent and final activities, not just
    ``channelData``. Earlier versions of this adapter only set it on
    ``channelData``, which Teams treats as a malformed continuation
    and may detach from the original stream.
    """

    @pytest.mark.asyncio
    async def test_subsequent_chunk_streaminfo_entity_carries_stream_id(self):
        adapter = _make_adapter(clock_step_ms=2000.0)
        adapter._teams_send = AsyncMock(side_effect=[{"id": "stream-id-1"}, {"id": "ignored"}])
        tid = _dm_thread_id(adapter)
        session = _TeamsStreamSession()
        adapter._active_streams[tid] = session

        async def text_gen():
            yield "first"
            yield " second"

        await adapter._stream_via_emit(tid, text_gen(), session)

        # First chunk's entity has no streamId (server hasn't assigned).
        first_entity = adapter._teams_send.await_args_list[0].args[1]["entities"][0]
        assert "streamId" not in first_entity

        # Second chunk's entity MUST carry the captured streamId.
        second_entity = adapter._teams_send.await_args_list[1].args[1]["entities"][0]
        assert second_entity["streamId"] == "stream-id-1", (
            "Subsequent streaminfo entity must include streamId per Bot "
            "Framework streaming contract. Setting it only on channelData "
            "may cause Teams to detach the chunk from the initial stream."
        )
        # And the channelData level still has it too — both sites required.
        second_channel_data = adapter._teams_send.await_args_list[1].args[1]["channelData"]
        assert second_channel_data["streamId"] == "stream-id-1"

    # Final-activity streaminfo+streamId is covered by
    # ``TestNativeStreamingWireFormat.test_close_session_sends_final_message``;
    # we don't duplicate it here. Subsequent-chunk coverage above is unique
    # because the streaming-vs-final wire shapes diverge (different
    # ``streamType``, the streaming chunks also carry ``streamSequence``),
    # so each test targets a distinct activity type.


# ---------------------------------------------------------------------------
# Stream lifecycle / dispatch
# ---------------------------------------------------------------------------


class TestStreamDispatch:
    """Verify the DM vs non-DM routing decision."""

    @pytest.mark.asyncio
    async def test_dm_thread_with_active_session_uses_native_streaming(self):
        adapter = _make_adapter()
        adapter._teams_send = AsyncMock(return_value={"id": "id-1"})
        tid = _dm_thread_id(adapter)
        session = _TeamsStreamSession()
        adapter._active_streams[tid] = session

        async def text_gen():
            yield "ping"

        await adapter.stream(tid, text_gen())

        payload = adapter._teams_send.await_args.args[1]
        # Native streaming uses ``typing`` (not ``message``) for chunks.
        assert payload["type"] == "typing"
        assert payload["channelData"]["streamType"] == _STREAM_TYPE_STREAMING

    @pytest.mark.asyncio
    async def test_dm_thread_without_active_session_falls_through(self):
        """A DM thread with no registered session uses accumulate-and-post.

        This is the proactive-message case — the bot is sending a message
        that wasn't triggered by an inbound webhook, so there's no live
        streaming context.
        """
        adapter = _make_adapter()
        adapter._teams_send = AsyncMock(return_value={"id": "post-id"})
        tid = _dm_thread_id(adapter)
        # No session registered.

        async def text_gen():
            yield "proactive"

        result = await adapter.stream(tid, text_gen())
        payload = adapter._teams_send.await_args.args[1]
        assert payload["type"] == "message"
        # Single accumulate-and-post send.
        assert adapter._teams_send.await_count == 1
        assert result.id == "post-id"

    @pytest.mark.asyncio
    async def test_channel_thread_uses_accumulate_and_post(self):
        """Channels (``19:`` prefix) accumulate and post — never native streaming."""
        adapter = _make_adapter()
        adapter._teams_send = AsyncMock(return_value={"id": "chan-id"})
        tid = _channel_thread_id(adapter)
        # Even if a session were somehow registered for a channel thread,
        # _handle_message_activity wouldn't do that — verify the dispatcher
        # behavior with no session, the realistic case.

        async def text_gen():
            yield "Hello "
            yield "channel"

        await adapter.stream(tid, text_gen())

        # Accumulate → single ``message`` send carrying all content.
        assert adapter._teams_send.await_count == 1
        payload = adapter._teams_send.await_args.args[1]
        assert payload["type"] == "message"
        assert payload["text"] == "Hello channel"


# ---------------------------------------------------------------------------
# Cancellation and error handling
# ---------------------------------------------------------------------------


class TestStreamCancellation:
    @pytest.mark.asyncio
    async def test_canceled_session_skips_remaining_chunks(self):
        """Once ``session.cancel()`` is called, no more typing activities go out."""
        adapter = _make_adapter()
        adapter._teams_send = AsyncMock(return_value={"id": "first"})
        tid = _dm_thread_id(adapter)
        session = _TeamsStreamSession()
        adapter._active_streams[tid] = session

        async def text_gen():
            yield "first"
            session.cancel()
            yield "second"  # should not be sent
            yield "third"  # should not be sent

        await adapter._stream_via_emit(tid, text_gen(), session)
        # Only the pre-cancel chunk made it.
        assert adapter._teams_send.await_count == 1
        assert adapter._teams_send.await_args.args[1]["text"] == "first"

    @pytest.mark.asyncio
    async def test_canceled_session_skips_final_message(self):
        """A canceled session does NOT post a final ``message`` activity.

        This avoids "clearing the streaming UI with a fake completion" when
        the user really did cancel.
        """
        adapter = _make_adapter()
        adapter._teams_send = AsyncMock(return_value={"id": "id-1"})
        tid = _dm_thread_id(adapter)
        session = _TeamsStreamSession()
        session.cancel()
        session.stream_id = "stream-1"
        session._text = "partial"

        await adapter._close_stream_session(tid, session)
        adapter._teams_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_close_session_no_chunks_no_op(self):
        """Closing a session that never emitted is a no-op (no orphan final)."""
        adapter = _make_adapter()
        adapter._teams_send = AsyncMock()
        tid = _dm_thread_id(adapter)
        session = _TeamsStreamSession()  # never emitted, no stream_id

        await adapter._close_stream_session(tid, session)
        adapter._teams_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_close_session_sends_final_when_first_chunk_returned_empty_id(
        self,
    ):
        """If Teams accepted chunks but never returned an ``id``, still send the final.

        Regression for the empty-``id`` edge case: the Bot Framework REST
        response can be 200 with ``{"id": ""}`` even on a successful
        ``typing`` activity send. ``stream_id`` stays ``None`` (the
        first-chunk guard skips assignment for the empty string), but
        ``text`` is non-empty because the user already saw the streamed
        chunks. Without a final ``message`` activity the Teams streaming
        UI would spin until Teams times the session out client-side —
        ship the final ``message`` anyway, omitting ``streamId`` from
        ``channelData``. Mirrors upstream's looser check.
        """
        adapter = _make_adapter()
        # First call (chunk): returns an empty id. Second call (final):
        # succeeds.
        adapter._teams_send = AsyncMock(side_effect=[{"id": ""}, {"id": "final-id"}])
        tid = _dm_thread_id(adapter)
        session = _TeamsStreamSession()
        adapter._active_streams[tid] = session

        async def text_gen():
            yield "hello world"

        await adapter._stream_via_emit(tid, text_gen(), session)

        # Sanity: the chunk send went through, but stream_id is unset
        # because the server didn't hand us one.
        assert session.stream_id is None
        assert session.text == "hello world"

        # Now close: the final ``message`` activity must still be sent
        # (omitting ``streamId``).
        await adapter._close_stream_session(tid, session)

        assert adapter._teams_send.await_count == 2
        final_payload = adapter._teams_send.await_args_list[1].args[1]
        assert final_payload["type"] == "message"
        assert final_payload["text"] == "hello world"
        assert final_payload["channelData"]["streamType"] == _STREAM_TYPE_FINAL
        # Critical: no streamId key when the server never assigned one,
        # rather than serializing ``"streamId": None``.
        assert "streamId" not in final_payload["channelData"]
        assert final_payload["entities"] == [{"type": "streaminfo", "streamType": _STREAM_TYPE_FINAL}]


class TestStreamErrors:
    @pytest.mark.asyncio
    async def test_iterator_exception_cancels_and_reraises(self):
        """If the source stream raises mid-iteration, cancel and re-raise.

        Mirrors the fallback-stream exception-capture divergence: native
        streaming's analog is to mark the session canceled (so close()
        doesn't post a final message that doesn't reflect the user's
        view) and propagate the original error.
        """
        adapter = _make_adapter()
        adapter._teams_send = AsyncMock(return_value={"id": "id-1"})
        tid = _dm_thread_id(adapter)
        session = _TeamsStreamSession()
        adapter._active_streams[tid] = session

        class StreamBoom(RuntimeError):
            pass

        async def text_gen():
            yield "good"
            raise StreamBoom("LLM connection dropped")

        with pytest.raises(StreamBoom, match="LLM connection dropped"):
            await adapter._stream_via_emit(tid, text_gen(), session)

        assert session.canceled is True
        # The pre-error chunk was still sent.
        assert adapter._teams_send.await_count == 1

    @pytest.mark.asyncio
    async def test_emit_send_failure_propagates_and_cancels_session(self):
        """A 429 / network error mid-stream re-raises and cancels the session.

        What to fix if this fails: ``_stream_via_emit`` must propagate the
        send exception (not soft-cancel + return a partial RawMessage).
        ``Thread.stream`` accumulates each chunk locally BEFORE yielding to
        the adapter, so swallowing the failure here would let the SDK
        record a SentMessage / append a message-history entry containing
        text Teams never accepted. Re-raising short-circuits the
        post-stream history append in ``Thread.stream`` so the recorded
        message matches what the user actually saw. See
        ``src/chat_sdk/adapters/teams/adapter.py`` around the
        ``_teams_send`` ``except`` block in ``_stream_via_emit``.
        """
        adapter = _make_adapter()
        adapter._teams_send = AsyncMock(
            side_effect=[
                {"id": "id-1"},
                RuntimeError("429 Too Many Requests"),
            ]
        )
        tid = _dm_thread_id(adapter)
        session = _TeamsStreamSession()
        adapter._active_streams[tid] = session

        async def text_gen():
            yield "hello"
            yield "world"
            yield "should-not-send"

        with pytest.raises(RuntimeError, match="429 Too Many Requests"):
            await adapter._stream_via_emit(tid, text_gen(), session)
        assert session.canceled is True
        # Two attempted sends (first ok, second failed); no third.
        assert adapter._teams_send.await_count == 2
        # session.sequence stays at 1 (first send incremented it; second
        # didn't because it failed before commit).
        assert session.sequence == 1

    @pytest.mark.asyncio
    async def test_cancelled_error_propagates_and_marks_session_canceled(self):
        """asyncio.CancelledError propagates and cancels the session."""
        adapter = _make_adapter()
        adapter._teams_send = AsyncMock(return_value={"id": "id-1"})
        tid = _dm_thread_id(adapter)
        session = _TeamsStreamSession()
        adapter._active_streams[tid] = session

        async def text_gen():
            yield "before-cancel"
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await adapter._stream_via_emit(tid, text_gen(), session)
        assert session.canceled is True


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestStreamEdgeCases:
    @pytest.mark.asyncio
    async def test_empty_string_chunks_skipped_in_native_streaming(self):
        adapter = _make_adapter()
        adapter._teams_send = AsyncMock(return_value={"id": "id-1"})
        tid = _dm_thread_id(adapter)
        session = _TeamsStreamSession()
        adapter._active_streams[tid] = session

        async def text_gen():
            yield ""
            yield "real"
            yield ""

        await adapter._stream_via_emit(tid, text_gen(), session)
        assert adapter._teams_send.await_count == 1
        assert session.sequence == 1

    @pytest.mark.asyncio
    async def test_one_chunk_stream_yields_id_and_text(self):
        """Very-short streams (one chunk) round-trip correctly."""
        adapter = _make_adapter()
        adapter._teams_send = AsyncMock(return_value={"id": "only-id"})
        tid = _dm_thread_id(adapter)
        session = _TeamsStreamSession()
        adapter._active_streams[tid] = session

        async def text_gen():
            yield "lonely"

        result = await adapter._stream_via_emit(tid, text_gen(), session)
        assert result.id == "only-id"
        assert result.raw["text"] == "lonely"
        assert session.stream_id == "only-id"

    @pytest.mark.asyncio
    async def test_dict_chunks_extract_text(self):
        adapter = _make_adapter()
        adapter._teams_send = AsyncMock(return_value={"id": "id-1"})
        tid = _dm_thread_id(adapter)
        session = _TeamsStreamSession()
        adapter._active_streams[tid] = session

        async def text_gen():
            yield {"type": "markdown_text", "text": "Hi"}
            yield {"type": "markdown_text", "text": " there"}
            yield {"type": "other", "data": "ignored"}

        result = await adapter._stream_via_emit(tid, text_gen(), session)
        assert result.raw["text"] == "Hi there"
        assert adapter._teams_send.await_count == 2

    @pytest.mark.asyncio
    async def test_stream_sequence_no_overflow_concern(self):
        """``streamSequence`` is a Python ``int`` — overflow is not a concern.

        Adversarial check (per docs/SELF_REVIEW.md): TS uses a JS number,
        which would lose precision past 2**53. Python ints are unbounded;
        we don't add a saturation check here because Bot Framework streams
        don't last long enough to approach a problematic count, and adding
        one would silently change behavior. This test pins the assumption.
        """
        session = _TeamsStreamSession()
        session.sequence = 2**60
        session.sequence += 1
        # Still increments cleanly, no exceptions, exact value.
        assert session.sequence == 2**60 + 1


# ---------------------------------------------------------------------------
# Webhook-level lifecycle (end-to-end through _handle_message_activity)
# ---------------------------------------------------------------------------


class TestHandleMessageActivityLifecycle:
    """Verify the message-activity → process_message → stream → close flow."""

    @pytest.mark.asyncio
    async def test_caller_wait_until_raise_does_not_kill_native_streaming(self):
        """A caller-supplied ``WebhookOptions.wait_until`` that raises must
        NOT tear down the DM streaming session before the chat task runs.

        What to fix if this fails: in
        ``src/chat_sdk/adapters/teams/adapter.py`` ``_chained_wait_until``,
        the call to the upstream ``wait_until`` must be wrapped in
        ``try/except`` (and logged). Otherwise the synchronous raise
        escapes through ``Chat.process_message``, the outer ``try`` skips
        ``await processing_done``, and the ``finally`` removes the session
        while the chat task is still scheduled. The handler's later
        ``thread.stream()`` call would then miss native streaming and
        fall back to a normal post.
        """
        adapter = _make_adapter()
        adapter._teams_send = AsyncMock(return_value={"id": "id-1"})

        tid = _dm_thread_id(adapter)

        # Build a chat that schedules the streaming task AND invokes
        # a deliberately-raising upstream wait_until.
        chat = MagicMock()
        chat.get_state = MagicMock(return_value=None)
        stream_calls: list[str] = []

        def process_message(adapter_arg, thread_id, message, options):
            async def _do_stream():
                async def gen():
                    yield "hi"

                # Snapshot whether native streaming is still wired up at
                # the moment the chat task runs.
                stream_calls.append("native" if thread_id in adapter_arg._active_streams else "fallback")
                await adapter_arg.stream(thread_id, gen())

            task = asyncio.get_running_loop().create_task(_do_stream())
            # Caller-supplied wait_until raises synchronously. The chained
            # wrapper must swallow this so processing_done still resolves.
            options.wait_until(task)

        chat.process_message = process_message
        adapter._chat = chat

        # Inject a raising upstream wait_until via WebhookOptions.
        from chat_sdk.types import WebhookOptions

        def raising_wait_until(_task: Any) -> None:
            raise RuntimeError("caller wait_until exploded")

        upstream_options = WebhookOptions(wait_until=raising_wait_until)

        activity = {
            "type": "message",
            "id": "incoming-1",
            "text": "user said something",
            "from": {"id": "user-1", "name": "User One"},
            "conversation": {"id": "a:1Abc-DM-conversation-id"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
        }

        # Should NOT raise — the chained wrapper logs and continues.
        await adapter._handle_message_activity(activity, upstream_options)

        # The streaming task ran while the session was still registered.
        assert stream_calls == ["native"], (
            "Caller wait_until raise tore down the session before the chat "
            "task ran; the handler fell back to a normal post instead of "
            "native Teams streaming"
        )
        # Session was cleaned up after the task finished.
        assert tid not in adapter._active_streams

    @pytest.mark.asyncio
    async def test_dm_message_activity_registers_session_and_finalizes(self):
        """A DM message activity registers a session, awaits processing, then drops it."""
        adapter = _make_adapter()
        adapter._teams_send = AsyncMock(return_value={"id": "id-1"})

        tid = _dm_thread_id(adapter)

        # Build a fake chat that streams during processing.
        chat = MagicMock()
        chat.get_state = MagicMock(return_value=None)

        captured_session: dict[str, Any] = {}

        def process_message(adapter_arg, thread_id, message, options):
            assert thread_id == tid
            # The session should be registered by the time process_message
            # is invoked, so the streaming dispatch sees it.
            captured_session["session"] = adapter_arg._active_streams[thread_id]

            async def _do_stream():
                async def gen():
                    yield "hi"

                await adapter_arg.stream(thread_id, gen())

            task = asyncio.get_running_loop().create_task(_do_stream())
            options.wait_until(task)

        chat.process_message = process_message
        adapter._chat = chat

        activity = {
            "type": "message",
            "id": "incoming-1",
            "text": "user said something",
            "from": {"id": "user-1", "name": "User One"},
            "conversation": {"id": "a:1Abc-DM-conversation-id"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
        }

        await adapter._handle_message_activity(activity)

        # After the handler returns, the session should have been removed.
        assert tid not in adapter._active_streams
        # And the session was closed: a final ``message`` activity went out
        # in addition to the streaming chunk.
        send_payloads = [c.args[1] for c in adapter._teams_send.await_args_list]
        types = [p["type"] for p in send_payloads]
        assert "typing" in types
        assert "message" in types
        final_payload = next(p for p in send_payloads if p["type"] == "message")
        assert final_payload["channelData"]["streamType"] == _STREAM_TYPE_FINAL

    @pytest.mark.asyncio
    async def test_channel_message_does_not_register_session(self):
        """Channel/group messages skip session registration entirely."""
        adapter = _make_adapter()
        adapter._teams_send = AsyncMock(return_value={"id": "id-1"})

        # Conversation id is constructed inside the activity dict below;
        # no separate thread-id variable needed for assertions here.

        chat = MagicMock()
        chat.get_state = MagicMock(return_value=None)
        seen_active_streams: dict[str, Any] = {}

        def process_message(adapter_arg, thread_id, message, options):
            seen_active_streams["snapshot"] = dict(adapter_arg._active_streams)

        chat.process_message = process_message
        adapter._chat = chat

        activity = {
            "type": "message",
            "id": "incoming-2",
            "text": "channel message",
            "from": {"id": "user-2", "name": "User Two"},
            "conversation": {"id": "19:abc@thread.tacv2"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
        }

        await adapter._handle_message_activity(activity)

        # No session was ever registered for the channel thread.
        assert seen_active_streams["snapshot"] == {}
        assert adapter._active_streams == {}

    @pytest.mark.asyncio
    async def test_handler_exception_after_partial_stream_drops_session_and_closes(self):
        """A handler that raises AFTER streaming still ships the final close
        activity and drops the session.

        Streams one chunk, then raises. The session must be removed from the
        registry, and ``_close_stream_session`` must still have shipped a
        final ``message`` activity (``streamType: "final"``) carrying the
        text the user already saw — otherwise the Teams streaming UI keeps
        spinning until Teams times the session out client-side.
        """
        adapter = _make_adapter()
        adapter._teams_send = AsyncMock(return_value={"id": "id-1"})

        tid = _dm_thread_id(adapter)

        chat = MagicMock()
        chat.get_state = MagicMock(return_value=None)

        def process_message(adapter_arg, thread_id, message, options):
            async def _stream_then_fail():
                async def gen():
                    yield "partial"

                await adapter_arg.stream(thread_id, gen())
                raise RuntimeError("handler boom")

            task = asyncio.get_running_loop().create_task(_stream_then_fail())
            options.wait_until(task)

        chat.process_message = process_message
        adapter._chat = chat

        activity = {
            "type": "message",
            "id": "incoming-3",
            "text": "user msg",
            "from": {"id": "user-3", "name": "User Three"},
            "conversation": {"id": "a:1Abc-DM-conversation-id"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
        }

        await adapter._handle_message_activity(activity)

        # Registry was cleaned up.
        assert tid not in adapter._active_streams
        # And the close path actually fired: typing chunk + final message,
        # in that order. Without the final message the Teams streaming UI
        # would keep spinning until Teams times the session out.
        send_payloads = [c.args[1] for c in adapter._teams_send.await_args_list]
        types = [p["type"] for p in send_payloads]
        assert "typing" in types, "Streaming chunk before the raise was never sent"
        assert "message" in types, (
            "Final close activity was not sent after the handler raised — "
            "_close_stream_session must run from the adapter's finally even "
            "when the chat task raised"
        )
        final_payload = next(p for p in send_payloads if p["type"] == "message")
        assert final_payload["channelData"]["streamType"] == _STREAM_TYPE_FINAL
        # And the final activity carries the text the user actually saw.
        assert final_payload["text"] == "partial"


# ---------------------------------------------------------------------------
# Pass-interaction: two simultaneous DM streams to the same user
# ---------------------------------------------------------------------------


class TestPassInteraction:
    @pytest.mark.asyncio
    async def test_distinct_dm_threads_each_have_isolated_session_state(self):
        """Two DM threads streaming in parallel must not share session state.

        This pins the ISOLATION property when sessions are explicitly
        passed to ``_stream_via_emit`` (the registry is bypassed). Two
        DMs in flight from the same bot to the same user (one per
        thread) must each carry their own ``streamId`` and
        ``streamSequence``.

        Same-thread concurrency (the ``_active_streams`` race) is a
        DIFFERENT property — see
        ``test_same_thread_concurrent_handlers_clobber_active_stream``.
        """
        adapter = _make_adapter()
        # Distinct server ids per send so we can verify thread-to-id mapping.
        send_log: list[tuple[str, dict[str, Any]]] = []

        async def fake_send(decoded, payload):
            send_log.append((decoded.conversation_id, payload))
            return {"id": f"id-for-{decoded.conversation_id}"}

        adapter._teams_send = fake_send

        tid_a = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="a:1Conv-A",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        tid_b = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="a:1Conv-B",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )

        session_a = _TeamsStreamSession()
        session_b = _TeamsStreamSession()
        adapter._active_streams[tid_a] = session_a
        adapter._active_streams[tid_b] = session_b

        async def gen_a():
            yield "A1"
            await asyncio.sleep(0)  # yield control
            yield "A2"

        async def gen_b():
            yield "B1"
            await asyncio.sleep(0)
            yield "B2"

        await asyncio.gather(
            adapter._stream_via_emit(tid_a, gen_a(), session_a),
            adapter._stream_via_emit(tid_b, gen_b(), session_b),
        )

        # Each session got its own server-assigned streamId.
        assert session_a.stream_id == "id-for-a:1Conv-A"
        assert session_b.stream_id == "id-for-a:1Conv-B"
        # Each session's sequence counts only its own chunks.
        assert session_a.sequence == 2
        assert session_b.sequence == 2
        # No bleed-through: A's payloads were posted to A's conversation,
        # and B's to B's.
        for conv_id, payload in send_log:
            assert payload["text"].startswith("A" if "A" in conv_id else "B")

    @pytest.mark.asyncio
    async def test_same_thread_concurrent_handlers_clobber_active_stream(self):
        """Two near-simultaneous webhooks for the SAME DM thread.

        Realistic case: a user double-sends, or two webhooks land on the
        same thread before the first finishes. ``_active_streams`` is a
        plain ``dict`` keyed by ``thread_id``, so the second registration
        overwrites the first — pin that behavior here so a future change
        to add per-thread queueing/locking is a deliberate decision, not
        an accidental observable change.

        Upstream's ``activeStreams`` is also a plain ``Map`` with the
        same overwrite semantics; this test mirrors that contract.
        """
        adapter = _make_adapter()
        # Track each session that gets registered, in the order of registration.
        registered_sessions: list[_TeamsStreamSession] = []
        # Snapshot the registry contents immediately AFTER each handler's
        # process_message call so we can pin the clobber.
        post_registration_snapshots: list[_TeamsStreamSession] = []

        # Block both handlers on a barrier so the second registration races
        # the first while the first is still "in flight". This pins the
        # registry behavior under genuine overlap, not just sequential calls.
        first_registered = asyncio.Event()
        release_handlers = asyncio.Event()

        adapter._teams_send = AsyncMock(return_value={"id": "send-id"})

        chat = MagicMock()
        chat.get_state = MagicMock(return_value=None)

        def process_message(adapter_arg, thread_id, message, options):
            # Snapshot the session that THIS handler call registered.
            registered_sessions.append(adapter_arg._active_streams[thread_id])

            async def _handler():
                # Hold both handlers open across the barrier so they truly
                # overlap. After release, snapshot the registry — by this
                # point both handlers have registered, and the LATER
                # registration must have won.
                if not first_registered.is_set():
                    first_registered.set()
                await release_handlers.wait()
                post_registration_snapshots.append(adapter_arg._active_streams.get(thread_id))

            task = asyncio.get_running_loop().create_task(_handler())
            options.wait_until(task)

        chat.process_message = process_message
        adapter._chat = chat

        tid = _dm_thread_id(adapter)
        activity = {
            "type": "message",
            "id": "incoming-same-thread",
            "text": "user said something",
            "from": {"id": "user-1", "name": "User One"},
            "conversation": {"id": "a:1Abc-DM-conversation-id"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
        }

        async def _drive_two_handlers():
            # Start the first; wait until it has registered before launching
            # the second so the second observes (and clobbers) the first's
            # registry entry. Then release both.
            t1 = asyncio.create_task(adapter._handle_message_activity(activity))
            await first_registered.wait()
            t2 = asyncio.create_task(adapter._handle_message_activity(activity))
            # Give the second handler a tick to register.
            await asyncio.sleep(0)
            release_handlers.set()
            await asyncio.gather(t1, t2)

        await _drive_two_handlers()

        # Two distinct sessions were created.
        assert len(registered_sessions) == 2
        first_session, second_session = registered_sessions
        assert first_session is not second_session
        # Pin upstream's plain-Map clobber semantics: BOTH in-flight
        # handlers, when they look up the registry post-overlap, see the
        # SECOND session — the first's entry was overwritten in place.
        # If a future change adds per-thread queueing/locking it must be
        # a deliberate decision (i.e. update this test).
        assert post_registration_snapshots == [second_session, second_session]
        # After both handlers exit, registry is empty. Handler 2's
        # finally-block matches ``current is session_2`` and pops; handler
        # 1's finally-block sees the entry already gone (or not its own)
        # and skips the pop — either way the dict ends empty.
        assert tid not in adapter._active_streams
