"""Slack Enterprise Grid support (#268).

Ports the non-cache half of vercel/chat ``907450d7`` (#724, chat@4.35.0)
from ``packages/adapter-slack/src/index.test.ts``: org-wide OAuth installs,
``authorizations[0]`` event routing, Socket Mode per-installation token
resolution, ``team_id`` / ``client_context_team_id`` injection, the
``event_id`` retry marker and ``W``-prefixed user IDs.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

try:
    from chat_sdk.adapters.slack.adapter import SlackAdapter
    from chat_sdk.adapters.slack.types import RequestContext, SlackAdapterConfig, SlackInstallation
    from chat_sdk.shared.errors import AuthenticationError
    from chat_sdk.state.memory import MemoryStateAdapter

    _SLACK_AVAILABLE = True
except ImportError:
    _SLACK_AVAILABLE = False

pytestmark = [
    pytest.mark.skipif(not _SLACK_AVAILABLE, reason="Slack adapter import failed"),
    pytest.mark.asyncio,
]

_SECRET = "test-signing-secret"

_MESSAGE_EVENT = {
    "type": "message",
    "user": "U_USER",
    "channel": "C123",
    "text": "hello from socket",
    "ts": "1234567890.123456",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeRequest:
    def __init__(self, body: str, headers: dict[str, str] | None = None, url: str = "") -> None:
        self.body = body.encode("utf-8")
        self.headers = headers or {}
        self.url = url

    async def text(self) -> str:
        return self.body.decode("utf-8")


def _signed_request(
    body: str, content_type: str = "application/json", extra_headers: dict[str, str] | None = None
) -> _FakeRequest:
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(_SECRET.encode(), f"v0:{ts}:{body}".encode(), hashlib.sha256).hexdigest()
    headers = {"x-slack-request-timestamp": ts, "x-slack-signature": sig, "content-type": content_type}
    headers.update(extra_headers or {})
    return _FakeRequest(body, headers)


def _make_chat(state: Any) -> MagicMock:
    chat = MagicMock()
    chat.process_message = MagicMock()
    chat.process_action = MagicMock()
    chat.process_slash_command = MagicMock()
    chat.get_state = MagicMock(return_value=state)
    chat.get_user_name = MagicMock(return_value="test-bot")
    chat.get_logger = MagicMock(return_value=MagicMock())
    return chat


async def _memory_state() -> MemoryStateAdapter:
    state = MemoryStateAdapter()
    await state.connect()
    return state


async def _multi_workspace_adapter(**config: Any) -> tuple[SlackAdapter, MagicMock, MemoryStateAdapter]:
    """Multi-workspace adapter (no default bot token) over a real memory state."""
    state = await _memory_state()
    chat = _make_chat(state)
    adapter = SlackAdapter(SlackAdapterConfig(signing_secret=_SECRET, **config))
    await adapter.initialize(chat)  # type: ignore[arg-type]
    return adapter, chat, state


async def _single_workspace_adapter() -> tuple[SlackAdapter, MagicMock, MemoryStateAdapter]:
    state = await _memory_state()
    chat = _make_chat(state)
    adapter = SlackAdapter(SlackAdapterConfig(signing_secret=_SECRET, bot_token="xoxb-test-token", bot_user_id="U_BOT"))
    await adapter.initialize(chat)  # type: ignore[arg-type]
    return adapter, chat, state


def _spy_resolver(adapter: SlackAdapter) -> AsyncMock:
    """Wrap ``_resolve_token_for_team`` so calls are recorded but still resolve."""
    spy = AsyncMock(side_effect=adapter._resolve_token_for_team)
    adapter._resolve_token_for_team = spy  # type: ignore[method-assign]
    return spy


async def _settle() -> None:
    """Let fire-and-forget tasks (marker writes, slash dispatch) run."""
    for _ in range(10):
        await asyncio.sleep(0)


def _oauth_adapter(access_result: dict[str, Any]) -> SlackAdapter:
    adapter = SlackAdapter(
        SlackAdapterConfig(signing_secret=_SECRET, client_id="client-id", client_secret="client-secret")
    )
    client = MagicMock()
    client.oauth_v2_access = AsyncMock(return_value=access_result)
    adapter._client_cache[""] = client
    return adapter


def _oauth_request(code: str = "oauth-code") -> _FakeRequest:
    return _FakeRequest("", url=f"https://example.com/auth/callback/slack?code={code}")


_ORG_WIDE_ACCESS = {
    "ok": True,
    "access_token": "xoxb-org-bot-token",
    "bot_user_id": "U_BOT_ORG",
    "team": None,
    "enterprise": {"id": "E_ORG_1", "name": "Acme Org"},
    "is_enterprise_install": True,
}


# ---------------------------------------------------------------------------
# handleOAuthCallback
# ---------------------------------------------------------------------------


class TestHandleOAuthCallbackEnterprise:
    async def test_keys_org_wide_installs_by_enterprise_id_team_is_null(self):
        adapter = _oauth_adapter(dict(_ORG_WIDE_ACCESS))
        await adapter.initialize(_make_chat(await _memory_state()))  # type: ignore[arg-type]

        result = await adapter.handle_oauth_callback(_oauth_request("oauth-code-org"))

        assert result["team_id"] == "E_ORG_1"
        assert result["enterprise_id"] == "E_ORG_1"
        assert result["is_enterprise_install"] is True
        assert result["installation"].team_name == "Acme Org"

        # Stored under the enterprise ID: the same key org-wide webhooks
        # (is_enterprise_install: true) resolve tokens by.
        stored = await adapter.get_installation("E_ORG_1")
        assert stored is not None
        assert stored.bot_token == "xoxb-org-bot-token"
        assert stored.enterprise_id == "E_ORG_1"
        assert stored.is_enterprise_install is True

    async def test_records_the_enterprise_id_on_workspace_installs_within_a_grid_org(self):
        adapter = _oauth_adapter(
            {
                "ok": True,
                "access_token": "xoxb-grid-workspace-token",
                "bot_user_id": "U_BOT_GRID",
                "team": {"id": "T_GRID_1", "name": "Grid Workspace"},
                "enterprise": {"id": "E_ORG_1", "name": "Acme Org"},
                "is_enterprise_install": False,
            }
        )
        await adapter.initialize(_make_chat(await _memory_state()))  # type: ignore[arg-type]

        result = await adapter.handle_oauth_callback(_oauth_request("oauth-code-grid"))

        assert result["team_id"] == "T_GRID_1"
        assert result["enterprise_id"] == "E_ORG_1"
        assert result["is_enterprise_install"] is False

        stored = await adapter.get_installation("T_GRID_1")
        assert stored is not None
        assert stored.bot_token == "xoxb-grid-workspace-token"
        assert stored.team_name == "Grid Workspace"
        assert stored.enterprise_id == "E_ORG_1"
        assert stored.is_enterprise_install is None

    async def test_throws_when_an_org_wide_install_response_is_missing_enterprise_id(self):
        adapter = _oauth_adapter(
            {
                "ok": True,
                "access_token": "xoxb-org-bot-token",
                "team": None,
                "enterprise": None,
                "is_enterprise_install": True,
            }
        )
        state = await _memory_state()
        await adapter.initialize(_make_chat(state))  # type: ignore[arg-type]

        with pytest.raises(AuthenticationError, match="missing access_token or enterprise.id"):
            await adapter.handle_oauth_callback(_oauth_request("oauth-code-org"))

    async def test_org_wide_oauth_install_round_trips_with_org_wide_event_webhooks(self):
        adapter = _oauth_adapter(dict(_ORG_WIDE_ACCESS))
        chat = _make_chat(await _memory_state())
        await adapter.initialize(chat)  # type: ignore[arg-type]
        seen: list[RequestContext | None] = []
        chat.process_message.side_effect = lambda *a, **kw: seen.append(adapter._request_context.get())

        await adapter.handle_oauth_callback(_oauth_request())

        body = json.dumps(
            {
                "type": "event_callback",
                "team_id": "T_GRID_1",
                "enterprise_id": "E_ORG_1",
                "is_enterprise_install": True,
                "event": {**_MESSAGE_EVENT, "text": "Hello org", "channel": "C456"},
            }
        )
        response = await adapter.handle_webhook(_signed_request(body))

        assert response["status"] == 200
        assert chat.process_message.call_count == 1
        ctx = seen[0]
        assert ctx is not None
        assert (ctx.token, ctx.installation_id, ctx.team_id, ctx.is_enterprise_install) == (
            "xoxb-org-bot-token",
            "E_ORG_1",
            "T_GRID_1",
            True,
        )

    async def test_workspace_install_keeps_team_name_and_omits_enterprise_fields(self):
        """Python-specific: the stored shape of a plain (non-Grid) install is
        unchanged, so installations written before #268 read back the same."""
        adapter = _oauth_adapter(
            {
                "ok": True,
                "access_token": "xoxb-plain",
                "team": {"id": "T_PLAIN", "name": "Plain"},
            }
        )
        state = await _memory_state()
        await adapter.initialize(_make_chat(state))  # type: ignore[arg-type]

        result = await adapter.handle_oauth_callback(_oauth_request())

        assert (result["team_id"], result["enterprise_id"], result["is_enterprise_install"]) == ("T_PLAIN", None, False)
        assert await state.get("slack:installation:T_PLAIN") == {
            "botToken": "xoxb-plain",
            "botUserId": None,
            "teamName": "Plain",
        }


