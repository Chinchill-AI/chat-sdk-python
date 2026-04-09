"""Tests for to_ai_messages.

Covers: basic conversion, user/assistant messages, attachments (image, file),
names, empty messages, link previews, mentions, and transformMessage hook.
"""

from __future__ import annotations

import base64
from typing import Any
from unittest.mock import MagicMock

import pytest

from chat_sdk.ai import AiMessage, ToAiMessagesOptions, to_ai_messages
from chat_sdk.testing import create_test_message
from chat_sdk.types import Attachment, Author, LinkPreview, Message

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bot_author() -> Author:
    return Author(
        user_id="bot",
        user_name="bot",
        full_name="Bot",
        is_bot=True,
        is_me=True,
    )


def _user_author(
    user_id: str = "U1",
    user_name: str = "alice",
    full_name: str = "Alice",
) -> Author:
    return Author(
        user_id=user_id,
        user_name=user_name,
        full_name=full_name,
        is_bot=False,
        is_me=False,
    )


# ============================================================================
# Basic conversion
# ============================================================================


class TestBasicConversion:
    """Tests for basic message role mapping and filtering."""

    @pytest.mark.asyncio
    async def test_maps_isme_to_assistant_and_others_to_user(self):
        messages = [
            create_test_message("1", "Hello bot"),
            create_test_message("2", "Hi there!", author=_bot_author()),
            create_test_message("3", "Follow up question"),
        ]
        result = await to_ai_messages(messages)

        assert result == [
            {"role": "user", "content": "Hello bot"},
            {"role": "assistant", "content": "Hi there!"},
            {"role": "user", "content": "Follow up question"},
        ]

    @pytest.mark.asyncio
    async def test_filters_out_empty_and_whitespaceonly_text(self):
        messages = [
            create_test_message("1", "Hello"),
            create_test_message("2", ""),
            create_test_message("3", "   "),
            create_test_message("4", "\t\n"),
            create_test_message("5", "World"),
        ]
        result = await to_ai_messages(messages)

        assert result == [
            {"role": "user", "content": "Hello"},
            {"role": "user", "content": "World"},
        ]

    @pytest.mark.asyncio
    async def test_preserves_chronological_order(self):
        messages = [
            create_test_message("1", "First"),
            create_test_message("2", "Second", author=_bot_author()),
            create_test_message("3", "Third"),
        ]
        result = await to_ai_messages(messages)
        assert [m["content"] for m in result] == ["First", "Second", "Third"]

    @pytest.mark.asyncio
    async def test_returns_empty_array_for_empty_input(self):
        assert await to_ai_messages([]) == []

    @pytest.mark.asyncio
    async def test_returns_empty_array_when_all_messages_have_empty_text(self):
        messages = [
            create_test_message("1", ""),
            create_test_message("2", "   "),
        ]
        assert await to_ai_messages(messages) == []


# ============================================================================
# Names
# ============================================================================


class TestIncludeNames:
    """Tests for includeNames option."""

    @pytest.mark.asyncio
    async def test_prefixes_user_messages_with_username_when_includenames_is_true(self):
        messages = [
            create_test_message("1", "Hello", author=_user_author(user_name="alice")),
            create_test_message("2", "Hi!", author=_bot_author()),
            create_test_message("3", "Thanks", author=_user_author(user_id="U2", user_name="bob", full_name="Bob")),
        ]
        result = await to_ai_messages(messages, ToAiMessagesOptions(include_names=True))

        assert result == [
            {"role": "user", "content": "[alice]: Hello"},
            {"role": "assistant", "content": "Hi!"},
            {"role": "user", "content": "[bob]: Thanks"},
        ]


# ============================================================================
# Link previews
# ============================================================================


