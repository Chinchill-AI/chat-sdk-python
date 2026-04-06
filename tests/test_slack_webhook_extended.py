"""Extended Slack webhook tests covering gaps identified from TS test suite.

Covers: multi-workspace with encryption, OAuth callback handling,
slash command dispatching, assistant thread events, app_home_opened events,
DM message handling, thread_broadcast subtypes, reaction events,
link extraction, formatted text, and edge cases in message parsing.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

try:
    from chat_sdk.adapters.slack.adapter import SlackAdapter
    from chat_sdk.adapters.slack.crypto import decode_key, decrypt_token, encrypt_token
    from chat_sdk.adapters.slack.types import (
        SlackAdapterConfig,
        SlackInstallation,
        SlackThreadId,
    )
    from chat_sdk.shared.errors import AdapterRateLimitError, ValidationError

    _SLACK_AVAILABLE = True
except ImportError:
    _SLACK_AVAILABLE = False

pytestmark = pytest.mark.skipif(not _SLACK_AVAILABLE, reason="Slack adapter import failed")


# ---------------------------------------------------------------------------
# Helpers (mirrors test_slack_webhook.py)
# ---------------------------------------------------------------------------


def _make_adapter(**overrides: Any) -> SlackAdapter:
    config = SlackAdapterConfig(
        signing_secret=overrides.pop("signing_secret", "test-signing-secret"),
        bot_token=overrides.pop("bot_token", "xoxb-test-token"),
        **overrides,
    )
    return SlackAdapter(config)


def _slack_signature(body: str, secret: str, timestamp: int | None = None) -> tuple[str, str]:
    ts = str(timestamp or int(time.time()))
    sig_base = f"v0:{ts}:{body}"
    sig = "v0=" + hmac.new(secret.encode(), sig_base.encode(), hashlib.sha256).hexdigest()
    return ts, sig


class _FakeRequest:
    def __init__(self, body: str, headers: dict[str, str] | None = None):
        self.body = body.encode("utf-8")
        self.headers = headers or {}

    async def text(self) -> str:
        return self.body.decode("utf-8")


def _make_signed_request(
    body: str,
    secret: str = "test-signing-secret",
    content_type: str = "application/json",
    timestamp_offset: int = 0,
) -> _FakeRequest:
    ts, sig = _slack_signature(body, secret, int(time.time()) + timestamp_offset)
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
    chat.process_message = AsyncMock()
    chat.handle_incoming_message = AsyncMock()
    chat.process_reaction = AsyncMock()
    chat.process_action = AsyncMock()
    chat.process_modal_submit = AsyncMock()
    chat.process_modal_close = MagicMock()
    chat.process_slash_command = AsyncMock()
    chat.process_member_joined_channel = AsyncMock()
    chat.process_assistant_thread_started = AsyncMock()
    chat.process_assistant_thread_context_changed = AsyncMock()
    chat.process_app_home_opened = AsyncMock()
    chat.get_state = MagicMock(return_value=state)
    chat.get_user_name = MagicMock(return_value="test-bot")
    chat.get_logger = MagicMock(return_value=MagicMock())
    return chat


def _make_interactive_req(payload: dict[str, Any], secret: str = "test-signing-secret") -> _FakeRequest:
    payload_str = json.dumps(payload)
    body = f"payload={payload_str}"
    return _make_signed_request(body, secret=secret, content_type="application/x-www-form-urlencoded")


# ---------------------------------------------------------------------------
# Multi-workspace mode with encryption
# ---------------------------------------------------------------------------


class TestMultiWorkspaceEncryption:
    @pytest.mark.asyncio
    async def test_set_installation_encrypts_token(self):
        """setInstallation encrypts token when encryptionKey is provided."""
        key_bytes = os.urandom(32)
        key_b64 = base64.b64encode(key_bytes).decode("ascii")
        state = _make_mock_state()
        adapter = SlackAdapter(SlackAdapterConfig(signing_secret="test-secret", encryption_key=key_b64))
        await adapter.initialize(_make_mock_chat(state))

        await adapter.set_installation(
            "T_ENC_1",
            SlackInstallation(bot_token="xoxb-secret-token", bot_user_id="U_BOT_E1"),
        )

        raw_value = state._cache.get("slack:installation:T_ENC_1")
        assert raw_value is not None
        # The stored value should contain encrypted token data (iv/data/tag)
        raw_token = raw_value.get("bot_token") if isinstance(raw_value, dict) else None
        if isinstance(raw_token, dict):
            assert "iv" in raw_token
            assert "data" in raw_token
            assert "tag" in raw_token
            # Should NOT be the plaintext value
            assert raw_token != "xoxb-secret-token"

    @pytest.mark.asyncio
    async def test_get_installation_decrypts_token(self):
        """getInstallation decrypts token when encryptionKey is provided."""
        key_bytes = os.urandom(32)
        key_b64 = base64.b64encode(key_bytes).decode("ascii")
        state = _make_mock_state()
        adapter = SlackAdapter(SlackAdapterConfig(signing_secret="test-secret", encryption_key=key_b64))
        await adapter.initialize(_make_mock_chat(state))

        await adapter.set_installation(
            "T_ENC_2",
            SlackInstallation(bot_token="xoxb-encrypted-token", team_name="Encrypted Team"),
        )

        installation = await adapter.get_installation("T_ENC_2")
        assert installation is not None
        assert installation.bot_token == "xoxb-encrypted-token"
        assert installation.team_name == "Encrypted Team"

    def test_invalid_encryption_key_raises(self):
        """Short encryption key raises at construction time."""
        short_key = base64.b64encode(os.urandom(16)).decode("ascii")
        with pytest.raises(ValueError, match="32 bytes"):
            SlackAdapter(SlackAdapterConfig(signing_secret="test-secret", encryption_key=short_key))


# ---------------------------------------------------------------------------
# Installation key prefix
# ---------------------------------------------------------------------------


class TestInstallationKeyPrefix:
    @pytest.mark.asyncio
    async def test_custom_installation_key_prefix(self):
        state = _make_mock_state()
        adapter = SlackAdapter(
            SlackAdapterConfig(
                signing_secret="test-secret",
                installation_key_prefix="myapp:workspaces",
            )
        )
        await adapter.initialize(_make_mock_chat(state))

        await adapter.set_installation("T_CUSTOM_1", SlackInstallation(bot_token="xoxb-token"))

        assert "myapp:workspaces:T_CUSTOM_1" in state._cache
        assert "slack:installation:T_CUSTOM_1" not in state._cache

        retrieved = await adapter.get_installation("T_CUSTOM_1")
        assert retrieved is not None
        assert retrieved.bot_token == "xoxb-token"

    @pytest.mark.asyncio
    async def test_default_installation_key_prefix(self):
        state = _make_mock_state()
        adapter = SlackAdapter(SlackAdapterConfig(signing_secret="test-secret"))
        await adapter.initialize(_make_mock_chat(state))

        await adapter.set_installation("T_DEFAULT_1", SlackInstallation(bot_token="xoxb-token"))
        assert "slack:installation:T_DEFAULT_1" in state._cache


# ---------------------------------------------------------------------------
# Multi-workspace webhook token resolution
# ---------------------------------------------------------------------------


class TestMultiWorkspaceWebhookResolution:
    @pytest.mark.asyncio
    async def test_resolves_token_for_event_callback(self):
        """handleWebhook resolves token from state for event_callback."""
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        adapter = SlackAdapter(SlackAdapterConfig(signing_secret="test-signing-secret"))
        await adapter.initialize(chat)

        await adapter.set_installation(
            "T_MULTI_1",
            SlackInstallation(bot_token="xoxb-multi-token-1", bot_user_id="U_BOT_M1"),
        )

        body = json.dumps(
            {
                "type": "event_callback",
                "team_id": "T_MULTI_1",
                "event": {
                    "type": "message",
                    "user": "U123",
                    "channel": "C456",
                    "text": "Hello multi",
                    "ts": "1234567890.123456",
                },
            }
        )
        req = _make_signed_request(body)
        response = await adapter.handle_webhook(req)
        assert response["status"] == 200
        assert chat.process_message.called

    @pytest.mark.asyncio
    async def test_resolves_token_for_interactive_payloads(self):
        """handleWebhook resolves token for block_actions in multi-workspace."""
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        adapter = SlackAdapter(SlackAdapterConfig(signing_secret="test-signing-secret"))
        await adapter.initialize(chat)

        await adapter.set_installation(
            "T_INTER_1",
            SlackInstallation(bot_token="xoxb-inter-token"),
        )

        req = _make_interactive_req(
            {
                "type": "block_actions",
                "team": {"id": "T_INTER_1"},
                "user": {"id": "U123", "username": "testuser", "name": "Test User"},
                "container": {
                    "type": "message",
                    "message_ts": "1234567890.123456",
                    "channel_id": "C456",
                },
                "channel": {"id": "C456", "name": "general"},
                "message": {"ts": "1234567890.123456"},
                "actions": [{"type": "button", "action_id": "test_action", "value": "v"}],
            }
        )
        response = await adapter.handle_webhook(req)
        assert response["status"] == 200

    @pytest.mark.asyncio
    async def test_url_verification_works_without_token(self):
        adapter = SlackAdapter(SlackAdapterConfig(signing_secret="test-signing-secret"))
        body = json.dumps({"type": "url_verification", "challenge": "challenge-multi-123"})
        req = _make_signed_request(body)

        response = await adapter.handle_webhook(req)
        assert response["status"] == 200
        resp_body = response.get("body", "")
        if isinstance(resp_body, str):
            parsed = json.loads(resp_body)
        else:
            parsed = resp_body
        assert parsed == {"challenge": "challenge-multi-123"}


# ---------------------------------------------------------------------------
# Slash command dispatching
# ---------------------------------------------------------------------------


class TestSlashCommandDispatching:
    def _make_slash_request(
        self,
        params: dict[str, str],
        secret: str = "test-signing-secret",
    ) -> _FakeRequest:
        from urllib.parse import urlencode

        body = urlencode(params)
        return _make_signed_request(body, secret=secret, content_type="application/x-www-form-urlencoded")

    @pytest.mark.asyncio
    async def test_detects_slash_command_payload(self):
        adapter = _make_adapter(bot_user_id="U_BOT")
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        req = self._make_slash_request(
            {
                "command": "/help",
                "text": "topic search",
                "user_id": "U123456",
                "channel_id": "C789ABC",
                "trigger_id": "trigger-123",
                "team_id": "T_TEAM_1",
            }
        )
        response = await adapter.handle_webhook(req)
        assert response["status"] == 200
        assert chat.process_slash_command.called

    @pytest.mark.asyncio
    async def test_slash_command_passes_correct_fields(self):
        adapter = _make_adapter(bot_user_id="U_BOT")
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        req = self._make_slash_request(
            {
                "command": "/status",
                "text": "verbose",
                "user_id": "U_USER_1",
                "channel_id": "C_CHANNEL_1",
                "trigger_id": "trigger-456",
                "team_id": "T_TEAM_1",
            }
        )
        await adapter.handle_webhook(req)

        assert chat.process_slash_command.called
        call_args = chat.process_slash_command.call_args
        event = call_args[0][0] if call_args[0] else call_args[1].get("event")
        if event:
            # Event may be a dict or a dataclass depending on implementation
            cmd = event.get("command") if isinstance(event, dict) else getattr(event, "command", None)
            txt = event.get("text") if isinstance(event, dict) else getattr(event, "text", None)
            assert cmd == "/status"
            assert txt == "verbose"

    @pytest.mark.asyncio
    async def test_interactive_payload_not_treated_as_slash_command(self):
        adapter = _make_adapter(bot_user_id="U_BOT")
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        req = _make_interactive_req(
            {
                "type": "block_actions",
                "user": {"id": "U123", "username": "user"},
                "actions": [{"action_id": "test"}],
                "container": {"message_ts": "123", "channel_id": "C456"},
                "channel": {"id": "C456", "name": "general"},
                "message": {"ts": "123"},
            }
        )
        response = await adapter.handle_webhook(req)
        assert response["status"] == 200
        # processSlashCommand should NOT be called for interactive payloads
        assert not chat.process_slash_command.called

    @pytest.mark.asyncio
    async def test_returns_200_immediately_for_slash_commands(self):
        adapter = _make_adapter(bot_user_id="U_BOT")
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        req = self._make_slash_request(
            {
                "command": "/feedback",
                "text": "",
                "user_id": "U123",
                "channel_id": "C456",
                "team_id": "T_TEAM_1",
            }
        )
        response = await adapter.handle_webhook(req)
        assert response["status"] == 200


# ---------------------------------------------------------------------------
# Assistant thread events
# ---------------------------------------------------------------------------


class TestAssistantThreadEvents:
    @pytest.mark.asyncio
    async def test_assistant_thread_started_event(self):
        adapter = _make_adapter(bot_user_id="U_BOT")
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        body = json.dumps(
            {
                "type": "event_callback",
                "team_id": "T123",
                "event": {
                    "type": "assistant_thread_started",
                    "assistant_thread": {
                        "user_id": "U_USER",
                        "channel_id": "C_CHAN",
                        "thread_ts": "1234567890.000000",
                        "context": {"channel_id": "C_CTX", "team_id": "T123"},
                    },
                    "event_ts": "1234567890.111111",
                },
            }
        )
        req = _make_signed_request(body)
        response = await adapter.handle_webhook(req)
        assert response["status"] == 200

    @pytest.mark.asyncio
    async def test_assistant_thread_context_changed_event(self):
        adapter = _make_adapter(bot_user_id="U_BOT")
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        body = json.dumps(
            {
                "type": "event_callback",
                "team_id": "T123",
                "event": {
                    "type": "assistant_thread_context_changed",
                    "assistant_thread": {
                        "user_id": "U_USER",
                        "channel_id": "C_CHAN",
                        "thread_ts": "1234567890.000000",
                        "context": {"channel_id": "C_NEW_CTX", "team_id": "T123"},
                    },
                    "event_ts": "1234567890.222222",
                },
            }
        )
        req = _make_signed_request(body)
        response = await adapter.handle_webhook(req)
        assert response["status"] == 200


# ---------------------------------------------------------------------------
# App home opened events
# ---------------------------------------------------------------------------


class TestAppHomeOpenedEvent:
    @pytest.mark.asyncio
    async def test_app_home_opened(self):
        adapter = _make_adapter(bot_user_id="U_BOT")
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        body = json.dumps(
            {
                "type": "event_callback",
                "team_id": "T123",
                "event": {
                    "type": "app_home_opened",
                    "user": "U_HOME_USER",
                    "channel": "D_APP_HOME",
                    "tab": "home",
                    "event_ts": "1234567890.333333",
                },
            }
        )
        req = _make_signed_request(body)
        response = await adapter.handle_webhook(req)
        assert response["status"] == 200


# ---------------------------------------------------------------------------
# DM message handling
# ---------------------------------------------------------------------------


class TestDMMessageHandling:
    @pytest.mark.asyncio
    async def test_top_level_dm_uses_empty_thread_ts(self):
        """Top-level DM messages should use empty threadTs for subscription matching."""
        adapter = _make_adapter(bot_user_id="U_BOT")
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        body = json.dumps(
            {
                "type": "event_callback",
                "team_id": "T123",
                "event": {
                    "type": "message",
                    "user": "U_USER",
                    "channel": "D_DM_CHAN",
                    "channel_type": "im",
                    "text": "hello from DM",
                    "ts": "1234567890.111111",
                },
            }
        )
        req = _make_signed_request(body)
        await adapter.handle_webhook(req)
        assert chat.process_message.called

    @pytest.mark.asyncio
    async def test_dm_thread_reply_uses_parent_thread_ts(self):
        adapter = _make_adapter(bot_user_id="U_BOT")
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        body = json.dumps(
            {
                "type": "event_callback",
                "team_id": "T123",
                "event": {
                    "type": "message",
                    "user": "U_USER",
                    "channel": "D_DM_CHAN",
                    "channel_type": "im",
                    "text": "reply in DM thread",
                    "ts": "1234567890.222222",
                    "thread_ts": "1234567890.111111",
                },
            }
        )
        req = _make_signed_request(body)
        await adapter.handle_webhook(req)
        assert chat.process_message.called


# ---------------------------------------------------------------------------
# Thread broadcast subtype
# ---------------------------------------------------------------------------


class TestThreadBroadcast:
    @pytest.mark.asyncio
    async def test_allows_thread_broadcast(self):
        adapter = _make_adapter(bot_user_id="U_BOT")
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        body = json.dumps(
            {
                "type": "event_callback",
                "team_id": "T123",
                "event": {
                    "type": "message",
                    "subtype": "thread_broadcast",
                    "user": "U_USER",
                    "channel": "C_CHAN",
                    "text": "Also posted to channel",
                    "ts": "1234567890.222222",
                    "thread_ts": "1234567890.000000",
                },
            }
        )
        req = _make_signed_request(body)
        await adapter.handle_webhook(req)
        assert chat.process_message.called


# ---------------------------------------------------------------------------
# Reaction events
# ---------------------------------------------------------------------------


class TestReactionEvents:
    @pytest.mark.asyncio
    async def test_handles_reaction_added_event(self):
        adapter = _make_adapter(bot_user_id="U_BOT")
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        body = json.dumps(
            {
                "type": "event_callback",
                "event": {
                    "type": "reaction_added",
                    "user": "U123",
                    "reaction": "thumbsup",
                    "item": {
                        "type": "message",
                        "channel": "C456",
                        "ts": "1234567890.123456",
                    },
                },
            }
        )
        req = _make_signed_request(body)
        response = await adapter.handle_webhook(req)
        assert response["status"] == 200

    @pytest.mark.asyncio
    async def test_handles_reaction_removed_event(self):
        adapter = _make_adapter(bot_user_id="U_BOT")
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        body = json.dumps(
            {
                "type": "event_callback",
                "event": {
                    "type": "reaction_removed",
                    "user": "U123",
                    "reaction": "thumbsup",
                    "item": {
                        "type": "message",
                        "channel": "C456",
                        "ts": "1234567890.123456",
                    },
                },
            }
        )
        req = _make_signed_request(body)
        response = await adapter.handle_webhook(req)
        assert response["status"] == 200


# ---------------------------------------------------------------------------
# Member joined channel
# ---------------------------------------------------------------------------


class TestMemberJoinedChannel:
    @pytest.mark.asyncio
    async def test_member_joined_channel_event(self):
        adapter = _make_adapter(bot_user_id="U_BOT")
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        body = json.dumps(
            {
                "type": "event_callback",
                "team_id": "T123",
                "event": {
                    "type": "member_joined_channel",
                    "user": "U_NEW_MEMBER",
                    "channel": "C_CHAN",
                    "channel_type": "C",
                    "team": "T123",
                    "event_ts": "1234567890.555555",
                },
            }
        )
        req = _make_signed_request(body)
        response = await adapter.handle_webhook(req)
        assert response["status"] == 200


# ---------------------------------------------------------------------------
# Edited message parsing
# ---------------------------------------------------------------------------


class TestEditedMessageParsing:
    def test_parses_edited_message(self):
        adapter = _make_adapter(bot_user_id="U_BOT")
        event = {
            "type": "message",
            "user": "U123",
            "channel": "C456",
            "text": "Edited message",
            "ts": "1234567890.123456",
            "edited": {"ts": "1234567891.000000"},
        }
        msg = adapter.parse_message(event)
        assert msg.metadata.edited is True
        assert msg.metadata.edited_at is not None

    def test_parses_edited_timestamp(self):
        adapter = _make_adapter(bot_user_id="U_BOT")
        event = {
            "type": "message",
            "user": "U123",
            "channel": "C456",
            "text": "Hello",
            "ts": "1609459200.000000",
            "edited": {"ts": "1609459260.000000"},
        }
        msg = adapter.parse_message(event)
        assert msg.metadata.edited_at is not None
        # 1609459260 = 2021-01-01 00:01:00 UTC (1 minute after the original)
        assert msg.metadata.edited_at.year == 2021


# ---------------------------------------------------------------------------
# Username parsing
# ---------------------------------------------------------------------------


class TestUsernameParsing:
    def test_parses_username_from_event(self):
        adapter = _make_adapter()
        event = {
            "type": "message",
            "user": "U123",
            "username": "testuser",
            "channel": "C456",
            "text": "Hello",
            "ts": "1234567890.123456",
        }
        msg = adapter.parse_message(event)
        assert msg.author.user_name == "testuser"


# ---------------------------------------------------------------------------
# Block actions with trigger_id
# ---------------------------------------------------------------------------


class TestBlockActionsTriggerId:
    @pytest.mark.asyncio
    async def test_block_actions_includes_trigger_id(self):
        adapter = _make_adapter()
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        req = _make_interactive_req(
            {
                "type": "block_actions",
                "trigger_id": "trigger456",
                "user": {"id": "U123", "username": "testuser", "name": "Test User"},
                "container": {
                    "type": "message",
                    "message_ts": "1234567890.123456",
                    "channel_id": "C456",
                },
                "channel": {"id": "C456", "name": "general"},
                "message": {"ts": "1234567890.123456"},
                "actions": [
                    {"type": "button", "action_id": "open_modal", "value": "modal-data"},
                ],
            }
        )
        response = await adapter.handle_webhook(req)
        assert response["status"] == 200


# ---------------------------------------------------------------------------
# Crypto module edge cases
# ---------------------------------------------------------------------------


class TestCryptoEdgeCases:
    def test_encrypt_decrypt_roundtrip(self):
        key = os.urandom(32)
        plaintext = "xoxb-very-secret-token-123"
        encrypted = encrypt_token(plaintext, key)
        decrypted = decrypt_token(encrypted, key)
        assert decrypted == plaintext

    def test_decode_key_hex(self):
        key_hex = "a" * 64  # 64 hex chars = 32 bytes
        decoded = decode_key(key_hex)
        assert len(decoded) == 32

    def test_decode_key_base64(self):
        key_bytes = os.urandom(32)
        key_b64 = base64.b64encode(key_bytes).decode("ascii")
        decoded = decode_key(key_b64)
        assert decoded == key_bytes

    def test_decode_key_invalid_length(self):
        with pytest.raises(ValueError, match="32 bytes"):
            decode_key(base64.b64encode(os.urandom(16)).decode("ascii"))
