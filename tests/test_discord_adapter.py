"""Tests for the Discord adapter -- constructor, thread IDs, webhook handling, message parsing.

Ported from packages/adapter-discord/src/index.test.ts.
"""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from chat_sdk.adapters.discord.adapter import (
    CHANNEL_TYPE_PUBLIC_THREAD,
    DiscordAdapter,
    create_discord_adapter,
)
from chat_sdk.adapters.discord.types import DiscordAdapterConfig, DiscordThreadId
from chat_sdk.shared.errors import ValidationError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# A valid hex public key (64 hex chars)
TEST_PUBLIC_KEY = "a" * 64


def _make_adapter(**overrides) -> DiscordAdapter:
    """Create a DiscordAdapter with minimal valid config."""
    config = DiscordAdapterConfig(
        bot_token=overrides.pop("bot_token", "test-token"),
        public_key=overrides.pop("public_key", TEST_PUBLIC_KEY),
        application_id=overrides.pop("application_id", "test-app-id"),
        **overrides,
    )
    return DiscordAdapter(config)


def _make_logger():
    return MagicMock(
        debug=MagicMock(),
        info=MagicMock(),
        warn=MagicMock(),
        error=MagicMock(),
        child=MagicMock(return_value=MagicMock()),
    )


class _FakeRequest:
    """A simple request-like object for testing webhook handlers."""

    def __init__(self, body: str, headers: dict[str, str] | None = None):
        self._body = body
        self.headers = headers or {}

    async def text(self) -> str:
        return self._body

    @property
    def data(self) -> bytes:
        return self._body.encode("utf-8")


# ---------------------------------------------------------------------------
# createDiscordAdapter factory
# ---------------------------------------------------------------------------


class TestCreateDiscordAdapter:
    def test_creates_instance(self):
        adapter = create_discord_adapter(
            DiscordAdapterConfig(
                bot_token="test-token",
                public_key=TEST_PUBLIC_KEY,
                application_id="test-app-id",
            )
        )
        assert isinstance(adapter, DiscordAdapter)
        assert adapter.name == "discord"

    def test_default_user_name(self):
        adapter = _make_adapter()
        assert adapter.user_name == "bot"

    def test_custom_user_name(self):
        adapter = _make_adapter(user_name="custombot")
        assert adapter.user_name == "custombot"


# ---------------------------------------------------------------------------
# Constructor env var resolution
# ---------------------------------------------------------------------------


class TestDiscordConstructorEnvVars:
    def test_throws_when_bot_token_missing(self, monkeypatch):
        monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
        monkeypatch.delenv("DISCORD_PUBLIC_KEY", raising=False)
        monkeypatch.delenv("DISCORD_APPLICATION_ID", raising=False)
        with pytest.raises(ValidationError, match="bot_token"):
            DiscordAdapter(DiscordAdapterConfig(bot_token=None))

    def test_throws_when_public_key_missing(self, monkeypatch):
        monkeypatch.delenv("DISCORD_PUBLIC_KEY", raising=False)
        monkeypatch.delenv("DISCORD_APPLICATION_ID", raising=False)
        with pytest.raises(ValidationError, match="public_key"):
            DiscordAdapter(DiscordAdapterConfig(bot_token="test", public_key=None))

    def test_throws_when_application_id_missing(self, monkeypatch):
        monkeypatch.delenv("DISCORD_APPLICATION_ID", raising=False)
        with pytest.raises(ValidationError, match="application_id"):
            DiscordAdapter(
                DiscordAdapterConfig(
                    bot_token="test",
                    public_key=TEST_PUBLIC_KEY,
                    application_id=None,
                )
            )

    def test_resolves_from_env(self, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "env-token")
        monkeypatch.setenv("DISCORD_PUBLIC_KEY", TEST_PUBLIC_KEY)
        monkeypatch.setenv("DISCORD_APPLICATION_ID", "env-app-id")
        adapter = DiscordAdapter()
        assert isinstance(adapter, DiscordAdapter)
        assert adapter.user_name == "bot"

    def test_prefers_config_over_env(self, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "env-token")
        monkeypatch.setenv("DISCORD_PUBLIC_KEY", TEST_PUBLIC_KEY)
        monkeypatch.setenv("DISCORD_APPLICATION_ID", "env-app-id")
        adapter = DiscordAdapter(
            DiscordAdapterConfig(
                bot_token="config-token",
                public_key=TEST_PUBLIC_KEY,
                application_id="config-app-id",
                user_name="mybot",
            )
        )
        assert adapter.user_name == "mybot"


