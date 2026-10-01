"""Port of upstream ``packages/chat/src/installation-events.test.ts``.

Upstream runs the suite under ``describe.each(["Installed", "Uninstalled"])``;
here every test is parametrized over both kinds.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from chat_sdk.chat import Chat
from chat_sdk.context import active_conversation
from chat_sdk.testing import MockLogger, create_mock_adapter, create_mock_chat_instance, create_mock_state
from chat_sdk.types import ChatConfig, ChatInstance, InstalledEvent, UninstalledEvent, WebhookOptions

KINDS = ["Installed", "Uninstalled"]
_UNSET: Any = object()


class _Setup:
    def __init__(self, kind: str, channel_id: str | None = _UNSET) -> None:
        if channel_id is _UNSET:
            channel_id = "teams:conversation:service"
        self.adapter = create_mock_adapter("teams")
        self.logger = MockLogger()
        self.chat = Chat(
            ChatConfig(
                user_name="bot",
                adapters={"teams": self.adapter},
                state=create_mock_state(),
                logger=self.logger,
            )
        )
        base: dict[str, Any] = {
            "adapter": self.adapter,
            "channel_id": channel_id,
            "conversation_id": "personal-conversation",
            "id": "installation-activity",
            "user_id": "installer",
            "tenant_id": "tenant",
            "locale": "en-US",
            "raw": {},
        }
        self.kind = kind
        self.installed = InstalledEvent(action="add", **base)
        self.uninstalled = UninstalledEvent(action="remove", **base)
        self.event = self.installed if kind == "Installed" else self.uninstalled

    def on(self, handler: Any) -> None:
        if self.kind == "Installed":
            self.chat.on_installed(handler)
        else:
            self.chat.on_uninstalled(handler)

    def process(self, options: WebhookOptions | None = None) -> None:
        if self.kind == "Installed":
            self.chat.process_installed(self.installed, options)
        else:
            self.chat.process_uninstalled(self.uninstalled, options)


def _collecting_options() -> tuple[WebhookOptions, list[Any]]:
    tasks: list[Any] = []
    return WebhookOptions(wait_until=tasks.append), tasks


@pytest.mark.parametrize("kind", KINDS)
class TestInstallationEvents:
    # TS: "runs registered handlers in order with the destination context"
    async def test_runs_registered_handlers_in_order_with_the_destination_context(self, kind: str):
        s = _Setup(kind)
        order: list[int] = []
        seen: list[Any] = []

        async def first(received: Any) -> None:
            await asyncio.sleep(0)
            seen.append((received, active_conversation()))
            order.append(1)

        def second(_received: Any) -> None:
            order.append(2)

        s.on(first)
        s.on(second)
        options, tasks = _collecting_options()
        s.process(options)
        assert len(tasks) == 1
        await asyncio.gather(*tasks)
        assert order == [1, 2]
        assert seen[0][0] is s.event
        assert seen[0][1] == "teams:conversation:service"

    # TS: "waits for asynchronous handlers, including events without a destination"
    async def test_waits_for_asynchronous_handlers_including_events_without_a_destination(self, kind: str):
        s = _Setup(kind, channel_id=None)
        gate = asyncio.Event()
        completed = False
        conversations: list[str | None] = []

        async def handler(_event: Any) -> None:
            nonlocal completed
            conversations.append(active_conversation())
            await gate.wait()
            completed = True

        s.on(handler)
        options, tasks = _collecting_options()
        s.process(options)
        # Let the handler start and block on the gate.
        for _ in range(3):
            await asyncio.sleep(0)
        assert conversations == [None]
        assert completed is False
        gate.set()
        await asyncio.gather(*tasks)
        assert completed is True

    # TS: "logs handler errors and resolves the background task"
    async def test_logs_handler_errors_and_resolves_the_background_task(self, kind: str):
        s = _Setup(kind)
        error = RuntimeError("handler failed")

        def failing(_event: Any) -> None:
            raise error

        s.on(failing)
        options, tasks = _collecting_options()
        s.process(options)
        assert await asyncio.gather(*tasks) == [None]
        assert s.logger.error.calls == [
            (
                f"{kind} handler error",
                {"error": error, "conversation_id": s.event.conversation_id, "activity_id": s.event.id},
            )
        ]

    # TS: "does nothing without handlers and runs without waitUntil"
    async def test_does_nothing_without_handlers_and_runs_without_waituntil(self, kind: str):
        s = _Setup(kind)
        options, tasks = _collecting_options()
        s.process(options)
        assert tasks == []
        assert s.chat._active_tasks == set()

        handler = MagicMock()
        s.on(handler)
        s.process()
        assert len(s.chat._active_tasks) == 1
        await asyncio.gather(*s.chat._active_tasks)
        handler.assert_called_once_with(s.event)

    # Python-specific: cancelling the task wait_until received (a host timeout)
    # must not cancel the handlers; a JS promise cannot be cancelled.
    async def test_cancelling_wait_until_task_does_not_cancel_handlers(self, kind: str):
        s = _Setup(kind)
        gate = asyncio.Event()
        completed = False

        async def handler(_event: Any) -> None:
            nonlocal completed
            await gate.wait()
            completed = True

        s.on(handler)
        options, tasks = _collecting_options()
        s.process(options)
        await asyncio.sleep(0)
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        gate.set()
        await asyncio.gather(*s.chat._active_tasks)
        assert completed is True

    # Python-specific: as upstream, a raising handler stops the handlers after it.
    async def test_handler_error_stops_later_handlers(self, kind: str):
        s = _Setup(kind)
        later = MagicMock()

        def failing(_event: Any) -> None:
            raise RuntimeError("boom")

        s.on(failing)
        s.on(later)
        options, tasks = _collecting_options()
        s.process(options)
        await asyncio.gather(*tasks)
        later.assert_not_called()


# Replaces upstream ``packages/tests/src/installation-matcher.test.ts``
# ("includes processInstalled" / "includes processUninstalled"), whose
# ``toHaveDispatched`` matcher has no Python equivalent.
def test_mock_chat_instance_records_installation_processors():
    chat = create_mock_chat_instance()
    adapter = create_mock_adapter("teams")
    installed = InstalledEvent(action="add", adapter=adapter, conversation_id="c", id="a", raw={})
    uninstalled = UninstalledEvent(action="remove", adapter=adapter, conversation_id="c", id="a", raw={})

    chat.process_installed(installed)
    chat.process_uninstalled(uninstalled)

    chat.process_installed.assert_called_once_with(installed)
    chat.process_uninstalled.assert_called_once_with(uninstalled)
    # Not a bare MagicMock: unknown processors are absent, like on a real Chat.
    assert getattr(chat, "process_not_a_real_event", None) is None


# Python-specific: the create_mock_chat_instance contract adapters rely on
# (upstream factories.ts resolves these three processors to undefined).
async def test_mock_chat_instance_async_processors_overrides_and_accessors():
    state = create_mock_state()
    sentinel = MagicMock(name="custom_process_message")
    chat = create_mock_chat_instance(state=state, user_name="helper-bot", overrides={"process_message": sentinel})

    assert await chat.handle_incoming_message("adapter", "thread", "message") is None
    assert await chat.abort_turn("slack:C1:1.2") is None
    chat.abort_turn.assert_awaited_once_with("slack:C1:1.2")
    assert await chat.process_options_load("event") is None
    assert await chat.process_modal_submit("event") is None
    chat.handle_incoming_message.assert_awaited_once_with("adapter", "thread", "message")
    chat.process_options_load.assert_awaited_once_with("event")
    chat.process_modal_submit.assert_awaited_once_with("event")
    assert chat.process_message is sentinel
    assert chat.get_state() is state
    assert chat.get_user_name() == "helper-bot"
    assert isinstance(chat, ChatInstance)
    assert chat.transcripts is chat.history.user
