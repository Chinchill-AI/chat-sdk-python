"""Tests for Slack adapter Socket Mode support (vercel/chat#162 port).

Covers:

* Configuration validation (app_token required, xapp- prefix, signing
  optional in socket mode, multi-workspace rejected by factory).
* ``ModalResponse(action="clear")`` produces ``response_action: clear``.
* ``handle_webhook`` accepts forwarded socket events with a valid
  ``x-slack-socket-token`` header and rejects mismatches.
* ``handle_webhook`` returns 405 for direct POSTs in socket mode.
* ``_route_socket_event`` dispatches events_api / slash_commands /
  interactive payloads to the same handlers the webhook path uses, calls
  ``ack`` exactly once, and skips Slack retries.
* ``start_socket_mode`` / ``stop_socket_mode`` are idempotent and the
  reconnect loop reconnects after a transient disconnect.
* ContextVar boundaries: events received over the socket inherit the
  per-instance request-context ContextVar.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Stub slack_sdk.socket_mode.* before importing the adapter. Other Slack
# test modules already stub ``slack_sdk.web`` via ``setdefault``; we extend
# the same pattern to the socket mode submodules so the adapter's lazy
# ``from slack_sdk.socket_mode.aiohttp import SocketModeClient`` resolves
# regardless of whether the real ``slack-sdk`` extra is installed.
# ---------------------------------------------------------------------------


def _ensure_socket_mode_stub() -> None:
    if "slack_sdk.socket_mode.aiohttp" in sys.modules and hasattr(
        sys.modules["slack_sdk.socket_mode.aiohttp"], "SocketModeClient"
    ):
        return

    sys.modules.setdefault("slack_sdk", ModuleType("slack_sdk"))
    sm_root = sys.modules.setdefault("slack_sdk.socket_mode", ModuleType("slack_sdk.socket_mode"))
    sm_aio = sys.modules.setdefault("slack_sdk.socket_mode.aiohttp", ModuleType("slack_sdk.socket_mode.aiohttp"))
    sm_resp = sys.modules.setdefault("slack_sdk.socket_mode.response", ModuleType("slack_sdk.socket_mode.response"))

    if not hasattr(sm_aio, "SocketModeClient"):

        class _StubSocketModeClient:
            """Replaced per-test by tests that exercise the lifecycle."""

            def __init__(self, *args: Any, app_token: str | None = None, **kwargs: Any):
                self.app_token = app_token
                self.socket_mode_request_listeners: list[Any] = []
                self._connected = False

            async def connect(self) -> None:
                self._connected = True

            async def disconnect(self) -> None:
                self._connected = False

            def is_connected(self) -> bool:
                return self._connected

            async def send_socket_mode_response(self, _response: Any) -> None:
                return None

        sm_aio.SocketModeClient = _StubSocketModeClient  # type: ignore[attr-defined]

    if not hasattr(sm_resp, "SocketModeResponse"):

        class _StubSocketModeResponse:
            def __init__(self, envelope_id: str = "", payload: Any = None):
                self.envelope_id = envelope_id
                self.payload = payload

        sm_resp.SocketModeResponse = _StubSocketModeResponse  # type: ignore[attr-defined]

    sm_root.aiohttp = sm_aio  # type: ignore[attr-defined]
    sm_root.response = sm_resp  # type: ignore[attr-defined]
    sys.modules["slack_sdk"].socket_mode = sm_root  # type: ignore[attr-defined]


_ensure_socket_mode_stub()


try:
    from chat_sdk.adapters.slack.adapter import SlackAdapter, create_slack_adapter  # noqa: E402
    from chat_sdk.adapters.slack.types import SlackAdapterConfig  # noqa: E402
    from chat_sdk.shared.errors import ValidationError  # noqa: E402
    from chat_sdk.types import ModalResponse  # noqa: E402

    _SLACK_AVAILABLE = True
except ImportError:
    _SLACK_AVAILABLE = False

pytestmark = pytest.mark.skipif(not _SLACK_AVAILABLE, reason="Slack adapter import failed")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_socket_adapter(**overrides: Any) -> SlackAdapter:
    config = SlackAdapterConfig(
        mode="socket",
        app_token=overrides.pop("app_token", "xapp-1-test"),
        bot_token=overrides.pop("bot_token", "xoxb-test-token"),
        signing_secret=overrides.pop("signing_secret", None),
        socket_forwarding_secret=overrides.pop("socket_forwarding_secret", "fwd-secret"),
        **overrides,
    )
    return SlackAdapter(config)


def _make_mock_state() -> MagicMock:
    cache: dict[str, Any] = {}
    state = MagicMock()
    state.get = AsyncMock(side_effect=lambda k: cache.get(k))
    state.set = AsyncMock(side_effect=lambda k, v, *a, **kw: cache.__setitem__(k, v))
    state.delete = AsyncMock(side_effect=lambda k: cache.pop(k, None))
    state.append_to_list = AsyncMock()
    state.get_list = AsyncMock(return_value=[])
    state._cache = cache
    return state


def _make_mock_chat() -> MagicMock:
    state = _make_mock_state()
    chat = MagicMock()
    chat.process_message = MagicMock()
    chat.handle_incoming_message = AsyncMock()
    chat.process_reaction = MagicMock()
    chat.process_action = MagicMock()
    chat.process_modal_submit = AsyncMock()
    chat.process_modal_close = MagicMock()
    chat.process_slash_command = MagicMock()
    chat.process_member_joined_channel = MagicMock()
    chat.get_state = MagicMock(return_value=state)
    chat.get_user_name = MagicMock(return_value="test-bot")
    chat.get_logger = MagicMock(return_value=MagicMock())
    return chat


class _FakeRequest:
    def __init__(self, body: str, headers: dict[str, str] | None = None):
        self.body = body.encode("utf-8")
        self.headers = headers or {}
        self.url = ""

    async def text(self) -> str:
        return self.body.decode("utf-8")


# ---------------------------------------------------------------------------
# Construction / config validation
# ---------------------------------------------------------------------------


class TestSocketModeConfig:
    def test_socket_mode_requires_app_token(self):
        with pytest.raises(ValidationError, match="appToken is required"):
            SlackAdapter(SlackAdapterConfig(mode="socket", bot_token="xoxb-test"))

    def test_socket_mode_app_token_must_start_with_xapp(self):
        with pytest.raises(ValidationError, match="must start with 'xapp-'"):
            SlackAdapter(
                SlackAdapterConfig(
                    mode="socket",
                    app_token="xoxb-not-an-app-token",
                    bot_token="xoxb-test",
                )
            )

    def test_socket_mode_signing_secret_optional(self):
        # Should not raise even though signing_secret is None.
        adapter = SlackAdapter(
            SlackAdapterConfig(
                mode="socket",
                app_token="xapp-1-foo",
                bot_token="xoxb-test",
            )
        )
        assert adapter.is_socket_mode is True
        assert adapter.mode == "socket"

    def test_webhook_mode_still_requires_signing_secret(self):
        # Make sure socket-mode allowance didn't accidentally relax the
        # webhook-mode check. Clear env so the test isn't accidentally
        # satisfied by SLACK_SIGNING_SECRET in the dev shell.
        prev = os.environ.pop("SLACK_SIGNING_SECRET", None)
        try:
            with pytest.raises(ValidationError, match="signingSecret is required"):
                SlackAdapter(SlackAdapterConfig(bot_token="xoxb-test"))
        finally:
            if prev is not None:
                os.environ["SLACK_SIGNING_SECRET"] = prev

    def test_app_token_picked_up_from_env(self):
        prev = os.environ.get("SLACK_APP_TOKEN")
        os.environ["SLACK_APP_TOKEN"] = "xapp-1-from-env"
        try:
            adapter = SlackAdapter(
                SlackAdapterConfig(mode="socket", bot_token="xoxb-test"),
            )
            assert adapter._app_token == "xapp-1-from-env"
        finally:
            if prev is not None:
                os.environ["SLACK_APP_TOKEN"] = prev
            else:
                os.environ.pop("SLACK_APP_TOKEN", None)

    def test_socket_forwarding_secret_falls_back_to_app_token(self):
        adapter = SlackAdapter(
            SlackAdapterConfig(
                mode="socket",
                app_token="xapp-1-foo",
                bot_token="xoxb-test",
            )
        )
        # Falls back to app_token only when neither config nor env is set.
        assert adapter._socket_forwarding_secret == "xapp-1-foo"

    def test_create_slack_adapter_rejects_multi_workspace_in_socket_mode(self):
        with pytest.raises(ValidationError, match="Multi-workspace"):
            create_slack_adapter(
                SlackAdapterConfig(
                    mode="socket",
                    app_token="xapp-1-foo",
                    client_id="cid",
                    client_secret="csec",
                )
            )


# ---------------------------------------------------------------------------
# Modal "clear" response action
# ---------------------------------------------------------------------------


class TestModalClearResponse:
    def test_clear_action_emits_response_action_clear(self):
        adapter = SlackAdapter(
            SlackAdapterConfig(signing_secret="s", bot_token="xoxb-x"),
        )
        result = adapter._modal_response_to_slack(ModalResponse(action="clear"))
        assert result == {"response_action": "clear"}


# ---------------------------------------------------------------------------
# Forwarded socket events via handle_webhook
# ---------------------------------------------------------------------------


class TestForwardedSocketEvents:
    async def test_webhook_in_socket_mode_returns_405_without_token(self):
        adapter = _make_socket_adapter()
        adapter._chat = _make_mock_chat()
        request = _FakeRequest(
            json.dumps({"type": "event_callback", "event": {"type": "message"}}),
            headers={"content-type": "application/json"},
        )
        result = await adapter.handle_webhook(request)
        assert result["status"] == 405

    async def test_webhook_accepts_valid_socket_token(self):
        import time as _time

        adapter = _make_socket_adapter()
        chat = _make_mock_chat()
        adapter._chat = chat
        # Drive the route through events_api so we can verify that the
        # underlying process_message handler is invoked.
        forwarded = {
            "type": "socket_event",
            "eventType": "events_api",
            "body": {
                "type": "event_callback",
                "event": {
                    "type": "message",
                    "channel": "C123",
                    "ts": "1.0",
                    "user": "U1",
                    "text": "hi",
                    "team": "T1",
                },
                "team_id": "T1",
            },
            "timestamp": int(_time.time()),
        }
        request = _FakeRequest(
            json.dumps(forwarded),
            headers={
                "content-type": "application/json",
                "x-slack-socket-token": "fwd-secret",
            },
        )
        result = await adapter.handle_webhook(request)
        assert result["status"] == 200
        assert result["body"] == "ok"
        # process_message gets called with the parsed event.
        assert chat.process_message.called

    async def test_webhook_rejects_invalid_socket_token(self):
        adapter = _make_socket_adapter(socket_forwarding_secret="real-secret")
        adapter._chat = _make_mock_chat()
        request = _FakeRequest(
            json.dumps({"type": "socket_event", "eventType": "events_api", "body": {}}),
            headers={
                "content-type": "application/json",
                "x-slack-socket-token": "wrong-secret",
            },
        )
        result = await adapter.handle_webhook(request)
        assert result["status"] == 401

    async def test_webhook_socket_token_rejected_when_no_secret_configured(self):
        # Adapter in webhook mode with no forwarding secret + no app token.
        adapter = SlackAdapter(
            SlackAdapterConfig(signing_secret="s", bot_token="xoxb-x"),
        )
        # Belt and suspenders: ensure the secret is unset.
        adapter._socket_forwarding_secret = None
        request = _FakeRequest(
            json.dumps({"type": "socket_event"}),
            headers={
                "content-type": "application/json",
                "x-slack-socket-token": "anything",
            },
        )
        result = await adapter.handle_webhook(request)
        assert result["status"] == 401


# ---------------------------------------------------------------------------
# _route_socket_event dispatch
# ---------------------------------------------------------------------------


class TestRouteSocketEvent:
    async def test_events_api_acks_then_dispatches(self):
        adapter = _make_socket_adapter()
        chat = _make_mock_chat()
        adapter._chat = chat
        ack = AsyncMock()
        body = {
            "team_id": "T1",
            "event": {
                "type": "message",
                "channel": "C1",
                "ts": "1.0",
                "user": "U1",
                "text": "hello",
                "team": "T1",
            },
        }
        await adapter._route_socket_event(body, "events_api", ack)
        ack.assert_awaited_once_with()
        assert chat.process_message.called

    async def test_events_api_missing_event_field_does_not_crash(self):
        adapter = _make_socket_adapter()
        adapter._chat = _make_mock_chat()
        ack = AsyncMock()
        await adapter._route_socket_event({"team_id": "T1"}, "events_api", ack)
        ack.assert_awaited_once_with()

    async def test_slash_command_acks_immediately_and_dispatches(self):
        adapter = _make_socket_adapter()
        chat = _make_mock_chat()
        adapter._chat = chat
        # Slash dispatch calls _lookup_user → Slack API. Stub it.
        adapter._lookup_user = AsyncMock(  # type: ignore[method-assign]
            return_value={"display_name": "u1", "real_name": "u1"}
        )
        ack = AsyncMock()
        body = {
            "command": "/foo",
            "text": "bar",
            "user_id": "U1",
            "channel_id": "C1",
            "team_id": "T1",
        }
        await adapter._route_socket_event(body, "slash_commands", ack)
        # Ack is sent immediately with no payload.
        ack.assert_awaited_once_with()
        # Slash dispatch is fire-and-forget — give the spawned task a turn
        # of the event loop to land.
        for _ in range(50):
            if chat.process_slash_command.called:
                break
            await asyncio.sleep(0.01)
        assert chat.process_slash_command.called

    async def test_interactive_acks_with_response_body_for_view_submission_errors(self):
        adapter = _make_socket_adapter()
        chat = _make_mock_chat()
        adapter._chat = chat

        # Have the modal-submit handler return errors so the dispatcher
        # builds an `errors` response body that should round-trip through
        # the ack.
        async def fake_modal_submit(*args: Any, **kwargs: Any) -> ModalResponse:
            return ModalResponse(action="errors", errors={"field": "bad"})

        chat.process_modal_submit = AsyncMock(side_effect=fake_modal_submit)
        ack = AsyncMock()
        payload = {
            "type": "view_submission",
            "team": {"id": "T1"},
            "user": {"id": "U1", "name": "x"},
            "view": {
                "id": "V1",
                "callback_id": "cb",
                "private_metadata": "",
                "state": {"values": {}},
            },
            "trigger_id": "trig",
        }
        await adapter._route_socket_event(payload, "interactive", ack)
        # Ack was awaited exactly once with the errors response body.
        assert ack.await_count == 1
        ack_args = ack.call_args
        assert ack_args.args, "ack should be called with the response body"
        body_arg = ack_args.args[0]
        assert isinstance(body_arg, dict)
        assert body_arg.get("response_action") == "errors"

    async def test_retry_attempt_is_skipped_but_acked(self):
        adapter = _make_socket_adapter()
        chat = _make_mock_chat()
        adapter._chat = chat
        request = MagicMock()
        request.envelope_id = "env-1"
        request.type = "events_api"
        request.payload = {"event": {"type": "message"}}
        request.retry_attempt = 2  # Slack retry — should be skipped.

        client = MagicMock()
        client.send_socket_mode_response = AsyncMock()

        await adapter._on_socket_request(client, request)

        # Ack went out (so Slack stops resending), but no dispatch.
        assert client.send_socket_mode_response.await_count == 1
        assert chat.process_message.called is False

    async def test_unknown_event_type_acks_and_does_nothing(self):
        adapter = _make_socket_adapter()
        adapter._chat = _make_mock_chat()
        ack = AsyncMock()
        await adapter._route_socket_event({}, "weird", ack)
        ack.assert_awaited_once_with()


# ---------------------------------------------------------------------------
# Lifecycle: start_socket_mode / stop_socket_mode
# ---------------------------------------------------------------------------


class _FakeSocketModeClient:
    """In-process stand-in for slack_sdk's SocketModeClient."""

    instances: list[_FakeSocketModeClient] = []

    def __init__(self, *args: Any, app_token: str | None = None, **kwargs: Any):
        self.app_token = app_token
        self.socket_mode_request_listeners: list[Any] = []
        self._connected = False
        self.connect_calls = 0
        self.disconnect_calls = 0
        # Test hooks
        self.fail_first_connect = False
        self.disconnect_after_s: float | None = None
        type(self).instances.append(self)

    async def connect(self) -> None:
        self.connect_calls += 1
        if self.fail_first_connect:
            raise RuntimeError("simulated connect failure")
        self._connected = True
        if self.disconnect_after_s is not None:
            asyncio.get_event_loop().call_later(self.disconnect_after_s, lambda: setattr(self, "_connected", False))

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected


@pytest.fixture
def patched_socket_client(monkeypatch):
    """Patch slack_sdk.socket_mode.aiohttp.SocketModeClient with a fake."""
    _FakeSocketModeClient.instances.clear()
    import slack_sdk.socket_mode.aiohttp as sm

    monkeypatch.setattr(sm, "SocketModeClient", _FakeSocketModeClient)
    yield _FakeSocketModeClient
    _FakeSocketModeClient.instances.clear()


class TestSocketModeLifecycle:
    async def test_start_then_stop(self, patched_socket_client):
        adapter = _make_socket_adapter()
        # Make backoff fast so the loop responds quickly to disconnects.
        adapter._socket_initial_backoff_s = 0.05
        adapter._socket_max_backoff_s = 0.1
        await adapter.start_socket_mode()
        assert len(patched_socket_client.instances) == 1
        client = patched_socket_client.instances[0]
        assert client.connect_calls == 1
        # Listener registered
        assert len(client.socket_mode_request_listeners) == 1

        await adapter.stop_socket_mode()
        # Disconnect was called at least once on the client.
        assert client.disconnect_calls >= 1
        # Task is cleared.
        assert adapter._socket_task is None

    async def test_start_is_idempotent(self, patched_socket_client):
        adapter = _make_socket_adapter()
        adapter._socket_initial_backoff_s = 0.05
        await adapter.start_socket_mode()
        await adapter.start_socket_mode()  # second call no-ops
        assert len(patched_socket_client.instances) == 1
        await adapter.stop_socket_mode()

    async def test_stop_is_idempotent(self, patched_socket_client):
        adapter = _make_socket_adapter()
        adapter._socket_initial_backoff_s = 0.05
        await adapter.start_socket_mode()
        await adapter.stop_socket_mode()
        await adapter.stop_socket_mode()  # safe second call

    async def test_first_connect_failure_propagates(self, patched_socket_client):
        # Patch the fake to fail every connect.
        original_connect = _FakeSocketModeClient.connect

        async def always_fail(self):
            self.connect_calls += 1
            raise RuntimeError("boom")

        _FakeSocketModeClient.connect = always_fail  # type: ignore[assignment]
        try:
            adapter = _make_socket_adapter()
            adapter._socket_initial_backoff_s = 0.01
            with pytest.raises(RuntimeError, match="boom"):
                await adapter.start_socket_mode()
        finally:
            _FakeSocketModeClient.connect = original_connect  # type: ignore[assignment]

    async def test_reconnects_after_transient_disconnect(self, patched_socket_client):
        # First client: connect succeeds, then immediately reports
        # disconnected so the loop has to reconnect.
        original_connect = _FakeSocketModeClient.connect
        attempt_count = {"n": 0}

        async def staged_connect(self):
            attempt_count["n"] += 1
            self.connect_calls += 1
            self._connected = True
            # First-stage client: drop connection after a tick to force
            # the reconnect path. Second-stage client stays up.
            if attempt_count["n"] == 1:
                # Schedule a drop after the loop's polling interval.
                async def drop():
                    await asyncio.sleep(0.01)
                    self._connected = False

                asyncio.create_task(drop())

        _FakeSocketModeClient.connect = staged_connect  # type: ignore[assignment]
        try:
            adapter = _make_socket_adapter()
            adapter._socket_initial_backoff_s = 0.01
            adapter._socket_max_backoff_s = 0.05
            await adapter.start_socket_mode()
            # Give the loop time to detect the drop and reconnect at least once.
            for _ in range(50):
                if attempt_count["n"] >= 2:
                    break
                await asyncio.sleep(0.05)
            await adapter.stop_socket_mode()
            assert attempt_count["n"] >= 2, f"expected reconnect, got {attempt_count['n']}"
        finally:
            _FakeSocketModeClient.connect = original_connect  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# ContextVar boundary: per-event request context survives into spawned tasks
