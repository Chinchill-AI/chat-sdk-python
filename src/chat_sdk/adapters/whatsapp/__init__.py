"""WhatsApp adapter for chat-sdk."""

from chat_sdk.adapters.whatsapp.adapter import WhatsAppAdapter, create_whatsapp_adapter, split_message
from chat_sdk.adapters.whatsapp.errors import WhatsAppApiError

__all__ = ["WhatsAppAdapter", "WhatsAppApiError", "create_whatsapp_adapter", "split_message"]
