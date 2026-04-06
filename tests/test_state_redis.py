"""Tests for RedisStateAdapter using a MockRedis that simulates redis.asyncio.

Since we cannot assume a running Redis instance, a MockRedis class
simulates the subset of the redis.asyncio interface that the adapter uses:
get, set (with px/nx), delete, exists, eval (Lua scripts), sadd, srem,
sismember, rpush, lpush, lrange, ltrim, llen, lpop, ping, aclose.
"""

from __future__ import annotations

import json
import time

import pytest
from chat_sdk.state.redis import RedisStateAdapter
from chat_sdk.types import Lock, QueueEntry


# ============================================================================
# MockRedis
# ============================================================================


class MockRedis:
    """In-memory simulation of the redis.asyncio client interface.

    Supports the operations used by ``RedisStateAdapter``:
    strings (get/set with NX/PX), sets (sadd/srem/sismember),
    lists (rpush/lpush/lrange/ltrim/llen/lpop), delete, exists,
    eval (Lua script simulation), ping, aclose.
    """

    def __init__(self) -> None:
        self._store: dict[str, str] = {}
        self._sets: dict[str, set[str]] = {}
        self._lists: dict[str, list[str]] = {}
        self._ttls: dict[str, float] = {}  # key -> absolute expiry in seconds
        self._closed = False

    # -- helpers ---------------------------------------------------------------

    def _is_expired(self, key: str) -> bool:
        if key in self._ttls:
            if time.time() >= self._ttls[key]:
                self._evict(key)
                return True
        return False

    def _evict(self, key: str) -> None:
        self._store.pop(key, None)
        self._sets.pop(key, None)
        self._lists.pop(key, None)
        self._ttls.pop(key, None)

    def _set_ttl(self, key: str, px_ms: int) -> None:
        self._ttls[key] = time.time() + (px_ms / 1000.0)

    # -- lifecycle -------------------------------------------------------------

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        self._closed = True

    # -- string commands -------------------------------------------------------

    async def get(self, key: str) -> str | None:
        if self._is_expired(key):
            return None
        return self._store.get(key)

    async def set(
        self,
        key: str,
        value: str,
        *,
        nx: bool = False,
        px: int | None = None,
    ) -> bool | None:
        if self._is_expired(key):
            pass  # key already evicted

        if nx:
            if key in self._store:
                return None  # key already exists -> NX fails
            self._store[key] = value
            if px:
                self._set_ttl(key, px)
            return True

        self._store[key] = value
        if px:
            self._set_ttl(key, px)
        else:
            self._ttls.pop(key, None)
        return True

    async def delete(self, key: str) -> int:
        existed = key in self._store or key in self._lists or key in self._sets
        self._evict(key)
        return 1 if existed else 0

    async def exists(self, key: str) -> int:
        if self._is_expired(key):
            return 0
        return 1 if key in self._store else 0

    # -- set commands ----------------------------------------------------------

    async def sadd(self, key: str, *values: str) -> int:
        s = self._sets.setdefault(key, set())
        before = len(s)
        s.update(values)
        return len(s) - before

    async def srem(self, key: str, *values: str) -> int:
        s = self._sets.get(key, set())
        removed = 0
        for v in values:
            if v in s:
                s.discard(v)
                removed += 1
        return removed

    async def sismember(self, key: str, value: str) -> int:
        s = self._sets.get(key, set())
        return 1 if value in s else 0

    # -- list commands ---------------------------------------------------------

    async def rpush(self, key: str, *values: str) -> int:
        lst = self._lists.setdefault(key, [])
        lst.extend(values)
        return len(lst)

    async def lpush(self, key: str, *values: str) -> int:
        lst = self._lists.setdefault(key, [])
        for v in reversed(values):
            lst.insert(0, v)
        return len(lst)

    async def lrange(self, key: str, start: int, stop: int) -> list[str]:
        if self._is_expired(key):
            return []
        lst = self._lists.get(key, [])
        if stop == -1:
            return lst[start:]
        return lst[start : stop + 1]

    async def ltrim(self, key: str, start: int, stop: int) -> str:
        lst = self._lists.get(key, [])
        if stop == -1:
            self._lists[key] = lst[start:]
        else:
            self._lists[key] = lst[start : stop + 1]
        return "OK"

    async def llen(self, key: str) -> int:
        if self._is_expired(key):
            return 0
        return len(self._lists.get(key, []))

    async def lpop(self, key: str) -> str | None:
        if self._is_expired(key):
            return None
        lst = self._lists.get(key, [])
        if not lst:
            return None
        return lst.pop(0)

    # -- eval (Lua script simulation) ------------------------------------------

    async def eval(self, script: str, num_keys: int, *args: str) -> int | str | None:
        """Simulates Redis EVAL for the specific Lua scripts used by the adapter."""
        keys = list(args[:num_keys])
        argv = list(args[num_keys:])

        if "del" in script and "get" in script and "pexpire" not in script:
            # _RELEASE_LOCK_SCRIPT
            key = keys[0]
            token = argv[0]
            current = self._store.get(key)
            if current == token:
                self._evict(key)
                return 1
            return 0

        if "pexpire" in script and "get" in script:
            # _EXTEND_LOCK_SCRIPT
            key = keys[0]
            token = argv[0]
            ttl_ms = int(argv[1])
            if self._is_expired(key):
                return 0
            current = self._store.get(key)
            if current == token:
                self._set_ttl(key, ttl_ms)
                return 1
            return 0

        if "rpush" in script and "ltrim" in script and "llen" not in script:
            # _APPEND_LIST_SCRIPT
            key = keys[0]
            value = argv[0]
            max_len = int(argv[1])
            ttl = int(argv[2])

            lst = self._lists.setdefault(key, [])
            lst.append(value)
            if max_len > 0:
                self._lists[key] = lst[-max_len:]
            if ttl > 0:
                self._set_ttl(key, ttl)
            return 1

        if "rpush" in script and "ltrim" in script and "llen" in script:
            # _ENQUEUE_SCRIPT
            key = keys[0]
            value = argv[0]
            max_size = int(argv[1])
            ttl_ms = int(argv[2])

            lst = self._lists.setdefault(key, [])
            lst.append(value)
            if max_size > 0:
                self._lists[key] = lst[-max_size:]
            self._set_ttl(key, ttl_ms)
            return len(self._lists[key])

        raise NotImplementedError(f"MockRedis.eval: unrecognised Lua script")


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def mock_redis() -> MockRedis:
    return MockRedis()


