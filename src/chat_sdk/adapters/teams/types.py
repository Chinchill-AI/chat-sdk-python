"""Type definitions for the Teams adapter.

Based on the Microsoft Teams Bot Framework / Teams SDK.
See: https://learn.microsoft.com/en-us/microsoftteams/platform/bots/
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeAlias, TypedDict

from chat_sdk.logger import Logger

# =============================================================================
# Configuration
# =============================================================================


@dataclass
class TeamsAuthCertificate:
    """Certificate-based authentication config.

    .. deprecated::
        Certificate auth is not yet supported by the Teams SDK. Setting
        ``certificate`` on :class:`TeamsAdapterConfig` raises at adapter
        startup. Ported for shape parity with upstream
        ``adapter-teams/src/types.ts`` so consumers can code against the
        config shape ahead of MS Teams SDK support.
    """

    # PEM-encoded certificate private key
    certificate_private_key: str
    # Hex-encoded certificate thumbprint (optional when x5c is provided)
    certificate_thumbprint: str | None = None
    # Public certificate for subject-name validation (optional)
    x5c: str | None = None


class TeamsAuthFederated(TypedDict, total=False):
    """Federated (workload identity) authentication config."""

    # Audience for the federated credential (defaults to api://AzureADTokenExchange)
    client_audience: str
    # Client ID for the managed identity assigned to the bot
    client_id: str


# Custom token factory: ``(scope, tenant_id) -> access token`` (sync or async).
# Same shape as the Teams SDK ``AppOptions.token`` it is forwarded to.
TeamsTokenFactory: TypeAlias = Callable[[str | list[str], str | None], str | Awaitable[str]]

# Lazy app-id resolver, awaited during ``initialize()`` when it returns an
# awaitable. Must produce a non-empty ``str``.
TeamsAppIdResolver: TypeAlias = Callable[[], str | Awaitable[str]]

# Verify a forwarded webhook instead of Microsoft's native JWT. Called with the
# framework request object and the exact raw body string; return a truthy value
# (or an awaitable resolving to one) to accept, a falsy value or raise to reject.
TeamsWebhookVerifier: TypeAlias = Callable[[Any, str], Any]


@dataclass
class TeamsAdapterConfig:
    """Teams adapter configuration.

    Supports Microsoft App Password, certificate, or federated authentication.

    See: https://learn.microsoft.com/en-us/microsoftteams/platform/bots/
    """

    # Override the Teams Bot Framework service URL (e.g. for GCC-High /
    # sovereign-cloud environments). Defaults to TEAMS_API_URL env var.
    api_url: str | None = None
    # Microsoft App ID, or a zero-argument resolver (sync or async) called once
    # during ``initialize()``. Defaults to TEAMS_APP_ID env var. With a
    # resolver, the Teams SDK ``App`` is built lazily in ``initialize()``.
    app_id: str | TeamsAppIdResolver | None = None
    # Microsoft App Password. Defaults to TEAMS_APP_PASSWORD env var.
    app_password: str | None = None
    # Microsoft App Tenant ID. Defaults to TEAMS_APP_TENANT_ID env var.
    app_tenant_id: str | None = None
    # Microsoft App Type.
    app_type: str | None = None  # "MultiTenant" | "SingleTenant"
    # Deprecated: certificate auth is not yet supported by the Teams SDK.
    # Passing a non-None value raises at adapter startup — kept for shape
    # parity with upstream adapter-teams/src/types.ts.
    certificate: TeamsAuthCertificate | None = None
    # Federated (workload identity) authentication.
    federated: TeamsAuthFederated | None = None
    # Logger instance for error reporting. Defaults to ConsoleLogger.
    logger: Logger | None = None
    # Override bot username (optional).
    user_name: str | None = None
    # Custom token factory for outbound Bot Framework / Graph calls, forwarded
    # to the Teams SDK ``AppOptions.token``. Called as ``token(scope,
    # tenant_id)``; may return the token or an awaitable. Takes precedence over
    # ``app_password``, ``federated`` credentials and client-secret environment
    # variables (``TEAMS_APP_PASSWORD`` and the SDK's ``CLIENT_SECRET``).
    token: TeamsTokenFactory | None = field(default=None, kw_only=True)
    # Custom verifier used instead of Microsoft JWT verification for inbound
    # webhooks. Called as ``webhook_verifier(request, raw_body)`` before the
    # body is parsed; a falsy result (or a raise) answers ``401``. When set,
    # the SDK's own JWT validation is disabled (the bridge verifies instead).
    webhook_verifier: TeamsWebhookVerifier | None = field(default=None, kw_only=True)


# =============================================================================
# Thread ID
# =============================================================================


@dataclass(frozen=True)
class TeamsThreadId:
    """Decoded thread ID for Teams.

    Format: teams:{base64url(conversation_id)}:{base64url(service_url)}
    """

    # Teams conversation ID
    conversation_id: str
    # Teams service URL
    service_url: str
    # Reply-to message ID (optional)
    reply_to_id: str | None = None


# =============================================================================
# Channel Context
# =============================================================================


class TeamsChannelContext(TypedDict, total=False):
    """Teams channel context extracted from activity.channelData.

    The ``type`` discriminator is optional for backwards-compatibility:
    cached entries written before vercel/chat#403 omit it, and downstream
    code treats a missing ``type`` as ``"channel"``.
    """

    channel_id: str
    team_id: str
    type: str  # Literal["channel"] when present


class TeamsDmContext(TypedDict):
    """Teams DM context with the resolved Microsoft Graph chat ID.

    Bot Framework hands out opaque DM conversation IDs (e.g.
    ``a:1xWhatever``) which are *not* accepted by Graph's
    ``/chats/{chat-id}/messages`` endpoint. The canonical Graph chat ID
    for a 1:1 DM is ``19:{userAadId}_{botId}@unq.gbl.spaces`` — derive
    and cache it from the incoming activity's ``from.aadObjectId``.
    """

    graph_chat_id: str
    type: str  # Literal["dm"]


# Discriminated union for Microsoft Graph API resolution context.
# Group chats are not represented — their conversation ID works as-is
# with Graph's chat endpoints.
TeamsGraphContext = TeamsChannelContext | TeamsDmContext


# =============================================================================
# Activity Types (simplified representations)
# =============================================================================


class TeamsActivity(TypedDict, total=False):
    """Simplified Teams activity (incoming webhook payload)."""

    attachments: list[dict[str, Any]]
    channel_data: dict[str, Any]
    channel_id: str
    conversation: dict[str, Any]
    entities: list[dict[str, Any]]
    from_: dict[str, Any]
    id: str
    name: str
    reactions_added: list[dict[str, Any]]
    reactions_removed: list[dict[str, Any]]
    recipient: dict[str, Any]
    reply_to_id: str
    service_url: str
    text: str
    text_format: str
    timestamp: str
    type: str
    value: Any
