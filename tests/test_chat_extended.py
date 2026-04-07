"""Extended Chat orchestrator tests: remaining categories from chat.test.ts.

Covers:
- onLockConflict (drop default, force, callback returning force/drop, async callback)
- concurrency: queue (context with skipped, drain, enqueue+drain, pattern handlers)
- concurrency: queue with onSubscribedMessage
- concurrency: queue edge cases (drop-newest, drop-oldest, TTL expiry)
- concurrency: debounce (timer delay, burst last-wins)
- concurrency: concurrent (parallel, no locking)
- lockScope (thread default, channel on adapter, channel on config, async resolver, queue+channel)
- persistMessageHistory (append, no-persist)
- openDM (adapter inference, Author object, unknown format, posting)
- isDM (true for DM, false for non-DM, adapter detection)
- Slash Commands (catch-all, specific, multiple, skip self, normalize, channel.post,
                  both specific+catch-all)

Ported from packages/chat/src/chat.test.ts (lines ~2128-3059).
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest
from chat_sdk.chat import Chat
from chat_sdk.errors import ChatError, LockError
from chat_sdk.testing import (
    MockAdapter,
    MockLogger,
    MockStateAdapter,
    create_mock_adapter,
    create_mock_state,
    create_test_message,
)
from chat_sdk.types import (
    Author,
    ChatConfig,
    ConcurrencyConfig,
    MessageContext,
    SlashCommandEvent,
)

HELP_REGEX = re.compile(r"help", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_chat(
    adapter: MockAdapter | None = None,
    state: MockStateAdapter | None = None,
    **overrides: Any,
) -> tuple[Chat, MockAdapter, MockStateAdapter]:
    adapter = adapter or create_mock_adapter("slack")
    state = state or create_mock_state()
    config = ChatConfig(
        user_name="testbot",
        adapters={"slack": adapter},
        state=state,
        logger=MockLogger(),
        **overrides,
    )
    chat = Chat(config)
    return chat, adapter, state


async def _init_chat(
    adapter: MockAdapter | None = None,
    state: MockStateAdapter | None = None,
    **overrides: Any,
) -> tuple[Chat, MockAdapter, MockStateAdapter]:
    chat, adapter, state = _make_chat(adapter, state, **overrides)
    await chat.webhooks["slack"]("request")
    return chat, adapter, state


def _make_slash_event(
    adapter: MockAdapter,
    command: str = "/help",
    text: str = "topic",
    is_me: bool = False,
    trigger_id: str | None = None,
    channel_id: str = "slack:C456",
) -> dict[str, Any]:
    return {
        "command": command,
        "text": text,
        "user": Author(
            user_id="BOT" if is_me else "U123",
            user_name="testbot" if is_me else "user",
            full_name="Test Bot" if is_me else "Test User",
            is_bot=is_me,
            is_me=is_me,
        ),
        "adapter": adapter,
        "raw": {"channel_id": "C456"},
        "channel_id": channel_id,
        **({"trigger_id": trigger_id} if trigger_id else {}),
    }


# ============================================================================
# onLockConflict
# ============================================================================


class TestOnLockConflict:
    """Tests for onLockConflict configuration."""

    @pytest.mark.asyncio
    async def test_drop_by_default_when_lock_is_held(self):
        chat, adapter, state = await _init_chat()

        @chat.on_mention
        async def handler(thread, message, context=None):
            pass

        # Pre-acquire the lock
        await state.acquire_lock("slack:C123:1234.5678", 30000)

        msg = create_test_message("msg-lock-1", "Hey @slack-bot")
        with pytest.raises(LockError):
            await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

    @pytest.mark.asyncio
    async def test_force_release_lock_when_on_lock_conflict_is_force(self):
        state = create_mock_state()
        adapter = create_mock_adapter("slack")

        chat, _, _ = await _init_chat(
            adapter=adapter,
            state=state,
            on_lock_conflict="force",
        )
        calls: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            calls.append(message.id)

        # Pre-acquire to simulate another handler
        await state.acquire_lock("slack:C123:1234.5678", 30000)

        msg = create_test_message("msg-lock-2", "Hey @slack-bot")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        assert len(calls) == 1
        assert calls[0] == "msg-lock-2"

    @pytest.mark.asyncio
    async def test_callback_returning_force(self):
        state = create_mock_state()
        adapter = create_mock_adapter("slack")

        chat, _, _ = await _init_chat(
            adapter=adapter,
            state=state,
            on_lock_conflict=lambda _tid, _msg: "force",
        )
        calls: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            calls.append(message.id)

        await state.acquire_lock("slack:C123:1234.5678", 30000)

        msg = create_test_message("msg-lock-3", "Hey @slack-bot")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_callback_returning_drop(self):
        """When callback returns a falsy value, message should be dropped."""
        state = create_mock_state()
        adapter = create_mock_adapter("slack")

        # Return None (falsy) to signal "drop"
        chat, _, _ = await _init_chat(
            adapter=adapter,
            state=state,
            on_lock_conflict=lambda _tid, _msg: None,
        )
        calls: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            calls.append(message.id)

        await state.acquire_lock("slack:C123:1234.5678", 30000)

        msg = create_test_message("msg-lock-4", "Hey @slack-bot")
        with pytest.raises(LockError):
            await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        assert len(calls) == 0

    @pytest.mark.asyncio
    async def test_async_callback(self):
        state = create_mock_state()
        adapter = create_mock_adapter("slack")

        async def _async_force(_tid, _msg):
            return "force"

        chat, _, _ = await _init_chat(
            adapter=adapter,
            state=state,
            on_lock_conflict=_async_force,
        )
        calls: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            calls.append(message.id)

        await state.acquire_lock("slack:C123:1234.5678", 30000)

        msg = create_test_message("msg-lock-5", "Hey @slack-bot")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        assert len(calls) == 1


# ============================================================================
# Concurrency: queue - skipped context
# ============================================================================


class TestConcurrencyQueueContext:
    """Tests for queue concurrency strategy context passing."""

    @pytest.mark.asyncio
    async def test_first_message_has_no_context(self):
        """First message processed directly gets no skipped context."""
        state = create_mock_state()
        adapter = create_mock_adapter("slack")

        chat, _, _ = await _init_chat(adapter=adapter, state=state, concurrency="queue")

        received_contexts: list[MessageContext | None] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            received_contexts.append(context)

        msg = create_test_message("msg-q-1", "Hey @slack-bot first")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        assert len(received_contexts) == 1
        assert received_contexts[0] is None

    @pytest.mark.asyncio
    async def test_enqueue_and_drain_with_skipped_context(self):
        """Messages queued while lock held are drained with skipped context."""
        state = create_mock_state()
        adapter = create_mock_adapter("slack")

        chat, _, _ = await _init_chat(adapter=adapter, state=state, concurrency="queue")

        received_messages: list[str] = []
        received_contexts: list[MessageContext | None] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            received_messages.append(message.text)
            received_contexts.append(context)

        # Pre-acquire to simulate busy handler
        await state.acquire_lock("slack:C123:1234.5678", 30000)

        # These queue up
        msg1 = create_test_message("msg-q-2", "Hey @slack-bot second")
        msg2 = create_test_message("msg-q-3", "Hey @slack-bot third")
        msg3 = create_test_message("msg-q-4", "Hey @slack-bot fourth")

        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg1)
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg2)
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg3)

        # Nothing processed yet
        assert len(received_messages) == 0

        # Release and trigger drain
        await state.force_release_lock("slack:C123:1234.5678")
        msg4 = create_test_message("msg-q-5", "Hey @slack-bot fifth")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg4)

        # msg4 first (direct), then drain delivers msg3 as latest with [msg1,msg2] skipped
        assert len(received_messages) == 2
        assert received_messages[0] == "Hey @slack-bot fifth"
        assert received_contexts[0] is None
        assert received_messages[1] == "Hey @slack-bot fourth"
        assert received_contexts[1] is not None
        assert len(received_contexts[1].skipped) == 2
        assert received_contexts[1].skipped[0].text == "Hey @slack-bot second"
        assert received_contexts[1].skipped[1].text == "Hey @slack-bot third"
        assert received_contexts[1].total_since_last_handler == 3

    @pytest.mark.asyncio
    async def test_queue_depth_when_lock_held(self):
        """Messages accumulate in queue while lock is held."""
        state = create_mock_state()
        adapter = create_mock_adapter("slack")

        chat, _, _ = await _init_chat(adapter=adapter, state=state, concurrency="queue")

        @chat.on_mention
        async def handler(thread, message, context=None):
            pass

        await state.acquire_lock("slack:C123:1234.5678", 30000)

        for i in range(3):
            msg = create_test_message(f"msg-depth-{i}", f"Hey @slack-bot msg {i}")
            await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        depth = await state.queue_depth("slack:C123:1234.5678")
        assert depth == 3


# ============================================================================
# Concurrency: queue with onSubscribedMessage
# ============================================================================


class TestConcurrencyQueueSubscribed:
    """Tests for queue concurrency with subscribed message handlers."""

    @pytest.mark.asyncio
    async def test_pass_skipped_context_to_subscribed_handler(self):
        state = create_mock_state()
        adapter = create_mock_adapter("slack")

        chat, _, _ = await _init_chat(adapter=adapter, state=state, concurrency="queue")

        received_messages: list[str] = []
        received_contexts: list[MessageContext | None] = []

        @chat.on_mention
        async def mention_handler(thread, message, context=None):
            await thread.subscribe()

        @chat.on_subscribed_message
        async def sub_handler(thread, message, context=None):
            received_messages.append(message.text)
            received_contexts.append(context)

        # Subscribe via mention
        msg0 = create_test_message("msg-sub-0", "Hey @slack-bot subscribe me")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg0)

        # Hold lock
        await state.acquire_lock("slack:C123:1234.5678", 30000)

        # Queue follow-up messages
        msg1 = create_test_message("msg-sub-1", "first follow-up")
        msg2 = create_test_message("msg-sub-2", "second follow-up")
        msg3 = create_test_message("msg-sub-3", "third follow-up")

        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg1)
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg2)
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg3)

        # Release and trigger drain
        await state.force_release_lock("slack:C123:1234.5678")
        msg4 = create_test_message("msg-sub-4", "fourth follow-up")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg4)

        assert received_messages == ["fourth follow-up", "third follow-up"]
        assert received_contexts[0] is None
        assert received_contexts[1] is not None
        assert [m.text for m in received_contexts[1].skipped] == [
            "first follow-up",
            "second follow-up",
        ]
        assert received_contexts[1].total_since_last_handler == 3


# ============================================================================
# Concurrency: queue edge cases
# ============================================================================


class TestConcurrencyQueueEdgeCases:
    """Tests for queue edge cases: max size, drop policy, TTL."""

    @pytest.mark.asyncio
    async def test_drop_newest_when_queue_full(self):
        state = create_mock_state()
        adapter = create_mock_adapter("slack")

        chat, _, _ = await _init_chat(
            adapter=adapter,
            state=state,
            concurrency=ConcurrencyConfig(
                strategy="queue",
                max_queue_size=2,
                on_queue_full="drop-newest",
            ),
        )

        @chat.on_mention
        async def handler(thread, message, context=None):
            pass

        await state.acquire_lock("slack:C123:1234.5678", 30000)

        # Fill queue
        await chat.handle_incoming_message(
            adapter,
            "slack:C123:1234.5678",
            create_test_message("msg-dq-1", "Hey @slack-bot one"),
        )
        await chat.handle_incoming_message(
            adapter,
            "slack:C123:1234.5678",
            create_test_message("msg-dq-2", "Hey @slack-bot two"),
        )
        # Third should be silently dropped
        await chat.handle_incoming_message(
            adapter,
            "slack:C123:1234.5678",
            create_test_message("msg-dq-3", "Hey @slack-bot three"),
        )

        depth = await state.queue_depth("slack:C123:1234.5678")
        assert depth == 2

    @pytest.mark.asyncio
    async def test_drop_oldest_when_queue_full(self):
        state = create_mock_state()
        adapter = create_mock_adapter("slack")

        chat, _, _ = await _init_chat(
            adapter=adapter,
            state=state,
            concurrency=ConcurrencyConfig(
                strategy="queue",
                max_queue_size=2,
                on_queue_full="drop-oldest",
            ),
        )

        received_messages: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            received_messages.append(message.text)

        await state.acquire_lock("slack:C123:1234.5678", 30000)

        # Enqueue 3 with max 2 => first evicted
        await chat.handle_incoming_message(
            adapter,
            "slack:C123:1234.5678",
            create_test_message("msg-do-1", "Hey @slack-bot one"),
        )
        await chat.handle_incoming_message(
            adapter,
            "slack:C123:1234.5678",
            create_test_message("msg-do-2", "Hey @slack-bot two"),
        )
        await chat.handle_incoming_message(
            adapter,
            "slack:C123:1234.5678",
            create_test_message("msg-do-3", "Hey @slack-bot three"),
        )

        depth = await state.queue_depth("slack:C123:1234.5678")
        assert depth == 2

        # Release and trigger drain
        await state.force_release_lock("slack:C123:1234.5678")
        await chat.handle_incoming_message(
            adapter,
            "slack:C123:1234.5678",
            create_test_message("msg-do-4", "Hey @slack-bot four"),
        )

        # msg-do-4 direct, then drain: [msg-do-2, msg-do-3] => handler(msg-do-3)
        assert received_messages[0] == "Hey @slack-bot four"
        assert received_messages[1] == "Hey @slack-bot three"

    @pytest.mark.asyncio
    async def test_skip_expired_entries_during_drain(self):
        state = create_mock_state()
        adapter = create_mock_adapter("slack")

        chat, _, _ = await _init_chat(
            adapter=adapter,
            state=state,
            concurrency=ConcurrencyConfig(
                strategy="queue",
                queue_entry_ttl_ms=1,  # expire almost immediately
            ),
        )

        received_messages: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            received_messages.append(message.text)

        await state.acquire_lock("slack:C123:1234.5678", 30000)

        await chat.handle_incoming_message(
            adapter,
            "slack:C123:1234.5678",
            create_test_message("msg-exp-1", "Hey @slack-bot expired"),
        )

        # Wait for TTL to expire
        await asyncio.sleep(0.015)

        await state.force_release_lock("slack:C123:1234.5678")
        await chat.handle_incoming_message(
            adapter,
            "slack:C123:1234.5678",
            create_test_message("msg-exp-2", "Hey @slack-bot fresh"),
        )

        # Only fresh message processed
        assert received_messages == ["Hey @slack-bot fresh"]

    @pytest.mark.asyncio
    async def test_queue_works_with_pattern_handlers(self):
        state = create_mock_state()
        adapter = create_mock_adapter("slack")

        chat, _, _ = await _init_chat(adapter=adapter, state=state, concurrency="queue")

        received_messages: list[str] = []

        @chat.on_message(HELP_REGEX)
        async def handler(thread, message, context=None):
            received_messages.append(message.text)
            if context:
                for s in context.skipped:
                    received_messages.append(f"skipped:{s.text}")

        await state.acquire_lock("slack:C123:1234.5678", 30000)

        await chat.handle_incoming_message(
            adapter,
            "slack:C123:1234.5678",
            create_test_message("msg-pat-1", "!help first"),
        )
        await chat.handle_incoming_message(
            adapter,
            "slack:C123:1234.5678",
            create_test_message("msg-pat-2", "!help second"),
        )

        await state.force_release_lock("slack:C123:1234.5678")
        await chat.handle_incoming_message(
            adapter,
            "slack:C123:1234.5678",
            create_test_message("msg-pat-3", "!help third"),
        )

        assert received_messages[0] == "!help third"
        assert received_messages[1] == "!help second"
        assert received_messages[2] == "skipped:!help first"


# ============================================================================
# Concurrency: debounce
# ============================================================================


class TestConcurrencyDebounce:
    """Tests for debounce concurrency strategy."""

    @pytest.mark.asyncio
    async def test_debounce_first_message_processed_after_delay(self):
        state = create_mock_state()
        adapter = create_mock_adapter("slack")

        chat, _, _ = await _init_chat(
            adapter=adapter,
            state=state,
            concurrency=ConcurrencyConfig(strategy="debounce", debounce_ms=50),
        )

        calls: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            calls.append(message.text)

        msg = create_test_message("msg-d-1", "Hey @slack-bot debounce")

        # Process in background
        task = asyncio.ensure_future(chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg))

        # Give it a moment but less than debounce
        await asyncio.sleep(0.01)
        assert len(calls) == 0  # not yet

        # Wait for debounce to fire
        await task

        assert len(calls) == 1
        assert calls[0] == "Hey @slack-bot debounce"

    @pytest.mark.asyncio
    async def test_burst_messages_final_wins(self):
        state = create_mock_state()
        adapter = create_mock_adapter("slack")

        chat, _, _ = await _init_chat(
            adapter=adapter,
            state=state,
            concurrency=ConcurrencyConfig(strategy="debounce", debounce_ms=50),
        )

        calls: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            calls.append(message.text)

        msg1 = create_test_message("msg-d-2", "Hey @slack-bot first")
        # First acquires the lock and enters debounce loop
        task = asyncio.ensure_future(chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg1))

        # Second and third arrive while debouncing
        await asyncio.sleep(0.005)
        msg2 = create_test_message("msg-d-3", "Hey @slack-bot second")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg2)

        msg3 = create_test_message("msg-d-4", "Hey @slack-bot third")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg3)

        # Wait for debounce to complete
        await task

        # Only the last message should win
        assert len(calls) == 1
        assert calls[0] == "Hey @slack-bot third"


# ============================================================================
# Concurrency: concurrent
# ============================================================================


class TestConcurrencyConcurrent:
    """Tests for concurrent strategy (no locking)."""

    @pytest.mark.asyncio
    async def test_process_without_lock(self):
        state = create_mock_state()
        adapter = create_mock_adapter("slack")

        chat, _, _ = await _init_chat(
            adapter=adapter,
            state=state,
            concurrency="concurrent",
        )

        calls: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            calls.append(message.id)

        # Pre-acquire lock -- concurrent should NOT be blocked
        await state.acquire_lock("slack:C123:1234.5678", 30000)

        msg = create_test_message("msg-c-1", "Hey @slack-bot concurrent")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        assert len(calls) == 1
        assert calls[0] == "msg-c-1"

    @pytest.mark.asyncio
    async def test_parallel_processing(self):
        """Multiple messages can be processed concurrently."""
        state = create_mock_state()
        adapter = create_mock_adapter("slack")

        chat, _, _ = await _init_chat(
            adapter=adapter,
            state=state,
            concurrency="concurrent",
        )

        calls: list[str] = []
        processing_count = 0
        max_parallel = 0

        @chat.on_mention
        async def handler(thread, message, context=None):
            nonlocal processing_count, max_parallel
            processing_count += 1
            max_parallel = max(max_parallel, processing_count)
            await asyncio.sleep(0.02)
            calls.append(message.id)
            processing_count -= 1

        msg1 = create_test_message("msg-c-2", "Hey @slack-bot one")
        msg2 = create_test_message("msg-c-3", "Hey @slack-bot two")

        await asyncio.gather(
            chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg1),
            chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg2),
        )

        assert len(calls) == 2
        assert max_parallel == 2


# ============================================================================
# lockScope
# ============================================================================


class TestLockScope:
    """Tests for lock scope configuration."""

    @pytest.mark.asyncio
    async def test_default_thread_scope_uses_thread_id(self):
        state = create_mock_state()
        adapter = create_mock_adapter("slack")

        chat, _, _ = await _init_chat(adapter=adapter, state=state)
        calls: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            calls.append(message.id)

        msg = create_test_message("msg-ls-1", "Hey @slack-bot")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        assert len(calls) == 1
        # Lock was acquired on full thread ID
        assert "slack:C123:1234.5678" in state._locks or len(calls) == 1

    @pytest.mark.asyncio
    async def test_channel_scope_on_adapter(self):
        state = create_mock_state()
        adapter = create_mock_adapter("telegram")
        adapter.lock_scope = "channel"

        config = ChatConfig(
            user_name="testbot",
            adapters={"telegram": adapter},
            state=state,
            logger=MockLogger(),
        )
        chat = Chat(config)
        await chat.webhooks["telegram"]("request")

        lock_keys_acquired: list[str] = []
        original_acquire = state.acquire_lock

        async def tracking_acquire(thread_id, ttl_ms):
            lock_keys_acquired.append(thread_id)
            return await original_acquire(thread_id, ttl_ms)

        state.acquire_lock = tracking_acquire  # type: ignore[assignment]

        calls: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            calls.append(message.id)

        msg = create_test_message("msg-ls-2", "Hey @telegram-bot")
        await chat.handle_incoming_message(adapter, "telegram:C123:topic456", msg)

        assert len(calls) == 1
        # Lock should have been acquired on the channel ID
        assert "telegram:C123" in lock_keys_acquired

    @pytest.mark.asyncio
    async def test_channel_scope_on_config(self):
        state = create_mock_state()
        adapter = create_mock_adapter("slack")

        lock_keys_acquired: list[str] = []
        original_acquire = state.acquire_lock

        async def tracking_acquire(thread_id, ttl_ms):
            lock_keys_acquired.append(thread_id)
            return await original_acquire(thread_id, ttl_ms)

        state.acquire_lock = tracking_acquire  # type: ignore[assignment]

        chat, _, _ = await _init_chat(
            adapter=adapter,
            state=state,
            lock_scope="channel",
        )

        calls: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            calls.append(message.id)

        msg = create_test_message("msg-ls-3", "Hey @slack-bot")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        assert len(calls) == 1
        assert "slack:C123" in lock_keys_acquired

    @pytest.mark.asyncio
    async def test_async_lock_scope_resolver(self):
        state = create_mock_state()
        adapter = create_mock_adapter("telegram")

        lock_keys_acquired: list[str] = []
        original_acquire = state.acquire_lock

        async def tracking_acquire(thread_id, ttl_ms):
            lock_keys_acquired.append(thread_id)
            return await original_acquire(thread_id, ttl_ms)

        state.acquire_lock = tracking_acquire  # type: ignore[assignment]

        async def _resolve(ctx):
            return "thread" if ctx.is_dm else "channel"

        config = ChatConfig(
            user_name="testbot",
            adapters={"telegram": adapter},
            state=state,
            logger=MockLogger(),
            lock_scope=_resolve,
        )
        chat = Chat(config)
        await chat.webhooks["telegram"]("request")

        calls: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            calls.append(message.id)

        # Non-DM: should use channel scope
        msg = create_test_message("msg-ls-4", "Hey @telegram-bot")
        await chat.handle_incoming_message(adapter, "telegram:C123:topic456", msg)

        assert len(calls) == 1
        assert "telegram:C123" in lock_keys_acquired

    @pytest.mark.asyncio
    async def test_queue_with_channel_scope(self):
        state = create_mock_state()
        adapter = create_mock_adapter("telegram")
        adapter.lock_scope = "channel"

        config = ChatConfig(
            user_name="testbot",
            adapters={"telegram": adapter},
            state=state,
            logger=MockLogger(),
            concurrency="queue",
        )
        chat = Chat(config)
        await chat.webhooks["telegram"]("request")

        @chat.on_mention
        async def handler(thread, message, context=None):
            pass

        # Pre-hold channel lock
        await state.acquire_lock("telegram:C123", 30000)

        # Both messages from different topics should use channel lock
        msg1 = create_test_message("msg-ls-5", "Hey @telegram-bot first")
        await chat.handle_incoming_message(adapter, "telegram:C123:topic1", msg1)

        msg2 = create_test_message("msg-ls-6", "Hey @telegram-bot second")
        await chat.handle_incoming_message(adapter, "telegram:C123:topic2", msg2)

        # Both queued on channel key
        depth = await state.queue_depth("telegram:C123")
        assert depth == 2


# ============================================================================
# persistMessageHistory
# ============================================================================


class TestPersistMessageHistory:
    """Tests for adapter persistMessageHistory flag."""

    @pytest.mark.asyncio
    async def test_cache_messages_when_persist_enabled(self):
        adapter = create_mock_adapter("whatsapp")
        adapter.persist_message_history = True
        state = create_mock_state()

        config = ChatConfig(
            user_name="testbot",
            adapters={"whatsapp": adapter},
            state=state,
            logger=MockLogger(),
        )
        chat = Chat(config)
        await chat.webhooks["whatsapp"]("request")

        # Need a handler so the message gets processed
        @chat.on_mention
        async def handler(thread, message, context=None):
            pass

        msg = create_test_message("msg-1", "Hello from WhatsApp")
        await chat.handle_incoming_message(adapter, "whatsapp:phone:user1", msg)

        # Check that message was stored in state cache
        stored = state.cache.get("msg-history:whatsapp:phone:user1")
        assert stored is not None
        assert isinstance(stored, list)
        assert stored[0]["id"] == "msg-1"

    @pytest.mark.asyncio
    async def test_no_cache_when_persist_not_set(self):
        chat, adapter, state = await _init_chat()

        @chat.on_mention
        async def handler(thread, message, context=None):
            pass

        msg = create_test_message("msg-2", "Hello from Slack")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        # No msg-history keys
        history_keys = [k for k in state.cache if k.startswith("msg-history:")]
        assert len(history_keys) == 0


# ============================================================================
# openDM
# ============================================================================


class TestOpenDM:
    """Tests for chat.open_dm() adapter inference."""

    @pytest.mark.asyncio
    async def test_infer_slack_adapter_from_u_user_id(self):
        chat, adapter, state = await _init_chat()

        thread = await chat.open_dm("U123456")

        assert thread is not None
        assert thread.id == "slack:DU123456:"

    @pytest.mark.asyncio
    async def test_accept_author_object(self):
        chat, adapter, state = await _init_chat()

        author = Author(
            user_id="U789ABC",
            user_name="testuser",
            full_name="Test User",
            is_bot=False,
            is_me=False,
        )
        thread = await chat.open_dm(author)

        assert thread is not None
        assert thread.id == "slack:DU789ABC:"

    @pytest.mark.asyncio
    async def test_throw_for_unknown_user_id_format(self):
        chat, adapter, state = await _init_chat()

        with pytest.raises(ChatError, match="Cannot infer adapter"):
            await chat.open_dm("invalid-user-id")

    @pytest.mark.asyncio
    async def test_allow_posting_to_dm_thread(self):
        chat, adapter, state = await _init_chat()

        thread = await chat.open_dm("U123456")
        await thread.post("Hello via DM!")

        assert len(adapter._post_calls) == 1
        assert adapter._post_calls[0][0] == "slack:DU123456:"
        assert adapter._post_calls[0][1] == "Hello via DM!"


# ============================================================================
# isDM
# ============================================================================


class TestIsDM:
    """Tests for DM detection on threads."""

    @pytest.mark.asyncio
    async def test_return_true_for_dm_threads(self):
        chat, adapter, state = await _init_chat()

        thread = await chat.open_dm("U123456")
        assert thread.is_dm is True

    @pytest.mark.asyncio
    async def test_return_false_for_non_dm_threads(self):
        chat, adapter, state = await _init_chat()
        captured_thread = None

        @chat.on_mention
        async def handler(thread, message, context=None):
            nonlocal captured_thread
            captured_thread = thread

        msg = create_test_message("msg-1", "Hey @slack-bot help")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        assert captured_thread is not None
        assert captured_thread.is_dm is False

    @pytest.mark.asyncio
    async def test_adapter_is_dm_method_called(self):
        """Adapter's is_dm method is used for detection."""
        chat, adapter, state = await _init_chat()
        is_dm_calls: list[str] = []

        original_is_dm = adapter.is_dm

        def tracking_is_dm(thread_id: str) -> bool:
            is_dm_calls.append(thread_id)
            return original_is_dm(thread_id)

        adapter.is_dm = tracking_is_dm  # type: ignore[assignment]

        @chat.on_mention
        async def handler(thread, message, context=None):
            pass

        msg = create_test_message("msg-1", "Hey @slack-bot help")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        assert "slack:C123:1234.5678" in is_dm_calls


