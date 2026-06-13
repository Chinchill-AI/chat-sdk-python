"""Small shared helpers for the Twilio adapter.

Mirrors upstream ``adapter-twilio/src/utils.ts`` plus the Python-only
request/param plumbing (the framework-agnostic stand-ins for the WHATWG
``Request`` / ``URLSearchParams`` objects upstream relies on).
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from typing import Any, Literal, TypedDict
from urllib.parse import quote, unquote

from chat_sdk.adapters.twilio.types import TwilioFormParams, TwilioParamsInput

# Characters JavaScript's encodeURIComponent leaves unescaped beyond
# Python's always-safe set (letters, digits, ``_.-~``).
_ENCODE_URI_COMPONENT_SAFE = "!*'()"


def encode_uri_component(value: str) -> str:
    """Percent-encode ``value`` exactly like JS ``encodeURIComponent``.

    Thread IDs are shared cross-SDK state, so the escaping must match the
    TS adapter byte-for-byte (e.g. ``+`` -> ``%2B``, ``:`` -> ``%3A``).
    """
    return quote(value, safe=_ENCODE_URI_COMPONENT_SAFE)


def decode_uri_component(value: str) -> str:
    """Percent-decode ``value`` like JS ``decodeURIComponent``."""
    return unquote(value)


class TwilioSenderFields(TypedDict, total=False):
    """Sender kwargs for ``send_twilio_message`` (exactly one key set)."""

    from_: str
    messaging_service_sid: str


def sender_fields(sender: str) -> TwilioSenderFields:
    """Split a thread sender into Messages API sender fields.

    ``MG``-prefixed senders are Messaging Service SIDs; anything else is a
    phone number / channel address. Mirrors upstream ``senderFields``.
    """
    if sender.startswith("MG"):
        return {"messaging_service_sid": sender}
    return {"from_": sender}


def attachment_type(content_type: str | None) -> Literal["audio", "file", "image", "video"]:
    """Map a media content type to the SDK ``Attachment.type`` enum."""
    if content_type is not None and content_type.startswith("image/"):
        return "image"
    if content_type is not None and content_type.startswith("video/"):
        return "video"
    if content_type is not None and content_type.startswith("audio/"):
        return "audio"
    return "file"


def twiml_response() -> dict[str, Any]:
    """Empty TwiML acknowledgment returned by the messaging webhook.

    Mirrors upstream ``twimlResponse`` (note: ``application/xml`` here;
    the voice helpers use ``text/xml;charset=UTF-8`` like upstream).
    """
    return {
        "body": "<Response></Response>",
        "status": 200,
        "headers": {"content-type": "application/xml"},
    }


__all__ = [
    "as_param_pairs",
    "attachment_type",
    "coalesce",
    "decode_uri_component",
    "encode_uri_component",
    "first_param",
    "get_request_header",
    "get_request_method",
    "get_request_text",
    "get_request_url",
    "sender_fields",
    "twiml_response",
]


# =============================================================================
# Form-param plumbing (URLSearchParams stand-in)
# =============================================================================


def as_param_pairs(params: TwilioParamsInput) -> TwilioFormParams:
    """Normalize a mapping / iterable of pairs to ordered (name, value) pairs."""
    if isinstance(params, Mapping):
        return [(str(name), str(value)) for name, value in params.items()]
    return [(str(name), str(value)) for name, value in params]


def first_param(params: TwilioFormParams, name: str) -> str | None:
    """First value for ``name``, treating empty strings as missing.

    Mirrors the upstream ``value()`` helper (``URLSearchParams.get`` +
    empty-string normalization) shared by the webhook and voice parsers.
    """
    for key, value in params:
        if key == name:
            return value if len(value) > 0 else None
    return None


def coalesce(first: str | None, second: str | None) -> str | None:
    """``??`` for two optional strings (empty strings are real values)."""
    return first if first is not None else second


# =============================================================================
# Framework-agnostic request access (duck-typed, like the other adapters)
# =============================================================================


def get_request_method(request: Any) -> str:
    """The request HTTP method, defaulting to POST."""
    method = getattr(request, "method", None)
    return str(method).upper() if method else "POST"


def get_request_url(request: Any) -> str:
    """The full request URL as a string."""
    return str(getattr(request, "url", ""))


def get_request_header(request: Any, name: str) -> str | None:
    """Get a header value from a request object (case-insensitive)."""
    headers = getattr(request, "headers", None)
    if headers is None:
        return None
    if isinstance(headers, Mapping):
        for key, value in headers.items():
            if str(key).lower() == name.lower():
                return value
        return None
    return headers.get(name)


async def get_request_text(request: Any) -> str:
    """Extract the request body as text (sync or async ``text``/``body``).

    Twilio signs the decoded form parameter *values* (not the raw bytes),
    so a text body is sufficient for signature verification — unlike the
    Meta adapters, which HMAC the raw wire bytes.
    """
    for attr_name in ("text", "body"):
        value = getattr(request, attr_name, None)
        if value is None:
            continue
        if callable(value):
            result = value()
            value = await result if inspect.isawaitable(result) else result
        if isinstance(value, (bytes, bytearray)):
            return bytes(value).decode("utf-8")
        if isinstance(value, str):
            return value
    return ""
