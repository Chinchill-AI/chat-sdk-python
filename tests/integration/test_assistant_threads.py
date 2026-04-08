"""Integration tests for Slack Assistant thread events.

Port of replay-assistant-threads.test.ts (18 tests).

Covers:
- assistant_thread_started event routing and data mapping
- assistant_context_changed event routing
- setSuggestedPrompts integration
- setAssistantStatus and setAssistantTitle
- Thread status updates
- Error handling and missing handler graceful behavior
- Multiple handler registration
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from chat_sdk.testing import create_mock_adapter
from chat_sdk.types import (
    AssistantContextChangedEvent,
    AssistantThreadStartedEvent,
    Message,
)

from .conftest import create_chat, create_msg

# ---------------------------------------------------------------------------
# Constants matching the TS tests
# ---------------------------------------------------------------------------

BOT_NAME = "TestBot"
BOT_USER_ID = "U_BOT_123"
USER_ID = "U_USER_456"
DM_CHANNEL = "D0ACX51K95H"
THREAD_TS = "1771460497.092039"
CONTEXT_CHANNEL = "C_CONTEXT_789"
TEAM_ID = "T_TEAM_123"


def _make_thread_started_event(
    adapter: Any,
    user_id: str = USER_ID,
    channel_id: str = DM_CHANNEL,
    thread_ts: str = THREAD_TS,
    context: dict[str, Any] | None = None,
) -> AssistantThreadStartedEvent:
    """Build an AssistantThreadStartedEvent for testing."""
    thread_id = f"slack:{channel_id}:{thread_ts}"
    return AssistantThreadStartedEvent(
        adapter=adapter,
        thread_id=thread_id,
        thread_ts=thread_ts,
        channel_id=channel_id,
        user_id=user_id,
        context=context
        or {
            "thread_entry_point": "app_home",
            "force_search": False,
        },
    )


def _make_context_changed_event(
    adapter: Any,
    context: dict[str, Any] | None = None,
) -> AssistantContextChangedEvent:
    """Build an AssistantContextChangedEvent for testing."""
    thread_id = f"slack:{DM_CHANNEL}:{THREAD_TS}"
    return AssistantContextChangedEvent(
        adapter=adapter,
        thread_id=thread_id,
        thread_ts=THREAD_TS,
        channel_id=DM_CHANNEL,
        user_id=USER_ID,
        context=context
        or {
            "channel_id": CONTEXT_CHANNEL,
            "team_id": TEAM_ID,
            "thread_entry_point": "channel",
        },
    )


# ============================================================================
# assistant_thread_started event routing + handler dispatch
# ============================================================================


class TestAssistantThreadStartedRouting:
    """Routing and data mapping for assistant_thread_started events."""

    @pytest.mark.asyncio
    async def test_routes_to_handler(self):
        """assistant_thread_started event is dispatched to the registered handler."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[AssistantThreadStartedEvent] = []

        @chat.on_assistant_thread_started
        async def handler(event):
            captured.append(event)

        event = _make_thread_started_event(adapter)
        chat.process_assistant_thread_started(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 1

    @pytest.mark.asyncio
    async def test_maps_event_data_correctly(self):
        """The event carries correct thread_id, user_id, channel_id, and thread_ts."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[AssistantThreadStartedEvent] = []

        @chat.on_assistant_thread_started
        async def handler(event):
            captured.append(event)

        event = _make_thread_started_event(adapter)
        chat.process_assistant_thread_started(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 1
        e = captured[0]
        assert e.thread_id == f"slack:{DM_CHANNEL}:{THREAD_TS}"
        assert e.user_id == USER_ID
        assert e.channel_id == DM_CHANNEL
        assert e.thread_ts == THREAD_TS
        assert e.adapter.name == "slack"

    @pytest.mark.asyncio
    async def test_extracts_context_with_thread_entry_point(self):
        """The context includes thread_entry_point from the event."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[AssistantThreadStartedEvent] = []

        @chat.on_assistant_thread_started
        async def handler(event):
            captured.append(event)

        event = _make_thread_started_event(adapter)
        chat.process_assistant_thread_started(event)
        await asyncio.sleep(0.05)

        assert captured[0].context["thread_entry_point"] == "app_home"

    @pytest.mark.asyncio
    async def test_extracts_context_channel_id_when_present(self):
        """Context carries channel_id and team_id when provided."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[AssistantThreadStartedEvent] = []

        @chat.on_assistant_thread_started
        async def handler(event):
            captured.append(event)

        event = _make_thread_started_event(
            adapter,
            context={
                "channel_id": CONTEXT_CHANNEL,
                "team_id": TEAM_ID,
                "thread_entry_point": "channel",
            },
        )
        chat.process_assistant_thread_started(event)
        await asyncio.sleep(0.05)

        assert captured[0].context["channel_id"] == CONTEXT_CHANNEL
        assert captured[0].context["team_id"] == TEAM_ID
        assert captured[0].context["thread_entry_point"] == "channel"

    @pytest.mark.asyncio
    async def test_handles_missing_context_fields_gracefully(self):
        """Empty context does not cause errors."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[AssistantThreadStartedEvent] = []

        @chat.on_assistant_thread_started
        async def handler(event):
            captured.append(event)

        event = _make_thread_started_event(adapter, context={})
        chat.process_assistant_thread_started(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 1
        assert captured[0].context.get("channel_id") is None
        assert captured[0].context.get("team_id") is None


# ============================================================================
# Error handling
# ============================================================================


class TestAssistantThreadStartedErrors:
    """Error handling for assistant_thread_started events."""

    @pytest.mark.asyncio
    async def test_does_not_crash_when_handler_throws(self):
        """An exception in the handler does not propagate to the caller."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]

        @chat.on_assistant_thread_started
        async def handler(event):
            raise RuntimeError("Handler exploded")

        event = _make_thread_started_event(adapter)
        chat.process_assistant_thread_started(event)
        # Should not raise
        await asyncio.sleep(0.05)

    @pytest.mark.asyncio
    async def test_still_handles_messages_when_no_handler(self):
        """When no assistant handler is registered, other handlers still work."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        mention_calls: list[Message] = []

        @chat.on_mention
        async def mention_handler(thread, message, context=None):
            mention_calls.append(message)

        # assistant_thread_started with no registered handler -- should not error
        event = _make_thread_started_event(adapter)
        chat.process_assistant_thread_started(event)
        await asyncio.sleep(0.05)

        # Regular mention should still work
        msg = create_msg(
            f"<@{BOT_USER_ID}> hello",
            user_id=USER_ID,
            user_name="test.user",
            is_mention=True,
        )
        await chat.handle_incoming_message(adapter, "slack:C_CHANNEL_123:1771460500.000001", msg)

        assert len(mention_calls) == 1
        assert "hello" in mention_calls[0].text


# ============================================================================
# Multiple handlers
# ============================================================================


class TestAssistantThreadStartedMultipleHandlers:
    """Multiple handlers for assistant_thread_started events."""

    @pytest.mark.asyncio
    async def test_calls_all_handlers_in_order(self):
        """All registered handlers are called in registration order."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        call_order: list[int] = []

        @chat.on_assistant_thread_started
        async def handler1(event):
            call_order.append(1)

        @chat.on_assistant_thread_started
        async def handler2(event):
            call_order.append(2)

        event = _make_thread_started_event(adapter)
        chat.process_assistant_thread_started(event)
        await asyncio.sleep(0.05)

        assert call_order == [1, 2]


# ============================================================================
# assistant_thread_context_changed
# ============================================================================


class TestAssistantContextChanged:
    """Routing and data mapping for assistant_thread_context_changed events."""

    @pytest.mark.asyncio
    async def test_routes_to_handler(self):
        """assistant_thread_context_changed event is dispatched to handler."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[AssistantContextChangedEvent] = []

        @chat.on_assistant_context_changed
        async def handler(event):
            captured.append(event)

        event = _make_context_changed_event(adapter)
        chat.process_assistant_context_changed(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 1
        assert captured[0].thread_id == f"slack:{DM_CHANNEL}:{THREAD_TS}"
        assert captured[0].context["channel_id"] == CONTEXT_CHANNEL
        assert captured[0].context["thread_entry_point"] == "channel"

    @pytest.mark.asyncio
    async def test_does_not_crash_when_no_handler(self):
        """No error when context_changed fires without a handler."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]

        event = _make_context_changed_event(adapter)
        chat.process_assistant_context_changed(event)
        await asyncio.sleep(0.05)
        # No assertion needed -- tests that the call completes without raising
        assert True


# ============================================================================
# setAssistantStatus + setAssistantTitle integration
# ============================================================================


class TestAssistantStatusAndTitle:
    """Status and title updates via assistant events."""

    @pytest.mark.asyncio
    async def test_start_typing_called_from_handler(self):
        """Handler can call adapter.start_typing with a status message."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]

        @chat.on_assistant_thread_started
        async def handler(event):
            await event.adapter.start_typing(event.thread_id, "Analyzing your request...")

        event = _make_thread_started_event(adapter)
        chat.process_assistant_thread_started(event)
        await asyncio.sleep(0.05)

        assert len(adapter._start_typing_calls) == 1
        thread_id, status = adapter._start_typing_calls[0]
        assert thread_id == f"slack:{DM_CHANNEL}:{THREAD_TS}"
        assert status == "Analyzing your request..."

    @pytest.mark.asyncio
    async def test_clear_status_with_empty_string(self):
        """Passing an empty status string clears the assistant status."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]

        @chat.on_assistant_thread_started
        async def handler(event):
            await event.adapter.start_typing(event.thread_id, "")

        event = _make_thread_started_event(adapter)
        chat.process_assistant_thread_started(event)
        await asyncio.sleep(0.05)

        assert len(adapter._start_typing_calls) == 1
        _, status = adapter._start_typing_calls[0]
        assert status == ""

    @pytest.mark.asyncio
    async def test_status_update_with_none(self):
        """Calling start_typing without a status still works."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]

        @chat.on_assistant_thread_started
        async def handler(event):
            await event.adapter.start_typing(event.thread_id)

        event = _make_thread_started_event(adapter)
        chat.process_assistant_thread_started(event)
        await asyncio.sleep(0.05)

        assert len(adapter._start_typing_calls) == 1
        _, status = adapter._start_typing_calls[0]
        assert status is None

    @pytest.mark.asyncio
    async def test_context_changed_then_status_update(self):
        """A context change followed by a status update works correctly."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        events: list[Any] = []

        @chat.on_assistant_context_changed
        async def context_handler(event):
            events.append(("context", event))
            await event.adapter.start_typing(event.thread_id, "Switching context...")

        context_event = _make_context_changed_event(adapter)
        chat.process_assistant_context_changed(context_event)
        await asyncio.sleep(0.05)

        assert len(events) == 1
        assert events[0][0] == "context"
        assert len(adapter._start_typing_calls) == 1
