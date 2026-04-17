"""Thread implementation for chat-sdk.

Python port of Vercel Chat SDK thread.ts.
Provides message posting (with streaming fallback), message history iteration,
ephemeral messages, scheduled messages, thread subscription, and state management.
"""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from chat_sdk.errors import ChatNotImplementedError
from chat_sdk.logger import Logger
from chat_sdk.plan import is_postable_object, post_postable_object
from chat_sdk.shared.streaming_markdown import StreamingMarkdownRenderer
from chat_sdk.types import (
    THREAD_STATE_TTL_MS,
    Adapter,
    AdapterPostableMessage,
    Attachment,
    Author,
    ChannelVisibility,
    EmojiValue,
    EphemeralMessage,
    FetchOptions,
    FormattedContent,
    MarkdownTextChunk,
    Message,
    MessageMetadata,
    PostableCard,
    PostableMarkdown,
    PostableMessage,
    PostableRaw,
    PostEphemeralOptions,
    RawMessage,
    ScheduledMessage,
    SentMessage,
    StateAdapter,
    StreamChunk,
    StreamOptions,
)

if TYPE_CHECKING:
    from chat_sdk.channel import ChannelImpl


# ---------------------------------------------------------------------------
# Chat resolver: ContextVar → process-global → error
# ---------------------------------------------------------------------------

_default_chat: _ChatSingleton | None = None
_active_chat: contextvars.ContextVar[_ChatSingleton | None] = contextvars.ContextVar("_active_chat", default=None)


@runtime_checkable
class _ChatSingleton(Protocol):
    """Minimal interface for the Chat singleton to avoid circular imports."""

    def get_adapter(self, name: str) -> Adapter | None: ...
    def get_state(self) -> StateAdapter: ...


def set_chat_singleton(chat: _ChatSingleton) -> None:
    """Register *chat* as the process-global default."""
    global _default_chat
    _default_chat = chat


def get_chat_singleton() -> _ChatSingleton:
    """Resolve the active Chat instance.

    Resolution order:
    1. ContextVar for the current async task (set via ``chat.activate()``)
    2. Process-global default (set via ``set_chat_singleton()``)
    3. Raise RuntimeError
    """
    ctx = _active_chat.get()
    if ctx is not None:
        return ctx
    if _default_chat is not None:
        return _default_chat
    raise RuntimeError("No Chat instance available. Use chat.activate() or register a singleton.")


def has_chat_singleton() -> bool:
    return _active_chat.get() is not None or _default_chat is not None


def clear_chat_singleton() -> None:
    global _default_chat
    _default_chat = None
    _active_chat.set(None)


# ---------------------------------------------------------------------------
# Serialized thread data
# ---------------------------------------------------------------------------


@dataclass
class SerializedThread:
    """Serialized thread data for passing to external systems."""

    _type: str = "chat:Thread"
    adapter_name: str = ""
    channel_id: str = ""
    channel_visibility: ChannelVisibility | None = None
    current_message: dict[str, Any] | None = None
    id: str = ""
    is_dm: bool = False


# ---------------------------------------------------------------------------
# Thread state key prefix
# ---------------------------------------------------------------------------

THREAD_STATE_KEY_PREFIX = "thread-state:"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_async_iterable(value: Any) -> bool:
    """Check if a value is an AsyncIterable (e.g. AI SDK textStream/fullStream)."""
    return value is not None and hasattr(value, "__aiter__")


