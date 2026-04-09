"""Tests for PostgresStateAdapter using a MockAsyncpgPool.

Since we cannot assume a running PostgreSQL instance, a MockAsyncpgPool
class simulates the asyncpg pool interface using in-memory dicts to
represent table storage.  SQL queries are pattern-matched to decide
which in-memory operation to perform.
"""

from __future__ import annotations

import datetime as _dt
import re
import time
from typing import Any

import pytest

from chat_sdk.errors import StateNotConnectedError
from chat_sdk.state.postgres import PostgresStateAdapter
from chat_sdk.types import Lock, QueueEntry

# ============================================================================
# MockAsyncpgPool
# ============================================================================


class _Record(dict):
    """Dict subclass that supports both dict-style and attribute access."""

    def __getattr__(self, key: str) -> Any:
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key) from None


class MockAsyncpgPool:
    """In-memory simulation of an asyncpg connection pool.

    Implements execute, fetch, fetchrow, fetchval by pattern-matching
    the SQL queries used by PostgresStateAdapter and operating on
    in-memory dicts that represent each table.

    Tables simulated:
    - chat_state_subscriptions: {(key_prefix, thread_id)}
    - chat_state_locks: {(key_prefix, thread_id): {token, expires_at, updated_at}}
    - chat_state_cache: {(key_prefix, cache_key): {value, expires_at, updated_at}}
    - chat_state_lists: {(key_prefix, list_key): [{seq, value, expires_at}]}
    - chat_state_queues: {(key_prefix, thread_id): [{seq, value, expires_at}]}
    """

    def __init__(self) -> None:
        self.subscriptions: set[tuple[str, str]] = set()
        self.locks: dict[tuple[str, str], dict[str, Any]] = {}
        self.cache: dict[tuple[str, str], dict[str, Any]] = {}
        self.lists: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.queues: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._seq_counter = 0
        self._closed = False
        self.executed_queries: list[str] = []

    def _next_seq(self) -> int:
        self._seq_counter += 1
        return self._seq_counter

    def _now(self) -> _dt.datetime:
        return _dt.datetime.now(_dt.UTC)

    # -- lifecycle -------------------------------------------------------------

    async def close(self) -> None:
        self._closed = True

    # -- connection acquisition (for transactions) -----------------------------

    def acquire(self) -> _MockConnectionCtx:
        """Return an async context manager that yields self (acts as connection)."""
        return _MockConnectionCtx(self)

    def transaction(self) -> _MockTransactionCtx:
        """Return a no-op async context manager for transaction blocks."""
        return _MockTransactionCtx()

    # -- query dispatch --------------------------------------------------------

    async def execute(self, query: str, *args: Any) -> str:
        self.executed_queries.append(query)
        q = _normalise(query)

        # Schema DDL: CREATE TABLE / CREATE INDEX
        if q.startswith("create table") or q.startswith("create index"):
            return "CREATE"

        # -- subscriptions --
        if "insert into chat_state_subscriptions" in q:
            key_prefix, thread_id = args[0], args[1]
            self.subscriptions.add((key_prefix, thread_id))
            return "INSERT 0 1"

        if "delete from chat_state_subscriptions" in q:
            key_prefix, thread_id = args[0], args[1]
            self.subscriptions.discard((key_prefix, thread_id))
            return "DELETE 1"

        # -- locks --
        if "delete from chat_state_locks" in q and "token" in q:
            # release_lock (with token check)
            key_prefix, thread_id, token = args[0], args[1], args[2]
            lock_key = (key_prefix, thread_id)
            lock = self.locks.get(lock_key)
            if lock and lock["token"] == token:
                del self.locks[lock_key]
                return "DELETE 1"
            return "DELETE 0"

        if "delete from chat_state_locks" in q:
            # force_release_lock
            key_prefix, thread_id = args[0], args[1]
            self.locks.pop((key_prefix, thread_id), None)
            return "DELETE 1"

        if "update chat_state_locks" in q:
            # extend_lock
            ttl_ms, key_prefix, thread_id, token = args[0], args[1], args[2], args[3]
            lock_key = (key_prefix, thread_id)
            lock = self.locks.get(lock_key)
            if lock and lock["token"] == token and lock["expires_at"] > self._now():
                lock["expires_at"] = self._now() + _dt.timedelta(milliseconds=ttl_ms)
                lock["updated_at"] = self._now()
                return "UPDATE 1"
            return "UPDATE 0"

        # -- cache --
        if "insert into chat_state_cache" in q and "on conflict" in q and "do update" in q:
            # set (upsert)
            key_prefix, cache_key, value, expires_at = args[0], args[1], args[2], args[3]
            self.cache[(key_prefix, cache_key)] = {
                "value": value,
                "expires_at": expires_at,
                "updated_at": self._now(),
            }
            return "INSERT 0 1"

        if "insert into chat_state_cache" in q and "do nothing" in q:
            # set_if_not_exists
            key_prefix, cache_key, value, expires_at = args[0], args[1], args[2], args[3]
            ck = (key_prefix, cache_key)
            if ck in self.cache:
                entry = self.cache[ck]
                # If key exists but expired, allow the insert
                if entry["expires_at"] is not None and entry["expires_at"] <= self._now():
                    self.cache[ck] = {
                        "value": value,
                        "expires_at": expires_at,
                        "updated_at": self._now(),
                    }
                    return "INSERT 0 1"
                return "INSERT 0 0"
            self.cache[ck] = {
                "value": value,
                "expires_at": expires_at,
                "updated_at": self._now(),
            }
            return "INSERT 0 1"

        if "delete from chat_state_cache" in q and "expires_at" in q:
            # Opportunistic cleanup of expired entry
            key_prefix, cache_key = args[0], args[1]
            ck = (key_prefix, cache_key)
            entry = self.cache.get(ck)
            if entry and entry["expires_at"] is not None and entry["expires_at"] <= self._now():
                del self.cache[ck]
            return "DELETE 1"

        if "delete from chat_state_cache" in q:
            key_prefix, cache_key = args[0], args[1]
            self.cache.pop((key_prefix, cache_key), None)
            return "DELETE 1"

        # -- lists --
        if "insert into chat_state_lists" in q:
            key_prefix, list_key, value, expires_at = args[0], args[1], args[2], args[3]
            lk = (key_prefix, list_key)
            if lk not in self.lists:
                self.lists[lk] = []
            self.lists[lk].append(
                {
                    "seq": self._next_seq(),
                    "value": value,
                    "expires_at": expires_at,
                }
            )
            return "INSERT 0 1"

        if "delete from chat_state_lists" in q and "offset" in q:
            # Trim overflow
            key_prefix, list_key, max_length = args[0], args[1], args[2]
            lk = (key_prefix, list_key)
            items = self.lists.get(lk, [])
            if len(items) > max_length:
                overflow = len(items) - max_length
                self.lists[lk] = items[overflow:]
            return "DELETE"

        if "update chat_state_lists" in q:
            # Update TTL on all entries
            key_prefix, list_key, expires_at = args[0], args[1], args[2]
            lk = (key_prefix, list_key)
            for item in self.lists.get(lk, []):
                item["expires_at"] = expires_at
            return "UPDATE"

        # -- queues --
        if "delete from chat_state_queues" in q and "expires_at <= now()" in q and "seq in" not in q:
            # Purge expired entries
            key_prefix, thread_id = args[0], args[1]
            qk = (key_prefix, thread_id)
            now = self._now()
            self.queues[qk] = [e for e in self.queues.get(qk, []) if e["expires_at"] > now]
            return "DELETE"

        if "insert into chat_state_queues" in q:
            key_prefix, thread_id, value, expires_at = args[0], args[1], args[2], args[3]
            qk = (key_prefix, thread_id)
            if qk not in self.queues:
                self.queues[qk] = []
            self.queues[qk].append(
                {
                    "seq": self._next_seq(),
                    "value": value,
                    "expires_at": expires_at,
                }
            )
            return "INSERT 0 1"

        if "delete from chat_state_queues" in q and "offset" in q:
            # Trim overflow (keep newest max_size)
            key_prefix, thread_id, max_size = args[0], args[1], args[2]
            qk = (key_prefix, thread_id)
            now = self._now()
            non_expired = [e for e in self.queues.get(qk, []) if e["expires_at"] > now]
            if len(non_expired) > max_size:
                overflow = len(non_expired) - max_size
                # Remove the oldest 'overflow' entries
                to_remove_seqs = {e["seq"] for e in non_expired[:overflow]}
                self.queues[qk] = [e for e in self.queues.get(qk, []) if e["seq"] not in to_remove_seqs]
            return "DELETE"

        return "OK"

    async def fetch(self, query: str, *args: Any) -> list[_Record]:
        self.executed_queries.append(query)
        q = _normalise(query)

        if "from chat_state_lists" in q:
            key_prefix, list_key = args[0], args[1]
            lk = (key_prefix, list_key)
            now = self._now()
            items = self.lists.get(lk, [])
            result = []
            for item in sorted(items, key=lambda x: x["seq"]):
                if item["expires_at"] is None or item["expires_at"] > now:
                    result.append(_Record({"value": item["value"]}))
            return result

        return []

    async def fetchrow(self, query: str, *args: Any) -> _Record | None:
        self.executed_queries.append(query)
        q = _normalise(query)

        # -- subscriptions --
        if "from chat_state_subscriptions" in q:
            key_prefix, thread_id = args[0], args[1]
            if (key_prefix, thread_id) in self.subscriptions:
                return _Record({"_": 1})
            return None

        # -- locks: acquire (atomic upsert: INSERT ... ON CONFLICT DO UPDATE WHERE expired) --
        if "insert into chat_state_locks" in q:
            key_prefix, thread_id, token = args[0], args[1], args[2]
            ttl_ms = args[3]
            lock_key = (key_prefix, thread_id)
            expires_at = self._now() + _dt.timedelta(milliseconds=ttl_ms)
            existing = self.locks.get(lock_key)

            if existing is None:
                # No existing row -- INSERT succeeds
                self.locks[lock_key] = {
                    "token": token,
                    "expires_at": expires_at,
                    "updated_at": self._now(),
                }
                return _Record(
                    {
                        "thread_id": thread_id,
                        "token": token,
                        "expires_at": expires_at,
                    }
                )

            # Row exists -- DO UPDATE fires only when expired
            if existing["expires_at"] <= self._now():
                self.locks[lock_key] = {
                    "token": token,
                    "expires_at": expires_at,
                    "updated_at": self._now(),
                }
                return _Record(
                    {
                        "thread_id": thread_id,
                        "token": token,
                        "expires_at": expires_at,
                    }
                )

            # Lock is still held -- DO UPDATE WHERE fails, RETURNING not fired
            return None

        # -- cache: get (SELECT value FROM chat_state_cache) --
        if "select value from chat_state_cache" in q:
            key_prefix, cache_key = args[0], args[1]
            ck = (key_prefix, cache_key)
            entry = self.cache.get(ck)
            if entry is None:
                return None
            if entry["expires_at"] is not None and entry["expires_at"] <= self._now():
                return None
            return _Record({"value": entry["value"]})

        # -- queues: dequeue (DELETE ... RETURNING value) --
        if "delete from chat_state_queues" in q and "returning value" in q:
            key_prefix, thread_id = args[0], args[1]
            qk = (key_prefix, thread_id)
            now = self._now()
            items = sorted(self.queues.get(qk, []), key=lambda x: x["seq"])
            for item in items:
                if item["expires_at"] > now:
                    self.queues[qk] = [e for e in self.queues[qk] if e["seq"] != item["seq"]]
                    return _Record({"value": item["value"]})
            return None

        return None

    async def fetchval(self, query: str, *args: Any) -> Any:
        self.executed_queries.append(query)
        q = _normalise(query)

        if q.strip() == "select 1":
            return 1

        if "select count(*) from chat_state_queues" in q:
            key_prefix, thread_id = args[0], args[1]
            qk = (key_prefix, thread_id)
            now = self._now()
            count = sum(1 for e in self.queues.get(qk, []) if e["expires_at"] > now)
            return count

        return None