@pytest.fixture
async def redis_state(mock_redis: MockRedis) -> RedisStateAdapter:
    adapter = RedisStateAdapter(client=mock_redis, key_prefix="test")
    await adapter.connect()
    yield adapter  # type: ignore[misc]
    await adapter.disconnect()


# ============================================================================
# Helpers
# ============================================================================


def _make_queue_entry(msg_id: str = "msg-1") -> QueueEntry:
    """Create a QueueEntry with a plain-dict message (JSON-serializable)."""
    msg = {"id": msg_id, "text": f"Message {msg_id}", "thread_id": "t1"}
    now = int(time.time() * 1000)
    return QueueEntry(message=msg, enqueued_at=now, expires_at=now + 90_000)


# ============================================================================
# Connection lifecycle
# ============================================================================


class TestRedisStateLifecycle:
    """Connection and disconnection tests."""

    @pytest.mark.asyncio
    async def test_connect_pings_redis(self, mock_redis: MockRedis):
        adapter = RedisStateAdapter(client=mock_redis, key_prefix="test")
        await adapter.connect()
        # If ping failed, connect would raise
        assert adapter._connected is True
        await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_connect_is_idempotent(self, mock_redis: MockRedis):
        adapter = RedisStateAdapter(client=mock_redis, key_prefix="test")
        await adapter.connect()
        await adapter.connect()  # Should not raise
        assert adapter._connected is True
        await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_disconnect_sets_connected_false(self, mock_redis: MockRedis):
        adapter = RedisStateAdapter(client=mock_redis, key_prefix="test")
        await adapter.connect()
        await adapter.disconnect()
        assert adapter._connected is False

    @pytest.mark.asyncio
    async def test_disconnect_without_connect_is_noop(self, mock_redis: MockRedis):
        adapter = RedisStateAdapter(client=mock_redis, key_prefix="test")
        await adapter.disconnect()  # Should not raise

    @pytest.mark.asyncio
    async def test_operations_fail_before_connect(self, mock_redis: MockRedis):
        adapter = RedisStateAdapter(client=mock_redis, key_prefix="test")
        with pytest.raises(RuntimeError, match="not connected"):
            await adapter.get("key")

    @pytest.mark.asyncio
    async def test_get_client_returns_underlying_client(self, mock_redis: MockRedis):
        adapter = RedisStateAdapter(client=mock_redis, key_prefix="test")
        assert adapter.get_client() is mock_redis

    @pytest.mark.asyncio
    async def test_injected_client_not_closed_on_disconnect(self, mock_redis: MockRedis):
        adapter = RedisStateAdapter(client=mock_redis, key_prefix="test")
        await adapter.connect()
        await adapter.disconnect()
        # Injected client should not be closed (owns_client is False)
        assert mock_redis._closed is False

    @pytest.mark.asyncio
    async def test_url_required_when_no_client(self):
        with pytest.raises((ValueError, ModuleNotFoundError)):
            RedisStateAdapter(key_prefix="test")


