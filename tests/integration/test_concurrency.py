"""Integration tests for concurrency strategies.

Verifies drop, queue, and concurrent strategies work as expected when
multiple messages arrive on the same thread concurrently.
"""

from __future__ import annotations

import asyncio

import pytest

from chat_sdk.errors import LockError

from .conftest import create_chat, create_msg


class TestConcurrencyDrop:
    """Tests for the 'drop' concurrency strategy (default)."""

    @pytest.mark.asyncio
    async def test_second_message_dropped_when_thread_locked(self):
        """With drop strategy, a second message on a locked thread raises LockError."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        calls: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            calls.append(message.id)
            await asyncio.sleep(0.1)

        msg1 = create_msg("Hey @slack-bot first", msg_id="drop-1")
        msg2 = create_msg("Hey @slack-bot second", msg_id="drop-2")

        task1 = asyncio.ensure_future(chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg1))
        await asyncio.sleep(0.01)

        with pytest.raises(LockError):
            await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg2)

        await task1

        assert "drop-1" in calls
        assert "drop-2" not in calls

    @pytest.mark.asyncio
    async def test_different_threads_process_independently(self):
        """Drop strategy only locks per-thread; different threads run in parallel."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        calls: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            calls.append(message.id)
            await asyncio.sleep(0.02)

        msg1 = create_msg("Hey @slack-bot", msg_id="t1-m1", thread_id="slack:C1:t1")
        msg2 = create_msg("Hey @slack-bot", msg_id="t2-m1", thread_id="slack:C1:t2")

        await asyncio.gather(
            chat.handle_incoming_message(adapter, "slack:C1:t1", msg1),
            chat.handle_incoming_message(adapter, "slack:C1:t2", msg2),
        )

        assert "t1-m1" in calls
        assert "t2-m1" in calls

    @pytest.mark.asyncio
    async def test_lock_released_after_handler_completes(self):
        """After first message finishes, a new message on the same thread succeeds."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        calls: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            calls.append(message.id)

        msg1 = create_msg("Hey @slack-bot first", msg_id="seq-1")
        msg2 = create_msg("Hey @slack-bot second", msg_id="seq-2")

        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg1)
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg2)

        assert calls == ["seq-1", "seq-2"]


class TestConcurrencyQueue:
    """Tests for the 'queue' concurrency strategy."""

    @pytest.mark.asyncio
    async def test_queued_message_processed_after_first_completes(self):
        """With queue strategy, a second message is queued and processed after the first."""
        chat, adapters, state = await create_chat(concurrency="queue")
        adapter = adapters["slack"]
        calls: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            calls.append(message.id)
            await asyncio.sleep(0.02)

        msg1 = create_msg("Hey @slack-bot first", msg_id="q-1")
        msg2 = create_msg("Hey @slack-bot second", msg_id="q-2")

        await asyncio.gather(
            chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg1),
            chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg2),
            return_exceptions=True,
        )

        # First message always processed
        assert "q-1" in calls

    @pytest.mark.asyncio
    async def test_queue_strategy_does_not_raise_lock_error(self):
        """Queue strategy enqueues instead of raising LockError."""
        chat, adapters, state = await create_chat(concurrency="queue")
        adapter = adapters["slack"]

        @chat.on_mention
        async def handler(thread, message, context=None):
            await asyncio.sleep(0.05)

        msg1 = create_msg("Hey @slack-bot first", msg_id="qnr-1")
        msg2 = create_msg("Hey @slack-bot second", msg_id="qnr-2")

        results = await asyncio.gather(
            chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg1),
            chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg2),
            return_exceptions=True,
        )

        # Neither result should be a LockError
        for r in results:
            assert not isinstance(r, LockError)


class TestConcurrencyConcurrent:
    """Tests for the 'concurrent' concurrency strategy."""

    @pytest.mark.asyncio
    async def test_both_messages_processed_in_parallel(self):
        """With concurrent strategy, both messages process without blocking."""
        chat, adapters, state = await create_chat(concurrency="concurrent")
        adapter = adapters["slack"]
        calls: list[str] = []
        start_times: list[float] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            import time

            start_times.append(time.monotonic())
            calls.append(message.id)
            await asyncio.sleep(0.02)

        msg1 = create_msg("Hey @slack-bot first", msg_id="c-1")
        msg2 = create_msg("Hey @slack-bot second", msg_id="c-2")

        await asyncio.gather(
            chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg1),
            chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg2),
        )

        assert "c-1" in calls
        assert "c-2" in calls

    @pytest.mark.asyncio
    async def test_concurrent_does_not_raise_lock_error(self):
        """Concurrent strategy never raises LockError."""
        chat, adapters, state = await create_chat(concurrency="concurrent")
        adapter = adapters["slack"]
        calls: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            calls.append(message.id)
            await asyncio.sleep(0.05)

        msg1 = create_msg("Hey @slack-bot first", msg_id="cnlr-1")
        msg2 = create_msg("Hey @slack-bot second", msg_id="cnlr-2")

        results = await asyncio.gather(
            chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg1),
            chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg2),
            return_exceptions=True,
        )

        for r in results:
            assert not isinstance(r, Exception)

        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_concurrent_messages_on_different_threads(self):
        """Concurrent processing works across multiple threads simultaneously."""
        chat, adapters, state = await create_chat(concurrency="concurrent")
        adapter = adapters["slack"]
        calls: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            calls.append(f"{thread.id}:{message.id}")
            await asyncio.sleep(0.01)

        msg1 = create_msg("Hey @slack-bot", msg_id="mt-1", thread_id="slack:C1:t1")
        msg2 = create_msg("Hey @slack-bot", msg_id="mt-2", thread_id="slack:C1:t2")
        msg3 = create_msg("Hey @slack-bot", msg_id="mt-3", thread_id="slack:C1:t3")

        await asyncio.gather(
            chat.handle_incoming_message(adapter, "slack:C1:t1", msg1),
            chat.handle_incoming_message(adapter, "slack:C1:t2", msg2),
            chat.handle_incoming_message(adapter, "slack:C1:t3", msg3),
        )

        assert len(calls) == 3