class _MockConnectionCtx:
    """Async context manager that yields the pool as a 'connection'."""

    def __init__(self, pool: MockAsyncpgPool) -> None:
        self._pool = pool

    async def __aenter__(self) -> MockAsyncpgPool:
        return self._pool

    async def __aexit__(self, *exc: Any) -> None:
        pass


class _MockTransactionCtx:
    """No-op async context manager for transaction blocks."""

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: Any) -> None:
        pass


def _normalise(sql: str) -> str:
    """Collapse whitespace and lowercase for pattern matching."""
    return re.sub(r"\s+", " ", sql).strip().lower()


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def mock_pool() -> MockAsyncpgPool:
    return MockAsyncpgPool()


@pytest.fixture
async def pg_state(mock_pool: MockAsyncpgPool) -> PostgresStateAdapter:
    adapter = PostgresStateAdapter(pool=mock_pool, key_prefix="test")
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
# Table auto-creation on connect
# ============================================================================


class TestPostgresStateConnect:
    """Connection lifecycle and schema initialisation."""

    @pytest.mark.asyncio
    async def test_connect_creates_tables(self, mock_pool: MockAsyncpgPool):
        adapter = PostgresStateAdapter(pool=mock_pool, key_prefix="test")
        await adapter.connect()

        create_stmts = [q for q in mock_pool.executed_queries if "create" in q.lower()]
        # Should have created tables and indexes
        assert len(create_stmts) >= 5  # 5 tables + indexes
        await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_connect_is_idempotent(self, mock_pool: MockAsyncpgPool):
        adapter = PostgresStateAdapter(pool=mock_pool, key_prefix="test")
        await adapter.connect()
        query_count = len(mock_pool.executed_queries)
        await adapter.connect()  # Should not re-create
        assert len(mock_pool.executed_queries) == query_count
        await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_disconnect_sets_connected_false(self, mock_pool: MockAsyncpgPool):
        adapter = PostgresStateAdapter(pool=mock_pool, key_prefix="test")
        await adapter.connect()
        await adapter.disconnect()
        assert adapter._connected is False

    @pytest.mark.asyncio
    async def test_disconnect_without_connect_is_noop(self, mock_pool: MockAsyncpgPool):
        adapter = PostgresStateAdapter(pool=mock_pool, key_prefix="test")
        # Disconnect before connect should complete without raising
        await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_operations_fail_before_connect(self, mock_pool: MockAsyncpgPool):
        adapter = PostgresStateAdapter(pool=mock_pool, key_prefix="test")
        with pytest.raises(StateNotConnectedError, match="not connected"):
            await adapter.get("key")

    @pytest.mark.asyncio
    async def test_get_pool_returns_underlying_pool(self, mock_pool: MockAsyncpgPool):
        adapter = PostgresStateAdapter(pool=mock_pool, key_prefix="test")
        assert adapter.get_pool() is mock_pool

    @pytest.mark.asyncio
    async def test_injected_pool_not_closed_on_disconnect(self, mock_pool: MockAsyncpgPool):
        adapter = PostgresStateAdapter(pool=mock_pool, key_prefix="test")
        await adapter.connect()
        await adapter.disconnect()
        assert mock_pool._closed is False

    @pytest.mark.asyncio
    async def test_url_required_when_no_pool(self):
        with pytest.raises(ValueError, match="Postgres url is required"):
            PostgresStateAdapter(key_prefix="test")


