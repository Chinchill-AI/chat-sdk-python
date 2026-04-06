"""Tests for MemoryStateAdapter: get/set/delete, TTL expiry, locks, lists, queues, subscriptions.

Tests the real in-memory state adapter (not the mock).
"""

from __future__ import annotations

import time

import pytest
from chat_sdk.state.memory import MemoryStateAdapter
from chat_sdk.types import Lock, QueueEntry

# ============================================================================
# Lifecycle
# ============================================================================


class TestMemoryStateLifecycle:
    """Connection and disconnection tests."""

    @pytest.mark.asyncio
    async def test_connect_and_disconnect(self):
        state = MemoryStateAdapter()
        await state.connect()
        # Should be usable now
        await state.set("key", "value")
        assert await state.get("key") == "value"

        await state.disconnect()
        # After disconnect, operations should fail
        with pytest.raises(RuntimeError, match="not connected"):
            await state.get("key")

    @pytest.mark.asyncio
    async def test_connect_is_idempotent(self):
        state = MemoryStateAdapter()
        await state.connect()
        await state.connect()  # Should not raise
        await state.set("key", "value")
        assert await state.get("key") == "value"
        await state.disconnect()

    @pytest.mark.asyncio
    async def test_disconnect_clears_all_state(self):
        state = MemoryStateAdapter()
        await state.connect()
        await state.set("key", "value")
        await state.subscribe("thread-1")
        await state.disconnect()

        # Reconnect and verify everything is cleared
        await state.connect()
        assert await state.get("key") is None
        assert await state.is_subscribed("thread-1") is False
        await state.disconnect()

    @pytest.mark.asyncio
    async def test_operations_fail_before_connect(self):
        state = MemoryStateAdapter()
        with pytest.raises(RuntimeError, match="not connected"):
            await state.get("key")


# ============================================================================
# Key/Value Cache: get / set / delete
# ============================================================================


class TestMemoryStateKV:
    """Key-value cache operations."""

    @pytest.mark.asyncio
    async def test_get_returns_none_for_missing_key(self, memory_state: MemoryStateAdapter):
        assert await memory_state.get("nonexistent") is None

    @pytest.mark.asyncio
    async def test_set_and_get(self, memory_state: MemoryStateAdapter):
        await memory_state.set("key", "value")
        assert await memory_state.get("key") == "value"

    @pytest.mark.asyncio
    async def test_set_overwrites_existing_value(self, memory_state: MemoryStateAdapter):
        await memory_state.set("key", "first")
        await memory_state.set("key", "second")
        assert await memory_state.get("key") == "second"

    @pytest.mark.asyncio
    async def test_set_with_various_types(self, memory_state: MemoryStateAdapter):
        await memory_state.set("str", "hello")
        await memory_state.set("int", 42)
        await memory_state.set("bool", True)
        await memory_state.set("dict", {"nested": "value"})
        await memory_state.set("list", [1, 2, 3])

        assert await memory_state.get("str") == "hello"
        assert await memory_state.get("int") == 42
        assert await memory_state.get("bool") is True
        assert await memory_state.get("dict") == {"nested": "value"}
        assert await memory_state.get("list") == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_delete_removes_key(self, memory_state: MemoryStateAdapter):
        await memory_state.set("key", "value")
        await memory_state.delete("key")
        assert await memory_state.get("key") is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_key_is_noop(self, memory_state: MemoryStateAdapter):
        # Should not raise
        await memory_state.delete("nonexistent")

    @pytest.mark.asyncio
    async def test_set_if_not_exists_when_key_missing(self, memory_state: MemoryStateAdapter):
        result = await memory_state.set_if_not_exists("key", "value")
        assert result is True
        assert await memory_state.get("key") == "value"

    @pytest.mark.asyncio
    async def test_set_if_not_exists_when_key_exists(self, memory_state: MemoryStateAdapter):
        await memory_state.set("key", "first")
        result = await memory_state.set_if_not_exists("key", "second")
        assert result is False
        assert await memory_state.get("key") == "first"


# ============================================================================
# TTL Expiry
# ============================================================================