# ---------------------------------------------------------------------------


class TestSocketContextVar:
    async def test_request_context_isolated_per_event(self):
        # Multi-workspace adapter so that events_api goes through token
        # resolution + ContextVar setup.
        adapter = SlackAdapter(
            SlackAdapterConfig(
                mode="socket",
                app_token="xapp-1-x",
                client_id="cid",
                client_secret="csec",
            )
        )
        chat = _make_mock_chat()
        adapter._chat = chat
        from chat_sdk.adapters.slack.types import RequestContext

        # Stub the per-team token lookup so the route doesn't hit storage.
        adapter._resolve_token_for_team = AsyncMock(  # type: ignore[method-assign]
            return_value=RequestContext(token="xoxb-team-1")
        )

        captured_tokens: list[str | None] = []

        def capture_message(*args: Any, **kwargs: Any) -> None:
            ctx = adapter._request_context.get()
            captured_tokens.append(ctx.token if ctx else None)

        chat.process_message = MagicMock(side_effect=capture_message)
        ack = AsyncMock()
        body = {
            "team_id": "T1",
            "event": {
                "type": "message",
                "channel": "C1",
                "ts": "1.0",
                "user": "U1",
                "text": "hi",
            },
        }
        await adapter._route_socket_event(body, "events_api", ack)
        assert captured_tokens == ["xoxb-team-1"]
        # And the outer context wasn't polluted.
        assert adapter._request_context.get() is None

    async def test_concurrent_events_for_different_teams_do_not_cross_contaminate(self):
        """Two concurrent ``_route_socket_event(events_api)`` calls for
        different teams must not see each other's tokens (hazard #6).

        What to fix if this fails: the events_api branch in
        ``_route_socket_event`` must use ``contextvars.copy_context()`` (or
        equivalent isolation) so that a slow handler for team T1 doesn't
        observe the ContextVar that another concurrent dispatch set for
        team T2. Direct ``ContextVar.set()`` without isolation will leak
        across ``asyncio.gather`` task boundaries.
        """
        adapter = SlackAdapter(
            SlackAdapterConfig(
                mode="socket",
                app_token="xapp-1-x",
                client_id="cid",
                client_secret="csec",
            )
        )
        chat = _make_mock_chat()
        adapter._chat = chat
        from chat_sdk.adapters.slack.types import RequestContext

        # Per-team token lookup. The first lookup awaits long enough for
        # the second to interleave; if isolation is broken the first will
        # observe the second's token.
        async def fake_resolve(team_id: str) -> RequestContext:
            if team_id == "T1":
                # Yield so the T2 dispatch can race in and set the
                # ContextVar before T1's process_message runs.
                await asyncio.sleep(0.02)
            return RequestContext(token=f"xoxb-{team_id}")

        adapter._resolve_token_for_team = AsyncMock(  # type: ignore[method-assign]
            side_effect=fake_resolve
        )

        observed: dict[str, list[str | None]] = {"T1": [], "T2": []}

        def capture(*args: Any, **kwargs: Any) -> None:
            ctx = adapter._request_context.get()
            tok = ctx.token if ctx else None
            # Map back to the team via the token suffix we constructed.
            if tok == "xoxb-T1":
                observed["T1"].append(tok)
            elif tok == "xoxb-T2":
                observed["T2"].append(tok)
            else:
                observed.setdefault("other", []).append(tok)

        chat.process_message = MagicMock(side_effect=capture)
        ack = AsyncMock()

        def body_for(team: str) -> dict[str, Any]:
            return {
                "team_id": team,
                "event": {
                    "type": "message",
                    "channel": f"C-{team}",
                    "ts": "1.0",
                    "user": "U1",
                    "text": "hi",
                    "team": team,
                },
            }

        # Fire both concurrently.
        await asyncio.gather(
            adapter._route_socket_event(body_for("T1"), "events_api", ack),
            adapter._route_socket_event(body_for("T2"), "events_api", ack),
        )

        assert observed["T1"] == ["xoxb-T1"], f"T1 saw wrong token(s): {observed}"
        assert observed["T2"] == ["xoxb-T2"], f"T2 saw wrong token(s): {observed}"
        # Outer context wasn't polluted by either dispatch.
        assert adapter._request_context.get() is None