class TestEncryptedEnterpriseInstallation:
    async def test_enterprise_fields_round_trip_with_encryption(self):
        import base64
        import os

        key = base64.b64encode(os.urandom(32)).decode()
        adapter, _, _ = await _multi_workspace_adapter(encryption_key=key)

        await adapter.set_installation(
            "E_ORG_1",
            SlackInstallation(bot_token="xoxb-org", enterprise_id="E_ORG_1", is_enterprise_install=True),
        )
        stored = await adapter.get_installation("E_ORG_1")

        assert stored is not None
        assert (stored.bot_token, stored.enterprise_id, stored.is_enterprise_install) == ("xoxb-org", "E_ORG_1", True)


# ---------------------------------------------------------------------------
# socket mode - multi-workspace token resolution
# ---------------------------------------------------------------------------


class TestSocketModeMultiWorkspaceTokenResolution:
    async def test_resolves_the_per_workspace_token_for_events_api(self):
        adapter, chat, _ = await _multi_workspace_adapter()
        await adapter.set_installation("T_SOCK_1", SlackInstallation(bot_token="xoxb-sock-token", bot_user_id="U_BOT"))
        resolve = _spy_resolver(adapter)

        await adapter._route_socket_event(
            {"team_id": "T_SOCK_1", "event": dict(_MESSAGE_EVENT)}, "events_api", AsyncMock()
        )

        resolve.assert_awaited_once_with("T_SOCK_1", False)
        assert chat.process_message.call_count == 1

    async def test_resolves_org_wide_installs_by_enterprise_id_for_events_api(self):
        adapter, chat, _ = await _multi_workspace_adapter()
        await adapter.set_installation(
            "E_ORG_1",
            SlackInstallation(bot_token="xoxb-org-token", bot_user_id="U_BOT_ORG", is_enterprise_install=True),
        )
        resolve = _spy_resolver(adapter)

        await adapter._route_socket_event(
            {
                "team_id": "T_ANY",
                "enterprise_id": "E_ORG_1",
                "is_enterprise_install": True,
                "event": dict(_MESSAGE_EVENT),
            },
            "events_api",
            AsyncMock(),
        )

        resolve.assert_awaited_once_with("E_ORG_1", True)
        assert chat.process_message.call_count == 1

    async def test_drops_events_api_events_when_no_installation_is_found(self):
        adapter, chat, _ = await _multi_workspace_adapter()

        await adapter._route_socket_event(
            {"team_id": "T_UNKNOWN", "event": dict(_MESSAGE_EVENT)}, "events_api", AsyncMock()
        )

        chat.process_message.assert_not_called()

    async def test_resolves_tokens_for_slash_commands_with_boolean_is_enterprise_install(self):
        adapter, chat, _ = await _multi_workspace_adapter()
        await adapter.set_installation(
            "E_ORG_1", SlackInstallation(bot_token="xoxb-org-token", is_enterprise_install=True)
        )
        resolve = _spy_resolver(adapter)
        seen: list[RequestContext | None] = []
        chat.process_slash_command.side_effect = lambda *a, **kw: seen.append(adapter._request_context.get())

        await adapter._route_socket_event(
            {
                "command": "/test",
                "text": "arg1",
                "user_id": "U_USER",
                "channel_id": "C123",
                "team_id": "T_ANY",
                "enterprise_id": "E_ORG_1",
                # Socket mode delivers form fields as JSON, so this arrives boolean
                "is_enterprise_install": True,
            },
            "slash_commands",
            AsyncMock(),
        )
        await _settle()

        assert chat.process_slash_command.call_count == 1
        resolve.assert_awaited_once_with("E_ORG_1", True)
        ctx = seen[0]
        assert ctx is not None
        assert (ctx.token, ctx.installation_id, ctx.team_id) == ("xoxb-org-token", "E_ORG_1", "T_ANY")

    async def test_resolves_tokens_for_interactive_payloads(self):
        adapter, chat, _ = await _multi_workspace_adapter()
        await adapter.set_installation("T_SOCK_2", SlackInstallation(bot_token="xoxb-sock-token-2"))
        resolve = _spy_resolver(adapter)

        await adapter._route_socket_event(
            {
                "type": "block_actions",
                "team": {"id": "T_SOCK_2"},
                "actions": [{"type": "button", "action_id": "test_action", "value": "v"}],
                "channel": {"id": "C123", "name": "test"},
                "container": {"type": "message", "message_ts": "1234567890.123456", "channel_id": "C123"},
                "message": {"ts": "1234567890.123456"},
                "trigger_id": "trigger123",
                "user": {"id": "U_USER", "username": "testuser"},
            },
            "interactive",
            AsyncMock(),
        )

        resolve.assert_awaited_once_with("T_SOCK_2", False)
        assert chat.process_action.call_count == 1

    async def test_resolves_org_wide_interactive_payloads_by_enterprise_id(self):
        """Python-specific: the socket interactive path now honors the
        enterprise identity (it used to resolve by ``team.id`` only)."""
        adapter, chat, _ = await _multi_workspace_adapter()
        await adapter.set_installation("E_ORG_1", SlackInstallation(bot_token="xoxb-org", is_enterprise_install=True))
        resolve = _spy_resolver(adapter)
        seen: list[RequestContext | None] = []
        chat.process_action.side_effect = lambda *a, **kw: seen.append(adapter._request_context.get())

        await adapter._route_socket_event(
            {
                "type": "block_actions",
                "team": {"id": "T_GRID_1"},
                "enterprise": {"id": "E_ORG_1"},
                "is_enterprise_install": True,
                "actions": [{"type": "button", "action_id": "a", "value": "v"}],
                "channel": {"id": "C123", "name": "test"},
                "message": {"ts": "1234567890.123456"},
                "user": {"id": "U_USER", "username": "testuser"},
            },
            "interactive",
            AsyncMock(),
        )

        resolve.assert_awaited_once_with("E_ORG_1", True)
        ctx = seen[0]
        assert ctx is not None
        assert (ctx.token, ctx.installation_id, ctx.team_id, ctx.enterprise_id) == (
            "xoxb-org",
            "E_ORG_1",
            "T_GRID_1",
            "E_ORG_1",
        )