class TestMemoryStateTTL:
    """TTL expiry tests for key/value cache."""

    @pytest.mark.asyncio
    async def test_set_with_ttl_expires(self, memory_state: MemoryStateAdapter):
        # Set with a very short TTL
        await memory_state.set("key", "value", ttl_ms=1)
        # Wait for expiry
        time.sleep(0.005)  # 5ms -- well past 1ms TTL
        assert await memory_state.get("key") is None

    @pytest.mark.asyncio
    async def test_set_with_ttl_available_before_expiry(self, memory_state: MemoryStateAdapter):
        await memory_state.set("key", "value", ttl_ms=60_000)
        assert await memory_state.get("key") == "value"

    @pytest.mark.asyncio
    async def test_set_without_ttl_never_expires(self, memory_state: MemoryStateAdapter):
        await memory_state.set("key", "value")
        # Even after a brief wait
        time.sleep(0.005)
        assert await memory_state.get("key") == "value"

    @pytest.mark.asyncio
    async def test_set_if_not_exists_respects_expired_key(self, memory_state: MemoryStateAdapter):
        await memory_state.set("key", "old", ttl_ms=1)
        time.sleep(0.005)
        result = await memory_state.set_if_not_exists("key", "new")
        assert result is True
        assert await memory_state.get("key") == "new"


# ============================================================================
# Locks
# ============================================================================


class TestMemoryStateLocks:
    """Locking tests: acquire, release, extend, force release."""

    @pytest.mark.asyncio
    async def test_acquire_lock(self, memory_state: MemoryStateAdapter):
        lock = await memory_state.acquire_lock("thread-1", 30_000)
        assert lock is not None
        assert lock.thread_id == "thread-1"
        assert lock.token != ""
        assert lock.expires_at > 0

    @pytest.mark.asyncio
    async def test_acquire_lock_fails_when_already_held(self, memory_state: MemoryStateAdapter):
        lock1 = await memory_state.acquire_lock("thread-1", 30_000)
        assert lock1 is not None

        lock2 = await memory_state.acquire_lock("thread-1", 30_000)
        assert lock2 is None

    @pytest.mark.asyncio
    async def test_acquire_lock_succeeds_after_expiry(self, memory_state: MemoryStateAdapter):
        lock1 = await memory_state.acquire_lock("thread-1", 1)  # 1ms TTL
        assert lock1 is not None
        time.sleep(0.005)

        lock2 = await memory_state.acquire_lock("thread-1", 30_000)
        assert lock2 is not None

    @pytest.mark.asyncio
    async def test_release_lock(self, memory_state: MemoryStateAdapter):
        lock = await memory_state.acquire_lock("thread-1", 30_000)
        assert lock is not None

        await memory_state.release_lock(lock)

        # Should be able to acquire again
        lock2 = await memory_state.acquire_lock("thread-1", 30_000)
        assert lock2 is not None

    @pytest.mark.asyncio
    async def test_release_lock_with_wrong_token_is_noop(self, memory_state: MemoryStateAdapter):
        lock = await memory_state.acquire_lock("thread-1", 30_000)
        assert lock is not None

        # Create a fake lock with wrong token
        fake_lock = Lock(thread_id="thread-1", token="wrong-token", expires_at=0)
        await memory_state.release_lock(fake_lock)

        # Original lock should still be held
        lock2 = await memory_state.acquire_lock("thread-1", 30_000)
        assert lock2 is None

    @pytest.mark.asyncio
    async def test_extend_lock(self, memory_state: MemoryStateAdapter):
        lock = await memory_state.acquire_lock("thread-1", 100)
        assert lock is not None

        result = await memory_state.extend_lock(lock, 60_000)
        assert result is True

        # Lock should still be held after original TTL would have expired
        time.sleep(0.15)
        lock2 = await memory_state.acquire_lock("thread-1", 30_000)
        assert lock2 is None  # Still held due to extension

    @pytest.mark.asyncio
    async def test_extend_lock_fails_with_wrong_token(self, memory_state: MemoryStateAdapter):
        lock = await memory_state.acquire_lock("thread-1", 30_000)
        assert lock is not None

        fake_lock = Lock(thread_id="thread-1", token="wrong-token", expires_at=0)
        result = await memory_state.extend_lock(fake_lock, 60_000)
        assert result is False

    @pytest.mark.asyncio
    async def test_extend_lock_fails_after_expiry(self, memory_state: MemoryStateAdapter):
        lock = await memory_state.acquire_lock("thread-1", 1)
        assert lock is not None
        time.sleep(0.005)

        result = await memory_state.extend_lock(lock, 60_000)
        assert result is False

    @pytest.mark.asyncio
    async def test_force_release_lock(self, memory_state: MemoryStateAdapter):
        lock = await memory_state.acquire_lock("thread-1", 30_000)
        assert lock is not None

        await memory_state.force_release_lock("thread-1")

        # Should be able to acquire again
        lock2 = await memory_state.acquire_lock("thread-1", 30_000)
        assert lock2 is not None

    @pytest.mark.asyncio
    async def test_force_release_nonexistent_lock_is_noop(self, memory_state: MemoryStateAdapter):
        # Should not raise
        await memory_state.force_release_lock("nonexistent")

    @pytest.mark.asyncio
    async def test_different_threads_get_independent_locks(self, memory_state: MemoryStateAdapter):
        lock1 = await memory_state.acquire_lock("thread-1", 30_000)
        lock2 = await memory_state.acquire_lock("thread-2", 30_000)
        assert lock1 is not None
        assert lock2 is not None
        assert lock1.token != lock2.token


