"""Final-pass tests for the Discord adapter -- channel_id_from_thread_id,
normalizeDiscordEmoji edge cases, fetchMessages cursor directions,
fetchChannelInfo, stream, disconnect, and protocol compliance.

These cover gaps remaining after test_discord_adapter.py and test_discord_extended.py.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from chat_sdk.adapters.discord.adapter import (
    CHANNEL_TYPE_DM,
    CHANNEL_TYPE_GROUP_DM,
    CHANNEL_TYPE_PUBLIC_THREAD,
    DiscordAdapter,
)
from chat_sdk.adapters.discord.format_converter import DiscordFormatConverter
from chat_sdk.adapters.discord.types import DiscordAdapterConfig, DiscordThreadId
from chat_sdk.shared.errors import ValidationError
from chat_sdk.types import FetchOptions

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


# ============================================================================
# channel_id_from_thread_id
# ============================================================================


class TestChannelIdFromThreadIdMethod:
    """Tests for the channel_id_from_thread_id method itself (not manual decode/encode)."""

    def test_strips_thread_part(self):
        adapter = _make_adapter()
        result = adapter.channel_id_from_thread_id("discord:guild1:channel456:thread789")
        assert result == "discord:guild1:channel456"

    def test_returns_same_for_channel_level(self):
        adapter = _make_adapter()
        result = adapter.channel_id_from_thread_id("discord:guild1:channel456")
        assert result == "discord:guild1:channel456"

    def test_handles_dm_channel(self):
        adapter = _make_adapter()
        result = adapter.channel_id_from_thread_id("discord:@me:dm123")
        assert result == "discord:@me:dm123"

    def test_handles_dm_with_thread(self):
        adapter = _make_adapter()
        result = adapter.channel_id_from_thread_id("discord:@me:dm123:thread456")
        assert result == "discord:@me:dm123"

    def test_invalid_thread_id_raises(self):
        adapter = _make_adapter()
        with pytest.raises(ValidationError):
            adapter.channel_id_from_thread_id("invalid")

    def test_invalid_prefix_raises(self):
        adapter = _make_adapter()
        with pytest.raises(ValidationError):
            adapter.channel_id_from_thread_id("slack:guild1:channel456")

    def test_too_few_parts_raises(self):
        adapter = _make_adapter()
        with pytest.raises(ValidationError):
            adapter.channel_id_from_thread_id("discord:only")


# ============================================================================
# normalizeDiscordEmoji edge cases (format converter)
# ============================================================================


class TestNormalizeDiscordEmojiEdgeCases:
    """Test Discord emoji/mention normalization via the format converter."""

    def test_custom_emoji_normalized_to_name(self):
        converter = DiscordFormatConverter()
        ast = converter.to_ast("Hello <:thumbsup:123456789>")
        # Custom emoji should be converted to :thumbsup:
        # The AST text should contain :thumbsup:
        text = converter.from_ast(ast)
        assert ":thumbsup:" in text

    def test_animated_emoji_normalized(self):
        converter = DiscordFormatConverter()
        ast = converter.to_ast("Look <a:dance:987654321>")
        text = converter.from_ast(ast)
        assert ":dance:" in text

    def test_multiple_custom_emoji(self):
        converter = DiscordFormatConverter()
        ast = converter.to_ast("<:a:1> and <:b:2>")
        text = converter.from_ast(ast)
        assert ":a:" in text
        assert ":b:" in text

    def test_user_mention_normalized(self):
        converter = DiscordFormatConverter()
        ast = converter.to_ast("Hello <@123456>")
        # Should normalize to @123456
        text = converter.from_ast(ast)
        # from_ast re-converts @mentions to <@mention> format
        assert "123456" in text

    def test_user_mention_with_exclamation(self):
        converter = DiscordFormatConverter()
        ast = converter.to_ast("Hello <@!123456>")
        text = converter.from_ast(ast)
        assert "123456" in text

    def test_channel_mention_normalized(self):
        converter = DiscordFormatConverter()
        ast = converter.to_ast("Go to <#789012>")
        text = converter.from_ast(ast)
        assert "789012" in text

    def test_role_mention_normalized(self):
        converter = DiscordFormatConverter()
        ast = converter.to_ast("Hey <@&555555>")
        text = converter.from_ast(ast)
        assert "555555" in text

    def test_spoiler_tags_converted(self):
        converter = DiscordFormatConverter()
        ast = converter.to_ast("This is ||hidden||")
        text = converter.from_ast(ast)
        assert "hidden" in text

    def test_empty_input(self):
        converter = DiscordFormatConverter()
        ast = converter.to_ast("")
        text = converter.from_ast(ast)
        assert text.strip() == ""

    def test_plain_text_unchanged(self):
        converter = DiscordFormatConverter()
        ast = converter.to_ast("No special formatting here")
        text = converter.from_ast(ast)
        assert "No special formatting here" in text

    def test_mixed_formatting(self):
        converter = DiscordFormatConverter()
        ast = converter.to_ast("**bold** and <:emoji:123> and <@user>")
        text = converter.from_ast(ast)
        assert "bold" in text
        assert ":emoji:" in text


# ============================================================================
# fetchMessages with cursors (forward/backward)
# ============================================================================


class TestFetchMessagesCursors:
    @pytest.mark.asyncio
    async def test_forward_cursor_uses_after(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(return_value=[])

        await adapter.fetch_messages(
            "discord:guild1:channel456",
            FetchOptions(cursor="msg200", direction="forward"),
        )

        call_args = adapter._discord_fetch.call_args
        assert "after=msg200" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_backward_cursor_uses_before(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(return_value=[])

        await adapter.fetch_messages(
            "discord:guild1:channel456",
            FetchOptions(cursor="msg200", direction="backward"),
        )

        call_args = adapter._discord_fetch.call_args
        assert "before=msg200" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_default_direction_is_backward(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(return_value=[])

        await adapter.fetch_messages(
            "discord:guild1:channel456",
            FetchOptions(cursor="msg200"),
        )

        call_args = adapter._discord_fetch.call_args
        assert "before=msg200" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_default_limit_is_50(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(return_value=[])

        await adapter.fetch_messages("discord:guild1:channel456")

        call_args = adapter._discord_fetch.call_args
        assert "limit=50" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_custom_limit(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(return_value=[])

        await adapter.fetch_messages(
            "discord:guild1:channel456",
            FetchOptions(limit=25),
        )

        call_args = adapter._discord_fetch.call_args
        assert "limit=25" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_forward_next_cursor_from_first_element(self):
        """Forward direction: next cursor is from raw_messages[0] (newest)."""
        adapter = _make_adapter(logger=_make_logger())
        raw_messages = [
            {
                "id": f"msg{i}",
                "channel_id": "channel456",
                "content": f"Message {i}",
                "timestamp": f"2021-01-01T00:0{i}:00.000Z",
                "author": {"id": "u1", "username": "user"},
                "attachments": [],
            }
            for i in range(5)
        ]
        adapter._discord_fetch = AsyncMock(return_value=raw_messages)

        result = await adapter.fetch_messages(
            "discord:guild1:channel456",
            FetchOptions(limit=5, direction="forward"),
        )

        # When result count == limit, next_cursor should be set
        assert result.next_cursor is not None
        # Forward: next cursor from raw_messages[0] (which is the first element returned by Discord)
        assert result.next_cursor == "msg0"

    @pytest.mark.asyncio
    async def test_backward_next_cursor_from_last_element(self):
        """Backward direction: next cursor is from raw_messages[-1] (oldest)."""
        adapter = _make_adapter(logger=_make_logger())
        raw_messages = [
            {
                "id": f"msg{i}",
                "channel_id": "channel456",
                "content": f"Message {i}",
                "timestamp": f"2021-01-01T00:0{i}:00.000Z",
                "author": {"id": "u1", "username": "user"},
                "attachments": [],
            }
            for i in range(5)
        ]
        adapter._discord_fetch = AsyncMock(return_value=raw_messages)

        result = await adapter.fetch_messages(
            "discord:guild1:channel456",
            FetchOptions(limit=5, direction="backward"),
        )

        assert result.next_cursor is not None
        # Backward: next cursor from raw_messages[-1] (the last/oldest element)
        assert result.next_cursor == "msg4"

    @pytest.mark.asyncio
    async def test_handles_non_list_response(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(return_value={"error": "something"})

        result = await adapter.fetch_messages("discord:guild1:channel456")

        assert len(result.messages) == 0
        assert result.next_cursor is None

    @pytest.mark.asyncio
    async def test_fetches_from_thread_channel(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(return_value=[])

        await adapter.fetch_messages("discord:guild1:channel456:thread789")

        call_args = adapter._discord_fetch.call_args
        assert "/channels/thread789/messages?" in call_args[0][0]


# ============================================================================
# fetchChannelInfo
# ============================================================================


class TestFetchChannelInfo:
    @pytest.mark.asyncio
    async def test_fetches_text_channel(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(
            return_value={
                "id": "channel456",
                "name": "general",
                "type": 0,
                "member_count": 42,
            }
        )

        result = await adapter.fetch_channel_info("discord:guild1:channel456")

        assert result.id == "discord:guild1:channel456"
        assert result.name == "general"
        assert result.is_dm is False
        assert result.member_count == 42
        assert result.metadata["guild_id"] == "guild1"
        assert result.metadata["channel_type"] == 0

    @pytest.mark.asyncio
    async def test_fetches_dm_channel(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(
            return_value={
                "id": "dm123",
                "type": CHANNEL_TYPE_DM,
            }
        )

        result = await adapter.fetch_channel_info("discord:@me:dm123")

        assert result.is_dm is True
        assert result.name is None

    @pytest.mark.asyncio
    async def test_fetches_group_dm(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(
            return_value={
                "id": "gdm456",
                "name": "Friends",
                "type": CHANNEL_TYPE_GROUP_DM,
            }
        )

        result = await adapter.fetch_channel_info("discord:@me:gdm456")

        assert result.is_dm is True
        assert result.name == "Friends"

    @pytest.mark.asyncio
    async def test_calls_correct_api_path(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._discord_fetch = AsyncMock(return_value={"id": "ch", "type": 0})

        await adapter.fetch_channel_info("discord:guild1:channel999")

        assert adapter._discord_fetch.call_count == 1
        adapter._discord_fetch.assert_called_once_with("/channels/channel999", "GET")


# ============================================================================
# stream (accumulate and post/edit)
# ============================================================================


class TestStream:
    @pytest.mark.asyncio
    async def test_stream_accumulates_and_posts(self):
        adapter = _make_adapter(logger=_make_logger())

        post_result = MagicMock()
        post_result.id = "posted-msg-1"
        adapter.post_message = AsyncMock(return_value=post_result)

        edit_result = MagicMock()
        edit_result.id = "posted-msg-1"
        adapter.edit_message = AsyncMock(return_value=edit_result)

        async def text_gen():
            yield "Hello "
            yield "world"

        result = await adapter.stream("discord:guild1:channel456", text_gen())

        assert result.id == "posted-msg-1"
        assert result.raw["text"] == "Hello world"
        # First chunk creates a new message
        adapter.post_message.assert_called_once()
        # Second chunk edits the existing message
        adapter.edit_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_stream_with_dict_chunks(self):
        adapter = _make_adapter(logger=_make_logger())

        post_result = MagicMock()
        post_result.id = "posted-msg-2"
        adapter.post_message = AsyncMock(return_value=post_result)
        adapter.edit_message = AsyncMock(return_value=post_result)

        async def text_gen():
            yield {"type": "markdown_text", "text": "Part 1"}
            yield {"type": "markdown_text", "text": " Part 2"}

        result = await adapter.stream("discord:guild1:channel456", text_gen())

        assert result.raw["text"] == "Part 1 Part 2"

    @pytest.mark.asyncio
    async def test_stream_skips_empty_chunks(self):
        adapter = _make_adapter(logger=_make_logger())

        post_result = MagicMock()
        post_result.id = "posted-msg-3"
        adapter.post_message = AsyncMock(return_value=post_result)
        adapter.edit_message = AsyncMock(return_value=post_result)

        async def text_gen():
            yield ""
            yield "content"
            yield ""

        result = await adapter.stream("discord:guild1:channel456", text_gen())

        assert result.raw["text"] == "content"
        adapter.post_message.assert_called_once()
        adapter.edit_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_stream_with_no_chunks(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter.post_message = AsyncMock()
        adapter.edit_message = AsyncMock()

        async def text_gen():
            return
            yield  # make it an async generator  # noqa: RET504

        result = await adapter.stream("discord:guild1:channel456", text_gen())

        assert result.id == ""
        assert result.raw["text"] == ""
        adapter.post_message.assert_not_called()
        adapter.edit_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_stream_with_unknown_chunk_type(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter.post_message = AsyncMock()
        adapter.edit_message = AsyncMock()

        async def text_gen():
            yield {"type": "plan_update", "title": "test"}

        result = await adapter.stream("discord:guild1:channel456", text_gen())

        # plan_update chunks don't have text, so nothing posted
        assert result.raw["text"] == ""
        adapter.post_message.assert_not_called()


# ============================================================================
# disconnect
# ============================================================================


class TestDisconnect:
    @pytest.mark.asyncio
    async def test_disconnect_is_noop(self):
        adapter = _make_adapter(logger=_make_logger())
        # No assertion needed -- tests that disconnect completes without raising
        await adapter.disconnect()
        assert True

    @pytest.mark.asyncio
    async def test_disconnect_logs_debug(self):
        logger = _make_logger()
        adapter = _make_adapter(logger=logger)
        await adapter.disconnect()
        assert logger.debug.called


# ============================================================================
# Protocol properties (lock_scope, persist_message_history)
# ============================================================================


class TestProtocolProperties:
    def test_lock_scope_is_none(self):
        adapter = _make_adapter()
        assert adapter.lock_scope is None

    def test_persist_message_history_is_none(self):
        adapter = _make_adapter()
        assert adapter.persist_message_history is None


# ============================================================================
# Format converter: render_postable variants
# ============================================================================


class TestFormatConverterRenderPostable:
    def test_render_postable_with_raw_string(self):
        converter = DiscordFormatConverter()
        result = converter.render_postable({"raw": "Hello @user"})
        assert "<@user>" in result

    def test_render_postable_with_markdown_key(self):
        converter = DiscordFormatConverter()
        result = converter.render_postable({"markdown": "**bold** text"})
        assert "**bold**" in result

    def test_render_postable_with_plain_string(self):
        converter = DiscordFormatConverter()
        result = converter.render_postable("Hello @user")
        assert "<@user>" in result

    def test_render_postable_with_empty_dict(self):
        converter = DiscordFormatConverter()
        result = converter.render_postable({})
        assert result == ""

    def test_render_postable_with_ast(self):
        converter = DiscordFormatConverter()
        ast = converter.to_ast("Hello world")
        result = converter.render_postable({"ast": ast})
        assert "Hello world" in result


# ============================================================================
# Additional edge cases for encode/decode round-trips
# ============================================================================


class TestEncodeDecodeRoundTrip:
    def test_roundtrip_guild_channel(self):
        adapter = _make_adapter()
        original = DiscordThreadId(guild_id="guild1", channel_id="ch1")
        encoded = adapter.encode_thread_id(original)
        decoded = adapter.decode_thread_id(encoded)
        assert decoded.guild_id == "guild1"
        assert decoded.channel_id == "ch1"
        assert decoded.thread_id is None

    def test_roundtrip_guild_channel_thread(self):
        adapter = _make_adapter()
        original = DiscordThreadId(guild_id="guild1", channel_id="ch1", thread_id="t1")
        encoded = adapter.encode_thread_id(original)
        decoded = adapter.decode_thread_id(encoded)
        assert decoded.guild_id == "guild1"
        assert decoded.channel_id == "ch1"
        assert decoded.thread_id == "t1"

    def test_roundtrip_dm(self):
        adapter = _make_adapter()
        original = DiscordThreadId(guild_id="@me", channel_id="dm1")
        encoded = adapter.encode_thread_id(original)
        decoded = adapter.decode_thread_id(encoded)
        assert decoded.guild_id == "@me"
        assert decoded.channel_id == "dm1"

    def test_channel_id_from_thread_id_roundtrip(self):
        adapter = _make_adapter()
        thread_id = "discord:guild1:channel456:thread789"
        channel_id = adapter.channel_id_from_thread_id(thread_id)
        # channel_id should decode cleanly
        decoded = adapter.decode_thread_id(channel_id)
        assert decoded.guild_id == "guild1"
        assert decoded.channel_id == "channel456"
        assert decoded.thread_id is None


# ============================================================================
# parse_message edge cases
# ============================================================================


class TestParseMessageEdgeCases:
    def test_parse_message_no_guild_id_defaults_to_me(self):
        adapter = _make_adapter()
        raw = {
            "id": "msg1",
            "channel_id": "dm123",
            "content": "DM message",
            "timestamp": "2021-01-01T00:00:00.000Z",
            "author": {"id": "u1", "username": "user1"},
            "attachments": [],
        }
        msg = adapter.parse_message(raw)
        assert msg.thread_id.startswith("discord:@me:")

    def test_parse_message_with_guild_id(self):
        adapter = _make_adapter()
        raw = {
            "id": "msg1",
            "guild_id": "guild123",
            "channel_id": "ch456",
            "content": "Guild message",
            "timestamp": "2021-01-01T00:00:00.000Z",
            "author": {"id": "u1", "username": "user1"},
            "attachments": [],
        }
        msg = adapter.parse_message(raw)
        assert msg.thread_id == "discord:guild123:ch456"

    def test_parse_message_bot_is_me(self):
        adapter = _make_adapter()
        raw = {
            "id": "msg1",
            "channel_id": "ch1",
            "content": "Bot message",
            "timestamp": "2021-01-01T00:00:00.000Z",
            "author": {"id": "test-app-id", "username": "bot", "bot": True},
            "attachments": [],
        }
        msg = adapter.parse_message(raw)
        assert msg.author.is_bot is True
        assert msg.author.is_me is True

    def test_parse_message_edited_timestamp(self):
        adapter = _make_adapter()
        raw = {
            "id": "msg1",
            "channel_id": "ch1",
            "content": "Edited",
            "timestamp": "2021-01-01T00:00:00.000Z",
            "edited_timestamp": "2021-01-01T01:00:00.000Z",
            "author": {"id": "u1", "username": "user1"},
            "attachments": [],
        }
        msg = adapter.parse_message(raw)
        assert msg.metadata.edited is True
        assert msg.metadata.edited_at is not None

    def test_parse_message_thread_starter(self):
        adapter = _make_adapter()
        raw = {
            "id": "msg1",
            "channel_id": "ch1",
            "content": "",
            "type": 21,  # ThreadStarterMessage
            "timestamp": "2021-01-01T00:00:00.000Z",
            "author": {"id": "u1", "username": "user1"},
            "referenced_message": {
                "id": "msg0",
                "channel_id": "ch1",
                "content": "Original message",
                "timestamp": "2021-01-01T00:00:00.000Z",
                "author": {"id": "u1", "username": "user1"},
                "attachments": [],
            },
            "attachments": [],
        }
        msg = adapter.parse_message(raw)
        assert msg.text == "Original message"

    def test_parse_message_attachment_types(self):
        adapter = _make_adapter()
        raw = {
            "id": "msg1",
            "channel_id": "ch1",
            "content": "",
            "timestamp": "2021-01-01T00:00:00.000Z",
            "author": {"id": "u1", "username": "user1"},
            "attachments": [
                {"filename": "photo.jpg", "url": "https://example.com/photo.jpg", "content_type": "image/jpeg"},
                {"filename": "video.mp4", "url": "https://example.com/video.mp4", "content_type": "video/mp4"},
                {"filename": "audio.mp3", "url": "https://example.com/audio.mp3", "content_type": "audio/mpeg"},
                {"filename": "doc.pdf", "url": "https://example.com/doc.pdf", "content_type": "application/pdf"},
                {"filename": "unknown", "url": "https://example.com/unknown"},
            ],
        }
        msg = adapter.parse_message(raw)
        assert msg.attachments[0].type == "image"
        assert msg.attachments[1].type == "video"
        assert msg.attachments[2].type == "audio"
        assert msg.attachments[3].type == "file"
        assert msg.attachments[4].type == "file"


# ============================================================================
# Truncate content
# ============================================================================


class TestTruncateContentEdgeCases:
    def test_exactly_at_limit(self):
        adapter = _make_adapter()
        text = "x" * 2000
        result = adapter._truncate_content(text)
        assert len(result) == 2000
        assert result == text

    def test_one_over_limit(self):
        adapter = _make_adapter()
        text = "x" * 2001
        result = adapter._truncate_content(text)
        assert len(result) == 2000
        assert result.endswith("...")

    def test_empty_string(self):
        adapter = _make_adapter()
        result = adapter._truncate_content("")
        assert result == ""


# ============================================================================
# Emoji encoding edge cases
# ============================================================================


class TestEmojiEncodingEdgeCases:
    def test_encode_simple_name(self):
        adapter = _make_adapter()
        result = adapter._encode_emoji("thumbsup")
        assert result == "thumbsup"

    def test_encode_unicode_emoji(self):
        adapter = _make_adapter()
        result = adapter._encode_emoji("\U0001f44d")
        assert "%F0%9F%91%8D" in result or "\U0001f44d" in result

    def test_encode_emoji_value_object(self):
        from chat_sdk.types import EmojiValue

        adapter = _make_adapter()
        result = adapter._encode_emoji(EmojiValue(name="heart"))
        assert result == "heart"


# ============================================================================
# Format converter: from_ast various node types
# ============================================================================


class TestFormatConverterNodeTypes:
    def test_bold(self):
        converter = DiscordFormatConverter()
        ast = converter.to_ast("**bold text**")
        text = converter.from_ast(ast)
        assert "**bold text**" in text

    def test_italic(self):
        converter = DiscordFormatConverter()
        ast = converter.to_ast("*italic text*")
        text = converter.from_ast(ast)
        assert "*italic text*" in text

    def test_strikethrough(self):
        converter = DiscordFormatConverter()
        ast = converter.to_ast("~~deleted~~")
        text = converter.from_ast(ast)
        assert "~~deleted~~" in text

    def test_inline_code(self):
        converter = DiscordFormatConverter()
        ast = converter.to_ast("`code`")
        text = converter.from_ast(ast)
        assert "`code`" in text

    def test_code_block(self):
        converter = DiscordFormatConverter()
        ast = converter.to_ast("```python\nprint('hello')\n```")
        text = converter.from_ast(ast)
        assert "```" in text
        assert "print" in text

    def test_link(self):
        converter = DiscordFormatConverter()
        ast = converter.to_ast("[click here](https://example.com)")
        text = converter.from_ast(ast)
        assert "https://example.com" in text

    def test_blockquote(self):
        converter = DiscordFormatConverter()
        ast = converter.to_ast("> quoted text")
        text = converter.from_ast(ast)
        assert ">" in text
        assert "quoted" in text


# ============================================================================
# is_dm
# ============================================================================


class TestIsDMEdgeCases:
    def test_guild_channel_is_not_dm(self):
        adapter = _make_adapter()
        assert adapter.is_dm("discord:guild1:channel456") is False

    def test_dm_is_dm(self):
        adapter = _make_adapter()
        assert adapter.is_dm("discord:@me:dm123") is True

    def test_dm_with_thread_is_dm(self):
        adapter = _make_adapter()
        assert adapter.is_dm("discord:@me:dm123:thread456") is True

    def test_guild_with_thread_is_not_dm(self):
        adapter = _make_adapter()
        assert adapter.is_dm("discord:guild1:ch1:thread1") is False