# ============================================================================
# Key/Value CRUD with TTL
# ============================================================================


class TestPostgresStateKV:
    """Key-value cache operations."""

    @pytest.mark.asyncio
    async def test_get_returns_none_for_missing_key(self, pg_state: PostgresStateAdapter):
        assert await pg_state.get("nonexistent") is None

    @pytest.mark.asyncio
    async def test_set_and_get_string(self, pg_state: PostgresStateAdapter):
        await pg_state.set("key", "value")
        assert await pg_state.get("key") == "value"

    @pytest.mark.asyncio
    async def test_set_and_get_dict(self, pg_state: PostgresStateAdapter):
        await pg_state.set("key", {"nested": "value"})
        assert await pg_state.get("key") == {"nested": "value"}

    @pytest.mark.asyncio
    async def test_set_and_get_int(self, pg_state: PostgresStateAdapter):
        await pg_state.set("key", 42)
        assert await pg_state.get("key") == 42

    @pytest.mark.asyncio
    async def test_set_and_get_list(self, pg_state: PostgresStateAdapter):
        await pg_state.set("key", [1, 2, 3])
        assert await pg_state.get("key") == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_set_overwrites_existing(self, pg_state: PostgresStateAdapter):
        await pg_state.set("key", "first")
        await pg_state.set("key", "second")
        assert await pg_state.get("key") == "second"

    @pytest.mark.asyncio
    async def test_delete_removes_key(self, pg_state: PostgresStateAdapter):
        await pg_state.set("key", "value")
        await pg_state.delete("key")
        assert await pg_state.get("key") is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_is_noop(self, pg_state: PostgresStateAdapter):
        await pg_state.delete("nonexistent")
        assert await pg_state.get("nonexistent") is None