def _extract_message_content(
    message: AdapterPostableMessage,
) -> tuple[str, FormattedContent, list[Attachment]]:
    """Extract plain text, formatted content, and attachments from a message.

    Returns (plain_text, formatted, attachments).
    """
    if isinstance(message, str):
        return (
            message,
            {"type": "root", "children": [{"type": "paragraph", "children": [{"type": "text", "value": message}]}]},
            [],
        )

    if isinstance(message, PostableRaw):
        return (
            message.raw,
            {"type": "root", "children": [{"type": "paragraph", "children": [{"type": "text", "value": message.raw}]}]},
            list(message.attachments or []),
        )

    if isinstance(message, PostableMarkdown):
        from chat_sdk.shared.markdown_parser import ast_to_plain_text, parse_markdown

        formatted = parse_markdown(message.markdown)
        plain = ast_to_plain_text(formatted)
        return (
            plain,
            formatted,
            list(message.attachments or []),
        )

    if isinstance(message, PostableCard):
        from chat_sdk.cards import card_to_fallback_text

        fallback = message.fallback_text or card_to_fallback_text(message.card) or "[card]"
        return (
            fallback,
            {"type": "root", "children": [{"type": "paragraph", "children": [{"type": "text", "value": fallback}]}]},
            [],
        )

    if hasattr(message, "ast"):
        from chat_sdk.shared.markdown_parser import ast_to_plain_text

        ast_dict = message.ast  # type: ignore[union-attr]
        plain = ast_to_plain_text(ast_dict)
        return plain, ast_dict, list(getattr(message, "attachments", None) or [])

    if isinstance(message, dict):
        # CardElement (dict-based)
        from chat_sdk.cards import card_to_fallback_text

        fallback = card_to_fallback_text(message) or "[card]"
        return (
            fallback,
            {"type": "root", "children": [{"type": "paragraph", "children": [{"type": "text", "value": fallback}]}]},
            [],
        )

    raise ValueError("Invalid PostableMessage format")


# ---------------------------------------------------------------------------
# ThreadImpl
# ---------------------------------------------------------------------------


@dataclass
class _ThreadImplConfig:
    """Config for creating a ThreadImpl."""

    id: str
    channel_id: str = ""
    channel_visibility: ChannelVisibility = "unknown"
    current_message: Message | None = None
    fallback_streaming_placeholder_text: str | None = "..."
    initial_message: Message | None = None
    is_dm: bool = False
    is_subscribed_context: bool = False
    logger: Logger | None = None
    streaming_update_interval_ms: int = 500

    # Direct adapter mode
    adapter: Adapter | None = None
    state_adapter: StateAdapter | None = None
    message_history: Any = None  # MessageHistoryCache

    # Lazy resolution mode
    adapter_name: str | None = None