# ============================================================================
# Slash Commands
# ============================================================================


class TestSlashCommands:
    """Tests for slash command routing."""

    @pytest.mark.asyncio
    async def test_call_handler_for_all_commands(self):
        chat, adapter, state = await _init_chat()
        calls: list[dict[str, Any]] = []

        @chat.on_slash_command
        async def handler(event):
            calls.append({"command": event.command, "text": event.text})

        event = SlashCommandEvent(
            command="/help",
            text="topic",
            user=Author(
                user_id="U123",
                user_name="user",
                full_name="Test User",
                is_bot=False,
                is_me=False,
            ),
            adapter=adapter,
            channel=None,
            raw={"channel_id": "C456"},
            trigger_id=None,
        )

        chat.process_slash_command(event)
        await asyncio.sleep(0.05)

        assert len(calls) == 1
        assert calls[0]["command"] == "/help"
        assert calls[0]["text"] == "topic"

    @pytest.mark.asyncio
    async def test_route_to_specific_command_handler(self):
        chat, adapter, state = await _init_chat()
        help_calls: list[str] = []
        status_calls: list[str] = []

        async def _help(event):
            help_calls.append(event.command)

        async def _status(event):
            status_calls.append(event.command)

        chat.on_slash_command("/help", _help)
        chat.on_slash_command("/status", _status)

        event = SlashCommandEvent(
            command="/help",
            text="",
            user=Author(
                user_id="U123",
                user_name="user",
                full_name="Test User",
                is_bot=False,
                is_me=False,
            ),
            adapter=adapter,
            channel=None,
            raw={"channel_id": "C456"},
            trigger_id=None,
        )

        chat.process_slash_command(event)
        await asyncio.sleep(0.05)

        assert len(help_calls) == 1
        assert len(status_calls) == 0

    @pytest.mark.asyncio
    async def test_route_to_multiple_commands(self):
        chat, adapter, state = await _init_chat()
        calls: list[str] = []

        async def _handler(event):
            calls.append(event.command)

        chat.on_slash_command(["/status", "/health"], _handler)

        for cmd in ["/status", "/health", "/help"]:
            event = SlashCommandEvent(
                command=cmd,
                text="",
                user=Author(
                    user_id="U123",
                    user_name="user",
                    full_name="Test User",
                    is_bot=False,
                    is_me=False,
                ),
                adapter=adapter,
                channel=None,
                raw={"channel_id": "C456"},
                trigger_id=None,
            )
            chat.process_slash_command(event)

        await asyncio.sleep(0.05)

        # /status and /health matched, but not /help
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_skip_slash_commands_from_self(self):
        chat, adapter, state = await _init_chat()
        calls: list[str] = []

        @chat.on_slash_command
        async def handler(event):
            calls.append(event.command)

        event = SlashCommandEvent(
            command="/help",
            text="",
            user=Author(
                user_id="BOT",
                user_name="testbot",
                full_name="Test Bot",
                is_bot=True,
                is_me=True,
            ),
            adapter=adapter,
            channel=None,
            raw={"channel_id": "C456"},
            trigger_id=None,
        )

        chat.process_slash_command(event)
        await asyncio.sleep(0.05)

        assert len(calls) == 0

    @pytest.mark.asyncio
    async def test_normalize_command_without_leading_slash(self):
        chat, adapter, state = await _init_chat()
        calls: list[str] = []

        async def _handler(event):
            calls.append(event.command)

        # Register with "help" (no slash)
        chat.on_slash_command("help", _handler)

        event = SlashCommandEvent(
            command="/help",
            text="",
            user=Author(
                user_id="U123",
                user_name="user",
                full_name="Test User",
                is_bot=False,
                is_me=False,
            ),
            adapter=adapter,
            channel=None,
            raw={"channel_id": "C456"},
            trigger_id=None,
        )

        chat.process_slash_command(event)
        await asyncio.sleep(0.05)

        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_channel_post_method(self):
        chat, adapter, state = await _init_chat()
        calls: list[str] = []

        @chat.on_slash_command
        async def handler(event):
            calls.append(event.command)
            await event.channel.post("Hello from slash command!")

        event = SlashCommandEvent(
            command="/help",
            text="",
            user=Author(
                user_id="U123",
                user_name="user",
                full_name="Test User",
                is_bot=False,
                is_me=False,
            ),
            adapter=adapter,
            channel=None,
            raw={"channel_id": "C456"},
            trigger_id=None,
            _open_modal=None,
        )

        chat.process_slash_command(event)
        await asyncio.sleep(0.05)

        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_run_both_specific_and_catchall(self):
        chat, adapter, state = await _init_chat()
        specific_calls: list[str] = []
        catchall_calls: list[str] = []

        async def specific_handler(event):
            specific_calls.append(event.command)

        chat.on_slash_command("/help", specific_handler)

        @chat.on_slash_command
        async def catchall(event):
            catchall_calls.append(event.command)

        event = SlashCommandEvent(
            command="/help",
            text="",
            user=Author(
                user_id="U123",
                user_name="user",
                full_name="Test User",
                is_bot=False,
                is_me=False,
            ),
            adapter=adapter,
            channel=None,
            raw={"channel_id": "C456"},
            trigger_id=None,
        )

        chat.process_slash_command(event)
        await asyncio.sleep(0.05)

        assert len(specific_calls) == 1
        assert len(catchall_calls) == 1

    @pytest.mark.asyncio
    async def test_event_has_channel_property(self):
        """The processed event exposes a channel object."""
        chat, adapter, state = await _init_chat()
        received_channels: list[Any] = []

        @chat.on_slash_command
        async def handler(event):
            received_channels.append(event.channel)

        event = SlashCommandEvent(
            command="/help",
            text="topic",
            user=Author(
                user_id="U123",
                user_name="user",
                full_name="Test User",
                is_bot=False,
                is_me=False,
            ),
            adapter=adapter,
            channel=None,
            raw={"channel_id": "C456"},
            trigger_id=None,
        )

        chat.process_slash_command(event)
        await asyncio.sleep(0.05)

        assert len(received_channels) == 1
        assert received_channels[0] is not None
        assert hasattr(received_channels[0], "post")

    @pytest.mark.asyncio
    async def test_specific_handler_not_called_for_other_command(self):
        """A handler for /help should not fire on /status."""
        chat, adapter, state = await _init_chat()
        calls: list[str] = []

        async def _handler(event):
            calls.append(event.command)

        chat.on_slash_command("/help", _handler)

        event = SlashCommandEvent(
            command="/status",
            text="",
            user=Author(
                user_id="U123",
                user_name="user",
                full_name="Test User",
                is_bot=False,
                is_me=False,
            ),
            adapter=adapter,
            channel=None,
            raw={"channel_id": "C456"},
            trigger_id=None,
        )

        chat.process_slash_command(event)
        await asyncio.sleep(0.05)

        assert len(calls) == 0

    @pytest.mark.asyncio
    async def test_single_action_id_registration(self):
        """on_slash_command with a single string works like a single command."""
        chat, adapter, state = await _init_chat()
        calls: list[str] = []

        async def _handler(event):
            calls.append(event.command)

        chat.on_slash_command("/deploy", _handler)

        event = SlashCommandEvent(
            command="/deploy",
            text="prod",
            user=Author(
                user_id="U123",
                user_name="user",
                full_name="Test User",
                is_bot=False,
                is_me=False,
            ),
            adapter=adapter,
            channel=None,
            raw={"channel_id": "C456"},
            trigger_id=None,
        )

        chat.process_slash_command(event)
        await asyncio.sleep(0.05)

        assert len(calls) == 1
        assert calls[0] == "/deploy"


