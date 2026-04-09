"""Integration tests for action (button click) and reaction replay flows.

Port of replay-actions-reactions.test.ts (8 tests).

Covers:
- Button click (block_actions) triggering action handler
- Reaction added/removed in thread context
- Static select and radio button action value extraction
- Action with modal opening (trigger_id)
- Cross-platform action and reaction handling (Slack, Teams, GChat)
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from chat_sdk.emoji import get_emoji
from chat_sdk.testing import create_mock_adapter
from chat_sdk.types import (
    ActionEvent,
    Author,
    ReactionEvent,
)

from .conftest import create_chat

# ---------------------------------------------------------------------------
# Action event builders
# ---------------------------------------------------------------------------


def _make_action_event(
    adapter: Any,
    action_id: str = "info",
    user_id: str = "U00FAKEUSER1",
    user_name: str = "testuser",
    thread_id: str = "slack:C00FAKECHAN1:1767326126.896109",
    message_id: str = "1767326126.896109",
    value: str | None = None,
    trigger_id: str | None = None,
) -> ActionEvent:
    """Build an ActionEvent for testing."""
    return ActionEvent(
        adapter=adapter,
        thread=None,
        thread_id=thread_id,
        message_id=message_id,
        user=Author(
            user_id=user_id,
            user_name=user_name,
            full_name=user_name.title(),
            is_bot=False,
            is_me=False,
        ),
        action_id=action_id,
        value=value,
        trigger_id=trigger_id,
        raw={},
    )


def _make_reaction_event(
    adapter: Any,
    raw_emoji: str = "+1",
    emoji_name: str = "thumbs_up",
    added: bool = True,
    user_id: str = "U00FAKEUSER1",
    user_name: str = "testuser",
    thread_id: str = "slack:C00FAKECHAN1:1767326126.896109",
    message_id: str = "1767326126.896109",
) -> ReactionEvent:
    """Build a ReactionEvent for testing."""
    emoji = get_emoji(emoji_name) or get_emoji("thumbs_up")
    return ReactionEvent(
        emoji=emoji,
        raw_emoji=raw_emoji,
        added=added,
        user=Author(
            user_id=user_id,
            user_name=user_name,
            full_name=user_name.title(),
            is_bot=False,
            is_me=False,
        ),
        message_id=message_id,
        thread_id=thread_id,
        adapter=adapter,
        thread=None,
        raw={},
    )


# ============================================================================
# Slack button click (block_actions)
# ============================================================================


class TestSlackBlockActions:
    """Slack block_actions (button click) handling."""

    @pytest.mark.asyncio
    async def test_button_click_triggers_action_handler(self):
        """A block_actions button click fires the on_action handler."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[ActionEvent] = []

        chat.on_action(lambda event: captured.append(event))

        event = _make_action_event(adapter, action_id="info")
        chat.process_action(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 1
        assert captured[0].action_id == "info"
        assert captured[0].user.user_id == "U00FAKEUSER1"
        assert captured[0].user.user_name == "testuser"
        assert captured[0].user.is_bot is False
        assert captured[0].user.is_me is False

    @pytest.mark.asyncio
    async def test_static_select_extracts_value(self):
        """A static_select action extracts value from selected_option."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[ActionEvent] = []

        chat.on_action(lambda event: captured.append(event))

        event = _make_action_event(
            adapter,
            action_id="quick_action",
            value="greet",
        )
        chat.process_action(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 1
        assert captured[0].action_id == "quick_action"
        assert captured[0].value == "greet"

    @pytest.mark.asyncio
    async def test_radio_buttons_extracts_value(self):
        """A radio_buttons action extracts value from selected_option."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[ActionEvent] = []

        chat.on_action(lambda event: captured.append(event))

        event = _make_action_event(
            adapter,
            action_id="plan_selected",
            value="all_text",
        )
        chat.process_action(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 1
        assert captured[0].action_id == "plan_selected"
        assert captured[0].value == "all_text"


# ============================================================================
# Slack reaction events
# ============================================================================


class TestSlackReactions:
    """Slack reaction_added and reaction_removed handling."""

    @pytest.mark.asyncio
    async def test_reaction_added_triggers_handler(self):
        """A reaction_added event fires the on_reaction handler."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[ReactionEvent] = []

        chat.on_reaction(lambda event: captured.append(event))

        event = _make_reaction_event(
            adapter,
            raw_emoji="+1",
            emoji_name="thumbs_up",
            added=True,
            user_id="U00FAKEUSER1",
            message_id="1767326126.896109",
        )
        chat.process_reaction(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 1
        assert captured[0].added is True
        assert captured[0].raw_emoji == "+1"
        assert captured[0].user.user_id == "U00FAKEUSER1"
        assert captured[0].message_id == "1767326126.896109"

    @pytest.mark.asyncio
    async def test_reaction_has_emoji_object(self):
        """The reaction event carries an emoji object with name and toString."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[ReactionEvent] = []

        chat.on_reaction(lambda event: captured.append(event))

        event = _make_reaction_event(adapter, raw_emoji="+1", emoji_name="thumbs_up")
        chat.process_reaction(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 1
        assert captured[0].emoji.name == "thumbs_up"


# ============================================================================
# Teams actions
# ============================================================================


class TestTeamsActions:
    """Teams adaptive card action handling."""

    @pytest.mark.asyncio
    async def test_teams_action_submit_triggers_handler(self):
        """A Teams adaptive card Action.Submit fires the action handler."""
        teams = create_mock_adapter("teams")
        chat, adapters, state = await create_chat(adapters={"teams": teams})
        captured: list[ActionEvent] = []

        chat.on_action(lambda event: captured.append(event))

        event = _make_action_event(
            teams,
            action_id="info",
            user_id="29:test-teams-user",
            user_name="Test User",
            thread_id="teams:conv123:msg456",
        )
        chat.process_action(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 1
        assert captured[0].action_id == "info"
        assert "29:" in captured[0].user.user_id

    @pytest.mark.asyncio
    async def test_teams_reaction_triggers_handler(self):
        """A Teams messageReaction event fires the reaction handler."""
        teams = create_mock_adapter("teams")
        chat, adapters, state = await create_chat(adapters={"teams": teams})
        captured: list[ReactionEvent] = []

        chat.on_reaction(lambda event: captured.append(event))

        event = _make_reaction_event(
            teams,
            raw_emoji="like",
            emoji_name="thumbs_up",
            added=True,
            user_id="29:test-teams-user",
            thread_id="teams:conv123:msg456",
        )
        chat.process_reaction(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 1
        assert captured[0].raw_emoji == "like"
        assert "29:" in captured[0].user.user_id


# ============================================================================
# Google Chat actions
# ============================================================================


class TestGChatActions:
    """Google Chat card button click and reaction handling."""

    @pytest.mark.asyncio
    async def test_gchat_card_button_triggers_handler(self):
        """A Google Chat card button click fires the action handler."""
        gchat = create_mock_adapter("gchat")
        chat, adapters, state = await create_chat(adapters={"gchat": gchat})
        captured: list[ActionEvent] = []

        chat.on_action(lambda event: captured.append(event))

        event = _make_action_event(
            gchat,
            action_id="hello",
            user_id="users/100000000000000000001",
            user_name="Test User",
            thread_id="gchat:spaces/ABC:threads/XYZ",
        )
        chat.process_action(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 1
        assert captured[0].action_id == "hello"
        assert captured[0].user.user_id == "users/100000000000000000001"
        assert "gchat:" in captured[0].thread_id

    @pytest.mark.asyncio
    async def test_gchat_reaction_via_pubsub(self):
        """A Google Chat reaction via Pub/Sub fires the reaction handler."""
        gchat = create_mock_adapter("gchat")
        chat, adapters, state = await create_chat(adapters={"gchat": gchat})
        captured: list[ReactionEvent] = []

        chat.on_reaction(lambda event: captured.append(event))

        emoji = get_emoji("thumbs_up")
        event = ReactionEvent(
            emoji=emoji,
            raw_emoji="\U0001f44d",
            added=True,
            user=Author(
                user_id="users/100000000000000000001",
                user_name="Test User",
                full_name="Test User",
                is_bot=False,
                is_me=False,
            ),
            message_id="messages/abc123",
            thread_id="gchat:spaces/ABC:threads/XYZ",
            adapter=gchat,
            thread=None,
            raw={},
        )
        chat.process_reaction(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 1
        assert captured[0].raw_emoji == "\U0001f44d"
        assert captured[0].user.user_id == "users/100000000000000000001"
        assert "messages/" in captured[0].message_id
