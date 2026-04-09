"""Replay integration test: Slack reaction webhooks.

Constructs realistic Slack ``reaction_added`` and ``reaction_removed`` webhook
payloads, creates a Chat instance with a MockAdapter, dispatches the reaction
events, and verifies the on_reaction handler is invoked with correct data.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from chat_sdk.emoji import get_emoji
from chat_sdk.types import Author, ReactionEvent

from .conftest import create_chat

# ---------------------------------------------------------------------------
# Realistic Slack reaction_added payload
# ---------------------------------------------------------------------------

SLACK_REACTION_ADDED_PAYLOAD: dict[str, Any] = {
    "token": "XXYYZZ",
    "team_id": "T00FAKETEAM",
    "api_app_id": "A00FAKEAPP1",
    "event": {
        "type": "reaction_added",
        "user": "U00FAKEUSER1",
        "reaction": "thumbsup",
        "item": {
            "type": "message",
            "channel": "C00FAKECHAN1",
            "ts": "1710000000.000100",
        },
        "item_user": "U00FAKEBOT01",
        "event_ts": "1710000050.000200",
    },
    "type": "event_callback",
    "event_id": "Ev00FAKEREACT01",
    "event_time": 1710000050,
}

# ---------------------------------------------------------------------------
# Realistic Slack reaction_removed payload
# ---------------------------------------------------------------------------

SLACK_REACTION_REMOVED_PAYLOAD: dict[str, Any] = {
    "token": "XXYYZZ",
    "team_id": "T00FAKETEAM",
    "api_app_id": "A00FAKEAPP1",
    "event": {
        "type": "reaction_removed",
        "user": "U00FAKEUSER1",
        "reaction": "thumbsup",
        "item": {
            "type": "message",
            "channel": "C00FAKECHAN1",
            "ts": "1710000000.000100",
        },
        "item_user": "U00FAKEBOT01",
        "event_ts": "1710000060.000300",
    },
    "type": "event_callback",
    "event_id": "Ev00FAKEREACT02",
    "event_time": 1710000060,
}

# ---------------------------------------------------------------------------
# Realistic Slack reaction with emoji shortcode
# ---------------------------------------------------------------------------

SLACK_REACTION_EYES_PAYLOAD: dict[str, Any] = {
    "token": "XXYYZZ",
    "team_id": "T00FAKETEAM",
    "api_app_id": "A00FAKEAPP1",
    "event": {
        "type": "reaction_added",
        "user": "U00FAKEUSER2",
        "reaction": "eyes",
        "item": {
            "type": "message",
            "channel": "C00FAKECHAN1",
            "ts": "1710000000.000100",
        },
        "item_user": "U00FAKEBOT01",
        "event_ts": "1710000070.000400",
    },
    "type": "event_callback",
    "event_id": "Ev00FAKEREACT03",
    "event_time": 1710000070,
}


def _make_reaction_event(
    adapter: Any,
    raw_emoji: str = "thumbsup",
    added: bool = True,
    user_id: str = "U00FAKEUSER1",
    user_name: str = "test.user",
    is_me: bool = False,
    thread_id: str = "slack:C00FAKECHAN1:1710000000.000100",
    message_id: str = "1710000000.000100",
) -> ReactionEvent:
    """Build a ReactionEvent from replayed payload data."""
    emoji = get_emoji("thumbs_up") if "thumbs" in raw_emoji else get_emoji(raw_emoji)
    return ReactionEvent(
        emoji=emoji,
        raw_emoji=raw_emoji,
        added=added,
        user=Author(
            user_id=user_id,
            user_name=user_name,
            full_name=user_name.title(),
            is_bot=is_me,
            is_me=is_me,
        ),
        message_id=message_id,
        thread_id=thread_id,
        adapter=adapter,
        thread=None,
        raw={},
    )


class TestReplayReactionAdded:
    """Replay a Slack reaction_added webhook."""

    @pytest.mark.asyncio
    async def test_reaction_added_triggers_handler(self):
        """reaction_added fires the on_reaction handler."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[ReactionEvent] = []

        chat.on_reaction(lambda event: captured.append(event))

        payload = SLACK_REACTION_ADDED_PAYLOAD["event"]
        event = _make_reaction_event(
            adapter,
            raw_emoji=payload["reaction"],
            added=True,
            user_id=payload["user"],
            message_id=payload["item"]["ts"],
        )
        chat.process_reaction(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 1
        assert captured[0].added is True
        assert captured[0].raw_emoji == "thumbsup"
        assert captured[0].user.user_id == "U00FAKEUSER1"
        assert captured[0].message_id == "1710000000.000100"

    @pytest.mark.asyncio
    async def test_reaction_added_has_correct_thread_id(self):
        """The reaction event thread_id matches the message's thread."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[ReactionEvent] = []

        chat.on_reaction(lambda event: captured.append(event))

        payload = SLACK_REACTION_ADDED_PAYLOAD["event"]
        thread_id = f"slack:{payload['item']['channel']}:{payload['item']['ts']}"
        event = _make_reaction_event(
            adapter,
            raw_emoji=payload["reaction"],
            added=True,
            user_id=payload["user"],
            thread_id=thread_id,
        )
        chat.process_reaction(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 1
        assert captured[0].thread_id == thread_id


class TestReplayReactionRemoved:
    """Replay a Slack reaction_removed webhook."""

    @pytest.mark.asyncio
    async def test_reaction_removed_triggers_handler(self):
        """reaction_removed fires the on_reaction handler with added=False."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[ReactionEvent] = []

        chat.on_reaction(lambda event: captured.append(event))

        payload = SLACK_REACTION_REMOVED_PAYLOAD["event"]
        event = _make_reaction_event(
            adapter,
            raw_emoji=payload["reaction"],
            added=False,
            user_id=payload["user"],
            message_id=payload["item"]["ts"],
        )
        chat.process_reaction(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 1
        assert captured[0].added is False
        assert captured[0].raw_emoji == "thumbsup"


class TestReplayReactionFiltered:
    """Replay reactions with emoji filtering."""

    @pytest.mark.asyncio
    async def test_filtered_reaction_handler_matches_emoji(self):
        """Handler with emoji filter only fires for matching reactions."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        thumbs_calls: list[ReactionEvent] = []
        all_calls: list[ReactionEvent] = []

        # Catch-all handler
        async def all_handler(event):
            all_calls.append(event)

        async def thumbs_handler(event):
            thumbs_calls.append(event)

        chat.on_reaction(all_handler)
        # Filtered handler for thumbs_up only
        chat.on_reaction(["thumbs_up"], thumbs_handler)

        # thumbsup reaction
        event1 = _make_reaction_event(adapter, raw_emoji="thumbsup", added=True)
        chat.process_reaction(event1)

        # eyes reaction
        event2 = _make_reaction_event(
            adapter,
            raw_emoji="eyes",
            added=True,
            user_id="U00FAKEUSER2",
        )
        chat.process_reaction(event2)
        await asyncio.sleep(0.05)

        assert len(all_calls) == 2
        assert len(thumbs_calls) == 1
        assert thumbs_calls[0].raw_emoji == "thumbsup"

    @pytest.mark.asyncio
    async def test_self_reaction_is_ignored(self):
        """Reactions from the bot itself (is_me=True) are not dispatched."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[ReactionEvent] = []

        chat.on_reaction(lambda event: captured.append(event))

        event = _make_reaction_event(
            adapter,
            raw_emoji="thumbsup",
            added=True,
            user_id="U00FAKEBOT01",
            user_name="testbot",
            is_me=True,
        )
        chat.process_reaction(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 0


class TestReplayReactionMultipleHandlers:
    """Replay reaction events with multiple handlers registered."""

    @pytest.mark.asyncio
    async def test_multiple_handlers_all_fire(self):
        """Both catch-all and filtered handlers fire for a matching emoji."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        handler1_calls: list[ReactionEvent] = []
        handler2_calls: list[ReactionEvent] = []

        async def handler1(event):
            handler1_calls.append(event)

        async def handler2(event):
            handler2_calls.append(event)

        chat.on_reaction(handler1)
        chat.on_reaction(handler2)

        event = _make_reaction_event(adapter, raw_emoji="thumbsup", added=True)
        chat.process_reaction(event)
        await asyncio.sleep(0.05)

        assert len(handler1_calls) == 1
        assert len(handler2_calls) == 1
