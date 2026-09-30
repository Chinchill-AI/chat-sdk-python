"""Type definitions for the WhatsApp adapter.

Based on the WhatsApp Business Cloud API (Meta Graph API).
See: https://developers.facebook.com/docs/whatsapp/cloud-api
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, NotRequired, TypedDict

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
    # Meta Graph API version (default: "v25.0")
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
    # User routing identifier, which may be a phone number or a
    # business-scoped user ID (BSUID)
    user_wa_id: str


# =============================================================================
# Webhook Payloads
# =============================================================================


class WhatsAppWebhookMetadata(TypedDict):
    """Metadata from the webhook value."""

    display_phone_number: str
    phone_number_id: str


class WhatsAppContactProfile(TypedDict):
    """Profile block of a contact."""

    name: str
    # WhatsApp username, present for users who have set one
    username: NotRequired[str]


class WhatsAppContact(TypedDict):
    """Contact information from an inbound message.

    Business-scoped user ID (BSUID) webhooks may omit ``wa_id`` (the phone
    number) and carry ``user_id`` / ``parent_user_id`` instead.
    """

    parent_user_id: NotRequired[str]
    profile: WhatsAppContactProfile
    user_id: NotRequired[str]
    wa_id: NotRequired[str]


class WhatsAppStatus(TypedDict, total=False):
    """Message delivery/read status update."""

    conversation: dict[str, Any]
    id: str
    pricing: dict[str, Any]
    recipient_id: str
    recipient_parent_user_id: str
    recipient_user_id: str
    status: str  # "sent" | "delivered" | "read" | "failed"
    timestamp: str


class WhatsAppUserIdChange(TypedDict, total=False):
    """Previous and current value of a rotated identifier."""

    current: str
    previous: str


class WhatsAppUserIdUpdate(TypedDict, total=False):
    """A business-scoped user ID rotation delivered under ``field: "user_id_update"``.

    Meta sends it when a phone number change regenerates a user's BSUID,
    carrying the previous and current values so existing records can be
    re-linked.

    See: https://developers.facebook.com/documentation/business-messaging/whatsapp/business-scoped-user-ids
    """

    # Human-readable description of the update
    detail: str
    # Previous and current parent BSUID, when parent BSUIDs are enabled
    parent_user_id: WhatsAppUserIdChange
    timestamp: str
    # Previous and current BSUID
    user_id: WhatsAppUserIdChange
    # User's phone number, omitted when sharing conditions aren't met
    wa_id: str


class WhatsAppWebhookValue(TypedDict, total=False):
    """The value payload containing messages, contacts, statuses and user ID updates."""

    contacts: list[WhatsAppContact]
    messages: list[dict[str, Any]]  # WhatsAppInboundMessage as dict
    messaging_product: str  # "whatsapp"
    metadata: WhatsAppWebhookMetadata
    statuses: list[WhatsAppStatus]
    user_id_update: list[WhatsAppUserIdUpdate]


class WhatsAppWebhookChange(TypedDict):
    """A change object containing the actual event data.

    Only ``messages`` and ``user_id_update`` changes are consumed by the
    adapter; other subscription fields are ignored.
    """

    field: str
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


class WhatsAppReferredProduct(TypedDict):
    """Product a customer is asking about (catalog product inquiries)."""

    catalog_id: str
    product_retailer_id: str


# Context accompanying quoted replies, forwarded messages, and catalog
# product inquiries. The shape depends on the message origin: replies (and
# interactions with a business message) carry `from` and `id`, forwarded
# messages carry only `forwarded` or `frequently_forwarded`, and catalog
# product inquiries add `referred_product`. No field is present in every
# variant. Functional form because `from` is a Python keyword.
#
# See: https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/reference/messages/text
WhatsAppInboundContext = TypedDict(
    "WhatsAppInboundContext",
    {
        # True when the message was forwarded five or fewer times; forwards only
        "forwarded": bool,
        # True when the message was forwarded more than five times; forwards only
        "frequently_forwarded": bool,
        # Sender of the quoted message on a reply, or the business display
        # phone number for a "Message business" button. Absent on forwards.
        "from": str,
        # ID of the quoted message on a reply, or of the message the user
        # tapped "Message business" from. Absent on forwards.
        "id": str,
        # Product the customer is asking about; catalog inquiries only
        "referred_product": WhatsAppReferredProduct,
    },
    total=False,
)


class WhatsAppSystemEvent(TypedDict):
    """System message payload (``type: "system"``) for identity changes."""

    body: str
    parent_user_id: NotRequired[str]
    type: str  # "user_changed_number" | "user_changed_user_id"
    user_id: str
    wa_id: NotRequired[str]


# Inbound message from a user. The `"from"` field name matches the raw JSON
# key (a Python keyword at class-body level, so we use the functional
# TypedDict form to preserve it verbatim).
#
# See: https://developers.facebook.com/docs/whatsapp/cloud-api/webhooks/payload-examples
WhatsAppInboundMessage = TypedDict(
    "WhatsAppInboundMessage",
    {
        # Audio message content
        "audio": dict[str, Any],
        # Legacy button response (from template quick replies)
        "button": dict[str, str],
        # Context for quoted replies, forwards and product inquiries
        "context": WhatsAppInboundContext,
        # Document message content
        "document": dict[str, Any],
        # Sender's WhatsApp ID (phone number). Absent on username-only /
        # BSUID-only webhooks.
        "from": str,
        # Sender's parent business-scoped user ID, when parent BSUIDs are enabled
        "from_parent_user_id": str,
        # Sender's business-scoped user ID (BSUID)
        "from_user_id": str,
        # Unique message ID
        "id": str,
        # Image message content
        "image": dict[str, Any],
        # Interactive message reply
        "interactive": dict[str, Any],
        # Location message content
        "location": dict[str, Any],
        # Reaction to a message
        "reaction": dict[str, str],
        # Sticker message content
        "sticker": dict[str, Any],
        # System message content (identity changes)
        "system": WhatsAppSystemEvent,
        # Text message content
        "text": dict[str, str],
        # Unix timestamp string
        "timestamp": str,
        # Message type: "text" | "image" | "document" | "audio" | "video" |
        # "voice" | "sticker" | "location" | "contacts" | "interactive" |
        # "button" | "reaction" | "order" | "system" | "template"
        "type": str,
        # Video message content
        "video": dict[str, Any],
        # Voice message content
        "voice": dict[str, Any],
    },
    total=False,
)


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


class WhatsAppSendResponseContact(TypedDict):
    """A contact entry in a send response."""

    input: str
    user_id: NotRequired[str]
    wa_id: NotRequired[str]


class WhatsAppSendResponse(TypedDict):
    """Response from sending a message via the Cloud API."""

    contacts: list[WhatsAppSendResponseContact]
    messages: list[dict[str, str]]
    messaging_product: str  # "whatsapp"


class WhatsAppTypingIndicatorResponse(TypedDict):
    """Response from sending a typing indicator via the Cloud API."""

    success: bool


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


class WhatsAppGraphErrorData(TypedDict, total=False):
    """Meta's ``error.error_data`` object."""

    details: str
    messaging_product: str


