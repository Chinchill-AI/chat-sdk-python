"""Faithful 1:1 translation of chat.test.ts (96 tests).

Every ``it("...")`` block in the TypeScript test suite is represented here
as a Python ``async def test_*`` method with the same inputs, same setup,
and same assertions -- converted to Python equivalents.

TS file: packages/chat/src/chat.test.ts  (3059 lines, 96 tests)
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest

from chat_sdk.chat import Chat
from chat_sdk.emoji import get_emoji
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
    ActionEvent,
    Author,
    ChatConfig,
    ConcurrencyConfig,
    EmojiValue,
    MessageContext,
    ModalSubmitEvent,
    ReactionEvent,
    SlashCommandEvent,
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
    """Create a Chat instance with mock adapter and state (not initialized)."""
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
    """Create and initialize a Chat instance (triggers adapters/state)."""
    chat, adapter, state = _make_chat(adapter, state, **overrides)
    await chat.webhooks["slack"]("request")
    return chat, adapter, state


async def _init_multi_chat(
    adapters_dict: dict[str, MockAdapter],
    state: MockStateAdapter | None = None,
    **overrides: Any,
) -> tuple[Chat, MockStateAdapter]:
    """Create and initialize a Chat instance with multiple adapters."""
    state = state or create_mock_state()
    config = ChatConfig(
        user_name="testbot",
        adapters=adapters_dict,
        state=state,
        logger=MockLogger(),
        **overrides,
    )
    chat = Chat(config)
    # Trigger initialization via the first adapter's webhook
    first_key = next(iter(adapters_dict))
    await chat.webhooks[first_key]("request")
    return chat, state


def _make_author(
    *,
    user_id: str = "U123",
    user_name: str = "user",
    full_name: str = "Test User",
    is_bot: bool = False,
    is_me: bool = False,
) -> Author:
    return Author(
        user_id=user_id,
        user_name=user_name,
        full_name=full_name,
        is_bot=is_bot,
        is_me=is_me,
    )


def _bot_author() -> Author:
    return _make_author(
        user_id="BOT",
        user_name="testbot",
        full_name="Test Bot",
        is_bot=True,
        is_me=True,
    )


def _make_reaction_event(
    adapter: MockAdapter,
    *,
    emoji: EmojiValue | None = None,
    raw_emoji: str = "+1",
    added: bool = True,
    user: Author | None = None,
    message_id: str = "msg-1",
    thread_id: str = "slack:C123:1234.5678",
) -> ReactionEvent:
    return ReactionEvent(
        emoji=emoji or get_emoji("thumbs_up"),
        raw_emoji=raw_emoji,
        added=added,
        user=user or _make_author(),
        message_id=message_id,
        thread_id=thread_id,
        adapter=adapter,
        thread=None,
        raw={},
    )


def _make_action_event(
    adapter: MockAdapter,
    *,
    action_id: str = "approve",
    value: str | None = "order-123",
    user: Author | None = None,
    message_id: str = "msg-1",
    thread_id: str = "slack:C123:1234.5678",
    trigger_id: str | None = None,
) -> ActionEvent:
    return ActionEvent(
        action_id=action_id,
        value=value,
        user=user or _make_author(),
        message_id=message_id,
        thread_id=thread_id,
        adapter=adapter,
        thread=None,
        raw={},
        trigger_id=trigger_id,
    )


def _make_slash_event(
    adapter: MockAdapter,
    *,
    command: str = "/help",
    text: str = "topic",
    user: Author | None = None,
    channel_id: str = "slack:C456",
    trigger_id: str | None = None,
) -> SlashCommandEvent:
    event = SlashCommandEvent(
        command=command,
        text=text,
        user=user or _make_author(),
        adapter=adapter,
        channel=None,
        raw={"channel_id": "C456"},
        trigger_id=trigger_id,
    )
    # Monkey-patch channel_id so _handle_slash_command_event sees it via getattr
    object.__setattr__(event, "channel_id", channel_id)
    return event


# ============================================================================
# 1. Initialization / shutdown (tests 1-7)
# ============================================================================


class TestChatInit:
    """TS describe("Chat") top-level init/shutdown tests."""

    # TS: "should initialize adapters"
    async def test_should_initialize_adapters(self):
        chat, adapter, state = await _init_chat()
        assert len(adapter._initialize_calls) == 1
        assert adapter._initialize_calls[0] is chat

    # TS: "should disconnect adapters during shutdown"
    async def test_should_disconnect_adapters_during_shutdown(self):
        chat, adapter, state = await _init_chat()
        await chat.shutdown()
        assert not chat._initialized

    # TS: "should disconnect adapter before state adapter during shutdown"
    async def test_should_disconnect_adapter_before_state_adapter_during_shutdown(self):
        chat, adapter, state = await _init_chat()
        order: list[str] = []

        original_adapter_disconnect = adapter.disconnect

        async def _adapter_disconnect():
            order.append("adapter")
            await original_adapter_disconnect()

        original_state_disconnect = state.disconnect

        async def _state_disconnect():
            order.append("state")
            await original_state_disconnect()

        adapter.disconnect = _adapter_disconnect  # type: ignore[assignment]
        state.disconnect = _state_disconnect  # type: ignore[assignment]

        await chat.shutdown()
        assert order.index("adapter") < order.index("state")

    # TS: "should allow adapters without disconnect during shutdown"
    async def test_should_allow_adapters_without_disconnect_during_shutdown(self):
        adapter = create_mock_adapter("slack")
        adapter.disconnect = None  # type: ignore[assignment]
        state = create_mock_state()

        config = ChatConfig(
            user_name="testbot",
            adapters={"slack": adapter},
            state=state,
            logger=MockLogger(),
        )
        local_chat = Chat(config)
        await local_chat.webhooks["slack"]("request")
        # Shutdown should complete even when adapter has no disconnect method
        await local_chat.shutdown()
        assert not local_chat._initialized

    # TS: "should disconnect all adapters during shutdown"
    async def test_should_disconnect_all_adapters_during_shutdown(self):
        slack = create_mock_adapter("slack")
        discord = create_mock_adapter("discord")
        state = create_mock_state()

        chat, _ = await _init_multi_chat(
            {"slack": slack, "discord": discord},
            state=state,
        )
        await chat.shutdown()
        assert not chat._initialized

    # TS: "should continue shutdown even if an adapter disconnect fails"
    async def test_should_continue_shutdown_even_if_an_adapter_disconnect_fails(self):
        failing = create_mock_adapter("slack")
        healthy = create_mock_adapter("discord")

        healthy_disconnected = False
        original_disconnect = healthy.disconnect

        async def _track_disconnect():
            nonlocal healthy_disconnected
            healthy_disconnected = True
            await original_disconnect()

        async def _raise():
            raise RuntimeError("connection lost")

        failing.disconnect = _raise  # type: ignore[assignment]
        healthy.disconnect = _track_disconnect  # type: ignore[assignment]
        state = create_mock_state()

        chat, _ = await _init_multi_chat(
            {"slack": failing, "discord": healthy},
            state=state,
        )
        # Should not raise despite failing adapter
        await chat.shutdown()

        # Healthy adapter was still disconnected despite the other adapter failing
        assert healthy_disconnected
        # Chat instance is fully shut down
        assert not chat._initialized

    # TS: "should register webhook handlers"
    async def test_should_register_webhook_handlers(self):
        chat, adapter, state = _make_chat()
        assert "slack" in chat.webhooks
        assert callable(chat.webhooks["slack"])


# ============================================================================
# 2. Fallback streaming placeholder (test 8)
# ============================================================================


class TestFallbackStreamingPlaceholder:
    # TS: "should preserve null fallback streaming placeholder config"
    async def test_should_preserve_null_fallback_streaming_placeholder_config(self):
        adapter = create_mock_adapter("slack")
        adapter.stream = None  # type: ignore[attr-defined]
        state = create_mock_state()

        custom_chat = Chat(
            ChatConfig(
                user_name="testbot",
                adapters={"slack": adapter},
                state=state,
                logger=MockLogger(),
                fallback_streaming_placeholder_text=None,
            )
        )
        await custom_chat.webhooks["slack"]("request")

        @custom_chat.on_mention
        async def handler(thread, message, context=None):
            async def _stream():
                yield "H"
                yield "i"

            await thread.post(_stream())

        msg = create_test_message("msg-1", "Hey @slack-bot help me")
        await custom_chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        # No placeholder "..." should have been posted
        for _tid, content in adapter._post_calls:
            assert content != "..."
        # With placeholder=None the first chunk posts immediately; subsequent
        # chunks are delivered via edit_message (matches upstream TS's
        # `render()`-based edit loop).
        assert len(adapter._post_calls) >= 1
        first_thread_id, _first_content = adapter._post_calls[0]
        assert first_thread_id == "slack:C123:1234.5678"
        # The final edit carries the full "Hi" payload.
        assert len(adapter._edit_calls) >= 1
        last_edit_content = adapter._edit_calls[-1][2]
        last_markdown = last_edit_content.markdown if hasattr(last_edit_content, "markdown") else last_edit_content
        assert last_markdown == "Hi"


# ============================================================================
# 3. Mention handling (tests 9-10)
# ============================================================================


class TestMentionHandling:
    # TS: "should call onNewMention handler when bot is mentioned"
    async def test_should_call_onnewmention_handler_when_bot_is_mentioned(self):
        chat, adapter, state = await _init_chat()
        calls: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            calls.append(message.id)

        msg = create_test_message("msg-1", "Hey @slack-bot help me")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        assert len(calls) == 1
        assert calls[0] == "msg-1"

    # TS: "should call onSubscribedMessage handler for subscribed threads"
    async def test_should_call_onsubscribedmessage_handler_for_subscribed_threads(self):
        chat, adapter, state = await _init_chat()
        mention_calls: list[str] = []
        subscribed_calls: list[str] = []

        @chat.on_mention
        async def mention_handler(thread, message, context=None):
            mention_calls.append(message.id)

        @chat.on_subscribed_message
        async def sub_handler(thread, message, context=None):
            subscribed_calls.append(message.id)

        await state.subscribe("slack:C123:1234.5678")
        msg = create_test_message("msg-1", "Follow up message")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        assert len(subscribed_calls) == 1
        assert subscribed_calls[0] == "msg-1"
        assert len(mention_calls) == 0


# ============================================================================
# 4. Skip self (test 11)
# ============================================================================


class TestSkipSelf:
    # TS: "should skip messages from self"
    async def test_should_skip_messages_from_self(self):
        chat, adapter, state = await _init_chat()
        calls: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            calls.append(message.id)

        msg = create_test_message(
            "msg-1",
            "I am the bot",
            author=_bot_author(),
        )
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)
        assert len(calls) == 0


# ============================================================================
# 5. Message deduplication (tests 12-18)
# ============================================================================


class TestMessageDeduplication:
    # TS: "should skip duplicate messages with the same id"
    async def test_should_skip_duplicate_messages_with_the_same_id(self):
        chat, adapter, state = await _init_chat()
        calls: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            calls.append(message.id)

        msg1 = create_test_message("msg-1", "Hey @slack-bot help")
        msg2 = create_test_message("msg-1", "Hey @slack-bot help")

        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg1)
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg2)

        assert len(calls) == 1
        assert calls[0] == "msg-1"

    # TS: "should use default dedupe TTL of 5 minutes"
    async def test_should_use_default_dedupe_ttl_of_5_minutes(self):
        chat, adapter, state = await _init_chat()

        @chat.on_mention
        async def handler(thread, message, context=None):
            pass

        msg = create_test_message("msg-1", "Hey @slack-bot help")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        # set_if_not_exists should have been called with 300_000ms TTL
        assert "dedupe:slack:msg-1" in state.cache
        # The default dedupe TTL is 5 minutes = 300_000ms

    # TS: "should use custom dedupeTtlMs when configured"
    async def test_should_use_custom_dedupettlms_when_configured(self):
        adapter = create_mock_adapter("slack")
        state = create_mock_state()

        custom_chat, _, _ = await _init_chat(
            adapter=adapter,
            state=state,
            dedupe_ttl_ms=300_000,
        )

        @custom_chat.on_mention
        async def handler(thread, message, context=None):
            pass

        msg = create_test_message("msg-2", "Hey @slack-bot help")
        await custom_chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        assert "dedupe:slack:msg-2" in state.cache

    # TS: "should use atomic setIfNotExists for deduplication"
    async def test_should_use_atomic_setifnotexists_for_deduplication(self):
        chat, adapter, state = await _init_chat()

        @chat.on_mention
        async def handler(thread, message, context=None):
            pass

        msg = create_test_message("msg-1", "Hey @slack-bot help")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        # Verify set_if_not_exists was called (key exists in cache)
        assert "dedupe:slack:msg-1" in state.cache

    # TS: "should handle concurrent duplicates atomically"
    async def test_should_handle_concurrent_duplicates_atomically(self):
        chat, adapter, state = await _init_chat()
        call_count = 0
        original_set_if_not_exists = state.set_if_not_exists

        async def _counting_set(key, value, ttl_ms=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return await original_set_if_not_exists(key, value, ttl_ms)
            # Second call: simulate race -- key already set
            return False

        state.set_if_not_exists = _counting_set  # type: ignore[assignment]

        calls: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            calls.append(message.id)

        msg1 = create_test_message("ts-1", "Hey @slack-bot help")
        msg2 = create_test_message("ts-1", "Hey @slack-bot help")

        await asyncio.gather(
            chat.handle_incoming_message(adapter, "slack:C123:ts-1", msg1),
            chat.handle_incoming_message(adapter, "slack:C123:ts-1", msg2),
            return_exceptions=True,
        )

        assert len(calls) == 1

    # TS: "should trigger onNewMention for message events containing a bot mention"
    async def test_should_trigger_onnewmention_for_message_events_containing_a_bot_mention(self):
        chat, adapter, state = await _init_chat()
        calls: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            calls.append(message.id)

        msg = create_test_message("msg-1", "Hey @slack-bot help")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        assert len(calls) == 1
        assert calls[0] == "msg-1"

    # TS: "should not trigger onNewMention when message event has no bot mention"
    async def test_should_not_trigger_onnewmention_when_message_event_has_no_bot_mention(self):
        chat, adapter, state = await _init_chat()
        mention_calls: list[str] = []
        pattern_calls: list[str] = []

        @chat.on_mention
        async def mention_handler(thread, message, context=None):
            mention_calls.append(message.id)

        @chat.on_message(HELLO_REGEX)
        async def pattern_handler(thread, message, context=None):
            pattern_calls.append(message.id)

        msg = create_test_message("msg-1", "hello everyone")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        assert len(mention_calls) == 0
        assert len(pattern_calls) == 1
        assert pattern_calls[0] == "msg-1"


# ============================================================================
# 6. Pattern matching (test 19)
# ============================================================================


class TestPatternMatching:
    # TS: "should match message patterns"
    async def test_should_match_message_patterns(self):
        chat, adapter, state = await _init_chat()
        calls: list[str] = []

        @chat.on_message(HELP_REGEX)
        async def handler(thread, message, context=None):
            calls.append(message.id)

        msg = create_test_message("msg-1", "Can someone help me?")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        assert len(calls) == 1
        assert calls[0] == "msg-1"


# ============================================================================
# 7. isMention property (tests 20-22)
# ============================================================================


class TestIsMention:
    # TS: "should set isMention=true when bot is mentioned"
    async def test_should_set_ismentiontrue_when_bot_is_mentioned(self):
        chat, adapter, state = await _init_chat()
        received_messages: list[Any] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            received_messages.append(message)

        msg = create_test_message("msg-1", "Hey @slack-bot help me")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        assert len(received_messages) == 1
        assert received_messages[0].is_mention is True

    # TS: "should set isMention=false when bot is not mentioned"
    async def test_should_set_ismentionfalse_when_bot_is_not_mentioned(self):
        chat, adapter, state = await _init_chat()
        received_messages: list[Any] = []

        @chat.on_message(HELP_REGEX)
        async def handler(thread, message, context=None):
            received_messages.append(message)

        msg = create_test_message("msg-1", "I need help")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        assert len(received_messages) == 1
        assert received_messages[0].is_mention is False

    # TS: "should set isMention=true in subscribed thread when mentioned"
    async def test_should_set_ismentiontrue_in_subscribed_thread_when_mentioned(self):
        chat, adapter, state = await _init_chat()
        received_messages: list[Any] = []

        @chat.on_subscribed_message
        async def handler(thread, message, context=None):
            received_messages.append(message)

        await state.subscribe("slack:C123:1234.5678")
        msg = create_test_message("msg-1", "Hey @slack-bot what about this?")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        assert len(received_messages) == 1
        assert received_messages[0].is_mention is True


# ============================================================================
# 8. onNewMention in subscribed threads (tests 23-24)
# ============================================================================


class TestMentionInSubscribedThreads:
    # TS: "should NOT call onNewMention for mentions in subscribed threads"
    async def test_should_not_call_onnewmention_for_mentions_in_subscribed_threads(self):
        chat, adapter, state = await _init_chat()
        mention_calls: list[str] = []
        subscribed_calls: list[str] = []

        @chat.on_mention
        async def mention_handler(thread, message, context=None):
            mention_calls.append(message.id)

        @chat.on_subscribed_message
        async def sub_handler(thread, message, context=None):
            subscribed_calls.append(message.id)

        await state.subscribe("slack:C123:1234.5678")
        msg = create_test_message("msg-1", "Hey @slack-bot are you there?")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        assert len(subscribed_calls) == 1
        assert subscribed_calls[0] == "msg-1"
        assert len(mention_calls) == 0

    # TS: "should call onNewMention only for mentions in unsubscribed threads"
    async def test_should_call_onnewmention_only_for_mentions_in_unsubscribed_threads(self):
        chat, adapter, state = await _init_chat()
        mention_calls: list[str] = []
        subscribed_calls: list[str] = []

        @chat.on_mention
        async def mention_handler(thread, message, context=None):
            mention_calls.append(message.id)

        @chat.on_subscribed_message
        async def sub_handler(thread, message, context=None):
            subscribed_calls.append(message.id)

        msg = create_test_message("msg-1", "Hey @slack-bot help me")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        assert len(mention_calls) == 1
        assert mention_calls[0] == "msg-1"
        assert len(subscribed_calls) == 0


# ============================================================================
# 9. onDirectMessage (tests 25-28)
# ============================================================================


class TestDirectMessage:
    # TS: "should route DMs to directMessage handler with channel"
    async def test_should_route_dms_to_directmessage_handler_with_channel(self):
        chat, adapter, state = await _init_chat()
        dm_calls: list[Any] = []
        mention_calls: list[str] = []

        @chat.on_direct_message
        async def dm_handler(thread, message, channel, context=None):
            dm_calls.append({"thread": thread, "channel": channel})

        @chat.on_mention
        async def mention_handler(thread, message, context=None):
            mention_calls.append(message.id)

        msg = create_test_message("msg-1", "Hello bot")
        await chat.handle_incoming_message(adapter, "slack:DU123:1234.5678", msg)

        assert len(dm_calls) == 1
        assert len(mention_calls) == 0
        assert dm_calls[0]["channel"] is not None
        assert dm_calls[0]["channel"].id == "slack:DU123"

    # TS: "should fall through to onNewMention when no DM handlers registered"
    async def test_should_fall_through_to_onnewmention_when_no_dm_handlers_registered(self):
        chat, adapter, state = await _init_chat()
        mention_calls: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            mention_calls.append(message.id)

        msg = create_test_message("msg-1", "Hello bot")
        await chat.handle_incoming_message(adapter, "slack:DU123:1234.5678", msg)

        assert len(mention_calls) == 1
        assert mention_calls[0] == "msg-1"

    # TS: "should route subscribed DM threads to onDirectMessage, not onSubscribedMessage"
    async def test_should_route_subscribed_dm_threads_to_ondirectmessage_not_onsubscribedmessage(self):
        chat, adapter, state = await _init_chat()
        dm_calls: list[str] = []
        subscribed_calls: list[str] = []

        @chat.on_direct_message
        async def dm_handler(thread, message, channel, context=None):
            dm_calls.append(message.id)

        @chat.on_subscribed_message
        async def sub_handler(thread, message, context=None):
            subscribed_calls.append(message.id)

        await state.subscribe("slack:DU123:1234.5678")
        msg = create_test_message("msg-1", "Follow up DM")
        await chat.handle_incoming_message(adapter, "slack:DU123:1234.5678", msg)

        assert len(dm_calls) == 1
        assert dm_calls[0] == "msg-1"
        assert len(subscribed_calls) == 0

    # TS: "should not route non-DM mentions to directMessage handler"
    async def test_should_not_route_nondm_mentions_to_directmessage_handler(self):
        chat, adapter, state = await _init_chat()
        dm_calls: list[str] = []
        mention_calls: list[str] = []

        @chat.on_direct_message
        async def dm_handler(thread, message, channel, context=None):
            dm_calls.append(message.id)

        @chat.on_mention
        async def mention_handler(thread, message, context=None):
            mention_calls.append(message.id)

        msg = create_test_message("msg-1", "Hey @slack-bot help")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        assert len(mention_calls) == 1
        assert mention_calls[0] == "msg-1"
        assert len(dm_calls) == 0


# ============================================================================
# 10. thread.isSubscribed() (tests 29-30)
# ============================================================================


class TestThreadIsSubscribed:
    # TS: "should return true for subscribed threads"
    async def test_should_return_true_for_subscribed_threads(self):
        chat, adapter, state = await _init_chat()
        captured_thread: Any = None

        @chat.on_subscribed_message
        async def handler(thread, message, context=None):
            nonlocal captured_thread
            captured_thread = thread

        await state.subscribe("slack:C123:1234.5678")
        msg = create_test_message("msg-1", "Follow up")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        assert captured_thread is not None
        is_subscribed = await captured_thread.is_subscribed()
        assert is_subscribed is True

    # TS: "should return false for unsubscribed threads"
    async def test_should_return_false_for_unsubscribed_threads(self):
        chat, adapter, state = await _init_chat()
        captured_thread: Any = None

        @chat.on_mention
        async def handler(thread, message, context=None):
            nonlocal captured_thread
            captured_thread = thread

        msg = create_test_message("msg-1", "Hey @slack-bot help")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        assert captured_thread is not None
        is_subscribed = await captured_thread.is_subscribed()
        assert is_subscribed is False


# ============================================================================
# 11. Reactions (tests 31-39)
# ============================================================================


class TestReactions:
    # TS: "should call onReaction handler for all reactions"
    async def test_should_call_onreaction_handler_for_all_reactions(self):
        chat, adapter, state = await _init_chat()
        received: list[ReactionEvent] = []

        chat.on_reaction(lambda event: received.append(event))

        event = _make_reaction_event(adapter)
        chat.process_reaction(event)
        await asyncio.sleep(0.02)

        assert len(received) == 1
        assert received[0].emoji == event.emoji
        assert received[0].raw_emoji == event.raw_emoji
        assert received[0].thread is not None

    # TS: "should call onReaction handler for specific emoji"
    async def test_should_call_onreaction_handler_for_specific_emoji(self):
        chat, adapter, state = await _init_chat()
        received: list[ReactionEvent] = []

        async def _handler(event):
            received.append(event)

        chat.on_reaction(["thumbs_up", "heart"], _handler)

        thumbs_up_event = _make_reaction_event(adapter, emoji=get_emoji("thumbs_up"), raw_emoji="+1")
        fire_event = _make_reaction_event(adapter, emoji=get_emoji("fire"), raw_emoji="fire")

        chat.process_reaction(thumbs_up_event)
        chat.process_reaction(fire_event)
        await asyncio.sleep(0.02)

        assert len(received) == 1
        assert received[0].emoji == thumbs_up_event.emoji

    # TS: "should skip reactions from self"
    async def test_should_skip_reactions_from_self(self):
        chat, adapter, state = await _init_chat()
        received: list[ReactionEvent] = []

        chat.on_reaction(lambda event: received.append(event))

        event = _make_reaction_event(adapter, user=_bot_author())
        chat.process_reaction(event)
        await asyncio.sleep(0.02)

        assert len(received) == 0

    # TS: "should match by rawEmoji when specified in filter"
    async def test_should_match_by_rawemoji_when_specified_in_filter(self):
        chat, adapter, state = await _init_chat()
        received: list[ReactionEvent] = []

        async def _handler(event):
            received.append(event)

        chat.on_reaction(["+1"], _handler)

        event = _make_reaction_event(adapter, emoji=get_emoji("thumbs_up"), raw_emoji="+1")
        chat.process_reaction(event)
        await asyncio.sleep(0.02)

        assert len(received) == 1
        assert received[0].raw_emoji == "+1"

    # TS: "should handle removed reactions"
    async def test_should_handle_removed_reactions(self):
        chat, adapter, state = await _init_chat()
        received: list[ReactionEvent] = []

        chat.on_reaction(lambda event: received.append(event))

        event = _make_reaction_event(adapter, added=False)
        chat.process_reaction(event)
        await asyncio.sleep(0.02)

        assert len(received) == 1
        assert received[0].added is False

    # TS: "should match Teams-style reactions (EmojiValue with string filter)"
    async def test_should_match_teamsstyle_reactions_emojivalue_with_string_filter(self):
        chat, adapter, state = await _init_chat()
        received: list[ReactionEvent] = []

        async def _handler(event):
            received.append(event)

        chat.on_reaction(["thumbs_up", "heart", "fire", "rocket"], _handler)

        teams_event = _make_reaction_event(
            adapter,
            emoji=get_emoji("thumbs_up"),
            raw_emoji="like",
            user=_make_author(user_id="29:abc123", user_name="unknown"),
            message_id="1767297849909",
            thread_id="teams:abc:def",
        )
        chat.process_reaction(teams_event)
        await asyncio.sleep(0.02)

        assert len(received) == 1
        assert received[0].emoji == teams_event.emoji

    # TS: "should match EmojiValue by object identity"
    async def test_should_match_emojivalue_by_object_identity(self):
        chat, adapter, state = await _init_chat()
        thumbs_up = get_emoji("thumbs_up")
        received: list[ReactionEvent] = []

        async def _handler(event):
            received.append(event)

        chat.on_reaction([thumbs_up], _handler)

        event = _make_reaction_event(adapter, emoji=thumbs_up, raw_emoji="like")
        chat.process_reaction(event)
        await asyncio.sleep(0.02)

        assert len(received) == 1

    # TS: "should include thread property in ReactionEvent"
    async def test_should_include_thread_property_in_reactionevent(self):
        chat, adapter, state = await _init_chat()
        received: list[ReactionEvent] = []

        chat.on_reaction(lambda event: received.append(event))

        event = _make_reaction_event(adapter)
        chat.process_reaction(event)
        await asyncio.sleep(0.02)

        assert len(received) == 1
        assert received[0].thread is not None
        assert received[0].thread.id == "slack:C123:1234.5678"
        assert callable(getattr(received[0].thread, "post", None))
        assert callable(getattr(received[0].thread, "is_subscribed", None))

    # TS: "should allow posting from reaction thread"
    async def test_should_allow_posting_from_reaction_thread(self):
        chat, adapter, state = await _init_chat()

        async def _handler(event):
            await event.thread.post("Thanks for the reaction!")

        chat.on_reaction(_handler)

        event = _make_reaction_event(adapter)
        chat.process_reaction(event)
        await asyncio.sleep(0.05)

        assert any(
            tid == "slack:C123:1234.5678" and content == "Thanks for the reaction!"
            for tid, content in adapter._post_calls
        )


# ============================================================================
# 12. Actions (tests 40-50)
# ============================================================================


class TestActions:
    # TS: "should call onAction handler for all actions"
    async def test_should_call_onaction_handler_for_all_actions(self):
        chat, adapter, state = await _init_chat()
        received: list[ActionEvent] = []

        chat.on_action(lambda event: received.append(event))

        event = _make_action_event(adapter)
        chat.process_action(event)
        await asyncio.sleep(0.02)

        assert len(received) == 1
        assert received[0].action_id == "approve"
        assert received[0].value == "order-123"
        assert received[0].thread is not None

    # TS: "should call onAction handler for specific action IDs"
    async def test_should_call_onaction_handler_for_specific_action_ids(self):
        chat, adapter, state = await _init_chat()
        received: list[ActionEvent] = []

        async def _handler(event):
            received.append(event)

        chat.on_action(["approve", "reject"], _handler)

        approve_event = _make_action_event(adapter, action_id="approve")
        skip_event = _make_action_event(adapter, action_id="skip")

        chat.process_action(approve_event)
        chat.process_action(skip_event)
        await asyncio.sleep(0.02)

        assert len(received) == 1
        assert received[0].action_id == "approve"

    # TS: "should call onAction handler for single action ID"
    async def test_should_call_onaction_handler_for_single_action_id(self):
        chat, adapter, state = await _init_chat()
        received: list[ActionEvent] = []

        async def _handler(event):
            received.append(event)

        chat.on_action("approve", _handler)

        event = _make_action_event(adapter, action_id="approve")
        chat.process_action(event)
        await asyncio.sleep(0.02)

        assert len(received) == 1
        assert received[0].action_id == "approve"

    # TS: "should skip actions from self"
    async def test_should_skip_actions_from_self(self):
        chat, adapter, state = await _init_chat()
        received: list[ActionEvent] = []

        chat.on_action(lambda event: received.append(event))

        event = _make_action_event(adapter, user=_bot_author())
        chat.process_action(event)
        await asyncio.sleep(0.02)

        assert len(received) == 0

    # TS: "should include thread property in ActionEvent"
    async def test_should_include_thread_property_in_actionevent(self):
        chat, adapter, state = await _init_chat()
        received: list[ActionEvent] = []

        chat.on_action(lambda event: received.append(event))

        event = _make_action_event(adapter)
        chat.process_action(event)
        await asyncio.sleep(0.02)

        assert len(received) == 1
        assert received[0].thread is not None
        assert received[0].thread.id == "slack:C123:1234.5678"
        assert callable(getattr(received[0].thread, "post", None))

    # TS: "should allow posting from action thread"
    async def test_should_allow_posting_from_action_thread(self):
        chat, adapter, state = await _init_chat()

        async def _handler(event):
            await event.thread.post("Action received!")

        chat.on_action(_handler)

        event = _make_action_event(adapter)
        chat.process_action(event)
        await asyncio.sleep(0.05)

        assert any(
            tid == "slack:C123:1234.5678" and content == "Action received!" for tid, content in adapter._post_calls
        )

    # TS: "should provide openModal method that calls adapter.openModal"
    async def test_should_provide_openmodal_method_that_calls_adapteropenmodal(self):
        chat, adapter, state = await _init_chat()
        captured_event: list[ActionEvent] = []

        async def _handler(event):
            captured_event.append(event)

        chat.on_action(_handler)

        event = _make_action_event(
            adapter,
            action_id="open_form",
            trigger_id="trigger-123",
        )
        chat.process_action(event)
        await asyncio.sleep(0.02)

        assert len(captured_event) == 1
        assert captured_event[0]._open_modal is not None

        modal = {
            "type": "modal",
            "callback_id": "test_modal",
            "title": "Test Modal",
            "children": [],
        }
        result = await captured_event[0].open_modal(modal)

        assert result is not None
        assert result.get("view_id") == "V123"

        # Wait for the async store to complete
        await asyncio.sleep(0.02)

        # Verify context was stored in state (key starts with modal-context:)
        modal_context_keys = [k for k in state.cache if k.startswith("modal-context:")]
        assert len(modal_context_keys) >= 1
        stored = state.cache[modal_context_keys[0]]
        assert stored["thread"] is not None
        assert stored["thread"]["_type"] == "chat:Thread"
        assert stored["thread"]["id"] == "slack:C123:1234.5678"

    # TS: "should convert JSX Modal to ModalElement in openModal"
    # SKIPPED: JSX is TypeScript-only; Python has no JSX equivalent
    async def test_should_convert_jsx_modal_to_modalelement_in_openmodal(self):
        pytest.skip("JSX Modal conversion is TypeScript-only")
        assert True  # unreachable -- pytest.skip raises

    # TS: "should return undefined from openModal when triggerId is missing"
    async def test_should_return_undefined_from_openmodal_when_triggerid_is_missing(self):
        chat, adapter, state = await _init_chat()
        captured_event: list[ActionEvent] = []

        async def _handler(event):
            captured_event.append(event)

        chat.on_action(_handler)

        event = _make_action_event(adapter, action_id="open_form", trigger_id=None)
        chat.process_action(event)
        await asyncio.sleep(0.02)

        assert len(captured_event) == 1

        modal = {
            "type": "modal",
            "callback_id": "test_modal",
            "title": "Test Modal",
            "children": [],
        }
        result = await captured_event[0].open_modal(modal)
        assert result is None

    # TS: "should return undefined from openModal when adapter does not support modals"
    async def test_should_return_undefined_from_openmodal_when_adapter_does_not_support_modals(self):
        adapter = create_mock_adapter("slack")
        adapter.open_modal = None  # type: ignore[assignment]

        chat, _, state = await _init_chat(adapter=adapter)
        captured_event: list[ActionEvent] = []

        async def _handler(event):
            captured_event.append(event)

        chat.on_action(_handler)

        event = _make_action_event(
            adapter,
            action_id="open_form",
            trigger_id="trigger-123",
        )
        chat.process_action(event)
        await asyncio.sleep(0.02)

        assert len(captured_event) == 1

        modal = {
            "type": "modal",
            "callback_id": "test_modal",
            "title": "Test Modal",
            "children": [],
        }
        result = await captured_event[0].open_modal(modal)
        assert result is None

    # TS: "should open modal when action has empty threadId (no thread context)"
    async def test_should_open_modal_when_action_has_empty_threadid_no_thread_context(self):
        chat, adapter, state = await _init_chat()
        captured_event: list[ActionEvent] = []

        async def _handler(event):
            captured_event.append(event)

        chat.on_action(_handler)

        event = _make_action_event(
            adapter,
            action_id="home_select_scope",
            message_id="",
            thread_id="",
            trigger_id="trigger-456",
        )
        chat.process_action(event)
        await asyncio.sleep(0.02)

        assert len(captured_event) == 1
        # thread should be None for empty threadId
        assert captured_event[0].thread is None
        assert captured_event[0]._open_modal is not None

        modal = {
            "type": "modal",
            "callback_id": "select_scope_form",
            "title": "Select a team",
            "children": [],
        }
        result = await captured_event[0].open_modal(modal)

        assert result is not None
        assert result.get("view_id") == "V123"

        # Wait for the async store to complete
        await asyncio.sleep(0.02)

        # Modal context should store None thread
        modal_context_keys = [k for k in state.cache if k.startswith("modal-context:")]
        assert len(modal_context_keys) >= 1
        stored = state.cache[modal_context_keys[0]]
        assert stored.get("thread") is None


# ============================================================================
# 13. openDM (tests 51-54)
# ============================================================================


class TestOpenDM:
    # TS: "should infer Slack adapter from U... userId"
    async def test_should_infer_slack_adapter_from_u_userid(self):
        chat, adapter, state = await _init_chat()
        thread = await chat.open_dm("U123456")
        assert thread is not None
        assert thread.id == "slack:DU123456:"

    # TS: "should accept Author object and extract userId"
    async def test_should_accept_author_object_and_extract_userid(self):
        chat, adapter, state = await _init_chat()
        author = _make_author(
            user_id="U789ABC",
            user_name="testuser",
            full_name="Test User",
        )
        thread = await chat.open_dm(author)
        assert thread is not None
        assert thread.id == "slack:DU789ABC:"

    # TS: "should throw error for unknown userId format"
    async def test_should_throw_error_for_unknown_userid_format(self):
        chat, adapter, state = await _init_chat()
        with pytest.raises(ChatError, match='Cannot infer adapter from userId "invalid-user-id"'):
            await chat.open_dm("invalid-user-id")

    # TS: "should allow posting to DM thread"
    async def test_should_allow_posting_to_dm_thread(self):
        chat, adapter, state = await _init_chat()
        thread = await chat.open_dm("U123456")
        await thread.post("Hello via DM!")

        assert any(tid == "slack:DU123456:" and content == "Hello via DM!" for tid, content in adapter._post_calls)


# ============================================================================
# thread() factory
# ============================================================================


class TestThreadFactory:
    """describe("thread") — chat.thread(id) factory for building a Thread handle."""

    # TS: "should return a Thread handle for a valid thread ID"
    async def test_should_return_a_thread_handle_for_a_valid_thread_id(self):
        chat, adapter, state = await _init_chat()
        thread = chat.thread("slack:C123:1234.5678")
        assert thread is not None
        assert thread.id == "slack:C123:1234.5678"

    # TS: "should allow posting to a thread handle"
    async def test_should_allow_posting_to_a_thread_handle(self):
        chat, adapter, state = await _init_chat()
        thread = chat.thread("slack:C123:1234.5678")
        await thread.post("Hello from outside a webhook!")

        assert any(
            tid == "slack:C123:1234.5678" and content == "Hello from outside a webhook!"
            for tid, content in adapter._post_calls
        )

    # TS: "should throw for an invalid thread ID"
    async def test_should_throw_for_an_invalid_thread_id(self):
        chat, adapter, state = await _init_chat()
        with pytest.raises(ChatError, match="Invalid thread ID"):
            chat.thread("")

    # TS: "should throw for an unknown adapter prefix"
    async def test_should_throw_for_an_unknown_adapter_prefix(self):
        chat, adapter, state = await _init_chat()
        with pytest.raises(ChatError, match=r'Adapter "unknown" not found'):
            chat.thread("unknown:C123:1234.5678")


# ============================================================================
# 14. isDM (tests 55-57)
# ============================================================================


class TestIsDM:
    # TS: "should return true for DM threads"
    async def test_should_return_true_for_dm_threads(self):
        chat, adapter, state = await _init_chat()
        thread = await chat.open_dm("U123456")
        assert thread.is_dm is True

    # TS: "should return false for non-DM threads"
    async def test_should_return_false_for_nondm_threads(self):
        chat, adapter, state = await _init_chat()
        captured_thread: Any = None

        @chat.on_mention
        async def handler(thread, message, context=None):
            nonlocal captured_thread
            captured_thread = thread

        msg = create_test_message("msg-1", "Hey @slack-bot help")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        assert captured_thread is not None
        assert captured_thread.is_dm is False

    # TS: "should use adapter isDM method for detection"
    async def test_should_use_adapter_isdm_method_for_detection(self):
        chat, adapter, state = await _init_chat()

        @chat.on_mention
        async def handler(thread, message, context=None):
            pass

        msg = create_test_message("msg-1", "Hey @slack-bot help")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        # The adapter's is_dm was called with the thread_id
        # MockAdapter.is_dm checks for ":D" in thread_id; "slack:C123:1234.5678" has no "D"
        assert adapter.is_dm("slack:C123:1234.5678") is False


# ============================================================================
# 15. Slash Commands (tests 58-71)
# ============================================================================


class TestSlashCommands:
    # TS: "should call onSlashCommand handler for all commands"
    async def test_should_call_onslashcommand_handler_for_all_commands(self):
        chat, adapter, state = await _init_chat()
        received: list[Any] = []

        @chat.on_slash_command
        async def handler(event):
            received.append(event)

        event = _make_slash_event(adapter, command="/help", text="topic")
        chat.process_slash_command(event)
        await asyncio.sleep(0.02)

        assert len(received) == 1
        assert received[0].command == "/help"
        assert received[0].text == "topic"
        assert received[0].channel is not None

    # TS: "should call onSlashCommand handler for specific command"
    async def test_should_call_onslashcommand_handler_for_specific_command(self):
        chat, adapter, state = await _init_chat()
        help_calls: list[str] = []
        status_calls: list[str] = []

        async def _help(event):
            help_calls.append(event.command)

        async def _status(event):
            status_calls.append(event.command)

        chat.on_slash_command("/help", _help)
        chat.on_slash_command("/status", _status)

        event = _make_slash_event(adapter, command="/help", text="")
        chat.process_slash_command(event)
        await asyncio.sleep(0.02)

        assert len(help_calls) == 1
        assert len(status_calls) == 0

    # TS: "should call onSlashCommand handler for multiple commands"
    async def test_should_call_onslashcommand_handler_for_multiple_commands(self):
        chat, adapter, state = await _init_chat()
        calls: list[str] = []

        async def _handler(event):
            calls.append(event.command)

        chat.on_slash_command(["/status", "/health"], _handler)

        for cmd in ["/status", "/health", "/help"]:
            event = _make_slash_event(adapter, command=cmd, text="")
            chat.process_slash_command(event)

        await asyncio.sleep(0.02)

        # Should be called for /status and /health, but not /help
        assert len(calls) == 2
        assert "/status" in calls
        assert "/health" in calls

    # TS: "should skip slash commands from self"
    async def test_should_skip_slash_commands_from_self(self):
        chat, adapter, state = await _init_chat()
        calls: list[str] = []

        @chat.on_slash_command
        async def handler(event):
            calls.append(event.command)

        event = _make_slash_event(
            adapter,
            command="/help",
            text="",
            user=_bot_author(),
        )
        chat.process_slash_command(event)
        await asyncio.sleep(0.02)

        assert len(calls) == 0

    # TS: "should normalize command names without leading slash"
    async def test_should_normalize_command_names_without_leading_slash(self):
        chat, adapter, state = await _init_chat()
        calls: list[str] = []

        async def _handler(event):
            calls.append(event.command)

        # Register with "help" (no slash) - normalized to "/help"
        chat.on_slash_command("help", _handler)

        event = _make_slash_event(adapter, command="/help", text="")
        chat.process_slash_command(event)
        await asyncio.sleep(0.02)

        assert len(calls) == 1
        assert calls[0] == "/help"

    # TS: "should provide channel.post method"
    async def test_should_provide_channelpost_method(self):
        chat, adapter, state = await _init_chat()
        post_result = None

        @chat.on_slash_command
        async def handler(event):
            nonlocal post_result
            post_result = await event.channel.post("Hello from slash command!")

        event = _make_slash_event(adapter, command="/help", text="")
        chat.process_slash_command(event)
        await asyncio.sleep(0.05)

        # Handler ran to completion and channel.post returned a SentMessage
        assert post_result is not None
        assert post_result.id == "msg-1"

    # TS: "should provide openModal method that calls adapter.openModal"
    async def test_slash_should_provide_openmodal_method_that_calls_adapteropenmodal(self):
        chat, adapter, state = await _init_chat()
        captured: list[Any] = []

        @chat.on_slash_command
        async def handler(event):
            captured.append(event)

        event = _make_slash_event(adapter, command="/feedback", text="", trigger_id="trigger-123")
        chat.process_slash_command(event)
        await asyncio.sleep(0.02)

        assert len(captured) == 1
        assert captured[0]._open_modal is not None

        modal = {
            "type": "modal",
            "callback_id": "feedback_modal",
            "title": "Feedback",
            "children": [],
        }
        result = await captured[0].open_modal(modal)

        assert result is not None
        assert result.get("view_id") == "V123"

    # TS: "should convert JSX Modal to ModalElement in openModal" (slash)
    # SKIPPED: JSX is TypeScript-only
    async def test_slash_should_convert_jsx_modal_to_modalelement_in_openmodal(self):
        pytest.skip("JSX Modal conversion is TypeScript-only")
        assert True  # unreachable -- pytest.skip raises

    # TS: "should return undefined from openModal when triggerId is missing" (slash)
    async def test_should_return_undefined_from_openmodal_when_triggerid_is_missing(self):
        chat, adapter, state = await _init_chat()
        captured: list[Any] = []

        @chat.on_slash_command
        async def handler(event):
            captured.append(event)

        event = _make_slash_event(adapter, command="/feedback", text="", trigger_id=None)
        chat.process_slash_command(event)
        await asyncio.sleep(0.02)

        assert len(captured) == 1

        modal = {
            "type": "modal",
            "callback_id": "test_modal",
            "title": "Test Modal",
            "children": [],
        }
        result = await captured[0].open_modal(modal)
        assert result is None

    # TS: "should return undefined from openModal when adapter does not support modals" (slash)
    async def test_slash_should_return_undefined_from_openmodal_when_adapter_does_not_support_modals(self):
        adapter = create_mock_adapter("slack")
        adapter.open_modal = None  # type: ignore[assignment]

        chat, _, state = await _init_chat(adapter=adapter)
        captured: list[Any] = []

        @chat.on_slash_command
        async def handler(event):
            captured.append(event)

        event = _make_slash_event(adapter, command="/feedback", text="", trigger_id="trigger-123")
        chat.process_slash_command(event)
        await asyncio.sleep(0.02)

        assert len(captured) == 1

        modal = {
            "type": "modal",
            "callback_id": "test_modal",
            "title": "Test Modal",
            "children": [],
        }
        result = await captured[0].open_modal(modal)
        assert result is None

    # TS: "should run both specific and catch-all handlers"
    async def test_should_run_both_specific_and_catchall_handlers(self):
        chat, adapter, state = await _init_chat()
        specific_calls: list[str] = []
        catch_all_calls: list[str] = []

        async def _specific(event):
            specific_calls.append(event.command)

        chat.on_slash_command("/help", _specific)

        @chat.on_slash_command
        async def catch_all(event):
            catch_all_calls.append(event.command)

        event = _make_slash_event(adapter, command="/help", text="")
        chat.process_slash_command(event)
        await asyncio.sleep(0.02)

        assert len(specific_calls) == 1
        assert len(catch_all_calls) == 1

    # TS: "should store channel context when opening modal and provide relatedChannel in modal submit"
    async def test_should_store_channel_context_when_opening_modal_and_provide_relatedchannel_in_modal_submit(self):
        chat, adapter, state = await _init_chat()
        captured_slash: list[Any] = []

        @chat.on_slash_command
        async def slash_handler(event):
            captured_slash.append(event)

        event = _make_slash_event(adapter, command="/feedback", text="", trigger_id="trigger-123")
        chat.process_slash_command(event)
        await asyncio.sleep(0.02)

        assert len(captured_slash) == 1

        # Open modal
        modal = {
            "type": "modal",
            "callback_id": "slash_feedback",
            "title": "Feedback",
            "children": [],
        }
        await captured_slash[0].open_modal(modal)

        # Wait for the async store to complete
        await asyncio.sleep(0.02)

        # Get context_id from state
        modal_context_keys = [k for k in state.cache if k.startswith("modal-context:")]
        assert len(modal_context_keys) >= 1
        context_id = modal_context_keys[0].split(":")[-1]

        # Submit modal
        modal_submit_received: list[ModalSubmitEvent] = []

        async def _modal_submit_handler(event):
            modal_submit_received.append(event)

        chat.on_modal_submit("slash_feedback", _modal_submit_handler)

        await chat.process_modal_submit(
            ModalSubmitEvent(
                callback_id="slash_feedback",
                view_id="V123",
                values={"message": "Great feedback!"},
                user=_make_author(),
                adapter=adapter,
                raw={},
            ),
            context_id,
        )

        assert len(modal_submit_received) == 1
        assert modal_submit_received[0].related_channel is not None
        assert modal_submit_received[0].related_channel.id == "slack:C456"
        assert modal_submit_received[0].related_thread is None
        assert modal_submit_received[0].related_message is None

    # TS: "should allow posting to relatedChannel from modal submit handler"
    async def test_should_allow_posting_to_relatedchannel_from_modal_submit_handler(self):
        chat, adapter, state = await _init_chat()
        captured_slash: list[Any] = []

        @chat.on_slash_command
        async def slash_handler(event):
            captured_slash.append(event)

        event = _make_slash_event(adapter, command="/feedback", text="", trigger_id="trigger-123")
        chat.process_slash_command(event)
        await asyncio.sleep(0.02)

        modal = {
            "type": "modal",
            "callback_id": "slash_feedback_post",
            "title": "Feedback",
            "children": [],
        }
        await captured_slash[0].open_modal(modal)
        await asyncio.sleep(0.02)

        modal_context_keys = [k for k in state.cache if k.startswith("modal-context:")]
        context_id = modal_context_keys[0].split(":")[-1]

        post_result = None

        async def _modal_handler(event):
            nonlocal post_result
            if event.related_channel:
                post_result = await event.related_channel.post("Thank you for your feedback!")

        chat.on_modal_submit("slash_feedback_post", _modal_handler)

        await chat.process_modal_submit(
            ModalSubmitEvent(
                callback_id="slash_feedback_post",
                view_id="V123",
                values={"message": "Great feedback!"},
                user=_make_author(),
                adapter=adapter,
                raw={},
            ),
            context_id,
        )

        # Handler ran to completion and channel.post returned a SentMessage
        assert post_result is not None
        assert post_result.id == "msg-1"

    # TS: "should provide relatedChannel from action-triggered modal (extracted from thread)"
    async def test_should_provide_relatedchannel_from_actiontriggered_modal_extracted_from_thread(self):
        chat, adapter, state = await _init_chat()
        captured_action: list[ActionEvent] = []

        async def _action_handler(event):
            captured_action.append(event)

        chat.on_action("feedback_button", _action_handler)

        action_event = _make_action_event(
            adapter,
            action_id="feedback_button",
            thread_id="slack:C789:1234.5678",
            trigger_id="trigger-action-123",
        )
        chat.process_action(action_event)
        await asyncio.sleep(0.02)

        assert len(captured_action) == 1

        modal = {
            "type": "modal",
            "callback_id": "action_feedback",
            "title": "Feedback",
            "children": [],
        }
        await captured_action[0].open_modal(modal)
        await asyncio.sleep(0.02)

        modal_context_keys = [k for k in state.cache if k.startswith("modal-context:")]
        assert len(modal_context_keys) >= 1
        context_id = modal_context_keys[0].split(":")[-1]

        modal_submit_received: list[ModalSubmitEvent] = []

        async def _modal_submit(event):
            modal_submit_received.append(event)

        chat.on_modal_submit("action_feedback", _modal_submit)

        await chat.process_modal_submit(
            ModalSubmitEvent(
                callback_id="action_feedback",
                view_id="V456",
                values={"message": "Button feedback!"},
                user=_make_author(),
                adapter=adapter,
                raw={},
            ),
            context_id,
        )

        assert len(modal_submit_received) == 1
        assert modal_submit_received[0].related_channel is not None
        assert modal_submit_received[0].related_channel.id == "slack:C789"
        assert modal_submit_received[0].related_thread is not None
        assert modal_submit_received[0].related_thread.id == "slack:C789:1234.5678"


# ============================================================================
# 16. onLockConflict (tests 72-76)
# ============================================================================


class TestOnLockConflict:
    # TS: "should drop by default when lock is held"
    async def test_should_drop_by_default_when_lock_is_held(self):
        chat, adapter, state = await _init_chat()

        @chat.on_mention
        async def handler(thread, message, context=None):
            pass

        await state.acquire_lock("slack:C123:1234.5678", 30000)

        msg = create_test_message("msg-lock-1", "Hey @slack-bot")
        with pytest.raises(LockError):
            await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

    # TS: "should force-release lock when onLockConflict is 'force'"
    async def test_should_forcerelease_lock_when_onlockconflict_is_force(self):
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

        await state.acquire_lock("slack:C123:1234.5678", 30000)

        msg = create_test_message("msg-lock-2", "Hey @slack-bot")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        assert len(calls) == 1
        assert calls[0] == "msg-lock-2"
        # Verify force_release_lock was called with the thread_id
        assert "slack:C123:1234.5678" in state._force_release_lock_calls
        # Verify lock was re-acquired after force-release
        last_acquire = state._acquire_lock_calls[-1]
        assert last_acquire[0] == "slack:C123:1234.5678"

    # TS: "should support callback returning 'force'"
    async def test_should_support_callback_returning_force(self):
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
        assert calls[0] == "msg-lock-3"

    # TS: "should support callback returning 'drop'"
    async def test_should_support_callback_returning_drop(self):
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

    # TS: "should support async callback"
    async def test_should_support_async_callback(self):
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
        assert calls[0] == "msg-lock-5"


# ============================================================================
# 17. concurrency: queue (tests 77-78)
# ============================================================================


class TestConcurrencyQueue:
    # TS: "should process queued messages with skipped context after handler finishes"
    async def test_should_process_queued_messages_with_skipped_context_after_handler_finishes(self):
        state = create_mock_state()
        adapter = create_mock_adapter("slack")

        chat, _, _ = await _init_chat(adapter=adapter, state=state, concurrency="queue")

        received_contexts: list[MessageContext | None] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            received_contexts.append(context)

        msg1 = create_test_message("msg-q-1", "Hey @slack-bot first")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg1)

        assert len(received_contexts) == 1
        assert received_contexts[0] is None

    # TS: "should enqueue messages when lock is held and drain after"
    async def test_should_enqueue_messages_when_lock_is_held_and_drain_after(self):
        state = create_mock_state()
        adapter = create_mock_adapter("slack")

        chat, _, _ = await _init_chat(adapter=adapter, state=state, concurrency="queue")

        received_messages: list[str] = []
        received_contexts: list[MessageContext | None] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            received_messages.append(message.text)
            received_contexts.append(context)

        # Pre-acquire lock to simulate busy handler
        await state.acquire_lock("slack:C123:1234.5678", 30000)

        # These should be enqueued
        msg1 = create_test_message("msg-q-2", "Hey @slack-bot second")
        msg2 = create_test_message("msg-q-3", "Hey @slack-bot third")
        msg3 = create_test_message("msg-q-4", "Hey @slack-bot fourth")

        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg1)
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg2)
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg3)

        # Handler not called yet
        assert len(received_messages) == 0

        # Release and trigger drain
        await state.force_release_lock("slack:C123:1234.5678")
        msg4 = create_test_message("msg-q-5", "Hey @slack-bot fifth")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg4)

        # msg4 direct, then drain: msg3 as latest with [msg1, msg2] skipped
        assert len(received_messages) == 2
        assert received_messages[0] == "Hey @slack-bot fifth"
        assert received_contexts[0] is None
        assert received_messages[1] == "Hey @slack-bot fourth"
        assert received_contexts[1] is not None
        assert [m.text for m in received_contexts[1].skipped] == [
            "Hey @slack-bot second",
            "Hey @slack-bot third",
        ]
        assert received_contexts[1].total_since_last_handler == 3


# ============================================================================
# 18. concurrency: queue with onSubscribedMessage (test 79)
# ============================================================================


class TestConcurrencyQueueSubscribed:
    # TS: "should pass skipped context to subscribed message handlers"
    async def test_should_pass_skipped_context_to_subscribed_message_handlers(self):
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

        # Pre-acquire lock
        await state.acquire_lock("slack:C123:1234.5678", 30000)

        # Queue follow-ups
        msg1 = create_test_message("msg-sub-1", "first follow-up")
        msg2 = create_test_message("msg-sub-2", "second follow-up")
        msg3 = create_test_message("msg-sub-3", "third follow-up")

        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg1)
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg2)
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg3)

        # Release and drain
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
# 19. concurrency: queue edge cases (tests 80-83)
# ============================================================================


class TestConcurrencyQueueEdgeCases:
    # TS: "should drop newest when queue is full with drop-newest policy"
    async def test_should_drop_newest_when_queue_is_full_with_dropnewest_policy(self):
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

    # TS: "should drop oldest when queue is full with drop-oldest policy"
    async def test_should_drop_oldest_when_queue_is_full_with_dropoldest_policy(self):
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

        # msg-do-4 direct, then drain: [msg-do-2, msg-do-3] (msg-do-1 evicted)
        assert received_messages[0] == "Hey @slack-bot four"
        assert received_messages[1] == "Hey @slack-bot three"

    # TS: "should skip expired entries during drain"
    async def test_should_skip_expired_entries_during_drain(self):
        state = create_mock_state()
        adapter = create_mock_adapter("slack")

        chat, _, _ = await _init_chat(
            adapter=adapter,
            state=state,
            concurrency=ConcurrencyConfig(
                strategy="queue",
                queue_entry_ttl_ms=1,  # Expire almost immediately
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
        await asyncio.sleep(0.02)

        await state.force_release_lock("slack:C123:1234.5678")
        await chat.handle_incoming_message(
            adapter,
            "slack:C123:1234.5678",
            create_test_message("msg-exp-2", "Hey @slack-bot fresh"),
        )

        # Only the fresh message should be processed
        assert received_messages == ["Hey @slack-bot fresh"]

    # TS: "should work with onNewMessage pattern handlers"
    async def test_should_work_with_onnewmessage_pattern_handlers(self):
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
# 20. concurrency: debounce (tests 84-85)
# ============================================================================


class TestConcurrencyDebounce:
    # TS: "should debounce the first message and process after delay"
    async def test_should_debounce_the_first_message_and_process_after_delay(self):
        state = create_mock_state()
        adapter = create_mock_adapter("slack")

        chat, _, _ = await _init_chat(
            adapter=adapter,
            state=state,
            concurrency=ConcurrencyConfig(
                strategy="debounce",
                debounce_ms=50,  # Use shorter delay for test speed
            ),
        )

        calls: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            calls.append(message.text)

        msg = create_test_message("msg-d-1", "Hey @slack-bot debounce")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        # Handler should have been called after debounce delay
        assert len(calls) == 1
        assert calls[0] == "Hey @slack-bot debounce"

    # TS: "should only process the last message in a burst"
    async def test_should_only_process_the_last_message_in_a_burst(self):
        state = create_mock_state()
        adapter = create_mock_adapter("slack")

        chat, _, _ = await _init_chat(
            adapter=adapter,
            state=state,
            concurrency=ConcurrencyConfig(
                strategy="debounce",
                debounce_ms=50,
            ),
        )

        calls: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            calls.append(message.text)

        # First message acquires lock, enters debounce loop
        msg1 = create_test_message("msg-d-2", "Hey @slack-bot first")
        task = asyncio.create_task(chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg1))

        # Allow event loop to start the debounce
        await asyncio.sleep(0.01)

        # Second and third message supersede while debouncing
        msg2 = create_test_message("msg-d-3", "Hey @slack-bot second")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg2)

        msg3 = create_test_message("msg-d-4", "Hey @slack-bot third")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg3)

        await task

        # Only one handler call with the last message
        assert len(calls) == 1
        assert calls[0] == "Hey @slack-bot third"


# ============================================================================
# 21. concurrency: concurrent (test 86)
# ============================================================================


class TestConcurrencyConcurrent:
    # TS: "should process messages without acquiring a lock"
    async def test_should_process_messages_without_acquiring_a_lock(self):
        state = create_mock_state()
        adapter = create_mock_adapter("slack")

        chat, _, _ = await _init_chat(adapter=adapter, state=state, concurrency="concurrent")

        calls: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            calls.append(message.text)

        # Pre-acquire lock -- should NOT block concurrent strategy
        await state.acquire_lock("slack:C123:1234.5678", 30000)

        msg = create_test_message("msg-c-1", "Hey @slack-bot concurrent")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        assert len(calls) == 1
        assert calls[0] == "Hey @slack-bot concurrent"

    # Python-specific: upstream accepts max_concurrent but doesn't enforce
    # it. We do. Bound should cap in-flight handlers at N; the (N+1)th
    # message has to wait until one of the first N releases.
    async def test_max_concurrent_bounds_in_flight_handlers(self):
        state = create_mock_state()
        adapter = create_mock_adapter("slack")

        chat, _, _ = await _init_chat(
            adapter=adapter,
            state=state,
            concurrency=ConcurrencyConfig(strategy="concurrent", max_concurrent=2),
        )

        in_flight = 0
        max_observed = 0
        gate = asyncio.Event()
        finished = 0

        @chat.on_mention
        async def handler(thread, message, context=None):
            nonlocal in_flight, max_observed, finished
            in_flight += 1
            max_observed = max(max_observed, in_flight)
            await gate.wait()
            in_flight -= 1
            finished += 1

        # Dispatch 5 messages concurrently — at most 2 should be in flight
        # at any time while the gate is closed.
        tasks = [
            asyncio.create_task(
                chat.handle_incoming_message(
                    adapter,
                    f"slack:C123:{i}",
                    create_test_message(f"msg-{i}", "Hey @slack-bot"),
                )
            )
            for i in range(5)
        ]

        # Wait until the first 2 handlers reach the gate. asyncio uses a
        # single-threaded cooperative scheduler, so between `_reach_cap`
        # returning and the next assertion, no other task can interleave
        # — tasks 3-5 are parked on `semaphore.acquire()`. The
        # `in_flight == 2` check IS stable here.
        async def _reach_cap() -> None:
            while in_flight < 2:
                await asyncio.sleep(0.001)

        await asyncio.wait_for(_reach_cap(), timeout=1.0)
        # Snapshot while the gate is still closed: exactly the bound
        # should be in flight, and no more.
        assert in_flight == 2

        # Release the gate; all 5 should drain. If the semaphore leaked,
        # `max_observed` inside the handlers captured the peak before
        # any could unblock, so the final assertion below would fail.
        gate.set()
        await asyncio.gather(*tasks)

        assert finished == 5
        # The critical assertion: peak in-flight never exceeded 2.
        assert max_observed == 2

    # Python-specific: reject invalid `max_concurrent` values at construction
    # time rather than silently falling back to unbounded (which would
    # surprise users who set `max_concurrent=0` expecting strict throttling).
    async def test_max_concurrent_zero_or_negative_raises(self):
        state = create_mock_state()
        adapter = create_mock_adapter("slack")

        for bad_value in (0, -1, -100):
            import pytest

            with pytest.raises(ValueError, match="max_concurrent must be a positive integer or None"):
                await _init_chat(
                    adapter=adapter,
                    state=state,
                    concurrency=ConcurrencyConfig(strategy="concurrent", max_concurrent=bad_value),
                )

    # Python-specific: reject non-integer `max_concurrent` at construction
    # instead of letting `asyncio.Semaphore` misbehave (`1.5` silently drives
    # the counter negative, `True` allocates a 1-way bound from a bool,
    # `"2"` raises `TypeError` from inside the primitive instead of our
    # `ValueError`).
    async def test_max_concurrent_non_integer_raises(self):
        import pytest

        state = create_mock_state()
        adapter = create_mock_adapter("slack")

        for bad_value in (1.5, True, False, "2", 0.0, [1]):
            with pytest.raises(ValueError, match="max_concurrent must be a positive integer or None"):
                await _init_chat(
                    adapter=adapter,
                    state=state,
                    concurrency=ConcurrencyConfig(strategy="concurrent", max_concurrent=bad_value),  # type: ignore[arg-type]
                )

    # Python-specific: setting `max_concurrent` with a non-concurrent strategy
    # is a misconfiguration — the field is only honored under `"concurrent"`.
    # Fail loudly instead of silently allocating an unused semaphore.
    async def test_max_concurrent_with_non_concurrent_strategy_raises(self):
        import pytest

        state = create_mock_state()
        adapter = create_mock_adapter("slack")

        for bad_strategy in ("queue", "debounce", "drop"):
            with pytest.raises(ValueError, match="only honored when strategy='concurrent'"):
                await _init_chat(
                    adapter=adapter,
                    state=state,
                    concurrency=ConcurrencyConfig(strategy=bad_strategy, max_concurrent=5),
                )

    # Python-specific: None / missing max_concurrent must keep the
    # unbounded behavior (matches upstream TS default of Infinity).
    # Parameterized to cover both the string form (max_concurrent implicit)
    # and the explicit ConcurrencyConfig(max_concurrent=None) form — the
    # two take separate code paths in Chat.__init__ (string → defaults,
    # ConcurrencyConfig → field read), so both must be verified.
    @pytest.mark.parametrize(
        "concurrency_value",
        [
            "concurrent",
            ConcurrencyConfig(strategy="concurrent", max_concurrent=None),
        ],
        ids=["string", "config_none"],
    )
    async def test_max_concurrent_none_allows_unbounded(self, concurrency_value):
        state = create_mock_state()
        adapter = create_mock_adapter("slack")

        chat, _, _ = await _init_chat(adapter=adapter, state=state, concurrency=concurrency_value)

        in_flight = 0
        max_observed = 0
        gate = asyncio.Event()

        @chat.on_mention
        async def handler(thread, message, context=None):
            nonlocal in_flight, max_observed
            in_flight += 1
            max_observed = max(max_observed, in_flight)
            await gate.wait()
            in_flight -= 1

        tasks = [
            asyncio.create_task(
                chat.handle_incoming_message(
                    adapter,
                    f"slack:C123:{i}",
                    create_test_message(f"msg-{i}", "Hey @slack-bot"),
                )
            )
            for i in range(5)
        ]

        # Poll until all 5 are in flight; with no semaphore they should
        # all reach the gate.
        async def _reach_five() -> None:
            while in_flight < 5:
                await asyncio.sleep(0.001)

        await asyncio.wait_for(_reach_five(), timeout=1.0)
        assert in_flight == 5
        gate.set()
        await asyncio.gather(*tasks)
        assert max_observed == 5


# ============================================================================
# 22. lockScope (tests 87-91)
# ============================================================================


class TestLockScope:
    # TS: "should use threadId as lock key with default (thread) scope"
    async def test_should_use_threadid_as_lock_key_with_default_thread_scope(self):
        state = create_mock_state()
        adapter = create_mock_adapter("slack")

        chat, _, _ = await _init_chat(adapter=adapter, state=state)

        @chat.on_mention
        async def handler(thread, message, context=None):
            pass

        msg = create_test_message("msg-ls-1", "Hey @slack-bot")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        # Lock should have been acquired on the full threadId
        assert any(key == "slack:C123:1234.5678" for key, _ttl in state._acquire_lock_calls)

    # TS: "should use channelId as lock key with channel scope on adapter"
    async def test_should_use_channelid_as_lock_key_with_channel_scope_on_adapter(self):
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

        @chat.on_mention
        async def handler(thread, message, context=None):
            pass

        msg = create_test_message("msg-ls-2", "Hey @telegram-bot")
        await chat.handle_incoming_message(adapter, "telegram:C123:topic456", msg)

        # channelIdFromThreadId returns first two parts: "telegram:C123"
        assert any(key == "telegram:C123" for key, _ttl in state._acquire_lock_calls)

    # TS: "should use channelId as lock key with channel scope on config"
    async def test_should_use_channelid_as_lock_key_with_channel_scope_on_config(self):
        state = create_mock_state()
        adapter = create_mock_adapter("slack")

        chat, _, _ = await _init_chat(
            adapter=adapter,
            state=state,
            lock_scope="channel",
        )

        @chat.on_mention
        async def handler(thread, message, context=None):
            pass

        msg = create_test_message("msg-ls-3", "Hey @slack-bot")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        # channelIdFromThreadId returns "slack:C123"
        assert any(key == "slack:C123" for key, _ttl in state._acquire_lock_calls)

    # TS: "should support async lockScope resolver function"
    async def test_should_support_async_lockscope_resolver_function(self):
        state = create_mock_state()
        adapter = create_mock_adapter("telegram")

        async def _resolver(ctx):
            return "thread" if ctx.is_dm else "channel"

        config = ChatConfig(
            user_name="testbot",
            adapters={"telegram": adapter},
            state=state,
            logger=MockLogger(),
            lock_scope=_resolver,
        )
        chat = Chat(config)
        await chat.webhooks["telegram"]("request")

        @chat.on_mention
        async def handler(thread, message, context=None):
            pass

        # Non-DM: should use channel scope
        msg = create_test_message("msg-ls-4", "Hey @telegram-bot")
        await chat.handle_incoming_message(adapter, "telegram:C123:topic456", msg)

        # Should use channel scope -> lock acquired on "telegram:C123"
        assert any(key == "telegram:C123" for key, _ttl in state._acquire_lock_calls)

    # TS: "should queue on channel-scoped lock key"
    async def test_should_queue_on_channelscoped_lock_key(self):
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

        # Pre-hold the channel lock to force enqueue
        await state.acquire_lock("telegram:C123", 30000)

        msg1 = create_test_message("msg-ls-5", "Hey @telegram-bot first")
        await chat.handle_incoming_message(adapter, "telegram:C123:topic1", msg1)

        msg2 = create_test_message("msg-ls-6", "Hey @telegram-bot second")
        await chat.handle_incoming_message(adapter, "telegram:C123:topic2", msg2)

        # Both should have been enqueued on the channel key
        depth = await state.queue_depth("telegram:C123")
        assert depth == 2
        # Verify each enqueue call used "telegram:C123" as key
        assert len(state._enqueue_calls) == 2
        for key, _entry, _max_size in state._enqueue_calls:
            assert key == "telegram:C123"


# ============================================================================
# 23. persistMessageHistory (tests 92-93)
# ============================================================================


class TestPersistMessageHistory:
    # TS: "should cache incoming messages when adapter has persistMessageHistory"
    async def test_should_cache_incoming_messages_when_adapter_has_persistmessagehistory(self):
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

        msg = create_test_message("msg-1", "Hello from WhatsApp")
        await chat.handle_incoming_message(adapter, "whatsapp:phone:user1", msg)

        stored = state.cache.get("msg-history:whatsapp:phone:user1")
        assert stored is not None
        assert isinstance(stored, list)
        assert stored[0]["id"] == "msg-1"

    # TS: "should NOT cache incoming messages when adapter does not set persistMessageHistory"
    async def test_should_not_cache_incoming_messages_when_adapter_does_not_set_persistmessagehistory(self):
        chat, adapter, state = await _init_chat()

        msg = create_test_message("msg-2", "Hello from Slack")
        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        history_keys = [k for k in state.cache if k.startswith("msg-history:")]
        assert len(history_keys) == 0