# ---------------------------------------------------------------------------
# Review-finding regression tests (PR #86)
# ---------------------------------------------------------------------------


class TestSocketConnectTimeout:
    """Regression for review finding #1.

    What to fix if this fails: ``start_socket_mode`` must wrap the wait on
    the initial connect with ``asyncio.wait_for(..., timeout=...)`` so a
    hung ``SocketModeClient.connect()`` cannot block ``initialize()``
    forever (hazard #11). On timeout the loop must be torn down.
    """

    async def test_hung_connect_raises_timeout_and_cleans_up(self, patched_socket_client):
        original_connect = _FakeSocketModeClient.connect

        async def hang_forever(self):
            self.connect_calls += 1
            # Sleep longer than any reasonable test timeout. The fix should
            # cancel us via the outer ``asyncio.wait_for``.
            await asyncio.sleep(60)

        _FakeSocketModeClient.connect = hang_forever  # type: ignore[assignment]
        try:
            adapter = _make_socket_adapter()
            adapter._socket_connect_timeout_s = 0.1
            adapter._socket_initial_backoff_s = 0.01
            with pytest.raises(TimeoutError, match="timed out"):
                await adapter.start_socket_mode()
            # The teardown path must clear the background task.
            assert adapter._socket_task is None
            assert adapter._socket_client is None
        finally:
            _FakeSocketModeClient.connect = original_connect  # type: ignore[assignment]

    def test_connect_timeout_default_is_30s(self):
        """Default surfaces in adapter state.

        What to fix if this fails: ``SlackAdapterConfig.connect_timeout_s``
        must default to ~30s and propagate into the adapter, otherwise the
        timeout fix would silently regress to ``None`` / very small values.
        """
        adapter = _make_socket_adapter()
        assert adapter._socket_connect_timeout_s == 30.0