class TestHttpEnterpriseResolution:
    async def test_slash_command_with_form_enterprise_flag_resolves_by_enterprise_id(self):
        adapter, chat, _ = await _multi_workspace_adapter()
        await adapter.set_installation("E_ORG_1", SlackInstallation(bot_token="xoxb-org", is_enterprise_install=True))
        seen: list[RequestContext | None] = []
        chat.process_slash_command.side_effect = lambda *a, **kw: seen.append(adapter._request_context.get())
        body = (
            "command=%2Ftest&text=x&user_id=U1&channel_id=C1"
            "&team_id=T_GRID_1&enterprise_id=E_ORG_1&is_enterprise_install=true"
        )

        response = await adapter.handle_webhook(_signed_request(body, "application/x-www-form-urlencoded"))

        assert response["status"] == 200
        ctx = seen[0]
        assert ctx is not None
        assert (ctx.token, ctx.installation_id, ctx.team_id, ctx.is_enterprise_install) == (
            "xoxb-org",
            "E_ORG_1",
            "T_GRID_1",
            True,
        )

    async def test_interactive_payload_with_string_false_flag_resolves_by_team(self):
        """``is_enterprise_install: "false"`` must not count as an org-wide
        install (upstream ``=== true || === "true"``)."""
        adapter, chat, _ = await _multi_workspace_adapter()
        await adapter.set_installation("T_GRID_1", SlackInstallation(bot_token="xoxb-team"))
        resolve = _spy_resolver(adapter)
        payload = {
            "type": "block_actions",
            "team": {"id": "T_GRID_1"},
            "enterprise": {"id": "E_ORG_1"},
            "is_enterprise_install": "false",
            "actions": [{"type": "button", "action_id": "a", "value": "v"}],
            "channel": {"id": "C123", "name": "test"},
            "message": {"ts": "1234567890.123456"},
            "user": {"id": "U_USER", "username": "testuser"},
        }
        from urllib.parse import quote

        body = "payload=" + quote(json.dumps(payload))

        response = await adapter.handle_webhook(_signed_request(body, "application/x-www-form-urlencoded"))

        assert response["status"] == 200
        resolve.assert_awaited_once_with("T_GRID_1", False)
        assert chat.process_action.call_count == 1


