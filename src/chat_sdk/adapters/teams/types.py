"""Type definitions for the Teams adapter.

Based on the Microsoft Teams Bot Framework / Teams SDK.
See: https://learn.microsoft.com/en-us/microsoftteams/platform/bots/
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypedDict

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


@dataclass
class TeamsAdapterConfig:
    """Teams adapter configuration.

    Supports Microsoft App Password, certificate, or federated authentication.

    See: https://learn.microsoft.com/en-us/microsoftteams/platform/bots/
    """

    # Microsoft App ID. Defaults to TEAMS_APP_ID env var.
    app_id: str | None = None
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
    # Minimum interval between native DM streaming activities, in
    # milliseconds. Bot Framework's streaming endpoint is throttled to
    # roughly 1 request/second; Microsoft recommends buffering tokens
    # for 1.5-2 seconds to avoid 429s mid-response. We default to 1500ms
    # per https://learn.microsoft.com/microsoftteams/platform/bots/streaming-ux.
    # Chunks that arrive within this window after the previous emit are
    # accumulated locally and shipped together on the next emit (or in
    # the final ``message`` activity if the stream ends inside the
    # window). A caller-supplied ``StreamOptions.update_interval_ms``
    # overrides this default for a single stream.
    native_stream_min_emit_interval_ms: int = 1500


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


class TeamsChannelContext(TypedDict):
    """Teams channel context extracted from activity.channelData."""

    channel_id: str
    team_id: str


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
