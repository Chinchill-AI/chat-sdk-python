"""Type definitions for the Discord adapter.

Based on the Discord API v10.
See: https://discord.com/developers/docs/intro
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypedDict

from chat_sdk.logger import Logger

# =============================================================================
# Configuration
# =============================================================================


@dataclass
class DiscordAdapterConfig:
    """Discord adapter configuration.

    Requires a bot token for API calls, a public key for webhook
    signature verification, and an application ID.

    See: https://discord.com/developers/docs/getting-started
    """

    # Discord application ID. Defaults to DISCORD_APPLICATION_ID env var.
    application_id: str | None = None
    # Discord bot token. Defaults to DISCORD_BOT_TOKEN env var.
    bot_token: str | None = None
    # Logger instance for error reporting. Defaults to ConsoleLogger.
    logger: Logger | None = None
    # Role IDs that should trigger mention handlers.
    # Defaults to DISCORD_MENTION_ROLE_IDS env var (comma-separated).
    mention_role_ids: list[str] | None = None
    # Discord application public key for webhook signature verification.
    # Defaults to DISCORD_PUBLIC_KEY env var.
    public_key: str | None = None
    # Override bot username (optional).
    user_name: str | None = None


# =============================================================================
# Thread ID
# =============================================================================


@dataclass(frozen=True)
class DiscordThreadId:
    """Decoded thread ID for Discord.

    Format: discord:{guild_id}:{channel_id}[:{thread_id}]

    guild_id is "@me" for DMs.
    """

    # Channel ID
    channel_id: str
    # Guild ID, or "@me" for DMs
    guild_id: str
    # Thread ID (if message is in a thread)
    thread_id: str | None = None


# =============================================================================
# Slash Command Context
# =============================================================================


@dataclass
class DiscordSlashCommandContext:
    """Per-request slash command context used while resolving deferred responses."""

    channel_id: str
    initial_response_sent: bool
    interaction_token: str


@dataclass
class DiscordRequestContext:
    """Async request context for Discord webhook handling."""

    slash_command: DiscordSlashCommandContext | None = None


# =============================================================================
# Interaction Types
# =============================================================================


class InteractionResponseType:
    """Discord interaction response types."""

    # ACK and edit later (deferred)
    DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE = 5
    # ACK component interaction, update message later
    DEFERRED_UPDATE_MESSAGE = 6


# =============================================================================
# Webhook Payloads
# =============================================================================


class DiscordUser(TypedDict, total=False):
    """Discord user object."""

    avatar: str
    bot: bool
    discriminator: str
    global_name: str
    id: str
    username: str


class DiscordCommandOption(TypedDict, total=False):
    """Discord command option."""

    name: str
    options: list[DiscordCommandOption]
    type: int
    value: str | int | bool


class DiscordInteractionData(TypedDict, total=False):
    """Discord interaction data (for components/commands)."""

    component_type: int
    custom_id: str
    name: str
    options: list[DiscordCommandOption]
    type: int
    values: list[str]


class DiscordInteractionChannel(TypedDict, total=False):
    """Discord channel in an interaction."""

    id: str
    type: int
    name: str
    parent_id: str


class DiscordInteractionMember(TypedDict, total=False):
    """Discord member in an interaction."""

    user: DiscordUser
    nick: str
    roles: list[str]
    joined_at: str


class DiscordInteraction(TypedDict, total=False):
    """Incoming Discord interaction from webhook."""

    application_id: str
    channel: DiscordInteractionChannel
    channel_id: str
    data: DiscordInteractionData
    guild_id: str
    id: str
    member: DiscordInteractionMember
    message: dict[str, Any]
    token: str
    type: int
    user: DiscordUser
    version: int


class DiscordEmoji(TypedDict, total=False):
    """Discord emoji."""

    animated: bool
    id: str
    name: str


class DiscordButton(TypedDict, total=False):
    """Discord button component."""

    custom_id: str
    disabled: bool
    emoji: DiscordEmoji
    label: str
    style: int
    type: int  # Component type 2 for button
    url: str


class DiscordActionRow(TypedDict):
    """Discord action row component."""

    components: list[DiscordButton]
    type: int  # Component type 1 for action row


class DiscordMessagePayload(TypedDict, total=False):
    """Discord message create payload."""

    allowed_mentions: dict[str, Any]
    attachments: list[dict[str, Any]]
    components: list[DiscordActionRow]
    content: str
    embeds: list[dict[str, Any]]
    message_reference: dict[str, Any]


class DiscordInteractionResponse(TypedDict, total=False):
    """Discord interaction response."""

    data: DiscordMessagePayload
    type: int


# =============================================================================
# Gateway Forwarded Events
# =============================================================================


class DiscordGatewayMessageAuthor(TypedDict, total=False):
    """Message author from a Gateway event."""

    id: str
    username: str
    global_name: str
    bot: bool


class DiscordGatewayAttachment(TypedDict, total=False):
    """File attachment from a Gateway event."""

    id: str
    url: str
    filename: str
    content_type: str
    size: int


class DiscordGatewayThread(TypedDict, total=False):
    """Thread info from a Gateway event."""

    id: str
    parent_id: str


class DiscordGatewayMention(TypedDict):
    """User mention from a Gateway event."""

    id: str
    username: str


class DiscordGatewayMessageData(TypedDict, total=False):
    """Message data from a MESSAGE_CREATE Gateway event."""

    attachments: list[DiscordGatewayAttachment]
    author: DiscordGatewayMessageAuthor
    channel_id: str
    channel_type: int
    content: str
    guild_id: str | None
    id: str
    is_mention: bool
    mention_roles: list[str]
    mentions: list[DiscordGatewayMention]
    thread: DiscordGatewayThread
    timestamp: str


class DiscordGatewayReactionEmoji(TypedDict, total=False):
    """Emoji from a Gateway reaction event."""

    name: str | None
    id: str | None


class DiscordGatewayReactionUser(TypedDict, total=False):
    """User in a Gateway reaction event."""

    id: str
    username: str
    global_name: str
    bot: bool


class DiscordGatewayReactionMember(TypedDict, total=False):
    """Member details in a Gateway reaction event."""

    user: DiscordGatewayReactionUser


class DiscordGatewayReactionData(TypedDict, total=False):
    """Reaction data from REACTION_ADD or REACTION_REMOVE Gateway events."""

    channel_id: str
    channel_type: int
    emoji: DiscordGatewayReactionEmoji
    guild_id: str | None
    member: DiscordGatewayReactionMember
    message_id: str
    user: DiscordGatewayReactionUser
    user_id: str


class DiscordForwardedEvent(TypedDict):
    """A Gateway event forwarded to the webhook endpoint."""

    data: Any
    timestamp: int
    type: str
