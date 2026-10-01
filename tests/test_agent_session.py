"""Port of upstream ``packages/chat/src/agent-session.test.ts`` (chat@4.39+, #201)."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

from chat_sdk.chat import Chat
from chat_sdk.context import active_conversation
from chat_sdk.testing import MockLogger, create_mock_adapter, create_mock_state
from chat_sdk.types import (
    AgentSessionStoppedEvent,
    AgentSessionTitleChangedEvent,
    ChatConfig,
    WebhookOptions,
)


def _make_chat() -> tuple[Chat, Any]:
    adapter = create_mock_adapter("slack")
    chat = Chat(ChatConfig(user_name="bot", adapters={"slack": adapter}, state=create_mock_state(), logger="error"))
    return chat, adapter


def _capture_wait_until() -> tuple[WebhookOptions, list[Any]]:
    tasks: list[Any] = []
    return WebhookOptions(wait_until=tasks.append), tasks


class TestAgentSessionEvents:
    """describe("agent session events")"""

    # it("dispatches stop events to registered handlers")
    async def test_dispatches_stop_events_to_registered_handlers(self):
        chat, adapter = _make_chat()
        handler = AsyncMock()
        chat.on_agent_session_stopped(handler)
        event = AgentSessionStoppedEvent(
            adapter=adapter,
            channel_id="D1",
            streaming_message_ts=["2.3"],
            thread_id="slack:D1:1.2",
            thread_ts="1.2",
            user_id="U1",
        )
        options, tasks = _capture_wait_until()

        chat.process_agent_session_stopped(event, options)
        assert len(tasks) == 1
        await tasks[0]

        handler.assert_awaited_once_with(event)

    # it("dispatches title changes to registered handlers")
    async def test_dispatches_title_changes_to_registered_handlers(self):
        chat, adapter = _make_chat()
        handler = AsyncMock()
        chat.on_agent_session_title_changed(handler)
        event = AgentSessionTitleChangedEvent(
            adapter=adapter,
            channel_id="D1",
            previous_title="Old title",
            thread_id="slack:D1:1.2",
            thread_ts="1.2",
            title="New title",
            user_id="U1",
        )
        options, tasks = _capture_wait_until()

        chat.process_agent_session_title_changed(event, options)
        assert len(tasks) == 1
        await tasks[0]

        handler.assert_awaited_once_with(event)


class TestAgentSessionDispatchPython:
    """Python-specific: error isolation, conversation scope and decorator use."""

    async def test_stop_handler_error_is_logged_and_wait_until_task_completes(self):
        adapter = create_mock_adapter("slack")
        mock_logger = MockLogger()
        chat = Chat(
            ChatConfig(user_name="bot", adapters={"slack": adapter}, state=create_mock_state(), logger=mock_logger)
        )
        later = AsyncMock()

        @chat.on_agent_session_stopped
        async def failing(event: AgentSessionStoppedEvent) -> None:
            raise RuntimeError("boom")

        chat.on_agent_session_stopped(later)
        event = AgentSessionStoppedEvent(
            adapter=adapter,
            channel_id="D1",
            streaming_message_ts=[],
            thread_id="slack:D1:1.2",
            thread_ts="1.2",
            user_id="U1",
        )
        options, tasks = _capture_wait_until()

        chat.process_agent_session_stopped(event, options)
        await tasks[0]  # the wait_until task completes despite the handler error
        await asyncio.sleep(0)  # let the handler task's done-callback log

        # As upstream, a raising handler stops the ones after it.
        later.assert_not_awaited()
        errors = [call for call in mock_logger.error.calls if call[0] == "Agent session stopped handler error"]
        assert len(errors) == 1
        assert isinstance(errors[0][1]["error"], RuntimeError)
        assert errors[0][1]["thread_id"] == "slack:D1:1.2"

    async def test_title_change_handlers_run_in_the_event_conversation(self):
        chat, adapter = _make_chat()
        seen: list[Any] = []

        @chat.on_agent_session_title_changed
        def sync_handler(event: AgentSessionTitleChangedEvent) -> None:
            seen.append(active_conversation())

        options, tasks = _capture_wait_until()
        chat.process_agent_session_title_changed(
            AgentSessionTitleChangedEvent(
                adapter=adapter,
                channel_id="D1",
                thread_id="slack:D1:9.9",
                thread_ts="9.9",
                title="T",
                user_id="U1",
            ),
            options,
        )
        await tasks[0]

        assert seen == ["slack:D1:9.9"]
