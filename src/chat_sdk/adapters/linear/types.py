"""Type definitions for the Linear adapter.

Based on the Linear API and webhook format.
See: https://linear.app/developers
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypedDict

from chat_sdk.logger import Logger

# =============================================================================
# Configuration
# =============================================================================


@dataclass
class LinearAdapterBaseConfig:
    """Base configuration options shared by all auth methods."""

    # Logger instance for error reporting. Defaults to ConsoleLogger.
    logger: Logger | None = None
    # Bot display name for @-mention detection.
    # Defaults to LINEAR_BOT_USERNAME env var or "linear-bot".
    user_name: str | None = None
    # Webhook signing secret for HMAC-SHA256 verification.
    # Defaults to LINEAR_WEBHOOK_SECRET env var.
    webhook_secret: str | None = None


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


class LinearCommentData(TypedDict, total=False):
    """Comment data from a webhook payload.

    See: https://linear.app/developers/webhooks#webhook-payload
    """

    # Comment body in markdown format
    body: str
    # ISO 8601 creation date
    created_at: str
    # Comment UUID
    id: str
    # Issue UUID the comment is associated with
    issue_id: str
    # Parent comment UUID (for nested/threaded replies)
    parent_id: str
    # ISO 8601 last update date
    updated_at: str
    # Direct URL to the comment
    url: str
    # User UUID who wrote the comment
    user_id: str


class CommentWebhookPayload(TypedDict, total=False):
    """Webhook payload for Comment events."""

    action: str  # "create" | "update" | "remove"
    actor: LinearWebhookActor
    created_at: str
    data: LinearCommentData
    organization_id: str
    type: str  # "Comment"
    updated_from: dict[str, Any]
    url: str
    webhook_id: str
    webhook_timestamp: int


class LinearReactionData(TypedDict, total=False):
    """Reaction data from a webhook payload."""

    comment_id: str
    emoji: str
    id: str
    user_id: str


class ReactionWebhookPayload(TypedDict, total=False):
    """Webhook payload for Reaction events."""

    action: str
    actor: LinearWebhookActor
    created_at: str
    data: LinearReactionData
    organization_id: str
    type: str  # "Reaction"
    url: str
    webhook_id: str
    webhook_timestamp: int


# Union type for all webhook payloads
LinearWebhookPayload = CommentWebhookPayload | ReactionWebhookPayload


# =============================================================================
# Raw Message Type
# =============================================================================


class LinearRawMessage(TypedDict, total=False):
    """Platform-specific raw message type for Linear."""

    # The raw comment data from webhook or API
    comment: LinearCommentData
    # Organization ID from the webhook
    organization_id: str