# ---------------------------------------------------------------------------
# withToken enterprise context injection
# ---------------------------------------------------------------------------


def _run_in_context(adapter: SlackAdapter, ctx: RequestContext, **kwargs: Any) -> dict[str, Any]:
    tok = adapter._request_context.set(ctx)
    try:
        return adapter._with_token_kwargs(**kwargs)
    finally:
        adapter._request_context.reset(tok)


class TestWithTokenEnterpriseContextInjection:
    @staticmethod
    def _adapter() -> SlackAdapter:
        return SlackAdapter(SlackAdapterConfig(signing_secret=_SECRET))

    async def test_injects_team_id_on_org_wide_install_calls(self):
        result = _run_in_context(
            self._adapter(),
            RequestContext(token="xoxb-org", is_enterprise_install=True, team_id="T_EVENT_1"),
            channel="C1",
        )

        assert result == {"channel": "C1", "team_id": "T_EVENT_1"}

    async def test_does_not_inject_team_id_for_workspace_installs(self):
        result = _run_in_context(
            self._adapter(),
            RequestContext(token="xoxb-team", is_enterprise_install=False, team_id="T_EVENT_1"),
            channel="C1",
        )

        assert result == {"channel": "C1"}

    async def test_does_not_override_a_caller_specified_team_id(self):
        result = _run_in_context(
            self._adapter(),
            RequestContext(token="xoxb-org", is_enterprise_install=True, team_id="T_EVENT_1"),
            channel="C1",
            team_id="T_EXPLICIT",
        )

        assert result["team_id"] == "T_EXPLICIT"

    async def test_echoes_context_team_id_as_client_context_team_id_on_calls_to_the_originating_channel(self):
        result = _run_in_context(
            self._adapter(),
            RequestContext(token="xoxb-team", context_team_id="T_AWAY_HOST", context_channel="C1"),
            channel="C1",
            text="hi",
        )

        assert result["client_context_team_id"] == "T_AWAY_HOST"

    async def test_does_not_echo_client_context_team_id_to_a_different_channel(self):
        result = _run_in_context(
            self._adapter(),
            RequestContext(token="xoxb-team", context_team_id="T_AWAY_HOST", context_channel="C1"),
            channel="C_OTHER",
            text="hi",
        )

        assert result == {"channel": "C_OTHER", "text": "hi"}

    async def test_does_not_add_client_context_team_id_to_non_channel_calls(self):
        result = _run_in_context(
            self._adapter(),
            RequestContext(token="xoxb-team", context_team_id="T_AWAY_HOST", context_channel="C1"),
            user="U1",
        )

        assert result == {"user": "U1"}

    async def test_captures_team_id_and_context_team_id_in_the_event_request_context(self):
        adapter, _, _ = await _multi_workspace_adapter()
        await adapter.set_installation("E_ORG_1", SlackInstallation(bot_token="xoxb-org", is_enterprise_install=True))

        resolved = await adapter._resolve_event_request_context(
            {
                "type": "event_callback",
                "team_id": "T_GRID_1",
                "enterprise_id": "E_ORG_1",
                "is_enterprise_install": True,
                # context_team_id is a top-level envelope field, not inside `event`.
                "context_team_id": "T_AWAY_HOST",
                "event": {"type": "message", "channel": "C1", "ts": "1.1"},
            }
        )

        assert isinstance(resolved, RequestContext)
        assert (
            resolved.installation_id,
            resolved.is_enterprise_install,
            resolved.team_id,
            resolved.context_team_id,
            resolved.context_channel,
        ) == ("E_ORG_1", True, "T_GRID_1", "T_AWAY_HOST", "C1")

    async def test_no_request_context_leaves_kwargs_unchanged(self):
        assert self._adapter()._with_token_kwargs(channel="C1", text="hi") == {"channel": "C1", "text": "hi"}

    async def test_api_calls_carry_the_resolved_enterprise_context(self):
        """Python-specific wiring check: the adapter's Web API call sites go
        through ``_with_token_kwargs`` (upstream ``withToken``)."""
        adapter, _, _ = await _multi_workspace_adapter()
        await adapter.set_installation("E_ORG_1", SlackInstallation(bot_token="xoxb-org", is_enterprise_install=True))
        resolved = await adapter._resolve_event_request_context(
            {
                "type": "event_callback",
                "team_id": "T_GRID_1",
                "enterprise_id": "E_ORG_1",
                "is_enterprise_install": True,
                "context_team_id": "T_AWAY_HOST",
                "event": {"type": "message", "channel": "C1", "ts": "1.1"},
            }
        )
        assert isinstance(resolved, RequestContext)
        client = MagicMock()
        client.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "2.2"})
        client.conversations_info = AsyncMock(return_value={"ok": True, "channel": {"name": "x"}})
        client.api_call = AsyncMock(return_value={"ok": True})
        adapter._get_client = lambda token=None: client  # type: ignore[method-assign]

        tok = adapter._request_context.set(resolved)
        try:
            await adapter.post_message("slack:C1:1.1", "hello")  # type: ignore[arg-type]
            await adapter.fetch_channel_info("slack:C_OTHER")
            # ``api_call`` path: the ``json`` body is what goes through withToken.
            await adapter.set_suggested_prompts("C1", None, [{"title": "t", "message": "m"}])
        finally:
            adapter._request_context.reset(tok)

        post_kwargs = client.chat_postMessage.await_args.kwargs
        assert (post_kwargs["team_id"], post_kwargs["client_context_team_id"]) == ("T_GRID_1", "T_AWAY_HOST")
        info_kwargs = client.conversations_info.await_args.kwargs
        assert info_kwargs == {"channel": "C_OTHER", "team_id": "T_GRID_1"}
        assert client.api_call.await_args.kwargs["json"] == {
            "channel_id": "C1",
            "prompts": [{"title": "t", "message": "m"}],
            "team_id": "T_GRID_1",
        }


