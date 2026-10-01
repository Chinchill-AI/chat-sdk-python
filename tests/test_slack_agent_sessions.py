"""Slack Agent Sessions lifecycle and native stop (issue #215).

Ports the Agent Sessions cases of upstream
``packages/adapter-slack/src/index.test.ts`` (chat@4.41.1; vercel/chat
2ce2be00 #862, 2cc8cc3f #897, 8b6d7f3a #882), plus Python-specific guards.
The stream rotation and native-stream-fallback cases live with their
describes in ``test_slack_stream_rotation.py`` / ``test_slack_api.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from chat_sdk.adapters.slack.adapter import SlackAdapter, create_slack_adapter
from chat_sdk.adapters.slack.types import SlackAdapterConfig, SlackSessionTitleContext
from chat_sdk.chat import Chat
from chat_sdk.state.memory import MemoryStateAdapter
from chat_sdk.testing import MockLogger, create_mock_chat_instance, create_mock_state
from chat_sdk.types import (
    AgentSessionStoppedEvent,
    ChatConfig,
    RawMessage,
    StreamOptions,
    TurnSignal,
    TypingOptions,
    WebhookOptions,
)

SECRET = "test-signing-secret"
TOKEN = "xoxb-test-token"
SET_STATUS = "agents.sessions.setStatus"
RENAME = "agents.sessions.rename"


@pytest.fixture(autouse=True)
def _clean_slack_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET", "SLACK_CLIENT_ID", "SLACK_CLIENT_SECRET", "SLACK_APP_TOKEN"):
        monkeypatch.delenv(key, raising=False)


class _FakeRequest:
    def __init__(self, body: str, headers: dict[str, str]):
        self.body = body.encode("utf-8")
        self.headers = headers
        self.url = ""

    async def text(self) -> str:
        return self.body.decode("utf-8")


def _signed(payload: dict[str, Any]) -> _FakeRequest:
    body = json.dumps(payload)
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(SECRET.encode(), f"v0:{ts}:{body}".encode(), hashlib.sha256).hexdigest()
    return _FakeRequest(
        body, {"x-slack-request-timestamp": ts, "x-slack-signature": sig, "content-type": "application/json"}
    )


def _adapter(**config: Any) -> tuple[SlackAdapter, MagicMock, MockLogger]:
    logger = MockLogger()
    defaults: dict[str, Any] = {"bot_token": TOKEN, "signing_secret": SECRET, "bot_user_id": "U_BOT", "logger": logger}
    defaults.update(config)
    adapter = create_slack_adapter(SlackAdapterConfig(**defaults))
    client = MagicMock()
    client.api_call = AsyncMock(return_value={"ok": True})
    client.assistant_threads_setStatus = AsyncMock(return_value={"ok": True})
    client.assistant_threads_setTitle = AsyncMock(return_value={"ok": True})
    client.users_info = AsyncMock(return_value={"ok": True, "user": {"name": "user", "real_name": "User"}})
    adapter._get_client = lambda token=None: client  # type: ignore[method-assign]
    return adapter, client, logger


def _api_calls(client: MagicMock, method: str) -> list[dict[str, Any]]:
    return [c.kwargs["json"] for c in client.api_call.await_args_list if c.kwargs.get("api_method") == method]


async def _dispatch(adapter: SlackAdapter, payload: dict[str, Any]) -> dict[str, Any]:
    tasks: list[Any] = []
    response = await adapter.handle_webhook(_signed(payload), WebhookOptions(wait_until=tasks.append))
    await asyncio.gather(*tasks)
    return response


def _dm_message(**extra: Any) -> dict[str, Any]:
    return {
        "type": "event_callback",
        "event": {
            "type": "message",
            "channel": "D1",
            "channel_type": "im",
            "user": "U1",
            "text": "hi",
            "ts": "1771.99",
            "event_ts": "1771.99",
            **extra,
        },
    }


def _stopped_event() -> dict[str, Any]:
    return {
        "type": "event_callback",
        "team_id": "T123",
        "event": {
            "type": "agent_session_stopped",
            "user": "U_USER",
            "channel": "D_AGENT",
            "thread_ts": "1234567890.111111",
            "streaming_message_ts": ["1234567891.222222", "1234567891.333333"],
            "event_ts": "1234567892.333333",
        },
    }


async def _init(adapter: SlackAdapter, state: Any = None) -> Any:
    chat = create_mock_chat_instance(
        state=state if state is not None else create_mock_state(),
        overrides={"process_message": MagicMock(return_value=None)},
    )
    await adapter.initialize(chat)
    return chat


# ---------------------------------------------------------------------------
# Typing / status / title (PR A)
# ---------------------------------------------------------------------------


class TestAgentSessionStatus:
    # TS: "routes custom labels and clearing separately on an agent_view DM thread"
    async def test_routes_custom_labels_and_clearing_separately_on_an_agent_view_dm_thread(self):
        adapter, client, _ = _adapter(agent_view=True)
        options = TypingOptions(initiator_user_id="U1")

        await adapter.start_typing("slack:D1:1771.99", "Thinking...", options=options)
        await adapter.start_typing("slack:D1:1771.99", "", options=options)
        await adapter.start_typing("slack:D1:1771.99", "Reading xyz.md", options=options)

        # Custom statuses go through the legacy Assistants API, whose
        # compatibility bridge renders the text in the agent-session loading UX.
        assert [c.kwargs for c in client.assistant_threads_setStatus.await_args_list] == [
            {"channel_id": "D1", "thread_ts": "1771.99", "status": "Thinking...", "loading_messages": ["Thinking..."]},
            {
                "channel_id": "D1",
                "thread_ts": "1771.99",
                "status": "Reading xyz.md",
                "loading_messages": ["Reading xyz.md"],
            },
        ]
        # An explicit empty status ends typing via the Agent Sessions lifecycle.
        assert _api_calls(client, SET_STATUS) == [
            {"channel_id": "D1", "thread_ts": "1771.99", "status": "active", "initiator_user_id": "U1"}
        ]

    # TS: "uses native $expected without custom text in agent view" (it.each).
    # Also the Python-specific ``""`` vs ``None`` guard (8b6d7f3a): never
    # collapse the two with ``or``.
    @pytest.mark.parametrize(("status", "expected"), [(None, "processing"), ("", "active")])
    async def test_uses_native_status_without_custom_text_in_agent_view(self, status, expected):
        adapter, client, _ = _adapter(agent_view=True, loading_messages=["Configured..."])

        await adapter.start_typing("slack:D123:1.2", status, options=TypingOptions(initiator_user_id="U123"))

        client.api_call.assert_awaited_once_with(
            api_method=SET_STATUS,
            json={"channel_id": "D123", "thread_ts": "1.2", "status": expected, "initiator_user_id": "U123"},
        )
        client.assistant_threads_setStatus.assert_not_awaited()

    # TS: "does not fall back to a legacy label when native status fails"
    async def test_does_not_fall_back_to_a_legacy_label_when_native_status_fails(self):
        adapter, client, logger = _adapter(agent_view=True)
        failure = RuntimeError("API error")
        client.api_call = AsyncMock(side_effect=failure)

        assert await adapter.start_typing("slack:D123:1.2") is None

        client.assistant_threads_setStatus.assert_not_awaited()
        warned = [c for c in logger.warn.calls if c[0] == "Slack API: agents.sessions.setStatus failed"]
        assert len(warned) == 1
        assert warned[0][1]["error"] is failure

    # TS: "surfaces custom status text via the legacy API and clears via the sessions lifecycle"
    async def test_surfaces_custom_status_text_via_the_legacy_api_and_clears_via_the_sessions_lifecycle(self):
        adapter, client, _ = _adapter(agent_view=True)

        await adapter.set_assistant_status("D123", "1.2", "Working...")
        await adapter.set_assistant_status("D123", "1.2", "")

        client.assistant_threads_setStatus.assert_awaited_once_with(
            channel_id="D123", thread_ts="1.2", status="Working...", loading_messages=["Working..."]
        )
        client.api_call.assert_awaited_once_with(
            api_method=SET_STATUS, json={"channel_id": "D123", "thread_ts": "1.2", "status": "active"}
        )

    # TS: "passes agent loading messages $expected" (it.each)
    @pytest.mark.parametrize(
        ("configured", "supplied", "expected"),
        [
            (None, None, ["Working..."]),
            (["Configured..."], None, ["Configured..."]),
            (["Configured..."], ["Searching...", "Reading..."], ["Searching...", "Reading..."]),
        ],
    )
    async def test_passes_agent_loading_messages(self, configured, supplied, expected):
        adapter, client, _ = _adapter(agent_view=True, loading_messages=configured)

        await adapter.set_assistant_status("D123", "1.2", "Working...", supplied)

        client.assistant_threads_setStatus.assert_awaited_once_with(
            channel_id="D123", thread_ts="1.2", status="Working...", loading_messages=expected
        )
        client.api_call.assert_not_awaited()

    # Python-specific: upstream ``!status.trim()`` -- whitespace-only clears too.
    async def test_whitespace_only_status_clears_the_session(self):
        adapter, client, _ = _adapter(agent_view=True)

        await adapter.set_assistant_status("D123", "1.2", "  \n")

        client.assistant_threads_setStatus.assert_not_awaited()
        assert _api_calls(client, SET_STATUS) == [{"channel_id": "D123", "thread_ts": "1.2", "status": "active"}]

    # TS: "renames an agent session under agentView"
    async def test_renames_an_agent_session_under_agentview(self):
        adapter, client, _ = _adapter(agent_view=True)

        await adapter.set_assistant_title("D123", "1.2", "Agent title")

        client.api_call.assert_awaited_once_with(
            api_method=RENAME, json={"channel_id": "D123", "thread_ts": "1.2", "title": "Agent title"}
        )
        client.assistant_threads_setTitle.assert_not_awaited()

    # Python-specific: the legacy assistant_view path makes no Agent Sessions call.
    async def test_legacy_mode_makes_no_agent_sessions_calls(self):
        adapter, client, _ = _adapter()

        await adapter.start_typing("slack:D1:1.2")
        await adapter.start_typing("slack:D1:1.2", "")
        await adapter.end_typing("slack:D1:1.2", "active")
        await adapter.set_assistant_status("D1", "1.2", "")
        await adapter.set_assistant_title("D1", "1.2", "Title")

        client.api_call.assert_not_awaited()
        assert [c.kwargs["status"] for c in client.assistant_threads_setStatus.await_args_list] == ["Typing...", "", ""]
        client.assistant_threads_setTitle.assert_awaited_once_with(channel_id="D1", thread_ts="1.2", title="Title")
        assert adapter.supports_turn_cancellation is False

    async def test_supports_turn_cancellation_under_agent_view(self):
        adapter, _, _ = _adapter(agent_view=True)

        assert adapter.supports_turn_cancellation is True


class TestEndTyping:
    @pytest.mark.parametrize(("status", "expected"), [("suspended", "suspended"), (None, "active")])
    async def test_moves_the_session_to_the_requested_status(self, status, expected):
        adapter, client, _ = _adapter(agent_view=True)

        await adapter.end_typing("slack:D1:1.2", status)

        assert _api_calls(client, SET_STATUS) == [{"channel_id": "D1", "thread_ts": "1.2", "status": expected}]

    async def test_skips_a_thread_without_a_ts(self):
        adapter, client, _ = _adapter(agent_view=True)

        await adapter.end_typing("slack:D1:")

        client.api_call.assert_not_awaited()

    async def test_logs_a_failure_instead_of_raising(self):
        adapter, client, logger = _adapter(agent_view=True)
        client.api_call = AsyncMock(side_effect=RuntimeError("boom"))

        await adapter.end_typing("slack:D1:1.2")

        assert [c[0] for c in logger.warn.calls] == ["Slack API: agents.sessions.setStatus failed"]


# ---------------------------------------------------------------------------
# stream(): cancellation and session status (PR A)
# ---------------------------------------------------------------------------


def _streaming(adapter: SlackAdapter, client: MagicMock) -> MagicMock:
    streamer = MagicMock()
    streamer.append = AsyncMock(return_value={"ok": True, "ts": "1234567890.5"})
    streamer.stop = AsyncMock(return_value={"ok": True, "ts": "1234567890.5"})
    client.chat_stream = AsyncMock(return_value=streamer)
    return streamer


class TestStreamCancellation:
    # Python-specific: an aborted turn stops reading the stream but still
    # finalizes the native stream exactly once (never left open in Slack).
    async def test_aborted_signal_stops_reading_and_still_stops_the_stream_once(self):
        adapter, client, _ = _adapter(agent_view=True)
        streamer = _streaming(adapter, client)
        signal = TurnSignal()
        thread_id = "slack:D1:1.2"
        consumed: list[str] = []

        async def chunks() -> AsyncIterator[str]:
            try:
                consumed.append("first")
                yield "first\n"
                signal._abort()  # what Chat.abort_turn does for a running turn
                consumed.append("second")
                yield "second\n"
                consumed.append("third")
                yield "third\n"
            finally:
                # JS ``for await`` closes the source on ``break``; so must we.
                consumed.append("closed")

        result = await adapter.stream(thread_id, chunks(), StreamOptions(signal=signal))

        assert signal.aborted is True
        # The chunk yielded after the abort is never sent.
        assert consumed == ["first", "second", "closed"]
        assert [c.kwargs["markdown_text"] for c in streamer.append.await_args_list] == ["first\n"]
        streamer.stop.assert_awaited_once_with(token=TOKEN, session_status="active")
        assert isinstance(result, RawMessage)
        assert result.id == "1234567890.5"

    async def test_requested_session_status_is_sent_on_the_final_stop(self):
        adapter, client, _ = _adapter(agent_view=True)
        streamer = _streaming(adapter, client)

        async def chunks() -> AsyncIterator[str]:
            yield "done\n"

        await adapter.stream("slack:D1:1.2", chunks(), StreamOptions(session_status="suspended"))

        assert streamer.stop.await_args.kwargs["session_status"] == "suspended"

    # Python-specific: without agent_view the stop payload is unchanged.
    async def test_legacy_stream_stop_carries_no_session_status(self):
        adapter, client, _ = _adapter()
        streamer = _streaming(adapter, client)

        async def chunks() -> AsyncIterator[str]:
            yield "done\n"

        await adapter.stream("slack:D1:1.2", chunks(), StreamOptions(session_status="suspended"))

        streamer.stop.assert_awaited_once_with(token=TOKEN)
        client.api_call.assert_not_awaited()

    async def test_post_and_edit_fallback_ends_the_agent_session(self):
        adapter, client, _ = _adapter(agent_view=True)
        streamer = _streaming(adapter, client)
        streamer.append = AsyncMock(side_effect=RuntimeError("no streaming here"))
        adapter.post_message = AsyncMock(  # type: ignore[method-assign]
            return_value=RawMessage(id="fallback-ts", thread_id="slack:D1:1.2", raw={})
        )
        adapter.edit_message = AsyncMock(  # type: ignore[method-assign]
            return_value=RawMessage(id="fallback-ts", thread_id="slack:D1:1.2", raw={})
        )

        async def chunks() -> AsyncIterator[str]:
            yield "hello\n"

        result = await adapter.stream("slack:D1:1.2", chunks(), StreamOptions(session_status="suspended"))

        assert result is not None
        assert result.id == "fallback-ts"
        streamer.stop.assert_not_awaited()
        assert _api_calls(client, SET_STATUS) == [{"channel_id": "D1", "thread_ts": "1.2", "status": "suspended"}]


# ---------------------------------------------------------------------------
# Events (PR B)
# ---------------------------------------------------------------------------


class TestAgentSessionEvents:
    # TS: "aborts and activates an agent session when the user stops it"
    async def test_aborts_and_activates_an_agent_session_when_the_user_stops_it(self):
        state = create_mock_state()
        adapter, client, _ = _adapter(agent_view=True)
        chat = await _init(adapter, state)

        response = await _dispatch(adapter, _stopped_event())

        thread_id = "slack:D_AGENT:1234567890.111111"
        assert response["status"] == 200
        chat.abort_turn.assert_awaited_once_with(thread_id)
        assert _api_calls(client, SET_STATUS) == [
            {"channel_id": "D_AGENT", "thread_ts": "1234567890.111111", "status": "active"}
        ]
        chat.process_agent_session_stopped.assert_called_once()
        event, options = chat.process_agent_session_stopped.call_args.args
        assert isinstance(event, AgentSessionStoppedEvent)
        assert event.adapter is adapter
        assert event.channel_id == "D_AGENT"
        assert event.streaming_message_ts == ["1234567891.222222", "1234567891.333333"]
        assert event.thread_id == thread_id
        assert event.thread_ts == "1234567890.111111"
        assert event.user_id == "U_USER"
        assert isinstance(options, WebhookOptions)
        assert state._acquire_lock_calls == []

    # Python-specific: abort and activation failures are logged and the stop
    # is still dispatched.
    async def test_stop_dispatches_even_when_abort_and_activation_fail(self):
        adapter, client, logger = _adapter(agent_view=True)
        chat = await _init(adapter)
        chat.abort_turn.side_effect = RuntimeError("state down")
        client.api_call = AsyncMock(side_effect=RuntimeError("slack down"))

        await _dispatch(adapter, _stopped_event())

        assert [c[0] for c in logger.warn.calls] == [
            "Failed to abort stopped Slack agent session",
            "Failed to activate stopped Slack agent session",
        ]
        chat.process_agent_session_stopped.assert_called_once()

    # TS: "dispatches agent session title changes"
    async def test_dispatches_agent_session_title_changes(self):
        adapter, _, _ = _adapter(agent_view=True)
        chat = await _init(adapter)

        response = await adapter.handle_webhook(
            _signed(
                {
                    "type": "event_callback",
                    "team_id": "T123",
                    "event": {
                        "type": "agent_session_title_changed",
                        "user": "U_USER",
                        "channel": "D_AGENT",
                        "thread_ts": "1234567890.111111",
                        "title": "New title",
                        "event_ts": "1234567892.333333",
                        "team_id": "T123",
                    },
                }
            )
        )

        assert response["status"] == 200
        event, options = chat.process_agent_session_title_changed.call_args.args
        assert event.previous_title is None
        assert event.thread_id == "slack:D_AGENT:1234567890.111111"
        assert event.thread_ts == "1234567890.111111"
        assert event.title == "New title"
        assert event.user_id == "U_USER"
        assert event.channel_id == "D_AGENT"
        assert options is None

    # Python-specific: the stop handler runs while the stopped turn still
    # holds the thread lock (a real Chat; gates, no sleeps).
    async def test_stop_aborts_a_running_turn_that_holds_the_thread_lock(self):
        adapter, client, _ = _adapter(agent_view=True)
        state = MemoryStateAdapter()
        chat = Chat(ChatConfig(user_name="bot", adapters={"slack": adapter}, state=state, logger=MockLogger()))
        started = asyncio.Event()
        stopped_events: list[AgentSessionStoppedEvent] = []
        handler_saw_abort: list[bool] = []

        async def on_dm(thread: Any, message: Any, *rest: Any) -> None:
            started.set()
            await thread.signal.wait()
            handler_saw_abort.append(thread.signal.aborted)

        async def on_stopped(event: AgentSessionStoppedEvent) -> None:
            stopped_events.append(event)

        chat.on_direct_message(on_dm)
        chat.on_agent_session_stopped(on_stopped)
        tasks: list[Any] = []
        try:
            # A threaded follow-up takes the normal (non-bridge) path.
            await chat.webhooks["slack"](
                _signed(_dm_message(thread_ts="1771.00")), WebhookOptions(wait_until=tasks.append)
            )
            await asyncio.wait_for(started.wait(), timeout=5)
            stop = _stopped_event()
            stop["event"].update(channel="D1", thread_ts="1771.00")

            await chat.webhooks["slack"](_signed(stop), WebhookOptions(wait_until=tasks.append))
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)

            assert handler_saw_abort == [True]
            assert [e.thread_id for e in stopped_events] == ["slack:D1:1771.00"]
            assert _api_calls(client, SET_STATUS) == [{"channel_id": "D1", "thread_ts": "1771.00", "status": "active"}]
        finally:
            await asyncio.gather(*tasks, return_exceptions=True)
            await chat.shutdown()


# ---------------------------------------------------------------------------
# Automatic session titles (PR B)
# ---------------------------------------------------------------------------


class TestAutomaticSessionTitles:
    # TS: "automatically titles a top-level agent_view DM session"
    async def test_automatically_titles_a_top_level_agent_view_dm_session(self):
        adapter, client, _ = _adapter(agent_view=True)
        await _init(adapter)

        await _dispatch(adapter, _dm_message())

        assert _api_calls(client, RENAME) == [{"channel_id": "D1", "thread_ts": "1771.99", "title": "hi"}]

    # TS: "skips automatic titles when sessionTitle is disabled"
    async def test_skips_automatic_titles_when_sessiontitle_is_disabled(self):
        adapter, client, _ = _adapter(agent_view=True, session_title=False)
        await _init(adapter)

        await _dispatch(adapter, _dm_message())

        assert _api_calls(client, RENAME) == []

    # TS: "does not retitle an agent_view DM follow-up"
    async def test_does_not_retitle_an_agent_view_dm_follow_up(self):
        adapter, client, _ = _adapter(agent_view=True)
        await _init(adapter)

        await _dispatch(adapter, _dm_message(thread_ts="1771.00"))

        assert _api_calls(client, RENAME) == []

    # Python-specific: first line only, trimmed, cut to 80 characters.
    async def test_uses_the_trimmed_first_line_cut_to_80_characters(self):
        adapter, client, _ = _adapter(agent_view=True)
        await _init(adapter)

        await _dispatch(adapter, _dm_message(text="   " + "x" * 100 + "  \nsecond line"))

        assert _api_calls(client, RENAME) == [{"channel_id": "D1", "thread_ts": "1771.99", "title": "x" * 80}]

    @pytest.mark.parametrize("is_async", [False, True])
    async def test_uses_a_sync_or_async_resolver(self, is_async):
        contexts: list[SlackSessionTitleContext] = []

        def resolve(context: SlackSessionTitleContext) -> str:
            contexts.append(context)
            return f"  Re: {context.text}  "

        async def resolve_async(context: SlackSessionTitleContext) -> str:
            return resolve(context)

        adapter, client, _ = _adapter(agent_view=True, session_title=resolve_async if is_async else resolve)
        await _init(adapter)

        await _dispatch(adapter, _dm_message())

        assert contexts == [SlackSessionTitleContext(channel_id="D1", text="hi", thread_ts="1771.99", user_id="U1")]
        assert _api_calls(client, RENAME) == [{"channel_id": "D1", "thread_ts": "1771.99", "title": "Re: hi"}]

    @pytest.mark.parametrize("resolved", [None, "   "])
    async def test_skips_when_the_resolver_returns_nothing(self, resolved):
        adapter, client, _ = _adapter(agent_view=True, session_title=lambda _context: resolved)
        await _init(adapter)

        await _dispatch(adapter, _dm_message())

        assert _api_calls(client, RENAME) == []

    async def test_logs_a_resolver_failure_without_failing_the_webhook(self):
        def boom(_context: SlackSessionTitleContext) -> str:
            raise RuntimeError("resolver failed")

        adapter, client, logger = _adapter(agent_view=True, session_title=boom)
        await _init(adapter)

        response = await _dispatch(adapter, _dm_message())

        assert response["status"] == 200
        assert [c[0] for c in logger.warn.calls] == ["Failed to set Slack agent session title"]
        assert _api_calls(client, RENAME) == []

    @pytest.mark.parametrize("extra", [{"bot_id": "B1"}, {"text": ""}])
    async def test_skips_bot_and_empty_messages(self, extra):
        adapter, client, _ = _adapter(agent_view=True)
        await _init(adapter)

        await _dispatch(adapter, _dm_message(**extra))

        assert _api_calls(client, RENAME) == []

    async def test_defaults_off_without_agent_view(self):
        adapter, client, _ = _adapter(session_title=True)
        await _init(adapter)

        await _dispatch(adapter, _dm_message())

        assert _api_calls(client, RENAME) == []