class ThreadImpl:
    """Concrete Thread implementation.

    Supports two construction modes:
    - *direct*: pass ``adapter`` and ``state_adapter`` explicitly.
    - *lazy*: pass ``adapter_name`` and let the singleton resolve them later.
    """

    def __init__(self, config: _ThreadImplConfig) -> None:
        self._id = config.id
        self._channel_id = config.channel_id
        self._is_dm = config.is_dm
        self._channel_visibility: ChannelVisibility = config.channel_visibility
        self._is_subscribed_context = config.is_subscribed_context
        self._current_message = config.current_message
        self._logger = config.logger
        self._streaming_update_interval_ms = config.streaming_update_interval_ms
        self._fallback_streaming_placeholder_text = config.fallback_streaming_placeholder_text

        # Recent messages cache
        self._recent_messages: list[Message] = []
        if config.initial_message is not None:
            self._recent_messages = [config.initial_message]

        # Direct vs lazy
        self._adapter: Adapter | None = config.adapter
        self._adapter_name: str | None = config.adapter_name
        self._state_adapter_instance: StateAdapter | None = config.state_adapter
        self._message_history: Any = config.message_history

        # Lazy channel cache
        self._channel_cache: ChannelImpl | None = None

    # -- Properties ----------------------------------------------------------

    @property
    def id(self) -> str:
        return self._id

    @property
    def channel_id(self) -> str:
        return self._channel_id

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
            raise RuntimeError("Thread has no adapter configured")

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
    def recent_messages(self) -> list[Message]:
        return self._recent_messages

    @recent_messages.setter
    def recent_messages(self, messages: list[Message]) -> None:
        self._recent_messages = messages

    # -- State ---------------------------------------------------------------

    async def get_state(self) -> Any | None:
        """Get the current thread state. Returns None if no state has been set."""
        return await self._state_adapter.get(f"{THREAD_STATE_KEY_PREFIX}{self._id}")

    async def set_state(
        self,
        state: dict[str, Any],
        *,
        replace: bool = False,
    ) -> None:
        """Set thread state. Merges with existing by default.

        State is persisted for 30 days.
        """
        key = f"{THREAD_STATE_KEY_PREFIX}{self._id}"
        if replace:
            await self._state_adapter.set(key, state, THREAD_STATE_TTL_MS)
        else:
            existing = await self._state_adapter.get(key)
            merged = {**(existing or {}), **state}
            await self._state_adapter.set(key, merged, THREAD_STATE_TTL_MS)

    # -- Channel -------------------------------------------------------------

    @property
    def channel(self) -> ChannelImpl:
        """Get the Channel containing this thread. Lazy-created and cached."""
        if self._channel_cache is None:
            from chat_sdk.channel import ChannelImpl, derive_channel_id

            channel_id = derive_channel_id(self.adapter, self._id)
            self._channel_cache = ChannelImpl(
                _ChannelImplConfigForThread(
                    id=channel_id,
                    adapter=self.adapter,
                    state_adapter=self._state_adapter,
                    is_dm=self._is_dm,
                    channel_visibility=self._channel_visibility,
                    message_history=self._message_history,
                )
            )
        return self._channel_cache

    # -- Messages (async iterators) -----------------------------------------

    async def messages(self) -> AsyncIterator[Message]:
        """Iterate messages newest-first (backward from most recent).

        Auto-paginates lazily.
        """
        adapter = self.adapter
        thread_id = self._id
        message_history = self._message_history
        cursor: str | None = None
        yielded_any = False

        while True:
            result = await adapter.fetch_messages(
                thread_id,
                FetchOptions(cursor=cursor, direction="backward"),
            )
            reversed_msgs = list(reversed(result.messages))
            for msg in reversed_msgs:
                yielded_any = True
                yield msg

            if not result.next_cursor or len(result.messages) == 0:
                break
            cursor = result.next_cursor

        # Fallback to cached history
        if not yielded_any and message_history is not None:
            cached: list[Message] = await message_history.get_messages(thread_id)
            for msg in reversed(cached):
                yield msg

    async def all_messages(self) -> AsyncIterator[Message]:
        """Iterate messages oldest-first (forward from beginning).

        Auto-paginates lazily.
        """
        adapter = self.adapter
        thread_id = self._id
        message_history = self._message_history
        cursor: str | None = None
        yielded_any = False

        while True:
            result = await adapter.fetch_messages(
                thread_id,
                FetchOptions(limit=100, cursor=cursor, direction="forward"),
            )
            for msg in result.messages:
                yielded_any = True
                yield msg

            if not result.next_cursor or len(result.messages) == 0:
                break
            cursor = result.next_cursor

        if not yielded_any and message_history is not None:
            cached: list[Message] = await message_history.get_messages(thread_id)
            for msg in cached:
                yield msg

    # -- Subscriptions -------------------------------------------------------

    async def is_subscribed(self) -> bool:
        if self._is_subscribed_context:
            return True
        return await self._state_adapter.is_subscribed(self._id)

    async def subscribe(self) -> None:
        await self._state_adapter.subscribe(self._id)
        if hasattr(self.adapter, "on_thread_subscribe") and self.adapter.on_thread_subscribe:  # type: ignore[union-attr]
            await self.adapter.on_thread_subscribe(self._id)  # type: ignore[union-attr]

    async def unsubscribe(self) -> None:
        await self._state_adapter.unsubscribe(self._id)

    # -- Posting -------------------------------------------------------------

    async def post(
        self,
        message: PostableMessage | Any,
    ) -> SentMessage | Any:
        """Post a message to this thread.

        Accepts a plain string, PostableMessage, AsyncIterable for streaming,
        or a PostableObject (e.g. Plan). PostableObjects are returned directly
        after posting so the caller can continue to mutate them.
        """
        # Handle PostableObject (e.g. Plan)
        if is_postable_object(message):
            raw = await self._handle_postable_object(message)
            # Cache in history with the real message ID (upstream skips this,
            # but that's a gap — posted messages should appear in history).
            if self._message_history is not None and raw is not None:
                fallback = message.get_fallback_text() if hasattr(message, "get_fallback_text") else ""
                sent = self._create_sent_message(raw.id, PostableMarkdown(markdown=fallback), raw.thread_id)
                await self._message_history.append(self._id, _to_message(sent))
            return message

        # Handle AsyncIterable (streaming)
        if _is_async_iterable(message):
            return await self._handle_stream(message)

        postable: AdapterPostableMessage = message  # type: ignore[assignment]
        raw_msg = await self.adapter.post_message(self._id, postable)
        result = self._create_sent_message(raw_msg.id, postable, raw_msg.thread_id)

        if self._message_history is not None:
            await self._message_history.append(self._id, _to_message(result))

        return result

    async def _handle_postable_object(self, obj: Any) -> Any:
        """Post a PostableObject using native adapter support or fallback."""
        return await post_postable_object(
            obj,
            self.adapter,
            self._id,
            lambda thread_id, message: self.adapter.post_message(thread_id, message),
            self._logger,
        )

    async def post_ephemeral(
        self,
        user: str | Author,
        message: AdapterPostableMessage,
        options: PostEphemeralOptions,
    ) -> EphemeralMessage | None:
        """Post an ephemeral message visible only to the specified user.

        Falls back to DM if the adapter does not support native ephemeral and
        ``options.fallback_to_dm`` is True.
        """
        user_id = user if isinstance(user, str) else user.user_id

        # Try native ephemeral
        if hasattr(self.adapter, "post_ephemeral") and self.adapter.post_ephemeral:  # type: ignore[union-attr]
            return await self.adapter.post_ephemeral(self._id, user_id, message)  # type: ignore[union-attr]

        if not options.fallback_to_dm:
            return None

        # Fallback: send via DM
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

    # -- Streaming -----------------------------------------------------------

    async def _handle_stream(
        self,
        raw_stream: Any,
    ) -> SentMessage:
        """Handle streaming from an AsyncIterable.

        Uses adapter's native streaming if available, otherwise falls back to post+edit.
        """
        # Build text-only stream from raw_stream
        text_stream = _from_full_stream(raw_stream)

        # Build streaming options from current message context
        options = StreamOptions()
        if self._current_message is not None:
            options.recipient_user_id = self._current_message.author.user_id
            raw = self._current_message.raw
            if isinstance(raw, dict):
                options.recipient_team_id = raw.get("team_id") or raw.get("team")

        # Use native streaming if adapter supports it
        if hasattr(self.adapter, "stream") and self.adapter.stream:  # type: ignore[union-attr]
            accumulated = ""

            async def _wrapped_stream() -> AsyncIterator[str | StreamChunk | dict[str, Any]]:
                nonlocal accumulated
                async for chunk in text_stream:
                    if isinstance(chunk, str):
                        accumulated += chunk
                    elif isinstance(chunk, dict) and chunk.get("type") == "markdown_text":
                        accumulated += chunk.get("text", "")
                    elif isinstance(chunk, MarkdownTextChunk):
                        accumulated += chunk.text
                    yield chunk

            raw_result = await self.adapter.stream(self._id, _wrapped_stream(), options)  # type: ignore[union-attr]
            sent = self._create_sent_message(
                raw_result.id,
                PostableMarkdown(markdown=accumulated),
                raw_result.thread_id,
            )
            if self._message_history is not None:
                await self._message_history.append(self._id, _to_message(sent))
            return sent

        # Fallback: post + edit with throttling (text-only)
        async def _text_only_stream() -> AsyncIterator[str]:
            async for chunk in text_stream:
                if isinstance(chunk, str):
                    yield chunk
                elif isinstance(chunk, dict) and chunk.get("type") == "markdown_text":
                    yield chunk.get("text", "")
                elif isinstance(chunk, MarkdownTextChunk):
                    yield chunk.text
                # Skip non-text chunks in fallback mode

        return await self._fallback_stream(_text_only_stream(), options)

    async def _fallback_stream(
        self,
        text_stream: Any,
        options: StreamOptions | None = None,
    ) -> SentMessage:
        """Fallback streaming using post + edit.

        Posts an initial placeholder, then edits the message at intervals as
        new text arrives from the stream.
        """
        interval_ms = (
            options.update_interval_ms if options and options.update_interval_ms else self._streaming_update_interval_ms
        )
        interval_s = interval_ms / 1000.0
        placeholder_text = self._fallback_streaming_placeholder_text

        msg: RawMessage | None = None
        if placeholder_text is not None:
            msg = await self.adapter.post_message(self._id, placeholder_text)

        thread_id_for_edits = self._id
        renderer = StreamingMarkdownRenderer()
        last_edit_content = ""
        stop_event = asyncio.Event()
        pending_edit: asyncio.Task[None] | None = None

        if msg is not None:
            thread_id_for_edits = msg.thread_id or self._id
            last_edit_content = placeholder_text or ""

        # Background edit loop
        async def _edit_loop() -> None:
            nonlocal last_edit_content
            while not stop_event.is_set() and msg is not None:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
                    break  # stop was signaled
                except asyncio.TimeoutError:  # noqa: UP041 — support Python 3.10
                    pass  # interval elapsed, do the edit
                if stop_event.is_set() or msg is None:
                    break
                content = renderer.get_committable_text()
                if content.strip() and content != last_edit_content:
                    try:
                        await self.adapter.edit_message(
                            thread_id_for_edits,
                            msg.id,
                            PostableMarkdown(markdown=content),
                        )
                        last_edit_content = content
                    except Exception as exc:
                        if self._logger:
                            self._logger.warn("fallbackStream edit failed", exc)

        if msg is not None:
            pending_edit = asyncio.create_task(_edit_loop())

        try:
            async for chunk in text_stream:
                renderer.push(chunk)
                if msg is None:
                    content = renderer.get_committable_text()
                    if content.strip():
                        msg = await self.adapter.post_message(self._id, PostableMarkdown(markdown=content))
                        thread_id_for_edits = msg.thread_id or self._id
                        last_edit_content = content
                        pending_edit = asyncio.create_task(_edit_loop())
        finally:
            stop_event.set()

        if pending_edit is not None:
            await pending_edit

        accumulated = renderer.get_text()
        final_content = renderer.finish()

        # Final message
        if msg is None:
            # Stream contract requires a SentMessage, so post at least a space
            # if the stream produced only whitespace.
            markdown = accumulated if accumulated.strip() else " "
            msg = await self.adapter.post_message(self._id, PostableMarkdown(markdown=markdown))
            thread_id_for_edits = msg.thread_id or self._id
            last_edit_content = accumulated

        # Always ensure the final content is sent, regardless of what _edit_loop did.
        # Re-check last_edit_content after awaiting pending_edit since _edit_loop
        # may have updated it concurrently.
        if final_content.strip() and final_content != last_edit_content:
            await self.adapter.edit_message(
                thread_id_for_edits,
                msg.id,
                PostableMarkdown(markdown=final_content),
            )
        elif placeholder_text is not None and not final_content.strip() and last_edit_content == placeholder_text:
            # Divergence from upstream 4.26: upstream leaves the placeholder
            # visible when the stream produces only whitespace, which strands
            # "..." on the message forever. We replace it with " " so the
            # placeholder is cleared consistently with the no-placeholder branch
            # (which also posts " " in this case). See docs/UPSTREAM_SYNC.md.
            await self.adapter.edit_message(
                thread_id_for_edits,
                msg.id,
                PostableMarkdown(markdown=" "),
            )
            last_edit_content = " "

        sent = self._create_sent_message(
            msg.id,
            PostableMarkdown(markdown=final_content),
            thread_id_for_edits,
        )
        if self._message_history is not None:
            await self._message_history.append(self._id, _to_message(sent))

        return sent

    # -- Typing indicator ----------------------------------------------------

    async def start_typing(self, status: str | None = None) -> None:
        await self.adapter.start_typing(self._id, status)

    # -- Refresh -------------------------------------------------------------

    async def refresh(self) -> None:
        """Reload recent messages from the adapter."""
        result = await self.adapter.fetch_messages(self._id, FetchOptions(limit=50))
        if result.messages:
            self._recent_messages = result.messages
        elif self._message_history is not None:
            self._recent_messages = await self._message_history.get_messages(self._id, 50)
        else:
            self._recent_messages = []

    # -- Mention helper ------------------------------------------------------

    def mention_user(self, user_id: str) -> str:
        return f"<@{user_id}>"

    # -- Serialization -------------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        """Serialize to a plain dict for external systems.

        Output uses camelCase keys to match the TypeScript SDK.
        """
        return {
            "_type": "chat:Thread",
            "id": self._id,
            "channelId": self._channel_id,
            "channelVisibility": self._channel_visibility,
            "currentMessage": self._current_message.to_json() if self._current_message else None,
            "isDM": self._is_dm,
            "adapterName": self._adapter_name or self.adapter.name,
        }

    @classmethod
    def from_json(
        cls,
        data: dict[str, Any] | ThreadImpl,
        adapter: Adapter | None = None,
        chat: _ChatSingleton | None = None,
    ) -> ThreadImpl:
        """Reconstruct a ThreadImpl from serialized JSON data.

        Parameters
        ----------
        data:
            Serialized thread dict (camelCase or snake_case keys accepted).
        adapter:
            Explicit adapter to use. Skips singleton lookup for adapter resolution.
        chat:
            Explicit Chat instance. If provided, adapter and state are resolved
            from this instance instead of the singleton. Useful in multi-chat
            or test scenarios.

        Idempotent: if ``data`` is already a :class:`ThreadImpl`, it is
        returned unchanged. This makes it safe to call via
        ``json.loads(..., object_hook=reviver)``.
        """
        if isinstance(data, ThreadImpl):
            return data
        current_msg_raw = data.get("currentMessage") or data.get("current_message")
        # ``object_hook`` revives nested dicts first, so ``currentMessage`` may
        # already be a Message instance by the time this runs.
        current_msg = Message.from_json(current_msg_raw) if current_msg_raw else None

        thread = cls(
            _ThreadImplConfig(
                id=data["id"],
                adapter_name=data.get("adapterName") or data.get("adapter_name", ""),
                channel_id=data.get("channelId") or data.get("channel_id", ""),
                channel_visibility=data.get("channelVisibility") or data.get("channel_visibility", "unknown"),
                current_message=current_msg,
                is_dm=data.get("isDM") if "isDM" in data else data.get("is_dm", False),
            )
        )
        if adapter is not None:
            thread._adapter = adapter
            # Keep _adapter_name in sync with the explicit adapter so
            # to_json() doesn't serialize a stale name after rebind.
            thread._adapter_name = adapter.name
        elif chat is not None:
            if thread._adapter_name:
                resolved = chat.get_adapter(thread._adapter_name)
                if resolved is None:
                    raise RuntimeError(f'Adapter "{thread._adapter_name}" not found in the provided Chat instance')
                thread._adapter = resolved
            thread._state_adapter_instance = chat.get_state()
        elif has_chat_singleton() and thread._adapter_name:
            # Eagerly bind from the active/global chat so the thread doesn't
            # lazily re-resolve later (which could hit a different chat).
            active = get_chat_singleton()
            resolved = active.get_adapter(thread._adapter_name)
            if resolved is not None:
                thread._adapter = resolved
            thread._state_adapter_instance = active.get_state()
        return thread

    @classmethod
    def from_json_compat(
        cls,
        data: dict[str, Any],
        adapter: Adapter | None = None,
    ) -> ThreadImpl:
        """Reconstruct a ThreadImpl from serialized JSON data with TS interop.

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
        thread_impl = self

        plain_text, formatted, attachments = _extract_message_content(postable)

        async def _edit(new_content: Any) -> SentMessage:
            await adapter.edit_message(thread_id, message_id, new_content)
            return thread_impl._create_sent_message(message_id, new_content)

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

    def create_sent_message_from_message(self, message: Message) -> SentMessage:
        """Create a SentMessage from an existing Message (for modal context)."""
        adapter = self.adapter
        thread_id = self._id
        message_id = message.id
        thread_impl = self

        async def _edit(new_content: Any) -> SentMessage:
            await adapter.edit_message(thread_id, message_id, new_content)
            return thread_impl._create_sent_message(message_id, new_content, thread_id)

        async def _delete() -> None:
            await adapter.delete_message(thread_id, message_id)

        async def _add_reaction(emoji: EmojiValue | str) -> None:
            await adapter.add_reaction(thread_id, message_id, emoji)

        async def _remove_reaction(emoji: EmojiValue | str) -> None:
            await adapter.remove_reaction(thread_id, message_id, emoji)

        return SentMessage(
            id=message.id,
            thread_id=message.thread_id,
            text=message.text,
            formatted=message.formatted,
            author=message.author,
            metadata=message.metadata,
            attachments=message.attachments,
            links=message.links,
            is_mention=message.is_mention,
            raw=message.raw,
            _edit=_edit,
            _delete=_delete,
            _add_reaction=_add_reaction,
            _remove_reaction=_remove_reaction,
        )


