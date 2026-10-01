"""Python-specific tests for turn cancellation and typing lifecycle (#201).

Upstream ports live in ``test_chat_faithful.py`` ("aborts an active thread
signal from another Chat instance"), ``test_thread_faithful.py`` ("passes the
initiating user and clears processing after posting") and
``test_agent_session.py``. These cover the Python-only machinery: the
``TurnSignal`` class, ``_take_until_aborted`` cancellation semantics, the
abort monitor's lifecycle and the ``start_typing`` signature probe.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import pytest

import chat_sdk.chat as chat_module
from chat_sdk._compat import accepts_kwarg
from chat_sdk.chat import Chat
from chat_sdk.plan import StreamingPlan, StreamingPlanOptions
from chat_sdk.testing import MockLogger, create_mock_adapter, create_mock_state, create_test_message
from chat_sdk.thread import ThreadImpl, _take_until_aborted, _ThreadImplConfig
from chat_sdk.types import ChatConfig, PostableMarkdown, RawMessage, TurnSignal

THREAD_ID = "slack:C123:1234.5678"


def _thread(adapter: Any, *, signal: TurnSignal | None = None, current_message: Any = None) -> ThreadImpl:
    return ThreadImpl(
        _ThreadImplConfig(
            id=THREAD_ID,
            adapter=adapter,
            channel_id="C123",
            state_adapter=create_mock_state(),
            current_message=current_message,
            signal=signal,
            streaming_update_interval_ms=60_000,  # no intermediate edits
        )
    )


def _chat(adapter: Any, state: Any = None, logger: Any = None) -> Chat:
    return Chat(
        ChatConfig(
            user_name="testbot",
            adapters={"slack": adapter},
            state=state if state is not None else create_mock_state(),
            logger=logger if logger is not None else MockLogger(),
        )
    )


class _StallingSource:
    """Yields ``chunks`` then blocks until closed; records closing."""

    def __init__(self, *chunks: str) -> None:
        self.chunks = chunks
        self.yielded = asyncio.Event()
        self.closed = False

    async def gen(self) -> AsyncIterator[str]:
        try:
            for chunk in self.chunks:
                yield chunk
            self.yielded.set()
            await asyncio.Event().wait()  # stall like a model mid tool-call
        finally:
            self.closed = True


# ---------------------------------------------------------------------------
# TurnSignal
# ---------------------------------------------------------------------------


class TestTurnSignal:
    async def test_wait_returns_on_abort_and_listeners_fire_once(self):
        signal = TurnSignal()
        calls: list[str] = []
        signal.add_listener(lambda: calls.append("a"))
        signal.add_listener(lambda: 1 / 0)  # a failing listener does not stop the rest
        signal.add_listener(lambda: calls.append("b"))
        waiter = asyncio.ensure_future(signal.wait())
        await asyncio.sleep(0)
        assert not waiter.done()

        signal._abort()
        signal._abort()  # idempotent
        await asyncio.wait_for(waiter, 1)

        assert signal.aborted is True
        assert calls == ["a", "b"]
        late: list[str] = []
        signal.add_listener(lambda: late.append("late"))  # already aborted: runs now
        assert late == ["late"]

    def test_signal_created_without_a_loop_works_in_later_loops(self):
        signal = TurnSignal()  # no running loop here

        async def wait_then_abort() -> bool:
            task = asyncio.ensure_future(signal.wait())
            await asyncio.sleep(0)
            signal._abort()
            await task
            return signal.aborted

        assert asyncio.run(wait_then_abort()) is True
        assert asyncio.run(signal.wait()) is None  # aborted: returns at once in a new loop

    async def test_removed_listener_is_not_called(self):
        signal = TurnSignal()
        listener = AsyncMock()
        signal.add_listener(listener)
        signal.remove_listener(listener)
        signal.remove_listener(listener)  # absent: no error
        signal._abort()
        listener.assert_not_called()


# ---------------------------------------------------------------------------
# _take_until_aborted
# ---------------------------------------------------------------------------


class TestTakeUntilAborted:
    async def test_abort_while_waiting_ends_the_stream_and_closes_the_source(self):
        signal = TurnSignal()
        source = _StallingSource("a", "b")
        received: list[str] = []

        async def consume() -> None:
            async for chunk in _take_until_aborted(source.gen(), signal):
                received.append(chunk)

        consumer = asyncio.ensure_future(consume())
        await source.yielded.wait()
        signal._abort()
        await asyncio.wait_for(consumer, 1)

        assert received == ["a", "b"]
        assert source.closed is True

    async def test_cancelling_the_consumer_still_propagates(self):
        signal = TurnSignal()
        source = _StallingSource("a")

        async def consume() -> None:
            async for _ in _take_until_aborted(source.gen(), signal):
                pass

        consumer = asyncio.ensure_future(consume())
        await source.yielded.wait()
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer
        assert source.closed is True
        assert signal.aborted is False

    async def test_a_timeout_raised_by_the_source_is_not_mistaken_for_an_abort(self):
        async def source() -> AsyncIterator[str]:
            yield "a"
            raise TimeoutError("model timed out")

        received: list[str] = []
        with pytest.raises(TimeoutError, match="model timed out"):
            async for chunk in _take_until_aborted(source(), TurnSignal()):
                received.append(chunk)
        assert received == ["a"]

    async def test_an_already_aborted_signal_yields_nothing(self):
        signal = TurnSignal()
        signal._abort()
        source = _StallingSource("a")
        received = [chunk async for chunk in _take_until_aborted(source.gen(), signal)]
        assert received == []


# ---------------------------------------------------------------------------
# Thread: streams under an aborted signal, typing lifecycle
# ---------------------------------------------------------------------------


class TestThreadAbortAndTyping:
    async def test_abort_mid_stream_finalizes_the_fallback_message(self):
        adapter = create_mock_adapter()
        signal = TurnSignal()
        thread = _thread(adapter, signal=signal)
        source = _StallingSource("Hello", " world")

        posting = asyncio.ensure_future(thread.post(source.gen()))
        await source.yielded.wait()
        signal._abort()
        sent = await asyncio.wait_for(posting, 1)

        assert source.closed is True
        assert adapter._post_calls[0] == (THREAD_ID, "...")
        assert adapter._edit_calls[-1] == (THREAD_ID, "msg-1", PostableMarkdown(markdown="Hello world"))
        assert sent.text == "Hello world"

    async def test_abort_between_chunks_closes_the_original_source(self):
        adapter = create_mock_adapter()
        signal = TurnSignal()
        source = _StallingSource("a", "b", "c")
        received: list[Any] = []

        async def native_stream(thread_id: str, stream: Any, options: Any = None) -> RawMessage:
            async for chunk in stream:
                received.append(chunk)
                signal._abort()  # the user stops while the adapter handles "a"
            return RawMessage(id="s1", thread_id=thread_id, raw={})

        adapter.stream = native_stream  # type: ignore[attr-defined]
        await _thread(adapter, signal=signal).post(source.gen())

        assert received == ["a"]
        assert source.closed is True  # closed through from_full_stream, not left to GC

    async def test_abort_closes_a_taskgroup_backed_source_cleanly(self):
        adapter = create_mock_adapter()
        signal = TurnSignal()
        closed: list[bool] = []

        async def source() -> AsyncIterator[str]:
            async with asyncio.TaskGroup():  # held across ``yield``
                try:
                    yield "a"
                    yield "b"
                finally:
                    closed.append(True)

        async def native_stream(thread_id: str, stream: Any, options: Any = None) -> RawMessage:
            async for _ in stream:
                signal._abort()
            return RawMessage(id="s1", thread_id=thread_id, raw={})

        adapter.stream = native_stream  # type: ignore[attr-defined]
        sent = await _thread(adapter, signal=signal).post(source())

        assert sent.text == "a"
        assert closed == [True]

    async def test_native_stream_receives_the_thread_signal(self):
        adapter = create_mock_adapter()
        signal = TurnSignal()
        seen: list[Any] = []

        async def native_stream(thread_id: str, stream: Any, options: Any = None) -> RawMessage:
            seen.append(options.signal)
            async for _ in stream:
                pass
            return RawMessage(id="s1", thread_id=thread_id, raw={})

        adapter.stream = native_stream  # type: ignore[attr-defined]

        async def chunks() -> AsyncIterator[str]:
            yield "hi"

        await _thread(adapter, signal=signal).post(chunks())
        assert seen == [signal]

    async def test_successful_native_stream_does_not_end_typing(self):
        adapter = create_mock_adapter()
        adapter.end_typing = AsyncMock()  # type: ignore[attr-defined]
        adapter.stream = AsyncMock(return_value=RawMessage(id="s1", thread_id=THREAD_ID, raw={}))  # type: ignore[attr-defined]
        thread = _thread(adapter, current_message=create_test_message("m1", "hi"))

        async def chunks() -> AsyncIterator[str]:
            yield "hi"

        await thread.start_typing()
        await thread.post(chunks())
        await thread.post("next")  # the indicator was already cleared by the stream

        adapter.end_typing.assert_not_awaited()

    async def test_failing_native_stream_ends_typing(self):
        adapter = create_mock_adapter()
        adapter.end_typing = AsyncMock()  # type: ignore[attr-defined]
        adapter.stream = AsyncMock(side_effect=RuntimeError("stream failed"))  # type: ignore[attr-defined]
        thread = _thread(adapter)

        async def chunks() -> AsyncIterator[str]:
            yield "hi"

        await thread.start_typing()
        with pytest.raises(RuntimeError, match="stream failed"):
            await thread.post(chunks())

        adapter.end_typing.assert_awaited_once_with(THREAD_ID, "active")

    async def test_fallback_stream_ends_typing_with_the_plan_session_status(self):
        adapter = create_mock_adapter()
        adapter.end_typing = AsyncMock()  # type: ignore[attr-defined]
        thread = _thread(adapter)

        async def chunks() -> AsyncIterator[str]:
            yield "Need approval"

        await thread.start_typing()
        await thread.post(StreamingPlan(chunks(), StreamingPlanOptions(session_status="suspended")))

        adapter.end_typing.assert_awaited_once_with(THREAD_ID, "suspended")

    async def test_end_typing_runs_once_per_start_typing(self):
        adapter = create_mock_adapter()
        adapter.end_typing = AsyncMock()  # type: ignore[attr-defined]
        thread = _thread(adapter)

        await thread.post("before typing")  # no start_typing yet: nothing to end
        await thread.start_typing()
        await thread.post("one")
        await thread.post("two")

        adapter.end_typing.assert_awaited_once_with(THREAD_ID, "active")

    async def test_two_argument_custom_start_typing_still_works(self):
        adapter = create_mock_adapter()
        calls: list[tuple[str, str | None]] = []

        async def legacy_start_typing(thread_id: str, status: str | None = None) -> None:
            calls.append((thread_id, status))

        adapter.start_typing = legacy_start_typing  # type: ignore[method-assign]
        thread = _thread(adapter, current_message=create_test_message("m1", "hi"))

        await thread.start_typing("thinking")

        assert calls == [(THREAD_ID, "thinking")]

    async def test_no_options_without_an_initiating_user(self):
        adapter = create_mock_adapter()
        message = create_test_message("m1", "hi")
        message.author.user_id = ""  # upstream truthiness: empty id sends no options
        await _thread(adapter, current_message=message).start_typing()
        await _thread(adapter).start_typing()

        assert adapter._start_typing_options == [None, None]

    async def test_rebinding_a_thread_resets_turn_state(self):
        old_adapter = create_mock_adapter("slack")
        old_signal = TurnSignal()
        thread = _thread(old_adapter, signal=old_signal)
        await thread.start_typing()
        old_signal._abort()
        new_adapter = create_mock_adapter("slack")
        new_adapter.end_typing = AsyncMock()  # type: ignore[attr-defined]
        new_chat = _chat(new_adapter)

        rebound = ThreadImpl.from_json(thread, chat=new_chat)

        assert rebound is thread
        assert rebound.signal is not old_signal
        assert rebound.signal.aborted is False
        await rebound.post("after rebind")  # the old turn's indicator is not ended here
        new_adapter.end_typing.assert_not_awaited()


class TestAcceptsKwarg:
    def test_probe_results(self):
        async def kw_only(thread_id: str, status: str | None = None, *, options: Any = None) -> None: ...
        async def var_kw(thread_id: str, **kwargs: Any) -> None: ...
        async def positional_only(thread_id: str, options: Any = None, /) -> None: ...
        async def legacy(thread_id: str, status: str | None = None) -> None: ...
        async def var_positional(thread_id: str, status: str | None = None, *options: Any) -> None: ...

        assert accepts_kwarg(kw_only, "options") is True
        assert accepts_kwarg(var_kw, "options") is True
        assert accepts_kwarg(positional_only, "options") is False
        assert accepts_kwarg(legacy, "options") is False
        assert accepts_kwarg(var_positional, "options") is False  # ``*options`` takes no keyword
        assert accepts_kwarg(create_mock_adapter().start_typing, "options") is True
        assert accepts_kwarg(len, "options") is False  # builtin without kwargs


# ---------------------------------------------------------------------------
# Chat: turn registry, markers, monitor
# ---------------------------------------------------------------------------


def _monitor_tasks() -> list[asyncio.Task[Any]]:
    return [
        t
        for t in asyncio.all_tasks()
        if not t.done() and getattr(t.get_coro(), "__qualname__", "").endswith("_monitor_turn_abort")
    ]


class TestChatTurnCancellation:
    async def test_local_abort_without_opt_in_writes_no_state(self):
        adapter = create_mock_adapter("slack")
        state = create_mock_state()
        chat = _chat(adapter, state)
        started = asyncio.Event()
        signals: list[TurnSignal] = []

        @chat.on_mention
        async def handler(thread: Any, message: Any, context: Any = None) -> None:
            signals.append(thread.signal)
            started.set()
            await thread.signal.wait()

        message = create_test_message("m1", "@testbot go")
        message.is_mention = True
        task = chat.process_message(adapter, "slack:C1:1.1", message)
        await started.wait()
        assert _monitor_tasks() == []
        await chat.abort_turn("slack:C1:1.1")
        await task

        assert signals[0].aborted is True
        assert not [k for k in state.cache if k.startswith(("active-turn:", "abort-turn:"))]
        assert chat._active_turn_signals == {}

    async def test_opted_in_turn_publishes_then_clears_its_markers_and_monitor(self, monkeypatch):
        monkeypatch.setattr(chat_module, "ABORT_POLL_INTERVAL_MS", 1)
        adapter = create_mock_adapter("slack")
        adapter.supports_turn_cancellation = True  # type: ignore[attr-defined]
        state = create_mock_state()
        chat = _chat(adapter, state)
        published: list[Any] = []
        monitors_during_turn: list[int] = []

        @chat.on_mention
        async def handler(thread: Any, message: Any, context: Any = None) -> None:
            published.append(state.cache.get("active-turn:slack:C1:2.2"))
            monitors_during_turn.append(len(_monitor_tasks()))

        message = create_test_message("m2", "@testbot go")
        message.is_mention = True
        await chat.process_message(adapter, "slack:C1:2.2", message)

        assert isinstance(published[0], str) and published[0]
        assert monitors_during_turn == [1]
        assert "active-turn:slack:C1:2.2" not in state.cache
        assert _monitor_tasks() == []
        assert chat._active_turn_signals == {}

    async def test_markers_owned_by_a_newer_turn_are_not_cleared(self):
        adapter = create_mock_adapter("slack")
        adapter.supports_turn_cancellation = True  # type: ignore[attr-defined]
        state = create_mock_state()
        chat = _chat(adapter, state)

        @chat.on_mention
        async def handler(thread: Any, message: Any, context: Any = None) -> None:
            # Another process started a newer turn and someone aborted it.
            await state.set("active-turn:slack:C1:3.3", "newer-turn")
            await state.set("abort-turn:slack:C1:3.3", "newer-turn")

        message = create_test_message("m3", "@testbot go")
        message.is_mention = True
        await chat.process_message(adapter, "slack:C1:3.3", message)

        assert state.cache["active-turn:slack:C1:3.3"] == "newer-turn"
        assert state.cache["abort-turn:slack:C1:3.3"] == "newer-turn"

    async def test_poll_error_logs_and_stops_polling_without_failing_the_turn(self):
        adapter = create_mock_adapter("slack")
        adapter.supports_turn_cancellation = True  # type: ignore[attr-defined]
        state = create_mock_state()
        logger = MockLogger()
        chat = _chat(adapter, state, logger)
        original_get = state.get
        handled = asyncio.Event()

        async def flaky_get(key: str) -> Any:
            if key.startswith("abort-turn:") and not handled.is_set():
                raise ConnectionError("redis down")
            return await original_get(key)

        state.get = flaky_get  # type: ignore[method-assign]

        @chat.on_mention
        async def handler(thread: Any, message: Any, context: Any = None) -> None:
            await asyncio.sleep(0)  # let the monitor's first poll run
            await asyncio.sleep(0)
            handled.set()

        message = create_test_message("m4", "@testbot go")
        message.is_mention = True
        await chat.process_message(adapter, "slack:C1:4.4", message)

        warnings = [c for c in logger.warn.calls if c[0] == "Could not poll turn cancellation state"]
        assert len(warnings) == 1
        assert isinstance(warnings[0][1]["error"], ConnectionError)
        assert "active-turn:slack:C1:4.4" not in state.cache

    async def test_abort_turn_without_active_turn_writes_nothing(self):
        state = create_mock_state()
        chat = _chat(create_mock_adapter("slack"), state)
        await chat.abort_turn("slack:C1:none")
        assert "abort-turn:slack:C1:none" not in state.cache

    async def test_cancellation_while_joining_the_monitor_still_cleans_up(self):
        adapter = create_mock_adapter("slack")
        adapter.supports_turn_cancellation = True  # type: ignore[attr-defined]
        state = create_mock_state()
        chat = _chat(adapter, state)
        original_get = state.get
        release = asyncio.Event()
        monitor_polling = asyncio.Event()
        stubborn_calls = 0

        async def stubborn_get(key: str) -> Any:
            nonlocal stubborn_calls
            if key.startswith("abort-turn:") and stubborn_calls == 0:
                stubborn_calls += 1
                monitor_polling.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    await release.wait()  # ignore the monitor's cancellation for now
                return None
            return await original_get(key)

        state.get = stubborn_get  # type: ignore[method-assign]
        handler_done = asyncio.Event()

        @chat.on_mention
        async def handler(thread: Any, message: Any, context: Any = None) -> None:
            await monitor_polling.wait()
            handler_done.set()

        message = create_test_message("m6", "@testbot go")
        message.is_mention = True
        task = chat.process_message(adapter, "slack:C1:6.6", message)
        await handler_done.wait()
        for _ in range(5):  # let dispatch reach the monitor join
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert chat._active_turn_signals == {}
        assert "active-turn:slack:C1:6.6" not in state.cache
        release.set()
        await asyncio.sleep(0)

    async def test_magicmock_adapter_flag_does_not_opt_in(self):
        from unittest.mock import MagicMock

        adapter = create_mock_adapter("slack")
        adapter.supports_turn_cancellation = MagicMock()  # truthy, but not True
        state = create_mock_state()
        chat = _chat(adapter, state)
        seen: list[Any] = []

        @chat.on_mention
        async def handler(thread: Any, message: Any, context: Any = None) -> None:
            seen.append(state.cache.get("active-turn:slack:C1:5.5"))

        message = create_test_message("m5", "@testbot go")
        message.is_mention = True
        await chat.process_message(adapter, "slack:C1:5.5", message)
        assert seen == [None]