# ============================================================================
# Lists
# ============================================================================


class TestMemoryStateLists:
    """List operations: append, get, max_length, TTL."""

    @pytest.mark.asyncio
    async def test_get_list_returns_empty_for_missing_key(self, memory_state: MemoryStateAdapter):
        result = await memory_state.get_list("nonexistent")
        assert result == []

    @pytest.mark.asyncio
    async def test_append_to_list_and_get(self, memory_state: MemoryStateAdapter):
        await memory_state.append_to_list("key", "a")
        await memory_state.append_to_list("key", "b")
        await memory_state.append_to_list("key", "c")
        result = await memory_state.get_list("key")
        assert result == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_append_to_list_with_max_length(self, memory_state: MemoryStateAdapter):
        for i in range(5):
            await memory_state.append_to_list("key", i, max_length=3)
        result = await memory_state.get_list("key")
        assert result == [2, 3, 4]

    @pytest.mark.asyncio
    async def test_list_with_ttl_expires(self, memory_state: MemoryStateAdapter):
        await memory_state.append_to_list("key", "a", ttl_ms=1)
        time.sleep(0.005)
        result = await memory_state.get_list("key")
        assert result == []

    @pytest.mark.asyncio
    async def test_list_with_ttl_available_before_expiry(self, memory_state: MemoryStateAdapter):
        await memory_state.append_to_list("key", "a", ttl_ms=60_000)
        result = await memory_state.get_list("key")
        assert result == ["a"]


# ============================================================================
# Queues
# ============================================================================


