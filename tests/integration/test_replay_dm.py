"""Replay integration test: DM webhooks (Slack im, WhatsApp-style, Telegram-style).

Constructs realistic DM webhook payloads, creates a Chat instance with a
MockAdapter, processes the message, and verifies the DM handler fires correctly.
"""

from __future__ import annotations

from typing import Any

import pytest
from chat_sdk.testing import create_mock_adapter
from chat_sdk.types import Message

from .conftest import create_chat, create_msg

# ---------------------------------------------------------------------------
# Realistic Slack DM (im) payload
# ---------------------------------------------------------------------------

SLACK_DM_PAYLOAD: dict[str, Any] = {
    "token": "XXYYZZ",
    "team_id": "T00FAKETEAM",
    "api_app_id": "A00FAKEAPP1",
    "event": {
        "type": "message",
        "channel": "D00FAKEDM01",
        "user": "U00FAKEUSER1",
        "text": "Hey bot, can you help me privately?",
        "ts": "1710000100.000100",
        "channel_type": "im",
        "event_ts": "1710000100.000100",
    },
    "type": "event_callback",
    "event_id": "Ev00FAKEDM01",
    "event_time": 1710000100,
}

# ---------------------------------------------------------------------------
# Realistic WhatsApp-style DM payload (simplified)
# ---------------------------------------------------------------------------

WHATSAPP_DM_PAYLOAD: dict[str, Any] = {
    "object": "whatsapp_business_account",
    "entry": [
        {
            "id": "WHATSAPP_BUSINESS_ACCOUNT_ID",
            "changes": [
                {
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {
                            "display_phone_number": "15550001234",
                            "phone_number_id": "PHONE_NUMBER_ID",
                        },
                        "contacts": [
                            {
                                "profile": {"name": "John Doe"},
                                "wa_id": "15550009999",
                            }
                        ],
                        "messages": [
                            {
                                "from": "15550009999",
                                "id": "wamid.FAKEMESSAGEID001",
                                "timestamp": "1710000200",
                                "text": {"body": "Hello from WhatsApp"},
                                "type": "text",
                            }
                        ],
                    },
                    "field": "messages",
                }
            ],
        }
    ],
}

# ---------------------------------------------------------------------------
# Realistic Telegram-style DM payload (simplified)
# ---------------------------------------------------------------------------

TELEGRAM_DM_PAYLOAD: dict[str, Any] = {
    "update_id": 100000001,
    "message": {
        "message_id": 42,
        "from": {
            "id": 123456789,
            "is_bot": False,
            "first_name": "Jane",
            "last_name": "Smith",
            "username": "janesmith",
        },
        "chat": {
            "id": 123456789,
            "first_name": "Jane",
            "last_name": "Smith",
            "username": "janesmith",
            "type": "private",
        },
        "date": 1710000300,
        "text": "Hello from Telegram",
    },
}


class TestReplayDMSlack:
    """Replay a Slack DM (im) webhook and verify the handler fires correctly."""

    @pytest.mark.asyncio
    async def test_slack_dm_triggers_direct_message_handler(self):
        """A Slack DM should route to on_direct_message."""
        adapter = create_mock_adapter("slack")
        chat, adapters, state = await create_chat(adapters={"slack": adapter})
        captured: list[tuple[Any, Message]] = []

        @chat.on_direct_message
        async def handler(thread, message, channel=None, context=None):
            captured.append((thread, message))

        event = SLACK_DM_PAYLOAD["event"]
        # DM thread IDs contain :D prefix in mock adapter
        thread_id = f"slack:D{event['channel']}:{event['ts']}"
        msg = create_msg(
            event["text"],
            msg_id=event["ts"],
            thread_id=thread_id,
            user_id=event["user"],
            user_name="test.user",
        )

        await chat.handle_incoming_message(adapter, thread_id, msg)

        assert len(captured) == 1
        thread, received = captured[0]
        assert received.text == "Hey bot, can you help me privately?"
        assert received.author.user_id == "U00FAKEUSER1"
        assert thread.id == thread_id
        assert thread.is_dm is True

    @pytest.mark.asyncio
    async def test_slack_dm_handler_can_reply(self):
        """Handler can post a reply in the DM thread."""
        adapter = create_mock_adapter("slack")
        chat, adapters, state = await create_chat(adapters={"slack": adapter})

        @chat.on_direct_message
        async def handler(thread, message, channel=None, context=None):
            await thread.post(f"Got your DM: {message.text}")

        event = SLACK_DM_PAYLOAD["event"]
        thread_id = f"slack:D{event['channel']}:{event['ts']}"
        msg = create_msg(
            event["text"],
            msg_id=event["ts"],
            thread_id=thread_id,
            user_id=event["user"],
            user_name="test.user",
        )

        await chat.handle_incoming_message(adapter, thread_id, msg)

        assert len(adapter._post_calls) == 1
        assert "Got your DM:" in str(adapter._post_calls[0][1])


