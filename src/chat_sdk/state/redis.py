"""Redis-backed StateAdapter implementation.

Python port of ``@chat-adapter/state-redis`` (index.ts).

Uses ``redis.asyncio`` for async Redis operations.
Suitable for production use with distributed locking and persistent state.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from typing import Any

from chat_sdk.errors import StateNotConnectedError
from chat_sdk.types import Lock, QueueEntry

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _generate_token() -> str:
    return f"redis_{int(time.time() * 1000)}_{secrets.token_hex(16)}"


# ---------------------------------------------------------------------------
# Lua scripts (evaluated atomically on the Redis server)
# ---------------------------------------------------------------------------

_RELEASE_LOCK_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""

_EXTEND_LOCK_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("pexpire", KEYS[1], ARGV[2])
else
    return 0
end
"""

_APPEND_LIST_SCRIPT = """
redis.call("rpush", KEYS[1], ARGV[1])
if tonumber(ARGV[2]) > 0 then
    -- LTRIM -N -1 keeps the N most-recently pushed items (tail).
    -- This implements "drop-oldest" semantics: when the list overflows,
    -- the oldest items (head, next to be dequeued) are dropped.
    redis.call("ltrim", KEYS[1], -tonumber(ARGV[2]), -1)
end
if tonumber(ARGV[3]) > 0 then
    redis.call("pexpire", KEYS[1], tonumber(ARGV[3]))
end
return 1
"""

_ENQUEUE_SCRIPT = """
redis.call("rpush", KEYS[1], ARGV[1])
if tonumber(ARGV[2]) > 0 then
    -- LTRIM -N -1 keeps the N most-recently pushed items (tail).
    -- This implements "drop-oldest" semantics: when the queue overflows,
    -- the oldest items (head, next to be dequeued) are dropped.
    redis.call("ltrim", KEYS[1], -tonumber(ARGV[2]), -1)
end
redis.call("pexpire", KEYS[1], ARGV[3])
return redis.call("llen", KEYS[1])
"""

# ---------------------------------------------------------------------------
# RedisStateAdapter
# ---------------------------------------------------------------------------


