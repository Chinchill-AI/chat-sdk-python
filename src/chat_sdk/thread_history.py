"""Persistent per-thread history cache backed by the StateAdapter.

Python port of thread-history.ts (renamed upstream from message-history.ts).

Used by adapters that lack server-side message history APIs
(e.g. WhatsApp, Telegram).  Messages are atomically appended via
``state.append_to_list()``, which is safe without holding a thread lock.

Distinct from the cross-platform per-user Transcripts API (see
``transcripts.py``) — this cache is keyed by thread, not user.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from chat_sdk.types import Message, StateAdapter

# Default maximum number of messages to store per thread
DEFAULT_MAX_MESSAGES = 100

# Default TTL for thread history (7 days in milliseconds)
DEFAULT_TTL_MS = 7 * 24 * 60 * 60 * 1000

# Key prefix for thread history entries.
#
# Kept as ``msg-history:`` for backwards compatibility — renaming would
# silently orphan every existing user's stored data. The user-facing names
# changed; the storage shape didn't.
KEY_PREFIX = "msg-history:"


@dataclass
class ThreadHistoryConfig:
    """Configuration for the thread history cache."""

    max_messages: int = DEFAULT_MAX_MESSAGES
    ttl_ms: int = DEFAULT_TTL_MS


class ThreadHistoryCache:
    """Persistent per-thread history cache backed by the StateAdapter.

    Used by adapters that lack server-side message history APIs
    (e.g. WhatsApp, Telegram).  Messages are atomically appended via
    ``state.append_to_list()``, which is safe without holding a thread lock.

    Distinct from the cross-platform per-user
    :class:`~chat_sdk.transcripts.TranscriptsApiImpl` (see ``transcripts.py``)
    — this cache is keyed by thread, not user.
    """

    def __init__(
        self,
        state: StateAdapter,
        config: ThreadHistoryConfig | None = None,
    ) -> None:
        self._state = state
        cfg = config or ThreadHistoryConfig()
        self._max_messages = cfg.max_messages
        self._ttl_ms = cfg.ttl_ms

    async def append(self, thread_id: str, message: Message) -> None:
        """Atomically append a message to the history for a thread.

        Trims to ``max_messages`` (keeps newest) and refreshes TTL.
        """
        key = f"{KEY_PREFIX}{thread_id}"

        # Serialize with raw nulled out to save storage
        serialized = message.to_json()
        serialized["raw"] = None

        await self._state.append_to_list(
            key,
            serialized,
            max_length=self._max_messages,
            ttl_ms=self._ttl_ms,
        )

    async def get_messages(self, thread_id: str, limit: int | None = None) -> list[Message]:
        """Get messages for a thread in chronological order (oldest first).

        Parameters
        ----------
        thread_id:
            The thread ID.
        limit:
            Optional limit on number of messages to return (returns newest N).
        """
        key = f"{KEY_PREFIX}{thread_id}"
        stored: list[dict[str, Any]] = await self._state.get_list(key)

        sliced = stored[len(stored) - limit :] if limit and len(stored) > limit else stored

        return [Message.from_json(s) for s in sliced]
