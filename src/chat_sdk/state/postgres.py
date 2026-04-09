"""PostgreSQL-backed StateAdapter implementation.

Python port of ``@chat-adapter/state-pg`` (index.ts).

Uses ``asyncpg`` for async PostgreSQL operations.
Suitable for production use with row-level locking and persistent state.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import os
import time
import uuid
from typing import Any

from chat_sdk.errors import StateNotConnectedError
from chat_sdk.types import Lock, QueueEntry

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _generate_token() -> str:
    return f"pg_{uuid.uuid4()}"


def _now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

_SCHEMA_STATEMENTS: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS chat_state_subscriptions (
        key_prefix text NOT NULL,
        thread_id text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (key_prefix, thread_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS chat_state_locks (
        key_prefix text NOT NULL,
        thread_id text NOT NULL,
        token text NOT NULL,
        expires_at timestamptz NOT NULL,
        updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (key_prefix, thread_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS chat_state_cache (
        key_prefix text NOT NULL,
        cache_key text NOT NULL,
        value text NOT NULL,
        expires_at timestamptz,
        updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (key_prefix, cache_key)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS chat_state_locks_expires_idx
        ON chat_state_locks (expires_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS chat_state_cache_expires_idx
        ON chat_state_cache (expires_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS chat_state_lists (
        key_prefix text NOT NULL,
        list_key text NOT NULL,
        seq bigserial NOT NULL,
        value text NOT NULL,
        expires_at timestamptz,
        PRIMARY KEY (key_prefix, list_key, seq)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS chat_state_lists_expires_idx
        ON chat_state_lists (expires_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS chat_state_queues (
        key_prefix text NOT NULL,
        thread_id text NOT NULL,
        seq bigserial NOT NULL,
        value text NOT NULL,
        expires_at timestamptz NOT NULL,
        PRIMARY KEY (key_prefix, thread_id, seq)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS chat_state_queues_expires_idx
        ON chat_state_queues (expires_at)
    """,
]


# ---------------------------------------------------------------------------
# PostgresStateAdapter
# ---------------------------------------------------------------------------


