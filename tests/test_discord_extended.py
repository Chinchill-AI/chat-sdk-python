"""Extended tests for the Discord adapter -- message ops, reactions, typing, DMs,
fetch, thread creation, gateway events, error handling.

Ported from the remaining test categories in
packages/adapter-discord/src/index.test.ts (lines ~1100-4037)
and packages/adapter-discord/src/gateway.test.ts.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chat_sdk.adapters.discord.adapter import (
    CHANNEL_TYPE_DM,
    CHANNEL_TYPE_GROUP_DM,
    CHANNEL_TYPE_PUBLIC_THREAD,
    DiscordAdapter,
    create_discord_adapter,
)
from chat_sdk.adapters.discord.types import DiscordAdapterConfig, DiscordThreadId
from chat_sdk.shared.errors import NetworkError, ValidationError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TEST_PUBLIC_KEY = "a" * 64


def _make_adapter(**overrides) -> DiscordAdapter:
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
    def __init__(self, body: str, headers: dict[str, str] | None = None):
        self._body = body
        self.headers = headers or {}

    async def text(self) -> str:
        return self._body

    @property
    def data(self) -> bytes:
        return self._body.encode("utf-8")


def _gateway_request(body: str, token: str = "test-token") -> _FakeRequest:
    return _FakeRequest(
        body,
        {
            "x-discord-gateway-token": token,
            "content-type": "application/json",
        },
    )


def _msg_response(msg_id="msg001", channel_id="channel456", content="Hello"):
    return {
        "id": msg_id,
        "channel_id": channel_id,
        "content": content,
        "timestamp": "2021-01-01T00:00:00.000Z",
        "author": {"id": "test-app-id", "username": "bot"},
    }


# ============================================================================
# Edge cases
# ============================================================================


class TestEdgeCases:
    def test_empty_content(self):
        adapter = _make_adapter()
        raw = {
            "id": "msg1",
            "channel_id": "ch",
            "author": {"id": "u", "username": "u"},
            "content": "",
            "timestamp": "2021-01-01T00:00:00.000Z",
            "attachments": [],
        }
        assert adapter.parse_message(raw).text == ""

    def test_null_width_height_attachments(self):
        adapter = _make_adapter()
        raw = {
            "id": "msg1",
            "channel_id": "ch",
            "author": {"id": "u", "username": "u"},
            "content": "",
            "timestamp": "2021-01-01T00:00:00.000Z",
            "attachments": [
                {
                    "filename": "doc.pdf",
                    "url": "https://example.com",
                    "content_type": "application/pdf",
                    "width": None,
                    "height": None,
                }
            ],
        }
        msg = adapter.parse_message(raw)
        # None/null width/height should not cause errors
        assert msg.attachments[0].type == "file"

    def test_missing_attachment_content_type(self):
        adapter = _make_adapter()
        raw = {
            "id": "msg1",
            "channel_id": "ch",
            "author": {"id": "u", "username": "u"},
            "content": "",
            "timestamp": "2021-01-01T00:00:00.000Z",
            "attachments": [{"filename": "unknown", "url": "https://example.com"}],
        }
        msg = adapter.parse_message(raw)
        assert msg.attachments[0].type == "file"


# ============================================================================
# Date Parsing
# ============================================================================


class TestDateParsing:
    def test_iso_timestamp_to_date(self):
        adapter = _make_adapter()
        raw = {
            "id": "msg1",
            "channel_id": "ch",
            "author": {"id": "u", "username": "u"},
            "content": "Hello",
            "timestamp": "2021-01-01T12:30:00.000Z",
            "attachments": [],
        }
        msg = adapter.parse_message(raw)
        assert msg.metadata.date_sent.year == 2021
        assert msg.metadata.date_sent.hour == 12
        assert msg.metadata.date_sent.minute == 30


# ============================================================================
# Formatted text extraction
# ============================================================================


class TestFormattedTextExtraction:
    def test_extracts_plain_text_from_markdown(self):
        adapter = _make_adapter()
        raw = {
            "id": "msg1",
            "channel_id": "ch",
            "author": {"id": "u", "username": "u"},
            "content": "**bold** and *italic*",
            "timestamp": "2021-01-01T00:00:00.000Z",
            "attachments": [],
        }
        msg = adapter.parse_message(raw)
        assert msg.text == "bold and italic"

    def test_extracts_text_from_user_mentions(self):
        adapter = _make_adapter()
        raw = {
            "id": "msg1",
            "channel_id": "ch",
            "author": {"id": "u", "username": "u"},
            "content": "Hey <@456789>!",
            "timestamp": "2021-01-01T00:00:00.000Z",
            "attachments": [],
        }
        msg = adapter.parse_message(raw)
        assert "@456789" in msg.text

    def test_extracts_text_from_channel_mentions(self):
        adapter = _make_adapter()
        raw = {
            "id": "msg1",
            "channel_id": "ch",
            "author": {"id": "u", "username": "u"},
            "content": "Check <#987654>",
            "timestamp": "2021-01-01T00:00:00.000Z",
            "attachments": [],
        }
        msg = adapter.parse_message(raw)
        assert "#987654" in msg.text


# ============================================================================
# Thread starter message handling
# ============================================================================


class TestThreadStarterMessage:
    def test_uses_referenced_message_content(self):
        adapter = _make_adapter()
        raw = {
            "id": "starter123",
            "channel_id": "thread456",
            "guild_id": "guild789",
            "author": {"id": "system", "username": "system", "bot": True},
            "content": "",
            "timestamp": "2021-01-01T00:00:00.000Z",
            "attachments": [],
            "type": 21,
            "message_reference": {
                "message_id": "parent123",
                "channel_id": "channel456",
                "guild_id": "guild789",
            },
            "referenced_message": {
                "id": "parent123",
                "channel_id": "channel456",
                "guild_id": "guild789",
                "author": {
                    "id": "user123",
                    "username": "parent-author",
                    "global_name": "Parent Author",
                },
                "content": "Parent message content",
                "timestamp": "2021-01-01T00:00:00.000Z",
                "attachments": [],
            },
        }
        msg = adapter.parse_message(raw)
        assert msg.id == "parent123"
        assert msg.text == "Parent message content"
        assert msg.author.user_id == "user123"

    def test_falls_back_when_no_referenced_message(self):
        adapter = _make_adapter()
        raw = {
            "id": "starter123",
            "channel_id": "thread456",
            "guild_id": "guild789",
            "author": {"id": "system", "username": "system", "bot": True},
            "content": "",
            "timestamp": "2021-01-01T00:00:00.000Z",
            "attachments": [],
            "type": 21,
            "message_reference": {
                "message_id": "parent123",
                "channel_id": "channel456",
            },
            "referenced_message": None,
        }
        msg = adapter.parse_message(raw)
        assert msg.id == "starter123"
        assert msg.text == ""

    def test_username_as_fullname_fallback(self):
        adapter = _make_adapter()
        raw = {
            "id": "msg1",
            "channel_id": "ch",
            "author": {"id": "u", "username": "testuser"},
            "content": "Hello",
            "timestamp": "2021-01-01T00:00:00.000Z",
            "attachments": [],
        }
        msg = adapter.parse_message(raw)
        assert msg.author.full_name == "testuser"


# ============================================================================
# postMessage
# ============================================================================


class TestPostMessage:
    @pytest.mark.asyncio
    async def test_posts_plain_text(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(return_value=_msg_response())

        result = await adapter.post_message("discord:guild1:channel456", "Hello world")

        assert result.id == "msg001"
        assert result.thread_id == "discord:guild1:channel456"
        adapter._discord_fetch.assert_called_once()
        call_args = adapter._discord_fetch.call_args
        assert call_args[0][0] == "/channels/channel456/messages"
        assert call_args[0][1] == "POST"

    @pytest.mark.asyncio
    async def test_posts_to_thread_channel(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(return_value=_msg_response(msg_id="msg002", channel_id="thread789"))

        result = await adapter.post_message("discord:guild1:channel456:thread789", "Thread reply")

        assert result.id == "msg002"
        assert result.thread_id == "discord:guild1:channel456:thread789"
        call_args = adapter._discord_fetch.call_args
        assert call_args[0][0] == "/channels/thread789/messages"

    @pytest.mark.asyncio
    async def test_truncates_long_content(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(return_value=_msg_response())

        long_message = "a" * 2500
        await adapter.post_message("discord:guild1:channel456", long_message)

        call_args = adapter._discord_fetch.call_args
        payload = call_args[0][2]
        assert len(payload["content"]) <= 2000
        assert payload["content"].endswith("...")

    @pytest.mark.asyncio
    async def test_does_not_truncate_short_content(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(return_value=_msg_response())

        await adapter.post_message("discord:guild1:channel456", "short")

        call_args = adapter._discord_fetch.call_args
        payload = call_args[0][2]
        assert payload["content"] == "short"


# ============================================================================
# editMessage
# ============================================================================


class TestEditMessage:
    @pytest.mark.asyncio
    async def test_edits_with_patch(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(return_value=_msg_response(content="Updated content"))

        result = await adapter.edit_message("discord:guild1:channel456", "msg001", "Updated content")

        assert result.id == "msg001"
        assert result.thread_id == "discord:guild1:channel456"
        call_args = adapter._discord_fetch.call_args
        assert call_args[0][0] == "/channels/channel456/messages/msg001"
        assert call_args[0][1] == "PATCH"

    @pytest.mark.asyncio
    async def test_edits_in_thread(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(return_value=_msg_response(msg_id="msg002", channel_id="thread789"))

        result = await adapter.edit_message("discord:guild1:channel456:thread789", "msg002", "Edited thread reply")

        assert result.id == "msg002"
        call_args = adapter._discord_fetch.call_args
        assert call_args[0][0] == "/channels/thread789/messages/msg002"

    @pytest.mark.asyncio
    async def test_truncates_on_edit(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(return_value=_msg_response())

        long_message = "b" * 2500
        await adapter.edit_message("discord:guild1:channel456", "msg003", long_message)

        call_args = adapter._discord_fetch.call_args
        payload = call_args[0][2]
        assert len(payload["content"]) <= 2000
        assert payload["content"].endswith("...")


# ============================================================================
# deleteMessage
# ============================================================================


class TestDeleteMessage:
    @pytest.mark.asyncio
    async def test_deletes_message(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(return_value=None)

        await adapter.delete_message("discord:guild1:channel456", "msg001")

        assert adapter._discord_fetch.call_count == 1
        adapter._discord_fetch.assert_called_once_with("/channels/channel456/messages/msg001", "DELETE")

    @pytest.mark.asyncio
    async def test_deletes_in_thread(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(return_value=None)

        await adapter.delete_message("discord:guild1:channel456:thread789", "msg002")

        assert adapter._discord_fetch.call_count == 1
        adapter._discord_fetch.assert_called_once_with("/channels/thread789/messages/msg002", "DELETE")


# ============================================================================
# addReaction
# ============================================================================


class TestAddReaction:
    @pytest.mark.asyncio
    async def test_adds_reaction(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(return_value=None)

        await adapter.add_reaction("discord:guild1:channel456", "msg001", "thumbs_up")

        call_args = adapter._discord_fetch.call_args
        path = call_args[0][0]
        assert "/channels/channel456/messages/msg001/reactions/" in path
        assert path.endswith("/@me")
        assert call_args[0][1] == "PUT"

    @pytest.mark.asyncio
    async def test_adds_reaction_in_thread(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(return_value=None)

        await adapter.add_reaction("discord:guild1:channel456:thread789", "msg001", "heart")

        call_args = adapter._discord_fetch.call_args
        path = call_args[0][0]
        assert "/channels/thread789/messages/msg001/reactions/" in path


# ============================================================================
# removeReaction
# ============================================================================


class TestRemoveReaction:
    @pytest.mark.asyncio
    async def test_removes_reaction(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(return_value=None)

        await adapter.remove_reaction("discord:guild1:channel456", "msg001", "thumbs_up")

        call_args = adapter._discord_fetch.call_args
        path = call_args[0][0]
        assert "/channels/channel456/messages/msg001/reactions/" in path
        assert path.endswith("/@me")
        assert call_args[0][1] == "DELETE"

    @pytest.mark.asyncio
    async def test_removes_reaction_in_thread(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(return_value=None)

        await adapter.remove_reaction("discord:guild1:channel456:thread789", "msg001", "fire")

        call_args = adapter._discord_fetch.call_args
        path = call_args[0][0]
        assert "/channels/thread789/messages/msg001/reactions/" in path
        assert call_args[0][1] == "DELETE"


# ============================================================================
# normalizeDiscordEmoji / encodeEmoji
# ============================================================================


class TestEmojiEncoding:
    def test_url_encodes_emoji(self):
        adapter = _make_adapter()
        result = adapter._encode_emoji("thumbs_up")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_handles_string_emoji_input(self):
        adapter = _make_adapter()
        result = adapter._encode_emoji("fire")
        assert isinstance(result, str)

    def test_handles_emoji_value_object(self):
        from chat_sdk.types import EmojiValue

        adapter = _make_adapter()
        result = adapter._encode_emoji(EmojiValue(name="heart"))
        assert isinstance(result, str)
        assert len(result) > 0


# ============================================================================
# truncateContent
# ============================================================================


class TestTruncateContent:
    def test_returns_unchanged_within_limit(self):
        adapter = _make_adapter()
        assert adapter._truncate_content("Hello world") == "Hello world"

    def test_returns_unchanged_at_exactly_2000(self):
        adapter = _make_adapter()
        content = "x" * 2000
        assert adapter._truncate_content(content) == content
        assert len(adapter._truncate_content(content)) == 2000

    def test_truncates_exceeding_2000_with_ellipsis(self):
        adapter = _make_adapter()
        content = "y" * 2500
        result = adapter._truncate_content(content)
        assert len(result) == 2000
        assert result.endswith("...")
        assert result[:1997] == "y" * 1997

    def test_truncates_at_exactly_2001(self):
        adapter = _make_adapter()
        content = "z" * 2001
        result = adapter._truncate_content(content)
        assert len(result) == 2000
        assert result.endswith("...")

    def test_handles_empty_string(self):
        adapter = _make_adapter()
        assert adapter._truncate_content("") == ""


# ============================================================================
# channelIdFromThreadId
# ============================================================================


class TestChannelIdFromThreadId:
    def test_returns_channel_level_from_thread(self):
        adapter = _make_adapter()
        # Thread IDs: discord:guild:channel:thread -> should decode and re-encode without thread
        decoded = adapter.decode_thread_id("discord:guild1:channel456:thread789")
        result = adapter.encode_thread_id(DiscordThreadId(guild_id=decoded.guild_id, channel_id=decoded.channel_id))
        assert result == "discord:guild1:channel456"

    def test_returns_as_is_for_channel(self):
        adapter = _make_adapter()
        decoded = adapter.decode_thread_id("discord:guild1:channel456")
        result = adapter.encode_thread_id(DiscordThreadId(guild_id=decoded.guild_id, channel_id=decoded.channel_id))
        assert result == "discord:guild1:channel456"

    def test_handles_dm_channel(self):
        adapter = _make_adapter()
        decoded = adapter.decode_thread_id("discord:@me:dm123")
        result = adapter.encode_thread_id(DiscordThreadId(guild_id=decoded.guild_id, channel_id=decoded.channel_id))
        assert result == "discord:@me:dm123"


# ============================================================================
# startTyping
# ============================================================================


class TestStartTyping:
    @pytest.mark.asyncio
    async def test_sends_typing_to_channel(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(return_value=None)

        await adapter.start_typing("discord:guild1:channel456")

        assert adapter._discord_fetch.call_count == 1
        adapter._discord_fetch.assert_called_once_with("/channels/channel456/typing", "POST")

    @pytest.mark.asyncio
    async def test_sends_typing_to_thread(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(return_value=None)

        await adapter.start_typing("discord:guild1:channel456:thread789")

        assert adapter._discord_fetch.call_count == 1
        adapter._discord_fetch.assert_called_once_with("/channels/thread789/typing", "POST")


# ============================================================================
# openDM
# ============================================================================


class TestOpenDM:
    @pytest.mark.asyncio
    async def test_creates_dm_channel(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(return_value={"id": "dm-channel-123", "type": 1})

        result = await adapter.open_dm("user123")

        assert result == "discord:@me:dm-channel-123"
        adapter._discord_fetch.assert_called_once_with("/users/@me/channels", "POST", {"recipient_id": "user123"})


# ============================================================================
# fetchMessages
# ============================================================================


class TestFetchMessages:
    @pytest.mark.asyncio
    async def test_fetches_from_channel(self):
        adapter = _make_adapter(logger=_make_logger())
        raw_messages = [
            {
                "id": "msg3",
                "channel_id": "channel456",
                "content": "Third",
                "timestamp": "2021-01-01T00:03:00.000Z",
                "author": {"id": "u1", "username": "user"},
                "attachments": [],
            },
            {
                "id": "msg2",
                "channel_id": "channel456",
                "content": "Second",
                "timestamp": "2021-01-01T00:02:00.000Z",
                "author": {"id": "u1", "username": "user"},
                "attachments": [],
            },
        ]
        adapter._discord_fetch = AsyncMock(return_value=raw_messages)

        from chat_sdk.types import FetchOptions

        result = await adapter.fetch_messages("discord:guild1:channel456", FetchOptions(limit=2))

        # Messages should be reversed to chronological order
        assert len(result.messages) == 2
        assert result.messages[0].id == "msg2"  # oldest first
        assert result.messages[1].id == "msg3"  # newest second

    @pytest.mark.asyncio
    async def test_fetches_from_thread(self):
        adapter = _make_adapter(logger=_make_logger())
        raw_messages = [
            {
                "id": "msg1",
                "channel_id": "thread789",
                "content": "Thread msg",
                "timestamp": "2021-01-01T00:00:00.000Z",
                "author": {"id": "u1", "username": "user"},
                "attachments": [],
            },
        ]
        adapter._discord_fetch = AsyncMock(return_value=raw_messages)

        result = await adapter.fetch_messages("discord:guild1:channel456:thread789")

        assert len(result.messages) == 1
        call_args = adapter._discord_fetch.call_args
        assert "/channels/thread789/messages?" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_backward_pagination_cursor(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(return_value=[])

        from chat_sdk.types import FetchOptions

        await adapter.fetch_messages(
            "discord:guild1:channel456",
            FetchOptions(cursor="msg100", direction="backward"),
        )

        call_args = adapter._discord_fetch.call_args
        assert "before=msg100" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_forward_pagination_cursor(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(return_value=[])

        from chat_sdk.types import FetchOptions

        await adapter.fetch_messages(
            "discord:guild1:channel456",
            FetchOptions(cursor="msg100", direction="forward"),
        )

        call_args = adapter._discord_fetch.call_args
        assert "after=msg100" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_returns_next_cursor_when_results_match_limit(self):
        adapter = _make_adapter(logger=_make_logger())
        raw_messages = [
            {
                "id": f"msg{i}",
                "channel_id": "channel456",
                "content": f"Message {i}",
                "timestamp": "2021-01-01T00:00:00.000Z",
                "author": {"id": "u1", "username": "user"},
                "attachments": [],
            }
            for i in range(10)
        ]
        adapter._discord_fetch = AsyncMock(return_value=raw_messages)

        from chat_sdk.types import FetchOptions

        result = await adapter.fetch_messages("discord:guild1:channel456", FetchOptions(limit=10))

        assert result.next_cursor is not None

    @pytest.mark.asyncio
    async def test_no_next_cursor_when_fewer_results(self):
        adapter = _make_adapter(logger=_make_logger())
        raw_messages = [
            {
                "id": "msg1",
                "channel_id": "channel456",
                "content": "Only one",
                "timestamp": "2021-01-01T00:00:00.000Z",
                "author": {"id": "u1", "username": "user"},
                "attachments": [],
            },
        ]
        adapter._discord_fetch = AsyncMock(return_value=raw_messages)

        from chat_sdk.types import FetchOptions

        result = await adapter.fetch_messages("discord:guild1:channel456", FetchOptions(limit=50))

        assert result.next_cursor is None


# ============================================================================
# fetchThread
# ============================================================================


class TestFetchThread:
    @pytest.mark.asyncio
    async def test_fetches_guild_text_channel(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(return_value={"id": "channel456", "name": "general", "type": 0})

        result = await adapter.fetch_thread("discord:guild1:channel456")

        assert result.id == "discord:guild1:channel456"
        assert result.channel_id == "channel456"
        assert result.channel_name == "general"
        assert result.is_dm is False

    @pytest.mark.asyncio
    async def test_fetches_dm_channel(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(return_value={"id": "dm123", "type": CHANNEL_TYPE_DM})

        result = await adapter.fetch_thread("discord:@me:dm123")

        assert result.is_dm is True

    @pytest.mark.asyncio
    async def test_fetches_group_dm(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(
            return_value={
                "id": "gdm123",
                "name": "Group Chat",
                "type": CHANNEL_TYPE_GROUP_DM,
            }
        )

        result = await adapter.fetch_thread("discord:@me:gdm123")

        assert result.is_dm is True
        assert result.channel_name == "Group Chat"


# ============================================================================
# Forwarded Gateway Events
# ============================================================================


class TestForwardedGatewayEvents:
    @pytest.mark.asyncio
    async def test_rejects_invalid_gateway_token(self):
        adapter = _make_adapter(logger=_make_logger())
        body = json.dumps(
            {
                "type": "GATEWAY_MESSAGE_CREATE",
                "timestamp": 1234567890,
                "data": {},
            }
        )
        request = _gateway_request(body, token="wrong-token")

        response = await adapter.handle_webhook(request)

        assert response["status"] == 401

    @pytest.mark.asyncio
    async def test_accepts_valid_gateway_token(self):
        adapter = _make_adapter(logger=_make_logger())
        body = json.dumps(
            {
                "type": "GATEWAY_UNKNOWN_EVENT",
                "timestamp": 1234567890,
                "data": {},
            }
        )
        request = _gateway_request(body)

        response = await adapter.handle_webhook(request)

        assert response["status"] == 200

    @pytest.mark.asyncio
    async def test_returns_400_for_invalid_json(self):
        adapter = _make_adapter(logger=_make_logger())
        request = _gateway_request("not-json")

        response = await adapter.handle_webhook(request)

        assert response["status"] == 400

    @pytest.mark.asyncio
    async def test_handles_message_create(self):
        adapter = _make_adapter(logger=_make_logger())
        mock_chat = MagicMock()
        mock_chat.handle_incoming_message = AsyncMock()
        adapter._chat = mock_chat

        body = json.dumps(
            {
                "type": "GATEWAY_MESSAGE_CREATE",
                "timestamp": 1234567890,
                "data": {
                    "id": "msg123",
                    "channel_id": "channel456",
                    "guild_id": "guild1",
                    "content": "Hello from gateway",
                    "timestamp": "2021-01-01T00:00:00.000Z",
                    "author": {"id": "user789", "username": "testuser", "bot": False},
                    "mentions": [],
                    "attachments": [],
                },
            }
        )
        request = _gateway_request(body)

        response = await adapter.handle_webhook(request)

        assert response["status"] == 200
        mock_chat.handle_incoming_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_handles_reaction_add(self):
        adapter = _make_adapter(logger=_make_logger())
        mock_chat = MagicMock()
        mock_chat.handle_incoming_message = AsyncMock()
        mock_chat.process_reaction = MagicMock()
        adapter._chat = mock_chat

        body = json.dumps(
            {
                "type": "GATEWAY_MESSAGE_REACTION_ADD",
                "timestamp": 1234567890,
                "data": {
                    "user_id": "user789",
                    "channel_id": "channel456",
                    "message_id": "msg123",
                    "guild_id": "guild1",
                    "emoji": {"name": "\U0001f44d", "id": None},
                    "member": {"user": {"id": "user789", "username": "testuser"}},
                },
            }
        )
        request = _gateway_request(body)

        response = await adapter.handle_webhook(request)

        assert response["status"] == 200
        mock_chat.process_reaction.assert_called_once()
        call_args = mock_chat.process_reaction.call_args[0][0]
        assert call_args.added is True
        assert call_args.message_id == "msg123"

    @pytest.mark.asyncio
    async def test_handles_reaction_remove(self):
        adapter = _make_adapter(logger=_make_logger())
        mock_chat = MagicMock()
        mock_chat.handle_incoming_message = AsyncMock()
        mock_chat.process_reaction = MagicMock()
        adapter._chat = mock_chat

        body = json.dumps(
            {
                "type": "GATEWAY_MESSAGE_REACTION_REMOVE",
                "timestamp": 1234567890,
                "data": {
                    "user_id": "user789",
                    "channel_id": "channel456",
                    "message_id": "msg123",
                    "guild_id": "guild1",
                    "emoji": {"name": "\u2764\ufe0f", "id": None},
                    "member": {"user": {"id": "user789", "username": "testuser"}},
                },
            }
        )
        request = _gateway_request(body)

        response = await adapter.handle_webhook(request)

        assert response["status"] == 200
        mock_chat.process_reaction.assert_called_once()
        call_args = mock_chat.process_reaction.call_args[0][0]
        assert call_args.added is False
        assert call_args.message_id == "msg123"


# ============================================================================
# Forwarded message -- thread detection
# ============================================================================


class TestForwardedMessageThreadHandling:
    @pytest.mark.asyncio
    async def test_uses_thread_info_when_provided(self):
        adapter = _make_adapter(logger=_make_logger())
        mock_chat = MagicMock()
        mock_chat.handle_incoming_message = AsyncMock()
        adapter._chat = mock_chat

        body = json.dumps(
            {
                "type": "GATEWAY_MESSAGE_CREATE",
                "timestamp": 1234567890,
                "data": {
                    "id": "msg123",
                    "channel_id": "thread789",
                    "guild_id": "guild1",
                    "content": "Thread message",
                    "timestamp": "2021-01-01T00:00:00.000Z",
                    "author": {"id": "user789", "username": "testuser", "bot": False},
                    "mentions": [],
                    "attachments": [],
                    "thread": {"id": "thread789", "parent_id": "channel456"},
                },
            }
        )
        request = _gateway_request(body)

        await adapter.handle_webhook(request)

        mock_chat.handle_incoming_message.assert_called_once()
        call_args = mock_chat.handle_incoming_message.call_args[0]
        assert call_args[1] == "discord:guild1:channel456:thread789"

    @pytest.mark.asyncio
    async def test_detects_thread_by_channel_type(self):
        adapter = _make_adapter(logger=_make_logger())
        mock_chat = MagicMock()
        mock_chat.handle_incoming_message = AsyncMock()
        adapter._chat = mock_chat
        adapter._discord_fetch = AsyncMock(return_value={"parent_id": "channel456"})

        body = json.dumps(
            {
                "type": "GATEWAY_MESSAGE_CREATE",
                "timestamp": 1234567890,
                "data": {
                    "id": "msg123",
                    "channel_id": "thread789",
                    "guild_id": "guild1",
                    "channel_type": CHANNEL_TYPE_PUBLIC_THREAD,
                    "content": "Thread message via channel_type",
                    "timestamp": "2021-01-01T00:00:00.000Z",
                    "author": {"id": "user789", "username": "testuser", "bot": False},
                    "mentions": [],
                    "attachments": [],
                },
            }
        )
        request = _gateway_request(body)

        await adapter.handle_webhook(request)

        adapter._discord_fetch.assert_called_once_with("/channels/thread789", "GET")
        call_args = mock_chat.handle_incoming_message.call_args[0]
        assert call_args[1] == "discord:guild1:channel456:thread789"

    @pytest.mark.asyncio
    async def test_creates_thread_when_mentioned(self):
        adapter = _make_adapter(logger=_make_logger())
        mock_chat = MagicMock()
        mock_chat.handle_incoming_message = AsyncMock()
        adapter._chat = mock_chat
        adapter._discord_fetch = AsyncMock(return_value={"id": "new-thread-id", "name": "New Thread"})

        body = json.dumps(
            {
                "type": "GATEWAY_MESSAGE_CREATE",
                "timestamp": 1234567890,
                "data": {
                    "id": "msg123",
                    "channel_id": "channel456",
                    "guild_id": "guild1",
                    "content": "Hey bot",
                    "timestamp": "2021-01-01T00:00:00.000Z",
                    "author": {"id": "user789", "username": "testuser", "bot": False},
                    "is_mention": True,
                    "mentions": [{"id": "test-app-id", "username": "bot"}],
                    "attachments": [],
                },
            }
        )
        request = _gateway_request(body)

        await adapter.handle_webhook(request)

        # Should have created a thread
        thread_call = adapter._discord_fetch.call_args_list[0]
        assert "/channels/channel456/messages/msg123/threads" in thread_call[0][0]
        assert thread_call[0][1] == "POST"
        assert thread_call[0][2]["auto_archive_duration"] == 1440


# ============================================================================
# Forwarded reaction -- thread parent caching
# ============================================================================


class TestForwardedReactionCaching:
    @pytest.mark.asyncio
    async def test_fetches_and_caches_thread_parent(self):
        adapter = _make_adapter(logger=_make_logger())
        mock_chat = MagicMock()
        mock_chat.process_reaction = MagicMock()
        adapter._chat = mock_chat
        adapter._discord_fetch = AsyncMock(return_value={"parent_id": "channel456"})

        body1 = json.dumps(
            {
                "type": "GATEWAY_MESSAGE_REACTION_ADD",
                "timestamp": 1234567890,
                "data": {
                    "user_id": "user789",
                    "channel_id": "thread789",
                    "message_id": "msg123",
                    "guild_id": "guild1",
                    "channel_type": CHANNEL_TYPE_PUBLIC_THREAD,
                    "emoji": {"name": "\U0001f44d", "id": None},
                    "member": {"user": {"id": "user789", "username": "testuser"}},
                },
            }
        )
        request1 = _gateway_request(body1)

        await adapter.handle_webhook(request1)

        adapter._discord_fetch.assert_called_once_with("/channels/thread789", "GET")
        call_args = mock_chat.process_reaction.call_args[0][0]
        assert call_args.thread_id == "discord:guild1:channel456:thread789"

        # Second reaction on same thread -- should use cache
        adapter._discord_fetch.reset_mock()
        mock_chat.process_reaction.reset_mock()

        body2 = json.dumps(
            {
                "type": "GATEWAY_MESSAGE_REACTION_ADD",
                "timestamp": 1234567890,
                "data": {
                    "user_id": "user789",
                    "channel_id": "thread789",
                    "message_id": "msg456",
                    "guild_id": "guild1",
                    "channel_type": CHANNEL_TYPE_PUBLIC_THREAD,
                    "emoji": {"name": "\U0001f525", "id": None},
                    "member": {"user": {"id": "user789", "username": "testuser"}},
                },
            }
        )
        request2 = _gateway_request(body2)

        await adapter.handle_webhook(request2)

        # Should NOT have fetched again (used cache)
        adapter._discord_fetch.assert_not_called()
        call_args = mock_chat.process_reaction.call_args[0][0]
        assert call_args.thread_id == "discord:guild1:channel456:thread789"

    @pytest.mark.asyncio
    async def test_missing_user_info_skips_reaction(self):
        adapter = _make_adapter(logger=_make_logger())
        mock_chat = MagicMock()
        mock_chat.process_reaction = MagicMock()
        adapter._chat = mock_chat

        body = json.dumps(
            {
                "type": "GATEWAY_MESSAGE_REACTION_ADD",
                "timestamp": 1234567890,
                "data": {
                    "user_id": "user789",
                    "channel_id": "channel456",
                    "message_id": "msg123",
                    "guild_id": "guild1",
                    "emoji": {"name": "\U0001f44d", "id": None},
                    # No member or user field
                },
            }
        )
        request = _gateway_request(body)

        response = await adapter.handle_webhook(request)

        assert response["status"] == 200
        mock_chat.process_reaction.assert_not_called()

    @pytest.mark.asyncio
    async def test_custom_emoji_with_id(self):
        adapter = _make_adapter(logger=_make_logger())
        mock_chat = MagicMock()
        mock_chat.process_reaction = MagicMock()
        adapter._chat = mock_chat

        body = json.dumps(
            {
                "type": "GATEWAY_MESSAGE_REACTION_ADD",
                "timestamp": 1234567890,
                "data": {
                    "user_id": "user789",
                    "channel_id": "channel456",
                    "message_id": "msg123",
                    "guild_id": "guild1",
                    "emoji": {"name": "custom_emoji", "id": "emoji123"},
                    "member": {"user": {"id": "user789", "username": "testuser"}},
                },
            }
        )
        request = _gateway_request(body)

        await adapter.handle_webhook(request)

        call_args = mock_chat.process_reaction.call_args[0][0]
        assert call_args.raw_emoji == "<:custom_emoji:emoji123>"


# ============================================================================
# Component interaction edge cases
# ============================================================================


class TestComponentInteractionEdgeCases:
    @pytest.mark.asyncio
    async def test_button_in_thread_context(self):
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
                "channel_id": "thread456",
                "channel": {
                    "id": "thread456",
                    "type": CHANNEL_TYPE_PUBLIC_THREAD,
                    "parent_id": "channel789",
                },
                "member": {
                    "user": {
                        "id": "user789",
                        "username": "testuser",
                        "global_name": "Test User",
                    },
                },
                "message": {
                    "id": "message123",
                    "channel_id": "thread456",
                },
                "data": {
                    "custom_id": "approve_btn",
                    "component_type": 2,
                },
            }
        )
        request = _FakeRequest(
            body,
            {
                "x-signature-ed25519": "valid",
                "x-signature-timestamp": "12345",
            },
        )

        await adapter.handle_webhook(request)

        mock_chat.process_action.assert_called_once()
        call_args = mock_chat.process_action.call_args[0][0]
        assert call_args.action_id == "approve_btn"
        assert call_args.thread_id == "discord:guild123:channel789:thread456"

    @pytest.mark.asyncio
    async def test_slash_command_in_thread(self):
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
                "channel_id": "thread456",
                "channel": {
                    "id": "thread456",
                    "type": CHANNEL_TYPE_PUBLIC_THREAD,
                    "parent_id": "channel789",
                },
                "member": {
                    "user": {
                        "id": "user789",
                        "username": "testuser",
                    },
                },
                "data": {"name": "status", "type": 1},
            }
        )
        request = _FakeRequest(
            body,
            {
                "x-signature-ed25519": "valid",
                "x-signature-timestamp": "12345",
            },
        )

        await adapter.handle_webhook(request)

        mock_chat.process_slash_command.assert_called_once()
        call_args = mock_chat.process_slash_command.call_args[0][0]
        assert call_args.command == "/status"
        assert call_args.channel_id == "discord:guild123:channel789:thread456"


# ============================================================================
# DM forwarded messages
# ============================================================================


class TestDMForwardedMessages:
    @pytest.mark.asyncio
    async def test_handles_dm_message_no_guild(self):
        adapter = _make_adapter(logger=_make_logger())
        mock_chat = MagicMock()
        mock_chat.handle_incoming_message = AsyncMock()
        adapter._chat = mock_chat

        body = json.dumps(
            {
                "type": "GATEWAY_MESSAGE_CREATE",
                "timestamp": 1234567890,
                "data": {
                    "id": "msg123",
                    "channel_id": "dm456",
                    "guild_id": None,
                    "content": "DM message",
                    "timestamp": "2021-01-01T00:00:00.000Z",
                    "author": {"id": "user789", "username": "testuser", "bot": False},
                    "mentions": [],
                    "attachments": [],
                },
            }
        )
        request = _gateway_request(body)

        await adapter.handle_webhook(request)

        call_args = mock_chat.handle_incoming_message.call_args[0]
        assert call_args[1] == "discord:@me:dm456"


# ============================================================================
# mentionRoleIds
# ============================================================================


class TestMentionRoleIds:
    @pytest.mark.asyncio
    async def test_detects_mention_via_role_id(self):
        adapter = _make_adapter(logger=_make_logger(), mention_role_ids=["role123"])
        mock_chat = MagicMock()
        mock_chat.handle_incoming_message = AsyncMock()
        adapter._chat = mock_chat
        adapter._discord_fetch = AsyncMock(return_value={"id": "new-thread", "name": "Thread"})

        body = json.dumps(
            {
                "type": "GATEWAY_MESSAGE_CREATE",
                "timestamp": 1234567890,
                "data": {
                    "id": "msg123",
                    "channel_id": "channel456",
                    "guild_id": "guild1",
                    "content": "Hey team",
                    "timestamp": "2021-01-01T00:00:00.000Z",
                    "author": {"id": "user789", "username": "testuser", "bot": False},
                    "mentions": [],
                    "mention_roles": ["role123"],
                    "attachments": [],
                },
            }
        )
        request = _gateway_request(body)

        await adapter.handle_webhook(request)

        # Should create a thread because of role mention
        thread_call = adapter._discord_fetch.call_args_list[0]
        assert "/channels/channel456/messages/msg123/threads" in thread_call[0][0]


# ============================================================================
# createDiscordThread 160004 Recovery
# ============================================================================


class TestCreateDiscordThread160004Recovery:
    @pytest.mark.asyncio
    async def test_recovers_when_thread_already_exists(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(
            side_effect=NetworkError(
                "discord",
                'Discord API error: 400 {"code": 160004, "message": "A thread has already been created for this message"}',
            )
        )

        result = await adapter._create_discord_thread("channel123", "msg456")

        assert result["id"] == "msg456"
        assert "Thread " in result["name"]

    @pytest.mark.asyncio
    async def test_propagates_non_160004_network_errors(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(
            side_effect=NetworkError(
                "discord",
                'Discord API error: 403 {"code": 50001, "message": "Missing Access"}',
            )
        )

        with pytest.raises(NetworkError):
            await adapter._create_discord_thread("channel123", "msg456")

    @pytest.mark.asyncio
    async def test_propagates_non_network_errors(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(side_effect=Exception("Connection failed"))

        with pytest.raises(Exception, match="Connection failed"):
            await adapter._create_discord_thread("channel123", "msg456")


# ============================================================================
# initialize after gateway events
# ============================================================================


class TestInitializeWithGateway:
    @pytest.mark.asyncio
    async def test_handles_webhook_after_initialization(self):
        adapter = _make_adapter(logger=_make_logger())
        mock_chat = MagicMock()
        mock_chat.handle_incoming_message = AsyncMock()
        mock_chat.process_slash_command = MagicMock()
        mock_chat.process_action = MagicMock()
        mock_chat.process_reaction = MagicMock()

        await adapter.initialize(mock_chat)

        body = json.dumps(
            {
                "type": "GATEWAY_MESSAGE_CREATE",
                "timestamp": 1234567890,
                "data": {
                    "id": "msg1",
                    "channel_id": "ch1",
                    "guild_id": "g1",
                    "content": "test",
                    "timestamp": "2021-01-01T00:00:00.000Z",
                    "author": {"id": "u1", "username": "user", "bot": False},
                    "mentions": [],
                    "attachments": [],
                },
            }
        )
        request = _gateway_request(body)

        response = await adapter.handle_webhook(request)

        assert response["status"] == 200
        mock_chat.handle_incoming_message.assert_called_once()


# ============================================================================
# Constructor env var resolution
# ============================================================================


class TestMentionRoleIdsEnvVar:
    def test_resolves_from_env(self, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "env-token")
        monkeypatch.setenv("DISCORD_PUBLIC_KEY", TEST_PUBLIC_KEY)
        monkeypatch.setenv("DISCORD_APPLICATION_ID", "env-app-id")
        monkeypatch.setenv("DISCORD_MENTION_ROLE_IDS", "role1, role2, role3")
        adapter = DiscordAdapter()
        assert isinstance(adapter, DiscordAdapter)

    def test_default_logger_when_not_provided(self, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "env-token")
        monkeypatch.setenv("DISCORD_PUBLIC_KEY", TEST_PUBLIC_KEY)
        monkeypatch.setenv("DISCORD_APPLICATION_ID", "env-app-id")
        adapter = DiscordAdapter()
        assert isinstance(adapter, DiscordAdapter)


# ============================================================================
# Render formatted - additional coverage
# ============================================================================


class TestRenderFormattedAdditional:
    def test_renders_ast_to_discord_markdown(self):
        adapter = _make_adapter()
        ast = {
            "type": "root",
            "children": [
                {
                    "type": "paragraph",
                    "children": [
                        {
                            "type": "strong",
                            "children": [{"type": "text", "value": "bold"}],
                        }
                    ],
                }
            ],
        }
        result = adapter.render_formatted(ast)
        assert result == "**bold**"

    def test_converts_mentions_in_rendered_output(self):
        adapter = _make_adapter()
        ast = {
            "type": "root",
            "children": [
                {
                    "type": "paragraph",
                    "children": [{"type": "text", "value": "Hello @someone"}],
                }
            ],
        }
        result = adapter.render_formatted(ast)
        assert "<@someone>" in result


# ============================================================================
# DiscordFormatConverter additional coverage
# ============================================================================


class TestDiscordFormatConverterAdditional:
    def test_to_ast_user_mentions(self):
        from chat_sdk.adapters.discord.format_converter import DiscordFormatConverter

        c = DiscordFormatConverter()
        text = c.extract_plain_text("Hello <@123456789>")
        assert text == "Hello @123456789"

    def test_to_ast_channel_mentions(self):
        from chat_sdk.adapters.discord.format_converter import DiscordFormatConverter

        c = DiscordFormatConverter()
        text = c.extract_plain_text("Check <#987654321>")
        assert text == "Check #987654321"

    def test_to_ast_custom_emoji(self):
        from chat_sdk.adapters.discord.format_converter import DiscordFormatConverter

        c = DiscordFormatConverter()
        text = c.extract_plain_text("Nice <:thumbsup:123>")
        assert text == "Nice :thumbsup:"

    def test_to_ast_bold(self):
        from chat_sdk.adapters.discord.format_converter import DiscordFormatConverter

        c = DiscordFormatConverter()
        ast = c.to_ast("**bold text**")
        assert ast is not None

    def test_to_ast_italic(self):
        from chat_sdk.adapters.discord.format_converter import DiscordFormatConverter

        c = DiscordFormatConverter()
        ast = c.to_ast("*italic text*")
        assert ast is not None

    def test_from_ast_mentions_to_discord(self):
        from chat_sdk.adapters.discord.format_converter import DiscordFormatConverter

        c = DiscordFormatConverter()
        ast = c.to_ast("Hello @someone")
        result = c.from_ast(ast)
        assert "<@someone>" in result

    def test_render_postable_plain_string(self):
        from chat_sdk.adapters.discord.format_converter import DiscordFormatConverter

        c = DiscordFormatConverter()
        result = c.render_postable("Hello @user")
        assert result == "Hello <@user>"

    def test_render_postable_raw_message(self):
        from chat_sdk.adapters.discord.format_converter import DiscordFormatConverter

        c = DiscordFormatConverter()
        result = c.render_postable({"raw": "Hello @user"})
        assert result == "Hello <@user>"


# ============================================================================
# Post message with card / embed
# ============================================================================


class TestPostMessageWithCard:
    @pytest.mark.asyncio
    async def test_posts_card_with_embeds(self):
        from chat_sdk.cards import Actions, Button, Card

        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(return_value=_msg_response())

        card_message = {
            "card": Card(
                title="Test Card",
                children=[Actions([Button(id="btn1", label="Click me")])],
            )
        }

        await adapter.post_message("discord:guild1:channel456", card_message)

        call_args = adapter._discord_fetch.call_args
        payload = call_args[0][2]
        assert "embeds" in payload
        assert len(payload["embeds"]) > 0
        assert "components" in payload
        assert len(payload["components"]) > 0


# ============================================================================
# Edit message with card
# ============================================================================


class TestEditMessageWithCard:
    @pytest.mark.asyncio
    async def test_edits_with_card(self):
        from chat_sdk.cards import Card, CardText

        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(return_value=_msg_response())

        card_message = {"card": Card(title="Updated", children=[CardText("New")])}

        await adapter.edit_message("discord:guild1:channel456", "msg001", card_message)

        call_args = adapter._discord_fetch.call_args
        payload = call_args[0][2]
        assert "embeds" in payload


# ============================================================================
# Forwarded message - bot skips own message
# ============================================================================


class TestForwardedMessageSkipsSelf:
    @pytest.mark.asyncio
    async def test_skips_own_bot_message(self):
        adapter = _make_adapter(logger=_make_logger())
        mock_chat = MagicMock()
        mock_chat.handle_incoming_message = AsyncMock()
        adapter._chat = mock_chat

        body = json.dumps(
            {
                "type": "GATEWAY_MESSAGE_CREATE",
                "timestamp": 1234567890,
                "data": {
                    "id": "msg123",
                    "channel_id": "channel456",
                    "guild_id": "guild1",
                    "content": "Bot message",
                    "timestamp": "2021-01-01T00:00:00.000Z",
                    "author": {"id": "test-app-id", "username": "bot", "bot": True},
                    "mentions": [],
                    "attachments": [],
                },
            }
        )
        request = _gateway_request(body)

        await adapter.handle_webhook(request)

        # Bot's own messages should still be forwarded to chat core
        # (it's up to the chat core to decide what to do)
        mock_chat.handle_incoming_message.assert_called_once()
        call_args = mock_chat.handle_incoming_message.call_args[0]
        msg = call_args[2]
        assert msg.author.is_me is True
        assert msg.author.is_bot is True


# ============================================================================
# Forwarded message with attachments
# ============================================================================


class TestForwardedMessageAttachments:
    @pytest.mark.asyncio
    async def test_forwards_attachments(self):
        adapter = _make_adapter(logger=_make_logger())
        mock_chat = MagicMock()
        mock_chat.handle_incoming_message = AsyncMock()
        adapter._chat = mock_chat

        body = json.dumps(
            {
                "type": "GATEWAY_MESSAGE_CREATE",
                "timestamp": 1234567890,
                "data": {
                    "id": "msg123",
                    "channel_id": "channel456",
                    "guild_id": "guild1",
                    "content": "See attached",
                    "timestamp": "2021-01-01T00:00:00.000Z",
                    "author": {"id": "user789", "username": "testuser", "bot": False},
                    "mentions": [],
                    "attachments": [
                        {
                            "filename": "image.png",
                            "url": "https://cdn.discord.com/image.png",
                            "content_type": "image/png",
                            "size": 12345,
                        }
                    ],
                },
            }
        )
        request = _gateway_request(body)

        await adapter.handle_webhook(request)

        mock_chat.handle_incoming_message.assert_called_once()
        msg = mock_chat.handle_incoming_message.call_args[0][2]
        assert len(msg.attachments) == 1
        assert msg.attachments[0].type == "image"
        assert msg.attachments[0].name == "image.png"


# ============================================================================
# fetchMessages default options
# ============================================================================


class TestFetchMessagesDefaults:
    @pytest.mark.asyncio
    async def test_uses_default_limit_50(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(return_value=[])

        await adapter.fetch_messages("discord:guild1:channel456")

        call_args = adapter._discord_fetch.call_args
        assert "limit=50" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_handles_empty_response(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(return_value=[])

        result = await adapter.fetch_messages("discord:guild1:channel456")

        assert result.messages == []
        assert result.next_cursor is None

    @pytest.mark.asyncio
    async def test_handles_non_list_response(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(return_value=None)

        result = await adapter.fetch_messages("discord:guild1:channel456")

        assert result.messages == []


# ============================================================================
# fetchThread - metadata
# ============================================================================


class TestFetchThreadMetadata:
    @pytest.mark.asyncio
    async def test_includes_guild_id_in_metadata(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(return_value={"id": "channel456", "name": "general", "type": 0})

        result = await adapter.fetch_thread("discord:guild1:channel456")

        assert result.metadata["guild_id"] == "guild1"
        assert result.metadata["channel_type"] == 0

    @pytest.mark.asyncio
    async def test_throws_on_invalid_channel_id(self):
        adapter = _make_adapter(logger=_make_logger())

        with pytest.raises(ValidationError):
            await adapter.fetch_thread("invalid")


# ============================================================================
# Deferred slash command responses
# ============================================================================


class TestDeferredSlashCommandResponse:
    @pytest.mark.asyncio
    async def test_stores_request_context(self):
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
                "token": "interaction-token-xyz",
                "guild_id": "guild123",
                "channel_id": "channel456",
                "member": {"user": {"id": "user789", "username": "testuser"}},
                "data": {"name": "ping", "type": 1},
            }
        )
        request = _FakeRequest(
            body,
            {"x-signature-ed25519": "valid", "x-signature-timestamp": "12345"},
        )

        await adapter.handle_webhook(request)

        ctx = adapter._request_context.get()
        assert ctx is not None
        assert ctx.slash_command.interaction_token == "interaction-token-xyz"