class RedisStateAdapter:
    """Redis state adapter for production use.

    Provides persistent subscriptions and distributed locking
    across multiple server instances.

    Implements the full :class:`~chat_sdk.types.StateAdapter` protocol.
    """

    def __init__(
        self,
        *,
        url: str | None = None,
        client: Any | None = None,
        key_prefix: str = "chat-sdk",
    ) -> None:
        self._key_prefix = key_prefix
        self._connected = False
        self._connect_lock = asyncio.Lock()

        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            # Lazy import: redis.asyncio is only required when actually used
            import redis.asyncio as aioredis

            resolved_url = url or os.environ.get("REDIS_URL")
            if not resolved_url:
                raise ValueError("Redis url is required. Set REDIS_URL or provide url in options.")
            self._client = aioredis.from_url(resolved_url, decode_responses=True)
            self._owns_client = True

    # -- key helpers ---------------------------------------------------------

    def _key(self, kind: str, identifier: str) -> str:
        return f"{self._key_prefix}:{kind}:{identifier}"

    def _subscriptions_set_key(self) -> str:
        return f"{self._key_prefix}:subscriptions"

    # -- lifecycle -----------------------------------------------------------

    async def connect(self) -> None:
        if self._connected:
            return

        async with self._connect_lock:
            if self._connected:
                return
            # Verify connectivity
            await self._client.ping()
            self._connected = True

    async def disconnect(self) -> None:
        if not self._connected:
            return
        if self._owns_client:
            await self._client.aclose()
        self._connected = False

    # -- subscriptions -------------------------------------------------------

    async def subscribe(self, thread_id: str) -> None:
        self._ensure_connected()
        await self._client.sadd(self._subscriptions_set_key(), thread_id)

    async def unsubscribe(self, thread_id: str) -> None:
        self._ensure_connected()
        await self._client.srem(self._subscriptions_set_key(), thread_id)

    async def is_subscribed(self, thread_id: str) -> bool:
        self._ensure_connected()
        result = await self._client.sismember(self._subscriptions_set_key(), thread_id)
        return bool(result)

    # -- locking -------------------------------------------------------------

    async def acquire_lock(self, thread_id: str, ttl_ms: int) -> Lock | None:
        self._ensure_connected()

        token = _generate_token()
        lock_key = self._key("lock", thread_id)

        # SET NX PX for atomic lock acquisition
        acquired = await self._client.set(lock_key, token, nx=True, px=ttl_ms)

        if acquired:
            return Lock(
                thread_id=thread_id,
                token=token,
                expires_at=int(time.time() * 1000) + ttl_ms,
            )
        return None

    async def force_release_lock(self, thread_id: str) -> None:
        self._ensure_connected()
        lock_key = self._key("lock", thread_id)
        await self._client.delete(lock_key)

    async def release_lock(self, lock: Lock) -> None:
        self._ensure_connected()
        lock_key = self._key("lock", lock.thread_id)
        await self._client.eval(_RELEASE_LOCK_SCRIPT, 1, lock_key, lock.token)

    async def extend_lock(self, lock: Lock, ttl_ms: int) -> bool:
        self._ensure_connected()
        lock_key = self._key("lock", lock.thread_id)
        result = await self._client.eval(_EXTEND_LOCK_SCRIPT, 1, lock_key, lock.token, str(ttl_ms))
        return result == 1

    # -- key/value cache -----------------------------------------------------

    async def get(self, key: str) -> Any | None:
        self._ensure_connected()

        cache_key = self._key("cache", key)
        value = await self._client.get(cache_key)

        if value is None:
            return None

        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value

    async def set(self, key: str, value: Any, ttl_ms: int | None = None) -> None:
        self._ensure_connected()

        cache_key = self._key("cache", key)
        serialized = json.dumps(value)

        if ttl_ms:
            await self._client.set(cache_key, serialized, px=ttl_ms)
        else:
            await self._client.set(cache_key, serialized)

    async def set_if_not_exists(self, key: str, value: Any, ttl_ms: int | None = None) -> bool:
        self._ensure_connected()

        cache_key = self._key("cache", key)
        serialized = json.dumps(value)

        if ttl_ms:
            result = await self._client.set(cache_key, serialized, nx=True, px=ttl_ms)
        else:
            result = await self._client.set(cache_key, serialized, nx=True)

        return result is not None

    async def delete(self, key: str) -> None:
        self._ensure_connected()

        cache_key = self._key("cache", key)
        await self._client.delete(cache_key)

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

        list_key = f"{self._key_prefix}:list:{key}"
        serialized = json.dumps(value)
        max_len = max_length or 0
        ttl = ttl_ms or 0

        await self._client.eval(_APPEND_LIST_SCRIPT, 1, list_key, serialized, str(max_len), str(ttl))

    async def get_list(self, key: str) -> list[Any]:
        self._ensure_connected()

        list_key = f"{self._key_prefix}:list:{key}"
        values = await self._client.lrange(list_key, 0, -1)

        result: list[Any] = []
        for v in values:
            try:
                result.append(json.loads(v))
            except (json.JSONDecodeError, TypeError):
                result.append(v)
        return result

    # -- queues --------------------------------------------------------------

    async def enqueue(self, thread_id: str, entry: QueueEntry, max_size: int) -> int:
        self._ensure_connected()

        queue_key = self._key("queue", thread_id)

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

        ttl_ms = str(max(entry.expires_at - int(time.time() * 1000), 60_000))

        result = await self._client.eval(_ENQUEUE_SCRIPT, 1, queue_key, serialized, str(max_size), ttl_ms)

        return int(result)

    async def dequeue(self, thread_id: str) -> QueueEntry | None:
        self._ensure_connected()

        queue_key = self._key("queue", thread_id)
        value = await self._client.lpop(queue_key)

        if value is None:
            return None

        data = json.loads(value)
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

        queue_key = self._key("queue", thread_id)
        return await self._client.llen(queue_key)

    # -- introspection -------------------------------------------------------

    def get_client(self) -> Any:
        """Return the underlying Redis client for advanced usage."""
        return self._client

    # -- internal ------------------------------------------------------------

    def _ensure_connected(self) -> None:
        if not self._connected:
            raise StateNotConnectedError("RedisStateAdapter")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_redis_state(
    *,
    url: str | None = None,
    client: Any | None = None,
    key_prefix: str = "chat-sdk",
) -> RedisStateAdapter:
    """Create a new Redis state adapter.

    Either provide a ``url`` or an existing ``client``.  If neither is given,
    the ``REDIS_URL`` environment variable is used.
    """
    return RedisStateAdapter(url=url, client=client, key_prefix=key_prefix)
