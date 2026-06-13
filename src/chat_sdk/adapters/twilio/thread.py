"""Twilio thread ID encoding/decoding.

Format: ``twilio:{encodeURIComponent(sender)}:{encodeURIComponent(recipient)}``
(e.g. ``twilio:%2B15550000001:%2B15550000002``). Must match the TS adapter
byte-for-byte for cross-language state sharing. Mirrors upstream
``adapter-twilio/src/thread.ts``.
"""

from __future__ import annotations

from chat_sdk.adapters.twilio.types import TwilioThreadId
from chat_sdk.adapters.twilio.utils import decode_uri_component, encode_uri_component
from chat_sdk.shared.errors import ValidationError


def encode_twilio_thread_id(platform_data: TwilioThreadId) -> str:
    """Encode a Twilio (sender, recipient) pair into a thread ID."""
    sender = encode_uri_component(platform_data.sender)
    recipient = encode_uri_component(platform_data.recipient)
    return f"twilio:{sender}:{recipient}"


def decode_twilio_thread_id(thread_id: str) -> TwilioThreadId:
    """Decode a ``twilio:{sender}:{recipient}`` thread ID.

    Mirrors upstream's destructuring: segments beyond the third are
    ignored (encoded values never contain ``:``).
    """
    parts = thread_id.split(":")
    adapter = parts[0] if parts else ""
    sender = parts[1] if len(parts) > 1 else ""
    recipient = parts[2] if len(parts) > 2 else ""
    if adapter != "twilio" or not sender or not recipient:
        raise ValidationError("twilio", f"Invalid Twilio thread ID: {thread_id}")
    return TwilioThreadId(
        recipient=decode_uri_component(recipient),
        sender=decode_uri_component(sender),
    )


def twilio_channel_id(thread_id: str) -> str:
    """Channel ID for a thread: ``twilio:{encodeURIComponent(sender)}``."""
    thread = decode_twilio_thread_id(thread_id)
    return f"twilio:{encode_uri_component(thread.sender)}"
