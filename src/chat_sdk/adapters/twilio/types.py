"""Type definitions for the Twilio adapter.

Based on Twilio Programmable Messaging (SMS/MMS webhooks + the Messages
REST API) and the Programmable Voice webhook surface.
See: https://www.twilio.com/docs/messaging

Python port of upstream ``packages/adapter-twilio`` (``src/types.ts``,
``src/webhook/types.ts``, and the option/resource interfaces from
``src/api/index.ts``). Internal fields are snake_case; the Twilio REST
resources (:data:`TwilioMessageResource` / :data:`TwilioCallResource`)
keep Twilio's own snake_case wire keys verbatim.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Required, TypedDict

from chat_sdk.logger import Logger

# Environment-variable fallbacks for credentials and senders, matching
# upstream (TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN are resolved lazily at
# API-call time; TWILIO_MESSAGING_SERVICE_SID / TWILIO_PHONE_NUMBER at
# adapter construction time).
ENV_ACCOUNT_SID = "TWILIO_ACCOUNT_SID"
ENV_AUTH_TOKEN = "TWILIO_AUTH_TOKEN"
ENV_MESSAGING_SERVICE_SID = "TWILIO_MESSAGING_SERVICE_SID"
ENV_PHONE_NUMBER = "TWILIO_PHONE_NUMBER"

# =============================================================================
# Credentials
# =============================================================================

# A credential value: either the literal string or a zero-argument resolver
# (sync or async), mirroring upstream ``TwilioCredential``.
TwilioCredential = str | Callable[[], "str | Awaitable[str]"]


@dataclass
class TwilioCredentials:
    """Account SID + auth token pair used by the low-level API helpers."""

    account_sid: TwilioCredential | None = None
    auth_token: TwilioCredential | None = None


# =============================================================================
# HTTP plumbing (upstream's injectable ``fetch``)
# =============================================================================


@dataclass
class TwilioHttpResponse:
    """Minimal HTTP response shape returned by a :data:`TwilioHttpRequest`."""

    status: int
    body: bytes = b""

    @property
    def ok(self) -> bool:
        """True for 2xx statuses, mirroring the WHATWG ``Response.ok``."""
        return 200 <= self.status < 300


# Injectable HTTP transport: ``(method, url, headers, body) -> response``.
# ``body`` is the URL-encoded form string or ``None``. The Python analog of
# upstream's ``fetch?: TwilioFetch`` config option; when omitted the API
# helpers lazily import aiohttp.
TwilioHttpRequest = Callable[
    [str, str, Mapping[str, str], "str | None"],
    Awaitable[TwilioHttpResponse],
]

# =============================================================================
# Form parameters
# =============================================================================

# Value accepted when building a form (``encode_twilio_form``); sequences
# append one pair per item, ``None`` is omitted. Mirrors ``TwilioFormValue``.
TwilioFormValue = bool | float | int | str | Sequence[str] | None

# Field mapping accepted when building a form. Mirrors ``TwilioFormFields``.
TwilioFormFields = Mapping[str, TwilioFormValue]

# Decoded form parameters as ordered (name, value) pairs — the Python stand-in
# for upstream's ``URLSearchParams`` (preserves duplicates and order).
TwilioFormParams = list[tuple[str, str]]

# Loose input shape accepted by the parse helpers: a mapping or any iterable
# of (name, value) pairs. Normalized to :data:`TwilioFormParams` internally.
TwilioParamsInput = Mapping[str, str] | Iterable[tuple[str, str]]

# =============================================================================
# Webhook hooks
# =============================================================================

# Static webhook URL or a resolver called with the incoming request,
# mirroring upstream ``TwilioWebhookUrl``. Twilio signs the exact public URL
# it POSTs to, so deployments behind proxies must supply the external URL.
TwilioWebhookUrl = str | Callable[[Any], "str | Awaitable[str]"]

# SECURITY surface — custom webhook verifier, mirroring upstream
# ``TwilioWebhookVerifier``. Called with ``(request, body)`` and fully
# replaces X-Twilio-Signature HMAC verification when set (it takes
# precedence over ``auth_token`` config and the TWILIO_AUTH_TOKEN env var).
# Returning a truthy value passes the request; falsy rejects it (401); a
# ``str`` return substitutes the request body before parsing.
TwilioWebhookVerifier = Callable[[Any, str], "bool | str | Awaitable[bool | str]"]

# =============================================================================
# Configuration
# =============================================================================


@dataclass
class TwilioAdapterConfig:
    """Twilio adapter configuration.

    Every field is optional, mirroring upstream: ``account_sid`` and
    ``auth_token`` fall back to the ``TWILIO_ACCOUNT_SID`` /
    ``TWILIO_AUTH_TOKEN`` env vars lazily at API-call time, and
    ``messaging_service_sid`` / ``phone_number`` fall back to their env
    vars when the adapter is constructed.

    See: https://www.twilio.com/docs/messaging/guides/webhook-request
    """

    # Account SID credential (string or resolver). Lazy env fallback:
    # TWILIO_ACCOUNT_SID at API-call time.
    account_sid: TwilioCredential | None = None
    # Override the REST API base URL (default https://api.twilio.com).
    api_url: str | None = None
    # Auth token credential (string or resolver). Lazy env fallback:
    # TWILIO_AUTH_TOKEN at API-call time. Also the webhook signing key.
    auth_token: TwilioCredential | None = None
    # Injectable HTTP transport (upstream's ``fetch``); defaults to aiohttp.
    http_request: TwilioHttpRequest | None = None
    # Logger instance (defaults to a console logger named "twilio").
    logger: Logger | None = None
    # Messaging Service SID sender. Falls back to
    # TWILIO_MESSAGING_SERVICE_SID at construction time.
    messaging_service_sid: str | None = None
    # Phone number sender. Falls back to TWILIO_PHONE_NUMBER at
    # construction time.
    phone_number: str | None = None
    # StatusCallback URL forwarded on outbound messages.
    status_callback_url: str | None = None
    # Bot display name (default "bot").
    user_name: str | None = None
    # Public webhook URL (or resolver) used for signature verification.
    webhook_url: TwilioWebhookUrl | None = None
    # Custom verifier replacing signature verification (SECURITY surface).
    webhook_verifier: TwilioWebhookVerifier | None = None

    def resolved_messaging_service_sid(self) -> str | None:
        """Messaging Service SID with the env fallback (``??`` semantics)."""
        if self.messaging_service_sid is not None:
            return self.messaging_service_sid
        return os.environ.get(ENV_MESSAGING_SERVICE_SID)

    def resolved_phone_number(self) -> str | None:
        """Phone number with the env fallback (``??`` semantics)."""
        if self.phone_number is not None:
            return self.phone_number
        return os.environ.get(ENV_PHONE_NUMBER)


# =============================================================================
# Thread ID
# =============================================================================


@dataclass(frozen=True)
class TwilioThreadId:
    """Decoded thread ID for Twilio.

    A conversation is the (sender, recipient) address pair: ``sender`` is
    the bot-side address (a phone number or an ``MG...`` Messaging Service
    SID), ``recipient`` is the user's address. Channel-prefixed addresses
    (e.g. ``whatsapp:+1555...``) are preserved verbatim.
    """

    recipient: str
    sender: str


# =============================================================================
# REST resources (Twilio wire shapes — snake_case keys are Twilio's own)
# =============================================================================

# Message resource returned by the Messages API. Functional TypedDict syntax
# because ``from`` is a Python keyword; only ``sid`` is guaranteed.
TwilioMessageResource = TypedDict(
    "TwilioMessageResource",
    {
        "account_sid": str,
        "body": "str | None",
        "date_created": "str | None",
        "date_sent": "str | None",
        "date_updated": "str | None",
        "direction": str,
        "error_code": "int | None",
        "error_message": "str | None",
        "from": "str | None",
        "messaging_service_sid": "str | None",
        "num_media": str,
        "sid": Required[str],
        "status": str,
        "to": "str | None",
        "uri": str,
    },
    total=False,
)

# Call resource returned by the Calls API (used by ``update_twilio_call``).
TwilioCallResource = TypedDict(
    "TwilioCallResource",
    {
        "account_sid": str,
        "answered_by": "str | None",
        "caller_name": "str | None",
        "date_created": "str | None",
        "date_updated": "str | None",
        "direction": str,
        "duration": "str | None",
        "end_time": "str | None",
        "from": "str | None",
        "parent_call_sid": "str | None",
        "sid": Required[str],
        "start_time": "str | None",
        "status": str,
        "to": "str | None",
        "uri": str,
    },
    total=False,
)

# =============================================================================
# Webhook payloads
# =============================================================================


@dataclass
class TwilioMediaPayload:
    """A single inbound MMS media item (``MediaUrl{N}``)."""

    url: str
    content_type: str | None = None


@dataclass
class TwilioTextPayload:
    """An inbound SMS/MMS message webhook."""

    body: str
    from_: str
    to: str
    media: list[TwilioMediaPayload]
    raw: TwilioFormParams
    account_sid: str | None = None
    message_sid: str | None = None
    kind: Literal["text"] = "text"


@dataclass
class TwilioStatusPayload:
    """A message status callback webhook (``MessageStatus``/``SmsStatus``)."""

    message_status: str
    raw: TwilioFormParams
    account_sid: str | None = None
    from_: str | None = None
    message_sid: str | None = None
    to: str | None = None
    kind: Literal["status"] = "status"


@dataclass
class TwilioUnsupportedPayload:
    """Any webhook the parser does not recognize."""

    raw: TwilioFormParams
    kind: Literal["unsupported"] = "unsupported"


TwilioWebhookPayload = TwilioTextPayload | TwilioStatusPayload | TwilioUnsupportedPayload


@dataclass
class TwilioVerifiedRequest:
    """Result of webhook verification: the raw body and its decoded params."""

    body: str
    params: TwilioFormParams


# =============================================================================
# Webhook errors
# =============================================================================


class TwilioWebhookError(Exception):
    """Base error for Twilio webhook handling."""


class TwilioWebhookParseError(TwilioWebhookError):
    """The webhook request body could not be read or parsed."""


class TwilioWebhookVerificationError(TwilioWebhookError):
    """The webhook request failed signature / verifier checks."""


# =============================================================================
# Raw Message Type
# =============================================================================

# Platform-specific raw message type for Twilio: either a Messages API
# resource (dict) or a parsed webhook payload (dataclass with ``kind``),
# mirroring upstream ``TwilioRawMessage``.
TwilioRawMessage = TwilioMessageResource | TwilioWebhookPayload
