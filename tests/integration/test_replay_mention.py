"""Replay integration test: Slack @mention webhook.

Constructs a realistic Slack ``app_mention`` webhook payload matching the
actual JSON structure Slack sends, creates a Chat instance with a MockAdapter,
calls handle_incoming_message (simulating the webhook path), and verifies the
on_mention handler is invoked with correct data.
"""

from __future__ import annotations

from typing import Any

import pytest

from chat_sdk.types import Message

from .conftest import create_chat, create_msg

# ---------------------------------------------------------------------------
# Realistic Slack app_mention payload (sanitised)
# ---------------------------------------------------------------------------

SLACK_MENTION_PAYLOAD: dict[str, Any] = {
    "token": "XXYYZZ",
    "team_id": "T00FAKETEAM",
    "api_app_id": "A00FAKEAPP1",
    "event": {
        "type": "app_mention",
        "user": "U00FAKEUSER1",
        "text": "<@U00FAKEBOT01> hello can you help me?",
        "ts": "1710000000.000100",
        "channel": "C00FAKECHAN1",
        "event_ts": "1710000000.000100",
        "channel_type": "channel",
        "thread_ts": "1710000000.000100",
    },
    "type": "event_callback",
    "event_id": "Ev00FAKEEV01",
    "event_time": 1710000000,
}


class TestReplayMention:
    """Replay a Slack mention webhook and verify the handler fires correctly."""

    @pytest.mark.asyncio
    async def test_slack_mention_triggers_on_mention(self):
        """Replay an app_mention event; verify handler receives correct data."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[tuple[Any, Message]] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            captured.append((thread, message))

        # Build a message from the realistic payload
        event = SLACK_MENTION_PAYLOAD["event"]
        thread_id = f"slack:{event['channel']}:{event['thread_ts']}"
        msg = create_msg(
            event["text"],
            msg_id=event["ts"],
            thread_id=thread_id,
            user_id=event["user"],
            user_name="test.user",
            is_mention=True,
        )

        await chat.handle_incoming_message(adapter, thread_id, msg)

        assert len(captured) == 1
        thread, received = captured[0]
        assert received.is_mention is True
        assert received.text == event["text"]
        assert received.author.user_id == "U00FAKEUSER1"
        assert thread.id == thread_id

    @pytest.mark.asyncio
    async def test_mention_handler_can_reply(self):
        """Handler can call thread.post() after receiving the mention."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]

        @chat.on_mention
        async def handler(thread, message, context=None):
            await thread.post("Thanks for mentioning me!")

        event = SLACK_MENTION_PAYLOAD["event"]
        thread_id = f"slack:{event['channel']}:{event['thread_ts']}"
        msg = create_msg(
            event["text"],
            msg_id=event["ts"],
            thread_id=thread_id,
            user_id=event["user"],
            user_name="test.user",
            is_mention=True,
        )

        await chat.handle_incoming_message(adapter, thread_id, msg)

        assert len(adapter._post_calls) == 1
        post_thread_id, content = adapter._post_calls[0]
        assert post_thread_id == thread_id
        assert content == "Thanks for mentioning me!"

    @pytest.mark.asyncio
    async def test_mention_message_has_correct_author(self):
        """Verify author fields from the replayed webhook."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured_msgs: list[Message] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            captured_msgs.append(message)

        event = SLACK_MENTION_PAYLOAD["event"]
        thread_id = f"slack:{event['channel']}:{event['thread_ts']}"
        msg = create_msg(
            event["text"],
            msg_id=event["ts"],
            thread_id=thread_id,
            user_id="U00FAKEUSER1",
            user_name="test.user",
            is_mention=True,
        )

        await chat.handle_incoming_message(adapter, thread_id, msg)

        assert len(captured_msgs) == 1
        author = captured_msgs[0].author
        assert author.user_id == "U00FAKEUSER1"
        assert author.user_name == "test.user"
        assert author.is_bot is False
        assert author.is_me is False

    @pytest.mark.asyncio
    async def test_bot_own_mention_is_ignored(self):
        """Messages from the bot itself (is_me=True) should not trigger on_mention."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[Message] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            captured.append(message)

        event = SLACK_MENTION_PAYLOAD["event"]
        thread_id = f"slack:{event['channel']}:{event['thread_ts']}"
        msg = create_msg(
            event["text"],
            msg_id=event["ts"],
            thread_id=thread_id,
            user_id="U00FAKEBOT01",
            user_name="testbot",
            is_bot=True,
            is_me=True,
        )

        await chat.handle_incoming_message(adapter, thread_id, msg)

        assert len(captured) == 0

    @pytest.mark.asyncio
    async def test_mention_subscribe_then_follow_up_routes_correctly(self):
        """After subscribing in mention, follow-up goes to on_subscribed_message."""
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

        event = SLACK_MENTION_PAYLOAD["event"]
        thread_id = f"slack:{event['channel']}:{event['thread_ts']}"

        # First: mention
        msg1 = create_msg(
            event["text"],
            msg_id=event["ts"],
            thread_id=thread_id,
            user_id="U00FAKEUSER1",
            user_name="test.user",
            is_mention=True,
        )
        await chat.handle_incoming_message(adapter, thread_id, msg1)
        assert len(mention_calls) == 1

        # Subscribe the thread
        await state.subscribe(thread_id)

        # Follow-up in same thread
        msg2 = create_msg(
            "Hey, follow up question",
            msg_id="1710000001.000200",
            thread_id=thread_id,
            user_id="U00FAKEUSER1",
            user_name="test.user",
        )
        await chat.handle_incoming_message(adapter, thread_id, msg2)

        assert len(subscribed_calls) == 1
        assert subscribed_calls[0].text == "Hey, follow up question"