# ---------------------------------------------------------------------------
# Thread ID Encoding / Decoding
# ---------------------------------------------------------------------------


class TestEncodeThreadId:
    def test_encodes_guild_and_channel(self):
        adapter = _make_adapter()
        tid = adapter.encode_thread_id(DiscordThreadId(guild_id="guild123", channel_id="channel456"))
        assert tid == "discord:guild123:channel456"

    def test_encodes_with_thread_id(self):
        adapter = _make_adapter()
        tid = adapter.encode_thread_id(
            DiscordThreadId(guild_id="guild123", channel_id="channel456", thread_id="thread789")
        )
        assert tid == "discord:guild123:channel456:thread789"

    def test_encodes_dm_channel(self):
        adapter = _make_adapter()
        tid = adapter.encode_thread_id(DiscordThreadId(guild_id="@me", channel_id="dm123"))
        assert tid == "discord:@me:dm123"


class TestDecodeThreadId:
    def test_decodes_valid_thread_id(self):
        adapter = _make_adapter()
        result = adapter.decode_thread_id("discord:guild123:channel456")
        assert result.guild_id == "guild123"
        assert result.channel_id == "channel456"
        assert result.thread_id is None

    def test_decodes_with_thread(self):
        adapter = _make_adapter()
        result = adapter.decode_thread_id("discord:guild123:channel456:thread789")
        assert result.guild_id == "guild123"
        assert result.channel_id == "channel456"
        assert result.thread_id == "thread789"

    def test_decodes_dm_thread(self):
        adapter = _make_adapter()
        result = adapter.decode_thread_id("discord:@me:dm123")
        assert result.guild_id == "@me"
        assert result.channel_id == "dm123"
        assert result.thread_id is None

    def test_throws_on_invalid_format(self):
        adapter = _make_adapter()
        with pytest.raises(ValidationError):
            adapter.decode_thread_id("invalid")
        with pytest.raises(ValidationError):
            adapter.decode_thread_id("discord:channel")
        with pytest.raises(ValidationError):
            adapter.decode_thread_id("slack:C12345:123")


# ---------------------------------------------------------------------------
# isDM
# ---------------------------------------------------------------------------


class TestIsDM:
    def test_returns_true_for_dm(self):
        adapter = _make_adapter()
        assert adapter.is_dm("discord:@me:dm123") is True

    def test_returns_false_for_guild(self):
        adapter = _make_adapter()
        assert adapter.is_dm("discord:guild123:channel456") is False

    def test_returns_false_for_thread_in_guild(self):
        adapter = _make_adapter()
        assert adapter.is_dm("discord:guild123:channel456:thread789") is False


# ---------------------------------------------------------------------------
# Webhook handling - PING
# ---------------------------------------------------------------------------


class TestHandleWebhookPing:
    @pytest.mark.asyncio
    async def test_responds_to_ping_with_pong(self):
        adapter = _make_adapter(logger=_make_logger())
        # Bypass signature verification by mocking
        adapter._verify_signature = AsyncMock(return_value=True)

        body = json.dumps({"type": 1})  # PING
        request = _FakeRequest(
            body,
            {
                "x-signature-ed25519": "valid",
                "x-signature-timestamp": "12345",
                "content-type": "application/json",
            },
        )

        response = await adapter.handle_webhook(request)
        response_body = json.loads(response["body"])
        assert response_body == {"type": 1}  # PONG
        assert response["status"] == 200


# ---------------------------------------------------------------------------
# Webhook handling - signature verification
# ---------------------------------------------------------------------------