class TestLinkPreviews:
    """Tests for link preview metadata appended to content."""

    @pytest.mark.asyncio
    async def test_appends_link_preview_metadata_to_content(self):
        messages = [
            create_test_message(
                "1",
                "Check this out",
                links=[
                    LinkPreview(
                        url="https://vercel.com/blog/post",
                        title="New Feature",
                        description="A cool new feature",
                        site_name="Vercel",
                    ),
                ],
            ),
        ]
        result = await to_ai_messages(messages)

        expected = (
            "Check this out\n\nLinks:\n"
            "https://vercel.com/blog/post\n"
            "Title: New Feature\n"
            "Description: A cool new feature\n"
            "Site: Vercel"
        )
        assert result == [{"role": "user", "content": expected}]

    @pytest.mark.asyncio
    async def test_appends_multiple_links(self):
        messages = [
            create_test_message(
                "1",
                "See these links",
                links=[
                    LinkPreview(url="https://example.com"),
                    LinkPreview(url="https://vercel.com", title="Vercel"),
                ],
            ),
        ]
        result = await to_ai_messages(messages)
        assert result[0]["content"] == (
            "See these links\n\nLinks:\nhttps://example.com\n\nhttps://vercel.com\nTitle: Vercel"
        )

    @pytest.mark.asyncio
    async def test_labels_links_with_fetchmessage_as_embedded_messages(self):
        async def fetch_linked() -> Message:
            return create_test_message("linked", "linked")

        messages = [
            create_test_message(
                "1",
                "Look at this thread",
                links=[
                    LinkPreview(
                        url="https://team.slack.com/archives/C123/p1234567890123456",
                        fetch_message=fetch_linked,
                    ),
                ],
            ),
        ]
        result = await to_ai_messages(messages)
        assert result[0]["content"] == (
            "Look at this thread\n\nLinks:\n[Embedded message: https://team.slack.com/archives/C123/p1234567890123456]"
        )

    @pytest.mark.asyncio
    async def test_includes_metadata_on_embedded_message_links(self):
        async def fetch_linked() -> Message:
            return create_test_message("linked", "linked")

        messages = [
            create_test_message(
                "1",
                "Look at this",
                links=[
                    LinkPreview(
                        url="https://team.slack.com/archives/C123/p1234567890123456",
                        title="Original message preview",
                        fetch_message=fetch_linked,
                    ),
                ],
            ),
        ]
        result = await to_ai_messages(messages)
        assert result[0]["content"] == (
            "Look at this\n\nLinks:\n"
            "[Embedded message: https://team.slack.com/archives/C123/p1234567890123456]\n"
            "Title: Original message preview"
        )

    @pytest.mark.asyncio
    async def test_does_not_append_links_section_when_links_array_is_empty(self):
        messages = [create_test_message("1", "No links here")]
        result = await to_ai_messages(messages)
        assert result[0]["content"] == "No links here"


# ============================================================================
# Attachments
# ============================================================================


