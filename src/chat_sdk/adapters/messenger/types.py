"""Type definitions for the Messenger (Meta) adapter.

Based on the Messenger Platform (Meta Graph API).
See: https://developers.facebook.com/docs/messenger-platform
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal, TypedDict

from chat_sdk.logger import Logger

# Environment-variable fallbacks for credentials, matching upstream
# (FACEBOOK_APP_SECRET / FACEBOOK_PAGE_ACCESS_TOKEN / FACEBOOK_VERIFY_TOKEN).
ENV_APP_SECRET = "FACEBOOK_APP_SECRET"
ENV_PAGE_ACCESS_TOKEN = "FACEBOOK_PAGE_ACCESS_TOKEN"
ENV_VERIFY_TOKEN = "FACEBOOK_VERIFY_TOKEN"

# =============================================================================
# Configuration
# =============================================================================


@dataclass
class MessengerAdapterConfig:
    """Messenger adapter configuration.

    Requires a Page access token for Send API calls and an App Secret
    for webhook signature verification. Credentials fall back to the
    ``FACEBOOK_*`` environment variables when not supplied explicitly.

    See: https://developers.facebook.com/docs/messenger-platform/getting-started
    """

    # Logger instance for error reporting
    logger: Logger
    # Facebook App Secret for webhook HMAC-SHA256 signature verification.
    # Falls back to the FACEBOOK_APP_SECRET env var.
    app_secret: str | None = None
    # Facebook Page access token for Send API calls.
    # Falls back to the FACEBOOK_PAGE_ACCESS_TOKEN env var.
    page_access_token: str | None = None
    # Token used to verify the webhook subscription challenge-response.
    # Falls back to the FACEBOOK_VERIFY_TOKEN env var.
    verify_token: str | None = None
    # Override bot username (optional)
    user_name: str | None = None
    # Meta Graph API version (default chosen by the adapter in PR 2)
    api_version: str | None = None

    def resolved_app_secret(self) -> str | None:
        """App secret with the ``FACEBOOK_APP_SECRET`` env fallback."""
        # ``is not None`` (not truthy) mirrors upstream's ``??`` null-coalescing
        # (index.ts:931) — an explicit config value wins over the env var.
        return self.app_secret if self.app_secret is not None else os.environ.get(ENV_APP_SECRET)

    def resolved_page_access_token(self) -> str | None:
        """Page access token with the ``FACEBOOK_PAGE_ACCESS_TOKEN`` env fallback."""
        return self.page_access_token if self.page_access_token is not None else os.environ.get(ENV_PAGE_ACCESS_TOKEN)

    def resolved_verify_token(self) -> str | None:
        """Verify token with the ``FACEBOOK_VERIFY_TOKEN`` env fallback."""
        return self.verify_token if self.verify_token is not None else os.environ.get(ENV_VERIFY_TOKEN)


# =============================================================================
# Thread ID
# =============================================================================


@dataclass(frozen=True)
class MessengerThreadId:
    """Decoded thread ID for Messenger.

    Messenger conversations are 1:1 between a Page and a user (PSID).
    There is no concept of channels.
    """

    # Page-scoped ID (PSID) of the recipient user
    recipient_id: str


# =============================================================================
# Messaging Event Components
# =============================================================================


class MessengerSender(TypedDict):
    """The sender of a messaging event (the user's PSID)."""

    id: str


class MessengerRecipient(TypedDict):
    """The recipient of a messaging event (the Page ID)."""

    id: str


class MessengerAttachmentPayload(TypedDict, total=False):
    """Payload carried by an inbound attachment."""

    sticker_id: int
    url: str


class MessengerAttachment(TypedDict, total=False):
    """An attachment on an inbound message."""

    payload: MessengerAttachmentPayload
    type: Literal["image", "video", "audio", "file", "fallback", "location"]


class MessengerQuickReply(TypedDict):
    """Quick-reply payload echoed back when a user taps a quick reply."""

    payload: str


class MessengerMessagePayload(TypedDict, total=False):
    """An inbound (or echoed) message payload."""

    attachments: list[MessengerAttachment]
    is_echo: bool
    mid: str
    quick_reply: MessengerQuickReply
    text: str


class MessengerDelivery(TypedDict, total=False):
    """Delivery receipt for previously sent messages."""

    mids: list[str]
    watermark: int


class MessengerRead(TypedDict):
    """Read receipt watermark."""

    watermark: int


class MessengerPostback(TypedDict, total=False):
    """Postback event from a tapped button or persistent-menu item."""

    mid: str
    payload: str
    title: str


class MessengerReaction(TypedDict):
    """Reaction (react/unreact) on a message."""

    action: Literal["react", "unreact"]
    emoji: str
    mid: str
    reaction: str


class MessengerMessagingEvent(TypedDict, total=False):
    """A single messaging event inside a webhook entry.

    Exactly one of ``message`` / ``postback`` / ``reaction`` / ``delivery`` /
    ``read`` is typically present, alongside ``sender`` / ``recipient`` /
    ``timestamp``.
    """

    delivery: MessengerDelivery
    message: MessengerMessagePayload
    postback: MessengerPostback
    reaction: MessengerReaction
    read: MessengerRead
    recipient: MessengerRecipient
    sender: MessengerSender
    timestamp: int


# =============================================================================
# Webhook Payloads
# =============================================================================


class MessengerWebhookEntry(TypedDict):
    """A single entry in the webhook notification."""

    id: str
    messaging: list[MessengerMessagingEvent]
    time: int


class MessengerWebhookPayload(TypedDict):
    """Top-level webhook notification envelope from Meta.

    See: https://developers.facebook.com/docs/messenger-platform/webhooks
    """

    entry: list[MessengerWebhookEntry]
    object: str  # "page"


# =============================================================================
# API Response / Profile Types
# =============================================================================


class MessengerSendApiResponse(TypedDict):
    """Response from the Send API."""

    message_id: str
    recipient_id: str


class MessengerUserProfile(TypedDict, total=False):
    """User profile fetched from the Graph API."""

    first_name: str
    id: str
    last_name: str
    profile_pic: str


# =============================================================================
# Raw Message Type
# =============================================================================


# Platform-specific raw message type for Messenger. A messaging event is the
# unit the adapter dispatches on, so the alias mirrors upstream exactly.
MessengerRawMessage = MessengerMessagingEvent


# =============================================================================
# Buttons & Templates (Send API)
# =============================================================================


class MessengerButton(TypedDict, total=False):
    """A button inside a template payload.

    ``postback`` buttons carry a ``payload``; ``web_url`` buttons carry a
    ``url``.
    """

    payload: str
    title: str
    type: Literal["postback", "web_url"]
    url: str


class MessengerTemplateElement(TypedDict, total=False):
    """An element of a Generic Template."""

    buttons: list[MessengerButton]
    image_url: str
    subtitle: str
    title: str


class MessengerGenericTemplatePayload(TypedDict):
    """Generic Template payload (title/subtitle/image + buttons)."""

    elements: list[MessengerTemplateElement]
    template_type: Literal["generic"]


class MessengerButtonTemplatePayload(TypedDict):
    """Button Template payload (text + buttons, no image)."""

    buttons: list[MessengerButton]
    template_type: Literal["button"]
    text: str


MessengerTemplatePayload = MessengerGenericTemplatePayload | MessengerButtonTemplatePayload
