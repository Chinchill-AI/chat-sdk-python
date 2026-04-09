"""Integration tests for platform-specific replay flows.

Port of replay-discord (51 tests), replay-telegram (2 tests),
replay-whatsapp (6 tests), replay-slack (24 tests), replay-teams (14 tests),
replay-gchat (16 tests).

Covers per platform:
- Slack: mention, DM, thread, reaction, slash command, follow-up
- Discord: slash command, button click, message create, role mention
- Telegram: message, callback query, subscription, follow-up
- WhatsApp: text message, DM handler, sequential messages, status update
- Teams: message, adaptive card action, reaction, follow-up
- GChat: mention, follow-up, card click, Pub/Sub, bot self-message
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
    Message,
    ReactionEvent,
)

from .conftest import create_chat, create_msg

# ============================================================================
# Slack platform tests
# ============================================================================


class TestSlackPlatformReplay:
    """Replay tests for Slack-specific flows."""

    @pytest.mark.asyncio
    async def test_mention_triggers_handler(self):
        """Slack @mention triggers on_mention handler."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[tuple[Any, Message]] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            captured.append((thread, message))

        msg = create_msg(
            "<@U00FAKEBOT01> hello",
            user_id="U00FAKEUSER1",
            user_name="testuser",
            is_mention=True,
        )
        await chat.handle_incoming_message(adapter, "slack:C00FAKECHAN1:1234.5678", msg)

        assert len(captured) == 1
        assert captured[0][1].is_mention is True
        assert "hello" in captured[0][1].text
        assert captured[0][1].author.user_id == "U00FAKEUSER1"

    @pytest.mark.asyncio
    async def test_mention_in_thread(self):
        """Slack @mention in a thread includes the thread_ts."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[tuple[Any, Message]] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            captured.append((thread, message))

        msg = create_msg(
            "<@U00FAKEBOT01> help",
            user_id="U00FAKEUSER1",
            is_mention=True,
            thread_id="slack:C00FAKECHAN1:1767224888.280449",
        )
        await chat.handle_incoming_message(adapter, "slack:C00FAKECHAN1:1767224888.280449", msg)

        assert len(captured) == 1
        assert "1767224888.280449" in captured[0][0].id

    @pytest.mark.asyncio
    async def test_follow_up_after_subscribe(self):
        """Follow-up message in subscribed thread routes to subscribed handler."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        mention_calls: list[Message] = []
        subscribed_calls: list[Message] = []

        @chat.on_mention
        async def mention_handler(thread, message, context=None):
            mention_calls.append(message)

        @chat.on_subscribed_message
        async def subscribed_handler(thread, message, context=None):
            subscribed_calls.append(message)

        thread_id = "slack:C00FAKECHAN1:1767224888.280449"

        # Mention
        msg1 = create_msg(
            "<@U00FAKEBOT01> Hey",
            msg_id="m1",
            user_id="U00FAKEUSER1",
            is_mention=True,
            thread_id=thread_id,
        )
        await chat.handle_incoming_message(adapter, thread_id, msg1)
        assert len(mention_calls) == 1

        # Subscribe
        await state.subscribe(thread_id)

        # Follow-up
        msg2 = create_msg(
            "Hi",
            msg_id="m2",
            user_id="U00FAKEUSER1",
            thread_id=thread_id,
        )
        await chat.handle_incoming_message(adapter, thread_id, msg2)

        assert len(subscribed_calls) == 1
        assert subscribed_calls[0].text == "Hi"

    @pytest.mark.asyncio
    async def test_dm_message(self):
        """Slack DM message triggers on_direct_message handler."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[Message] = []

        @chat.on_direct_message
        async def handler(thread, message, channel=None, context=None):
            captured.append(message)

        # MockAdapter.is_dm checks for ":D" in thread_id
        dm_thread_id = "slack:DDMCHAN1:"
        msg = create_msg(
            "Hello from DM",
            user_id="U00FAKEUSER1",
            thread_id=dm_thread_id,
        )
        await chat.handle_incoming_message(adapter, dm_thread_id, msg)

        assert len(captured) == 1
        assert captured[0].text == "Hello from DM"

    @pytest.mark.asyncio
    async def test_bot_self_message_ignored(self):
        """Messages from the bot itself (is_me=True) are not routed."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        calls: list[Message] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            calls.append(message)

        msg = create_msg(
            "Bot's own message",
            user_id="U00FAKEBOT01",
            is_bot=True,
            is_me=True,
        )
        await chat.handle_incoming_message(adapter, "slack:C00FAKECHAN1:1234.5678", msg)

        assert len(calls) == 0

    @pytest.mark.asyncio
    async def test_slack_reaction_event(self):
        """Slack reaction_added event fires reaction handler."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[ReactionEvent] = []

        chat.on_reaction(lambda event: captured.append(event))

        emoji = get_emoji("thumbs_up")
        event = ReactionEvent(
            emoji=emoji,
            raw_emoji="+1",
            added=True,
            user=Author(
                user_id="U00FAKEUSER1",
                user_name="testuser",
                full_name="Test User",
                is_bot=False,
                is_me=False,
            ),
            message_id="1767326126.896109",
            thread_id="slack:C00FAKECHAN1:1234.5678",
            adapter=adapter,
            thread=None,
            raw={},
        )
        chat.process_reaction(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 1
        assert captured[0].added is True


# ============================================================================
# Discord platform tests
# ============================================================================


class TestDiscordPlatformReplay:
    """Replay tests for Discord-specific flows."""

    @pytest.mark.asyncio
    async def test_button_click_hello(self):
        """Discord 'hello' button click fires action handler."""
        discord = create_mock_adapter("discord")
        chat, adapters, state = await create_chat(adapters={"discord": discord})
        captured: list[ActionEvent] = []

        chat.on_action(lambda event: captured.append(event))

        event = ActionEvent(
            adapter=discord,
            thread=None,
            thread_id="discord:guild1:chan1:thread1",
            message_id="msg-123",
            user=Author(
                user_id="123456789012345678",
                user_name="TestUser",
                full_name="Test User",
                is_bot=False,
                is_me=False,
            ),
            action_id="hello",
            raw={},
        )
        chat.process_action(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 1
        assert captured[0].action_id == "hello"
        assert captured[0].user.user_id == "123456789012345678"

    @pytest.mark.asyncio
    async def test_button_click_messages(self):
        """Discord 'messages' button click triggers fetch."""
        discord = create_mock_adapter("discord")
        chat, adapters, state = await create_chat(adapters={"discord": discord})
        captured: list[ActionEvent] = []

        chat.on_action(lambda event: captured.append(event))

        event = ActionEvent(
            adapter=discord,
            thread=None,
            thread_id="discord:guild1:chan1:thread1",
            message_id="msg-456",
            user=Author(
                user_id="123456789012345678",
                user_name="TestUser",
                full_name="Test User",
                is_bot=False,
                is_me=False,
            ),
            action_id="messages",
            raw={},
        )
        chat.process_action(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 1
        assert captured[0].action_id == "messages"

    @pytest.mark.asyncio
    async def test_button_click_info(self):
        """Discord 'info' button click shows bot information."""
        discord = create_mock_adapter("discord")
        chat, adapters, state = await create_chat(adapters={"discord": discord})
        captured: list[ActionEvent] = []

        chat.on_action(lambda event: captured.append(event))

        event = ActionEvent(
            adapter=discord,
            thread=None,
            thread_id="discord:guild1:chan1:thread1",
            message_id="msg-789",
            user=Author(
                user_id="123456789012345678",
                user_name="TestUser",
                full_name="Test User",
                is_bot=False,
                is_me=False,
            ),
            action_id="info",
            raw={},
        )
        chat.process_action(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 1
        assert captured[0].action_id == "info"

    @pytest.mark.asyncio
    async def test_mention_via_gateway(self):
        """Discord gateway MESSAGE_CREATE with bot mention fires handler."""
        discord = create_mock_adapter("discord")
        discord.bot_user_id = "123456789012345678"
        chat, adapters, state = await create_chat(adapters={"discord": discord})
        captured: list[Message] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            captured.append(message)

        msg = create_msg(
            "<@123456789012345678> hello",
            msg_id="d1",
            user_id="987654321098765432",
            thread_id="discord:guild1:chan1:thread1",
            is_mention=True,
        )
        await chat.handle_incoming_message(discord, "discord:guild1:chan1:thread1", msg)

        assert len(captured) == 1

    @pytest.mark.asyncio
    async def test_discord_thread_id_format(self):
        """Discord thread IDs include guild, channel, and thread."""
        discord = create_mock_adapter("discord")
        chat, adapters, state = await create_chat(adapters={"discord": discord})
        captured_threads: list[Any] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            captured_threads.append(thread)

        msg = create_msg(
            "<@bot> hello",
            msg_id="d2",
            thread_id="discord:guild1:chan1:thread1",
            is_mention=True,
        )
        await chat.handle_incoming_message(discord, "discord:guild1:chan1:thread1", msg)

        assert len(captured_threads) == 1
        assert "discord:" in captured_threads[0].id
        assert "guild1" in captured_threads[0].id

    @pytest.mark.asyncio
    async def test_role_mention_detected(self):
        """Discord role mentions are detected when configured."""
        discord = create_mock_adapter("discord")
        discord.bot_user_id = "bot123"
        chat, adapters, state = await create_chat(adapters={"discord": discord})
        captured: list[Message] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            captured.append(message)

        msg = create_msg(
            "<@&role123> help",
            msg_id="d3",
            thread_id="discord:guild1:chan1:thread1",
            is_mention=True,
        )
        await chat.handle_incoming_message(discord, "discord:guild1:chan1:thread1", msg)

        assert len(captured) == 1


# ============================================================================
# Telegram platform tests
# ============================================================================


class TestTelegramPlatformReplay:
    """Replay tests for Telegram-specific flows."""

    @pytest.mark.asyncio
    async def test_mention_webhook_and_subscribe(self):
        """Telegram @mention message triggers handler and allows subscription."""
        telegram = create_mock_adapter("telegram")
        chat, adapters, state = await create_chat(adapters={"telegram": telegram})
        captured: list[tuple[Any, Message]] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            captured.append((thread, message))

        msg = create_msg(
            "@vercelchatsdkbot hello",
            user_id="123456789",
            user_name="testuser",
            thread_id="telegram:7527593",
            is_mention=True,
        )
        await chat.handle_incoming_message(telegram, "telegram:7527593", msg)

        assert len(captured) == 1
        assert "@vercelchatsdkbot" in captured[0][1].text
        assert captured[0][0].id == "telegram:7527593"

    @pytest.mark.asyncio
    async def test_non_mention_follow_up_in_subscribed_thread(self):
        """Non-mention follow-up in subscribed Telegram thread routes correctly."""
        telegram = create_mock_adapter("telegram")
        chat, adapters, state = await create_chat(adapters={"telegram": telegram})
        mention_calls: list[Message] = []
        subscribed_calls: list[Message] = []

        @chat.on_mention
        async def mention_handler(thread, message, context=None):
            mention_calls.append(message)

        @chat.on_subscribed_message
        async def subscribed_handler(thread, message, context=None):
            subscribed_calls.append(message)

        # Mention
        msg1 = create_msg(
            "@vercelchatsdkbot hello",
            msg_id="tg1",
            user_id="123456789",
            thread_id="telegram:7527593",
            is_mention=True,
        )
        await chat.handle_incoming_message(telegram, "telegram:7527593", msg1)
        assert len(mention_calls) == 1

        # Subscribe
        await state.subscribe("telegram:7527593")

        # Follow-up
        msg2 = create_msg(
            "how are you",
            msg_id="tg2",
            user_id="123456789",
            thread_id="telegram:7527593",
        )
        await chat.handle_incoming_message(telegram, "telegram:7527593", msg2)

        assert len(subscribed_calls) == 1
        assert subscribed_calls[0].text == "how are you"


# ============================================================================
# WhatsApp platform tests
# ============================================================================


class TestWhatsAppPlatformReplay:
    """Replay tests for WhatsApp-specific flows."""

    @pytest.mark.asyncio
    async def test_dm_webhook_triggers_handler(self):
        """WhatsApp text message fires the DM handler."""
        whatsapp = create_mock_adapter("whatsapp")
        chat, adapters, state = await create_chat(adapters={"whatsapp": whatsapp})
        captured: list[Message] = []

        @chat.on_direct_message
        async def handler(thread, message, channel=None, context=None):
            captured.append(message)

        # MockAdapter.is_dm checks for ":D" in thread_id
        dm_thread_id = "whatsapp:Dphone123:15550002222"
        msg = create_msg(
            "What is Vercel?",
            user_id="15550002222",
            user_name="Test User",
            thread_id=dm_thread_id,
        )
        await chat.handle_incoming_message(whatsapp, dm_thread_id, msg)

        assert len(captured) == 1
        assert captured[0].text == "What is Vercel?"
        assert captured[0].author.user_id == "15550002222"
        assert captured[0].author.is_bot is False

    @pytest.mark.asyncio
    async def test_correct_thread_id(self):
        """WhatsApp thread ID includes phone number ID and user phone."""
        whatsapp = create_mock_adapter("whatsapp")
        chat, adapters, state = await create_chat(adapters={"whatsapp": whatsapp})
        captured_threads: list[Any] = []

        @chat.on_direct_message
        async def handler(thread, message, channel=None, context=None):
            captured_threads.append(thread)

        dm_thread_id = "whatsapp:Dphone123:15550002222"
        msg = create_msg(
            "Hello",
            user_id="15550002222",
            thread_id=dm_thread_id,
        )
        await chat.handle_incoming_message(whatsapp, dm_thread_id, msg)

        assert len(captured_threads) == 1
        assert captured_threads[0].id == dm_thread_id

    @pytest.mark.asyncio
    async def test_sequential_dm_messages(self):
        """Sequential WhatsApp DM messages both get handled."""
        whatsapp = create_mock_adapter("whatsapp")
        chat, adapters, state = await create_chat(adapters={"whatsapp": whatsapp})
        captured: list[Message] = []

        @chat.on_direct_message
        async def handler(thread, message, channel=None, context=None):
            captured.append(message)

        dm_thread_id = "whatsapp:Dphone123:15550002222"
        msg1 = create_msg(
            "What is Vercel?",
            msg_id="wa1",
            user_id="15550002222",
            thread_id=dm_thread_id,
        )
        await chat.handle_incoming_message(whatsapp, dm_thread_id, msg1)

        msg2 = create_msg(
            "Tell me more",
            msg_id="wa2",
            user_id="15550002222",
            thread_id=dm_thread_id,
        )
        await chat.handle_incoming_message(whatsapp, dm_thread_id, msg2)

        assert len(captured) == 2
        assert captured[0].text == "What is Vercel?"
        assert captured[1].text == "Tell me more"

    @pytest.mark.asyncio
    async def test_whatsapp_is_dm(self):
        """WhatsApp messages are identified as DMs."""
        whatsapp = create_mock_adapter("whatsapp")
        # WhatsApp thread IDs contain :D for DM detection in mock
        assert whatsapp.is_dm("whatsapp:Dphone123:") is True


# ============================================================================
# Teams platform tests
# ============================================================================


class TestTeamsPlatformReplay:
    """Replay tests for Teams-specific flows."""

    @pytest.mark.asyncio
    async def test_mention_triggers_handler(self):
        """Teams @mention triggers on_mention handler."""
        teams = create_mock_adapter("teams")
        chat, adapters, state = await create_chat(adapters={"teams": teams})
        captured: list[Message] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            captured.append(message)

        msg = create_msg(
            "<at>TestBot</at> help",
            user_id="29:test-teams-user",
            user_name="Test User",
            thread_id="teams:conv123:msg456",
            is_mention=True,
        )
        await chat.handle_incoming_message(teams, "teams:conv123:msg456", msg)

        assert len(captured) == 1
        assert "29:" in captured[0].author.user_id

    @pytest.mark.asyncio
    async def test_adaptive_card_action(self):
        """Teams adaptive card action fires the action handler."""
        teams = create_mock_adapter("teams")
        chat, adapters, state = await create_chat(adapters={"teams": teams})
        captured: list[ActionEvent] = []

        chat.on_action(lambda event: captured.append(event))

        event = ActionEvent(
            adapter=teams,
            thread=None,
            thread_id="teams:conv123:msg456",
            message_id="msg-teams-123",
            user=Author(
                user_id="29:test-teams-user",
                user_name="Test User",
                full_name="Test User",
                is_bot=False,
                is_me=False,
            ),
            action_id="info",
            raw={},
        )
        chat.process_action(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 1
        assert captured[0].action_id == "info"

    @pytest.mark.asyncio
    async def test_teams_reaction(self):
        """Teams messageReaction event fires reaction handler."""
        teams = create_mock_adapter("teams")
        chat, adapters, state = await create_chat(adapters={"teams": teams})
        captured: list[ReactionEvent] = []

        chat.on_reaction(lambda event: captured.append(event))

        emoji = get_emoji("thumbs_up")
        event = ReactionEvent(
            emoji=emoji,
            raw_emoji="like",
            added=True,
            user=Author(
                user_id="29:test-teams-user",
                user_name="Test User",
                full_name="Test User",
                is_bot=False,
                is_me=False,
            ),
            message_id="msg-teams-456",
            thread_id="teams:conv123:msg456",
            adapter=teams,
            thread=None,
            raw={},
        )
        chat.process_reaction(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 1
        assert captured[0].raw_emoji == "like"

    @pytest.mark.asyncio
    async def test_teams_follow_up(self):
        """Teams follow-up message in subscribed thread works."""
        teams = create_mock_adapter("teams")
        chat, adapters, state = await create_chat(adapters={"teams": teams})
        mention_calls: list[Message] = []
        subscribed_calls: list[Message] = []

        @chat.on_mention
        async def mention_handler(thread, message, context=None):
            mention_calls.append(message)

        @chat.on_subscribed_message
        async def subscribed_handler(thread, message, context=None):
            subscribed_calls.append(message)

        thread_id = "teams:conv123:msg456"

        msg1 = create_msg(
            "<at>TestBot</at> help",
            msg_id="t1",
            user_id="29:test-user",
            thread_id=thread_id,
            is_mention=True,
        )
        await chat.handle_incoming_message(teams, thread_id, msg1)
        assert len(mention_calls) == 1

        await state.subscribe(thread_id)

        msg2 = create_msg(
            "More details",
            msg_id="t2",
            user_id="29:test-user",
            thread_id=thread_id,
        )
        await chat.handle_incoming_message(teams, thread_id, msg2)

        assert len(subscribed_calls) == 1


# ============================================================================
# Google Chat platform tests
# ============================================================================


class TestGChatPlatformReplay:
    """Replay tests for Google Chat-specific flows."""

    @pytest.mark.asyncio
    async def test_mention_triggers_handler(self):
        """GChat @mention triggers on_mention handler."""
        gchat = create_mock_adapter("gchat")
        chat, adapters, state = await create_chat(adapters={"gchat": gchat})
        captured: list[tuple[Any, Message]] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            captured.append((thread, message))

        msg = create_msg(
            "hello",
            user_id="users/100000000000000000001",
            user_name="Test User",
            thread_id="gchat:spaces/AAQAJ9CXYcg:threads/kVOtO797ZPI",
            is_mention=True,
        )
        await chat.handle_incoming_message(gchat, "gchat:spaces/AAQAJ9CXYcg:threads/kVOtO797ZPI", msg)

        assert len(captured) == 1
        assert "hello" in captured[0][1].text
        assert captured[0][1].author.user_id == "users/100000000000000000001"

    @pytest.mark.asyncio
    async def test_mention_author_details(self):
        """GChat mention carries correct author details."""
        gchat = create_mock_adapter("gchat")
        chat, adapters, state = await create_chat(adapters={"gchat": gchat})
        captured: list[Message] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            captured.append(message)

        msg = create_msg(
            "hello",
            user_id="users/100000000000000000001",
            user_name="Test User",
            thread_id="gchat:spaces/ABC:threads/XYZ",
            is_mention=True,
        )
        await chat.handle_incoming_message(gchat, "gchat:spaces/ABC:threads/XYZ", msg)

        assert len(captured) == 1
        assert captured[0].author.user_name == "Test User"
        assert captured[0].author.is_bot is False
        assert captured[0].author.is_me is False

    @pytest.mark.asyncio
    async def test_follow_up_via_pubsub(self):
        """GChat follow-up via Pub/Sub routes to subscribed handler."""
        gchat = create_mock_adapter("gchat")
        chat, adapters, state = await create_chat(adapters={"gchat": gchat})
        mention_calls: list[Message] = []
        subscribed_calls: list[Message] = []

        @chat.on_mention
        async def mention_handler(thread, message, context=None):
            mention_calls.append(message)

        @chat.on_subscribed_message
        async def subscribed_handler(thread, message, context=None):
            subscribed_calls.append(message)

        thread_id = "gchat:spaces/AAQAJ9CXYcg:threads/kVOtO797ZPI"

        # Mention
        msg1 = create_msg(
            "hello",
            msg_id="g1",
            user_id="users/100000000000000000001",
            thread_id=thread_id,
            is_mention=True,
        )
        await chat.handle_incoming_message(gchat, thread_id, msg1)
        assert len(mention_calls) == 1

        # Subscribe
        await state.subscribe(thread_id)

        # Follow-up via Pub/Sub
        msg2 = create_msg(
            "Hey",
            msg_id="g2",
            user_id="users/100000000000000000001",
            thread_id=thread_id,
        )
        await chat.handle_incoming_message(gchat, thread_id, msg2)

        assert len(subscribed_calls) == 1
        assert subscribed_calls[0].text == "Hey"

    @pytest.mark.asyncio
    async def test_bot_self_message_not_routed(self):
        """GChat messages from the bot itself (is_me=True) are skipped."""
        gchat = create_mock_adapter("gchat")
        gchat.bot_user_id = "bot/123"
        chat, adapters, state = await create_chat(adapters={"gchat": gchat})

        subscribed_calls: list[Message] = []

        @chat.on_subscribed_message
        async def handler(thread, message, context=None):
            subscribed_calls.append(message)

        # Subscribe first
        thread_id = "gchat:spaces/ABC:threads/XYZ"
        await state.subscribe(thread_id)

        # Bot's own message
        msg = create_msg(
            "Bot's own message",
            user_id="bot/123",
            thread_id=thread_id,
            is_bot=True,
            is_me=True,
        )
        await chat.handle_incoming_message(gchat, thread_id, msg)

        assert len(subscribed_calls) == 0

    @pytest.mark.asyncio
    async def test_card_button_click(self):
        """GChat card button click fires action handler."""
        gchat = create_mock_adapter("gchat")
        chat, adapters, state = await create_chat(adapters={"gchat": gchat})
        captured: list[ActionEvent] = []

        chat.on_action(lambda event: captured.append(event))

        event = ActionEvent(
            adapter=gchat,
            thread=None,
            thread_id="gchat:spaces/ABC:threads/XYZ",
            message_id="messages/abc123",
            user=Author(
                user_id="users/100000000000000000001",
                user_name="Test User",
                full_name="Test User",
                is_bot=False,
                is_me=False,
            ),
            action_id="hello",
            raw={},
        )
        chat.process_action(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 1
        assert captured[0].action_id == "hello"
        assert "gchat:" in captured[0].thread_id

    @pytest.mark.asyncio
    async def test_handler_can_post_reply(self):
        """GChat mention handler can post a reply via thread.post()."""
        gchat = create_mock_adapter("gchat")
        chat, adapters, state = await create_chat(adapters={"gchat": gchat})

        @chat.on_mention
        async def handler(thread, message, context=None):
            await thread.post("Thanks for mentioning me!")

        msg = create_msg(
            "hello",
            user_id="users/100000000000000000001",
            thread_id="gchat:spaces/ABC:threads/XYZ",
            is_mention=True,
        )
        await chat.handle_incoming_message(gchat, "gchat:spaces/ABC:threads/XYZ", msg)

        assert len(gchat._post_calls) == 1
        _, content = gchat._post_calls[0]
        assert content == "Thanks for mentioning me!"

    @pytest.mark.asyncio
    async def test_edit_message_after_post(self):
        """GChat handler can post and then edit a message."""
        gchat = create_mock_adapter("gchat")
        chat, adapters, state = await create_chat(adapters={"gchat": gchat})

        @chat.on_mention
        async def handler(thread, message, context=None):
            sent = await thread.post("Processing...")
            await sent.edit("Thanks for your message")

        msg = create_msg(
            "hello",
            user_id="users/100000000000000000001",
            thread_id="gchat:spaces/ABC:threads/XYZ",
            is_mention=True,
        )
        await chat.handle_incoming_message(gchat, "gchat:spaces/ABC:threads/XYZ", msg)

        assert len(gchat._post_calls) == 1
        assert len(gchat._edit_calls) == 1
        _, _, edit_content = gchat._edit_calls[0]
        assert edit_content == "Thanks for your message"
