"""Extended tests for the Discord adapter -- message ops, reactions, typing, DMs,
fetch, thread creation, gateway events, error handling.

Ported from the remaining test categories in
packages/adapter-discord/src/index.test.ts (lines ~1100-4037)
and packages/adapter-discord/src/gateway.test.ts.
"""

from __future__ import annotations

import asyncio
import gc
import json
import warnings
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from chat_sdk.adapters.discord.adapter import (
    CHANNEL_TYPE_DM,
    CHANNEL_TYPE_GROUP_DM,
    CHANNEL_TYPE_PUBLIC_THREAD,
    DiscordAdapter,
    DiscordApiError,
)
from chat_sdk.adapters.discord.types import (
    DiscordAdapterConfig,
    DiscordInteractionFlagsContext,
    DiscordInteractionResponseFlag,
    DiscordRequestContext,
    DiscordSlashCommandContext,
    DiscordThreadId,
)
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


# ``GET /channels/thread789`` response: thread789's parent is channel456.
THREAD_789_CHANNEL = {"id": "thread789", "parent_id": "channel456"}


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
        adapter._discord_fetch = AsyncMock(
            side_effect=[THREAD_789_CHANNEL, _msg_response(msg_id="msg002", channel_id="thread789")]
        )

        result = await adapter.post_message("discord:guild1:channel456:thread789", "Thread reply")

        assert result.id == "msg002"
        assert result.thread_id == "discord:guild1:channel456:thread789"
        assert adapter._discord_fetch.call_args_list[0].args == ("/channels/thread789", "GET")
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
        adapter._discord_fetch = AsyncMock(
            side_effect=[THREAD_789_CHANNEL, _msg_response(msg_id="msg002", channel_id="thread789")]
        )

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
        adapter._discord_fetch = AsyncMock(side_effect=[THREAD_789_CHANNEL, None])

        await adapter.delete_message("discord:guild1:channel456:thread789", "msg002")

        assert [c.args for c in adapter._discord_fetch.call_args_list] == [
            ("/channels/thread789", "GET"),
            ("/channels/thread789/messages/msg002", "DELETE"),
        ]


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
        adapter._discord_fetch = AsyncMock(side_effect=[THREAD_789_CHANNEL, None])

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
        adapter._discord_fetch = AsyncMock(side_effect=[THREAD_789_CHANNEL, None])

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
        adapter._discord_fetch = AsyncMock(side_effect=[THREAD_789_CHANNEL, None])

        await adapter.start_typing("discord:guild1:channel456:thread789")

        assert [c.args for c in adapter._discord_fetch.call_args_list] == [
            ("/channels/thread789", "GET"),
            ("/channels/thread789/typing", "POST"),
        ]


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
        adapter._discord_fetch = AsyncMock(side_effect=[THREAD_789_CHANNEL, raw_messages])

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

        # When results match the limit, next_cursor is the last message id (backward direction)
        assert result.next_cursor == "msg9"

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
# Forwarded gateway interactions (gateway-only mode, vercel/chat#490)
# ============================================================================


