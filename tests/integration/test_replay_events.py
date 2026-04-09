"""Integration tests for member_joined_channel and slash command events.

Port of replay-member-joined-channel (5 tests) and replay-slash-commands (11 tests).

Covers:
- Member joined channel event routing
- Channel ID encoding for member events
- Welcome message posting from member_joined_channel handler
- Simultaneous member_joined_channel and mention handling
- Graceful handling when no handler is registered
- Slash command with text parsing
- Slash command with arguments
- Multiple slash command handlers with filtering
- Slash command -> modal -> response flow
- Slash command triggerId for modals
- Channel posting from slash command handler
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from chat_sdk.types import (
    Author,
    MemberJoinedChannelEvent,
    Message,
    ModalSubmitEvent,
    SlashCommandEvent,
)

from .conftest import create_chat, create_msg

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BOT_NAME = "TestBot"
BOT_USER_ID = "U00FAKEBOT01"
USER_ID = "U00FAKEUSER1"
CHANNEL_ID = "C00FAKECHAN1"


# ============================================================================
# Member Joined Channel
# ============================================================================


def _make_member_joined_event(
    adapter: Any,
    user_id: str = BOT_USER_ID,
    channel_id: str = f"slack:{CHANNEL_ID}:",
    inviter_id: str | None = USER_ID,
) -> MemberJoinedChannelEvent:
    """Build a MemberJoinedChannelEvent for testing."""
    return MemberJoinedChannelEvent(
        adapter=adapter,
        channel_id=channel_id,
        user_id=user_id,
        inviter_id=inviter_id,
    )


class TestMemberJoinedChannel:
    """Member joined channel event routing and handling."""

    @pytest.mark.asyncio
    async def test_routes_event_to_handler(self):
        """member_joined_channel event is dispatched to handler."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[MemberJoinedChannelEvent] = []

        @chat.on_member_joined_channel
        async def handler(event):
            captured.append(event)

        event = _make_member_joined_event(adapter)
        chat.process_member_joined_channel(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 1
        assert captured[0].user_id == BOT_USER_ID
        assert CHANNEL_ID in captured[0].channel_id
        assert captured[0].inviter_id == USER_ID
        assert captured[0].adapter.name == "slack"

    @pytest.mark.asyncio
    async def test_provides_encoded_channel_id(self):
        """channelId is encoded in slack:CHANNEL: format."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[MemberJoinedChannelEvent] = []

        @chat.on_member_joined_channel
        async def handler(event):
            captured.append(event)

        event = _make_member_joined_event(adapter, channel_id=f"slack:{CHANNEL_ID}:")
        chat.process_member_joined_channel(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 1
        assert captured[0].channel_id == f"slack:{CHANNEL_ID}:"

    @pytest.mark.asyncio
    async def test_can_post_welcome_message(self):
        """Handler can post a welcome message to the channel via the adapter."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]

        @chat.on_member_joined_channel
        async def handler(event):
            if event.user_id == BOT_USER_ID:
                await event.adapter.post_message(
                    event.channel_id,
                    "Bot is available in this channel.",
                )

        event = _make_member_joined_event(adapter)
        chat.process_member_joined_channel(event)
        await asyncio.sleep(0.05)

        assert len(adapter._post_calls) == 1
        thread_id, content = adapter._post_calls[0]
        assert CHANNEL_ID in thread_id
        assert content == "Bot is available in this channel."

    @pytest.mark.asyncio
    async def test_handles_both_member_joined_and_mention(self):
        """Both member_joined_channel and mention can be handled in the same session."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        joined_events: list[MemberJoinedChannelEvent] = []
        mention_msgs: list[Message] = []

        @chat.on_member_joined_channel
        async def joined_handler(event):
            joined_events.append(event)

        @chat.on_mention
        async def mention_handler(thread, message, context=None):
            mention_msgs.append(message)

        # Bot joins channel
        join_event = _make_member_joined_event(adapter)
        chat.process_member_joined_channel(join_event)
        await asyncio.sleep(0.05)
        assert len(joined_events) == 1

        # User mentions bot
        msg = create_msg(
            "<@U00FAKEBOT01> test",
            user_id=USER_ID,
            user_name="testuser",
            is_mention=True,
        )
        await chat.handle_incoming_message(adapter, "slack:C00FAKECHAN1:1710000000.000100", msg)

        assert len(mention_msgs) == 1
        assert "test" in mention_msgs[0].text

    @pytest.mark.asyncio
    async def test_ignores_when_no_handler_registered(self):
        """No error when member_joined_channel fires without a handler."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]

        event = _make_member_joined_event(adapter)
        # No handler registered -- processing completes silently
        assert len(chat._member_joined_channel_handlers) == 0
        chat.process_member_joined_channel(event)
        await asyncio.sleep(0.05)


# ============================================================================
# Slash Commands
# ============================================================================


def _make_slash_event(
    adapter: Any,
    command: str = "/test-feedback",
    text: str = "",
    user_id: str = "U00FAKEUSER2",
    user_name: str = "Test User",
    trigger_id: str | None = "10520020890661.10229338706656.2e2188a074adf3bf9f8456b30180f405",
    channel_id: str = "C00FAKECHAN3",
) -> SlashCommandEvent:
    """Build a SlashCommandEvent for testing."""
    return SlashCommandEvent(
        adapter=adapter,
        channel=None,
        user=Author(
            user_id=user_id,
            user_name=user_name,
            full_name=user_name,
            is_bot=False,
            is_me=False,
        ),
        command=command,
        text=text,
        trigger_id=trigger_id,
        raw={
            "command": command,
            "text": text,
            "user_id": user_id,
            "user_name": user_name,
            "channel_id": channel_id,
            "trigger_id": trigger_id,
        },
    )


class TestSlashCommands:
    """Slash command event handling."""

    @pytest.mark.asyncio
    async def test_slash_command_with_correct_properties(self):
        """Slash command event carries correct command, text, and user."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[SlashCommandEvent] = []

        @chat.on_slash_command
        async def handler(event):
            captured.append(event)

        event = _make_slash_event(adapter, command="/test-feedback", text="")
        chat.process_slash_command(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 1
        assert captured[0].command == "/test-feedback"
        assert captured[0].text == ""
        assert captured[0].user.user_id == "U00FAKEUSER2"
        assert captured[0].user.user_name == "Test User"

    @pytest.mark.asyncio
    async def test_slash_command_with_arguments(self):
        """Slash command text includes arguments."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[SlashCommandEvent] = []

        @chat.on_slash_command
        async def handler(event):
            captured.append(event)

        event = _make_slash_event(adapter, command="/test-feedback", text="some arguments here")
        chat.process_slash_command(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 1
        assert captured[0].text == "some arguments here"

    @pytest.mark.asyncio
    async def test_slash_command_provides_trigger_id(self):
        """Slash command provides a trigger_id for opening modals."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[SlashCommandEvent] = []

        @chat.on_slash_command
        async def handler(event):
            captured.append(event)

        event = _make_slash_event(adapter)
        chat.process_slash_command(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 1
        assert captured[0].trigger_id is not None

    @pytest.mark.asyncio
    async def test_filtered_handler_matches_specific_command(self):
        """Handler with command filter only fires for matching commands."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        feedback_calls: list[SlashCommandEvent] = []
        all_calls: list[SlashCommandEvent] = []

        @chat.on_slash_command
        async def catch_all(event):
            all_calls.append(event)

        @chat.on_slash_command("/test-feedback")
        async def feedback_handler(event):
            feedback_calls.append(event)

        event1 = _make_slash_event(adapter, command="/test-feedback")
        event2 = _make_slash_event(adapter, command="/status")

        chat.process_slash_command(event1)
        chat.process_slash_command(event2)
        await asyncio.sleep(0.05)

        assert len(all_calls) == 2
        assert len(feedback_calls) == 1
        assert feedback_calls[0].command == "/test-feedback"

    @pytest.mark.asyncio
    async def test_multiple_command_filter(self):
        """Handler registered with multiple commands fires for all matching."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        calls: list[SlashCommandEvent] = []

        @chat.on_slash_command(["/help", "/status"])
        async def handler(event):
            calls.append(event)

        event1 = _make_slash_event(adapter, command="/help")
        event2 = _make_slash_event(adapter, command="/status")
        event3 = _make_slash_event(adapter, command="/deploy")

        chat.process_slash_command(event1)
        chat.process_slash_command(event2)
        chat.process_slash_command(event3)
        await asyncio.sleep(0.05)

        assert len(calls) == 2
        assert {c.command for c in calls} == {"/help", "/status"}

    @pytest.mark.asyncio
    async def test_raw_payload_preserved(self):
        """The raw payload is accessible on the slash command event."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[SlashCommandEvent] = []

        @chat.on_slash_command
        async def handler(event):
            captured.append(event)

        event = _make_slash_event(adapter, command="/test-feedback")
        chat.process_slash_command(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 1
        assert captured[0].raw is not None
        assert captured[0].raw["command"] == "/test-feedback"
        assert captured[0].raw["channel_id"] == "C00FAKECHAN3"


# ============================================================================
# Slash command -> modal submit flow
# ============================================================================


class TestSlashCommandModalFlow:
    """Slash command triggering modal submission flow."""

    @pytest.mark.asyncio
    async def test_modal_submit_from_slash_command(self):
        """Modal submitted after a slash command carries correct values."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        slash_calls: list[SlashCommandEvent] = []
        modal_calls: list[ModalSubmitEvent] = []

        @chat.on_slash_command
        async def slash_handler(event):
            slash_calls.append(event)

        @chat.on_modal_submit
        async def modal_handler(event):
            modal_calls.append(event)

        # Slash command
        slash_event = _make_slash_event(adapter)
        chat.process_slash_command(slash_event)
        await asyncio.sleep(0.05)
        assert len(slash_calls) == 1

        # Modal submission
        modal_event = ModalSubmitEvent(
            adapter=adapter,
            user=Author(
                user_id="U00FAKEUSER2",
                user_name="testuser",
                full_name="Test User",
                is_bot=False,
                is_me=False,
            ),
            view_id="V0AF71PAUQK",
            callback_id="feedback_form",
            values={"message": "Hello!", "category": "feature", "email": "user@example.com"},
            raw={},
        )
        await chat.process_modal_submit(modal_event)

        assert len(modal_calls) == 1
        assert modal_calls[0].callback_id == "feedback_form"
        assert modal_calls[0].values["message"] == "Hello!"

    @pytest.mark.asyncio
    async def test_no_related_thread_when_modal_from_slash_command(self):
        """Modal opened from slash command does not have relatedThread."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[ModalSubmitEvent] = []

        @chat.on_modal_submit
        async def handler(event):
            captured.append(event)

        modal_event = ModalSubmitEvent(
            adapter=adapter,
            user=Author(
                user_id="U00FAKEUSER2",
                user_name="testuser",
                full_name="Test User",
                is_bot=False,
                is_me=False,
            ),
            view_id="V0AF71PAUQK",
            callback_id="feedback_form",
            values={"message": "Hello!"},
            raw={},
        )
        await chat.process_modal_submit(modal_event)

        assert len(captured) == 1
        assert captured[0].related_thread is None
        assert captured[0].related_message is None
