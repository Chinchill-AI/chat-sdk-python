"""Integration tests for action (button click) event handling.

Verifies that on_action handlers fire correctly, that action_id filtering
works, and that the event carries the correct user, value, and triggerId.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from chat_sdk.types import ActionEvent, Author

from .conftest import create_chat


class TestActionFlow:
    """End-to-end tests for button click / action event handling."""

    def _make_action(
        self,
        adapter: Any,
        action_id: str = "approve",
        value: str | None = "order-123",
        trigger_id: str | None = "T12345",
        is_me: bool = False,
        thread_id: str = "slack:C123:1234.5678",
    ) -> ActionEvent:
        return ActionEvent(
            action_id=action_id,
            value=value,
            trigger_id=trigger_id,
            user=Author(
                user_id="BOT" if is_me else "U456",
                user_name="testbot" if is_me else "alice",
                full_name="Test Bot" if is_me else "Alice",
                is_bot=is_me,
                is_me=is_me,
            ),
            message_id="msg-1",
            thread_id=thread_id,
            adapter=adapter,
            thread=None,
            raw={"payload": "raw-action-data"},
        )

    @pytest.mark.asyncio
    async def test_action_triggers_catch_all_handler(self):
        """An action event fires a catch-all on_action handler."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        calls: list[ActionEvent] = []

        chat.on_action(lambda event: calls.append(event))

        event = self._make_action(adapter)
        chat.process_action(event)
        await asyncio.sleep(0.05)

        assert len(calls) == 1
        received = calls[0]
        assert received.action_id == "approve"
        assert received.value == "order-123"
        assert received.user.user_name == "alice"

    @pytest.mark.asyncio
    async def test_filtered_action_handler_matches_specific_action_id(self):
        """Handler registered with action_ids only fires for matching actions."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        calls: list[ActionEvent] = []

        chat.on_action(["approve", "reject"], lambda event: calls.append(event))

        approve = self._make_action(adapter, "approve")
        skip = self._make_action(adapter, "skip")

        chat.process_action(approve)
        chat.process_action(skip)
        await asyncio.sleep(0.05)

        assert len(calls) == 1
        assert calls[0].action_id == "approve"

    @pytest.mark.asyncio
    async def test_action_event_has_correct_user(self):
        """The action event carries the correct user information."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        calls: list[ActionEvent] = []

        chat.on_action(lambda event: calls.append(event))

        event = self._make_action(adapter)
        chat.process_action(event)
        await asyncio.sleep(0.05)

        assert len(calls) == 1
        assert calls[0].user.user_id == "U456"
        assert calls[0].user.user_name == "alice"
        assert calls[0].user.is_bot is False

    @pytest.mark.asyncio
    async def test_action_event_has_correct_value(self):
        """The action event value field matches what was sent."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        calls: list[ActionEvent] = []

        chat.on_action(lambda event: calls.append(event))

        event = self._make_action(adapter, value="item-456")
        chat.process_action(event)
        await asyncio.sleep(0.05)

        assert len(calls) == 1
        assert calls[0].value == "item-456"

    @pytest.mark.asyncio
    async def test_action_event_has_trigger_id(self):
        """The action event carries the triggerId for modal opening."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        calls: list[ActionEvent] = []

        chat.on_action(lambda event: calls.append(event))

        event = self._make_action(adapter, trigger_id="T99999")
        chat.process_action(event)
        await asyncio.sleep(0.05)

        assert len(calls) == 1
        assert calls[0].trigger_id == "T99999"

    @pytest.mark.asyncio
    async def test_action_event_has_thread(self):
        """The action event gets a Thread object when thread_id is set."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        calls: list[ActionEvent] = []

        chat.on_action(lambda event: calls.append(event))

        event = self._make_action(adapter)
        chat.process_action(event)
        await asyncio.sleep(0.05)

        assert len(calls) == 1
        assert calls[0].thread is not None
        assert calls[0].thread.id == "slack:C123:1234.5678"

    @pytest.mark.asyncio
    async def test_action_from_self_is_ignored(self):
        """Actions from the bot itself are not dispatched."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        calls: list[ActionEvent] = []

        chat.on_action(lambda event: calls.append(event))

        event = self._make_action(adapter, is_me=True)
        chat.process_action(event)
        await asyncio.sleep(0.05)

        assert len(calls) == 0

    @pytest.mark.asyncio
    async def test_multiple_action_handlers(self):
        """Both catch-all and filtered handlers fire for a matching action."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        all_calls: list[ActionEvent] = []
        approve_calls: list[ActionEvent] = []

        async def all_handler(event: ActionEvent):
            all_calls.append(event)

        async def approve_handler(event: ActionEvent):
            approve_calls.append(event)

        chat.on_action(all_handler)
        chat.on_action(["approve"], approve_handler)

        event = self._make_action(adapter, "approve")
        chat.process_action(event)
        await asyncio.sleep(0.05)

        assert len(all_calls) == 1
        assert len(approve_calls) == 1

    @pytest.mark.asyncio
    async def test_action_handler_can_post_to_thread(self):
        """An action handler can post a response via the thread."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]

        async def handler(event: ActionEvent):
            if event.thread:
                await event.thread.post(f"Action {event.action_id} received!")

        chat.on_action(handler)

        event = self._make_action(adapter, "approve")
        chat.process_action(event)
        await asyncio.sleep(0.05)

        assert len(adapter._post_calls) == 1
        assert "Action approve received!" in str(adapter._post_calls[0][1])

    @pytest.mark.asyncio
    async def test_decorator_style_action_handler(self):
        """The decorator style @chat.on_action('id') works."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        calls: list[ActionEvent] = []

        @chat.on_action("approve")
        async def handler(event: ActionEvent):
            calls.append(event)

        event = self._make_action(adapter, "approve")
        chat.process_action(event)
        await asyncio.sleep(0.05)

        assert len(calls) == 1