class TestForwardedSocketFreshness:
    """Regression for review finding #2.

    What to fix if this fails: ``handle_webhook`` must reject forwarded
    socket events whose ``timestamp`` field is outside the 5-minute window
    (mirroring ``_verify_signature``). Without this an attacker who
    captures one forwarded payload can replay it indefinitely (hazard #12).
    """

    async def test_replay_old_event_rejected(self):
        import time as _time

        adapter = _make_socket_adapter()
        adapter._chat = _make_mock_chat()
        forwarded = {
            "type": "socket_event",
            "eventType": "events_api",
            "body": {"type": "event_callback", "event": {}},
            "timestamp": int(_time.time()) - 6 * 60,  # 6 minutes old
        }
        request = _FakeRequest(
            json.dumps(forwarded),
            headers={
                "content-type": "application/json",
                "x-slack-socket-token": "fwd-secret",
            },
        )
        result = await adapter.handle_webhook(request)
        assert result["status"] == 401, "stale forwarded event must be rejected"

    async def test_missing_timestamp_rejected(self):
        adapter = _make_socket_adapter()
        adapter._chat = _make_mock_chat()
        forwarded = {
            "type": "socket_event",
            "eventType": "events_api",
            "body": {"type": "event_callback", "event": {}},
            # No "timestamp" — must not pass freshness check.
        }
        request = _FakeRequest(
            json.dumps(forwarded),
            headers={
                "content-type": "application/json",
                "x-slack-socket-token": "fwd-secret",
            },
        )
        result = await adapter.handle_webhook(request)
        assert result["status"] == 401

    async def test_js_emitted_milliseconds_timestamp_accepted(self):
        """Upstream's ``forwardSocketEvent`` always emits ``Date.now()``
        (milliseconds since epoch). The Python receiver must accept the JS
        wire format, not only the Python ``int(time.time())`` (seconds) shape.

        What to fix if this fails: in
        ``src/chat_sdk/adapters/slack/adapter.py`` ``handle_webhook``, the
        forwarded-event freshness check must auto-detect millisecond-shaped
        timestamps (anything > 10**11 — that magnitude crossed in 2001) and
        normalize to seconds before comparing to ``time.time()``. A naive
        seconds-only check rejects every real upstream-emitted forward with
        a ~56,000-year skew.
        """
        import time as _time

        adapter = _make_socket_adapter()
        adapter._chat = _make_mock_chat()
        # JS-shaped: ``Date.now()`` returns milliseconds.
        forwarded = {
            "type": "socket_event",
            "eventType": "events_api",
            "body": {"type": "event_callback", "event": {}},
            "timestamp": int(_time.time() * 1000),  # ms, like Date.now()
        }
        request = _FakeRequest(
            json.dumps(forwarded),
            headers={
                "content-type": "application/json",
                "x-slack-socket-token": "fwd-secret",
            },
        )
        result = await adapter.handle_webhook(request)
        assert result["status"] == 200, (
            "Forwarded event with JS-shaped Date.now() timestamp (ms) was "
            "rejected by the freshness check; the receiver must auto-detect "
            "ms vs s by magnitude"
        )

    async def test_js_emitted_milliseconds_replay_rejected(self):
        """A 6-minute-old JS-shaped (ms) timestamp must still be rejected."""
        import time as _time

        adapter = _make_socket_adapter()
        adapter._chat = _make_mock_chat()
        forwarded = {
            "type": "socket_event",
            "eventType": "events_api",
            "body": {"type": "event_callback", "event": {}},
            # 6 minutes old, in milliseconds
            "timestamp": int((_time.time() - 6 * 60) * 1000),
        }
        request = _FakeRequest(
            json.dumps(forwarded),
            headers={
                "content-type": "application/json",
                "x-slack-socket-token": "fwd-secret",
            },
        )
        result = await adapter.handle_webhook(request)
        assert result["status"] == 401


