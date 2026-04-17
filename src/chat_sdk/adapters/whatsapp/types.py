"""Type definitions for the WhatsApp adapter.

Based on the WhatsApp Business Cloud API (Meta Graph API).
See: https://developers.facebook.com/docs/whatsapp/cloud-api
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypedDict

from chat_sdk.logger import Logger

# =============================================================================
# Configuration
# =============================================================================


@dataclass
class WhatsAppAdapterConfig:
    """WhatsApp adapter configuration.

    Requires a System User access token for API calls and an App Secret
    for webhook signature verification.

    See: https://developers.facebook.com/docs/whatsapp/cloud-api/get-started
    """

    # Access token (System User token) for WhatsApp Cloud API calls
    access_token: str
    # Meta App Secret for webhook HMAC-SHA256 signature verification
    app_secret: str
    # Logger instance for error reporting
    logger: Logger
    # WhatsApp Business phone number ID (not the phone number itself)
    phone_number_id: str
    # Bot display name used for identification
    user_name: str
    # Verify token for webhook challenge-response verification
    verify_token: str
    # Meta Graph API version (default: "v21.0")
    api_version: str | None = None


# =============================================================================
# Thread ID
# =============================================================================


@dataclass(frozen=True)
class WhatsAppThreadId:
    """Decoded thread ID for WhatsApp.

    WhatsApp conversations are always 1:1 between a business phone number
    and a user. There is no concept of threads or channels.

    Format: whatsapp:{phone_number_id}:{user_wa_id}
    """

    # Business phone number ID
    phone_number_id: str
    # User's WhatsApp ID (their phone number)
    user_wa_id: str


# =============================================================================
# Webhook Payloads
# =============================================================================


class WhatsAppWebhookMetadata(TypedDict):
    """Metadata from the webhook value."""

    display_phone_number: str
    phone_number_id: str


class WhatsAppContact(TypedDict):
    """Contact information from an inbound message."""

    profile: dict[str, str]  # {"name": str}
    wa_id: str


class WhatsAppStatus(TypedDict, total=False):
    """Message delivery/read status update."""

    conversation: dict[str, Any]
    id: str
    pricing: dict[str, Any]
    recipient_id: str
    status: str  # "sent" | "delivered" | "read" | "failed"
    timestamp: str


class WhatsAppWebhookValue(TypedDict, total=False):
    """The value payload containing messages, contacts, and statuses."""

    contacts: list[WhatsAppContact]
    messages: list[dict[str, Any]]  # WhatsAppInboundMessage as dict
    messaging_product: str  # "whatsapp"
    metadata: WhatsAppWebhookMetadata
    statuses: list[WhatsAppStatus]


class WhatsAppWebhookChange(TypedDict):
    """A change object containing the actual event data."""

    field: str  # "messages"
    value: WhatsAppWebhookValue


class WhatsAppWebhookEntry(TypedDict):
    """A single entry in the webhook notification."""

    changes: list[WhatsAppWebhookChange]
    id: str


class WhatsAppWebhookPayload(TypedDict):
    """Top-level webhook notification envelope from Meta.

    See: https://developers.facebook.com/docs/whatsapp/cloud-api/webhooks/components
    """

    entry: list[WhatsAppWebhookEntry]
    object: str  # "whatsapp_business_account"


# =============================================================================
# Inbound Message
# =============================================================================


# Using functional TypedDict syntax because "from" is a reserved Python keyword.
# See: https://peps.python.org/pep-0589/#alternative-syntax
WhatsAppInboundMessage = TypedDict(
    "WhatsAppInboundMessage",
    {
        "audio": dict[str, Any],
        "button": dict[str, str],
        "context": dict[str, str],
        "document": dict[str, Any],
        # Sender's WhatsApp ID (JSON key "from")
        "from": str,
        "id": str,
        "image": dict[str, Any],
        "interactive": dict[str, Any],
        "location": dict[str, Any],
        "reaction": dict[str, str],
        "sticker": dict[str, Any],
        "text": dict[str, str],
        "timestamp": str,
        "type": str,
        "video": dict[str, Any],
        "voice": dict[str, Any],
    },
    total=False,
)
"""Inbound message from a user.

See: https://developers.facebook.com/docs/whatsapp/cloud-api/webhooks/payload-examples
"""


# =============================================================================
# Media Response
# =============================================================================


class WhatsAppMediaResponse(TypedDict):
    """Response from the media URL endpoint.

    See: https://developers.facebook.com/docs/whatsapp/cloud-api/reference/media#get-media-url
    """

    file_size: int
    id: str
    messaging_product: str  # "whatsapp"
    mime_type: str
    sha256: str
    url: str


# =============================================================================
# API Response Types
# =============================================================================


class WhatsAppSendResponse(TypedDict):
    """Response from sending a message via the Cloud API."""

    contacts: list[dict[str, str]]
    messages: list[dict[str, str]]
    messaging_product: str  # "whatsapp"


class WhatsAppInteractiveButtonReply(TypedDict):
    """A single reply button for interactive messages."""

    reply: dict[str, str]  # {"id": str, "title": str}
    type: str  # "reply"


class WhatsAppInteractiveSectionRow(TypedDict, total=False):
    """A row in an interactive list section."""

    description: str
    id: str
    title: str


class WhatsAppInteractiveSection(TypedDict):
    """A section in an interactive list."""

    rows: list[WhatsAppInteractiveSectionRow]
    title: str


class WhatsAppInteractiveMessage(TypedDict, total=False):
    """Interactive message payload for sending buttons or lists.

    The action field can be either:
    - buttons: list of reply buttons (max 3)
    - sections: list of sections with rows + button label
    """

    action: dict[str, Any]
    body: dict[str, str]  # {"text": str}
    footer: dict[str, str]  # {"text": str}
    header: dict[str, str]  # {"text": str, "type": "text"}
    type: str  # "button" | "list"


# =============================================================================
# Raw Message Type
# =============================================================================


class WhatsAppRawMessage(TypedDict, total=False):
    """Platform-specific raw message type for WhatsApp.

    Used as a dict literal throughout the adapter code, so this is a
    TypedDict rather than a dataclass.
    """

    # The raw inbound message data
    message: WhatsAppInboundMessage
    # Phone number ID that received the message
    phone_number_id: str
    # Contact info from the webhook
    contact: WhatsAppContact | None