class TestAttachments:
    """Tests for attachment handling in to_ai_messages."""

    @pytest.mark.asyncio
    async def test_includes_image_attachments_as_image_parts(self):
        async def fetch_data() -> bytes:
            return b"jpeg-data"

        messages = [
            create_test_message(
                "1",
                "Look at this image",
                attachments=[
                    Attachment(
                        type="image",
                        mime_type="image/jpeg",
                        name="photo.jpg",
                        fetch_data=fetch_data,
                    ),
                ],
            ),
        ]
        result = await to_ai_messages(messages)
        content = result[0]["content"]

        assert isinstance(content, list)
        assert len(content) == 2
        assert content[0] == {"type": "text", "text": "Look at this image"}
        assert content[1]["type"] == "file"

    @pytest.mark.asyncio
    async def test_includes_text_file_attachments_as_file_parts(self):
        async def fetch_data() -> bytes:
            return b'{"key": "value"}'

        messages = [
            create_test_message(
                "1",
                "Here is a config",
                attachments=[
                    Attachment(
                        type="file",
                        mime_type="application/json",
                        name="config.json",
                        fetch_data=fetch_data,
                    ),
                ],
            ),
        ]
        result = await to_ai_messages(messages)
        content = result[0]["content"]

        assert isinstance(content, list)
        assert len(content) == 2
        assert content[0] == {"type": "text", "text": "Here is a config"}
        assert content[1]["type"] == "file"

    @pytest.mark.asyncio
    async def test_supports_various_text_mime_types(self):
        mime_types = [
            "text/plain",
            "text/csv",
            "text/html",
            "application/json",
            "application/xml",
            "application/javascript",
            "application/yaml",
        ]

        for mime_type in mime_types:

            async def fetch_data() -> bytes:
                return b"content"

            messages = [
                create_test_message(
                    "1",
                    "file",
                    attachments=[
                        Attachment(type="file", mime_type=mime_type, fetch_data=fetch_data),
                    ],
                ),
            ]
            result = await to_ai_messages(messages)
            content = result[0]["content"]
            assert isinstance(content, list), f"Failed for {mime_type}"
            assert content[1]["type"] == "file", f"Failed for {mime_type}"

    @pytest.mark.asyncio
    async def test_includes_multiple_attachments_as_parts(self):
        async def make_fetch(data: bytes):
            async def fetch() -> bytes:
                return data

            return fetch

        messages = [
            create_test_message(
                "1",
                "Multiple files",
                attachments=[
                    Attachment(type="image", mime_type="image/png", fetch_data=await make_fetch(b"png1")),
                    Attachment(type="image", mime_type="image/jpeg", fetch_data=await make_fetch(b"jpg2")),
                    Attachment(
                        type="file", mime_type="text/plain", name="log.txt", fetch_data=await make_fetch(b"log content")
                    ),
                ],
            ),
        ]
        result = await to_ai_messages(messages)
        content = result[0]["content"]

        assert len(content) == 4  # 1 text + 3 attachments
        assert content[0]["type"] == "text"
        assert content[1]["type"] == "file"
        assert content[2]["type"] == "file"
        assert content[3]["type"] == "file"

    @pytest.mark.asyncio
    async def test_warns_on_video_attachments(self):
        on_unsupported = MagicMock()
        messages = [
            create_test_message(
                "1",
                "Watch this",
                attachments=[
                    Attachment(type="video", url="https://example.com/video.mp4", mime_type="video/mp4"),
                ],
            ),
        ]
        result = await to_ai_messages(
            messages,
            ToAiMessagesOptions(on_unsupported_attachment=on_unsupported),
        )

        assert result[0]["content"] == "Watch this"  # string, no parts
        assert on_unsupported.call_count == 1
        assert on_unsupported.call_args[0][0].type == "video"

    @pytest.mark.asyncio
    async def test_warns_on_audio_attachments(self):
        on_unsupported = MagicMock()
        messages = [
            create_test_message(
                "1",
                "Listen to this",
                attachments=[
                    Attachment(type="audio", url="https://example.com/audio.mp3", mime_type="audio/mpeg"),
                ],
            ),
        ]
        result = await to_ai_messages(
            messages,
            ToAiMessagesOptions(on_unsupported_attachment=on_unsupported),
        )

        assert result[0]["content"] == "Listen to this"
        assert on_unsupported.call_count == 1
        assert on_unsupported.call_args[0][0].type == "audio"

    @pytest.mark.asyncio
    async def test_skips_nontext_file_attachments_silently(self):
        on_unsupported = MagicMock()
        messages = [
            create_test_message(
                "1",
                "Here is a PDF",
                attachments=[
                    Attachment(
                        type="file", url="https://example.com/doc.pdf", mime_type="application/pdf", name="doc.pdf"
                    ),
                ],
            ),
        ]
        result = await to_ai_messages(
            messages,
            ToAiMessagesOptions(on_unsupported_attachment=on_unsupported),
        )

        assert result[0]["content"] == "Here is a PDF"
        assert on_unsupported.call_count == 0

    @pytest.mark.asyncio
    async def test_uses_fetchdata_to_inline_image_as_base64(self):
        raw_data = b"fake-png-data"

        async def fetch_data() -> bytes:
            return raw_data

        messages = [
            create_test_message(
                "1",
                "Private image",
                attachments=[
                    Attachment(type="image", mime_type="image/png", fetch_data=fetch_data),
                ],
            ),
        ]
        result = await to_ai_messages(messages)
        content = result[0]["content"]

        assert isinstance(content, list)
        assert content[1]["type"] == "file"
        expected_b64 = base64.b64encode(raw_data).decode("ascii")
        assert content[1]["data"] == f"data:image/png;base64,{expected_b64}"
        assert content[1]["mediaType"] == "image/png"

    @pytest.mark.asyncio
    async def test_uses_fetchdata_to_inline_text_file_as_base64(self):
        raw_data = b"error at line 42"

        async def fetch_data() -> bytes:
            return raw_data

        messages = [
            create_test_message(
                "1",
                "Here is a log",
                attachments=[
                    Attachment(type="file", mime_type="text/plain", name="server.log", fetch_data=fetch_data),
                ],
            ),
        ]
        result = await to_ai_messages(messages)
        content = result[0]["content"]

        assert isinstance(content, list)
        assert content[1]["type"] == "file"
        expected_b64 = base64.b64encode(raw_data).decode("ascii")
        assert content[1]["data"] == f"data:text/plain;base64,{expected_b64}"
        assert content[1]["filename"] == "server.log"

    @pytest.mark.asyncio
    async def test_skips_image_when_fetchdata_fails(self):
        async def fetch_data() -> bytes:
            raise RuntimeError("network error")

        messages = [
            create_test_message(
                "1",
                "Image here",
                attachments=[
                    Attachment(
                        type="image", url="https://example.com/img.png", mime_type="image/png", fetch_data=fetch_data
                    ),
                ],
            ),
        ]
        result = await to_ai_messages(messages)
        assert result[0]["content"] == "Image here"

    @pytest.mark.asyncio
    async def test_skips_attachments_without_url_or_fetchdata(self):
        messages = [
            create_test_message(
                "1",
                "Uploaded something",
                attachments=[
                    Attachment(type="image", mime_type="image/png"),
                ],
            ),
        ]
        result = await to_ai_messages(messages)
        assert result[0]["content"] == "Uploaded something"

    @pytest.mark.asyncio
    async def test_keeps_string_content_when_no_supported_attachments(self):
        messages = [
            create_test_message("1", "Just text", attachments=[]),
        ]
        result = await to_ai_messages(messages)
        assert isinstance(result[0]["content"], str)


