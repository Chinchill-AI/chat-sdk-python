"""Python-specific coverage for the held-lock heartbeat and drain isolation (#190).

The faithful upstream ports live in ``tests/test_chat_faithful.py``
(``TestConcurrencyLockLifetime`` and friends). These tests pin the asyncio
design choices the upstream suite cannot see: ``stop()`` waiting for an
in-flight extend before ``release_lock``, the extend-error ownership rule,
the ``drop`` strategy's heartbeat (including the ``on_lock_conflict="force"``
path), a JSON-roundtripped queue entry in the channel-scoped isolation path,
and the monotonic lifetime clock (a documented divergence).

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
from chat_sdk.types import ChatConfig, ConcurrencyConfig, Lock, Message, MessageContext, QueueEntry
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
        # stop() cancels an idle loop without yielding; it finishes on the next turn.
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
        assert extended_tokens
        assert stale.token not in extended_tokens
        assert set(extended_tokens) == {"test-token-2"}


class TestHeartbeatStopOrdering:
    async def test_stop_waits_for_an_in_flight_extend_before_release(self, monkeypatch):
        clock = FakeClock().install(monkeypatch)
        state = create_mock_state()
        order: list[str] = []
        extend_gate = asyncio.Event()

        async def blocked_extend(lock: Lock, ttl_ms: int) -> bool:
            order.append("extend-start")
            await extend_gate.wait()
            order.append("extend-end")
            return True

        real_release = state.release_lock

        async def recording_release(lock: Lock) -> None:
            order.append("release")
            await real_release(lock)

        state.extend_lock = AsyncMock(side_effect=blocked_extend)  # type: ignore[method-assign]
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

        # Handler returns while the extend is still in flight.
        finish_handler.set()
        await clock.settle()
        assert not task.done()
        state.release_lock.assert_not_awaited()

        extend_gate.set()
        await task

        assert order == ["extend-start", "extend-end", "release"]
        state.release_lock.assert_awaited_once()
        # stop() cancels an idle loop without yielding; it finishes on the next turn.
        await clock.settle()
        assert heartbeats[0].task.cancelled()

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
        # stop() cancels an idle loop without yielding; it finishes on the next turn.
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


class TestDebounceMidHandlerArrival:
    async def test_message_arriving_during_the_handler_is_debounced_and_dispatched_without_a_new_webhook(
        self, monkeypatch
    ):
        clock = FakeClock().install(monkeypatch)
        state = create_mock_state()
        install_token_lock_mock(state, clock)
        chat, adapter, _ = await _make_chat(state, concurrency=ConcurrencyConfig(strategy="debounce", debounce_ms=100))

        release = asyncio.Event()
        received: list[tuple[str, MessageContext | None]] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            received.append((message.id, context))
            if message.id == "deb-mid-1":
                await release.wait()

        first = asyncio.create_task(
            chat.handle_incoming_message(adapter, THREAD, create_test_message("deb-mid-1", "Hey @slack-bot one"))
        )
        await clock.wait_for(lambda: len(received) == 1)
        # Lock is busy: this only enqueues.
        await chat.handle_incoming_message(adapter, THREAD, create_test_message("deb-mid-2", "Hey @slack-bot two"))
        assert len(received) == 1

        release.set()
        await clock.advance(100)
        await clock.advance(100)
        await first

        assert [message_id for message_id, _ in received] == ["deb-mid-1", "deb-mid-2"]
        assert received[1][1] == MessageContext(skipped=[], total_since_last_handler=1)


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
        chat = Chat(
            ChatConfig(
                user_name="testbot",
                adapters={"slack": create_mock_adapter("slack")},
                state=create_mock_state(),
                logger=MockLogger(),
                concurrency=concurrency,
            )
        )
        assert chat._concurrency_max_lock_lifetime_ms == expected

    async def test_lifetime_cap_uses_monotonic_time_not_the_wall_clock(self, monkeypatch):
        # Divergence from upstream (Date.now()) -- see docs/UPSTREAM_SYNC.md.
        # A forward wall-clock step past the cap must not end renewal early.
        clock = FakeClock().install(monkeypatch)
        state = create_mock_state()
        state.extend_lock = AsyncMock(return_value=True)  # type: ignore[method-assign]
        chat, adapter, logger = await _make_chat(
            state, concurrency=ConcurrencyConfig(strategy="queue", max_lock_lifetime_ms=60_000)
        )
        release = asyncio.Event()

        @chat.on_mention
        async def handler(thread, message, context=None):
            await release.wait()

        task = asyncio.create_task(
            chat.handle_incoming_message(adapter, THREAD, create_test_message("mono-1", "Hey @slack-bot"))
        )
        await clock.settle()
        clock.jump_wall_clock(10 * 60_000)
        await clock.advance(DEFAULT_LOCK_TTL_MS // 3)

        assert state.extend_lock.await_count == 1
        assert "Lock heartbeat reached max_lock_lifetime_ms — the lock will lapse at its TTL" not in _warn_messages(
            logger
        )

        release.set()
        await task


def _skew_acquired_expiry(state: MockStateAdapter, skew_ms: int) -> None:
    """Return ``Lock.expires_at`` on a backend clock ``skew_ms`` off the app's.

    The Python Postgres backend stamps ``expires_at`` with the database's
    ``now()``; a negative skew is a database clock running behind the app
    host. The backend's own expiry bookkeeping is unchanged.
    """
    real_acquire = state.acquire_lock

    async def skewed_acquire(thread_id: str, ttl_ms: int) -> Lock | None:
        lock = await real_acquire(thread_id, ttl_ms)
        if lock is None:
            return None
        return Lock(thread_id=lock.thread_id, token=lock.token, expires_at=lock.expires_at + skew_ms)

    state.acquire_lock = skewed_acquire  # type: ignore[method-assign]


class TestHeldUntilIsLocal:
    """``held_until`` is seeded from the local clock, not ``Lock.expires_at``."""

    async def test_lagging_backend_expiry_still_dispatches_the_debounced_message(self, monkeypatch):
        clock = FakeClock().install(monkeypatch)
        state = create_mock_state()
        install_token_lock_mock(state, clock)
        _skew_acquired_expiry(state, -29_000)
        chat, adapter, logger = await _make_chat(
            state, concurrency=ConcurrencyConfig(strategy="debounce", debounce_ms=1500)
        )
        handled: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            handled.append(message.id)

        task = asyncio.create_task(
            chat.handle_incoming_message(adapter, THREAD, create_test_message("skew-d-1", "Hey @slack-bot"))
        )
        await clock.advance(1500)
        await clock.advance(1500)
        await task

        assert handled == ["skew-d-1"]
        assert "Stopping debounce loop after lock ownership was lost" not in _warn_messages(logger)

    async def test_lagging_backend_expiry_still_drains_the_queue(self, monkeypatch):
        clock = FakeClock().install(monkeypatch)
        state = create_mock_state()
        install_token_lock_mock(state, clock)
        _skew_acquired_expiry(state, -31_000)
        chat, adapter, _ = await _make_chat(state, concurrency="queue")
        release = asyncio.Event()
        handled: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            handled.append(message.id)
            if message.id == "skew-q-1":
                await release.wait()

        first = asyncio.create_task(
            chat.handle_incoming_message(adapter, THREAD, create_test_message("skew-q-1", "Hey @slack-bot one"))
        )
        await clock.wait_for(lambda: handled == ["skew-q-1"])
        await chat.handle_incoming_message(adapter, THREAD, create_test_message("skew-q-2", "Hey @slack-bot two"))
        await clock.advance(1_000)
        release.set()
        await first

        assert handled == ["skew-q-1", "skew-q-2"]
        assert await state.queue_depth(THREAD) == 0

    async def test_leading_backend_expiry_does_not_stretch_ownership_past_the_ttl(self, monkeypatch):
        # Backend clock 60s ahead: expires_at claims 90s of ownership. With
        # the backend unreachable, the drain must still stop once the TTL
        # measured locally has run out.
        clock = FakeClock().install(monkeypatch)
        state = create_mock_state()
        install_token_lock_mock(state, clock)
        _skew_acquired_expiry(state, 60_000)
        state.extend_lock = AsyncMock(side_effect=ConnectionError("backend down"))  # type: ignore[method-assign]
        chat, adapter, logger = await _make_chat(state, concurrency="queue")
        release = asyncio.Event()
        handled: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            handled.append(message.id)
            if message.id == "lead-1":
                await release.wait()

        first = asyncio.create_task(
            chat.handle_incoming_message(adapter, THREAD, create_test_message("lead-1", "Hey @slack-bot one"))
        )
        await clock.wait_for(lambda: handled == ["lead-1"])
        await chat.handle_incoming_message(adapter, THREAD, create_test_message("lead-2", "Hey @slack-bot two"))
        await clock.advance(DEFAULT_LOCK_TTL_MS + 1)
        release.set()
        await first

        assert handled == ["lead-1"]
        assert "Lock lapsed while the heartbeat could not reach the state backend" in _warn_messages(logger)
        assert await state.queue_depth(THREAD) == 1

    async def test_slow_extend_refreshes_ownership_from_when_it_was_requested(self, monkeypatch):
        # The backend applies the new TTL somewhere inside a slow extend, so
        # the local deadline counts from the request (10s -> 40s), not from
        # when the call returned (18s -> 48s).
        clock = FakeClock().install(monkeypatch)
        state = create_mock_state()
        calls = 0

        async def slow_then_down(lock: Lock, ttl_ms: int) -> bool:
            nonlocal calls
            calls += 1
            if calls == 1:
                await clock.sleep(8_000)
                return True
            raise ConnectionError("backend down")

        state.extend_lock = AsyncMock(side_effect=slow_then_down)  # type: ignore[method-assign]
        chat, adapter, logger = await _make_chat(state, concurrency="queue")
        release = asyncio.Event()
        handled: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            handled.append(message.id)
            if message.id == "slow-1":
                await release.wait()

        first = asyncio.create_task(
            chat.handle_incoming_message(adapter, THREAD, create_test_message("slow-1", "Hey @slack-bot one"))
        )
        await clock.wait_for(lambda: handled == ["slow-1"])
        await chat.handle_incoming_message(adapter, THREAD, create_test_message("slow-2", "Hey @slack-bot two"))
        await clock.advance(41_000)
        release.set()
        await first

        assert handled == ["slow-1"]
        assert "Stopping queue drain after lock ownership was lost" in _warn_messages(logger)
        assert await state.queue_depth(THREAD) == 1

    async def test_wall_clock_step_does_not_make_a_held_lock_look_lapsed(self, monkeypatch):
        # Divergence from upstream (heldUntil on Date.now()) -- see
        # docs/UPSTREAM_SYNC.md. An NTP step forward must not strand the queue.
        clock = FakeClock().install(monkeypatch)
        state = create_mock_state()
        state.extend_lock = AsyncMock(return_value=True)  # type: ignore[method-assign]
        chat, adapter, logger = await _make_chat(state, concurrency="queue")
        release = asyncio.Event()
        handled: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            handled.append(message.id)
            if message.id == "step-1":
                await release.wait()

        first = asyncio.create_task(
            chat.handle_incoming_message(adapter, THREAD, create_test_message("step-1", "Hey @slack-bot one"))
        )
        await clock.wait_for(lambda: handled == ["step-1"])
        await chat.handle_incoming_message(adapter, THREAD, create_test_message("step-2", "Hey @slack-bot two"))
        clock.jump_wall_clock(DEFAULT_LOCK_TTL_MS + 1)
        release.set()
        await first

        assert handled == ["step-1", "step-2"]
        assert "Stopping queue drain after lock ownership was lost" not in _warn_messages(logger)


class TestLifetimeCapEndsDrains:
    """``max_lock_lifetime_ms`` bounds drains: renewal stops, the lock lapses, the loop exits."""

    async def test_queue_drain_stops_after_the_capped_lock_lapses(self, monkeypatch):
        clock = FakeClock().install(monkeypatch)
        state = create_mock_state()
        install_token_lock_mock(state, clock)
        chat, adapter, logger = await _make_chat(
            state, concurrency=ConcurrencyConfig(strategy="queue", max_lock_lifetime_ms=20_000)
        )
        release = asyncio.Event()
        handled: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            handled.append(message.id)
            if message.id == "cap-q-1":
                await release.wait()

        first = asyncio.create_task(
            chat.handle_incoming_message(adapter, THREAD, create_test_message("cap-q-1", "Hey @slack-bot one"))
        )
        await clock.wait_for(lambda: handled == ["cap-q-1"])
        await chat.handle_incoming_message(adapter, THREAD, create_test_message("cap-q-2", "Hey @slack-bot two"))
        # Extend at 10s (held until 40s); the cap stops renewal at the 20s tick.
        await clock.advance(40_001)
        release.set()
        await first

        warns = _warn_messages(logger)
        assert "Lock heartbeat reached max_lock_lifetime_ms — the lock will lapse at its TTL" in warns
        assert "Stopping queue drain after lock ownership was lost" in warns
        assert handled == ["cap-q-1"]
        assert await state.queue_depth(THREAD) == 1

    async def test_continuous_debounce_traffic_ends_at_the_lifetime_cap(self, monkeypatch):
        clock = FakeClock().install(monkeypatch)
        state = create_mock_state()
        install_token_lock_mock(state, clock)
        chat, adapter, logger = await _make_chat(
            state,
            concurrency=ConcurrencyConfig(strategy="debounce", debounce_ms=1500, max_lock_lifetime_ms=20_000),
        )

        @chat.on_mention
        async def handler(thread, message, context=None):
            return None

        started_at = clock.now
        holder = asyncio.create_task(
            chat.handle_incoming_message(adapter, THREAD, create_test_message("cap-d-0", "Hey @slack-bot"))
        )
        others: list[asyncio.Task[None]] = []
        n = 0
        while not holder.done() and clock.now - started_at < 60_000:
            await clock.advance(1_000)
            n += 1
            others.append(
                asyncio.create_task(
                    chat.handle_incoming_message(adapter, THREAD, create_test_message(f"cap-d-{n}", "Hey @slack-bot"))
                )
            )
        ended_after = clock.now - started_at

        assert holder.done()
        # Past the 20s cap the pre-dispatch ownership check is refused, so the
        # loop ends after its next debounce sleep (the lock itself lapses one
        # TTL after the last extend).
        assert 20_000 <= ended_after <= 20_000 + 1_500 + 1_000
        assert "Stopping debounce loop after lock ownership was lost" in _warn_messages(logger)

        await clock.advance(10_000)
        await asyncio.gather(holder, *others)

    async def test_skipped_resets_between_dispatches_in_one_debounce_loop(self, monkeypatch):
        clock = FakeClock().install(monkeypatch)
        state = create_mock_state()
        install_token_lock_mock(state, clock)
        chat, adapter, _ = await _make_chat(state, concurrency=ConcurrencyConfig(strategy="debounce", debounce_ms=1500))
        release = asyncio.Event()
        received: list[tuple[str, list[str]]] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            received.append((message.id, [m.id for m in context.skipped]))
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

        assert received == [("reset-2", ["reset-1"]), ("reset-3", [])]

    async def test_holder_self_enqueue_keeps_a_leftover_entry_as_skipped(self, monkeypatch):
        clock = FakeClock().install(monkeypatch)
        state = create_mock_state()
        install_token_lock_mock(state, clock)
        chat, adapter, _ = await _make_chat(state, concurrency=ConcurrencyConfig(strategy="debounce", debounce_ms=1500))
        received: list[tuple[str, list[str]]] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            received.append((message.id, [m.id for m in context.skipped]))

        # A message left queued by a previous holder whose lock lapsed.
        leftover = create_test_message("left-1", "Hey @slack-bot earlier")
        await state.enqueue(
            THREAD, QueueEntry(message=leftover, enqueued_at=clock.now, expires_at=clock.now + 90_000), 10
        )
        task = asyncio.create_task(
            chat.handle_incoming_message(adapter, THREAD, create_test_message("left-2", "Hey @slack-bot now"))
        )
        await clock.advance(1500)
        await clock.advance(1500)
        await task

        assert received == [("left-2", ["left-1"])]


class TestHeartbeatFailuresAreVisible:
    @pytest.mark.parametrize("bad", ["600000", 1.5, True, -1])
    def test_invalid_max_lock_lifetime_ms_is_rejected_at_init(self, bad):
        with pytest.raises(ValueError, match="max_lock_lifetime_ms"):
            Chat(
                ChatConfig(
                    user_name="testbot",
                    adapters={"slack": create_mock_adapter("slack")},
                    state=create_mock_state(),
                    logger=MockLogger(),
                    concurrency=ConcurrencyConfig(strategy="queue", max_lock_lifetime_ms=bad),
                )
            )

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

        monkeypatch.setattr(chat_module, "_monotonic_ms", broken_clock)
        await clock.advance(DEFAULT_LOCK_TTL_MS // 3)
        monkeypatch.setattr(chat_module, "_monotonic_ms", clock.monotonic_ms)

        assert heartbeats[0].task.done()
        assert [call[0] for call in logger.error.calls] == ["Lock heartbeat crashed — the lock will lapse at its TTL"]

        release.set()
        await task


class TestHeldLockHandoff:
    """Windows between the heartbeat and the drain that must not strand or double-run work."""

    async def test_back_to_back_queue_messages_are_both_handled(self):
        # The second message finds the lock held and enqueues; the holder's
        # drain must still see it -- stopping an idle heartbeat must not open
        # an event-loop turn between the last empty-queue check and release.
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

    async def test_drain_confirms_the_token_before_dispatching_after_a_takeover(self, monkeypatch):
        # Another worker force-releases and re-acquires between heartbeat
        # ticks; the old holder must not dispatch queued work under a lock it
        # no longer owns. Divergence from upstream -- see docs/UPSTREAM_SYNC.md.
        clock = FakeClock().install(monkeypatch)
        state = create_mock_state()
        holder = install_token_lock_mock(state, clock)
        chat, adapter, logger = await _make_chat(state, concurrency="queue")
        release = asyncio.Event()
        handled: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            handled.append(message.id)
            if message.id == "take-1":
                await release.wait()

        first = asyncio.create_task(
            chat.handle_incoming_message(adapter, THREAD, create_test_message("take-1", "Hey @slack-bot one"))
        )
        await clock.wait_for(lambda: handled == ["take-1"])
        await chat.handle_incoming_message(adapter, THREAD, create_test_message("take-2", "Hey @slack-bot two"))
        await state.force_release_lock(THREAD)
        new_owner = await state.acquire_lock(THREAD, DEFAULT_LOCK_TTL_MS)
        assert new_owner is not None
        release.set()
        await first

        assert handled == ["take-1"]
        assert "Stopping queue drain after lock ownership was lost" in _warn_messages(logger)
        assert await state.queue_depth(THREAD) == 1
        # The old holder's release is token-checked and leaves the new owner alone.
        assert holder["active"] is new_owner

    async def test_stalled_extend_does_not_block_release_past_the_known_expiry(self, monkeypatch):
        clock = FakeClock().install(monkeypatch)
        state = create_mock_state()
        never = asyncio.Event()

        async def stalled_extend(lock: Lock, ttl_ms: int) -> bool:
            await never.wait()
            return True

        state.extend_lock = AsyncMock(side_effect=stalled_extend)  # type: ignore[method-assign]
        real_release = state.release_lock
        state.release_lock = AsyncMock(side_effect=real_release)  # type: ignore[method-assign]
        chat, adapter, logger = await _make_chat(state)
        finish = asyncio.Event()

        @chat.on_mention
        async def handler(thread, message, context=None):
            await finish.wait()

        task = asyncio.create_task(
            chat.handle_incoming_message(adapter, THREAD, create_test_message("stall-1", "Hey @slack-bot"))
        )
        await clock.advance(DEFAULT_LOCK_TTL_MS // 3)  # the extend starts and stalls
        finish.set()
        await clock.advance(DEFAULT_LOCK_TTL_MS // 3)
        assert not task.done()  # still inside the window where the lock is known held
        state.release_lock.assert_not_awaited()

        await clock.advance(DEFAULT_LOCK_TTL_MS // 3 + 1)
        assert task.done()
        await task

        state.release_lock.assert_awaited_once()
        assert "Releasing lock while a heartbeat extend is still in flight" in _warn_messages(logger)
        never.set()
        await clock.settle()

    async def test_message_enqueued_while_the_drain_waits_on_an_in_flight_extend_is_drained(self, monkeypatch):
        clock = FakeClock().install(monkeypatch)
        state = create_mock_state()
        install_token_lock_mock(state, clock)
        real_extend = state.extend_lock
        gate = asyncio.Event()

        async def gated_extend(lock: Lock, ttl_ms: int) -> bool:
            await gate.wait()
            return await real_extend(lock, ttl_ms)

        chat, adapter, _ = await _make_chat(state, concurrency="queue")
        finish = asyncio.Event()
        handled: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            handled.append(message.id)
            if message.id == "wait-1":
                await finish.wait()

        first = asyncio.create_task(
            chat.handle_incoming_message(adapter, THREAD, create_test_message("wait-1", "Hey @slack-bot one"))
        )
        await clock.wait_for(lambda: handled == ["wait-1"])
        # Gate only the heartbeat tick: the drain's ownership check before
        # the first handler finished already ran, so gate extends from now on.
        state.extend_lock = gated_extend  # type: ignore[method-assign]
        await clock.advance(DEFAULT_LOCK_TTL_MS // 3)  # heartbeat extend now in flight
        state.extend_lock = real_extend  # type: ignore[method-assign]
        finish.set()
        await clock.settle()
        assert not first.done()  # the holder is waiting on the in-flight extend

        await chat.handle_incoming_message(adapter, THREAD, create_test_message("wait-2", "Hey @slack-bot two"))
        gate.set()
        await first

        assert handled == ["wait-1", "wait-2"]
        assert await state.queue_depth(THREAD) == 0


class TestHeartbeatDeadlines:
    """Unit coverage for ``_LockHeartbeat`` deadline bookkeeping."""

    async def test_late_heartbeat_extend_keeps_a_newer_confirmed_deadline(self, monkeypatch):
        clock = FakeClock().install(monkeypatch)
        state = create_mock_state()
        calls = 0

        async def extend(lock: Lock, ttl_ms: int) -> bool:
            nonlocal calls
            calls += 1
            if calls == 1:  # heartbeat tick at 10s, returns at 16s
                await clock.sleep(6_000)
                return True
            if calls == 2:  # confirm_ownership at 15s
                return True
            raise ConnectionError("backend down")

        state.extend_lock = AsyncMock(side_effect=extend)  # type: ignore[method-assign]
        lock = Lock(thread_id=THREAD, token="t", expires_at=0)
        hb = chat_module._LockHeartbeat(state, lock, chat_module._monotonic_ms(), 600_000, MockLogger())
        try:
            await clock.advance(15_000)
            assert await hb.confirm_ownership() is True  # held until 45s
            await clock.advance(26_000)  # 41s: the late 10s extend (40s) must not win
            assert hb.is_ownership_lost() is False
            await clock.advance(4_000)  # 45s
            assert hb.is_ownership_lost() is True
        finally:
            await hb.stop()

    async def test_confirm_is_refused_past_the_cap_even_with_renewal_stuck(self, monkeypatch):
        clock = FakeClock().install(monkeypatch)
        state = create_mock_state()
        never = asyncio.Event()
        calls = 0

        async def extend(lock: Lock, ttl_ms: int) -> bool:
            nonlocal calls
            calls += 1
            if calls == 1:  # the heartbeat's own extend stalls
                await never.wait()
            return True

        state.extend_lock = AsyncMock(side_effect=extend)  # type: ignore[method-assign]
        lock = Lock(thread_id=THREAD, token="t", expires_at=0)
        hb = chat_module._LockHeartbeat(state, lock, chat_module._monotonic_ms(), 20_000, MockLogger())
        try:
            await clock.advance(15_000)
            assert await hb.confirm_ownership() is True
            await clock.advance(6_000)  # 21s: past the cap
            assert await hb.confirm_ownership() is False
            assert calls == 2  # the refused check never reached the backend
        finally:
            never.set()
            await hb.stop()
