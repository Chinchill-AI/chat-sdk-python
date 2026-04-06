"""Integration tests for mention detection and routing.

Verifies that the Chat orchestrator correctly detects @-mentions across
platforms, routes them to on_mention handlers, provides a usable Thread
object, and filters out self-mentions.
"""

from __future__ import annotations

from typing import Any

import pytest
from chat_sdk.testing import create_mock_adapter
from chat_sdk.types import Message

from .conftest import create_chat, create_msg


class TestMentionFlow:
    """End-to-end tests for @mention handling."""

    @pytest.mark.asyncio
    async def test_bot_mentioned_in_slack_triggers_on_mention(self):
        """When a message contains @botname, the on_mention handler fires."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        calls: list[tuple[Any, Message]] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            calls.append((thread, message))

        msg = create_msg("Hey @slack-bot can you help?")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        assert len(calls) == 1
        thread, received = calls[0]
        assert received.is_mention is True
        assert thread.id == "slack:C123:1234.5678"

    @pytest.mark.asyncio
    async def test_mention_handler_receives_correct_message_fields(self):
        """Handler gets correct author, text, and is_mention=True."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        received_messages: list[Message] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            received_messages.append(message)

        msg = create_msg(
            "Hello @slack-bot",
            user_id="U999",
            user_name="alice",
        )
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        assert len(received_messages) == 1
        m = received_messages[0]
        assert m.is_mention is True
        assert m.author.user_id == "U999"
        assert m.author.user_name == "alice"
        assert "Hello @slack-bot" in m.text

    @pytest.mark.asyncio
    async def test_handler_can_reply_via_thread_post(self):
        """Handler can call thread.post() and the adapter records it."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]

        @chat.on_mention
        async def handler(thread, message, context=None):
            await thread.post("I heard you!")

        msg = create_msg("Hey @slack-bot")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        assert len(adapter._post_calls) == 1
        thread_id, content = adapter._post_calls[0]
        assert thread_id == "slack:C123:1234.5678"
        assert content == "I heard you!"

    @pytest.mark.asyncio
    async def test_self_mentions_are_ignored(self):
        """Messages from the bot itself (is_me=True) are not routed."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        calls: list[Message] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            calls.append(message)

        msg = create_msg(
            "I am the bot @slack-bot",
            is_bot=True,
            is_me=True,
        )
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        assert len(calls) == 0

    @pytest.mark.asyncio
    async def test_mention_with_bot_user_id(self):
        """Mention detection works when message contains the bot's user ID."""
        adapter = create_mock_adapter("slack")
        adapter.bot_user_id = "UBOTID"
        chat, adapters, state = await create_chat(adapters={"slack": adapter})
        calls: list[Message] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            calls.append(message)

        msg = create_msg("Hello @UBOTID please help")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        assert len(calls) == 1
        assert calls[0].is_mention is True

    @pytest.mark.asyncio
    async def test_mention_with_is_mention_preset(self):
        """When is_mention is already True on the incoming message, handler fires."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        calls: list[Message] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            calls.append(message)

        # Message doesn't contain @botname in text but has is_mention=True
        msg = create_msg("Help me please", is_mention=True)
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        assert len(calls) == 1
        assert calls[0].is_mention is True

    @pytest.mark.asyncio
    async def test_non_mention_does_not_trigger_on_mention(self):
        """A message without @botname and is_mention=False skips on_mention."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        mention_calls: list[Message] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            mention_calls.append(message)

        msg = create_msg("Just a normal message")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        assert len(mention_calls) == 0

    @pytest.mark.asyncio
    async def test_multi_adapter_mention_detection(self):
        """Mention detection uses the correct adapter's bot_user_id."""
        slack = create_mock_adapter("slack")
        slack.bot_user_id = "USLACK"
        discord = create_mock_adapter("discord")
        discord.bot_user_id = "123456789012345678"

        chat, adapters, state = await create_chat(
            adapters={"slack": slack, "discord": discord},
        )
        slack_calls: list[Message] = []
        discord_calls: list[Message] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            if thread.id.startswith("slack:"):
                slack_calls.append(message)
            else:
                discord_calls.append(message)

        # Slack mention
        slack_msg = create_msg("Hey @USLACK help", msg_id="s1")
        await chat.handle_incoming_message(slack, "slack:C123:1234.5678", slack_msg)

        # Discord mention
        discord_msg = create_msg(
            "<@123456789012345678> help",
            msg_id="d1",
            thread_id="discord:ch1:th1",
        )
        await chat.handle_incoming_message(discord, "discord:ch1:th1", discord_msg)

        assert len(slack_calls) == 1
        assert len(discord_calls) == 1

    @pytest.mark.asyncio
    async def test_mention_in_subscribed_thread_routes_to_subscribed_handler(self):
        """When a thread is subscribed, even a mention goes to on_subscribed_message."""
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

        # First: mention to subscribe
        msg1 = create_msg("Hey @slack-bot subscribe", msg_id="m1")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg1)
        assert len(mention_calls) == 1

        # Manually subscribe the thread
        await state.subscribe("slack:C123:1234.5678")

        # Second: another mention in the same thread -- should go to subscribed
        msg2 = create_msg("Hey @slack-bot again", msg_id="m2")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg2)

        assert len(mention_calls) == 1  # no additional mention call
        assert len(subscribed_calls) == 1