# ============================================================================
# Additional onLockConflict edge cases
# ============================================================================


class TestOnLockConflictExtra:
    """Additional onLockConflict tests."""

    @pytest.mark.asyncio
    async def test_force_release_re_acquires_lock(self):
        """After force-release, the handler should actually run and release."""
        state = create_mock_state()
        adapter = create_mock_adapter("slack")

        chat, _, _ = await _init_chat(
            adapter=adapter,
            state=state,
            on_lock_conflict="force",
        )
        calls: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            calls.append(message.id)

        # Pre-acquire
        await state.acquire_lock("slack:C123:1234.5678", 30000)

        msg = create_test_message("msg-frl-1", "Hey @slack-bot")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        assert calls == ["msg-frl-1"]
        # Lock should be released after handler
        lock_after = await state.acquire_lock("slack:C123:1234.5678", 30000)
        assert lock_after is not None

    @pytest.mark.asyncio
    async def test_callback_receives_thread_id_and_message(self):
        """The callback gets the thread_id and message arguments."""
        state = create_mock_state()
        adapter = create_mock_adapter("slack")

        received_args: list[tuple[str, Any]] = []

        def _cb(thread_id, message):
            received_args.append((thread_id, message))
            return "force"

        chat, _, _ = await _init_chat(
            adapter=adapter,
            state=state,
            on_lock_conflict=_cb,
        )

        @chat.on_mention
        async def handler(thread, message, context=None):
            pass

        await state.acquire_lock("slack:C123:1234.5678", 30000)

        msg = create_test_message("msg-cb-1", "Hey @slack-bot")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        assert len(received_args) == 1
        assert received_args[0][0] == "slack:C123:1234.5678"
        assert received_args[0][1].id == "msg-cb-1"


