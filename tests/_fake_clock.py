"""Fake clock + token-checked lock mock for ``Chat`` concurrency tests (#190).

Python stand-in for upstream's ``vi.useFakeTimers()`` and the
``installTokenLockMock`` helper in ``packages/chat/src/chat.test.ts``
(chat@4.39.0, 5b538f6f). ``FakeClock.install`` swaps the three time helpers
in ``chat_sdk.chat`` (``_sleep``, ``_now_ms``, ``_monotonic_ms``), so the
lock heartbeat, debounce/burst windows and queue-entry expiry all run on
virtual time: a 90-second scenario completes without a real sleep.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import time
from collections.abc import Callable
from typing import Any

import pytest

from chat_sdk.testing import MockStateAdapter
from chat_sdk.types import Lock

# Enough event-loop turns for every task woken by a timer to run until it
# blocks on its next timer/event. Only real suspension points yield, so a
# chain of awaits on in-memory mocks needs very few turns.
_SETTLE_TURNS = 50


class FakeClock:
    """Virtual wall + monotonic clock with vitest-style timer advancement."""

    def __init__(self) -> None:
        self.now = int(time.time() * 1000)
        self._monotonic_base = self.now
        self._timers: list[tuple[int, int, asyncio.Future[None]]] = []
        self._seq = itertools.count()

    # -- the helpers patched into chat_sdk.chat --------------------------------

    def now_ms(self) -> int:
        return self.now

    def monotonic_ms(self) -> int:
        return self.now - self._monotonic_base

    async def sleep(self, ms: int) -> None:
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._timers, (self.now + int(ms), next(self._seq), fut))
        await fut

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeClock:
        monkeypatch.setattr("chat_sdk.chat._sleep", self.sleep)
        monkeypatch.setattr("chat_sdk.chat._now_ms", self.now_ms)
        monkeypatch.setattr("chat_sdk.chat._monotonic_ms", self.monotonic_ms)
        return self

    # -- test controls -----------------------------------------------------------

    def jump_wall_clock(self, ms: int) -> None:
        """Step only the wall clock (an NTP step); monotonic time is unchanged.

        Timers are not fired: they run on elapsed (monotonic) time.
        """
        self.now += int(ms)
        self._monotonic_base += int(ms)
        self._timers = [(deadline + int(ms), seq, fut) for deadline, seq, fut in self._timers]
        heapq.heapify(self._timers)

    def pending_timers(self) -> int:
        """Timers still waiting to fire (``vi.getTimerCount()``)."""
        return sum(1 for _, _, fut in self._timers if not fut.done())

    async def settle(self) -> None:
        for _ in range(_SETTLE_TURNS):
            await asyncio.sleep(0)

    async def advance(self, ms: int) -> None:
        """Fire every timer due within ``ms``, in order (``advanceTimersByTimeAsync``)."""
        target = self.now + int(ms)
        await self.settle()
        while self._timers and self._timers[0][0] <= target:
            deadline, _, fut = heapq.heappop(self._timers)
            if fut.done():  # sleeper was cancelled
                continue
            self.now = max(self.now, deadline)
            fut.set_result(None)
            await self.settle()
        self.now = target
        await self.settle()

    async def wait_for(self, predicate: Callable[[], bool], *, step_ms: int = 50, max_steps: int = 200) -> None:
        """Advance in ``step_ms`` increments until ``predicate()`` holds (``vi.waitFor``)."""
        for _ in range(max_steps):
            await self.settle()
            if predicate():
                return
            await self.advance(step_ms)
        raise AssertionError("condition not met within the fake-time budget")


def install_token_lock_mock(state: MockStateAdapter, clock: FakeClock) -> dict[str, Any]:
    """Token-checked, expiry-aware lock semantics on ``clock`` (``installTokenLockMock``).

    ``MockStateAdapter``'s own lock ignores expiry and its ``extend_lock``
    always returns ``True``; the real backends compare tokens and expiry.
    Returns a dict exposing ``active`` (the current ``Lock`` or ``None``).
    """
    holder: dict[str, Any] = {"active": None}
    tokens = itertools.count(1)

    async def acquire_lock(thread_id: str, ttl_ms: int) -> Lock | None:
        active: Lock | None = holder["active"]
        if active is not None and active.expires_at > clock.now:
            return None
        lock = Lock(thread_id=thread_id, token=f"test-token-{next(tokens)}", expires_at=clock.now + ttl_ms)
        holder["active"] = lock
        return lock

    async def extend_lock(lock: Lock, ttl_ms: int) -> bool:
        active: Lock | None = holder["active"]
        if active is None or active.token != lock.token or active.expires_at <= clock.now:
            return False
        active.expires_at = clock.now + ttl_ms
        return True

    async def release_lock(lock: Lock) -> None:
        active: Lock | None = holder["active"]
        if active is not None and active.token == lock.token:
            holder["active"] = None

    async def force_release_lock(thread_id: str) -> None:
        holder["active"] = None

    state.acquire_lock = acquire_lock  # type: ignore[method-assign]
    state.force_release_lock = force_release_lock  # type: ignore[method-assign]
    state.extend_lock = extend_lock  # type: ignore[method-assign]
    state.release_lock = release_lock  # type: ignore[method-assign]
    return holder