# ============================================================================
# Expired row cleanup
# ============================================================================


class TestPostgresStateTTL:
    """TTL expiry and expired row cleanup."""

    @pytest.mark.asyncio
    async def test_set_with_ttl_expires(self, pg_state: PostgresStateAdapter):
        await pg_state.set("key", "value", ttl_ms=1)
        time.sleep(0.005)
        assert await pg_state.get("key") is None

    @pytest.mark.asyncio
    async def test_set_with_ttl_available_before_expiry(self, pg_state: PostgresStateAdapter):
        await pg_state.set("key", "value", ttl_ms=60_000)
        assert await pg_state.get("key") == "value"

    @pytest.mark.asyncio
    async def test_set_without_ttl_never_expires(self, pg_state: PostgresStateAdapter):
        await pg_state.set("key", "value")
        time.sleep(0.005)
        assert await pg_state.get("key") == "value"

    @pytest.mark.asyncio
    async def test_expired_key_cleaned_on_get(self, pg_state: PostgresStateAdapter, mock_pool: MockAsyncpgPool):
        """When get() returns None for an expired key, the adapter runs an opportunistic DELETE."""
        await pg_state.set("key", "value", ttl_ms=1)
        time.sleep(0.005)
        await pg_state.get("key")

        # After the get, the expired row should have been cleaned up
        delete_queries = [q for q in mock_pool.executed_queries if "delete from chat_state_cache" in q.lower()]
        assert len(delete_queries) >= 1