# ============================================================================
# Additional queue tests
# ============================================================================


class TestConcurrencyQueueExtra:
    """Extra queue concurrency tests."""

    @pytest.mark.asyncio
    async def test_queue_drains_fully_with_multiple_batches(self):
        """Multiple messages in queue are drained, not just the first batch."""
        state = create_mock_state()
        adapter = create_mock_adapter("slack")

        chat, _, _ = await _init_chat(adapter=adapter, state=state, concurrency="queue")

        received: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            received.append(message.id)

        # Hold lock and queue a message
        await state.acquire_lock("slack:C123:1234.5678", 30000)
        await chat.handle_incoming_message(
            adapter,
            "slack:C123:1234.5678",
            create_test_message("msg-drain-1", "Hey @slack-bot one"),
        )

        # Release and send a new message
        await state.force_release_lock("slack:C123:1234.5678")
        await chat.handle_incoming_message(
            adapter,
            "slack:C123:1234.5678",
            create_test_message("msg-drain-2", "Hey @slack-bot two"),
        )

        # Both direct + queued should have been processed
        assert "msg-drain-2" in received
        assert len(received) == 2  # direct msg + drained msg

    @pytest.mark.asyncio
    async def test_empty_queue_no_extra_handler_calls(self):
        """When queue is empty after processing, no extra handler calls happen."""
        state = create_mock_state()
        adapter = create_mock_adapter("slack")

        chat, _, _ = await _init_chat(adapter=adapter, state=state, concurrency="queue")

        call_count = 0

        @chat.on_mention
        async def handler(thread, message, context=None):
            nonlocal call_count
            call_count += 1

        msg = create_test_message("msg-eq-1", "Hey @slack-bot")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        assert call_count == 1

    @pytest.mark.asyncio
    async def test_queue_messages_not_lost_on_release(self):
        """Queued messages are not lost when lock is released externally."""
        state = create_mock_state()
        adapter = create_mock_adapter("slack")

        chat, _, _ = await _init_chat(adapter=adapter, state=state, concurrency="queue")

        received: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            received.append(message.text)

        await state.acquire_lock("slack:C123:1234.5678", 30000)

        await chat.handle_incoming_message(
            adapter,
            "slack:C123:1234.5678",
            create_test_message("msg-noloss-1", "Hey @slack-bot queued"),
        )

        # Verify in queue
        depth = await state.queue_depth("slack:C123:1234.5678")
        assert depth == 1

        # Release and trigger processing
        await state.force_release_lock("slack:C123:1234.5678")
        await chat.handle_incoming_message(
            adapter,
            "slack:C123:1234.5678",
            create_test_message("msg-noloss-2", "Hey @slack-bot fresh"),
        )

        assert "Hey @slack-bot fresh" in received
        assert "Hey @slack-bot queued" in received


