"""Messenger (Meta) adapter for chat-sdk.

Python port of upstream ``packages/adapter-messenger``. Supports webhook
routing (with HMAC-SHA256 signature verification), Send API integration via
the Meta Graph API, and message/card/streaming primitives.
"""

from chat_sdk.adapters.messenger.adapter import (
    DEFAULT_API_VERSION,
    GRAPH_API_BASE,
    MESSENGER_MESSAGE_LIMIT,
    MessengerAdapter,
    create_messenger_adapter,
)
from chat_sdk.adapters.messenger.cards import (
    MessengerCardResult,
    card_to_messenger,
    card_to_messenger_text,
    decode_messenger_callback_data,
    encode_messenger_callback_data,
)
from chat_sdk.adapters.messenger.format_converter import MessengerFormatConverter
from chat_sdk.adapters.messenger.types import (
    MessengerAdapterConfig,
    MessengerButton,
    MessengerButtonTemplatePayload,
    MessengerGenericTemplatePayload,
    MessengerMessagingEvent,
    MessengerRawMessage,
    MessengerSendApiResponse,
    MessengerTemplateElement,
    MessengerTemplatePayload,
    MessengerThreadId,
    MessengerUserProfile,
    MessengerWebhookEntry,
    MessengerWebhookPayload,
)

__all__ = [
    "DEFAULT_API_VERSION",
    "GRAPH_API_BASE",
    "MESSENGER_MESSAGE_LIMIT",
    "MessengerAdapter",
    "MessengerAdapterConfig",
    "MessengerButton",
    "MessengerButtonTemplatePayload",
    "MessengerCardResult",
    "MessengerFormatConverter",
    "MessengerGenericTemplatePayload",
    "MessengerMessagingEvent",
    "MessengerRawMessage",
    "MessengerSendApiResponse",
    "MessengerTemplateElement",
    "MessengerTemplatePayload",
    "MessengerThreadId",
    "MessengerUserProfile",
    "MessengerWebhookEntry",
    "MessengerWebhookPayload",
    "card_to_messenger",
    "card_to_messenger_text",
    "create_messenger_adapter",
    "decode_messenger_callback_data",
    "encode_messenger_callback_data",
]
