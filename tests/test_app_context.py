"""Port of upstream ``packages/chat/src/app-context.test.ts``."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from chat_sdk.chat import Chat
from chat_sdk.context import active_conversation
from chat_sdk.testing import MockLogger, create_mock_adapter, create_mock_state
from chat_sdk.types import (
    AppContextChangedEvent,
    AppContextChannelEntity,
    ChatConfig,
    WebhookOptions,
)


def _make_chat() -> tuple[Chat, Any, MockLogger]:
    adapter = create_mock_adapter("mock")
    logger = MockLogger()
    chat = Chat(ChatConfig(user_name="bot", adapters={"mock": adapter}, state=create_mock_state(), logger=logger))
    return chat, adapter, logger


def _event(adapter: Any) -> AppContextChangedEvent:
    return AppContextChangedEvent(
        adapter=adapter,
        channel_id="D1",
        user_id="U1",
        entities=[AppContextChannelEntity(channel_id="C2")],
        raw={},
    )


class TestOnAppContextChanged:
    # TS: "dispatches app_context_changed events to registered handlers"
    async def test_dispatches_appcontextchanged_events_to_registered_handlers(self):
        chat, adapter, _ = _make_chat()
        handler = AsyncMock()
        chat.on_app_context_changed(handler)

        event = _event(adapter)
        tasks: list[Any] = []
        chat.process_app_context_changed(event, WebhookOptions(wait_until=tasks.append))
        assert len(tasks) == 1
        await tasks[0]

        handler.assert_awaited_once_with(event)
        assert event.entities[0].kind == "channel"

    # Python-specific: handlers run under conversation(event.channel_id), and
    # a failure is logged while wait_until's task still completes.
    async def test_runs_in_channel_conversation_and_logs_errors(self):
        chat, adapter, logger = _make_chat()
        seen: list[str | None] = []

        def failing(_event: AppContextChangedEvent) -> None:
            seen.append(active_conversation())
            raise RuntimeError("boom")

        chat.on_app_context_changed(failing)
        tasks: list[Any] = []
        chat.process_app_context_changed(_event(adapter), WebhookOptions(wait_until=tasks.append))
        assert await tasks[0] is None

        assert seen == ["D1"]
        assert logger.error.calls == [("App context changed handler error", {"error": "boom", "user_id": "U1"})]