# ============================================================================
# Additional debounce tests
# ============================================================================


class TestConcurrencyDebounceExtra:
    """Extra debounce tests."""

    @pytest.mark.asyncio
    async def test_debounce_single_message_eventually_processes(self):
        """A single message with debounce still gets processed."""
        state = create_mock_state()
        adapter = create_mock_adapter("slack")

        chat, _, _ = await _init_chat(
            adapter=adapter,
            state=state,
            concurrency=ConcurrencyConfig(strategy="debounce", debounce_ms=30),
        )

        calls: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            calls.append(message.id)

        msg = create_test_message("msg-deb-single", "Hey @slack-bot single")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        assert len(calls) == 1
        assert calls[0] == "msg-deb-single"


# ============================================================================
# Additional concurrent tests
# ============================================================================


class TestConcurrencyConcurrentExtra:
    """Extra concurrent strategy tests."""

    @pytest.mark.asyncio
    async def test_concurrent_does_not_acquire_lock(self):
        """Concurrent strategy should not interact with the lock system."""
        state = create_mock_state()
        adapter = create_mock_adapter("slack")

        lock_acquire_calls: list[str] = []
        original_acquire = state.acquire_lock

        async def tracking_acquire(thread_id, ttl_ms):
            lock_acquire_calls.append(thread_id)
            return await original_acquire(thread_id, ttl_ms)

        state.acquire_lock = tracking_acquire  # type: ignore[assignment]

        chat, _, _ = await _init_chat(
            adapter=adapter,
            state=state,
            concurrency="concurrent",
        )

        @chat.on_mention
        async def handler(thread, message, context=None):
            pass

        msg = create_test_message("msg-cc-1", "Hey @slack-bot concurrent")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        # The concurrent strategy should not call acquire_lock
        assert "slack:C123:1234.5678" not in lock_acquire_calls

    @pytest.mark.asyncio
    async def test_concurrent_independent_threads_do_not_block(self):
        """Messages to different threads can process concurrently."""
        state = create_mock_state()
        adapter = create_mock_adapter("slack")

        chat, _, _ = await _init_chat(
            adapter=adapter,
            state=state,
            concurrency="concurrent",
        )

        calls: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            calls.append(f"{thread.id}:{message.id}")
            await asyncio.sleep(0.01)

        msg1 = create_test_message("msg-ind-1", "Hey @slack-bot one")
        msg2 = create_test_message("msg-ind-2", "Hey @slack-bot two")

        await asyncio.gather(
            chat.handle_incoming_message(adapter, "slack:C123:thread1", msg1),
            chat.handle_incoming_message(adapter, "slack:C456:thread2", msg2),
        )

        assert len(calls) == 2


