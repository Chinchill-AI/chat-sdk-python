"""Slack-specific types for the chat-sdk Slack adapter."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypeAlias, TypedDict

from chat_sdk.logger import Logger

# ---------------------------------------------------------------------------
# Bot token resolver
# ---------------------------------------------------------------------------

# Bot token configuration. Either a static string or a zero-arg callable that
# returns either ``str`` synchronously or an awaitable resolving to ``str``.
# The callable is invoked each time a token is needed, enabling rotation or
# lazy retrieval from a secret manager.
#
# Matches the upstream TS contract:
#   ``type SlackBotToken = string | (() => string | Promise<string>)``
SlackBotTokenResolver = Callable[[], "str | Awaitable[str]"]
SlackBotToken: TypeAlias = str | SlackBotTokenResolver

# Custom webhook verifier. Receives the original request object and the raw
# body string already consumed by the adapter. Return:
#   - ``True`` (or any truthy non-string value) to accept the request as-is.
#   - A ``str`` to accept *and* substitute the verified body for downstream
#     parsing (useful when the verifier canonicalizes the payload).
#   - ``False``/falsy or raise to reject (adapter responds with 401).
#
# May be sync or async.
#
# SECURITY: When a custom verifier replaces ``signing_secret``, the adapter's
# built-in HMAC + timestamp tolerance check is bypassed. The implementer is
# responsible for:
#   - constant-time signature comparison (use ``hmac.compare_digest``, never ``==``)
#   - replay protection (validate ``x-slack-request-timestamp`` freshness)
#   - any other freshness/origin checks the platform requires
#   - body-substitution safety: when returning a ``str`` to substitute the body
#     for downstream parsing, the returned bytes MUST be derived from a
#     verified payload. Returning attacker-controlled bytes (e.g. echoing the
#     unverified raw body or splicing in untrusted fields) grants payload
#     injection — downstream parsing trusts the substituted body unconditionally.
SlackWebhookVerifier = Callable[[Any, str], "bool | str | None | Awaitable[bool | str | None]"]

# Connection mode for the Slack adapter. ``"webhook"`` (default) consumes
# events via signed HTTP POSTs from Slack. ``"socket"`` opens a long-lived
# WebSocket via Slack's Socket Mode and ACKs each event over the socket.
SlackAdapterMode = Literal["webhook", "socket"]

# =============================================================================
# Configuration
# =============================================================================


@dataclass
class SlackAdapterConfig:
    """Configuration for the Slack adapter."""

    # App-level token (xapp-...). Required when ``mode == "socket"``.
    app_token: str | None = None
    # Bot token (xoxb-...). Required for single-workspace mode. Omit for multi-workspace.
    # May be a string, or a zero-arg callable returning ``str`` or ``Awaitable[str]``
    # (called on each use to support rotation or deferred resolution from a
    # secret manager). See :data:`SlackBotToken`.
    bot_token: SlackBotToken | None = None
    # Bot user ID (will be fetched if not provided)
    bot_user_id: str | None = None
    # Slack app client ID (required for OAuth / multi-workspace)
    client_id: str | None = None
    # Slack app client secret (required for OAuth / multi-workspace)
    client_secret: str | None = None
    # Base64-encoded 32-byte AES-256-GCM encryption key.
    # If provided, bot tokens stored via set_installation() will be encrypted at rest.
    encryption_key: str | None = None
    # Prefix for the state key used to store workspace installations.
    # Defaults to ``slack:installation``. The full key will be ``{prefix}:{team_id}``
    # (or ``{prefix}:{enterprise_id}`` for Enterprise Grid org-wide installs).
    installation_key_prefix: str = "slack:installation"
    # External installation provider for multi-workspace apps using external
    # token management (e.g. Vercel Connect). When set, the adapter bypasses
    # internal StateAdapter storage for token lookups on incoming webhooks.
    #
    # For Enterprise Grid org-wide installs, ``installation_id`` will be the
    # enterprise ID; otherwise it will be the team ID.
    #
    # Precedence: a configured default ``bot_token`` (single-workspace mode,
    # static or resolver) still wins — per-installation resolution (and thus
    # this provider) only runs in multi-workspace mode. See the resolver rows
    # in docs/UPSTREAM_SYNC.md.
    installation_provider: SlackInstallationProvider | None = None
    # Logger instance for error reporting. Defaults to ConsoleLogger.
    logger: Logger | None = None
    # Connection mode: ``"webhook"`` (default) or ``"socket"``. When set to
    # ``"socket"`` the adapter opens a Slack Socket Mode WebSocket on
    # ``initialize()`` and dispatches events over it. ``signing_secret`` is
    # not required in socket mode (Slack does not sign socket events).
    mode: SlackAdapterMode = "webhook"
    # Signing secret for webhook verification. Defaults to SLACK_SIGNING_SECRET env var,
    # *unless* ``webhook_verifier`` is provided — an explicit verifier takes
    # precedence over both this field and the ``SLACK_SIGNING_SECRET`` env var,
    # so an env-configured deployment can't silently shadow the verifier the
    # caller wired up. Required in webhook mode; optional in socket mode.
    signing_secret: str | None = None
    # Custom webhook verifier. When provided, replaces the built-in HMAC + timestamp
    # check. See :data:`SlackWebhookVerifier` for the SECURITY contract — the
    # implementer is responsible for constant-time comparison and replay protection.
    # ``webhook_verifier`` takes precedence over ``signing_secret`` and the
    # ``SLACK_SIGNING_SECRET`` env var; when it is set, those are ignored.
    webhook_verifier: SlackWebhookVerifier | None = None
    # Shared secret for authenticating events forwarded from a separate
    # socket-mode listener via HTTP POST. Auto-detected from
    # SLACK_SOCKET_FORWARDING_SECRET. Falls back to ``app_token`` if not set
    # (matches upstream behavior; prefer setting this explicitly so the
    # long-lived xapp- token isn't used as a bearer credential).
    socket_forwarding_secret: str | None = None
    # Maximum number of cached AsyncWebClient instances (LRU-bounded).
    # Defaults to 100. Increase for large multi-workspace deployments.
    client_cache_max: int | None = None
    # Maximum number of seconds to wait for the initial Socket Mode WebSocket
    # handshake. If the slack_sdk ``connect()`` call hangs (e.g. Slack edge
    # is degraded), ``start_socket_mode`` raises after this many seconds so
    # ``initialize()`` doesn't block forever (hazard #11).
    connect_timeout_s: float = 30.0
    # Override bot username (optional)
    user_name: str | None = None


# =============================================================================
# Installation
# =============================================================================


@dataclass
class SlackInstallation:
    """Data stored per Slack workspace installation."""

    bot_token: str
    bot_user_id: str | None = None
    team_name: str | None = None


class SlackInstallationProvider(Protocol):
    """External installation provider for multi-workspace token management.

    Implementations resolve a :class:`SlackInstallation` from an external
    system (e.g. Vercel Connect) instead of the adapter's internal
    StateAdapter storage. ``installation_id`` is the ``enterprise_id`` for
    Enterprise Grid org-wide installs (``is_enterprise_install=True``),
    otherwise the ``team_id``. Return ``None`` when no installation exists.

    The provider is read-only: ``set_installation`` / ``delete_installation``
    / ``handle_oauth_callback`` continue to write to the internal state
    adapter, so callers using a provider should manage their own writes
    through their external system.
    """

    def get_installation(
        self, installation_id: str, is_enterprise_install: bool
    ) -> Awaitable[SlackInstallation | None]: ...


# =============================================================================
# Thread ID
# =============================================================================


@dataclass
class SlackThreadId:
    """Slack-specific thread ID data."""

    channel: str
    thread_ts: str


# =============================================================================
# Slack Event Payloads
# =============================================================================


class SlackRichTextElement(TypedDict, total=False):
    """An element inside a rich_text block section."""

    type: str
    url: str
    text: str


class SlackRichTextSection(TypedDict, total=False):
    """A section inside a rich_text block."""

    type: str
    elements: list[SlackRichTextElement]


class SlackRichTextBlock(TypedDict, total=False):
    """A rich_text block in a Slack event."""

    type: str
    elements: list[SlackRichTextSection]


class SlackFileInfo(TypedDict, total=False):
    """File metadata from a Slack event."""

    id: str
    mimetype: str
    url_private: str
    name: str
    size: int
    original_w: int
    original_h: int


class SlackEvent(TypedDict, total=False):
    """Slack event payload (raw message format)."""

    blocks: list[SlackRichTextBlock]
    bot_id: str
    channel: str
    # Channel type: "channel", "group", "mpim", or "im" (DM)
    channel_type: str
    edited: dict[str, str]  # {"ts": "..."}
    files: list[SlackFileInfo]
    # Timestamp of the latest reply (present on thread parent messages)
    latest_reply: str
    # Number of replies in the thread (present on thread parent messages)
    reply_count: int
    subtype: str
    team: str
    team_id: str
    text: str
    thread_ts: str
    ts: str
    type: str  # required
    user: str
    username: str


class SlackReactionItem(TypedDict):
    """The item a reaction was applied to."""

    type: str
    channel: str
    ts: str


class SlackReactionEvent(TypedDict, total=False):
    """Slack reaction event payload."""

    event_ts: str
    item: SlackReactionItem
    item_user: str
    reaction: str
    type: str  # "reaction_added" | "reaction_removed"
    user: str


class SlackAssistantContext(TypedDict, total=False):
    """Context from a Slack assistant thread event."""

    channel_id: str
    team_id: str
    enterprise_id: str
    thread_entry_point: str
    force_search: bool


class SlackAssistantThread(TypedDict, total=False):
    """Assistant thread info from Slack events."""

    user_id: str
    channel_id: str
    thread_ts: str
    context: SlackAssistantContext


class SlackAssistantThreadStartedEvent(TypedDict, total=False):
    """Slack assistant_thread_started event payload."""

    assistant_thread: SlackAssistantThread
    event_ts: str
    type: str  # "assistant_thread_started"


class SlackAssistantContextChangedEvent(TypedDict, total=False):
    """Slack assistant_thread_context_changed event payload."""

    assistant_thread: SlackAssistantThread
    event_ts: str
    type: str  # "assistant_thread_context_changed"


class SlackAppHomeOpenedEvent(TypedDict, total=False):
    """Slack app_home_opened event payload."""

    channel: str
    event_ts: str
    tab: str
    type: str  # "app_home_opened"
    user: str


class SlackMemberJoinedChannelEvent(TypedDict, total=False):
    """Slack member_joined_channel event payload."""

    channel: str
    channel_type: str
    event_ts: str
    inviter: str
    team: str
    type: str  # "member_joined_channel"
    user: str


class SlackUserProfile(TypedDict, total=False):
    """Slack user profile."""

    display_name: str
    real_name: str


class SlackUserInfo(TypedDict, total=False):
    """Slack user info inside a user_change event."""

    id: str
    name: str
    real_name: str
    profile: SlackUserProfile


class SlackUserChangeEvent(TypedDict, total=False):
    """Slack user_change event payload."""

    event_ts: str
    type: str  # "user_change"
    user: SlackUserInfo


# Union type for all event kinds
SlackEventUnion = (
    SlackEvent
    | SlackReactionEvent
    | SlackAssistantThreadStartedEvent
    | SlackAssistantContextChangedEvent
    | SlackAppHomeOpenedEvent
    | SlackMemberJoinedChannelEvent
    | SlackUserChangeEvent
)


class SlackWebhookPayload(TypedDict, total=False):
    """Slack webhook payload envelope."""

    challenge: str
    # Enterprise ID for Enterprise Grid org-wide installs
    enterprise_id: str
    event: Any  # SlackEventUnion
    event_id: str
    event_time: int
    # Whether this is an Enterprise Grid org-wide install
    is_enterprise_install: bool
    # Whether this event occurred in an externally shared channel (Slack Connect)
    is_ext_shared_channel: bool
    team_id: str
    type: str  # required


# =============================================================================
# Interactive Payloads
# =============================================================================


class SlackActionInfo(TypedDict, total=False):
    """A single action from a block_actions payload."""

    type: str
    action_id: str
    block_id: str
    value: str
    action_ts: str
    selected_option: dict[str, str]  # {"value": "..."}


class SlackChannelRef(TypedDict, total=False):
    """Channel reference in interactive payloads."""

    id: str
    name: str


class SlackContainerInfo(TypedDict, total=False):
    """Container info in interactive payloads."""

    type: str
    message_ts: str
    channel_id: str
    is_ephemeral: bool
    thread_ts: str


class SlackMessageRef(TypedDict, total=False):
    """Message reference in interactive payloads."""

    ts: str
    thread_ts: str


class SlackUserRef(TypedDict, total=False):
    """User reference in interactive payloads."""

    id: str
    username: str
    name: str


class SlackBlockActionsPayload(TypedDict, total=False):
    """Slack interactive payload for button clicks."""

    actions: list[SlackActionInfo]
    channel: SlackChannelRef
    container: SlackContainerInfo
    message: SlackMessageRef
    response_url: str
    trigger_id: str
    type: str  # "block_actions"
    user: SlackUserRef


class SlackViewStateInput(TypedDict, total=False):
    """A single input value in a view submission."""

    value: str
    selected_option: dict[str, str]  # {"value": "..."}


class SlackViewState(TypedDict, total=False):
    """State of a submitted view."""

    values: dict[str, dict[str, SlackViewStateInput]]


class SlackViewInfo(TypedDict, total=False):
    """View information in submission/close payloads."""

    id: str
    callback_id: str
    private_metadata: str
    state: SlackViewState


class SlackViewSubmissionPayload(TypedDict, total=False):
    """Slack view_submission payload."""

    trigger_id: str
    type: str  # "view_submission"
    user: SlackUserRef
    view: SlackViewInfo


class SlackViewClosedPayload(TypedDict, total=False):
    """Slack view_closed payload."""

    type: str  # "view_closed"
    user: SlackUserRef
    view: SlackViewInfo


SlackInteractivePayload = SlackBlockActionsPayload | SlackViewSubmissionPayload | SlackViewClosedPayload


# =============================================================================
# Cached data
# =============================================================================


@dataclass
class CachedUser:
    """Cached user info."""

    display_name: str
    real_name: str


@dataclass
class CachedChannel:
    """Cached channel info."""

    name: str


# =============================================================================
# Request context (multi-workspace)
# =============================================================================


@dataclass
class RequestContext:
    """Per-request context for multi-workspace token resolution."""

    token: str
    bot_user_id: str | None = None
    is_ext_shared_channel: bool | None = None
    # Enterprise ID for Enterprise Grid org-wide installs
    enterprise_id: str | None = None
    # Whether this request came from an Enterprise Grid org-wide install
    is_enterprise_install: bool | None = None
