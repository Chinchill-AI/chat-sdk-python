"""Verification for the Slack webhook primitives subpath.

Port of ``packages/adapter-slack/src/webhook/verify.ts`` (vercel/chat#538).

Python-specific notes (intentional divergences from the TS surface):

- ``verify_slack_signature`` is synchronous — Python's ``hmac`` needs no
  WebCrypto-style async key import.
- ``verify_slack_request`` / ``read_slack_webhook`` accept a pre-read
  ``body=`` because duck-typed Python request bodies are not always
  re-readable (the Fetch API's ``request.text()`` has no universal Python
  equivalent). When omitted, the body is read via
  :func:`~chat_sdk.adapters.slack.webhook.utils.read_slack_request_body`.
- ``now`` returns epoch **seconds** (like ``time.time``), not milliseconds.
"""

from __future__ import annotations

import hashlib
import hmac
import inspect
import math
import re
import time
from collections.abc import Callable
from typing import Any

from chat_sdk.adapters.slack.webhook.parse import parse_slack_webhook_body
from chat_sdk.adapters.slack.webhook.types import (
    SlackHeaders,
    SlackWebhookPayload,
    SlackWebhookVerificationError,
    SlackWebhookVerifier,
)
from chat_sdk.adapters.slack.webhook.utils import get_header, read_slack_request_body

_HEX_PATTERN = re.compile(r"^[0-9a-f]+$", re.IGNORECASE)
_DEFAULT_MAX_SKEW_SECONDS = 300


async def read_slack_webhook(
    request: Any,
    *,
    body: str | None = None,
    content_type: str | None = None,
    signing_secret: str | None = None,
    webhook_verifier: SlackWebhookVerifier | None = None,
    max_skew_seconds: float | None = None,
    now: Callable[[], float] | None = None,
) -> SlackWebhookPayload:
    """Verify a Slack request and parse its body into a typed payload."""
    verified_body = await verify_slack_request(
        request,
        body=body,
        signing_secret=signing_secret,
        webhook_verifier=webhook_verifier,
        max_skew_seconds=max_skew_seconds,
        now=now,
    )
    return parse_slack_webhook_body(
        verified_body,
        content_type=content_type,
        headers=getattr(request, "headers", None),
    )


async def verify_slack_request(
    request: Any,
    *,
    body: str | None = None,
    signing_secret: str | None = None,
    webhook_verifier: SlackWebhookVerifier | None = None,
    max_skew_seconds: float | None = None,
    now: Callable[[], float] | None = None,
) -> str:
    """Verify a Slack request and return its (possibly substituted) body.

    A configured ``webhook_verifier`` takes precedence over
    ``signing_secret``; see
    :data:`~chat_sdk.adapters.slack.webhook.types.SlackWebhookVerifier`
    for the security contract. Raises
    :class:`SlackWebhookVerificationError` on rejection.
    """
    if body is None:
        body = await read_slack_request_body(request)
    if webhook_verifier is not None:
        result = webhook_verifier(request, body)
        if inspect.isawaitable(result):
            result = await result
        if not result:
            raise SlackWebhookVerificationError("Slack webhook verifier rejected the request")
        return result if isinstance(result, str) else body

    verify_slack_signature(
        body,
        getattr(request, "headers", None),
        signing_secret=signing_secret,
        max_skew_seconds=max_skew_seconds,
        now=now,
    )
    return body


def verify_slack_signature(
    body: str,
    headers: SlackHeaders | None,
    *,
    signing_secret: str | None,
    max_skew_seconds: float | None = None,
    now: Callable[[], float] | None = None,
) -> None:
    """Verify Slack's ``v0`` HMAC-SHA256 request signature.

    Raises :class:`SlackWebhookVerificationError` when the secret is
    missing, the signature headers are absent or malformed, the timestamp
    is outside the skew window (default 300 seconds), or the digest does
    not match. Returns ``None`` on success.
    """
    if not signing_secret:
        raise SlackWebhookVerificationError("Slack signing secret is required")

    timestamp = get_header(headers, "x-slack-request-timestamp")
    signature = get_header(headers, "x-slack-signature")
    if not (timestamp and signature):
        raise SlackWebhookVerificationError("Slack signature headers are required")

    try:
        timestamp_seconds = float(timestamp)
    except ValueError as exc:
        raise SlackWebhookVerificationError("Slack timestamp is invalid") from exc
    if not math.isfinite(timestamp_seconds):
        raise SlackWebhookVerificationError("Slack timestamp is invalid")

    now_seconds = math.floor(now() if now is not None else time.time())
    skew = max_skew_seconds if max_skew_seconds is not None else _DEFAULT_MAX_SKEW_SECONDS
    if abs(now_seconds - timestamp_seconds) > skew:
        raise SlackWebhookVerificationError("Slack timestamp is too old")

    if not _verify_slack_signature_value(body, signing_secret, timestamp, signature):
        raise SlackWebhookVerificationError("Slack signature is invalid")


def _verify_slack_signature_value(body: str, signing_secret: str, timestamp: str, signature: str) -> bool:
    provided = _parse_slack_signature(signature)
    expected = hmac.new(
        signing_secret.encode("utf-8"),
        f"v0:{timestamp}:{body}".encode(),
        hashlib.sha256,
    ).digest()
    # ``hmac.compare_digest`` is the canonical constant-time comparison.
    # A regression to ``==`` would leak signature bytes via timing.
    return hmac.compare_digest(provided, expected)


def _parse_slack_signature(signature: str) -> bytes:
    if not signature.startswith("v0="):
        raise SlackWebhookVerificationError("Slack signature is invalid")

    hex_part = signature[3:]
    if len(hex_part) % 2 != 0 or not _HEX_PATTERN.match(hex_part):
        raise SlackWebhookVerificationError("Slack signature is invalid")

    return bytes.fromhex(hex_part)