class TestHandleWebhookSignature:
    @pytest.mark.asyncio
    async def test_rejects_without_signature_header(self):
        adapter = _make_adapter(logger=_make_logger())
        body = json.dumps({"type": 1})
        request = _FakeRequest(body, {"x-signature-timestamp": "12345"})
        response = await adapter.handle_webhook(request)
        assert response["status"] == 401

    @pytest.mark.asyncio
    async def test_rejects_without_timestamp_header(self):
        adapter = _make_adapter(logger=_make_logger())
        body = json.dumps({"type": 1})
        request = _FakeRequest(body, {"x-signature-ed25519": "abcd" * 32})
        response = await adapter.handle_webhook(request)
        assert response["status"] == 401

    @pytest.mark.asyncio
    async def test_rejects_invalid_signature(self):
        adapter = _make_adapter(logger=_make_logger())
        body = json.dumps({"type": 1})
        request = _FakeRequest(body, {"x-signature-ed25519": "invalid", "x-signature-timestamp": "12345"})
        response = await adapter.handle_webhook(request)
        assert response["status"] == 401


# ---------------------------------------------------------------------------
# Webhook handling - MESSAGE_COMPONENT
# ---------------------------------------------------------------------------


class TestHandleWebhookMessageComponent:
    @pytest.mark.asyncio
    async def test_handles_button_click(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._verify_signature = AsyncMock(return_value=True)

        mock_chat = MagicMock()
        mock_chat.process_action = MagicMock()
        adapter._chat = mock_chat

        body = json.dumps(
            {
                "type": 3,  # MESSAGE_COMPONENT
                "id": "interaction123",
                "application_id": "test-app-id",
                "token": "interaction-token",
                "guild_id": "guild123",
                "channel_id": "channel456",
                "member": {
                    "user": {
                        "id": "user789",
                        "username": "testuser",
                        "global_name": "Test User",
                    },
                },
                "message": {
                    "id": "message123",
                    "channel_id": "channel456",
                },
                "data": {
                    "custom_id": "approve_btn",
                    "component_type": 2,
                },
            }
        )
        request = _FakeRequest(body, {"x-signature-ed25519": "valid", "x-signature-timestamp": "12345"})

        response = await adapter.handle_webhook(request)
        assert response["status"] == 200
        response_body = json.loads(response["body"])
        assert response_body["type"] == 6  # DEFERRED_UPDATE_MESSAGE


# ---------------------------------------------------------------------------
# Webhook handling - APPLICATION_COMMAND
# ---------------------------------------------------------------------------


class TestHandleWebhookApplicationCommand:
    @pytest.mark.asyncio
    async def test_handles_slash_command(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._verify_signature = AsyncMock(return_value=True)

        mock_chat = MagicMock()
        mock_chat.process_slash_command = MagicMock()
        adapter._chat = mock_chat

        body = json.dumps(
            {
                "type": 2,  # APPLICATION_COMMAND
                "id": "interaction123",
                "application_id": "test-app-id",
                "token": "interaction-token",
                "guild_id": "guild123",
                "channel_id": "channel456",
                "member": {
                    "user": {
                        "id": "user789",
                        "username": "testuser",
                    },
                },
                "data": {
                    "id": "cmd123",
                    "name": "test",
                    "type": 1,
                },
            }
        )
        request = _FakeRequest(body, {"x-signature-ed25519": "valid", "x-signature-timestamp": "12345"})

        response = await adapter.handle_webhook(request)
        assert response["status"] == 200
        response_body = json.loads(response["body"])
        assert response_body["type"] == 5  # DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE

    @pytest.mark.asyncio
    async def test_dispatches_slash_command_to_chat(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._verify_signature = AsyncMock(return_value=True)

        mock_chat = MagicMock()
        mock_chat.process_slash_command = MagicMock()
        adapter._chat = mock_chat

        body = json.dumps(
            {
                "type": 2,
                "id": "interaction123",
                "application_id": "test-app-id",
                "token": "interaction-token",
                "guild_id": "guild123",
                "channel_id": "channel456",
                "member": {
                    "user": {
                        "id": "user789",
                        "username": "testuser",
                        "global_name": "Test User",
                    },
                },
                "data": {
                    "name": "test",
                    "type": 1,
                    "options": [
                        {"name": "topic", "type": 3, "value": "status"},
                        {"name": "verbose", "type": 5, "value": True},
                    ],
                },
            }
        )
        request = _FakeRequest(body, {"x-signature-ed25519": "valid", "x-signature-timestamp": "12345"})

        await adapter.handle_webhook(request)

        mock_chat.process_slash_command.assert_called_once()
        call_args = mock_chat.process_slash_command.call_args[0][0]
        assert call_args.command == "/test"
        assert call_args.text == "status True"

    @pytest.mark.asyncio
    async def test_expands_subcommand_path(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._verify_signature = AsyncMock(return_value=True)

        mock_chat = MagicMock()
        mock_chat.process_slash_command = MagicMock()
        adapter._chat = mock_chat

        body = json.dumps(
            {
                "type": 2,
                "id": "interaction123",
                "application_id": "test-app-id",
                "token": "interaction-token",
                "guild_id": "guild123",
                "channel_id": "channel456",
                "member": {
                    "user": {
                        "id": "user789",
                        "username": "testuser",
                        "global_name": "Test User",
                    },
                },
                "data": {
                    "name": "project",
                    "type": 1,
                    "options": [
                        {
                            "name": "issue",
                            "type": 2,
                            "options": [
                                {
                                    "name": "create",
                                    "type": 1,
                                    "options": [
                                        {"name": "title", "type": 3, "value": "Login fails"},
                                        {"name": "priority", "type": 3, "value": "high"},
                                    ],
                                }
                            ],
                        }
                    ],
                },
            }
        )
        request = _FakeRequest(body, {"x-signature-ed25519": "valid", "x-signature-timestamp": "12345"})

        await adapter.handle_webhook(request)

        call_args = mock_chat.process_slash_command.call_args[0][0]
        assert call_args.command == "/project issue create"
        assert call_args.text == "Login fails high"


# ---------------------------------------------------------------------------
# Webhook handling - JSON parsing
# ---------------------------------------------------------------------------


class TestHandleWebhookJsonParsing:
    @pytest.mark.asyncio
    async def test_returns_400_for_invalid_json(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._verify_signature = AsyncMock(return_value=True)

        request = _FakeRequest(
            "not valid json",
            {"x-signature-ed25519": "valid", "x-signature-timestamp": "12345"},
        )
        response = await adapter.handle_webhook(request)
        assert response["status"] == 400

    @pytest.mark.asyncio
    async def test_returns_400_for_unknown_interaction_type(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._verify_signature = AsyncMock(return_value=True)

        request = _FakeRequest(
            json.dumps({"type": 999}),
            {"x-signature-ed25519": "valid", "x-signature-timestamp": "12345"},
        )
        response = await adapter.handle_webhook(request)
        assert response["status"] == 400


# ---------------------------------------------------------------------------
# parseMessage
# ---------------------------------------------------------------------------


class TestParseMessage:
    def test_parses_basic_message(self):
        adapter = _make_adapter()
        raw = {
            "id": "message123",
            "channel_id": "channel456",
            "guild_id": "guild789",
            "author": {
                "id": "user123",
                "username": "testuser",
                "discriminator": "0001",
                "global_name": "Test User",
            },
            "content": "Hello world",
            "timestamp": "2021-01-01T00:00:00.000Z",
            "edited_timestamp": None,
            "attachments": [],
        }
        msg = adapter.parse_message(raw)
        assert msg.id == "message123"
        assert msg.text == "Hello world"
        assert msg.author.user_id == "user123"
        assert msg.author.user_name == "testuser"
        assert msg.author.full_name == "Test User"
        assert msg.author.is_bot is False
        assert msg.thread_id == "discord:guild789:channel456"

    def test_parses_bot_message(self):
        adapter = _make_adapter()
        raw = {
            "id": "message123",
            "channel_id": "channel456",
            "guild_id": "guild789",
            "author": {
                "id": "bot123",
                "username": "somebot",
                "bot": True,
            },
            "content": "Bot message",
            "timestamp": "2021-01-01T00:00:00.000Z",
            "attachments": [],
        }
        msg = adapter.parse_message(raw)
        assert msg.author.is_bot is True

    def test_parses_dm_message_no_guild(self):
        adapter = _make_adapter()
        raw = {
            "id": "message123",
            "channel_id": "dm456",
            "author": {
                "id": "user123",
                "username": "testuser",
            },
            "content": "DM message",
            "timestamp": "2021-01-01T00:00:00.000Z",
            "attachments": [],
        }
        msg = adapter.parse_message(raw)
        assert msg.thread_id == "discord:@me:dm456"

    def test_parses_edited_message(self):
        adapter = _make_adapter()
        raw = {
            "id": "message123",
            "channel_id": "channel456",
            "guild_id": "guild789",
            "author": {
                "id": "user123",
                "username": "testuser",
            },
            "content": "Edited message",
            "timestamp": "2021-01-01T00:00:00.000Z",
            "edited_timestamp": "2021-01-01T00:01:00.000Z",
            "attachments": [],
        }
        msg = adapter.parse_message(raw)
        assert msg.metadata.edited is True

    def test_parses_message_with_attachments(self):
        adapter = _make_adapter()
        raw = {
            "id": "message123",
            "channel_id": "channel456",
            "guild_id": "guild789",
            "author": {
                "id": "user123",
                "username": "testuser",
            },
            "content": "Message with attachment",
            "timestamp": "2021-01-01T00:00:00.000Z",
            "attachments": [
                {
                    "id": "att123",
                    "filename": "image.png",
                    "size": 12345,
                    "url": "https://cdn.discord.com/image.png",
                    "content_type": "image/png",
                },
            ],
        }
        msg = adapter.parse_message(raw)
        assert len(msg.attachments) == 1
        assert msg.attachments[0].type == "image"
        assert msg.attachments[0].name == "image.png"
        assert msg.attachments[0].mime_type == "image/png"

    def test_handles_different_attachment_types(self):
        adapter = _make_adapter()

        def make_msg(content_type: str):
            return {
                "id": "msg",
                "channel_id": "ch",
                "guild_id": "g",
                "author": {"id": "u", "username": "u"},
                "content": "",
                "timestamp": "2021-01-01T00:00:00.000Z",
                "attachments": [{"filename": "f", "url": "http://x", "content_type": content_type}],
            }

        assert adapter.parse_message(make_msg("image/png")).attachments[0].type == "image"
        assert adapter.parse_message(make_msg("video/mp4")).attachments[0].type == "video"
        assert adapter.parse_message(make_msg("audio/mpeg")).attachments[0].type == "audio"
        assert adapter.parse_message(make_msg("application/pdf")).attachments[0].type == "file"

    def test_detects_self_message(self):
        adapter = _make_adapter(application_id="test-app-id")
        raw = {
            "id": "msg1",
            "channel_id": "ch",
            "guild_id": "g",
            "author": {"id": "test-app-id", "username": "bot"},
            "content": "hello",
            "timestamp": "2021-01-01T00:00:00.000Z",
            "attachments": [],
        }
        msg = adapter.parse_message(raw)
        assert msg.author.is_me is True

    def test_non_self_message(self):
        adapter = _make_adapter(application_id="test-app-id")
        raw = {
            "id": "msg1",
            "channel_id": "ch",
            "guild_id": "g",
            "author": {"id": "other-user", "username": "user"},
            "content": "hello",
            "timestamp": "2021-01-01T00:00:00.000Z",
            "attachments": [],
        }
        msg = adapter.parse_message(raw)
        assert msg.author.is_me is False


# ---------------------------------------------------------------------------
# renderFormatted
# ---------------------------------------------------------------------------


class TestRenderFormatted:
    def test_delegates_to_format_converter(self):
        adapter = _make_adapter()
        ast = {
            "type": "root",
            "children": [
                {
                    "type": "paragraph",
                    "children": [{"type": "text", "value": "Hello world"}],
                }
            ],
        }
        result = adapter.render_formatted(ast)
        assert isinstance(result, str)
        assert "Hello world" in result


# ---------------------------------------------------------------------------
# initialize
# ---------------------------------------------------------------------------


class TestInitialize:
    @pytest.mark.asyncio
    async def test_stores_chat_instance(self):
        adapter = _make_adapter()
        mock_chat = MagicMock()
        await adapter.initialize(mock_chat)
        assert adapter._chat is mock_chat
