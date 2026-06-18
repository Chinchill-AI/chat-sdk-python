"""Tests for the Slack external installation provider (vercel/chat#467).

Port of the ``describe("installationProvider")`` block from
packages/adapter-slack/src/index.test.ts (chat@4.29.0).

When ``installation_provider`` is configured, per-installation token lookups
bypass the internal StateAdapter. For Enterprise Grid org-wide installs
(``is_enterprise_install``), the lookup key is the enterprise ID instead of
the team ID.
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
    from chat_sdk.adapters.slack.types import SlackAdapterConfig, SlackInstallation
    from chat_sdk.shared.errors import AuthenticationError
    from chat_sdk.types import Attachment

    _SLACK_AVAILABLE = True
except ImportError:
    _SLACK_AVAILABLE = False

pytestmark = pytest.mark.skipif(not _SLACK_AVAILABLE, reason="Slack adapter import failed")

SECRET = "test-signing-secret"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeRequest:
    def __init__(self, body: str, headers: dict[str, str]):
        self.body = body.encode("utf-8")
        self.headers = headers
        self.url = ""

    async def text(self) -> str:
        return self.body.decode("utf-8")


def _make_signed_request(body: str, content_type: str = "application/json") -> _FakeRequest:
    ts = str(int(time.time()))
    sig_base = f"v0:{ts}:{body}"
    sig = "v0=" + hmac.new(SECRET.encode(), sig_base.encode(), hashlib.sha256).hexdigest()
    return _FakeRequest(
        body,
        {
            "x-slack-request-timestamp": ts,
            "x-slack-signature": sig,
            "content-type": content_type,
        },
    )


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


def _make_mock_chat(state: MagicMock) -> MagicMock:
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


def _make_provider(installation: SlackInstallation | None) -> MagicMock:
    provider = MagicMock()
    provider.get_installation = AsyncMock(return_value=installation)
    return provider


async def _make_provider_adapter(
    provider: MagicMock,
) -> tuple[SlackAdapter, MagicMock, MagicMock]:
    """Multi-workspace adapter (no bot_token) with an installation provider."""
    state = _make_mock_state()
    chat = _make_mock_chat(state)
    adapter = SlackAdapter(SlackAdapterConfig(signing_secret=SECRET, installation_provider=provider))
    await adapter.initialize(chat)
    return adapter, chat, state


def _event_body(**envelope: Any) -> str:
    body = {
        "type": "event_callback",
        "event": {
            "type": "message",
            "channel": "C_TEST",
            "user": "U_USER",
            "username": "testuser",
            "text": "hello",
            "ts": "1234567890.123456",
        },
    }
    body.update(envelope)
    return json.dumps(body)


def _slash_body(params: dict[str, str]) -> str:
    from urllib.parse import urlencode

    return urlencode(params)


def _interactive_body(payload: dict[str, Any]) -> str:
    from urllib.parse import quote

    return f"payload={quote(json.dumps(payload))}"


def _block_actions_payload(**extra: Any) -> dict[str, Any]:
    payload = {
        "type": "block_actions",
        "user": {"id": "U123", "username": "testuser", "name": "Test User"},
        "container": {
            "type": "message",
            "message_ts": "1234567890.123456",
            "channel_id": "C_INTER",
        },
        "channel": {"id": "C_INTER", "name": "general"},
        "message": {"ts": "1234567890.123456"},
        "actions": [{"type": "button", "action_id": "test_action", "value": "v"}],
    }
    payload.update(extra)
    return payload


# ---------------------------------------------------------------------------
# event_callback resolution
# ---------------------------------------------------------------------------


class TestInstallationProviderEvents:
    @pytest.mark.asyncio
    async def test_uses_provider_for_token_resolution_in_event_callback(self):
        provider = _make_provider(SlackInstallation(bot_token="xoxb-external-token", bot_user_id="U_BOT_EXT"))
        adapter, _, _ = await _make_provider_adapter(provider)

        req = _make_signed_request(_event_body(team_id="T_EXTERNAL_1"))
        response = await adapter.handle_webhook(req)

        assert response["status"] == 200
        provider.get_installation.assert_called_once_with("T_EXTERNAL_1", False)

    @pytest.mark.asyncio
    async def test_uses_enterprise_id_when_is_enterprise_install_true(self):
        provider = _make_provider(SlackInstallation(bot_token="xoxb-enterprise-token", bot_user_id="U_BOT_ENT"))
        adapter, _, _ = await _make_provider_adapter(provider)

        req = _make_signed_request(
            _event_body(
                team_id="T_WORKSPACE_1",
                enterprise_id="E_ENTERPRISE_1",
                is_enterprise_install=True,
            )
        )
        response = await adapter.handle_webhook(req)

        assert response["status"] == 200
        provider.get_installation.assert_called_once_with("E_ENTERPRISE_1", True)

    @pytest.mark.asyncio
    async def test_uses_team_id_when_is_enterprise_install_false(self):
        provider = _make_provider(SlackInstallation(bot_token="xoxb-team-token", bot_user_id="U_BOT_TEAM"))
        adapter, _, _ = await _make_provider_adapter(provider)

        req = _make_signed_request(
            _event_body(
                team_id="T_TEAM_ONLY",
                enterprise_id="E_SHOULD_IGNORE",
                is_enterprise_install=False,
            )
        )
        await adapter.handle_webhook(req)

        provider.get_installation.assert_called_once_with("T_TEAM_ONLY", False)

    @pytest.mark.asyncio
    async def test_returns_200_ok_when_provider_returns_none(self):
        provider = _make_provider(None)
        adapter, _, _ = await _make_provider_adapter(provider)

        req = _make_signed_request(_event_body(team_id="T_UNKNOWN"))
        response = await adapter.handle_webhook(req)

        assert response["status"] == 200
        provider.get_installation.assert_called_once_with("T_UNKNOWN", False)

    @pytest.mark.asyncio
    async def test_does_not_fall_back_to_state_when_provider_is_set(self):
        provider = _make_provider(None)
        adapter, chat, state = await _make_provider_adapter(provider)

        # Set an installation in state - should NOT be used
        await adapter.set_installation(
            "T_STATE_TEAM",
            SlackInstallation(bot_token="xoxb-state-token", bot_user_id="U_BOT_STATE"),
        )

        req = _make_signed_request(_event_body(team_id="T_STATE_TEAM"))
        response = await adapter.handle_webhook(req)

        assert response["status"] == 200
        provider.get_installation.assert_called_once_with("T_STATE_TEAM", False)
        # Event must not be processed because the provider returned None --
        # falling back to the state-stored token here would mean the provider
        # is not authoritative.
        chat.process_message.assert_not_called()
        # The internal installation key must never have been read.
        read_keys = [str(c.args[0]) for c in state.get.call_args_list]
        assert not any("slack:installation" in k for k in read_keys)

    @pytest.mark.asyncio
    async def test_provider_token_reaches_request_context(self):
        """The resolved installation's token is what downstream API calls use."""
        provider = _make_provider(SlackInstallation(bot_token="xoxb-ctx-token", bot_user_id="U_BOT_CTX"))
        adapter, chat, _ = await _make_provider_adapter(provider)

        seen_tokens: list[str] = []

        def capture(adapter_arg: Any, thread_id: str, factory: Any, options: Any = None) -> None:
            seen_tokens.append(adapter_arg._get_token())

        chat.process_message = MagicMock(side_effect=capture)

        req = _make_signed_request(_event_body(team_id="T_CTX"))
        await adapter.handle_webhook(req)

        assert seen_tokens == ["xoxb-ctx-token"]


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------


