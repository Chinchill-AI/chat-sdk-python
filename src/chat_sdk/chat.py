"""Chat orchestrator for chat-sdk.

Python port of Vercel Chat SDK chat.ts + chat-singleton.ts.
Main entry point: takes a ChatConfig, registers event handlers via decorator-style
methods, routes webhooks to adapters, manages concurrency, deduplication, and
thread/channel creation.
"""

from __future__ import annotations

import asyncio
import contextvars
import dataclasses
import inspect
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, Literal

from chat_sdk.callback_url import (
    CallbackContext,
    decode_callback_value,
    post_to_callback_url,
    resolve_callback_url,
)
from chat_sdk.channel import ChannelImpl, _ChannelImplConfigWithAdapter
from chat_sdk.context import conversation
from chat_sdk.errors import ChatError, ChatNotImplementedError, LockError
from chat_sdk.history import HistoryApiImpl, UserHistoryApiImpl
from chat_sdk.logger import ConsoleLogger, Logger
from chat_sdk.thread import (
    ThreadImpl,
    _active_chat,
    _ThreadImplConfig,
    get_chat_singleton,
    has_chat_singleton,
    set_chat_singleton,
)
from chat_sdk.types import (
    ActionEvent,
    Adapter,
    AgentSessionStoppedEvent,
    AgentSessionStoppedHandler,
    AgentSessionTitleChangedEvent,
    AgentSessionTitleChangedHandler,
    AppContextChangedEvent,
    AppContextChangedHandler,
    AppHomeOpenedEvent,
    AssistantContextChangedEvent,
    AssistantThreadStartedEvent,
    Attachment,
    Author,
    Channel,
    ChannelVisibility,
    ChatConfig,
    ConcurrencyConfig,
    ConcurrencyStrategy,
    EmojiValue,
    IdentityContext,
    IdentityResolver,
    InstallationEvent,
    InstalledEvent,
    InstalledHandler,
    Lock,
    LockScope,
    LockScopeContext,
    MemberJoinedChannelEvent,
    Message,
    MessageContext,
    MessageDeletedEvent,
    MessageDeletedHandler,
    MessageMetadata,
    MessageUpdatedHandler,
    ModalCloseEvent,
    ModalResponse,
    ModalSubmitEvent,
    OnLockConflict,
    OptionsLoadEvent,
    OptionsLoadResult,
    QueueEntry,
    ReactionEvent,
    SlashCommandEvent,
    StateAdapter,
    StreamOptions,
    TranscriptsApi,
    TranscriptsConfig,
    TurnSignal,
    UninstalledEvent,
    UninstalledHandler,
    UserHistoryConfig,
    UserInfo,
    WebhookOptions,
    _parse_iso,
    set_message_adapter,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_LOCK_TTL_MS = 30_000  # 30 seconds
DEFAULT_MAX_LOCK_LIFETIME_MS = 600_000  # 10 minutes
# 10 minutes: outlives Slack's ~+5 min Events API retry (vercel/chat#667).
DEDUPE_TTL_MS = 10 * 60 * 1000
MODAL_CONTEXT_TTL_MS = 24 * 60 * 60 * 1000  # 24 hours
# Turn cancellation (adapters with ``supports_turn_cancellation``): how long
# the ``active-turn:`` / ``abort-turn:`` markers live, and how often a running
# turn polls for an abort from another process.
ACTIVE_TURN_TTL_MS = 60 * 60 * 1000  # 1 hour
ABORT_POLL_INTERVAL_MS = 250

SLACK_USER_ID_REGEX = re.compile(r"^[UW][A-Z0-9]+$")
DISCORD_SNOWFLAKE_REGEX = re.compile(r"^\d{17,19}$")
LINEAR_UUID_REGEX = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
NUMERIC_REGEX = re.compile(r"^\d+$")

# ---------------------------------------------------------------------------
# Handler type aliases
# ---------------------------------------------------------------------------

MentionHandler = Callable[[Any, Message, Any], Awaitable[None] | None]
DirectMessageHandler = Callable[[Any, Message, Any, Any], Awaitable[None] | None]
MessageHandler = Callable[[Any, Message, Any], Awaitable[None] | None]
SubscribedMessageHandler = Callable[[Any, Message, Any], Awaitable[None] | None]
ReactionHandler = Callable[[ReactionEvent], Any]
ActionHandler = Callable[[ActionEvent], Any]
OptionsLoadHandler = Callable[
    [OptionsLoadEvent],
    Awaitable[OptionsLoadResult | None] | OptionsLoadResult | None,
]
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


class _OptionsLoadPattern:
    __slots__ = ("action_ids", "handler")

    def __init__(self, action_ids: list[str], handler: OptionsLoadHandler) -> None:
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
    __slots__ = ("thread", "message", "channel", "callback_url")

    def __init__(
        self,
        thread: dict[str, Any] | None = None,
        message: dict[str, Any] | None = None,
        channel: dict[str, Any] | None = None,
        callback_url: str | None = None,
    ) -> None:
        self.thread = thread
        self.message = message
        self.channel = channel
        self.callback_url = callback_url


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


async def _sleep(ms: int) -> None:
    """Promise-based sleep for debounce timing."""
    await asyncio.sleep(ms / 1000.0)


def _now_ms() -> int:
    """Epoch milliseconds (``Date.now()``).

    Module-level so tests can swap in a fake clock together with :func:`_sleep`.
    """
    return int(time.time() * 1000)


def _monotonic_ms() -> int:
    """Monotonic milliseconds for scheduling heartbeat ticks.

    ``setInterval`` fires on a monotonic timer, so a wall-clock step must not
    move the tick schedule. Epoch time (:func:`_now_ms`) still backs the
    lifetime cap and ``held_until``, as upstream's ``Date.now()`` does.
    Module-level so tests can swap in a fake clock.
    """
    return int(time.monotonic() * 1000)


class _LockHeartbeat:
    """Keeps a held thread lock alive while a handler runs.

    Port of upstream ``startLockHeartbeat`` (chat@4.41.1, 5b538f6f). Every
    ``DEFAULT_LOCK_TTL_MS / 3`` it calls ``state.extend_lock``; it stops
    renewing once ``max_lock_lifetime_ms`` has elapsed (so a hung handler's
    lock lapses at its TTL), and marks ownership lost when an extend returns
    ``False`` or the backend stays unreachable past ``held_until``.

    asyncio translation of ``setInterval`` + ``inFlight``:

    * Ticks are fixed-rate on a monotonic clock (``start + k * TTL/3``), as
      with ``setInterval``. The loop awaits each extend, and ticks that came due
      while it was in flight are skipped (upstream's "don't stack extends"
      rule), so a slow extend never pushes later ticks back.
    * The extend runs as its own task awaited through ``asyncio.shield``:
      cancelling a task cancels the I/O it awaits (a JS promise cannot be
      cancelled), and ``stop()`` must not interrupt a backend call mid-command.
    * ``stop()`` only awaits when an extend is in flight. Awaiting the
      cancelled loop task would cost a full event-loop iteration, where
      upstream's ``clearInterval(); await undefined`` costs none.

    Use via :meth:`Chat._with_held_lock`, which awaits :meth:`stop` before
    ``release_lock``.
    """

    def __init__(self, state: StateAdapter, lock: Lock, max_lifetime_ms: int, logger: Logger) -> None:
        self._state = state
        self._lock = lock
        self._max_lifetime_ms = max_lifetime_ms
        self._logger = logger
        self._started_at = _now_ms()
        # Latest instant we know the lock is still ours; refreshed on each
        # successful extend. Divergence from upstream (``lock.expiresAt``) —
        # see docs/UPSTREAM_SYNC.md: the Python Postgres backend stamps
        # ``expires_at`` with the database clock, so a lagging database would
        # make a fresh lock look lapsed. Seed it with the same client-side rule
        # upstream applies after every successful extend.
        self._held_until = _now_ms() + DEFAULT_LOCK_TTL_MS
        self._ownership_lost = False
        self._stopped = False
        self._in_flight: asyncio.Task[None] | None = None
        self._task: asyncio.Task[None] = asyncio.get_running_loop().create_task(self._run())

    @property
    def task(self) -> asyncio.Task[None]:
        """The renewal loop task (exposed for lifecycle assertions)."""
        return self._task

    def is_ownership_lost(self) -> bool:
        return self._ownership_lost or _now_ms() >= self._held_until

    async def _run(self) -> None:
        # CancelledError is deliberately not caught: ``stop()`` cancels this task.
        interval = DEFAULT_LOCK_TTL_MS // 3
        next_tick = _monotonic_ms() + interval
        try:
            while True:
                # Fixed rate on a monotonic clock, like ``setInterval``: sleep
                # to the next tick deadline, not a full interval after the
                # previous extend, and ignore wall-clock steps.
                await _sleep(max(0, next_tick - _monotonic_ms()))
                next_tick += interval
                if self._stopped:
                    return
                if _now_ms() - self._started_at >= self._max_lifetime_ms:
                    # Renewal cap: let the lock lapse at its TTL so a hung
                    # handler can't block the thread forever.
                    self._logger.warn(
                        "Lock heartbeat reached max_lock_lifetime_ms — the lock will lapse at its TTL",
                        {
                            "thread_id": self._lock.thread_id,
                            "token": self._lock.token,
                            "max_lock_lifetime_ms": self._max_lifetime_ms,
                        },
                    )
                    return
                self._in_flight = asyncio.get_running_loop().create_task(self._extend_once())
                await asyncio.shield(self._in_flight)
                self._in_flight = None
                if self._ownership_lost:
                    return
                # Ticks that came due while the extend was in flight are
                # skipped (upstream: ``if (inFlight) return``).
                now = _monotonic_ms()
                while next_tick <= now:
                    next_tick += interval
        except Exception as err:
            # Port of the interval callback's ``.catch``: an exception in an
            # un-awaited task would otherwise surface only at GC.
            self._logger.error(
                "Lock heartbeat crashed — the lock will lapse at its TTL",
                {"error": err, "thread_id": self._lock.thread_id, "token": self._lock.token},
            )

    async def _extend_once(self) -> None:
        try:
            extended = await self._state.extend_lock(self._lock, DEFAULT_LOCK_TTL_MS)
        except Exception as err:
            if self._stopped:
                return
            if _now_ms() >= self._held_until:
                self._ownership_lost = True
                self._logger.warn(
                    "Lock lapsed while the heartbeat could not reach the state backend",
                    {"error": err, "thread_id": self._lock.thread_id, "token": self._lock.token},
                )
            else:
                self._logger.warn(
                    "Lock heartbeat failed",
                    {"error": err, "thread_id": self._lock.thread_id, "token": self._lock.token},
                )
            return
        if extended:
            self._held_until = _now_ms() + DEFAULT_LOCK_TTL_MS
            return
        self._ownership_lost = True
        if not self._stopped:
            self._logger.warn(
                "Lock heartbeat stopped after ownership was lost",
                {"thread_id": self._lock.thread_id, "token": self._lock.token},
            )

    async def stop(self) -> None:
        """Stop renewing and wait for any in-flight extend (``stopped = true; clearInterval(); await inFlight``)."""
        self._stopped = True
        self._task.cancel()
        if self._in_flight is not None:
            await asyncio.shield(self._in_flight)


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
# Chat activation context manager
# ---------------------------------------------------------------------------


class _ChatActivation:
    """Context manager that sets a Chat as the active instance for the current context."""

    def __init__(self, chat: Chat) -> None:
        self._chat = chat
        self._token: contextvars.Token[Any] | None = None

    def __enter__(self) -> Chat:
        self._token = _active_chat.set(self._chat)  # type: ignore[arg-type]
        return self._chat

    def __exit__(self, *_: Any) -> None:
        if self._token is not None:
            _active_chat.reset(self._token)
            self._token = None


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
        self._dedupe_ttl_ms = config.dedupe_ttl_ms if config.dedupe_ttl_ms is not None else DEDUPE_TTL_MS
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
            self._concurrency_max_lock_lifetime_ms = DEFAULT_MAX_LOCK_LIFETIME_MS
        elif isinstance(concurrency, str):
            self._concurrency_strategy = concurrency
            self._concurrency_debounce_ms = 1500
            self._concurrency_max_concurrent = None
            self._concurrency_max_queue_size = 10
            self._concurrency_on_queue_full = "drop-oldest"
            self._concurrency_queue_entry_ttl_ms = 90_000
            self._concurrency_max_lock_lifetime_ms = DEFAULT_MAX_LOCK_LIFETIME_MS
        else:
            # ConcurrencyConfig dataclass
            self._concurrency_strategy = concurrency.strategy
            self._concurrency_debounce_ms = concurrency.debounce_ms
            self._concurrency_max_concurrent = concurrency.max_concurrent
            self._concurrency_max_queue_size = concurrency.max_queue_size
            self._concurrency_on_queue_full = concurrency.on_queue_full
            self._concurrency_queue_entry_ttl_ms = concurrency.queue_entry_ttl_ms
            # ``??`` semantics: an explicit ``None`` falls back to the default.
            self._concurrency_max_lock_lifetime_ms = (
                concurrency.max_lock_lifetime_ms
                if concurrency.max_lock_lifetime_ms is not None
                else DEFAULT_MAX_LOCK_LIFETIME_MS
            )
        # Config validation (see docs/UPSTREAM_SYNC.md). JS coerces a numeric
        # string in ``Date.now() - startedAt >= maxLockLifetimeMs``; Python
        # raises TypeError on the heartbeat's first tick, silently ending
        # renewal. Fail fast here instead. ``bool`` is rejected too.
        lifetime = self._concurrency_max_lock_lifetime_ms
        if isinstance(lifetime, bool) or not isinstance(lifetime, int) or lifetime < 0:
            raise ValueError(
                f"ConcurrencyConfig.max_lock_lifetime_ms must be a non-negative integer (milliseconds) or None; "
                f"got {lifetime!r}."
            )

        # -- Concurrent-strategy semaphore ------------------------------------
        # Divergence from upstream — see docs/UPSTREAM_SYNC.md.
        # `max_concurrent` bounds in-flight handler dispatches when using the
        # `"concurrent"` strategy. `None` means unbounded (matches the upstream
        # TS default of `Infinity`). A positive integer caps parallel handler
        # runs. Upstream accepts the config field but never enforces it
        # (3 writes, 0 reads); we enforce it via `asyncio.Semaphore`.
        #
        # Only construct the semaphore when the strategy actually uses it —
        # if a user sets `max_concurrent=5` with `strategy="queue"`, they
        # have a misconfiguration that we surface as a `ValueError` rather
        # than silently allocating an unused primitive.
        #
        # Reject `<= 0` explicitly rather than silently ignoring — a user
        # passing `max_concurrent=0` likely means "pause all processing"
        # (not supported) or has a typo. Either way, silently falling back
        # to unbounded concurrency would surprise them.
        raw_max = concurrency.max_concurrent if isinstance(concurrency, ConcurrencyConfig) else None
        if raw_max is not None and (
            # Reject non-int (including bool, which is an int subclass but
            # semantically meaningless here) before any arithmetic —
            # `asyncio.Semaphore(1.5)` silently goes negative, `Semaphore(True)`
            # allocates a 1-way bound from a boolean, and `Semaphore("2")`
            # raises `TypeError` instead of our ValueError.
            isinstance(raw_max, bool) or not isinstance(raw_max, int) or raw_max <= 0
        ):
            raise ValueError(
                f"ConcurrencyConfig.max_concurrent must be a positive integer or None; "
                f"got {raw_max!r}. Pass None for unbounded concurrency."
            )
        if self._concurrency_max_concurrent is not None and self._concurrency_strategy != "concurrent":
            raise ValueError(
                f"ConcurrencyConfig.max_concurrent is only honored when strategy='concurrent'; "
                f"got strategy={self._concurrency_strategy!r}. Either switch to strategy='concurrent' "
                "or drop max_concurrent."
            )
        self._concurrent_semaphore: asyncio.Semaphore | None = (
            asyncio.Semaphore(self._concurrency_max_concurrent)
            if self._concurrency_max_concurrent is not None
            else None
        )

        # -- Thread history ----------------------------------------------------
        # Precedence mirrors upstream
        # `config.history?.thread ?? config.threadHistory ?? config.messageHistory`
        # (`message_history` is the deprecated alias of `thread_history`).
        history_config = config.history
        thread_history_config = history_config.thread if history_config is not None else None
        if thread_history_config is None:
            thread_history_config = (
                config.thread_history if config.thread_history is not None else config.message_history
            )
        self._thread_history = _ThreadHistoryCache(self._state_adapter, thread_history_config)

        # -- User history (cross-platform per-user persistence) ----------------
        # `history.user` is merged over the legacy `transcripts` block field by
        # field, so a migration that only moves the identity resolver keeps
        # the retention / max_per_user settings still on the legacy block.
        user_history_config = _merge_user_history_config(
            config.transcripts, history_config.user if history_config is not None else None
        )
        history_user_identity = (
            history_config.user.identity if history_config is not None and history_config.user is not None else None
        )
        resolved_identity = history_user_identity if history_user_identity is not None else config.identity
        if user_history_config is not None and resolved_identity is None:
            raise ValueError(
                "ChatConfig requires an identity resolver when user history (or legacy `transcripts`) is "
                "configured — set `history.user.identity` or the deprecated top-level `identity` field"
            )
        self._identity: IdentityResolver | None = resolved_identity

        # -- Unified History API -----------------------------------------------
        # The resolver closes over `self._adapters` so adapters registered
        # after construction are seen at call time.
        self._history = HistoryApiImpl(
            lambda name: self._adapters.get(name),
            cache=self._thread_history,
            user=(
                UserHistoryApiImpl(self._state_adapter, user_history_config)
                if user_history_config is not None
                else None
            ),
        )

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
        self._options_load_handlers: list[_OptionsLoadPattern] = []
        self._modal_submit_handlers: list[_ModalSubmitPattern] = []
        self._modal_close_handlers: list[_ModalClosePattern] = []
        self._slash_command_handlers: list[_SlashCommandPattern] = []
        self._assistant_thread_started_handlers: list[AssistantThreadStartedHandler] = []
        self._assistant_context_changed_handlers: list[AssistantContextChangedHandler] = []
        self._agent_session_stopped_handlers: list[AgentSessionStoppedHandler] = []
        self._agent_session_title_changed_handlers: list[AgentSessionTitleChangedHandler] = []
        self._app_home_opened_handlers: list[AppHomeOpenedHandler] = []
        self._app_context_changed_handlers: list[AppContextChangedHandler] = []
        self._member_joined_channel_handlers: list[MemberJoinedChannelHandler] = []
        self._message_updated_handlers: list[MessageUpdatedHandler] = []
        self._message_deleted_handlers: list[MessageDeletedHandler] = []
        self._installed_handlers: list[InstalledHandler] = []
        self._uninstalled_handlers: list[UninstalledHandler] = []

        # -- Init state -------------------------------------------------------
        self._init_promise: asyncio.Task[None] | None = None
        self._initialized = False

        # -- Active handler tasks (for cancellation on shutdown) --------------
        self._active_tasks: set[asyncio.Task[Any]] = set()

        # -- Running turns: thread_id -> turn_id -> signal (``abort_turn``) ---
        self._active_turn_signals: dict[str, dict[str, TurnSignal]] = {}

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
    # History API
    # ========================================================================

    @property
    def history(self) -> HistoryApiImpl:
        """Unified History API.

        - ``history.user``: cross-platform per-user transcript store (raises
          on access if not configured)
        - ``history.thread``: per-thread message listing
        - ``history.channel``: channel-level messages and thread listings
        """
        return self._history

    @property
    def transcripts(self) -> TranscriptsApi:
        """Deprecated: use ``chat.history.user`` instead.

        Available only when user history (``history.user`` or the legacy
        ``transcripts``) is configured with an identity resolver.  Raises on
        access otherwise so callers fail loudly rather than silently
        no-op'ing.
        """
        return self.history.user

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

    def activate(self) -> _ChatActivation:
        """Set this Chat as the active instance for the current async context.

        Usage::

            with chat.activate():
                # Thread/Channel deserialization resolves to this chat
                thread = ThreadImpl.from_json(data)

        This is preferred over ``register_singleton()`` when multiple Chat
        instances coexist (e.g., in tests, multi-tenant servers). The
        activation is scoped to the current :class:`contextvars.Context`,
        so concurrent async tasks don't interfere.
        """
        return _ChatActivation(self)

    # ========================================================================
    # ChatInstance protocol implementation
    # ========================================================================

    def get_adapter(self, name: str) -> Adapter | None:
        return self._adapters.get(name)

    def get_state(self) -> StateAdapter:
        return self._state_adapter

    def get_streaming_options(self) -> StreamOptions:
        """Streaming defaults applied to threads restored into this Chat.

        Only ``update_interval_ms`` and ``fallback_streaming_placeholder_text``
        are set; the placeholder is ``UNSET`` when not configured.
        Internal: used by restored threads (vercel/chat#967).
        """
        return StreamOptions(
            update_interval_ms=self._streaming_update_interval_ms,
            fallback_streaming_placeholder_text=self._fallback_streaming_placeholder_text,
        )

    def owns_adapter(self, adapter: Adapter) -> bool:
        """Whether this exact adapter instance is registered with this Chat.

        Compares by identity over the registered values, since the key an
        adapter is registered under may differ from ``adapter.name``.
        Internal: used by restored threads and channels (vercel/chat#967).
        """
        return any(registered is adapter for registered in self._adapters.values())

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
        # Avoid concurrent initialization: every caller shares one attempt
        # task. A failed *state* connection is forgotten so the next caller
        # retries once the dependency (e.g. Redis) has recovered; an adapter
        # ``initialize()`` failure stays cached until ``shutdown()`` so a retry
        # never re-initializes adapters that already started (vercel/chat#924).
        # No await between the check and the assignment, so no lock is needed.
        attempt = self._init_promise
        if attempt is None:
            self._logger.info("Initializing chat instance...")
            # Construct the Task directly (never eagerly started) rather than
            # via ``loop.create_task``: an eager task factory would run a
            # synchronously failing ``connect()`` before ``_init_promise`` is
            # assigned, and the failure would then be cached. Upstream's
            # ``.catch`` always runs after the assignment.
            attempt = asyncio.Task(self._run_init_attempt(), loop=asyncio.get_running_loop())
            # Mark the failure observed: if every caller was cancelled, the
            # shield drops its callback and nobody retrieves the exception
            # ("Task exception was never retrieved"). Awaiting callers still
            # get it re-raised.
            attempt.add_done_callback(lambda t: t.cancelled() or t.exception())
            self._init_promise = attempt
        # Shield so a cancelled caller (e.g. an aborted webhook request) does
        # not cancel the attempt that concurrent callers share.
        await asyncio.shield(attempt)

    async def _run_init_attempt(self) -> None:
        try:
            await self._state_adapter.connect()
        except BaseException:
            # Only forget this attempt if it is still the current one: a
            # ``shutdown()`` + newer ``initialize()`` may have replaced it.
            if self._init_promise is asyncio.current_task():
                self._init_promise = None
            raise
        await self._do_initialize()

    async def _do_initialize(self) -> None:
        self._logger.debug("State connected")

        init_tasks = []
        for adapter in self._adapters.values():
            self._logger.debug("Initializing adapter", adapter.name)
            init_tasks.append(adapter.initialize(self))  # type: ignore[arg-type]

        try:
            await asyncio.gather(*init_tasks)
        except Exception as err:
            self._logger.error(
                "Adapter initialization failed; call shutdown() before retrying initialization",
                {"error": str(err)},
            )
            raise
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
        # Upstream parity (chat.ts ``shutdown``): an in-flight initialization
        # attempt is forgotten, not cancelled. It settles for its own callers,
        # as in upstream where the attempt promise cannot be cancelled (pinned
        # by test_keeps_a_newer_attempt_when_a_preshutdown_state_connection_rejects).
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
        """Register a handler for direct messages.

        Called for every message received in a DM thread when at least one
        direct message handler is registered. Direct message handlers run
        before :py:meth:`on_subscribed_message`, :py:meth:`on_mention`, and
        pattern handlers.

        If no ``on_direct_message`` handlers are registered, DMs continue
        through normal routing. Unsubscribed DMs fall through to
        :py:meth:`on_mention` for backward compatibility.

        Args:
            handler: Handler called for DM messages. Receives
                ``(thread, message, channel, context)``.
        """
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

    # -- Options load ---

    def on_options_load(
        self,
        action_ids_or_handler: str | list[str] | OptionsLoadHandler | None = None,
        handler: OptionsLoadHandler | None = None,
    ) -> OptionsLoadHandler | Callable[[OptionsLoadHandler], OptionsLoadHandler]:
        """Register a handler for loading dynamic options for external selects.

        Specific action IDs run before catch-all handlers.

        Overloaded:
        - ``chat.on_options_load(handler)`` -- all selects
        - ``chat.on_options_load("id", handler)``
        - ``chat.on_options_load(["id1", "id2"], handler)``
        - Decorator: ``@chat.on_options_load("id")``
        """
        if callable(action_ids_or_handler) and handler is None:
            self._options_load_handlers.append(_OptionsLoadPattern([], action_ids_or_handler))
            self._logger.debug("Registered options load handler for all action IDs")
            return action_ids_or_handler

        if isinstance(action_ids_or_handler, (str, list)) and handler is not None:
            ids = [action_ids_or_handler] if isinstance(action_ids_or_handler, str) else action_ids_or_handler
            self._options_load_handlers.append(_OptionsLoadPattern(ids, handler))
            self._logger.debug("Registered options load handler", {"action_ids": ids})
            return handler

        # Decorator form
        ids = (
            [action_ids_or_handler]
            if isinstance(action_ids_or_handler, str)
            else (action_ids_or_handler if isinstance(action_ids_or_handler, list) else [])
        )

        def decorator(h: OptionsLoadHandler) -> OptionsLoadHandler:
            self._options_load_handlers.append(_OptionsLoadPattern(ids, h))
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

    def on_agent_session_stopped(self, handler: AgentSessionStoppedHandler) -> AgentSessionStoppedHandler:
        """Handle the user stopping an agent session's turn (Slack native stop).

        Runs without the thread lock (the stopped turn still holds it).
        """
        self._agent_session_stopped_handlers.append(handler)
        self._logger.debug("Registered agent session stopped handler")
        return handler

    def on_agent_session_title_changed(
        self, handler: AgentSessionTitleChangedHandler
    ) -> AgentSessionTitleChangedHandler:
        """Handle an agent session's title change."""
        self._agent_session_title_changed_handlers.append(handler)
        self._logger.debug("Registered agent session title changed handler")
        return handler

    def on_app_home_opened(self, handler: AppHomeOpenedHandler) -> AppHomeOpenedHandler:
        self._app_home_opened_handlers.append(handler)
        self._logger.debug("Registered app home opened handler")
        return handler

    def on_app_context_changed(self, handler: AppContextChangedHandler) -> AppContextChangedHandler:
        self._app_context_changed_handlers.append(handler)
        self._logger.debug("Registered app context changed handler")
        return handler

    def on_installed(self, handler: InstalledHandler) -> InstalledHandler:
        """Handle bot installation, including upgrades that add the bot (currently Teams only)."""
        self._installed_handlers.append(handler)
        return handler

    def on_uninstalled(self, handler: UninstalledHandler) -> UninstalledHandler:
        """Handle bot removal, including upgrades that remove the bot (currently Teams only)."""
        self._uninstalled_handlers.append(handler)
        return handler

    def on_member_joined_channel(self, handler: MemberJoinedChannelHandler) -> MemberJoinedChannelHandler:
        self._member_joined_channel_handlers.append(handler)
        self._logger.debug("Registered member joined channel handler")
        return handler

    # -- Message lifecycle events ---

    def on_message_updated(self, handler: MessageUpdatedHandler) -> MessageUpdatedHandler:
        """Register a handler for message edit/update events.

        Called as ``handler(thread, message, previous_message)``. These
        lifecycle events are dispatched directly by adapters and never route
        through ``on_message`` / ``on_mention`` / ``on_subscribed_message``.
        The bot's own edits are skipped.
        """
        self._message_updated_handlers.append(handler)
        self._logger.debug("Registered message updated handler")
        return handler

    def on_message_deleted(self, handler: MessageDeletedHandler) -> MessageDeletedHandler:
        """Register a handler for message delete events.

        Delete events usually do not include the deleted message body;
        handlers get a :class:`MessageDeletedEvent` with the normalized
        platform IDs needed to update external storage.
        """
        self._message_deleted_handlers.append(handler)
        self._logger.debug("Registered message deleted handler")
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

        Uses explicit ``chat=self`` for deserialization instead of mutating
        process-global state. Each Chat instance produces a reviver bound
        to itself.
        """
        chat = self

        def _reviver(key: str, value: Any) -> Any:
            if isinstance(value, dict) and "_type" in value:
                t = value["_type"]
                if t == "chat:Thread":
                    return ThreadImpl.from_json(value, chat=chat)
                if t == "chat:Channel":
                    return ChannelImpl.from_json(value, chat=chat)
                if t == "chat:Message":
                    message = _message_from_json(value)
                    # Bind to this Chat's adapter so `message.subject`
                    # resolves (vercel/chat#967).
                    owner = chat.get_adapter(message.thread_id.split(":")[0])
                    if owner is not None:
                        set_message_adapter(message, owner)
                    return message
            return value

        return _reviver

    # ========================================================================
    # Process* methods (called by adapters)
    # ========================================================================

    def _hand_to_wait_until(
        self,
        task: asyncio.Task[Any],
        options: WebhookOptions | None,
        *,
        propagate: bool = False,
    ) -> None:
        """Hand a handler task to ``options.wait_until``.

        Upstream passes ``waitUntil`` a ``task.catch(log)`` promise that
        fulfils even when the handler fails; only message/action/slash-command
        honour ``propagateHandlerErrors`` (vercel/chat#943). ``propagate``
        marks those three call sites.
        """
        if not options or not options.wait_until:
            return
        if propagate and options.propagate_handler_errors:
            options.wait_until(task)
            return
        tracked = self._tracked(task)
        if tracked is not None:
            options.wait_until(tracked)

    def _tracked(self, task: asyncio.Task[Any]) -> asyncio.Task[None] | None:
        """Return a Task that completes when *task* does, swallowing its error.

        The TS ``task.catch(...)`` equivalent. It must be a real ``Task`` (the
        Teams DM streaming gate hooks ``add_done_callback``), and it awaits
        ``asyncio.shield(task)`` so cancelling the wrapper (e.g. a host's
        ``wait_until`` bookkeeping) never cancels the handler. The error is
        already logged by the handler task's done-callback.
        """

        async def _await_quietly() -> None:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise  # the wrapper itself was cancelled
                # The handler task was cancelled: treat as completion.
            except Exception:  # noqa: S110 — logged by the task's done-callback
                pass

        return _create_task(_await_quietly(), self._active_tasks)

    def process_message(
        self,
        adapter: Adapter,
        thread_id: str,
        message_or_factory: Message | Callable[[], Awaitable[Message]],
        options: WebhookOptions | None = None,
    ) -> asyncio.Task[None] | None:
        """Process an incoming message from an adapter.

        Handles waitUntil registration and error catching. Returns the
        handler task (``None`` only when no event loop is running) so
        streaming callers can await full handler completion and observe
        handler exceptions; fire-and-forget webhook callers may ignore it.
        By default ``wait_until`` receives a task that completes normally
        when the handler fails (the error is logged) — platforms shouldn't
        retry on handler bugs; ``WebhookOptions.propagate_handler_errors``
        hands it the raw task instead (vercel/chat#943). ``deduplicate=False``
        skips chat-level deduplication (vercel/chat#942).
        """
        skip_dedupe = options is not None and options.deduplicate is False

        async def _task() -> None:
            msg = await message_or_factory() if callable(message_or_factory) else message_or_factory
            if skip_dedupe:
                # Upstream processMessage wraps the dedupe-bypass route in
                # runInConversation too; handle_incoming_message enters it itself.
                with conversation(thread_id):
                    await self._route_incoming_message(adapter, thread_id, msg, deduplicate=False)
            else:
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
            self._hand_to_wait_until(task, options, propagate=True)
        return task

    def process_message_updated(
        self,
        adapter: Adapter,
        thread_id: str,
        message: Message | Callable[[], Awaitable[Message]],
        *,
        previous_message: Message | Callable[[], Awaitable[Message]] | None = None,
        options: WebhookOptions | None = None,
    ) -> asyncio.Task[None] | None:
        """Process an incoming message update (edit) from an adapter.

        ``message`` / ``previous_message`` are each a parsed :class:`Message`
        or an async factory for lazy parsing. Updates bypass routing,
        deduplication and locking, and the bot's own edits are skipped.
        ``previous_message`` and ``options`` are keyword-only: in
        :meth:`process_message` the fourth positional slot is ``options``, so a
        positional call written by analogy would otherwise hand the
        ``WebhookOptions`` to handlers as ``previous_message`` and skip
        ``wait_until``. Returns the handler task (``None`` without a running loop); it raises
        on handler failure, while ``wait_until`` always receives the
        error-swallowing wrapper (upstream ``processMessageUpdated``).
        """

        async def _task() -> None:
            msg = await message() if callable(message) else message
            prev = await previous_message() if callable(previous_message) else previous_message
            await self._handle_message_updated(adapter, thread_id, msg, prev)

        task = _create_task(_task(), self._active_tasks)
        if task is not None:
            task.add_done_callback(
                lambda t: (
                    self._logger.error(
                        "Message update processing error", {"thread_id": thread_id, "error": str(t.exception())}
                    )
                    if not t.cancelled() and t.exception()
                    else None
                )
            )
            self._hand_to_wait_until(task, options)
        return task

    def process_message_deleted(
        self,
        event: MessageDeletedEvent,
        options: WebhookOptions | None = None,
    ) -> asyncio.Task[None] | None:
        """Process an incoming message delete from an adapter.

        Handlers receive the event with ``platform`` filled from the adapter
        name when the adapter left it ``None``. No Thread is constructed.
        Returns the handler task (``None`` without a running loop); see
        :meth:`process_message_updated` for error and ``wait_until`` handling.
        """
        task = _create_task(self._handle_message_deleted(event), self._active_tasks)
        if task is not None:
            task.add_done_callback(
                lambda t: (
                    self._logger.error(
                        "Message delete processing error",
                        {"thread_id": event.thread_id, "message_id": event.message_id, "error": str(t.exception())},
                    )
                    if not t.cancelled() and t.exception()
                    else None
                )
            )
            self._hand_to_wait_until(task, options)
        return task

    def process_reaction(
        self,
        event: ReactionEvent,
        options: WebhookOptions | None = None,
    ) -> asyncio.Task[None] | None:
        """Process an incoming reaction event.

        Returns the handler task (``None`` without a running loop); it raises
        on handler failure. ``wait_until`` always receives the error-swallowing
        wrapper (vercel/chat#942).
        """

        async def _task() -> None:
            # Enter the conversation inside the task: create_task copies the
            # context at creation, so setting it here keeps it task-local.
            with conversation(event.thread_id):
                await self._handle_reaction_event(event)

        task = _create_task(_task(), self._active_tasks)
        if task is not None:
            task.add_done_callback(
                lambda t: (
                    self._logger.error("Reaction processing error", {"error": str(t.exception())})
                    if not t.cancelled() and t.exception()
                    else None
                )
            )
            self._hand_to_wait_until(task, options)
        return task

    def process_action(
        self,
        event: ActionEvent,
        options: WebhookOptions | None = None,
    ) -> asyncio.Task[None] | None:
        """Process an incoming action event (button click).

        Returns the handler task (``None`` without a running loop); it raises
        on handler failure. See :meth:`process_message` for ``wait_until``.
        """

        async def _task() -> None:
            with conversation(event.thread_id):
                await self._handle_action_event(event)

        task = _create_task(_task(), self._active_tasks)
        if task is not None:
            task.add_done_callback(
                lambda t: (
                    self._logger.error(
                        "Action processing error",
                        {"error": str(t.exception()), "action_id": event.action_id, "message_id": event.message_id},
                    )
                    if not t.cancelled() and t.exception()
                    else None
                )
            )
            self._hand_to_wait_until(task, options, propagate=True)
        return task

    async def process_options_load(
        self,
        event: OptionsLoadEvent,
        options: WebhookOptions | None = None,  # noqa: ARG002 (match upstream signature)
    ) -> OptionsLoadResult | None:
        """Process an options-load event (external-select suggestion lookup).

        Runs specific-action-ID handlers before catch-all handlers and returns
        the first handler result that isn't ``None`` — including an explicit
        ``[]``, which short-circuits subsequent handlers (handler says "I
        handled this action, show no options"). Errors are logged and skipped
        so later handlers still get a chance. Mirrors upstream
        ``processOptionsLoad`` (TS ``if (options) { return options; }``, where
        ``[]`` is truthy and therefore short-circuits).
        """
        matching_handlers = [
            pat for pat in self._options_load_handlers if pat.action_ids and event.action_id in pat.action_ids
        ] + [pat for pat in self._options_load_handlers if not pat.action_ids]

        for pat in matching_handlers:
            try:
                result = await self._invoke_handler(pat.handler, event)
                if result is not None:
                    return result
            except Exception as exc:
                self._logger.error(
                    "Options load handler error",
                    {"action_id": event.action_id, "error": str(exc)},
                )
        return None

    async def process_modal_submit(
        self,
        event: ModalSubmitEvent,
        context_id: str | None = None,
        options: WebhookOptions | None = None,
    ) -> ModalResponse | None:
        """Process a modal form submission. Returns optional response."""
        related = await self._retrieve_modal_context(event.adapter, context_id)
        callback_url = related.get("callback_url")

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

        result: ModalResponse | None = None
        with conversation(_modal_conversation_id(full_event)):
            for pat in self._modal_submit_handlers:
                if not pat.callback_ids or event.callback_id in pat.callback_ids:
                    try:
                        response = await self._invoke_handler(pat.handler, full_event)
                        if response is not None:
                            result = response
                            break
                    except Exception as exc:
                        self._logger.error(
                            "Modal submit handler error",
                            {"callback_id": event.callback_id, "error": str(exc)},
                        )

        if callback_url and getattr(result, "action", None) != "errors":
            # POST the form values to the modal's callback URL. The response
            # is returned to the platform without waiting for the POST; the
            # task is handed to options.wait_until for serverless callers.
            payload = {
                "type": "modal_submit",
                "callbackId": event.callback_id,
                "values": event.values,
                "user": {"id": event.user.user_id, "name": event.user.user_name},
            }

            async def _post_modal_callback() -> None:
                try:
                    post_result = await post_to_callback_url(callback_url, payload)
                    if post_result.error is not None:
                        self._logger.error(
                            "Modal callbackUrl POST failed",
                            {"callback_url": callback_url, "error": str(post_result.error)},
                        )
                except Exception as error:  # mirrors upstream's trailing .catch
                    self._logger.error(
                        "Modal callbackUrl POST failed",
                        {"callback_url": callback_url, "error": str(error)},
                    )

            task = _create_task(_post_modal_callback(), self._active_tasks)
            if task is not None and options and options.wait_until:
                options.wait_until(task)

        return result

    def process_modal_close(
        self,
        event: ModalCloseEvent,
        context_id: str | None = None,
        options: WebhookOptions | None = None,
    ) -> None:
        """Process a modal close event."""

        async def _task() -> None:
            related = await self._retrieve_modal_context(event.adapter, context_id)

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

            with conversation(_modal_conversation_id(full_event)):
                for pat in self._modal_close_handlers:
                    if not pat.callback_ids or event.callback_id in pat.callback_ids:
                        await self._invoke_handler(pat.handler, full_event)

        task = _create_task(_task(), self._active_tasks)
        if task is not None:
            task.add_done_callback(
                lambda t: (
                    self._logger.error("Modal close handler error", {"error": str(t.exception())})
                    if not t.cancelled() and t.exception()
                    else None
                )
            )
            self._hand_to_wait_until(task, options)

    def process_slash_command(
        self,
        event: SlashCommandEvent,
        options: WebhookOptions | None = None,
    ) -> asyncio.Task[None] | None:
        """Process a slash command event.

        Returns the handler task (``None`` without a running loop); it raises
        on handler failure. See :meth:`process_message` for ``wait_until``.
        """
        task = _create_task(self._handle_slash_command_event(event), self._active_tasks)
        if task is not None:
            task.add_done_callback(
                lambda t: (
                    self._logger.error(
                        "Slash command processing error",
                        {"error": str(t.exception()), "command": event.command, "text": event.text},
                    )
                    if not t.cancelled() and t.exception()
                    else None
                )
            )
            self._hand_to_wait_until(task, options, propagate=True)
        return task

    def process_assistant_thread_started(
        self,
        event: AssistantThreadStartedEvent,
        options: WebhookOptions | None = None,
    ) -> None:
        async def _task() -> None:
            with conversation(event.thread_id):
                for h in self._assistant_thread_started_handlers:
                    await self._invoke_handler(h, event)

        task = _create_task(_task(), self._active_tasks)
        if task is not None:
            task.add_done_callback(
                lambda t: (
                    self._logger.error("Assistant thread started handler error", {"error": str(t.exception())})
                    if not t.cancelled() and t.exception()
                    else None
                )
            )
            self._hand_to_wait_until(task, options)

    def process_agent_session_stopped(
        self,
        event: AgentSessionStoppedEvent,
        options: WebhookOptions | None = None,
    ) -> None:
        """Dispatch an agent-session stop (upstream ``processAgentSessionStopped``).

        Takes no lock: the turn being stopped holds the thread lock.
        """
        self._run_agent_session_handlers(
            "Agent session stopped handler error", self._agent_session_stopped_handlers, event, options
        )

    def process_agent_session_title_changed(
        self,
        event: AgentSessionTitleChangedEvent,
        options: WebhookOptions | None = None,
    ) -> None:
        """Dispatch an agent-session title change (upstream ``processAgentSessionTitleChanged``)."""
        self._run_agent_session_handlers(
            "Agent session title changed handler error", self._agent_session_title_changed_handlers, event, options
        )

    def _run_agent_session_handlers(
        self,
        error_message: str,
        handlers: list[Any],
        event: AgentSessionStoppedEvent | AgentSessionTitleChangedEvent,
        options: WebhookOptions | None,
    ) -> None:
        async def _task() -> None:
            with conversation(event.thread_id):
                for h in handlers:
                    await self._invoke_handler(h, event)

        task = _create_task(_task(), self._active_tasks)
        if task is not None:
            task.add_done_callback(
                lambda t: (
                    self._logger.error(error_message, {"error": t.exception(), "thread_id": event.thread_id})
                    if not t.cancelled() and t.exception()
                    else None
                )
            )
            self._hand_to_wait_until(task, options)

    async def abort_turn(self, thread_id: str) -> None:
        """Abort the active turn for ``thread_id`` (upstream ``abortTurn``).

        Aborts ``thread.signal`` for turns running in this process. When a
        turn running elsewhere published itself (its adapter sets
        ``supports_turn_cancellation``), the abort is recorded in the shared
        state backend and that process aborts its turn on its next poll.
        """
        local = self._active_turn_signals.get(thread_id)
        if local:
            for signal in list(local.values()):
                signal._abort()

        active_turn_id = await self._state_adapter.get(self._active_turn_key(thread_id))
        if active_turn_id:
            await self._state_adapter.set(self._abort_turn_key(thread_id), active_turn_id, ACTIVE_TURN_TTL_MS)

    def process_assistant_context_changed(
        self,
        event: AssistantContextChangedEvent,
        options: WebhookOptions | None = None,
    ) -> None:
        async def _task() -> None:
            with conversation(event.thread_id):
                for h in self._assistant_context_changed_handlers:
                    await self._invoke_handler(h, event)

        task = _create_task(_task(), self._active_tasks)
        if task is not None:
            task.add_done_callback(
                lambda t: (
                    self._logger.error("Assistant context changed handler error", {"error": str(t.exception())})
                    if not t.cancelled() and t.exception()
                    else None
                )
            )
            self._hand_to_wait_until(task, options)

    def process_app_home_opened(
        self,
        event: AppHomeOpenedEvent,
        options: WebhookOptions | None = None,
    ) -> None:
        async def _task() -> None:
            # Upstream parity (chat.ts processAppHomeOpened /
            # processMemberJoinedChannel): the adapter's event channel id is
            # used as-is (Slack reports App Home's raw ``D…`` id and
            # member-joined as ``slack:C…:``, upstream too).
            with conversation(event.channel_id):
                for h in self._app_home_opened_handlers:
                    await self._invoke_handler(h, event)

        task = _create_task(_task(), self._active_tasks)
        if task is not None:
            task.add_done_callback(
                lambda t: (
                    self._logger.error("App home opened handler error", {"error": str(t.exception())})
                    if not t.cancelled() and t.exception()
                    else None
                )
            )
            self._hand_to_wait_until(task, options)

    def process_app_context_changed(
        self,
        event: AppContextChangedEvent,
        options: WebhookOptions | None = None,
    ) -> None:
        """Dispatch a Slack agent_view ``app_context_changed`` event (upstream ``processAppContextChanged``)."""

        async def _task() -> None:
            with conversation(event.channel_id):
                for h in self._app_context_changed_handlers:
                    await self._invoke_handler(h, event)

        task = _create_task(_task(), self._active_tasks)
        if task is not None:
            task.add_done_callback(
                lambda t: (
                    self._logger.error(
                        "App context changed handler error", {"error": str(t.exception()), "user_id": event.user_id}
                    )
                    if not t.cancelled() and t.exception()
                    else None
                )
            )
            self._hand_to_wait_until(task, options)

    def process_member_joined_channel(
        self,
        event: MemberJoinedChannelEvent,
        options: WebhookOptions | None = None,
    ) -> None:
        async def _task() -> None:
            with conversation(event.channel_id):
                for h in self._member_joined_channel_handlers:
                    await self._invoke_handler(h, event)

        task = _create_task(_task(), self._active_tasks)
        if task is not None:
            task.add_done_callback(
                lambda t: (
                    self._logger.error("Member joined channel handler error", {"error": str(t.exception())})
                    if not t.cancelled() and t.exception()
                    else None
                )
            )
            self._hand_to_wait_until(task, options)

    def process_installed(self, event: InstalledEvent, options: WebhookOptions | None = None) -> None:
        """Dispatch a bot-installed event (upstream ``processInstalled``)."""
        self._run_installation_handlers("Installed", self._installed_handlers, event, options)

    def process_uninstalled(self, event: UninstalledEvent, options: WebhookOptions | None = None) -> None:
        """Dispatch a bot-uninstalled event (upstream ``processUninstalled``)."""
        self._run_installation_handlers("Uninstalled", self._uninstalled_handlers, event, options)

    def _run_installation_handlers(
        self,
        kind: Literal["Installed", "Uninstalled"],
        handlers: list[Any],
        event: InstallationEvent,
        options: WebhookOptions | None,
    ) -> None:
        """Run installation handlers in order under the destination conversation.

        Upstream ``runInstallationHandlers``: no task without handlers; a
        handler error is logged (and, as upstream, stops the handlers after
        it), so the task handed to ``wait_until`` always completes normally.
        A ``None`` ``channel_id`` runs the handlers with no conversation set.
        """
        if not handlers:
            return

        async def _task() -> None:
            try:
                with conversation(event.channel_id):
                    for h in handlers:
                        await self._invoke_handler(h, event)
            except Exception as error:
                self._logger.error(
                    f"{kind} handler error",
                    {"error": error, "conversation_id": event.conversation_id, "activity_id": event.id},
                )

        task = _create_task(_task(), self._active_tasks)
        if task is not None:
            # Python-specific: hand over the shielded wrapper, like the other
            # lifecycle dispatchers, so a host cancelling its wait_until task
            # cannot cancel the handlers (a JS promise has no cancellation).
            self._hand_to_wait_until(task, options)

    # ========================================================================
    # Slash command handling
    # ========================================================================

    async def _handle_slash_command_event(self, event: SlashCommandEvent) -> None:
        self._logger.debug(
            "Incoming slash command",
            {
                "adapter": event.adapter.name,
                "command": event.command,
                # Divergence from upstream — see docs/UPSTREAM_SYNC.md: the
                # command text's length, not its content.
                "textLength": len(event.text or ""),
                "user": event.user.user_name,
            },
        )

        if event.user.is_me:
            self._logger.debug("Skipping slash command from self")
            return

        # Create channel for the command
        channel_id = getattr(event, "channel_id", None) or (event.channel.id if event.channel else "")
        channel = ChannelImpl(
            _ChannelImplConfigWithAdapter(
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
            await self._store_modal_context(
                event.adapter.name,
                context_id,
                channel=channel,
                callback_url=modal.get("callback_url") if isinstance(modal, dict) else None,
            )
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

        # Run handlers with the command's channel as the active conversation
        # (upstream handleSlashCommandEvent -> runInConversation(channelId)).
        with conversation(channel_id):
            for pat in self._slash_command_handlers:
                if not pat.commands:
                    self._logger.debug("Running catch-all slash command handler")
                    await self._invoke_handler(pat.handler, full_event)
                    continue
                if event.command in pat.commands:
                    self._logger.debug("Running matched slash command handler", {"command": event.command})
                    await self._invoke_handler(pat.handler, full_event)

    # ========================================================================
    # Modal context persistence
    # ========================================================================

    async def _store_modal_context(
        self,
        adapter_name: str,
        context_id: str,
        thread: ThreadImpl | None = None,
        message: Message | None = None,
        channel: Channel | None = None,
        callback_url: str | None = None,
    ) -> None:
        # Upstream ``storeModalContext`` is ``async`` and the ``openModal``
        # helper *awaits* it before calling ``adapter.openModal`` (chat.ts
        # :1280/:1342/:1554). We mirror that: the state write must land
        # before the modal can be submitted, otherwise with a remote
        # (Redis/Postgres) backend a fast submit can race ahead of a
        # fire-and-forget write and miss the stored callbackUrl/channel.
        key = f"modal-context:{adapter_name}:{context_id}"
        context = {
            "thread": thread.to_json() if thread else None,
            "message": message.to_json() if message else None,
            "channel": channel.to_json() if channel else None,
            # camelCase: stored state is a serialization boundary shared
            # with the TS SDK (matches the serialized thread/message keys).
            "callbackUrl": callback_url,
        }
        await self._state_adapter.set(key, context, MODAL_CONTEXT_TTL_MS)

    async def _retrieve_modal_context(
        self,
        adapter: Adapter,
        context_id: str | None,
    ) -> dict[str, Any]:
        adapter_name = adapter.name
        if not context_id:
            return {
                "callback_url": None,
                "related_thread": None,
                "related_message": None,
                "related_channel": None,
            }

        key = f"modal-context:{adapter_name}:{context_id}"
        stored = await self._state_adapter.get(key)

        if not stored:
            return {
                "callback_url": None,
                "related_thread": None,
                "related_message": None,
                "related_channel": None,
            }

        # Bind restored objects to this Chat (vercel/chat#967). Upstream looks
        # the adapter up by `adapter.name` and always passes `this`; we bind
        # the event's own adapter instead, so an adapter registered under a
        # custom key still restores into this Chat (an explicit `chat`
        # resolves eagerly here, so a by-name miss would raise). Never fall
        # back to the active singleton: it may be another bot's Chat. An
        # adapter this Chat did not register is matched only against the
        # Chat that owns that exact instance. Divergence from upstream
        # (eager ownership) — see docs/UPSTREAM_SYNC.md.
        owner = self if self.owns_adapter(adapter) else None

        related_thread = None
        if stored.get("thread"):
            related_thread = ThreadImpl.from_json(stored["thread"], adapter, chat=owner)

        related_message = None
        if stored.get("message") and related_thread is not None:
            msg = _message_from_json(stored["message"])
            related_message = related_thread.create_sent_message_from_message(msg)

        related_channel = None
        if stored.get("channel"):
            related_channel = ChannelImpl.from_json(stored["channel"], adapter, chat=owner)

        # Accept both camelCase (written by this SDK and the TS SDK) and
        # snake_case (hand-written state) — same tolerance as from_json.
        callback_url = stored["callbackUrl"] if "callbackUrl" in stored else stored.get("callback_url")

        return {
            "callback_url": callback_url,
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

        # Decode a callback token (`__cb:<token>`) planted at post time by
        # process_card_callback_urls. When one resolves, handlers see the
        # button's original value and the action payload is POSTed to the
        # stored callback URL concurrently with the handlers. A token only
        # resolves for the button and conversation that minted it, and only
        # once (vercel/chat#875); otherwise handlers see the raw value and
        # nothing is POSTed.
        callback_token = decode_callback_value(event.value).callback_token

        resolved = None
        if callback_token:
            channel_id = event.adapter.channel_id_from_thread_id(event.thread_id) if event.thread_id else None
            resolved = await resolve_callback_url(
                callback_token,
                self._state_adapter,
                CallbackContext(
                    action_id=event.action_id,
                    channel_id=channel_id,
                    thread_id=event.thread_id,
                ),
            )

        action_value = resolved.original_value if resolved is not None else event.value

        callback_url_task: asyncio.Task[Any] | None = None
        if resolved is not None:
            callback_url = resolved.url
            # Wire payload: camelCase keys, optional keys omitted (not None)
            # to mirror upstream's JSON.stringify semantics — hazard #7.
            # `actionId` is the minted button's id, as stored with the token.
            payload: dict[str, Any] = {"type": "action", "actionId": resolved.action_id}
            if resolved.original_value is not None:
                payload["value"] = resolved.original_value
            payload["user"] = {"id": event.user.user_id, "name": event.user.user_name}
            if event.thread_id is not None:
                payload["threadId"] = event.thread_id
            if event.message_id is not None:
                payload["messageId"] = event.message_id

            async def _post_action_callback() -> None:
                post_result = await post_to_callback_url(callback_url, payload)
                if post_result.error is not None:
                    self._logger.error(
                        "Button callbackUrl POST failed",
                        {
                            "callback_url": callback_url,
                            "action_id": event.action_id,
                            "error": str(post_result.error),
                        },
                    )

            callback_url_task = _create_task(_post_action_callback(), self._active_tasks)

        thread: ThreadImpl | None = None
        if event.thread_id:
            is_subscribed = False
            # Without a message id there is no message context: the thread
            # gets `current_message=None` rather than a stub (vercel/chat#633).
            # Upstream parity (chat.ts processAction, chat@4.41.1). Such a
            # thread has no recipient context, so Slack's `stream()` returns
            # None and core's post+edit fallback replies (the adapter half of
            # the same change; the rest of it is #207).
            message_for_thread = (
                Message(
                    id=event.message_id,
                    thread_id=event.thread_id,
                    text="",
                    formatted={"type": "root", "children": []},
                    raw=event.raw,
                    author=event.user,
                    metadata=MessageMetadata(date_sent=datetime.now(tz=timezone.utc), edited=False),
                    attachments=[],
                )
                if event.message_id
                else None
            )
            thread = self._create_thread(event.adapter, event.thread_id, message_for_thread, is_subscribed)

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
                                    MessageMetadata(date_sent=datetime.now(tz=timezone.utc), edited=False),
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
            await self._store_modal_context(
                event.adapter.name,
                context_id,
                thread=thread,
                message=fetched_message,
                channel=channel_impl,
                callback_url=modal.get("callback_url") if isinstance(modal, dict) else None,
            )
            return await event.adapter.open_modal(trigger_id, modal, context_id)  # type: ignore[union-attr]

        full_event = ActionEvent(
            adapter=event.adapter,
            thread=thread,
            thread_id=event.thread_id,
            message_id=event.message_id,
            user=event.user,
            action_id=event.action_id,
            value=action_value,
            trigger_id=event.trigger_id,
            raw=event.raw,
            _open_modal=_open_modal,
        )

        for pat in self._action_handlers:
            if not pat.action_ids:
                self._logger.debug("Running catch-all action handler")
                await self._invoke_handler(pat.handler, full_event)
                continue
            if event.action_id in pat.action_ids:
                self._logger.debug("Running matched action handler", {"action_id": event.action_id})
                await self._invoke_handler(pat.handler, full_event)

        if callback_url_task is not None:
            await callback_url_task

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
        # A reaction without the reacted-to message has no message context
        # (vercel/chat#633).
        thread = self._create_thread(
            event.adapter,
            event.thread_id,
            event.message,
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
                await self._invoke_handler(pat.handler, full_event)
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
                await self._invoke_handler(pat.handler, full_event)

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
        return self._create_thread(adapter, thread_id, None, False)

    async def get_user(self, user: str | Author) -> UserInfo | None:
        """Look up user information by user ID.

        The adapter is automatically inferred from the user ID format
        (Slack ``U.../W...``, Teams ``29:...``, Google Chat ``users/...``,
        Linear UUID, or numeric for Discord/Telegram/GitHub — disambiguated
        by which adapters are registered).

        Returns user details including ``email`` and ``avatar_url`` when
        available — both require appropriate scopes on some platforms (for
        example ``users:read.email`` on Slack).

        Parameters
        ----------
        user:
            Platform-specific user ID string, or an :class:`Author` object.

        Returns
        -------
        :class:`UserInfo` or ``None``
            ``None`` is returned when the user is not found.

        Raises
        ------
        :class:`~chat_sdk.errors.ChatError`
            * ``Cannot infer adapter from userId "..."`` — the user ID does
              not match any of the supported platform formats, or the
              inferred adapter is not registered on this Chat instance.
            * ``Numeric userId "..." is ambiguous between adapters: ...`` —
              multiple registered adapters could resolve a numeric ID; call
              the platform adapter's ``get_user`` directly instead.
            * ``Adapter "<name>" does not support get_user`` — the resolved
              adapter does not implement user lookup (e.g. WhatsApp).

        Mirrors ``chat.getUser`` from the upstream TS SDK
        (``vercel/chat#391``).

        Examples
        --------
        ::

            user = await chat.get_user("U123456")
            print(user.email if user else "<not found>")
        """
        user_id = user if isinstance(user, str) else user.user_id
        adapter = self._infer_adapter_from_user_id(user_id)
        # Legacy adapters built before the chat.get_user port may not
        # define ``get_user`` at all — calling on them would raise
        # ``AttributeError`` and break the SDK's error contract. Translate
        # both the missing-method (legacy) and explicitly-raised
        # ``ChatNotImplementedError`` (modern) cases to the same
        # ``ChatError`` so callers can rely on a single failure mode.
        get_user_method = getattr(adapter, "get_user", None)
        if get_user_method is None:
            raise ChatError(f'Adapter "{adapter.name}" does not support get_user')
        try:
            return await get_user_method(user_id)
        except ChatNotImplementedError as exc:
            raise ChatError(f'Adapter "{adapter.name}" does not support get_user') from exc

    def channel(self, channel_id: str) -> ChannelImpl:
        """Get a Channel by its channel ID (e.g. 'slack:C123ABC')."""
        adapter_name = channel_id.split(":")[0] if ":" in channel_id else ""
        if not adapter_name:
            raise ChatError(f"Invalid channel ID: {channel_id}")
        adapter = self._adapters.get(adapter_name)
        if adapter is None:
            registered = sorted(self._adapters.keys())
            raise ChatError(
                f'Adapter "{adapter_name}" not found for channel ID "{channel_id}" (registered adapters: {registered})'
            )
        return ChannelImpl(
            _ChannelImplConfigWithAdapter(
                id=channel_id,
                adapter=adapter,
                state_adapter=self._state_adapter,
            )
        )

    def thread(
        self,
        thread_id: str,
        *,
        current_message: Message | None = None,
    ) -> ThreadImpl:
        """Get a Thread by its thread ID (e.g. 'slack:C123ABC:1234567890.123456').

        The adapter is resolved from the thread ID prefix; state and message
        history come from this Chat instance. This is the public
        construction path for worker processes — use it instead of
        reaching into ``ThreadImpl`` / ``_ThreadImplConfig`` directly.

        Parameters
        ----------
        thread_id:
            Fully-qualified thread ID. Must start with a registered
            adapter name followed by ``:``.
        current_message:
            Optional reference to the message the worker is responding to.
            Required for Slack native streaming, which populates
            ``recipient_user_id`` / ``recipient_team_id`` from the author
            of this message. Omit for post-only worker flows.

        Mirrors ``chat.thread(threadId)`` from the upstream TS SDK.
        """
        # Validate thread ID shape structurally, before calling the adapter.
        # A valid ID is `{adapter}:{channel}[:{rest}...]` with:
        #   - non-empty adapter prefix
        #   - non-empty **channel** segment (the part between the first
        #     and second colon, if there's a second colon; or the whole
        #     remainder if not)
        # Rejects `"slack:"`, `"slack::"`, `"slack::thread"`, etc. at
        # the SDK boundary so we don't rely on each adapter's
        # `channel_id_from_thread_id` doing its own defense. (Different
        # adapters return different things for malformed input; we need
        # a single source of truth.)
        #
        # Checking just "any segment is non-empty" would wrongly accept
        # `"slack::thread"` (empty channel, non-empty thread).
        adapter_name, sep, remainder = thread_id.partition(":")
        if not sep or not adapter_name or not remainder:
            raise ChatError(f"Invalid thread ID: {thread_id}")
        channel_segment, _, _ = remainder.partition(":")
        if not channel_segment:
            raise ChatError(f"Invalid thread ID: {thread_id}")

        adapter = self._adapters.get(adapter_name)
        if adapter is None:
            registered = sorted(self._adapters.keys())
            raise ChatError(
                f'Adapter "{adapter_name}" not found for thread ID "{thread_id}" (registered adapters: {registered})'
            )

        # Defer to the adapter to derive channel_id as a secondary sanity
        # check — some platform-specific patterns can pass the structural
        # check but still be malformed (e.g. wrong number of segments).
        try:
            derived_channel_id = adapter.channel_id_from_thread_id(thread_id)
        except Exception as exc:
            raise ChatError(f"Invalid thread ID: {thread_id}") from exc
        if not derived_channel_id:
            raise ChatError(f"Invalid thread ID: {thread_id}")

        # Without `current_message` the handle has no message context:
        # `current_message` is None and `recent_messages` is empty
        # (vercel/chat#633).
        return self._create_thread(adapter, thread_id, current_message, False)

    # ========================================================================
    # Adapter inference
    # ========================================================================

    def _infer_adapter_from_user_id(self, user_id: str) -> Adapter:
        # ── Unique-prefix formats — no collision possible across adapters ──

        # Google Chat: "users/123456789"
        if user_id.startswith("users/"):
            adapter = self._adapters.get("gchat")
            if adapter:
                return adapter

        # Teams: "29:base64string..."
        if user_id.startswith("29:"):
            adapter = self._adapters.get("teams")
            if adapter:
                return adapter

        # Linear: UUID v4 (e.g. "8f1f3c7e-d4e1-4f9a-bf2b-1c3d4e5f6a7b")
        if LINEAR_UUID_REGEX.match(user_id):
            adapter = self._adapters.get("linear")
            if adapter:
                return adapter

        # Slack: "U..." or "W..." (uppercase only, alphanumeric, 7+ chars).
        # Case-sensitive on purpose — lowercase strings like "user123" are
        # GitHub logins, not Slack IDs.
        if SLACK_USER_ID_REGEX.match(user_id):
            adapter = self._adapters.get("slack")
            if adapter:
                return adapter

        # Numeric IDs: shared by Discord (17-19 digit snowflakes), Telegram
        # (positive integer up to 52 bits), and GitHub (numeric account_id).
        # Disambiguate by which adapters the caller actually registered.
        if NUMERIC_REGEX.match(user_id):
            candidates: list[str] = []
            is_discord_snowflake = bool(DISCORD_SNOWFLAKE_REGEX.match(user_id))
            discord_registered = "discord" in self._adapters
            if is_discord_snowflake and discord_registered:
                candidates.append("discord")
            # Telegram and GitHub numeric IDs are shorter than Discord
            # snowflakes (17-19 digits) in practice. When the id falls in
            # the snowflake range AND Discord is registered, exclude
            # Telegram and GitHub from candidates so Discord routes
            # deterministically. Without this guard, Discord+Telegram
            # deployments raise ambiguity on every Discord user lookup
            # — see PR #90 Codex review (discussion_r3284323050). When
            # Discord is not registered, an 18-digit id can legitimately
            # be a Telegram or GitHub id, so keep them as candidates to
            # avoid over-restricting single-adapter deployments.
            if not (is_discord_snowflake and discord_registered):
                if "telegram" in self._adapters:
                    candidates.append("telegram")
                if "github" in self._adapters:
                    candidates.append("github")

            if len(candidates) == 1:
                adapter = self._adapters.get(candidates[0])
                if adapter:
                    return adapter
            if len(candidates) > 1:
                raise ChatError(
                    f'Numeric userId "{user_id}" is ambiguous between adapters: '
                    f"{', '.join(candidates)}. Call the platform's adapter "
                    "directly (e.g. `adapter.get_user(user_id)`)."
                )

        raise ChatError(
            f'Cannot infer adapter from userId "{user_id}". '
            'Expected: Slack ("U..."), Teams ("29:..."), Google Chat ("users/..."), '
            "Linear (UUID), or Discord/Telegram/GitHub (numeric)."
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
            # The public contract lets callers return either `LockScope` (sync)
            # or `Awaitable[LockScope]`. `inspect.isawaitable` narrows so we
            # only `await` the coroutine/future branch — doing so
            # unconditionally would raise `TypeError` on a sync return.
            result = self._lock_scope_config(
                LockScopeContext(
                    adapter=adapter,
                    channel_id=channel_id,
                    is_dm=is_dm,
                    thread_id=thread_id,
                )
            )
            scope = await result if inspect.isawaitable(result) else result
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

        Handles deduplication, bot filtering, concurrency, and dispatch. Runs
        with ``thread_id`` as the active conversation (upstream
        ``handleIncomingMessage`` -> ``runInConversation``).
        """
        with conversation(thread_id):
            await self._route_incoming_message(adapter, thread_id, message)

    async def _route_incoming_message(
        self,
        adapter: Adapter,
        thread_id: str,
        message: Message,
        deduplicate: bool = True,
    ) -> None:
        """Self-filter, then (unless ``deduplicate`` is False) dedupe, then dispatch."""
        self._logger.debug(
            "Incoming message",
            {
                "adapter": adapter.name,
                "thread_id": thread_id,
                "message_id": message.id,
                "is_bot": message.author.is_bot,
                "is_me": message.author.is_me,
            },
        )

        # Skip self messages
        if message.author.is_me:
            self._logger.debug("Skipping message from self (is_me=True)")
            return

        if not deduplicate:
            await self._dispatch_incoming_message(adapter, thread_id, message)
            return

        # Deduplicate
        dedupe_key = f"dedupe:{adapter.name}:{message.id}"
        is_first = await self._state_adapter.set_if_not_exists(dedupe_key, True, self._dedupe_ttl_ms)
        if not is_first:
            self._logger.debug("Skipping duplicate message", {"message_id": message.id})
            return

        await self._dispatch_incoming_message(adapter, thread_id, message)

    async def _dispatch_incoming_message(
        self,
        adapter: Adapter,
        thread_id: str,
        message: Message,
    ) -> None:
        """Persist history, resolve the lock key, and run the concurrency strategy."""
        # Persist incoming message before acquiring lock.
        # `persist_message_history` is the deprecated adapter flag; either
        # being truthy enables persistence (mirrors upstream
        # `adapter.persistThreadHistory || adapter.persistMessageHistory`).
        # Both flags are optional on the adapter, hence getattr.
        if getattr(adapter, "persist_thread_history", None) or getattr(adapter, "persist_message_history", None):
            channel_id = adapter.channel_id_from_thread_id(thread_id)
            appends = [self._thread_history.append(thread_id, message)]
            if channel_id != thread_id:
                appends.append(self._thread_history.append(channel_id, message))
            await asyncio.gather(*appends)

        # Resolve lock key
        lock_key = await self._get_lock_key(adapter, thread_id)

        strategy = self._concurrency_strategy

        if strategy == "concurrent":
            await self._handle_concurrent(adapter, thread_id, message)
            return

        if strategy in ("queue", "debounce", "burst"):
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

        async def _run(_heartbeat: _LockHeartbeat) -> None:
            await self._dispatch_to_handlers(adapter, thread_id, message)

        await self._with_held_lock(lock, thread_id, lock_key, _run)

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
            # Lock busy -- enqueue. Debounce shares the queue capacity
            # (upstream #659): superseded messages are kept so the handler
            # sees them in ``context.skipped``.
            effective_max = max_queue_size
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

            now = _now_ms()
            entry = QueueEntry(
                message=message,
                enqueued_at=now,
                expires_at=now + queue_entry_ttl_ms,
            )
            await self._state_adapter.enqueue(lock_key, entry, effective_max)
            self._logger.info(
                "message-debounce-reset" if strategy == "debounce" else "message-queued",
                {
                    "thread_id": thread_id,
                    "lock_key": lock_key,
                    "message_id": message.id,
                    "queue_depth": min(depth + 1, effective_max),
                },
            )
            return

        # We hold the lock
        self._logger.debug("Lock acquired", {"thread_id": thread_id, "lock_key": lock_key, "token": lock.token})

        async def _run(heartbeat: _LockHeartbeat) -> None:
            if strategy == "debounce":
                # Debounce: enqueue our own message and enter the debounce loop
                now = _now_ms()
                await self._state_adapter.enqueue(
                    lock_key,
                    QueueEntry(message=message, enqueued_at=now, expires_at=now + queue_entry_ttl_ms),
                    max_queue_size,
                )
                self._logger.info(
                    "message-debouncing",
                    {
                        "thread_id": thread_id,
                        "lock_key": lock_key,
                        "message_id": message.id,
                        "debounce_ms": debounce_ms,
                    },
                )
                await self._debounce_loop(heartbeat, adapter, lock_key)
            elif strategy == "burst":
                # Burst: enqueue the first message, sleep `debounce_ms` so any
                # messages arriving during the window are queued alongside it,
                # then drain like ``queue`` -- the latest message is dispatched
                # with the earlier ones in ``context.skipped``. The heartbeat
                # keeps the lock alive across the window.
                now = _now_ms()
                await self._state_adapter.enqueue(
                    lock_key,
                    QueueEntry(message=message, enqueued_at=now, expires_at=now + queue_entry_ttl_ms),
                    max_queue_size,
                )
                self._logger.info(
                    "message-debouncing",
                    {
                        "thread_id": thread_id,
                        "lock_key": lock_key,
                        "message_id": message.id,
                        "debounce_ms": debounce_ms,
                    },
                )
                await _sleep(debounce_ms)
                await self._drain_queue(heartbeat, adapter, lock_key)
            else:
                # Queue: process our message immediately, then drain any
                # queued messages.
                await self._dispatch_to_handlers(adapter, thread_id, message)
                await self._drain_queue(heartbeat, adapter, lock_key)

        await self._with_held_lock(lock, thread_id, lock_key, _run)

    # -- Held-lock lifecycle -------------------------------------------------

    async def _with_held_lock(
        self,
        lock: Lock,
        thread_id: str,
        lock_key: str,
        fn: Callable[[_LockHeartbeat], Awaitable[None]],
    ) -> None:
        """Run ``fn`` while a heartbeat keeps ``lock`` alive, then stop it and release.

        Every lock-holding path goes through here so acquiring a lock is
        never paired with a missing heartbeat. ``stop()`` is awaited before
        ``release_lock`` so an in-flight extend never lands after the release.
        """
        heartbeat = _LockHeartbeat(self._state_adapter, lock, self._concurrency_max_lock_lifetime_ms, self._logger)
        try:
            await fn(heartbeat)
        finally:
            try:
                await heartbeat.stop()
            finally:
                await self._state_adapter.release_lock(lock)
                self._logger.debug("Lock released", {"thread_id": thread_id, "lock_key": lock_key})

    # -- Pending-queue collection --------------------------------------------

    async def _take_pending(self, adapter: Adapter, lock_key: str) -> list[Message]:
        """Dequeue every entry for ``lock_key``, rehydrated, dropping expired ones.

        The collection loop shared by upstream ``debounceLoop`` and ``drainQueue``.
        """
        pending: list[Message] = []
        while True:
            entry = await self._state_adapter.dequeue(lock_key)
            if entry is None:
                return pending
            msg = self._rehydrate_message(entry.message, adapter)
            if _now_ms() <= entry.expires_at:
                pending.append(msg)
            else:
                self._logger.info(
                    "message-expired",
                    {"thread_id": msg.thread_id, "lock_key": lock_key, "message_id": msg.id},
                )

    # -- Debounce loop -------------------------------------------------------

    async def _debounce_loop(
        self,
        heartbeat: _LockHeartbeat,
        adapter: Adapter,
        lock_key: str,
    ) -> None:
        """Wait ``debounce_ms``, fold superseded messages into ``skipped``, dispatch the last.

        Loops until the queue stays empty for a whole window, so a message
        enqueued while the handler ran is debounced and processed instead of
        stranded until the next webhook.
        """
        debounce_ms = self._concurrency_debounce_ms
        skipped: list[Message] = []

        while True:
            await _sleep(debounce_ms)
            if heartbeat.is_ownership_lost():
                # Another instance may hold the lock now -- leave the queue to it.
                self._logger.warn("Stopping debounce loop after lock ownership was lost", {"lock_key": lock_key})
                return

            pending = await self._take_pending(adapter, lock_key)
            if not pending:
                return
            # Upstream parity: ownership is not re-checked after collecting
            # (see the note in _drain_queue).

            latest = pending[-1]
            skipped.extend(pending[:-1])

            # Check if anything new arrived during the dequeue
            depth = await self._state_adapter.queue_depth(lock_key)
            if depth > 0:
                # Newer message superseded this one -- loop again
                skipped.append(latest)
                self._logger.info(
                    "message-superseded",
                    {"thread_id": latest.thread_id, "lock_key": lock_key, "dropped_id": latest.id},
                )
                continue

            # Nothing new -- this is the final message in the burst. Dispatch
            # it under ITS OWN thread id (channel-scoped locks can hold
            # messages from several threads; upstream #832).
            message_thread_id = latest.thread_id
            message_skipped = [m for m in skipped if m.thread_id == message_thread_id]
            self._logger.info(
                "message-dequeued",
                {"thread_id": message_thread_id, "lock_key": lock_key, "message_id": latest.id},
            )
            # Re-enter the conversation of the message being dispatched, not
            # the lock holder's: a channel-scoped lock drains other threads.
            with conversation(message_thread_id):
                await self._dispatch_to_handlers(
                    adapter,
                    message_thread_id,
                    latest,
                    MessageContext(
                        skipped=message_skipped,
                        total_since_last_handler=len(message_skipped) + 1,
                    ),
                )
            skipped = []
            # Loop again: a message enqueued while the handler ran must be
            # debounced and processed, not stranded until the next webhook.

    # -- Drain queue ---------------------------------------------------------

    async def _drain_queue(
        self,
        heartbeat: _LockHeartbeat,
        adapter: Adapter,
        lock_key: str,
    ) -> None:
        """Dispatch the latest pending message with the rest as skipped; repeat until empty."""
        while True:
            if heartbeat.is_ownership_lost():
                # Another instance may hold the lock now -- leave the queue to it.
                self._logger.warn("Stopping queue drain after lock ownership was lost", {"lock_key": lock_key})
                return

            pending = await self._take_pending(adapter, lock_key)
            if not pending:
                return
            # Upstream parity (chat@4.41.1 drainQueue/debounceLoop): ownership
            # is NOT re-checked after collecting. Dequeue is destructive, so
            # these messages are now invisible to any new holder: dispatching
            # them is the only loss-free choice (dropping loses them; handing
            # them back cannot be done atomically through the StateAdapter
            # protocol). The cost is a possible brief overlap with a new
            # holder, which upstream accepts. See docs/UPSTREAM_SYNC.md.

            # Latest message is the one we process, under ITS OWN thread id;
            # skipped context only carries messages from that same thread
            # (channel-scoped locks can hold several threads; upstream #832).
            latest = pending[-1]
            message_thread_id = latest.thread_id
            skipped = [m for m in pending[:-1] if m.thread_id == message_thread_id]

            self._logger.info(
                "message-dequeued",
                {
                    "thread_id": message_thread_id,
                    "lock_key": lock_key,
                    "message_id": latest.id,
                    "skipped_count": len(skipped),
                    "total_since_last_handler": len(skipped) + 1,
                },
            )

            context = MessageContext(
                skipped=skipped,
                total_since_last_handler=len(skipped) + 1,
            )
            with conversation(message_thread_id):
                await self._dispatch_to_handlers(adapter, message_thread_id, latest, context)
            # After processing, check if MORE messages arrived during this
            # handler (loop continues).

    # -- Concurrent strategy -------------------------------------------------

    async def _handle_concurrent(
        self,
        adapter: Adapter,
        thread_id: str,
        message: Message,
    ) -> None:
        # Enforce `max_concurrent` bound when configured. Upstream TS
        # accepts the config field but never enforces it; we do, so that
        # consumers setting `ConcurrencyConfig(strategy="concurrent",
        # max_concurrent=N)` actually get a bound of N in-flight handlers.
        if self._concurrent_semaphore is None:
            await self._dispatch_to_handlers(adapter, thread_id, message)
            return
        async with self._concurrent_semaphore:
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
        """Run one turn: route the message under a fresh cancellation signal.

        Upstream ``dispatchToHandlers``. The signal is registered for
        :meth:`abort_turn`; for adapters with ``supports_turn_cancellation``
        the turn is also published to the state backend and polled for an
        abort from another process, and its markers are cleared afterwards.
        """
        turn_id = str(uuid.uuid4())
        signal = TurnSignal()
        # ``is True``: a MagicMock adapter would otherwise opt in by accident.
        cancellable = getattr(adapter, "supports_turn_cancellation", False) is True
        monitor: asyncio.Task[None] | None = None
        signals = self._active_turn_signals.setdefault(thread_id, {})
        signals[turn_id] = signal
        # Python-specific: everything after registration sits inside the
        # ``try`` so a cancelled task still unregisters (JS cannot be
        # interrupted between these steps).
        try:
            if cancellable:
                try:
                    await self._state_adapter.set(self._active_turn_key(thread_id), turn_id, ACTIVE_TURN_TTL_MS)
                except Exception as error:
                    self._logger.warn(
                        "Could not publish active turn for cancellation", {"error": error, "thread_id": thread_id}
                    )
                monitor = asyncio.get_running_loop().create_task(self._monitor_turn_abort(thread_id, turn_id, signal))
            await self._dispatch_to_handlers_with_signal(adapter, thread_id, message, signal, context)
        finally:
            signals.pop(turn_id, None)
            if not signals and self._active_turn_signals.get(thread_id) is signals:
                del self._active_turn_signals[thread_id]
            try:
                if monitor is not None:
                    monitor.cancel()
                    # Waits for the poll to stop; a cancellation of this task
                    # still propagates (``gather`` only absorbs the monitor's).
                    await asyncio.gather(monitor, return_exceptions=True)
            finally:
                # Still runs when this task is cancelled during the join.
                if cancellable:
                    await self._clear_turn_markers(thread_id, turn_id)

    @staticmethod
    def _active_turn_key(thread_id: str) -> str:
        return f"active-turn:{thread_id}"

    @staticmethod
    def _abort_turn_key(thread_id: str) -> str:
        return f"abort-turn:{thread_id}"

    async def _monitor_turn_abort(self, thread_id: str, turn_id: str, signal: TurnSignal) -> None:
        """Poll for an abort of this turn until it is aborted or cancelled."""
        key = self._abort_turn_key(thread_id)
        while not signal.aborted:
            try:
                aborted_turn_id = await self._state_adapter.get(key)
            except Exception as error:
                self._logger.warn("Could not poll turn cancellation state", {"error": error, "thread_id": thread_id})
                return
            if aborted_turn_id == turn_id:
                signal._abort()
                return
            await asyncio.sleep(ABORT_POLL_INTERVAL_MS / 1000)

    async def _clear_turn_markers(self, thread_id: str, turn_id: str) -> None:
        """Delete this turn's markers, leaving any written by a newer turn.

        Upstream parity (chat.ts ``clearTurnMarkers``, chat@4.41.1): the
        ownership check and the delete are separate state calls, as upstream;
        ``StateAdapter`` has no compare-and-delete. A newer turn publishing
        in between loses its marker until it publishes again.
        """
        try:
            active_key = self._active_turn_key(thread_id)
            if await self._state_adapter.get(active_key) == turn_id:
                await self._state_adapter.delete(active_key)
            abort_key = self._abort_turn_key(thread_id)
            if await self._state_adapter.get(abort_key) == turn_id:
                await self._state_adapter.delete(abort_key)
        except Exception as error:
            self._logger.warn("Could not clear turn cancellation state", {"error": error, "thread_id": thread_id})

    async def _dispatch_to_handlers_with_signal(
        self,
        adapter: Adapter,
        thread_id: str,
        message: Message,
        signal: TurnSignal,
        context: MessageContext | None = None,
    ) -> None:
        """Route a message to the correct handler chain."""
        # Register the owning adapter so handlers can lazily resolve
        # ``message.subject`` via the adapter's optional ``fetch_subject`` hook.
        # Mirrors upstream's ``setMessageAdapter`` call at the dispatch bind
        # site (packages/chat/src/chat.ts). Every dispatched message flows
        # through here, so this is the single registration point.
        set_message_adapter(message, adapter)
        # Skipped messages (queue drain / burst collapse) are surfaced to
        # handlers via ``context.skipped`` but never themselves dispatched,
        # so they also need their adapter bound for ``await msg.subject`` to
        # work inside the handler.
        if context is not None:
            for skipped_msg in context.skipped:
                set_message_adapter(skipped_msg, adapter)

        # Detect mention on the dispatched message and every skipped one
        # (upstream #656/#659): an earlier skipped mention still routes to
        # mention handlers.
        has_mention = self._set_mention_flags(adapter, message, context)

        # Check subscription
        is_subscribed = await self._state_adapter.is_subscribed(thread_id)
        self._logger.debug("Subscription check", {"thread_id": thread_id, "is_subscribed": is_subscribed})

        thread = self._create_thread(adapter, thread_id, message, is_subscribed, signal)
        await self._resolve_message_identity(adapter, thread_id, message)

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
                result = h(thread, message, channel, context)
                if inspect.isawaitable(result):
                    await result
            return

        # Backward compat: treat DMs as mentions when no DM handlers are
        # registered. This is a routing rule, not a detection result, so it
        # deliberately overrides an adapter's ``False``: every DM is addressed
        # to the bot (upstream chat.ts, vercel/chat#946).
        if is_dm:
            message.is_mention = True

        # Subscribed thread
        if is_subscribed:
            self._logger.debug("Message in subscribed thread", {"thread_id": thread_id})
            await self._run_handlers(self._subscribed_message_handlers, thread, message, context)
            return

        # Mention -- the dispatched message itself, or a skipped message when
        # mention handlers exist (otherwise fall through to patterns).
        if message.is_mention or (has_mention and self._mention_handlers):
            self._logger.debug("Bot mentioned", {"thread_id": thread_id})
            await self._run_handlers(self._mention_handlers, thread, message, context)
            return

        # Pattern matching
        matched = False
        for pat in self._message_patterns:
            if pat.pattern.search(message.text):
                self._logger.debug("Message matched pattern", {"pattern": pat.pattern.pattern})
                matched = True
                result = pat.handler(thread, message, context)
                if inspect.isawaitable(result):
                    await result

        if not matched:
            self._logger.debug("No handlers matched message", {"thread_id": thread_id})

    async def _resolve_message_identity(self, adapter: Adapter, thread_id: str, message: Message) -> None:
        """Resolve the cross-platform user key (Transcripts API) onto *message*.

        Cached on the Message instance so handlers and the Transcripts API see
        the same value without re-invoking the resolver. Upstream
        ``resolveMessageIdentity``; the resolver may be sync or async.
        """
        if self._identity is None or message.user_key is not None:
            return
        try:
            resolved = self._identity(IdentityContext(adapter=adapter.name, author=message.author, message=message))
            if inspect.isawaitable(resolved):
                resolved = await resolved
            if resolved:
                message.user_key = resolved
        except Exception as err:
            self._logger.warn(
                "Identity resolver threw; skipping userKey",
                {
                    "error": err,
                    "adapter": adapter.name,
                    "thread_id": thread_id,
                    "author_user_id": message.author.user_id,
                },
            )

    # ========================================================================
    # Message lifecycle (update / delete)
    # ========================================================================

    async def _handle_message_updated(
        self,
        adapter: Adapter,
        thread_id: str,
        message: Message,
        previous_message: Message | None = None,
    ) -> None:
        """Dispatch a message update to ``on_message_updated`` handlers only.

        Upstream ``handleMessageUpdated``: no dedupe, no lock, no routing.
        """
        # Upstream parity (chat@4.41.1 chat.ts:2503): only the current message
        # is bound to the adapter; ``previous_message`` is passed through as the
        # adapter built it (its ``subject`` resolves to None unless the adapter
        # bound it, as upstream message.ts:191-198).
        set_message_adapter(message, adapter)
        self._logger.debug(
            "Incoming message update",
            {
                "adapter": adapter.name,
                "thread_id": thread_id,
                "message_id": message.id,
                "author": message.author.user_name,
                "author_user_id": message.author.user_id,
                "is_bot": message.author.is_bot,
                "is_me": message.author.is_me,
            },
        )

        # Skip the bot's own edits, matching process_message. Post-and-edit
        # streaming edits the bot's reply repeatedly and Slack emits a
        # message_changed for each one, so without this a single streamed
        # reply would fire this handler once per delta.
        if message.author.is_me:
            self._logger.debug(
                "Skipping message update from self (isMe=true)",
                {"adapter": adapter.name, "thread_id": thread_id, "author": message.author.user_name},
            )
            return

        is_subscribed = await self._state_adapter.is_subscribed(thread_id)
        thread = self._create_thread(adapter, thread_id, message, is_subscribed)
        await self._resolve_message_identity(adapter, thread_id, message)

        with conversation(thread_id):
            for h in self._message_updated_handlers:
                await self._invoke_handler(h, thread, message, previous_message)

    async def _handle_message_deleted(self, event: MessageDeletedEvent) -> None:
        """Dispatch a normalized message delete (upstream ``handleMessageDeleted``).

        Deletes often carry no message body, so no Thread is constructed.
        """
        if event.previous_message is not None:
            set_message_adapter(event.previous_message, event.adapter)

        self._logger.debug(
            "Incoming message delete",
            {
                "adapter": event.adapter.name,
                "thread_id": event.thread_id,
                "message_id": event.message_id,
                "channel_id": event.channel_id,
            },
        )

        normalized = event if event.platform is not None else dataclasses.replace(event, platform=event.adapter.name)

        with conversation(event.thread_id):
            for h in self._message_deleted_handlers:
                await self._invoke_handler(h, normalized)

    # ========================================================================
    # Thread creation
    # ========================================================================

    def _create_thread(
        self,
        adapter: Adapter,
        thread_id: str,
        initial_message: Message | None,
        is_subscribed_context: bool = False,
        signal: TurnSignal | None = None,
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
                signal=signal,
                logger=self._logger,
                streaming_update_interval_ms=self._streaming_update_interval_ms,
                fallback_streaming_placeholder_text=self._fallback_streaming_placeholder_text,
                thread_history=(
                    self._thread_history
                    if (
                        getattr(adapter, "persist_thread_history", None)
                        or getattr(adapter, "persist_message_history", None)
                    )
                    else None
                ),
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

    def _set_mention_flags(
        self,
        adapter: Adapter,
        message: Message,
        context: MessageContext | None = None,
    ) -> bool:
        """Fill ``is_mention`` on ``message`` and each ``context.skipped`` in place.

        Returns whether any of them mentions the bot. Port of upstream
        ``setMentionFlags`` (vercel/chat#946): ``is_mention`` is tri-state. An
        adapter that reads the platform's own mention metadata reports a
        definitive ``True``/``False``; text detection runs only when it
        reported nothing (``None``), so a known non-mention is not re-derived
        from flattened text (code samples, quoted text). Never collapse this
        with ``or`` -- that would re-derive an adapter's ``False``.
        """
        if message.is_mention is None:
            message.is_mention = self._detect_mention(adapter, message)
        has_mention = message.is_mention is True
        if context is not None:
            for skipped in context.skipped:
                if skipped.is_mention is None:
                    skipped.is_mention = self._detect_mention(adapter, skipped)
                has_mention = has_mention or skipped.is_mention is True
        return has_mention

    def _detect_mention(self, adapter: Adapter, message: Message) -> bool:
        """Whether ``message.text`` @-mentions the bot by username or user id.

        Port of upstream ``detectMention`` (vercel/chat#621, #761): the ``@``
        must not follow a word character, so emails and URL userinfo
        (``jane@acme.com``) do not mention a bot named ``acme``, and the name
        must not be followed by a word character or ``-``, so ``@bot-dev`` /
        ``@botlong`` do not mention ``@bot`` (``mybot[bot]`` still matches).
        """
        bot_user_name = adapter.user_name or self._user_name
        bot_user_id = adapter.bot_user_id

        # @username check
        username_pattern = self._get_mention_pattern(f"username:{bot_user_name}", _mention_name_pattern(bot_user_name))
        if username_pattern.search(message.text):
            return True

        if bot_user_id:
            user_id_pattern = self._get_mention_pattern(f"userid:{bot_user_id}", _mention_name_pattern(bot_user_id))
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

    def _rehydrate_message(self, raw: Any, adapter: Adapter | None = None) -> Message:
        """Reconstruct a proper Message from a dequeued entry (may be plain dict).

        After a JSON roundtrip through the state adapter (queue/debounce
        strategies), the message is a plain dict and any ``fetch_data``
        callables on attachments have been stripped.  If ``adapter``
        exposes a ``rehydrate_attachment`` hook we call it on every
        attachment that lost its ``fetch_data`` closure so downstream
        handlers can still download bytes.
        """
        # A ``Message`` input falls through to the rehydrate pass, as upstream
        # has done since vercel/chat#802 (chat@4.38.0). Our Redis / Postgres
        # ``dequeue()`` already return ``Message`` instances, so this is the
        # common case; attachments that still have ``fetch_data`` are skipped.
        pending_reply_to: Any = None
        if isinstance(raw, Message):
            msg = raw
        elif isinstance(raw, dict):
            if raw.get("_type") == "chat:Message":
                msg = _message_from_json(raw)
            else:
                # Fallback: plain dict
                metadata_raw = raw.get("metadata", {})
                date_sent = metadata_raw.get("date_sent")
                if isinstance(date_sent, str):
                    date_sent = _parse_iso(date_sent)
                elif not isinstance(date_sent, datetime):
                    date_sent = datetime.now(tz=timezone.utc)

                edited_at = metadata_raw.get("edited_at")
                if isinstance(edited_at, str):
                    edited_at = _parse_iso(edited_at)

                author_raw = raw.get("author", {})
                pending_reply_to = raw.get("replyTo") if "replyTo" in raw else raw.get("reply_to")
                msg = Message(
                    id=raw.get("id", ""),
                    # Drains dispatch each message under its own thread id
                    # (#190), so accept the camelCase key too.
                    thread_id=raw.get("thread_id") or raw.get("threadId", ""),
                    text=raw.get("text", ""),
                    formatted=raw.get("formatted", {"type": "root", "children": []}),
                    raw=raw.get("raw"),
                    author=Author(
                        user_id=author_raw.get("user_id", ""),
                        user_name=author_raw.get("user_name", ""),
                        full_name=author_raw.get("full_name", ""),
                        is_bot=author_raw.get("is_bot", False),
                        is_me=author_raw.get("is_me", False),
                        email=author_raw.get("email"),
                        is_system=(
                            author_raw.get("isSystem") if "isSystem" in author_raw else author_raw.get("is_system")
                        ),
                    ),
                    metadata=MessageMetadata(
                        date_sent=date_sent,
                        edited=metadata_raw.get("edited", False),
                        edited_at=edited_at,
                    ),
                    attachments=_coerce_attachments(raw.get("attachments", [])),
                    is_mention=raw.get("is_mention"),
                    links=raw.get("links", []),
                )
        else:
            # Last resort: assume it's already a Message-like object
            return raw  # type: ignore[return-value]

        # Apply the adapter's rehydrate_attachment hook (if provided) to any
        # attachment that lost its fetch_data closure during serialization.
        # Matches TS: `adapter?.rehydrateAttachment?.(att)` — duck-typed so
        # adapters that do not declare the hook (e.g. bare MockAdapter) are
        # treated as no-ops and the attachment is left untouched.
        rehydrate_fn: Callable[[Attachment], Attachment] | None = (
            getattr(adapter, "rehydrate_attachment", None) if adapter else None
        )
        if rehydrate_fn is not None and msg.attachments:
            msg.attachments = [att if att.fetch_data is not None else rehydrate_fn(att) for att in msg.attachments]

        # Recurse into the replied-to message (upstream vercel/chat#802) so its
        # attachments are rehydrated too. The plain-dict fallback above leaves
        # the raw ``replyTo`` value in ``pending_reply_to``.
        reply_to: Any = pending_reply_to if pending_reply_to is not None else msg.reply_to
        msg.reply_to = self._rehydrate_message(reply_to, adapter) if isinstance(reply_to, (Message, dict)) else None

        return msg

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
            await self._invoke_handler(h, thread, message, context)

    @staticmethod
    async def _invoke_handler(handler: Any, /, *args: Any, **kwargs: Any) -> Any:
        """Invoke a handler and await the result only if awaitable.

        All Chat handler types (message, reaction, action, slash, modal,
        options-load, assistant, home, member-joined) are declared as
        `Callable[..., Awaitable[T] | T]` — i.e. users may register either
        a sync or an async callable. Awaiting the return value
        unconditionally raises `TypeError: object NoneType can't be used
        in 'await' expression` for sync handlers, so this helper narrows
        with `inspect.isawaitable`.

        Returns whatever the handler returned (post-await for async) so
        callers that need the value (modal submit → `ModalResponse`) can
        still capture it.
        """
        result = handler(*args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result


# ---------------------------------------------------------------------------
# Helper: construct Message from serialized dict
# ---------------------------------------------------------------------------


def _modal_conversation_id(event: ModalSubmitEvent | ModalCloseEvent) -> str | None:
    """The conversation a modal event runs in: its related thread, else channel.

    Mirrors upstream ``relatedThread?.id ?? relatedChannel?.id``; ``None``
    (no related thread or channel) runs the handlers bare.
    """
    for related in (event.related_thread, event.related_channel):
        related_id = getattr(related, "id", None) if related is not None else None
        if related_id is not None:
            return related_id
    return None


# Upstream's guards are ``(?<!\w)`` / ``(?![\w-])`` with flag ``i`` (no ``u``),
# where JS ``\w`` is ASCII-only. Python's ``\w`` is Unicode, so the ASCII
# class is spelled out; ``é@bot`` stays a mention, as upstream. Not
# ``re.ASCII``: that would make IGNORECASE ASCII-only, while JS ``i`` folds
# non-ASCII names. The guards are case-sensitive scoped groups so Python's
# Unicode case folding cannot widen ``[A-Za-z]`` to the Kelvin sign / long s
# (JS ``i`` without ``u`` never folds non-ASCII into ASCII). See
# docs/UPSTREAM_SYNC.md.
_MENTION_BEFORE_GUARD = r"(?-i:(?<![A-Za-z0-9_]))"
_MENTION_AFTER_GUARD = r"(?-i:(?![A-Za-z0-9_-]))"


def _mention_name_pattern(name: str) -> str:
    """Regex source for an ``@name`` mention (compiled with ``re.IGNORECASE``)."""
    return f"{_MENTION_BEFORE_GUARD}@{re.escape(name)}{_MENTION_AFTER_GUARD}"


def _coerce_attachments(raw: Any) -> list[Attachment]:
    """Convert a list of attachment dicts (post JSON roundtrip) to ``Attachment`` instances.

    ``Message.from_json()`` already handles this when the outer dict uses the
    ``_type: "chat:Message"`` envelope, but the plain-dict fallback in
    ``_rehydrate_message`` may receive raw dicts (e.g. in-memory state that
    bypassed ``to_json``).  Idempotent: ``Attachment`` instances pass through.
    """
    if not raw:
        return []
    out: list[Attachment] = []
    for att in raw:
        if isinstance(att, Attachment):
            out.append(att)
        elif isinstance(att, dict):
            mime_type = att.get("mimeType")
            if mime_type is None:
                mime_type = att.get("mime_type")
            fetch_metadata = att.get("fetchMetadata")
            if fetch_metadata is None:
                fetch_metadata = att.get("fetch_metadata")
            out.append(
                Attachment(
                    type=att.get("type", "file"),
                    url=att.get("url"),
                    name=att.get("name"),
                    mime_type=mime_type,
                    size=att.get("size"),
                    width=att.get("width"),
                    height=att.get("height"),
                    # ``data`` is not part of ``SerializedAttachment`` (bytes
                    # aren't JSON-safe, so ``to_json`` drops it).  But
                    # in-memory state backends can hand us raw dicts that
                    # still carry the bytes; pass them through so we don't
                    # silently lose pre-fetched data on rehydrate.
                    data=att.get("data"),
                    fetch_metadata=fetch_metadata,
                )
            )
    return out


def _message_from_json(data: dict[str, Any]) -> Message:
    author_raw = data.get("author", {})
    metadata_raw = data.get("metadata", {})
    reply_to_raw = data.get("replyTo") if "replyTo" in data else data.get("reply_to")

    date_sent = metadata_raw.get("dateSent") or metadata_raw.get("date_sent")
    if isinstance(date_sent, str):
        date_sent = _parse_iso(date_sent)
    elif not isinstance(date_sent, datetime):
        date_sent = datetime.now(tz=timezone.utc)

    edited_at = metadata_raw.get("editedAt") or metadata_raw.get("edited_at")
    if isinstance(edited_at, str):
        edited_at = _parse_iso(edited_at)

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
            email=author_raw.get("email"),
            is_system=author_raw.get("isSystem") if "isSystem" in author_raw else author_raw.get("is_system"),
        ),
        metadata=MessageMetadata(
            date_sent=date_sent,
            edited=metadata_raw.get("edited", False),
            edited_at=edited_at,
        ),
        attachments=_coerce_attachments(data.get("attachments", [])),
        is_mention=data.get("isMention") if "isMention" in data else data.get("is_mention"),
        links=data.get("links", []),
        # The reviver runs bottom-up, so a nested ``replyTo`` may already be a
        # revived ``Message``; pass it through.
        reply_to=(
            reply_to_raw
            if isinstance(reply_to_raw, Message)
            else _message_from_json(reply_to_raw)
            if isinstance(reply_to_raw, dict)
            else None
        ),
    )


def _merge_user_history_config(
    transcripts: TranscriptsConfig | None,
    user: UserHistoryConfig | None,
) -> TranscriptsConfig | None:
    """Merge ``history.user`` over the legacy ``transcripts`` block.

    Upstream spreads ``{...transcripts, ...history.user}``. Dataclass fields
    always exist, so the merge is field by field: a ``history.user`` field
    wins unless it is ``None`` (unset). Returns ``None`` when neither block
    is configured.
    """
    if transcripts is None and user is None:
        return None

    def pick(name: str) -> Any:
        value = getattr(user, name) if user is not None else None
        if value is not None:
            return value
        return getattr(transcripts, name) if transcripts is not None else None

    store_formatted = pick("store_formatted")
    return TranscriptsConfig(
        max_per_user=pick("max_per_user"),
        retention=pick("retention"),
        store_formatted=store_formatted if store_formatted is not None else False,
    )


# ---------------------------------------------------------------------------
# Minimal ThreadHistoryCache (placeholder -- real impl uses StateAdapter lists)
# ---------------------------------------------------------------------------

# Key prefix for thread history entries.
#
# Kept as ``msg-history:`` for backwards compatibility — renaming would
# silently orphan every existing user's stored data. The user-facing names
# changed; the storage shape didn't. (Matches KEY_PREFIX in thread_history.py.)
_THREAD_HISTORY_KEY_PREFIX = "msg-history:"


class _ThreadHistoryCache:
    """Lightweight in-SDK per-thread history cache backed by the state adapter."""

    def __init__(self, state: StateAdapter, config: dict[str, Any] | None = None) -> None:
        self._state = state
        self._max_messages = (config or {}).get("max_messages", 100)
        self._ttl_ms = (config or {}).get("ttl_ms", 30 * 24 * 60 * 60 * 1000)

    async def append(self, thread_id: str, message: Message) -> None:
        key = f"{_THREAD_HISTORY_KEY_PREFIX}{thread_id}"
        # Serialize with raw nulled out to save storage (matches
        # ThreadHistoryCache.append in thread_history.py). Without this,
        # SentMessage.raw — now populated post-#117 with platform payloads
        # like Slack team_id/user_id, Discord guild IDs — would persist to
        # the state adapter on every reply, inflating storage and PII surface.
        data = message.to_json()
        # Null raw along the whole ``replyTo`` chain (upstream vercel/chat#802).
        current: dict[str, Any] | None = data
        while current is not None:
            current["raw"] = None
            current = current.get("replyTo")
        await self._state.append_to_list(key, data, max_length=self._max_messages, ttl_ms=self._ttl_ms)

    async def get_messages(self, thread_id: str, limit: int | None = None) -> list[Message]:
        key = f"{_THREAD_HISTORY_KEY_PREFIX}{thread_id}"
        raw_list = await self._state.get_list(key)
        messages = [_message_from_json(r) if isinstance(r, dict) else r for r in raw_list]
        if limit is not None:
            messages = messages[-limit:]
        return messages
