"""Continuation / extraction helpers for the Teams webhook primitives.

Port of ``packages/adapter-teams/src/webhook/continuation.ts`` (chat@4.31,
commit 8c71411). Pulls provider-native reply data, the acting user, message
attachments, and bot-mention state out of a raw Bot Framework activity.

These helpers are SDK-free: they operate on plain dicts (the camelCase wire
shape) using membership / ``is not None`` checks — never truthiness ``or``
fallbacks that would coerce ``""``/``0``/``False`` into the wrong branch.
"""

from __future__ import annotations

from typing import Any

from chat_sdk.adapters.teams.webhook.types import (
    TeamsActivity,
    TeamsContinuation,
    TeamsWebhookAttachment,
    TeamsWebhookUser,
)


def _record(value: Any) -> dict[str, Any] | None:
    """Return ``value`` when it is a dict, else ``None`` (optional chaining)."""
    return value if isinstance(value, dict) else None


def _opt_str(value: Any) -> str | None:
    """Return ``value`` only when it is a non-empty string, else ``None``.

    Mirrors the upstream ``activity.foo ? { foo } : {}`` spread guards, which
    drop empty strings as well as ``undefined`` (an empty string is falsy in
    JS). Keeping the same shape avoids emitting blank continuation fields.
    """
    return value if isinstance(value, str) and value else None


def extract_teams_continuation(activity: TeamsActivity) -> TeamsContinuation:
    """Build a :class:`TeamsContinuation` from a raw activity.

    ``conversation_id`` / ``service_url`` default to ``""`` (upstream ``?? ""``)
    so the continuation always carries the two fields a reply needs; the rest
    are populated only when present. Upstream builds the result with an object
    spread where the *later* source wins when both are present, so the
    channelData-native fields take precedence: ``teamsChannelId`` over
    ``channel.id``; ``teamsTeamId`` over ``team.id``; ``channelData.tenant.id``
    over ``conversation.tenantId``.
    """
    channel_data = _record(activity.get("channelData")) or {}
    conversation = _record(activity.get("conversation")) or {}
    channel = _record(channel_data.get("channel")) or {}
    team = _record(channel_data.get("team")) or {}
    tenant = _record(channel_data.get("tenant")) or {}

    channel_id = _opt_str(channel_data.get("teamsChannelId"))
    if channel_id is None:
        channel_id = _opt_str(channel.get("id"))

    team_id = _opt_str(channel_data.get("teamsTeamId"))
    if team_id is None:
        team_id = _opt_str(team.get("id"))

    tenant_id = _opt_str(tenant.get("id"))
    if tenant_id is None:
        tenant_id = _opt_str(conversation.get("tenantId"))

    conversation_id = conversation.get("id")
    service_url = activity.get("serviceUrl")

    return TeamsContinuation(
        activity_id=_opt_str(activity.get("id")),
        channel_id=channel_id,
        conversation_id=conversation_id if isinstance(conversation_id, str) else "",
        reply_to_id=_opt_str(activity.get("replyToId")),
        service_url=service_url if isinstance(service_url, str) else "",
        team_id=team_id,
        tenant_id=tenant_id,
    )


def extract_teams_user(activity: TeamsActivity) -> TeamsWebhookUser | None:
    """Return the acting user, or ``None`` when ``from.id`` is missing.

    Upstream short-circuits on ``!activity.from?.id`` — a present user object
    with no ``id`` yields ``None``. ``aadObjectId`` / ``name`` are included
    only when truthy on the wire.
    """
    sender = _record(activity.get("from"))
    if sender is None:
        return None
    user_id = sender.get("id")
    if not (isinstance(user_id, str) and user_id):
        return None
    return TeamsWebhookUser(
        aad_object_id=_opt_str(sender.get("aadObjectId")),
        id=user_id,
        name=_opt_str(sender.get("name")),
    )


def extract_teams_attachments(activity: TeamsActivity) -> list[TeamsWebhookAttachment]:
    """Normalize ``activity.attachments``, skipping non-object entries.

    Non-list ``attachments`` yields ``[]``; ``None`` / non-dict entries are
    dropped. The typed ``content`` is the entry's ``content`` value (``None``
    when absent); the key-present-vs-absent distinction (upstream's
    ``typeof attachment.content === "undefined" ? {} : { content }``) is
    preserved on ``raw``. ``content_type`` / ``content_url`` / ``name`` are
    copied only when they are strings.
    """
    raw_attachments = activity.get("attachments")
    if not isinstance(raw_attachments, list):
        return []
    result: list[TeamsWebhookAttachment] = []
    for attachment in raw_attachments:
        record = _record(attachment)
        if record is None:
            continue
        content_type = record.get("contentType")
        content_url = record.get("contentUrl")
        name = record.get("name")
        result.append(
            TeamsWebhookAttachment(
                # The typed ``content`` is ``None`` for both a missing key and an
                # explicit ``null`` (upstream copies the value only when present,
                # but the absent-value is itself ``undefined``/``None``). The
                # key-present-vs-absent distinction is preserved in ``raw``.
                content=record.get("content"),
                content_type=content_type if isinstance(content_type, str) else None,
                content_url=content_url if isinstance(content_url, str) else None,
                name=name if isinstance(name, str) else None,
                raw=record,
            )
        )
    return result


def is_teams_mention(activity: TeamsActivity, bot_app_id: str | None = None) -> bool:
    """Return ``True`` when the activity @-mentions the bot.

    Without a ``bot_app_id`` this is always ``False``. A match requires a
    ``mention`` entity whose ``mentioned.id`` either equals ``bot_app_id`` or
    ends with the exact ``":" + bot_app_id`` suffix (Bot Framework prefixes
    user ids like ``28:<app-id>``).
    """
    if not bot_app_id:
        return False
    entities = activity.get("entities")
    if not isinstance(entities, list):
        return False
    suffix = f":{bot_app_id}"
    for entity in entities:
        record = _record(entity)
        if record is None or record.get("type") != "mention":
            continue
        mentioned = _record(record.get("mentioned"))
        if mentioned is None:
            continue
        mentioned_id = mentioned.get("id")
        if not isinstance(mentioned_id, str):
            continue
        if mentioned_id == bot_app_id or mentioned_id.endswith(suffix):
            return True
    return False
