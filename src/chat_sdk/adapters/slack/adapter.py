"""Slack adapter for chat-sdk.

Supports single-workspace (bot token) and multi-workspace (OAuth) modes.
All conversations use Slack threads as the unit of isolation.

Python port of packages/adapter-slack/src/index.ts.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import contextvars
import hashlib
import hmac
import inspect
import json
import os
import re
import time
import warnings
from collections import OrderedDict
from collections.abc import AsyncIterable, Awaitable, Callable
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, NoReturn, TypedDict, cast
from urllib.parse import parse_qs, urlparse

from chat_sdk.adapters.slack.cards import (
    card_to_block_kit,
    card_to_fallback_text,
)
from chat_sdk.adapters.slack.crypto import (
    EncryptedTokenData,
    decode_key,
    decrypt_token,
    encrypt_token,
    is_encrypted_token_data,
)
from chat_sdk.adapters.slack.format_converter import SlackFormatConverter
from chat_sdk.adapters.slack.modals import (
    ModalMetadata,
    SlackModalResponse,
    decode_modal_metadata,
    encode_modal_metadata,
    modal_to_slack_view,
)
from chat_sdk.adapters.slack.types import (
    RequestContext,
    SlackAdapterConfig,
    SlackAdapterMode,
    SlackBotToken,
    SlackBotTokenResolver,
    SlackInstallation,
    SlackThreadId,
    SlackWebhookVerifier,
)
from chat_sdk.emoji import convert_emoji_placeholders, emoji_to_slack, resolve_emoji_from_slack
from chat_sdk.logger import ConsoleLogger, Logger
from chat_sdk.modals import ModalElement, OptionsLoadGroup, SelectOptionElement
from chat_sdk.shared.adapter_utils import extract_card, extract_files
from chat_sdk.shared.errors import AdapterRateLimitError, AuthenticationError, ValidationError
from chat_sdk.types import (
    ActionEvent,
    AdapterPostableMessage,
    AppHomeOpenedEvent,
    AssistantContextChangedEvent,
    AssistantThreadStartedEvent,
    Attachment,
    Author,
    ChannelInfo,
    ChannelVisibility,
    ChatInstance,
    EmojiValue,
    EphemeralMessage,
    FetchOptions,
    FetchResult,
    FileUpload,
    FormattedContent,
    LinkPreview,
    ListThreadsOptions,
    ListThreadsResult,
    LockScope,
    MemberJoinedChannelEvent,
    Message,
    MessageMetadata,
    ModalCloseEvent,
    ModalResponse,
    ModalSubmitEvent,
    OptionsLoadEvent,
    PostableMarkdown,
    RawMessage,
    ReactionEvent,
    ScheduledMessage,
    SlashCommandEvent,
    StreamChunk,
    StreamOptions,
    ThreadInfo,
    ThreadSummary,
    UserInfo,
    WebhookOptions,
)

# Slack expects block_suggestion responses within 3s. Leave headroom for
# network latency so the HTTP response lands before Slack gives up.
OPTIONS_LOAD_TIMEOUT_MS = 2500

# Strong-reference set for fire-and-forget tasks to prevent GC collection.
_background_tasks: set[asyncio.Task[Any]] = set()


def _pin_task(task: asyncio.Task[Any]) -> None:
    """Pin a fire-and-forget task so the GC doesn't collect it."""
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SLACK_USER_ID_PATTERN = re.compile(r"^[A-Z0-9_]+$")
SLACK_USER_ID_EXACT_PATTERN = re.compile(r"^U[A-Z0-9]+$")

SLACK_MESSAGE_URL_PATTERN = re.compile(r"^https?://[^/]+\.slack\.com/archives/([A-Z0-9]+)/p(\d+)(?:\?.*)?$")

# Cache TTLs (milliseconds)
_USER_CACHE_TTL_MS = 8 * 24 * 60 * 60 * 1000  # 8 days
_CHANNEL_CACHE_TTL_MS = 8 * 24 * 60 * 60 * 1000
_REVERSE_INDEX_TTL_MS = 8 * 24 * 60 * 60 * 1000


class SlackUserCacheEntry(TypedDict, total=False):
    """Cached user shape returned by :meth:`SlackAdapter._lookup_user`.

    The first five keys always exist on a successful lookup or
    cache hit. ``_lookup_failed`` appears only on the failure path
    (API exception or empty user payload) — callers like
    :meth:`SlackAdapter.get_user` use it to return ``None`` instead of
    a fallback ``UserInfo``. ``total=False`` because both the cache hit
    branch and the failure branch omit ``_lookup_failed``.
    """

    display_name: str
    real_name: str
    email: str | None
    avatar_url: str | None
    is_bot: bool | None
    _lookup_failed: bool


def _make_slack_lookup_failed(user_id: str) -> SlackUserCacheEntry:
    """Build the sentinel cache entry for a failed Slack user lookup.

    Shared between the ``except`` path and the empty-user-payload path
    so both produce the exact same fallback shape (and neither caches
    it — see :meth:`SlackAdapter._lookup_user`).
    """
    return {
        "display_name": user_id,
        "real_name": user_id,
        "email": None,
        "avatar_url": None,
        "is_bot": None,
        "_lookup_failed": True,
    }


# Ignored message subtypes (system/meta events).
# `message_changed` is NOT in this set — it is routed to
# `_handle_message_changed` so we can capture link unfurl metadata.
_IGNORED_SUBTYPES = frozenset(
    {
        "message_deleted",
        "message_replied",
        "channel_join",
        "channel_leave",
        "channel_topic",
        "channel_purpose",
        "channel_name",
        "channel_archive",
        "channel_unarchive",
        "group_join",
        "group_leave",
        "group_topic",
        "group_purpose",
        "group_name",
        "group_archive",
        "group_unarchive",
        "ekm_access_denied",
        "tombstone",
    }
)

# Link-unfurl wait window: Slack delivers unfurled attachments via a
# separate `message_changed` event ~100-2000ms after the original. We
# poll briefly so the message handler sees enriched links instead of
# bare URLs.
_TRAILING_SLASH_PATTERN = re.compile(r"/$")
_UNFURL_WAIT_MS = 2000
_UNFURL_POLL_MS = 150
_UNFURL_CACHE_TTL_MS = 60 * 60 * 1000  # 1 hour


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_next_mention(text: str) -> int:
    """Find the next ``<@`` or ``<#`` mention in *text*."""
    at_idx = text.find("<@")
    hash_idx = text.find("<#")
    if at_idx == -1:
        return hash_idx
    if hash_idx == -1:
        return at_idx
    return min(at_idx, hash_idx)


