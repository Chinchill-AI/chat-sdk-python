"""Type definitions for the Linear adapter.

Based on the Linear API and webhook format.
See: https://linear.app/developers

Agent-session types (``mode``, the ``kind`` discriminator, the agent-session
raw-message variant, and ``AgentSessionEventWebhookPayload``) are a faithful
port of upstream ``packages/adapter-linear/src/types.ts`` (vercel/chat,
adapter-linear 4.27.0 / chat@4.31.0). Upstream re-uses ``@linear/sdk`` and
``@linear/sdk/webhooks`` types; our adapter is raw GraphQL, so the
agent-session webhook payload is hand-authored to mirror the SDK shape that
upstream ``index.ts`` consumes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from chat_sdk.logger import Logger

# =============================================================================
# Configuration
# =============================================================================

# Incoming webhook handling mode for the Linear adapter. Mirrors upstream
# ``LinearAdapterMode`` (types.ts:44). ``"comments"`` is the data-change
# webhook model (the existing behavior); ``"agent-sessions"`` is the app-actor
# model used for ``AgentSessionEvent`` webhooks.
LinearAdapterMode = Literal["agent-sessions", "comments"]


@dataclass
class LinearAdapterBaseConfig:
    """Base configuration options shared by all auth methods."""

    # Override the Linear GraphQL API base URL. Defaults to the LINEAR_API_URL
    # env var, then to "https://api.linear.app/graphql". Mirrors upstream
    # ``config.apiUrl ?? process.env.LINEAR_API_URL`` → ``LinearClient.apiUrl``
    # (vercel/chat adapter-linear index.ts:239, types.ts:51). Useful for
    # proxies, mocks, or self-hosted GraphQL gateways.
    api_url: str | None = None
    # Logger instance for error reporting. Defaults to ConsoleLogger.
    logger: Logger | None = None
    # Controls which inbound Linear webhook model should trigger message
    # handling. Defaults to "comments". Use "agent-sessions" for app-actor
    # installs. Faithful port of upstream ``config.mode ?? "comments"``
    # (types.ts:67, index.ts:236).
    mode: LinearAdapterMode | None = None
    # Bot display name for @-mention detection.
    # Defaults to LINEAR_BOT_USERNAME env var or "linear-bot".
    user_name: str | None = None
    # Webhook signing secret for HMAC-SHA256 verification.
    # Defaults to LINEAR_WEBHOOK_SECRET env var.
    webhook_secret: str | None = None
    # Optional 32-byte AES-256-GCM key used to encrypt OAuth ``access_token``
    # and ``refresh_token`` values at rest in the state store. Accepts either a
    # 64-char hex string or a 44-char base64 string. Defaults to the
    # ``LINEAR_ENCRYPTION_KEY`` env var. Strongly recommended for multi-tenant
    # deployments -- without it, a state-store compromise yields plaintext
    # per-tenant Linear API tokens. When unset, installations are stored as
    # plaintext (legacy behavior).
    encryption_key: str | None = None
    # Prefix for the state key used to store per-organization installations.
    # Defaults to ``linear:installation``. The full key is ``{prefix}:{org_id}``.
    installation_key_prefix: str = "linear:installation"


@dataclass
class LinearAdapterAPIKeyConfig(LinearAdapterBaseConfig):
    """Configuration using a personal API key.

    See: https://linear.app/docs/api-and-webhooks
    """

    # Personal API key. Defaults to LINEAR_API_KEY env var.
    api_key: str | None = None


@dataclass
class LinearAdapterOAuthConfig(LinearAdapterBaseConfig):
    """Configuration using an OAuth access token (pre-obtained).

    See: https://linear.app/developers/oauth-2-0-authentication
    """

    # OAuth access token. Defaults to LINEAR_ACCESS_TOKEN env var.
    access_token: str | None = None


@dataclass
class LinearAdapterAppConfig(LinearAdapterBaseConfig):
    """Configuration using OAuth client credentials (recommended for apps).

    See: https://linear.app/developers/oauth-2-0-authentication#client-credentials-tokens
    """

    # OAuth application client ID. Defaults to LINEAR_CLIENT_ID env var.
    client_id: str | None = None
    # OAuth application client secret. Defaults to LINEAR_CLIENT_SECRET env var.
    client_secret: str | None = None


# Union type for all config options
LinearAdapterConfig = (
    LinearAdapterBaseConfig | LinearAdapterAPIKeyConfig | LinearAdapterOAuthConfig | LinearAdapterAppConfig
)


# =============================================================================
# Installation
# =============================================================================


@dataclass
class LinearInstallation:
    """Per-organization OAuth installation persisted to the state store.

    Used in multi-tenant mode after a successful OAuth exchange. When an
    ``encryption_key`` is configured on the adapter, ``access_token`` and
    ``refresh_token`` are AES-256-GCM-encrypted at rest; the in-memory form
    always holds plaintext.
    """

    access_token: str
    organization_id: str
    bot_user_id: str | None = None
    expires_at: int | None = None
    refresh_token: str | None = None


# =============================================================================
# Thread ID
# =============================================================================


@dataclass(frozen=True)
class LinearThreadId:
    """Decoded thread ID for Linear.

    Thread types:
    - Issue-level: Top-level comments on the issue (no comment_id)
    - Comment thread: Replies nested under a specific root comment (has comment_id)

    Format: linear:{issue_id}[:c:{comment_id}]
    """

    # Linear issue UUID
    issue_id: str
    # Root comment ID for comment-level threads (optional)
    comment_id: str | None = None
    # Agent session UUID for app-actor interactions (optional). Faithful port of
    # upstream ``LinearThreadId.agentSessionId`` (types.ts:189).
    agent_session_id: str | None = None


@dataclass(frozen=True)
class LinearAgentSessionThreadId(LinearThreadId):
    """Decoded thread ID for Linear threads associated with agent sessions.

    Faithful port of upstream ``LinearAgentSessionThreadId``
    (``LinearThreadId & { agentSessionId: string }``, types.ts:203). Narrows
    ``agent_session_id`` to a required, non-optional ``str`` so that downstream
    agent-session code (L4) can rely on the field being present.
    """

    # Required for agent-session threads (narrows the optional base field).
    agent_session_id: str = ""


# =============================================================================
# Webhook Payloads
# =============================================================================


class LinearWebhookActor(TypedDict, total=False):
    """Actor who triggered the webhook event.

    See: https://linear.app/developers/webhooks#data-change-events-payload
    """

    email: str
    id: str
    name: str
    type: str  # "user" | "application" | "integration"
    url: str


class LinearActorData(TypedDict, total=False):
    """Normalized author of a comment stored on a ``LinearCommentData``.

    Faithful port of upstream ``LinearActorData`` (types.ts:214). Distinct from
    the raw webhook ``LinearWebhookActor``: this is the *normalized* shape that
    ``_parse_message_from_agent_session_event`` writes onto the raw message's
    ``comment.user`` so the parsed ``Message.author`` reflects the real
    poster (display name, full name, bot/user discriminator). ``email`` /
    ``avatarUrl`` are optional; ``type`` is ``"user" | "bot"`` (NOT the webhook
    actor's ``"application" | "integration"``).
    """

    avatarUrl: str
    displayName: str
    email: str
    fullName: str
    id: str
    type: str  # "user" | "bot"


class LinearCommentData(TypedDict, total=False):
    """Comment data from a webhook payload.

    Field names use camelCase to match the Linear API JSON format.
    See: https://linear.app/developers/webhooks#webhook-payload
    """

    # Comment body in markdown format
    body: str
    # ISO 8601 creation date
    createdAt: str
    # Comment UUID
    id: str
    # Issue UUID the comment is associated with
    issueId: str
    # Parent comment UUID (for nested/threaded replies)
    parentId: str
    # ISO 8601 last update date
    updatedAt: str
    # Direct URL to the comment
    url: str
    # User UUID who wrote the comment (raw webhook / flat-comment shape).
    userId: str
    # Normalized author. Upstream's ``LinearCommentData.user`` is a required
    # ``LinearActorData`` (types.ts:247); kept optional here (``total=False``)
    # so the existing flat-comment webhook path — which only carries ``userId``
    # — is unaffected. ``_parse_message_from_agent_session_event`` populates it.
    user: LinearActorData


class CommentWebhookPayload(TypedDict, total=False):
    """Webhook payload for Comment events.

    Field names use camelCase to match the Linear API JSON format.
    """

    action: str  # "create" | "update" | "remove"
    actor: LinearWebhookActor
    createdAt: str
    data: LinearCommentData
    organizationId: str
    type: str  # "Comment"
    updatedFrom: dict[str, Any]
    url: str
    webhookId: str
    webhookTimestamp: int


class LinearReactionData(TypedDict, total=False):
    """Reaction data from a webhook payload.

    Field names use camelCase to match the Linear API JSON format.
    """

    commentId: str
    emoji: str
    id: str
    userId: str


class ReactionWebhookPayload(TypedDict, total=False):
    """Webhook payload for Reaction events.

    Field names use camelCase to match the Linear API JSON format.
    """

    action: str
    actor: LinearWebhookActor
    createdAt: str
    data: LinearReactionData
    organizationId: str
    type: str  # "Reaction"
    url: str
    webhookId: str
    webhookTimestamp: int


# -----------------------------------------------------------------------------
# Agent-session webhook payload (mode="agent-sessions")
# -----------------------------------------------------------------------------
#
# Hand-authored to mirror upstream ``@linear/sdk/webhooks``
# ``AgentSessionEventWebhookPayload`` as consumed by upstream
# ``adapter-linear/src/index.ts``. Webhook payloads are external JSON, so the
# RAW Linear wire keys (camelCase) are kept verbatim — no snake_case aliasing.
# These are NetworkError-prone external inputs; ``total=False`` mirrors the
# optional-heavy SDK shape and lets the parse logic (L3) read fields defensively.


class AgentSessionCommentChild(TypedDict, total=False):
    """Root comment of the thread an agent session is attached to.

    Mirrors ``CommentChildWebhookPayload`` as consumed for
    ``agentSession.comment`` (index.ts:1026-1035).
    """

    id: str
    body: str
    userId: str


class AgentSessionIssueChild(TypedDict, total=False):
    """Issue an agent session is associated with.

    Mirrors ``IssueWithDescriptionChildWebhookPayload`` as consumed for
    ``agentSession.issue`` (index.ts:959).
    """

    id: str


class AgentSessionUserChild(TypedDict, total=False):
    """Human user responsible for the agent session.

    Mirrors ``UserChildWebhookPayload`` as consumed for
    ``agentSession.creator`` (index.ts:1037-1044).
    """

    id: str
    name: str
    email: str
    avatarUrl: str
    url: str


class AgentSessionWebhookPayload(TypedDict, total=False):
    """The agent session an ``AgentSessionEvent`` belongs to.

    Mirrors ``AgentSessionWebhookPayload`` (@linear/sdk/webhooks) as consumed by
    upstream ``index.ts``.
    """

    id: str
    appUserId: str
    issueId: str
    issue: AgentSessionIssueChild
    commentId: str
    sourceCommentId: str
    comment: AgentSessionCommentChild
    creator: AgentSessionUserChild
    url: str
    status: str
    summary: str
    sourceMetadata: dict[str, Any]


class AgentActivityWebhookContent(TypedDict, total=False):
    """Content of an agent activity (e.g. the prompt body).

    Mirrors the ``content`` discriminated object consumed at index.ts:984.
    """

    type: str  # e.g. "prompt"
    body: str


class AgentActivityWebhookPayload(TypedDict, total=False):
    """The agent activity that triggered a ``prompted`` event.

    Mirrors ``AgentActivityWebhookPayload`` (@linear/sdk/webhooks) as consumed
    by upstream ``index.ts`` (e.g. index.ts:968-998).
    """

    id: str
    sourceCommentId: str
    content: AgentActivityWebhookContent
    user: AgentSessionUserChild
    createdAt: str


class GuidanceRuleWebhookPayload(TypedDict, total=False):
    """A single guidance rule for the agent's behavior."""

    body: str


class CommentChildWebhookPayload(TypedDict, total=False):
    """A comment in the thread before the agent session was initiated."""

    id: str
    body: str


class AgentSessionEventWebhookPayload(TypedDict, total=False):
    """Webhook payload for ``AgentSessionEvent`` events (mode="agent-sessions").

    Faithful port of upstream ``AgentSessionEventWebhookPayload``
    (@linear/sdk/webhooks), hand-authored to match the shape consumed by
    upstream ``adapter-linear/src/index.ts``. Raw Linear wire keys (camelCase).
    """

    type: str  # "AgentSessionEvent"
    action: str  # "created" | "prompted"
    createdAt: str
    appUserId: str
    oauthClientId: str
    organizationId: str
    webhookId: str
    webhookTimestamp: int
    # Formatted prompt with relevant context; present only for "created" events.
    promptContext: str
    agentSession: AgentSessionWebhookPayload
    agentActivity: AgentActivityWebhookPayload
    guidance: list[GuidanceRuleWebhookPayload]
    previousComments: list[CommentChildWebhookPayload]


# Union type for all webhook payloads
LinearWebhookPayload = CommentWebhookPayload | ReactionWebhookPayload | AgentSessionEventWebhookPayload


# =============================================================================
# Raw Message Type
# =============================================================================
#
# Discriminated union on the ``kind`` field. Faithful port of upstream
# ``LinearRawMessage`` (types.ts:279). Every variant MUST carry ``kind`` so the
# union discriminates cleanly; constructing a raw message without ``kind``
# breaks the union (emit/parse symmetry). The existing (comment-based)
# producers in ``adapter.py`` all set ``kind="comment"``.


class LinearCommentRawMessage(TypedDict, total=False):
    """Platform-specific raw message for a standard Linear comment.

    Faithful port of upstream ``LinearCommentRawMessage`` (types.ts:258).
    ``kind`` and ``comment`` are required; ``organizationId`` is part of the
    upstream base but our raw-GraphQL producers do not always have it on hand,
    so it stays optional (``total=False``) for back-compat with the existing
    comment path.
    """

    # Raw message kind discriminator. Always "comment" for this variant.
    kind: Literal["comment"]
    # The raw comment data from webhook or API.
    comment: LinearCommentData
    # Organization ID from the webhook or request context.
    organizationId: str


class LinearAgentSessionCommentRawMessage(TypedDict, total=False):
    """Platform-specific raw message for a comment backed by an agent session.

    Faithful port of upstream ``LinearAgentSessionCommentRawMessage``
    (types.ts:265). Carries the agent session the comment belongs to plus an
    optional prompt context.
    """

    # Raw message kind discriminator. Always "agent_session_comment".
    kind: Literal["agent_session_comment"]
    # The visible Linear comment backing this message.
    comment: LinearCommentData
    # The agent session the comment belongs to.
    agentSessionId: str
    # The prompt context associated with this agent session comment (optional).
    agentSessionPromptContext: str
    # Organization ID from the webhook or request context.
    organizationId: str


# Platform-specific raw message type for Linear (discriminated union on `kind`).
# Faithful port of upstream ``LinearRawMessage`` (types.ts:279).
LinearRawMessage = LinearCommentRawMessage | LinearAgentSessionCommentRawMessage
