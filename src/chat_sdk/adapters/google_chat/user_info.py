"""User info caching utilities for Google Chat adapter.

Google Chat Pub/Sub messages don't include user display names,
so we cache them from direct webhook messages for later use.

Python port of user-info.ts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from chat_sdk.logger import Logger
from chat_sdk.types import StateAdapter

# Key prefix for user info cache
USER_INFO_KEY_PREFIX = "gchat:user:"
# TTL for user info cache (7 days)
USER_INFO_CACHE_TTL_MS = 7 * 24 * 60 * 60 * 1000


@dataclass
class CachedUserInfo:
    """Cached user info."""

    display_name: str
    email: str | None = None


class UserInfoCache:
    """User info cache that stores display names for Google Chat users.

    Uses both in-memory cache (fast path) and persistent state adapter.
    """

    _MAX_CACHE_SIZE = 1000

    def __init__(
        self,
        state: StateAdapter | None,
        logger: Logger,
    ) -> None:
        self._in_memory_cache: dict[str, CachedUserInfo] = {}
        self._state = state
        self._logger = logger

    async def set(
        self,
        user_id: str,
        display_name: str,
        email: str | None = None,
    ) -> None:
        """Cache user info for later lookup."""
        if not display_name or display_name == "unknown":
            return

        user_info = CachedUserInfo(display_name=display_name, email=email)

        # Always update in-memory cache
        self._in_memory_cache[user_id] = user_info

        # Evict oldest entries when cache exceeds max size
        if len(self._in_memory_cache) > self._MAX_CACHE_SIZE:
            # Remove the oldest entries (first inserted in dict order)
            excess = len(self._in_memory_cache) - self._MAX_CACHE_SIZE
            keys_to_remove = list(self._in_memory_cache.keys())[:excess]
            for key in keys_to_remove:
                del self._in_memory_cache[key]

        # Also persist to state adapter if available
        if self._state:
            cache_key = f"{USER_INFO_KEY_PREFIX}{user_id}"
            await self._state.set(
                cache_key,
                {"display_name": display_name, "email": email},
                USER_INFO_CACHE_TTL_MS,
            )

    async def get(self, user_id: str) -> CachedUserInfo | None:
        """Get cached user info.

        Checks in-memory cache first, then falls back to state adapter.
        """
        # Check in-memory cache first (fast path)
        in_memory = self._in_memory_cache.get(user_id)
        if in_memory:
            return in_memory

        # Fall back to state adapter
        if not self._state:
            return None

        cache_key = f"{USER_INFO_KEY_PREFIX}{user_id}"
        from_state: Any = await self._state.get(cache_key)

        # Populate in-memory cache if found in state
        if from_state:
            info = CachedUserInfo(
                display_name=from_state.get("display_name", "unknown")
                if isinstance(from_state, dict)
                else getattr(from_state, "display_name", "unknown"),
                email=from_state.get("email") if isinstance(from_state, dict) else getattr(from_state, "email", None),
            )
            self._in_memory_cache[user_id] = info
            return info

        return None

    async def resolve_display_name(
        self,
        user_id: str,
        provided_display_name: str | None,
        bot_user_id: str | None,
        bot_user_name: str,
    ) -> str:
        """Resolve user display name, using cache if available.

        Args:
            user_id: The user's resource name (e.g., "users/123456")
            provided_display_name: Display name from the message if available
            bot_user_id: The bot's user ID (for self-identification)
            bot_user_name: The bot's configured username
        """
        # If display name is provided and not "unknown", use it
        if provided_display_name and provided_display_name != "unknown":
            # Also cache it for future use
            try:
                await self.set(user_id, provided_display_name)
            except Exception as err:
                self._logger.error(
                    "Failed to cache user info",
                    {"user_id": user_id, "error": err},
                )
            return provided_display_name

        # If this is our bot's user ID, use the configured bot name
        if bot_user_id and user_id == bot_user_id:
            return bot_user_name

        # Try to get from cache
        cached = await self.get(user_id)
        if cached and cached.display_name:
            return cached.display_name

        # Fall back to extracting name from userId (e.g., "users/123" -> "User 123")
        return user_id.replace("users/", "User ")