# ============================================================================
# Key/Value CRUD with TTL
# ============================================================================


class TestRedisStateKV:
    """Key-value cache operations."""

    @pytest.mark.asyncio
    async def test_get_returns_none_for_missing_key(self, redis_state: RedisStateAdapter):
        assert await redis_state.get("nonexistent") is None

    @pytest.mark.asyncio
    async def test_set_and_get_string(self, redis_state: RedisStateAdapter):
        await redis_state.set("key", "value")
        assert await redis_state.get("key") == "value"

    @pytest.mark.asyncio
    async def test_set_and_get_dict(self, redis_state: RedisStateAdapter):
        await redis_state.set("key", {"nested": "value"})
        assert await redis_state.get("key") == {"nested": "value"}

    @pytest.mark.asyncio
    async def test_set_and_get_int(self, redis_state: RedisStateAdapter):
        await redis_state.set("key", 42)
        assert await redis_state.get("key") == 42

    @pytest.mark.asyncio
    async def test_set_and_get_list(self, redis_state: RedisStateAdapter):
        await redis_state.set("key", [1, 2, 3])
        assert await redis_state.get("key") == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_set_overwrites_existing_value(self, redis_state: RedisStateAdapter):
        await redis_state.set("key", "first")
        await redis_state.set("key", "second")
        assert await redis_state.get("key") == "second"

    @pytest.mark.asyncio
    async def test_delete_removes_key(self, redis_state: RedisStateAdapter):
        await redis_state.set("key", "value")
        await redis_state.delete("key")
        assert await redis_state.get("key") is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_is_noop(self, redis_state: RedisStateAdapter):
        await redis_state.delete("nonexistent")  # Should not raise

    @pytest.mark.asyncio
    async def test_set_with_ttl_expires(self, redis_state: RedisStateAdapter):
        await redis_state.set("key", "value", ttl_ms=1)
        time.sleep(0.005)
        assert await redis_state.get("key") is None

    @pytest.mark.asyncio
    async def test_set_with_ttl_available_before_expiry(self, redis_state: RedisStateAdapter):
        await redis_state.set("key", "value", ttl_ms=60_000)
        assert await redis_state.get("key") == "value"

    @pytest.mark.asyncio
    async def test_set_without_ttl_never_expires(self, redis_state: RedisStateAdapter):
        await redis_state.set("key", "value")
        time.sleep(0.005)
        assert await redis_state.get("key") == "value"


# ============================================================================
# setIfNotExists (atomic NX)
# ============================================================================


class TestRedisStateSetIfNotExists:
    """Atomic set-if-not-exists."""

    @pytest.mark.asyncio
    async def test_set_if_not_exists_when_missing(self, redis_state: RedisStateAdapter):
        result = await redis_state.set_if_not_exists("key", "value")
        assert result is True
        assert await redis_state.get("key") == "value"

    @pytest.mark.asyncio
    async def test_set_if_not_exists_when_exists(self, redis_state: RedisStateAdapter):
        await redis_state.set("key", "first")
        result = await redis_state.set_if_not_exists("key", "second")
        assert result is False
        assert await redis_state.get("key") == "first"

    @pytest.mark.asyncio
    async def test_set_if_not_exists_with_ttl(self, redis_state: RedisStateAdapter):
        result = await redis_state.set_if_not_exists("key", "value", ttl_ms=1)
        assert result is True
        time.sleep(0.005)
        assert await redis_state.get("key") is None

    @pytest.mark.asyncio
    async def test_set_if_not_exists_after_expired_key(self, redis_state: RedisStateAdapter):
        await redis_state.set("key", "old", ttl_ms=1)
        time.sleep(0.005)
        result = await redis_state.set_if_not_exists("key", "new")
        assert result is True
        assert await redis_state.get("key") == "new"


