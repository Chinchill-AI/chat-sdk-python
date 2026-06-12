"""Cross-platform per-user transcript store.

Python port of transcripts.ts.

Backed by ``StateAdapter.append_to_list`` — every built-in state adapter
supports it with no contract changes.  Keyed by a resolved cross-platform
user key from ``ChatConfig.identity``.

Distinct from :class:`~chat_sdk.thread_history.ThreadHistoryCache` (which is
per-thread, used by adapters that lack server-side history APIs).
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Any

from chat_sdk.types import (
    AppendInput,
    AppendOptions,
    CountQuery,
    DeleteResult,
    DeleteTarget,
    DurationString,
    ListQuery,
    Message,
    Postable,
    StateAdapter,
    TranscriptEntry,
    TranscriptRole,
    TranscriptsConfig,
)

KEY_PREFIX = "transcripts:user:"
DEFAULT_MAX_PER_USER = 200
DEFAULT_LIST_LIMIT = 50
DURATION_RE = re.compile(r"^(\d+)([smhd])$")

# Sentinel value written by ``delete()`` so the underlying list is
# functionally empty without needing a ``clear_list`` primitive on the state
# adapter contract.
#
# ``append_to_list(key, tombstone, max_length=1)`` evicts every prior entry
# across all built-in state adapters (memory trims the shared list;
# redis uses LTRIM; postgres trims oldest rows).  The remaining single
# tombstone is filtered out by ``list()`` and ``count()``, and is pushed out
# naturally on the next append once ``max_per_user`` writes have happened.
#
# The marker string matches the upstream TS SDK so stores are interoperable.
TOMBSTONE_MARKER = "__chatSdkTombstone"

MS_PER_UNIT = {
    "s": 1_000,
    "m": 60_000,
    "h": 3_600_000,
    "d": 86_400_000,
}


def _is_tombstone(value: Any) -> bool:
    return isinstance(value, dict) and value.get(TOMBSTONE_MARKER) is True


def _key_for(user_key: str) -> str:
    return f"{KEY_PREFIX}{user_key}"


def _parse_duration(value: int | float | DurationString | None) -> int | float | None:
    if value is None:
        return None
    # Numbers pass through unchanged (upstream `typeof value === "number"`).
    # bool is an int subclass but is not a valid duration — fall through to
    # the string path's error (mirrors upstream, where a boolean fails the
    # regex and raises).
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    match = DURATION_RE.match(value) if isinstance(value, str) else None
    if not match:
        raise ValueError(f'Invalid duration: {value} (expected number of ms, or "<n>[smhd]")')
    n = int(match.group(1))
    return n * MS_PER_UNIT[match.group(2)]


class TranscriptsApiImpl:
    """Cross-platform per-user transcript store, backed by
    ``StateAdapter.append_to_list``.

    Distinct from :class:`~chat_sdk.thread_history.ThreadHistoryCache` (which
    is per-thread, used by adapters that lack server-side history APIs).  This
    store is keyed by a resolved cross-platform user key from
    ``ChatConfig.identity``.
    """

    def __init__(self, state: StateAdapter, config: TranscriptsConfig) -> None:
        self._state = state
        self._max_per_user = config.max_per_user if config.max_per_user is not None else DEFAULT_MAX_PER_USER
        parsed_retention = _parse_duration(config.retention)
        # append_to_list takes ttl_ms: int | None; fractional ms are meaningless
        self._retention_ms = int(parsed_retention) if parsed_retention is not None else None
        self._store_formatted = config.store_formatted

    async def append(
        self,
        thread: Postable,
        message: Message | AppendInput,
        options: AppendOptions | None = None,
    ) -> TranscriptEntry | None:
        """Persist a Message (or AppendInput) under the user key.

        - For Message: ``user_key`` is read from the Message instance (set by
          the SDK during inbound dispatch via the configured IdentityResolver).
          No-op (returns ``None``) if the Message has no ``user_key``.
        - For AppendInput: ``options.user_key`` is required.
        """
        if isinstance(message, Message):
            user_key = message.user_key
            role: TranscriptRole = "user"
            platform_message_id: str | None = message.id
            if not user_key:
                return None
        else:
            user_key = options.user_key if options is not None else None
            role = message.role
            platform_message_id = message.platform_message_id
            if not user_key:
                raise ValueError("transcripts.append: options.user_key is required when appending an AppendInput")

        entry = TranscriptEntry(
            id=str(uuid.uuid4()),
            user_key=user_key,
            role=role,
            text=message.text,
            platform=thread.adapter.name,
            thread_id=thread.id,
            timestamp=int(time.time() * 1000),
        )
        if self._store_formatted and message.formatted:
            entry.formatted = message.formatted
        if platform_message_id is not None:
            entry.platform_message_id = platform_message_id

        await self._state.append_to_list(
            _key_for(user_key),
            entry.to_json(),
            max_length=self._max_per_user,
            ttl_ms=self._retention_ms,
        )

        return entry

    async def list(self, query: ListQuery) -> list[TranscriptEntry]:
        """Return the most recent entries in chronological order (oldest
        first), capped at ``query.limit`` (default 50).

        Pagination is intentionally not supported — the store keeps at most
        ``max_per_user`` entries per user.  To widen the window, raise
        ``max_per_user``; to fetch a different slice, narrow with
        ``thread_id`` / ``platforms`` / ``roles``.
        """
        raw = await self._state.get_list(_key_for(query.user_key))
        filtered = [TranscriptEntry.from_json(entry) for entry in raw if not _is_tombstone(entry)]

        if query.platforms:
            platforms = set(query.platforms)
            filtered = [m for m in filtered if m.platform in platforms]
        if query.thread_id is not None:
            tid = query.thread_id
            filtered = [m for m in filtered if m.thread_id == tid]
        if query.roles:
            roles = set(query.roles)
            filtered = [m for m in filtered if m.role in roles]

        limit = query.limit if query.limit is not None else DEFAULT_LIST_LIMIT
        if len(filtered) > limit:
            filtered = filtered[len(filtered) - limit :]
        return filtered

    async def count(self, query: CountQuery) -> int:
        """Total stored count for a user key."""
        raw = await self._state.get_list(_key_for(query.user_key))
        return sum(1 for entry in raw if not _is_tombstone(entry))

    async def delete(self, target: DeleteTarget) -> DeleteResult:
        """GDPR / DSR delete — wipes every stored message under the user key."""
        key = _key_for(target.user_key)
        existing = await self._state.get_list(key)
        previous = sum(1 for entry in existing if not _is_tombstone(entry))
        # Append a tombstone with max_length=1 to evict every prior entry. The
        # remaining tombstone is filtered out by list()/count(), and is
        # naturally pushed out by the next append once max_per_user writes
        # accumulate.  (Not ``state.delete(key)`` — that only addresses the
        # k/v namespace on every non-memory state adapter.)
        tombstone: dict[str, Any] = {TOMBSTONE_MARKER: True}
        await self._state.append_to_list(key, tombstone, max_length=1, ttl_ms=self._retention_ms)
        return DeleteResult(deleted=previous)
