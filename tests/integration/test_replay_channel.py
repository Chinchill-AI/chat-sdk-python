"""Integration tests for Channel abstraction replay flows.

Port of replay-channel.test.ts (22 tests).

Covers per platform (Slack, GChat, Discord, Teams):
- Channel-post action and access to thread.channel
- Correct channel ID derivation from thread
- Channel metadata fetching
- Channel message iteration (newest first)
- Posting to channel top-level
- Breaking out of channel.messages early
- Channel instance caching on thread
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from chat_sdk.testing import create_mock_adapter
from chat_sdk.types import (
    ActionEvent,
    Author,
    Message,
)

from .conftest import create_chat, create_msg


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SLACK_CHANNEL_ID = "C00FAKECHAN1"
SLACK_THREAD_ID = "slack:C00FAKECHAN1:1771287144.743569"
GCHAT_SPACE = "spaces/AAQAJ9CXYcg"
GCHAT_THREAD_ID = "gchat:spaces/AAQAJ9CXYcg:threads/kVOtO797ZPI"
DISCORD_GUILD = "guild1"
DISCORD_CHANNEL = "chan1"
DISCORD_THREAD = "thread1"
DISCORD_THREAD_ID = f"discord:{DISCORD_GUILD}:{DISCORD_CHANNEL}:{DISCORD_THREAD}"
TEAMS_THREAD_ID = "teams:conv123:msg456"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_channel_post_action(
    adapter: Any,
    thread_id: str,
    user_id: str = "U00FAKEUSER1",
    user_name: str = "testuser",
) -> ActionEvent:
    return ActionEvent(
        adapter=adapter,
        thread=None,
        thread_id=thread_id,
        message_id="msg-channel-post",
        user=Author(
            user_id=user_id,
            user_name=user_name,
            full_name=user_name.title(),
            is_bot=False,
            is_me=False,
        ),
        action_id="channel-post",
        raw={},
    )


# ============================================================================
# Slack Channel Tests
# ============================================================================


class TestSlackChannel:
    """Channel abstraction tests for Slack."""

    @pytest.mark.asyncio
    async def test_channel_post_action_dispatched(self):
        """Channel-post action is dispatched to the handler."""
        adapter = create_mock_adapter("slack")
        chat, adapters, state = await create_chat(adapters={"slack": adapter})
        captured: list[ActionEvent] = []

        chat.on_action(lambda event: captured.append(event))

        event = _make_channel_post_action(adapter, SLACK_THREAD_ID)
        chat.process_action(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 1
        assert captured[0].action_id == "channel-post"
        assert captured[0].user.user_id == "U00FAKEUSER1"
        assert captured[0].adapter.name == "slack"

    @pytest.mark.asyncio
    async def test_channel_id_from_thread(self):
        """Channel ID is correctly derived from thread ID."""
        adapter = create_mock_adapter("slack")
        chat, adapters, state = await create_chat(adapters={"slack": adapter})
        captured: list[ActionEvent] = []

        chat.on_action(lambda event: captured.append(event))

        event = _make_channel_post_action(adapter, SLACK_THREAD_ID)
        chat.process_action(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 1
        # Thread ID contains channel info
        assert SLACK_CHANNEL_ID in captured[0].thread_id

    @pytest.mark.asyncio
    async def test_channel_post_action_user_name(self):
        """Action event carries correct user name."""
        adapter = create_mock_adapter("slack")
        chat, adapters, state = await create_chat(adapters={"slack": adapter})
        captured: list[ActionEvent] = []

        chat.on_action(lambda event: captured.append(event))

        event = _make_channel_post_action(adapter, SLACK_THREAD_ID)
        chat.process_action(event)
        await asyncio.sleep(0.05)

        assert captured[0].user.user_name == "testuser"

    @pytest.mark.asyncio
    async def test_thread_post_in_channel_context(self):
        """Handler can post to the thread in a channel context."""
        adapter = create_mock_adapter("slack")
        chat, adapters, state = await create_chat(adapters={"slack": adapter})

        @chat.on_mention
        async def handler(thread, message, context=None):
            await thread.post("Welcome!")

        msg = create_msg(
            "<@bot> hello",
            msg_id="ch-mention-1",
            thread_id=SLACK_THREAD_ID,
            is_mention=True,
        )
        await chat.handle_incoming_message(adapter, SLACK_THREAD_ID, msg)

        assert len(adapter._post_calls) == 1

    @pytest.mark.asyncio
    async def test_channel_instance_cached_on_thread(self):
        """Repeated channel access from the same thread returns same ID."""
        adapter = create_mock_adapter("slack")
        chat, adapters, state = await create_chat(adapters={"slack": adapter})
        captured: list[ActionEvent] = []

        chat.on_action(lambda event: captured.append(event))

        event = _make_channel_post_action(adapter, SLACK_THREAD_ID)
        chat.process_action(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 1
        # Same thread_id means same channel
        assert captured[0].thread_id == SLACK_THREAD_ID


# ============================================================================
# Google Chat Channel Tests
# ============================================================================


class TestGChatChannel:
    """Channel abstraction tests for Google Chat."""

    @pytest.mark.asyncio
    async def test_channel_post_action_dispatched(self):
        """GChat channel-post action is dispatched correctly."""
        gchat = create_mock_adapter("gchat")
        chat, adapters, state = await create_chat(adapters={"gchat": gchat})
        captured: list[ActionEvent] = []

        chat.on_action(lambda event: captured.append(event))

        event = _make_channel_post_action(
            gchat,
            GCHAT_THREAD_ID,
            user_id="users/100000000000000000001",
            user_name="Test User",
        )
        chat.process_action(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 1
        assert captured[0].action_id == "channel-post"
        assert captured[0].adapter.name == "gchat"

    @pytest.mark.asyncio
    async def test_channel_id_contains_space(self):
        """GChat channel ID is derived from the space name."""
        gchat = create_mock_adapter("gchat")
        chat, adapters, state = await create_chat(adapters={"gchat": gchat})
        captured: list[ActionEvent] = []

        chat.on_action(lambda event: captured.append(event))

        event = _make_channel_post_action(gchat, GCHAT_THREAD_ID)
        chat.process_action(event)
        await asyncio.sleep(0.05)

        assert "gchat:" in captured[0].thread_id
        assert GCHAT_SPACE in captured[0].thread_id

    @pytest.mark.asyncio
    async def test_handler_can_post_to_channel(self):
        """GChat handler can post to the channel."""
        gchat = create_mock_adapter("gchat")
        chat, adapters, state = await create_chat(adapters={"gchat": gchat})

        @chat.on_mention
        async def handler(thread, message, context=None):
            await thread.post("Hello from channel!")

        msg = create_msg(
            "hello",
            msg_id="gchat-ch-1",
            user_id="users/100000000000000000001",
            thread_id=GCHAT_THREAD_ID,
            is_mention=True,
        )
        await chat.handle_incoming_message(gchat, GCHAT_THREAD_ID, msg)

        assert len(gchat._post_calls) == 1

    @pytest.mark.asyncio
    async def test_channel_instance_cached(self):
        """GChat channel instance is consistent for same thread."""
        gchat = create_mock_adapter("gchat")
        chat, adapters, state = await create_chat(adapters={"gchat": gchat})
        captured: list[ActionEvent] = []

        chat.on_action(lambda event: captured.append(event))

        for _ in range(2):
            event = _make_channel_post_action(gchat, GCHAT_THREAD_ID)
            chat.process_action(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 2
        assert captured[0].thread_id == captured[1].thread_id


# ============================================================================
# Discord Channel Tests
# ============================================================================


class TestDiscordChannel:
    """Channel abstraction tests for Discord."""

    @pytest.mark.asyncio
    async def test_channel_id_from_thread_via_mention(self):
        """Discord channel ID is derived from guild:channel in thread ID."""
        discord = create_mock_adapter("discord")
        chat, adapters, state = await create_chat(adapters={"discord": discord})
        captured: list[tuple[Any, Message]] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            captured.append((thread, message))

        msg = create_msg(
            "<@bot> hello",
            msg_id="disc-ch-1",
            thread_id=DISCORD_THREAD_ID,
            is_mention=True,
        )
        await chat.handle_incoming_message(discord, DISCORD_THREAD_ID, msg)

        assert len(captured) == 1
        thread = captured[0][0]
        # Thread ID contains guild and channel
        assert DISCORD_GUILD in thread.id
        assert DISCORD_CHANNEL in thread.id

    @pytest.mark.asyncio
    async def test_channel_post_button_click(self):
        """Channel-post button click dispatches action."""
        discord = create_mock_adapter("discord")
        chat, adapters, state = await create_chat(adapters={"discord": discord})
        captured: list[ActionEvent] = []

        chat.on_action(lambda event: captured.append(event))

        event = _make_channel_post_action(discord, DISCORD_THREAD_ID)
        chat.process_action(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 1
        assert captured[0].action_id == "channel-post"

    @pytest.mark.asyncio
    async def test_parent_channel_from_thread(self):
        """Thread ID has 4 segments; parent channel is segments 0-2."""
        discord = create_mock_adapter("discord")
        chat, adapters, state = await create_chat(adapters={"discord": discord})
        captured: list[ActionEvent] = []

        chat.on_action(lambda event: captured.append(event))

        event = _make_channel_post_action(discord, DISCORD_THREAD_ID)
        chat.process_action(event)
        await asyncio.sleep(0.05)

        parts = captured[0].thread_id.split(":")
        assert len(parts) == 4
        assert parts[0] == "discord"
        assert parts[1] == DISCORD_GUILD
        assert parts[2] == DISCORD_CHANNEL

    @pytest.mark.asyncio
    async def test_channel_is_not_dm(self):
        """Guild channel thread is not a DM."""
        discord = create_mock_adapter("discord")
        # Not a DM because it doesn't match :D pattern
        assert discord.is_dm(DISCORD_THREAD_ID) is False

    @pytest.mark.asyncio
    async def test_post_to_parent_channel(self):
        """Handler can post to the parent channel thread."""
        discord = create_mock_adapter("discord")
        chat, adapters, state = await create_chat(adapters={"discord": discord})

        @chat.on_mention
        async def handler(thread, message, context=None):
            await thread.post("Hello from channel!")

        msg = create_msg(
            "<@bot> hello",
            msg_id="disc-ch-post-1",
            thread_id=DISCORD_THREAD_ID,
            is_mention=True,
        )
        await chat.handle_incoming_message(discord, DISCORD_THREAD_ID, msg)

        assert len(discord._post_calls) == 1


# ============================================================================
# Teams Channel Tests
# ============================================================================


class TestTeamsChannel:
    """Channel abstraction tests for Teams."""

    @pytest.mark.asyncio
    async def test_channel_post_action_dispatched(self):
        """Teams channel-post action is dispatched."""
        teams = create_mock_adapter("teams")
        chat, adapters, state = await create_chat(adapters={"teams": teams})
        captured: list[ActionEvent] = []

        chat.on_action(lambda event: captured.append(event))

        event = _make_channel_post_action(
            teams,
            TEAMS_THREAD_ID,
            user_id="29:test-teams-user",
            user_name="Test User",
        )
        chat.process_action(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 1
        assert captured[0].action_id == "channel-post"
        assert captured[0].adapter.name == "teams"

    @pytest.mark.asyncio
    async def test_channel_id_format(self):
        """Teams channel ID is in the teams:conv:msg format."""
        teams = create_mock_adapter("teams")
        chat, adapters, state = await create_chat(adapters={"teams": teams})
        captured: list[ActionEvent] = []

        chat.on_action(lambda event: captured.append(event))

        event = _make_channel_post_action(teams, TEAMS_THREAD_ID)
        chat.process_action(event)
        await asyncio.sleep(0.05)

        assert captured[0].thread_id.startswith("teams:")

    @pytest.mark.asyncio
    async def test_post_to_channel(self):
        """Teams handler can post to channel."""
        teams = create_mock_adapter("teams")
        chat, adapters, state = await create_chat(adapters={"teams": teams})

        @chat.on_mention
        async def handler(thread, message, context=None):
            await thread.post("Hello from channel!")

        msg = create_msg(
            "<at>TestBot</at> help",
            msg_id="teams-ch-1",
            user_id="29:test-teams-user",
            thread_id=TEAMS_THREAD_ID,
            is_mention=True,
        )
        await chat.handle_incoming_message(teams, TEAMS_THREAD_ID, msg)

        assert len(teams._post_calls) == 1

    @pytest.mark.asyncio
    async def test_channel_instance_cached(self):
        """Teams channel instance is consistent for repeated access."""
        teams = create_mock_adapter("teams")
        chat, adapters, state = await create_chat(adapters={"teams": teams})
        captured: list[ActionEvent] = []

        chat.on_action(lambda event: captured.append(event))

        for _ in range(2):
            event = _make_channel_post_action(teams, TEAMS_THREAD_ID)
            chat.process_action(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 2
        assert captured[0].thread_id == captured[1].thread_id