# ---------------------------------------------------------------------------
# event delivery deduplication
# ---------------------------------------------------------------------------


def _event_body(event_id: str) -> str:
    return json.dumps(
        {
            "type": "event_callback",
            "team_id": "T123",
            "event_id": event_id,
            "event": {
                "type": "message",
                "user": "U_USER",
                "channel": "C123",
                "text": "hello",
                "ts": "1234567890.123456",
            },
        }
    )


class TestEventDeliveryDeduplication:
    async def test_drops_a_retried_delivery_of_an_already_dispatched_event(self):
        adapter, chat, state = await _single_workspace_adapter()

        await adapter.handle_webhook(_signed_request(_event_body("Ev1")))
        # Fire-and-forget marker write
        await _settle()
        assert chat.process_message.call_count == 1
        assert await state.get("slack:event-delivered:Ev1") is True

        retry = _signed_request(_event_body("Ev1"), extra_headers={"x-slack-retry-num": "1"})
        response = await adapter.handle_webhook(retry)

        assert response["status"] == 200
        assert chat.process_message.call_count == 1

    async def test_processes_a_retry_when_the_original_delivery_was_never_dispatched(self):
        adapter, chat, _ = await _single_workspace_adapter()

        retry = _signed_request(_event_body("Ev_missed"), extra_headers={"x-slack-retry-num": "2"})
        response = await adapter.handle_webhook(retry)

        assert response["status"] == 200
        assert chat.process_message.call_count == 1

    async def test_does_not_consult_state_on_first_deliveries(self):
        adapter, chat, state = await _single_workspace_adapter()
        get_spy = AsyncMock(side_effect=state.get)
        state.get = get_spy  # type: ignore[method-assign]

        await adapter.handle_webhook(_signed_request(_event_body("Ev2")))

        assert chat.process_message.call_count == 1
        assert "slack:event-delivered:Ev2" not in [c.args[0] for c in get_spy.await_args_list]

    async def test_dedupes_retried_socket_deliveries_by_event_id(self):
        adapter, chat, _ = await _single_workspace_adapter()
        body = {
            "team_id": "T123",
            "event_id": "Ev_sock",
            "event": dict(_MESSAGE_EVENT),
        }

        await adapter._route_socket_event(body, "events_api", AsyncMock())
        await _settle()
        assert chat.process_message.call_count == 1

        await adapter._route_socket_event(body, "events_api", AsyncMock(), None, 1)

        assert chat.process_message.call_count == 1

    async def test_marker_is_written_with_a_24_hour_ttl(self):
        adapter, _, state = await _single_workspace_adapter()
        set_spy = AsyncMock(side_effect=state.set)
        state.set = set_spy  # type: ignore[method-assign]

        await adapter.handle_webhook(_signed_request(_event_body("Ev_ttl")))
        await _settle()

        set_spy.assert_awaited_once_with("slack:event-delivered:Ev_ttl", True, 24 * 60 * 60 * 1000)

    async def test_retry_is_processed_when_the_state_read_fails(self):
        adapter, chat, state = await _single_workspace_adapter()
        await state.set("slack:event-delivered:Ev_err", True)
        state.get = AsyncMock(side_effect=RuntimeError("state down"))  # type: ignore[method-assign]

        retry = _signed_request(_event_body("Ev_err"), extra_headers={"x-slack-retry-num": "1"})
        response = await adapter.handle_webhook(retry)

        assert response["status"] == 200
        assert chat.process_message.call_count == 1

    async def test_a_failed_marker_write_does_not_fail_the_webhook(self):
        adapter, chat, state = await _single_workspace_adapter()
        state.set = AsyncMock(side_effect=RuntimeError("state down"))  # type: ignore[method-assign]

        response = await adapter.handle_webhook(_signed_request(_event_body("Ev_write")))
        await _settle()

        assert response["status"] == 200
        assert chat.process_message.call_count == 1

    async def test_malformed_retry_header_counts_as_a_first_delivery(self):
        adapter, chat, state = await _single_workspace_adapter()
        await state.set("slack:event-delivered:Ev_bad", True)

        retry = _signed_request(_event_body("Ev_bad"), extra_headers={"x-slack-retry-num": "not-a-number"})
        await adapter.handle_webhook(retry)

        assert chat.process_message.call_count == 1

    async def test_forwarded_socket_event_retry_num_drops_a_dispatched_event(self):
        state = await _memory_state()
        chat = _make_chat(state)
        adapter = SlackAdapter(
            SlackAdapterConfig(
                mode="socket",
                app_token="xapp-1-x",
                bot_token="xoxb-test-token",
                socket_forwarding_secret="fwd-secret",
            )
        )
        adapter._chat = chat
        await state.set("slack:event-delivered:Ev_fwd", True)

        forwarded = json.dumps(
            {
                "type": "socket_event",
                "eventType": "events_api",
                "body": {"team_id": "T123", "event_id": "Ev_fwd", "event": dict(_MESSAGE_EVENT)},
                "retryNum": 1,
                "timestamp": int(time.time() * 1000),
            }
        )
        response = await adapter.handle_webhook(_FakeRequest(forwarded, {"x-slack-socket-token": "fwd-secret"}))

        assert response["status"] == 200
        chat.process_message.assert_not_called()