def _normalize_bot_token_provider(
    value: SlackBotToken | None,
) -> SlackBotTokenResolver | None:
    """Normalize a ``bot_token`` config value to a zero-arg resolver.

    Mirrors upstream's ``normalizeBotTokenProvider``. A static string becomes
    a resolver that returns the same string; a callable is returned as-is so
    rotation or async lookup is preserved. ``None`` -> ``None`` (no token).
    """
    if value is None:
        return None
    if callable(value):
        # Already a resolver — keep the user's callable so rotation works.
        return value
    static = value

    def _provider() -> str:
        return static

    return _provider


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class SlackAdapter:
    """Slack adapter for chat-sdk.

    Implements the Adapter interface for the Slack Web API.
    Supports both single-workspace (static bot token) and multi-workspace
    (per-team OAuth token lookup) modes.
    """

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(self, config: SlackAdapterConfig | None = None) -> None:
        # ContextVar replaces Node AsyncLocalStorage for per-request token context.
        # Created per-instance so multiple SlackAdapter instances don't share state.
        self._request_context: ContextVar[RequestContext | None] = ContextVar(
            f"slack_request_context_{id(self)}", default=None
        )
        # Per-request cache of the resolved default bot token. Populated at the
        # top of ``handle_webhook`` (after the signature/verifier check) and
        # read by the sync ``_get_token`` path. Each concurrent request gets
        # its own contextvar copy so dynamic resolvers returning different
        # values per call cannot bleed between in-flight requests.
        self._resolved_default_token: ContextVar[str | None] = ContextVar(
            f"slack_resolved_default_token_{id(self)}", default=None
        )
        if config is None:
            config = SlackAdapterConfig()

        mode = config.mode or "webhook"

        # ``webhook_verifier`` takes precedence over ``signing_secret`` (config)
        # and the ``SLACK_SIGNING_SECRET`` env var. When the caller wires up a
        # verifier we ignore both so an env-configured deployment can't silently
        # shadow it (mirrors upstream vercel/chat#468, which reversed the
        # original direction the Python port shipped in PR #87).
        #
        # Empty-string ``signing_secret`` is rejected outright below;
        # empty ``SLACK_SIGNING_SECRET`` env values are normalized to ``None``
        # so they can't masquerade as a configured secret.
        webhook_verifier = config.webhook_verifier
        # Reject an explicit empty-string ``signing_secret`` at construction —
        # even when a ``webhook_verifier`` is set. An explicit ``""`` is a
        # config typo (e.g. an unset env var interpolated into the field), and
        # silently normalizing it to ``None`` would flip the adapter from the
        # built-in HMAC check to the custom verifier *without the caller's
        # knowledge*. Fail fast here so the typo surfaces at init rather than
        # silently altering which verification path runs in production. (An
        # unset/``None`` signing_secret still legitimately defers to the
        # verifier or the env fallback below — only the explicit ``""`` is a
        # hard error.)
        if config.signing_secret == "":
            raise ValidationError(
                "slack",
                "signing_secret must be a non-empty string when provided.",
            )
        # Reject a non-callable ``webhook_verifier`` at construction. A typo
        # such as ``webhook_verifier=""`` / ``False`` / ``123`` passes the
        # ``is not None`` guard below, then ``handle_webhook`` tries to *call*
        # it, the resulting ``TypeError`` is caught and reported as an invalid
        # signature, and every webhook fails closed with 401 — an opaque
        # production outage from a one-character mistake. ``None`` (unset) is
        # fine; anything else must be callable.
        if webhook_verifier is not None and not callable(webhook_verifier):
            raise ValidationError(
                "slack",
                "webhook_verifier must be callable.",
            )
        if webhook_verifier is not None:
            # Verifier wins: drop both the config ``signing_secret`` and the
            # ``SLACK_SIGNING_SECRET`` env fallback. Mirrors upstream
            # vercel/chat#468 (``webhookVerifier`` ?? undefined : (...)).
            signing_secret: str | None = None
        else:
            signing_secret = (
                config.signing_secret if config.signing_secret is not None else os.environ.get("SLACK_SIGNING_SECRET")
            )
            if signing_secret == "":
                signing_secret = None
        # ``signing_secret`` is required in webhook mode (unless a
        # ``webhook_verifier`` replaces the built-in HMAC check). Socket mode
        # legitimately runs without a signing secret because Slack does not
        # sign events delivered over the WebSocket — only HTTP-forwarded events
        # need a separate ``socket_forwarding_secret`` bearer check.
        if mode == "webhook" and signing_secret is None and webhook_verifier is None:
            raise ValidationError(
                "slack",
                "signingSecret or webhookVerifier is required for webhook mode. Set "
                "SLACK_SIGNING_SECRET, provide a non-empty signing_secret in config, "
                "or provide a webhook_verifier.",
            )

        # Auth fields: botToken presence selects single-workspace mode.
        # Explicit ``is not None`` to mirror the truthiness-trap rule above:
        # an explicit empty string for any of these should still count as
        # "config provided" and disable the env-fallback path. ``app_token``
        # also participates (so socket-mode-only configs disable env fallbacks
        # for the other secrets the same way bot-token-only configs do).
        zero_config = (
            config.signing_secret is None
            and config.bot_token is None
            and config.client_id is None
            and config.client_secret is None
            and config.app_token is None
        )

        bot_token_config: SlackBotToken | None = config.bot_token
        # Reject explicit empty-string ``bot_token`` at init for the same
        # reason ``signing_secret=""`` is rejected: it would prime
        # ``_default_bot_token_cache`` with ``""`` and the sync ``_get_token``
        # path would happily return it, producing ``Authorization: Bearer ``
        # API calls and opaque ``invalid_auth`` errors from Slack. (The async
        # resolver path catches this later, but failing fast at construction
        # is strictly better.) Callable resolvers may legitimately *return*
        # non-string values at resolve time — that case is already validated
        # in ``_resolve_default_token``.
        if isinstance(bot_token_config, str) and bot_token_config == "":
            raise ValidationError(
                "slack",
                "bot_token must be a non-empty string or a callable resolver; got an empty string.",
            )
        if bot_token_config is None and zero_config:
            env_token = os.environ.get("SLACK_BOT_TOKEN")
            # Same empty-string-as-missing rule as ``signing_secret``: an empty
            # SLACK_BOT_TOKEN would cache ``""`` in
            # ``_default_bot_token_cache`` and ``_get_token`` would happily
            # return it, producing opaque "invalid_auth" errors from Slack on
            # every API call. Treat ``""`` as unset so the adapter falls
            # through to multi-workspace mode (or fails clearly later).
            if env_token is not None and env_token != "":
                bot_token_config = env_token

        bot_token_provider = _normalize_bot_token_provider(bot_token_config)

        self._name = "slack"
        self._signing_secret: str | None = signing_secret
        # ``webhook_verifier`` takes precedence; ``signing_secret`` is only used
        # when no verifier is configured (matches upstream vercel/chat#468).
        self._webhook_verifier: SlackWebhookVerifier | None = webhook_verifier
        # Resolver returning the default (single-workspace) bot token. ``None`` in
        # multi-workspace mode where the token is resolved per-team from the
        # InstallationStore. Single-workspace mode with a static string still
        # uses a resolver under the hood (returns the same string each call).
        self._default_bot_token_provider: SlackBotTokenResolver | None = bot_token_provider
        # Last successfully resolved default token, kept for the sync ``current_token``
        # accessor and for any code path that needs a token outside an awaitable
        # context. Populated lazily on first await of the resolver and refreshed
        # on each subsequent webhook entry. Static-string configs prime this at
        # construction time so sync access works before any webhook fires.
        self._default_bot_token_cache: str | None = bot_token_config if isinstance(bot_token_config, str) else None
        # True when the user passed a callable ``bot_token`` (sync or async).
        # The config contract says callable resolvers are invoked on each use
        # to support rotation, so sync access goes through a fresh-invoke
        # branch instead of reading ``_default_bot_token_cache``. Static
        # strings have nothing to rotate and stay on the cached fast path.
        self._is_dynamic_bot_token: bool = callable(bot_token_config)

        # ------------------------------------------------------------------
        # Socket mode wiring (PR #86). Resolved AFTER the bot-token resolver
        # is set up so the eventual socket client can read the resolved token
        # via ``_get_token`` / ``current_token_async``. ``app_token`` is the
        # long-lived app-level secret used to open the WebSocket; the bot
        # token (potentially resolver-backed) is still used for API calls.
        # ------------------------------------------------------------------
        app_token = config.app_token if config.app_token is not None else os.environ.get("SLACK_APP_TOKEN")
        if app_token == "":
            app_token = None
        if mode == "socket":
            if not app_token:
                raise ValidationError(
                    "slack",
                    "appToken is required for socket mode. Set SLACK_APP_TOKEN or provide it in config.",
                )
            # Hazard #12: validate the long-lived secret format on init so a
            # typo'd bot token (xoxb-) doesn't get silently used as an app
            # token. Slack app-level tokens always start with ``xapp-``.
            if not app_token.startswith("xapp-"):
                raise ValidationError(
                    "slack",
                    "appToken must start with 'xapp-' (Slack app-level token). "
                    "Bot tokens (xoxb-) are not valid for socket mode.",
                )

        # Socket mode state
        self._mode: SlackAdapterMode = mode
        self._app_token: str | None = app_token
        self._socket_forwarding_secret: str | None = (
            config.socket_forwarding_secret or os.environ.get("SLACK_SOCKET_FORWARDING_SECRET") or app_token
        )
        # The active SocketModeClient instance (when running in socket mode).
        # Typed as ``Any`` because slack_sdk is an optional dependency.
        self._socket_client: Any = None
        # Background task that runs the connect/run/reconnect loop. Tracked so
        # ``disconnect()`` can cancel it cleanly (hazard #5).
        self._socket_task: asyncio.Task[None] | None = None
        # Set when shutdown is requested so the reconnect loop knows to exit
        # rather than retry on a clean disconnect. The Event also wakes up
        # ``_socket_sleep_with_backoff`` immediately so ``stop_socket_mode``
        # doesn't have to wait the full backoff window.
        self._socket_shutdown_event: asyncio.Event = asyncio.Event()
        # Default backoff schedule in seconds. Kept short so tests run fast,
        # but capped low enough that a flapping Slack connection doesn't busy
        # loop. Slack's recommended pattern is exponential backoff with jitter;
        # our minimal schedule mirrors that behavior with explicit caps.
        self._socket_initial_backoff_s = 1.0
        self._socket_max_backoff_s = 30.0
        # Bound the initial Socket Mode handshake so ``initialize()`` doesn't
        # block forever if slack_sdk's ``connect()`` hangs (hazard #11). The
        # config field is typed ``float`` with a 30s default, so this is just
        # a read.
        self._socket_connect_timeout_s: float = config.connect_timeout_s
        self._logger: Logger = config.logger or ConsoleLogger("info")
        self._user_name: str = config.user_name or "bot"
        self._bot_user_id: str | None = config.bot_user_id or None
        self._bot_id: str | None = None  # Bot app ID (B_xxx)
        self._chat: ChatInstance | None = None
        self._format_converter = SlackFormatConverter()
        self._lock_scope: LockScope = "thread"
        self._persist_message_history = False

        # Channel external/shared cache
        self._external_channels: set[str] = set()

        # Cache of AsyncWebClient instances keyed by bot token (LRU-bounded)
        self._client_cache: OrderedDict[str, Any] = OrderedDict()
        self._client_cache_max = config.client_cache_max if config.client_cache_max is not None else 100

        # Cache of synchronous slack_sdk.WebClient instances keyed by bot
        # token, backing the public ``web_client`` property (the direct port
        # of upstream's ``getClientForToken``). Kept separate from
        # ``_client_cache`` because that one holds async ``AsyncWebClient``
        # instances used by the adapter's own API calls; the two client types
        # are not interchangeable. Mirrors upstream's plain (unbounded) Map —
        # one entry per distinct token — since callers reach for this escape
        # hatch rarely and tokens are low-cardinality.
        self._web_client_cache: dict[str, Any] = {}

        # Multi-workspace OAuth fields.
        # ``is not None`` (not truthiness) so an explicit empty-string user
        # config does not silently fall back to env (hazard #1). Empty env
        # values are treated as "unset" (mirrors SLACK_BOT_TOKEN env rule):
        # an empty SLACK_CLIENT_ID would be useless downstream and produce
        # opaque OAuth failures rather than a clear "not configured" state.
        if config.client_id is not None:
            self._client_id: str | None = config.client_id
        elif zero_config:
            env_client_id = os.environ.get("SLACK_CLIENT_ID")
            self._client_id = env_client_id if env_client_id else None
        else:
            self._client_id = None
        if config.client_secret is not None:
            self._client_secret: str | None = config.client_secret
        elif zero_config:
            env_client_secret = os.environ.get("SLACK_CLIENT_SECRET")
            self._client_secret = env_client_secret if env_client_secret else None
        else:
            self._client_secret = None
        self._installation_key_prefix = config.installation_key_prefix or "slack:installation"

        # ``is not None`` (not truthiness) so an explicit ``encryption_key=""``
        # is treated as "user explicitly opted out" and is NOT silently
        # shadowed by ``SLACK_ENCRYPTION_KEY`` from the env. Mirrors the
        # client_id / client_secret rule above (hazard #1). An empty user
        # config still short-circuits ``decode_key`` via the final
        # ``if encryption_key_raw`` guard, so no broken key is built.
        if config.encryption_key is not None:
            encryption_key_raw: str | None = config.encryption_key
        else:
            encryption_key_raw = os.environ.get("SLACK_ENCRYPTION_KEY")
        self._encryption_key: bytes | None = None
        if encryption_key_raw:
            self._encryption_key = decode_key(encryption_key_raw)

    # ------------------------------------------------------------------
    # Properties (Adapter protocol)
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return self._name

    @property
    def user_name(self) -> str:
        return self._user_name

    @property
    def bot_user_id(self) -> str | None:
        ctx = self._request_context.get()
        if ctx and ctx.bot_user_id:
            return ctx.bot_user_id
        return self._bot_user_id

    @property
    def lock_scope(self) -> LockScope:
        return self._lock_scope

    @property
    def persist_message_history(self) -> bool:
        return self._persist_message_history

    @property
    def mode(self) -> SlackAdapterMode:
        """Connection mode (``"webhook"`` or ``"socket"``)."""
        return self._mode

    @property
    def is_socket_mode(self) -> bool:
        """``True`` when the adapter is configured for Socket Mode."""
        return self._mode == "socket"

    # ------------------------------------------------------------------
    # Public request-context accessors
    #
    # These are Python-only extensions to the Adapter surface. They let
    # code running inside a handler call the Slack Web API directly —
    # e.g. ``users.info`` for caller-email resolution — without
    # reaching into the underscore-prefixed ``_get_token`` /
    # ``_get_client`` helpers. See docs/UPSTREAM_SYNC.md.
    # ------------------------------------------------------------------

    @property
    def current_token(self) -> str:
        """Return the bot token bound to the current request context.

        In multi-workspace mode this is the token resolved by the
        ``InstallationStore`` for the current request; in single-workspace
        mode it is the default bot token (or the most recently resolved
        token when a dynamic ``bot_token`` resolver is configured).

        Synchronous: when ``bot_token`` is a resolver and this property is
        accessed before the resolver has run for the first time, an
        :class:`AuthenticationError` is raised. Use
        :meth:`current_token_async` from async contexts to invoke the
        resolver on demand.
        """
        return self._get_token()

    async def current_token_async(self) -> str:
        """Async variant of :attr:`current_token` that invokes the resolver.

        Prefer this over :attr:`current_token` in async code paths when a
        dynamic ``bot_token`` resolver is configured — it ensures the
        resolver is awaited rather than relying on the cached value.
        """
        return await self._resolve_token_async()

    @property
    def current_client(self) -> Any:
        """Return an ``AsyncWebClient`` preconfigured with :attr:`current_token`.

        Return type is ``Any`` (rather than the concrete
        ``AsyncWebClient``) because ``slack_sdk`` is an optional
        dependency — consumers who install the SDK without the `slack`
        extra shouldn't pay a type-check-time import cost. Docstring
        captures the actual runtime type for tooling that reads it.

        The returned client is LRU-cached by token. Raises
        :class:`AuthenticationError` when no token is available.
        """
        return self._get_client()

    @property
    def web_client(self) -> Any:
        """Direct access to a synchronous ``slack_sdk.WebClient``.

        Bound to the bot token for the current request context
        (multi-workspace) or the configured default token
        (single-workspace). Use for any Slack Web API call not covered by
        the adapter's high-level methods — e.g.
        ``adapter.web_client.pins_add(...)`` or
        ``adapter.web_client.usergroups_list(...)``.

        Resolution order (the standard 3-level resolver):

        1. Token from the current request context (set during webhook
           handling, or by :meth:`with_bot_token` / :meth:`with_bot_token_async`).
        2. The default bot token, when configured as a static string or
           already-resolved value.
        3. Otherwise raise :class:`AuthenticationError`.

        Raises :class:`AuthenticationError` if neither is available —
        typical causes are accessing ``web_client`` outside any
        webhook / :meth:`with_bot_token` context in multi-workspace mode,
        or having configured ``bot_token`` as an async resolver that has
        not run yet. In the latter case await
        :meth:`current_token_async` (or process the work inside the
        webhook flow) so the resolver primes the token first.

        Return type is ``Any`` (rather than the concrete ``WebClient``)
        because ``slack_sdk`` is an optional dependency — consumers who do
        not install the ``slack`` extra should not pay an import cost.

        This is the direct port of upstream's ``adapter.webClient`` getter
        (vercel/chat ``2f108bd``). Unlike :attr:`current_client` it returns
        the *synchronous* ``WebClient`` (the analog of the single TS
        ``WebClient``), so its methods are not awaitables.
        """
        return self._get_web_client_for_token(self._get_token())

    @property
    def client(self) -> Any:
        """Deprecated alias for :attr:`web_client`.

        .. deprecated::
            Use :attr:`web_client` instead. This alias mirrors upstream's
            pre-rename ``adapter.client`` (vercel/chat ``8366b8b``) and is
            kept for one release for backwards compatibility; it will be
            removed in a future version. Emits :class:`DeprecationWarning`.
        """
        warnings.warn(
            "SlackAdapter.client is deprecated; use SlackAdapter.web_client instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.web_client

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    def _get_token(self) -> str:
        """Return the current bot token for API calls (sync path).

        Checks (in order):

        1. Multi-workspace request context (``_request_context``)
        2. Per-request resolved default token (``_resolved_default_token``)
           — primed by ``handle_webhook`` after invoking the resolver
        3. Sync dynamic resolver — invoked **fresh on every call** to honor
           the rotation contract in :attr:`SlackAdapterConfig.bot_token`
           ("called on each use to support rotation")
        4. Static default token cache (set by the constructor for
           string-typed ``bot_token`` configs, or by the async path after
           ``_resolve_default_token`` runs)
        5. Raises :class:`AuthenticationError`

        Async resolvers cannot be awaited from a sync context — sync access
        outside a webhook scope falls back to the process-wide cache, or
        raises if the async path has not run yet. Use
        :meth:`current_token_async` or enter via :meth:`handle_webhook` so
        the resolver runs first.
        """
        ctx = self._request_context.get()
        if ctx and ctx.token:
            return ctx.token
        per_request = self._resolved_default_token.get()
        if per_request is not None:
            return per_request
        # Sync dynamic resolver: invoke fresh every call to honor rotation.
        # Static strings (no rotation possible) and async resolvers (which
        # need a webhook entry to be awaited) fall through to the cache.
        provider = self._default_bot_token_provider
        if self._is_dynamic_bot_token and provider is not None and not inspect.iscoroutinefunction(provider):
            resolved = provider()
            # Defensive: a "sync" callable may still *return* a coroutine
            # (e.g. ``lambda: some_async_fn()``) and ``iscoroutinefunction``
            # would not catch that. Refuse to use such a value in a sync
            # context — and close the awaitable to suppress the
            # ``coroutine was never awaited`` RuntimeWarning before raising.
            if inspect.isawaitable(resolved):
                close = getattr(resolved, "close", None)
                if callable(close):
                    close()
                raise AuthenticationError(
                    "slack",
                    "Bot token resolver returned an awaitable in a sync "
                    "context. Use the async API (handle_webhook / "
                    "current_token_async) so the resolver can be awaited.",
                )
            if not isinstance(resolved, str) or not resolved:
                raise AuthenticationError(
                    "slack",
                    "Bot token resolver returned an empty or non-string value.",
                )
            # Intentionally do NOT write ``_default_bot_token_cache``: caching
            # would break the rotation contract for the next sync access.
            return resolved
        if self._default_bot_token_cache is not None:
            return self._default_bot_token_cache
        if provider is not None:
            # Async resolver configured but never awaited. ``handle_webhook``
            # or ``current_token_async`` must run first to prime the cache.
            raise AuthenticationError(
                "slack",
                "Async bot token resolver has not been invoked yet. Use the "
                "async API (handle_webhook / current_token_async) so the "
                "resolver runs first.",
            )
        raise AuthenticationError(
            "slack",
            "No bot token available. In multi-workspace mode, ensure the webhook is being processed.",
        )

    async def _resolve_token_async(self) -> str:
        """Async equivalent of :meth:`_get_token` that invokes the resolver.

        Calls the configured ``bot_token`` resolver (if any), refreshes the
        per-request cache used by :meth:`_get_token`, and returns the
        resulting token. Multi-workspace request context still wins.
        """
        ctx = self._request_context.get()
        if ctx and ctx.token:
            return ctx.token
        if self._default_bot_token_provider is not None:
            return await self._resolve_default_token()
        if self._default_bot_token_cache is not None:
            return self._default_bot_token_cache
        raise AuthenticationError(
            "slack",
            "No bot token available. In multi-workspace mode, ensure the webhook is being processed.",
        )

    async def _resolve_default_token(self) -> str:
        """Invoke the ``bot_token`` resolver and prime the per-request cache.

        Per upstream, the resolver is called *every time a token is needed*
        (it is the resolver's responsibility to memoize if desired) — this
        method does not memoize across calls within a process. The result is
        stashed in the per-request :attr:`_resolved_default_token` ContextVar
        so the sync ``_get_token`` path inside that request sees the value
        without re-invoking the resolver, while concurrent requests with
        their own ContextVar copies are isolated from each other.

        For static-string ``bot_token`` configs the resolver returns the
        same string each call, but we still update the per-request cache to
        keep the code path uniform.

        Raises :class:`AuthenticationError` if no resolver is configured.
        """
        provider = self._default_bot_token_provider
        if provider is None:
            raise AuthenticationError(
                "slack",
                "No default bot token resolver configured (multi-workspace mode).",
            )
        try:
            result = provider()
            if inspect.isawaitable(result):
                token = await result
            else:
                token = result
        except Exception as exc:
            self._logger.error("Bot token resolver raised", {"error": exc})
            raise
        if not isinstance(token, str) or not token:
            raise AuthenticationError(
                "slack",
                "Bot token resolver returned an empty or non-string value.",
            )
        # Refresh the process-wide cache so sync ``current_token`` /
        # ``current_client`` access outside the request ContextVar scope
        # (e.g. callers reading the most recently resolved token from a
        # different task) still observes the freshly resolved value.
        self._default_bot_token_cache = token
        # Per-request cache for downstream sync ``_get_token`` calls.
        self._resolved_default_token.set(token)
        return token

    @property
    def _is_single_workspace(self) -> bool:
        """``True`` when a default bot token (static or resolver) is configured.

        Multi-workspace mode is the absence of a default token, in which
        case tokens are resolved per-team via the InstallationStore.
        """
        return self._default_bot_token_provider is not None

    def _get_client(self, token: str | None = None) -> Any:
        """Return an ``AsyncWebClient`` for the given (or current) token.

        Clients are cached by token so we avoid creating a new instance on
        every request.  The import is deferred so that ``slack_sdk`` is only
        required at call-time.

        When *token* is explicitly passed (even as ``""``) it is used as-is;
        only when *token* is ``None`` do we fall back to ``_get_token()``.
        """
        resolved_token = self._get_token() if token is None else token

        if resolved_token in self._client_cache:
            self._client_cache.move_to_end(resolved_token)
            return self._client_cache[resolved_token]

        from slack_sdk.web.async_client import AsyncWebClient

        client = AsyncWebClient(token=resolved_token)
        self._client_cache[resolved_token] = client
        if len(self._client_cache) > self._client_cache_max:
            # Evict oldest (LRU).  We intentionally do NOT close the evicted
            # client's session here because other concurrent requests may still
            # hold a reference to the evicted AsyncWebClient instance.  The
            # underlying aiohttp.ClientSession will be closed by the garbage
            # collector (via __del__) once all references are released.
            self._client_cache.popitem(last=False)
        return client

    def _invalidate_client(self, token: str) -> None:
        """Remove a cached client (e.g., on token revocation).

        For dynamic-resolver configs also clears the resolved-token caches
        so the next access re-invokes the resolver instead of serving the
        revoked token. Static-string configs intentionally retain their
        cache: there is no refresh path, so clearing would just make every
        subsequent sync access raise.
        """
        self._client_cache.pop(token, None)
        self._web_client_cache.pop(token, None)
        if self._is_dynamic_bot_token:
            if self._default_bot_token_cache == token:
                self._default_bot_token_cache = None
            if self._resolved_default_token.get() == token:
                self._resolved_default_token.set(None)

    def _get_web_client_for_token(self, token: str) -> Any:
        """Return a synchronous ``slack_sdk.WebClient`` for *token*, cached.

        Backs the public :attr:`web_client` property and is the direct port
        of upstream's ``getClientForToken`` (vercel/chat ``2f108bd``): one
        cached ``WebClient`` instance per distinct token. The import is
        deferred so ``slack_sdk`` stays an optional dependency (hazard #10).

        Distinct from :meth:`_get_client`, which caches the *async*
        ``AsyncWebClient`` used by the adapter's own API calls.
        """
        client = self._web_client_cache.get(token)
        if client is None:
            from slack_sdk import WebClient

            client = WebClient(token=token)
            self._web_client_cache[token] = client
        return client

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    async def initialize(self, chat: ChatInstance) -> None:
        """Initialize the adapter and optionally fetch bot identity."""
        self._chat = chat

        # Single-workspace: fetch bot user ID via auth.test. We resolve the
        # bot token via the resolver here so dynamic resolvers work at init
        # time without forcing the caller to seed a static token first.
        if self._is_single_workspace and not self._bot_user_id:
            try:
                token = await self._resolve_default_token()
                client = self._get_client(token)
                auth_result = await client.auth_test()
                self._bot_user_id = auth_result.get("user_id")
                self._bot_id = auth_result.get("bot_id") or None
                user = auth_result.get("user")
                if user:
                    self._user_name = user
                self._logger.info(
                    "Slack auth completed",
                    {"botUserId": self._bot_user_id, "botId": self._bot_id},
                )
            except Exception as exc:
                self._logger.warn("Could not fetch bot user ID", {"error": exc})

        if not self._is_single_workspace:
            self._logger.info("Slack adapter initialized in multi-workspace mode")

        if self._mode == "socket":
            await self.start_socket_mode()

    async def disconnect(self) -> None:
        """Close any persistent connections held by the adapter.

        In webhook mode this is a no-op. In socket mode it cancels the
        background reconnect loop, closes the active ``SocketModeClient``,
        and waits for the loop to settle. Idempotent — calling it twice or
        before ``initialize()`` is safe.
        """
        await self.stop_socket_mode()

    # ==================================================================
    # Multi-workspace installation management
    # ==================================================================

    def _installation_key(self, team_id: str) -> str:
        return f"{self._installation_key_prefix}:{team_id}"

    async def set_installation(self, team_id: str, installation: SlackInstallation) -> None:
        """Save a workspace installation (call from your OAuth callback)."""
        if not self._chat:
            raise ValidationError(
                "slack",
                "Adapter not initialized. Ensure chat.initialize() has been called first.",
            )

        state = self._chat.get_state()
        key = self._installation_key(team_id)

        if self._encryption_key:
            encrypted = encrypt_token(installation.bot_token, self._encryption_key)
            data_to_store: dict[str, Any] = {
                "botToken": {
                    "iv": encrypted.iv,
                    "data": encrypted.data,
                    "tag": encrypted.tag,
                },
                "botUserId": installation.bot_user_id,
                "teamName": installation.team_name,
            }
        else:
            data_to_store = {
                "botToken": installation.bot_token,
                "botUserId": installation.bot_user_id,
                "teamName": installation.team_name,
            }

        await state.set(key, data_to_store)
        self._logger.info(
            "Slack installation saved",
            {"teamId": team_id, "teamName": installation.team_name},
        )

    async def get_installation(self, team_id: str) -> SlackInstallation | None:
        """Retrieve a workspace installation."""
        if not self._chat:
            raise ValidationError(
                "slack",
                "Adapter not initialized. Ensure chat.initialize() has been called first.",
            )

        state = self._chat.get_state()
        key = self._installation_key(team_id)
        stored = await state.get(key)

        if not stored:
            return None

        bot_token_raw = (stored.get("botToken") or stored.get("bot_token")) if isinstance(stored, dict) else None
        bot_user_id = (stored.get("botUserId") or stored.get("bot_user_id") or "") if isinstance(stored, dict) else ""
        team_name = (stored.get("teamName") or stored.get("team_name") or "") if isinstance(stored, dict) else ""
        if self._encryption_key and is_encrypted_token_data(bot_token_raw):
            # `is_encrypted_token_data` is a runtime type guard but doesn't
            # carry TypeGuard narrowing, so pyrefly still sees `None`. Assert
            # to collapse the Optional for the field access below.
            assert bot_token_raw is not None
            decrypted = decrypt_token(
                EncryptedTokenData(
                    iv=bot_token_raw["iv"],
                    data=bot_token_raw["data"],
                    tag=bot_token_raw["tag"],
                ),
                self._encryption_key,
            )
            return SlackInstallation(
                bot_token=decrypted,
                bot_user_id=bot_user_id,
                team_name=team_name,
            )

        return SlackInstallation(
            bot_token=bot_token_raw if isinstance(bot_token_raw, str) else "",
            bot_user_id=bot_user_id,
            team_name=team_name,
        )

    async def handle_oauth_callback(
        self,
        request: Any,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Handle the Slack OAuth V2 callback.

        Args:
            request: The incoming HTTP request containing the OAuth callback.
            options: Optional dict with ``redirect_uri`` key to send to Slack
                during the code exchange. When provided it takes priority over
                any ``redirect_uri`` query parameter in the callback URL.

        Returns ``{"team_id": ..., "installation": SlackInstallation}``.
        """
        if not (self._client_id and self._client_secret):
            raise ValidationError(
                "slack",
                "client_id and client_secret are required for OAuth. Pass them in create_slack_adapter().",
            )

        # Extract query params from request
        url: str = getattr(request, "url", "")
        if isinstance(url, str) and "?" in url:
            query = dict(parse_qs(url.split("?", 1)[1]))
            code = query.get("code", [None])[0] if isinstance(query.get("code"), list) else query.get("code")
            query_redirect_uri = (
                query.get("redirect_uri", [None])[0]
                if isinstance(query.get("redirect_uri"), list)
                else query.get("redirect_uri")
            )
        else:
            code = None
            query_redirect_uri = None

        if not code:
            raise ValidationError(
                "slack",
                "Missing 'code' query parameter in OAuth callback request.",
            )

        # Options redirect_uri takes priority over the query param
        redirect_uri = (options or {}).get("redirect_uri") or query_redirect_uri

        client = self._get_client("")
        kwargs: dict[str, Any] = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "code": code,
        }
        if redirect_uri:
            kwargs["redirect_uri"] = redirect_uri
        result = await client.oauth_v2_access(**kwargs)

        if not (result.get("ok") and result.get("access_token") and result.get("team", {}).get("id")):
            raise AuthenticationError(
                "slack",
                f"Slack OAuth failed: {result.get('error') or 'missing access_token or team.id'}",
            )

        team_id = result["team"]["id"]
        installation = SlackInstallation(
            bot_token=result["access_token"],
            bot_user_id=result.get("bot_user_id"),
            team_name=result.get("team", {}).get("name"),
        )

        await self.set_installation(team_id, installation)
        return {"team_id": team_id, "installation": installation}

    async def delete_installation(self, team_id: str) -> None:
        """Remove a workspace installation."""
        if not self._chat:
            raise ValidationError(
                "slack",
                "Adapter not initialized. Ensure chat.initialize() has been called first.",
            )
        state = self._chat.get_state()
        await state.delete(self._installation_key(team_id))
        self._logger.info("Slack installation deleted", {"teamId": team_id})

    def with_bot_token(self, token: str, fn: Callable[[], Any]) -> Any:
        """Run *fn* with a specific bot token in context (for cron jobs, etc.)."""
        tok = self._request_context.set(RequestContext(token=token))
        try:
            return fn()
        finally:
            self._request_context.reset(tok)

    async def with_bot_token_async(self, token: str, fn: Callable[[], Awaitable[Any]]) -> Any:
        """Run an async function with a specific bot token in context."""
        tok = self._request_context.set(RequestContext(token=token))
        try:
            return await fn()
        finally:
            self._request_context.reset(tok)

    # ==================================================================
    # Private helpers - token resolution
    # ==================================================================

    async def _resolve_token_for_team(self, team_id: str) -> RequestContext | None:
        """Resolve the bot token for a team from the state adapter."""
        try:
            installation = await self.get_installation(team_id)
            if installation:
                return RequestContext(
                    token=installation.bot_token,
                    bot_user_id=installation.bot_user_id,
                )
            self._logger.warn("No installation found for team", {"teamId": team_id})
            return None
        except Exception as exc:
            self._logger.error("Failed to resolve token for team", {"teamId": team_id, "error": exc})
            return None

    def _extract_team_id_from_interactive(self, body: str) -> str | None:
        """Extract team_id from an interactive payload (form-urlencoded)."""
        try:
            params = parse_qs(body)
            payload_str = params.get("payload", [None])[0]
            if not payload_str:
                return None
            payload = json.loads(payload_str)
            return payload.get("team", {}).get("id") or payload.get("team_id")
        except Exception:
            return None

    # ==================================================================
    # User / Channel lookup with caching
    # ==================================================================

    async def _lookup_user(self, user_id: str) -> SlackUserCacheEntry:
        """Look up user info from Slack API with caching.

        Returns a dict with keys ``display_name``, ``real_name``, and
        (when available from the Slack API or from a cached entry) the
        optional fields ``email``, ``avatar_url``, ``is_bot``.

        On API failure — or when the API returns success but with an
        empty/missing ``user`` payload — the returned dict is a fallback
        shape (``display_name`` / ``real_name`` populated with the user
        ID) and carries the private ``_lookup_failed: True`` sentinel so
        callers that need to distinguish "really not found" from "fall
        back to ID" — like :meth:`get_user` — can return ``None``
        instead. The fallback entry is **not** cached so a subsequent
        call retries the lookup.
        """
        cache_key = f"slack:user:{user_id}"

        if self._chat:
            cached = await self._chat.get_state().get(cache_key)
            if cached and isinstance(cached, dict):
                return {
                    "display_name": cached.get("display_name", user_id),
                    "real_name": cached.get("real_name", user_id),
                    "email": cached.get("email"),
                    "avatar_url": cached.get("avatar_url"),
                    "is_bot": cached.get("is_bot"),
                }

        try:
            client = self._get_client()
            result = await client.users_info(user=user_id)
            user = result.get("user") or {}
            # Slack can return `{"ok": True, "user": {}}` in some edge cases
            # (rare, but observed when scopes are partial or the workspace
            # rejects the lookup post-success). Treat a missing/empty user
            # payload as a lookup failure so we don't poison the cache
            # with a `UserInfo("Uxxx", "Uxxx", "Uxxx")` shape that
            # `get_user` would then convert into a non-null fallback —
            # diverging from the null-on-failure contract callers expect.
            if not user:
                self._logger.warn(
                    "Slack users.info returned empty user payload",
                    {"userId": user_id},
                )
                return _make_slack_lookup_failed(user_id)
            profile = user.get("profile", {})

            display_name = (
                profile.get("display_name")
                or profile.get("real_name")
                or user.get("real_name")
                or user.get("name")
                or user_id
            )
            real_name = user.get("real_name") or profile.get("real_name") or display_name
            email = profile.get("email")
            # Upstream chose `image_192` (vs the older `image_72`) for
            # better avatar quality — see vercel/chat#391.
            avatar_url = profile.get("image_192")
            is_bot = user.get("is_bot")

            cached_entry: SlackUserCacheEntry = {
                "display_name": display_name,
                "real_name": real_name,
                "email": email,
                "avatar_url": avatar_url,
                "is_bot": is_bot,
            }

            if self._chat:
                await self._chat.get_state().set(
                    cache_key,
                    cached_entry,
                    _USER_CACHE_TTL_MS,
                )
                # Reverse index: display name -> user IDs
                normalized_name = display_name.lower()
                reverse_key = f"slack:user-by-name:{normalized_name}"
                existing = await self._chat.get_state().get_list(reverse_key)
                if user_id not in existing:
                    await self._chat.get_state().append_to_list(
                        reverse_key,
                        user_id,
                        max_length=50,
                        ttl_ms=_REVERSE_INDEX_TTL_MS,
                    )

            self._logger.debug(
                "Fetched user info",
                {"userId": user_id, "displayName": display_name, "realName": real_name},
            )
            return cached_entry
        except Exception as exc:
            self._logger.warn("Could not fetch user info", {"userId": user_id, "error": exc})
            # Keep the fallback dict shape so existing callers (mention
            # resolution, slash command author binding, message parsing)
            # don't change behavior on transient lookup failures — they
            # already used `display_name`/`real_name` and would have
            # received the user ID either way. The private sentinel lets
            # `get_user` distinguish "API failed" from "API returned data".
            return _make_slack_lookup_failed(user_id)

    async def _lookup_channel(self, channel_id: str) -> str:
        """Look up channel name from Slack API with caching."""
        cache_key = f"slack:channel:{channel_id}"

        if self._chat:
            cached = await self._chat.get_state().get(cache_key)
            if cached and isinstance(cached, dict):
                return cached.get("name", channel_id)

        try:
            client = self._get_client()
            result = await client.conversations_info(channel=channel_id)
            channel = result.get("channel", {})
            name = channel.get("name", channel_id)

            if self._chat:
                await self._chat.get_state().set(cache_key, {"name": name}, _CHANNEL_CACHE_TTL_MS)

            self._logger.debug("Fetched channel info", {"channelId": channel_id, "name": name})
            return name
        except Exception as exc:
            self._logger.warn("Could not fetch channel info", {"channelId": channel_id, "error": exc})
            return channel_id

    # ==================================================================
    # Public user lookup (chat.get_user)
    # ==================================================================

    async def get_user(self, user_id: str) -> UserInfo | None:
        """Look up Slack user info via ``users.info``.

        Returns ``None`` when the Slack API call fails (network error,
        rate limit, missing scopes, unknown user). ``email`` requires the
        ``users:read.email`` scope; ``avatar_url`` is the high-quality
        ``image_192`` from the user's Slack profile.

        Resolves the bot token via :meth:`_resolve_token_async` first so
        callable ``bot_token`` resolvers work outside ``handle_webhook``
        (cron jobs, background tasks) without forcing the caller to seed
        the per-request cache themselves. Static-string ``bot_token`` and
        in-webhook flow are unaffected (the resolver path is a no-op when
        the per-request cache is already primed).

        Mirrors upstream ``SlackAdapter.getUser`` (vercel/chat#391).
        """
        try:
            # Prime the per-request token cache so the sync `_get_token`
            # path inside `_lookup_user` -> `_get_client` finds a value
            # even when called outside `handle_webhook` with a callable
            # `bot_token` resolver. Mirrors the resolver pattern used by
            # other public adapter methods reachable from background
            # contexts (see #87 / docs/UPSTREAM_SYNC.md "bot_token
            # resolver invocation site" row).
            await self._resolve_token_async()
        except AuthenticationError:
            return None
        try:
            cached = await self._lookup_user(user_id)
        except Exception:
            return None
        if cached.get("_lookup_failed"):
            return None
        return UserInfo(
            user_id=user_id,
            user_name=cached.get("display_name") or user_id,
            full_name=cached.get("real_name") or user_id,
            is_bot=bool(cached.get("is_bot")) if cached.get("is_bot") is not None else False,
            email=cached.get("email"),
            avatar_url=cached.get("avatar_url"),
        )

    # ==================================================================
    # Webhook handling
    # ==================================================================

    async def handle_webhook(self, request: Any, options: WebhookOptions | None = None) -> dict[str, Any]:
        """Handle incoming webhooks from Slack.

        Handles URL verification, event callbacks, interactive payloads,
        and slash commands.

        Returns a dict with ``body`` and ``status`` keys.
        """
        # Read the raw body. `hasattr` narrows `Any` → `object` (not
        # awaitable), so we use `getattr(..., None)` to preserve the
        # `Any` type across the duck-typed framework branches.
        # Handle both callable (`async def text(self)`) and non-callable
        # (`text: str` attribute) forms of `request.text`. Gating entry
        # on callability would drop populated string attributes.
        text_attr = getattr(request, "text", None)
        body: str
        if text_attr is not None:
            if callable(text_attr):
                result = text_attr()
                text_attr = await result if inspect.isawaitable(result) else result
            body = text_attr.decode("utf-8") if isinstance(text_attr, (bytes, bytearray)) else str(text_attr)
        else:
            raw = getattr(request, "body", None)
            if raw is not None:
                # Some frameworks expose `body` as an async method (e.g.
                # `async def body(self)`) — call it, then await if the
                # result is awaitable. Previously we only handled the
                # coroutine-as-attribute case, not the async-method case.
                if callable(raw):
                    raw = raw()
                if asyncio.iscoroutine(raw) or asyncio.isfuture(raw) or inspect.isawaitable(raw):
                    raw = await raw
                body = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
            else:
                body = str(request)

        self._logger.debug("Slack webhook raw body", {"body": body[:500]})

        # Extract headers
        headers = getattr(request, "headers", {})

        # Forwarded socket-mode events bypass Slack signature verification —
        # they're authenticated by a shared bearer secret instead. This lets a
        # separate process run the WebSocket and POST events back to the
        # webhook endpoint over HTTP. Handled BEFORE any signature / verifier /
        # resolver work because the auth model is entirely different: an
        # ``x-slack-socket-token`` request never carries a Slack signature
        # and shouldn't pay the resolver-call cost (or surface resolver
        # failures) when its bearer is invalid. Hazard #12: refuse if no
        # secret is configured rather than treating an empty header match as
        # success.
        socket_token = headers.get("x-slack-socket-token") or headers.get("X-Slack-Socket-Token")
        if socket_token:
            if not self._socket_forwarding_secret or not hmac.compare_digest(
                socket_token, self._socket_forwarding_secret
            ):
                self._logger.warn("Invalid socket forwarding token")
                return {"body": "Invalid socket token", "status": 401}
            try:
                event = json.loads(body)
            except (json.JSONDecodeError, ValueError):
                return {"body": "Invalid JSON", "status": 400}
            # Hazard #12 (replay): the shared bearer alone is not enough —
            # without a freshness check, an old captured forwarded event
            # could be replayed indefinitely. Mirror the 5-minute window
            # ``_verify_signature`` enforces on signed webhook traffic.
            #
            # Wire format: upstream's ``forwardSocketEvent`` always emits
            # ``timestamp: Date.now()`` — milliseconds since the Unix epoch
            # (~1.78e12 today). Python's ``time.time()`` returns seconds.
            # Auto-detect the unit by magnitude (anything > 10**11 is
            # certainly milliseconds — that crossed in 2001) so we accept
            # both the JS-emitted ms shape AND a Python-emitted seconds
            # shape if a future ``forward_socket_event`` listener lands.
            ts_raw = event.get("timestamp") if isinstance(event, dict) else None
            try:
                ts_int = int(ts_raw) if ts_raw is not None else None
            except (TypeError, ValueError):
                ts_int = None
            ts_seconds = ts_int // 1000 if ts_int is not None and ts_int > 10**11 else ts_int
            if ts_seconds is None or abs(int(time.time()) - ts_seconds) > 300:
                self._logger.warn(
                    "Forwarded socket event outside freshness window",
                    {"timestamp": ts_raw},
                )
                return {"body": "Stale socket event", "status": 401}
            await self._handle_forwarded_socket_event(event, options)
            return {"body": "ok", "status": 200}

        # In socket mode, refuse direct webhook POSTs — Slack delivers events
        # over the WebSocket instead. We still allow forwarded events above.
        if self._mode == "socket":
            return {"body": "Webhooks are disabled in socket mode", "status": 405}

        # Verify the request — when a custom ``webhook_verifier`` is configured
        # it takes precedence over ``signing_secret`` / ``SLACK_SIGNING_SECRET``
        # (matches upstream vercel/chat#468). The verifier may also return a
        # string that replaces the body for downstream parsing (e.g.
        # canonicalization).
        if self._webhook_verifier is not None:
            try:
                verifier_result = self._webhook_verifier(request, body)
                if inspect.isawaitable(verifier_result):
                    verifier_result = await verifier_result
            except Exception as exc:
                self._logger.warn("Webhook verifier rejected request", {"error": exc})
                return {"body": "Invalid signature", "status": 401}
            if not verifier_result:
                self._logger.warn("Webhook verifier rejected request")
                return {"body": "Invalid signature", "status": 401}
            if isinstance(verifier_result, str):
                # Substitute the verifier-supplied canonical body before
                # parsing.  Other truthy returns are pure verification.
                body = verifier_result
        else:
            timestamp = headers.get("x-slack-request-timestamp") or headers.get("X-Slack-Request-Timestamp")
            signature = headers.get("x-slack-signature") or headers.get("X-Slack-Signature")
            if not self._verify_signature(body, timestamp, signature):
                return {"body": "Invalid signature", "status": 401}

        # URL verification is special: Slack sends a JSON ``url_verification``
        # ping at app-install / event-subscription time and only expects the
        # ``challenge`` echo. No token / API call is required — so resolve
        # the JSON-payload short-circuit BEFORE invoking the bot-token
        # resolver. A broken or down resolver (secrets manager outage, key
        # rotation in flight) must NOT prevent URL verification from
        # succeeding — that would block app installation and re-subscription.
        # Mirrors upstream parity where ``getToken`` is only called at
        # per-API-call sites, not at webhook entry.
        content_type = headers.get("content-type") or headers.get("Content-Type") or ""
        early_payload: dict[str, Any] | None = None
        if "application/json" in content_type or (not content_type and body and body.lstrip().startswith(("{", "["))):
            try:
                maybe = json.loads(body)
                if isinstance(maybe, dict):
                    early_payload = maybe
            except (json.JSONDecodeError, ValueError):
                early_payload = None
            if (
                early_payload is not None
                and early_payload.get("type") == "url_verification"
                and early_payload.get("challenge")
            ):
                return {
                    "body": json.dumps({"challenge": early_payload["challenge"]}),
                    "status": 200,
                    "headers": {"Content-Type": "application/json"},
                }

        # Resolve the default bot token via the (possibly async) resolver
        # before dispatching, so synchronous ``_get_token`` call sites
        # downstream see a primed cache. For static-string configs this is
        # already primed at construction, so the resolver is a no-op identity
        # closure. For dynamic resolvers we re-resolve on every webhook entry
        # so rotation is observed.
        if self._is_single_workspace:
            try:
                await self._resolve_default_token()
            except Exception as exc:
                # Resolver failures here would manifest as auth errors deeper
                # in dispatch; surface them at the entry point so the caller
                # gets a clean 500 instead of partial processing. Re-raise.
                self._logger.error("Bot token resolver failed", {"error": exc})
                raise

        # Form-urlencoded payloads (interactive + slash commands)
        if "application/x-www-form-urlencoded" in content_type:
            params = parse_qs(body, keep_blank_values=True)

            # Slash command
            if "command" in params and "payload" not in params:
                team_id = (params.get("team_id") or [None])[0]
                if not self._is_single_workspace and team_id:
                    ctx = await self._resolve_token_for_team(team_id)
                    if ctx:
                        tok = self._request_context.set(ctx)
                        try:
                            return await self._handle_slash_command(params, options)
                        finally:
                            self._request_context.reset(tok)
                    self._logger.warn("Could not resolve token for slash command")
                return await self._handle_slash_command(params, options)

            # Interactive payload
            if not self._is_single_workspace:
                team_id_interactive = self._extract_team_id_from_interactive(body)
                if team_id_interactive:
                    ctx = await self._resolve_token_for_team(team_id_interactive)
                    if ctx:
                        tok = self._request_context.set(ctx)
                        try:
                            return await self._handle_interactive_payload(body, options)
                        finally:
                            self._request_context.reset(tok)
                self._logger.warn("Could not resolve token for interactive payload")
            return await self._handle_interactive_payload(body, options)

        # JSON payload
        try:
            payload: dict[str, Any] = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return {"body": "Invalid JSON", "status": 400}

        # URL verification challenge
        if payload.get("type") == "url_verification" and payload.get("challenge"):
            return {
                "body": json.dumps({"challenge": payload["challenge"]}),
                "status": 200,
                "headers": {"Content-Type": "application/json"},
            }

        # Multi-workspace: resolve token before processing events.
        # Use contextvars.copy_context() so the ContextVar value persists into
        # any async tasks spawned by _process_event_payload (e.g. process_message
        # creates a task via asyncio.create_task).  The copied context is
        # isolated -- the ContextVar change does not leak back to the caller
        # and does not need an explicit reset.
        if not self._is_single_workspace and payload.get("type") == "event_callback":
            team_id_event = payload.get("team_id")
            if team_id_event:
                ctx = await self._resolve_token_for_team(team_id_event)
                if ctx:
                    isolated = contextvars.copy_context()
                    isolated.run(self._request_context.set, ctx)
                    isolated.run(self._process_event_payload, payload, options)
                    return {"body": "ok", "status": 200}
                self._logger.warn("Could not resolve token for team", {"teamId": team_id_event})
                return {"body": "ok", "status": 200}

        # Single-workspace mode or fallback
        self._process_event_payload(payload, options)
        return {"body": "ok", "status": 200}

    # ==================================================================
    # Signature verification
    # ==================================================================

    def _verify_signature(self, body: str, timestamp: str | None, signature: str | None) -> bool:
        # Defensive: ``_verify_signature`` should never be reached when only a
        # ``webhook_verifier`` is configured (handle_webhook gates on that),
        # but if a subclass calls this directly without a signing_secret,
        # fail closed. This also covers the socket-mode case where
        # ``signing_secret`` is legitimately ``None`` — without this guard a
        # caller could call ``handle_webhook`` while in socket mode and
        # silently pass verification with an empty secret.
        if not self._signing_secret:
            return False

        if not (timestamp and signature):
            return False

        # Check timestamp is recent (within 5 minutes)
        now = int(time.time())
        try:
            ts_int = int(timestamp)
        except (ValueError, TypeError):
            return False
        if abs(now - ts_int) > 300:
            return False

        sig_basestring = f"v0:{timestamp}:{body}"
        expected = (
            "v0="
            + hmac.new(
                self._signing_secret.encode("utf-8"),
                sig_basestring.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
        )

        try:
            # ``hmac.compare_digest`` is the canonical constant-time comparison.
            # Custom verifiers passed via ``webhook_verifier`` MUST do the
            # same — a regression to ``==`` would leak signature bytes via
            # timing.
            return hmac.compare_digest(signature, expected)
        except Exception:
            return False

    # ==================================================================
    # Event dispatch
    # ==================================================================

    def _process_event_payload(self, payload: dict[str, Any], options: WebhookOptions | None = None) -> None:
        """Extract and dispatch events from a validated payload."""
        if payload.get("type") != "event_callback" or not payload.get("event"):
            return

        event: dict[str, Any] = payload["event"]

        # Track external/shared channel status. Note: socket-mode payloads
        # synthesized in ``_route_socket_event`` never carry this field, which
        # mirrors upstream's ``routeSocketEvent`` shape. Socket-mode adapters
        # therefore won't populate ``_external_channels`` from this path —
        # documented as a known divergence in ``docs/UPSTREAM_SYNC.md``.
        if payload.get("is_ext_shared_channel"):
            channel_id = event.get("channel") or (event.get("item", {}).get("channel") if "item" in event else None)
            if channel_id:
                self._external_channels.add(channel_id)

        event_type = event.get("type", "")

        if event_type in ("message", "app_mention"):
            if not (event.get("team") or event.get("team_id")) and payload.get("team_id"):
                event["team_id"] = payload["team_id"]
            self._handle_message_event(event, options)
        elif event_type in ("reaction_added", "reaction_removed"):
            self._handle_reaction_event(event, options)
        elif event_type == "assistant_thread_started":
            self._handle_assistant_thread_started(event, options)
        elif event_type == "assistant_thread_context_changed":
            self._handle_assistant_context_changed(event, options)
        elif event_type == "app_home_opened" and event.get("tab") == "home":
            self._handle_app_home_opened(event, options)
        elif event_type == "member_joined_channel":
            self._handle_member_joined_channel(event, options)
        elif event_type == "user_change":
            self._handle_user_change(event)

    # ==================================================================
    # Interactive payloads
    # ==================================================================

    async def _handle_interactive_payload(self, body: str, options: WebhookOptions | None = None) -> dict[str, Any]:
        params = parse_qs(body, keep_blank_values=True)
        payload_str = (params.get("payload") or [None])[0]

        if not payload_str:
            return {"body": "Missing payload", "status": 400}

        try:
            payload: dict[str, Any] = json.loads(payload_str)
        except (json.JSONDecodeError, ValueError):
            return {"body": "Invalid payload JSON", "status": 400}

        return await self._dispatch_interactive_payload(payload, options)

    async def _dispatch_interactive_payload(
        self,
        payload: dict[str, Any],
        options: WebhookOptions | None = None,
    ) -> dict[str, Any]:
        """Dispatch a pre-parsed interactive payload to the right handler.

        Used by both the webhook path (after form-decoding) and the socket
        mode path (which receives the payload as a JSON object directly).
        """
        payload_type = payload.get("type")

        if payload_type == "block_actions":
            self._handle_block_actions(payload, options)
            return {"body": "", "status": 200}
        elif payload_type == "block_suggestion":
            return await self._handle_block_suggestion(payload, options)
        elif payload_type == "view_submission":
            return await self._handle_view_submission(payload, options)
        elif payload_type == "view_closed":
            self._handle_view_closed(payload, options)
            return {"body": "", "status": 200}

        return {"body": "", "status": 200}

    # ==================================================================
    # Slash commands
    # ==================================================================

    async def _handle_slash_command(
        self,
        params: dict[str, list[str]],
        options: WebhookOptions | None = None,
    ) -> dict[str, Any]:
        if not self._chat:
            self._logger.warn("Chat instance not initialized, ignoring slash command")
            return {"body": "", "status": 200}

        command = (params.get("command") or [""])[0]
        text = (params.get("text") or [""])[0]
        user_id = (params.get("user_id") or [""])[0]
        channel_id = (params.get("channel_id") or [""])[0]
        trigger_id = (params.get("trigger_id") or [None])[0]

        self._logger.debug(
            "Processing Slack slash command",
            {"command": command, "text": text, "userId": user_id, "channelId": channel_id},
        )
        user_info = await self._lookup_user(user_id)
        event = SlashCommandEvent(
            command=command,
            text=text,
            user=Author(
                user_id=user_id,
                user_name=user_info["display_name"],
                full_name=user_info["real_name"],
                is_bot=False,
                is_me=False,
            ),
            adapter=self,
            channel=None,  # pyrefly: ignore[bad-argument-type]  # filled in by Chat
            raw={k: v[0] for k, v in params.items()} if params else {},
            trigger_id=trigger_id,
        )
        # Attach channel_id so chat.py can build a ChannelImpl
        event.channel_id = f"slack:{channel_id}" if channel_id else ""  # type: ignore[attr-defined]
        self._chat.process_slash_command(event, options)
        return {"body": "", "status": 200}

    # ==================================================================
    # Block actions
    # ==================================================================

    def _handle_block_actions(self, payload: dict[str, Any], options: WebhookOptions | None = None) -> None:
        if not self._chat:
            self._logger.warn("Chat instance not initialized, ignoring action")
            return

        channel = (payload.get("channel") or {}).get("id") or (payload.get("container") or {}).get("channel_id")
        message_ts = (payload.get("message") or {}).get("ts") or (payload.get("container") or {}).get("message_ts")
        thread_ts = (
            (payload.get("message") or {}).get("thread_ts")
            or (payload.get("container") or {}).get("thread_ts")
            or message_ts
        )

        is_view_action = (payload.get("container") or {}).get("type") == "view"

        if not (is_view_action or channel):
            self._logger.warn("Missing channel in block_actions", {"channel": channel})
            return

        thread_id = ""
        if channel and (thread_ts or message_ts):
            thread_id = self.encode_thread_id(SlackThreadId(channel=channel, thread_ts=thread_ts or message_ts or ""))

        is_ephemeral = (payload.get("container") or {}).get("is_ephemeral") is True
        response_url = payload.get("response_url")
        user_ref = payload.get("user") or {}
        message_id: str
        if is_ephemeral and response_url and message_ts:
            message_id = self._encode_ephemeral_message_id(message_ts, response_url, user_ref.get("id", ""))
        else:
            message_id = message_ts or ""

        for action in payload.get("actions", []):
            action_value = (action.get("selected_option") or {}).get("value") or action.get("value")
            action_event = ActionEvent(
                action_id=action.get("action_id", ""),
                value=action_value,
                user=Author(
                    user_id=user_ref.get("id", ""),
                    user_name=user_ref.get("username") or user_ref.get("name") or "unknown",
                    full_name=user_ref.get("name") or user_ref.get("username") or "unknown",
                    is_bot=False,
                    is_me=False,
                ),
                message_id=message_id,
                thread_id=thread_id,
                thread=None,  # pyrefly: ignore[bad-argument-type]  # filled in by Chat
                adapter=self,
                raw=payload,
                trigger_id=payload.get("trigger_id"),
            )

            self._logger.debug(
                "Processing Slack block action",
                {
                    "actionId": action.get("action_id"),
                    "value": action.get("value"),
                    "messageId": message_ts,
                    "threadId": thread_id,
                    "triggerId": payload.get("trigger_id"),
                },
            )
            self._chat.process_action(action_event, options)

    # ==================================================================
    # Block suggestion (external-select options load)
    # ==================================================================

    async def _handle_block_suggestion(
        self, payload: dict[str, Any], options: WebhookOptions | None = None
    ) -> dict[str, Any]:
        """Handle a Slack block_suggestion interactive payload.

        Slack requires a response within 3s for block_suggestion and does not
        support an async ack pattern — options must be in the response body.
        Race the handler against a 2.5s budget and fall back to an empty 200
        so the menu shows "No results" instead of hanging for the user.
        """
        if not self._chat:
            self._logger.warn("Chat instance not initialized, ignoring block suggestion")
            return self._options_load_response([])

        user_ref = payload.get("user") or {}
        user_id = user_ref.get("id", "")
        username = user_ref.get("username")
        name = user_ref.get("name")
        # Upstream uses `||` truthy-fallthrough intentionally: an empty-string
        # username falls through to name, then user_id. See upstream
        # packages/adapter-slack/src/index.ts lines ~1258-1260.
        user_name = username or name or user_id
        full_name = name or username or user_id

        action_id = payload.get("action_id", "")
        val = payload.get("value")
        event = OptionsLoadEvent(
            action_id=action_id,
            query=val if val is not None else "",
            user=Author(
                user_id=user_id,
                user_name=user_name,
                full_name=full_name,
                is_bot=False,
                is_me=False,
            ),
            adapter=self,
            raw=payload,
        )

        # Use asyncio.shield so the orphaned task still runs (and logs errors)
        # if we time out. `wait_for` cancels the awaitable on timeout; shielding
        # prevents that cancellation from propagating into the handler task.
        # Use asyncio.ensure_future — process_options_load is typed as returning
        # Awaitable (matching sibling process_* methods on the ChatInstance
        # Protocol); create_task() would require narrowing to Coroutine.
        load_task = asyncio.ensure_future(self._chat.process_options_load(event, options))

        try:
            result = await asyncio.wait_for(asyncio.shield(load_task), timeout=OPTIONS_LOAD_TIMEOUT_MS / 1000.0)
        except asyncio.TimeoutError:
            self._logger.warn(
                "Options load handler timed out",
                {"action_id": action_id, "timeout_ms": OPTIONS_LOAD_TIMEOUT_MS},
            )

            def _late_error(t: asyncio.Task[Any]) -> None:
                if t.cancelled():
                    return
                exc = t.exception()
                if exc is not None:
                    self._logger.error(
                        "Options load handler error after timeout",
                        {"action_id": action_id, "error": str(exc)},
                    )

            load_task.add_done_callback(_late_error)
            _pin_task(load_task)
            # Register with wait_until so serverless/webhook runtimes
            # (e.g. Vercel) keep the task alive past the HTTP response;
            # otherwise the late-error logging path above can be killed
            # before it runs. wait_until is user/runtime-provided, so
            # guard against it raising — we still want to return the
            # empty-options HTTP 200 fallback.
            if options and options.wait_until:
                try:
                    options.wait_until(load_task)
                except Exception as err:
                    self._logger.warn(
                        "wait_until raised while registering timed-out options load task",
                        {"action_id": action_id, "error": str(err)},
                    )
            return self._options_load_response([])

        return self._options_load_response(result if result is not None else [])

    def _options_load_response(
        self,
        result: list[SelectOptionElement] | list[OptionsLoadGroup],
    ) -> dict[str, Any]:
        """Serialize a flat option list or grouped option list to a Slack JSON response.

        Mirrors upstream ``optionsLoadResponse``: when the first entry has an
        ``options`` key it is treated as a list of :class:`OptionsLoadGroup`
        and rendered as ``option_groups``; otherwise it's a flat list of
        :class:`SelectOptionElement` rendered as ``options``. Slack's spec is
        explicit that the two are mutually exclusive (only one may appear in
        the response body).
        """
        # Detect grouped form (TS: ``"options" in result[0]``). A grouped
        # entry is a dict with an ``options`` list inside it; a flat entry is
        # a dict with ``label``/``value`` keys.
        is_groups = (
            len(result) > 0
            and isinstance(result[0], dict)
            and "options" in result[0]
            and isinstance(result[0].get("options"), list)
        )

        if is_groups:
            groups_in = cast("list[OptionsLoadGroup]", result)[:100]
            slack_groups: list[dict[str, Any]] = []
            for group in groups_in:
                group_options = group.get("options", [])[:100]
                slack_groups.append(
                    {
                        # Slack spec: group label is plain_text, max 75 chars.
                        "label": {"type": "plain_text", "text": group.get("label", "")[:75]},
                        "options": [self._select_option_to_slack(opt) for opt in group_options],
                    }
                )
            return {
                "body": json.dumps({"option_groups": slack_groups}),
                "status": 200,
                "headers": {"Content-Type": "application/json"},
            }

        flat = cast("list[SelectOptionElement]", result)[:100]
        return {
            "body": json.dumps({"options": [self._select_option_to_slack(opt) for opt in flat]}),
            "status": 200,
            "headers": {"Content-Type": "application/json"},
        }

    @staticmethod
    def _select_option_to_slack(opt: SelectOptionElement) -> dict[str, Any]:
        """Convert a :class:`SelectOptionElement` to Slack's option object shape.

        Mirrors upstream ``selectOptionToSlackOption`` — the ``description``
        key is omitted (not set to ``null``) when not provided.
        """
        entry: dict[str, Any] = {
            "text": {"type": "plain_text", "text": opt.get("label", "")},
            "value": opt.get("value", ""),
        }
        desc = opt.get("description")
        if desc:
            entry["description"] = {"type": "plain_text", "text": desc}
        return entry

    # ==================================================================
    # View submission / close
    # ==================================================================

    async def _handle_view_submission(
        self, payload: dict[str, Any], options: WebhookOptions | None = None
    ) -> dict[str, Any]:
        if not self._chat:
            self._logger.warn("Chat instance not initialized, ignoring view submission")
            return {"body": "", "status": 200}

        view = payload.get("view", {})
        state_values = view.get("state", {}).get("values", {})

        # Flatten values
        values: dict[str, str] = {}
        for block_values in state_values.values():
            for action_id, input_val in block_values.items():
                values[action_id] = (
                    input_val.get("value") or (input_val.get("selected_option") or {}).get("value") or ""
                )

        meta = decode_modal_metadata(view.get("private_metadata") or None)
        user_ref = payload.get("user", {})

        event = ModalSubmitEvent(
            callback_id=view.get("callback_id", ""),
            view_id=view.get("id", ""),
            values=values,
            private_metadata=meta.private_metadata,
            user=Author(
                user_id=user_ref.get("id", ""),
                user_name=user_ref.get("username") or user_ref.get("name") or "unknown",
                full_name=user_ref.get("name") or user_ref.get("username") or "unknown",
                is_bot=False,
                is_me=False,
            ),
            adapter=self,
            raw=payload,
        )

        response = await self._chat.process_modal_submit(event, meta.context_id, options)

        if response:
            slack_response = self._modal_response_to_slack(response, meta.context_id)
            return {
                "body": json.dumps(slack_response),
                "status": 200,
                "headers": {"Content-Type": "application/json"},
            }

        return {"body": "", "status": 200}

    def _handle_view_closed(self, payload: dict[str, Any], options: WebhookOptions | None = None) -> None:
        if not self._chat:
            self._logger.warn("Chat instance not initialized, ignoring view closed")
            return

        view = payload.get("view", {})
        meta = decode_modal_metadata(view.get("private_metadata") or None)
        user_ref = payload.get("user", {})

        event = ModalCloseEvent(
            callback_id=view.get("callback_id", ""),
            view_id=view.get("id", ""),
            private_metadata=meta.private_metadata,
            user=Author(
                user_id=user_ref.get("id", ""),
                user_name=user_ref.get("username") or user_ref.get("name") or "unknown",
                full_name=user_ref.get("name") or user_ref.get("username") or "unknown",
                is_bot=False,
                is_me=False,
            ),
            adapter=self,
            raw=payload,
        )

        self._chat.process_modal_close(event, meta.context_id, options)

    def _modal_response_to_slack(self, response: ModalResponse, context_id: str | None = None) -> SlackModalResponse:
        if response.action == "close":
            return {}
        if response.action == "clear":
            # Close the entire modal view stack (Slack ``response_action: clear``).
            return {"response_action": "clear"}
        if response.action == "errors":
            return {"response_action": "errors", "errors": response.errors or {}}
        if response.action in ("update", "push"):
            modal = response.modal
            if isinstance(modal, dict):
                metadata = encode_modal_metadata(
                    ModalMetadata(
                        context_id=context_id,
                        private_metadata=modal.get("private_metadata"),
                    )
                )
                view = modal_to_slack_view(cast(ModalElement, modal), metadata)
                return {"response_action": response.action, "view": view}
        return {}

    # ==================================================================
    # Socket Mode
    # ==================================================================

    async def start_socket_mode(self) -> None:
        """Open a Slack Socket Mode WebSocket and dispatch events.

        Spawns a tracked background task that connects, runs the message
        loop, and reconnects with exponential backoff on disconnect (per
        Slack's recommendation). Returns once the initial connection has
        been established.

        Raises :class:`ValidationError` if the adapter wasn't configured
        with ``app_token`` (must start with ``xapp-``).

        Idempotent: a second call while connected is a no-op.
        """
        if not self._app_token:
            raise ValidationError(
                "slack",
                "appToken is required for socket mode. Set SLACK_APP_TOKEN or provide it in config.",
            )

        if self._socket_task is not None and not self._socket_task.done():
            # Already running.
            return

        # Lazy import (hazard #10) — slack_sdk is an optional dependency.
        try:
            from slack_sdk.socket_mode.aiohttp import SocketModeClient  # noqa: F401
        except ImportError as exc:
            raise ValidationError(
                "slack",
                "slack_sdk Socket Mode dependencies are not installed. "
                "Install with `pip install chat-sdk[slack-socket]`.",
            ) from exc

        self._socket_shutdown_event.clear()
        connected = asyncio.Event()
        loop = asyncio.get_running_loop()
        # Hazard #5: track the task explicitly so ``stop_socket_mode`` can
        # cancel it cleanly. Don't use ``asyncio.ensure_future`` without
        # tracking — a stray reference loss would orphan the WebSocket.
        self._socket_task = loop.create_task(self._socket_mode_loop(connected))

        # Wait for either the first successful connect or for the loop to
        # exit (which means the very first connect raised). Re-raise so the
        # caller learns about a hard config failure (bad app token, network
        # offline) instead of silently spinning forever. Bound the wait with
        # ``connect_timeout_s`` so a hung handshake (slack_sdk's ``connect()``
        # never returning) doesn't make ``initialize()`` block indefinitely
        # (hazard #11).
        wait_task = asyncio.create_task(connected.wait())
        try:
            # ``shield`` keeps the inner ``asyncio.wait`` alive when
            # ``wait_for`` times out, so we can deterministically tear
            # ``wait_task`` and ``_socket_task`` down ourselves in the
            # except branch. Both tasks are cancelled there (``wait_task``
            # directly, ``_socket_task`` via ``stop_socket_mode``), so the
            # shielded inner wait always resolves shortly after — no orphan.
            first_done, pending = await asyncio.wait_for(
                asyncio.shield(
                    asyncio.wait(
                        {wait_task, self._socket_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                ),
                timeout=self._socket_connect_timeout_s,
            )
        except asyncio.TimeoutError:
            # Don't leak the wait task or the still-running socket loop —
            # tear them down before surfacing the failure.
            wait_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                # ``await`` is load-bearing here: it drains the cancelled
                # task before this function returns so the asyncio loop
                # doesn't emit "Task was destroyed but it is pending!"
                # warnings at shutdown, and so callers can rely on a
                # synchronous "task fully gone" contract after timeout.
                # ``contextlib.suppress`` absorbs the expected
                # ``CancelledError`` that wait_task raises on cancel.
                # Static analyzers (github-code-quality, etc.) sometimes
                # flag this as "statement has no effect" because they
                # model ``await`` syntactically rather than as a
                # side-effecting suspension — that's a false positive;
                # do not remove this line.
                await wait_task
            await self.stop_socket_mode()
            raise TimeoutError(f"Slack Socket Mode connect timed out after {self._socket_connect_timeout_s}s") from None
        # Hazard #5: if the loop task finished first, cancel the wait task
        # explicitly so the orphan ``connected.wait()`` doesn't sit forever.
        if wait_task in pending:
            wait_task.cancel()
        if connected.is_set():
            return
        # Socket loop exited before connecting — surface its exception.
        for done in first_done:
            if done is self._socket_task:
                exc = done.exception()
                if exc is not None:
                    raise exc

    async def stop_socket_mode(self) -> None:
        """Close the Socket Mode connection and cancel the reconnect loop.

        Idempotent. Safe to call from any task; it disconnects the active
        client and waits for the background task to finish.
        """
        self._socket_shutdown_event.set()

        client = self._socket_client
        self._socket_client = None
        if client is not None:
            try:
                await client.disconnect()
            except Exception as exc:  # pragma: no cover - best-effort cleanup
                self._logger.warn("Error disconnecting Slack socket client", {"error": str(exc)})

        task = self._socket_task
        self._socket_task = None
        if task is not None and not task.done():
            task.cancel()
            # Cancellation is expected on shutdown; surface anything else so
            # surprising loop crashes aren't silently swallowed.
            with contextlib.suppress(asyncio.CancelledError):
                # ``await`` is load-bearing: deterministically drains the
                # cancelled loop task before ``stop_socket_mode()``
                # returns. Without it, ``stop_socket_mode`` can return
                # while the loop task is still tearing down, which:
                #   - breaks ``test_stop_idempotent`` (the second call
                #     can race the first's cleanup)
                #   - risks "Task was destroyed but it is pending!"
                #     warnings if the loop holds GC-only references
                # ``contextlib.suppress`` absorbs the expected
                # ``CancelledError``. Static analyzers sometimes flag
                # this as "statement has no effect" because they model
                # ``await`` syntactically — that's a false positive; do
                # not remove this line.
                await task
        if task is not None:
            self._logger.info("Slack socket mode disconnected")

    async def _socket_mode_loop(self, connected: asyncio.Event) -> None:
        """Connect/run/reconnect loop for Socket Mode.

        Slack's Socket Mode WebSocket is long-lived but can disconnect for
        many reasons (refresh, network blip, restart). We retry with
        exponential backoff (with jitter) and reset the backoff once a
        connection holds for any non-trivial time.
        """
        from slack_sdk.socket_mode.aiohttp import SocketModeClient

        backoff = self._socket_initial_backoff_s
        try:
            while not self._socket_shutdown_event.is_set():
                client = SocketModeClient(app_token=cast(str, self._app_token))
                # Register our request handler. ``socket_mode_request_listeners``
                # is the documented public extension point on the slack_sdk
                # client; each listener is ``async (client, request) -> None``.
                client.socket_mode_request_listeners.append(self._on_socket_request)
                self._socket_client = client
                try:
                    await client.connect()
                except Exception as exc:
                    self._logger.error(
                        "Slack socket mode connect failed",
                        {"error": str(exc)},
                    )
                    self._socket_client = None
                    if self._socket_shutdown_event.is_set():
                        return
                    if not connected.is_set():
                        # First connect failed and nobody's listening yet —
                        # surface the error to the caller of start_socket_mode.
                        raise
                    await self._socket_sleep_with_backoff(backoff)
                    backoff = min(backoff * 2, self._socket_max_backoff_s)
                    continue

                # Connection established (or in progress) — let the caller of
                # start_socket_mode resume.
                self._logger.info("Slack socket mode connected")
                connected.set()
                backoff = self._socket_initial_backoff_s

                # Wait until the socket disconnects or shutdown is requested.
                while not self._socket_shutdown_event.is_set():
                    if not client.is_connected():
                        break
                    await asyncio.sleep(1.0)

                # Tear down the current client before reconnecting.
                self._socket_client = None
                try:
                    await client.disconnect()
                except Exception as exc:  # pragma: no cover - best-effort
                    self._logger.warn(
                        "Error disconnecting Slack socket client during reconnect",
                        {"error": str(exc)},
                    )

                if self._socket_shutdown_event.is_set():
                    return
                self._logger.info("Slack socket mode disconnected, reconnecting")
                await self._socket_sleep_with_backoff(backoff)
                backoff = min(backoff * 2, self._socket_max_backoff_s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Make sure first-connect failures propagate to the caller of
            # start_socket_mode, but also log everything else loudly.
            if not connected.is_set():
                raise
            self._logger.error(
                "Slack socket mode loop crashed",
                {"error": str(exc)},
            )
        finally:
            client = self._socket_client
            self._socket_client = None
            if client is not None:
                with contextlib.suppress(Exception):  # pragma: no cover - best-effort
                    await client.disconnect()

    async def _socket_sleep_with_backoff(self, seconds: float) -> None:
        """Sleep for ``seconds`` but wake immediately on shutdown.

        Uses the per-adapter ``_socket_shutdown_event`` so ``stop_socket_mode``
        can interrupt the backoff window without polling — wakeup latency is
        bounded by event-loop scheduling, not the previous 0.25s poll.
        """
        try:
            await asyncio.wait_for(self._socket_shutdown_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return

    async def _on_socket_request(self, client: Any, request: Any) -> None:
        """Listener invoked by ``SocketModeClient`` for each socket message.

        Signature matches slack_sdk's documented hook:
        ``async (client: SocketModeClient, request: SocketModeRequest) -> None``.
        """
        from slack_sdk.socket_mode.response import SocketModeResponse

        envelope_id = getattr(request, "envelope_id", "") or ""
        event_type = getattr(request, "type", "") or ""
        payload = getattr(request, "payload", None) or {}
        retry_attempt = getattr(request, "retry_attempt", 0) or 0

        async def ack(response_payload: dict[str, Any] | None = None) -> None:
            try:
                await client.send_socket_mode_response(
                    SocketModeResponse(envelope_id=envelope_id, payload=response_payload)
                )
            except Exception as exc:  # pragma: no cover - best-effort
                self._logger.warn(
                    "Failed to send socket mode ack",
                    {"envelope_id": envelope_id, "error": str(exc)},
                )

        # Slack re-delivers events that weren't acked in time. Skip retries
        # so we don't double-process — but still ack so Slack stops resending.
        if retry_attempt and retry_attempt > 0:
            await ack()
            self._logger.debug("Skipping socket mode retry", {"retry_attempt": retry_attempt})
            return

        await self._route_socket_event(payload, event_type, ack)

    async def _route_socket_event(
        self,
        body: dict[str, Any],
        event_type: str,
        ack: Callable[..., Awaitable[None]],
        options: WebhookOptions | None = None,
    ) -> None:
        """Route a socket-mode event to the same handler the webhook path uses.

        Mirrors upstream's ``routeSocketEvent``. The ``ack`` callback delivers
        the SocketModeResponse back to Slack — for events_api and
        slash_commands we ack immediately and let processing run in the
        background; for interactive payloads we may attach a response body
        (e.g. modal ``view_submission`` errors) onto the ack.
        """

        def wrap_async(coro: Awaitable[Any]) -> None:
            """Run ``coro`` either via ``waitUntil`` or as a tracked task."""
            if options is not None and options.wait_until is not None:
                # ``wait_until`` semantics: caller takes ownership.
                options.wait_until(cast(Any, coro))
                return
            task = asyncio.get_running_loop().create_task(cast(Any, coro))

            def _log_exc(t: asyncio.Task[Any]) -> None:
                if t.cancelled():
                    return
                exc = t.exception()
                if exc is not None:
                    self._logger.error(
                        "Error in socket mode async handler",
                        {"error": str(exc)},
                    )

            task.add_done_callback(_log_exc)
            _pin_task(task)

        if event_type == "events_api":
            await ack()
            event = body.get("event")
            if not isinstance(event, dict):
                self._logger.warn(
                    "Socket mode events_api missing event field",
                    {"body_type": type(body).__name__},
                )
                return
            # Match the webhook path's synthesized payload exactly. Upstream
            # doesn't include ``is_ext_shared_channel`` here, and the webhook
            # JSON we pass into ``_process_event_payload`` doesn't either —
            # adding it on the socket path is a quiet socket-vs-webhook
            # divergence (hazard #7). Keep the keys that flow into
            # downstream handlers, drop the rest.
            payload: dict[str, Any] = {
                "type": "event_callback",
                "event": event,
                "team_id": body.get("team_id"),
                "event_id": body.get("event_id"),
                "event_time": body.get("event_time"),
            }
            # Multi-workspace: resolve token before dispatch (mirrors webhook
            # path). copy_context() keeps the ContextVar set on tasks spawned
            # by handlers (hazard #6).
            team_id_event = payload.get("team_id")
            try:
                if not self._is_single_workspace and team_id_event:
                    ctx = await self._resolve_token_for_team(team_id_event)
                    if ctx is None:
                        self._logger.warn(
                            "Could not resolve token for team",
                            {"teamId": team_id_event},
                        )
                        return
                    isolated = contextvars.copy_context()
                    isolated.run(self._request_context.set, ctx)
                    isolated.run(self._process_event_payload, payload, options)
                else:
                    self._process_event_payload(payload, options)
            except Exception as exc:
                self._logger.error(
                    "Error processing socket mode events_api",
                    {"error": str(exc)},
                )
            return

        if event_type == "slash_commands":
            # Slash responses are out-of-band via ``response_url`` (Slack's
            # delayed-response pattern), not the WebSocket ack. The empty ack
            # here just tells Slack we received the command; the body of the
            # reply flows through ``_handle_slash_command`` posting to the
            # response_url. Matches upstream's `routeSocketEvent`.
            await ack()
            # slash_commands payload is a flat dict mirroring the
            # form-urlencoded fields; convert to the parse_qs shape that
            # _handle_slash_command expects (each value wrapped in a list).
            params: dict[str, list[str]] = {k: [v] for k, v in body.items() if isinstance(v, str)}

            async def run_slash() -> None:
                team_id_slash = (params.get("team_id") or [None])[0]
                if not self._is_single_workspace and team_id_slash:
                    ctx = await self._resolve_token_for_team(team_id_slash)
                    if ctx is None:
                        self._logger.warn("Could not resolve token for slash command")
                        return
                    tok = self._request_context.set(ctx)
                    try:
                        await self._handle_slash_command(params, options)
                    finally:
                        self._request_context.reset(tok)
                else:
                    await self._handle_slash_command(params, options)

            wrap_async(run_slash())
            return

        if event_type == "interactive":
            try:
                # Multi-workspace: scope token resolution to the dispatch.
                team_ref = body.get("team")
                team_id_interactive = team_ref.get("id") if isinstance(team_ref, dict) else body.get("team_id")
                if not self._is_single_workspace and team_id_interactive:
                    ctx = await self._resolve_token_for_team(team_id_interactive)
                    if ctx is None:
                        self._logger.warn("Could not resolve token for interactive payload")
                        await ack()
                        return
                    tok = self._request_context.set(ctx)
                    try:
                        result = await self._dispatch_interactive_payload(body, options)
                    finally:
                        self._request_context.reset(tok)
                else:
                    result = await self._dispatch_interactive_payload(body, options)
            except Exception as exc:
                self._logger.error(
                    "Error processing socket mode interactive",
                    {"error": str(exc)},
                )
                # Hazard #15 (UX): an empty ack on view_submission silently
                # closes the modal, so the user has no signal anything went
                # wrong. Return ``response_action=errors`` so Slack keeps the
                # modal open with a visible message. Safe for non-modal
                # interactive types too — Slack ignores the field when the
                # payload type doesn't expect it.
                await ack({"response_action": "errors", "errors": {"_": "internal error"}})
                return

            response_body: dict[str, Any] | None = None
            body_str = result.get("body") if isinstance(result, dict) else None
            if isinstance(body_str, str) and body_str:
                content_type = result.get("headers", {}).get("Content-Type", "") if isinstance(result, dict) else ""
                if "application/json" in content_type:
                    try:
                        parsed = json.loads(body_str)
                        if isinstance(parsed, dict):
                            response_body = parsed
                    except (json.JSONDecodeError, ValueError):
                        response_body = None
            await ack(response_body)
            return

        # Unknown event type — still ack so Slack doesn't redeliver.
        await ack()
        self._logger.debug("Unhandled socket mode event type", {"type": event_type})

    async def _handle_forwarded_socket_event(
        self,
        event: dict[str, Any],
        options: WebhookOptions | None = None,
    ) -> None:
        """Process a socket-mode event forwarded over HTTP.

        Companion to :meth:`_route_socket_event` for the serverless pattern
        where a long-running listener runs in one process and posts events
        to a webhook handler elsewhere. The ack already happened on the
        listener side; we just route to the same handler dispatch.
        """

        async def noop_ack(_response: dict[str, Any] | None = None) -> None:
            return None

        body = event.get("body")
        event_type = event.get("eventType") or event.get("event_type") or ""
        if not isinstance(body, dict) or not isinstance(event_type, str):
            self._logger.warn(
                "Forwarded socket event has invalid shape",
                {"event_type": type(event_type).__name__},
            )
            return
        await self._route_socket_event(body, event_type, noop_ack, options)

    # ==================================================================
    # Message events
    # ==================================================================

    def _handle_message_event(self, event: dict[str, Any], options: WebhookOptions | None = None) -> None:
        if not self._chat:
            self._logger.warn("Chat instance not initialized, ignoring event")
            return

        subtype = event.get("subtype")
        if subtype == "message_changed":
            self._handle_message_changed(event, options)
            return
        if subtype and subtype in _IGNORED_SUBTYPES:
            self._logger.debug("Ignoring message subtype", {"subtype": subtype})
            return

        if not (event.get("channel") and event.get("ts")):
            self._logger.debug(
                "Ignoring event without channel or ts",
                {"channel": event.get("channel"), "ts": event.get("ts")},
            )
            return

        # DMs: top-level messages use empty threadTs
        is_dm = event.get("channel_type") == "im"
        thread_ts = (event.get("thread_ts") or "") if is_dm else (event.get("thread_ts") or event.get("ts", ""))
        thread_id = self.encode_thread_id(SlackThreadId(channel=event["channel"], thread_ts=thread_ts))

        is_mention = event.get("type") == "app_mention"

        async def factory() -> Message:
            msg = await self._parse_slack_message(event, thread_id)
            if is_mention:
                msg.is_mention = True
            return msg

        self._chat.process_message(self, thread_id, factory, options)

    # ==================================================================
    # Reaction events
    # ==================================================================

    def _handle_reaction_event(self, event: dict[str, Any], options: WebhookOptions | None = None) -> None:
        if not self._chat:
            self._logger.warn("Chat instance not initialized, ignoring reaction")
            return

        item = event.get("item", {})
        if item.get("type") != "message":
            self._logger.debug("Ignoring reaction to non-message item", {"itemType": item.get("type")})
            return

        channel = item.get("channel", "")
        message_id = item.get("ts", "")
        raw_emoji = event.get("reaction", "")
        normalized_emoji = resolve_emoji_from_slack(raw_emoji)

        # Check if reaction is from this bot
        ctx = self._request_context.get()
        user_id = event.get("user", "")
        is_me = (
            (ctx is not None and ctx.bot_user_id and user_id == ctx.bot_user_id)
            or (self._bot_user_id is not None and user_id == self._bot_user_id)
            or (self._bot_id is not None and user_id == self._bot_id)
        )

        chat = self._chat

        async def _resolve_and_process() -> None:
            # Resolve the actual parent thread_ts via conversations.replies.
            # item.ts may be a reply rather than the root message, so we
            # need to look up the thread_ts of the message to find the
            # conversation root.
            parent_ts = message_id
            try:
                client = self._get_client()
                result = await client.conversations_replies(
                    channel=channel,
                    ts=message_id,
                    limit=1,
                    inclusive=True,
                )
                msgs = result.get("messages", [])
                if msgs:
                    parent_ts = msgs[0].get("thread_ts") or msgs[0].get("ts") or message_id
            except Exception as err:
                self._logger.debug(
                    "Could not resolve parent thread_ts for reaction, using item.ts",
                    {"error": str(err), "channel": channel, "ts": message_id},
                )

            thread_id = self.encode_thread_id(SlackThreadId(channel=channel, thread_ts=parent_ts))

            reaction_event = ReactionEvent(
                emoji=normalized_emoji,
                raw_emoji=raw_emoji,
                added=event.get("type") == "reaction_added",
                user=Author(
                    user_id=user_id,
                    user_name=user_id,
                    full_name=user_id,
                    is_bot=False,
                    is_me=is_me,
                ),
                message_id=message_id,
                thread_id=thread_id,
                thread=None,  # pyrefly: ignore[bad-argument-type]  # filled in by Chat
                raw=event,
                adapter=self,
            )

            chat.process_reaction(reaction_event, options)

        try:
            task = asyncio.get_running_loop().create_task(_resolve_and_process())
        except RuntimeError:
            return  # No running event loop
        task.add_done_callback(
            lambda t: (
                self._logger.error("Reaction resolve error", {"error": str(t.exception())}) if t.exception() else None
            )
        )
        if options and options.wait_until:
            options.wait_until(task)

    # ==================================================================
    # Assistant events
    # ==================================================================

    def _handle_assistant_thread_started(self, event: dict[str, Any], options: WebhookOptions | None = None) -> None:
        if not self._chat:
            self._logger.warn("Chat instance not initialized, ignoring assistant_thread_started")
            return

        assistant_thread = event.get("assistant_thread")
        if not assistant_thread:
            self._logger.warn("Malformed assistant_thread_started: missing assistant_thread")
            return

        channel_id = assistant_thread.get("channel_id", "")
        thread_ts = assistant_thread.get("thread_ts", "")
        user_id = assistant_thread.get("user_id", "")
        context = assistant_thread.get("context", {})

        thread_id = self.encode_thread_id(SlackThreadId(channel=channel_id, thread_ts=thread_ts))

        self._chat.process_assistant_thread_started(
            AssistantThreadStartedEvent(
                adapter=self,
                thread_id=thread_id,
                user_id=user_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                context={
                    "channel_id": context.get("channel_id"),
                    "team_id": context.get("team_id"),
                    "enterprise_id": context.get("enterprise_id"),
                    "thread_entry_point": context.get("thread_entry_point"),
                    "force_search": context.get("force_search"),
                },
            ),
            options,
        )

    def _handle_assistant_context_changed(self, event: dict[str, Any], options: WebhookOptions | None = None) -> None:
        if not self._chat:
            self._logger.warn("Chat instance not initialized, ignoring assistant_thread_context_changed")
            return

        assistant_thread = event.get("assistant_thread")
        if not assistant_thread:
            self._logger.warn("Malformed assistant_thread_context_changed: missing assistant_thread")
            return

        channel_id = assistant_thread.get("channel_id", "")
        thread_ts = assistant_thread.get("thread_ts", "")
        user_id = assistant_thread.get("user_id", "")
        context = assistant_thread.get("context", {})

        thread_id = self.encode_thread_id(SlackThreadId(channel=channel_id, thread_ts=thread_ts))

        self._chat.process_assistant_context_changed(
            AssistantContextChangedEvent(
                adapter=self,
                thread_id=thread_id,
                user_id=user_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                context={
                    "channel_id": context.get("channel_id"),
                    "team_id": context.get("team_id"),
                    "enterprise_id": context.get("enterprise_id"),
                    "thread_entry_point": context.get("thread_entry_point"),
                    "force_search": context.get("force_search"),
                },
            ),
            options,
        )

    # ==================================================================
    # App home / member joined
    # ==================================================================

    def _handle_app_home_opened(self, event: dict[str, Any], options: WebhookOptions | None = None) -> None:
        if not self._chat:
            self._logger.warn("Chat instance not initialized, ignoring app_home_opened")
            return

        self._chat.process_app_home_opened(
            AppHomeOpenedEvent(
                adapter=self,
                user_id=event.get("user", ""),
                channel_id=event.get("channel", ""),
            ),
            options,
        )

    def _handle_member_joined_channel(self, event: dict[str, Any], options: WebhookOptions | None = None) -> None:
        if not self._chat:
            self._logger.warn("Chat instance not initialized, ignoring member_joined_channel")
            return

        self._chat.process_member_joined_channel(
            MemberJoinedChannelEvent(
                adapter=self,
                user_id=event.get("user", ""),
                channel_id=self.encode_thread_id(SlackThreadId(channel=event.get("channel", ""), thread_ts="")),
                inviter_id=event.get("inviter"),
            ),
            options,
        )

    def _handle_user_change(self, event: dict[str, Any]) -> None:
        if not self._chat:
            return
        user_info = event.get("user", {})
        user_id = user_info.get("id")
        if user_id:
            try:
                # Fire and forget cache invalidation
                _pin_task(
                    asyncio.get_running_loop().create_task(self._chat.get_state().delete(f"slack:user:{user_id}"))
                )
            except RuntimeError:
                pass  # No running event loop
            except Exception as exc:
                self._logger.warn(
                    "Failed to invalidate user cache",
                    {"userId": user_id, "error": exc},
                )

    # ==================================================================
    # Publish Home view / Assistant helpers
    # ==================================================================

    async def publish_home_view(self, user_id: str, view: dict[str, Any]) -> None:
        """Publish a Home tab view for a user."""
        client = self._get_client()
        await client.views_publish(user_id=user_id, view=view)

    async def set_suggested_prompts(
        self,
        channel_id: str,
        thread_ts: str,
        prompts: list[dict[str, str]],
        title: str | None = None,
    ) -> None:
        """Set suggested prompts for an assistant thread."""
        client = self._get_client()
        kwargs: dict[str, Any] = {
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "prompts": prompts,
        }
        if title:
            kwargs["title"] = title
        await client.assistant_threads_setSuggestedPrompts(**kwargs)

    async def set_assistant_status(
        self,
        channel_id: str,
        thread_ts: str,
        status: str,
        loading_messages: list[str] | None = None,
    ) -> None:
        """Set status/thinking indicator for an assistant thread."""
        client = self._get_client()
        kwargs: dict[str, Any] = {
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "status": status,
        }
        if loading_messages:
            kwargs["loading_messages"] = loading_messages
        await client.assistant_threads_setStatus(**kwargs)

    async def set_assistant_title(self, channel_id: str, thread_ts: str, title: str) -> None:
        """Set title for an assistant thread (shown in History tab)."""
        client = self._get_client()
        await client.assistant_threads_setTitle(channel_id=channel_id, thread_ts=thread_ts, title=title)

    # ==================================================================
    # Mention resolution
    # ==================================================================

    async def _resolve_inline_mentions(self, text: str, skip_self_mention: bool) -> str:
        """Resolve inline user/channel mentions to display names.

        Converts ``<@U123>`` to ``<@U123|displayName>`` so downstream parsers
        render them as ``@displayName`` instead of ``@U123``.
        """
        user_ids: set[str] = set()
        channel_ids: set[str] = set()

        for segment in text.split("<"):
            end = segment.find(">")
            if end == -1:
                continue
            inner = segment[:end]
            if inner.startswith("@"):
                rest = inner[1:]
                pipe_idx = rest.find("|")
                uid = rest[:pipe_idx] if pipe_idx >= 0 else rest
                if SLACK_USER_ID_PATTERN.match(uid):
                    user_ids.add(uid)
            elif inner.startswith("#"):
                rest = inner[1:]
                pipe_idx = rest.find("|")
                if pipe_idx == -1 and SLACK_USER_ID_PATTERN.match(rest):
                    channel_ids.add(rest)

        if not user_ids and not channel_ids:
            return text

        if skip_self_mention and self._bot_user_id:
            user_ids.discard(self._bot_user_id)

        if not user_ids and not channel_ids:
            return text

        # Look up all mentioned users and channels in parallel
        user_lookups, channel_lookups = await asyncio.gather(
            asyncio.gather(*(self._lookup_user_name(uid) for uid in user_ids)),
            asyncio.gather(*(self._lookup_channel_name(cid) for cid in channel_ids)),
        )

        user_name_map = dict(zip(user_ids, user_lookups, strict=False))
        channel_name_map = dict(zip(channel_ids, channel_lookups, strict=False))

        # Replace mentions using split-based approach (no ReDoS)
        result = ""
        remaining = text
        start_idx = _find_next_mention(remaining)
        while start_idx != -1:
            result += remaining[:start_idx]
            remaining = remaining[start_idx:]
            end_idx = remaining.find(">")
            if end_idx == -1:
                break
            prefix = remaining[1]  # '@' or '#'
            inner = remaining[2:end_idx]
            pipe_idx = inner.find("|")
            id_str = inner[:pipe_idx] if pipe_idx >= 0 else inner
            if prefix == "@" and SLACK_USER_ID_PATTERN.match(id_str):
                name = user_name_map.get(id_str)
                result += f"<@{id_str}|{name}>" if name else f"<@{id_str}>"
            elif prefix == "#" and pipe_idx == -1 and id_str in channel_name_map:
                name = channel_name_map[id_str]
                result += f"<#{id_str}|{name}>"
            else:
                result += remaining[: end_idx + 1]
            remaining = remaining[end_idx + 1 :]
            start_idx = _find_next_mention(remaining)
        return result + remaining

    async def _lookup_user_name(self, user_id: str) -> str:
        """Look up a user's display name (helper for parallel resolution)."""
        info = await self._lookup_user(user_id)
        return info["display_name"]

    async def _lookup_channel_name(self, channel_id: str) -> str:
        """Look up a channel name (helper for parallel resolution)."""
        return await self._lookup_channel(channel_id)

    # ==================================================================
    # Outgoing mention resolution
    # ==================================================================

    async def _resolve_outgoing_mentions(self, text: str, thread_id: str) -> str:
        """Resolve ``@name`` mentions in text to Slack ``<@USER_ID>`` format."""
        if not self._chat:
            return text
        state = self._chat.get_state()

        mention_pattern = re.compile(r"(?<![\w<])@(\w+)")
        mentions: dict[str, list[str]] = {}

        for match in mention_pattern.finditer(text):
            name = match.group(1)
            if SLACK_USER_ID_EXACT_PATTERN.match(name):
                continue
            idx = match.start()
            if idx > 0 and text[idx - 1] == "<":
                continue
            lower_name = name.lower()
            if lower_name not in mentions:
                mentions[lower_name] = []

        if not mentions:
            return text

        # Look up user IDs for each mentioned name
        for name in list(mentions.keys()):
            user_ids = await state.get_list(f"slack:user-by-name:{name}")
            mentions[name] = list(set(user_ids))

        # Load thread participants if needed (ambiguous mentions)
        participants: set[str] | None = None
        needs_participants = any(len(ids) > 1 for ids in mentions.values())
        if needs_participants:
            participant_list = await state.get_list(f"slack:thread-participants:{thread_id}")
            participants = set(participant_list)

        def replace_mention(match: re.Match[str]) -> str:
            name = match.group(1)
            offset = match.start()
            if offset > 0 and text[offset - 1] == "<":
                return match.group(0)
            if SLACK_USER_ID_EXACT_PATTERN.match(name):
                return match.group(0)

            user_ids = mentions.get(name.lower())
            if not user_ids:
                return match.group(0)
            if len(user_ids) == 1:
                return f"<@{user_ids[0]}>"
            if participants:
                in_thread = [uid for uid in user_ids if uid in participants]
                if len(in_thread) == 1:
                    return f"<@{in_thread[0]}>"
            return match.group(0)

        return mention_pattern.sub(replace_mention, text)

    async def _resolve_message_mentions(
        self, message: AdapterPostableMessage, thread_id: str
    ) -> AdapterPostableMessage:
        """Pre-process outgoing message to resolve @name mentions."""
        if not self._chat:
            return message
        if isinstance(message, str):
            return await self._resolve_outgoing_mentions(message, thread_id)
        if hasattr(message, "raw") and isinstance(getattr(message, "raw", None), str):
            resolved = await self._resolve_outgoing_mentions(message.raw, thread_id)  # type: ignore[union-attr]
            return type(message)(**{**message.__dict__, "raw": resolved})  # type: ignore[arg-type]
        if hasattr(message, "markdown") and isinstance(getattr(message, "markdown", None), str):
            resolved = await self._resolve_outgoing_mentions(message.markdown, thread_id)  # type: ignore[union-attr]
            return type(message)(**{**message.__dict__, "markdown": resolved})  # type: ignore[arg-type]
        return message

    # ==================================================================
    # Link extraction
    # ==================================================================

    def _extract_links(self, event: dict[str, Any]) -> list[LinkPreview]:
        """Extract link URLs from a Slack event.

        Also merges any inline unfurl metadata that Slack already attached to
        this same event (legacy ``attachments`` array). Cross-event unfurl
        metadata (delivered later via ``message_changed``) is merged
        asynchronously via :meth:`_enrich_links`.
        """
        urls: set[str] = set()

        for block in event.get("blocks", []):
            if block.get("type") == "rich_text" and block.get("elements"):
                for section in block["elements"]:
                    for element in section.get("elements", []):
                        if element.get("type") == "link" and element.get("url"):
                            urls.add(element["url"])

        if not urls and event.get("text"):
            for match in re.finditer(r"<(https?://[^>]+)>", event["text"]):
                raw = match.group(1)
                pipe_idx = raw.find("|")
                urls.add(raw[:pipe_idx] if pipe_idx >= 0 else raw)

        # Build unfurl metadata index from inline (same-event) attachments.
        unfurls: dict[str, dict[str, str | None]] = {}
        for att in event.get("attachments") or []:
            if not isinstance(att, dict):
                continue
            att_url = att.get("from_url") or att.get("original_url")
            if att_url and (att.get("title") or att.get("text")):
                unfurls[att_url] = {
                    "title": att.get("title"),
                    "description": att.get("text"),
                    "image_url": att.get("image_url") or att.get("thumb_url"),
                    "site_name": att.get("service_name"),
                }
                urls.add(att_url)

        previews: list[LinkPreview] = []
        for url in urls:
            preview = self._create_link_preview(url)
            # TS uses ``url.replace(TRAILING_SLASH_PATTERN, "")`` (no ``g``
            # flag) which strips a single trailing ``/``. Python's
            # ``re.sub`` defaults to replacing all occurrences, so we
            # pin ``count=1`` for parity. (Practically the regex anchors
            # at end-of-string so only one match exists, but locking
            # this in prevents drift if the pattern ever loosens.)
            unfurl = (
                unfurls.get(url) or unfurls.get(_TRAILING_SLASH_PATTERN.sub("", url, count=1)) or unfurls.get(f"{url}/")
            )
            if unfurl:
                preview = self._merge_unfurl_into_preview(preview, unfurl)
            previews.append(preview)
        return previews

    @staticmethod
    def _merge_unfurl_into_preview(preview: LinkPreview, unfurl: dict[str, str | None]) -> LinkPreview:
        """Return a new LinkPreview with unfurl metadata merged in.

        Mirrors the TS spread ``{ ...preview, ...unfurl }``: the unfurl
        values OVERRIDE the preview's ``description`` / ``image_url`` /
        ``site_name`` (the unfurled attachment is the authoritative
        source). ``title`` is short-circuited by callers (``_enrich_links``
        skips merging when the preview already has a title), but for the
        same-event ``_extract_links`` path the unfurl's title also wins
        when present. ``fetch_message`` is never present on the unfurl
        and is preserved from the preview.
        """
        return LinkPreview(
            url=preview.url,
            title=unfurl.get("title") if unfurl.get("title") is not None else preview.title,
            description=unfurl.get("description") if unfurl.get("description") is not None else preview.description,
            image_url=unfurl.get("image_url") if unfurl.get("image_url") is not None else preview.image_url,
            site_name=unfurl.get("site_name") if unfurl.get("site_name") is not None else preview.site_name,
            fetch_message=preview.fetch_message,
        )

    def _handle_message_changed(self, event: dict[str, Any], _options: WebhookOptions | None = None) -> None:
        """Cache unfurl metadata from ``message_changed`` events.

        Slack delivers link unfurls asynchronously by editing the original
        message and dispatching ``message_changed``. We extract any unfurl
        attachments and store them keyed by the inner message ``ts`` so
        :meth:`_enrich_links` can pick them up for the original event.
        """
        inner = event.get("message")
        channel = event.get("channel")
        if not (inner and channel and isinstance(inner, dict)):
            return

        attachments = inner.get("attachments") or []
        has_unfurls = any(
            isinstance(att, dict) and (att.get("from_url") or att.get("original_url")) for att in attachments
        )
        if not has_unfurls:
            self._logger.debug("Ignoring message_changed without unfurl data")
            return

        ts = inner.get("ts")
        if not (self._chat and ts):
            return

        self._logger.debug(
            "Processing message_changed for link unfurls",
            {"channel": channel, "ts": ts, "attachmentCount": len(attachments)},
        )

        unfurls: dict[str, dict[str, str | None]] = {}
        for att in attachments:
            if not isinstance(att, dict):
                continue
            att_url = att.get("from_url") or att.get("original_url")
            if att_url and (att.get("title") or att.get("text")):
                unfurls[att_url] = {
                    "title": att.get("title"),
                    "description": att.get("text"),
                    "image_url": att.get("image_url") or att.get("thumb_url"),
                    "site_name": att.get("service_name"),
                }

        if not unfurls:
            return

        async def _store() -> None:
            try:
                await self._chat.get_state().set(  # type: ignore[union-attr]
                    f"slack:unfurls:{ts}",
                    unfurls,
                    _UNFURL_CACHE_TTL_MS,
                )
            except Exception as exc:
                self._logger.error("Failed to cache unfurl metadata", {"error": exc})

        try:
            task = asyncio.get_running_loop().create_task(_store())
            task.add_done_callback(
                lambda t: (
                    self._logger.error("Unfurl cache task failed", {"error": t.exception()}) if t.exception() else None
                )
            )
        except RuntimeError:
            # No running loop (sync test context) — skip silently.
            self._logger.debug("No running loop; skipping unfurl cache write")

    async def _enrich_links(self, links: list[LinkPreview], message_ts: str | None) -> list[LinkPreview]:
        """Enrich ``links`` with unfurl metadata from a ``message_changed`` cache.

        Polls the state cache for up to ``_UNFURL_WAIT_MS`` to give Slack
        time to deliver the cross-event ``message_changed`` payload.
        Returns the original list (untouched) when there is nothing to wait
        for.
        """
        if not (self._chat and message_ts) or not links:
            return links

        all_have_metadata = all((link.title is not None) or (link.fetch_message is not None) for link in links)
        if all_have_metadata:
            return links

        deadline = time.monotonic() + (_UNFURL_WAIT_MS / 1000.0)
        state = self._chat.get_state()
        stored: dict[str, dict[str, str | None]] | None = None
        while True:
            try:
                stored = await state.get(f"slack:unfurls:{message_ts}")
            except Exception as exc:
                self._logger.warn(
                    "Failed to read unfurl data from state",
                    {"error": str(exc), "message_ts": message_ts},
                )
                return links
            if stored or time.monotonic() >= deadline:
                break
            await asyncio.sleep(_UNFURL_POLL_MS / 1000.0)

        if not stored:
            return links

        out: list[LinkPreview] = []
        for link in links:
            if link.title is not None:
                out.append(link)
                continue
            unfurl = (
                stored.get(link.url)
                or stored.get(_TRAILING_SLASH_PATTERN.sub("", link.url, count=1))
                or stored.get(f"{link.url}/")
            )
            if unfurl:
                out.append(self._merge_unfurl_into_preview(link, unfurl))
            else:
                out.append(link)
        return out

    def _create_link_preview(self, url: str) -> LinkPreview:
        """Create a LinkPreview for a URL.

        If the URL points to a Slack message, includes a ``fetch_message``
        callback.
        """
        match = SLACK_MESSAGE_URL_PATTERN.match(url)
        if not match:
            return LinkPreview(url=url)

        channel = match.group(1)
        raw_ts = match.group(2)
        ts = f"{raw_ts[: len(raw_ts) - 6]}.{raw_ts[len(raw_ts) - 6 :]}"
        thread_id = self.encode_thread_id(SlackThreadId(channel=channel, thread_ts=ts))

        async def fetch_message() -> Message:
            client = self._get_client()
            result = await client.conversations_history(channel=channel, latest=ts, inclusive=True, limit=1)
            messages = result.get("messages", [])
            target = next((m for m in messages if m.get("ts") == ts), None)
            if not target:
                raise RuntimeError(f"Message not found: {url}")
            return await self._parse_slack_message(target, thread_id)

        return LinkPreview(url=url, fetch_message=fetch_message)

    # ==================================================================
    # Message parsing
    # ==================================================================

    async def _parse_slack_message(
        self,
        event: dict[str, Any],
        thread_id: str,
        *,
        skip_self_mention: bool = True,
    ) -> Message:
        """Parse a Slack event into a normalized Message (async with user lookup)."""
        is_me = self._is_message_from_self(event)
        raw_text = event.get("text", "")

        user_name = event.get("username", "unknown")
        full_name = event.get("username", "unknown")

        if event.get("user") and not event.get("username"):
            user_info = await self._lookup_user(event["user"])
            user_name = user_info["display_name"]
            full_name = user_info["real_name"]

        # Track thread participants
        if event.get("user") and self._chat:
            try:
                participant_key = f"slack:thread-participants:{thread_id}"
                participants = await self._chat.get_state().get_list(participant_key)
                if event["user"] not in participants:
                    await self._chat.get_state().append_to_list(
                        participant_key,
                        event["user"],
                        max_length=100,
                        ttl_ms=_REVERSE_INDEX_TTL_MS,
                    )
            except Exception as exc:
                self._logger.warn(
                    "Failed to track thread participant",
                    {"threadId": thread_id, "userId": event.get("user"), "error": exc},
                )

        text = await self._resolve_inline_mentions(raw_text, skip_self_mention)

        ts_str = event.get("ts", "0")
        try:
            date_sent = datetime.fromtimestamp(float(ts_str), tz=timezone.utc)
        except (ValueError, TypeError, OSError):
            date_sent = datetime.now(tz=timezone.utc)

        edited_at: datetime | None = None
        if event.get("edited"):
            try:
                edited_at = datetime.fromtimestamp(float(event["edited"].get("ts", "0")), tz=timezone.utc)
            except (ValueError, TypeError, OSError):
                edited_at = None

        return Message(
            id=event.get("ts", ""),
            thread_id=thread_id,
            text=self._format_converter.extract_plain_text(text),
            formatted=self._format_converter.to_ast(text),
            raw=event,
            author=Author(
                user_id=event.get("user") or event.get("bot_id") or "unknown",
                user_name=user_name,
                full_name=full_name,
                is_bot=bool(event.get("bot_id")),
                is_me=is_me,
            ),
            metadata=MessageMetadata(
                date_sent=date_sent,
                edited=bool(event.get("edited")),
                edited_at=edited_at,
            ),
            attachments=[
                self._create_attachment(f, team_id=event.get("team") or event.get("team_id"))
                for f in event.get("files", [])
            ],
            # ``_enrich_links`` polls the unfurl cache for up to
            # ``_UNFURL_WAIT_MS`` (2000 ms) before giving up, so every
            # message containing a not-yet-unfurled link adds up to
            # ~2s of latency to message handling worst-case (it returns
            # immediately when the cache is already populated or when
            # there are no links to enrich).
            links=await self._enrich_links(self._extract_links(event), event.get("ts")),
        )

    def _parse_slack_message_sync(self, event: dict[str, Any], thread_id: str) -> Message:
        """Synchronous message parsing (no user lookup, falls back to user ID)."""
        is_me = self._is_message_from_self(event)
        text = event.get("text", "")
        user_name = event.get("username") or event.get("user") or "unknown"
        full_name = event.get("username") or event.get("user") or "unknown"

        ts_str = event.get("ts", "0")
        try:
            date_sent = datetime.fromtimestamp(float(ts_str), tz=timezone.utc)
        except (ValueError, TypeError, OSError):
            date_sent = datetime.now(tz=timezone.utc)

        edited_at: datetime | None = None
        if event.get("edited"):
            try:
                edited_at = datetime.fromtimestamp(float(event["edited"].get("ts", "0")), tz=timezone.utc)
            except (ValueError, TypeError, OSError):
                edited_at = None

        return Message(
            id=event.get("ts", ""),
            thread_id=thread_id,
            text=self._format_converter.extract_plain_text(text),
            formatted=self._format_converter.to_ast(text),
            raw=event,
            author=Author(
                user_id=event.get("user") or event.get("bot_id") or "unknown",
                user_name=user_name,
                full_name=full_name,
                is_bot=bool(event.get("bot_id")),
                is_me=is_me,
            ),
            metadata=MessageMetadata(
                date_sent=date_sent,
                edited=bool(event.get("edited")),
                edited_at=edited_at,
            ),
            attachments=[
                self._create_attachment(f, team_id=event.get("team") or event.get("team_id"))
                for f in event.get("files", [])
            ],
            links=self._extract_links(event),
        )

    def _create_attachment(self, file: dict[str, Any], team_id: str | None = None) -> Attachment:
        """Create an Attachment from a Slack file object.

        ``team_id`` identifies the workspace the file belongs to and is
        stored in ``fetch_metadata`` so :meth:`rehydrate_attachment` can
        rebuild the download closure (with workspace-specific token) after
        the queue/debounce path JSON-serializes the message.
        """
        url = file.get("url_private")
        # Capture per-request token from the active webhook context so
        # ``fetch_data`` can run later without being inside the ContextVar
        # frame (e.g. after the message has been queued + rehydrated).
        # For single-workspace mode the default provider is re-resolved at
        # fetch time so dynamic ``bot_token`` resolvers honor rotation.
        ctx = self._request_context.get()
        ctx_token: str | None = ctx.token if ctx and ctx.token else None

        mimetype = file.get("mimetype", "")
        att_type: str = "file"
        if mimetype.startswith("image/"):
            att_type = "image"
        elif mimetype.startswith("video/"):
            att_type = "video"
        elif mimetype.startswith("audio/"):
            att_type = "audio"

        async def fetch_data() -> bytes:
            token = ctx_token if ctx_token is not None else await self._resolve_token_async()
            return await self._fetch_slack_file(url, token)  # type: ignore[arg-type]

        fetch_meta: dict[str, str] = {}
        if url:
            fetch_meta["url"] = url
        if team_id:
            fetch_meta["teamId"] = team_id

        return Attachment(
            type=att_type,  # type: ignore[arg-type]
            url=url,
            name=file.get("name"),
            mime_type=file.get("mimetype"),
            size=file.get("size"),
            width=file.get("original_w"),
            height=file.get("original_h"),
            fetch_data=fetch_data if url else None,
            fetch_metadata=fetch_meta or None,
        )

    @staticmethod
    def _is_trusted_slack_download_url(url: str) -> bool:
        """Gate Slack file downloads to known Slack-owned hosts.

        We refuse to forward ``Authorization: Bearer {token}`` to an
        arbitrary URL.  After ``rehydrate_attachment`` reconstructs the
        fetch closure from serialized ``fetch_metadata``, that URL may
        have been tampered with in the state store — a crafted value
        could exfiltrate the workspace bot token.

        This is a Python-first divergence: upstream Slack adapter does not
        validate the URL.  See ``docs/UPSTREAM_SYNC.md`` Known Non-Parity.
        """
        try:
            parsed = urlparse(url)
        except (ValueError, TypeError):
            return False
        if parsed.scheme != "https":
            return False
        host = (parsed.hostname or "").lower()
        if not host:
            return False
        # Exact-match hosts
        if host in {"files.slack.com", "slack.com"}:
            return True
        # Suffix match for Slack-owned subdomains
        return host.endswith(".slack.com") or host.endswith(".slack-edge.com")

    async def _fetch_slack_file(self, url: str, token: str) -> bytes:
        """Download a file from a Slack ``url_private`` endpoint.

        Shared by :meth:`_create_attachment` (direct fetch closure) and
        :meth:`rehydrate_attachment` (reconstructed closure after JSON
        roundtrip).  Validates the host against the Slack allowlist
        before forwarding the bot token (SSRF guard).
        """
        if not self._is_trusted_slack_download_url(url):
            raise ValidationError(
                "slack",
                f"Refusing to fetch Slack file from untrusted URL: {url}",
            )

        import httpx

        async with httpx.AsyncClient() as http:
            resp = await http.get(url, headers={"Authorization": f"Bearer {token}"})
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            if "text/html" in content_type:
                raise RuntimeError(
                    "Failed to download file from Slack: received HTML login page. "
                    'Ensure your Slack app has the "files:read" OAuth scope.'
                )
            return resp.content

    def rehydrate_attachment(self, attachment: Attachment) -> Attachment:
        """Reconstruct ``fetch_data`` on a deserialized Slack attachment.

        Matches the upstream TS implementation: looks up the download URL
        (and optional ``teamId`` for multi-workspace installations) from
        ``attachment.fetch_metadata``, and rebuilds a ``fetch_data`` closure
        that resolves the workspace-specific bot token at call time.

        Returns the attachment unchanged when no URL is available.  The
        URL is re-validated inside the closure (by ``_fetch_slack_file``)
        rather than here so that a trusted-at-serialize-time URL still
        fails closed if the allowlist tightens later.
        """
        meta = attachment.fetch_metadata if attachment.fetch_metadata is not None else {}
        meta_url = meta.get("url")
        url = meta_url if meta_url is not None else attachment.url
        team_id = meta.get("teamId")
        if not url:
            return attachment

        adapter = self

        async def fetch_data() -> bytes:
            if team_id:
                installation = await adapter.get_installation(team_id)
                if installation is None:
                    raise AuthenticationError(
                        "slack",
                        f"Installation not found for team {team_id}",
                    )
                token = installation.bot_token
            else:
                # Use the async resolver so a dynamic ``bot_token`` provider
                # is invoked at fetch time (rotation-safe).
                token = await adapter._resolve_token_async()
            return await adapter._fetch_slack_file(url, token)

        return Attachment(
            type=attachment.type,
            url=attachment.url,
            name=attachment.name,
            mime_type=attachment.mime_type,
            size=attachment.size,
            width=attachment.width,
            height=attachment.height,
            data=attachment.data,
            fetch_data=fetch_data,
            fetch_metadata=attachment.fetch_metadata,
        )

    def _is_message_from_self(self, event: dict[str, Any]) -> bool:
        """Check if a Slack event is from this bot."""
        ctx = self._request_context.get()
        if ctx and ctx.bot_user_id and event.get("user") == ctx.bot_user_id:
            return True
        if self._bot_user_id and event.get("user") == self._bot_user_id:
            return True
        return bool(self._bot_id and event.get("bot_id") == self._bot_id)

    # ==================================================================
    # Table block rendering
    # ==================================================================

    def _render_with_table_blocks(self, message: AdapterPostableMessage) -> dict[str, Any] | None:
        """Try to render a message with native Slack table blocks.

        Returns ``{"text": ..., "blocks": ...}`` if the message contains tables,
        ``None`` otherwise.
        """
        ast: dict[str, Any] | None = None
        if isinstance(message, dict):
            ast = message.get("ast")  # type: ignore[union-attr]
        elif hasattr(message, "ast"):
            ast = getattr(message, "ast", None)
        elif hasattr(message, "markdown"):
            # We don't have a full markdown->AST parser in Python; skip table blocks
            return None

        if not ast:
            return None

        blocks = self._format_converter.to_blocks_with_table(ast)
        if not blocks:
            return None

        fallback_text = convert_emoji_placeholders(self._format_converter.render_postable(message), "slack")
        return {"text": fallback_text, "blocks": blocks}

    # ==================================================================
    # Post / Edit / Delete messages
    # ==================================================================

    async def post_message(self, thread_id: str, message: AdapterPostableMessage) -> RawMessage:
        """Post a message to a Slack thread."""
        message = await self._resolve_message_mentions(message, thread_id)
        decoded = self.decode_thread_id(thread_id)
        channel = decoded.channel
        thread_ts = decoded.thread_ts

        try:
            client = self._get_client()

            # Check for files to upload. ``files_upload_v2`` returns the
            # Slack-confirmed file IDs; we surface them on ``RawMessage.raw``
            # so consumers can gate on actual delivery (parity with
            # discord/telegram, which upload inline and expose the platform
            # response naturally). ``None`` means no upload happened; an empty
            # list means Slack confirmed zero attachments (a real signal).
            uploaded_file_ids: list[str] | None = None
            files = extract_files(message)
            if files:
                uploaded_file_ids = await self._upload_files(files, channel, thread_ts or None)
                has_text = (
                    isinstance(message, str)
                    or (hasattr(message, "raw") and getattr(message, "raw", None))
                    or (hasattr(message, "markdown") and getattr(message, "markdown", None))
                    or (hasattr(message, "ast") and getattr(message, "ast", None))
                )
                card = extract_card(message)
                if not (has_text or card):
                    return RawMessage(
                        id=f"file-{int(time.time() * 1000)}",
                        thread_id=thread_id,
                        raw=self._augment_raw_with_uploads({"files": files}, uploaded_file_ids),
                    )

            card = extract_card(message)
            if card:
                blocks = card_to_block_kit(card)
                fallback_text = card_to_fallback_text(card)
                self._logger.debug(
                    "Slack API: chat.postMessage (blocks)",
                    {"channel": channel, "threadTs": thread_ts, "blockCount": len(blocks)},
                )
                result = await client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts or None,
                    text=fallback_text,
                    blocks=blocks,
                    unfurl_links=False,
                    unfurl_media=False,
                )
                return RawMessage(
                    id=result.get("ts", ""),
                    thread_id=thread_id,
                    raw=self._augment_raw_with_uploads(
                        result.data if hasattr(result, "data") else result,
                        uploaded_file_ids,
                    ),
                )

            # Table blocks
            table_result = self._render_with_table_blocks(message)
            if table_result:
                result = await client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts or None,
                    text=table_result["text"],
                    blocks=table_result["blocks"],
                    unfurl_links=False,
                    unfurl_media=False,
                )
                return RawMessage(
                    id=result.get("ts", ""),
                    thread_id=thread_id,
                    raw=self._augment_raw_with_uploads(
                        result.data if hasattr(result, "data") else result,
                        uploaded_file_ids,
                    ),
                )

            # Regular text
            text = convert_emoji_placeholders(self._format_converter.render_postable(message), "slack")
            self._logger.debug(
                "Slack API: chat.postMessage",
                {"channel": channel, "threadTs": thread_ts, "textLength": len(text)},
            )
            result = await client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts or None,
                text=text,
                unfurl_links=False,
                unfurl_media=False,
            )
            return RawMessage(
                id=result.get("ts", ""),
                thread_id=thread_id,
                raw=self._augment_raw_with_uploads(
                    result.data if hasattr(result, "data") else result,
                    uploaded_file_ids,
                ),
            )
        except Exception as error:
            self._handle_slack_error(error)

    @staticmethod
    def _augment_raw_with_uploads(raw: Any, uploaded_file_ids: list[str] | None) -> Any:
        """Add Slack-confirmed file IDs to a ``RawMessage.raw`` payload.

        Returns ``raw`` unchanged when no upload occurred (``uploaded_file_ids``
        is ``None``). Otherwise returns a NEW dict that merges the existing raw
        (Slack never returns an ``uploaded_file_ids`` key, so this is additive
        and non-breaking) with the confirmed IDs. An empty list is preserved —
        it signals that Slack confirmed zero attachments.
        """
        if uploaded_file_ids is None:
            return raw
        base = raw if isinstance(raw, dict) else {}
        return {**base, "uploaded_file_ids": uploaded_file_ids}

    async def edit_message(
        self,
        thread_id: str,
        message_id: str,
        message: AdapterPostableMessage,
    ) -> RawMessage:
        """Edit a message in a Slack thread."""
        message = await self._resolve_message_mentions(message, thread_id)

        # Handle ephemeral messages via response_url
        ephemeral = self._decode_ephemeral_message_id(message_id)
        if ephemeral:
            decoded = self.decode_thread_id(thread_id)
            result = await self._send_to_response_url(
                ephemeral["response_url"],
                "replace",
                message=message,
                thread_ts=decoded.thread_ts,
            )
            return RawMessage(
                id=ephemeral["message_ts"],
                thread_id=thread_id,
                raw={"ephemeral": True, **result},
            )

        decoded = self.decode_thread_id(thread_id)
        channel = decoded.channel

        try:
            client = self._get_client()
            card = extract_card(message)

            if card:
                blocks = card_to_block_kit(card)
                fallback_text = card_to_fallback_text(card)
                result = await client.chat_update(channel=channel, ts=message_id, text=fallback_text, blocks=blocks)
                return RawMessage(
                    id=result.get("ts", ""),
                    thread_id=thread_id,
                    raw=result.data if hasattr(result, "data") else result,
                )

            table_result = self._render_with_table_blocks(message)
            if table_result:
                result = await client.chat_update(
                    channel=channel,
                    ts=message_id,
                    text=table_result["text"],
                    blocks=table_result["blocks"],
                )
                return RawMessage(
                    id=result.get("ts", ""),
                    thread_id=thread_id,
                    raw=result.data if hasattr(result, "data") else result,
                )

            text = convert_emoji_placeholders(self._format_converter.render_postable(message), "slack")
            result = await client.chat_update(channel=channel, ts=message_id, text=text)
            return RawMessage(
                id=result.get("ts", ""),
                thread_id=thread_id,
                raw=result.data if hasattr(result, "data") else result,
            )
        except Exception as error:
            self._handle_slack_error(error)

    async def delete_message(self, thread_id: str, message_id: str) -> None:
        """Delete a message from a Slack thread."""
        ephemeral = self._decode_ephemeral_message_id(message_id)
        if ephemeral:
            await self._send_to_response_url(ephemeral["response_url"], "delete")
            return

        decoded = self.decode_thread_id(thread_id)
        channel = decoded.channel

        try:
            client = self._get_client()
            self._logger.debug("Slack API: chat.delete", {"channel": channel, "messageId": message_id})
            await client.chat_delete(channel=channel, ts=message_id)
        except Exception as error:
            self._handle_slack_error(error)

    # ==================================================================
    # Reactions
    # ==================================================================

    async def add_reaction(self, thread_id: str, message_id: str, emoji: EmojiValue | str) -> None:
        """Add a reaction to a message."""
        decoded = self.decode_thread_id(thread_id)
        channel = decoded.channel
        slack_emoji = emoji_to_slack(emoji)
        name = slack_emoji.replace(":", "")

        try:
            client = self._get_client()
            self._logger.debug(
                "Slack API: reactions.add",
                {"channel": channel, "messageId": message_id, "emoji": name},
            )
            await client.reactions_add(channel=channel, timestamp=message_id, name=name)
        except Exception as error:
            self._handle_slack_error(error)

    async def remove_reaction(self, thread_id: str, message_id: str, emoji: EmojiValue | str) -> None:
        """Remove a reaction from a message."""
        decoded = self.decode_thread_id(thread_id)
        channel = decoded.channel
        slack_emoji = emoji_to_slack(emoji)
        name = slack_emoji.replace(":", "")

        try:
            client = self._get_client()
            self._logger.debug(
                "Slack API: reactions.remove",
                {"channel": channel, "messageId": message_id, "emoji": name},
            )
            await client.reactions_remove(channel=channel, timestamp=message_id, name=name)
        except Exception as error:
            self._handle_slack_error(error)

    # ==================================================================
    # Typing indicator
    # ==================================================================

    async def start_typing(self, thread_id: str, status: str | None = None) -> None:
        """Show typing / status indicator in the thread.

        Uses Slack's ``assistant.threads.setStatus`` API when available.
        """
        decoded = self.decode_thread_id(thread_id)
        channel = decoded.channel
        thread_ts = decoded.thread_ts
        if not thread_ts:
            self._logger.debug("Slack: startTyping skipped - no thread context")
            return

        status_text = status or "Typing..."
        self._logger.debug(
            "Slack API: assistant.threads.setStatus",
            {"channel": channel, "threadTs": thread_ts, "status": status_text},
        )
        try:
            client = self._get_client()
            await client.assistant_threads_setStatus(
                channel_id=channel,
                thread_ts=thread_ts,
                status=status_text,
                loading_messages=[status_text],
            )
        except Exception as exc:
            self._logger.warn(
                "Slack API: assistant.threads.setStatus failed",
                {"channel": channel, "threadTs": thread_ts, "error": exc},
            )

    # ==================================================================
    # Streaming
    # ==================================================================

    async def stream(
        self,
        thread_id: str,
        text_stream: AsyncIterable[str | StreamChunk],
        options: StreamOptions | None = None,
    ) -> RawMessage:
        """Stream a message using Slack's native streaming API.

        Consumes an async iterable of text chunks and/or structured
        ``StreamChunk`` objects and streams them to Slack.

        Requires ``recipient_user_id`` and ``recipient_team_id`` in *options*.
        """
        if not options or not (options.recipient_user_id and options.recipient_team_id):
            raise ValidationError(
                "slack",
                "Slack streaming requires recipient_user_id and recipient_team_id in options",
            )

        decoded = self.decode_thread_id(thread_id)
        channel = decoded.channel
        # Normalize empty thread_ts to None to avoid Slack API "invalid_thread_ts" errors.
        # ``chat.startStream`` rejects an empty thread_ts (top-level DMs have no
        # parent thread to attach to), but ``chat.postMessage`` accepts it.
        # Degrade DMs to a single accumulated post_message call so streaming
        # replies aren't silently dropped (chat-sdk-python#94).
        thread_ts = decoded.thread_ts or None
        if not thread_ts:
            self._logger.debug(
                "Slack: stream degraded to post_message - no thread context",
                {"channel": channel},
            )
            accumulated = ""
            async for chunk in text_stream:
                if isinstance(chunk, str):
                    accumulated += chunk
                elif hasattr(chunk, "type") and chunk.type == "markdown_text":  # type: ignore[union-attr]
                    accumulated += chunk.text  # type: ignore[union-attr]
            return await self.post_message(thread_id, PostableMarkdown(markdown=accumulated))
        self._logger.debug("Slack: starting stream", {"channel": channel, "threadTs": thread_ts})

        token = self._get_token()
        client = self._get_client(token)

        stream_kwargs: dict[str, Any] = {
            "channel": channel,
            "thread_ts": thread_ts,
            "recipient_user_id": options.recipient_user_id,
            "recipient_team_id": options.recipient_team_id,
        }
        if options.task_display_mode:
            stream_kwargs["task_display_mode"] = options.task_display_mode

        streamer = await client.chat_stream(**stream_kwargs)

        first = True
        last_appended = ""

        # Use StreamingMarkdownRenderer for safe incremental rendering
        from chat_sdk.shared.streaming_markdown import StreamingMarkdownRenderer

        renderer = StreamingMarkdownRenderer(wrap_tables_for_append=False)
        structured_chunks_supported = True

        async def flush_markdown_delta(delta: str) -> None:
            nonlocal first
            if not delta:
                return
            if first:
                await streamer.append(markdown_text=delta, token=token)
                first = False
            else:
                await streamer.append(markdown_text=delta)

        async def send_structured_chunk(chunk: StreamChunk | dict[str, Any]) -> None:
            nonlocal first, last_appended, structured_chunks_supported
            if not structured_chunks_supported:
                return
            committable = renderer.get_committable_text()
            delta = committable[len(last_appended) :]
            await flush_markdown_delta(delta)
            last_appended = committable

            def _read(name: str) -> Any:
                if isinstance(chunk, dict):
                    return chunk.get(name)
                return getattr(chunk, name, None)

            chunk_type = _read("type")
            if not chunk_type:
                self._logger.warn(
                    "Slack stream: ignoring chunk with no `type` field",
                    {"chunkRepr": repr(chunk)[:200]},
                )
                return

            try:
                chunk_data: dict[str, Any] = {"type": chunk_type}
                for field_name in ("id", "title", "status", "output", "text"):
                    value = _read(field_name)
                    if value is not None:
                        chunk_data[field_name] = value

                if first:
                    await streamer.append(chunks=[chunk_data], token=token)
                    first = False
                else:
                    await streamer.append(chunks=[chunk_data])
            except Exception as exc:
                structured_chunks_supported = False
                self._logger.warn(
                    "Slack stream: structured-chunk append failed, falling back to "
                    "text-only for the rest of this stream. Likely causes: missing "
                    "`assistant_view` / `assistant:write` scope on the app manifest, "
                    "malformed chunk payload, or a transient Slack API error.",
                    {"chunkType": chunk_type, "error": exc},
                )

        async def push_text_and_flush(text: str) -> None:
            nonlocal last_appended
            renderer.push(text)
            committable = renderer.get_committable_text()
            delta = committable[len(last_appended) :]
            await flush_markdown_delta(delta)
            last_appended = committable

        async for chunk in text_stream:
            if isinstance(chunk, str):
                await push_text_and_flush(chunk)
            elif isinstance(chunk, dict) and chunk.get("type") == "markdown_text":
                # Dict-shaped StreamChunks are part of the contract: the
                # `_from_full_stream` normalizer in thread.py forwards dict
                # `{type: "markdown_text", ...}` items unchanged, so adapters
                # must handle them the same as the dataclass form.
                text_value = chunk.get("text")
                if isinstance(text_value, str) and text_value:
                    await push_text_and_flush(text_value)
            elif hasattr(chunk, "type") and chunk.type == "markdown_text":  # type: ignore[union-attr]
                await push_text_and_flush(chunk.text)  # type: ignore[union-attr]
            else:
                await send_structured_chunk(chunk)

        # Flush remaining (finish releases all held-back content)
        final_committable = renderer.finish()
        final_delta = final_committable[len(last_appended) :]
        await flush_markdown_delta(final_delta)

        stop_kwargs: dict[str, Any] = {}
        if options.stop_blocks:
            stop_kwargs["blocks"] = options.stop_blocks
        result = await streamer.stop(**stop_kwargs) if stop_kwargs else await streamer.stop()

        message_ts = ""
        if isinstance(result, dict):
            message_ts = (result.get("message") or {}).get("ts") or result.get("ts", "")
        elif hasattr(result, "data"):
            data = result.data
            message_ts = (data.get("message") or {}).get("ts") or data.get("ts", "")

        self._logger.debug("Slack: stream complete", {"messageId": message_ts})
        return RawMessage(id=message_ts, thread_id=thread_id, raw=result)

    # ==================================================================
    # Ephemeral messages
    # ==================================================================

    async def post_ephemeral(
        self,
        thread_id: str,
        user_id: str,
        message: AdapterPostableMessage,
    ) -> EphemeralMessage:
        """Post an ephemeral (user-only visible) message."""
        message = await self._resolve_message_mentions(message, thread_id)
        decoded = self.decode_thread_id(thread_id)
        channel = decoded.channel
        thread_ts = decoded.thread_ts

        try:
            client = self._get_client()
            card = extract_card(message)

            if card:
                blocks = card_to_block_kit(card)
                fallback_text = card_to_fallback_text(card)
                result = await client.chat_postEphemeral(
                    channel=channel,
                    thread_ts=thread_ts or None,
                    user=user_id,
                    text=fallback_text,
                    blocks=blocks,
                )
                return EphemeralMessage(
                    id=result.get("message_ts", ""),
                    thread_id=thread_id,
                    used_fallback=False,
                    raw=result.data if hasattr(result, "data") else result,
                )

            table_result = self._render_with_table_blocks(message)
            if table_result:
                result = await client.chat_postEphemeral(
                    channel=channel,
                    thread_ts=thread_ts or None,
                    user=user_id,
                    text=table_result["text"],
                    blocks=table_result["blocks"],
                )
                return EphemeralMessage(
                    id=result.get("message_ts", ""),
                    thread_id=thread_id,
                    used_fallback=False,
                    raw=result.data if hasattr(result, "data") else result,
                )

            text = convert_emoji_placeholders(self._format_converter.render_postable(message), "slack")
            result = await client.chat_postEphemeral(
                channel=channel,
                thread_ts=thread_ts or None,
                user=user_id,
                text=text,
            )
            return EphemeralMessage(
                id=result.get("message_ts", ""),
                thread_id=thread_id,
                used_fallback=False,
                raw=result.data if hasattr(result, "data") else result,
            )
        except Exception as error:
            self._handle_slack_error(error)

    # ==================================================================
    # Schedule messages
    # ==================================================================

    async def schedule_message(
        self,
        thread_id: str,
        message: AdapterPostableMessage,
        post_at: datetime,
    ) -> ScheduledMessage:
        """Schedule a message for future delivery."""
        message = await self._resolve_message_mentions(message, thread_id)
        decoded = self.decode_thread_id(thread_id)
        channel = decoded.channel
        thread_ts = decoded.thread_ts
        post_at_unix = int(post_at.timestamp())

        if post_at_unix <= int(time.time()):
            raise ValidationError("slack", "post_at must be in the future")

        files = extract_files(message)
        if files:
            raise ValidationError("slack", "File uploads are not supported in scheduled messages")

        # For multi-workspace mode, snapshot the per-team token from the
        # active request context — ``cancel()`` may run outside the
        # ContextVar frame. For single-workspace mode, defer resolution to
        # ``cancel()`` so dynamic resolvers honor token rotation between
        # ``schedule_message()`` and ``cancel()`` (Slack rotated tokens have
        # a 12h TTL and scheduled messages can outlive their schedule-time
        # token).
        ctx = self._request_context.get()
        ctx_token: str | None = ctx.token if ctx and ctx.token else None
        token = ctx_token if ctx_token is not None else await self._resolve_token_async()

        try:
            client = self._get_client(token)
            card = extract_card(message)

            if card:
                blocks = card_to_block_kit(card)
                fallback_text = card_to_fallback_text(card)
                result = await client.chat_scheduleMessage(
                    channel=channel,
                    thread_ts=thread_ts or None,
                    post_at=post_at_unix,
                    text=fallback_text,
                    blocks=blocks,
                    unfurl_links=False,
                    unfurl_media=False,
                )
            else:
                text = convert_emoji_placeholders(self._format_converter.render_postable(message), "slack")
                result = await client.chat_scheduleMessage(
                    channel=channel,
                    thread_ts=thread_ts or None,
                    post_at=post_at_unix,
                    text=text,
                    unfurl_links=False,
                    unfurl_media=False,
                )

            scheduled_message_id = result.get("scheduled_message_id", "")
            adapter = self

            async def cancel() -> None:
                # Multi-workspace: use the snapshotted ctx_token (resolver
                # runs outside the request frame). Single-workspace: re-
                # resolve so token rotation between schedule + cancel works.
                cancel_token = ctx_token if ctx_token is not None else await adapter._resolve_token_async()
                c = adapter._get_client(cancel_token)
                await c.chat_deleteScheduledMessage(channel=channel, scheduled_message_id=scheduled_message_id)

            return ScheduledMessage(
                scheduled_message_id=scheduled_message_id,
                channel_id=channel,
                post_at=post_at,
                raw=result.data if hasattr(result, "data") else result,
                _cancel=cancel,
            )
        except Exception as error:
            self._handle_slack_error(error)

    # ==================================================================
    # Open DM
    # ==================================================================

    async def open_dm(self, user_id: str) -> str:
        """Open a DM conversation with a user. Returns a thread ID."""
        try:
            client = self._get_client()
            self._logger.debug("Slack API: conversations.open", {"userId": user_id})
            result = await client.conversations_open(users=user_id)
            channel_info = result.get("channel", {})
            channel_id = channel_info.get("id")
            if not channel_id:
                raise RuntimeError("Failed to open DM - no channel returned")

            return self.encode_thread_id(SlackThreadId(channel=channel_id, thread_ts=""))
        except Exception as error:
            self._handle_slack_error(error)

    # ==================================================================
    # Open modal
    # ==================================================================

    async def open_modal(self, trigger_id: str, modal: dict[str, Any], context_id: str | None = None) -> dict[str, str]:
        """Open a Slack modal using views.open."""
        metadata = encode_modal_metadata(
            ModalMetadata(
                context_id=context_id,
                private_metadata=modal.get("private_metadata"),
            )
        )
        view = modal_to_slack_view(cast(ModalElement, modal), metadata)

        self._logger.debug(
            "Slack API: views.open",
            {"triggerId": trigger_id, "callbackId": modal.get("callback_id")},
        )

        try:
            client = self._get_client()
            result = await client.views_open(trigger_id=trigger_id, view=view)
            view_id = (result.get("view") or {}).get("id", "")
            return {"viewId": view_id}
        except Exception as error:
            self._handle_slack_error(error)

    async def update_modal(self, view_id: str, modal: dict[str, Any]) -> dict[str, str]:
        """Update an existing modal using views.update."""
        view = modal_to_slack_view(cast(ModalElement, modal))

        try:
            client = self._get_client()
            result = await client.views_update(view_id=view_id, view=view)
            new_view_id = (result.get("view") or {}).get("id", "")
            return {"viewId": new_view_id}
        except Exception as error:
            self._handle_slack_error(error)

    # ==================================================================
    # File uploads
    # ==================================================================

    async def _upload_files(
        self,
        files: list[FileUpload],
        channel: str,
        thread_ts: str | None = None,
    ) -> list[str]:
        """Upload files to Slack and share them to a channel."""
        file_uploads = []
        for file in files:
            try:
                file_uploads.append({"file": file.data, "filename": file.filename})
            except Exception as exc:
                self._logger.error(
                    "Failed to prepare file for upload",
                    {"filename": file.filename, "error": exc},
                )

        if not file_uploads:
            return []

        self._logger.debug(
            "Slack API: files.uploadV2 (batch)",
            {"fileCount": len(file_uploads), "filenames": [f["filename"] for f in file_uploads]},
        )

        client = self._get_client()
        kwargs: dict[str, Any] = {"channel": channel, "file_uploads": file_uploads}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts

        result = await client.files_upload_v2(**kwargs)
        file_ids: list[str] = []
        result_data = result.data if hasattr(result, "data") else result
        for uploaded in result_data.get("files") or []:
            for f in uploaded.get("files") or []:
                if f.get("id"):
                    file_ids.append(f["id"])

        return file_ids

    # ==================================================================
    # Fetch messages
    # ==================================================================

    async def fetch_messages(self, thread_id: str, options: FetchOptions | None = None) -> FetchResult:
        """Fetch messages from a Slack thread."""
        opts = options or FetchOptions()
        decoded = self.decode_thread_id(thread_id)
        channel = decoded.channel
        thread_ts = decoded.thread_ts
        direction = getattr(opts, "direction", "backward") or "backward"
        limit = getattr(opts, "limit", 100) if getattr(opts, "limit", 100) is not None else 100

        try:
            if direction == "forward":
                return await self._fetch_messages_forward(
                    channel, thread_ts, thread_id, limit, getattr(opts, "cursor", None)
                )
            return await self._fetch_messages_backward(
                channel, thread_ts, thread_id, limit, getattr(opts, "cursor", None)
            )
        except Exception as error:
            self._handle_slack_error(error)

    async def _fetch_messages_forward(
        self,
        channel: str,
        thread_ts: str,
        thread_id: str,
        limit: int,
        cursor: str | None = None,
    ) -> FetchResult:
        client = self._get_client()
        result = await client.conversations_replies(channel=channel, ts=thread_ts, limit=limit, cursor=cursor)
        slack_messages = result.get("messages", [])
        next_cursor = (result.get("response_metadata") or {}).get("next_cursor")

        messages = await asyncio.gather(*(self._parse_slack_message(msg, thread_id) for msg in slack_messages))
        return FetchResult(messages=list(messages), next_cursor=next_cursor or None)

    async def _fetch_messages_backward(
        self,
        channel: str,
        thread_ts: str,
        thread_id: str,
        limit: int,
        cursor: str | None = None,
    ) -> FetchResult:
        latest = cursor or None
        fetch_limit = min(1000, max(limit * 2, 200))

        client = self._get_client()
        result = await client.conversations_replies(
            channel=channel, ts=thread_ts, limit=fetch_limit, latest=latest, inclusive=False
        )
        slack_messages = result.get("messages", [])

        start_index = max(0, len(slack_messages) - limit)
        selected = slack_messages[start_index:]

        messages = await asyncio.gather(*(self._parse_slack_message(msg, thread_id) for msg in selected))

        next_cursor: str | None = None
        if (start_index > 0 or result.get("has_more")) and selected:
            oldest = selected[0]
            if oldest.get("ts"):
                next_cursor = oldest["ts"]

        return FetchResult(messages=list(messages), next_cursor=next_cursor)

    async def fetch_message(self, thread_id: str, message_id: str) -> Message | None:
        """Fetch a single message by ID (timestamp)."""
        decoded = self.decode_thread_id(thread_id)
        channel = decoded.channel
        thread_ts = decoded.thread_ts

        try:
            client = self._get_client()
            result = await client.conversations_replies(
                channel=channel, ts=thread_ts, oldest=message_id, inclusive=True, limit=1
            )
            messages = result.get("messages", [])
            target = next((m for m in messages if m.get("ts") == message_id), None)
            if not target:
                return None
            return await self._parse_slack_message(target, thread_id)
        except Exception as error:
            self._handle_slack_error(error)

    async def fetch_thread(self, thread_id: str) -> ThreadInfo:
        """Fetch thread info."""
        decoded = self.decode_thread_id(thread_id)
        channel = decoded.channel
        thread_ts = decoded.thread_ts

        try:
            client = self._get_client()
            result = await client.conversations_info(channel=channel)
            channel_info = result.get("channel", {})

            if channel_info.get("is_ext_shared"):
                self._external_channels.add(channel)

            visibility: ChannelVisibility = "unknown"
            if channel_info.get("is_ext_shared"):
                visibility = "external"
            elif channel_info.get("is_private") or channel.startswith("D"):
                visibility = "private"
            elif channel.startswith("C"):
                visibility = "workspace"

            return ThreadInfo(
                id=thread_id,
                channel_id=channel,
                channel_name=channel_info.get("name"),
                channel_visibility=visibility,
                metadata={"threadTs": thread_ts, "channel": channel_info},
            )
        except Exception as error:
            self._handle_slack_error(error)

    # ==================================================================
    # Channel-level methods
    # ==================================================================

    def channel_id_from_thread_id(self, thread_id: str) -> str:
        """Derive channel ID from a Slack thread ID."""
        decoded = self.decode_thread_id(thread_id)
        return f"slack:{decoded.channel}"

    async def fetch_channel_messages(self, channel_id: str, options: FetchOptions | None = None) -> FetchResult:
        """Fetch channel-level messages (conversations.history)."""
        channel = channel_id.split(":")[1] if ":" in channel_id else channel_id
        if not channel:
            raise ValidationError("slack", f"Invalid Slack channel ID: {channel_id}")

        opts = options or FetchOptions()
        direction = getattr(opts, "direction", "backward") or "backward"
        limit = getattr(opts, "limit", 100) if getattr(opts, "limit", 100) is not None else 100

        try:
            if direction == "forward":
                return await self._fetch_channel_messages_forward(channel, limit, getattr(opts, "cursor", None))
            return await self._fetch_channel_messages_backward(channel, limit, getattr(opts, "cursor", None))
        except Exception as error:
            self._handle_slack_error(error)

    async def _fetch_channel_messages_forward(self, channel: str, limit: int, cursor: str | None = None) -> FetchResult:
        client = self._get_client()
        kwargs: dict[str, Any] = {"channel": channel, "limit": limit}
        if cursor:
            kwargs["oldest"] = cursor
            kwargs["inclusive"] = False
        result = await client.conversations_history(**kwargs)

        slack_messages = list(reversed(result.get("messages", [])))
        messages = await asyncio.gather(
            *(
                self._parse_slack_message(
                    msg,
                    f"slack:{channel}:{msg.get('thread_ts') or msg.get('ts', '')}",
                    skip_self_mention=False,
                )
                for msg in slack_messages
            )
        )

        next_cursor: str | None = None
        if result.get("has_more") and slack_messages:
            newest = slack_messages[-1]
            if newest.get("ts"):
                next_cursor = newest["ts"]

        return FetchResult(messages=list(messages), next_cursor=next_cursor)

    async def _fetch_channel_messages_backward(
        self, channel: str, limit: int, cursor: str | None = None
    ) -> FetchResult:
        client = self._get_client()
        kwargs: dict[str, Any] = {"channel": channel, "limit": limit}
        if cursor:
            kwargs["latest"] = cursor
            kwargs["inclusive"] = False
        result = await client.conversations_history(**kwargs)

        slack_messages = result.get("messages", [])
        chronological = list(reversed(slack_messages))

        messages = await asyncio.gather(
            *(
                self._parse_slack_message(
                    msg,
                    f"slack:{channel}:{msg.get('thread_ts') or msg.get('ts', '')}",
                    skip_self_mention=False,
                )
                for msg in chronological
            )
        )

        next_cursor: str | None = None
        if result.get("has_more") and chronological:
            oldest = chronological[0]
            if oldest.get("ts"):
                next_cursor = oldest["ts"]

        return FetchResult(messages=list(messages), next_cursor=next_cursor)

    async def list_threads(self, channel_id: str, options: ListThreadsOptions | None = None) -> ListThreadsResult:
        """List threads in a Slack channel."""
        channel = channel_id.split(":")[1] if ":" in channel_id else channel_id
        if not channel:
            raise ValidationError("slack", f"Invalid Slack channel ID: {channel_id}")

        opts = options or ListThreadsOptions()
        limit = getattr(opts, "limit", 50) if getattr(opts, "limit", 50) is not None else 50

        try:
            client = self._get_client()
            result = await client.conversations_history(
                channel=channel,
                limit=min(limit * 3, 200),
                cursor=getattr(opts, "cursor", None),
            )

            slack_messages = result.get("messages", [])
            thread_messages = [m for m in slack_messages if (m.get("reply_count") or 0) > 0]
            selected = thread_messages[:limit]

            threads: list[ThreadSummary] = []
            for msg in selected:
                thread_ts = msg.get("ts", "")
                tid = f"slack:{channel}:{thread_ts}"
                root_message = await self._parse_slack_message(msg, tid, skip_self_mention=False)

                last_reply_at: datetime | None = None
                if msg.get("latest_reply"):
                    try:
                        last_reply_at = datetime.fromtimestamp(float(msg["latest_reply"]), tz=timezone.utc)
                    except (ValueError, TypeError, OSError):
                        last_reply_at = None

                threads.append(
                    ThreadSummary(
                        id=tid,
                        root_message=root_message,
                        reply_count=msg.get("reply_count"),
                        last_reply_at=last_reply_at,
                    )
                )

            next_cursor = (result.get("response_metadata") or {}).get("next_cursor")
            return ListThreadsResult(threads=threads, next_cursor=next_cursor or None)
        except Exception as error:
            self._handle_slack_error(error)

    async def fetch_channel_info(self, channel_id: str) -> ChannelInfo:
        """Fetch Slack channel info/metadata."""
        channel = channel_id.split(":")[1] if ":" in channel_id else channel_id
        if not channel:
            raise ValidationError("slack", f"Invalid Slack channel ID: {channel_id}")

        try:
            client = self._get_client()
            result = await client.conversations_info(channel=channel)
            info = result.get("channel", {})

            if info.get("is_ext_shared"):
                self._external_channels.add(channel)

            visibility: ChannelVisibility = "unknown"
            if info.get("is_ext_shared"):
                visibility = "external"
            elif info.get("is_im") or info.get("is_mpim") or info.get("is_private") or channel.startswith("D"):
                visibility = "private"
            elif channel.startswith("C"):
                visibility = "workspace"

            return ChannelInfo(
                id=channel_id,
                name=f"#{info['name']}" if info.get("name") else None,
                is_dm=bool(info.get("is_im") or info.get("is_mpim")),
                channel_visibility=visibility,
                member_count=info.get("num_members"),
                metadata={
                    "purpose": (info.get("purpose") or {}).get("value"),
                    "topic": (info.get("topic") or {}).get("value"),
                },
            )
        except Exception as error:
            self._handle_slack_error(error)

    async def post_channel_message(self, channel_id: str, message: AdapterPostableMessage) -> RawMessage:
        """Post a top-level message to a channel (not in a thread)."""
        channel = channel_id.split(":")[1] if ":" in channel_id else channel_id
        if not channel:
            raise ValidationError("slack", f"Invalid Slack channel ID: {channel_id}")

        synthetic_thread_id = f"slack:{channel}:"
        return await self.post_message(synthetic_thread_id, message)

    # ==================================================================
    # Thread ID encoding / decoding
    # ==================================================================

    def encode_thread_id(self, platform_data: SlackThreadId) -> str:
        """Encode a SlackThreadId to a string."""
        return f"slack:{platform_data.channel}:{platform_data.thread_ts}"

    def decode_thread_id(self, thread_id: str) -> SlackThreadId:
        """Decode a thread ID string to SlackThreadId."""
        parts = thread_id.split(":")
        if len(parts) < 2 or len(parts) > 3 or parts[0] != "slack":
            raise ValidationError("slack", f"Invalid Slack thread ID: {thread_id}")
        return SlackThreadId(
            channel=parts[1],
            thread_ts=parts[2] if len(parts) == 3 else "",
        )

    def is_dm(self, thread_id: str) -> bool:
        """Check if a thread is a direct message conversation."""
        decoded = self.decode_thread_id(thread_id)
        return decoded.channel.startswith("D")

    def get_channel_visibility(self, thread_id: str) -> ChannelVisibility:
        """Get the visibility scope of the channel containing the thread."""
        decoded = self.decode_thread_id(thread_id)
        channel = decoded.channel

        if channel in self._external_channels:
            return "external"
        if channel.startswith("G") or channel.startswith("D"):
            return "private"
        if channel.startswith("C"):
            return "workspace"
        return "unknown"

    def parse_message(self, raw: dict[str, Any]) -> Message:
        """Parse a raw Slack event into a Message (synchronous)."""
        event = raw
        thread_ts = event.get("thread_ts") or event.get("ts", "")
        thread_id = self.encode_thread_id(SlackThreadId(channel=event.get("channel", ""), thread_ts=thread_ts))
        return self._parse_slack_message_sync(event, thread_id)

    def render_formatted(self, content: FormattedContent) -> str:
        """Render formatted content (AST) to Slack mrkdwn."""
        return self._format_converter.from_ast(content)

    # ==================================================================
    # Error handling
    # ==================================================================

    def _handle_slack_error(self, error: Any) -> NoReturn:
        """Re-raise Slack errors with appropriate SDK error types.

        Always raises — the `NoReturn` annotation lets type checkers skip
        the "missing return" warning for callers that rely on this to
        propagate out of a `try/except` block.
        """
        # slack_sdk's SlackApiError has a .response attribute (SlackResponse)
        # SlackResponse has a .data dict and an .get() method
        resp = getattr(error, "response", None)
        error_code: str | None = None
        if resp is not None:
            # SlackResponse has .data dict or direct attribute access
            if hasattr(resp, "data") and isinstance(resp.data, dict):
                error_code = resp.data.get("error")
            elif isinstance(resp, dict):
                error_code = resp.get("error")

        # Invalidate cached client on auth errors (token revocation / invalid_auth)
        if error_code in ("invalid_auth", "token_revoked", "account_inactive"):
            try:
                token = self._get_token()
                self._invalidate_client(token)
            except AuthenticationError:
                pass

        # Check for rate limiting
        if error_code == "ratelimited":
            retry_after = None
            if hasattr(resp, "headers"):
                retry_after = resp.headers.get("Retry-After")
            elif isinstance(resp, dict):
                retry_after = resp.get("headers", {}).get("Retry-After")
            retry_val = None
            if retry_after:
                try:
                    retry_val = int(retry_after)
                except (ValueError, TypeError):
                    retry_val = None
            raise AdapterRateLimitError("slack", retry_val) from error

        raise error  # type: ignore[misc]

    # ==================================================================
    # Ephemeral message ID encoding
    # ==================================================================

    def _encode_ephemeral_message_id(self, message_ts: str, response_url: str, user_id: str) -> str:
        data = json.dumps({"responseUrl": response_url, "userId": user_id})
        encoded = base64.b64encode(data.encode("utf-8")).decode("ascii")
        return f"ephemeral:{message_ts}:{encoded}"

    def _decode_ephemeral_message_id(self, message_id: str) -> dict[str, str] | None:
        if not message_id.startswith("ephemeral:"):
            return None
        parts = message_id.split(":", 2)
        if len(parts) < 3:
            return None
        message_ts = parts[1]
        encoded_data = parts[2]
        try:
            decoded = base64.b64decode(encoded_data).decode("utf-8")
            try:
                data = json.loads(decoded)
                if data.get("responseUrl") and data.get("userId"):
                    return {
                        "message_ts": message_ts,
                        "response_url": data["responseUrl"],
                        "user_id": data["userId"],
                    }
            except (json.JSONDecodeError, ValueError):
                return {"message_ts": message_ts, "response_url": decoded, "user_id": ""}
            return None
        except Exception:
            self._logger.warn("Failed to decode ephemeral messageId", {"messageId": message_id})
            return None

    # ==================================================================
    # Response URL
    # ==================================================================

    async def _send_to_response_url(
        self,
        response_url: str,
        action: str,
        *,
        message: AdapterPostableMessage | None = None,
        thread_ts: str | None = None,
    ) -> dict[str, Any]:
        """Send a request to Slack's response_url to modify an ephemeral message."""
        # Validate response_url points to Slack (prevent SSRF)
        from urllib.parse import urlparse

        parsed = urlparse(response_url)
        if not (parsed.scheme == "https" and parsed.hostname and parsed.hostname.endswith(".slack.com")):
            raise ValidationError("slack", f"Invalid response_url: must be https://*.slack.com, got {response_url}")

        import httpx

        payload: dict[str, Any]

        if action == "delete":
            payload = {"delete_original": True}
        else:
            if not message:
                raise ValidationError("slack", "Message required for replace action")

            card = extract_card(message)
            if card:
                payload = {
                    "replace_original": True,
                    "text": card_to_fallback_text(card),
                    "blocks": card_to_block_kit(card),
                }
            else:
                table_result = self._render_with_table_blocks(message)
                if table_result:
                    payload = {
                        "replace_original": True,
                        "text": table_result["text"],
                        "blocks": table_result["blocks"],
                    }
                else:
                    payload = {
                        "replace_original": True,
                        "text": convert_emoji_placeholders(self._format_converter.render_postable(message), "slack"),
                    }

            if thread_ts:
                payload["thread_ts"] = thread_ts

        self._logger.debug(
            "Slack response_url request",
            {"action": action, "threadTs": thread_ts},
        )

        async with httpx.AsyncClient() as http:
            resp = await http.post(
                response_url,
                json=payload,
                headers={"Content-Type": "application/json"},
            )

            if not resp.is_success:
                error_text = resp.text
                self._logger.error(
                    "Slack response_url failed",
                    {"action": action, "status": resp.status_code, "body": error_text},
                )
                raise RuntimeError(f"Failed to {action} via response_url: {error_text}")

            response_text = resp.text
            if response_text:
                try:
                    return json.loads(response_text)
                except (json.JSONDecodeError, ValueError):
                    return {"raw": response_text}
            return {}


# ==================================================================
# Factory
# ==================================================================


def create_slack_adapter(config: SlackAdapterConfig | None = None) -> SlackAdapter:
    """Create a new SlackAdapter instance.

    For socket mode, the factory rejects multi-workspace setups upfront —
    Socket Mode is a single-workspace transport (the WebSocket carries one
    app's events for one workspace) and silently mixing the two would mask
    a config mistake.
    """
    if config is not None and (config.mode or "webhook") == "socket" and (config.client_id or config.client_secret):
        raise ValidationError(
            "slack",
            "Multi-workspace (clientId/clientSecret) is not supported in socket mode.",
        )
    return SlackAdapter(config)