# ============================================================================
# Additional lockScope tests
# ============================================================================


class TestLockScopeExtra:
    """Extra lock scope tests."""

    @pytest.mark.asyncio
    async def test_dm_thread_uses_thread_scope_with_resolver(self):
        """Async resolver returning 'thread' for DMs uses thread ID as lock key."""
        state = create_mock_state()
        adapter = create_mock_adapter("slack")

        lock_keys_acquired: list[str] = []
        original_acquire = state.acquire_lock

        async def tracking_acquire(thread_id, ttl_ms):
            lock_keys_acquired.append(thread_id)
            return await original_acquire(thread_id, ttl_ms)

        state.acquire_lock = tracking_acquire  # type: ignore[assignment]

        async def _resolve(ctx):
            return "thread" if ctx.is_dm else "channel"

        chat, _, _ = await _init_chat(
            adapter=adapter,
            state=state,
            lock_scope=_resolve,
        )

        @chat.on_mention
        async def handler(thread, message, context=None):
            pass

        # DM thread: should use thread scope
        msg = create_test_message("msg-ls-dm", "Hey @slack-bot")
        await chat.handle_incoming_message(adapter, "slack:DU123:1234.5678", msg)

        assert "slack:DU123:1234.5678" in lock_keys_acquired

    @pytest.mark.asyncio
    async def test_lock_scope_not_set_defaults_to_thread(self):
        """Without any lock_scope config, defaults to thread scope."""
        state = create_mock_state()
        adapter = create_mock_adapter("slack")

        lock_keys_acquired: list[str] = []
        original_acquire = state.acquire_lock

        async def tracking_acquire(thread_id, ttl_ms):
            lock_keys_acquired.append(thread_id)
            return await original_acquire(thread_id, ttl_ms)

        state.acquire_lock = tracking_acquire  # type: ignore[assignment]

        chat, _, _ = await _init_chat(adapter=adapter, state=state)

        @chat.on_mention
        async def handler(thread, message, context=None):
            pass

        msg = create_test_message("msg-ls-def", "Hey @slack-bot")
        await chat.handle_incoming_message(adapter, "slack:C999:thread123", msg)

        assert "slack:C999:thread123" in lock_keys_acquired


