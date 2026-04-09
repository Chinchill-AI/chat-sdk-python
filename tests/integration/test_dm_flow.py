"""Integration tests for direct message (DM) handling.

Verifies that DMs are correctly detected and routed to on_direct_message
handlers, that DMs in subscribed threads go to the subscribed handler,
and that thread.post() works in DM contexts.
"""

from __future__ import annotations

from typing import Any

import pytest

from chat_sdk.testing import create_mock_adapter
from chat_sdk.types import Message

from .conftest import create_chat, create_msg


class TestDMFlow:
    """End-to-end tests for direct message routing."""

    @pytest.mark.asyncio
    async def test_dm_received_calls_on_direct_message_handler(self):
        """A DM triggers the on_direct_message handler."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        calls: list[tuple[Any, Message]] = []

        @chat.on_direct_message
        async def handler(thread, message, channel=None, context=None):
            calls.append((thread, message))

        # DM thread IDs contain :D in the mock adapter
        dm_thread_id = "slack:DDMCHAN:"
        msg = create_msg("Hello via DM", thread_id=dm_thread_id)
        await chat.handle_incoming_message(adapter, dm_thread_id, msg)

        assert len(calls) == 1
        thread, received = calls[0]
        assert received.text == "Hello via DM"
        assert thread.id == dm_thread_id

    @pytest.mark.asyncio
    async def test_dm_reply_via_thread_post(self):
        """Handler can reply to a DM using thread.post()."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]

        @chat.on_direct_message
        async def handler(thread, message, channel=None, context=None):
            await thread.post(f"Got your DM: {message.text}")

        dm_thread_id = "slack:DDMCHAN:"
        msg = create_msg("Please help", thread_id=dm_thread_id)
        await chat.handle_incoming_message(adapter, dm_thread_id, msg)

        assert len(adapter._post_calls) == 1
        tid, content = adapter._post_calls[0]
        assert tid == dm_thread_id
        assert "Got your DM: Please help" in str(content)

    @pytest.mark.asyncio
    async def test_dm_without_handler_treated_as_mention(self):
        """DMs without on_direct_message handler fall through to on_mention."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        mention_calls: list[Message] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            mention_calls.append(message)

        dm_thread_id = "slack:DDMCHAN:"
        msg = create_msg("Hello bot", thread_id=dm_thread_id)
        await chat.handle_incoming_message(adapter, dm_thread_id, msg)

        assert len(mention_calls) == 1
        assert mention_calls[0].is_mention is True

    @pytest.mark.asyncio
    async def test_dm_in_subscribed_thread_not_routed_to_dm_handler(self):
        """DMs always go to the DM handler, even if the thread is subscribed.

        The DM handler takes priority when both DM and subscribed handlers exist.
        """
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        dm_calls: list[Message] = []
        subscribed_calls: list[Message] = []

        @chat.on_direct_message
        async def dm_handler(thread, message, channel=None, context=None):
            dm_calls.append(message)

        @chat.on_subscribed_message
        async def sub_handler(thread, message, context=None):
            subscribed_calls.append(message)

        dm_thread_id = "slack:DDMCHAN:"
        await state.subscribe(dm_thread_id)

        msg = create_msg("Follow-up DM", thread_id=dm_thread_id)
        await chat.handle_incoming_message(adapter, dm_thread_id, msg)

        # DM handler should take priority
        assert len(dm_calls) == 1
        assert len(subscribed_calls) == 0

    @pytest.mark.asyncio
    async def test_non_dm_not_routed_to_dm_handler(self):
        """Normal channel messages do not trigger on_direct_message."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        dm_calls: list[Message] = []
        mention_calls: list[Message] = []

        @chat.on_direct_message
        async def dm_handler(thread, message, channel=None, context=None):
            dm_calls.append(message)

        @chat.on_mention
        async def mention_handler(thread, message, context=None):
            mention_calls.append(message)

        channel_thread_id = "slack:C123:1234.5678"
        msg = create_msg("Hey @slack-bot", thread_id=channel_thread_id)
        await chat.handle_incoming_message(adapter, channel_thread_id, msg)

        assert len(dm_calls) == 0
        assert len(mention_calls) == 1

    @pytest.mark.asyncio
    async def test_open_dm_returns_thread(self):
        """chat.open_dm() returns a usable Thread for the DM conversation."""
        adapter = create_mock_adapter("slack")
        adapter.bot_user_id = "UBOT"
        chat, adapters, state = await create_chat(adapters={"slack": adapter})

        dm_thread = await chat.open_dm("UALICE")

        assert dm_thread is not None
        assert dm_thread.id == "slack:DUALICE:"
        # Should be able to post
        await dm_thread.post("Hello from bot!")
        assert len(adapter._post_calls) == 1
        assert adapter._post_calls[0][0] == "slack:DUALICE:"

    @pytest.mark.asyncio
    async def test_multiple_dms_to_different_users(self):
        """Multiple DM conversations can be opened and posted to independently."""
        adapter = create_mock_adapter("slack")
        adapter.bot_user_id = "UBOT"
        chat, adapters, state = await create_chat(adapters={"slack": adapter})

        dm_alice = await chat.open_dm("UALICE")
        dm_bob = await chat.open_dm("UBOB")

        await dm_alice.post("Hi Alice!")
        await dm_bob.post("Hi Bob!")

        assert len(adapter._post_calls) == 2
        assert adapter._post_calls[0][0] == "slack:DUALICE:"
        assert adapter._post_calls[1][0] == "slack:DUBOB:"

    @pytest.mark.asyncio
    async def test_self_dm_ignored(self):
        """DMs from the bot itself are filtered out."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        calls: list[Message] = []

        @chat.on_direct_message
        async def handler(thread, message, channel=None, context=None):
            calls.append(message)

        dm_thread_id = "slack:DDMCHAN:"
        msg = create_msg(
            "Bot talking to itself",
            thread_id=dm_thread_id,
            is_bot=True,
            is_me=True,
        )
        await chat.handle_incoming_message(adapter, dm_thread_id, msg)

        assert len(calls) == 0
