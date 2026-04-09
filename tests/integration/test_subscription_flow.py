"""Integration tests for thread subscription flows.

Verifies that subscribing to a thread routes subsequent messages to
on_subscribed_message, unsubscribing restores normal routing, and
mentions in subscribed threads go to on_subscribed_message.
"""

from __future__ import annotations

from typing import Any

import pytest

from chat_sdk.types import Message

from .conftest import create_chat, create_msg


class TestSubscriptionFlow:
    """End-to-end tests for thread subscription lifecycle."""

    @pytest.mark.asyncio
    async def test_subscribe_routes_subsequent_messages_to_subscribed_handler(self):
        """After subscribing, follow-up messages go to on_subscribed_message."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        mention_calls: list[Message] = []
        subscribed_calls: list[Message] = []

        @chat.on_mention
        async def mention_handler(thread, message, context=None):
            mention_calls.append(message)
            await thread.subscribe()

        @chat.on_subscribed_message
        async def subscribed_handler(thread, message, context=None):
            subscribed_calls.append(message)

        thread_id = "slack:C123:1234.5678"

        # First message: mention triggers subscribe
        msg1 = create_msg("Hey @slack-bot subscribe me", msg_id="sub-1")
        await chat.handle_incoming_message(adapter, thread_id, msg1)
        assert len(mention_calls) == 1

        # Second message: should go to subscribed handler
        msg2 = create_msg("Follow-up message", msg_id="sub-2")
        await chat.handle_incoming_message(adapter, thread_id, msg2)

        assert len(subscribed_calls) == 1
        assert subscribed_calls[0].text == "Follow-up message"
        assert len(mention_calls) == 1  # no additional mention call

    @pytest.mark.asyncio
    async def test_unsubscribe_stops_routing_to_subscribed_handler(self):
        """After unsubscribing, messages no longer go to on_subscribed_message."""
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
            if "unsub" in message.text.lower():
                await thread.unsubscribe()

        thread_id = "slack:C123:1234.5678"

        # Subscribe manually
        await state.subscribe(thread_id)

        # Message in subscribed thread
        msg1 = create_msg("Hello", msg_id="unsub-1")
        await chat.handle_incoming_message(adapter, thread_id, msg1)
        assert len(subscribed_calls) == 1

        # Message that triggers unsubscribe
        msg2 = create_msg("Please unsub", msg_id="unsub-2")
        await chat.handle_incoming_message(adapter, thread_id, msg2)
        assert len(subscribed_calls) == 2

        # After unsubscribe, a mention should go back to mention handler
        msg3 = create_msg("Hey @slack-bot are you there?", msg_id="unsub-3")
        await chat.handle_incoming_message(adapter, thread_id, msg3)

        assert len(mention_calls) == 1
        assert len(subscribed_calls) == 2  # no additional subscribed call

    @pytest.mark.asyncio
    async def test_mention_in_subscribed_thread_goes_to_subscribed_handler(self):
        """A message with @mention in a subscribed thread goes to on_subscribed_message."""
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

        thread_id = "slack:C123:1234.5678"
        await state.subscribe(thread_id)

        msg = create_msg("Hey @slack-bot help again", msg_id="ms-1")
        await chat.handle_incoming_message(adapter, thread_id, msg)

        assert len(subscribed_calls) == 1
        assert len(mention_calls) == 0

    @pytest.mark.asyncio
    async def test_subscribed_handler_can_reply(self):
        """The subscribed handler can post replies via thread.post()."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]

        @chat.on_subscribed_message
        async def handler(thread, message, context=None):
            await thread.post(f"You said: {message.text}")

        thread_id = "slack:C123:1234.5678"
        await state.subscribe(thread_id)

        msg = create_msg("I need more help", msg_id="sr-1")
        await chat.handle_incoming_message(adapter, thread_id, msg)

        assert len(adapter._post_calls) == 1
        assert "You said: I need more help" in str(adapter._post_calls[0][1])

    @pytest.mark.asyncio
    async def test_is_subscribed_property_on_thread(self):
        """The thread.is_subscribed property reflects subscription state."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        thread_refs: list[Any] = []

        @chat.on_mention
        async def mention_handler(thread, message, context=None):
            thread_refs.append(thread)
            await thread.subscribe()

        @chat.on_subscribed_message
        async def subscribed_handler(thread, message, context=None):
            thread_refs.append(thread)

        thread_id = "slack:C123:1234.5678"

        # Mention triggers subscribe
        msg1 = create_msg("Hey @slack-bot", msg_id="isp-1")
        await chat.handle_incoming_message(adapter, thread_id, msg1)

        # Follow-up in subscribed thread
        msg2 = create_msg("Follow up", msg_id="isp-2")
        await chat.handle_incoming_message(adapter, thread_id, msg2)

        assert len(thread_refs) == 2
        # The second thread should be in subscribed context
        is_sub = await thread_refs[1].is_subscribed()
        assert is_sub is True

    @pytest.mark.asyncio
    async def test_subscribe_different_threads_independently(self):
        """Subscribing to one thread does not affect another."""
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

        thread_a = "slack:C123:thread-a"
        thread_b = "slack:C123:thread-b"

        # Subscribe only thread_a
        await state.subscribe(thread_a)

        msg_a = create_msg("Message in A", msg_id="ind-a")
        await chat.handle_incoming_message(adapter, thread_a, msg_a)

        msg_b = create_msg("Hey @slack-bot in B", msg_id="ind-b")
        await chat.handle_incoming_message(adapter, thread_b, msg_b)

        assert len(subscribed_calls) == 1
        assert subscribed_calls[0].text == "Message in A"
        assert len(mention_calls) == 1
        assert mention_calls[0].text == "Hey @slack-bot in B"

    @pytest.mark.asyncio
    async def test_multiple_subscribes_are_idempotent(self):
        """Calling subscribe multiple times on the same thread is safe."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        subscribed_calls: list[Message] = []

        @chat.on_mention
        async def mention_handler(thread, message, context=None):
            await thread.subscribe()
            await thread.subscribe()  # duplicate

        @chat.on_subscribed_message
        async def subscribed_handler(thread, message, context=None):
            subscribed_calls.append(message)

        thread_id = "slack:C123:1234.5678"

        msg1 = create_msg("Hey @slack-bot", msg_id="idem-1")
        await chat.handle_incoming_message(adapter, thread_id, msg1)

        msg2 = create_msg("Follow up", msg_id="idem-2")
        await chat.handle_incoming_message(adapter, thread_id, msg2)

        assert len(subscribed_calls) == 1