# ============================================================================
# Locks: acquire / release / extend / force
# ============================================================================


class TestRedisStateLocks:
    """Distributed locking via Lua scripts."""

    @pytest.mark.asyncio
    async def test_acquire_lock(self, redis_state: RedisStateAdapter):
        lock = await redis_state.acquire_lock("thread-1", 30_000)
        assert lock is not None
        assert lock.thread_id == "thread-1"
        assert lock.token.startswith("redis_")
        assert lock.expires_at > 0

    @pytest.mark.asyncio
    async def test_acquire_lock_fails_when_held(self, redis_state: RedisStateAdapter):
        lock1 = await redis_state.acquire_lock("thread-1", 30_000)
        assert lock1 is not None
        lock2 = await redis_state.acquire_lock("thread-1", 30_000)
        assert lock2 is None

    @pytest.mark.asyncio
    async def test_acquire_lock_succeeds_after_expiry(self, redis_state: RedisStateAdapter):
        lock1 = await redis_state.acquire_lock("thread-1", 1)
        assert lock1 is not None
        time.sleep(0.005)
        lock2 = await redis_state.acquire_lock("thread-1", 30_000)
        assert lock2 is not None

    @pytest.mark.asyncio
    async def test_release_lock_allows_reacquire(self, redis_state: RedisStateAdapter):
        lock = await redis_state.acquire_lock("thread-1", 30_000)
        assert lock is not None
        await redis_state.release_lock(lock)
        lock2 = await redis_state.acquire_lock("thread-1", 30_000)
        assert lock2 is not None

    @pytest.mark.asyncio
    async def test_release_lock_wrong_token_is_noop(self, redis_state: RedisStateAdapter):
        lock = await redis_state.acquire_lock("thread-1", 30_000)
        assert lock is not None

        fake_lock = Lock(thread_id="thread-1", token="wrong-token", expires_at=0)
        await redis_state.release_lock(fake_lock)

        # Original lock still held
        lock2 = await redis_state.acquire_lock("thread-1", 30_000)
        assert lock2 is None

    @pytest.mark.asyncio
    async def test_extend_lock(self, redis_state: RedisStateAdapter):
        lock = await redis_state.acquire_lock("thread-1", 100)
        assert lock is not None

        result = await redis_state.extend_lock(lock, 60_000)
        assert result is True

        time.sleep(0.15)
        # Lock still held because we extended
        lock2 = await redis_state.acquire_lock("thread-1", 30_000)
        assert lock2 is None

    @pytest.mark.asyncio
    async def test_extend_lock_wrong_token_fails(self, redis_state: RedisStateAdapter):
        lock = await redis_state.acquire_lock("thread-1", 30_000)
        assert lock is not None

        fake_lock = Lock(thread_id="thread-1", token="wrong-token", expires_at=0)
        result = await redis_state.extend_lock(fake_lock, 60_000)
        assert result is False

    @pytest.mark.asyncio
    async def test_extend_lock_after_expiry_fails(self, redis_state: RedisStateAdapter):
        lock = await redis_state.acquire_lock("thread-1", 1)
        assert lock is not None
        time.sleep(0.005)

        result = await redis_state.extend_lock(lock, 60_000)
        assert result is False

    @pytest.mark.asyncio
    async def test_force_release_lock(self, redis_state: RedisStateAdapter):
        lock = await redis_state.acquire_lock("thread-1", 30_000)
        assert lock is not None

        await redis_state.force_release_lock("thread-1")

        lock2 = await redis_state.acquire_lock("thread-1", 30_000)
        assert lock2 is not None

    @pytest.mark.asyncio
    async def test_force_release_nonexistent_is_noop(self, redis_state: RedisStateAdapter):
        await redis_state.force_release_lock("nonexistent")  # Should not raise

    @pytest.mark.asyncio
    async def test_independent_locks_per_thread(self, redis_state: RedisStateAdapter):
        lock1 = await redis_state.acquire_lock("thread-1", 30_000)
        lock2 = await redis_state.acquire_lock("thread-2", 30_000)
        assert lock1 is not None
        assert lock2 is not None
        assert lock1.token != lock2.token


