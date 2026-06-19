"""Types for the Teams webhook primitives subpath.

Port of ``packages/adapter-teams/src/webhook/types.ts`` (chat@4.31, commit
8c71411). These types describe the lightweight, runtime-free webhook surface:
classifying an inbound Bot Framework :class:`TeamsActivity` into typed,
discriminated payload dataclasses with provider-native continuation data —
without the full Teams adapter, the ``microsoft_teams`` SDK, chat state,
dedupe, locks, or subscriptions.

Wire-shape note (port rule: camelCase at the serialization boundary):
the *typed* results use ``snake_case`` discriminated dataclasses, while the
``raw`` passthrough preserves the original camelCase Bot Framework activity
verbatim (``channelData.teamsChannelId``, ``replyToId``, ``aadObjectId``,
``tenantId``, ...). Callers that need wire-exact fields read them from
``raw``; callers that want a normalized view read the typed fields.

Importing this module never imports ``microsoft_teams``, HTTP clients, or the
high-level :mod:`chat_sdk.adapters.teams.adapter`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias

# A raw Bot Framework activity, kept as a plain dict so the primitives stay
# SDK-free. Upstream models this as ``TeamsActivity extends Record<string,
# unknown>`` — every wire key (camelCase) survives untouched in this mapping.
TeamsActivity: TypeAlias = dict[str, Any]


@dataclass
class TeamsContinuation:
    """Provider-native continuation data for replying to an activity.

    ``conversation_id`` and ``service_url`` are always present (empty string
    when absent on the wire, mirroring upstream's ``?? ""``); every other
    field is omitted entirely when the source value is missing.
    """

    conversation_id: str
    service_url: str
    activity_id: str | None = None
    channel_id: str | None = None
    reply_to_id: str | None = None
    team_id: str | None = None
    tenant_id: str | None = None


@dataclass
class TeamsWebhookUser:
    """The acting Teams user extracted from ``activity.from``."""

    id: str
    aad_object_id: str | None = None
    name: str | None = None


@dataclass
class TeamsWebhookAttachment:
    """A normalized attachment, with the original entry kept in ``raw``."""

    raw: dict[str, Any]
    content: Any | None = None
    content_type: str | None = None
    content_url: str | None = None
    name: str | None = None


@dataclass
class TeamsParseOptions:
    """Options for :func:`parse_teams_webhook_body`."""

    bot_app_id: str | None = None


@dataclass
class _TeamsPayloadBase:
    """Shared fields for every classified Teams payload."""

    raw: TeamsActivity
    continuation: TeamsContinuation | None = None


@dataclass
class TeamsMessagePayload(_TeamsPayloadBase):
    """A plain inbound ``message`` activity."""

    attachments: list[TeamsWebhookAttachment] = field(default_factory=list)
    is_mention: bool = False
    text: str = ""
    user: TeamsWebhookUser | None = None
    kind: Literal["message"] = "message"


@dataclass
class TeamsMessageReactionPayload(_TeamsPayloadBase):
    """A ``messageReaction`` activity (reaction add/remove)."""

    action: str | None = None
    message_id: str | None = None
    user: TeamsWebhookUser | None = None
    kind: Literal["message_reaction"] = "message_reaction"


@dataclass
class TeamsCardActionPayload(_TeamsPayloadBase):
    """An Adaptive Card / ``Action.Submit`` action."""

    action_id: str | None = None
    user: TeamsWebhookUser | None = None
    value: Any | None = None
    kind: Literal["card_action"] = "card_action"


@dataclass
class TeamsDialogOpenPayload(_TeamsPayloadBase):
    """A ``task/fetch`` invoke (dialog open request)."""

    user: TeamsWebhookUser | None = None
    value: Any | None = None
    kind: Literal["dialog_open"] = "dialog_open"


@dataclass
class TeamsDialogSubmitPayload(_TeamsPayloadBase):
    """A ``task/submit`` invoke (dialog submission)."""

    user: TeamsWebhookUser | None = None
    value: Any | None = None
    kind: Literal["dialog_submit"] = "dialog_submit"


@dataclass
class TeamsConversationUpdatePayload(_TeamsPayloadBase):
    """A ``conversationUpdate`` lifecycle activity."""

    kind: Literal["conversation_update"] = "conversation_update"


@dataclass
class TeamsInstallationUpdatePayload(_TeamsPayloadBase):
    """An ``installationUpdate`` lifecycle activity."""

    action: str | None = None
    kind: Literal["installation_update"] = "installation_update"


@dataclass
class TeamsUnsupportedPayload(_TeamsPayloadBase):
    """Any activity the primitives recognize but do not model."""

    reason: str = ""
    kind: Literal["unsupported"] = "unsupported"


TeamsWebhookPayload: TypeAlias = (
    TeamsCardActionPayload
    | TeamsConversationUpdatePayload
    | TeamsDialogOpenPayload
    | TeamsDialogSubmitPayload
    | TeamsInstallationUpdatePayload
    | TeamsMessagePayload
    | TeamsMessageReactionPayload
    | TeamsUnsupportedPayload
)


class TeamsWebhookError(Exception):
    """Base error for the Teams webhook primitives."""


class TeamsWebhookParseError(TeamsWebhookError):
    """Raised when a Teams webhook body cannot be parsed."""