class TestInteractiveDispatchErrorAck:
    """Regression for review finding #5.

    What to fix if this fails: when ``_dispatch_interactive_payload`` raises
    in the socket-mode interactive branch, the ack must include
    ``response_action: errors`` instead of being empty. An empty ack on a
    ``view_submission`` silently closes the modal — the user gets no signal
    anything went wrong.
    """

    async def test_dispatch_exception_acks_with_errors_response_action(self):
        adapter = _make_socket_adapter()
        adapter._chat = _make_mock_chat()
        # Force the dispatcher to blow up.
        adapter._dispatch_interactive_payload = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("kaboom")
        )
        ack = AsyncMock()
        payload = {
            "type": "view_submission",
            "team": {"id": "T1"},
            "user": {"id": "U1", "name": "x"},
            "view": {"id": "V1", "callback_id": "cb", "private_metadata": "", "state": {"values": {}}},
            "trigger_id": "trig",
        }
        await adapter._route_socket_event(payload, "interactive", ack)
        assert ack.await_count == 1
        ack_args = ack.call_args
        assert ack_args.args, "ack must be called with a response payload, not empty"
        body_arg = ack_args.args[0]
        assert isinstance(body_arg, dict)
        assert body_arg.get("response_action") == "errors"
        assert "errors" in body_arg


