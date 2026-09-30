"""Tests for PostgresStateAdapter using a MockAsyncpgPool.

Since we cannot assume a running PostgreSQL instance, a MockAsyncpgPool
class simulates the asyncpg pool interface using in-memory dicts to
represent table storage.  SQL queries are pattern-matched to decide
which in-memory operation to perform.  The mock's clock is injectable
(``MockAsyncpgPool.advance``) so expiry tests never sleep.

``TestPostgresMigrationOwnedSchemaIntegration`` runs against a real
PostgreSQL server, and only when ``POSTGRES_TEST_URL`` is set (never
``POSTGRES_URL``: the tests create and drop schemas and roles).
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import os
import re
import sys
import time
import types
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

import chat_sdk
import chat_sdk.state
from chat_sdk.errors import ChatError, StateNotConnectedError, StateSchemaError
from chat_sdk.state.postgres import (
    POSTGRES_SCHEMA_STATEMENTS,
    PostgresStateAdapter,
    create_postgres_state,
)
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
        self.close_calls = 0
        self.executed_queries: list[str] = []
        # (method, query, args) for every query, in order.
        self.calls: list[tuple[str, str, tuple[Any, ...]]] = []
        # Offset added to wall-clock time; advance() moves it forward so expiry
        # tests are deterministic. The adapter computes expires_at from real
        # time, so an offset (not a frozen clock) keeps both sides comparable.
        self._clock_offset = _dt.timedelta(0)
        # Row returned by the auto_create_schema=False privilege probe. None
        # means "every alias in the probe is granted".
        self.probe_row: dict[str, bool | None] | None = None
        self.probe_error: BaseException | None = None
        # Exceptions raised, in order, by the next "SELECT 1" calls.
        self.select_one_errors: list[BaseException] = []

    def _next_seq(self) -> int:
        self._seq_counter += 1
        return self._seq_counter

    def _now(self) -> _dt.datetime:
        return _dt.datetime.now(_dt.timezone.utc) + self._clock_offset

    def advance(self, ms: int) -> None:
        """Move the database clock forward by ``ms`` milliseconds."""
        self._clock_offset += _dt.timedelta(milliseconds=ms)

    def _record(self, method: str, query: str, args: tuple[Any, ...]) -> str:
        self.executed_queries.append(query)
        self.calls.append((method, query, args))
        return _normalise(query)

    def _claim_cache_key(self, args: tuple[Any, ...]) -> bool:
        """Model ``INSERT ... ON CONFLICT DO UPDATE ... WHERE expired RETURNING``.

        Inserts an absent key or reclaims an expired one (``expires_at <=
        now()``). A live or permanent (``expires_at IS NULL``) row is left
        untouched and no row is returned, as in real PostgreSQL.
        """
        key_prefix, cache_key, value, expires_at = args[0], args[1], args[2], args[3]
        ck = (key_prefix, cache_key)
        existing = self.cache.get(ck)
        if existing is not None and (existing["expires_at"] is None or existing["expires_at"] > self._now()):
            return False
        self.cache[ck] = {"value": value, "expires_at": expires_at, "updated_at": self._now()}
        return True

    # -- lifecycle -------------------------------------------------------------

    async def close(self) -> None:
        self.close_calls += 1
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
        q = self._record("execute", query, args)

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
        # Conditional upsert (set_if_not_exists): must be matched before the
        # unconditional "do update" branch below, which would overwrite.
        if "insert into chat_state_cache" in q and "expires_at <= now()" in q:
            return "INSERT 0 1" if self._claim_cache_key(args) else "INSERT 0 0"

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
            # Real PostgreSQL: any existing row (expired or not) is a conflict,
            # and DO NOTHING never overwrites it.
            key_prefix, cache_key, value, expires_at = args[0], args[1], args[2], args[3]
            ck = (key_prefix, cache_key)
            if ck in self.cache:
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
        q = self._record("fetch", query, args)

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
        q = self._record("fetchrow", query, args)

        # -- schema probe (auto_create_schema=False) --
        if "has_table_privilege(" in q:
            if self.probe_error is not None:
                raise self.probe_error
            if self.probe_row is not None:
                return _Record(self.probe_row)
            return _Record({alias: True for alias in re.findall(r" as (\w+)", q)})

        # -- cache: set_if_not_exists (conditional upsert ... RETURNING) --
        if "insert into chat_state_cache" in q and "expires_at <= now()" in q:
            return _Record({"cache_key": args[1]}) if self._claim_cache_key(args) else None

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
        q = self._record("fetchval", query, args)

        if q.strip() == "select 1":
            if self.select_one_errors:
                raise self.select_one_errors.pop(0)
            return 1

        # -- cache: set_if_not_exists (conditional upsert ... RETURNING cache_key) --
        if "insert into chat_state_cache" in q and "expires_at <= now()" in q:
            return args[1] if self._claim_cache_key(args) else None

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
# Schema initialization (auto_create_schema) -- upstream describe("schema initialization")
# ============================================================================

_README = Path(__file__).resolve().parent.parent / "README.md"
_MIGRATION_HEADING = "#### Migration-owned schema"
_SQL_FENCES = re.compile(r"```sql[^\n]*\r?\n(.*?)```", re.DOTALL)
_SCHEMA_ERROR_HINT = (
    "Run the adapter migration on this database and search_path, grant the runtime role access, "
    "or set auto_create_schema=True."
)


def _collapse(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


def _documented_schema_statements(path: Path) -> list[str]:
    """Statements of the DDL block under the migration heading, whitespace-normalized."""
    content = path.read_text(encoding="utf-8")
    section = content.split(_MIGRATION_HEADING, 1)[1] if _MIGRATION_HEADING in content else ""
    block = next(
        (sql for sql in _SQL_FENCES.findall(section) if sql.lstrip().startswith(POSTGRES_SCHEMA_STATEMENTS[0][:40])),
        "",
    )
    return [statement for statement in (_collapse(part) for part in block.split(";")) if statement]


class _UndefinedTableError(Exception):
    """Stand-in for ``asyncpg.exceptions.UndefinedTableError`` (asyncpg is optional)."""

    sqlstate = "42P01"


def _call_summary(pool: MockAsyncpgPool) -> list[tuple[str, str]]:
    """(method, "SELECT 1" | "probe" | "ddl" | query) for every call so far."""
    summary = []
    for method, query, _ in pool.calls:
        if query == "SELECT 1":
            summary.append((method, "SELECT 1"))
        elif "has_table_privilege(" in query:
            summary.append((method, "probe"))
        elif query.lstrip().upper().startswith("CREATE"):
            summary.append((method, "ddl"))
        else:
            summary.append((method, query))
    return summary


class TestPostgresStateSchemaInitialization:
    """``auto_create_schema`` and the migration-owned schema probe."""

    @pytest.mark.asyncio
    # None mirrors upstream's `autoCreateSchema ?? true`: it keeps the default.
    @pytest.mark.parametrize(
        "kwargs",
        [{}, {"auto_create_schema": True}, {"auto_create_schema": None}],
        ids=["omitted", "true", "none"],
    )
    async def test_creates_every_table_and_index_when_auto_create_schema_is(
        self, mock_pool: MockAsyncpgPool, kwargs: dict[str, Any]
    ):
        adapter = PostgresStateAdapter(pool=mock_pool, **kwargs)
        await adapter.connect()
        assert mock_pool.executed_queries == ["SELECT 1", *POSTGRES_SCHEMA_STATEMENTS]
        assert [method for method, _, _ in mock_pool.calls[1:]] == ["execute"] * len(POSTGRES_SCHEMA_STATEMENTS)

    def test_keeps_the_published_migration_in_sync_with_the_statements_connect_runs(self):
        source = [_collapse(statement) for statement in POSTGRES_SCHEMA_STATEMENTS]
        assert len(source) == 9
        assert _documented_schema_statements(_README) == source
        # Public, immutable, and re-exported where the README says it is.
        assert isinstance(POSTGRES_SCHEMA_STATEMENTS, tuple)
        assert chat_sdk.state.POSTGRES_SCHEMA_STATEMENTS is POSTGRES_SCHEMA_STATEMENTS
        assert "POSTGRES_SCHEMA_STATEMENTS" in chat_sdk.state.__all__

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["constructor", "factory"])
    async def test_probes_instead_of_creating_the_schema_for_an_external_pool_via(
        self, mock_pool: MockAsyncpgPool, method: str
    ):
        adapter = (
            PostgresStateAdapter(pool=mock_pool, auto_create_schema=False)
            if method == "constructor"
            else create_postgres_state(pool=mock_pool, auto_create_schema=False)
        )
        await asyncio.gather(adapter.connect(), adapter.connect(), adapter.connect())
        await adapter.connect()
        assert _call_summary(mock_pool) == [("fetchval", "SELECT 1"), ("fetchrow", "probe")]
        assert "CREATE" not in mock_pool.calls[1][1]
        await adapter.disconnect()
        assert mock_pool.close_calls == 0
        await adapter.connect()
        assert len(mock_pool.calls) == 4

    @pytest.mark.asyncio
    async def test_probes_only_the_privileges_each_table_needs(self, mock_pool: MockAsyncpgPool):
        adapter = PostgresStateAdapter(pool=mock_pool, auto_create_schema=False)
        await adapter.connect()
        probe = mock_pool.calls[1][1]
        # Upstream's tablePrivileges map, column by column: every WHERE,
        # conflict target, RETURNING, SET and INSERT column the adapter uses.
        expected = {
            "chat_state_subscriptions": {
                "SELECT": ("key_prefix", "thread_id"),
                "INSERT": ("key_prefix", "thread_id"),
            },
            "chat_state_locks": {
                "SELECT": ("key_prefix", "thread_id", "token", "expires_at"),
                "INSERT": ("key_prefix", "thread_id", "token", "expires_at"),
                "UPDATE": ("token", "expires_at", "updated_at"),
            },
            "chat_state_cache": {
                "SELECT": ("key_prefix", "cache_key", "value", "expires_at"),
                "INSERT": ("key_prefix", "cache_key", "value", "expires_at"),
                "UPDATE": ("value", "expires_at", "updated_at"),
            },
            "chat_state_lists": {
                "SELECT": ("key_prefix", "list_key", "seq", "value", "expires_at"),
                "INSERT": ("key_prefix", "list_key", "value", "expires_at"),
                "UPDATE": ("expires_at",),
            },
            "chat_state_queues": {
                "SELECT": ("key_prefix", "thread_id", "seq", "value", "expires_at"),
                "INSERT": ("key_prefix", "thread_id", "value", "expires_at"),
            },
        }
        column_checks = re.findall(r"has_column_privilege\('(\w+)', '(\w+)', '(\w+)'\)", probe)
        assert len(column_checks) == len(set(column_checks))
        assert set(column_checks) == {
            (table, column, privilege)
            for table, privileges in expected.items()
            for privilege, columns in privileges.items()
            for column in columns
        }
        for table in ("chat_state_subscriptions", "chat_state_queues"):
            assert f"has_table_privilege('{table}', 'DELETE')" in probe
            assert f"has_table_privilege('{table}', 'UPDATE')" not in probe
            assert f"has_column_privilege('{table}', 'expires_at', 'UPDATE')" not in probe
        for table in ("chat_state_locks", "chat_state_cache", "chat_state_lists"):
            assert f"has_column_privilege('{table}', 'expires_at', 'UPDATE')" in probe
        assert "has_column_privilege('chat_state_cache', 'updated_at', 'UPDATE')" in probe
        assert "has_column_privilege('chat_state_cache', 'updated_at', 'INSERT')" not in probe
        assert "has_any_column_privilege" not in probe
        for table in ("chat_state_lists", "chat_state_queues"):
            assert f"has_sequence_privilege(pg_get_serial_sequence('{table}', 'seq'), 'USAGE, UPDATE')" in probe
            assert f"attrelid = '{table}'::regclass" in probe
        # The key prefix is a runtime value and must never be interpolated.
        assert "chat-sdk" not in probe

    @pytest.mark.asyncio
    async def test_rejects_connect_when_a_migration_owned_table_is_missing(self, mock_pool: MockAsyncpgPool):
        error = _UndefinedTableError('relation "chat_state_locks" does not exist')
        mock_pool.probe_error = error
        adapter = create_postgres_state(pool=mock_pool, auto_create_schema=False)

        with pytest.raises(chat_sdk.StateSchemaError) as exc_info:
            await adapter.connect()

        assert str(exc_info.value) == (
            'PostgreSQL state schema is not ready: relation "chat_state_locks" does not exist. ' + _SCHEMA_ERROR_HINT
        )
        assert exc_info.value.__cause__ is error
        # A caller-supplied pool is never closed, even after a failed connect.
        assert mock_pool.close_calls == 0
        assert adapter.get_pool() is mock_pool
        assert isinstance(exc_info.value, ChatError)
        assert chat_sdk.StateSchemaError is StateSchemaError
        assert ("execute", "ddl") not in _call_summary(mock_pool)
        with pytest.raises(StateNotConnectedError, match="not connected"):
            await adapter.get("key")

    @pytest.mark.asyncio
    async def test_rejects_connect_naming_every_object_the_runtime_role_cannot_use(self, mock_pool: MockAsyncpgPool):
        mock_pool.probe_row = {
            "chat_state_subscriptions": True,
            "chat_state_locks": True,
            "chat_state_cache": False,
            "chat_state_lists": True,
            "chat_state_queues": True,
            "chat_state_lists_seq": None,
            "chat_state_queues_seq": False,
        }
        adapter = create_postgres_state(pool=mock_pool, auto_create_schema=False)

        with pytest.raises(StateSchemaError) as exc_info:
            await adapter.connect()

        assert str(exc_info.value) == (
            "PostgreSQL state schema is not ready: the current role lacks privileges on "
            "chat_state_cache, chat_state_queues_seq. " + _SCHEMA_ERROR_HINT
        )
        with pytest.raises(StateNotConnectedError):
            await adapter.subscribe("thread")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("source", ["constructor", "factory", "POSTGRES_URL", "DATABASE_URL"])
    async def test_forwards_opt_out_for_a_url_from_and_closes_the_owned_pool(
        self, monkeypatch: pytest.MonkeyPatch, source: str
    ):
        url = "postgres://localhost:5432/test"
        pool = MockAsyncpgPool()
        create_pool = AsyncMock(return_value=pool)
        # asyncpg is an optional extra; connect() imports it lazily.
        monkeypatch.setitem(sys.modules, "asyncpg", types.SimpleNamespace(create_pool=create_pool))
        for name in ("POSTGRES_URL", "DATABASE_URL"):
            if source == name:
                monkeypatch.setenv(name, url)
            else:
                monkeypatch.delenv(name, raising=False)

        if source == "constructor":
            adapter = PostgresStateAdapter(url=url, auto_create_schema=False)
        elif source == "factory":
            adapter = create_postgres_state(url=url, auto_create_schema=False)
        else:
            adapter = create_postgres_state(auto_create_schema=False)

        await asyncio.gather(adapter.connect(), adapter.connect())
        await adapter.connect()
        create_pool.assert_awaited_once_with(dsn=url)
        assert _call_summary(pool) == [("fetchval", "SELECT 1"), ("fetchrow", "probe")]
        await adapter.disconnect()
        assert pool.close_calls == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failure", ["schema", "connectivity", "cancelled"])
    async def test_closes_the_owned_pool_when_connect_fails(self, monkeypatch: pytest.MonkeyPatch, failure: str):
        """Python divergence (see docs/UPSTREAM_SYNC.md): asyncpg opens its
        connections eagerly and ``disconnect()`` is a no-op until ``connect()``
        succeeds, so a pool the failed attempt created is closed at once and
        the next attempt builds a fresh one.
        """
        url = "postgres://localhost:5432/test"
        failed_pool, fresh_pool = MockAsyncpgPool(), MockAsyncpgPool()
        expected: type[BaseException]
        if failure == "schema":
            failed_pool.probe_error = _UndefinedTableError('relation "chat_state_cache" does not exist')
            expected = StateSchemaError
        elif failure == "connectivity":
            failed_pool.select_one_errors.append(ConnectionRefusedError("connection refused"))
            expected = ConnectionRefusedError
        else:
            failed_pool.probe_error = asyncio.CancelledError()
            expected = asyncio.CancelledError
        create_pool = AsyncMock(side_effect=[failed_pool, fresh_pool])
        # asyncpg is an optional extra; connect() imports it lazily.
        monkeypatch.setitem(sys.modules, "asyncpg", types.SimpleNamespace(create_pool=create_pool))
        adapter = create_postgres_state(url=url, auto_create_schema=False)

        with pytest.raises(expected):
            await adapter.connect()

        assert failed_pool.close_calls == 1
        assert adapter.get_pool() is None
        await adapter.disconnect()
        assert failed_pool.close_calls == 1

        await adapter.connect()
        assert create_pool.await_count == 2
        assert adapter.get_pool() is fresh_pool
        assert _call_summary(fresh_pool) == [("fetchval", "SELECT 1"), ("fetchrow", "probe")]
        await adapter.disconnect()
        assert (failed_pool.close_calls, fresh_pool.close_calls) == (1, 1)

    @pytest.mark.asyncio
    async def test_retries_a_failed_connectivity_check_without_ddl(
        self, mock_pool: MockAsyncpgPool, caplog: pytest.LogCaptureFixture
    ):
        error = ConnectionRefusedError("connection refused")
        mock_pool.select_one_errors.append(error)
        adapter = create_postgres_state(pool=mock_pool, auto_create_schema=False)

        with caplog.at_level("ERROR", logger="chat_sdk.state.postgres"), pytest.raises(ConnectionRefusedError) as ei:
            await adapter.connect()

        assert ei.value is error
        # No DDL and no probe after the failed SELECT 1.
        assert _call_summary(mock_pool) == [("fetchval", "SELECT 1")]
        assert "Postgres connect failed" in caplog.text
        with pytest.raises(StateNotConnectedError, match="not connected"):
            await adapter.get("key")

        await adapter.connect()
        assert _call_summary(mock_pool) == [
            ("fetchval", "SELECT 1"),
            ("fetchval", "SELECT 1"),
            ("fetchrow", "probe"),
        ]
        await adapter.subscribe("thread")
        assert await adapter.is_subscribed("thread") is True

    @pytest.mark.asyncio
    async def test_concurrent_connect_retries_after_a_failed_connectivity_check(self, mock_pool: MockAsyncpgPool):
        """Python divergence (see docs/UPSTREAM_SYNC.md): ``connect()`` calls are
        serialized on an ``asyncio.Lock`` rather than sharing one in-flight
        attempt, so a caller queued behind a failed attempt retries (and here
        succeeds) instead of receiving the same error, as upstream's callers do.
        """
        error = ConnectionRefusedError("connection refused")
        mock_pool.select_one_errors.append(error)
        adapter = create_postgres_state(pool=mock_pool, auto_create_schema=False)

        results = await asyncio.gather(adapter.connect(), adapter.connect(), return_exceptions=True)

        assert results == [error, None]
        assert _call_summary(mock_pool) == [
            ("fetchval", "SELECT 1"),
            ("fetchval", "SELECT 1"),
            ("fetchrow", "probe"),
        ]
        assert await adapter.get("key") is None


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
    async def test_set_with_ttl_expires(self, pg_state: PostgresStateAdapter, mock_pool: MockAsyncpgPool):
        await pg_state.set("key", "value", ttl_ms=1000)
        mock_pool.advance(1001)
        assert await pg_state.get("key") is None

    @pytest.mark.asyncio
    async def test_set_with_ttl_available_before_expiry(self, pg_state: PostgresStateAdapter):
        await pg_state.set("key", "value", ttl_ms=60_000)
        assert await pg_state.get("key") == "value"

    @pytest.mark.asyncio
    async def test_set_without_ttl_never_expires(self, pg_state: PostgresStateAdapter, mock_pool: MockAsyncpgPool):
        await pg_state.set("key", "value")
        mock_pool.advance(365 * 24 * 3600 * 1000)
        assert await pg_state.get("key") == "value"

    @pytest.mark.asyncio
    async def test_expired_key_cleaned_on_get(self, pg_state: PostgresStateAdapter, mock_pool: MockAsyncpgPool):
        """When get() returns None for an expired key, the adapter runs an opportunistic DELETE."""
        await pg_state.set("key", "value", ttl_ms=1000)
        mock_pool.advance(1001)
        await pg_state.get("key")

        # After the get, the expired row should have been cleaned up
        delete_queries = [q for q in mock_pool.executed_queries if "delete from chat_state_cache" in q.lower()]
        assert len(delete_queries) == 1
        assert ("test", "key") not in mock_pool.cache


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
    async def test_fails_when_key_exists(self, pg_state: PostgresStateAdapter, mock_pool: MockAsyncpgPool):
        await pg_state.set("key", "first", ttl_ms=60_000)
        expires_before = mock_pool.cache[("test", "key")]["expires_at"]
        result = await pg_state.set_if_not_exists("key", "second", ttl_ms=120_000)
        assert result is False
        assert await pg_state.get("key") == "first"
        # A live row keeps its original expiry.
        assert mock_pool.cache[("test", "key")]["expires_at"] == expires_before

    @pytest.mark.asyncio
    async def test_with_ttl(self, pg_state: PostgresStateAdapter):
        result = await pg_state.set_if_not_exists("key", "value", ttl_ms=60_000)
        assert result is True
        assert await pg_state.get("key") == "value"

    @pytest.mark.asyncio
    async def test_should_allow_set_if_not_exists_to_replace_expired_keys(
        self, pg_state: PostgresStateAdapter, mock_pool: MockAsyncpgPool
    ):
        """Upstream #636: an expired row is reclaimed in the same statement.

        Before the fix, ``ON CONFLICT DO NOTHING`` let a stale row block every
        new claim until get() happened to delete it. No get() runs here.
        """
        await pg_state.set("key", "old", ttl_ms=1000)
        mock_pool.advance(1001)
        mock_pool.calls.clear()

        result = await pg_state.set_if_not_exists("key", "value", ttl_ms=5000)

        assert result is True
        assert len(mock_pool.calls) == 1
        method, query, args = mock_pool.calls[0]
        assert method == "fetchval"
        assert (
            "WHERE chat_state_cache.expires_at IS NOT NULL AND chat_state_cache.expires_at <= now() "
            "RETURNING cache_key" in re.sub(r"\s+", " ", query)
        )
        assert args[:3] == ("test", "key", '"value"')
        assert isinstance(args[3], _dt.datetime)
        assert args[3].tzinfo is not None
        assert mock_pool.cache[("test", "key")]["value"] == '"value"'
        assert await pg_state.get("key") == "value"

    @pytest.mark.asyncio
    async def test_permanent_key_is_never_reclaimed(self, pg_state: PostgresStateAdapter, mock_pool: MockAsyncpgPool):
        """A key stored without a TTL (``expires_at IS NULL``) never expires, so
        set_if_not_exists must not overwrite it however much time passes."""
        await pg_state.set("key", "permanent")
        mock_pool.advance(365 * 24 * 3600 * 1000)
        assert await pg_state.set_if_not_exists("key", "new", ttl_ms=5000) is False
        assert mock_pool.cache[("test", "key")]["expires_at"] is None
        assert await pg_state.get("key") == "permanent"

    @pytest.mark.asyncio
    async def test_zero_ttl_claim_is_permanent(self, pg_state: PostgresStateAdapter, mock_pool: MockAsyncpgPool):
        """``ttl_ms=0`` means no expiry (upstream ``ttlMs ? ... : null``)."""
        assert await pg_state.set_if_not_exists("key", "first", ttl_ms=0) is True
        assert mock_pool.cache[("test", "key")]["expires_at"] is None
        mock_pool.advance(365 * 24 * 3600 * 1000)
        assert await pg_state.set_if_not_exists("key", "second", ttl_ms=5000) is False
        assert await pg_state.get("key") == "first"


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
    async def test_acquire_lock_succeeds_when_expired(self, pg_state: PostgresStateAdapter, mock_pool: MockAsyncpgPool):
        lock1 = await pg_state.acquire_lock("thread-1", 1000)
        assert lock1 is not None
        mock_pool.advance(1001)
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
    async def test_extend_lock(self, pg_state: PostgresStateAdapter, mock_pool: MockAsyncpgPool):
        lock = await pg_state.acquire_lock("thread-1", 100)
        assert lock is not None
        result = await pg_state.extend_lock(lock, 60_000)
        assert result is True

        # Lock should still be held after original TTL
        mock_pool.advance(150)
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
    async def test_extend_lock_after_expiry_fails(self, pg_state: PostgresStateAdapter, mock_pool: MockAsyncpgPool):
        lock = await pg_state.acquire_lock("thread-1", 1000)
        assert lock is not None
        mock_pool.advance(1001)

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
        # Force-expire the lock for testing
        lock_key = ("test", "race-thread")
        mock_pool.locks[lock_key]["expires_at"] = mock_pool._now() - _dt.timedelta(seconds=1)

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
    async def test_list_with_ttl_expires(self, pg_state: PostgresStateAdapter, mock_pool: MockAsyncpgPool):
        await pg_state.append_to_list("key", "a", ttl_ms=1000)
        mock_pool.advance(1001)
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


# ============================================================================
# Real PostgreSQL (opt-in) -- upstream postgres.integration.test.ts
# ============================================================================

# Explicit opt-in: never use an application's POSTGRES_URL for destructive
# tests. Requires a disposable database and an administrator with CREATEROLE
# and CREATE on the database. Superuser is not required.
_TEST_URL = os.environ.get("POSTGRES_TEST_URL")
_TTL_MS = 300_000
# Postgres reports whichever missing relation it resolves first.
_MISSING_RELATION = re.compile(
    r'^PostgreSQL state schema is not ready: relation "chat_state_[a-z]+" does not exist\. '
    r"Run the adapter migration"
)


class _PgTestEnv:
    """A throwaway schema plus a least-privilege runtime role."""

    def __init__(self, admin: Any, schema: str, role: str) -> None:
        self.admin = admin
        self.schema = schema
        self.role = role
        self.schemas: list[str] = []
        self.pools: list[Any] = []
        self.connections: list[Any] = []
        self.role_created = False
        self.runtime: Any = None
        self.state: PostgresStateAdapter | None = None

    async def pool(self, search_path: str | None = None, *, as_role: bool = True, max_size: int = 1) -> Any:
        import asyncpg

        settings = {"search_path": search_path or self.schema}
        if as_role:
            settings["role"] = self.role
        pool = await asyncpg.create_pool(_TEST_URL, min_size=1, max_size=max_size, server_settings=settings)
        self.pools.append(pool)
        return pool

    async def connection(self, search_path: str, *, as_role: bool) -> Any:
        import asyncpg

        settings = {"search_path": search_path}
        if as_role:
            settings["role"] = self.role
        conn = await asyncpg.connect(_TEST_URL, server_settings=settings)
        self.connections.append(conn)
        return conn

    async def owned_schema(self, name: str, statements: tuple[str, ...] | list[str]) -> Any:
        """Create schema ``name`` and run ``statements`` in it as the admin."""
        await self.admin.execute(f"CREATE SCHEMA {name}")
        self.schemas.append(name)
        owner = await self.connection(name, as_role=False)
        for statement in statements:
            await owner.execute(statement)
        return owner


def _queue_entry() -> QueueEntry:
    now = int(time.time() * 1000)
    return QueueEntry(message={"id": "message"}, enqueued_at=now, expires_at=now + _TTL_MS)


def _assert_same_entry(actual: QueueEntry | None, expected: QueueEntry) -> None:
    assert actual is not None
    assert (actual.message, actual.enqueued_at, actual.expires_at) == (
        expected.message,
        expected.enqueued_at,
        expected.expires_at,
    )


@pytest.mark.skipif(not _TEST_URL, reason="set POSTGRES_TEST_URL to a disposable database to run")
class TestPostgresMigrationOwnedSchemaIntegration:
    """``auto_create_schema=False`` and ``set_if_not_exists`` against real PostgreSQL."""

    @pytest.fixture
    async def pg(self):
        import asyncpg

        suffix = uuid.uuid4().hex
        admin = await asyncpg.connect(_TEST_URL)
        env = _PgTestEnv(admin, f"chat_test_{suffix}", f"chat_runtime_{suffix}")
        try:
            # Identifiers are generated from a UUID, never user-supplied SQL.
            await admin.execute(f"CREATE ROLE {env.role} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT")
            env.role_created = True
            # A non-superuser admin needs membership to SET ROLE on the runtime pools.
            await admin.execute(f"GRANT {env.role} TO CURRENT_USER")
            # The same statements connect() runs with auto_create_schema=True.
            owner = await env.owned_schema(env.schema, POSTGRES_SCHEMA_STATEMENTS)
            # Least privilege rather than the README's blanket grants: no UPDATE
            # on the two tables the adapter never updates, and nextval via
            # UPDATE only on the queue sequence.
            await owner.execute(f"GRANT USAGE ON SCHEMA {env.schema} TO {env.role}")
            await owner.execute(
                f"GRANT SELECT, INSERT, DELETE ON chat_state_subscriptions, chat_state_queues TO {env.role}"
            )
            await owner.execute(
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON chat_state_locks, chat_state_cache, chat_state_lists "
                f"TO {env.role}"
            )
            await owner.execute(f"GRANT USAGE ON SEQUENCE chat_state_lists_seq_seq TO {env.role}")
            await owner.execute(f"GRANT UPDATE ON SEQUENCE chat_state_queues_seq_seq TO {env.role}")
            env.runtime = await env.pool()
            env.state = create_postgres_state(pool=env.runtime, auto_create_schema=False)
            await env.state.connect()
            yield env
        finally:
            if env.state is not None:
                await env.state.disconnect()
            for pool in env.pools:
                await pool.close()
            for conn in env.connections:
                await conn.close()
            for created in env.schemas:
                await admin.execute(f"DROP SCHEMA {created} CASCADE")
            if env.role_created:
                await admin.execute(f"DROP ROLE {env.role}")
            await admin.close()

    @pytest.mark.asyncio
    async def test_supports_every_state_family_without_object_ownership_or_schema_create(self, pg: _PgTestEnv):
        import asyncpg

        state = pg.state
        assert state is not None
        row = await pg.runtime.fetchrow(
            "SELECT current_user AS role, has_schema_privilege(current_user, $1, 'CREATE') AS can_create",
            pg.schema,
        )
        assert dict(row) == {"role": pg.role, "can_create": False}
        granted = await pg.runtime.fetchrow(
            "SELECT has_table_privilege('chat_state_subscriptions', 'UPDATE') AS subscriptions_update, "
            "has_table_privilege('chat_state_queues', 'UPDATE') AS queues_update, "
            "has_sequence_privilege('chat_state_queues_seq_seq', 'USAGE') AS queues_seq_usage"
        )
        assert dict(granted) == {"subscriptions_update": False, "queues_update": False, "queues_seq_usage": False}
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await pg.runtime.execute("CREATE TABLE forbidden (id integer)")
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await pg.runtime.execute("CREATE INDEX forbidden ON chat_state_cache (cache_key)")

        await state.subscribe("thread")
        assert await state.is_subscribed("thread") is True
        await state.unsubscribe("thread")
        assert await state.is_subscribed("thread") is False
        await state.set("cache", {"count": 1}, _TTL_MS)
        assert await state.get("cache") == {"count": 1}
        await state.delete("cache")
        assert await state.get("cache") is None
        lock = await state.acquire_lock("thread", _TTL_MS)
        assert lock is not None
        assert await state.acquire_lock("thread", _TTL_MS) is None
        assert await state.extend_lock(lock, _TTL_MS) is True
        await state.release_lock(lock)
        assert await state.acquire_lock("thread", _TTL_MS) is not None
        await state.force_release_lock("thread")
        await state.append_to_list("list", {"count": 1}, ttl_ms=_TTL_MS, max_length=1)
        await state.append_to_list("list", {"count": 2}, ttl_ms=_TTL_MS, max_length=1)
        assert await state.get_list("list") == [{"count": 2}]
        entry = _queue_entry()
        assert await state.enqueue("thread", entry, 10) == 1
        assert await state.queue_depth("thread") == 1
        _assert_same_entry(await state.dequeue("thread"), entry)
        assert await state.dequeue("thread") is None
        assert await state.queue_depth("thread") == 0

    @pytest.mark.asyncio
    async def test_fails_connect_with_a_descriptive_error_when_the_tables_are_missing(self, pg: _PgTestEnv):
        import asyncpg

        pool = await pg.pool("pg_catalog")
        adapter = create_postgres_state(pool=pool, auto_create_schema=False)
        with pytest.raises(StateSchemaError) as exc_info:
            await adapter.connect()
        assert _MISSING_RELATION.match(str(exc_info.value))
        assert isinstance(exc_info.value.__cause__, asyncpg.exceptions.UndefinedTableError)
        assert exc_info.value.__cause__.sqlstate == "42P01"
        with pytest.raises(StateNotConnectedError, match="not connected"):
            await adapter.subscribe("missing")
        assert await pool.fetchval("SELECT 1") == 1

    @pytest.mark.asyncio
    async def test_fails_connect_naming_the_objects_the_runtime_role_cannot_use(self, pg: _PgTestEnv):
        adapter = create_postgres_state(pool=await pg.pool(), auto_create_schema=False)
        owner = await pg.connection(pg.schema, as_role=False)
        await owner.execute(f"REVOKE INSERT ON chat_state_cache FROM {pg.role}")
        await owner.execute(f"REVOKE UPDATE ON SEQUENCE chat_state_queues_seq_seq FROM {pg.role}")
        try:
            with pytest.raises(
                StateSchemaError,
                match=re.escape(
                    "PostgreSQL state schema is not ready: the current role lacks privileges on "
                    "chat_state_cache, chat_state_queues_seq."
                ),
            ):
                await adapter.connect()
        finally:
            await owner.execute(f"GRANT INSERT ON chat_state_cache TO {pg.role}")
            await owner.execute(f"GRANT UPDATE ON SEQUENCE chat_state_queues_seq_seq TO {pg.role}")
        await adapter.connect()
        assert await adapter.get("anything") is None

    @pytest.mark.asyncio
    async def test_accepts_sufficient_column_grants_and_rejects_missing_required_grants(self, pg: _PgTestEnv):
        columns = f"{pg.schema}_columns"
        owner = await pg.owned_schema(columns, POSTGRES_SCHEMA_STATEMENTS)
        await owner.execute(f"GRANT USAGE ON SCHEMA {columns} TO {pg.role}")
        await owner.execute(f"GRANT DELETE ON ALL TABLES IN SCHEMA {columns} TO {pg.role}")
        await owner.execute(f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA {columns} TO {pg.role}")
        for grant in (
            "SELECT (key_prefix, thread_id), INSERT (key_prefix, thread_id) ON chat_state_subscriptions",
            "SELECT (key_prefix, thread_id, token, expires_at), INSERT (key_prefix, thread_id, token, expires_at), "
            "UPDATE (token, expires_at, updated_at) ON chat_state_locks",
            "SELECT (key_prefix, cache_key, value, expires_at), INSERT (key_prefix, cache_key, value, expires_at), "
            "UPDATE (value, expires_at, updated_at) ON chat_state_cache",
            "SELECT (key_prefix, list_key, seq, value, expires_at), INSERT (key_prefix, list_key, value, expires_at), "
            "UPDATE (expires_at) ON chat_state_lists",
            "SELECT (key_prefix, thread_id, seq, value, expires_at), INSERT (key_prefix, thread_id, value, expires_at) "
            "ON chat_state_queues",
        ):
            await owner.execute(f"GRANT {grant} TO {pg.role}")
        pool = await pg.pool(columns)
        adapter = create_postgres_state(pool=pool, auto_create_schema=False)
        privileges = await pool.fetchrow(
            "SELECT has_table_privilege('chat_state_cache', 'INSERT') AS table_insert, "
            "has_column_privilege('chat_state_cache', 'value', 'INSERT') AS column_insert"
        )
        assert dict(privileges) == {"table_insert": False, "column_insert": True}

        # Every statement the adapter issues must work with exactly these grants.
        await adapter.connect()
        await adapter.subscribe("thread")
        await adapter.subscribe("thread")
        assert await adapter.is_subscribed("thread") is True
        await adapter.unsubscribe("thread")
        assert await adapter.is_subscribed("thread") is False
        await adapter.set("cache", "first", _TTL_MS)
        await adapter.set("cache", "updated", _TTL_MS)
        assert await adapter.get("cache") == "updated"
        assert await adapter.set_if_not_exists("cache", "duplicate", _TTL_MS) is False
        await adapter.delete("cache")
        assert await adapter.get("cache") is None
        assert await adapter.set_if_not_exists("cache", "new", _TTL_MS) is True
        lock = await adapter.acquire_lock("thread", _TTL_MS)
        assert lock is not None
        assert await adapter.acquire_lock("thread", _TTL_MS) is None
        assert await adapter.extend_lock(lock, _TTL_MS) is True
        await adapter.release_lock(lock)
        await adapter.force_release_lock("thread")
        await adapter.append_to_list("list", "first", ttl_ms=_TTL_MS, max_length=1)
        await adapter.append_to_list("list", "second", ttl_ms=_TTL_MS, max_length=1)
        assert await adapter.get_list("list") == ["second"]
        entry = _queue_entry()
        await adapter.enqueue("thread", entry, 1)
        assert await adapter.enqueue("thread", entry, 1) == 1
        assert await adapter.queue_depth("thread") == 1
        _assert_same_entry(await adapter.dequeue("thread"), entry)
        assert await adapter.dequeue("thread") is None
        await adapter.disconnect()

        for grant in (
            "SELECT (cache_key) ON chat_state_cache",
            "SELECT (token) ON chat_state_locks",
            "INSERT (value) ON chat_state_cache",
            "UPDATE (updated_at) ON chat_state_cache",
            "DELETE ON chat_state_cache",
            "USAGE ON SEQUENCE chat_state_lists_seq_seq",
        ):
            await owner.execute(f"REVOKE {grant} FROM {pg.role}")
            try:
                with pytest.raises(StateSchemaError, match="the current role lacks privileges"):
                    await adapter.connect()
            finally:
                await owner.execute(f"GRANT {grant} TO {pg.role}")
            await adapter.connect()
            await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_accepts_identity_columns_without_any_sequence_grant(self, pg: _PgTestEnv):
        identity = f"{pg.schema}_identity"
        owner = await pg.owned_schema(
            identity,
            [
                statement.replace("seq bigserial NOT NULL", "seq bigint GENERATED ALWAYS AS IDENTITY")
                for statement in POSTGRES_SCHEMA_STATEMENTS
            ],
        )
        await owner.execute(f"GRANT USAGE ON SCHEMA {identity} TO {pg.role}")
        await owner.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {identity} TO {pg.role}")
        adapter = create_postgres_state(pool=await pg.pool(identity), auto_create_schema=False)
        await adapter.connect()
        await adapter.append_to_list("list", {"count": 1}, ttl_ms=_TTL_MS)
        assert await adapter.get_list("list") == [{"count": 1}]
        entry = _queue_entry()
        assert await adapter.enqueue("thread", entry, 10) == 1
        _assert_same_entry(await adapter.dequeue("thread"), entry)

    @pytest.mark.asyncio
    async def test_claims_an_absent_key_and_preserves_future_expiring_and_permanent_values(self, pg: _PgTestEnv):
        state = pg.state
        assert state is not None
        select_row = "SELECT value, expires_at FROM chat_state_cache WHERE cache_key = $1"
        assert await state.set_if_not_exists("absent", "first", _TTL_MS) is True
        before = await pg.runtime.fetchrow(select_row, "absent")
        assert await state.set_if_not_exists("absent", "second", _TTL_MS) is False
        after = await pg.runtime.fetchrow(select_row, "absent")
        assert dict(after) == dict(before)
        await state.set("permanent", "first")
        assert await state.set_if_not_exists("permanent", "second", _TTL_MS) is False
        assert await state.get("permanent") == "first"
        other = create_postgres_state(pool=pg.runtime, auto_create_schema=False, key_prefix="other")
        await other.connect()
        assert await other.set_if_not_exists("permanent", "independent", _TTL_MS) is True
        assert await other.get("permanent") == "independent"
        assert await state.get("permanent") == "first"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("replacement_ttl", [_TTL_MS, None])
    async def test_renews_an_expired_entry_without_get_replacement_ttl(
        self, pg: _PgTestEnv, replacement_ttl: int | None
    ):
        state = pg.state
        assert state is not None
        key = f"expired-{replacement_ttl}"
        await pg.runtime.execute(
            "INSERT INTO chat_state_cache (key_prefix, cache_key, value, expires_at, updated_at) "
            "VALUES ($1, $2, $3, now() - interval '1 day', now() - interval '1 day')",
            "chat-sdk",
            key,
            '"old"',
        )
        assert await state.set_if_not_exists(key, "new", replacement_ttl) is True
        row = await pg.runtime.fetchrow(
            "SELECT value, expires_at, updated_at > now() - interval '1 minute' AS updated "
            "FROM chat_state_cache WHERE cache_key = $1",
            key,
        )
        assert row["value"] == '"new"'
        assert row["updated"] is True
        if replacement_ttl:
            assert row["expires_at"] > _dt.datetime.now(_dt.timezone.utc)
        else:
            assert row["expires_at"] is None

    @pytest.mark.asyncio
    async def test_renews_at_the_exact_expiry_boundary_using_the_same_transaction_clock(self, pg: _PgTestEnv):
        # One connection (not a pool, whose release would roll back) keeps
        # BEGIN, the seed, the adapter's queries and ROLLBACK in one
        # transaction, where now() is constant.
        conn = await pg.connection(pg.schema, as_role=True)
        adapter = create_postgres_state(pool=conn, auto_create_schema=False)
        await adapter.connect()
        await conn.execute("BEGIN")
        try:
            await conn.execute(
                "INSERT INTO chat_state_cache (key_prefix, cache_key, value, expires_at) "
                "VALUES ('chat-sdk', 'boundary', '\"old\"', now())"
            )
            assert await adapter.set_if_not_exists("boundary", "new", _TTL_MS) is True
            assert await adapter.get("boundary") == "new"
        finally:
            await conn.execute("ROLLBACK")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("initial_state", ["absent", "expired"])
    async def test_has_exactly_one_winner_for_concurrent_key_claims(self, pg: _PgTestEnv, initial_state: str):
        state = pg.state
        assert state is not None
        key = f"concurrent-{initial_state}"
        if initial_state == "expired":
            await pg.runtime.execute(
                "INSERT INTO chat_state_cache (key_prefix, cache_key, value, expires_at) "
                "VALUES ($1, $2, $3, now() - interval '1 day')",
                "chat-sdk",
                key,
                '"old"',
            )
        contenders = [create_postgres_state(pool=await pg.pool(), auto_create_schema=False) for _ in range(8)]
        await asyncio.gather(*(adapter.connect() for adapter in contenders))
        pids = await asyncio.gather(*(adapter.get_pool().fetchval("SELECT pg_backend_pid()") for adapter in contenders))
        assert len(set(pids)) == len(contenders)
        results = await asyncio.gather(
            *(adapter.set_if_not_exists(key, index, _TTL_MS) for index, adapter in enumerate(contenders))
        )
        assert results.count(True) == 1
        assert await state.get(key) == results.index(True)
