"""Chat orchestrator for chat-sdk.

Python port of Vercel Chat SDK chat.ts + chat-singleton.ts.
Main entry point: takes a ChatConfig, registers event handlers via decorator-style
methods, routes webhooks to adapters, manages concurrency, deduplication, and
thread/channel creation.
"""

from __future__ import annotations

import asyncio
import dataclasses
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from chat_sdk.channel import ChannelImpl
from chat_sdk.errors import ChatError, LockError
from chat_sdk.logger import ConsoleLogger, Logger
from chat_sdk.thread import (
    ThreadImpl,
    _ThreadImplConfig,
    get_chat_singleton,
    has_chat_singleton,
    set_chat_singleton,
)
from chat_sdk.types import (
    ActionEvent,
    Adapter,
    AppHomeOpenedEvent,
    AssistantContextChangedEvent,
    AssistantThreadStartedEvent,
    Author,
    ChannelVisibility,
    ChatConfig,
    ConcurrencyStrategy,
    EmojiValue,
    Lock,
    LockScope,
    LockScopeContext,
    MemberJoinedChannelEvent,
    Message,
    MessageContext,
    MessageMetadata,
    ModalCloseEvent,
    ModalResponse,
    ModalSubmitEvent,
    OnLockConflict,
    QueueEntry,
    ReactionEvent,
    SlashCommandEvent,
    StateAdapter,
    WebhookOptions,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_LOCK_TTL_MS = 30_000  # 30 seconds
DEDUPE_TTL_MS = 5 * 60 * 1000  # 5 minutes
MODAL_CONTEXT_TTL_MS = 24 * 60 * 60 * 1000  # 24 hours

SLACK_USER_ID_REGEX = re.compile(r"^U[A-Z0-9]+$", re.IGNORECASE)
DISCORD_SNOWFLAKE_REGEX = re.compile(r"^\d{17,19}$")

# ---------------------------------------------------------------------------
# Handler type aliases
# ---------------------------------------------------------------------------

MentionHandler = Callable[[Any, Message, Any], Awaitable[None] | None]
DirectMessageHandler = Callable[[Any, Message, Any, Any], Awaitable[None] | None]
MessageHandler = Callable[[Any, Message, Any], Awaitable[None] | None]
SubscribedMessageHandler = Callable[[Any, Message, Any], Awaitable[None] | None]
ReactionHandler = Callable[[ReactionEvent], Any]
ActionHandler = Callable[[ActionEvent], Any]
ModalSubmitHandler = Callable[[ModalSubmitEvent], Any]
ModalCloseHandler = Callable[[ModalCloseEvent], Any]
SlashCommandHandler = Callable[[SlashCommandEvent], Any]
AssistantThreadStartedHandler = Callable[[AssistantThreadStartedEvent], Any]
AssistantContextChangedHandler = Callable[[AssistantContextChangedEvent], Any]
AppHomeOpenedHandler = Callable[[AppHomeOpenedEvent], Any]
MemberJoinedChannelHandler = Callable[[MemberJoinedChannelEvent], Any]

EmojiFilter = EmojiValue | str

# ---------------------------------------------------------------------------
# Internal pattern types
# ---------------------------------------------------------------------------


class _MessagePattern:
    __slots__ = ("pattern", "handler")

    def __init__(self, pattern: re.Pattern[str], handler: MessageHandler) -> None:
        self.pattern = pattern
        self.handler = handler


class _ReactionPattern:
    __slots__ = ("emoji", "handler")

    def __init__(self, emoji: list[EmojiFilter], handler: ReactionHandler) -> None:
        self.emoji = emoji
        self.handler = handler


class _ActionPattern:
    __slots__ = ("action_ids", "handler")

    def __init__(self, action_ids: list[str], handler: ActionHandler) -> None:
        self.action_ids = action_ids
        self.handler = handler


class _ModalSubmitPattern:
    __slots__ = ("callback_ids", "handler")

    def __init__(self, callback_ids: list[str], handler: ModalSubmitHandler) -> None:
        self.callback_ids = callback_ids
        self.handler = handler


class _ModalClosePattern:
    __slots__ = ("callback_ids", "handler")

    def __init__(self, callback_ids: list[str], handler: ModalCloseHandler) -> None:
        self.callback_ids = callback_ids
        self.handler = handler


class _SlashCommandPattern:
    __slots__ = ("commands", "handler")

    def __init__(self, commands: list[str], handler: SlashCommandHandler) -> None:
        self.commands = commands
        self.handler = handler


# ---------------------------------------------------------------------------
# Stored modal context
# ---------------------------------------------------------------------------


class _StoredModalContext:
    __slots__ = ("thread", "message", "channel")

    def __init__(
        self,
        thread: dict[str, Any] | None = None,
        message: dict[str, Any] | None = None,
        channel: dict[str, Any] | None = None,
    ) -> None:
        self.thread = thread
        self.message = message
        self.channel = channel


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


async def _sleep(ms: int) -> None:
    """Promise-based sleep for debounce timing."""
    await asyncio.sleep(ms / 1000.0)


def _create_task(
    coro: Any,
    active_tasks: set[asyncio.Task[Any]] | None = None,
) -> asyncio.Task[Any] | None:
    """Create an asyncio task using the running loop.

    Returns ``None`` when no event loop is running (e.g. called from a
    synchronous context without an active loop).  Callers should guard
    against this.

    If *active_tasks* is provided the new task is added to the set and a
    done-callback is registered to remove it when the task finishes.
    """
    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(coro)
        if active_tasks is not None:
            active_tasks.add(task)
            task.add_done_callback(active_tasks.discard)
        return task
    except RuntimeError:
        # No running event loop -- cannot schedule the coroutine.
        # Close the coroutine to avoid "coroutine was never awaited" warning.
        coro.close()
        return None


# ---------------------------------------------------------------------------
# Chat class
# ---------------------------------------------------------------------------


class Chat:
    """Main Chat orchestrator.

    Takes a ``ChatConfig`` and provides decorator-style registration for
    event handlers (mentions, DMs, reactions, actions, modals, slash commands,
    assistant events, etc.).

    Routes incoming webhooks to adapters and manages concurrency, deduplication,
    and locking.

    Example::

        chat = Chat(ChatConfig(
            user_name="mybot",
            adapters={"slack": slack_adapter},
            state=memory_state,
        ))

        @chat.on_mention
        async def handle_mention(thread, message):
            await thread.subscribe()
            await thread.post("Hello!")

        # In your web framework
        @app.post("/slack/events")
        async def slack_events(request):
            return await chat.webhooks["slack"](request)
    """

    def __init__(self, config: ChatConfig | None = None, **kwargs: Any) -> None:
        if config is None:
            known_fields = {f.name for f in dataclasses.fields(ChatConfig)}
            unknown = set(kwargs) - known_fields
            if unknown:
                raise TypeError(f"Unknown Chat config fields: {unknown}")
            config = ChatConfig(**kwargs)
        self._user_name = config.user_name
        self._state_adapter = config.state
        self._adapters: dict[str, Adapter] = {}
        self._streaming_update_interval_ms = config.streaming_update_interval_ms
        self._fallback_streaming_placeholder_text = config.fallback_streaming_placeholder_text
        self._dedupe_ttl_ms = config.dedupe_ttl_ms or DEDUPE_TTL_MS
        self._lock_scope_config = config.lock_scope
        self._on_lock_conflict: OnLockConflict | None = config.on_lock_conflict

        # -- Concurrency config -----------------------------------------------
        concurrency = config.concurrency
        if concurrency is None:
            self._concurrency_strategy: ConcurrencyStrategy = "drop"
            self._concurrency_debounce_ms = 1500
            self._concurrency_max_concurrent: int | None = None
            self._concurrency_max_queue_size = 10
            self._concurrency_on_queue_full: str = "drop-oldest"
            self._concurrency_queue_entry_ttl_ms = 90_000
        elif isinstance(concurrency, str):
            self._concurrency_strategy = concurrency
            self._concurrency_debounce_ms = 1500
            self._concurrency_max_concurrent = None
            self._concurrency_max_queue_size = 10
            self._concurrency_on_queue_full = "drop-oldest"
            self._concurrency_queue_entry_ttl_ms = 90_000
        else:
            # ConcurrencyConfig dataclass
            self._concurrency_strategy = concurrency.strategy
            self._concurrency_debounce_ms = concurrency.debounce_ms
            self._concurrency_max_concurrent = concurrency.max_concurrent
            self._concurrency_max_queue_size = concurrency.max_queue_size
            self._concurrency_on_queue_full = concurrency.on_queue_full
            self._concurrency_queue_entry_ttl_ms = concurrency.queue_entry_ttl_ms

        # -- Message history (placeholder -- real impl would use MessageHistoryCache)
        self._message_history = _MessageHistoryCache(self._state_adapter, config.message_history)

        # -- Logger -----------------------------------------------------------
        if isinstance(config.logger, str):
            self._logger: Logger = ConsoleLogger(config.logger)
        elif config.logger is not None:
            self._logger = config.logger
        else:
            self._logger = ConsoleLogger("info")

        # -- Handler registries -----------------------------------------------
        self._mention_handlers: list[MentionHandler] = []
        self._direct_message_handlers: list[DirectMessageHandler] = []
        self._message_patterns: list[_MessagePattern] = []
        self._subscribed_message_handlers: list[SubscribedMessageHandler] = []
        self._reaction_handlers: list[_ReactionPattern] = []
        self._action_handlers: list[_ActionPattern] = []
        self._modal_submit_handlers: list[_ModalSubmitPattern] = []
        self._modal_close_handlers: list[_ModalClosePattern] = []
        self._slash_command_handlers: list[_SlashCommandPattern] = []
        self._assistant_thread_started_handlers: list[AssistantThreadStartedHandler] = []
        self._assistant_context_changed_handlers: list[AssistantContextChangedHandler] = []
        self._app_home_opened_handlers: list[AppHomeOpenedHandler] = []
        self._member_joined_channel_handlers: list[MemberJoinedChannelHandler] = []

        # -- Init state -------------------------------------------------------
        self._init_promise: asyncio.Task[None] | None = None
        self._initialized = False
        self._init_lock = asyncio.Lock()

        # -- Active handler tasks (for cancellation on shutdown) --------------
        self._active_tasks: set[asyncio.Task[Any]] = set()

        # -- Cached mention regex patterns (populated lazily) ----------------
        self._mention_patterns: dict[str, re.Pattern[str]] = {}

        # -- Register adapters and build webhooks map --------------------------
        self.webhooks: dict[str, Callable[..., Awaitable[Any]]] = {}
        for name, adapter in config.adapters.items():
            self._adapters[name] = adapter
            # Capture name in closure
            self.webhooks[name] = self._make_webhook_handler(name)

        self._logger.debug("Chat instance created", {"adapters": list(config.adapters.keys())})

    # ========================================================================
    # Singleton management
    # ========================================================================

    def register_singleton(self) -> Chat:
        """Register this Chat instance as the global singleton.

        Required for Thread/Channel deserialization without explicit adapter refs.
        """
        set_chat_singleton(self)  # type: ignore[arg-type]
        return self

    @staticmethod
    def get_singleton() -> Chat:
        """Get the registered singleton Chat instance."""
        return get_chat_singleton()  # type: ignore[return-value]

    @staticmethod
    def has_singleton() -> bool:
        return has_chat_singleton()

    # ========================================================================
    # ChatInstance protocol implementation
    # ========================================================================

    def get_adapter(self, name: str) -> Adapter | None:
        return self._adapters.get(name)

    def get_state(self) -> StateAdapter:
        return self._state_adapter

    def get_user_name(self) -> str:
        return self._user_name

    def get_logger(self, prefix: str | None = None) -> Logger:
        if prefix:
            return self._logger.child(prefix)
        return self._logger

    # ========================================================================
    # Webhook routing
    # ========================================================================

    def _make_webhook_handler(self, adapter_name: str) -> Callable[..., Awaitable[Any]]:
        async def handler(request: Any, options: WebhookOptions | None = None) -> Any:
            return await self._handle_webhook(adapter_name, request, options)

        return handler

    async def _handle_webhook(
        self,
        adapter_name: str,
        request: Any,
        options: WebhookOptions | None = None,
    ) -> Any:
        """Handle a webhook request for a specific adapter."""
        await self._ensure_initialized()

        adapter = self._adapters.get(adapter_name)
        if adapter is None:
            raise ChatError(f"Unknown adapter: {adapter_name}")

        return await adapter.handle_webhook(request, options)

    # ========================================================================
    # Initialization
    # ========================================================================

    async def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            if self._init_promise is None:
                self._init_promise = asyncio.get_running_loop().create_task(self._do_initialize())
            try:
                await self._init_promise
            except Exception:
                # Reset so a subsequent call can retry initialization.
                self._init_promise = None
                raise

    async def _do_initialize(self) -> None:
        self._logger.info("Initializing chat instance...")
        await self._state_adapter.connect()
        self._logger.debug("State connected")

        init_tasks = []
        for adapter in self._adapters.values():
            self._logger.debug("Initializing adapter", adapter.name)
            init_tasks.append(adapter.initialize(self))  # type: ignore[arg-type]

        await asyncio.gather(*init_tasks)
        self._initialized = True
        self._logger.info(
            "Chat instance initialized",
            {"adapters": list(self._adapters.keys())},
        )

    async def initialize(self) -> None:
        """Manually trigger initialization (automatic on first webhook)."""
        await self._ensure_initialized()

    async def shutdown(self) -> None:
        """Gracefully shut down all adapters and state."""
        self._logger.info("Shutting down chat instance...")

        # Cancel in-flight handler tasks before tearing down adapters/state
        for task in list(self._active_tasks):
            task.cancel()
        # Give tasks time to handle cancellation
        if self._active_tasks:
            await asyncio.gather(*self._active_tasks, return_exceptions=True)

        tasks = []
        for adapter in self._adapters.values():
            if hasattr(adapter, "disconnect") and adapter.disconnect:  # type: ignore[union-attr]
                tasks.append(adapter.disconnect())  # type: ignore[union-attr]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                self._logger.error("Adapter disconnect failed", str(r))
        await self._state_adapter.disconnect()
        self._initialized = False
        self._init_promise = None
        self._logger.info("Chat instance shut down")

    # ========================================================================
    # Handler registration (decorator-style)
    # ========================================================================

    def on_mention(self, handler: MentionHandler) -> MentionHandler:
        """Register a handler for new @-mentions of the bot in unsubscribed threads.

        Can be used as a decorator::

            @chat.on_mention
            async def handle(thread, message):
                await thread.post("Hi!")
        """
        self._mention_handlers.append(handler)
        self._logger.debug("Registered mention handler")
        return handler

    def on_direct_message(self, handler: DirectMessageHandler) -> DirectMessageHandler:
        """Register a handler for direct messages."""
        self._direct_message_handlers.append(handler)
        self._logger.debug("Registered direct message handler")
        return handler

    def on_message(
        self,
        pattern: re.Pattern[str] | str,
    ) -> Callable[[MessageHandler], MessageHandler]:
        """Register a handler for messages matching a regex pattern.

        Usage::

            @chat.on_message(r"^!help")
            async def handle(thread, message):
                await thread.post("Help!")
        """
        compiled = re.compile(pattern) if isinstance(pattern, str) else pattern

        def decorator(handler: MessageHandler) -> MessageHandler:
            self._message_patterns.append(_MessagePattern(compiled, handler))
            self._logger.debug("Registered message pattern handler", {"pattern": str(compiled.pattern)})
            return handler

        return decorator

    def on_subscribed_message(self, handler: SubscribedMessageHandler) -> SubscribedMessageHandler:
        """Register a handler for messages in subscribed threads."""
        self._subscribed_message_handlers.append(handler)
        self._logger.debug("Registered subscribed message handler")
        return handler

    # -- Reactions ---

    def on_reaction(
        self,
        emoji_or_handler: list[EmojiFilter] | ReactionHandler | None = None,
        handler: ReactionHandler | None = None,
    ) -> ReactionHandler | Callable[[ReactionHandler], ReactionHandler]:
        """Register a handler for reaction events.

        Overloaded:
        - ``chat.on_reaction(handler)`` -- all reactions
        - ``chat.on_reaction([emoji_list], handler)``
        - ``@chat.on_reaction()`` or ``@chat.on_reaction([emoji_list])`` as decorator
        """
        if callable(emoji_or_handler) and handler is None:
            # on_reaction(handler) -- no filter
            self._reaction_handlers.append(_ReactionPattern([], emoji_or_handler))
            self._logger.debug("Registered reaction handler for all emoji")
            return emoji_or_handler

        if isinstance(emoji_or_handler, list) and handler is not None:
            # on_reaction([emoji], handler)
            self._reaction_handlers.append(_ReactionPattern(emoji_or_handler, handler))
            self._logger.debug("Registered reaction handler", {"emoji": [str(e) for e in emoji_or_handler]})
            return handler

        # Decorator form: @chat.on_reaction() or @chat.on_reaction([emoji])
        emoji_list = emoji_or_handler if isinstance(emoji_or_handler, list) else []

        def decorator(h: ReactionHandler) -> ReactionHandler:
            self._reaction_handlers.append(_ReactionPattern(emoji_list, h))
            return h

        return decorator

    # -- Actions ---

    def on_action(
        self,
        action_ids_or_handler: str | list[str] | ActionHandler | None = None,
        handler: ActionHandler | None = None,
    ) -> ActionHandler | Callable[[ActionHandler], ActionHandler]:
        """Register a handler for action events (button clicks in cards).

        Overloaded:
        - ``chat.on_action(handler)`` -- all actions
        - ``chat.on_action("id", handler)``
        - ``chat.on_action(["id1", "id2"], handler)``
        - Decorator: ``@chat.on_action("id")``
        """
        if callable(action_ids_or_handler) and handler is None:
            self._action_handlers.append(_ActionPattern([], action_ids_or_handler))
            self._logger.debug("Registered action handler for all actions")
            return action_ids_or_handler

        if isinstance(action_ids_or_handler, (str, list)) and handler is not None:
            ids = [action_ids_or_handler] if isinstance(action_ids_or_handler, str) else action_ids_or_handler
            self._action_handlers.append(_ActionPattern(ids, handler))
            self._logger.debug("Registered action handler", {"action_ids": ids})
            return handler

        # Decorator form
        ids = (
            [action_ids_or_handler]
            if isinstance(action_ids_or_handler, str)
            else (action_ids_or_handler if isinstance(action_ids_or_handler, list) else [])
        )

        def decorator(h: ActionHandler) -> ActionHandler:
            self._action_handlers.append(_ActionPattern(ids, h))
            return h

        return decorator

    # -- Modal submit ---

    def on_modal_submit(
        self,
        callback_ids_or_handler: str | list[str] | ModalSubmitHandler | None = None,
        handler: ModalSubmitHandler | None = None,
    ) -> ModalSubmitHandler | Callable[[ModalSubmitHandler], ModalSubmitHandler]:
        """Register a handler for modal form submissions."""
        if callable(callback_ids_or_handler) and handler is None:
            self._modal_submit_handlers.append(_ModalSubmitPattern([], callback_ids_or_handler))
            self._logger.debug("Registered modal submit handler for all modals")
            return callback_ids_or_handler

        if isinstance(callback_ids_or_handler, (str, list)) and handler is not None:
            ids = [callback_ids_or_handler] if isinstance(callback_ids_or_handler, str) else callback_ids_or_handler
            self._modal_submit_handlers.append(_ModalSubmitPattern(ids, handler))
            self._logger.debug("Registered modal submit handler", {"callback_ids": ids})
            return handler

        ids = (
            [callback_ids_or_handler]
            if isinstance(callback_ids_or_handler, str)
            else (callback_ids_or_handler if isinstance(callback_ids_or_handler, list) else [])
        )

        def decorator(h: ModalSubmitHandler) -> ModalSubmitHandler:
            self._modal_submit_handlers.append(_ModalSubmitPattern(ids, h))
            return h

        return decorator

    # -- Modal close ---

    def on_modal_close(
        self,
        callback_ids_or_handler: str | list[str] | ModalCloseHandler | None = None,
        handler: ModalCloseHandler | None = None,
    ) -> ModalCloseHandler | Callable[[ModalCloseHandler], ModalCloseHandler]:
        """Register a handler for modal close events."""
        if callable(callback_ids_or_handler) and handler is None:
            self._modal_close_handlers.append(_ModalClosePattern([], callback_ids_or_handler))
            self._logger.debug("Registered modal close handler for all modals")
            return callback_ids_or_handler

        if isinstance(callback_ids_or_handler, (str, list)) and handler is not None:
            ids = [callback_ids_or_handler] if isinstance(callback_ids_or_handler, str) else callback_ids_or_handler
            self._modal_close_handlers.append(_ModalClosePattern(ids, handler))
            self._logger.debug("Registered modal close handler", {"callback_ids": ids})
            return handler

        ids = (
            [callback_ids_or_handler]
            if isinstance(callback_ids_or_handler, str)
            else (callback_ids_or_handler if isinstance(callback_ids_or_handler, list) else [])
        )

        def decorator(h: ModalCloseHandler) -> ModalCloseHandler:
            self._modal_close_handlers.append(_ModalClosePattern(ids, h))
            return h

        return decorator

    # -- Slash commands ---

    def on_slash_command(
        self,
        commands_or_handler: str | list[str] | SlashCommandHandler | None = None,
        handler: SlashCommandHandler | None = None,
    ) -> SlashCommandHandler | Callable[[SlashCommandHandler], SlashCommandHandler]:
        """Register a handler for slash command events.

        Usage::

            @chat.on_slash_command("/help")
            async def handle(event):
                await event.channel.post("Help!")

            @chat.on_slash_command(["/status", "/health"])
            async def handle(event):
                await event.channel.post("OK")

            # Catch-all
            @chat.on_slash_command
            async def handle(event):
                pass
        """
        if callable(commands_or_handler) and handler is None:
            self._slash_command_handlers.append(_SlashCommandPattern([], commands_or_handler))
            self._logger.debug("Registered slash command handler for all commands")
            return commands_or_handler

        if isinstance(commands_or_handler, (str, list)) and handler is not None:
            cmds = [commands_or_handler] if isinstance(commands_or_handler, str) else commands_or_handler
            normalized = [c if c.startswith("/") else f"/{c}" for c in cmds]
            self._slash_command_handlers.append(_SlashCommandPattern(normalized, handler))
            self._logger.debug("Registered slash command handler", {"commands": normalized})
            return handler

        # Decorator form
        cmds_raw = (
            [commands_or_handler]
            if isinstance(commands_or_handler, str)
            else (commands_or_handler if isinstance(commands_or_handler, list) else [])
        )
        normalized = [c if c.startswith("/") else f"/{c}" for c in cmds_raw] if cmds_raw else []

        def decorator(h: SlashCommandHandler) -> SlashCommandHandler:
            self._slash_command_handlers.append(_SlashCommandPattern(normalized, h))
            return h

        return decorator

    # -- Assistant events ---

    def on_assistant_thread_started(self, handler: AssistantThreadStartedHandler) -> AssistantThreadStartedHandler:
        self._assistant_thread_started_handlers.append(handler)
        self._logger.debug("Registered assistant thread started handler")
        return handler

    def on_assistant_context_changed(self, handler: AssistantContextChangedHandler) -> AssistantContextChangedHandler:
        self._assistant_context_changed_handlers.append(handler)
        self._logger.debug("Registered assistant context changed handler")
        return handler

    def on_app_home_opened(self, handler: AppHomeOpenedHandler) -> AppHomeOpenedHandler:
        self._app_home_opened_handlers.append(handler)
        self._logger.debug("Registered app home opened handler")
        return handler

    def on_member_joined_channel(self, handler: MemberJoinedChannelHandler) -> MemberJoinedChannelHandler:
        self._member_joined_channel_handlers.append(handler)
        self._logger.debug("Registered member joined channel handler")
        return handler

    # ========================================================================
    # Adapter lookup
    # ========================================================================

    def get_adapter_by_name(self, name: str) -> Adapter | None:
        """Get an adapter by name."""
        return self._adapters.get(name)

    # ========================================================================
    # JSON reviver
    # ========================================================================

    def reviver(self) -> Callable[[str, Any], Any]:
        """Return a JSON reviver that deserializes Thread/Channel/Message objects.

        Ensures this Chat instance is the registered singleton.
        """
        self.register_singleton()

        def _reviver(key: str, value: Any) -> Any:
            if isinstance(value, dict) and "_type" in value:
                t = value["_type"]
                if t == "chat:Thread":
                    return ThreadImpl.from_json(value)
                if t == "chat:Channel":
                    return ChannelImpl.from_json(value)
                if t == "chat:Message":
                    return _message_from_json(value)
            return value

        return _reviver

    # ========================================================================
    # Process* methods (called by adapters)
    # ========================================================================

    def process_message(
        self,
        adapter: Adapter,
        thread_id: str,
        message_or_factory: Message | Callable[[], Awaitable[Message]],
        options: WebhookOptions | None = None,
    ) -> None:
        """Process an incoming message from an adapter.

        Handles waitUntil registration and error catching.
        """

        async def _task() -> None:
            msg = await message_or_factory() if callable(message_or_factory) else message_or_factory
            await self.handle_incoming_message(adapter, thread_id, msg)

        task = _create_task(_task(), self._active_tasks)
        if task is not None:
            task.add_done_callback(
                lambda t: (
                    self._logger.error(
                        "Message processing error", {"thread_id": thread_id, "error": str(t.exception())}
                    )
                    if not t.cancelled() and t.exception()
                    else None
                )
            )
            if options and options.wait_until:
                options.wait_until(task)

    def process_reaction(
        self,
        event: ReactionEvent,
        options: WebhookOptions | None = None,
    ) -> None:
        """Process an incoming reaction event."""
        task = _create_task(self._handle_reaction_event(event), self._active_tasks)
        if task is not None:
            task.add_done_callback(
                lambda t: (
                    self._logger.error("Reaction processing error", {"error": str(t.exception())})
                    if not t.cancelled() and t.exception()
                    else None
                )
            )
            if options and options.wait_until:
                options.wait_until(task)

    def process_action(
        self,
        event: ActionEvent,
        options: WebhookOptions | None = None,
    ) -> None:
        """Process an incoming action event (button click)."""
        task = _create_task(self._handle_action_event(event), self._active_tasks)
        if task is not None:
            task.add_done_callback(
                lambda t: (
                    self._logger.error("Action processing error", {"error": str(t.exception())})
                    if not t.cancelled() and t.exception()
                    else None
                )
            )
            if options and options.wait_until:
                options.wait_until(task)

    async def process_modal_submit(
        self,
        event: ModalSubmitEvent,
        context_id: str | None = None,
        options: WebhookOptions | None = None,
    ) -> ModalResponse | None:
        """Process a modal form submission. Returns optional response."""
        related = await self._retrieve_modal_context(event.adapter.name, context_id)

        full_event = ModalSubmitEvent(
            adapter=event.adapter,
            user=event.user,
            view_id=event.view_id,
            callback_id=event.callback_id,
            values=event.values,
            private_metadata=event.private_metadata,
            related_thread=related.get("related_thread"),
            related_message=related.get("related_message"),
            related_channel=related.get("related_channel"),
            raw=event.raw,
        )

        for pat in self._modal_submit_handlers:
            if not pat.callback_ids or event.callback_id in pat.callback_ids:
                try:
                    response = await pat.handler(full_event)
                    if response is not None:
                        return response
                except Exception as exc:
                    self._logger.error(
                        "Modal submit handler error",
                        {"callback_id": event.callback_id, "error": str(exc)},
                    )
        return None

    def process_modal_close(
        self,
        event: ModalCloseEvent,
        context_id: str | None = None,
        options: WebhookOptions | None = None,
    ) -> None:
        """Process a modal close event."""

        async def _task() -> None:
            related = await self._retrieve_modal_context(event.adapter.name, context_id)

            full_event = ModalCloseEvent(
                adapter=event.adapter,
                user=event.user,
                view_id=event.view_id,
                callback_id=event.callback_id,
                private_metadata=event.private_metadata,
                related_thread=related.get("related_thread"),
                related_message=related.get("related_message"),
                related_channel=related.get("related_channel"),
                raw=event.raw,
            )

            for pat in self._modal_close_handlers:
                if not pat.callback_ids or event.callback_id in pat.callback_ids:
                    await pat.handler(full_event)

        task = _create_task(_task(), self._active_tasks)
        if task is not None:
            task.add_done_callback(
                lambda t: (
                    self._logger.error("Modal close handler error", {"error": str(t.exception())})
                    if not t.cancelled() and t.exception()
                    else None
                )
            )
            if options and options.wait_until:
                options.wait_until(task)

    def process_slash_command(
        self,
        event: SlashCommandEvent,
        options: WebhookOptions | None = None,
    ) -> None:
        """Process a slash command event."""
        task = _create_task(self._handle_slash_command_event(event), self._active_tasks)
        if task is not None:
            task.add_done_callback(
                lambda t: (
                    self._logger.error("Slash command processing error", {"error": str(t.exception())})
                    if not t.cancelled() and t.exception()
                    else None
                )
            )
            if options and options.wait_until:
                options.wait_until(task)

    def process_assistant_thread_started(
        self,
        event: AssistantThreadStartedEvent,
        options: WebhookOptions | None = None,
    ) -> None:
        async def _task() -> None:
            for h in self._assistant_thread_started_handlers:
                await h(event)

        task = _create_task(_task(), self._active_tasks)
        if task is not None:
            task.add_done_callback(
                lambda t: (
                    self._logger.error("Assistant thread started handler error", {"error": str(t.exception())})
                    if not t.cancelled() and t.exception()
                    else None
                )
            )
            if options and options.wait_until:
                options.wait_until(task)

    def process_assistant_context_changed(
        self,
        event: AssistantContextChangedEvent,
        options: WebhookOptions | None = None,
    ) -> None:
        async def _task() -> None:
            for h in self._assistant_context_changed_handlers:
                await h(event)

        task = _create_task(_task(), self._active_tasks)
        if task is not None:
            task.add_done_callback(
                lambda t: (
                    self._logger.error("Assistant context changed handler error", {"error": str(t.exception())})
                    if not t.cancelled() and t.exception()
                    else None
                )
            )
            if options and options.wait_until:
                options.wait_until(task)

    def process_app_home_opened(
        self,
        event: AppHomeOpenedEvent,
        options: WebhookOptions | None = None,
    ) -> None:
        async def _task() -> None:
            for h in self._app_home_opened_handlers:
                await h(event)

        task = _create_task(_task(), self._active_tasks)
        if task is not None:
            task.add_done_callback(
                lambda t: (
                    self._logger.error("App home opened handler error", {"error": str(t.exception())})
                    if not t.cancelled() and t.exception()
                    else None
                )
            )
            if options and options.wait_until:
                options.wait_until(task)

    def process_member_joined_channel(
        self,
        event: MemberJoinedChannelEvent,
        options: WebhookOptions | None = None,
    ) -> None:
        async def _task() -> None:
            for h in self._member_joined_channel_handlers:
                await h(event)

        task = _create_task(_task(), self._active_tasks)
        if task is not None:
            task.add_done_callback(
                lambda t: (
                    self._logger.error("Member joined channel handler error", {"error": str(t.exception())})
                    if not t.cancelled() and t.exception()
                    else None
                )
            )
            if options and options.wait_until:
                options.wait_until(task)

    # ========================================================================
    # Slash command handling
    # ========================================================================

    async def _handle_slash_command_event(self, event: SlashCommandEvent) -> None:
        self._logger.debug(
            "Incoming slash command",
            {
                "adapter": event.adapter.name,
                "command": event.command,
                "text": event.text,
                "user": event.user.user_name,
            },
        )

        if event.user.is_me:
            self._logger.debug("Skipping slash command from self")
            return

        # Create channel for the command
        channel_id = getattr(event, "channel_id", None) or (event.channel.id if event.channel else "")
        channel = ChannelImpl(
            _ChannelImplConfigForChat(
                id=channel_id,
                adapter=event.adapter,
                state_adapter=self._state_adapter,
            )
        )

        # Build openModal helper
        async def _open_modal(modal: Any) -> dict[str, str] | None:
            trigger_id = event.trigger_id
            if not trigger_id:
                self._logger.warn("Cannot open modal: no trigger_id available")
                return None
            if not hasattr(event.adapter, "open_modal") or not event.adapter.open_modal:  # type: ignore[union-attr]
                self._logger.warn(f"Cannot open modal: {event.adapter.name} does not support modals")
                return None
            context_id = str(uuid.uuid4())
            self._store_modal_context(event.adapter.name, context_id, channel=channel)
            return await event.adapter.open_modal(trigger_id, modal, context_id)  # type: ignore[union-attr]

        full_event = SlashCommandEvent(
            adapter=event.adapter,
            channel=channel,
            user=event.user,
            command=event.command,
            text=event.text,
            trigger_id=event.trigger_id,
            raw=event.raw,
            _open_modal=_open_modal,
        )

        for pat in self._slash_command_handlers:
            if not pat.commands:
                self._logger.debug("Running catch-all slash command handler")
                await pat.handler(full_event)
                continue
            if event.command in pat.commands:
                self._logger.debug("Running matched slash command handler", {"command": event.command})
                await pat.handler(full_event)

    # ========================================================================
    # Modal context persistence
    # ========================================================================

    def _store_modal_context(
        self,
        adapter_name: str,
        context_id: str,
        thread: ThreadImpl | None = None,
        message: Message | None = None,
        channel: ChannelImpl | None = None,
    ) -> None:
        key = f"modal-context:{adapter_name}:{context_id}"
        context = {
            "thread": thread.to_json() if thread else None,
            "message": message.to_json() if message else None,
            "channel": channel.to_json() if channel else None,
        }
        task = _create_task(self._state_adapter.set(key, context, MODAL_CONTEXT_TTL_MS), self._active_tasks)
        if task is not None:
            task.add_done_callback(
                lambda t: (
                    self._logger.error("Failed to store modal context", {"context_id": context_id})
                    if not t.cancelled() and t.exception()
                    else None
                )
            )

    async def _retrieve_modal_context(
        self,
        adapter_name: str,
        context_id: str | None,
    ) -> dict[str, Any]:
        if not context_id:
            return {
                "related_thread": None,
                "related_message": None,
                "related_channel": None,
            }

        key = f"modal-context:{adapter_name}:{context_id}"
        stored = await self._state_adapter.get(key)

        if not stored:
            return {
                "related_thread": None,
                "related_message": None,
                "related_channel": None,
            }

        adapter = self._adapters.get(adapter_name)

        related_thread = None
        if stored.get("thread"):
            related_thread = ThreadImpl.from_json(stored["thread"], adapter)

        related_message = None
        if stored.get("message") and related_thread is not None:
            msg = _message_from_json(stored["message"])
            related_message = related_thread.create_sent_message_from_message(msg)

        related_channel = None
        if stored.get("channel"):
            related_channel = ChannelImpl.from_json(stored["channel"], adapter)

        return {
            "related_thread": related_thread,
            "related_message": related_message,
            "related_channel": related_channel,
        }

    # ========================================================================
    # Action handling
    # ========================================================================

    async def _handle_action_event(self, event: ActionEvent) -> None:
        self._logger.debug(
            "Incoming action",
            {
                "adapter": event.adapter.name,
                "action_id": event.action_id,
                "value": event.value,
                "user": event.user.user_name,
            },
        )

        if event.user.is_me:
            self._logger.debug("Skipping action from self")
            return

        thread: ThreadImpl | None = None
        if event.thread_id:
            is_subscribed = False
            dummy_message = Message(
                id=event.message_id or "",
                thread_id=event.thread_id,
                text="",
                formatted={"type": "root", "children": []},
                raw=event.raw,
                author=event.user,
                metadata=MessageMetadata(date_sent=datetime.now(tz=UTC), edited=False),
                attachments=[],
            )
            thread = self._create_thread(event.adapter, event.thread_id, dummy_message, is_subscribed)

        # Build openModal helper
        async def _open_modal(modal: Any) -> dict[str, str] | None:
            trigger_id = event.trigger_id
            if not trigger_id:
                self._logger.warn("Cannot open modal: no trigger_id available")
                return None
            if not hasattr(event.adapter, "open_modal") or not event.adapter.open_modal:  # type: ignore[union-attr]
                self._logger.warn(f"Cannot open modal: {event.adapter.name} does not support modals")
                return None

            # Try to fetch the message for modal context
            fetched_message: Message | None = None
            if thread and event.message_id:
                if hasattr(event.adapter, "fetch_message") and event.adapter.fetch_message:  # type: ignore[union-attr]
                    try:
                        raw_fetched = await event.adapter.fetch_message(event.thread_id, event.message_id)  # type: ignore[union-attr]
                        if raw_fetched:
                            fetched_message = Message(
                                id=raw_fetched.id if hasattr(raw_fetched, "id") else event.message_id,
                                thread_id=event.thread_id,
                                text=getattr(raw_fetched, "text", ""),
                                formatted=getattr(raw_fetched, "formatted", {"type": "root", "children": []}),
                                raw=getattr(raw_fetched, "raw", None),
                                author=getattr(raw_fetched, "author", event.user),
                                metadata=getattr(
                                    raw_fetched,
                                    "metadata",
                                    MessageMetadata(date_sent=datetime.now(tz=UTC), edited=False),
                                ),
                            )
                    except Exception:
                        pass
                if fetched_message is None and thread and thread.recent_messages:
                    first = thread.recent_messages[0]
                    if hasattr(first, "to_json"):
                        fetched_message = first

            context_id = str(uuid.uuid4())
            channel_impl = thread.channel if thread else None
            self._store_modal_context(
                event.adapter.name,
                context_id,
                thread=thread,
                message=fetched_message,
                channel=channel_impl,
            )
            return await event.adapter.open_modal(trigger_id, modal, context_id)  # type: ignore[union-attr]

        full_event = ActionEvent(
            adapter=event.adapter,
            thread=thread,
            thread_id=event.thread_id,
            message_id=event.message_id,
            user=event.user,
            action_id=event.action_id,
            value=event.value,
            trigger_id=event.trigger_id,
            raw=event.raw,
            _open_modal=_open_modal,
        )

        for pat in self._action_handlers:
            if not pat.action_ids:
                self._logger.debug("Running catch-all action handler")
                await pat.handler(full_event)
                continue
            if event.action_id in pat.action_ids:
                self._logger.debug("Running matched action handler", {"action_id": event.action_id})
                await pat.handler(full_event)

    # ========================================================================
    # Reaction handling
    # ========================================================================

    async def _handle_reaction_event(self, event: ReactionEvent) -> None:
        self._logger.debug(
            "Incoming reaction",
            {
                "emoji": str(event.emoji),
                "raw_emoji": event.raw_emoji,
                "added": event.added,
                "user": event.user.user_name,
            },
        )

        if event.user.is_me:
            self._logger.debug("Skipping reaction from self")
            return

        if event.adapter is None:
            self._logger.error("Reaction event missing adapter")
            return

        is_subscribed = await self._state_adapter.is_subscribed(event.thread_id)
        thread = self._create_thread(
            event.adapter,
            event.thread_id,
            event.message
            or Message(
                id=event.message_id,
                thread_id=event.thread_id,
                text="",
                formatted={"type": "root", "children": []},
                raw=None,
                author=event.user,
                metadata=MessageMetadata(date_sent=datetime.now(tz=UTC), edited=False),
            ),
            is_subscribed,
        )

        full_event = ReactionEvent(
            adapter=event.adapter,
            thread=thread,
            thread_id=event.thread_id,
            message_id=event.message_id,
            user=event.user,
            emoji=event.emoji,
            raw_emoji=event.raw_emoji,
            added=event.added,
            message=event.message,
            raw=event.raw,
        )

        for pat in self._reaction_handlers:
            if not pat.emoji:
                self._logger.debug("Running catch-all reaction handler")
                await pat.handler(full_event)
                continue

            matches = any(
                (
                    filt is full_event.emoji
                    or (isinstance(filt, str) and (filt == full_event.emoji.name or filt == full_event.raw_emoji))
                    or (
                        isinstance(filt, EmojiValue)
                        and (filt.name == full_event.emoji.name or filt.name == full_event.raw_emoji)
                    )
                )
                for filt in pat.emoji
            )
            if matches:
                self._logger.debug("Running matched reaction handler")
                await pat.handler(full_event)

    # ========================================================================
    # openDM / channel
    # ========================================================================

    async def open_dm(self, user: str | Author) -> ThreadImpl:
        """Open a DM conversation with a user. Adapter inferred from user ID format."""
        user_id = user if isinstance(user, str) else user.user_id
        adapter = self._infer_adapter_from_user_id(user_id)
        if not hasattr(adapter, "open_dm") or not adapter.open_dm:  # type: ignore[union-attr]
            raise ChatError(f'Adapter "{adapter.name}" does not support open_dm')

        thread_id: str = await adapter.open_dm(user_id)  # type: ignore[union-attr]
        return self._create_thread(
            adapter,
            thread_id,
            Message(
                id="",
                thread_id=thread_id,
                text="",
                formatted={"type": "root", "children": []},
                raw=None,
                author=Author(user_id="", user_name="", full_name="", is_bot=False, is_me=False),
                metadata=MessageMetadata(date_sent=datetime.now(tz=UTC), edited=False),
            ),
            False,
        )

    def channel(self, channel_id: str) -> ChannelImpl:
        """Get a Channel by its channel ID (e.g. 'slack:C123ABC')."""
        adapter_name = channel_id.split(":")[0] if ":" in channel_id else ""
        if not adapter_name:
            raise ChatError(f"Invalid channel ID: {channel_id}")
        adapter = self._adapters.get(adapter_name)
        if adapter is None:
            raise ChatError(f'Adapter "{adapter_name}" not found for channel ID "{channel_id}"')
        return ChannelImpl(
            _ChannelImplConfigForChat(
                id=channel_id,
                adapter=adapter,
                state_adapter=self._state_adapter,
            )
        )

    # ========================================================================
    # Adapter inference
    # ========================================================================

    def _infer_adapter_from_user_id(self, user_id: str) -> Adapter:
        # Google Chat: users/...
        if user_id.startswith("users/"):
            adapter = self._adapters.get("gchat")
            if adapter:
                return adapter

        # Teams: 29:...
        if user_id.startswith("29:"):
            adapter = self._adapters.get("teams")
            if adapter:
                return adapter

        # Slack: U followed by alphanumeric
        if SLACK_USER_ID_REGEX.match(user_id):
            adapter = self._adapters.get("slack")
            if adapter:
                return adapter

        # Discord: snowflake
        if DISCORD_SNOWFLAKE_REGEX.match(user_id):
            adapter = self._adapters.get("discord")
            if adapter:
                return adapter

        raise ChatError(
            f'Cannot infer adapter from userId "{user_id}". '
            "Expected: Slack (U...), Teams (29:...), Google Chat (users/...), Discord (numeric)."
        )

    # ========================================================================
    # Lock key resolution
    # ========================================================================

    async def _get_lock_key(self, adapter: Adapter, thread_id: str) -> str:
        channel_id = adapter.channel_id_from_thread_id(thread_id)

        scope: LockScope
        if callable(self._lock_scope_config):
            is_dm = (
                adapter.is_dm(thread_id)
                if hasattr(adapter, "is_dm") and callable(getattr(adapter, "is_dm", None))
                else False
            )  # type: ignore[union-attr]
            scope = await self._lock_scope_config(
                LockScopeContext(
                    adapter=adapter,
                    channel_id=channel_id,
                    is_dm=is_dm,
                    thread_id=thread_id,
                )
            )
        else:
            scope = self._lock_scope_config or adapter.lock_scope or "thread"  # type: ignore[assignment]

        return channel_id if scope == "channel" else thread_id

    # ========================================================================
    # Incoming message handling (core)
    # ========================================================================

    async def handle_incoming_message(
        self,
        adapter: Adapter,
        thread_id: str,
        message: Message,
    ) -> None:
        """Handle an incoming message. Called by adapters or process_message.

        Handles deduplication, bot filtering, concurrency, and dispatch.
        """
        self._logger.debug(
            "Incoming message",
            {
                "adapter": adapter.name,
                "thread_id": thread_id,
                "message_id": message.id,
                "author": message.author.user_name,
                "is_me": message.author.is_me,
            },
        )

        # Skip self messages
        if message.author.is_me:
            self._logger.debug("Skipping message from self (is_me=True)")
            return

        # Deduplicate
        dedupe_key = f"dedupe:{adapter.name}:{message.id}"
        is_first = await self._state_adapter.set_if_not_exists(dedupe_key, True, self._dedupe_ttl_ms)
        if not is_first:
            self._logger.debug("Skipping duplicate message", {"message_id": message.id})
            return

        # Persist incoming message before acquiring lock
        if adapter.persist_message_history:
            channel_id = adapter.channel_id_from_thread_id(thread_id)
            appends = [self._message_history.append(thread_id, message)]
            if channel_id != thread_id:
                appends.append(self._message_history.append(channel_id, message))
            await asyncio.gather(*appends)

        # Resolve lock key
        lock_key = await self._get_lock_key(adapter, thread_id)

        strategy = self._concurrency_strategy

        if strategy == "concurrent":
            await self._handle_concurrent(adapter, thread_id, message)
            return

        if strategy in ("queue", "debounce"):
            await self._handle_queue_or_debounce(adapter, thread_id, lock_key, message, strategy)
            return

        # Default: drop
        await self._handle_drop(adapter, thread_id, lock_key, message)

    # -- Drop strategy -------------------------------------------------------

    async def _handle_drop(
        self,
        adapter: Adapter,
        thread_id: str,
        lock_key: str,
        message: Message,
    ) -> None:
        lock = await self._state_adapter.acquire_lock(lock_key, DEFAULT_LOCK_TTL_MS)
        if lock is None:
            # Lock acquisition failed -- consult on_lock_conflict policy
            lock = await self._resolve_lock_conflict(thread_id, lock_key, message)
            if lock is None:
                self._logger.warn("Could not acquire lock on thread", {"thread_id": thread_id, "lock_key": lock_key})
                raise LockError(
                    thread_id,
                    f"Could not acquire lock on thread {thread_id}. Another instance may be processing.",
                )

        self._logger.debug("Lock acquired", {"thread_id": thread_id, "lock_key": lock_key, "token": lock.token})
        try:
            await self._dispatch_to_handlers(adapter, thread_id, message)
        finally:
            await self._state_adapter.release_lock(lock)
            self._logger.debug("Lock released", {"thread_id": thread_id, "lock_key": lock_key})

    async def _resolve_lock_conflict(
        self,
        thread_id: str,
        lock_key: str,
        message: Message,
    ) -> Lock | None:
        """Attempt to resolve a lock conflict based on the ``on_lock_conflict`` policy.

        Returns a :class:`Lock` if the conflict was resolved and the lock
        was successfully re-acquired, or ``None`` if the message should be
        dropped.
        """
        conflict = self._on_lock_conflict

        if conflict is None or conflict == "drop":
            return None

        if conflict == "force":
            self._logger.info(
                "Force-releasing lock due to on_lock_conflict='force'",
                {"thread_id": thread_id, "lock_key": lock_key},
            )
            await self._state_adapter.force_release_lock(lock_key)
            return await self._state_adapter.acquire_lock(lock_key, DEFAULT_LOCK_TTL_MS)

        # Callable handler -- invoke and inspect result
        if callable(conflict):
            result = conflict(thread_id, message)
            # Support both sync and async callables
            if asyncio.iscoroutine(result) or asyncio.isfuture(result):
                result = await result
            if result == "force" or result is True:
                self._logger.info(
                    "on_lock_conflict callback returned 'force', force-releasing lock",
                    {"thread_id": thread_id, "lock_key": lock_key},
                )
                await self._state_adapter.force_release_lock(lock_key)
                return await self._state_adapter.acquire_lock(lock_key, DEFAULT_LOCK_TTL_MS)

        return None

    # -- Queue / Debounce strategy -------------------------------------------

    async def _handle_queue_or_debounce(
        self,
        adapter: Adapter,
        thread_id: str,
        lock_key: str,
        message: Message,
        strategy: str,
    ) -> None:
        max_queue_size = self._concurrency_max_queue_size
        queue_entry_ttl_ms = self._concurrency_queue_entry_ttl_ms
        on_queue_full = self._concurrency_on_queue_full
        debounce_ms = self._concurrency_debounce_ms

        lock = await self._state_adapter.acquire_lock(lock_key, DEFAULT_LOCK_TTL_MS)

        if lock is None:
            # Lock busy -- enqueue
            effective_max = 1 if strategy == "debounce" else max_queue_size
            depth = await self._state_adapter.queue_depth(lock_key)

            if depth >= effective_max and strategy != "debounce" and on_queue_full == "drop-newest":
                self._logger.info(
                    "message-dropped",
                    {
                        "thread_id": thread_id,
                        "lock_key": lock_key,
                        "message_id": message.id,
                        "reason": "queue-full",
                    },
                )
                return

            now = int(datetime.now(tz=UTC).timestamp() * 1000)
            entry = QueueEntry(
                message=message,
                enqueued_at=now,
                expires_at=now + queue_entry_ttl_ms,
            )
            await self._state_adapter.enqueue(lock_key, entry, effective_max)
            self._logger.info(
                "message-debounce-reset" if strategy == "debounce" else "message-queued",
                {"thread_id": thread_id, "lock_key": lock_key, "message_id": message.id},
            )
            return

        # We hold the lock
        self._logger.debug("Lock acquired", {"thread_id": thread_id, "lock_key": lock_key, "token": lock.token})

        try:
            if strategy == "debounce":
                now = int(datetime.now(tz=UTC).timestamp() * 1000)
                await self._state_adapter.enqueue(
                    lock_key,
                    QueueEntry(message=message, enqueued_at=now, expires_at=now + queue_entry_ttl_ms),
                    1,
                )
                self._logger.info(
                    "message-debouncing",
                    {
                        "thread_id": thread_id,
                        "message_id": message.id,
                        "debounce_ms": debounce_ms,
                    },
                )
                await self._debounce_loop(lock, adapter, thread_id, lock_key)
            else:
                await self._dispatch_to_handlers(adapter, thread_id, message)
                await self._drain_queue(lock, adapter, thread_id, lock_key)
        finally:
            await self._state_adapter.release_lock(lock)
            self._logger.debug("Lock released", {"thread_id": thread_id, "lock_key": lock_key})

    # -- Debounce loop -------------------------------------------------------

    async def _debounce_loop(
        self,
        lock: Lock,
        adapter: Adapter,
        thread_id: str,
        lock_key: str,
    ) -> None:
        debounce_ms = self._concurrency_debounce_ms
        max_iterations = 20
        iteration = 0

        while True:
            iteration += 1
            if iteration > max_iterations:
                self._logger.warn(
                    "Debounce loop exceeded max iterations, breaking",
                    {"thread_id": thread_id, "lock_key": lock_key, "max_iterations": max_iterations},
                )
                break

            await _sleep(debounce_ms)
            extended = await self._state_adapter.extend_lock(lock, DEFAULT_LOCK_TTL_MS)
            if not extended:
                self._logger.warn(
                    "Lock lost during debounce processing, aborting", {"thread_id": thread_id, "lock_key": lock_key}
                )
                return

            entry = await self._state_adapter.dequeue(lock_key)
            if entry is None:
                break

            msg = self._rehydrate_message(entry.message)
            now = int(datetime.now(tz=UTC).timestamp() * 1000)
            if now > entry.expires_at:
                self._logger.info("message-expired", {"thread_id": thread_id, "message_id": msg.id})
                continue

            depth = await self._state_adapter.queue_depth(lock_key)
            if depth > 0:
                self._logger.info("message-superseded", {"thread_id": thread_id, "dropped_id": msg.id})
                continue

            self._logger.info("message-dequeued", {"thread_id": thread_id, "message_id": msg.id})
            await self._dispatch_to_handlers(adapter, thread_id, msg)
            break

    # -- Drain queue ---------------------------------------------------------

    async def _drain_queue(
        self,
        lock: Lock,
        adapter: Adapter,
        thread_id: str,
        lock_key: str,
    ) -> None:
        while True:
            pending: list[tuple[Message, int]] = []
            while True:
                entry = await self._state_adapter.dequeue(lock_key)
                if entry is None:
                    break
                msg = self._rehydrate_message(entry.message)
                now = int(datetime.now(tz=UTC).timestamp() * 1000)
                if now <= entry.expires_at:
                    pending.append((msg, entry.expires_at))
                else:
                    self._logger.info("message-expired", {"thread_id": thread_id, "message_id": msg.id})

            if not pending:
                return

            extended = await self._state_adapter.extend_lock(lock, DEFAULT_LOCK_TTL_MS)
            if not extended:
                self._logger.warn(
                    "Lock lost during drain processing, aborting", {"thread_id": thread_id, "lock_key": lock_key}
                )
                return

            latest_msg, _ = pending[-1]
            skipped = [m for m, _ in pending[:-1]]

            self._logger.info(
                "message-dequeued",
                {
                    "thread_id": thread_id,
                    "message_id": latest_msg.id,
                    "skipped_count": len(skipped),
                    "total_since_last_handler": len(pending),
                },
            )

            context = MessageContext(
                skipped=skipped,
                total_since_last_handler=len(pending),
            )
            await self._dispatch_to_handlers(adapter, thread_id, latest_msg, context)

            # After dispatch, re-extend lock before next dequeue iteration
            extended = await self._state_adapter.extend_lock(lock, DEFAULT_LOCK_TTL_MS)
            if not extended:
                self._logger.warn("Lock lost after handler dispatch", {"thread_id": thread_id, "lock_key": lock_key})
                return

    # -- Concurrent strategy -------------------------------------------------

    async def _handle_concurrent(
        self,
        adapter: Adapter,
        thread_id: str,
        message: Message,
    ) -> None:
        await self._dispatch_to_handlers(adapter, thread_id, message)

    # ========================================================================
    # Dispatch to handlers
    # ========================================================================

    async def _dispatch_to_handlers(
        self,
        adapter: Adapter,
        thread_id: str,
        message: Message,
        context: MessageContext | None = None,
    ) -> None:
        """Route a message to the correct handler chain."""
        # Detect mention
        message.is_mention = message.is_mention or self._detect_mention(adapter, message)

        # Check subscription
        is_subscribed = await self._state_adapter.is_subscribed(thread_id)
        self._logger.debug("Subscription check", {"thread_id": thread_id, "is_subscribed": is_subscribed})

        thread = self._create_thread(adapter, thread_id, message, is_subscribed)

        # DM routing
        is_dm = (
            adapter.is_dm(thread_id)  # type: ignore[union-attr]
            if hasattr(adapter, "is_dm") and callable(getattr(adapter, "is_dm", None))
            else False
        )

        if is_dm and self._direct_message_handlers:
            self._logger.debug("Direct message received - calling handlers", {"thread_id": thread_id})
            channel = thread.channel
            for h in self._direct_message_handlers:
                await h(thread, message, channel, context)
            return

        # Backward compat: DMs without handlers treated as mentions
        if is_dm:
            message.is_mention = True

        # Subscribed thread
        if is_subscribed:
            self._logger.debug("Message in subscribed thread", {"thread_id": thread_id})
            await self._run_handlers(self._subscribed_message_handlers, thread, message, context)
            return

        # Mention
        if message.is_mention:
            self._logger.debug("Bot mentioned", {"thread_id": thread_id})
            await self._run_handlers(self._mention_handlers, thread, message, context)
            return

        # Pattern matching
        matched = False
        for pat in self._message_patterns:
            if pat.pattern.search(message.text):
                self._logger.debug("Message matched pattern", {"pattern": pat.pattern.pattern})
                matched = True
                await pat.handler(thread, message, context)

        if not matched:
            self._logger.debug("No handlers matched message", {"thread_id": thread_id})

    # ========================================================================
    # Thread creation
    # ========================================================================

    def _create_thread(
        self,
        adapter: Adapter,
        thread_id: str,
        initial_message: Message,
        is_subscribed_context: bool = False,
    ) -> ThreadImpl:
        channel_id = adapter.channel_id_from_thread_id(thread_id)
        is_dm = (
            adapter.is_dm(thread_id)  # type: ignore[union-attr]
            if hasattr(adapter, "is_dm") and callable(getattr(adapter, "is_dm", None))
            else False
        )
        channel_visibility: ChannelVisibility = (
            adapter.get_channel_visibility(thread_id)  # type: ignore[union-attr]
            if hasattr(adapter, "get_channel_visibility") and callable(getattr(adapter, "get_channel_visibility", None))
            else "unknown"
        )

        return ThreadImpl(
            _ThreadImplConfig(
                id=thread_id,
                adapter=adapter,
                channel_id=channel_id,
                state_adapter=self._state_adapter,
                initial_message=initial_message,
                is_subscribed_context=is_subscribed_context,
                is_dm=is_dm,
                channel_visibility=channel_visibility,
                current_message=initial_message,
                logger=self._logger,
                streaming_update_interval_ms=self._streaming_update_interval_ms,
                fallback_streaming_placeholder_text=self._fallback_streaming_placeholder_text,
                message_history=(self._message_history if adapter.persist_message_history else None),
            )
        )

    # ========================================================================
    # Mention detection
    # ========================================================================

    def _get_mention_pattern(self, key: str, pattern_str: str) -> re.Pattern[str]:
        """Return a cached compiled regex, compiling on first use."""
        pat = self._mention_patterns.get(key)
        if pat is None:
            pat = re.compile(pattern_str, re.IGNORECASE)
            self._mention_patterns[key] = pat
        return pat

    def _detect_mention(self, adapter: Adapter, message: Message) -> bool:
        bot_user_name = adapter.user_name or self._user_name
        bot_user_id = adapter.bot_user_id

        # @username check
        username_pattern = self._get_mention_pattern(f"username:{bot_user_name}", rf"@{re.escape(bot_user_name)}\b")
        if username_pattern.search(message.text):
            return True

        if bot_user_id:
            user_id_pattern = self._get_mention_pattern(f"userid:{bot_user_id}", rf"@{re.escape(bot_user_id)}\b")
            if user_id_pattern.search(message.text):
                return True

            # Discord <@USER_ID> or <@!USER_ID>
            discord_pattern = self._get_mention_pattern(f"discord:{bot_user_id}", rf"<@!?{re.escape(bot_user_id)}>")
            if discord_pattern.search(message.text):
                return True

        return False

    # ========================================================================
    # Message rehydration
    # ========================================================================

    def _rehydrate_message(self, raw: Any) -> Message:
        """Reconstruct a proper Message from a dequeued entry (may be plain dict)."""
        if isinstance(raw, Message):
            return raw

        if isinstance(raw, dict):
            if raw.get("_type") == "chat:Message":
                return _message_from_json(raw)
            # Fallback: plain dict
            metadata_raw = raw.get("metadata", {})
            date_sent = metadata_raw.get("date_sent")
            if isinstance(date_sent, str):
                date_sent = datetime.fromisoformat(date_sent)
            elif not isinstance(date_sent, datetime):
                date_sent = datetime.now(tz=UTC)

            edited_at = metadata_raw.get("edited_at")
            if isinstance(edited_at, str):
                edited_at = datetime.fromisoformat(edited_at)

            author_raw = raw.get("author", {})
            return Message(
                id=raw.get("id", ""),
                thread_id=raw.get("thread_id", ""),
                text=raw.get("text", ""),
                formatted=raw.get("formatted", {"type": "root", "children": []}),
                raw=raw.get("raw"),
                author=Author(
                    user_id=author_raw.get("user_id", ""),
                    user_name=author_raw.get("user_name", ""),
                    full_name=author_raw.get("full_name", ""),
                    is_bot=author_raw.get("is_bot", False),
                    is_me=author_raw.get("is_me", False),
                ),
                metadata=MessageMetadata(
                    date_sent=date_sent,
                    edited=metadata_raw.get("edited", False),
                    edited_at=edited_at,
                ),
                attachments=raw.get("attachments", []),
                is_mention=raw.get("is_mention"),
                links=raw.get("links", []),
            )

        # Last resort: assume it's already a Message-like object
        return raw  # type: ignore[return-value]

    # ========================================================================
    # Handler execution
    # ========================================================================

    async def _run_handlers(
        self,
        handlers: list[Any],
        thread: ThreadImpl,
        message: Message,
        context: MessageContext | None = None,
    ) -> None:
        for h in handlers:
            await h(thread, message, context)


# ---------------------------------------------------------------------------
# Helper: construct Message from serialized dict
# ---------------------------------------------------------------------------


def _message_from_json(data: dict[str, Any]) -> Message:
    author_raw = data.get("author", {})
    metadata_raw = data.get("metadata", {})

    date_sent = metadata_raw.get("dateSent") or metadata_raw.get("date_sent")
    if isinstance(date_sent, str):
        date_sent = datetime.fromisoformat(date_sent)
    elif not isinstance(date_sent, datetime):
        date_sent = datetime.now(tz=UTC)

    edited_at = metadata_raw.get("editedAt") or metadata_raw.get("edited_at")
    if isinstance(edited_at, str):
        edited_at = datetime.fromisoformat(edited_at)

    return Message(
        id=data.get("id", ""),
        thread_id=data.get("threadId") or data.get("thread_id", ""),
        text=data.get("text", ""),
        formatted=data.get("formatted", {"type": "root", "children": []}),
        raw=data.get("raw"),
        author=Author(
            user_id=author_raw.get("userId") or author_raw.get("user_id", ""),
            user_name=author_raw.get("userName") or author_raw.get("user_name", ""),
            full_name=author_raw.get("fullName") or author_raw.get("full_name", ""),
            is_bot=author_raw.get("isBot") if "isBot" in author_raw else author_raw.get("is_bot", False),
            is_me=author_raw.get("isMe") if "isMe" in author_raw else author_raw.get("is_me", False),
        ),
        metadata=MessageMetadata(
            date_sent=date_sent,
            edited=metadata_raw.get("edited", False),
            edited_at=edited_at,
        ),
        attachments=data.get("attachments", []),
        is_mention=data.get("isMention") if "isMention" in data else data.get("is_mention"),
        links=data.get("links", []),
    )


# ---------------------------------------------------------------------------
# Minimal MessageHistoryCache (placeholder -- real impl uses StateAdapter lists)
# ---------------------------------------------------------------------------


class _MessageHistoryCache:
    """Lightweight in-SDK message history cache backed by the state adapter."""

    def __init__(self, state: StateAdapter, config: dict[str, Any] | None = None) -> None:
        self._state = state
        self._max_messages = (config or {}).get("max_messages", 100)
        self._ttl_ms = (config or {}).get("ttl_ms", 30 * 24 * 60 * 60 * 1000)

    async def append(self, thread_id: str, message: Message) -> None:
        key = f"msg-history:{thread_id}"
        data = message.to_json()
        await self._state.append_to_list(key, data, max_length=self._max_messages, ttl_ms=self._ttl_ms)

    async def get_messages(self, thread_id: str, limit: int | None = None) -> list[Message]:
        key = f"msg-history:{thread_id}"
        raw_list = await self._state.get_list(key)
        messages = [_message_from_json(r) if isinstance(r, dict) else r for r in raw_list]
        if limit is not None:
            messages = messages[-limit:]
        return messages


# ---------------------------------------------------------------------------
# Config helper used by Chat._create_thread internally
# (avoids importing channel config types at module level)
# ---------------------------------------------------------------------------


@dataclass
class _ChannelImplConfigForChat:
    """Config passed from Chat to ChannelImpl."""

    id: str
    adapter: Adapter
    state_adapter: StateAdapter
    channel_visibility: ChannelVisibility = "unknown"
    is_dm: bool = False
    message_history: Any = None