# ============================================================================
# setIfNotExists
# ============================================================================


class TestPostgresStateSetIfNotExists:
    """Atomic set-if-not-exists."""

    @pytest.mark.asyncio
    async def test_succeeds_when_key_missing(self, pg_state: PostgresStateAdapter):
        result = await pg_state.set_if_not_exists("key", "value")
        assert result is True
        assert await pg_state.get("key") == "value"

    @pytest.mark.asyncio
    async def test_fails_when_key_exists(self, pg_state: PostgresStateAdapter):
        await pg_state.set("key", "first")
        result = await pg_state.set_if_not_exists("key", "second")
        assert result is False
        assert await pg_state.get("key") == "first"

    @pytest.mark.asyncio
    async def test_with_ttl(self, pg_state: PostgresStateAdapter):
        result = await pg_state.set_if_not_exists("key", "value", ttl_ms=60_000)
        assert result is True
        assert await pg_state.get("key") == "value"

    @pytest.mark.asyncio
    async def test_succeeds_after_expired_key(self, pg_state: PostgresStateAdapter):
        await pg_state.set("key", "old", ttl_ms=1)
        time.sleep(0.005)
        # The key is expired; set_if_not_exists should succeed
        result = await pg_state.set_if_not_exists("key", "new")
        assert result is True
        assert await pg_state.get("key") == "new"


# ============================================================================
# Lock contention
# ============================================================================


