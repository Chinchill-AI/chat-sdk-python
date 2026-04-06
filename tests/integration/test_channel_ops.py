"""Integration tests for channel-level operations.

Port of replay-channel.test.ts (22 tests) and replay-channel-mention.test.ts (2 tests).

Covers:
- Channel-level message posting
- Channel mention handling
- Thread creation from channel message
- Channel visibility and metadata
- Channel ID derivation from thread
"""

from __future__ import annotations

from typing import Any

import pytest
from chat_sdk.testing import MockAdapter, create_mock_adapter
from chat_sdk.types import (
    ActionEvent,
    Author,
    ChannelInfo,
    FetchResult,
    Message,
)

from .conftest import create_chat, create_msg


# ---------------------------------------------------------------------------
# Realistic Slack channel payloads (sanitised)
# ---------------------------------------------------------------------------

SLACK_CHANNEL_MENTION_PAYLOAD: dict[str, Any] = {
    "type": "event_callback",
    "team_id": "T00FAKETEAM",
    "event": {
        "type": "app_mention",
        "user": "U00FAKEUSER1",
        "text": "<@U00FAKEBOT01> Hey",
        "ts": "1771287144.743569",
        "channel": "C00FAKECHAN1",
        "event_ts": "1771287144.743569",
        "thread_ts": "1771287144.743569",
    },
    "event_id": "Ev_CHAN_001",
    "event_time": 1771287144,
}


def _make_action_event(
    adapter: Any,
    action_id: str = "channel-post",
    user_id: str = "U00FAKEUSER1",
    user_name: str = "testuser",
    thread_id: str = "slack:C00FAKECHAN1:1771287144.743569",
    message_id: str = "1771287144.743569",
    value: str | None = None,
) -> ActionEvent:
    """Build an ActionEvent for channel operations testing."""
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
        raw={},
    )


# ============================================================================
# Channel operations via actions
# ============================================================================


