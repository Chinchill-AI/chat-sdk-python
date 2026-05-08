"""Slack-specific types for the chat-sdk Slack adapter."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypedDict

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
SlackBotToken = "str | SlackBotTokenResolver"

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
SlackWebhookVerifier = Callable[[Any, str], "bool | str | None | Awaitable[bool | str | None]"]

# =============================================================================
# Configuration
# =============================================================================


@dataclass
class SlackAdapterConfig:
    """Configuration for the Slack adapter."""

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
    # Defaults to ``slack:installation``. The full key will be ``{prefix}:{team_id}``.
    installation_key_prefix: str = "slack:installation"
    # Logger instance for error reporting. Defaults to ConsoleLogger.
    logger: Logger | None = None
    # Signing secret for webhook verification. Defaults to SLACK_SIGNING_SECRET env var,
    # *unless* ``webhook_verifier`` is provided — passing an explicit verifier opts
    # out of the env fallback so a deployment-set ``SLACK_SIGNING_SECRET`` can't
    # silently shadow the verifier.
    signing_secret: str | None = None
    # Custom webhook verifier. When provided, replaces the built-in HMAC + timestamp
    # check. See :data:`SlackWebhookVerifier` for the SECURITY contract — the
    # implementer is responsible for constant-time comparison and replay protection.
    # When both ``signing_secret`` and ``webhook_verifier`` are set, ``signing_secret``
    # takes precedence.
    webhook_verifier: SlackWebhookVerifier | None = None
    # Maximum number of cached AsyncWebClient instances (LRU-bounded).
    # Defaults to 100. Increase for large multi-workspace deployments.
    client_cache_max: int | None = None
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
    event: Any  # SlackEventUnion
    event_id: str
    event_time: int
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