class TestPostgresStateLocks:
    """Lock acquire/release/extend/force."""

    @pytest.mark.asyncio
    async def test_acquire_lock(self, pg_state: PostgresStateAdapter):
        lock = await pg_state.acquire_lock("thread-1", 30_000)
        assert lock is not None
        assert lock.thread_id == "thread-1"
        assert lock.token.startswith("pg_")
        assert lock.expires_at > 0

    @pytest.mark.asyncio
    async def test_acquire_lock_fails_when_held(self, pg_state: PostgresStateAdapter):
        lock1 = await pg_state.acquire_lock("thread-1", 30_000)
        assert lock1 is not None
        lock2 = await pg_state.acquire_lock("thread-1", 30_000)
        assert lock2 is None

    @pytest.mark.asyncio
    async def test_acquire_lock_succeeds_when_expired(self, pg_state: PostgresStateAdapter):
        lock1 = await pg_state.acquire_lock("thread-1", 1)
        assert lock1 is not None
        time.sleep(0.005)
        lock2 = await pg_state.acquire_lock("thread-1", 30_000)
        assert lock2 is not None
        assert lock2.thread_id == "thread-1"
        assert lock2.token.startswith("pg_")
        assert lock2.token != lock1.token  # New lock should have a fresh token

    @pytest.mark.asyncio
    async def test_release_lock_correct_token(self, pg_state: PostgresStateAdapter):
        lock = await pg_state.acquire_lock("thread-1", 30_000)
        assert lock is not None
        await pg_state.release_lock(lock)

        lock2 = await pg_state.acquire_lock("thread-1", 30_000)
        assert lock2 is not None
        assert lock2.thread_id == "thread-1"
        assert lock2.token != lock.token  # New lock after release gets a fresh token

    @pytest.mark.asyncio
    async def test_release_lock_wrong_token(self, pg_state: PostgresStateAdapter):
        lock = await pg_state.acquire_lock("thread-1", 30_000)
        assert lock is not None

        fake_lock = Lock(thread_id="thread-1", token="wrong-token", expires_at=0)
        await pg_state.release_lock(fake_lock)

        # Original lock still held
        lock2 = await pg_state.acquire_lock("thread-1", 30_000)
        assert lock2 is None

    @pytest.mark.asyncio
    async def test_extend_lock(self, pg_state: PostgresStateAdapter):
        lock = await pg_state.acquire_lock("thread-1", 100)
        assert lock is not None
        result = await pg_state.extend_lock(lock, 60_000)
        assert result is True

        # Lock should still be held after original TTL
        time.sleep(0.15)
        lock2 = await pg_state.acquire_lock("thread-1", 30_000)
        assert lock2 is None

    @pytest.mark.asyncio
    async def test_extend_lock_wrong_token_fails(self, pg_state: PostgresStateAdapter):
        lock = await pg_state.acquire_lock("thread-1", 30_000)
        assert lock is not None

        fake_lock = Lock(thread_id="thread-1", token="wrong-token", expires_at=0)
        result = await pg_state.extend_lock(fake_lock, 60_000)
        assert result is False

    @pytest.mark.asyncio
    async def test_extend_lock_after_expiry_fails(self, pg_state: PostgresStateAdapter):
        lock = await pg_state.acquire_lock("thread-1", 1)
        assert lock is not None
        time.sleep(0.005)

        result = await pg_state.extend_lock(lock, 60_000)
        assert result is False

    @pytest.mark.asyncio
    async def test_force_release_lock(self, pg_state: PostgresStateAdapter):
        lock = await pg_state.acquire_lock("thread-1", 30_000)
        assert lock is not None
        await pg_state.force_release_lock("thread-1")

        lock2 = await pg_state.acquire_lock("thread-1", 30_000)
        assert lock2 is not None
        assert lock2.thread_id == "thread-1"
        assert lock2.token != lock.token  # New lock after force release gets a fresh token

    @pytest.mark.asyncio
    async def test_force_release_nonexistent_is_noop(self, pg_state: PostgresStateAdapter):
        await pg_state.force_release_lock("nonexistent")
        # Can still acquire a lock on the same thread after force-releasing a nonexistent one
        lock = await pg_state.acquire_lock("nonexistent", 30_000)
        assert lock is not None
        assert lock.thread_id == "nonexistent"

    @pytest.mark.asyncio
    async def test_independent_locks_per_thread(self, pg_state: PostgresStateAdapter):
        lock1 = await pg_state.acquire_lock("thread-1", 30_000)
        lock2 = await pg_state.acquire_lock("thread-2", 30_000)
        assert lock1 is not None
        assert lock2 is not None
        assert lock1.token != lock2.token

    @pytest.mark.asyncio
    async def test_acquire_lock_uses_single_atomic_upsert(
        self, pg_state: PostgresStateAdapter, mock_pool: MockAsyncpgPool
    ):
        """Verify acquire_lock issues exactly one SQL statement (atomic upsert).

        The old two-step approach (INSERT ... DO NOTHING then UPDATE ... WHERE
        expired) had a TOCTOU race: two callers could both see the INSERT fail,
        then both attempt the UPDATE. The fix uses a single INSERT ... ON
        CONFLICT DO UPDATE WHERE expired, which is atomic because Postgres
        acquires a row lock on the conflicting row.
        """
        # Clear any queries from fixture setup (connect / schema creation)
        mock_pool.executed_queries.clear()

        # First acquire: new row inserted
        lock1 = await pg_state.acquire_lock("race-thread", 30_000)
        assert lock1 is not None

        # Should have issued exactly one query for the lock acquisition
        lock_queries = [q for q in mock_pool.executed_queries if "chat_state_locks" in q.lower()]
        assert len(lock_queries) == 1, f"Expected 1 atomic upsert query, got {len(lock_queries)}: {lock_queries}"

        # Second acquire while held: should fail in single query too
        mock_pool.executed_queries.clear()
        lock2 = await pg_state.acquire_lock("race-thread", 30_000)
        assert lock2 is None

        lock_queries = [q for q in mock_pool.executed_queries if "chat_state_locks" in q.lower()]
        assert len(lock_queries) == 1, f"Expected 1 atomic upsert query for contended lock, got {len(lock_queries)}"

        # Third acquire after expiry: should succeed in single query
        mock_pool.executed_queries.clear()
        time.sleep(0.005)
        # Force-expire the lock for testing
        lock_key = ("test", "race-thread")
        mock_pool.locks[lock_key]["expires_at"] = _dt.datetime.now(_dt.UTC) - _dt.timedelta(seconds=1)

        lock3 = await pg_state.acquire_lock("race-thread", 30_000)
        assert lock3 is not None

        lock_queries = [q for q in mock_pool.executed_queries if "chat_state_locks" in q.lower()]
        assert len(lock_queries) == 1, f"Expected 1 atomic upsert query for expired lock, got {len(lock_queries)}"


