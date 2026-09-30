"""Google Chat adapter types.

Python port of TypeScript interfaces from the Google Chat adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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

    # HTTP endpoint URL. Used for button-click action routing on cards AND as
    # an accepted JWT audience for direct-webhook verification when the Chat
    # app's "Authentication audience" setting is "HTTP endpoint URL" (always
    # the case for Workspace Add-on Chat apps). Must match the URL registered
    # in the Chat API console exactly. Counts as a direct-webhook verifier for
    # the constructor's fail-closed check.
    endpoint_url: str | None = None

    # Google Cloud project number for verifying direct webhook JWTs
    google_chat_project_number: str | None = None

    # User email to impersonate for Workspace Events API calls
    impersonate_user: str | None = None

    # Logger instance
    logger: Any = None  # Logger protocol

    # Pub/Sub audience for JWT verification. Pub/Sub pushes additionally
    # require ``pubsub_service_account_email`` (the audience is public, so it
    # does not identify the caller).
    pubsub_audience: str | None = None

    # Pub/Sub topic for receiving all messages
    pubsub_topic: str | None = None

    # Override bot username
    user_name: str | None = None

    # Explicit opt-in to disable webhook signature verification. Required to
    # construct the adapter when none of google_chat_project_number,
    # endpoint_url or pubsub_audience is configured. Without this flag the constructor raises
    # ValidationError -- fail-closed by default. Only enable in development or
    # when an upstream layer (e.g. authenticated Cloud Run invocations) provides
    # equivalent guarantees. Falls back to the
    # GOOGLE_CHAT_DISABLE_SIGNATURE_VERIFICATION env var when left unset (None).
    #
    # The opt-out only covers a transport with no verifier configured: a set
    # google_chat_project_number or endpoint_url still verifies direct
    # webhooks, and a set pubsub_audience still verifies Pub/Sub pushes. So
    # setting endpoint_url (even only for button routing) means direct
    # webhooks are verified despite this flag.
    #
    # Kept at the END of the field list intentionally: GoogleChatAdapterConfig
    # is a positional-args dataclass, so inserting a new field in the middle
    # would silently shift every later positional arg for existing callers.
    disable_signature_verification: bool | None = None

    # ---- Keyword-only fields -------------------------------------------------
    # Fields below are ``kw_only`` so they never shift the positional order
    # above (existing positional callers keep working). Add new fields here.
    # Divergence from upstream -- see docs/UPSTREAM_SYNC.md

    # Exact service-account identity of this app's Workspace Add-on, in the
    # form ``service-{projectNumber}@gcp-sa-gsuiteaddons.iam.gserviceaccount.com``
    # (your OWN project number). Workspace Add-on Chat apps sign endpoint-URL
    # webhooks with this identity instead of ``chat@system.gserviceaccount.com``.
    # Every add-on project shares that email shape, so the identity is only
    # meaningful compared exactly: when unset, add-on tokens are rejected
    # (401) rather than trusted by shape. Ordinary Chat apps are unaffected.
    # Falls back to the GOOGLE_CHAT_WORKSPACE_ADDON_SERVICE_ACCOUNT_EMAIL env
    # var when left unset (None).
    workspace_add_on_service_account_email: str | None = field(default=None, kw_only=True)

    # Service account the Pub/Sub push subscription authenticates as (the
    # identity in the subscription's push auth settings). Required to accept
    # Pub/Sub pushes: the token's ``email`` claim must equal it exactly and
    # ``email_verified`` must be true, otherwise the push is rejected (401).
    # Falls back to the GOOGLE_CHAT_PUBSUB_SERVICE_ACCOUNT_EMAIL env var when
    # left unset (None).
    pubsub_service_account_email: str | None = field(default=None, kw_only=True)