class TestInstallationProviderSlashCommands:
    @pytest.mark.asyncio
    async def test_uses_provider_for_slash_commands(self):
        provider = _make_provider(SlackInstallation(bot_token="xoxb-slash-token", bot_user_id="U_BOT_SLASH"))
        adapter, _, _ = await _make_provider_adapter(provider)

        body = _slash_body(
            {
                "command": "/test",
                "text": "hello",
                "team_id": "T_SLASH_TEAM",
                "channel_id": "C_SLASH",
                "user_id": "U_SLASHER",
                "response_url": "https://hooks.slack.com/commands/xxx",
            }
        )
        req = _make_signed_request(body, content_type="application/x-www-form-urlencoded")
        await adapter.handle_webhook(req)

        provider.get_installation.assert_called_once_with("T_SLASH_TEAM", False)

    @pytest.mark.asyncio
    async def test_uses_enterprise_id_for_slash_commands_in_enterprise_grid(self):
        provider = _make_provider(SlackInstallation(bot_token="xoxb-ent-slash-token", bot_user_id="U_BOT_ENT_SLASH"))
        adapter, _, _ = await _make_provider_adapter(provider)

        body = _slash_body(
            {
                "command": "/test",
                "text": "hello",
                "team_id": "T_ENT_WORKSPACE",
                "enterprise_id": "E_ENT_ORG",
                "is_enterprise_install": "true",
                "channel_id": "C_SLASH",
                "user_id": "U_SLASHER",
                "response_url": "https://hooks.slack.com/commands/xxx",
            }
        )
        req = _make_signed_request(body, content_type="application/x-www-form-urlencoded")
        await adapter.handle_webhook(req)

        provider.get_installation.assert_called_once_with("E_ENT_ORG", True)


