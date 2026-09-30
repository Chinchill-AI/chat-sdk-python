"""Python-specific coverage for the held-lock heartbeat and drain isolation (#190).

The faithful upstream ports live in ``tests/test_chat_faithful.py``
(``TestConcurrencyLockLifetime`` and friends). These tests cover what the
upstream suite cannot see: the asyncio translation of ``stop()`` (waits for
an in-flight extend before ``release_lock``, does not cancel it, does not
yield when idle), the extend-error ownership rule, the ``drop`` strategy's
heartbeat, the client-side ``held_until`` seed (the one behavioral
divergence), config validation, crash logging, and JSON-roundtripped queue
entries in the channel-scoped isolation path.

Everything runs on ``tests._fake_clock.FakeClock`` -- no real sleeps.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from chat_sdk import chat as chat_module
from chat_sdk.chat import DEFAULT_LOCK_TTL_MS, DEFAULT_MAX_LOCK_LIFETIME_MS, Chat
from chat_sdk.errors import LockError
from chat_sdk.state import create_memory_state
from chat_sdk.testing import (
    MockAdapter,
    MockLogger,
    MockStateAdapter,
    create_mock_adapter,
    create_mock_state,
    create_test_message,
)
from chat_sdk.types import ChatConfig, ConcurrencyConfig, Lock, Message, QueueEntry
from tests._fake_clock import FakeClock, install_token_lock_mock

THREAD = "slack:C123:1234.5678"


async def _make_chat(
    state: MockStateAdapter,
    adapter: MockAdapter | None = None,
    **overrides: Any,
) -> tuple[Chat, MockAdapter, MockLogger]:
    adapter = adapter or create_mock_adapter("slack")
    logger = MockLogger()
    chat = Chat(
        ChatConfig(
            user_name="testbot",
            adapters={adapter.name: adapter},
            state=state,
            logger=logger,
            **overrides,
        )
    )
    await chat.webhooks[adapter.name]("request")
    return chat, adapter, logger


def _record_heartbeats(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Capture every ``_LockHeartbeat`` the chat creates."""
    created: list[Any] = []
    real_cls = chat_module._LockHeartbeat

    class _Recording(real_cls):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            created.append(self)

    monkeypatch.setattr(chat_module, "_LockHeartbeat", _Recording)
    return created


def _warn_messages(logger: MockLogger) -> list[str]:
    return [call[0] for call in logger.warn.calls]