# ---------------------------------------------------------------------------
# W-prefixed enterprise user IDs
# ---------------------------------------------------------------------------


class TestWPrefixedEnterpriseUserIds:
    async def test_treats_bare_at_w_mentions_as_raw_user_ids_not_display_names(self):
        adapter, _, state = await _single_workspace_adapter()
        get_list_spy = AsyncMock(side_effect=state.get_list)
        state.get_list = get_list_spy  # type: ignore[method-assign]

        result = await adapter._resolve_outgoing_mentions("Hey @W012345AB, ping", "slack:C1:1.1")

        # Left for the markdown layer to render as <@W012345AB>, with no
        # reverse-index lookup attempted for "w012345ab"
        assert result == "Hey @W012345AB, ping"
        assert "slack:user-by-name:w012345ab" not in [c.args[0] for c in get_list_spy.await_args_list]

    async def test_resolves_incoming_at_w_mentions_like_u_prefixed_ones(self):
        adapter, _, _ = await _single_workspace_adapter()
        client = MagicMock()
        client.users_info = AsyncMock(
            return_value={
                "user": {
                    "name": "wanda",
                    "profile": {"display_name": "Wanda", "real_name": "Wanda Grid"},
                    "real_name": "Wanda Grid",
                }
            }
        )
        adapter._get_client = lambda token=None: client  # type: ignore[method-assign]

        message = await adapter._parse_slack_message(
            {
                "type": "message",
                "user": "W_SENDER_1",
                "username": "sender",
                "text": "hello <@W012345AB>",
                "ts": "1234567890.123456",
                "channel": "C123",
            },
            "slack:C123:1234567890.123456",
        )

        assert "Wanda" in message.text


