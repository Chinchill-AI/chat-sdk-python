"""Slack-specific types for the chat-sdk Slack adapter."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeAlias, TypedDict

# Custom webhook verifier — defined in (and re-exported from) the low-level
# ``chat_sdk.adapters.slack.webhook`` subpath since vercel/chat#538. See
# ``webhook/types.py`` for the full SECURITY contract (constant-time
# comparison, replay protection, body-substitution safety).
from chat_sdk.adapters.slack.webhook.types import SlackWebhookVerifier
from chat_sdk.logger import Logger
from chat_sdk.shared.download import AttachmentTransport

if TYPE_CHECKING:
    import httpx

    from chat_sdk.adapters.slack.agent_context import SlackAppContext
    from chat_sdk.types import AppContextEntity

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

# Connection mode for the Slack adapter. ``"webhook"`` (default) consumes
# events via signed HTTP POSTs from Slack. ``"socket"`` opens a long-lived
# WebSocket via Slack's Socket Mode and ACKs each event over the socket.
SlackAdapterMode = Literal["webhook", "socket"]

# =============================================================================
# Agent configuration (vercel/chat 0f743c9b #698)
# =============================================================================


@dataclass
class SlackFeedbackButtonsOptions:
    """Options for the feedback buttons appended to streamed replies.

    Every field defaults (``None``) to upstream's value: ``action_id``
    ``"message_feedback"``, labels ``"Good response"`` / ``"Bad response"``,
    values ``"positive"`` / ``"negative"``.
    """

    # ``action_id`` dispatched to ``chat.on_action`` when a button is clicked.
    action_id: str | None = None
    # Label for the negative button.
    negative_label: str | None = None
    # Action value dispatched for negative clicks.
    negative_value: str | None = None
    # Label for the positive button.
    positive_label: str | None = None
    # Action value dispatched for positive clicks.
    positive_value: str | None = None


class SlackSuggestedPrompt(TypedDict):
    """A single suggested prompt shown in an assistant/agent thread (wire shape)."""

    # Full prompt text sent as the user's message when the prompt is clicked.
    message: str
    # Short label shown on the prompt button.
    title: str


@dataclass
class SlackSuggestedPromptsOptions:
    """Suggested prompts payload applied when an assistant/agent thread opens."""

    # The prompts to display. Slack shows at most 4; extras are dropped with a warning.
    prompts: list[SlackSuggestedPrompt]
    # Optional heading shown above the prompts.
    title: str | None = None


@dataclass
class SlackSuggestedPromptsContext:
    """Context passed to a dynamic ``suggested_prompts`` resolver."""

    # The DM channel the assistant/agent thread lives in.
    channel_id: str
    # The user who opened the thread.
    user_id: str
    # Enterprise the user opened the thread from (legacy assistant_view).
    enterprise_id: str | None = None
    # Active-view context entities (agent_view, when Slack folds context in).
    entities: list[AppContextEntity] | None = None
    # Workspace the thread belongs to (assistant_thread context, or the
    # ``app_home_opened`` envelope's ``authorizations[0].team_id`` / ``team_id``).
    team_id: str | None = None
    # Assistant thread root (legacy assistant_view; ``None`` under agent_view).
    thread_ts: str | None = None


# Suggested prompts configuration: a static payload, or a resolver (sync or
# async) invoked each time an assistant/agent thread opens. Return ``None``
# from the resolver to skip setting prompts for that thread.
SlackSuggestedPromptsResolver = Callable[
    [SlackSuggestedPromptsContext],
    "SlackSuggestedPromptsOptions | None | Awaitable[SlackSuggestedPromptsOptions | None]",
]
SlackSuggestedPrompts: TypeAlias = SlackSuggestedPromptsOptions | SlackSuggestedPromptsResolver

# =============================================================================
# Configuration
# =============================================================================


@dataclass
class SlackAdapterConfig:
    """Configuration for the Slack adapter."""

    # Override the Slack Web API base URL (passed as ``base_url`` to every
    # ``slack_sdk`` client the adapter builds — the default client, the
    # per-token async cache, and the synchronous ``web_client`` escape hatch).
    # Defaults to the ``SLACK_API_URL`` env var, then to slack_sdk's built-in
    # ``https://slack.com/api/``. Mirrors upstream ``config.apiUrl`` →
    # ``slackApiUrl`` (vercel/chat 6b17c60). Useful for proxies, Slack-API
    # mocks in tests, or Enterprise-routed deployments.
    api_url: str | None = None
    # Extra keyword arguments forwarded to every ``slack_sdk`` client the
    # adapter builds — the default ``AsyncWebClient``, the per-token async
    # cache, and the synchronous ``WebClient`` escape hatch. Gated on
    # ``is not None`` so an explicit empty ``{}`` still spreads (a no-op).
    #
    # DIVERGENCE (vercel/chat 8336a3e): upstream forwards ``webClientOptions``
    # to ``@slack/web-api``'s ``WebClient`` (an axios-backed client), so its
    # most useful keys are ``retryConfig`` and ``timeout``. There is no 1:1
    # mapping in ``slack_sdk``: it has no ``retryConfig``/``rejectRateLimitedCalls``
    # — retry behavior is configured via ``retry_handlers`` (a list of
    # ``slack_sdk.http_retry.RetryHandler``), and ``timeout`` is an int of
    # seconds. So these map to **slack_sdk WebClient kwargs**, not axios options.
    # Example: ``web_client_options={"timeout": 15, "retry_handlers": [...]}``.
    # ``headers`` (a dict) is deep-copied per client so cached per-token clients
    # never share a mutable dict and caller input is never mutated. See the
    # ``webClientOptions`` divergence row in docs/UPSTREAM_SYNC.md.
    #
    # Socket Mode (vercel/chat 6adca361): ``proxy`` (an HTTP proxy URL) is
    # passed to ``SocketModeClient(proxy=...)`` for the WebSocket, and
    # ``proxy``, ``ssl`` and ``api_url`` reach the ``apps.connections.open``
    # client it uses. Other keys apply only to Web API clients, and none of
    # them configure ``http_client_factory`` or ``file_transport``.
    web_client_options: dict[str, Any] | None = None
    # Transport for lazy and rehydrated file downloads (vercel/chat 6adca361,
    # upstream ``fileTransport``). Replaces the default DNS-pinned aiohttp
    # transport, which is the only place resolved addresses are checked
    # against the private-range blocklist, so the transport or egress proxy
    # must itself reject internal destinations and DNS rebinding. It must
    # return the raw response without following redirects. The downloader
    # still validates every hop URL, limits redirects, sends the bot token
    # only on hops to Slack origins, enforces the 30 s deadline and caps the
    # decoded body at 25 MB. A subclass ``_create_file_transport()`` wins.
    #
    # The default transport ignores ``HTTPS_PROXY`` / ``HTTP_PROXY`` (aiohttp
    # ``trust_env=False``: a proxy would resolve hosts itself and bypass the
    # pinned-address check). Before this, downloads used ``httpx.AsyncClient()``,
    # which honored those env vars, so a deployment whose only egress is an
    # env-configured proxy must now set ``file_transport`` (Python-only
    # change; upstream's Node transport never read env proxies).
    file_transport: AttachmentTransport | None = None
    # Factory for the ``httpx.AsyncClient`` used for ``response_url`` posts
    # (ephemeral replace/delete). Python counterpart of upstream ``fetch``
    # (vercel/chat 6adca361): set ``proxy=``/``verify=``/``transport=`` on
    # the client to route these requests through an egress proxy. Called
    # once per request and the adapter closes the client it returns, so
    # return a fresh client each time. Defaults to ``httpx.AsyncClient()``.
    # Does not affect Web API clients (``web_client_options``), Socket Mode
    # or file downloads (``file_transport``).
    http_client_factory: Callable[[], httpx.AsyncClient] | None = None
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
    # Stream replies through Slack's native streaming API (``chat.startStream``
    # / ``appendStream`` / ``stopStream``). With ``False``, ``stream()``
    # returns ``None`` and core delivers replies with post+edit. When the
    # workspace rejects the first native call, the reply falls back to
    # post+edit mid-stream; ``feature_not_enabled`` / ``method_deprecated`` /
    # ``unknown_method`` also turn native streaming off for the rest of this
    # adapter instance's life. Upstream ``nativeStreaming`` (default ``true``).
    native_streaming: bool = True
    # Override bot username (optional)
    user_name: str | None = None
    # Enable Slack's Agent messaging experience (``agent_view`` manifest mode).
    # With it on, ``app_home_opened`` fires for every tab (it is the DM-open
    # signal), each top-level DM message is its own thread root
    # (``slack:{D}:{ts}``; a subscribed ``slack:{D}:`` from ``open_dm`` still
    # receives top-level DMs), and configured ``suggested_prompts`` are
    # applied on a Messages-tab open without ``thread_ts``. Defaults to False.
    agent_view: bool = False
    # Suggested prompts pinned automatically when an assistant/agent thread
    # opens: on ``assistant_thread_started`` (legacy assistant_view) and on a
    # Messages-tab ``app_home_opened`` when ``agent_view`` is on. A static
    # :class:`SlackSuggestedPromptsOptions` or a sync/async resolver taking a
    # :class:`SlackSuggestedPromptsContext`. Failures are logged, never raised.
    suggested_prompts: SlackSuggestedPrompts | None = None
    # Default rotating loading messages for the assistant thinking indicator
    # (``assistant.threads.setStatus`` ``loading_messages``). Used by
    # ``start_typing`` and ``set_assistant_status`` when no explicit
    # status/messages are passed.
    loading_messages: list[str] | None = None
    # Append Slack's native feedback buttons (a ``context_actions`` block with
    # a ``feedback_buttons`` element) to every natively streamed reply when the
    # stream stops, after any caller ``stop_blocks``. Clicks dispatch to
    # ``chat.on_action``. ``True`` uses the defaults; pass
    # :class:`SlackFeedbackButtonsOptions` to customize labels, values and the
    # action id. See ``build_feedback_buttons_block`` for non-streamed messages.
    feedback_buttons: bool | SlackFeedbackButtonsOptions | None = None


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
    # Bot messages: ``{"user_id": "U…"}`` -- the bot's user id (vs app ``bot_id``)
    bot_profile: dict[str, Any]
    channel: str
    # Channel type: "channel", "group", "mpim", or "im" (DM)
    channel_type: str
    # Deleted message timestamp on message_deleted events
    deleted_ts: str
    edited: dict[str, str]  # {"ts": "..."}
    event_ts: str
    files: list[SlackFileInfo]
    # Hidden flag on message_changed events (true for unfurl-only updates)
    hidden: bool
    # Timestamp of the latest reply (present on thread parent messages)
    latest_reply: str
    # Inner message on message_changed events
    message: SlackEvent
    # Previous message snapshot on message_changed / message_deleted events
    previous_message: SlackEvent
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
    # Folded active-view context (agent_view only).
    context: SlackAppContext
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
    selected_date: str
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
    # The resolved installation this request runs under (``team_id``, or the
    # ``enterprise_id`` for org-wide installs). Scopes installation-owned
    # cache keys (user profiles, display-name index, channel names, unfurl
    # metadata) so one workspace's data is never served to another.
    installation_id: str | None = None
