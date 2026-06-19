"""Teams webhook primitives — a lightweight, runtime-free subpath.

Port of ``packages/adapter-teams/src/webhook`` (chat@4.31, commit 8c71411),
exposed upstream as ``@chat-adapter/teams/webhook``. Provides primitives for
classifying an inbound Bot Framework activity into typed, discriminated
payloads, plus continuation / user / attachment extraction and bot-mention
detection — without the full Teams adapter, the ``microsoft_teams`` SDK,
chat state, dedupe, locks, or subscriptions.

Parse-ONLY: these primitives never verify JWTs and never make outbound HTTP
requests. Importing this module never imports ``microsoft_teams``, HTTP
clients, or the high-level :mod:`chat_sdk.adapters.teams.adapter`.

Wire boundary: typed results are ``snake_case`` dataclasses; the original
camelCase Bot Framework activity is preserved verbatim on each payload's
``raw`` field (``channelData.teamsChannelId``, ``replyToId``, ``aadObjectId``,
``tenantId``, ...).
"""

from __future__ import annotations

import inspect
from typing import Any

from chat_sdk.adapters.teams.webhook.continuation import (
    extract_teams_attachments,
    extract_teams_continuation,
    extract_teams_user,
    is_teams_mention,
)
from chat_sdk.adapters.teams.webhook.parse import parse_teams_webhook_body
from chat_sdk.adapters.teams.webhook.types import (
    TeamsActivity,
    TeamsCardActionPayload,
    TeamsContinuation,
    TeamsConversationUpdatePayload,
    TeamsDialogOpenPayload,
    TeamsDialogSubmitPayload,
    TeamsInstallationUpdatePayload,
    TeamsMessagePayload,
    TeamsMessageReactionPayload,
    TeamsParseOptions,
    TeamsUnsupportedPayload,
    TeamsWebhookAttachment,
    TeamsWebhookError,
    TeamsWebhookParseError,
    TeamsWebhookPayload,
    TeamsWebhookUser,
)


async def read_teams_request_body(request: Any) -> str:
    """Read the raw body from a duck-typed request object.

    Python stand-in for the Fetch API's ``await request.text()`` used by the
    upstream ``readTeamsWebhook`` helper. Supports:

    - ``request.text`` as an (async or sync) method or plain attribute
    - ``request.body`` as an (async or sync) method, awaitable, bytes, or str
    - falling back to ``str(request)``

    Bytes are decoded as UTF-8.
    """
    text_attr = getattr(request, "text", None)
    if text_attr is not None:
        if callable(text_attr):
            result = text_attr()
            text_attr = await result if inspect.isawaitable(result) else result
        return text_attr.decode("utf-8") if isinstance(text_attr, (bytes, bytearray)) else str(text_attr)
    raw = getattr(request, "body", None)
    if raw is not None:
        if callable(raw):
            raw = raw()
        if inspect.isawaitable(raw):
            raw = await raw
        return raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
    return str(request)


async def read_teams_webhook(
    request: Any,
    options: TeamsParseOptions | None = None,
) -> TeamsWebhookPayload:
    """Read a request body and classify it — no JWT verification.

    Port of upstream ``readTeamsWebhook``: reads ``await request.text()`` then
    delegates to :func:`parse_teams_webhook_body`.
    """
    body = await read_teams_request_body(request)
    return parse_teams_webhook_body(body, options)


__all__ = [
    "TeamsActivity",
    "TeamsCardActionPayload",
    "TeamsContinuation",
    "TeamsConversationUpdatePayload",
    "TeamsDialogOpenPayload",
    "TeamsDialogSubmitPayload",
    "TeamsInstallationUpdatePayload",
    "TeamsMessagePayload",
    "TeamsMessageReactionPayload",
    "TeamsParseOptions",
    "TeamsUnsupportedPayload",
    "TeamsWebhookAttachment",
    "TeamsWebhookError",
    "TeamsWebhookParseError",
    "TeamsWebhookPayload",
    "TeamsWebhookUser",
    "extract_teams_attachments",
    "extract_teams_continuation",
    "extract_teams_user",
    "is_teams_mention",
    "parse_teams_webhook_body",
    "read_teams_request_body",
    "read_teams_webhook",
]
