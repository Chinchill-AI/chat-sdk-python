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


def _make_adapter() -> TeamsAdapter:
    return TeamsAdapter(
        TeamsAdapterConfig(
            app_id="test-app-id",
            app_password="test-password",
            logger=MagicMock(
                debug=MagicMock(),
                info=MagicMock(),
                warn=MagicMock(),
                error=MagicMock(),
            ),
        )
    )


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
        assert payload["entities"] == [{"type": "streaminfo", "streamType": _STREAM_TYPE_FINAL}]


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
    async def test_emit_send_failure_cancels_session(self):
        """A 429 / network error mid-stream cancels the session, no exception."""
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

        # No exception bubbles — soft cancel.
        result = await adapter._stream_via_emit(tid, text_gen(), session)
        assert session.canceled is True
        # Two attempted sends (first ok, second failed); no third.
        assert adapter._teams_send.await_count == 2
        # RawMessage carries the accumulated text including the failed-send chunk
        # (which the user already saw rendered locally).
        assert result.raw["text"] == "helloworld"

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
    async def test_dm_message_registers_session_and_closes_after_processing(self):
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
    async def test_handler_exception_still_drops_session_and_closes(self):
        """A failing handler doesn't leak the session — finally always cleans up."""
        adapter = _make_adapter()
        adapter._teams_send = AsyncMock(return_value={"id": "id-1"})

        tid = _dm_thread_id(adapter)

        chat = MagicMock()
        chat.get_state = MagicMock(return_value=None)

        def process_message(adapter_arg, thread_id, message, options):
            async def _failing():
                raise RuntimeError("handler boom")

            task = asyncio.get_running_loop().create_task(_failing())
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

        # Exception is swallowed by the chat's task error path; what we
        # care about here is no leaked session.
        await adapter._handle_message_activity(activity)
        assert tid not in adapter._active_streams


# ---------------------------------------------------------------------------
# Pass-interaction: two simultaneous DM streams to the same user
# ---------------------------------------------------------------------------


class TestPassInteraction:
    @pytest.mark.asyncio
    async def test_two_concurrent_dm_streams_have_independent_sessions(self):
        """Two streams to two different DM threads must not share state.

        Per docs/SELF_REVIEW.md pass-interaction check: two DMs in flight
        from the same bot to the same user (one per thread) must each
        carry their own ``streamId`` and ``streamSequence``.
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