class WhatsAppGraphError(TypedDict, total=False):
    """Error object inside a failed Meta Graph API response.

    See: https://developers.facebook.com/documentation/business-messaging/whatsapp/support/error-codes/
    """

    code: int
    error_data: WhatsAppGraphErrorData
    # Optional and deprecated in the Cloud API.
    error_subcode: int
    fbtrace_id: str
    message: str
    type: str


class WhatsAppGraphErrorBody(TypedDict, total=False):
    """Body of a failed Meta Graph API response."""

    error: WhatsAppGraphError


# =============================================================================
# Template Messages
# =============================================================================


class WhatsAppTemplateTextParameter(TypedDict):
    """Text parameter (the only kind whose emoji placeholders are converted)."""

    type: Literal["text"]
    text: str


class WhatsAppTemplateCurrency(TypedDict):
    """Currency value for a ``currency`` template parameter."""

    amount_1000: int
    code: str
    fallback_value: str


class WhatsAppTemplateCurrencyParameter(TypedDict):
    """Currency template parameter."""

    type: Literal["currency"]
    currency: WhatsAppTemplateCurrency


class WhatsAppTemplateDateTimeParameter(TypedDict):
    """Date/time template parameter."""

    type: Literal["date_time"]
    date_time: dict[str, str]  # {"fallback_value": str}


