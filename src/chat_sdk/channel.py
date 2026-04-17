"""Channel implementation for chat-sdk.

Python port of Vercel Chat SDK channel.ts.
Provides message posting (accumulates async streams), message history iteration,
thread enumeration, channel metadata, and channel state management.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from chat_sdk.errors import ChatNotImplementedError
from chat_sdk.plan import is_postable_object, post_postable_object
from chat_sdk.thread import (
    _ChannelImplConfigForThread,
    _ChatSingleton,
    _extract_message_content,
    _from_full_stream,
    _is_async_iterable,
    _to_message,
    get_chat_singleton,
    has_chat_singleton,
)
from chat_sdk.types import (
    THREAD_STATE_TTL_MS,
    Adapter,
    AdapterPostableMessage,
    Author,
    ChannelInfo,
    ChannelVisibility,
    EmojiValue,
    EphemeralMessage,
    FetchOptions,
    Message,
    MessageMetadata,
    PostableMarkdown,
    PostableMessage,
    PostEphemeralOptions,
    RawMessage,
    ScheduledMessage,
    SentMessage,
    StateAdapter,
    ThreadSummary,
)

# ---------------------------------------------------------------------------
# Channel state key prefix
# ---------------------------------------------------------------------------

CHANNEL_STATE_KEY_PREFIX = "channel-state:"


# ---------------------------------------------------------------------------
# Serialized channel data
# ---------------------------------------------------------------------------


@dataclass
class SerializedChannel:
    """Serialized channel data for passing to external systems."""

    _type: str = "chat:Channel"
    adapter_name: str = ""
    channel_visibility: ChannelVisibility | None = None
    id: str = ""
    is_dm: bool = False


# ---------------------------------------------------------------------------
# Config types
# ---------------------------------------------------------------------------


@dataclass
class _ChannelImplConfigWithAdapter:
    """Config with explicit adapter/state instances."""

    id: str
    adapter: Adapter
    state_adapter: StateAdapter
    channel_visibility: ChannelVisibility = "unknown"
    is_dm: bool = False
    message_history: Any = None


@dataclass
class _ChannelImplConfigLazy:
    """Config with lazy adapter resolution."""

    id: str
    adapter_name: str
    channel_visibility: ChannelVisibility = "unknown"
    is_dm: bool = False


# Union of accepted config types
_ChannelImplConfig = _ChannelImplConfigWithAdapter | _ChannelImplConfigLazy | _ChannelImplConfigForThread


# ---------------------------------------------------------------------------
# ChannelImpl
# ---------------------------------------------------------------------------


class ChannelImpl:
    """Concrete Channel implementation.

    Similar to ThreadImpl but simpler -- no streaming support at the channel level
    (async streams are accumulated and posted as a single message).
    """

    def __init__(self, config: _ChannelImplConfig) -> None:
        self._id = config.id
        self._is_dm = getattr(config, "is_dm", False)
        self._channel_visibility: ChannelVisibility = getattr(config, "channel_visibility", "unknown")
        self._name: str | None = None

        if isinstance(config, _ChannelImplConfigLazy):
            self._adapter: Adapter | None = None
            self._adapter_name: str | None = config.adapter_name
            self._state_adapter_instance: StateAdapter | None = None
            self._message_history: Any = None
        else:
            # _ChannelImplConfigWithAdapter, _ChannelImplConfigForThread,
            # or _ChannelImplConfigForChat (from chat.py) -- all have
            # adapter, state_adapter, and optional message_history attrs.
            self._adapter = config.adapter  # type: ignore[union-attr]
            self._adapter_name = None
            self._state_adapter_instance = config.state_adapter  # type: ignore[union-attr]
            self._message_history = getattr(config, "message_history", None)

    # -- Properties ----------------------------------------------------------

    @property
    def id(self) -> str:
        return self._id

    @property
    def is_dm(self) -> bool:
        return self._is_dm

    @property
    def channel_visibility(self) -> ChannelVisibility:
        return self._channel_visibility

    @property
    def adapter(self) -> Adapter:
        if self._adapter is not None:
            return self._adapter

        if not self._adapter_name:
            raise RuntimeError("Channel has no adapter configured")

        chat = get_chat_singleton()
        adapter = chat.get_adapter(self._adapter_name)
        if adapter is None:
            raise RuntimeError(f'Adapter "{self._adapter_name}" not found in Chat singleton')

        self._adapter = adapter
        return adapter

    @property
    def _state_adapter(self) -> StateAdapter:
        if self._state_adapter_instance is not None:
            return self._state_adapter_instance

        chat = get_chat_singleton()
        self._state_adapter_instance = chat.get_state()
        return self._state_adapter_instance

    @property
    def name(self) -> str | None:
        return self._name

    # -- State ---------------------------------------------------------------

    async def get_state(self) -> Any | None:
        """Get the current channel state."""
        return await self._state_adapter.get(f"{CHANNEL_STATE_KEY_PREFIX}{self._id}")

    async def set_state(
        self,
        new_state: dict[str, Any],
        *,
        replace: bool = False,
    ) -> None:
        """Set channel state. Merges with existing by default."""
        key = f"{CHANNEL_STATE_KEY_PREFIX}{self._id}"
        if replace:
            await self._state_adapter.set(key, new_state, THREAD_STATE_TTL_MS)
        else:
            existing = await self._state_adapter.get(key)
            merged = {**(existing or {}), **new_state}
            await self._state_adapter.set(key, merged, THREAD_STATE_TTL_MS)

    # -- Messages (async iterator, newest first) -----------------------------

    async def messages(self) -> AsyncIterator[Message]:
        """Iterate messages newest-first (backward from most recent).

        Uses adapter.fetch_channel_messages if available, otherwise falls back
        to adapter.fetch_messages with the channel ID.
        """
        adapter = self.adapter
        channel_id = self._id
        message_history = self._message_history
        cursor: str | None = None
        yielded_any = False

        while True:
            fetch_options = FetchOptions(cursor=cursor, direction="backward")
            if hasattr(adapter, "fetch_channel_messages") and adapter.fetch_channel_messages:  # type: ignore[union-attr]
                result = await adapter.fetch_channel_messages(channel_id, fetch_options)  # type: ignore[union-attr]
            else:
                result = await adapter.fetch_messages(channel_id, fetch_options)

            reversed_msgs = list(reversed(result.messages))
            for msg in reversed_msgs:
                yielded_any = True
                yield msg

            if not result.next_cursor or len(result.messages) == 0:
                break
            cursor = result.next_cursor

        # Fallback to cached history
        if not yielded_any and message_history is not None:
            cached: list[Message] = await message_history.get_messages(channel_id)
            for msg in reversed(cached):
                yield msg

    # -- Threads (async iterator, most recently active first) ----------------

    async def threads(self) -> AsyncIterator[ThreadSummary]:
        """Iterate threads in this channel, most recently active first."""
        adapter = self.adapter

        if not hasattr(adapter, "list_threads") or not adapter.list_threads:  # type: ignore[union-attr]
            return

        channel_id = self._id
        cursor: str | None = None

        while True:
            result = await adapter.list_threads(channel_id, {"cursor": cursor})  # type: ignore[union-attr]
            for thread in result.threads:
                yield thread

            if not result.next_cursor or len(result.threads) == 0:
                break
            cursor = result.next_cursor

    # -- Metadata ------------------------------------------------------------

    async def fetch_metadata(self) -> ChannelInfo:
        """Fetch channel metadata from the platform."""
        if hasattr(self.adapter, "fetch_channel_info") and self.adapter.fetch_channel_info:  # type: ignore[union-attr]
            info: ChannelInfo = await self.adapter.fetch_channel_info(self._id)  # type: ignore[union-attr]
            self._name = info.name
            return info

        return ChannelInfo(
            id=self._id,
            is_dm=self._is_dm,
            metadata={},
        )

    # -- Posting -------------------------------------------------------------

    async def post(
        self,
        message: PostableMessage | Any,
    ) -> SentMessage | Any:
        """Post a message to this channel.

        If the message is an AsyncIterable (streaming), accumulates all text
        and posts as a single message -- channels do not support real-time streaming.

        If the message is a PostableObject (e.g. Plan), it is posted via
        native adapter support or fallback text.
        """
        # Handle PostableObject (e.g. Plan)
        if is_postable_object(message):
            raw = await self._handle_postable_object(message)
            if self._message_history is not None and raw is not None:
                fallback = message.get_fallback_text() if hasattr(message, "get_fallback_text") else ""
                sent = self._create_sent_message(raw.id, PostableMarkdown(markdown=fallback), raw.thread_id)
                await self._message_history.append(self._id, _to_message(sent))
            return message

        if _is_async_iterable(message):
            accumulated = ""
            async for chunk in _from_full_stream(message):
                if isinstance(chunk, str):
                    accumulated += chunk
            return await self._post_single_message(PostableMarkdown(markdown=accumulated))

        postable: AdapterPostableMessage = message  # type: ignore[assignment]
        return await self._post_single_message(postable)

    async def _post_single_message(
        self,
        postable: AdapterPostableMessage,
    ) -> SentMessage:
        if hasattr(self.adapter, "post_channel_message") and self.adapter.post_channel_message:  # type: ignore[union-attr]
            raw_msg: RawMessage = await self.adapter.post_channel_message(self._id, postable)  # type: ignore[union-attr]
        else:
            raw_msg = await self.adapter.post_message(self._id, postable)

        sent = self._create_sent_message(raw_msg.id, postable, raw_msg.thread_id)

        if self._message_history is not None:
            await self._message_history.append(self._id, _to_message(sent))

        return sent

    async def _handle_postable_object(self, obj: Any) -> Any:
        """Post a PostableObject using native adapter support or fallback."""
        adapter = self.adapter

        async def _post_fn(thread_id: str, message: str) -> Any:
            if hasattr(adapter, "post_channel_message") and adapter.post_channel_message:
                return await adapter.post_channel_message(thread_id, message)
            return await adapter.post_message(thread_id, message)

        return await post_postable_object(obj, adapter, self._id, _post_fn)

    async def post_ephemeral(
        self,
        user: str | Author,
        message: AdapterPostableMessage,
        options: PostEphemeralOptions,
    ) -> EphemeralMessage | None:
        """Post an ephemeral message visible only to the specified user."""
        user_id = user if isinstance(user, str) else user.user_id

        if hasattr(self.adapter, "post_ephemeral") and self.adapter.post_ephemeral:  # type: ignore[union-attr]
            return await self.adapter.post_ephemeral(self._id, user_id, message)  # type: ignore[union-attr]

        if not options.fallback_to_dm:
            return None

        if hasattr(self.adapter, "open_dm") and self.adapter.open_dm:  # type: ignore[union-attr]
            dm_thread_id: str = await self.adapter.open_dm(user_id)  # type: ignore[union-attr]
            result: RawMessage = await self.adapter.post_message(dm_thread_id, message)
            return EphemeralMessage(
                id=result.id,
                thread_id=dm_thread_id,
                used_fallback=True,
                raw=result.raw,
            )

        return None

    async def schedule(
        self,
        message: AdapterPostableMessage,
        *,
        post_at: datetime,
    ) -> ScheduledMessage:
        """Schedule a message for future delivery."""
        if not hasattr(self.adapter, "schedule_message") or not self.adapter.schedule_message:  # type: ignore[union-attr]
            raise ChatNotImplementedError(
                self.adapter.name,
                "scheduling",
            )
        return await self.adapter.schedule_message(self._id, message, {"post_at": post_at})  # type: ignore[union-attr]

    # -- Typing indicator ----------------------------------------------------

    async def start_typing(self, status: str | None = None) -> None:
        await self.adapter.start_typing(self._id, status)

    # -- Mention helper ------------------------------------------------------

    def mention_user(self, user_id: str) -> str:
        return f"<@{user_id}>"

    # -- Serialization -------------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        """Serialize to a plain dict for external systems.

        Output uses camelCase keys to match the TypeScript SDK.
        """
        return {
            "_type": "chat:Channel",
            "id": self._id,
            "adapterName": self._adapter_name or self.adapter.name,
            "channelVisibility": self._channel_visibility,
            "isDM": self._is_dm,
        }

    @classmethod
    def from_json(
        cls,
        data: dict[str, Any] | ChannelImpl,
        adapter: Adapter | None = None,
        chat: _ChatSingleton | None = None,
    ) -> ChannelImpl:
        """Reconstruct a ChannelImpl from serialized JSON data.

        Parameters
        ----------
        data:
            Serialized channel dict (camelCase or snake_case keys accepted).
        adapter:
            Explicit adapter. Skips singleton lookup.
        chat:
            Explicit Chat instance for adapter/state resolution.

        Idempotent: if ``data`` is already a :class:`ChannelImpl`, it is
        returned unchanged. This makes it safe to call via
        ``json.loads(..., object_hook=reviver)``.
        """
        if isinstance(data, ChannelImpl):
            return data
        channel = cls(
            _ChannelImplConfigLazy(
                id=data["id"],
                adapter_name=data.get("adapterName") or data.get("adapter_name", ""),
                channel_visibility=data.get("channelVisibility") or data.get("channel_visibility", "unknown"),
                is_dm=data.get("isDM") if "isDM" in data else data.get("is_dm", False),
            )
        )
        if adapter is not None:
            channel._adapter = adapter
            # Divergence from upstream — see docs/UPSTREAM_SYNC.md.
            # Keep _adapter_name in sync with the explicit adapter so
            # to_json() doesn't serialize a stale name.
            channel._adapter_name = adapter.name
        elif chat is not None:
            if channel._adapter_name:
                resolved = chat.get_adapter(channel._adapter_name)
                if resolved is None:
                    raise RuntimeError(f'Adapter "{channel._adapter_name}" not found in the provided Chat instance')
                channel._adapter = resolved
            channel._state_adapter_instance = chat.get_state()
        elif has_chat_singleton() and channel._adapter_name:
            active = get_chat_singleton()
            resolved = active.get_adapter(channel._adapter_name)
            if resolved is not None:
                channel._adapter = resolved
            channel._state_adapter_instance = active.get_state()
        return channel

    @classmethod
    def from_json_compat(
        cls,
        data: dict[str, Any],
        adapter: Adapter | None = None,
    ) -> ChannelImpl:
        """Reconstruct a ChannelImpl from serialized JSON data with TS interop.

        Like :meth:`from_json` but explicitly accepts both camelCase and
        snake_case keys for cross-SDK compatibility.
        """
        return cls.from_json(data, adapter=adapter)

    # -- SentMessage construction --------------------------------------------

    def _create_sent_message(
        self,
        message_id: str,
        postable: AdapterPostableMessage,
        thread_id_override: str | None = None,
    ) -> SentMessage:
        adapter = self.adapter
        thread_id = thread_id_override or self._id
        channel_impl = self

        plain_text, formatted, attachments = _extract_message_content(postable)

        async def _edit(new_content: Any) -> SentMessage:
            await adapter.edit_message(thread_id, message_id, new_content)
            return channel_impl._create_sent_message(message_id, new_content)

        async def _delete() -> None:
            await adapter.delete_message(thread_id, message_id)

        async def _add_reaction(emoji: EmojiValue | str) -> None:
            await adapter.add_reaction(thread_id, message_id, emoji)

        async def _remove_reaction(emoji: EmojiValue | str) -> None:
            await adapter.remove_reaction(thread_id, message_id, emoji)

        return SentMessage(
            id=message_id,
            thread_id=thread_id,
            text=plain_text,
            formatted=formatted,
            author=Author(
                user_id="self",
                user_name=adapter.user_name,
                full_name=adapter.user_name,
                is_bot=True,
                is_me=True,
            ),
            metadata=MessageMetadata(
                date_sent=datetime.now(tz=timezone.utc),
                edited=False,
            ),
            attachments=attachments,
            links=[],
            raw=None,
            _edit=_edit,
            _delete=_delete,
            _add_reaction=_add_reaction,
            _remove_reaction=_remove_reaction,
        )


# ---------------------------------------------------------------------------
# Utility: derive channel ID from thread ID
# ---------------------------------------------------------------------------


def derive_channel_id(adapter: Adapter, thread_id: str) -> str:
    """Derive the channel ID from a thread ID using the adapter."""
    return adapter.channel_id_from_thread_id(thread_id)
