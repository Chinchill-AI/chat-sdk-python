"""Messenger (Meta) adapter for chat-sdk.

PR 1 of 2 (scaffolding): types, format converter, and card conversion.
The adapter itself (webhook routing, Graph API, signature verification,
send/stream) is added in PR 2.
"""

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
    "decode_messenger_callback_data",
    "encode_messenger_callback_data",
]