# ============================================================================
# Lists: append with maxLength
# ============================================================================


class TestRedisStateLists:
    """List operations: append, get, max_length, TTL."""

    @pytest.mark.asyncio
    async def test_get_list_returns_empty_for_missing_key(self, redis_state: RedisStateAdapter):
        result = await redis_state.get_list("nonexistent")
        assert result == []

    @pytest.mark.asyncio
    async def test_append_and_get_list(self, redis_state: RedisStateAdapter):
        await redis_state.append_to_list("key", "a")
        await redis_state.append_to_list("key", "b")
        await redis_state.append_to_list("key", "c")
        result = await redis_state.get_list("key")
        assert result == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_append_with_max_length(self, redis_state: RedisStateAdapter):
        for i in range(5):
            await redis_state.append_to_list("key", i, max_length=3)
        result = await redis_state.get_list("key")
        assert result == [2, 3, 4]

    @pytest.mark.asyncio
    async def test_append_preserves_order(self, redis_state: RedisStateAdapter):
        for letter in ["x", "y", "z"]:
            await redis_state.append_to_list("key", letter)
        assert await redis_state.get_list("key") == ["x", "y", "z"]

    @pytest.mark.asyncio
    async def test_list_with_ttl_expires(self, redis_state: RedisStateAdapter):
        await redis_state.append_to_list("key", "a", ttl_ms=1)
        time.sleep(0.005)
        result = await redis_state.get_list("key")
        assert result == []

    @pytest.mark.asyncio
    async def test_list_with_ttl_available_before_expiry(self, redis_state: RedisStateAdapter):
        await redis_state.append_to_list("key", "a", ttl_ms=60_000)
        result = await redis_state.get_list("key")
        assert result == ["a"]

    @pytest.mark.asyncio
    async def test_list_with_dict_values(self, redis_state: RedisStateAdapter):
        await redis_state.append_to_list("key", {"id": 1})
        await redis_state.append_to_list("key", {"id": 2})
        result = await redis_state.get_list("key")
        assert result == [{"id": 1}, {"id": 2}]

    @pytest.mark.asyncio
    async def test_max_length_one(self, redis_state: RedisStateAdapter):
        await redis_state.append_to_list("key", "a", max_length=1)
        await redis_state.append_to_list("key", "b", max_length=1)
        result = await redis_state.get_list("key")
        assert result == ["b"]


# ============================================================================
# Queues: enqueue / dequeue FIFO
# ============================================================================


class TestRedisStateQueues:
    """Queue operations: enqueue, dequeue, queue_depth."""

    @pytest.mark.asyncio
    async def test_queue_depth_returns_zero_for_empty_queue(self, redis_state: RedisStateAdapter):
        assert await redis_state.queue_depth("thread-1") == 0

    @pytest.mark.asyncio
    async def test_enqueue_and_dequeue(self, redis_state: RedisStateAdapter):
        entry = _make_queue_entry("msg-1")
        depth = await redis_state.enqueue("thread-1", entry, max_size=10)
        assert depth == 1

        result = await redis_state.dequeue("thread-1")
        assert result is not None
        assert result.message["id"] == "msg-1"

    @pytest.mark.asyncio
    async def test_dequeue_returns_none_for_empty_queue(self, redis_state: RedisStateAdapter):
        result = await redis_state.dequeue("thread-1")
        assert result is None

    @pytest.mark.asyncio
    async def test_enqueue_fifo_order(self, redis_state: RedisStateAdapter):
        for i in range(3):
            await redis_state.enqueue("thread-1", _make_queue_entry(f"msg-{i}"), max_size=10)

        results = []
        for _ in range(3):
            entry = await redis_state.dequeue("thread-1")
            assert entry is not None
            results.append(entry.message["id"])
        assert results == ["msg-0", "msg-1", "msg-2"]

    @pytest.mark.asyncio
    async def test_enqueue_respects_max_size(self, redis_state: RedisStateAdapter):
        for i in range(5):
            await redis_state.enqueue("thread-1", _make_queue_entry(f"msg-{i}"), max_size=3)

        assert await redis_state.queue_depth("thread-1") == 3

        first = await redis_state.dequeue("thread-1")
        assert first is not None
        assert first.message["id"] == "msg-2"

    @pytest.mark.asyncio
    async def test_queue_depth_after_operations(self, redis_state: RedisStateAdapter):
        await redis_state.enqueue("thread-1", _make_queue_entry("msg-0"), max_size=10)
        await redis_state.enqueue("thread-1", _make_queue_entry("msg-1"), max_size=10)
        assert await redis_state.queue_depth("thread-1") == 2

        await redis_state.dequeue("thread-1")
        assert await redis_state.queue_depth("thread-1") == 1

        await redis_state.dequeue("thread-1")
        assert await redis_state.queue_depth("thread-1") == 0

    @pytest.mark.asyncio
    async def test_independent_queues_per_thread(self, redis_state: RedisStateAdapter):
        await redis_state.enqueue("thread-1", _make_queue_entry("msg-a"), max_size=10)
        await redis_state.enqueue("thread-2", _make_queue_entry("msg-b"), max_size=10)

        assert await redis_state.queue_depth("thread-1") == 1
        assert await redis_state.queue_depth("thread-2") == 1

        e1 = await redis_state.dequeue("thread-1")
        assert e1 is not None
        assert e1.message["id"] == "msg-a"

        e2 = await redis_state.dequeue("thread-2")
        assert e2 is not None
        assert e2.message["id"] == "msg-b"