# ============================================================================
# List operations
# ============================================================================


class TestPostgresStateLists:
    """List operations: append, get, max_length, TTL."""

    @pytest.mark.asyncio
    async def test_get_list_returns_empty_for_missing_key(self, pg_state: PostgresStateAdapter):
        result = await pg_state.get_list("nonexistent")
        assert result == []

    @pytest.mark.asyncio
    async def test_append_and_get_list(self, pg_state: PostgresStateAdapter):
        await pg_state.append_to_list("key", "a")
        await pg_state.append_to_list("key", "b")
        await pg_state.append_to_list("key", "c")
        result = await pg_state.get_list("key")
        assert result == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_append_with_max_length(self, pg_state: PostgresStateAdapter):
        for i in range(5):
            await pg_state.append_to_list("key", i, max_length=3)
        result = await pg_state.get_list("key")
        assert result == [2, 3, 4]

    @pytest.mark.asyncio
    async def test_append_preserves_order(self, pg_state: PostgresStateAdapter):
        for letter in ["x", "y", "z"]:
            await pg_state.append_to_list("key", letter)
        assert await pg_state.get_list("key") == ["x", "y", "z"]

    @pytest.mark.asyncio
    async def test_list_with_ttl_expires(self, pg_state: PostgresStateAdapter):
        await pg_state.append_to_list("key", "a", ttl_ms=1)
        time.sleep(0.005)
        result = await pg_state.get_list("key")
        assert result == []

    @pytest.mark.asyncio
    async def test_list_with_ttl_available_before_expiry(self, pg_state: PostgresStateAdapter):
        await pg_state.append_to_list("key", "a", ttl_ms=60_000)
        result = await pg_state.get_list("key")
        assert result == ["a"]

    @pytest.mark.asyncio
    async def test_list_with_dict_values(self, pg_state: PostgresStateAdapter):
        await pg_state.append_to_list("key", {"id": 1})
        await pg_state.append_to_list("key", {"id": 2})
        result = await pg_state.get_list("key")
        assert result == [{"id": 1}, {"id": 2}]

    @pytest.mark.asyncio
    async def test_max_length_one(self, pg_state: PostgresStateAdapter):
        await pg_state.append_to_list("key", "a", max_length=1)
        await pg_state.append_to_list("key", "b", max_length=1)
        result = await pg_state.get_list("key")
        assert result == ["b"]


# ============================================================================
# Queue FIFO ordering
# ============================================================================


class TestPostgresStateQueues:
    """Queue operations: enqueue, dequeue, queue_depth."""

    @pytest.mark.asyncio
    async def test_queue_depth_returns_zero_for_empty_queue(self, pg_state: PostgresStateAdapter):
        assert await pg_state.queue_depth("thread-1") == 0

    @pytest.mark.asyncio
    async def test_enqueue_and_dequeue(self, pg_state: PostgresStateAdapter):
        entry = _make_queue_entry("msg-1")
        depth = await pg_state.enqueue("thread-1", entry, max_size=10)
        assert depth == 1

        result = await pg_state.dequeue("thread-1")
        assert result is not None
        assert result.message["id"] == "msg-1"

    @pytest.mark.asyncio
    async def test_dequeue_returns_none_for_empty_queue(self, pg_state: PostgresStateAdapter):
        result = await pg_state.dequeue("thread-1")
        assert result is None

    @pytest.mark.asyncio
    async def test_enqueue_fifo_order(self, pg_state: PostgresStateAdapter):
        for i in range(3):
            await pg_state.enqueue("thread-1", _make_queue_entry(f"msg-{i}"), max_size=10)

        results = []
        for _ in range(3):
            entry = await pg_state.dequeue("thread-1")
            assert entry is not None
            results.append(entry.message["id"])
        assert results == ["msg-0", "msg-1", "msg-2"]

    @pytest.mark.asyncio
    async def test_enqueue_respects_max_size(self, pg_state: PostgresStateAdapter):
        for i in range(5):
            await pg_state.enqueue("thread-1", _make_queue_entry(f"msg-{i}"), max_size=3)

        assert await pg_state.queue_depth("thread-1") == 3

        first = await pg_state.dequeue("thread-1")
        assert first is not None
        assert first.message["id"] == "msg-2"

    @pytest.mark.asyncio
    async def test_queue_depth_after_operations(self, pg_state: PostgresStateAdapter):
        await pg_state.enqueue("thread-1", _make_queue_entry("msg-0"), max_size=10)
        await pg_state.enqueue("thread-1", _make_queue_entry("msg-1"), max_size=10)
        assert await pg_state.queue_depth("thread-1") == 2

        await pg_state.dequeue("thread-1")
        assert await pg_state.queue_depth("thread-1") == 1

        await pg_state.dequeue("thread-1")
        assert await pg_state.queue_depth("thread-1") == 0

    @pytest.mark.asyncio
    async def test_independent_queues_per_thread(self, pg_state: PostgresStateAdapter):
        await pg_state.enqueue("thread-1", _make_queue_entry("msg-a"), max_size=10)
        await pg_state.enqueue("thread-2", _make_queue_entry("msg-b"), max_size=10)

        assert await pg_state.queue_depth("thread-1") == 1
        assert await pg_state.queue_depth("thread-2") == 1

        e1 = await pg_state.dequeue("thread-1")
        assert e1 is not None
        assert e1.message["id"] == "msg-a"

        e2 = await pg_state.dequeue("thread-2")
        assert e2 is not None
        assert e2.message["id"] == "msg-b"


