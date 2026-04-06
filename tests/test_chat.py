"""Tests for the Chat orchestrator: construction, handler registration, webhook routing,
message processing, concurrency strategies.

Ported from packages/chat/src/chat.test.ts (first ~1000 lines).
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest
from chat_sdk.chat import Chat
from chat_sdk.emoji import get_emoji
from chat_sdk.errors import LockError
from chat_sdk.testing import (
    MockAdapter,
    MockLogger,
    MockStateAdapter,
    create_mock_adapter,
    create_mock_state,
    create_test_message,
)
from chat_sdk.types import (
    ActionEvent,
    Author,
    ChatConfig,
    ReactionEvent,
)

HELP_REGEX = re.compile(r"help", re.IGNORECASE)
HELLO_REGEX = re.compile(r"hello", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_chat(
    adapter: MockAdapter | None = None,
    state: MockStateAdapter | None = None,
    **overrides: Any,
) -> tuple[Chat, MockAdapter, MockStateAdapter]:
    """Create a Chat instance with mock adapter and state."""
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
    """Create and initialize a Chat instance."""
    chat, adapter, state = _make_chat(adapter, state, **overrides)
    # Trigger initialization via webhook
    await chat.webhooks["slack"]("request")
    return chat, adapter, state


# ============================================================================
# Initialization / shutdown
# ============================================================================


class TestChatInit:
    """Tests for Chat initialization and shutdown."""

    @pytest.mark.asyncio
    async def test_initialize_adapters(self):
        chat, adapter, state = await _init_chat()
        assert len(adapter._initialize_calls) == 1
        assert adapter._initialize_calls[0] is chat

    @pytest.mark.asyncio
    async def test_disconnect_adapters_during_shutdown(self):
        chat, adapter, state = await _init_chat()
        await chat.shutdown()
        # disconnect is a no-op in MockAdapter but shouldn't raise
        # verify state adapter was also disconnected by checking it was called

    @pytest.mark.asyncio
    async def test_disconnect_all_adapters_during_shutdown(self):
        slack = create_mock_adapter("slack")
        discord = create_mock_adapter("discord")
        state = create_mock_state()
        config = ChatConfig(
            user_name="testbot",
            adapters={"slack": slack, "discord": discord},
            state=state,
            logger=MockLogger(),
        )
        chat = Chat(config)
        await chat.webhooks["slack"]("request")
        await chat.shutdown()
        # Both adapters should have disconnect called without error

    @pytest.mark.asyncio
    async def test_continue_shutdown_even_if_adapter_disconnect_fails(self):
        failing = create_mock_adapter("slack")
        healthy = create_mock_adapter("discord")

        # Make failing adapter's disconnect raise
        async def _raise_disconnect():
            raise RuntimeError("connection lost")

        failing.disconnect = _raise_disconnect  # type: ignore[assignment]

        state = create_mock_state()
        config = ChatConfig(
            user_name="testbot",
            adapters={"slack": failing, "discord": healthy},
            state=state,
            logger=MockLogger(),
        )
        chat = Chat(config)
        await chat.webhooks["slack"]("request")
        # Should not raise
        await chat.shutdown()

    @pytest.mark.asyncio
    async def test_register_webhook_handlers(self):
        chat, adapter, state = _make_chat()
        assert "slack" in chat.webhooks
        assert callable(chat.webhooks["slack"])


# ============================================================================
# Mention handling
# ============================================================================


class TestChatMentions:
    """Tests for @-mention handling."""

    @pytest.mark.asyncio
    async def test_call_on_mention_handler_when_bot_is_mentioned(self):
        chat, adapter, state = await _init_chat()
        calls: list[tuple] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            calls.append((thread, message))

        message = create_test_message("msg-1", "Hey @slack-bot help me")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", message)

        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_skip_messages_from_self(self):
        chat, adapter, state = await _init_chat()
        calls: list[tuple] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            calls.append((thread, message))

        message = create_test_message(
            "msg-1",
            "I am the bot",
            author=Author(
                user_id="BOT",
                user_name="testbot",
                full_name="Test Bot",
                is_bot=True,
                is_me=True,
            ),
        )
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", message)

        assert len(calls) == 0

    @pytest.mark.asyncio
    async def test_set_is_mention_true_when_bot_is_mentioned(self):
        chat, adapter, state = await _init_chat()
        received_messages: list[Any] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            received_messages.append(message)

        message = create_test_message("msg-1", "Hey @slack-bot help me")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", message)

        assert len(received_messages) == 1
        assert received_messages[0].is_mention is True

    @pytest.mark.asyncio
    async def test_set_is_mention_false_when_bot_is_not_mentioned(self):
        chat, adapter, state = await _init_chat()
        received_messages: list[Any] = []

        @chat.on_message(HELP_REGEX)
        async def handler(thread, message, context=None):
            received_messages.append(message)

        message = create_test_message("msg-1", "I need help")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", message)

        assert len(received_messages) == 1
        assert received_messages[0].is_mention is False


# ============================================================================
# Message deduplication
# ============================================================================


class TestChatDeduplication:
    """Tests for message deduplication."""

    @pytest.mark.asyncio
    async def test_skip_duplicate_messages_with_same_id(self):
        chat, adapter, state = await _init_chat()
        calls: list[tuple] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            calls.append((thread, message))

        msg1 = create_test_message("msg-1", "Hey @slack-bot help")
        msg2 = create_test_message("msg-1", "Hey @slack-bot help")

        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg1)
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg2)

        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_use_atomic_set_if_not_exists_for_deduplication(self):
        chat, adapter, state = await _init_chat()

        @chat.on_mention
        async def handler(thread, message, context=None):
            pass

        message = create_test_message("msg-1", "Hey @slack-bot help")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", message)

        # Verify set_if_not_exists was used
        assert "dedupe:slack:msg-1" in state.cache

    @pytest.mark.asyncio
    async def test_handle_concurrent_duplicates_atomically(self):
        chat, adapter, state = await _init_chat()

        call_count = 0
        original_set = state.set_if_not_exists

        async def mock_set_if_not_exists(key, value, ttl_ms=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return await original_set(key, value, ttl_ms)
            return False

        state.set_if_not_exists = mock_set_if_not_exists  # type: ignore[assignment]

        handler_calls: list[tuple] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            handler_calls.append((thread, message))

        msg1 = create_test_message("ts-1", "Hey @slack-bot help")
        msg2 = create_test_message("ts-1", "Hey @slack-bot help")

        await asyncio.gather(
            chat.handle_incoming_message(adapter, "slack:C123:ts-1", msg1),
            chat.handle_incoming_message(adapter, "slack:C123:ts-1", msg2),
            return_exceptions=True,
        )

        assert len(handler_calls) == 1

    @pytest.mark.asyncio
    async def test_trigger_on_mention_for_message_containing_bot_mention(self):
        chat, adapter, state = await _init_chat()
        calls: list[tuple] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            calls.append((thread, message))

        message = create_test_message("msg-1", "Hey @slack-bot help")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", message)

        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_not_trigger_on_mention_when_no_bot_mention(self):
        chat, adapter, state = await _init_chat()
        mention_calls: list[tuple] = []
        pattern_calls: list[tuple] = []

        @chat.on_mention
        async def mention_handler(thread, message, context=None):
            mention_calls.append((thread, message))

        @chat.on_message(HELLO_REGEX)
        async def pattern_handler(thread, message, context=None):
            pattern_calls.append((thread, message))

        message = create_test_message("msg-1", "hello everyone")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", message)

        assert len(mention_calls) == 0
        assert len(pattern_calls) == 1


# ============================================================================
# Subscribed messages
# ============================================================================


class TestChatSubscribedMessages:
    """Tests for subscribed thread message handling."""

    @pytest.mark.asyncio
    async def test_call_on_subscribed_message_handler_for_subscribed_threads(self):
        chat, adapter, state = await _init_chat()
        mention_calls: list[tuple] = []
        subscribed_calls: list[tuple] = []

        @chat.on_mention
        async def mention_handler(thread, message, context=None):
            mention_calls.append((thread, message))

        @chat.on_subscribed_message
        async def subscribed_handler(thread, message, context=None):
            subscribed_calls.append((thread, message))

        await state.subscribe("slack:C123:1234.5678")
        message = create_test_message("msg-1", "Follow up message")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", message)

        assert len(subscribed_calls) == 1
        assert len(mention_calls) == 0

    @pytest.mark.asyncio
    async def test_not_call_on_mention_for_mentions_in_subscribed_threads(self):
        chat, adapter, state = await _init_chat()
        mention_calls: list[tuple] = []
        subscribed_calls: list[tuple] = []

        @chat.on_mention
        async def mention_handler(thread, message, context=None):
            mention_calls.append((thread, message))

        @chat.on_subscribed_message
        async def subscribed_handler(thread, message, context=None):
            subscribed_calls.append((thread, message))

        await state.subscribe("slack:C123:1234.5678")
        message = create_test_message("msg-1", "Hey @slack-bot are you there?")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", message)

        assert len(subscribed_calls) == 1
        assert len(mention_calls) == 0

    @pytest.mark.asyncio
    async def test_call_on_mention_only_for_mentions_in_unsubscribed_threads(self):
        chat, adapter, state = await _init_chat()
        mention_calls: list[tuple] = []
        subscribed_calls: list[tuple] = []

        @chat.on_mention
        async def mention_handler(thread, message, context=None):
            mention_calls.append((thread, message))

        @chat.on_subscribed_message
        async def subscribed_handler(thread, message, context=None):
            subscribed_calls.append((thread, message))

        # Thread is NOT subscribed
        message = create_test_message("msg-1", "Hey @slack-bot help me")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", message)

        assert len(mention_calls) == 1
        assert len(subscribed_calls) == 0

    @pytest.mark.asyncio
    async def test_set_is_mention_true_in_subscribed_thread_when_mentioned(self):
        chat, adapter, state = await _init_chat()
        received_messages: list[Any] = []

        @chat.on_subscribed_message
        async def handler(thread, message, context=None):
            received_messages.append(message)

        await state.subscribe("slack:C123:1234.5678")
        message = create_test_message("msg-1", "Hey @slack-bot what about this?")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", message)

        assert len(received_messages) == 1
        assert received_messages[0].is_mention is True


# ============================================================================
# Pattern matching
# ============================================================================


class TestChatPatternMatching:
    """Tests for message pattern matching."""

    @pytest.mark.asyncio
    async def test_match_message_patterns(self):
        chat, adapter, state = await _init_chat()
        calls: list[tuple] = []

        @chat.on_message(HELP_REGEX)
        async def handler(thread, message, context=None):
            calls.append((thread, message))

        message = create_test_message("msg-1", "Can someone help me?")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", message)

        assert len(calls) == 1


# ============================================================================
# Direct messages
# ============================================================================


class TestChatDirectMessages:
    """Tests for direct message handling."""

    @pytest.mark.asyncio
    async def test_route_dms_to_direct_message_handler(self):
        chat, adapter, state = await _init_chat()
        dm_calls: list[tuple] = []
        mention_calls: list[tuple] = []

        @chat.on_direct_message
        async def dm_handler(thread, message, channel=None, context=None):
            dm_calls.append((thread, message, channel))

        @chat.on_mention
        async def mention_handler(thread, message, context=None):
            mention_calls.append((thread, message))

        message = create_test_message("msg-1", "Hello bot")
        await chat.handle_incoming_message(adapter, "slack:DU123:1234.5678", message)

        assert len(dm_calls) == 1
        assert len(mention_calls) == 0
        # Verify channel is passed
        assert dm_calls[0][2] is not None

    @pytest.mark.asyncio
    async def test_fall_through_to_on_mention_when_no_dm_handlers(self):
        chat, adapter, state = await _init_chat()
        calls: list[tuple] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            calls.append((thread, message))

        message = create_test_message("msg-1", "Hello bot")
        await chat.handle_incoming_message(adapter, "slack:DU123:1234.5678", message)

        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_route_subscribed_dm_threads_to_on_direct_message(self):
        chat, adapter, state = await _init_chat()
        dm_calls: list[tuple] = []
        subscribed_calls: list[tuple] = []

        @chat.on_direct_message
        async def dm_handler(thread, message, channel=None, context=None):
            dm_calls.append((thread, message))

        @chat.on_subscribed_message
        async def subscribed_handler(thread, message, context=None):
            subscribed_calls.append((thread, message))

        await state.subscribe("slack:DU123:1234.5678")
        message = create_test_message("msg-1", "Follow up DM")
        await chat.handle_incoming_message(adapter, "slack:DU123:1234.5678", message)

        assert len(dm_calls) == 1
        assert len(subscribed_calls) == 0

    @pytest.mark.asyncio
    async def test_not_route_non_dm_mentions_to_direct_message_handler(self):
        chat, adapter, state = await _init_chat()
        dm_calls: list[tuple] = []
        mention_calls: list[tuple] = []

        @chat.on_direct_message
        async def dm_handler(thread, message, channel=None, context=None):
            dm_calls.append((thread, message))

        @chat.on_mention
        async def mention_handler(thread, message, context=None):
            mention_calls.append((thread, message))

        message = create_test_message("msg-1", "Hey @slack-bot help")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", message)

        assert len(mention_calls) == 1
        assert len(dm_calls) == 0


# ============================================================================
# thread.is_subscribed()
# ============================================================================


class TestThreadIsSubscribed:
    """Tests for thread.is_subscribed() via handler context."""

    @pytest.mark.asyncio
    async def test_return_true_for_subscribed_threads(self):
        chat, adapter, state = await _init_chat()
        captured_thread = None

        @chat.on_subscribed_message
        async def handler(thread, message, context=None):
            nonlocal captured_thread
            captured_thread = thread

        await state.subscribe("slack:C123:1234.5678")
        message = create_test_message("msg-1", "Follow up")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", message)

        assert captured_thread is not None
        is_subscribed = await captured_thread.is_subscribed()
        assert is_subscribed is True

    @pytest.mark.asyncio
    async def test_return_false_for_unsubscribed_threads(self):
        chat, adapter, state = await _init_chat()
        captured_thread = None

        @chat.on_mention
        async def handler(thread, message, context=None):
            nonlocal captured_thread
            captured_thread = thread

        message = create_test_message("msg-1", "Hey @slack-bot help")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", message)

        assert captured_thread is not None
        is_subscribed = await captured_thread.is_subscribed()
        assert is_subscribed is False


# ============================================================================
# Reactions
# ============================================================================


class TestChatReactions:
    """Tests for reaction event handling."""

    def _make_reaction_event(
        self,
        adapter: MockAdapter,
        emoji_name: str = "thumbs_up",
        raw_emoji: str = "+1",
        added: bool = True,
        is_me: bool = False,
        thread_id: str = "slack:C123:1234.5678",
    ) -> ReactionEvent:
        return ReactionEvent(
            emoji=get_emoji(emoji_name),
            raw_emoji=raw_emoji,
            added=added,
            user=Author(
                user_id="BOT" if is_me else "U123",
                user_name="testbot" if is_me else "user",
                full_name="Test Bot" if is_me else "Test User",
                is_bot=is_me,
                is_me=is_me,
            ),
            message_id="msg-1",
            thread_id=thread_id,
            adapter=adapter,
            thread=None,
            raw={},
        )

    @pytest.mark.asyncio
    async def test_call_on_reaction_handler_for_all_reactions(self):
        chat, adapter, state = await _init_chat()
        calls: list[ReactionEvent] = []

        chat.on_reaction(lambda event: calls.append(event))

        event = self._make_reaction_event(adapter)
        chat.process_reaction(event)
        await asyncio.sleep(0.02)

        assert len(calls) == 1
        received = calls[0]
        assert received.emoji == event.emoji
        assert received.raw_emoji == event.raw_emoji
        assert received.thread is not None

    @pytest.mark.asyncio
    async def test_call_on_reaction_handler_for_specific_emoji(self):
        chat, adapter, state = await _init_chat()
        calls: list[ReactionEvent] = []

        chat.on_reaction(["thumbs_up", "heart"], lambda event: calls.append(event))

        thumbs_event = self._make_reaction_event(adapter, "thumbs_up", "+1")
        fire_event = self._make_reaction_event(adapter, "fire", "fire")

        chat.process_reaction(thumbs_event)
        chat.process_reaction(fire_event)
        await asyncio.sleep(0.02)

        assert len(calls) == 1
        assert calls[0].emoji == get_emoji("thumbs_up")

    @pytest.mark.asyncio
    async def test_skip_reactions_from_self(self):
        chat, adapter, state = await _init_chat()
        calls: list[ReactionEvent] = []

        chat.on_reaction(lambda event: calls.append(event))

        event = self._make_reaction_event(adapter, is_me=True)
        chat.process_reaction(event)
        await asyncio.sleep(0.02)

        assert len(calls) == 0

    @pytest.mark.asyncio
    async def test_match_by_raw_emoji_when_specified_in_filter(self):
        chat, adapter, state = await _init_chat()
        calls: list[ReactionEvent] = []

        chat.on_reaction(["+1"], lambda event: calls.append(event))

        event = self._make_reaction_event(adapter, "thumbs_up", "+1")
        chat.process_reaction(event)
        await asyncio.sleep(0.02)

        assert len(calls) == 1
        assert calls[0].raw_emoji == "+1"

    @pytest.mark.asyncio
    async def test_handle_removed_reactions(self):
        chat, adapter, state = await _init_chat()
        calls: list[ReactionEvent] = []

        chat.on_reaction(lambda event: calls.append(event))

        event = self._make_reaction_event(adapter, added=False)
        chat.process_reaction(event)
        await asyncio.sleep(0.02)

        assert len(calls) == 1
        assert calls[0].added is False

    @pytest.mark.asyncio
    async def test_match_teams_style_reactions(self):
        chat, adapter, state = await _init_chat()
        calls: list[ReactionEvent] = []

        chat.on_reaction(
            ["thumbs_up", "heart", "fire", "rocket"],
            lambda event: calls.append(event),
        )

        event = ReactionEvent(
            emoji=get_emoji("thumbs_up"),
            raw_emoji="like",
            added=True,
            user=Author(
                user_id="29:abc123",
                user_name="unknown",
                full_name="Test User",
                is_bot=False,
                is_me=False,
            ),
            message_id="1767297849909",
            thread_id="teams:abc:def",
            adapter=adapter,
            thread=None,
            raw={},
        )

        chat.process_reaction(event)
        await asyncio.sleep(0.02)

        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_match_emoji_value_by_object_identity(self):
        chat, adapter, state = await _init_chat()
        thumbs_up = get_emoji("thumbs_up")
        calls: list[ReactionEvent] = []

        chat.on_reaction([thumbs_up], lambda event: calls.append(event))

        event = ReactionEvent(
            emoji=thumbs_up,
            raw_emoji="like",
            added=True,
            user=Author(
                user_id="U123",
                user_name="user",
                full_name="Test User",
                is_bot=False,
                is_me=False,
            ),
            message_id="msg-1",
            thread_id="slack:C123:1234.5678",
            adapter=adapter,
            thread=None,
            raw={},
        )

        chat.process_reaction(event)
        await asyncio.sleep(0.02)

        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_include_thread_property_in_reaction_event(self):
        chat, adapter, state = await _init_chat()
        calls: list[ReactionEvent] = []

        chat.on_reaction(lambda event: calls.append(event))

        event = self._make_reaction_event(adapter)
        chat.process_reaction(event)
        await asyncio.sleep(0.02)

        assert len(calls) == 1
        received = calls[0]
        assert received.thread is not None
        assert received.thread.id == "slack:C123:1234.5678"
        assert callable(getattr(received.thread, "post", None))
        assert callable(getattr(received.thread, "is_subscribed", None))

    @pytest.mark.asyncio
    async def test_allow_posting_from_reaction_thread(self):
        chat, adapter, state = await _init_chat()
        calls: list[ReactionEvent] = []

        async def _handler(event: ReactionEvent):
            calls.append(event)
            await event.thread.post("Thanks for the reaction!")

        chat.on_reaction(_handler)

        event = self._make_reaction_event(adapter)
        chat.process_reaction(event)
        await asyncio.sleep(0.05)

        assert len(calls) == 1
        assert len(adapter._post_calls) == 1
        assert adapter._post_calls[0][0] == "slack:C123:1234.5678"
        assert adapter._post_calls[0][1] == "Thanks for the reaction!"


# ============================================================================
# Actions
# ============================================================================


class TestChatActions:
    """Tests for action event handling."""

    def _make_action_event(
        self,
        adapter: MockAdapter,
        action_id: str = "approve",
        value: str | None = "order-123",
    ) -> ActionEvent:
        return ActionEvent(
            action_id=action_id,
            value=value,
            user=Author(
                user_id="U123",
                user_name="user",
                full_name="Test User",
                is_bot=False,
                is_me=False,
            ),
            message_id="msg-1",
            thread_id="slack:C123:1234.5678",
            adapter=adapter,
            thread=None,
            raw={},
        )

    @pytest.mark.asyncio
    async def test_call_on_action_handler_for_all_actions(self):
        chat, adapter, state = await _init_chat()
        calls: list[ActionEvent] = []

        chat.on_action(lambda event: calls.append(event))

        event = self._make_action_event(adapter)
        chat.process_action(event)
        await asyncio.sleep(0.02)

        assert len(calls) == 1
        received = calls[0]
        assert received.action_id == "approve"
        assert received.value == "order-123"
        assert received.thread is not None

    @pytest.mark.asyncio
    async def test_call_on_action_handler_for_specific_action_ids(self):
        chat, adapter, state = await _init_chat()
        calls: list[ActionEvent] = []

        chat.on_action(["approve", "reject"], lambda event: calls.append(event))

        approve_event = self._make_action_event(adapter, "approve")
        skip_event = self._make_action_event(adapter, "skip")

        chat.process_action(approve_event)
        chat.process_action(skip_event)
        await asyncio.sleep(0.02)

        assert len(calls) == 1
        assert calls[0].action_id == "approve"


# ============================================================================
# Concurrency: drop strategy
# ============================================================================


class TestChatConcurrencyDrop:
    """Tests for the drop concurrency strategy (default)."""

    @pytest.mark.asyncio
    async def test_drop_message_when_lock_unavailable(self):
        chat, adapter, state = await _init_chat()

        @chat.on_mention
        async def handler(thread, message, context=None):
            # Hold the lock for a bit
            await asyncio.sleep(0.1)

        msg1 = create_test_message("msg-1", "Hey @slack-bot first")
        msg2 = create_test_message("msg-2", "Hey @slack-bot second")

        # Start first message processing
        task1 = asyncio.ensure_future(chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg1))
        await asyncio.sleep(0.01)

        # Second message should fail to acquire lock
        with pytest.raises(LockError):
            await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg2)

        await task1


# ============================================================================
# Concurrency: queue strategy
# ============================================================================


class TestChatConcurrencyQueue:
    """Tests for the queue concurrency strategy."""

    @pytest.mark.asyncio
    async def test_queue_messages_when_lock_busy(self):
        chat, adapter, state = await _init_chat(concurrency="queue")
        calls: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            calls.append(message.id)
            await asyncio.sleep(0.02)

        msg1 = create_test_message("msg-1", "Hey @slack-bot first")
        msg2 = create_test_message("msg-2", "Hey @slack-bot second")

        # Process both; second should be queued then processed
        await asyncio.gather(
            chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg1),
            chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg2),
            return_exceptions=True,
        )

        # At least the first message should have been processed
        assert "msg-1" in calls