class TestForwardedGatewayInteractions:
    """Port of the "legacy gateway interactions" describe block.

    Discord sends interactions through either the Gateway or an
    Interactions Endpoint URL, not both. In gateway-only deployments the
    forwarder relays the raw INTERACTION_CREATE payload; the adapter must
    defer it via the interaction callback REST endpoint (the wire call
    discord.js makes for deferReply/deferUpdate) and route through the
    existing slash-command / action handler paths.
    """

    def _slash_interaction(self, **overrides):
        interaction = {
            "id": "interaction123",
            "application_id": "test-app-id",
            "token": "interaction-token",
            "type": 2,  # APPLICATION_COMMAND
            "version": 1,
            "guild_id": "guild123",
            "channel_id": "channel456",
            "channel": {"id": "channel456", "type": 0},
            "user": {
                "id": "user789",
                "username": "testuser",
                "discriminator": "0001",
                "global_name": "Test User",
                "bot": False,
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
        interaction.update(overrides)
        return interaction

    def _component_interaction(self, **overrides):
        interaction = {
            "id": "interaction123",
            "application_id": "test-app-id",
            "token": "interaction-token",
            "type": 3,  # MESSAGE_COMPONENT
            "version": 1,
            "guild_id": "guild123",
            "channel_id": "channel456",
            "channel": {"id": "channel456", "type": 0},
            "user": {
                "id": "user789",
                "username": "testuser",
                "discriminator": "0001",
                "global_name": "Test User",
                "bot": False,
            },
            "data": {"custom_id": "approve_btn", "component_type": 2},
            "message": {"id": "message123"},
        }
        interaction.update(overrides)
        return interaction

    def _forwarded(self, interaction) -> str:
        return json.dumps(
            {
                "type": "GATEWAY_INTERACTION_CREATE",
                "timestamp": 1234567890,
                "data": interaction,
            }
        )

    @pytest.mark.asyncio
    async def test_handles_slash_command_interactions_from_the_gateway(self):
        adapter = _make_adapter(logger=_make_logger())
        mock_chat = MagicMock()
        mock_chat.process_slash_command = MagicMock()
        adapter._chat = mock_chat
        adapter._discord_fetch = AsyncMock(return_value=None)

        response = await adapter.handle_webhook(_gateway_request(self._forwarded(self._slash_interaction())))

        assert response["status"] == 200
        # deferReply: explicit callback REST call (type 5)
        adapter._discord_fetch.assert_awaited_once_with(
            "/interactions/interaction123/interaction-token/callback",
            "POST",
            {"type": 5},
        )
        mock_chat.process_slash_command.assert_called_once()
        event = mock_chat.process_slash_command.call_args[0][0]
        assert event.command == "/test"
        assert event.text == "status true"
        assert event.channel_id == "discord:guild123:channel456"
        assert event.user.user_id == "user789"
        assert event.user.user_name == "testuser"
        assert event.user.full_name == "Test User"

    @pytest.mark.asyncio
    async def test_handles_component_interactions_from_the_gateway(self):
        adapter = _make_adapter(logger=_make_logger())
        mock_chat = MagicMock()
        mock_chat.process_action = MagicMock()
        adapter._chat = mock_chat
        adapter._discord_fetch = AsyncMock(return_value=None)

        response = await adapter.handle_webhook(_gateway_request(self._forwarded(self._component_interaction())))

        assert response["status"] == 200
        # deferUpdate: explicit callback REST call (type 6)
        adapter._discord_fetch.assert_awaited_once_with(
            "/interactions/interaction123/interaction-token/callback",
            "POST",
            {"type": 6},
        )
        mock_chat.process_action.assert_called_once()
        event = mock_chat.process_action.call_args[0][0]
        assert event.action_id == "approve_btn"
        assert event.value == "approve_btn"
        assert event.message_id == "message123"
        assert event.thread_id == "discord:guild123:channel456"

    @pytest.mark.asyncio
    async def test_slash_command_defer_failure_skips_handler(self):
        """If the deferral REST call fails the handler must not run --
        matches upstream where a deferReply rejection is caught and logged
        before the normalize+handle step."""
        logger = _make_logger()
        adapter = _make_adapter(logger=logger)
        mock_chat = MagicMock()
        mock_chat.process_slash_command = MagicMock()
        adapter._chat = mock_chat
        adapter._discord_fetch = AsyncMock(side_effect=NetworkError("discord", "boom"))

        response = await adapter.handle_webhook(_gateway_request(self._forwarded(self._slash_interaction())))

        # Forwarded-event responses stay 200; the failure is logged.
        assert response["status"] == 200
        mock_chat.process_slash_command.assert_not_called()
        error_messages = [c.args[0] for c in logger.error.call_args_list]
        assert "Error handling Gateway interaction" in error_messages

    @pytest.mark.asyncio
    async def test_unhandled_interaction_type_is_ignored_without_defer(self):
        """PING/autocomplete-style interactions are not deferred or routed."""
        adapter = _make_adapter(logger=_make_logger())
        mock_chat = MagicMock()
        mock_chat.process_slash_command = MagicMock()
        mock_chat.process_action = MagicMock()
        adapter._chat = mock_chat
        adapter._discord_fetch = AsyncMock(return_value=None)

        response = await adapter.handle_webhook(_gateway_request(self._forwarded(self._slash_interaction(type=4))))

        assert response["status"] == 200
        adapter._discord_fetch.assert_not_awaited()
        mock_chat.process_slash_command.assert_not_called()
        mock_chat.process_action.assert_not_called()

    @pytest.mark.asyncio
    async def test_interaction_missing_token_is_not_deferred_or_routed(self):
        """A malformed forward without id/token must not produce a garbage
        callback URL or dispatch a handler."""
        adapter = _make_adapter(logger=_make_logger())
        mock_chat = MagicMock()
        mock_chat.process_action = MagicMock()
        adapter._chat = mock_chat
        adapter._discord_fetch = AsyncMock(return_value=None)

        interaction = self._component_interaction()
        del interaction["token"]
        response = await adapter.handle_webhook(_gateway_request(self._forwarded(interaction)))

        assert response["status"] == 200
        adapter._discord_fetch.assert_not_awaited()
        mock_chat.process_action.assert_not_called()

    @pytest.mark.asyncio
    async def test_gateway_slash_command_supports_deferred_response_flow(self):
        """The gateway-deferred slash interaction resolves like an HTTP one:
        the handler's first post_message PATCHes the @original webhook
        message using the interaction token."""
        adapter = _make_adapter(logger=_make_logger())
        mock_chat = MagicMock()

        def run_handler(event, options=None):
            # Simulate Chat dispatching a handler that replies immediately.
            import asyncio as _asyncio

            task = _asyncio.get_running_loop().create_task(adapter.post_message(event.channel_id, "reply from handler"))
            run_handler.task = task

        mock_chat.process_slash_command = MagicMock(side_effect=run_handler)
        adapter._chat = mock_chat
        adapter._discord_fetch = AsyncMock(return_value={"id": "reply-msg-1"})

        await adapter.handle_webhook(_gateway_request(self._forwarded(self._slash_interaction())))
        await run_handler.task

        paths = [c.args[0] for c in adapter._discord_fetch.await_args_list]
        # First the deferral, then the @original PATCH (not a channel POST).
        assert paths[0] == "/interactions/interaction123/interaction-token/callback"
        assert paths[1] == "/webhooks/test-app-id/interaction-token/messages/@original"
        patch_call = adapter._discord_fetch.await_args_list[1]
        assert patch_call.args[1] == "PATCH"


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
# Forwarded message -- tri-state is_mention (upstream vercel/chat#946)
# ============================================================================


def _forwarded_thread_message(content: str, **extra) -> str:
    return json.dumps(
        {
            "type": "GATEWAY_MESSAGE_CREATE",
            "timestamp": 1234567890,
            "data": {
                "id": "msg123",
                "channel_id": "thread789",
                "guild_id": "guild1",
                "content": content,
                "timestamp": "2021-01-01T00:00:00.000Z",
                "author": {"id": "user789", "username": "testuser", "bot": False},
                "mentions": [],
                "attachments": [],
                "thread": {"id": "thread789", "parent_id": "channel456"},
                **extra,
            },
        }
    )


class TestForwardedMessageMentionFlag:
    # Mirrors upstream "keeps allowlisted forwarded messages in their Discord
    # thread" (``isMention: isMentioned || undefined``): an unmentioned
    # forwarded message reports no detection (None), never a definitive False.
    @pytest.mark.asyncio
    async def test_unmentioned_forwarded_message_leaves_is_mention_unset(self):
        adapter = _make_adapter(logger=_make_logger())
        mock_chat = MagicMock()
        mock_chat.handle_incoming_message = AsyncMock()
        adapter._chat = mock_chat

        await adapter.handle_webhook(_gateway_request(_forwarded_thread_message("No mention needed")))

        message = mock_chat.handle_incoming_message.await_args.args[2]
        assert message.is_mention is None

    @pytest.mark.asyncio
    async def test_mentioned_forwarded_message_reports_definitive_mention(self):
        adapter = _make_adapter(logger=_make_logger())
        mock_chat = MagicMock()
        mock_chat.handle_incoming_message = AsyncMock()
        adapter._chat = mock_chat

        await adapter.handle_webhook(
            _gateway_request(
                _forwarded_thread_message("<@test-app-id> hi", mentions=[{"id": "test-app-id", "username": "bot"}])
            )
        )

        message = mock_chat.handle_incoming_message.await_args.args[2]
        assert message.is_mention is True

    # End to end through a real Chat: a literal ``@botname`` with no Discord
    # mention metadata still routes to ``on_mention`` via text detection.
    @pytest.mark.asyncio
    async def test_literal_botname_text_routes_to_on_mention(self):
        from chat_sdk.chat import Chat
        from chat_sdk.testing import MockLogger, create_mock_state
        from chat_sdk.types import ChatConfig

        adapter = _make_adapter(logger=_make_logger(), user_name="mybot")
        chat = Chat(
            ChatConfig(user_name="mybot", adapters={"discord": adapter}, state=create_mock_state(), logger=MockLogger())
        )
        mention_handler = AsyncMock(return_value=None)
        chat.on_mention(mention_handler)

        await chat.webhooks["discord"](_gateway_request(_forwarded_thread_message("hey @mybot can you help")))

        mention_handler.assert_awaited_once()
        thread, message = mention_handler.await_args.args[:2]
        assert thread.id == "discord:guild1:channel456:thread789"
        assert message.is_mention is True


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
        body = '{"code": 160004, "message": "A thread has already been created for this message"}'
        adapter._discord_fetch = AsyncMock(
            side_effect=NetworkError(
                "discord",
                f"Discord API error: 400 {body}",
                DiscordApiError(400, body),
            )
        )

        result = await adapter._create_discord_thread("channel123", "msg456")

        assert result["id"] == "msg456"
        assert "Thread " in result["name"]

    @pytest.mark.asyncio
    async def test_propagates_non_160004_network_errors(self):
        adapter = _make_adapter(logger=_make_logger())
        body = '{"code": 50001, "message": "Missing Access"}'
        adapter._discord_fetch = AsyncMock(
            side_effect=NetworkError("discord", f"Discord API error: 403 {body}", DiscordApiError(403, body))
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
# (Duplicate DiscordFormatConverter tests removed -- covered by
#  test_discord_format.py)
# ============================================================================


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
# Card dedup: card messages must not also include the fallback text content
# (port of vercel/chat#256 / chat@4.27.0)
# ============================================================================


class TestCardContentDedup:
    """Discord renders both ``content`` and embed cards; sending both
    produces duplicate text. ``post_message`` must omit ``content`` when a
    card is present, and ``edit_message`` must explicitly clear it (PATCH
    keeps omitted fields).

    What to fix if this fails: in ``adapter.py`` ``post_message`` and
    ``edit_message``, the card branch must NOT call
    ``card_to_fallback_text``. ``edit_message`` must set
    ``payload["content"] = ""`` so leftover text from a previous edit
    doesn't render alongside the new card.
    """

    @pytest.mark.asyncio
    async def test_post_message_with_card_omits_content(self):
        from chat_sdk.cards import Card, CardText

        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(return_value=_msg_response())

        # Card with title + body — fallback text would be "**Title**\nBody".
        card_message = {
            "card": Card(title="Title", children=[CardText("Body")]),
        }

        await adapter.post_message("discord:guild1:channel456", card_message)

        payload = adapter._discord_fetch.call_args[0][2]
        # The omit-content contract for card posts: ``content`` must be
        # absent (or explicitly ``None``) -- NOT an empty string. Sending
        # ``content=""`` on a new post would still let a regression slip
        # through where the duplicate-text bug is "fixed" by sending
        # empty content instead of omitting the key, and Discord's
        # behavior could differ for future clients/embed types.
        assert "content" not in payload or payload["content"] is None
        assert "embeds" in payload and len(payload["embeds"]) > 0

    @pytest.mark.asyncio
    async def test_edit_message_with_card_clears_content_explicitly(self):
        from chat_sdk.cards import Card, CardText

        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(return_value=_msg_response())

        # First post regular text, then edit to a card. The edit must
        # explicitly send ``content=""`` so the original text disappears.
        card_message = {
            "card": Card(title="Updated", children=[CardText("New body")]),
        }
        await adapter.edit_message("discord:guild1:channel456", "msg001", card_message)

        payload = adapter._discord_fetch.call_args[0][2]
        # Contract: the key MUST be present (Discord PATCH preserves
        # omitted fields, so missing key would leave the old content).
        assert "content" in payload, "edit_message with card must explicitly set content"
        assert payload["content"] == "", (
            "edit_message with card must clear content to empty string, not include card fallback text"
        )

    @pytest.mark.asyncio
    async def test_post_message_with_card_no_text_blocks_still_omits_content(self):
        # Adversarial: card with NO text content at all — nothing to
        # duplicate, but the omit-content invariant should still hold.
        from chat_sdk.cards import Actions, Button, Card

        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(return_value=_msg_response())

        card_message = {
            "card": Card(
                title="",
                children=[Actions([Button(id="ok", label="OK")])],
            )
        }
        await adapter.post_message("discord:guild1:channel456", card_message)

        payload = adapter._discord_fetch.call_args[0][2]
        # Same omit-content contract: ``content`` must be absent (or
        # explicitly ``None``), never an empty string.
        assert "content" not in payload or payload["content"] is None


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

        seen: list[Any] = []
        mock_chat = MagicMock()
        # Chat copies the context into the handler task when
        # process_slash_command creates it, so capture what it sees then.
        mock_chat.process_slash_command = MagicMock(side_effect=lambda *_: seen.append(adapter._request_context.get()))
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

        assert len(seen) == 1
        assert seen[0].slash_command.interaction_token == "interaction-token-xyz"
        assert seen[0].slash_command.initial_response_flags is None
        # Scoped like upstream ``requestContext.run``: nothing leaks into the
        # caller's context once the handler task has been created.
        assert adapter._request_context.get() is None


# ============================================================================
# 4.41 sync (#230): ephemeral slash responses, select values, channel
# allowlist, global mentions opt-in, thread renames.
# ============================================================================


def _signed_request(payload: dict[str, Any]) -> _FakeRequest:
    return _FakeRequest(
        json.dumps(payload),
        {"x-signature-ed25519": "valid", "x-signature-timestamp": "12345"},
    )


def _slash_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": 2,
        "id": "interaction123",
        "application_id": "test-app-id",
        "token": "interaction-token",
        "version": 1,
        "guild_id": "guild123",
        "channel_id": "channel456",
        "member": {
            "user": {"id": "user789", "username": "testuser", "discriminator": "0001"},
            "roles": ["role123"],
            "joined_at": "2021-01-01T00:00:00.000Z",
        },
        "data": {"id": "cmd123", "name": "test", "type": 1},
    }
    payload.update(overrides)
    return payload


def _forwarded_message(**data: Any) -> _FakeRequest:
    message: dict[str, Any] = {
        "id": "msg123",
        "channel_id": "channel456",
        "guild_id": "guild1",
        "content": "No mention needed",
        "timestamp": "2021-01-01T00:00:00.000Z",
        "author": {"id": "user789", "username": "testuser", "bot": False},
        "mentions": [],
        "attachments": [],
    }
    message.update(data)
    return _gateway_request(json.dumps({"type": "GATEWAY_MESSAGE_CREATE", "timestamp": 1234567890, "data": message}))


def _incoming_chat() -> MagicMock:
    chat = MagicMock()
    chat.handle_incoming_message = AsyncMock()
    return chat


class TestInteractionFlags:
    @pytest.mark.asyncio
    async def test_sets_initial_deferred_slash_command_interaction_flags_from_config(self):
        interaction_flags = MagicMock(return_value=DiscordInteractionResponseFlag.EPHEMERAL)
        adapter = _make_adapter(logger=_make_logger(), interaction_flags=interaction_flags)
        adapter._verify_signature = AsyncMock(return_value=True)
        adapter._chat = MagicMock()

        response = await adapter.handle_webhook(_signed_request(_slash_payload()))

        assert response["status"] == 200
        assert json.loads(response["body"]) == {"type": 5, "data": {"flags": 64}}
        interaction_flags.assert_called_once()
        context = interaction_flags.call_args.args[0]
        assert isinstance(context, DiscordInteractionFlagsContext)
        assert context.channel_id == "discord:guild123:channel456"
        assert context.command == "/test"
        assert context.text == ""
        assert context.interaction["id"] == "interaction123"
        assert context.interaction["member"]["roles"] == ["role123"]
        assert context.user["id"] == "user789"
        slash = adapter._chat.process_slash_command.call_args.args[0]
        assert slash.channel_id == "discord:guild123:channel456"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("returned", "expected"), [(None, {"type": 5}), (0, {"type": 5, "data": {"flags": 0}})])
    async def test_flags_are_sent_unless_the_callback_returns_none(self, returned, expected):
        # ``0`` is a real flag value (upstream ``flags === undefined`` check).
        adapter = _make_adapter(logger=_make_logger(), interaction_flags=lambda _ctx: returned)
        adapter._verify_signature = AsyncMock(return_value=True)
        adapter._chat = MagicMock()

        response = await adapter.handle_webhook(_signed_request(_slash_payload()))

        assert json.loads(response["body"]) == expected

    @pytest.mark.asyncio
    async def test_raising_flags_callback_still_acks_without_flags(self):
        # Divergence from upstream (which lets it throw): log, ACK, dispatch.
        logger = _make_logger()

        def boom(_ctx: DiscordInteractionFlagsContext) -> int:
            raise RuntimeError("bad callback")

        adapter = _make_adapter(logger=logger, interaction_flags=boom)
        adapter._verify_signature = AsyncMock(return_value=True)
        adapter._chat = MagicMock()

        response = await adapter.handle_webhook(_signed_request(_slash_payload()))

        assert response["status"] == 200
        assert json.loads(response["body"]) == {"type": 5}
        adapter._chat.process_slash_command.assert_called_once()
        slash = adapter._chat.process_slash_command.call_args.args[0]
        assert slash.command == "/test"
        assert [c.args[0] for c in logger.error.call_args_list] == [
            "Discord interaction_flags callback failed; deferring without flags"
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["async", "bool"])
    async def test_async_or_non_int_flags_callback_still_acks_without_flags(self, kind):
        # Same divergence: an ``async def`` callback's coroutine (or ``True``)
        # is not a flag value. The coroutine is closed, never left un-awaited.
        logger = _make_logger()
        awaited: list[bool] = []

        async def async_flags(_ctx: DiscordInteractionFlagsContext) -> int:
            awaited.append(True)
            return DiscordInteractionResponseFlag.EPHEMERAL

        callback: Any = async_flags if kind == "async" else (lambda _ctx: True)
        adapter = _make_adapter(logger=logger, interaction_flags=callback)
        adapter._verify_signature = AsyncMock(return_value=True)
        seen: list[Any] = []
        adapter._chat = MagicMock()
        adapter._chat.process_slash_command = MagicMock(
            side_effect=lambda *_: seen.append(adapter._request_context.get())
        )

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            response = await adapter.handle_webhook(_signed_request(_slash_payload()))
            gc.collect()

        assert response["status"] == 200
        assert json.loads(response["body"]) == {"type": 5}
        assert seen[0].slash_command.initial_response_flags is None
        assert awaited == []
        assert [w for w in caught if "never awaited" in str(w.message)] == []
        assert [c.args[0] for c in logger.error.call_args_list] == [
            "Discord interaction_flags callback failed; deferring without flags"
        ]

    @pytest.mark.asyncio
    async def test_sets_gateway_deferred_slash_command_interaction_flags_from_config(self):
        interaction_flags = MagicMock(return_value=DiscordInteractionResponseFlag.EPHEMERAL)
        adapter = _make_adapter(logger=_make_logger(), interaction_flags=interaction_flags)
        seen: list[Any] = []
        adapter._chat = MagicMock()
        adapter._chat.process_slash_command = MagicMock(
            side_effect=lambda *_: seen.append(adapter._request_context.get())
        )
        adapter._discord_fetch = AsyncMock(return_value=None)
        interaction = _slash_payload(
            channel={"id": "channel456", "type": 0},
            data={"name": "test", "type": 1, "options": [{"name": "topic", "type": 3, "value": "status"}]},
        )

        await adapter.handle_webhook(
            _gateway_request(json.dumps({"type": "GATEWAY_INTERACTION_CREATE", "timestamp": 1, "data": interaction}))
        )

        adapter._discord_fetch.assert_awaited_once_with(
            "/interactions/interaction123/interaction-token/callback",
            "POST",
            {"type": 5, "data": {"flags": 64}},
        )
        context = interaction_flags.call_args.args[0]
        assert (context.channel_id, context.command, context.text, context.user["id"]) == (
            "discord:guild123:channel456",
            "/test",
            "status",
            "user789",
        )
        slash = adapter._chat.process_slash_command.call_args.args[0]
        assert slash.text == "status"
        # Stored for the handler task, so follow-ups stay ephemeral too.
        assert seen[0].slash_command.initial_response_flags == 64

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("returned", "expected"), [(None, {"type": 5}), (0, {"type": 5, "data": {"flags": 0}})])
    async def test_gateway_flags_are_sent_unless_the_callback_returns_none(self, returned, expected):
        adapter = _make_adapter(logger=_make_logger(), interaction_flags=lambda _ctx: returned)
        adapter._chat = MagicMock()
        adapter._discord_fetch = AsyncMock(return_value=None)
        envelope = {"type": "GATEWAY_INTERACTION_CREATE", "timestamp": 1, "data": _slash_payload()}

        await adapter.handle_webhook(_gateway_request(json.dumps(envelope)))

        adapter._discord_fetch.assert_awaited_once_with(
            "/interactions/interaction123/interaction-token/callback", "POST", expected
        )

    @pytest.mark.asyncio
    async def test_ephemeral_flag_sticks_to_the_deferred_edit_and_every_follow_up(self):
        adapter = _make_adapter(
            logger=_make_logger(), interaction_flags=lambda _ctx: DiscordInteractionResponseFlag.EPHEMERAL
        )
        adapter._verify_signature = AsyncMock(return_value=True)
        adapter._discord_fetch = AsyncMock(side_effect=[{"id": "original"}, {"id": "followup1"}])
        tasks: list[asyncio.Task[None]] = []

        def run_handler(event: Any, options: Any = None) -> None:
            async def handler() -> None:
                await adapter.post_message(event.channel_id, "first")
                await adapter.post_message(event.channel_id, "second")

            tasks.append(asyncio.get_running_loop().create_task(handler()))

        adapter._chat = MagicMock()
        adapter._chat.process_slash_command = MagicMock(side_effect=run_handler)

        await adapter.handle_webhook(_signed_request(_slash_payload()))
        await tasks[0]

        assert [c.args for c in adapter._discord_fetch.await_args_list] == [
            (
                "/webhooks/test-app-id/interaction-token/messages/@original",
                "PATCH",
                {"content": "first", "flags": 64},
            ),
            ("/webhooks/test-app-id/interaction-token?wait=true", "POST", {"content": "second", "flags": 64}),
        ]

    @pytest.mark.asyncio
    async def test_keeps_slash_command_follow_up_responses_ephemeral(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(return_value={"id": "followup123"})
        slash = DiscordSlashCommandContext(
            channel_id="discord:guild123:channel456",
            initial_response_flags=DiscordInteractionResponseFlag.EPHEMERAL,
            initial_response_sent=True,
            interaction_token="interaction-token",
        )

        result = await adapter._post_slash_command_response(
            slash, "discord:guild123:channel456", {"content": "Private follow-up"}, []
        )

        adapter._discord_fetch.assert_awaited_once_with(
            "/webhooks/test-app-id/interaction-token?wait=true",
            "POST",
            {"content": "Private follow-up", "flags": 64},
            files=None,
        )
        assert result.id == "followup123"

    @pytest.mark.asyncio
    async def test_payload_flags_are_ored_into_the_initial_flags(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(return_value={"id": "followup123"})
        slash = DiscordSlashCommandContext(
            channel_id="discord:guild123:channel456",
            initial_response_flags=DiscordInteractionResponseFlag.EPHEMERAL,
            initial_response_sent=True,
            interaction_token="interaction-token",
        )

        await adapter._post_slash_command_response(
            slash, "discord:guild123:channel456", {"content": "x", "flags": 4}, []
        )

        assert adapter._discord_fetch.await_args.args[2] == {"content": "x", "flags": 68}

    @pytest.mark.asyncio
    async def test_concurrent_posts_edit_original_once_then_follow_up(self):
        # The first-response flag is set before the PATCH is awaited
        # (upstream index.ts:1483-1486), so a concurrent post follows up.
        adapter = _make_adapter(logger=_make_logger())

        async def fetch(path: str, method: str, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            await asyncio.sleep(0)
            return {"id": method}

        adapter._discord_fetch = AsyncMock(side_effect=fetch)
        slash = DiscordSlashCommandContext(
            channel_id="discord:guild123:channel456",
            initial_response_sent=False,
            interaction_token="interaction-token",
        )
        adapter._request_context.set(DiscordRequestContext(slash_command=slash))

        await asyncio.gather(
            adapter.post_message("discord:guild123:channel456", "a"),
            adapter.post_message("discord:guild123:channel456", "b"),
        )

        assert [c.args[:2] for c in adapter._discord_fetch.await_args_list] == [
            ("/webhooks/test-app-id/interaction-token/messages/@original", "PATCH"),
            ("/webhooks/test-app-id/interaction-token?wait=true", "POST"),
        ]

    @pytest.mark.asyncio
    async def test_unflagged_follow_up_goes_to_the_interaction_webhook_without_flags(self):
        # Without ``interaction_flags`` the payload is unchanged; a second
        # post is still an interaction follow-up, not a channel message.
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(return_value={"id": "followup123"})
        slash = DiscordSlashCommandContext(
            channel_id="discord:guild123:channel456",
            initial_response_sent=True,
            interaction_token="interaction-token",
        )
        adapter._request_context.set(DiscordRequestContext(slash_command=slash))

        await adapter.post_message("discord:guild123:channel456", "public follow-up")

        adapter._discord_fetch.assert_awaited_once_with(
            "/webhooks/test-app-id/interaction-token?wait=true",
            "POST",
            {"content": "public follow-up"},
            files=None,
        )


class TestSelectMenuValues:
    def _select_interaction(self, values: Any) -> dict[str, Any]:
        return {
            "type": 3,
            "id": "interaction123",
            "token": "interaction-token",
            "guild_id": "guild123",
            "channel_id": "channel456",
            "channel": {"id": "channel456", "type": 0},
            "member": {"user": {"id": "user789", "username": "testuser"}},
            "message": {"id": "message123", "channel_id": "channel456"},
            "data": {"custom_id": "priority", "component_type": 3, "values": values},
        }

    @pytest.mark.parametrize(
        ("values", "expected"),
        [
            (["high"], "high"),
            # An empty-string choice is a real value.
            ([""], ""),
            # No choice falls back to the custom_id's action id.
            ([], "priority"),
        ],
        ids=["selected", "empty_string", "no_values"],
    )
    def test_uses_selected_values_from_select_interactions(self, values, expected):
        adapter = _make_adapter(logger=_make_logger())
        adapter._chat = MagicMock()

        adapter._handle_component_interaction(self._select_interaction(values))

        event = adapter._chat.process_action.call_args.args[0]
        assert event.action_id == "priority"
        assert event.value == expected


class TestRespondToChannelIds:
    def test_should_resolve_respond_to_channel_ids_from_discord_respond_to_channel_ids_env_var(self, monkeypatch):
        monkeypatch.setenv("DISCORD_RESPOND_TO_CHANNEL_IDS", "channel1, channel2,")
        adapter = _make_adapter()
        assert adapter._respond_to_channel_ids == ["channel1", "channel2"]

    def test_explicit_empty_list_beats_the_env_var(self, monkeypatch):
        monkeypatch.setenv("DISCORD_RESPOND_TO_CHANNEL_IDS", "channel1")
        adapter = _make_adapter(respond_to_channel_ids=[])
        assert adapter._respond_to_channel_ids == []

    @pytest.mark.asyncio
    async def test_keeps_allowlisted_forwarded_messages_in_their_discord_thread(self):
        adapter = _make_adapter(logger=_make_logger(), respond_to_channel_ids=["channel456"])
        adapter._discord_fetch = AsyncMock(return_value={"id": "thread789", "name": "Thread"})
        chat = _incoming_chat()
        adapter._chat = chat
        in_thread = {"channel_id": "thread789", "thread": {"id": "thread789", "parent_id": "channel456"}}

        await adapter.handle_webhook(_forwarded_message(id="msg123"))
        await adapter.handle_webhook(_forwarded_message(id="msg456", **in_thread))
        await adapter.handle_webhook(
            _forwarded_message(
                id="msg789", author={"id": "other-bot", "username": "other-bot", "bot": True}, **in_thread
            )
        )

        # Only the top-level message creates a thread.
        adapter._discord_fetch.assert_awaited_once()
        assert adapter._discord_fetch.await_args.args[:2] == ("/channels/channel456/messages/msg123/threads", "POST")
        calls = chat.handle_incoming_message.call_args_list
        assert [(c.args[1], c.args[2].is_mention) for c in calls] == [
            ("discord:guild1:channel456:thread789", True),
            ("discord:guild1:channel456:thread789", True),
            # Bot authors never count as allowlisted.
            ("discord:guild1:channel456:thread789", None),
        ]

    @pytest.mark.asyncio
    async def test_unlisted_channel_is_not_a_mention(self):
        adapter = _make_adapter(logger=_make_logger(), respond_to_channel_ids=["other"])
        adapter._discord_fetch = AsyncMock()
        chat = _incoming_chat()
        adapter._chat = chat

        await adapter.handle_webhook(_forwarded_message())

        adapter._discord_fetch.assert_not_awaited()
        assert chat.handle_incoming_message.call_args.args[2].is_mention is None


class TestRespondToGlobalMentionsHandling:
    @pytest.mark.asyncio
    async def test_ignores_everyone_in_forwarded_gateway_messages_by_default(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(return_value={"id": "new-thread", "name": "Thread"})
        chat = _incoming_chat()
        adapter._chat = chat

        await adapter.handle_webhook(_forwarded_message(content="@everyone big announcement", mention_everyone=True))

        adapter._discord_fetch.assert_not_awaited()
        assert chat.handle_incoming_message.call_args.args[2].is_mention is None

    @pytest.mark.asyncio
    async def test_treats_everyone_in_forwarded_gateway_messages_as_a_mention_when_respond_to_global_mentions_is_true(
        self,
    ):
        adapter = _make_adapter(logger=_make_logger(), respond_to_global_mentions=True)
        adapter._discord_fetch = AsyncMock(return_value={"id": "new-thread", "name": "Thread"})
        chat = _incoming_chat()
        adapter._chat = chat

        await adapter.handle_webhook(_forwarded_message(content="@everyone big announcement", mention_everyone=True))

        assert adapter._discord_fetch.await_args.args[:2] == ("/channels/channel456/messages/msg123/threads", "POST")
        assert chat.handle_incoming_message.call_args.args[1] == "discord:guild1:channel456:new-thread"
        assert chat.handle_incoming_message.call_args.args[2].is_mention is True

    @pytest.mark.asyncio
    async def test_still_detects_direct_user_mentions_when_everyone_is_ignored(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(return_value={"id": "new-thread", "name": "Thread"})
        chat = _incoming_chat()
        adapter._chat = chat

        await adapter.handle_webhook(
            _forwarded_message(
                content="@everyone <@test-app-id>",
                mention_everyone=True,
                mentions=[{"id": "test-app-id", "username": "bot"}],
            )
        )

        assert chat.handle_incoming_message.call_args.args[2].is_mention is True

    @pytest.mark.asyncio
    async def test_non_boolean_mention_everyone_is_ignored_even_when_opted_in(self):
        # Strict ``=== true``: a truthy non-bool from a forwarder is not a ping.
        adapter = _make_adapter(logger=_make_logger(), respond_to_global_mentions=True)
        adapter._discord_fetch = AsyncMock()
        chat = _incoming_chat()
        adapter._chat = chat

        await adapter.handle_webhook(_forwarded_message(mention_everyone="true"))

        adapter._discord_fetch.assert_not_awaited()
        assert chat.handle_incoming_message.call_args.args[2].is_mention is None

    @pytest.mark.asyncio
    async def test_forwarder_supplied_is_mention_is_ignored(self):
        # Upstream 61b98fca: the mention comes from the dispatch payload only.
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock()
        chat = _incoming_chat()
        adapter._chat = chat

        await adapter.handle_webhook(_forwarded_message(is_mention=True))

        adapter._discord_fetch.assert_not_awaited()
        assert chat.handle_incoming_message.call_args.args[2].is_mention is None


class TestSetThreadTitle:
    @pytest.mark.asyncio
    async def test_renames_discord_thread_channels(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(side_effect=[{"id": "thread789", "parent_id": "channel456"}, None])

        await adapter.set_thread_title("discord:guild1:channel456:thread789", "New thread title")
        await adapter.set_thread_title("discord:guild1:channel456", "Ignored channel title")

        assert [c.args for c in adapter._discord_fetch.await_args_list] == [
            ("/channels/thread789", "GET"),
            ("/channels/thread789", "PATCH", {"name": "New thread title"}),
        ]
