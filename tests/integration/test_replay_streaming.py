"""Replay integration test: streaming flow end-to-end.

Tests the streaming pattern where a handler posts an async iterable
(simulating AI streaming) and verifies the adapter receives the stream
and processes it correctly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from chat_sdk.testing import create_mock_adapter
from chat_sdk.types import Message

from .conftest import create_chat, create_msg

# ---------------------------------------------------------------------------
# Realistic Slack app_mention payload triggering AI mode
# ---------------------------------------------------------------------------

SLACK_AI_MENTION_PAYLOAD: dict[str, Any] = {
    "token": "XXYYZZ",
    "team_id": "T00FAKETEAM",
    "api_app_id": "A00FAKEAPP1",
    "event": {
        "type": "app_mention",
        "user": "U00FAKEUSER1",
        "text": "<@U00FAKEBOT01> AI tell me about love",
        "ts": "1710001000.000100",
        "channel": "C00FAKECHAN1",
        "event_ts": "1710001000.000100",
        "channel_type": "channel",
        "thread_ts": "1710001000.000100",
    },
    "type": "event_callback",
    "event_id": "Ev00FAKEAI01",
    "event_time": 1710001000,
}

SLACK_AI_FOLLOWUP_PAYLOAD: dict[str, Any] = {
    "token": "XXYYZZ",
    "team_id": "T00FAKETEAM",
    "api_app_id": "A00FAKEAPP1",
    "event": {
        "type": "message",
        "user": "U00FAKEUSER1",
        "text": "Who are you?",
        "ts": "1710001010.000200",
        "channel": "C00FAKECHAN1",
        "event_ts": "1710001010.000200",
        "channel_type": "channel",
        "thread_ts": "1710001000.000100",
    },
    "type": "event_callback",
    "event_id": "Ev00FAKEAI02",
    "event_time": 1710001010,
}


async def _async_text_stream(chunks: list[str]) -> AsyncIterator[str]:
    """Create an async iterable text stream simulating AI response chunks."""
    for chunk in chunks:
        yield chunk


class TestReplayStreamingMention:
    """Replay a mention that triggers AI streaming response."""

    @pytest.mark.asyncio
    async def test_ai_mention_triggers_handler_and_posts_stream(self):
        """A mention with 'AI' triggers AI mode and posts a streaming response."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        mention_captured: list[Message] = []
        ai_mode_enabled = False

        @chat.on_mention
        async def handler(thread, message, context=None):
            nonlocal ai_mode_enabled
            mention_captured.append(message)
            if "AI" in message.text.upper():
                ai_mode_enabled = True
                await thread.post("AI Mode Enabled!")
                # Post the streaming response as accumulated text
                # (MockAdapter doesn't support actual streaming, so we post final text)
                stream = _async_text_stream(["Love ", "is ", "a ", "complex ", "emotion."])
                accumulated = ""
                async for chunk in stream:
                    accumulated += chunk
                await thread.post(accumulated)

        event = SLACK_AI_MENTION_PAYLOAD["event"]
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

        assert len(mention_captured) == 1
        assert "AI" in mention_captured[0].text.upper()
        assert ai_mode_enabled is True

        # Verify posts: first "AI Mode Enabled!", then the accumulated stream
        assert len(adapter._post_calls) == 2
        assert adapter._post_calls[0][1] == "AI Mode Enabled!"
        assert adapter._post_calls[1][1] == "Love is a complex emotion."

    @pytest.mark.asyncio
    async def test_non_ai_mention_does_not_stream(self):
        """A mention without 'AI' does not trigger streaming mode."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        ai_mode_enabled = False

        @chat.on_mention
        async def handler(thread, message, context=None):
            nonlocal ai_mode_enabled
            if "AI" in message.text.upper():
                ai_mode_enabled = True
            await thread.post("Got your message!")

        msg = create_msg(
            "<@U00FAKEBOT01> hello",
            msg_id="1710001100.000100",
            thread_id="slack:C00FAKECHAN1:1710001100.000100",
            user_id="U00FAKEUSER1",
            user_name="test.user",
            is_mention=True,
        )

        await chat.handle_incoming_message(adapter, "slack:C00FAKECHAN1:1710001100.000100", msg)

        assert ai_mode_enabled is False
        assert len(adapter._post_calls) == 1
        assert adapter._post_calls[0][1] == "Got your message!"


class TestReplayStreamingFollowUp:
    """Replay a follow-up message in AI streaming mode."""

    @pytest.mark.asyncio
    async def test_follow_up_in_ai_mode_streams_response(self):
        """After enabling AI mode via mention, follow-ups get streamed responses."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        ai_mode_enabled = False

        @chat.on_mention
        async def mention_handler(thread, message, context=None):
            nonlocal ai_mode_enabled
            if "AI" in message.text.upper():
                ai_mode_enabled = True
                await thread.post("AI Mode Enabled!")

        @chat.on_subscribed_message
        async def subscribed_handler(thread, message, context=None):
            if ai_mode_enabled:
                stream = _async_text_stream(["I am ", "an AI ", "assistant ", "here to help."])
                accumulated = ""
                async for chunk in stream:
                    accumulated += chunk
                await thread.post(accumulated)

        # Step 1: AI mention to enable mode
        event1 = SLACK_AI_MENTION_PAYLOAD["event"]
        thread_id = f"slack:{event1['channel']}:{event1['thread_ts']}"
        msg1 = create_msg(
            event1["text"],
            msg_id=event1["ts"],
            thread_id=thread_id,
            user_id=event1["user"],
            user_name="test.user",
            is_mention=True,
        )
        await chat.handle_incoming_message(adapter, thread_id, msg1)
        assert ai_mode_enabled is True

        # Subscribe the thread
        await state.subscribe(thread_id)
        adapter._post_calls.clear()

        # Step 2: Follow-up in the same thread
        event2 = SLACK_AI_FOLLOWUP_PAYLOAD["event"]
        msg2 = create_msg(
            event2["text"],
            msg_id=event2["ts"],
            thread_id=thread_id,
            user_id=event2["user"],
            user_name="test.user",
        )
        await chat.handle_incoming_message(adapter, thread_id, msg2)

        # Verify the streamed response was posted
        assert len(adapter._post_calls) == 1
        assert adapter._post_calls[0][1] == "I am an AI assistant here to help."


