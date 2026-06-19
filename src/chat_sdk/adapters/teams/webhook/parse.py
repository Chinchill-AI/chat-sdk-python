"""Parsing for the Teams webhook primitives subpath.

Port of ``packages/adapter-teams/src/webhook/parse.ts`` (chat@4.31, commit
8c71411). Classifies an inbound Bot Framework activity into a typed,
discriminated payload (``message`` / ``card_action`` / ``dialog_open`` /
``dialog_submit`` / ``message_reaction`` / ``conversation_update`` /
``installation_update`` / ``unsupported``) without the full Teams adapter or
the ``microsoft_teams`` SDK.

Parse-ONLY: no JWT verification, no outbound HTTP. (JWT verification and the
authenticated reply client live in sibling primitive subpaths.)
"""

from __future__ import annotations

import json
from typing import Any

from chat_sdk.adapters.teams.webhook.continuation import (
    extract_teams_attachments,
    extract_teams_continuation,
    extract_teams_user,
    is_teams_mention,
)
from chat_sdk.adapters.teams.webhook.types import (
    TeamsActivity,
    TeamsCardActionPayload,
    TeamsConversationUpdatePayload,
    TeamsDialogOpenPayload,
    TeamsDialogSubmitPayload,
    TeamsInstallationUpdatePayload,
    TeamsMessagePayload,
    TeamsMessageReactionPayload,
    TeamsParseOptions,
    TeamsUnsupportedPayload,
    TeamsWebhookParseError,
    TeamsWebhookPayload,
)


def parse_teams_webhook_body(
    body: str | Any,
    options: TeamsParseOptions | None = None,
) -> TeamsWebhookPayload:
    """Classify a Teams webhook body into a typed payload.

    ``body`` may be a JSON string (parsed here) or an already-decoded mapping.
    A string that is not valid JSON, or a body that does not decode to an
    object, raises :class:`TeamsWebhookParseError`.
    """
    if options is None:
        options = TeamsParseOptions()
    activity = _parse_activity(body)
    continuation = extract_teams_continuation(activity)
    user = extract_teams_user(activity)

    activity_type = activity.get("type")

    if activity_type == "message":
        if _is_action_submit_message(activity):
            return TeamsCardActionPayload(
                action_id=_read_action_id(activity.get("value")),
                continuation=continuation,
                raw=activity,
                user=user,
                value=activity.get("value"),
            )
        text = activity.get("text")
        return TeamsMessagePayload(
            attachments=extract_teams_attachments(activity),
            continuation=continuation,
            is_mention=is_teams_mention(activity, options.bot_app_id),
            raw=activity,
            text=text if isinstance(text, str) else "",
            user=user,
        )

    if activity_type == "messageReaction":
        action = activity.get("action")
        reply_to_id = activity.get("replyToId")
        message_id = reply_to_id if reply_to_id is not None else activity.get("id")
        return TeamsMessageReactionPayload(
            action=action if isinstance(action, str) else None,
            continuation=continuation,
            message_id=message_id if isinstance(message_id, str) else None,
            raw=activity,
            user=user,
        )

    if activity_type == "invoke":
        name = activity.get("name")
        if name == "task/fetch":
            return TeamsDialogOpenPayload(
                continuation=continuation,
                raw=activity,
                user=user,
                value=activity.get("value"),
            )
        if name == "task/submit":
            return TeamsDialogSubmitPayload(
                continuation=continuation,
                raw=activity,
                user=user,
                value=activity.get("value"),
            )
        if name == "adaptiveCard/action":
            return TeamsCardActionPayload(
                action_id=_read_action_id(activity.get("value")),
                continuation=continuation,
                raw=activity,
                user=user,
                value=activity.get("value"),
            )

    if activity_type == "conversationUpdate":
        return TeamsConversationUpdatePayload(continuation=continuation, raw=activity)

    if activity_type == "installationUpdate":
        action = activity.get("action")
        return TeamsInstallationUpdatePayload(
            action=action if isinstance(action, str) else None,
            continuation=continuation,
            raw=activity,
        )

    reason_type = activity_type if isinstance(activity_type, str) else "unknown"
    return TeamsUnsupportedPayload(
        continuation=continuation,
        raw=activity,
        reason=f"Unsupported Teams activity type: {reason_type}",
    )


def _parse_activity(body: str | Any) -> TeamsActivity:
    if isinstance(body, str):
        try:
            parsed = json.loads(body)
        except (json.JSONDecodeError, ValueError) as exc:
            raise TeamsWebhookParseError("Invalid Teams webhook JSON body") from exc
        return _assert_activity(parsed)
    return _assert_activity(body)


def _assert_activity(value: Any) -> TeamsActivity:
    if not isinstance(value, dict):
        raise TeamsWebhookParseError("Teams webhook body must be an object")
    return value


def _is_action_submit_message(activity: TeamsActivity) -> bool:
    value = activity.get("value")
    return isinstance(value, dict) and ("actionId" in value or "msteams" in value)


def _read_action_id(value: Any) -> str | None:
    if not (isinstance(value, dict) and "actionId" in value):
        return None
    action_id = value.get("actionId")
    return action_id if isinstance(action_id, str) else None
