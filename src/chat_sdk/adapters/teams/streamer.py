"""Teams SDK streamer plumbing.

Builds the :class:`~microsoft_teams.api.ConversationReference` the Teams SDK
``IStreamer`` (``microsoft_teams.apps.StreamerProtocol`` /
``HttpStream``) needs, from the inbound Bot Framework activity dict the adapter
already parses. Kept in its own module so the SDK model construction stays
isolated from ``adapter.py`` and importable lazily (Port Rule: optional/SDK
deps imported inside functions, not at module top).

Mirrors what the Teams SDK's own ``ActivityContext`` does in
``microsoft_teams/apps/app_process.py`` ``_build_context`` (it builds a
``ConversationReference`` from the activity and calls
``ActivitySender.create_stream(ref)`` to expose ``ctx.stream``). Our bridge
owns dispatch, so we reproduce just the reference-building step.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from microsoft_teams.api import ConversationReference


def build_conversation_reference(activity: dict[str, Any], *, bot_app_id: str) -> ConversationReference:
    """Build a :class:`ConversationReference` from an inbound activity dict.

    The streamer reads ``ref.conversation.id`` and ``ref.service_url`` to
    target the Bot Framework streaming endpoint, and ``ref.bot`` to populate
    the outgoing activity's ``from`` account. We source these from the inbound
    activity: the bot is the activity ``recipient`` (falling back to a
    synthetic account carrying ``bot_app_id`` when the recipient is absent),
    the conversation is the activity ``conversation``, and ``channelId`` /
    ``serviceUrl`` come straight off the activity.

    Raises if required fields are missing — the caller catches and falls back
    to buffered posting.
    """
    from microsoft_teams.api import Account, ConversationAccount, ConversationReference

    recipient = activity.get("recipient") or {}
    bot = Account(
        id=recipient.get("id") or bot_app_id or "",
        name=recipient.get("name"),
    )

    conversation_raw = activity.get("conversation") or {}
    conversation = ConversationAccount(
        id=conversation_raw.get("id") or "",
        conversation_type=conversation_raw.get("conversationType"),
        tenant_id=conversation_raw.get("tenantId"),
        name=conversation_raw.get("name"),
        is_group=conversation_raw.get("isGroup"),
    )

    return ConversationReference(
        service_url=activity.get("serviceUrl") or "",
        activity_id=activity.get("id"),
        bot=bot,
        channel_id=activity.get("channelId") or "msteams",
        conversation=conversation,
        locale=activity.get("locale"),
    )