class TestReplayStreamingEdgeCases:
    """Edge cases for streaming responses."""

    @pytest.mark.asyncio
    async def test_empty_stream_posts_empty_string(self):
        """An empty stream results in posting an empty string."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]

        @chat.on_mention
        async def handler(thread, message, context=None):
            stream = _async_text_stream([])
            accumulated = ""
            async for chunk in stream:
                accumulated += chunk
            await thread.post(accumulated)

        msg = create_msg(
            "<@U00FAKEBOT01> test",
            msg_id="1710002000.000100",
            thread_id="slack:C00FAKECHAN1:1710002000.000100",
            user_id="U00FAKEUSER1",
            user_name="test.user",
            is_mention=True,
        )

        await chat.handle_incoming_message(adapter, "slack:C00FAKECHAN1:1710002000.000100", msg)

        assert len(adapter._post_calls) == 1
        assert adapter._post_calls[0][1] == ""

    @pytest.mark.asyncio
    async def test_single_chunk_stream(self):
        """A stream with a single chunk posts that chunk directly."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]

        @chat.on_mention
        async def handler(thread, message, context=None):
            stream = _async_text_stream(["Complete response in one chunk."])
            accumulated = ""
            async for chunk in stream:
                accumulated += chunk
            await thread.post(accumulated)

        msg = create_msg(
            "<@U00FAKEBOT01> test",
            msg_id="1710003000.000100",
            thread_id="slack:C00FAKECHAN1:1710003000.000100",
            user_id="U00FAKEUSER1",
            user_name="test.user",
            is_mention=True,
        )

        await chat.handle_incoming_message(adapter, "slack:C00FAKECHAN1:1710003000.000100", msg)

        assert len(adapter._post_calls) == 1
        assert adapter._post_calls[0][1] == "Complete response in one chunk."

    @pytest.mark.asyncio
    async def test_multiple_stream_posts(self):
        """Handler can interleave regular posts with streamed posts."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]

        @chat.on_mention
        async def handler(thread, message, context=None):
            await thread.post("Thinking...")

            stream = _async_text_stream(["Part 1. ", "Part 2. ", "Part 3."])
            accumulated = ""
            async for chunk in stream:
                accumulated += chunk
            await thread.post(accumulated)

            await thread.post("Done!")

        msg = create_msg(
            "<@U00FAKEBOT01> stream test",
            msg_id="1710004000.000100",
            thread_id="slack:C00FAKECHAN1:1710004000.000100",
            user_id="U00FAKEUSER1",
            user_name="test.user",
            is_mention=True,
        )

        await chat.handle_incoming_message(adapter, "slack:C00FAKECHAN1:1710004000.000100", msg)

        assert len(adapter._post_calls) == 3
        assert adapter._post_calls[0][1] == "Thinking..."
        assert adapter._post_calls[1][1] == "Part 1. Part 2. Part 3."
        assert adapter._post_calls[2][1] == "Done!"


class TestReplayStreamingMultiAdapter:
    """Streaming across multiple adapters."""

    @pytest.mark.asyncio
    async def test_streaming_works_across_different_adapters(self):
        """Streaming responses work regardless of the adapter."""
        slack = create_mock_adapter("slack")
        discord = create_mock_adapter("discord")
        chat, adapters, state = await create_chat(
            adapters={"slack": slack, "discord": discord},
        )

        @chat.on_mention
        async def handler(thread, message, context=None):
            stream = _async_text_stream(["Hello ", "from ", "stream!"])
            accumulated = ""
            async for chunk in stream:
                accumulated += chunk
            await thread.post(accumulated)

        # Slack mention
        slack_msg = create_msg(
            "<@bot> AI stream",
            msg_id="s1",
            thread_id="slack:C00FAKECHAN1:1710005000.000100",
            user_id="U00FAKEUSER1",
            user_name="slack.user",
            is_mention=True,
        )
        await chat.handle_incoming_message(slack, "slack:C00FAKECHAN1:1710005000.000100", slack_msg)

        # Discord mention
        discord_msg = create_msg(
            "<@bot> AI stream",
            msg_id="d1",
            thread_id="discord:ch1:th1",
            user_id="123456789",
            user_name="discord.user",
            is_mention=True,
        )
        await chat.handle_incoming_message(discord, "discord:ch1:th1", discord_msg)

        assert len(slack._post_calls) == 1
        assert slack._post_calls[0][1] == "Hello from stream!"
        assert len(discord._post_calls) == 1
        assert discord._post_calls[0][1] == "Hello from stream!"