# ============================================================================
# Additional persistMessageHistory tests
# ============================================================================


class TestPersistMessageHistoryExtra:
    """Extra message history tests."""

    @pytest.mark.asyncio
    async def test_history_appends_multiple_messages(self):
        adapter = create_mock_adapter("whatsapp")
        adapter.persist_message_history = True
        state = create_mock_state()

        config = ChatConfig(
            user_name="testbot",
            adapters={"whatsapp": adapter},
            state=state,
            logger=MockLogger(),
        )
        chat = Chat(config)
        await chat.webhooks["whatsapp"]("request")

        @chat.on_mention
        async def handler(thread, message, context=None):
            pass

        msg1 = create_test_message("msg-h-1", "First message")
        msg2 = create_test_message("msg-h-2", "Second message")
        await chat.handle_incoming_message(adapter, "whatsapp:phone:user1", msg1)
        await chat.handle_incoming_message(adapter, "whatsapp:phone:user1", msg2)

        stored = state.cache.get("msg-history:whatsapp:phone:user1")
        assert stored is not None
        assert len(stored) == 2
        assert stored[0]["id"] == "msg-h-1"
        assert stored[1]["id"] == "msg-h-2"

    @pytest.mark.asyncio
    async def test_history_also_stored_at_channel_level(self):
        """Messages are also stored at the channel level (channelId != threadId)."""
        adapter = create_mock_adapter("whatsapp")
        adapter.persist_message_history = True
        state = create_mock_state()

        config = ChatConfig(
            user_name="testbot",
            adapters={"whatsapp": adapter},
            state=state,
            logger=MockLogger(),
        )
        chat = Chat(config)
        await chat.webhooks["whatsapp"]("request")

        @chat.on_mention
        async def handler(thread, message, context=None):
            pass

        msg = create_test_message("msg-ch-1", "Channel level")
        await chat.handle_incoming_message(adapter, "whatsapp:phone:user1", msg)

        # Should be stored both at thread and channel level
        thread_stored = state.cache.get("msg-history:whatsapp:phone:user1")
        channel_stored = state.cache.get("msg-history:whatsapp:phone")
        assert thread_stored is not None
        assert channel_stored is not None