class TestSocketEventsApiPayloadParity:
    """Regression for review finding #7.

    What to fix if this fails: the synthesized ``event_callback`` payload
    in the socket-mode events_api branch must match the webhook path.
    Adding ``is_ext_shared_channel`` here is a quiet socket-vs-webhook
    divergence — neither upstream nor the Python webhook path includes it.
    """

    async def test_synthesized_payload_does_not_include_is_ext_shared_channel(self):
        adapter = _make_socket_adapter()
        adapter._chat = _make_mock_chat()

        # Capture the payload handed to _process_event_payload.
        captured: list[dict[str, Any]] = []

        def fake_process(payload: dict[str, Any], _options: Any = None) -> None:
            captured.append(payload)

        adapter._process_event_payload = fake_process  # type: ignore[method-assign]
        ack = AsyncMock()
        body = {
            "team_id": "T1",
            "event_id": "Ev1",
            "event_time": 1234,
            "is_ext_shared_channel": True,  # Should be dropped.
            "event": {
                "type": "message",
                "channel": "C1",
                "ts": "1.0",
                "user": "U1",
                "text": "hi",
                "team": "T1",
            },
        }
        await adapter._route_socket_event(body, "events_api", ack)
        assert len(captured) == 1
        assert "is_ext_shared_channel" not in captured[0]
        # Sanity: the keys we *do* synthesize are still present.
        assert captured[0]["type"] == "event_callback"
        assert captured[0]["team_id"] == "T1"
        assert captured[0]["event_id"] == "Ev1"
        assert captured[0]["event_time"] == 1234
