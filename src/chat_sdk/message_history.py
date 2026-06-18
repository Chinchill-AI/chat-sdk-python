"""Deprecated — renamed to ``chat_sdk.thread_history``.

Python port of message-history.ts (which upstream turned into a
backwards-compatibility re-export shim when the cache was renamed to
``ThreadHistoryCache`` in thread-history.ts).

This module is preserved for backwards compatibility and re-exports the
new names under the old identifiers.  New code should import
``ThreadHistoryCache`` and ``ThreadHistoryConfig`` from
``chat_sdk.thread_history`` directly.
"""

from __future__ import annotations

from chat_sdk.thread_history import (
    DEFAULT_MAX_MESSAGES,
    DEFAULT_TTL_MS,
    KEY_PREFIX,
    ThreadHistoryCache,
    ThreadHistoryConfig,
)

# Deprecated: use ``ThreadHistoryCache`` from ``chat_sdk.thread_history``.
MessageHistoryCache = ThreadHistoryCache

# Deprecated: use ``ThreadHistoryConfig`` from ``chat_sdk.thread_history``.
MessageHistoryConfig = ThreadHistoryConfig

__all__ = [
    "DEFAULT_MAX_MESSAGES",
    "DEFAULT_TTL_MS",
    "KEY_PREFIX",
    "MessageHistoryCache",
    "MessageHistoryConfig",
]