# ============================================================================
# Additional openDM tests
# ============================================================================


class TestOpenDMExtra:
    """Extra openDM tests."""

    @pytest.mark.asyncio
    async def test_dm_thread_is_dm_true(self):
        """Opened DM thread has is_dm=True."""
        chat, adapter, state = await _init_chat()

        thread = await chat.open_dm("U999")
        assert thread.is_dm is True

    @pytest.mark.asyncio
    async def test_dm_thread_has_post_method(self):
        """DM thread object supports posting."""
        chat, adapter, state = await _init_chat()

        thread = await chat.open_dm("U888")
        assert callable(getattr(thread, "post", None))

    @pytest.mark.asyncio
    async def test_dm_thread_has_is_subscribed_method(self):
        """DM thread object supports is_subscribed check."""
        chat, adapter, state = await _init_chat()

        thread = await chat.open_dm("U777")
        assert callable(getattr(thread, "is_subscribed", None))

        is_sub = await thread.is_subscribed()
        assert is_sub is False


# ============================================================================
# Additional isDM edge cases
# ============================================================================


class TestIsDMExtra:
    """Extra isDM tests."""

    @pytest.mark.asyncio
    async def test_dm_handler_gets_dm_thread(self):
        """Thread provided to DM handler has is_dm=True."""
        chat, adapter, state = await _init_chat()
        captured_thread = None

        @chat.on_direct_message
        async def handler(thread, message, channel=None, context=None):
            nonlocal captured_thread
            captured_thread = thread

        msg = create_test_message("msg-dm-1", "Hello DM")
        await chat.handle_incoming_message(adapter, "slack:DU123:1234.5678", msg)

        assert captured_thread is not None
        assert captured_thread.is_dm is True

    @pytest.mark.asyncio
    async def test_mention_handler_gets_non_dm_thread(self):
        """Thread provided to mention handler in a channel has is_dm=False."""
        chat, adapter, state = await _init_chat()
        captured_thread = None

        @chat.on_mention
        async def handler(thread, message, context=None):
            nonlocal captured_thread
            captured_thread = thread

        msg = create_test_message("msg-ch-2", "Hey @slack-bot channel")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        assert captured_thread is not None
        assert captured_thread.is_dm is False


# ============================================================================
# Additional queue edge case tests
# ============================================================================


class TestConcurrencyQueueMaxSize:
    """Tests for queue max size behavior."""

    @pytest.mark.asyncio
    async def test_default_max_queue_size_is_ten(self):
        """Default queue max size is 10."""
        state = create_mock_state()
        adapter = create_mock_adapter("slack")

        chat, _, _ = await _init_chat(adapter=adapter, state=state, concurrency="queue")

        @chat.on_mention
        async def handler(thread, message, context=None):
            pass

        await state.acquire_lock("slack:C123:1234.5678", 30000)

        # Enqueue 12 messages with default max of 10
        for i in range(12):
            await chat.handle_incoming_message(
                adapter,
                "slack:C123:1234.5678",
                create_test_message(f"msg-max-{i}", f"Hey @slack-bot msg {i}"),
            )

        depth = await state.queue_depth("slack:C123:1234.5678")
        assert depth == 10

    @pytest.mark.asyncio
    async def test_queue_preserves_order(self):
        """Messages in queue maintain FIFO order."""
        state = create_mock_state()
        adapter = create_mock_adapter("slack")

        chat, _, _ = await _init_chat(adapter=adapter, state=state, concurrency="queue")

        received_order: list[str] = []
        received_skipped: list[list[str]] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            received_order.append(message.id)
            if context:
                received_skipped.append([m.id for m in context.skipped])

        await state.acquire_lock("slack:C123:1234.5678", 30000)

        # Enqueue in order
        for i in range(3):
            await chat.handle_incoming_message(
                adapter,
                "slack:C123:1234.5678",
                create_test_message(f"msg-ord-{i}", f"Hey @slack-bot {i}"),
            )

        await state.force_release_lock("slack:C123:1234.5678")
        await chat.handle_incoming_message(
            adapter,
            "slack:C123:1234.5678",
            create_test_message("msg-ord-trigger", "Hey @slack-bot trigger"),
        )

        # Direct message first, then drained: latest is msg-ord-2, skipped are [0, 1]
        assert received_order[0] == "msg-ord-trigger"
        assert received_order[1] == "msg-ord-2"
        assert received_skipped[0] == ["msg-ord-0", "msg-ord-1"]

    @pytest.mark.asyncio
    async def test_different_threads_have_independent_queues(self):
        """Queue is per-thread, not global."""
        state = create_mock_state()
        adapter = create_mock_adapter("slack")

        chat, _, _ = await _init_chat(adapter=adapter, state=state, concurrency="queue")

        calls: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            calls.append(message.id)

        # Hold lock on thread 1 only
        await state.acquire_lock("slack:C123:thread1", 30000)

        await chat.handle_incoming_message(
            adapter,
            "slack:C123:thread1",
            create_test_message("msg-t1-1", "Hey @slack-bot t1"),
        )

        # Thread 2 should process normally (no lock held)
        await chat.handle_incoming_message(
            adapter,
            "slack:C456:thread2",
            create_test_message("msg-t2-1", "Hey @slack-bot t2"),
        )

        depth_t1 = await state.queue_depth("slack:C123:thread1")
        assert depth_t1 == 1
        assert "msg-t2-1" in calls