# ---------------------------------------------------------------------------
# event routing via authorizations[]
# ---------------------------------------------------------------------------


class TestEventRoutingViaAuthorizations:
    async def test_prefers_authorizations_0_over_top_level_fields_for_org_installs(self):
        adapter, _, _ = await _multi_workspace_adapter()
        await adapter.set_installation("E_ORG_1", SlackInstallation(bot_token="xoxb-org", is_enterprise_install=True))

        # Envelope where the org identity lives only in authorizations (the
        # documented location); top-level omits is_enterprise_install.
        resolved = await adapter._resolve_event_request_context(
            {
                "type": "event_callback",
                "team_id": "T_GRID_1",
                "event": dict(_MESSAGE_EVENT),
                "authorizations": [{"enterprise_id": "E_ORG_1", "team_id": None, "is_enterprise_install": True}],
            }
        )

        assert isinstance(resolved, RequestContext)
        assert (resolved.installation_id, resolved.is_enterprise_install, resolved.token, resolved.team_id) == (
            "E_ORG_1",
            True,
            "xoxb-org",
            "T_GRID_1",
        )

    async def test_uses_the_authorizations_team_over_a_slack_connect_top_level_team(self):
        adapter, _, _ = await _multi_workspace_adapter()
        await adapter.set_installation("T_RECIPIENT", SlackInstallation(bot_token="xoxb-recipient"))

        # Shared-channel envelope: top-level names the other org's workspace,
        # authorizations[0] names the actual recipient installation
        resolved = await adapter._resolve_event_request_context(
            {
                "type": "event_callback",
                "team_id": "T_OTHER_ORG",
                "enterprise_id": "E_OTHER_ORG",
                "event": dict(_MESSAGE_EVENT),
                "authorizations": [{"enterprise_id": None, "team_id": "T_RECIPIENT", "is_enterprise_install": False}],
            }
        )

        assert isinstance(resolved, RequestContext)
        assert (resolved.installation_id, resolved.is_enterprise_install, resolved.token) == (
            "T_RECIPIENT",
            False,
            "xoxb-recipient",
        )

    async def test_falls_back_to_top_level_fields_when_authorizations_is_absent(self):
        adapter, _, _ = await _multi_workspace_adapter()
        await adapter.set_installation("E_ORG_1", SlackInstallation(bot_token="xoxb-org", is_enterprise_install=True))

        resolved = await adapter._resolve_event_request_context(
            {
                "type": "event_callback",
                "team_id": "T_GRID_1",
                "enterprise_id": "E_ORG_1",
                "is_enterprise_install": True,
                "event": dict(_MESSAGE_EVENT),
            }
        )

        assert isinstance(resolved, RequestContext)
        assert (resolved.installation_id, resolved.is_enterprise_install, resolved.token) == (
            "E_ORG_1",
            True,
            "xoxb-org",
        )

    async def test_resolution_states_for_single_workspace_missing_ids_and_unknown_installs(self):
        single, _, _ = await _single_workspace_adapter()
        multi, _, _ = await _multi_workspace_adapter()
        event = {"type": "event_callback", "event": dict(_MESSAGE_EVENT)}

        assert await single._resolve_event_request_context({**event, "team_id": "T1"}) == "not-applicable"
        assert await multi._resolve_event_request_context(event) == "not-applicable"
        assert await multi._resolve_event_request_context({"type": "url_verification"}) == "not-applicable"
        assert await multi._resolve_event_request_context({**event, "team_id": "T_UNKNOWN"}) == "unresolved"

    async def test_http_webhook_drops_events_for_unknown_authorizations(self):
        adapter, chat, _ = await _multi_workspace_adapter()
        await adapter.set_installation("T_OTHER_ORG", SlackInstallation(bot_token="xoxb-other"))
        body = json.dumps(
            {
                "type": "event_callback",
                "team_id": "T_OTHER_ORG",
                "event": dict(_MESSAGE_EVENT),
                "authorizations": [{"team_id": "T_NOT_INSTALLED", "is_enterprise_install": False}],
            }
        )

        response = await adapter.handle_webhook(_signed_request(body))

        assert response["status"] == 200
        chat.process_message.assert_not_called()