# ============================================================================
# Transform message
# ============================================================================


class TestTransformMessage:
    """Tests for the transformMessage hook."""

    @pytest.mark.asyncio
    async def test_transformmessage_can_modify_text_content(self):
        messages = [create_test_message("1", "Hello <@U123>")]
        result = await to_ai_messages(
            messages,
            ToAiMessagesOptions(
                transform_message=lambda ai_msg, _src: {
                    **ai_msg,
                    "content": ai_msg["content"].replace("<@U123>", "@VercelBot"),
                },
            ),
        )
        assert result == [{"role": "user", "content": "Hello @VercelBot"}]

    @pytest.mark.asyncio
    async def test_transformmessage_returning_null_skips_the_message(self):
        messages = [
            create_test_message("1", "Keep this"),
            create_test_message("2", "Skip this"),
            create_test_message("3", "Keep this too"),
        ]
        result = await to_ai_messages(
            messages,
            ToAiMessagesOptions(
                transform_message=lambda ai_msg, _src: None if "Skip" in ai_msg["content"] else ai_msg,
            ),
        )
        assert result == [
            {"role": "user", "content": "Keep this"},
            {"role": "user", "content": "Keep this too"},
        ]

    @pytest.mark.asyncio
    async def test_transformmessage_receives_correct_source_message(self):
        messages = [
            create_test_message("msg-1", "Hello", author=_user_author(user_name="alice")),
        ]
        calls: list[tuple[Any, Any]] = []

        def transform(ai_msg: AiMessage, src: Message) -> AiMessage:
            calls.append((ai_msg, src))
            return ai_msg

        await to_ai_messages(messages, ToAiMessagesOptions(transform_message=transform))

        assert len(calls) == 1
        ai_msg, source_msg = calls[0]
        assert ai_msg == {"role": "user", "content": "Hello"}
        assert source_msg.id == "msg-1"
        assert source_msg.author.user_name == "alice"

    @pytest.mark.asyncio
    async def test_transformmessage_works_with_async_callbacks(self):
        messages = [create_test_message("1", "Original")]

        async def async_transform(ai_msg: AiMessage, _src: Message) -> AiMessage:
            return {**ai_msg, "content": "Transformed"}

        result = await to_ai_messages(
            messages,
            ToAiMessagesOptions(transform_message=async_transform),
        )
        assert result == [{"role": "user", "content": "Transformed"}]


# ============================================================================
# Mentions
# ============================================================================


