"""Python-specific coverage for message update/delete lifecycle events.

The upstream ``chat.test.ts`` cases live in ``tests/test_chat_faithful.py``
(``TestMessageLifecycleEvents``); these cover the Python port's own edges:
self-edit skipping, lazy factories, the active conversation, routing
isolation, identity resolution, error/``wait_until`` handling and the
``platform`` normalization.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from chat_sdk.chat import Chat
from chat_sdk.context import active_conversation
from chat_sdk.testing import MockLogger, create_mock_adapter, create_mock_state, create_test_message
from chat_sdk.types import Author, ChatConfig, MessageDeletedEvent, WebhookOptions

THREAD_ID = "slack:C123:1234.5678"


async def _init_chat(**overrides: Any) -> tuple[Chat, Any, Any, MockLogger]:
    adapter = create_mock_adapter("slack")
    state = create_mock_state()
    logger = MockLogger()
    chat = Chat(ChatConfig(user_name="testbot", adapters={"slack": adapter}, state=state, logger=logger, **overrides))
    await chat.webhooks["slack"]("request")
    return chat, adapter, state, logger


def _delete_event(adapter: Any, **overrides: Any) -> MessageDeletedEvent:
    fields: dict[str, Any] = {
        "adapter": adapter,
        "channel_id": "C123",
        "message_id": "1234.5678",
        "raw": {},
        "thread_id": THREAD_ID,
    }
    fields.update(overrides)
    return MessageDeletedEvent(**fields)


class TestMessageUpdated:
    async def test_skips_the_bots_own_edits(self):
        chat, adapter, _, _ = await _init_chat()
        handler = AsyncMock()
        chat.on_message_updated(handler)
        own = create_test_message(
            "m1",
            "streamed delta",
            author=Author(user_id="BOT", user_name="testbot", full_name="Bot", is_bot=True, is_me=True),
        )

        await chat.process_message_updated(adapter, THREAD_ID, own)

        handler.assert_not_awaited()

    async def test_resolves_lazy_message_and_previous_message_factories(self):
        chat, adapter, _, _ = await _init_chat()
        handler = AsyncMock()
        chat.on_message_updated(handler)
        message = create_test_message("m1", "edited")
        previous = create_test_message("m1", "original")
        message_factory = AsyncMock(return_value=message)
        previous_factory = AsyncMock(return_value=previous)

        await chat.process_message_updated(adapter, THREAD_ID, message_factory, previous_factory)

        message_factory.assert_awaited_once_with()
        previous_factory.assert_awaited_once_with()
        _, received, received_previous = handler.await_args.args
        assert received is message
        assert received_previous is previous

    async def test_previous_message_defaults_to_none(self):
        chat, adapter, _, _ = await _init_chat()
        handler = AsyncMock()
        chat.on_message_updated(handler)

        await chat.process_message_updated(adapter, THREAD_ID, create_test_message("m1", "edited"))

        assert handler.await_args.args[2] is None

    async def test_runs_in_thread_conversation_without_reaching_subscribed_handlers(self):
        chat, adapter, state, _ = await _init_chat()
        await state.subscribe(THREAD_ID)
        subscribed = AsyncMock()
        chat.on_subscribed_message(subscribed)
        seen: list[tuple[str | None, bool]] = []

        async def handler(thread: Any, _message: Any, _previous: Any) -> None:
            # The Thread carries the dispatch-time subscription snapshot (as
            # upstream createThread(..., isSubscribed)), not a fresh lookup.
            await state.unsubscribe(THREAD_ID)
            seen.append((active_conversation(), await thread.is_subscribed()))

        chat.on_message_updated(handler)

        await chat.process_message_updated(adapter, THREAD_ID, create_test_message("m1", "edited"))

        assert seen == [(THREAD_ID, True)]
        subscribed.assert_not_awaited()

    async def test_bypasses_deduplication(self):
        chat, adapter, _, _ = await _init_chat()
        handler = AsyncMock()
        chat.on_message_updated(handler)
        message = create_test_message("m1", "edited")

        await chat.process_message_updated(adapter, THREAD_ID, message)
        await chat.process_message_updated(adapter, THREAD_ID, message)

        assert handler.await_count == 2

    async def test_resolves_identity_before_handlers(self):
        chat, adapter, _, _ = await _init_chat(identity=lambda ctx: f"key:{ctx.author.user_id}")
        keys: list[str | None] = []

        def handler(_thread: Any, message: Any, _previous: Any) -> None:
            keys.append(message.user_key)

        chat.on_message_updated(handler)

        await chat.process_message_updated(adapter, THREAD_ID, create_test_message("m1", "edited"))

        assert keys == ["key:U123"]

    async def test_handler_error_raises_from_task_but_wait_until_task_completes(self):
        chat, adapter, _, logger = await _init_chat()

        def failing(*_args: Any) -> None:
            raise RuntimeError("boom")

        chat.on_message_updated(failing)
        waited: list[Any] = []

        task = chat.process_message_updated(
            adapter,
            THREAD_ID,
            create_test_message("m1", "edited"),
            # Upstream hands waitUntil the tracked task even when
            # propagateHandlerErrors is set (only message/action/slash honour it).
            options=WebhookOptions(wait_until=waited.append, propagate_handler_errors=True),
        )

        with pytest.raises(RuntimeError, match="boom"):
            await task
        assert await waited[0] is None
        assert ("Message update processing error", {"thread_id": THREAD_ID, "error": "boom"}) in logger.error.calls


class TestMessageDeleted:
    async def test_runs_in_thread_conversation(self):
        chat, adapter, _, _ = await _init_chat()
        seen: list[str | None] = []
        chat.on_message_deleted(lambda _event: seen.append(active_conversation()))

        await chat.process_message_deleted(_delete_event(adapter))

        assert seen == [THREAD_ID]

    async def test_keeps_adapter_supplied_platform(self):
        chat, adapter, _, _ = await _init_chat()
        handler = AsyncMock()
        chat.on_message_deleted(handler)
        event = _delete_event(adapter, platform="slack-enterprise")

        await chat.process_message_deleted(event)

        handler.assert_awaited_once()
        assert handler.await_args.args[0] is event

    async def test_handler_error_is_logged_and_wait_until_task_completes(self):
        chat, adapter, _, logger = await _init_chat()

        def failing(_event: Any) -> None:
            raise RuntimeError("gone")

        chat.on_message_deleted(failing)
        waited: list[Any] = []

        task = chat.process_message_deleted(_delete_event(adapter), WebhookOptions(wait_until=waited.append))

        with pytest.raises(RuntimeError, match="gone"):
            await task
        assert await waited[0] is None
        assert (
            "Message delete processing error",
            {"thread_id": THREAD_ID, "message_id": "1234.5678", "error": "gone"},
        ) in logger.error.calls