class PostgresStateAdapter:
    """PostgreSQL state adapter for production use.

    Provides persistent subscriptions and row-level locking.
    Auto-creates required tables on first ``connect()``.

    Implements the full :class:`~chat_sdk.types.StateAdapter` protocol.
    """

    def __init__(
        self,
        *,
        url: str | None = None,
        pool: Any | None = None,
        key_prefix: str = "chat-sdk",
    ) -> None:
        self._key_prefix = key_prefix
        self._connected = False
        self._connect_lock = asyncio.Lock()
        self._owns_pool = pool is None

        if pool is not None:
            self._pool = pool
            self._url: str | None = None
        else:
            self._pool = None  # created lazily in connect()
            resolved_url = url or os.environ.get("POSTGRES_URL") or os.environ.get("DATABASE_URL")
            if not resolved_url:
                raise ValueError(
                    "Postgres url is required. Set POSTGRES_URL or DATABASE_URL, or provide url in options."
                )
            self._url = resolved_url

    # -- lifecycle -----------------------------------------------------------

    async def connect(self) -> None:
        if self._connected:
            return

        async with self._connect_lock:
            if self._connected:
                return

            try:
                if self._pool is None:
                    import asyncpg

                    self._pool = await asyncpg.create_pool(dsn=self._url)

                # Verify connectivity
                await self._pool.fetchval("SELECT 1")
                await self._ensure_schema()
                self._connected = True
            except Exception:
                _logger.exception("Postgres connect failed")
                raise

    async def disconnect(self) -> None:
        if not self._connected:
            return
        if self._owns_pool and self._pool is not None:
            await self._pool.close()
        self._connected = False

    # -- subscriptions -------------------------------------------------------

    async def subscribe(self, thread_id: str) -> None:
        self._ensure_connected()
        await self._pool.execute(
            """INSERT INTO chat_state_subscriptions (key_prefix, thread_id)
               VALUES ($1, $2)
               ON CONFLICT DO NOTHING""",
            self._key_prefix,
            thread_id,
        )

    async def unsubscribe(self, thread_id: str) -> None:
        self._ensure_connected()
        await self._pool.execute(
            """DELETE FROM chat_state_subscriptions
               WHERE key_prefix = $1 AND thread_id = $2""",
            self._key_prefix,
            thread_id,
        )

    async def is_subscribed(self, thread_id: str) -> bool:
        self._ensure_connected()
        row = await self._pool.fetchrow(
            """SELECT 1 FROM chat_state_subscriptions
               WHERE key_prefix = $1 AND thread_id = $2
               LIMIT 1""",
            self._key_prefix,
            thread_id,
        )
        return row is not None

    # -- locking -------------------------------------------------------------

    async def acquire_lock(self, thread_id: str, ttl_ms: int) -> Lock | None:
        self._ensure_connected()

        token = _generate_token()

        # Atomic upsert: INSERT succeeds for new rows; ON CONFLICT DO UPDATE
        # fires only when the existing row is expired (WHERE expires_at <= now()).
        # Postgres acquires a row lock on the conflicting row, so only one
        # concurrent caller can win — eliminating the TOCTOU race that existed
        # in the previous two-step INSERT-then-UPDATE approach.
        row = await self._pool.fetchrow(
            """INSERT INTO chat_state_locks (key_prefix, thread_id, token, expires_at)
               VALUES ($1, $2, $3, now() + make_interval(secs => $4::float / 1000))
               ON CONFLICT (key_prefix, thread_id) DO UPDATE
                 SET token = EXCLUDED.token,
                     expires_at = EXCLUDED.expires_at,
                     updated_at = now()
                 WHERE chat_state_locks.expires_at <= now()
               RETURNING thread_id, token, expires_at""",
            self._key_prefix,
            thread_id,
            token,
            ttl_ms,
        )

        if row is None:
            return None

        return Lock(
            thread_id=row["thread_id"],
            token=row["token"],
            expires_at=int(row["expires_at"].timestamp() * 1000),
        )

    async def force_release_lock(self, thread_id: str) -> None:
        self._ensure_connected()
        await self._pool.execute(
            """DELETE FROM chat_state_locks
               WHERE key_prefix = $1 AND thread_id = $2""",
            self._key_prefix,
            thread_id,
        )

    async def release_lock(self, lock: Lock) -> None:
        self._ensure_connected()
        await self._pool.execute(
            """DELETE FROM chat_state_locks
               WHERE key_prefix = $1 AND thread_id = $2 AND token = $3""",
            self._key_prefix,
            lock.thread_id,
            lock.token,
        )

    async def extend_lock(self, lock: Lock, ttl_ms: int) -> bool:
        self._ensure_connected()
        result = await self._pool.execute(
            """UPDATE chat_state_locks
               SET expires_at = now() + $1 * interval '1 millisecond',
                   updated_at = now()
               WHERE key_prefix = $2
                 AND thread_id = $3
                 AND token = $4
                 AND expires_at > now()""",
            ttl_ms,
            self._key_prefix,
            lock.thread_id,
            lock.token,
        )
        # asyncpg returns a command tag like "UPDATE 1" or "UPDATE 0"
        return result is not None and result.endswith("1")

    # -- key/value cache -----------------------------------------------------

    async def get(self, key: str) -> Any | None:
        self._ensure_connected()

        row = await self._pool.fetchrow(
            """SELECT value FROM chat_state_cache
               WHERE key_prefix = $1 AND cache_key = $2
                 AND (expires_at IS NULL OR expires_at > now())
               LIMIT 1""",
            self._key_prefix,
            key,
        )

        if row is None:
            # Opportunistic cleanup of expired entry
            await self._pool.execute(
                """DELETE FROM chat_state_cache
                   WHERE key_prefix = $1 AND cache_key = $2
                     AND expires_at <= now()""",
                self._key_prefix,
                key,
            )
            return None

        try:
            return json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return row["value"]

    async def set(self, key: str, value: Any, ttl_ms: int | None = None) -> None:
        self._ensure_connected()

        serialized = json.dumps(value)
        expires_at = _pg_timestamp_from_ms(ttl_ms) if ttl_ms else None

        await self._pool.execute(
            """INSERT INTO chat_state_cache (key_prefix, cache_key, value, expires_at)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (key_prefix, cache_key) DO UPDATE
                 SET value = EXCLUDED.value,
                     expires_at = EXCLUDED.expires_at,
                     updated_at = now()""",
            self._key_prefix,
            key,
            serialized,
            expires_at,
        )

    async def set_if_not_exists(self, key: str, value: Any, ttl_ms: int | None = None) -> bool:
        self._ensure_connected()

        serialized = json.dumps(value)
        expires_at = _pg_timestamp_from_ms(ttl_ms) if ttl_ms else None

        result = await self._pool.execute(
            """INSERT INTO chat_state_cache (key_prefix, cache_key, value, expires_at)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (key_prefix, cache_key) DO NOTHING""",
            self._key_prefix,
            key,
            serialized,
            expires_at,
        )
        # asyncpg returns "INSERT 0 1" on success, "INSERT 0 0" on conflict
        return result is not None and result.endswith("1")

    async def delete(self, key: str) -> None:
        self._ensure_connected()
        await self._pool.execute(
            """DELETE FROM chat_state_cache
               WHERE key_prefix = $1 AND cache_key = $2""",
            self._key_prefix,
            key,
        )

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

        serialized = json.dumps(value)
        expires_at = _pg_timestamp_from_ms(ttl_ms) if ttl_ms else None

        # Insert the new entry
        await self._pool.execute(
            """INSERT INTO chat_state_lists (key_prefix, list_key, value, expires_at)
               VALUES ($1, $2, $3, $4)""",
            self._key_prefix,
            key,
            serialized,
            expires_at,
        )

        # Trim overflow if max_length is specified
        if max_length:
            await self._pool.execute(
                """DELETE FROM chat_state_lists
                   WHERE key_prefix = $1 AND list_key = $2 AND seq IN (
                     SELECT seq FROM chat_state_lists
                     WHERE key_prefix = $1 AND list_key = $2
                     ORDER BY seq ASC
                     OFFSET 0
                     LIMIT GREATEST(
                       (SELECT count(*) FROM chat_state_lists
                        WHERE key_prefix = $1 AND list_key = $2) - $3,
                       0
                     )
                   )""",
                self._key_prefix,
                key,
                max_length,
            )

        # Update TTL on all entries for this key
        if expires_at:
            await self._pool.execute(
                """UPDATE chat_state_lists
                   SET expires_at = $3
                   WHERE key_prefix = $1 AND list_key = $2""",
                self._key_prefix,
                key,
                expires_at,
            )

    async def get_list(self, key: str) -> list[Any]:
        self._ensure_connected()

        rows = await self._pool.fetch(
            """SELECT value FROM chat_state_lists
               WHERE key_prefix = $1 AND list_key = $2
                 AND (expires_at IS NULL OR expires_at > now())
               ORDER BY seq ASC""",
            self._key_prefix,
            key,
        )

        result: list[Any] = []
        for row in rows:
            try:
                result.append(json.loads(row["value"]))
            except (json.JSONDecodeError, TypeError):
                result.append(row["value"])
        return result

    # -- queues --------------------------------------------------------------

    async def enqueue(self, thread_id: str, entry: QueueEntry, max_size: int) -> int:
        self._ensure_connected()

        if hasattr(entry.message, "to_json"):
            msg_data = entry.message.to_json()
        elif isinstance(entry.message, dict):
            msg_data = entry.message
        else:
            msg_data = entry.message.__dict__

        serialized = json.dumps(
            {
                "enqueued_at": entry.enqueued_at,
                "expires_at": entry.expires_at,
                "message": msg_data,
            }
        )
        expires_at = _pg_timestamp_from_epoch_ms(entry.expires_at)

        # Wrap insert + trim in a transaction to avoid TOCTOU races
        async with self._pool.acquire() as conn, conn.transaction():
            # Purge expired entries first
            await conn.execute(
                """DELETE FROM chat_state_queues
                       WHERE key_prefix = $1 AND thread_id = $2 AND expires_at <= now()""",
                self._key_prefix,
                thread_id,
            )

            # Insert the new entry
            await conn.execute(
                """INSERT INTO chat_state_queues (key_prefix, thread_id, value, expires_at)
                       VALUES ($1, $2, $3, $4)""",
                self._key_prefix,
                thread_id,
                serialized,
                expires_at,
            )

            # Trim overflow (keep newest max_size non-expired entries)
            if max_size > 0:
                await conn.execute(
                    """DELETE FROM chat_state_queues
                           WHERE key_prefix = $1 AND thread_id = $2 AND seq IN (
                             SELECT seq FROM chat_state_queues
                             WHERE key_prefix = $1 AND thread_id = $2
                               AND expires_at > now()
                             ORDER BY seq ASC
                             OFFSET 0
                             LIMIT GREATEST(
                               (SELECT count(*) FROM chat_state_queues
                                WHERE key_prefix = $1 AND thread_id = $2 AND expires_at > now()) - $3,
                               0
                             )
                           )""",
                    self._key_prefix,
                    thread_id,
                    max_size,
                )

            # Return current non-expired depth
            depth = await conn.fetchval(
                """SELECT count(*) FROM chat_state_queues
                       WHERE key_prefix = $1 AND thread_id = $2 AND expires_at > now()""",
                self._key_prefix,
                thread_id,
            )

        return int(depth)

    async def dequeue(self, thread_id: str) -> QueueEntry | None:
        self._ensure_connected()

        # Purge expired entries first
        await self._pool.execute(
            """DELETE FROM chat_state_queues
               WHERE key_prefix = $1 AND thread_id = $2 AND expires_at <= now()""",
            self._key_prefix,
            thread_id,
        )

        # Atomically select + delete the oldest non-expired entry
        row = await self._pool.fetchrow(
            """DELETE FROM chat_state_queues
               WHERE key_prefix = $1 AND thread_id = $2
                 AND seq = (
                   SELECT seq FROM chat_state_queues
                   WHERE key_prefix = $1 AND thread_id = $2
                     AND expires_at > now()
                   ORDER BY seq ASC
                   LIMIT 1
                 )
               RETURNING value""",
            self._key_prefix,
            thread_id,
        )

        if row is None:
            return None

        data = json.loads(row["value"])
        msg_data = data["message"]
        if isinstance(msg_data, dict) and msg_data.get("_type") == "chat:Message":
            from chat_sdk.types import Message

            msg = Message.from_json(msg_data)
        else:
            msg = msg_data
        return QueueEntry(
            enqueued_at=data["enqueued_at"],
            expires_at=data["expires_at"],
            message=msg,
        )

    async def queue_depth(self, thread_id: str) -> int:
        self._ensure_connected()

        depth = await self._pool.fetchval(
            """SELECT count(*) FROM chat_state_queues
               WHERE key_prefix = $1 AND thread_id = $2 AND expires_at > now()""",
            self._key_prefix,
            thread_id,
        )

        return int(depth)

    # -- introspection -------------------------------------------------------

    def get_pool(self) -> Any:
        """Return the underlying asyncpg pool for advanced usage."""
        return self._pool

    # -- internal ------------------------------------------------------------

    def _ensure_connected(self) -> None:
        if not self._connected:
            raise StateNotConnectedError("PostgresStateAdapter")

    async def _ensure_schema(self) -> None:
        """Create required tables and indexes if they do not exist."""
        for stmt in _SCHEMA_STATEMENTS:
            await self._pool.execute(stmt)


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------


def _pg_timestamp_from_ms(ttl_ms: int) -> _dt.datetime:
    """Return a timezone-aware datetime ``ttl_ms`` milliseconds from now."""
    return _dt.datetime.now(_dt.UTC) + _dt.timedelta(milliseconds=ttl_ms)


def _pg_timestamp_from_epoch_ms(epoch_ms: int) -> _dt.datetime:
    """Return a timezone-aware datetime from an epoch-millisecond value."""
    return _dt.datetime.fromtimestamp(epoch_ms / 1000, tz=_dt.UTC)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_postgres_state(
    *,
    url: str | None = None,
    pool: Any | None = None,
    key_prefix: str = "chat-sdk",
) -> PostgresStateAdapter:
    """Create a new PostgreSQL state adapter.

    Either provide a ``url`` or an existing asyncpg ``pool``.  If neither is
    given, the ``POSTGRES_URL`` / ``DATABASE_URL`` environment variable is used.
    """
    return PostgresStateAdapter(url=url, pool=pool, key_prefix=key_prefix)
