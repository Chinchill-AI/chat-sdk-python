"""Twilio adapter for chat-sdk.

Python port of upstream ``packages/adapter-twilio``. Supports SMS and MMS
bots over Twilio Messaging webhooks and the Messages REST API, plus
low-level voice helpers for custom Twilio voice routes.
"""

from chat_sdk.adapters.twilio.cards import card_to_twilio_text
from chat_sdk.adapters.twilio.format_converter import (
    TWILIO_MESSAGE_LIMIT,
    TwilioFormatConverter,
    TwilioTextResult,
    truncate_twilio_text,
    twilio_text_or_placeholder,
)
from chat_sdk.adapters.twilio.types import (
    TwilioAdapterConfig,
    TwilioCallResource,
    TwilioCredential,
    TwilioCredentials,
    TwilioFormFields,
    TwilioFormParams,
    TwilioHttpRequest,
    TwilioHttpResponse,
    TwilioMediaPayload,
    TwilioMessageResource,
    TwilioRawMessage,
    TwilioStatusPayload,
    TwilioTextPayload,
    TwilioThreadId,
    TwilioUnsupportedPayload,
    TwilioVerifiedRequest,
    TwilioWebhookError,
    TwilioWebhookParseError,
    TwilioWebhookPayload,
    TwilioWebhookUrl,
    TwilioWebhookVerificationError,
    TwilioWebhookVerifier,
)

__all__ = [
    "TWILIO_MESSAGE_LIMIT",
    "TwilioAdapterConfig",
    "TwilioCallResource",
    "TwilioCredential",
    "TwilioCredentials",
    "TwilioFormFields",
    "TwilioFormParams",
    "TwilioFormatConverter",
    "TwilioHttpRequest",
    "TwilioHttpResponse",
    "TwilioMediaPayload",
    "TwilioMessageResource",
    "TwilioRawMessage",
    "TwilioStatusPayload",
    "TwilioTextPayload",
    "TwilioTextResult",
    "TwilioThreadId",
    "TwilioUnsupportedPayload",
    "TwilioVerifiedRequest",
    "TwilioWebhookError",
    "TwilioWebhookParseError",
    "TwilioWebhookPayload",
    "TwilioWebhookUrl",
    "TwilioWebhookVerificationError",
    "TwilioWebhookVerifier",
    "card_to_twilio_text",
    "truncate_twilio_text",
    "twilio_text_or_placeholder",
]
