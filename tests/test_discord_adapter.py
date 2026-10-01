"""Tests for the Discord adapter -- constructor, thread IDs, webhook handling, message parsing.

Ported from packages/adapter-discord/src/index.test.ts.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from chat_sdk.adapters.discord.adapter import (
    DiscordAdapter,
    DiscordApiError,
    create_discord_adapter,
)
from chat_sdk.adapters.discord.types import (
    DiscordAdapterConfig,
    DiscordRequestContext,
    DiscordSlashCommandContext,
    DiscordThreadId,
)
from chat_sdk.shared.errors import NetworkError, ValidationError
from chat_sdk.types import Attachment, Message, PostableMarkdown

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
        # Boolean option values flatten as JSON-style "true"/"false",
        # matching TS `String(true)` (wire parity, vercel/chat#490 test).
        assert call_args.text == "status true"

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


# ===========================================================================
# 4.41 sync (#229): thread-parent validation, starter-message routing,
# DiscordApiError codes, forwarded snapshots, attachment downloads.
# ===========================================================================


class _FakeResponse:
    def __init__(self, status: int, body: Any):
        self.status = status
        self.ok = 200 <= status < 300
        self._body = body

    async def text(self) -> str:
        return self._body if isinstance(self._body, str) else json.dumps(self._body)

    async def json(self) -> Any:
        return self._body


class _FakeResponseContext:
    def __init__(self, response: _FakeResponse):
        self._response = response

    async def __aenter__(self) -> _FakeResponse:
        return self._response

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeSession:
    """aiohttp-session stand-in so ``_discord_fetch`` builds real errors.

    ``route(method, url)`` returns ``(status, body)``; every call is recorded
    as ``(url, method)``.
    """

    closed = False

    def __init__(self, route: Callable[[str, str], tuple[int, Any]]):
        self._route = route
        self.calls: list[tuple[str, str]] = []

    def request(self, method: str, url: str, **_kwargs: Any) -> _FakeResponseContext:
        self.calls.append((url, method))
        status, body = self._route(method, url)
        return _FakeResponseContext(_FakeResponse(status, body))


API = "https://discord.com/api/v10"
# Upstream passes "heart" through its emoji resolver; this port's
# ``_encode_emoji`` URL-quotes the given string as-is, so pass the glyph.
HEART_GLYPH = "\u2764\ufe0f"
HEART = "%E2%9D%A4%EF%B8%8F"


def _with_session(adapter: DiscordAdapter, route: Callable[[str, str], tuple[int, Any]]) -> _FakeSession:
    session = _FakeSession(route)
    adapter._http_session = session
    return session


def _mismatched(adapter: DiscordAdapter) -> AsyncMock:
    """Seed thread789 as a child of otherChannel; any fetch is a failure."""
    adapter._remember_thread_parent("thread789", "otherChannel")
    fetch = AsyncMock(return_value={"id": "should-not-happen"})
    adapter._discord_fetch = fetch
    return fetch


class TestThreadParentValidation:
    @pytest.mark.asyncio
    async def test_rejects_a_thread_id_whose_target_belongs_to_another_channel(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(return_value={"id": "victimChannel", "parent_id": "otherChannel"})

        with pytest.raises(ValidationError, match="does not belong to channel channel456"):
            await adapter.fetch_messages("discord:guild1:channel456:victimChannel")

        adapter._discord_fetch.assert_called_once_with("/channels/victimChannel", "GET")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "call",
        [
            lambda a, t: a.post_message(t, "hi"),
            lambda a, t: a.edit_message(t, "msg1", "hi"),
            lambda a, t: a.delete_message(t, "msg1"),
            lambda a, t: a.add_reaction(t, "msg1", "heart"),
            lambda a, t: a.remove_reaction(t, "msg1", "heart"),
            lambda a, t: a.start_typing(t),
            lambda a, t: a.fetch_messages(t),
            # A starter-message id must not bypass the check either.
            lambda a, t: a.edit_message(t, "thread789", "hi"),
        ],
        ids=["post", "edit", "delete", "add_reaction", "remove_reaction", "typing", "fetch", "edit_starter"],
    )
    async def test_every_outbound_thread_operation_rejects_a_mismatched_parent(self, call):
        adapter = _make_adapter(logger=_make_logger())
        fetch = _mismatched(adapter)

        with pytest.raises(ValidationError, match="Discord thread thread789 does not belong to channel channel456"):
            await call(adapter, "discord:guild1:channel456:thread789")

        fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_slash_command_response_is_validated_before_patching(self):
        adapter = _make_adapter(logger=_make_logger())
        fetch = _mismatched(adapter)
        slash = DiscordSlashCommandContext(
            channel_id="discord:guild1:channel456:thread789",
            initial_response_sent=False,
            interaction_token="tok",
        )
        adapter._request_context.set(DiscordRequestContext(slash_command=slash))

        with pytest.raises(ValidationError):
            await adapter.post_message("discord:guild1:channel456:thread789", "hi")

        fetch.assert_not_called()
        assert slash.initial_response_sent is False

    @pytest.mark.asyncio
    async def test_parentless_channel_lookup_is_rejected(self):
        # A non-thread channel (no parent_id) can never be a thread segment.
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(return_value={"id": "channel999", "type": 0})

        with pytest.raises(ValidationError):
            await adapter.start_typing("discord:guild1:channel456:channel999")
        adapter._discord_fetch.assert_called_once_with("/channels/channel999", "GET")

    @pytest.mark.asyncio
    async def test_channel_only_thread_id_needs_no_lookup(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(return_value=None)

        await adapter.start_typing("discord:guild1:channel456")

        adapter._discord_fetch.assert_called_once_with("/channels/channel456/typing", "POST")

    @pytest.mark.asyncio
    async def test_verified_parent_is_cached_until_the_ttl_expires(self, monkeypatch):
        import chat_sdk.adapters.discord.adapter as discord_module

        now = [1_000.0]
        monkeypatch.setattr(discord_module.time, "time", lambda: now[0])
        adapter = _make_adapter(logger=_make_logger())
        thread_channel = {"id": "thread789", "parent_id": "channel456"}
        adapter._discord_fetch = AsyncMock(side_effect=[thread_channel, None, None, thread_channel, None])
        thread_id = "discord:guild1:channel456:thread789"

        await adapter.start_typing(thread_id)  # GET + POST
        now[0] += discord_module.THREAD_PARENT_CACHE_TTL - 1
        await adapter.start_typing(thread_id)  # cached: POST only
        now[0] += 2
        await adapter.start_typing(thread_id)  # expired: GET + POST

        assert [c.args for c in adapter._discord_fetch.call_args_list] == [
            ("/channels/thread789", "GET"),
            ("/channels/thread789/typing", "POST"),
            ("/channels/thread789/typing", "POST"),
            ("/channels/thread789", "GET"),
            ("/channels/thread789/typing", "POST"),
        ]

    def test_cache_stays_bounded(self, monkeypatch):
        import chat_sdk.adapters.discord.adapter as discord_module

        monkeypatch.setattr(discord_module, "THREAD_PARENT_CACHE_MAX", 3)
        adapter = _make_adapter()
        for index in range(5):
            adapter._remember_thread_parent(f"t{index}", "p")
        # Re-remembering refreshes recency, so t2 survives the next eviction.
        adapter._remember_thread_parent("t2", "p")
        adapter._remember_thread_parent("t5", "p")

        assert list(adapter._thread_parent_cache) == ["t4", "t2", "t5"]

    @pytest.mark.asyncio
    async def test_component_interaction_in_a_thread_remembers_its_parent(self):
        adapter = _make_adapter(logger=_make_logger())
        mock_chat = MagicMock()
        await adapter.initialize(mock_chat)
        adapter._handle_component_interaction(
            {
                "type": 3,
                "channel_id": "thread789",
                "guild_id": "guild1",
                "channel": {"id": "thread789", "type": 11, "parent_id": "channel456"},
                "message": {"id": "msg1"},
                "member": {"user": {"id": "u1", "username": "user"}},
                "data": {"custom_id": "approve"},
            }
        )
        assert mock_chat.process_action.call_args[0][0].thread_id == "discord:guild1:channel456:thread789"

        adapter._discord_fetch = AsyncMock(return_value=None)
        await adapter.start_typing("discord:guild1:channel456:thread789")
        adapter._discord_fetch.assert_called_once_with("/channels/thread789/typing", "POST")

    @pytest.mark.asyncio
    async def test_parentless_thread_interaction_encodes_the_channel_only(self):
        adapter = _make_adapter(logger=_make_logger())
        mock_chat = MagicMock()
        await adapter.initialize(mock_chat)
        adapter._handle_application_command_interaction(
            {
                "type": 2,
                "channel_id": "thread789",
                "guild_id": "guild1",
                "channel": {"id": "thread789", "type": 11},
                "member": {"user": {"id": "u1", "username": "user"}},
                "data": {"name": "help"},
                "token": "tok",
            }
        )
        event = mock_chat.process_slash_command.call_args[0][0]
        assert event.channel_id == "discord:guild1:thread789"
        assert adapter._thread_parent_cache == {}

    @pytest.mark.asyncio
    async def test_forwarded_message_thread_info_is_remembered(self):
        adapter = _make_adapter(logger=_make_logger())
        mock_chat = MagicMock()
        mock_chat.handle_incoming_message = AsyncMock()
        await adapter.initialize(mock_chat)
        adapter._discord_fetch = AsyncMock(return_value=None)

        await adapter._handle_forwarded_message(
            {
                "id": "m1",
                "channel_id": "thread789",
                "guild_id": "guild1",
                "content": "hi",
                "author": {"id": "u1", "username": "user"},
                "mentions": [],
                "thread": {"id": "thread789", "parent_id": "channel456"},
                "timestamp": "2021-01-01T00:00:00.000Z",
            }
        )
        await adapter.start_typing("discord:guild1:channel456:thread789")

        adapter._discord_fetch.assert_called_once_with("/channels/thread789/typing", "POST")

    @pytest.mark.asyncio
    async def test_uses_forwarded_thread_info_without_fetching_the_channel(self):
        adapter = _make_adapter(logger=_make_logger())
        mock_chat = MagicMock()
        await adapter.initialize(mock_chat)
        adapter._discord_fetch = AsyncMock()
        body = json.dumps(
            {
                "type": "GATEWAY_MESSAGE_REACTION_ADD",
                "timestamp": 1,
                "data": {
                    "user_id": "user789",
                    "channel_id": "thread789",
                    "message_id": "msg123",
                    "guild_id": "guild1",
                    "channel_type": 11,
                    "thread": {"id": "thread789", "parent_id": "channel456"},
                    "emoji": {"name": "\U0001f44d", "id": None},
                    "member": {"user": {"id": "user789", "username": "testuser"}},
                },
            }
        )
        request = _FakeRequest(body, {"x-discord-gateway-token": "test-token"})

        response = await adapter.handle_webhook(request)

        assert response["status"] == 200
        adapter._discord_fetch.assert_not_called()
        assert mock_chat.process_reaction.call_args[0][0].thread_id == "discord:guild1:channel456:thread789"
        assert adapter._cached_thread_parent("thread789") == "channel456"


class TestThreadStarterMessageRouting:
    @pytest.mark.asyncio
    async def test_routes_forum_starter_operations_to_the_thread(self):
        adapter = _make_adapter(logger=_make_logger())

        def route(method: str, url: str) -> tuple[int, Any]:
            if url.endswith("/channels/starter123") and method == "GET":
                return 200, {"id": "starter123", "parent_id": "forum456"}
            # Forum channels hold no messages: a channel-type error, not 10008.
            if "/channels/forum456/" in url:
                return 400, {"code": 50024, "message": "Cannot execute action on this channel type"}
            if method in ("DELETE", "PUT"):
                return 204, None
            return 200, {"id": "starter123", "channel_id": "starter123", "content": "updated"}

        session = _with_session(adapter, route)
        thread_id = "discord:guild1:forum456:starter123"
        await adapter.edit_message(thread_id, "starter123", "updated")
        await adapter.delete_message(thread_id, "starter123")
        await adapter.add_reaction(thread_id, "starter123", HEART_GLYPH)
        await adapter.remove_reaction(thread_id, "starter123", HEART_GLYPH)

        assert session.calls == [
            (f"{API}/channels/starter123", "GET"),
            (f"{API}/channels/starter123/messages/starter123", "PATCH"),
            (f"{API}/channels/starter123/messages/starter123", "DELETE"),
            (f"{API}/channels/starter123/messages/starter123/reactions/{HEART}/@me", "PUT"),
            (f"{API}/channels/starter123/messages/starter123/reactions/{HEART}/@me", "DELETE"),
        ]

    @pytest.mark.asyncio
    async def test_falls_back_to_the_parent_channel_for_a_text_channel_starter(self):
        adapter = _make_adapter(logger=_make_logger())

        def route(method: str, url: str) -> tuple[int, Any]:
            if url.endswith("/channels/starter123") and method == "GET":
                return 200, {"id": "starter123", "parent_id": "channel456"}
            # A text-channel thread's starter lives in the parent channel.
            if "/channels/starter123/" in url:
                return 404, {"code": 10008, "message": "Unknown Message"}
            if method == "PUT":
                return 204, None
            return 200, {"id": "starter123", "channel_id": "channel456", "content": "updated"}

        session = _with_session(adapter, route)
        thread_id = "discord:guild1:channel456:starter123"
        result = await adapter.edit_message(thread_id, "starter123", "updated")
        await adapter.add_reaction(thread_id, "starter123", HEART_GLYPH)

        assert result.id == "starter123"
        assert session.calls == [
            (f"{API}/channels/starter123", "GET"),
            (f"{API}/channels/starter123/messages/starter123", "PATCH"),
            (f"{API}/channels/channel456/messages/starter123", "PATCH"),
            (f"{API}/channels/starter123/messages/starter123/reactions/{HEART}/@me", "PUT"),
            (f"{API}/channels/channel456/messages/starter123/reactions/{HEART}/@me", "PUT"),
        ]

    @pytest.mark.asyncio
    async def test_does_not_retry_other_discord_errors(self):
        adapter = _make_adapter(logger=_make_logger())
        responses = iter(
            [
                (200, {"id": "starter123", "parent_id": "channel456"}),
                (403, {"code": 50013, "message": "Missing Permissions"}),
            ]
        )
        session = _with_session(adapter, lambda _m, _u: next(responses))

        with pytest.raises(NetworkError, match="50013") as exc_info:
            await adapter.add_reaction("discord:guild1:channel456:starter123", "starter123", HEART_GLYPH)

        assert len(session.calls) == 2
        original = exc_info.value.original_error
        assert isinstance(original, DiscordApiError)
        assert (original.status, original.code) == (403, 50013)

    @pytest.mark.asyncio
    async def test_non_starter_message_in_a_thread_is_not_probed_twice(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._remember_thread_parent("thread789", "channel456")
        session = _with_session(adapter, lambda _m, _u: (404, {"code": 10008, "message": "Unknown Message"}))

        with pytest.raises(NetworkError):
            await adapter.delete_message("discord:guild1:channel456:thread789", "msg999")

        assert session.calls == [(f"{API}/channels/thread789/messages/msg999", "DELETE")]


class TestDiscordApiError:
    @pytest.mark.parametrize(
        ("body", "code"),
        [
            ('{"code": 10008, "message": "Unknown Message"}', 10008),
            ("<html>502 Bad Gateway</html>", None),
            ("", None),
            ('{"code": "10008"}', None),
            ('{"code": true}', None),
            ("[10008]", None),
        ],
        ids=["json", "html", "empty", "string-code", "bool-code", "non-object"],
    )
    def test_parses_only_a_numeric_json_code(self, body, code):
        error = DiscordApiError(500, body)
        assert error.code == code
        assert error.status == 500

    @pytest.mark.asyncio
    async def test_should_not_recover_when_160004_only_appears_elsewhere_in_the_body(self):
        adapter = _make_adapter(logger=_make_logger())
        _with_session(
            adapter,
            lambda _m, _u: (429, {"code": 429, "message": "You are being rate limited.", "retry_after": 160004}),
        )

        with pytest.raises(NetworkError) as exc_info:
            await adapter._create_discord_thread("channel123", "msg456")
        assert exc_info.value.original_error.code == 429

    @pytest.mark.asyncio
    async def test_recovers_from_160004_raised_by_the_real_fetch(self):
        adapter = _make_adapter(logger=_make_logger())
        _with_session(
            adapter,
            lambda _m, _u: (400, {"code": 160004, "message": "A thread has already been created for this message"}),
        )

        result = await adapter._create_discord_thread("channel123", "msg456")

        assert result["id"] == "msg456"


# ---------------------------------------------------------------------------
# Forwarded message snapshots (vercel/chat #825)
# ---------------------------------------------------------------------------


def _snapshot_message(**overrides: Any) -> dict[str, Any]:
    raw = {
        "id": "message123",
        "channel_id": "channel456",
        "guild_id": "guild789",
        "author": {"id": "user123", "username": "testuser"},
        "content": "Outer context",
        "timestamp": "2021-01-01T00:00:00.000Z",
        "edited_timestamp": None,
        "attachments": [
            {
                "id": "outer123",
                "filename": "outer.txt",
                "size": 100,
                "url": "https://cdn.discord.com/outer.txt",
                "content_type": "text/plain",
            }
        ],
        "type": 0,
        "message_reference": {"type": 1, "message_id": "source123", "channel_id": "source456"},
        "message_snapshots": [
            {
                "message": {
                    "type": 0,
                    "content": "Forwarded voice note",
                    "attachments": [
                        {
                            "id": "snapshot123",
                            "filename": "voice.ogg",
                            "size": 1234,
                            "url": "https://cdn.discord.com/voice.ogg",
                            "content_type": "audio/ogg",
                        }
                    ],
                }
            }
        ],
    }
    raw.update(overrides)
    return raw


class TestForwardedSnapshots:
    def test_parses_outer_and_snapshot_content_and_attachments(self):
        adapter = _make_adapter()

        message = adapter.parse_message(_snapshot_message())

        assert message.text == "Outer context\n\nForwarded voice note"
        assert [(a.name, a.type, a.url) for a in message.attachments] == [
            ("outer.txt", "file", "https://cdn.discord.com/outer.txt"),
            ("voice.ogg", "audio", "https://cdn.discord.com/voice.ogg"),
        ]
        assert all(a.fetch_data is not None for a in message.attachments)

    def test_handles_a_message_without_attachments(self):
        adapter = _make_adapter()
        raw = _snapshot_message(content="Message without attachments", message_snapshots=None)
        del raw["attachments"]

        message = adapter.parse_message(raw)

        assert message.text == "Message without attachments"
        assert message.attachments == []

    def test_empty_outer_content_is_dropped_from_the_joined_text(self):
        adapter = _make_adapter()
        message = adapter.parse_message(_snapshot_message(content="", attachments=[]))
        assert message.text == "Forwarded voice note"

    def test_thread_starter_referenced_message_snapshots_are_read(self):
        adapter = _make_adapter()
        referenced = _snapshot_message(content="", attachments=[])
        raw = {"id": "starter", "type": 21, "content": "", "referenced_message": referenced}

        message = adapter._parse_discord_message(raw, "discord:guild789:channel456:starter")

        assert message.text == "Forwarded voice note"
        assert [a.name for a in message.attachments] == ["voice.ogg"]

    @pytest.mark.asyncio
    async def test_reads_content_and_attachments_from_forwarded_message_snapshots(self):
        adapter = _make_adapter(logger=_make_logger())
        mock_chat = MagicMock()
        mock_chat.handle_incoming_message = AsyncMock()
        await adapter.initialize(mock_chat)
        body = json.dumps(
            {
                "type": "GATEWAY_MESSAGE_CREATE",
                "timestamp": 1,
                "data": {
                    "id": "forwarded-message",
                    "channel_id": "thread789",
                    "guild_id": "guild1",
                    "content": "",
                    "author": {"id": "user789", "username": "testuser", "bot": False},
                    "mentions": [],
                    "attachments": [],
                    "thread": {"id": "thread789", "parent_id": "channel456"},
                    "timestamp": "2026-08-14T15:39:39.136Z",
                    "message_snapshots": [
                        {
                            "message": {
                                "content": "Forwarded voice note",
                                "attachments": [
                                    {
                                        "content_type": "audio/ogg",
                                        "url": "https://cdn.discordapp.com/attachments/1/2/voice.ogg",
                                        "filename": "voice.ogg",
                                        "size": 1234,
                                    }
                                ],
                            }
                        }
                    ],
                },
            }
        )

        await adapter.handle_webhook(_FakeRequest(body, {"x-discord-gateway-token": "test-token"}))

        _adapter, thread_id, message = mock_chat.handle_incoming_message.call_args[0]
        assert thread_id == "discord:guild1:channel456:thread789"
        assert message.text == "Forwarded voice note"
        assert [(a.mime_type, a.name, a.url) for a in message.attachments] == [
            ("audio/ogg", "voice.ogg", "https://cdn.discordapp.com/attachments/1/2/voice.ogg")
        ]
        assert message.attachments[0].fetch_data is not None


# ---------------------------------------------------------------------------
# rehydrate_attachment / guarded downloads (vercel/chat #679, #800, #865)
# ---------------------------------------------------------------------------

SIGNED_URL = "https://cdn.discordapp.com/attachments/1/2/photo.png?ex=abc&is=def&hm=123"


class TestRehydrateAttachment:
    @pytest.mark.asyncio
    async def test_rebuilds_fetch_data_to_download_the_attachment_from_its_cdn_url(self):
        transfer = AsyncMock(return_value=b"photo")

        class _Adapter(DiscordAdapter):
            async def _download_attachment(self, url: str) -> bytes:
                return await transfer(url)

        adapter = _Adapter(
            DiscordAdapterConfig(bot_token="test-token", public_key=TEST_PUBLIC_KEY, application_id="test-app-id")
        )

        attachment = adapter.rehydrate_attachment(Attachment(type="image", url=SIGNED_URL))

        assert await attachment.fetch_data() == b"photo"
        transfer.assert_awaited_once_with(SIGNED_URL)

    @pytest.mark.asyncio
    async def test_rejects_internal_attachment_urls_before_the_network(self, monkeypatch):
        import chat_sdk.shared.download as download_module

        transport = AsyncMock()
        monkeypatch.setattr(download_module, "create_transport", lambda _adapter: transport)
        adapter = _make_adapter()

        attachment = adapter.rehydrate_attachment(
            Attachment(type="image", url="https://169.254.169.254/latest/meta-data")
        )

        with pytest.raises(NetworkError, match="Refusing to fetch an internal attachment URL"):
            await attachment.fetch_data()
        transport.assert_not_called()

    def test_returns_the_attachment_unchanged_when_it_has_no_url(self):
        adapter = _make_adapter()
        attachment = Attachment(type="image")

        rehydrated = adapter.rehydrate_attachment(attachment)

        assert rehydrated is attachment
        assert rehydrated.fetch_data is None

    @pytest.mark.asyncio
    async def test_prefers_fetch_metadata_url_over_the_attachment_url(self):
        adapter = _make_adapter()
        adapter._download_attachment = AsyncMock(return_value=b"x")
        attachment = Attachment(type="file", url="https://cdn.discordapp.com/stale", fetch_metadata={"url": SIGNED_URL})

        rehydrated = adapter.rehydrate_attachment(attachment)
        await rehydrated.fetch_data()

        adapter._download_attachment.assert_awaited_once_with(SIGNED_URL)
        assert attachment.fetch_data is None  # the input is not mutated

    @pytest.mark.asyncio
    async def test_inbound_attachment_survives_serialization(self):
        adapter = _make_adapter()
        adapter._download_attachment = AsyncMock(return_value=b"bytes")
        raw = _snapshot_message(
            attachments=[
                {"filename": "photo.png", "url": SIGNED_URL, "content_type": "image/png", "width": 800, "height": 600}
            ],
            message_snapshots=None,
        )
        parsed = adapter.parse_message(raw)
        assert parsed.attachments[0].fetch_metadata == {"url": SIGNED_URL}
        assert (parsed.attachments[0].width, parsed.attachments[0].height) == (800, 600)

        restored = Message.from_json(parsed.to_json())
        assert restored.attachments[0].fetch_data is None
        rehydrated = adapter.rehydrate_attachment(restored.attachments[0])

        assert await rehydrated.fetch_data() == b"bytes"
        adapter._download_attachment.assert_awaited_once_with(SIGNED_URL)

    @pytest.mark.asyncio
    async def test_download_goes_through_the_guarded_downloader(self, monkeypatch):
        import chat_sdk.shared.download as download_module

        guarded = AsyncMock(return_value=b"ok")
        monkeypatch.setattr(download_module, "download_attachment", guarded)
        adapter = _make_adapter()

        assert await adapter._download_attachment(SIGNED_URL) == b"ok"
        # Defaults (25 MB, 30 s) and no Discord credentials.
        guarded.assert_awaited_once_with(SIGNED_URL, adapter="discord")

    @pytest.mark.asyncio
    async def test_download_wraps_unknown_errors_in_network_error(self, monkeypatch):
        import chat_sdk.shared.download as download_module

        cause = OSError("connection reset")
        monkeypatch.setattr(download_module, "download_attachment", AsyncMock(side_effect=cause))
        adapter = _make_adapter()

        with pytest.raises(NetworkError, match="Failed to download Discord attachment") as exc_info:
            await adapter._download_attachment(SIGNED_URL)
        assert exc_info.value.original_error is cause


class TestSuppressedLinksPayload:
    @pytest.mark.asyncio
    async def test_preserves_suppressed_links_in_the_discord_api_payload(self):
        adapter = _make_adapter(logger=_make_logger())
        markdown = "<https://google.com> [Google](<https://google.com>)"
        adapter._discord_fetch = AsyncMock(return_value={"id": "msg-suppressed-links"})

        await adapter.post_message("discord:guild1:channel456", PostableMarkdown(markdown=markdown))

        adapter._discord_fetch.assert_called_once()
        path, method, payload = adapter._discord_fetch.call_args[0][:3]
        assert (path, method, payload["content"]) == ("/channels/channel456/messages", "POST", markdown)