# ---------------------------------------------------------------------------
# Interactive payloads
# ---------------------------------------------------------------------------


class TestInstallationProviderInteractive:
    @pytest.mark.asyncio
    async def test_uses_provider_for_interactive_payloads(self):
        provider = _make_provider(SlackInstallation(bot_token="xoxb-interactive-token", bot_user_id="U_BOT_INTER"))
        adapter, _, _ = await _make_provider_adapter(provider)

        body = _interactive_body(_block_actions_payload(team={"id": "T_INTER_PROVIDER"}))
        req = _make_signed_request(body, content_type="application/x-www-form-urlencoded")
        response = await adapter.handle_webhook(req)

        assert response["status"] == 200
        provider.get_installation.assert_called_once_with("T_INTER_PROVIDER", False)

    @pytest.mark.asyncio
    async def test_uses_enterprise_id_for_interactive_payloads_in_enterprise_grid(self):
        provider = _make_provider(
            SlackInstallation(bot_token="xoxb-ent-interactive-token", bot_user_id="U_BOT_ENT_INTER")
        )
        adapter, _, _ = await _make_provider_adapter(provider)

        body = _interactive_body(
            _block_actions_payload(
                team={"id": "T_ENT_INTER_WORKSPACE"},
                enterprise={"id": "E_ENT_INTER_ORG"},
                is_enterprise_install=True,
            )
        )
        req = _make_signed_request(body, content_type="application/x-www-form-urlencoded")
        response = await adapter.handle_webhook(req)

        assert response["status"] == 200
        provider.get_installation.assert_called_once_with("E_ENT_INTER_ORG", True)


# ---------------------------------------------------------------------------
# rehydrate_attachment
# ---------------------------------------------------------------------------


class TestInstallationProviderRehydrate:
    @pytest.mark.asyncio
    async def test_rehydrate_attachment_uses_provider_for_token_resolution(self):
        provider = _make_provider(SlackInstallation(bot_token="xoxb-rehydrate-token", bot_user_id="U_BOT_REHYDRATE"))
        adapter, _, _ = await _make_provider_adapter(provider)
        adapter._fetch_slack_file = AsyncMock(return_value=b"\x00" * 8)  # type: ignore[method-assign]

        rehydrated = adapter.rehydrate_attachment(
            Attachment(
                type="image",
                url="https://files.slack.com/img.png",
                fetch_metadata={
                    "url": "https://files.slack.com/img.png",
                    "teamId": "T_REHYDRATE",
                },
            )
        )

        assert rehydrated.fetch_data is not None
        await rehydrated.fetch_data()

        provider.get_installation.assert_called_once_with("T_REHYDRATE", False)
        adapter._fetch_slack_file.assert_awaited_once_with("https://files.slack.com/img.png", "xoxb-rehydrate-token")

    @pytest.mark.asyncio
    async def test_rehydrate_attachment_uses_enterprise_id_when_enterprise_install(self):
        provider = _make_provider(
            SlackInstallation(bot_token="xoxb-ent-rehydrate-token", bot_user_id="U_BOT_ENT_REHYDRATE")
        )
        adapter, _, _ = await _make_provider_adapter(provider)
        adapter._fetch_slack_file = AsyncMock(return_value=b"\x00" * 8)  # type: ignore[method-assign]

        rehydrated = adapter.rehydrate_attachment(
            Attachment(
                type="image",
                url="https://files.slack.com/img.png",
                fetch_metadata={
                    "url": "https://files.slack.com/img.png",
                    "teamId": "T_WORKSPACE",
                    "enterpriseId": "E_ORG",
                    "isEnterpriseInstall": "true",
                },
            )
        )

        assert rehydrated.fetch_data is not None
        await rehydrated.fetch_data()

        provider.get_installation.assert_called_once_with("E_ORG", True)
        adapter._fetch_slack_file.assert_awaited_once_with(
            "https://files.slack.com/img.png", "xoxb-ent-rehydrate-token"
        )

    @pytest.mark.asyncio
    async def test_rehydrate_attachment_raises_when_provider_returns_none(self):
        provider = _make_provider(None)
        adapter, _, _ = await _make_provider_adapter(provider)

        rehydrated = adapter.rehydrate_attachment(
            Attachment(
                type="image",
                url="https://files.slack.com/img.png",
                fetch_metadata={
                    "url": "https://files.slack.com/img.png",
                    "teamId": "T_MISSING",
                },
            )
        )

        assert rehydrated.fetch_data is not None
        with pytest.raises(AuthenticationError, match="Installation not found for team T_MISSING"):
            await rehydrated.fetch_data()
        provider.get_installation.assert_called_once_with("T_MISSING", False)