class TestReplayDMWhatsApp:
    """Replay a WhatsApp-style DM webhook (routed via MockAdapter)."""

    @pytest.mark.asyncio
    async def test_whatsapp_dm_triggers_direct_message_handler(self):
        """WhatsApp DM payload parsed and routed to on_direct_message."""
        adapter = create_mock_adapter("whatsapp")
        chat, adapters, state = await create_chat(adapters={"whatsapp": adapter})
        captured: list[tuple[Any, Message]] = []

        @chat.on_direct_message
        async def handler(thread, message, channel=None, context=None):
            captured.append((thread, message))

        # Simulate parsing the WhatsApp payload into our standard format
        wa_msg = WHATSAPP_DM_PAYLOAD["entry"][0]["changes"][0]["value"]["messages"][0]
        wa_contact = WHATSAPP_DM_PAYLOAD["entry"][0]["changes"][0]["value"]["contacts"][0]
        thread_id = f"whatsapp:D{wa_msg['from']}:"
        msg = create_msg(
            wa_msg["text"]["body"],
            msg_id=wa_msg["id"],
            thread_id=thread_id,
            user_id=wa_msg["from"],
            user_name=wa_contact["profile"]["name"],
        )

        await chat.handle_incoming_message(adapter, thread_id, msg)

        assert len(captured) == 1
        thread, received = captured[0]
        assert received.text == "Hello from WhatsApp"
        assert received.author.user_id == "15550009999"
        assert thread.is_dm is True

    @pytest.mark.asyncio
    async def test_whatsapp_dm_author_fields(self):
        """Verify WhatsApp author fields are correctly propagated."""
        adapter = create_mock_adapter("whatsapp")
        chat, adapters, state = await create_chat(adapters={"whatsapp": adapter})
        captured_msgs: list[Message] = []

        @chat.on_direct_message
        async def handler(thread, message, channel=None, context=None):
            captured_msgs.append(message)

        wa_msg = WHATSAPP_DM_PAYLOAD["entry"][0]["changes"][0]["value"]["messages"][0]
        wa_contact = WHATSAPP_DM_PAYLOAD["entry"][0]["changes"][0]["value"]["contacts"][0]
        thread_id = f"whatsapp:D{wa_msg['from']}:"
        msg = create_msg(
            wa_msg["text"]["body"],
            msg_id=wa_msg["id"],
            thread_id=thread_id,
            user_id=wa_msg["from"],
            user_name=wa_contact["profile"]["name"],
        )

        await chat.handle_incoming_message(adapter, thread_id, msg)

        assert len(captured_msgs) == 1
        author = captured_msgs[0].author
        assert author.user_id == "15550009999"
        assert author.user_name == "John Doe"
        assert author.is_bot is False


class TestReplayDMTelegram:
    """Replay a Telegram-style DM webhook (routed via MockAdapter)."""

    @pytest.mark.asyncio
    async def test_telegram_dm_triggers_direct_message_handler(self):
        """Telegram DM payload parsed and routed to on_direct_message."""
        adapter = create_mock_adapter("telegram")
        chat, adapters, state = await create_chat(adapters={"telegram": adapter})
        captured: list[tuple[Any, Message]] = []

        @chat.on_direct_message
        async def handler(thread, message, channel=None, context=None):
            captured.append((thread, message))

        tg_msg = TELEGRAM_DM_PAYLOAD["message"]
        thread_id = f"telegram:D{tg_msg['chat']['id']}:"
        msg = create_msg(
            tg_msg["text"],
            msg_id=str(tg_msg["message_id"]),
            thread_id=thread_id,
            user_id=str(tg_msg["from"]["id"]),
            user_name=tg_msg["from"]["username"],
        )

        await chat.handle_incoming_message(adapter, thread_id, msg)

        assert len(captured) == 1
        thread, received = captured[0]
        assert received.text == "Hello from Telegram"
        assert received.author.user_id == "123456789"
        assert received.author.user_name == "janesmith"
        assert thread.is_dm is True

    @pytest.mark.asyncio
    async def test_telegram_dm_handler_can_reply(self):
        """Handler can post a reply in the Telegram DM thread."""
        adapter = create_mock_adapter("telegram")
        chat, adapters, state = await create_chat(adapters={"telegram": adapter})

        @chat.on_direct_message
        async def handler(thread, message, channel=None, context=None):
            await thread.post("Received your message!")

        tg_msg = TELEGRAM_DM_PAYLOAD["message"]
        thread_id = f"telegram:D{tg_msg['chat']['id']}:"
        msg = create_msg(
            tg_msg["text"],
            msg_id=str(tg_msg["message_id"]),
            thread_id=thread_id,
            user_id=str(tg_msg["from"]["id"]),
            user_name=tg_msg["from"]["username"],
        )

        await chat.handle_incoming_message(adapter, thread_id, msg)

        assert len(adapter._post_calls) == 1
        assert adapter._post_calls[0][1] == "Received your message!"


class TestReplayDMMultiPlatform:
    """Test DM handling across multiple adapters simultaneously."""

    @pytest.mark.asyncio
    async def test_dm_from_different_platforms_route_independently(self):
        """DMs from different platforms each invoke the handler independently."""
        slack_adapter = create_mock_adapter("slack")
        telegram_adapter = create_mock_adapter("telegram")
        chat, adapters, state = await create_chat(
            adapters={"slack": slack_adapter, "telegram": telegram_adapter},
        )
        captured: list[tuple[str, Message]] = []

        @chat.on_direct_message
        async def handler(thread, message, channel=None, context=None):
            captured.append((thread.id, message))

        # Slack DM
        slack_tid = "slack:DD00FAKEDM01:1710000100.000100"
        slack_msg = create_msg(
            "Slack DM",
            msg_id="slack-dm-1",
            thread_id=slack_tid,
            user_id="U00FAKEUSER1",
            user_name="slack.user",
        )
        await chat.handle_incoming_message(slack_adapter, slack_tid, slack_msg)

        # Telegram DM
        tg_tid = "telegram:D123456789:"
        tg_msg = create_msg(
            "Telegram DM",
            msg_id="tg-dm-1",
            thread_id=tg_tid,
            user_id="123456789",
            user_name="janesmith",
        )
        await chat.handle_incoming_message(telegram_adapter, tg_tid, tg_msg)

        assert len(captured) == 2
        assert captured[0][0].startswith("slack:")
        assert captured[0][1].text == "Slack DM"
        assert captured[1][0].startswith("telegram:")
        assert captured[1][1].text == "Telegram DM"