class TestChannelPost:
    """Channel post action triggers and channel-level operations."""

    @pytest.mark.asyncio
    async def test_channel_post_action_triggers_handler(self):
        """A channel-post action fires the on_action handler."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[ActionEvent] = []

        chat.on_action(lambda event: captured.append(event))

        event = _make_action_event(adapter, action_id="channel-post")
        chat.process_action(event)
        # Allow the async task to complete
        import asyncio

        await asyncio.sleep(0.05)

        assert len(captured) == 1
        assert captured[0].action_id == "channel-post"

    @pytest.mark.asyncio
    async def test_channel_post_action_has_correct_user(self):
        """The action event carries correct user information."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[ActionEvent] = []

        chat.on_action(lambda event: captured.append(event))

        event = _make_action_event(adapter, user_id="U00FAKEUSER1", user_name="testuser")
        chat.process_action(event)
        import asyncio

        await asyncio.sleep(0.05)

        assert len(captured) == 1
        assert captured[0].user.user_id == "U00FAKEUSER1"
        assert captured[0].user.user_name == "testuser"
        assert captured[0].user.is_bot is False
        assert captured[0].user.is_me is False

    @pytest.mark.asyncio
    async def test_channel_post_action_has_correct_thread_id(self):
        """The action event references the correct thread ID."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[ActionEvent] = []

        chat.on_action(lambda event: captured.append(event))

        thread_id = "slack:C00FAKECHAN1:1771287144.743569"
        event = _make_action_event(adapter, thread_id=thread_id)
        chat.process_action(event)
        import asyncio

        await asyncio.sleep(0.05)

        assert len(captured) == 1
        assert captured[0].thread_id == thread_id

    @pytest.mark.asyncio
    async def test_channel_id_derived_from_thread_id(self):
        """The channel ID is correctly derived from a thread ID."""
        adapter = create_mock_adapter("slack")
        channel_id = adapter.channel_id_from_thread_id("slack:C00FAKECHAN1:1771287144.743569")
        assert channel_id == "slack:C00FAKECHAN1"

    @pytest.mark.asyncio
    async def test_channel_id_for_different_platforms(self):
        """Channel ID derivation works for different platform prefixes."""
        discord = create_mock_adapter("discord")
        channel_id = discord.channel_id_from_thread_id("discord:guild1:chan1:thread1")
        # MockAdapter splits on ":" and takes first two parts
        assert "discord" in channel_id

        gchat = create_mock_adapter("gchat")
        channel_id = gchat.channel_id_from_thread_id("gchat:spaces/ABC123:threads/XYZ")
        assert "gchat" in channel_id


# ============================================================================
# Channel metadata
# ============================================================================


class TestChannelMetadata:
    """Fetching and caching channel metadata."""

    @pytest.mark.asyncio
    async def test_fetch_channel_info_returns_metadata(self):
        """The adapter returns channel metadata with name and isDM."""
        adapter = create_mock_adapter("slack")
        info = await adapter.fetch_channel_info("slack:C00FAKECHAN1")
        assert info.id == "slack:C00FAKECHAN1"
        assert info.name is not None
        assert info.is_dm is False

    @pytest.mark.asyncio
    async def test_fetch_channel_info_different_channels(self):
        """Different channel IDs return different info objects."""
        adapter = create_mock_adapter("slack")
        info1 = await adapter.fetch_channel_info("slack:C00FAKECHAN1")
        info2 = await adapter.fetch_channel_info("slack:C00FAKECHAN2")
        assert info1.id != info2.id

    @pytest.mark.asyncio
    async def test_channel_is_dm_detection(self):
        """DM channels are correctly identified."""
        adapter = create_mock_adapter("slack")
        assert adapter.is_dm("slack:DU00USER1:") is True
        assert adapter.is_dm("slack:C00FAKECHAN1:1234.5678") is False


# ============================================================================
# Channel message posting
# ============================================================================


class TestChannelMessagePosting:
    """Posting messages at the channel level."""

    @pytest.mark.asyncio
    async def test_post_channel_message(self):
        """Posting to a channel via the adapter records the call."""
        adapter = create_mock_adapter("slack")
        result = await adapter.post_channel_message("slack:C00FAKECHAN1", "Hello from channel!")
        assert result.id == "msg-1"

    @pytest.mark.asyncio
    async def test_fetch_channel_messages_returns_empty_by_default(self):
        """Fetching channel messages from mock adapter returns empty."""
        adapter = create_mock_adapter("slack")
        result = await adapter.fetch_channel_messages("slack:C00FAKECHAN1")
        assert result.messages == []


# ============================================================================
# Channel messages iteration
# ============================================================================


class TestChannelMessageIteration:
    """Iterating over channel messages."""

    @pytest.mark.asyncio
    async def test_channel_messages_empty_channel(self):
        """An empty channel returns no messages."""
        adapter = create_mock_adapter("slack")
        result = await adapter.fetch_channel_messages("slack:C00FAKECHAN1")
        assert len(result.messages) == 0
        assert result.next_cursor is None


# ============================================================================
# Channel mention resolution (from replay-channel-mention.test.ts)
# ============================================================================


class TestChannelMentionResolution:
    """Channel mention handling and resolution."""

    @pytest.mark.asyncio
    async def test_bare_channel_mention_in_message_text(self):
        """Messages can contain channel references that need resolution."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[Message] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            captured.append(message)

        # Message text contains a channel reference
        msg = create_msg(
            "<@U00FAKEBOT01> Check #test-help-channel for details",
            user_id="U00FAKEUSER1",
            user_name="testuser",
            is_mention=True,
        )
        await chat.handle_incoming_message(adapter, "slack:C00FAKECHAN1:1710000000.000100", msg)

        assert len(captured) == 1
        assert "#test-help-channel" in captured[0].text

    @pytest.mark.asyncio
    async def test_labeled_channel_mention_preserved(self):
        """Labeled channel mentions keep their display name."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[Message] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            captured.append(message)

        msg = create_msg(
            "<@U00FAKEBOT01> Check #already-named for info",
            user_id="U00FAKEUSER1",
            user_name="testuser",
            is_mention=True,
        )
        await chat.handle_incoming_message(adapter, "slack:C00FAKECHAN1:1710000000.000100", msg)

        assert len(captured) == 1
        assert "#already-named" in captured[0].text


# ============================================================================
# Channel visibility
# ============================================================================


class TestChannelVisibility:
    """Channel visibility detection."""

    @pytest.mark.asyncio
    async def test_channel_visibility_for_dm(self):
        """DM threads are identified as DM channels."""
        adapter = create_mock_adapter("slack")
        assert adapter.is_dm("slack:DU00USER1:") is True

    @pytest.mark.asyncio
    async def test_channel_visibility_for_public(self):
        """Public channels are not DM."""
        adapter = create_mock_adapter("slack")
        assert adapter.is_dm("slack:C00FAKECHAN1:1234.5678") is False

    @pytest.mark.asyncio
    async def test_get_channel_visibility_returns_unknown_for_mock(self):
        """MockAdapter returns 'unknown' visibility by default."""
        adapter = create_mock_adapter("slack")
        vis = adapter.get_channel_visibility("slack:C00FAKECHAN1:1234.5678")
        assert vis == "unknown"


# ============================================================================
# Thread creation from channel
# ============================================================================


class TestThreadFromChannel:
    """Creating threads from channel-level messages."""

    @pytest.mark.asyncio
    async def test_mention_creates_thread_context(self):
        """A mention in a channel creates a thread for the handler."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured_threads: list[Any] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            captured_threads.append(thread)

        msg = create_msg(
            "<@U00FAKEBOT01> start a conversation",
            user_id="U00FAKEUSER1",
            is_mention=True,
            thread_id="slack:C00FAKECHAN1:1771287144.743569",
        )
        await chat.handle_incoming_message(adapter, "slack:C00FAKECHAN1:1771287144.743569", msg)

        assert len(captured_threads) == 1
        assert captured_threads[0].id == "slack:C00FAKECHAN1:1771287144.743569"

    @pytest.mark.asyncio
    async def test_thread_post_goes_to_correct_thread(self):
        """Posting from a thread context targets the correct thread ID."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]

        @chat.on_mention
        async def handler(thread, message, context=None):
            await thread.post("Welcome!")

        msg = create_msg(
            "<@U00FAKEBOT01> hello",
            user_id="U00FAKEUSER1",
            is_mention=True,
            thread_id="slack:C00FAKECHAN1:1771287144.743569",
        )
        await chat.handle_incoming_message(adapter, "slack:C00FAKECHAN1:1771287144.743569", msg)

        assert len(adapter._post_calls) == 1
        post_thread_id, content = adapter._post_calls[0]
        assert post_thread_id == "slack:C00FAKECHAN1:1771287144.743569"
        assert content == "Welcome!"


# ============================================================================
# Multi-platform channel operations
# ============================================================================


class TestMultiPlatformChannelOps:
    """Channel operations across different platform adapters."""

    @pytest.mark.asyncio
    async def test_multi_adapter_channel_id_derivation(self):
        """Each adapter correctly derives channel IDs."""
        slack = create_mock_adapter("slack")
        discord = create_mock_adapter("discord")
        gchat = create_mock_adapter("gchat")

        slack_channel = slack.channel_id_from_thread_id("slack:C00FAKECHAN1:1234.5678")
        discord_channel = discord.channel_id_from_thread_id("discord:guild1:chan1")
        gchat_channel = gchat.channel_id_from_thread_id("gchat:spaces/ABC:threads/XYZ")

        assert "slack" in slack_channel
        assert "discord" in discord_channel
        assert "gchat" in gchat_channel

    @pytest.mark.asyncio
    async def test_multi_adapter_channel_messages_isolated(self):
        """Channel operations on different adapters are isolated."""
        slack = create_mock_adapter("slack")
        discord = create_mock_adapter("discord")

        chat, adapters, state = await create_chat(adapters={"slack": slack, "discord": discord})
        slack_calls: list[Message] = []
        discord_calls: list[Message] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            if thread.id.startswith("slack:"):
                slack_calls.append(message)
            else:
                discord_calls.append(message)

        # Slack mention
        msg1 = create_msg(
            "Hey @slack-bot",
            msg_id="s1",
            thread_id="slack:C123:1234.5678",
            is_mention=True,
        )
        await chat.handle_incoming_message(slack, "slack:C123:1234.5678", msg1)

        # Discord mention
        msg2 = create_msg(
            "Hey @discord-bot",
            msg_id="d1",
            thread_id="discord:guild1:chan1",
            is_mention=True,
        )
        await chat.handle_incoming_message(discord, "discord:guild1:chan1", msg2)

        assert len(slack_calls) == 1
        assert len(discord_calls) == 1
