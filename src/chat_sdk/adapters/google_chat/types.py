"""Google Chat adapter types.

Python port of TypeScript interfaces from the Google Chat adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, TypedDict

# =============================================================================
# Google Chat Card v2 types (simplified)
# =============================================================================


class GoogleChatCardColor(TypedDict, total=False):
    """RGB color for buttons."""

    red: float
    green: float
    blue: float


class GoogleChatCardHeader(TypedDict, total=False):
    """Card header."""

    image_type: Literal["CIRCLE", "SQUARE"]
    image_url: str
    subtitle: str
    title: str


class GoogleChatButtonAction(TypedDict):
    """Button click action."""

    function: str
    parameters: list[dict[str, str]]


class GoogleChatButtonOnClick(TypedDict):
    """Button click handler with action."""

    action: GoogleChatButtonAction


class GoogleChatLinkOnClick(TypedDict):
    """Link button click handler."""

    open_link: dict[str, str]


class GoogleChatButton(TypedDict, total=False):
    """Interactive button widget."""

    color: GoogleChatCardColor
    disabled: bool
    on_click: GoogleChatButtonOnClick
    text: str


class GoogleChatLinkButton(TypedDict, total=False):
    """Link button widget."""

    color: GoogleChatCardColor
    on_click: GoogleChatLinkOnClick
    text: str


class GoogleChatWidget(TypedDict, total=False):
    """Card widget."""

    button_list: dict[str, list[dict[str, Any]]]
    decorated_text: dict[str, Any]
    divider: dict[str, Any]
    image: dict[str, Any]
    text_paragraph: dict[str, str]


class GoogleChatCardSection(TypedDict, total=False):
    """Card section."""

    collapsible: bool
    header: str
    widgets: list[dict[str, Any]]


class GoogleChatCardBody(TypedDict, total=False):
    """Card body (inner)."""

    header: dict[str, Any]
    sections: list[dict[str, Any]]


class GoogleChatCard(TypedDict, total=False):
    """Google Chat Card v2."""

    card: dict[str, Any]
    card_id: str


# =============================================================================
# Card Conversion Options
# =============================================================================


@dataclass
class CardConversionOptions:
    """Options for card conversion."""

    card_id: str | None = None
    endpoint_url: str | None = None


# =============================================================================
# Google Chat Message / Event types
# =============================================================================


class GoogleChatAnnotation(TypedDict, total=False):
    """Message annotation (mention, etc.)."""

    type: str
    start_index: int
    length: int
    user_mention: dict[str, Any]


class GoogleChatAttachment(TypedDict, total=False):
    """Message attachment."""

    name: str
    content_name: str
    content_type: str
    download_uri: str
    attachment_data_ref: dict[str, Any] | None


class GoogleChatSender(TypedDict, total=False):
    """Message sender."""

    name: str
    display_name: str
    type: str
    email: str


class GoogleChatThread(TypedDict, total=False):
    """Thread reference."""

    name: str


class GoogleChatSpaceRef(TypedDict, total=False):
    """Space reference within a message."""

    name: str
    type: str
    display_name: str


class GoogleChatMessage(TypedDict, total=False):
    """Google Chat message structure."""

    annotations: list[dict[str, Any]]
    argument_text: str
    attachment: list[dict[str, Any]]
    create_time: str
    formatted_text: str
    name: str
    sender: dict[str, Any]
    space: dict[str, Any]
    text: str
    thread: dict[str, Any]


class GoogleChatSpace(TypedDict, total=False):
    """Google Chat space structure."""

    display_name: str
    name: str
    single_user_bot_dm: bool
    space_threading_state: str
    space_type: str
    type: str


class GoogleChatUser(TypedDict, total=False):
    """Google Chat user structure."""

    display_name: str
    email: str
    name: str
    type: str


class GoogleChatMessagePayload(TypedDict, total=False):
    """Message payload within a Chat event."""

    space: dict[str, Any]
    message: dict[str, Any]


class GoogleChatAddedToSpacePayload(TypedDict, total=False):
    """Added to space payload."""

    space: dict[str, Any]


class GoogleChatRemovedFromSpacePayload(TypedDict, total=False):
    """Removed from space payload."""

    space: dict[str, Any]


class GoogleChatButtonClickedPayload(TypedDict, total=False):
    """Button clicked payload."""

    space: dict[str, Any]
    message: dict[str, Any]
    user: dict[str, Any]


class GoogleChatEventChat(TypedDict, total=False):
    """Chat section of Google Chat event."""

    user: dict[str, Any]
    event_time: str
    message_payload: dict[str, Any]
    added_to_space_payload: dict[str, Any]
    removed_from_space_payload: dict[str, Any]
    button_clicked_payload: dict[str, Any]


class GoogleChatCommonEventObject(TypedDict, total=False):
    """Common event object."""

    user_locale: str
    host_app: str
    platform: str
    invoked_function: str
    parameters: dict[str, str]


class GoogleChatEvent(TypedDict, total=False):
    """Google Workspace Add-ons event format."""

    chat: dict[str, Any]
    common_event_object: dict[str, Any]


# =============================================================================
# Service Account Credentials
# =============================================================================


@dataclass
class ServiceAccountCredentials:
    """Service account credentials for JWT auth."""

    client_email: str
    private_key: str
    project_id: str | None = None


# =============================================================================
# Cached subscription info
# =============================================================================


@dataclass
class SpaceSubscriptionInfo:
    """Cached subscription info."""

    subscription_name: str
    expire_time: int  # Unix timestamp ms


# =============================================================================
# Adapter Configuration
# =============================================================================


@dataclass
class GoogleChatAdapterConfig:
    """Configuration for Google Chat adapter.

    Supports multiple auth methods:
    - Service account credentials (JSON key)
    - Application Default Credentials (ADC)
    - Custom auth (e.g., OAuth2)
    - Auto-detect from environment variables
    """

    # Auth options (mutually exclusive)
    credentials: ServiceAccountCredentials | None = None
    use_application_default_credentials: bool = False

    # HTTP endpoint URL for button click actions
    endpoint_url: str | None = None

    # Google Cloud project number for verifying direct webhook JWTs
    google_chat_project_number: str | None = None

    # User email to impersonate for Workspace Events API calls
    impersonate_user: str | None = None

    # Logger instance
    logger: Any = None  # Logger protocol

    # Pub/Sub audience for JWT verification
    pubsub_audience: str | None = None

    # Pub/Sub topic for receiving all messages
    pubsub_topic: str | None = None

    # Override bot username
    user_name: str | None = None