class WhatsAppTemplateMedia(TypedDict, total=False):
    """Media reference for an image or video template parameter."""

    id: str
    link: str


class WhatsAppTemplateDocument(TypedDict, total=False):
    """Document reference for a document template parameter."""

    filename: str
    id: str
    link: str


class WhatsAppTemplateImageParameter(TypedDict):
    """Image template parameter."""

    type: Literal["image"]
    image: WhatsAppTemplateMedia


class WhatsAppTemplateDocumentParameter(TypedDict):
    """Document template parameter."""

    type: Literal["document"]
    document: WhatsAppTemplateDocument


class WhatsAppTemplateVideoParameter(TypedDict):
    """Video template parameter."""

    type: Literal["video"]
    video: WhatsAppTemplateMedia


# Parameter for a template header or body component.
# See: https://developers.facebook.com/docs/whatsapp/cloud-api/reference/messages#parameter-object
WhatsAppTemplateParameter = (
    WhatsAppTemplateTextParameter
    | WhatsAppTemplateCurrencyParameter
    | WhatsAppTemplateDateTimeParameter
    | WhatsAppTemplateImageParameter
    | WhatsAppTemplateDocumentParameter
    | WhatsAppTemplateVideoParameter
)


class WhatsAppTemplatePayloadParameter(TypedDict):
    """Quick reply button payload, echoed back in the button response."""

    type: Literal["payload"]
    payload: str


# Parameter for a template button component. URL buttons take a text
# parameter substituted into the button's URL; quick reply buttons take a
# payload echoed back in the button response.
WhatsAppTemplateButtonParameter = WhatsAppTemplateTextParameter | WhatsAppTemplatePayloadParameter


class WhatsAppTemplateHeaderComponent(TypedDict):
    """Header component of a template message."""

    type: Literal["header"]
    parameters: list[WhatsAppTemplateParameter]


class WhatsAppTemplateBodyComponent(TypedDict):
    """Body component of a template message."""

    type: Literal["body"]
    parameters: list[WhatsAppTemplateParameter]


class WhatsAppTemplateButtonComponent(TypedDict):
    """Button component of a template message."""

    type: Literal["button"]
    sub_type: Literal["url", "quick_reply"]
    index: int
    parameters: list[WhatsAppTemplateButtonParameter]


# A component of a template message carrying variable substitutions.
WhatsAppTemplateComponent = (
    WhatsAppTemplateHeaderComponent | WhatsAppTemplateBodyComponent | WhatsAppTemplateButtonComponent
)


class WhatsAppTemplateMessage(TypedDict):
    """A pre-approved template message.

    Templates are the only message type the Cloud API accepts outside the
    24-hour customer service window, so they are required for
    business-initiated conversations.

    See: https://developers.facebook.com/docs/whatsapp/cloud-api/guides/send-message-templates
    """

    # Name of the approved template
    name: str
    # Template language code (e.g. "en", "en_US")
    language: str
    # Variable substitutions for the template's components. Omit for
    # templates without variables.
    components: NotRequired[list[WhatsAppTemplateComponent]]


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
    # Canonical user ID the thread is keyed by (phone number or BSUID).
    # Upstream's camelCase `userId`, snake_cased like `phone_number_id`.
    user_id: str