# ---------------------------------------------------------------------------
# Helper: convert SentMessage -> Message for history caching
# ---------------------------------------------------------------------------


def _to_message(sent: SentMessage) -> Message:
    return Message(
        id=sent.id,
        thread_id=sent.thread_id,
        text=sent.text,
        formatted=sent.formatted,
        author=sent.author,
        metadata=sent.metadata,
        attachments=sent.attachments,
        links=sent.links,
        is_mention=sent.is_mention,
        raw=sent.raw,
    )


# ---------------------------------------------------------------------------
# Helper: normalise async stream (mirrors from-full-stream.ts)
# ---------------------------------------------------------------------------


async def _from_full_stream(raw_stream: Any) -> AsyncIterator[str | StreamChunk | dict[str, Any]]:
    """Normalise a raw async iterable into str or StreamChunk items.

    Handles plain strings, AI SDK fullStream events, and StreamChunk objects.
    Mirrors from-full-stream.ts: tracks ``finish-step`` events so that a
    ``"\n\n"`` separator is emitted between consecutive steps.
    """
    needs_separator = False
    has_emitted_text = False

    async for item in raw_stream:
        if isinstance(item, str):
            yield item
            continue

        if hasattr(item, "type"):
            # StreamChunk or StreamEvent
            item_type = item.type

            # Pass through known StreamChunk types
            if item_type in ("markdown_text", "task_update", "plan_update"):
                yield item
                continue

            # AI SDK v6 uses "text", v5 uses "textDelta"; also accept "delta"
            if item_type == "text-delta":
                text_content = next(
                    (
                        v
                        for k in ("text", "delta", "textDelta", "text_delta")
                        if (v := getattr(item, k, None)) is not None
                    ),
                    "",
                )
                if isinstance(text_content, str) and text_content:
                    if needs_separator and has_emitted_text:
                        yield "\n\n"
                    needs_separator = False
                    has_emitted_text = True
                    yield text_content
            elif item_type == "finish-step":
                needs_separator = True

        elif isinstance(item, dict):
            t = item.get("type")

            # Pass through known StreamChunk dict types
            if t in ("markdown_text", "task_update", "plan_update"):
                yield item
                continue

            if t == "text-delta":
                text_content = next(
                    (v for k in ("text", "delta", "textDelta", "text_delta") if (v := item.get(k)) is not None),
                    "",
                )
                if isinstance(text_content, str) and text_content:
                    if needs_separator and has_emitted_text:
                        yield "\n\n"
                    needs_separator = False
                    has_emitted_text = True
                    yield text_content
            elif t == "finish-step":
                needs_separator = True


# ---------------------------------------------------------------------------
# Internal dataclass used by ThreadImpl.channel property to avoid import loop
# ---------------------------------------------------------------------------


@dataclass
class _ChannelImplConfigForThread:
    """Passed to ChannelImpl from ThreadImpl.channel property."""

    id: str
    adapter: Adapter
    state_adapter: StateAdapter
    is_dm: bool = False
    channel_visibility: ChannelVisibility = "unknown"
    message_history: Any = None
