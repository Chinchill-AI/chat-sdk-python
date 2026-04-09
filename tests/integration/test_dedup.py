"""Integration tests for message deduplication.

Verifies that the Chat orchestrator correctly deduplicates messages using
atomic set_if_not_exists, and that messages with different IDs are both
processed.
"""

from __future__ import annotations

import asyncio

import pytest

from chat_sdk.testing import create_mock_adapter

from .conftest import create_chat, create_msg


class TestDeduplication:
    """End-to-end tests for message deduplication."""

    @pytest.mark.asyncio
    async def test_same_message_id_processed_only_once(self):
        """Sending the same message ID twice results in only one handler call."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        calls: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            calls.append(message.id)

        msg1 = create_msg("Hey @slack-bot", msg_id="dup-1")
        msg2 = create_msg("Hey @slack-bot", msg_id="dup-1")

        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg1)
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg2)

        assert len(calls) == 1
        assert calls[0] == "dup-1"

    @pytest.mark.asyncio
    async def test_different_message_ids_both_processed(self):
        """Messages with different IDs are both processed."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        calls: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            calls.append(message.id)

        msg1 = create_msg("Hey @slack-bot first", msg_id="uniq-1")
        msg2 = create_msg("Hey @slack-bot second", msg_id="uniq-2")

        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg1)
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg2)

        assert len(calls) == 2
        assert "uniq-1" in calls
        assert "uniq-2" in calls

    @pytest.mark.asyncio
    async def test_dedup_key_stored_in_state(self):
        """Dedup key is stored in the state adapter cache."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]

        @chat.on_mention
        async def handler(thread, message, context=None):
            pass

        msg = create_msg("Hey @slack-bot", msg_id="key-check")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        # The dedup key follows the format dedupe:{adapter}:{message_id}
        val = await state.get("dedupe:slack:key-check")
        assert val is True

    @pytest.mark.asyncio
    async def test_concurrent_duplicates_handled_atomically(self):
        """When two copies arrive concurrently, only one is processed."""
        chat, adapters, state = await create_chat(concurrency="concurrent")
        adapter = adapters["slack"]
        calls: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            calls.append(message.id)

        msg1 = create_msg("Hey @slack-bot", msg_id="atomic-1")
        msg2 = create_msg("Hey @slack-bot", msg_id="atomic-1")

        await asyncio.gather(
            chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg1),
            chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg2),
            return_exceptions=True,
        )

        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_dedup_across_different_threads_same_message_id(self):
        """A message ID is deduped globally, not per-thread."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        calls: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            calls.append(message.id)

        msg1 = create_msg(
            "Hey @slack-bot",
            msg_id="cross-thread",
            thread_id="slack:C1:t1",
        )
        msg2 = create_msg(
            "Hey @slack-bot",
            msg_id="cross-thread",
            thread_id="slack:C1:t2",
        )

        await chat.handle_incoming_message(adapter, "slack:C1:t1", msg1)
        await chat.handle_incoming_message(adapter, "slack:C1:t2", msg2)

        # Same message ID means deduped, even across threads
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_dedup_scoped_per_adapter(self):
        """Dedup is scoped per adapter: same message ID on different adapters both process."""
        slack = create_mock_adapter("slack")
        discord = create_mock_adapter("discord")
        chat, adapters, state = await create_chat(
            adapters={"slack": slack, "discord": discord},
        )
        calls: list[tuple[str, str]] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            calls.append((thread.id.split(":")[0], message.id))

        # Same message ID on two different adapters
        msg_slack = create_msg("Hey @slack-bot", msg_id="shared-id")
        msg_discord = create_msg(
            "Hey @discord-bot",
            msg_id="shared-id",
            thread_id="discord:ch1:th1",
        )

        await chat.handle_incoming_message(slack, "slack:C123:1234.5678", msg_slack)
        await chat.handle_incoming_message(discord, "discord:ch1:th1", msg_discord)

        assert len(calls) == 2
        adapters_seen = {c[0] for c in calls}
        assert "slack" in adapters_seen
        assert "discord" in adapters_seen
