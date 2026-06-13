"""Twilio webhook verification and parsing.

Python port of upstream ``adapter-twilio/src/webhook/{index,parse,verify}.ts``.

Verification follows Twilio's documented scheme: the ``X-Twilio-Signature``
header is HMAC-SHA1 (base64) over the exact public webhook URL concatenated
with the sorted POST form parameters (``name`` + ``value`` per pair, names
sorted, duplicate values deduplicated and sorted — matching upstream's
``twilioSignatureBase``). GET requests sign the URL only (the query string
is already part of it). Comparison is constant-time
(``hmac.compare_digest``).

See: https://www.twilio.com/docs/usage/security#validating-requests
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import inspect
import math
from typing import Any
from urllib.parse import parse_qsl, urlparse

from chat_sdk.adapters.twilio.api import resolve_twilio_credential
from chat_sdk.adapters.twilio.types import (
    ENV_AUTH_TOKEN,
    TwilioCredential,
    TwilioFormParams,
    TwilioMediaPayload,
    TwilioParamsInput,
    TwilioStatusPayload,
    TwilioTextPayload,
    TwilioUnsupportedPayload,
    TwilioVerifiedRequest,
    TwilioWebhookPayload,
    TwilioWebhookUrl,
    TwilioWebhookVerificationError,
    TwilioWebhookVerifier,
)
from chat_sdk.adapters.twilio.utils import (
    as_param_pairs,
    coalesce,
    first_param,
    get_request_header,
    get_request_method,
    get_request_text,
    get_request_url,
)

# =============================================================================
# Verification
# =============================================================================


async def read_twilio_webhook(
    request: Any,
    *,
    auth_token: TwilioCredential | None = None,
    webhook_url: TwilioWebhookUrl | None = None,
    webhook_verifier: TwilioWebhookVerifier | None = None,
) -> TwilioWebhookPayload:
    """Verify an incoming webhook request and parse its payload."""
    verified = await verify_twilio_request(
        request,
        auth_token=auth_token,
        webhook_url=webhook_url,
        webhook_verifier=webhook_verifier,
    )
    return parse_twilio_webhook_body(verified.params)


async def verify_twilio_request(
    request: Any,
    *,
    auth_token: TwilioCredential | None = None,
    webhook_url: TwilioWebhookUrl | None = None,
    webhook_verifier: TwilioWebhookVerifier | None = None,
) -> TwilioVerifiedRequest:
    """Verify a webhook request, returning its body and decoded params.

    ``webhook_verifier`` (when set) fully replaces signature verification:
    a falsy result rejects the request, a ``str`` result substitutes the
    body. Otherwise the ``X-Twilio-Signature`` header is checked against
    the auth token (explicit value/resolver, else ``TWILIO_AUTH_TOKEN``).
    """
    body = await get_request_text(request)

    if webhook_verifier is not None:
        result = webhook_verifier(request, body)
        if inspect.isawaitable(result):
            result = await result
        if not result:
            raise TwilioWebhookVerificationError("Twilio webhook verifier rejected the request")
        effective_body = result if isinstance(result, str) else body
        return TwilioVerifiedRequest(
            body=effective_body,
            params=_params_for_request(request, effective_body),
        )

    signature = get_request_header(request, "x-twilio-signature")
    if not signature:
        raise TwilioWebhookVerificationError("Twilio signature header is required")

    token = await resolve_twilio_credential(auth_token, ENV_AUTH_TOKEN)
    url = await resolve_twilio_webhook_url(request, webhook_url)
    params = _params_for_request(request, body)
    signed_params = None if get_request_method(request) == "GET" else params
    expected = sign_twilio_request(auth_token=token, params=signed_params, url=url)
    if not hmac.compare_digest(expected.encode("utf-8"), signature.encode("utf-8")):
        raise TwilioWebhookVerificationError("Twilio signature is invalid")
    return TwilioVerifiedRequest(body=body, params=params)


def sign_twilio_request(
    *,
    auth_token: str,
    url: str,
    params: TwilioParamsInput | None = None,
) -> str:
    """Compute the ``X-Twilio-Signature`` value for a URL + form params.

    Sync in Python (upstream is async only because of WebCrypto).
    """
    base = twilio_signature_base(url, params)
    digest = hmac.new(auth_token.encode("utf-8"), base.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def twilio_signature_base(url: str, params: TwilioParamsInput | None = None) -> str:
    """Build Twilio's validation base string: URL + sorted name/value pairs.

    Mirrors upstream exactly: pairs are grouped by name, values are
    deduplicated (set semantics) and sorted, and names are sorted.
    """
    if params is None:
        return url
    grouped: dict[str, set[str]] = {}
    for name, value in as_param_pairs(params):
        grouped.setdefault(name, set()).add(value)
    base = url
    for name in sorted(grouped):
        for value in sorted(grouped[name]):
            base += f"{name}{value}"
    return base


async def resolve_twilio_webhook_url(request: Any, webhook_url: TwilioWebhookUrl | None) -> str:
    """Resolve the URL Twilio signed: explicit/resolved, else the request URL."""
    if callable(webhook_url):
        result = webhook_url(request)
        return await result if inspect.isawaitable(result) else result
    return webhook_url if webhook_url is not None else get_request_url(request)


def _params_for_request(request: Any, body: str) -> TwilioFormParams:
    """Decoded params: the URL query for GET, the form body otherwise."""
    if get_request_method(request) == "GET":
        return parse_qsl(urlparse(get_request_url(request)).query, keep_blank_values=True)
    return parse_qsl(body, keep_blank_values=True)


# =============================================================================
# Parsing
# =============================================================================


def parse_twilio_webhook_body(params: TwilioParamsInput) -> TwilioWebhookPayload:
    """Classify a decoded webhook into text / status / unsupported."""
    pairs = as_param_pairs(params)
    status = coalesce(first_param(pairs, "MessageStatus"), first_param(pairs, "SmsStatus"))
    body = first_param(pairs, "Body")
    from_ = first_param(pairs, "From")
    to = first_param(pairs, "To")
    message_sid = coalesce(first_param(pairs, "MessageSid"), first_param(pairs, "SmsMessageSid"))

    if status is not None and body is None:
        return TwilioStatusPayload(
            account_sid=first_param(pairs, "AccountSid"),
            from_=from_,
            message_sid=message_sid,
            message_status=status,
            raw=pairs,
            to=to,
        )

    if from_ is not None and to is not None and (body is not None or _media_count(pairs) > 0):
        return TwilioTextPayload(
            account_sid=first_param(pairs, "AccountSid"),
            body=body if body is not None else "",
            from_=from_,
            media=_media_payloads(pairs),
            message_sid=message_sid,
            raw=pairs,
            to=to,
        )

    return TwilioUnsupportedPayload(raw=pairs)


def _media_payloads(pairs: TwilioFormParams) -> list[TwilioMediaPayload]:
    media: list[TwilioMediaPayload] = []
    for index in range(_media_count(pairs)):
        url = first_param(pairs, f"MediaUrl{index}")
        if url is None:
            continue
        media.append(
            TwilioMediaPayload(
                url=url,
                content_type=first_param(pairs, f"MediaContentType{index}"),
            )
        )
    return media


def _media_count(pairs: TwilioFormParams) -> int:
    """``NumMedia`` as a loop bound, with JS ``Number`` edge semantics."""
    raw = first_param(pairs, "NumMedia")
    if raw is None:
        return 0
    try:
        count = float(raw)
    except ValueError:
        return 0
    if math.isnan(count) or count <= 0:
        return 0
    try:
        return math.ceil(count)
    except (OverflowError, ValueError):
        return 0
