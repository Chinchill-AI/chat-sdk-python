"""In-memory StateAdapter implementation.

Python port of ``@chat-adapter/state-memory`` (index.ts).

WARNING: State is not persisted across restarts.
Use a Redis-backed adapter for production.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
import warnings
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

from chat_sdk.types import Lock, QueueEntry

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


@dataclass
class _CachedValue:
    """Wrapper around a cached value with optional expiry."""

    value: Any
    expires_at: float | None = None  # None = no expiry (epoch ms)


def _generate_token() -> str:
    return f"mem_{int(time.time() * 1000)}_{secrets.token_hex(16)}"


def _now_ms() -> float:
    return time.time() * 1000


# ---------------------------------------------------------------------------
# MemoryStateAdapter
# ---------------------------------------------------------------------------


class MemoryStateAdapter:
    """In-memory state adapter for development and testing.

    Implements the full :class:`~chat_sdk.types.StateAdapter` protocol
    including subscriptions, distributed-ish locking, key/value cache with
    TTL, ordered lists, and per-thread queues.
    """

    def __init__(self) -> None:
        self._subscriptions: set[str] = set()
        self._locks: dict[str, _MemoryLock] = {}
        self._cache: dict[str, _CachedValue] = {}
        self._queues: dict[str, list[QueueEntry]] = {}
        self._connected = False
        self._connect_done = False

    # -- lifecycle -----------------------------------------------------------

    async def connect(self) -> None:
        if self._connected:
            return
        if os.environ.get("NODE_ENV") == "production" or os.environ.get("ENV") == "production":
            warnings.warn(
                "[chat] MemoryStateAdapter is not recommended for production. "
                "Consider using a Redis-backed state adapter instead.",
                stacklevel=2,
            )
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False
        self._subscriptions.clear()
        self._locks.clear()
        self._cache.clear()
        self._queues.clear()

    # -- subscriptions -------------------------------------------------------

    async def subscribe(self, thread_id: str) -> None:
        self._ensure_connected()
        self._subscriptions.add(thread_id)

    async def unsubscribe(self, thread_id: str) -> None:
        self._ensure_connected()
        self._subscriptions.discard(thread_id)

    async def is_subscribed(self, thread_id: str) -> bool:
        self._ensure_connected()
        return thread_id in self._subscriptions

    # -- locking -------------------------------------------------------------

    async def acquire_lock(self, thread_id: str, ttl_ms: int) -> Lock | None:
        self._ensure_connected()
        self._clean_expired_locks()

        existing = self._locks.get(thread_id)
        if existing is not None and existing.expires_at > _now_ms():
            return None

        lock = _MemoryLock(
            thread_id=thread_id,
            token=_generate_token(),
            expires_at=_now_ms() + ttl_ms,
        )
        self._locks[thread_id] = lock
        return Lock(
            thread_id=lock.thread_id,
            token=lock.token,
            expires_at=int(lock.expires_at),
        )

    async def force_release_lock(self, thread_id: str) -> None:
        self._ensure_connected()
        self._locks.pop(thread_id, None)

    async def release_lock(self, lock: Lock) -> None:
        self._ensure_connected()
        existing = self._locks.get(lock.thread_id)
        if existing is not None and existing.token == lock.token:
            del self._locks[lock.thread_id]

    async def extend_lock(self, lock: Lock, ttl_ms: int) -> bool:
        self._ensure_connected()
        existing = self._locks.get(lock.thread_id)
        if existing is None or existing.token != lock.token:
            return False
        if existing.expires_at < _now_ms():
            del self._locks[lock.thread_id]
            return False
        existing.expires_at = _now_ms() + ttl_ms
        return True

    # -- key/value cache -----------------------------------------------------

    async def get(self, key: str) -> Any | None:
        self._ensure_connected()
        cached = self._cache.get(key)
        if cached is None:
            return None
        if cached.expires_at is not None and cached.expires_at <= _now_ms():
            del self._cache[key]
            return None
        return cached.value

    async def set(self, key: str, value: Any, ttl_ms: int | None = None) -> None:
        self._ensure_connected()
        self._cache[key] = _CachedValue(
            value=value,
            expires_at=(_now_ms() + ttl_ms) if ttl_ms else None,
        )

    async def set_if_not_exists(self, key: str, value: Any, ttl_ms: int | None = None) -> bool:
        self._ensure_connected()
        existing = self._cache.get(key)
        if existing is not None:
            if existing.expires_at is not None and existing.expires_at <= _now_ms():
                del self._cache[key]
            else:
                return False
        self._cache[key] = _CachedValue(
            value=value,
            expires_at=(_now_ms() + ttl_ms) if ttl_ms else None,
        )
        return True

    async def delete(self, key: str) -> None:
        self._ensure_connected()
        self._cache.pop(key, None)

    # -- lists ---------------------------------------------------------------

    async def append_to_list(
        self,
        key: str,
        value: Any,
        *,
        max_length: int | None = None,
        ttl_ms: int | None = None,
    ) -> None:
        self._ensure_connected()
        cached = self._cache.get(key)

        if cached is not None and cached.expires_at is not None and cached.expires_at <= _now_ms():
            lst: list[Any] = []
        elif cached is not None and isinstance(cached.value, list):
            lst = cached.value
        elif cached is not None:
            logger.warning(
                "append_to_list: existing value for key %r is %s, not list; resetting to empty list",
                key,
                type(cached.value).__name__,
            )
            lst = []
        else:
            lst = []

        lst.append(value)

        if max_length and len(lst) > max_length:
            lst = lst[len(lst) - max_length :]

        self._cache[key] = _CachedValue(
            value=lst,
            expires_at=(_now_ms() + ttl_ms) if ttl_ms else None,
        )

    async def get_list(self, key: str) -> list[Any]:
        self._ensure_connected()
        cached = self._cache.get(key)
        if cached is None:
            return []
        if cached.expires_at is not None and cached.expires_at <= _now_ms():
            del self._cache[key]
            return []
        if isinstance(cached.value, list):
            return list(cached.value)
        return []

    # -- queues --------------------------------------------------------------

    async def enqueue(self, thread_id: str, entry: QueueEntry, max_size: int) -> int:
        self._ensure_connected()
        queue = self._queues.setdefault(thread_id, [])
        queue.append(entry)
        if len(queue) > max_size:
            del queue[: len(queue) - max_size]
        return len(queue)

    async def dequeue(self, thread_id: str) -> QueueEntry | None:
        self._ensure_connected()
        queue = self._queues.get(thread_id)
        if not queue:
            return None
        entry = queue.pop(0)
        if not queue:
            del self._queues[thread_id]
        return entry

    async def queue_depth(self, thread_id: str) -> int:
        self._ensure_connected()
        queue = self._queues.get(thread_id)
        return len(queue) if queue else 0

    # -- testing helpers -----------------------------------------------------

    def _get_subscription_count(self) -> int:
        return len(self._subscriptions)

    def _get_lock_count(self) -> int:
        self._clean_expired_locks()
        return len(self._locks)

    # -- internal ------------------------------------------------------------

    def _ensure_connected(self) -> None:
        if not self._connected:
            raise RuntimeError("MemoryStateAdapter is not connected. Call connect() first.")

    def _clean_expired_locks(self) -> None:
        now = _now_ms()
        expired = [tid for tid, lk in self._locks.items() if lk.expires_at <= now]
        for tid in expired:
            del self._locks[tid]


@dataclass
class _MemoryLock:
    """Internal lock representation with mutable expiry."""

    thread_id: str
    token: str
    expires_at: float


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_memory_state() -> MemoryStateAdapter:
    """Create a new in-memory state adapter."""
    return MemoryStateAdapter()
