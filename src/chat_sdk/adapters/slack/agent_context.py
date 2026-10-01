"""Slack active-view context (Agent messaging experience / ``agent_view``).

Port of upstream ``packages/adapter-slack/src/agent-context.ts``
(vercel/chat ``1721fa01`` #684). Normalizes Slack's ``app_context`` /
``context`` entity tokens into the core :data:`chat_sdk.types.AppContextEntity`
union. Fields stay snake_case (``channel_id``, ``message_ts``).
"""

from __future__ import annotations

from typing import Any, TypedDict

from chat_sdk.types import (
    AppContextCanvasEntity,
    AppContextChannelEntity,
    AppContextEntity,
    AppContextListEntity,
    AppContextMessageEntity,
    AppContextUnknownEntity,
    Message,
)

__all__ = [
    "SlackAppContext",
    "SlackAppContextChangedEvent",
    "SlackAppContextEntity",
    "get_app_context",
    "normalize_app_context_entities",
]


class SlackAppContextEntity(TypedDict, total=False):
    """A single entity in a Slack active-view context (wire shape)."""

    enterprise_id: str
    team_id: str
    type: str
    value: Any


class SlackAppContext(TypedDict, total=False):
    """Slack active-view context (``app_context`` on messages, ``context`` elsewhere)."""

    entities: list[SlackAppContextEntity]


class SlackAppContextChangedEvent(TypedDict, total=False):
    """Slack ``app_context_changed`` event payload (wire shape, agent_view only)."""

    channel: str
    context: SlackAppContext
    event_ts: str
    type: str  # "app_context_changed"
    user: str


_CHANNEL_TOKEN = "slack#/types/channel_id"
_CANVAS_TOKEN = "slack#/types/canvas_id"
_LIST_TOKEN = "slack#/types/list_id"
_MESSAGE_TOKEN = "slack#/types/message_context"


def _js_truthy(value: Any) -> bool:
    """JS truthiness for a JSON value: ``{}`` and ``[]`` are truthy."""
    return isinstance(value, (dict, list)) or bool(value)


def normalize_app_context_entities(context: Any) -> list[AppContextEntity]:
    """Normalize Slack active-view context entities into core ``AppContextEntity`` values.

    Unrecognized entity types map to ``kind="unknown"`` for forward
    compatibility, as does a ``message_context`` whose value is not an object
    with string ``message_ts`` / ``channel_id``.

    Python-specific: a malformed context never raises. Upstream throws (a
    webhook 500 and a Slack retry loop) when ``entities`` is not an array or
    an entity is ``null``; here a non-object context or non-list ``entities``
    yields ``[]`` and a non-object entity becomes ``kind="unknown"`` with
    ``type=""`` and the raw entity as ``value``.

    Args:
        context: The Slack context object (``{"entities": [...]}``); may be
            missing or malformed.

    Returns:
        Relevance-ordered normalized entities; empty when the context has none.
    """
    # Divergence from upstream — see docs/UPSTREAM_SYNC.md
    if not isinstance(context, dict):
        return []
    entities = context.get("entities")
    if not isinstance(entities, list):
        return []

    result: list[AppContextEntity] = []
    for entity in entities:
        if not isinstance(entity, dict):
            result.append(AppContextUnknownEntity(type="", value=entity))
            continue

        # Fields pass through unvalidated, as upstream's casts do.
        team_id: Any = entity.get("team_id")
        enterprise_id: Any = entity.get("enterprise_id")
        entity_type: Any = entity.get("type")
        value: Any = entity.get("value")

        if entity_type == _CHANNEL_TOKEN:
            result.append(AppContextChannelEntity(channel_id=value, team_id=team_id, enterprise_id=enterprise_id))
            continue
        if entity_type == _CANVAS_TOKEN:
            result.append(AppContextCanvasEntity(canvas_id=value, team_id=team_id, enterprise_id=enterprise_id))
            continue
        if entity_type == _LIST_TOKEN:
            result.append(AppContextListEntity(list_id=value, team_id=team_id, enterprise_id=enterprise_id))
            continue
        # A malformed message_context value falls through to kind "unknown"
        # instead of crashing the webhook (upstream comment: one odd entity
        # must not turn into a 500 + Slack retry loop).
        if (
            entity_type == _MESSAGE_TOKEN
            and isinstance(value, dict)
            and isinstance(value.get("message_ts"), str)
            and isinstance(value.get("channel_id"), str)
        ):
            result.append(
                AppContextMessageEntity(
                    channel_id=value["channel_id"],
                    message_ts=value["message_ts"],
                    team_id=team_id,
                    enterprise_id=enterprise_id,
                )
            )
            continue

        result.append(
            AppContextUnknownEntity(type=entity_type, value=value, team_id=team_id, enterprise_id=enterprise_id)
        )
    return result


def get_app_context(message: Message) -> list[AppContextEntity]:
    """Read the folded active-view context Slack attaches to a DM message.

    Slack folds it into ``message.im``'s ``app_context`` field; the adapter
    keeps the raw event on ``message.raw``.

    Args:
        message: The incoming message whose raw payload may carry ``app_context``.

    Returns:
        Normalized entities; empty when no folded context is present.
    """
    raw = getattr(message, "raw", None)
    context = raw.get("app_context") if isinstance(raw, dict) else None
    return normalize_app_context_entities(context) if _js_truthy(context) else []
