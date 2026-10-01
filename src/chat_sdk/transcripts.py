"""Deprecated: superseded by the unified History API (``chat.history.user``).

Import :class:`~chat_sdk.history.UserHistoryApiImpl` (or use
``chat.history.user`` at runtime) instead. This module is kept for backwards
compatibility: ``TranscriptsApiImpl`` is the same class object as
``UserHistoryApiImpl``. Related types (``TranscriptsApi``,
``TranscriptEntry``, ...) are exported from :mod:`chat_sdk.types` and the
package root.

Python port of ``transcripts.ts``.
"""

from __future__ import annotations

# The module constants are re-exported so pre-#197 imports keep working.
from chat_sdk.history.user import (
    DEFAULT_LIST_LIMIT,
    DEFAULT_MAX_PER_USER,
    DURATION_RE,
    KEY_PREFIX,
    MS_PER_UNIT,
    TOMBSTONE_MARKER,
    UserHistoryApiImpl,
)

TranscriptsApiImpl = UserHistoryApiImpl

__all__ = [
    "DEFAULT_LIST_LIMIT",
    "DEFAULT_MAX_PER_USER",
    "DURATION_RE",
    "KEY_PREFIX",
    "MS_PER_UNIT",
    "TOMBSTONE_MARKER",
    "TranscriptsApiImpl",
]