class TestMemoryStateQueues:
    """Queue operations: enqueue, dequeue, queue_depth."""

    def _make_entry(self, msg_id: str = "msg-1") -> QueueEntry:
        """Create a QueueEntry for testing."""
        from chat_sdk.testing import create_test_message

        msg = create_test_message(msg_id, f"Message {msg_id}")
        now = int(time.time() * 1000)
        return QueueEntry(message=msg, enqueued_at=now, expires_at=now + 90_000)

    @pytest.mark.asyncio
    async def test_queue_depth_returns_zero_for_empty_queue(self, memory_state: MemoryStateAdapter):
        assert await memory_state.queue_depth("thread-1") == 0

    @pytest.mark.asyncio
    async def test_enqueue_and_dequeue(self, memory_state: MemoryStateAdapter):
        entry = self._make_entry("msg-1")
        depth = await memory_state.enqueue("thread-1", entry, max_size=10)
        assert depth == 1

        result = await memory_state.dequeue("thread-1")
        assert result is not None
        assert result.message.id == "msg-1"

    @pytest.mark.asyncio
    async def test_dequeue_returns_none_for_empty_queue(self, memory_state: MemoryStateAdapter):
        result = await memory_state.dequeue("thread-1")
        assert result is None

    @pytest.mark.asyncio
    async def test_enqueue_respects_max_size(self, memory_state: MemoryStateAdapter):
        for i in range(5):
            await memory_state.enqueue("thread-1", self._make_entry(f"msg-{i}"), max_size=3)

        assert await memory_state.queue_depth("thread-1") == 3

        # Oldest entries should have been dropped
        first = await memory_state.dequeue("thread-1")
        assert first is not None
        assert first.message.id == "msg-2"

    @pytest.mark.asyncio
    async def test_enqueue_fifo_order(self, memory_state: MemoryStateAdapter):
        for i in range(3):
            await memory_state.enqueue("thread-1", self._make_entry(f"msg-{i}"), max_size=10)

        results = []
        for _ in range(3):
            entry = await memory_state.dequeue("thread-1")
            assert entry is not None
            results.append(entry.message.id)

        assert results == ["msg-0", "msg-1", "msg-2"]

    @pytest.mark.asyncio
    async def test_queue_depth_after_operations(self, memory_state: MemoryStateAdapter):
        await memory_state.enqueue("thread-1", self._make_entry("msg-0"), max_size=10)
        await memory_state.enqueue("thread-1", self._make_entry("msg-1"), max_size=10)
        assert await memory_state.queue_depth("thread-1") == 2

        await memory_state.dequeue("thread-1")
        assert await memory_state.queue_depth("thread-1") == 1

        await memory_state.dequeue("thread-1")
        assert await memory_state.queue_depth("thread-1") == 0

    @pytest.mark.asyncio
    async def test_independent_queues_per_thread(self, memory_state: MemoryStateAdapter):
        await memory_state.enqueue("thread-1", self._make_entry("msg-a"), max_size=10)
        await memory_state.enqueue("thread-2", self._make_entry("msg-b"), max_size=10)

        assert await memory_state.queue_depth("thread-1") == 1
        assert await memory_state.queue_depth("thread-2") == 1

        entry1 = await memory_state.dequeue("thread-1")
        assert entry1 is not None
        assert entry1.message.id == "msg-a"

        entry2 = await memory_state.dequeue("thread-2")
        assert entry2 is not None
        assert entry2.message.id == "msg-b"


# ============================================================================
# Subscriptions
# ============================================================================


class TestMemoryStateSubscriptions:
    """Subscription operations: subscribe, unsubscribe, is_subscribed."""

    @pytest.mark.asyncio
    async def test_is_subscribed_returns_false_for_unsubscribed(self, memory_state: MemoryStateAdapter):
        assert await memory_state.is_subscribed("thread-1") is False

    @pytest.mark.asyncio
    async def test_subscribe_and_is_subscribed(self, memory_state: MemoryStateAdapter):
        await memory_state.subscribe("thread-1")
        assert await memory_state.is_subscribed("thread-1") is True

    @pytest.mark.asyncio
    async def test_unsubscribe(self, memory_state: MemoryStateAdapter):
        await memory_state.subscribe("thread-1")
        await memory_state.unsubscribe("thread-1")
        assert await memory_state.is_subscribed("thread-1") is False

    @pytest.mark.asyncio
    async def test_unsubscribe_nonexistent_is_noop(self, memory_state: MemoryStateAdapter):
        # Should not raise
        await memory_state.unsubscribe("nonexistent")

    @pytest.mark.asyncio
    async def test_subscribe_is_idempotent(self, memory_state: MemoryStateAdapter):
        await memory_state.subscribe("thread-1")
        await memory_state.subscribe("thread-1")
        assert await memory_state.is_subscribed("thread-1") is True

    @pytest.mark.asyncio
    async def test_independent_subscriptions(self, memory_state: MemoryStateAdapter):
        await memory_state.subscribe("thread-1")
        await memory_state.subscribe("thread-2")

        assert await memory_state.is_subscribed("thread-1") is True
        assert await memory_state.is_subscribed("thread-2") is True
        assert await memory_state.is_subscribed("thread-3") is False

    @pytest.mark.asyncio
    async def test_subscription_count(self, memory_state: MemoryStateAdapter):
        assert memory_state._get_subscription_count() == 0
        await memory_state.subscribe("thread-1")
        await memory_state.subscribe("thread-2")
        assert memory_state._get_subscription_count() == 2
        await memory_state.unsubscribe("thread-1")
        assert memory_state._get_subscription_count() == 1