class TestDropStrategyHeartbeat:
    async def test_second_message_past_the_ttl_raises_lock_error_instead_of_running_concurrently(self, monkeypatch):
        clock = FakeClock().install(monkeypatch)
        state = create_mock_state()
        install_token_lock_mock(state, clock)
        heartbeats = _record_heartbeats(monkeypatch)
        chat, adapter, _ = await _make_chat(state)

        release = asyncio.Event()
        handled: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            handled.append(message.id)
            if message.id == "drop-long-1":
                await release.wait()

        first = asyncio.create_task(
            chat.handle_incoming_message(adapter, THREAD, create_test_message("drop-long-1", "Hey @slack-bot one"))
        )
        await clock.wait_for(lambda: handled == ["drop-long-1"])

        await clock.advance(DEFAULT_LOCK_TTL_MS + 1)
        with pytest.raises(LockError):
            await chat.handle_incoming_message(
                adapter, THREAD, create_test_message("drop-long-2", "Hey @slack-bot two")
            )

        release.set()
        await first

        assert handled == ["drop-long-1"]
        assert len(heartbeats) == 1
        await clock.settle()
        assert heartbeats[0].task.cancelled()
        assert clock.pending_timers() == 0

    async def test_force_acquired_lock_is_renewed_by_the_heartbeat(self, monkeypatch):
        clock = FakeClock().install(monkeypatch)
        state = create_mock_state()
        install_token_lock_mock(state, clock)
        extended_tokens: list[str] = []
        real_extend = state.extend_lock

        async def recording_extend(lock: Lock, ttl_ms: int) -> bool:
            extended_tokens.append(lock.token)
            return await real_extend(lock, ttl_ms)

        state.extend_lock = recording_extend  # type: ignore[method-assign]
        chat, adapter, _ = await _make_chat(state, on_lock_conflict="force")

        stale = await state.acquire_lock(THREAD, DEFAULT_LOCK_TTL_MS)
        assert stale is not None
        release = asyncio.Event()

        @chat.on_mention
        async def handler(thread, message, context=None):
            await release.wait()

        task = asyncio.create_task(
            chat.handle_incoming_message(adapter, THREAD, create_test_message("force-1", "Hey @slack-bot"))
        )
        await clock.advance(DEFAULT_LOCK_TTL_MS // 3 + 1)
        release.set()
        await task

        # The forced lock (not the stale holder's) is the one kept alive.
        assert stale.token == "test-token-1"
        assert extended_tokens == ["test-token-2"]


class TestHeartbeatStop:
    """asyncio translation of ``stopped = true; clearInterval(); await inFlight``."""

    async def _hold_with_gated_extend(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[FakeClock, MockStateAdapter, list[str], asyncio.Event, asyncio.Task[None], list[Any]]:
        """Handler returns while a heartbeat extend is blocked on ``gate``."""
        clock = FakeClock().install(monkeypatch)
        state = create_mock_state()
        order: list[str] = []
        gate = asyncio.Event()

        async def gated_extend(lock: Lock, ttl_ms: int) -> bool:
            order.append("extend-start")
            try:
                await gate.wait()
            except asyncio.CancelledError:
                order.append("extend-cancelled")
                raise
            order.append("extend-end")
            return True

        real_release = state.release_lock

        async def recording_release(lock: Lock) -> None:
            order.append("release")
            await real_release(lock)

        state.extend_lock = AsyncMock(side_effect=gated_extend)  # type: ignore[method-assign]
        state.release_lock = AsyncMock(side_effect=recording_release)  # type: ignore[method-assign]
        heartbeats = _record_heartbeats(monkeypatch)
        chat, adapter, _ = await _make_chat(state)
        finish_handler = asyncio.Event()

        @chat.on_mention
        async def handler(thread, message, context=None):
            await finish_handler.wait()

        task = asyncio.create_task(
            chat.handle_incoming_message(adapter, THREAD, create_test_message("stop-1", "Hey @slack-bot"))
        )
        await clock.advance(DEFAULT_LOCK_TTL_MS // 3)
        assert order == ["extend-start"]
        finish_handler.set()
        await clock.settle()
        assert not task.done()
        state.release_lock.assert_not_awaited()
        return clock, state, order, gate, task, heartbeats

    async def test_stop_waits_for_an_in_flight_extend_before_release(self, monkeypatch):
        clock, state, order, gate, task, heartbeats = await self._hold_with_gated_extend(monkeypatch)

        gate.set()
        await task

        assert order == ["extend-start", "extend-end", "release"]
        state.release_lock.assert_awaited_once()
        await clock.settle()
        assert heartbeats[0].task.cancelled()

    async def test_cancelling_the_caller_does_not_cancel_the_in_flight_extend(self, monkeypatch):
        # asyncio cancellation interrupts the awaited I/O (a JS promise cannot
        # be cancelled); stop() must not abort extend_lock mid-command.
        clock, state, order, gate, task, _ = await self._hold_with_gated_extend(monkeypatch)

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled()
        assert order == ["extend-start", "release"]

        gate.set()
        await clock.settle()
        assert order == ["extend-start", "release", "extend-end"]

    async def test_idle_stop_does_not_yield_between_the_last_empty_check_and_release(self):
        # The second message finds the lock held and enqueues; the holder's
        # drain must still see it. Awaiting the cancelled loop task in stop()
        # would open an event-loop turn (upstream's clearInterval opens none)
        # in which the message enqueues after the drain's last empty check.
        state = create_memory_state()
        chat, adapter, _ = await _make_chat(state, concurrency="queue")  # type: ignore[arg-type]
        handled: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            handled.append(message.id)

        await asyncio.gather(
            chat.handle_incoming_message(adapter, THREAD, create_test_message("b2b-1", "Hey @slack-bot one")),
            chat.handle_incoming_message(adapter, THREAD, create_test_message("b2b-2", "Hey @slack-bot two")),
        )

        assert handled == ["b2b-1", "b2b-2"]
        assert await state.queue_depth(THREAD) == 0

    async def test_handler_error_still_stops_the_heartbeat_and_releases_once(self, monkeypatch):
        clock = FakeClock().install(monkeypatch)
        state = create_mock_state()
        real_release = state.release_lock
        state.release_lock = AsyncMock(side_effect=real_release)  # type: ignore[method-assign]
        heartbeats = _record_heartbeats(monkeypatch)
        chat, adapter, _ = await _make_chat(state, concurrency="queue")

        @chat.on_mention
        async def handler(thread, message, context=None):
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            await chat.handle_incoming_message(adapter, THREAD, create_test_message("err-1", "Hey @slack-bot"))

        state.release_lock.assert_awaited_once()
        await clock.settle()
        assert heartbeats[0].task.cancelled()
        assert clock.pending_timers() == 0


class TestExtendErrors:
    async def _run_queue_scenario(
        self,
        monkeypatch: pytest.MonkeyPatch,
        advance_ms: int,
    ) -> tuple[list[str], MockStateAdapter, MockLogger, AsyncMock]:
        clock = FakeClock().install(monkeypatch)
        state = create_mock_state()
        install_token_lock_mock(state, clock)
        state.extend_lock = AsyncMock(side_effect=ConnectionError("backend down"))  # type: ignore[method-assign]
        real_release = state.release_lock
        state.release_lock = AsyncMock(side_effect=real_release)  # type: ignore[method-assign]
        chat, adapter, logger = await _make_chat(state, concurrency="queue")

        release = asyncio.Event()
        handled: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            handled.append(message.id)
            if message.id == "err-q-1":
                await release.wait()

        first = asyncio.create_task(
            chat.handle_incoming_message(adapter, THREAD, create_test_message("err-q-1", "Hey @slack-bot one"))
        )
        await clock.settle()
        await chat.handle_incoming_message(adapter, THREAD, create_test_message("err-q-2", "Hey @slack-bot two"))

        await clock.advance(advance_ms)
        release.set()
        await first
        return handled, state, logger, state.release_lock

    async def test_extend_error_before_the_known_expiry_keeps_ownership(self, monkeypatch):
        # One failed extend at 10s; the lock is still known-held until 30s.
        handled, state, logger, release_lock = await self._run_queue_scenario(monkeypatch, DEFAULT_LOCK_TTL_MS // 3 + 1)

        assert handled == ["err-q-1", "err-q-2"]
        assert "Lock heartbeat failed" in _warn_messages(logger)
        assert "Stopping queue drain after lock ownership was lost" not in _warn_messages(logger)
        release_lock.assert_awaited_once()

    async def test_extend_error_past_the_known_expiry_stops_the_drain_but_still_releases(self, monkeypatch):
        # Failed extends at 10s and 20s keep ownership; the one at 30s finds
        # the lock past its last known expiry and marks it lost.
        handled, state, logger, release_lock = await self._run_queue_scenario(monkeypatch, DEFAULT_LOCK_TTL_MS + 1)

        assert handled == ["err-q-1"]
        warns = _warn_messages(logger)
        assert "Lock lapsed while the heartbeat could not reach the state backend" in warns
        assert "Stopping queue drain after lock ownership was lost" in warns
        # The queued message is left for the next lock holder.
        assert await state.queue_depth(THREAD) == 1
        release_lock.assert_awaited_once()

    async def test_false_extend_marks_ownership_lost_and_stops_renewing(self, monkeypatch):
        clock = FakeClock().install(monkeypatch)
        state = create_mock_state()
        state.extend_lock = AsyncMock(return_value=False)  # type: ignore[method-assign]
        heartbeats = _record_heartbeats(monkeypatch)
        chat, adapter, logger = await _make_chat(state)

        release = asyncio.Event()

        @chat.on_mention
        async def handler(thread, message, context=None):
            await release.wait()

        task = asyncio.create_task(
            chat.handle_incoming_message(adapter, THREAD, create_test_message("false-1", "Hey @slack-bot"))
        )
        await clock.advance(DEFAULT_LOCK_TTL_MS)

        assert heartbeats[0].is_ownership_lost()
        assert heartbeats[0].task.done()
        # One extend only: renewal stops after the first ``False``.
        assert state.extend_lock.await_count == 1
        assert "Lock heartbeat stopped after ownership was lost" in _warn_messages(logger)

        release.set()
        await task


class TestHeldUntilSeed:
    """Divergence from upstream (``heldUntil = lock.expiresAt``) -- see docs/UPSTREAM_SYNC.md."""

    async def test_lagging_database_clock_does_not_make_a_fresh_lock_look_lapsed(self, monkeypatch):
        # The Python Postgres backend stamps expires_at with the database's
        # now(); here the database runs 31s behind the app. Seeded from
        # expires_at, the lock would look lapsed at once and the debounce
        # loop would strand its own message.
        clock = FakeClock().install(monkeypatch)
        state = create_mock_state()
        install_token_lock_mock(state, clock)
        real_acquire = state.acquire_lock

        async def lagging_acquire(thread_id: str, ttl_ms: int) -> Lock | None:
            lock = await real_acquire(thread_id, ttl_ms)
            if lock is None:
                return None
            return Lock(thread_id=lock.thread_id, token=lock.token, expires_at=lock.expires_at - 31_000)

        state.acquire_lock = lagging_acquire  # type: ignore[method-assign]
        chat, adapter, logger = await _make_chat(
            state, concurrency=ConcurrencyConfig(strategy="debounce", debounce_ms=1500)
        )
        handled: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            handled.append(message.id)

        task = asyncio.create_task(
            chat.handle_incoming_message(adapter, THREAD, create_test_message("skew-1", "Hey @slack-bot"))
        )
        await clock.advance(1500)
        await clock.advance(1500)
        await task

        assert handled == ["skew-1"]
        assert "Stopping debounce loop after lock ownership was lost" not in _warn_messages(logger)


class TestDebounceLoop:
    async def test_skipped_resets_between_dispatches_in_one_debounce_loop(self, monkeypatch):
        # A message arriving while the handler runs is debounced and
        # dispatched by the same loop, with the previous batch's skipped
        # context cleared.
        clock = FakeClock().install(monkeypatch)
        state = create_mock_state()
        install_token_lock_mock(state, clock)
        chat, adapter, _ = await _make_chat(state, concurrency=ConcurrencyConfig(strategy="debounce", debounce_ms=1500))
        release = asyncio.Event()
        received: list[tuple[str, list[str], int]] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            received.append((message.id, [m.id for m in context.skipped], context.total_since_last_handler))
            if message.id == "reset-2":
                await release.wait()

        holder = asyncio.create_task(
            chat.handle_incoming_message(adapter, THREAD, create_test_message("reset-1", "Hey @slack-bot one"))
        )
        await clock.advance(500)
        await chat.handle_incoming_message(adapter, THREAD, create_test_message("reset-2", "Hey @slack-bot two"))
        await clock.wait_for(lambda: len(received) == 1)
        await chat.handle_incoming_message(adapter, THREAD, create_test_message("reset-3", "Hey @slack-bot three"))
        release.set()
        await clock.advance(1500)
        await clock.advance(1500)
        await holder

        assert received == [("reset-2", ["reset-1"], 2), ("reset-3", [], 1)]


def _serialized(message: Message) -> dict[str, Any]:
    """``Message.to_json()`` shape (``_type`` tag, camelCase), as Redis/Postgres store it."""
    return json.loads(json.dumps(message.to_json()))


def _plain_snake(message: Message) -> dict[str, Any]:
    """Untagged snake_case dict (a custom state backend) -- the rehydration dict fallback."""
    return {
        "id": message.id,
        "thread_id": message.thread_id,
        "text": message.text,
        "author": {"user_id": message.author.user_id, "user_name": message.author.user_name},
    }


def _plain_camel(message: Message) -> dict[str, Any]:
    """Untagged camelCase dict (TS-interop without ``_type``) -- the dict fallback."""
    return {"id": message.id, "threadId": message.thread_id, "text": message.text, "author": {}}


class TestChannelScopedIsolationAfterJsonRoundtrip:
    @pytest.mark.parametrize("to_dict", [_serialized, _plain_snake, _plain_camel])
    async def test_dict_queue_entries_dispatch_under_their_own_thread(self, to_dict):
        state = create_mock_state()
        real_enqueue = state.enqueue

        async def json_enqueue(thread_id: str, entry: QueueEntry, max_size: int) -> int:
            plain = to_dict(entry.message)
            return await real_enqueue(
                thread_id,
                QueueEntry(message=plain, enqueued_at=entry.enqueued_at, expires_at=entry.expires_at),  # type: ignore[arg-type]
                max_size,
            )

        state.enqueue = json_enqueue  # type: ignore[method-assign]
        adapter = create_mock_adapter("telegram")
        adapter.lock_scope = "channel"
        chat, _, _ = await _make_chat(state, adapter, concurrency="queue")

        topic1 = "telegram:C123:topic1"
        topic2 = "telegram:C123:topic2"
        dispatched: list[tuple[str, str, list[str]]] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            skipped = [m.id for m in context.skipped] if context is not None else []
            dispatched.append((thread.id, message.id, skipped))

        await state.acquire_lock("telegram:C123", DEFAULT_LOCK_TTL_MS)
        for msg_id, thread_id in (("iso-1", topic2), ("iso-2", topic1), ("iso-3", topic2)):
            await chat.handle_incoming_message(
                adapter, thread_id, create_test_message(msg_id, "Hey @telegram-bot", thread_id=thread_id)
            )
        await state.force_release_lock("telegram:C123")
        await chat.handle_incoming_message(
            adapter, topic1, create_test_message("iso-4", "Hey @telegram-bot", thread_id=topic1)
        )

        # iso-4 runs directly; the drain dispatches iso-3 in ITS thread with
        # only same-thread iso-1 skipped -- topic1's iso-2 never leaks in.
        assert dispatched == [(topic1, "iso-4", []), (topic2, "iso-3", ["iso-1"])]


def _chat_with(concurrency: Any) -> Chat:
    return Chat(
        ChatConfig(
            user_name="testbot",
            adapters={"slack": create_mock_adapter("slack")},
            state=create_mock_state(),
            logger=MockLogger(),
            concurrency=concurrency,
        )
    )


class TestLockLifetimeConfig:
    @pytest.mark.parametrize(
        ("concurrency", "expected"),
        [
            (None, DEFAULT_MAX_LOCK_LIFETIME_MS),
            ("queue", DEFAULT_MAX_LOCK_LIFETIME_MS),
            (ConcurrencyConfig(strategy="queue"), DEFAULT_MAX_LOCK_LIFETIME_MS),
            (ConcurrencyConfig(strategy="queue", max_lock_lifetime_ms=None), DEFAULT_MAX_LOCK_LIFETIME_MS),  # type: ignore[arg-type]
            (ConcurrencyConfig(strategy="burst", max_lock_lifetime_ms=5_000), 5_000),
        ],
    )
    def test_max_lock_lifetime_resolves_in_every_config_branch(self, concurrency, expected):
        assert _chat_with(concurrency)._concurrency_max_lock_lifetime_ms == expected

    @pytest.mark.parametrize("bad", ["600000", 1.5, True, -1])
    def test_invalid_max_lock_lifetime_ms_is_rejected_at_init(self, bad):
        # JS coerces "600000" in the heartbeat's comparison; Python would
        # raise TypeError on the first tick and silently end renewal.
        with pytest.raises(ValueError, match="max_lock_lifetime_ms"):
            _chat_with(ConcurrencyConfig(strategy="queue", max_lock_lifetime_ms=bad))


class TestHeartbeatCrash:
    async def test_crashed_renewal_loop_is_logged(self, monkeypatch):
        clock = FakeClock().install(monkeypatch)
        state = create_mock_state()
        install_token_lock_mock(state, clock)
        heartbeats = _record_heartbeats(monkeypatch)
        chat, adapter, logger = await _make_chat(state, concurrency="queue")
        release = asyncio.Event()

        @chat.on_mention
        async def handler(thread, message, context=None):
            await release.wait()

        task = asyncio.create_task(
            chat.handle_incoming_message(adapter, THREAD, create_test_message("crash-1", "Hey @slack-bot"))
        )
        await clock.settle()

        def broken_clock() -> int:
            raise RuntimeError("clock unavailable")

        monkeypatch.setattr(chat_module, "_now_ms", broken_clock)
        await clock.advance(DEFAULT_LOCK_TTL_MS // 3)
        monkeypatch.setattr(chat_module, "_now_ms", clock.now_ms)

        assert heartbeats[0].task.done()
        assert [call[0] for call in logger.error.calls] == ["Lock heartbeat crashed — the lock will lapse at its TTL"]

        release.set()
        await task
