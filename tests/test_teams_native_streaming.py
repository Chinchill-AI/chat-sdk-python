"""Behavioral tests for Teams native streaming via the SDK ``IStreamer``.

Port of upstream ``@chat-adapter/teams@4.30.0`` ``index.ts``
(``streamViaEmit`` / ``streamWithEmitter``): for DMs, the Teams adapter
dispatches stream chunks through the Teams SDK ``IStreamer.emit()`` and lets
the SDK ship the Bot Framework streaming wire format
(``streamType``/``streamSequence``/``streamId``), throttle flushes, and retry
on 429. Group chats / channels / proactive messages fall back to a single
buffered ``post_message`` (SDK-backed from PR 2).

These tests mock ``ctx.stream`` with a ``StreamerProtocol`` double and pin the
ADAPTER-level contract:

- one ``emit()`` per non-empty chunk (markdown_text dict + dataclass + string);
- ``close()`` is NEVER called by ``stream()`` / ``_stream_via_emit`` — the SDK
  (here: the handler ``finally``) closes the streamer after the handler returns;
- the first chunk's server-assigned id is captured via ``on_chunk`` and only
  awaited when text was emitted and the stream was not canceled;
- DM-only ``processing_done`` blocking + streamer registration/teardown;
- group / proactive buffered fallback through ``post_message``;
- both cancellation paths (``.canceled`` property AND ``StreamCancelledError``).

The Bot Framework wire format and the throttle are now SDK-internal, so the
wire-format / throttle-internal assertions from the hand-rolled era are dropped.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from chat_sdk.adapters.teams.adapter import TeamsAdapter
from chat_sdk.adapters.teams.types import TeamsAdapterConfig, TeamsThreadId
from chat_sdk.chat import Chat
from chat_sdk.testing import MockLogger, create_mock_state
from chat_sdk.thread import ThreadImpl, _ThreadImplConfig
from chat_sdk.types import UNSET, ChatConfig, Message, RawMessage, StreamOptions


def _make_adapter() -> TeamsAdapter:
    """Build a TeamsAdapter with a no-op logger for streaming tests."""
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


class _SentActivity:
    """Minimal stand-in for the SDK ``SentActivity`` (carries an ``id``)."""

    def __init__(self, activity_id: str) -> None:
        self.id = activity_id


class FakeStreamer:
    """A ``StreamerProtocol`` double mirroring the SDK ``HttpStream`` surface.

    Records every ``emit()`` call, exposes ``canceled`` / ``closed``, fires
    registered ``on_chunk`` handlers (so the adapter can capture the first
    chunk's id), and tracks whether ``close()`` was called — the central
    assertion is that the adapter never calls it during streaming.

    ``canceled`` can be flipped at construction (already-canceled) or after N
    emits via ``cancel_after`` to exercise the user-pressed-Stop path. When
    ``raise_on_emit`` is set, the Nth ``emit`` raises ``StreamCancelledError``
    to exercise the exception-based cancellation path.
    """

    def __init__(
        self,
        *,
        chunk_id: str = "stream-msg-1",
        canceled: bool = False,
        cancel_after: int | None = None,
        raise_cancel_after: int | None = None,
    ) -> None:
        self.emitted: list[str] = []
        # Status texts passed to ``update()``, and the order of ``update`` /
        # ``emit`` calls (``("update", text)`` / ``("emit", text)``).
        self.updates: list[str] = []
        self.call_order: list[tuple[str, str]] = []
        self._canceled = canceled
        self.closed = False
        self.close_calls = 0
        self._chunk_id = chunk_id
        self._cancel_after = cancel_after
        self._raise_cancel_after = raise_cancel_after
        self._chunk_handlers: list[Any] = []
        self._first_chunk_emitted = False
        self.count = 0
        self.sequence = 1

    @property
    def canceled(self) -> bool:
        return self._canceled

    def on_chunk(self, handler: Any) -> None:
        self._chunk_handlers.append(handler)

    def on_close(self, handler: Any) -> None:  # pragma: no cover - parity shim
        pass

    def update(self, text: str) -> None:
        # Synchronous, like the SDK ``HttpStream.update`` (which queues an
        # informative typing activity through ``emit``).
        self.updates.append(text)
        self.call_order.append(("update", text))

    def clear_text(self) -> None:  # pragma: no cover - parity shim
        pass

    def emit(self, activity: Any) -> None:
        from microsoft_teams.apps import StreamCancelledError

        # Faithful to the SDK ``HttpStream.emit``: raise at the top when the
        # stream is canceled, BEFORE recording the chunk (a canceled emit never
        # ships text).
        if self._canceled:
            raise StreamCancelledError("Stream has been cancelled.")

        # The Nth emit raises a cancel error (channel stopped the stream): flip
        # canceled and raise here, again WITHOUT recording the chunk.
        if self._raise_cancel_after is not None and len(self.emitted) + 1 >= self._raise_cancel_after:
            self._canceled = True
            raise StreamCancelledError("Teams channel stopped the stream.")

        text = activity if isinstance(activity, str) else getattr(activity, "text", "")
        self.emitted.append(text)
        self.call_order.append(("emit", text))

        # Fire on_chunk for the FIRST emitted chunk only, like the SDK
        # (the first stream activity returns the assigned message id).
        if not self._first_chunk_emitted:
            self._first_chunk_emitted = True
            sent = _SentActivity(self._chunk_id)
            for handler in self._chunk_handlers:
                asyncio.get_running_loop().create_task(handler(sent))

        if self._cancel_after is not None and len(self.emitted) >= self._cancel_after:
            self._canceled = True

    async def close(self) -> Any:
        self.close_calls += 1
        self.closed = True
        return _SentActivity(self._chunk_id)


async def _register_streamer(adapter: TeamsAdapter, thread_id: str, streamer: FakeStreamer) -> None:
    """Register a streamer as if ``_handle_message_activity`` captured it."""
    adapter._active_streams[thread_id] = streamer  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# DM native streaming via IStreamer.emit
# ---------------------------------------------------------------------------


class TestStreamViaEmit:
    """The DM path emits each chunk through the SDK ``IStreamer``."""

    @pytest.mark.asyncio
    async def test_emits_once_per_string_chunk(self):
        adapter = _make_adapter()
        tid = _dm_thread_id(adapter)
        streamer = FakeStreamer()
        await _register_streamer(adapter, tid, streamer)

        async def gen():
            yield "Hello "
            yield "World"

        result = await adapter.stream(tid, gen())

        # One emit per non-empty chunk — the SDK coalesces/throttles internally.
        assert streamer.emitted == ["Hello ", "World"]
        assert isinstance(result, RawMessage)
        assert result.raw["text"] == "Hello World"

    @pytest.mark.asyncio
    async def test_emits_markdown_text_dict_chunks(self):
        adapter = _make_adapter()
        tid = _dm_thread_id(adapter)
        streamer = FakeStreamer()
        await _register_streamer(adapter, tid, streamer)

        async def gen():
            yield {"type": "markdown_text", "text": "foo "}
            yield {"type": "markdown_text", "text": "bar"}

        result = await adapter.stream(tid, gen())
        assert streamer.emitted == ["foo ", "bar"]
        assert result.raw["text"] == "foo bar"

    @pytest.mark.asyncio
    async def test_emits_markdown_text_dataclass_chunks(self):
        """Dataclass ``MarkdownTextChunk`` form is extracted like Thread does."""
        adapter = _make_adapter()
        tid = _dm_thread_id(adapter)
        streamer = FakeStreamer()
        await _register_streamer(adapter, tid, streamer)

        class _Chunk:
            def __init__(self, text: str) -> None:
                self.type = "markdown_text"
                self.text = text

        async def gen():
            yield _Chunk("alpha ")
            yield _Chunk("beta")

        result = await adapter.stream(tid, gen())
        assert streamer.emitted == ["alpha ", "beta"]
        assert result.raw["text"] == "alpha beta"

    @pytest.mark.asyncio
    async def test_skips_empty_and_non_text_chunks(self):
        adapter = _make_adapter()
        tid = _dm_thread_id(adapter)
        streamer = FakeStreamer()
        await _register_streamer(adapter, tid, streamer)

        async def gen():
            yield ""
            yield {"type": "task_update", "data": {}}  # no text
            yield {"type": "markdown_text", "text": "kept"}
            yield ""

        result = await adapter.stream(tid, gen())
        # Only the real text chunk emits.
        assert streamer.emitted == ["kept"]
        assert result.raw["text"] == "kept"

    @pytest.mark.asyncio
    async def test_skips_thinking_chunk_by_default(self):
        """``ThinkingChunk`` is streaming-only reasoning, not message content.

        The default emit path skips it: it is never emitted to the SDK
        ``IStreamer`` and the recorded text equals the text chunks only.
        """
        from chat_sdk.types import ThinkingChunk

        adapter = _make_adapter()
        tid = _dm_thread_id(adapter)
        streamer = FakeStreamer()
        await _register_streamer(adapter, tid, streamer)

        async def gen():
            yield ThinkingChunk(content="reasoning A")
            yield "Hello "
            yield ThinkingChunk(content="reasoning B")
            yield "World"

        result = await adapter.stream(tid, gen())
        assert streamer.emitted == ["Hello ", "World"]
        assert result.raw["text"] == "Hello World"

    @pytest.mark.asyncio
    async def test_thinking_chunk_invokes_render_hook(self):
        """An opt-in ``render_thinking`` hook receives the reasoning while the
        emitted text stays thinking-free."""
        from chat_sdk.types import ThinkingChunk

        adapter = _make_adapter()
        tid = _dm_thread_id(adapter)
        streamer = FakeStreamer()
        await _register_streamer(adapter, tid, streamer)

        seen: list[str] = []

        async def render_thinking(content: str) -> None:
            seen.append(content)

        adapter.render_thinking = render_thinking  # type: ignore[attr-defined]

        async def gen():
            yield ThinkingChunk(content="thought A")
            yield "text"
            yield ThinkingChunk(content="thought B")

        result = await adapter.stream(tid, gen())
        assert seen == ["thought A", "thought B"]
        assert streamer.emitted == ["text"]
        assert result.raw["text"] == "text"

    @pytest.mark.asyncio
    async def test_never_calls_close(self):
        """``stream()`` / ``_stream_via_emit`` must NEVER call ``stream.close()``.

        The SDK (in production, the App; here, the handler ``finally``) sends
        the ``streamType: 'final'`` message after the handler returns. If the
        adapter closed the streamer itself it would double-close and could ship
        the final message before later handler output.
        """
        adapter = _make_adapter()
        tid = _dm_thread_id(adapter)
        streamer = FakeStreamer()
        await _register_streamer(adapter, tid, streamer)

        async def gen():
            yield "data"

        await adapter.stream(tid, gen())

        assert streamer.close_calls == 0, "stream() must not close the SDK streamer"
        assert streamer.closed is False

    @pytest.mark.asyncio
    async def test_captures_first_chunk_id(self):
        """The returned ``RawMessage.id`` comes from the first ``on_chunk`` event."""
        adapter = _make_adapter()
        tid = _dm_thread_id(adapter)
        streamer = FakeStreamer(chunk_id="assigned-id-42")
        await _register_streamer(adapter, tid, streamer)

        async def gen():
            yield "one "
            yield "two"

        result = await adapter.stream(tid, gen())
        assert result.id == "assigned-id-42"

    @pytest.mark.asyncio
    async def test_empty_stream_does_not_hang_on_chunk_id(self):
        """An empty stream emits nothing and must NOT await the chunk id.

        Awaiting ``id_captured`` when no chunk was delivered would hang forever
        — the adapter only awaits it when text was emitted.
        """
        adapter = _make_adapter()
        tid = _dm_thread_id(adapter)
        streamer = FakeStreamer()
        await _register_streamer(adapter, tid, streamer)

        async def gen():
            if False:  # empty async generator
                yield ""

        # Must complete (the await-id guard prevents a hang); enforce a timeout.
        result = await asyncio.wait_for(adapter.stream(tid, gen()), timeout=1.0)
        assert streamer.emitted == []
        assert result.id == ""
        assert result.raw["text"] == ""

    @pytest.mark.asyncio
    async def test_id_capture_does_not_deadlock_when_handler_runs_late(self):
        """The chunk id is awaited; if on_chunk fires asynchronously it still resolves."""
        adapter = _make_adapter()
        tid = _dm_thread_id(adapter)
        streamer = FakeStreamer(chunk_id="late-id")
        await _register_streamer(adapter, tid, streamer)

        async def gen():
            yield "x"

        result = await asyncio.wait_for(adapter.stream(tid, gen()), timeout=1.0)
        assert result.id == "late-id"


# ---------------------------------------------------------------------------
# Cancellation: the .canceled property AND StreamCancelledError
# ---------------------------------------------------------------------------


class TestCancellation:
    @pytest.mark.asyncio
    async def test_canceled_property_before_first_chunk_skips_streaming(self):
        """An already-canceled streamer routes to the buffered fallback.

        ``stream()`` checks ``active_stream.canceled`` up front — a canceled
        streamer is treated as "no native streamer", so we accumulate and post.
        """
        adapter = _make_adapter()
        tid = _dm_thread_id(adapter)
        streamer = FakeStreamer(canceled=True)
        await _register_streamer(adapter, tid, streamer)

        send = AsyncMock(return_value=_SentActivity("fallback-id"))
        adapter._app.activity_sender = SimpleNamespace(send=send)

        async def gen():
            yield "Hello world"

        result = await adapter.stream(tid, gen())

        # Did NOT emit through the canceled streamer...
        assert streamer.emitted == []
        # ...fell back to a single SDK post instead.
        send.assert_called_once()
        assert result.id == "fallback-id"

    @pytest.mark.asyncio
    async def test_cancel_mid_stream_via_property_stops_emitting(self):
        """``.canceled`` flipping mid-stream halts further emits (user Stop)."""
        adapter = _make_adapter()
        tid = _dm_thread_id(adapter)
        # Cancel after the first emit.
        streamer = FakeStreamer(cancel_after=1)
        await _register_streamer(adapter, tid, streamer)

        async def gen():
            yield "first"
            yield "second"
            yield "third"

        result = await adapter.stream(tid, gen())

        # Only the first chunk was emitted before .canceled flipped.
        assert streamer.emitted == ["first"]
        # Recorded text reflects only what was emitted before cancellation.
        assert result.raw["text"] == "first"

    @pytest.mark.asyncio
    async def test_stream_cancelled_error_is_swallowed(self):
        """A ``StreamCancelledError`` from ``emit`` is caught, not re-raised.

        Mirrors upstream ``streamViaEmit``: ``StreamCancelledError`` during
        iteration is logged and swallowed (any OTHER exception re-raises).
        """
        adapter = _make_adapter()
        tid = _dm_thread_id(adapter)
        # The 2nd emit raises StreamCancelledError.
        streamer = FakeStreamer(raise_cancel_after=2)
        await _register_streamer(adapter, tid, streamer)

        async def gen():
            yield "aa"
            yield "bb"
            yield "cc"

        # Must NOT raise.
        result = await adapter.stream(tid, gen())

        # The 2nd emit raised before shipping, so only "aa" was emitted; the
        # adapter swallows StreamCancelledError and stops. Crucially the raising
        # chunk is NOT recorded (emit raises before `accumulated += text`),
        # matching upstream's `stream.emit(text); accumulated += text;` order.
        assert streamer.emitted == ["aa"]
        assert result.raw["text"] == "aa"

    @pytest.mark.asyncio
    async def test_non_cancel_exception_propagates(self):
        """A non-cancel exception from the stream iterator surfaces to the caller."""
        adapter = _make_adapter()
        tid = _dm_thread_id(adapter)
        streamer = FakeStreamer()
        await _register_streamer(adapter, tid, streamer)

        async def gen():
            yield "ok"
            raise RuntimeError("iterator boom")

        with pytest.raises(RuntimeError, match="iterator boom"):
            await adapter.stream(tid, gen())

    @pytest.mark.asyncio
    async def test_canceled_stream_does_not_await_chunk_id(self):
        """When canceled before any chunk is delivered, the chunk id is not awaited."""
        adapter = _make_adapter()
        tid = _dm_thread_id(adapter)
        # Cancels on the very first emit (no on_chunk will fire usefully).
        streamer = FakeStreamer(cancel_after=1)
        await _register_streamer(adapter, tid, streamer)

        async def gen():
            yield "only"

        # The post-loop guard skips awaiting id when stream.canceled is True,
        # so this completes promptly rather than hanging.
        result = await asyncio.wait_for(adapter.stream(tid, gen()), timeout=1.0)
        assert result.raw["text"] == "only"


# ---------------------------------------------------------------------------
# Group / proactive / non-DM buffered fallback
# ---------------------------------------------------------------------------


class TestBufferedFallback:
    @pytest.mark.asyncio
    async def test_no_active_streamer_buffers_and_posts_once(self):
        """A thread with no registered streamer accumulates and posts once."""
        adapter = _make_adapter()
        tid = _channel_thread_id(adapter)

        send = AsyncMock(return_value=_SentActivity("posted-1"))
        adapter._app.activity_sender = SimpleNamespace(send=send)

        async def gen():
            yield "Hello "
            yield "world"

        result = await adapter.stream(tid, gen())

        send.assert_called_once()
        activity, ref = send.call_args.args
        assert ref.conversation.id == "19:abc@thread.tacv2"
        assert activity.text == "Hello world"
        assert result.id == "posted-1"

    @pytest.mark.asyncio
    async def test_empty_buffered_stream_skips_post(self):
        adapter = _make_adapter()
        tid = _channel_thread_id(adapter)

        send = AsyncMock(return_value=_SentActivity("nope"))
        adapter._app.activity_sender = SimpleNamespace(send=send)

        async def gen():
            yield ""
            yield ""

        result = await adapter.stream(tid, gen())

        send.assert_not_called()
        assert result.id == ""
        assert result.raw["text"] == ""


# ---------------------------------------------------------------------------
# Placeholder-aware streaming (upstream describe "streaming", vercel/chat#709)
# ---------------------------------------------------------------------------


class TestPlaceholderStreaming:
    @pytest.mark.asyncio
    async def test_uses_core_fallback_for_an_explicit_group_chat_placeholder(self):
        adapter = _make_adapter()
        consumed = False

        async def source():
            nonlocal consumed
            consumed = True
            yield "Done"

        result = await adapter.stream(
            _channel_thread_id(adapter),
            source(),
            StreamOptions(fallback_streaming_placeholder_text="Working..."),
        )

        assert result is None
        assert consumed is False

    @pytest.mark.asyncio
    async def test_an_empty_string_placeholder_also_uses_core_fallback(self):
        """``""`` is an explicit placeholder (a ``str``), not "unset"."""
        adapter = _make_adapter()
        consumed = False

        async def source():
            nonlocal consumed
            consumed = True
            yield "Done"

        result = await adapter.stream(
            _channel_thread_id(adapter),
            source(),
            StreamOptions(fallback_streaming_placeholder_text=""),
        )

        assert result is None
        assert consumed is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "options",
        [None, StreamOptions(), StreamOptions(fallback_streaming_placeholder_text=None)],
        ids=["no-options", "unset", "none"],
    )
    async def test_preserves_buffered_group_chat_streaming_for_placeholder(self, options: StreamOptions | None):
        """No options, ``UNSET`` and ``None`` all keep the single buffered post."""
        adapter = _make_adapter()
        tid = _channel_thread_id(adapter)
        post_message = AsyncMock(return_value=RawMessage(id="answer-id", thread_id=tid, raw={}))
        adapter.post_message = post_message  # type: ignore[method-assign]

        async def gen():
            yield "Do"
            yield "ne"

        result = await adapter.stream(tid, gen(), options)

        post_message.assert_awaited_once()
        posted_tid, postable = post_message.call_args.args
        assert posted_tid == tid
        assert postable.markdown == "Done"
        assert result is not None
        assert result.id == "answer-id"

    @pytest.mark.asyncio
    async def test_a_canceled_dm_streamer_buffers_even_with_a_placeholder(self):
        """Upstream returns ``null`` only when there is no streamer at all; a
        canceled one falls through to the buffered post."""
        adapter = _make_adapter()
        tid = _dm_thread_id(adapter)
        await _register_streamer(adapter, tid, FakeStreamer(canceled=True))
        post_message = AsyncMock(return_value=RawMessage(id="buffered-1", thread_id=tid, raw={}))
        adapter.post_message = post_message  # type: ignore[method-assign]

        async def gen():
            yield "late"

        result = await adapter.stream(tid, gen(), StreamOptions(fallback_streaming_placeholder_text="Working..."))

        assert result is not None
        assert result.id == "buffered-1"
        assert post_message.call_args.args[1].markdown == "late"

    @pytest.mark.asyncio
    async def test_sends_an_explicit_placeholder_as_native_status_before_the_first_chunk(self):
        adapter = _make_adapter()
        tid = _dm_thread_id(adapter)
        streamer = FakeStreamer(chunk_id="answer-id")

        async def gen():
            yield "Do"
            yield "ne"

        result = await adapter._stream_via_emit(tid, gen(), streamer, "Working...")  # type: ignore[arg-type]

        assert streamer.updates == ["Working..."]
        assert streamer.call_order == [("update", "Working..."), ("emit", "Do"), ("emit", "ne")]
        assert result.id == "answer-id"
        assert result.thread_id == tid

    @pytest.mark.asyncio
    async def test_stream_forwards_the_placeholder_to_the_active_dm_streamer(self):
        adapter = _make_adapter()
        tid = _dm_thread_id(adapter)
        streamer = FakeStreamer(chunk_id="dm-1")
        await _register_streamer(adapter, tid, streamer)

        async def gen():
            yield "hi"

        result = await adapter.stream(tid, gen(), StreamOptions(fallback_streaming_placeholder_text=""))

        assert streamer.call_order == [("update", ""), ("emit", "hi")]
        assert result is not None
        assert result.id == "dm-1"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("placeholder", [UNSET, None], ids=["unset", "none"])
    async def test_unset_or_none_placeholder_sends_no_native_status(self, placeholder: Any):
        adapter = _make_adapter()
        tid = _dm_thread_id(adapter)
        streamer = FakeStreamer(chunk_id="dm-2")
        await _register_streamer(adapter, tid, streamer)

        async def gen():
            yield "hi"

        result = await adapter.stream(tid, gen(), StreamOptions(fallback_streaming_placeholder_text=placeholder))

        assert streamer.updates == []
        assert streamer.call_order == [("emit", "hi")]
        assert result is not None
        assert result.id == "dm-2"

    @pytest.mark.asyncio
    async def test_a_cancel_raised_by_the_status_update_is_swallowed(self):
        """``update`` goes through ``emit`` on the SDK streamer, so a stream
        canceled between the check and the update raises ``StreamCancelledError``
        there; it is handled like a cancel during iteration."""
        from microsoft_teams.apps import StreamCancelledError

        adapter = _make_adapter()
        tid = _dm_thread_id(adapter)
        streamer = FakeStreamer()
        streamer.update = MagicMock(side_effect=StreamCancelledError("Stream has been cancelled."))  # type: ignore[method-assign]
        pulled = False

        async def gen():
            nonlocal pulled
            pulled = True
            yield "never"

        result = await adapter._stream_via_emit(tid, gen(), streamer, "Working...")  # type: ignore[arg-type]

        assert result.id == ""
        assert streamer.emitted == []
        assert pulled is False

    @pytest.mark.asyncio
    async def test_group_chat_placeholder_posts_then_edits_through_thread(self):
        """End to end through ``ThreadImpl``: an explicit placeholder in a group
        chat is posted by core and then edited to the final text."""
        adapter = _make_adapter()
        tid = _channel_thread_id(adapter)
        adapter.post_message = AsyncMock(return_value=RawMessage(id="ph-1", thread_id=tid, raw={}))  # type: ignore[method-assign]
        adapter.edit_message = AsyncMock(return_value=RawMessage(id="ph-1", thread_id=tid, raw={}))  # type: ignore[method-assign]
        thread = ThreadImpl(
            _ThreadImplConfig(
                id=tid,
                channel_id="19:abc@thread.tacv2",
                adapter=adapter,  # type: ignore[arg-type]
                state_adapter=MagicMock(),
                fallback_streaming_placeholder_text="Working...",
                streaming_update_interval_ms=0,
            )
        )

        async def gen():
            yield "Do"
            yield "ne"

        sent = await thread.post(gen())

        adapter.post_message.assert_awaited_once_with(tid, "Working...")
        assert adapter.edit_message.await_args_list[-1].args[:2] == (tid, "ph-1")
        assert sent.id == "ph-1"
        assert sent.text == "Done"


class TestFirstChunkIdWait:
    @pytest.mark.asyncio
    async def test_returns_without_an_id_when_no_chunk_is_ever_delivered(self, monkeypatch: pytest.MonkeyPatch):
        """Python-only bound: with ``microsoft-teams-apps`` 2.0.16+, a terminal
        403 on the first flush neither emits ``chunk`` nor sets ``canceled``,
        so an unbounded wait would hang the handler."""
        from chat_sdk.adapters.teams import adapter as adapter_module

        monkeypatch.setattr(adapter_module, "STREAM_FIRST_CHUNK_ID_TIMEOUT_S", 0.05)
        adapter = _make_adapter()
        tid = _dm_thread_id(adapter)
        streamer = FakeStreamer()
        streamer._chunk_handlers = []
        streamer.on_chunk = MagicMock()  # type: ignore[method-assign]  # never fires

        async def gen():
            yield "hello"

        result = await asyncio.wait_for(adapter._stream_via_emit(tid, gen(), streamer), 2)  # type: ignore[arg-type]

        assert result.id == ""
        assert result.raw == {"text": "hello"}
        assert streamer.emitted == ["hello"]
        adapter._logger.warn.assert_called_once()

    @pytest.mark.asyncio
    async def test_real_sdk_stream_with_a_terminal_403_does_not_hang(self, monkeypatch: pytest.MonkeyPatch):
        """A real SDK ``HttpStream`` whose first flush gets a 403 that is not a
        cancel: ``_stream_via_emit`` returns once the bound elapses."""
        import httpx
        from microsoft_teams.api import ApiClient
        from microsoft_teams.common import Client, ClientOptions

        from chat_sdk.adapters.teams import adapter as adapter_module

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"error": {"message": "Content stream is not allowed"}})

        client = Client(ClientOptions(token="bot-token"))
        client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        adapter = _make_adapter()
        adapter._app.api = ApiClient("https://smba.trafficmanager.net/teams", client)  # type: ignore[method-assign]
        tid = _dm_thread_id(adapter)
        stream = adapter._create_streamer(_dm_activity(), tid)
        assert stream is not None

        monkeypatch.setattr(adapter_module, "STREAM_FIRST_CHUNK_ID_TIMEOUT_S", 0.5)

        async def gen():
            yield "hello"

        result = await asyncio.wait_for(adapter._stream_via_emit(tid, gen(), stream), 10)

        assert result.id == ""
        assert result.raw == {"text": "hello"}


# ---------------------------------------------------------------------------
# Webhook-level lifecycle (end-to-end through _handle_message_activity)
# ---------------------------------------------------------------------------


def _dm_activity(conversation_id: str = "a:1Abc-DM-conversation-id", activity_id: str = "incoming-1") -> dict[str, Any]:
    return {
        "type": "message",
        "id": activity_id,
        "text": "user said something",
        "from": {"id": "user-1", "name": "User One"},
        "recipient": {"id": "28:test-app-id", "name": "bot"},
        "conversation": {"id": conversation_id, "conversationType": "personal"},
        "channelId": "msteams",
        "serviceUrl": "https://smba.trafficmanager.net/teams/",
    }


class TestHandleMessageActivityLifecycle:
    """The message-activity → process_message → stream → close flow for DMs."""

    @pytest.mark.asyncio
    async def test_dm_registers_streamer_blocks_then_closes(self):
        """A DM registers the SDK streamer, awaits processing, then closes it."""
        adapter = _make_adapter()
        streamer = FakeStreamer(chunk_id="id-1")
        # Patch streamer creation so we control the double.
        adapter._create_streamer = MagicMock(return_value=streamer)  # type: ignore[method-assign]

        tid = _dm_thread_id(adapter)
        captured: dict[str, Any] = {}

        def process_message(adapter_arg, thread_id, message, options):
            assert thread_id == tid
            # The streamer is registered by the time process_message runs.
            captured["registered"] = adapter_arg._active_streams[thread_id]

            async def _do_stream():
                async def gen():
                    yield "hi"

                await adapter_arg.stream(thread_id, gen())

            task = asyncio.get_running_loop().create_task(_do_stream())
            options.wait_until(task)

        chat = MagicMock()
        chat.get_state = MagicMock(return_value=None)
        chat.process_message = process_message
        adapter._chat = chat

        await adapter._handle_message_activity(_dm_activity())

        assert captured["registered"] is streamer
        # The handler closed the streamer (lifecycle-owner role) exactly once.
        assert streamer.close_calls == 1
        # And dropped the registry entry.
        assert tid not in adapter._active_streams
        # The chunk was emitted through the native streamer.
        assert streamer.emitted == ["hi"]

    @pytest.mark.asyncio
    async def test_channel_message_does_not_register_streamer(self):
        """Channel/group messages skip streamer registration entirely."""
        adapter = _make_adapter()
        # If _create_streamer were called for a channel, fail loudly.
        adapter._create_streamer = MagicMock(side_effect=AssertionError("should not create streamer for channel"))  # type: ignore[method-assign]

        seen: dict[str, Any] = {}

        def process_message(adapter_arg, thread_id, message, options):
            seen["snapshot"] = dict(adapter_arg._active_streams)

        chat = MagicMock()
        chat.get_state = MagicMock(return_value=None)
        chat.process_message = process_message
        adapter._chat = chat

        activity = _dm_activity(conversation_id="19:abc@thread.tacv2", activity_id="incoming-2")
        activity["conversation"]["conversationType"] = "channel"

        await adapter._handle_message_activity(activity)

        assert seen["snapshot"] == {}
        assert adapter._active_streams == {}

    @pytest.mark.asyncio
    async def test_handler_exception_after_partial_stream_closes_and_drops(self):
        """A handler that raises AFTER streaming still closes the streamer and
        drops the registry entry."""
        adapter = _make_adapter()
        streamer = FakeStreamer(chunk_id="id-1")
        adapter._create_streamer = MagicMock(return_value=streamer)  # type: ignore[method-assign]

        tid = _dm_thread_id(adapter)

        def process_message(adapter_arg, thread_id, message, options):
            async def _stream_then_fail():
                async def gen():
                    yield "partial"

                await adapter_arg.stream(thread_id, gen())
                raise RuntimeError("handler boom")

            task = asyncio.get_running_loop().create_task(_stream_then_fail())
            options.wait_until(task)

        chat = MagicMock()
        chat.get_state = MagicMock(return_value=None)
        chat.process_message = process_message
        adapter._chat = chat

        await adapter._handle_message_activity(_dm_activity(activity_id="incoming-3"))

        # Registry cleaned up and streamer closed even though the handler raised.
        assert tid not in adapter._active_streams
        assert streamer.close_calls == 1
        assert streamer.emitted == ["partial"]

    @pytest.mark.asyncio
    async def test_caller_wait_until_raise_does_not_kill_native_streaming(self):
        """A caller-supplied ``WebhookOptions.wait_until`` that raises must NOT
        tear down the DM streamer before the chat task runs."""
        from chat_sdk.types import WebhookOptions

        adapter = _make_adapter()
        streamer = FakeStreamer(chunk_id="id-1")
        adapter._create_streamer = MagicMock(return_value=streamer)  # type: ignore[method-assign]

        tid = _dm_thread_id(adapter)
        stream_calls: list[str] = []

        def process_message(adapter_arg, thread_id, message, options):
            async def _do_stream():
                async def gen():
                    yield "hi"

                stream_calls.append("native" if thread_id in adapter_arg._active_streams else "fallback")
                await adapter_arg.stream(thread_id, gen())

            task = asyncio.get_running_loop().create_task(_do_stream())
            options.wait_until(task)

        chat = MagicMock()
        chat.get_state = MagicMock(return_value=None)
        chat.process_message = process_message
        adapter._chat = chat

        def raising_wait_until(_task: Any) -> None:
            raise RuntimeError("caller wait_until exploded")

        upstream_options = WebhookOptions(wait_until=raising_wait_until)

        # Should NOT raise — the chained wrapper logs and continues.
        await adapter._handle_message_activity(_dm_activity(), upstream_options)

        assert stream_calls == ["native"], (
            "Caller wait_until raise tore down the streamer before the chat "
            "task ran; the handler fell back to a normal post instead of "
            "native Teams streaming"
        )
        assert tid not in adapter._active_streams
        assert streamer.close_calls == 1

    @pytest.mark.asyncio
    async def test_streamer_creation_failure_falls_back_to_fire_and_forget(self):
        """If the SDK streamer can't be built, the DM still processes (no stream)."""
        adapter = _make_adapter()
        adapter._create_streamer = MagicMock(return_value=None)  # type: ignore[method-assign]

        tid = _dm_thread_id(adapter)
        seen: dict[str, Any] = {}

        def process_message(adapter_arg, thread_id, message, options):
            seen["registered"] = thread_id in adapter_arg._active_streams

        chat = MagicMock()
        chat.get_state = MagicMock(return_value=None)
        chat.process_message = process_message
        adapter._chat = chat

        await adapter._handle_message_activity(_dm_activity())

        # No streamer registered → fire-and-forget path.
        assert seen["registered"] is False
        assert tid not in adapter._active_streams


class TestHandleMessageActivityWithRealChat:
    """The DM ``processing_done`` gate driven by a real ``Chat`` (vercel/chat#943).

    ``Chat.process_message`` now hands ``wait_until`` an error-swallowing
    wrapper Task by default (the raw handler task only with
    ``propagate_handler_errors=True``). The gate must still wait for the
    handler either way, and the caller's options must survive the shim.
    """

    @staticmethod
    def _wire(adapter: TeamsAdapter) -> tuple[Chat, list[str]]:
        chat = Chat(
            ChatConfig(
                user_name="bot",
                adapters={"teams": adapter},
                state=create_mock_state(),
                logger=MockLogger(),
            )
        )
        adapter._chat = chat  # type: ignore[assignment]
        stream_modes: list[str] = []

        @chat.on_direct_message
        async def handler(thread, message, channel=None, context=None):
            # Yield several times so a gate that resolved early would have
            # already closed/unregistered the streamer.
            for _ in range(5):
                await asyncio.sleep(0)
            stream_modes.append("native" if thread.id in adapter._active_streams else "fallback")

            async def gen():
                yield "hello"

            await adapter.stream(thread.id, gen())
            raise RuntimeError("handler boom")

        return chat, stream_modes

    @pytest.mark.asyncio
    @pytest.mark.parametrize("propagate", [False, True])
    async def test_dm_gate_waits_for_handler_through_wait_until(self, propagate: bool):
        from chat_sdk.types import WebhookOptions

        adapter = _make_adapter()
        streamer = FakeStreamer(chunk_id="id-1")
        adapter._create_streamer = MagicMock(return_value=streamer)  # type: ignore[method-assign]
        _chat, stream_modes = self._wire(adapter)
        handed: list[Any] = []

        await adapter._handle_message_activity(
            _dm_activity(activity_id=f"incoming-real-{propagate}"),
            WebhookOptions(wait_until=handed.append, propagate_handler_errors=propagate),
        )

        # The gate held the streamer open until the handler finished streaming.
        assert stream_modes == ["native"]
        assert streamer.emitted == ["hello"]
        assert streamer.close_calls == 1
        assert _dm_thread_id(adapter) not in adapter._active_streams

        # The caller's wait_until got a Task; the option spread through the
        # shim decides whether it carries the handler error.
        assert len(handed) == 1
        background = handed[0]
        assert isinstance(background, asyncio.Task)
        (outcome,) = await asyncio.gather(background, return_exceptions=True)
        if propagate:
            assert isinstance(outcome, RuntimeError)
            assert str(outcome) == "handler boom"
        else:
            assert outcome is None

    # Python-specific: a host that cancels the default wait_until wrapper
    # (e.g. a background-task timeout) must not release the gate while the
    # shielded handler is still streaming.
    @pytest.mark.asyncio
    async def test_cancelled_wait_until_wrapper_keeps_dm_streamer_open(self):
        from chat_sdk.types import WebhookOptions

        adapter = _make_adapter()
        streamer = FakeStreamer(chunk_id="id-1")
        adapter._create_streamer = MagicMock(return_value=streamer)  # type: ignore[method-assign]
        _chat, stream_modes = self._wire(adapter)

        def cancel_wrapper(task: Any) -> None:
            task.cancel()

        await adapter._handle_message_activity(
            _dm_activity(activity_id="incoming-real-cancelled-wrapper"),
            WebhookOptions(wait_until=cancel_wrapper),
        )

        assert stream_modes == ["native"]
        assert streamer.emitted == ["hello"]
        assert streamer.close_calls == 1
        assert _dm_thread_id(adapter) not in adapter._active_streams


# ---------------------------------------------------------------------------
# Streamer construction from the inbound activity
# ---------------------------------------------------------------------------


class TestCreateStreamer:
    """``_create_streamer`` builds the SDK stream on both SDK lines (#250).

    ``microsoft-teams-apps`` 2.0.x exposes ``App.activity_sender.create_stream``;
    2.1.x removed ``ActivitySender`` and builds ``HttpStream(api, ref)``. The
    branch tests force each shape by stubbing the App, so both run on
    whichever SDK is installed. ``test_real_sdk_stream_is_pinned_to_inbound_service_url``
    runs unstubbed against the installed SDK.
    """

    SOVEREIGN_URL = "https://smba.infra.gov.teams.microsoft.us/teams/"

    @staticmethod
    def _drop_activity_sender(adapter: TeamsAdapter) -> None:
        """Make the App look like 2.1.x (no ``activity_sender``)."""
        if hasattr(adapter._app, "activity_sender"):
            del adapter._app.activity_sender

    def test_real_sdk_stream_is_pinned_to_inbound_service_url(self):
        """No stubs: the installed SDK yields an ``HttpStream`` on its own client.

        The stream's client must target the inbound activity's service URL and
        must not be the shared ``App.api``, so outbound calls to another region
        (which use ``_api_for`` clients) can never redirect the stream's chunks.
        """
        from microsoft_teams.apps import HttpStream

        adapter = _make_adapter()
        tid = _dm_thread_id(adapter)
        streamer = adapter._create_streamer(_dm_activity(), tid)

        assert isinstance(streamer, HttpStream)
        assert streamer._ref.conversation.id == "a:1Abc-DM-conversation-id"
        assert streamer._ref.bot.id == "28:test-app-id"
        assert streamer._client is not adapter._app.api
        assert streamer._client.service_url == "https://smba.trafficmanager.net/teams"

        regional = adapter._api_for(self.SOVEREIGN_URL)
        assert regional.service_url == self.SOVEREIGN_URL.rstrip("/")
        assert streamer._client is not regional
        assert streamer._client.service_url == "https://smba.trafficmanager.net/teams"

    def test_uses_activity_sender_when_present(self):
        """2.0.x shape: ``activity_sender.create_stream(ref)`` builds the stream."""
        adapter = _make_adapter()
        tid = _dm_thread_id(adapter)
        created: dict[str, Any] = {}
        fake = FakeStreamer()

        def create_stream(ref):
            created["ref"] = ref
            return fake

        adapter._app.activity_sender = MagicMock(create_stream=create_stream)

        assert adapter._create_streamer(_dm_activity(), tid) is fake
        ref = created["ref"]
        assert ref.conversation.id == "a:1Abc-DM-conversation-id"
        assert ref.service_url == "https://smba.trafficmanager.net/teams/"
        assert ref.bot.id == "28:test-app-id"

    def test_builds_http_stream_on_scoped_client_without_activity_sender(self, monkeypatch):
        """2.1.x shape: ``HttpStream`` gets ``App.api`` scoped to the inbound service URL."""
        import microsoft_teams.apps as teams_apps

        adapter = _make_adapter()
        tid = _dm_thread_id(adapter)
        self._drop_activity_sender(adapter)

        scoped_api = object()
        api = MagicMock()
        api.from_service_url = MagicMock(return_value=scoped_api)
        adapter._app.api = api

        built: dict[str, Any] = {}
        fake = FakeStreamer()

        def fake_http_stream(client, ref):
            built["client"] = client
            built["ref"] = ref
            return fake

        monkeypatch.setattr(teams_apps, "HttpStream", fake_http_stream)

        assert adapter._create_streamer(_dm_activity(), tid) is fake
        api.from_service_url.assert_called_once_with("https://smba.trafficmanager.net/teams/")
        assert built["client"] is scoped_api
        assert built["ref"].conversation.id == "a:1Abc-DM-conversation-id"
        assert built["ref"].bot.id == "28:test-app-id"

    def test_returns_none_without_service_url(self):
        adapter = _make_adapter()
        tid = _dm_thread_id(adapter)
        activity = _dm_activity()
        del activity["serviceUrl"]
        assert adapter._create_streamer(activity, tid) is None

    def test_returns_none_on_disallowed_service_url(self):
        adapter = _make_adapter()
        tid = _dm_thread_id(adapter)
        activity = _dm_activity()
        activity["serviceUrl"] = "https://evil.example.com/"
        # SSRF allow-list rejection is caught → None (buffered fallback).
        assert adapter._create_streamer(activity, tid) is None

    def test_returns_none_when_create_stream_raises(self):
        adapter = _make_adapter()
        tid = _dm_thread_id(adapter)

        def boom(ref):
            raise RuntimeError("sdk exploded")

        adapter._app.activity_sender = MagicMock(create_stream=boom)
        assert adapter._create_streamer(_dm_activity(), tid) is None
        adapter._logger.warn.assert_called_once()
        assert adapter._logger.warn.call_args.args[1]["error"] == "sdk exploded"

    def test_returns_none_when_sdk_has_neither_stream_entry_point(self):
        """No ``activity_sender`` and no ``ApiClient.from_service_url`` → buffered fallback.

        Guards an SDK shape we do not support: falling through to
        ``HttpStream(App.api, ref)`` there could share the retargetable client.
        """
        adapter = _make_adapter()
        tid = _dm_thread_id(adapter)
        self._drop_activity_sender(adapter)
        adapter._app.api = MagicMock(spec=["service_url"])

        assert adapter._create_streamer(_dm_activity(), tid) is None
        adapter._logger.warn.assert_called_once()


# ---------------------------------------------------------------------------
# History fidelity (the #1 risk of the transitional-divergence unwind)
# ---------------------------------------------------------------------------


class _CapturingHistory:
    """Records every ``append`` so tests can assert recorded message history."""

    def __init__(self) -> None:
        self.appended: list[tuple[str, Message]] = []

    async def append(self, thread_id: str, message: Message) -> None:
        self.appended.append((thread_id, message))

    async def get_messages(self, thread_id: str, limit: int | None = None) -> list[Message]:
        return [m for (t, m) in self.appended if t == thread_id]


def _make_thread_over_adapter(adapter: TeamsAdapter, thread_id: str, history: _CapturingHistory) -> ThreadImpl:
    """Build a real ThreadImpl driving the real Teams adapter.stream()."""
    return ThreadImpl(
        _ThreadImplConfig(
            id=thread_id,
            channel_id="a:1Abc-DM-conversation-id",
            adapter=adapter,  # type: ignore[arg-type]
            state_adapter=MagicMock(),
            thread_history=history,
            is_dm=True,
            streaming_update_interval_ms=500,
        )
    )


class TestHistoryFidelity:
    """Prove the recorded message history is correct after unwinding the two
    transitional divergences (``RawMessage.text`` + ``update_interval_ms``
    non-seeding). This is the SentMessage→Message recording path through
    ``Thread.stream`` driving the real Teams ``adapter.stream()``."""

    @pytest.mark.asyncio
    async def test_native_stream_records_full_streamed_text(self):
        """The recorded history entry carries the full streamed text.

        With the ``RawMessage.text`` override removed, ``Thread._handle_stream``
        builds the ``SentMessage`` from its own local accumulator. Since the
        Teams adapter now emits each chunk as it is yielded, the accumulator
        and what the SDK streamer shipped stay in lockstep.
        """
        adapter = _make_adapter()
        tid = _dm_thread_id(adapter)
        streamer = FakeStreamer(chunk_id="hist-id-1")
        await _register_streamer(adapter, tid, streamer)

        history = _CapturingHistory()
        thread = _make_thread_over_adapter(adapter, tid, history)

        async def gen():
            yield "Hello "
            yield "streamed "
            yield "world"

        sent = await thread.post(gen())

        # Streamer received each chunk.
        assert streamer.emitted == ["Hello ", "streamed ", "world"]
        # SentMessage text == the full streamed text.
        assert sent.text == "Hello streamed world"
        assert sent.id == "hist-id-1"
        # Exactly one history entry, matching the SentMessage.
        assert len(history.appended) == 1
        recorded_thread_id, recorded_message = history.appended[0]
        assert recorded_thread_id == tid
        assert isinstance(recorded_message, Message)
        assert recorded_message.text == "Hello streamed world", (
            "Recorded message history diverged from the streamed text — the "
            "SentMessage→Message recording path is broken after the unwind."
        )

    @pytest.mark.asyncio
    async def test_cancelled_native_stream_records_pulled_text(self):
        """On mid-stream cancellation, history records exactly the text the
        wrapping iterator PULLED — upstream parity, no over- or under-recording.

        This pins the precise post-unwind cancellation semantics, which is the
        sharpest history-fidelity edge. ``Thread._handle_stream`` wraps the
        stream so its accumulator grows as each chunk is pulled via ``next()``
        BEFORE the adapter sees it. The adapter's loop is
        ``for chunk in stream: if stream.canceled: break; emit(chunk)`` — so
        the iteration that detects cancellation has already PULLED its chunk
        (the wrapper accumulated it) but does NOT emit it. Result: history
        records the emitted text plus exactly one pulled-but-unemitted chunk,
        and stops there — it does NOT drain the rest of the iterator.

        This is byte-for-byte what upstream records (``streamViaEmit`` +
        ``handleStream``'s wrapping iterator behave identically), which is why
        the old ``RawMessage.text`` override is no longer needed: there is no
        Python-only divergence to reconcile. If someone changes the loop to
        drain the whole iterator on cancel, the recorded text would balloon to
        ``"keptdropped-1dropped-2"`` and this test fails.
        """
        adapter = _make_adapter()
        tid = _dm_thread_id(adapter)
        # Cancel after the first emit.
        streamer = FakeStreamer(chunk_id="hist-id-2", cancel_after=1)
        await _register_streamer(adapter, tid, streamer)

        history = _CapturingHistory()
        thread = _make_thread_over_adapter(adapter, tid, history)

        async def gen():
            yield "kept"
            yield "dropped-1"
            yield "dropped-2"

        sent = await thread.post(gen())

        # Only the first chunk was emitted to the SDK streamer.
        assert streamer.emitted == ["kept"]
        # History records the emitted chunk plus the single chunk pulled in the
        # iteration that detected cancellation — and crucially NOT "dropped-2",
        # proving the loop breaks instead of draining the iterator.
        assert sent.text == "keptdropped-1"
        assert "dropped-2" not in sent.text
        assert len(history.appended) == 1
        _, recorded = history.appended[0]
        assert recorded.text == "keptdropped-1", (
            "History must record exactly the text the wrapping iterator pulled "
            "(upstream parity). Draining the iterator on cancel would record "
            "the whole tail; recording only emitted text would require the "
            "removed RawMessage.text override."
        )

    @pytest.mark.asyncio
    async def test_update_interval_ms_seeds_normally(self):
        """``Thread._handle_stream`` now seeds ``update_interval_ms`` (500ms).

        This is the second unwound divergence: the Teams adapter no longer owns
        a quota throttle (the SDK ``IStreamer`` does), so Thread seeds the
        thread default like upstream. We capture the ``StreamOptions`` the
        adapter received.
        """
        adapter = _make_adapter()
        tid = _dm_thread_id(adapter)
        streamer = FakeStreamer(chunk_id="hist-id-3")
        await _register_streamer(adapter, tid, streamer)

        captured: dict[str, Any] = {}
        real_stream = adapter.stream

        async def spy_stream(thread_id, text_stream, options=None):
            captured["options"] = options
            return await real_stream(thread_id, text_stream, options)

        adapter.stream = spy_stream  # type: ignore[method-assign]

        history = _CapturingHistory()
        thread = _make_thread_over_adapter(adapter, tid, history)

        async def gen():
            yield "x"

        await thread.post(gen())

        assert captured["options"] is not None
        assert captured["options"].update_interval_ms == 500, (
            "Thread must seed update_interval_ms with the thread default after "
            "the non-seeding divergence was unwound (upstream parity)."
        )


# ---------------------------------------------------------------------------
# Pass-interaction: distinct DM threads keep isolated streamers
# ---------------------------------------------------------------------------


class TestPassInteraction:
    @pytest.mark.asyncio
    async def test_distinct_dm_threads_have_isolated_streamers(self):
        adapter = _make_adapter()
        tid_a = adapter.encode_thread_id(
            TeamsThreadId(conversation_id="a:userA", service_url="https://smba.trafficmanager.net/teams/")
        )
        tid_b = adapter.encode_thread_id(
            TeamsThreadId(conversation_id="a:userB", service_url="https://smba.trafficmanager.net/teams/")
        )
        streamer_a = FakeStreamer(chunk_id="id-a")
        streamer_b = FakeStreamer(chunk_id="id-b")
        await _register_streamer(adapter, tid_a, streamer_a)
        await _register_streamer(adapter, tid_b, streamer_b)

        async def gen_a():
            yield "from-a"

        async def gen_b():
            yield "from-b"

        res_a, res_b = await asyncio.gather(adapter.stream(tid_a, gen_a()), adapter.stream(tid_b, gen_b()))

        assert streamer_a.emitted == ["from-a"]
        assert streamer_b.emitted == ["from-b"]
        assert res_a.id == "id-a"
        assert res_b.id == "id-b"