class TestMentions:
    """Tests for mention rendering in message text."""

    @pytest.mark.asyncio
    async def test_renders_mentions_with_display_names_in_message_text(self):
        messages = [create_test_message("1", "Hey @john, can you review this?")]
        result = await to_ai_messages(messages)
        assert result[0]["content"] == "Hey @john, can you review this?"

    @pytest.mark.asyncio
    async def test_renders_multiple_mentions_correctly(self):
        messages = [create_test_message("1", "@alice and @bob please look at this")]
        result = await to_ai_messages(messages)
        assert result[0]["content"] == "@alice and @bob please look at this"

    @pytest.mark.asyncio
    async def test_renders_mentions_with_includenames_enabled(self):
        messages = [
            create_test_message(
                "1",
                "Hey @bob, thoughts?",
                author=_user_author(user_name="alice"),
            ),
        ]
        result = await to_ai_messages(messages, ToAiMessagesOptions(include_names=True))
        assert result[0]["content"] == "[alice]: Hey @bob, thoughts?"


# ============================================================================
# Mixed link types (embedded + regular)
# ============================================================================


class TestMixedLinks:
    """Tests for mixed embedded and regular links."""

    @pytest.mark.asyncio
    async def test_mixes_embedded_messages_and_regular_links(self):
        async def fetch_linked() -> Message:
            return create_test_message("linked", "linked")

        messages = [
            create_test_message(
                "1",
                "Check these",
                links=[
                    LinkPreview(
                        url="https://team.slack.com/archives/C123/p1234567890123456",
                        fetch_message=fetch_linked,
                    ),
                    LinkPreview(
                        url="https://vercel.com",
                        title="Vercel",
                        site_name="Vercel",
                    ),
                ],
            ),
        ]
        result = await to_ai_messages(messages)
        assert result[0]["content"] == (
            "Check these\n\nLinks:\n"
            "[Embedded message: https://team.slack.com/archives/C123/p1234567890123456]\n\n"
            "https://vercel.com\nTitle: Vercel\nSite: Vercel"
        )


# ============================================================================
# Links with attachments
# ============================================================================


class TestLinksWithAttachments:
    """Tests for link rendering when attachments are present."""

    @pytest.mark.asyncio
    async def test_includes_links_in_text_part_when_attachments_are_present(self):
        async def fetch_data() -> bytes:
            return b"img"

        messages = [
            create_test_message(
                "1",
                "Image with link",
                links=[LinkPreview(url="https://example.com", title="Example")],
                attachments=[
                    Attachment(type="image", mime_type="image/png", fetch_data=fetch_data),
                ],
            ),
        ]
        result = await to_ai_messages(messages)
        content = result[0]["content"]

        assert isinstance(content, list)
        text_part = content[0]
        assert text_part["type"] == "text"
        assert "Links:\nhttps://example.com" in text_part["text"]
        assert content[1]["type"] == "file"


# ============================================================================
# Additional mention tests
# ============================================================================


class TestAdditionalMentions:
    """Additional mention rendering tests."""

    @pytest.mark.asyncio
    async def test_renders_mentions_with_user_ids_when_display_name_unavailable(self):
        messages = [create_test_message("1", "Hey @U456, check this")]
        result = await to_ai_messages(messages)

        assert result[0]["content"] == "Hey @U456, check this"
        assert "<@" not in result[0]["content"]

    @pytest.mark.asyncio
    async def test_renders_mentions_in_messages_with_links(self):
        messages = [
            create_test_message(
                "1",
                "@alice shared a link",
                links=[LinkPreview(url="https://example.com")],
            ),
        ]
        result = await to_ai_messages(messages)

        assert "@alice shared a link" in result[0]["content"]
        assert "https://example.com" in result[0]["content"]
        assert "<@" not in result[0]["content"]


# ============================================================================
# Transform message with attachments
# ============================================================================


class TestTransformMessageAttachments:
    """Tests for transformMessage with multipart content."""

    @pytest.mark.asyncio
    async def test_transformmessage_receives_multipart_content_for_messages_with_attachments(self):
        async def fetch_data() -> bytes:
            return b"png-data"

        messages = [
            create_test_message(
                "1",
                "Image here",
                attachments=[
                    Attachment(type="image", mime_type="image/png", fetch_data=fetch_data),
                ],
            ),
        ]
        calls: list[tuple[Any, Any]] = []

        def transform(ai_msg: AiMessage, src: Message) -> AiMessage:
            calls.append((ai_msg, src))
            return ai_msg

        await to_ai_messages(messages, ToAiMessagesOptions(transform_message=transform))

        assert len(calls) == 1
        ai_msg = calls[0][0]
        assert ai_msg["role"] == "user"
        assert isinstance(ai_msg["content"], list)
        assert len(ai_msg["content"]) == 2