# ============================================================================
# Subscriptions
# ============================================================================


class TestPostgresStateSubscriptions:
    """Subscription operations: subscribe, unsubscribe, is_subscribed."""

    @pytest.mark.asyncio
    async def test_is_subscribed_returns_false_initially(self, pg_state: PostgresStateAdapter):
        assert await pg_state.is_subscribed("thread-1") is False

    @pytest.mark.asyncio
    async def test_subscribe_and_check(self, pg_state: PostgresStateAdapter):
        await pg_state.subscribe("thread-1")
        assert await pg_state.is_subscribed("thread-1") is True

    @pytest.mark.asyncio
    async def test_unsubscribe(self, pg_state: PostgresStateAdapter):
        await pg_state.subscribe("thread-1")
        await pg_state.unsubscribe("thread-1")
        assert await pg_state.is_subscribed("thread-1") is False

    @pytest.mark.asyncio
    async def test_unsubscribe_nonexistent_is_noop(self, pg_state: PostgresStateAdapter):
        await pg_state.unsubscribe("nonexistent")
        assert await pg_state.is_subscribed("nonexistent") is False

    @pytest.mark.asyncio
    async def test_subscribe_is_idempotent(self, pg_state: PostgresStateAdapter):
        await pg_state.subscribe("thread-1")
        await pg_state.subscribe("thread-1")
        assert await pg_state.is_subscribed("thread-1") is True

    @pytest.mark.asyncio
    async def test_independent_subscriptions(self, pg_state: PostgresStateAdapter):
        await pg_state.subscribe("thread-1")
        await pg_state.subscribe("thread-2")
        assert await pg_state.is_subscribed("thread-1") is True
        assert await pg_state.is_subscribed("thread-2") is True
        assert await pg_state.is_subscribed("thread-3") is False

    @pytest.mark.asyncio
    async def test_unsubscribe_one_does_not_affect_other(self, pg_state: PostgresStateAdapter):
        await pg_state.subscribe("thread-1")
        await pg_state.subscribe("thread-2")
        await pg_state.unsubscribe("thread-1")
        assert await pg_state.is_subscribed("thread-1") is False
        assert await pg_state.is_subscribed("thread-2") is True


# ============================================================================
# Key prefix isolation
# ============================================================================


class TestPostgresStateKeyPrefix:
    """Verify key prefix isolation between adapter instances."""

    @pytest.mark.asyncio
    async def test_different_prefixes_are_isolated(self, mock_pool: MockAsyncpgPool):
        a = PostgresStateAdapter(pool=mock_pool, key_prefix="prefix-a")
        b = PostgresStateAdapter(pool=mock_pool, key_prefix="prefix-b")
        await a.connect()
        await b.connect()

        await a.set("shared-key", "value-a")
        await b.set("shared-key", "value-b")

        assert await a.get("shared-key") == "value-a"
        assert await b.get("shared-key") == "value-b"

        await a.disconnect()
        await b.disconnect()