# ---------------------------------------------------------------------------
# Enterprise Grid metadata capture on attachments
# ---------------------------------------------------------------------------


class TestEnterpriseGridAttachmentMetadata:
    @pytest.mark.asyncio
    async def test_event_callback_with_file_captures_enterprise_grid_metadata(self):
        provider = _make_provider(SlackInstallation(bot_token="xoxb-ent-event-token", bot_user_id="U_BOT_ENT_EVENT"))
        adapter, chat, _ = await _make_provider_adapter(provider)

        # Invoke the factory while still inside the request-context frame so
        # _create_attachment can read the per-request enterprise context. The
        # task is created during process_message, before the isolated context
        # exits, so the contextvars snapshot carries the enterprise fields.
        captured: list[asyncio.Task[Any]] = []

        def capture(adapter_arg: Any, thread_id: str, factory: Any, options: Any = None) -> None:
            captured.append(asyncio.get_running_loop().create_task(factory()))

        chat.process_message = MagicMock(side_effect=capture)

        body = json.dumps(
            {
                "type": "event_callback",
                "team_id": "T_ENT_WORKSPACE",
                "enterprise_id": "E_ENT_FILE_ORG",
                "is_enterprise_install": True,
                "event": {
                    "type": "message",
                    "channel": "C_TEST",
                    "user": "U_USER",
                    # username present so the parse skips the user-lookup API call
                    "username": "testuser",
                    "text": "with file",
                    "ts": "1234567890.123456",
                    "files": [
                        {
                            "id": "F1",
                            "mimetype": "image/png",
                            "url_private": "https://files.slack.com/captured.png",
                            "name": "captured.png",
                        }
                    ],
                },
            }
        )
        req = _make_signed_request(body)
        await adapter.handle_webhook(req)

        assert len(captured) == 1
        message = await captured[0]
        attachment = message.attachments[0]
        assert attachment.fetch_metadata == {
            "url": "https://files.slack.com/captured.png",
            "teamId": "T_ENT_WORKSPACE",
            "enterpriseId": "E_ENT_FILE_ORG",
            "isEnterpriseInstall": "true",
        }

    @pytest.mark.asyncio
    async def test_non_enterprise_event_omits_enterprise_grid_metadata_keys(self):
        """Plain workspace installs must not serialize enterprise keys
        (omit, not ``None``/``"false"`` -- hazard #7)."""
        provider = _make_provider(SlackInstallation(bot_token="xoxb-plain-token", bot_user_id="U_BOT_PLAIN"))
        adapter, chat, _ = await _make_provider_adapter(provider)

        captured: list[asyncio.Task[Any]] = []

        def capture(adapter_arg: Any, thread_id: str, factory: Any, options: Any = None) -> None:
            captured.append(asyncio.get_running_loop().create_task(factory()))

        chat.process_message = MagicMock(side_effect=capture)

        body = json.dumps(
            {
                "type": "event_callback",
                "team_id": "T_PLAIN",
                "event": {
                    "type": "message",
                    "channel": "C_TEST",
                    "user": "U_USER",
                    "username": "testuser",
                    "text": "with file",
                    "ts": "1234567890.123456",
                    "files": [
                        {
                            "id": "F2",
                            "mimetype": "image/png",
                            "url_private": "https://files.slack.com/plain.png",
                            "name": "plain.png",
                        }
                    ],
                },
            }
        )
        req = _make_signed_request(body)
        await adapter.handle_webhook(req)

        assert len(captured) == 1
        message = await captured[0]
        attachment = message.attachments[0]
        assert attachment.fetch_metadata == {
            "url": "https://files.slack.com/plain.png",
            "teamId": "T_PLAIN",
        }