# ============================================================================
# Subscriptions
# ============================================================================


class TestRedisStateSubscriptions:
    """Subscription operations: subscribe, unsubscribe, is_subscribed."""

    @pytest.mark.asyncio
    async def test_is_subscribed_returns_false_initially(self, redis_state: RedisStateAdapter):
        assert await redis_state.is_subscribed("thread-1") is False

    @pytest.mark.asyncio
    async def test_subscribe_and_check(self, redis_state: RedisStateAdapter):
        await redis_state.subscribe("thread-1")
        assert await redis_state.is_subscribed("thread-1") is True

    @pytest.mark.asyncio
    async def test_unsubscribe(self, redis_state: RedisStateAdapter):
        await redis_state.subscribe("thread-1")
        await redis_state.unsubscribe("thread-1")
        assert await redis_state.is_subscribed("thread-1") is False

    @pytest.mark.asyncio
    async def test_unsubscribe_nonexistent_is_noop(self, redis_state: RedisStateAdapter):
        await redis_state.unsubscribe("nonexistent")  # Should not raise

    @pytest.mark.asyncio
    async def test_subscribe_is_idempotent(self, redis_state: RedisStateAdapter):
        await redis_state.subscribe("thread-1")
        await redis_state.subscribe("thread-1")
        assert await redis_state.is_subscribed("thread-1") is True

    @pytest.mark.asyncio
    async def test_independent_subscriptions(self, redis_state: RedisStateAdapter):
        await redis_state.subscribe("thread-1")
        await redis_state.subscribe("thread-2")
        assert await redis_state.is_subscribed("thread-1") is True
        assert await redis_state.is_subscribed("thread-2") is True
        assert await redis_state.is_subscribed("thread-3") is False

    @pytest.mark.asyncio
    async def test_unsubscribe_one_does_not_affect_other(self, redis_state: RedisStateAdapter):
        await redis_state.subscribe("thread-1")
        await redis_state.subscribe("thread-2")
        await redis_state.unsubscribe("thread-1")
        assert await redis_state.is_subscribed("thread-1") is False
        assert await redis_state.is_subscribed("thread-2") is True


# ============================================================================
# Key prefix isolation
# ============================================================================


class TestRedisStateKeyPrefix:
    """Verify key prefix isolation between adapter instances."""

    @pytest.mark.asyncio
    async def test_different_prefixes_are_isolated(self, mock_redis: MockRedis):
        a = RedisStateAdapter(client=mock_redis, key_prefix="prefix-a")
        b = RedisStateAdapter(client=mock_redis, key_prefix="prefix-b")
        await a.connect()
        await b.connect()

        await a.set("shared-key", "value-a")
        await b.set("shared-key", "value-b")

        assert await a.get("shared-key") == "value-a"
        assert await b.get("shared-key") == "value-b"

        await a.disconnect()
        await b.disconnect()
