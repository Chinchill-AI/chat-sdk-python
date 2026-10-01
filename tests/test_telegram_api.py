"""Tests for Telegram adapter API-calling methods.

Covers: post_message (text, card, parse mode), edit_message, delete_message,
add_reaction, remove_reaction, start_typing, callback query dispatch,
reaction update dispatch, error mapping (401, 429, 403),
fetch_thread, fetch_channel_info.

Mocks telegram_fetch to intercept all Bot API calls without network access.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from chat_sdk.adapters.telegram.adapter import (
    TelegramAdapter,
    _trim_to_markdown_v2_safe_boundary,
    ends_with_orphan_backslash,
    truncate_for_telegram,
)
from chat_sdk.adapters.telegram.format_converter import TelegramFormatConverter, escape_markdown_v2
from chat_sdk.adapters.telegram.types import (
    TelegramAdapterConfig,
)
from chat_sdk.shared.errors import (
    AdapterPermissionError,
    AdapterRateLimitError,
    AuthenticationError,
    NetworkError,
    ValidationError,
)
from chat_sdk.shared.mock_adapter import create_mock_state
from chat_sdk.thread import ThreadImpl, _ThreadImplConfig
from chat_sdk.types import Attachment, FetchOptions, FileUpload, Message, PostableMarkdown, PostableRaw

# =============================================================================
# Helpers
# =============================================================================

CHAT_ID = "-1001234567890"
THREAD_ID = f"telegram:{CHAT_ID}"
MESSAGE_ID_INT = 42
COMPOSITE_MESSAGE_ID = f"{CHAT_ID}:{MESSAGE_ID_INT}"


def _make_adapter(**overrides: Any) -> TelegramAdapter:
    """Create a TelegramAdapter with minimal valid config."""
    config = TelegramAdapterConfig(
        bot_token=overrides.pop("bot_token", "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"),
        **overrides,
    )
    return TelegramAdapter(config)


def _make_telegram_message(
    chat_id: str = CHAT_ID,
    message_id: int = MESSAGE_ID_INT,
    text: str = "Hello",
) -> dict[str, Any]:
    """Return a minimal Telegram message dict."""
    return {
        "message_id": message_id,
        "chat": {"id": int(chat_id), "type": "supergroup"},
        "from": {"id": 111, "is_bot": False, "first_name": "Alice"},
        "date": 1700000000,
        "text": text,
    }


def _init_adapter(adapter: TelegramAdapter) -> MagicMock:
    """Wire up a mock ChatInstance so dispatch methods work."""
    chat = MagicMock()
    chat.process_message = MagicMock()
    chat.process_action = MagicMock()
    chat.process_reaction = MagicMock()
    # handle_webhook claims each update_id before dispatch (vercel/chat#799).
    chat.get_state.return_value.set_if_not_exists = AsyncMock(return_value=True)
    adapter._chat = chat
    adapter._bot_user_id = "999"
    adapter._webhook_scope = hashlib.sha256(b"999").hexdigest()
    return chat


# =============================================================================
# Tests -- post_message
# =============================================================================


class TestPostMessageSendsText:
    """post_message with markdown routes through the native rich endpoint."""

    @pytest.mark.asyncio
    async def test_post_message_sends_text(self):
        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter.telegram_fetch = AsyncMock(return_value=_make_telegram_message(text="Hi"))

        result = await adapter.post_message(THREAD_ID, {"markdown": "Hi"})

        adapter.telegram_fetch.assert_called_once()
        call_args = adapter.telegram_fetch.call_args
        method = call_args[0][0]
        payload = call_args[0][1]

        # A ``{markdown}`` payload now goes through ``sendRichMessage`` with the
        # markdown carried verbatim in ``rich_message`` (vercel/chat#479).
        assert method == "sendRichMessage"
        assert payload["chat_id"] == CHAT_ID
        assert payload["rich_message"]["markdown"] == "Hi"
        assert result.id is not None


class TestPostMessageWithCard:
    """post_message with a card includes reply_markup with inline keyboard."""

    @pytest.mark.asyncio
    async def test_post_message_with_card(self):
        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter.telegram_fetch = AsyncMock(return_value=_make_telegram_message(text="Pick"))

        # Use the proper CardElement structure with Actions and Buttons
        card_msg = {
            "card": {
                "type": "card",
                "title": "Question",
                "children": [
                    {"type": "text", "content": "Pick one"},
                    {
                        "type": "actions",
                        "children": [
                            {"type": "button", "id": "yes", "label": "Yes"},
                            {"type": "button", "id": "no", "label": "No"},
                        ],
                    },
                ],
            }
        }

        await adapter.post_message(THREAD_ID, card_msg)

        adapter.telegram_fetch.assert_called_once()
        call_args = adapter.telegram_fetch.call_args
        method = call_args[0][0]
        payload = call_args[0][1]

        assert method == "sendMessage"
        # reply_markup should be present for the card buttons
        assert payload.get("reply_markup") is not None
        keyboard = payload["reply_markup"]["inline_keyboard"]
        assert len(keyboard) > 0
        # With a card, parse_mode should be MarkdownV2 (legacy "Markdown" was
        # deprecated by Telegram and rejected most LLM-generated text).
        assert payload.get("parse_mode") == "MarkdownV2"


class TestPostMessageParseMode:
    """post_message markdown: rich primary, MarkdownV2 on the regular fallback."""

    @pytest.mark.asyncio
    async def test_post_message_parse_mode_markdown(self):
        # Rich is the primary path: the markdown is sent through
        # ``sendRichMessage`` with no Bot-API ``parse_mode`` (the rich endpoint
        # parses markdown natively).
        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter.telegram_fetch = AsyncMock(return_value=_make_telegram_message(text="**bold**"))

        await adapter.post_message(THREAD_ID, {"markdown": "**bold**"})

        method = adapter.telegram_fetch.call_args[0][0]
        payload = adapter.telegram_fetch.call_args[0][1]
        assert method == "sendRichMessage"
        assert payload.get("parse_mode") is None

    @pytest.mark.asyncio
    async def test_post_message_parse_mode_markdown_on_regular_path(self):
        # When rich is unavailable the markdown falls to the regular
        # ``sendMessage`` path, which DOES carry ``parse_mode=MarkdownV2``.
        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter._rich_messages_available = False
        adapter.telegram_fetch = AsyncMock(return_value=_make_telegram_message(text="**bold**"))

        await adapter.post_message(THREAD_ID, {"markdown": "**bold**"})

        method = adapter.telegram_fetch.call_args[0][0]
        payload = adapter.telegram_fetch.call_args[0][1]
        assert method == "sendMessage"
        assert payload.get("parse_mode") == "MarkdownV2"


# =============================================================================
# Tests -- typed attachment uploads (vercel/chat#485)
# =============================================================================


def _form_fields(form_data: Any) -> dict[str, Any]:
    """Map a non-binary aiohttp.FormData field name -> string value."""
    fields: dict[str, Any] = {}
    for type_options, _headers, value in form_data._fields:
        if not isinstance(value, (bytes, bytearray, memoryview)):
            fields[type_options["name"]] = value
    return fields


def _form_binary_field(form_data: Any, name: str) -> tuple[Any, str | None, str | None]:
    """Return (value, filename, content_type) for a binary FormData field."""
    for type_options, headers, value in form_data._fields:
        if type_options["name"] == name:
            return value, type_options.get("filename"), headers.get("Content-Type")
    raise AssertionError(f"FormData field {name!r} not found")


class TestPostMessageTypedAttachmentUploads:
    """post_message routes typed attachments to the per-type Telegram method."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("attachment_type", "field", "method", "mime_type", "name"),
        [
            ("image", "photo", "sendPhoto", "image/png", "image.png"),
            ("audio", "audio", "sendAudio", "audio/mpeg", "track.mp3"),
            ("video", "video", "sendVideo", "video/mp4", "clip.mp4"),
            ("file", "document", "sendDocument", "application/pdf", "report.pdf"),
        ],
    )
    async def test_typed_attachment_selects_method(
        self,
        attachment_type: str,
        field: str,
        method: str,
        mime_type: str,
        name: str,
    ):
        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter.telegram_fetch = AsyncMock(return_value=_make_telegram_message())

        await adapter.post_message(
            THREAD_ID,
            PostableMarkdown(
                markdown="attached **media**",
                attachments=[
                    Attachment(
                        type=attachment_type,  # type: ignore[arg-type]
                        data=b"payload",
                        mime_type=mime_type,
                        name=name,
                        width=1280 if attachment_type == "video" else None,
                        height=720 if attachment_type == "video" else None,
                    )
                ],
            ),
        )

        called_method = adapter.telegram_fetch.call_args[0][0]
        form_data = adapter.telegram_fetch.call_args[0][1]
        assert called_method == method

        fields = _form_fields(form_data)
        assert fields["chat_id"] == CHAT_ID
        assert fields["caption"] == "attached *media*"
        assert fields["parse_mode"] == "MarkdownV2"

        value, filename, content_type = _form_binary_field(form_data, field)
        assert value == b"payload"
        assert filename == name
        assert content_type == mime_type

        if attachment_type == "video":
            assert fields["width"] == "1280"
            assert fields["height"] == "720"
        else:
            assert "width" not in fields
            assert "height" not in fields

    @pytest.mark.asyncio
    async def test_attachment_loaded_through_fetch_data(self):
        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter.telegram_fetch = AsyncMock(return_value=_make_telegram_message())

        fetch_data = AsyncMock(return_value=b"payload")

        await adapter.post_message(
            THREAD_ID,
            PostableRaw(
                raw="",
                attachments=[
                    Attachment(
                        type="file",
                        mime_type="application/pdf",
                        name="report.pdf",
                        fetch_data=fetch_data,
                    )
                ],
            ),
        )

        fetch_data.assert_awaited_once()
        method = adapter.telegram_fetch.call_args[0][0]
        form_data = adapter.telegram_fetch.call_args[0][1]
        assert method == "sendDocument"
        fields = _form_fields(form_data)
        # ``raw=""`` ships verbatim with no caption / parse_mode.
        assert "caption" not in fields
        assert "parse_mode" not in fields
        value, _filename, _content_type = _form_binary_field(form_data, "document")
        assert value == b"payload"

    @pytest.mark.asyncio
    async def test_url_only_attachment_uses_url_field(self):
        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter.telegram_fetch = AsyncMock(return_value=_make_telegram_message())

        await adapter.post_message(
            THREAD_ID,
            PostableMarkdown(
                markdown="public **image**",
                attachments=[
                    Attachment(
                        type="image",
                        mime_type="image/png",
                        name="image.png",
                        url="https://cdn.example.com/image.png",
                    )
                ],
            ),
        )

        method = adapter.telegram_fetch.call_args[0][0]
        payload = adapter.telegram_fetch.call_args[0][1]
        assert method == "sendPhoto"
        # URL-only attachments ship as a JSON dict, not multipart FormData.
        assert isinstance(payload, dict)
        assert payload["chat_id"] == CHAT_ID
        assert payload["photo"] == "https://cdn.example.com/image.png"
        assert payload["caption"] == "public *image*"
        assert payload["parse_mode"] == "MarkdownV2"

    @pytest.mark.asyncio
    async def test_url_only_video_attachment_includes_dimensions(self):
        # Edge case beyond the upstream URL test: video width/height ride
        # along on the JSON URL path too.
        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter.telegram_fetch = AsyncMock(return_value=_make_telegram_message())

        await adapter.post_message(
            THREAD_ID,
            PostableMarkdown(
                markdown="clip",
                attachments=[
                    Attachment(
                        type="video",
                        url="https://cdn.example.com/clip.mp4",
                        width=1280,
                        height=720,
                    )
                ],
            ),
        )

        method = adapter.telegram_fetch.call_args[0][0]
        payload = adapter.telegram_fetch.call_args[0][1]
        assert method == "sendVideo"
        assert payload["video"] == "https://cdn.example.com/clip.mp4"
        assert payload["width"] == 1280
        assert payload["height"] == 720

    @pytest.mark.asyncio
    async def test_rejects_mixed_files_and_attachments(self):
        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter.telegram_fetch = AsyncMock(return_value=_make_telegram_message())

        with pytest.raises(ValidationError, match="mixing file uploads and attachments"):
            await adapter.post_message(
                THREAD_ID,
                PostableRaw(
                    raw="mixed",
                    attachments=[Attachment(type="image", data=b"one")],
                    files=[FileUpload(data=b"two", filename="two.txt")],
                ),
            )

        adapter.telegram_fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_attachment_without_data_or_url(self):
        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter.telegram_fetch = AsyncMock(return_value=_make_telegram_message())

        with pytest.raises(ValidationError, match="Attachment data or URL required for image"):
            await adapter.post_message(
                THREAD_ID,
                PostableRaw(raw="", attachments=[Attachment(type="image")]),
            )

        adapter.telegram_fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_unsupported_attachment_type(self):
        """An attachment.type not in ATTACHMENT_UPLOADS raises a clear ValidationError.

        ``Attachment.type`` is a ``Literal``, but Python does not enforce it at
        runtime, so an untyped/dynamic caller can supply an out-of-set value.
        Without the guard the per-type dict lookup raises a bare ``KeyError``;
        we surface a ``ValidationError`` naming the bad type and the supported
        set instead. Load-bearing: reverting the guard makes this raise
        ``KeyError`` (not ``ValidationError``) and the test fails.
        """
        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter.telegram_fetch = AsyncMock(return_value=_make_telegram_message())

        with pytest.raises(ValidationError, match="Unsupported attachment type: sticker"):
            await adapter.post_message(
                THREAD_ID,
                PostableRaw(
                    raw="",
                    attachments=[Attachment(type="sticker", data=b"x")],  # type: ignore[arg-type]
                ),
            )

        adapter.telegram_fetch.assert_not_called()


# =============================================================================
# Tests -- outbound media groups (vercel/chat#605, #278)
# =============================================================================


def _album_part(message_id: int, chat_id: str = "123", **media: Any) -> dict[str, Any]:
    """A message Telegram returns from ``sendMediaGroup`` (``sampleMessage`` + media)."""
    return {
        "message_id": message_id,
        "chat": {"id": int(chat_id), "type": "private"},
        "from": {"id": 999, "is_bot": True, "first_name": "Bot", "username": "mybot"},
        "date": 1700000000,
        **media,
    }


def _media_group(form_data: Any) -> list[dict[str, Any]]:
    """``readMediaGroup``: the ``media`` field must be a JSON string."""
    media = _form_fields(form_data)["media"]
    assert isinstance(media, str)
    return json.loads(media)


def _form_field_names(form_data: Any) -> list[str]:
    return [type_options["name"] for type_options, _headers, _value in form_data._fields]


_TWO_FILES = [
    FileUpload(data=b"one", filename="one.txt", mime_type="text/plain"),
    FileUpload(data=b"two", filename="two.txt", mime_type="text/plain"),
]


class TestPostMessageMediaGroups:
    """2-10 files or attachments go out as one ``sendMediaGroup``."""

    @pytest.mark.asyncio
    async def test_posts_multiple_files_as_a_telegram_media_group(self):
        adapter = _make_adapter(user_name="mybot")
        _init_adapter(adapter)
        adapter.telegram_fetch = AsyncMock(
            return_value=[
                _album_part(
                    21,
                    media_group_id="group-1",
                    document={"file_id": "doc-1", "file_unique_id": "doc-unique-1", "file_name": "one.txt"},
                ),
                _album_part(
                    22,
                    media_group_id="group-1",
                    document={"file_id": "doc-2", "file_unique_id": "doc-unique-2", "file_name": "two.txt"},
                ),
            ]
        )

        posted = await adapter.post_message(
            "telegram:-100123:42",
            PostableMarkdown(markdown="attached **files**", files=list(_TWO_FILES)),
        )

        assert posted.id == "123:22"
        adapter.telegram_fetch.assert_awaited_once()
        method, form_data = adapter.telegram_fetch.await_args.args
        assert method == "sendMediaGroup"

        fields = _form_fields(form_data)
        assert fields["chat_id"] == "-100123"
        assert fields["message_thread_id"] == "42"
        assert _media_group(form_data) == [
            {
                "caption": "attached *files*",
                "media": "attach://media0",
                "parse_mode": "MarkdownV2",
                "type": "document",
            },
            {"media": "attach://media1", "type": "document"},
        ]
        assert _form_binary_field(form_data, "media0") == (b"one", "one.txt", "text/plain")
        assert _form_binary_field(form_data, "media1") == (b"two", "two.txt", "text/plain")
        # Every returned message is cached, not only the one returned.
        cached = await adapter.fetch_messages("telegram:123:42")
        assert [message.id for message in cached.messages] == ["123:21", "123:22"]

    @pytest.mark.asyncio
    async def test_posts_and_normalizes_mixed_image_and_video_attachments_as_a_telegram_media_group(self):
        adapter = _make_adapter(user_name="mybot")
        _init_adapter(adapter)
        adapter.telegram_fetch = AsyncMock(
            return_value=[
                _album_part(
                    31,
                    media_group_id="group-2",
                    photo=[{"file_id": "photo-1", "file_unique_id": "p1", "width": 100, "height": 100}],
                ),
                _album_part(
                    32,
                    media_group_id="group-2",
                    video={"file_id": "video-1", "file_unique_id": "v1", "width": 1280, "height": 720},
                ),
            ]
        )

        posted = await adapter.post_message(
            "telegram:123",
            PostableMarkdown(
                markdown="visual **album**",
                attachments=[
                    Attachment(
                        type="image",
                        mime_type="image/png",
                        name="image.png",
                        url="https://cdn.example.com/image.png",
                    ),
                    Attachment(
                        type="video",
                        data=b"video",
                        height=720,
                        mime_type="video/mp4",
                        name="video.mp4",
                        width=1280,
                    ),
                ],
            ),
        )

        assert posted.id == "123:32"
        method, form_data = adapter.telegram_fetch.await_args.args
        assert method == "sendMediaGroup"
        assert _media_group(form_data) == [
            {
                "caption": "visual *album*",
                "media": "https://cdn.example.com/image.png",
                "parse_mode": "MarkdownV2",
                "type": "photo",
            },
            {"height": 720, "media": "attach://media1", "type": "video", "width": 1280},
        ]
        assert "media0" not in _form_field_names(form_data)
        assert _form_binary_field(form_data, "media1") == (b"video", "video.mp4", "video/mp4")

        cached = await adapter.fetch_messages("telegram:123", FetchOptions(limit=10))
        round_tripped = [
            [
                adapter.rehydrate_attachment(attachment)
                for attachment in Message.from_json(json.loads(json.dumps(message.to_json()))).attachments
            ]
            for message in cached.messages
        ]
        assert [[(a.type, a.mime_type, a.fetch_metadata) for a in group] for group in round_tripped] == [
            [("image", "image/jpeg", {"fileId": "photo-1", "fileUniqueId": "p1"})],
            [("video", None, {"fileId": "video-1", "fileUniqueId": "v1"})],
        ]
        assert all(callable(a.fetch_data) for group in round_tripped for a in group)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "types",
        [
            pytest.param(("image", "file"), id="image-and-document"),
            # Audio is its own category: it may not join a photo/video album.
            pytest.param(("audio", "image"), id="audio-and-image"),
        ],
    )
    async def test_rejects_incompatible_telegram_media_group_attachment_types(self, types: tuple[str, str]):
        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter.telegram_fetch = AsyncMock(return_value=[])

        with pytest.raises(ValidationError, match="documents and audio files must be grouped only"):
            await adapter.post_message(
                "telegram:123",
                PostableRaw(
                    raw="attachments",
                    attachments=[Attachment(type=t, data=b"x") for t in types],  # type: ignore[arg-type]
                ),
            )

        adapter.telegram_fetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rejects_telegram_media_groups_with_more_than_10_files(self):
        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter.telegram_fetch = AsyncMock(return_value=[])

        with pytest.raises(ValidationError, match="Telegram media groups support 2-10 files"):
            await adapter.post_message(
                "telegram:123",
                PostableRaw(
                    raw="files",
                    files=[FileUpload(data=str(index).encode(), filename=f"{index}.txt") for index in range(11)],
                ),
            )

        adapter.telegram_fetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sends_ten_audio_attachments_as_one_media_group(self):
        # Edge case beyond upstream: the 10-item upper bound is inclusive and
        # a same-category (audio) album passes the type check.
        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter.telegram_fetch = AsyncMock(return_value=[_album_part(index) for index in range(1, 11)])

        posted = await adapter.post_message(
            "telegram:123",
            PostableRaw(raw="", attachments=[Attachment(type="audio", data=b"a") for _ in range(10)]),
        )

        assert posted.id == "123:10"
        method, form_data = adapter.telegram_fetch.await_args.args
        assert method == "sendMediaGroup"
        media = _media_group(form_data)
        assert [item["type"] for item in media] == ["audio"] * 10
        # ``raw=""`` carries no caption.
        assert "caption" not in media[0]
        assert _form_binary_field(form_data, "media9")[1] == "attachment-9"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["files", "attachments"])
    async def test_rejects_an_inline_keyboard_on_a_media_group(self, kind: str):
        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter.telegram_fetch = AsyncMock(return_value=[])
        fetch_data = AsyncMock(return_value=b"payload")
        uploads: dict[str, Any] = (
            {"files": list(_TWO_FILES)}
            if kind == "files"
            else {"attachments": [Attachment(type="image", fetch_data=fetch_data), Attachment(type="image", data=b"2")]}
        )
        card = {
            "type": "card",
            "title": "Pick",
            "children": [{"type": "actions", "children": [{"type": "button", "id": "yes", "label": "Yes"}]}],
        }

        with pytest.raises(ValidationError, match="Telegram media groups do not support inline keyboards"):
            await adapter.post_message("telegram:123", {"card": card, **uploads})

        # The keyboard is rejected before any attachment is downloaded.
        fetch_data.assert_not_awaited()
        adapter.telegram_fetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_retries_a_media_group_caption_as_plain_text_when_markdown_v2_is_rejected(self):
        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter.telegram_fetch = AsyncMock(
            side_effect=[
                ValidationError("telegram", "Bad Request: can't parse caption entities: Can't find end of the entity"),
                [_album_part(41), _album_part(42)],
            ]
        )

        posted = await adapter.post_message(
            "telegram:123",
            PostableMarkdown(markdown="attached **files**", files=list(_TWO_FILES)),
        )

        assert posted.id == "123:42"
        assert adapter.telegram_fetch.await_count == 2
        (first_method, first_form), (retry_method, retry_form) = [
            call.args for call in adapter.telegram_fetch.await_args_list
        ]
        assert first_method == retry_method == "sendMediaGroup"
        assert _media_group(first_form)[0]["parse_mode"] == "MarkdownV2"
        # A fresh multipart body (aiohttp FormData is single-use) with the
        # plain caption and no parse_mode.
        assert retry_form is not first_form
        assert _media_group(retry_form) == [
            {"caption": "attached files", "media": "attach://media0", "type": "document"},
            {"media": "attach://media1", "type": "document"},
        ]
        assert _form_binary_field(retry_form, "media1")[0] == b"two"

    @pytest.mark.asyncio
    async def test_uploads_fetch_data_bytes_in_a_media_group_instead_of_the_url(self):
        # Re-posting received attachments: they carry ``fetch_data`` (and maybe
        # a URL); the downloaded bytes win, as upstream ``data ? attach:// : url``.
        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter.telegram_fetch = AsyncMock(return_value=[_album_part(51), _album_part(52)])
        fetch_data = AsyncMock(return_value=b"fetched")

        await adapter.post_message(
            "telegram:123",
            PostableRaw(
                raw="",
                attachments=[
                    Attachment(
                        type="image",
                        url="https://cdn.example.com/image.png",
                        fetch_data=fetch_data,
                        mime_type="image/png",
                        name="image.png",
                    ),
                    Attachment(type="image", data=b"second"),
                ],
            ),
        )

        fetch_data.assert_awaited_once()
        _method, form_data = adapter.telegram_fetch.await_args.args
        assert [item["media"] for item in _media_group(form_data)] == ["attach://media0", "attach://media1"]
        assert _form_binary_field(form_data, "media0") == (b"fetched", "image.png", "image/png")

    @pytest.mark.asyncio
    async def test_media_group_sends_integral_dimensions_only_for_videos(self):
        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter.telegram_fetch = AsyncMock(return_value=[_album_part(61), _album_part(62)])

        await adapter.post_message(
            "telegram:123",
            PostableRaw(
                raw="",
                attachments=[
                    Attachment(type="image", data=b"photo", width=640, height=480),
                    # ``Number.isInteger``: 1280.0 is the integer 1280, ``True`` is not a number.
                    Attachment(type="video", data=b"video", width=1280.0, height=True),  # type: ignore[arg-type]
                ],
            ),
        )

        _method, form_data = adapter.telegram_fetch.await_args.args
        photo, video = _media_group(form_data)
        assert photo == {"media": "attach://media0", "type": "photo"}
        assert video == {"media": "attach://media1", "type": "video", "width": 1280}
        # JSON ``1280`` (int), not ``1280.0``.
        assert type(video["width"]) is int

    @pytest.mark.asyncio
    async def test_raises_network_error_when_send_media_group_returns_no_messages(self):
        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter.telegram_fetch = AsyncMock(return_value=[])

        with pytest.raises(NetworkError, match="Telegram postMessage did not return any sent messages"):
            await adapter.post_message("telegram:123", PostableRaw(raw="", files=list(_TWO_FILES)))

        adapter.telegram_fetch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rejects_an_unsupported_attachment_type_in_a_media_group_before_downloading(self):
        """Divergence from upstream: an out-of-set ``type`` raises ValidationError
        (not ``KeyError``) before any ``fetch_data`` download.

        ``Attachment.type`` is a ``Literal`` Python does not enforce; upstream's
        TS union rules the value out at compile time (docs/UPSTREAM_SYNC.md).
        """
        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter.telegram_fetch = AsyncMock(return_value=[])
        fetch_data = AsyncMock(return_value=b"payload")

        with pytest.raises(ValidationError, match="Unsupported attachment type: sticker"):
            await adapter.post_message(
                "telegram:123",
                PostableRaw(
                    raw="",
                    attachments=[
                        Attachment(type="image", fetch_data=fetch_data),
                        Attachment(type="sticker", data=b"x"),  # type: ignore[arg-type]
                    ],
                ),
            )

        fetch_data.assert_not_awaited()
        adapter.telegram_fetch.assert_not_awaited()


# =============================================================================
# Tests -- edit_message
# =============================================================================


class TestEditMessage:
    """edit_message calls editMessageText with the correct payload."""

    @pytest.mark.asyncio
    async def test_edit_message(self):
        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter.telegram_fetch = AsyncMock(return_value=_make_telegram_message(text="Updated"))

        await adapter.edit_message(
            THREAD_ID,
            COMPOSITE_MESSAGE_ID,
            {"markdown": "Updated"},
        )

        adapter.telegram_fetch.assert_called_once()
        method = adapter.telegram_fetch.call_args[0][0]
        payload = adapter.telegram_fetch.call_args[0][1]

        # A ``{markdown}`` edit routes through the native rich edit
        # (``editMessageText`` with ``rich_message``), not a MarkdownV2 text
        # edit (vercel/chat#479).
        assert method == "editMessageText"
        assert payload["chat_id"] == CHAT_ID
        assert payload["message_id"] == MESSAGE_ID_INT
        assert payload["rich_message"]["markdown"] == "Updated"


# =============================================================================
# Tests -- delete_message
# =============================================================================


class TestDeleteMessage:
    """delete_message calls deleteMessage with chat_id and message_id."""

    @pytest.mark.asyncio
    async def test_delete_message(self):
        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter.telegram_fetch = AsyncMock(return_value=True)

        await adapter.delete_message(THREAD_ID, COMPOSITE_MESSAGE_ID)

        adapter.telegram_fetch.assert_called_once()
        method = adapter.telegram_fetch.call_args[0][0]
        payload = adapter.telegram_fetch.call_args[0][1]

        assert method == "deleteMessage"
        assert payload["chat_id"] == CHAT_ID
        assert payload["message_id"] == MESSAGE_ID_INT


# =============================================================================
# Tests -- add_reaction / remove_reaction
# =============================================================================


class TestAddReaction:
    """add_reaction sends setMessageReaction with a reaction array."""

    @pytest.mark.asyncio
    async def test_add_reaction(self):
        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter.telegram_fetch = AsyncMock(return_value=True)

        await adapter.add_reaction(THREAD_ID, COMPOSITE_MESSAGE_ID, "thumbs_up")

        adapter.telegram_fetch.assert_called_once()
        method = adapter.telegram_fetch.call_args[0][0]
        payload = adapter.telegram_fetch.call_args[0][1]

        assert method == "setMessageReaction"
        assert payload["chat_id"] == CHAT_ID
        assert payload["message_id"] == MESSAGE_ID_INT
        # reaction should be a non-empty list
        assert isinstance(payload["reaction"], list)
        assert len(payload["reaction"]) == 1
        assert payload["reaction"][0]["type"] == "emoji"


class TestRemoveReaction:
    """remove_reaction sends setMessageReaction with an empty array."""

    @pytest.mark.asyncio
    async def test_remove_reaction(self):
        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter.telegram_fetch = AsyncMock(return_value=True)

        await adapter.remove_reaction(THREAD_ID, COMPOSITE_MESSAGE_ID, "thumbs_up")

        adapter.telegram_fetch.assert_called_once()
        method = adapter.telegram_fetch.call_args[0][0]
        payload = adapter.telegram_fetch.call_args[0][1]

        assert method == "setMessageReaction"
        assert payload["reaction"] == []


# =============================================================================
# Tests -- start_typing
# =============================================================================


class TestStartTyping:
    """start_typing sends sendChatAction with action=typing."""

    @pytest.mark.asyncio
    async def test_start_typing(self):
        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter.telegram_fetch = AsyncMock(return_value=True)

        await adapter.start_typing(THREAD_ID)

        adapter.telegram_fetch.assert_called_once()
        method = adapter.telegram_fetch.call_args[0][0]
        payload = adapter.telegram_fetch.call_args[0][1]

        assert method == "sendChatAction"
        assert payload["chat_id"] == CHAT_ID
        assert payload["action"] == "typing"


# =============================================================================
# Tests -- callback query dispatch
# =============================================================================


class TestCallbackQueryDispatch:
    """Callback query dispatches process_action with the correct action_id."""

    def test_callback_query_dispatch(self):
        adapter = _make_adapter()
        chat = _init_adapter(adapter)

        callback_query = {
            "id": "cq-001",
            "from": {"id": 111, "is_bot": False, "first_name": "Alice"},
            "message": {
                "message_id": 42,
                "chat": {"id": int(CHAT_ID), "type": "supergroup"},
                "date": 1700000000,
                "text": "Prompt",
            },
            "data": "approve",
        }

        adapter.handle_callback_query(callback_query)

        chat.process_action.assert_called_once()
        action_payload = chat.process_action.call_args[0][0]
        assert action_payload.action_id == "approve"
        assert action_payload.adapter is adapter


# =============================================================================
# Tests -- reaction update dispatch
# =============================================================================


class TestReactionUpdateDispatch:
    """Reaction updates dispatch process_reaction for added AND removed."""

    def test_reaction_update_dispatch(self):
        adapter = _make_adapter()
        chat = _init_adapter(adapter)

        reaction_update = {
            "chat": {"id": int(CHAT_ID), "type": "supergroup"},
            "message_id": MESSAGE_ID_INT,
            "date": 1700000000,
            "old_reaction": [{"type": "emoji", "emoji": "\ud83d\udc4d"}],
            "new_reaction": [{"type": "emoji", "emoji": "\u2764\ufe0f"}],
        }

        adapter.handle_message_reaction_update(reaction_update)

        # Should fire twice: once for the added reaction, once for the removed
        assert chat.process_reaction.call_count == 2

        calls = chat.process_reaction.call_args_list
        # One should be added=True, one added=False
        added_flags = {c[0][0].added for c in calls}
        assert added_flags == {True, False}


# =============================================================================
# Tests -- error mapping
# =============================================================================


class TestErrorMapping401:
    """401 response maps to AuthenticationError."""

    def test_error_mapping_401(self):
        adapter = _make_adapter()
        with pytest.raises(AuthenticationError):
            adapter.throw_telegram_api_error(
                "getMe",
                401,
                {"ok": False, "error_code": 401, "description": "Unauthorized"},
            )


class TestErrorMapping429:
    """429 response maps to AdapterRateLimitError."""

    def test_error_mapping_429(self):
        adapter = _make_adapter()
        with pytest.raises(AdapterRateLimitError):
            adapter.throw_telegram_api_error(
                "sendMessage",
                429,
                {
                    "ok": False,
                    "error_code": 429,
                    "description": "Too Many Requests",
                    "parameters": {"retry_after": 30},
                },
            )


class TestErrorMapping403:
    """403 response maps to PermissionError."""

    def test_error_mapping_403(self):
        adapter = _make_adapter()
        with pytest.raises(AdapterPermissionError):
            adapter.throw_telegram_api_error(
                "sendMessage",
                403,
                {"ok": False, "error_code": 403, "description": "Forbidden"},
            )


# =============================================================================
# Tests -- fetch_thread
# =============================================================================


class TestFetchThread:
    """fetch_thread calls getChat and returns ThreadInfo."""

    @pytest.mark.asyncio
    async def test_fetch_thread(self):
        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter.telegram_fetch = AsyncMock(
            return_value={
                "id": int(CHAT_ID),
                "type": "supergroup",
                "title": "My Group",
            }
        )

        info = await adapter.fetch_thread(THREAD_ID)

        adapter.telegram_fetch.assert_called_once()
        method = adapter.telegram_fetch.call_args[0][0]
        payload = adapter.telegram_fetch.call_args[0][1]

        assert method == "getChat"
        assert payload["chat_id"] == CHAT_ID
        assert info.channel_name == "My Group"
        assert info.is_dm is False


# =============================================================================
# Tests -- fetch_channel_info
# =============================================================================


class TestFetchChannelInfo:
    """fetch_channel_info calls getChat + getChatMemberCount."""

    @pytest.mark.asyncio
    async def test_fetch_channel_info(self):
        adapter = _make_adapter()
        _init_adapter(adapter)

        # telegram_fetch is called twice: first getChat, then getChatMemberCount
        adapter.telegram_fetch = AsyncMock(
            side_effect=[
                {
                    "id": int(CHAT_ID),
                    "type": "supergroup",
                    "title": "Dev Team",
                },
                150,  # member count
            ]
        )

        info = await adapter.fetch_channel_info(CHAT_ID)

        assert adapter.telegram_fetch.call_count == 2
        first_call = adapter.telegram_fetch.call_args_list[0]
        second_call = adapter.telegram_fetch.call_args_list[1]

        assert first_call[0][0] == "getChat"
        assert second_call[0][0] == "getChatMemberCount"
        assert info.name == "Dev Team"
        assert info.member_count == 150
        assert info.is_dm is False


# =============================================================================
# Tests -- fetch_channel_info member count failure
# =============================================================================


class TestFetchChannelInfoMemberCountFails:
    """getChatMemberCount failure is swallowed and member_count is None."""

    @pytest.mark.asyncio
    async def test_member_count_failure(self):
        adapter = _make_adapter()
        _init_adapter(adapter)

        adapter.telegram_fetch = AsyncMock(
            side_effect=[
                {"id": int(CHAT_ID), "type": "supergroup", "title": "Grp"},
                Exception("count failed"),
            ]
        )

        info = await adapter.fetch_channel_info(CHAT_ID)
        assert info.name == "Grp"
        assert info.member_count is None


# =============================================================================
# Tests -- handle_webhook
# =============================================================================


class TestHandleWebhook:
    """Webhook handling including secret token verification."""

    @pytest.mark.asyncio
    async def test_webhook_rejects_invalid_secret(self):
        adapter = _make_adapter(secret_token="correct-secret")
        _init_adapter(adapter)

        class FakeReq:
            headers = {"x-telegram-bot-api-secret-token": "wrong-secret"}

            async def text(self):
                return '{"update_id": 1}'

        result = await adapter.handle_webhook(FakeReq())
        assert result["status"] == 401

    @pytest.mark.asyncio
    async def test_webhook_accepts_valid_secret(self):
        adapter = _make_adapter(secret_token="my-secret")
        chat = _init_adapter(adapter)
        # A private message also fires a typing chat action; keep it offline.
        adapter.telegram_fetch = AsyncMock(return_value=True)  # type: ignore[method-assign]

        class FakeReq:
            headers = {"x-telegram-bot-api-secret-token": "my-secret"}

            async def text(self):
                return (
                    '{"update_id": 1, "message": {"message_id": 1,'
                    ' "chat": {"id": 123, "type": "private"},'
                    ' "from": {"id": 111, "is_bot": false, "first_name": "A"},'
                    ' "date": 1700000000, "text": "hi"}}'
                )

        result = await adapter.handle_webhook(FakeReq())
        assert result["status"] == 200
        chat.process_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_webhook_rejects_before_reading_body_without_verification(self, monkeypatch: pytest.MonkeyPatch):
        # Fail closed (vercel/chat#858): with neither a secret nor the explicit
        # opt-out, the request is rejected before its body is even read. Clear
        # the env fallbacks so an exported opt-out/secret cannot mask this.
        monkeypatch.delenv("TELEGRAM_ALLOW_UNVERIFIED_WEBHOOKS", raising=False)
        monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET_TOKEN", raising=False)
        adapter = _make_adapter()  # auto mode, no secret_token, no opt-out
        chat = _init_adapter(adapter)
        read_body = AsyncMock(return_value='{"update_id": 1}')

        class FakeReq:
            headers = {}
            text = read_body

        result = await adapter.handle_webhook(FakeReq())
        assert result["status"] == 401
        assert result["body"] == "Webhook verification required"
        read_body.assert_not_awaited()
        chat.process_message.assert_not_called()
        chat.get_state.return_value.set_if_not_exists.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_webhook_invalid_json(self):
        adapter = _make_adapter(allow_unverified_webhooks=True)
        _init_adapter(adapter)

        class FakeReq:
            headers = {}

            async def text(self):
                return "not-json"

        result = await adapter.handle_webhook(FakeReq())
        assert result["status"] == 400

    @pytest.mark.asyncio
    async def test_webhook_no_chat_instance(self):
        adapter = _make_adapter(allow_unverified_webhooks=True)
        # _chat is None (not initialized)

        class FakeReq:
            headers = {}

            async def text(self):
                return '{"update_id": 1}'

        result = await adapter.handle_webhook(FakeReq())
        assert result["status"] == 200


# =============================================================================
# Tests -- process_update dispatch
# =============================================================================


class TestProcessUpdateDispatch:
    """process_update dispatches to correct handlers."""

    def test_dispatches_edited_message(self):
        adapter = _make_adapter()
        chat = _init_adapter(adapter)

        update = {
            "update_id": 1,
            "edited_message": _make_telegram_message(text="edited"),
        }
        adapter.process_update(update)
        assert chat.process_message.call_count == 1

    def test_dispatches_channel_post(self):
        adapter = _make_adapter()
        chat = _init_adapter(adapter)

        update = {
            "update_id": 1,
            "channel_post": _make_telegram_message(text="channel"),
        }
        adapter.process_update(update)
        assert chat.process_message.call_count == 1

    def test_dispatches_reaction(self):
        adapter = _make_adapter()
        chat = _init_adapter(adapter)

        update = {
            "update_id": 1,
            "message_reaction": {
                "chat": {"id": int(CHAT_ID), "type": "supergroup"},
                "message_id": 42,
                "date": 1700000000,
                "old_reaction": [],
                "new_reaction": [{"type": "emoji", "emoji": "\ud83d\udc4d"}],
            },
        }
        adapter.process_update(update)
        assert chat.process_reaction.call_count == 1

    def test_handle_incoming_message_no_chat(self):
        adapter = _make_adapter()
        # When _chat is None, handle_incoming_message_update returns early without error
        result = adapter.handle_incoming_message_update(_make_telegram_message())
        assert result is None
        assert adapter._chat is None


# =============================================================================
# Tests -- edit_message inline result (True)
# =============================================================================


class TestEditMessageInlineResult:
    """When Telegram returns True for inline message edits."""

    @pytest.mark.asyncio
    async def test_edit_inline_message_with_cache(self):
        adapter = _make_adapter()
        _init_adapter(adapter)

        # First post a message to populate cache
        adapter.telegram_fetch = AsyncMock(return_value=_make_telegram_message(text="original"))
        await adapter.post_message(THREAD_ID, {"markdown": "original"})

        # Now edit - Telegram returns True for inline edits
        adapter.telegram_fetch = AsyncMock(return_value=True)

        result = await adapter.edit_message(
            THREAD_ID,
            COMPOSITE_MESSAGE_ID,
            {"markdown": "Updated"},
        )
        assert result.id == COMPOSITE_MESSAGE_ID

    @pytest.mark.asyncio
    async def test_edit_inline_no_cache_raises(self):
        from chat_sdk.errors import ChatNotImplementedError

        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter.telegram_fetch = AsyncMock(return_value=True)

        with pytest.raises(ChatNotImplementedError):
            await adapter.edit_message(
                THREAD_ID,
                COMPOSITE_MESSAGE_ID,
                {"markdown": "fail"},
            )


# =============================================================================
# Tests -- edit_message empty text raises
# =============================================================================


class TestEditMessageEmpty:
    """Editing with empty text raises ValidationError."""

    @pytest.mark.asyncio
    async def test_edit_empty_text_raises(self):
        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter.telegram_fetch = AsyncMock()

        with pytest.raises(ValidationError):
            await adapter.edit_message(
                THREAD_ID,
                COMPOSITE_MESSAGE_ID,
                {"markdown": ""},
            )


# =============================================================================
# Tests -- post_message empty text raises
# =============================================================================


class TestPostMessageEmptyText:
    """Posting with empty text raises ValidationError."""

    @pytest.mark.asyncio
    async def test_post_empty_text_raises(self):
        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter.telegram_fetch = AsyncMock()

        with pytest.raises(ValidationError):
            await adapter.post_message(THREAD_ID, {"markdown": ""})


# =============================================================================
# Tests -- to_telegram_reaction
# =============================================================================


class TestToTelegramReaction:
    """to_telegram_reaction handles different emoji input types."""

    def test_emoji_value_input(self):
        from chat_sdk.types import EmojiValue

        adapter = _make_adapter()
        result = adapter.to_telegram_reaction(EmojiValue(name="thumbs_up"))
        assert result["type"] == "emoji"

    def test_custom_emoji_prefix(self):
        adapter = _make_adapter()
        result = adapter.to_telegram_reaction("custom:12345")
        assert result["type"] == "custom_emoji"
        assert result["custom_emoji_id"] == "12345"

    def test_emoji_placeholder(self):
        adapter = _make_adapter()
        result = adapter.to_telegram_reaction("{{emoji:thumbs_up}}")
        assert result["type"] == "emoji"

    def test_emoji_name_string(self):
        adapter = _make_adapter()
        result = adapter.to_telegram_reaction("thumbs_up")
        assert result["type"] == "emoji"

    def test_raw_emoji_passthrough(self):
        adapter = _make_adapter()
        result = adapter.to_telegram_reaction("\ud83d\ude00")  # grinning face
        assert result["type"] == "emoji"
        assert result["emoji"] == "\ud83d\ude00"


# =============================================================================
# Tests -- reaction_key / reaction_to_emoji_value
# =============================================================================


class TestReactionHelpers:
    def test_reaction_key_emoji(self):
        adapter = _make_adapter()
        assert adapter.reaction_key({"type": "emoji", "emoji": "\ud83d\udc4d"}) == "\ud83d\udc4d"

    def test_reaction_key_custom(self):
        adapter = _make_adapter()
        result = adapter.reaction_key({"type": "custom_emoji", "custom_emoji_id": "123"})
        assert result == "custom:123"

    def test_reaction_to_emoji_value_emoji(self):
        adapter = _make_adapter()
        result = adapter.reaction_to_emoji_value({"type": "emoji", "emoji": "\ud83d\udc4d"})
        assert result.name == "\ud83d\udc4d"

    def test_reaction_to_emoji_value_custom(self):
        adapter = _make_adapter()
        result = adapter.reaction_to_emoji_value({"type": "custom_emoji", "custom_emoji_id": "456"})
        assert result.name == "custom:456"


# =============================================================================
# Tests -- parse_telegram_message author variants
# =============================================================================


class TestParseTelegramMessageAuthors:
    """parse_telegram_message handles different author source fields."""

    def test_sender_chat_author(self):
        adapter = _make_adapter()
        _init_adapter(adapter)

        msg = {
            "message_id": 1,
            "chat": {"id": 123, "type": "supergroup"},
            "sender_chat": {"id": 456, "type": "channel", "title": "News"},
            "date": 1700000000,
            "text": "Channel post",
        }
        result = adapter.parse_telegram_message(msg, THREAD_ID)
        assert result.author.user_id == "chat:456"

    def test_fallback_author(self):
        adapter = _make_adapter()
        _init_adapter(adapter)

        msg = {
            "message_id": 1,
            "chat": {"id": 789, "type": "supergroup", "title": "Group"},
            "date": 1700000000,
            "text": "Anonymous",
        }
        result = adapter.parse_telegram_message(msg, THREAD_ID)
        assert result.author.user_id == "789"
        assert result.author.user_name == "Group"


# =============================================================================
# Tests -- is_bot_mentioned
# =============================================================================


class TestIsBotMentioned:
    """Bot mention detection covers various entity types."""

    def test_mention_entity(self):
        adapter = _make_adapter(user_name="testbot")
        _init_adapter(adapter)

        msg = {
            "message_id": 1,
            "chat": {"id": 123, "type": "supergroup"},
            "from": {"id": 111, "is_bot": False, "first_name": "A"},
            "date": 1700000000,
            "text": "@testbot hello",
            "entities": [{"type": "mention", "offset": 0, "length": 8}],
        }
        assert adapter.is_bot_mentioned(msg, "@testbot hello")

    def test_text_mention_entity(self):
        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter._bot_user_id = "999"

        msg = {
            "message_id": 1,
            "chat": {"id": 123, "type": "supergroup"},
            "from": {"id": 111, "is_bot": False, "first_name": "A"},
            "date": 1700000000,
            "text": "hello bot",
            "entities": [
                {"type": "text_mention", "offset": 6, "length": 3, "user": {"id": 999}},
            ],
        }
        assert adapter.is_bot_mentioned(msg, "hello bot")

    def test_bot_command_with_mention(self):
        adapter = _make_adapter(user_name="mybot")
        _init_adapter(adapter)

        msg = {
            "message_id": 1,
            "chat": {"id": 123, "type": "supergroup"},
            "from": {"id": 111, "is_bot": False, "first_name": "A"},
            "date": 1700000000,
            "text": "/start@mybot",
            "entities": [{"type": "bot_command", "offset": 0, "length": 12}],
        }
        assert adapter.is_bot_mentioned(msg, "/start@mybot")

    def test_regex_mention_fallback(self):
        adapter = _make_adapter(user_name="fallbot")
        _init_adapter(adapter)

        msg = {
            "message_id": 1,
            "chat": {"id": 123, "type": "supergroup"},
            "date": 1700000000,
            "text": "hey @fallbot check this",
        }
        assert adapter.is_bot_mentioned(msg, "hey @fallbot check this")

    def test_no_mention(self):
        adapter = _make_adapter(user_name="mybot")
        _init_adapter(adapter)

        msg = {
            "message_id": 1,
            "chat": {"id": 123, "type": "supergroup"},
            "date": 1700000000,
            "text": "hello world",
        }
        assert not adapter.is_bot_mentioned(msg, "hello world")

    def test_empty_text(self):
        adapter = _make_adapter()
        _init_adapter(adapter)
        msg = {"message_id": 1, "chat": {"id": 123}, "date": 1700000000, "text": ""}
        assert not adapter.is_bot_mentioned(msg, "")


# =============================================================================
# Tests -- resolve_parse_mode
# =============================================================================


class TestResolveParseMode:
    def test_card_returns_markdown_v2(self):
        adapter = _make_adapter()
        assert adapter.resolve_parse_mode({}, {"type": "card"}) == "MarkdownV2"

    def test_markdown_key_returns_markdown_v2(self):
        adapter = _make_adapter()
        assert adapter.resolve_parse_mode({"markdown": "**bold**"}, None) == "MarkdownV2"

    def test_plain_string_returns_none(self):
        # Plain str messages ship verbatim — no parse mode.
        adapter = _make_adapter()
        assert adapter.resolve_parse_mode("hello", None) is None

    def test_raw_dict_returns_none(self):
        # `{"raw": ...}` ships verbatim — no parse mode.
        adapter = _make_adapter()
        assert adapter.resolve_parse_mode({"raw": "verbatim"}, None) is None

    def test_ast_returns_markdown_v2(self):
        # `{ast}` shapes go through the format converter (which emits MarkdownV2).
        adapter = _make_adapter()
        ast_msg = {"ast": {"type": "root", "children": []}}
        assert adapter.resolve_parse_mode(ast_msg, None) == "MarkdownV2"


# =============================================================================
# Tests -- truncate_message / truncate_caption
# =============================================================================


class TestTruncation:
    def test_truncate_short_message(self):
        adapter = _make_adapter()
        assert adapter.truncate_message("short") == "short"

    def test_truncate_long_message(self):
        adapter = _make_adapter()
        long_text = "x" * 5000
        result = adapter.truncate_message(long_text)
        assert len(result) <= 4096

    def test_truncate_caption(self):
        adapter = _make_adapter()
        long_text = "x" * 2000
        result = adapter.truncate_caption(long_text)
        assert len(result) <= 1024


# =============================================================================
# Tests -- MarkdownV2-safe truncation (port of markdown.test.ts @ chat@4.41.1)
#
# Background: legacy ``slice + "..."`` truncation produces invalid MarkdownV2
# because (1) ``.`` is a reserved character that must be escaped as ``\.``,
# (2) the slice can leave a trailing orphan ``\`` that escapes the
# appended ellipsis or nothing, (3) the slice can cut through a paired
# entity (``*bold*``, `` `code` ``, ``[label](url)``) leaving it
# unclosed. Telegram rejects all three with
# ``Bad Request: can't parse entities``.
#
# Since vercel/chat#915 text that fits the limit is returned unchanged (the
# chat#446 under-limit trim is gone) and the over-limit trimmer is a single
# ``_scan_delimiters`` pass that skips code, escapes and link URLs.
# =============================================================================


_ESCAPED_ELLIPSIS_PATTERN = "\\.\\.\\."


class TestTruncateForTelegram:
    """describe("truncateForTelegram")."""

    def test_returns_text_unchanged_when_under_limit(self):
        """Plain text under the limit is returned verbatim."""
        assert truncate_for_telegram("hello", 100, "plain") == "hello"

    # it("returns MarkdownV2 that fits the limit unchanged, even with unpaired
    #     markers") -- replaces the chat#446 "streaming leak" test, which
    # trimmed the unpaired ``_`` under the limit.
    def test_returns_markdown_v2_that_fits_the_limit_unchanged_even_with_unpaired_markers(self):
        text = "Hello *world* _italic and bold *bold*"
        assert truncate_for_telegram(text, 4096, "MarkdownV2") == text

    def test_truncates_plain_text_with_literal_ellipsis(self):
        """Plain text over the limit is sliced and gets ``...``."""
        result = truncate_for_telegram("a" * 200, 100, "plain")
        assert len(result) == 100
        assert result.endswith("...")

    def test_truncates_markdown_v2_with_escaped_ellipsis(self):
        """MarkdownV2 over the limit gets ``\\.\\.\\.`` (escaped)."""
        result = truncate_for_telegram("a" * 200, 100, "MarkdownV2")
        assert len(result) <= 100
        assert result.endswith(_ESCAPED_ELLIPSIS_PATTERN)

    def test_strips_orphan_backslash_before_ellipsis(self):
        """A slice ending with a single ``\\`` gets the backslash dropped.

        Without this, the orphan ``\\`` would escape the first ``\\`` of the
        ellipsis and Telegram would reject the message.
        """
        text = ("a" * 90) + "\\" + ("b" * 50)
        result = truncate_for_telegram(text, 100, "MarkdownV2")
        before_ellipsis = result.removesuffix(_ESCAPED_ELLIPSIS_PATTERN)
        assert not ends_with_orphan_backslash(before_ellipsis)
        assert result.endswith(_ESCAPED_ELLIPSIS_PATTERN)

    def test_handles_input_that_is_all_special_chars(self):
        """``escape_markdown_v2("." * 200)`` truncates without crashing.

        Each ``.`` becomes ``\\.``; the truncator must not leave a trailing
        orphan ``\\`` from cutting between ``\\`` and ``.``.
        """
        rendered = escape_markdown_v2("." * 200)
        result = truncate_for_telegram(rendered, 100, "MarkdownV2")
        assert len(result) <= 100
        assert result.endswith(_ESCAPED_ELLIPSIS_PATTERN)
        before_ellipsis = result.removesuffix(_ESCAPED_ELLIPSIS_PATTERN)
        assert not ends_with_orphan_backslash(before_ellipsis)

    def test_does_not_modify_plain_parse_mode_messages(self):
        """``parse_mode="plain"`` returns the input verbatim."""
        text = "Hello *world* _unclosed"
        assert truncate_for_telegram(text, 4096, "plain") == text

    # it("trims back to before the [ when hard-truncated inside a link URL")
    def test_trims_back_to_before_the_bracket_when_hard_truncated_inside_a_link_url(self):
        prefix = "a" * 80
        text = f"{prefix}[link](https://example.com/{'x' * 100})"
        assert truncate_for_telegram(text, 100, "MarkdownV2") == f"{prefix}{_ESCAPED_ELLIPSIS_PATTERN}"

    # it("trims at the orphan * when hard-truncated inside bold")
    def test_trims_at_the_orphan_star_when_hard_truncated_inside_bold(self):
        text = ("a" * 80) + "*" + ("b" * 100)
        assert truncate_for_telegram(text, 100, "MarkdownV2") == ("a" * 80) + _ESCAPED_ELLIPSIS_PATTERN

    # it("trims at the __ opener when hard-truncated inside underline")
    def test_trims_at_the_double_underscore_opener_when_hard_truncated_inside_underline(self):
        text = f"__b__ rest {'z' * 100}"
        assert truncate_for_telegram(text, 9, "MarkdownV2") == _ESCAPED_ELLIPSIS_PATTERN
        assert truncate_for_telegram(text, 11, "MarkdownV2") == f"__b__{_ESCAPED_ELLIPSIS_PATTERN}"

    def test_strips_unclosed_code_when_backtick_crosses_limit(self):
        """A `` ` `` that opens an inline-code span whose closer is past the cut."""
        text = ("a" * 80) + "`" + ("b" * 100)
        assert truncate_for_telegram(text, 100, "MarkdownV2") == ("a" * 80) + _ESCAPED_ELLIPSIS_PATTERN

    def test_strips_unmatched_open_bracket_when_link_label_crosses_limit(self):
        """An unmatched ``[`` from a link whose ``]`` is past the cut."""
        text = ("a" * 80) + "[label" + ("b" * 100)
        assert truncate_for_telegram(text, 100, "MarkdownV2") == ("a" * 80) + _ESCAPED_ELLIPSIS_PATTERN

    # it("keeps a rendered link whose URL has odd underscores when the cut
    #     lands after it")
    def test_keeps_a_rendered_link_whose_url_has_odd_underscores_when_the_cut_lands_after_it(self):
        rendered = TelegramFormatConverter().render_postable(
            {
                "markdown": "body text\n\n[Read more](https://example.com/page?first_param=a&second_param=b&third_param=c)"
            }
        )
        text = f"{rendered} {'z' * 100}"
        result = truncate_for_telegram(text, len(rendered) + 6, "MarkdownV2")
        assert result == f"{rendered}{_ESCAPED_ELLIPSIS_PATTERN}"
        assert "&third_param=c)" in result

    # it("retreats before a partial link at every destination cut")
    def test_retreats_before_a_partial_link_at_every_destination_cut(self):
        prefix = "before "
        link = "[x\\]](https://example.com/a\\)`b\\\\)"
        text = f"{prefix}{link} after {'z' * 100}"
        for cut in range(len(prefix), len(prefix) + len(link) + 2):
            expected = prefix if cut < len(prefix) + len(link) else text[:cut]
            result = truncate_for_telegram(text, cut + 6, "MarkdownV2")
            assert result == f"{expected}{_ESCAPED_ELLIPSIS_PATTERN}", f"cut at {cut}"
            assert not ends_with_orphan_backslash(result)

    # it.each(["`code`", "```js\ncode\n```"])("retreats before code when
    #     cutting through its body or delimiters: %s")
    @pytest.mark.parametrize("code", ["`code`", "```js\ncode\n```"])
    def test_retreats_before_code_when_cutting_through_its_body_or_delimiters(self, code: str):
        prefix = "[x](https://example.com/a`b) "
        text = f"{prefix}{code} {'z' * 100}"
        for cut in range(1, len(code)):
            result = truncate_for_telegram(text, len(prefix) + cut + 6, "MarkdownV2")
            assert result == f"{prefix}{_ESCAPED_ELLIPSIS_PATTERN}", f"cut at {cut}"


class TestTrimToMarkdownV2SafeBoundary:
    """describe("trimToMarkdownV2SafeBoundary")."""

    @pytest.mark.parametrize(
        "text",
        [
            "*bold* _italic_ ~strike~ `code`",
            "__underline__ and _italic_",
            "```python\nprint(*args, **kwargs)\n```",
            "Result: `` done",
            "``x`` done",
            "`` x\n\n```\ncode\n```",
            "[a] b",
            "see [ref] and *bold*",
            "*b* [x](u) [y] z",
        ],
    )
    def test_leaves_balanced_markdown_v2_unchanged(self, text: str):
        assert _trim_to_markdown_v2_safe_boundary(text) == text

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Hello *world* _italic and bold *bold*", "Hello *world* "),
            ("_oops [x](https://e.co/?a_b=1)", ""),
            ("__b", ""),
            ("_a_ __b__ _c", "_a_ __b__ "),
            ("before [x]", "before "),
            ("before [x](https://e", "before "),
            ("a `", "a "),
            ("a ``", "a "),
            ("a ```js\ncode", "a "),
        ],
    )
    def test_drops_the_unpaired_tail_of(self, text: str, expected: str):
        assert _trim_to_markdown_v2_safe_boundary(text) == expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("before \\`literal", "before \\`literal"),
            ("before \\\\`unfinished", "before \\\\"),
            ("`a\\`b`", "`a\\`b`"),
            ("```\na\\`b\n```", "```\na\\`b\n```"),
            ("`a``b`", "`a``b`"),
            ("\\*a*", "\\*a"),
            ("\\\\*a", "\\\\"),
            ("\\_a_", "\\_a"),
            ("\\\\_a", "\\\\"),
            ("\\~a~", "\\~a"),
            ("\\\\~a", "\\\\"),
        ],
    )
    def test_respects_escape_parity_around_delimiters(self, text: str, expected: str):
        assert _trim_to_markdown_v2_safe_boundary(text) == expected

    # Python-only: an unpaired ``_`` after a link whose URL holds ``_`` is
    # still trimmed; the URL skip masks only the ``(...)`` part.
    def test_trims_an_unpaired_underscore_after_a_link_with_an_underscore_url(self):
        text = "[x](https://example.com/foo_bar) _unclosed"
        assert _trim_to_markdown_v2_safe_boundary(text) == "[x](https://example.com/foo_bar) "

    # Python-only: a link with ``_`` in its URL followed by a balanced
    # ``_italic_`` keeps both.
    def test_keeps_a_link_with_an_underscore_url_followed_by_balanced_italic(self):
        text = "[x](https://example.com/foo_bar) _italic_"
        assert _trim_to_markdown_v2_safe_boundary(text) == text

    def test_ignores_link_syntax_inside_inline_code(self):
        """``[`` inside an inline code span is literal text."""
        text = "`[label](https://nope`"
        assert _trim_to_markdown_v2_safe_boundary(text) == text


class TestTrimLinkUrlsWithRawEntityMarkers:
    """describe("link URLs with raw entity-marker characters")."""

    # it.each(["`", "```"])("preserves %s inside a link destination")
    @pytest.mark.parametrize("ticks", ["`", "```"])
    def test_preserves_ticks_inside_a_link_destination(self, ticks: str):
        text = f"before [x](https://example.com/a{ticks}b_*~) after"
        assert _trim_to_markdown_v2_safe_boundary(text) == text

    # it("preserves an explicit Markdown link with a backtick destination")
    def test_preserves_an_explicit_markdown_link_with_a_backtick_destination(self):
        rendered = TelegramFormatConverter().render_postable({"markdown": "[x](https://example.com/a`b)"})
        assert rendered == "[x](https://example.com/a`b)"
        assert _trim_to_markdown_v2_safe_boundary(rendered) == rendered

    # it("preserves an autolinked bare URL containing a backtick"). The Python
    # Markdown parser has no GFM autolink literals (CLAUDE.md Known
    # Limitations), so the bare URL is not rendered as a link here; the
    # trimmer is checked against the string upstream's renderer produces.
    def test_preserves_an_autolinked_bare_url_containing_a_backtick(self):
        rendered = "before [https://example\\.com/a\\`b](https://example.com/a`b) after"
        assert _trim_to_markdown_v2_safe_boundary(rendered) == rendered

    @pytest.mark.parametrize(
        "text",
        [
            "`before` [x](https://example.com/a`b) `after`",
            "```js\nbefore\n```\n[x](https://example.com/a`b)\n```js\nafter\n```",
            "before [x\\]](https://example.com/a\\)`b\\\\) after",
            "before [x](https://example.com/a\\`b) after",
            "before [x](https://example.com/a\\\\`b) after",
            "`[x](https://example.com/a\\`b)`",
            "```\n[x](https://example.com/a\\`b)\n```",
        ],
    )
    def test_preserves_links_alongside_code_and_escaped_delimiters(self, text: str):
        assert _trim_to_markdown_v2_safe_boundary(text) == text

    @pytest.mark.parametrize("code", ["`unfinished", "```js\nunfinished"])
    def test_does_not_let_url_backticks_balance_unfinished_code(self, code: str):
        prefix = "before [x](https://example.com/a`b) "
        assert _trim_to_markdown_v2_safe_boundary(prefix + code) == prefix

    @pytest.mark.parametrize(
        "text",
        [
            "[x](https://e.co/?a_b=1)",
            "[x](https://e.co/?a_b=1&c_d=2&e_f=3)",
            "text *bold* [x](https://e.co/?a_b=1)",
            "[x](https://e.co/?glob=*.ts&home=~user)",
        ],
    )
    def test_preserves_a_link_whose_url_contains_entity_markers(self, text: str):
        assert _trim_to_markdown_v2_safe_boundary(text) == text


class TestTruncateForTelegramUtf16:
    """Length limits are measured in UTF-16 code units per Telegram's
    documented caps. Non-BMP characters (emoji and other astral code
    points) consume 2 UTF-16 code units each; using Python's ``len()``
    (which counts codepoints) lets emoji-heavy MarkdownV2 messages
    exceed Telegram's 4096 / 1024 limits and be rejected by the API."""

    def test_markdown_v2_emoji_message_truncates_at_utf16_limit(self):
        """4096 emoji is 8192 UTF-16 units -> must be truncated, not passed through.

        Without the UTF-16 fix, ``len(text) == 4096 <= 4096`` returns
        ``text`` unchanged and Telegram rejects the 8192-unit payload as
        ``MESSAGE_TOO_LONG``.
        """
        text = "\U0001f600" * 4096  # grinning face emoji, each = 2 UTF-16 units
        result = truncate_for_telegram(text, 4096, "MarkdownV2")
        # Result must fit within Telegram's 4096 UTF-16-unit cap.
        result_units = len(result.encode("utf-16-le")) // 2
        assert result_units <= 4096
        # And must actually be shorter than the input (truncation happened).
        assert len(result) < len(text)

    def test_plain_emoji_message_truncates_at_utf16_limit(self):
        """Same defensive measurement for the plain branch via the helper."""
        text = "\U0001f600" * 4096
        result = truncate_for_telegram(text, 4096, "plain")
        result_units = len(result.encode("utf-16-le")) // 2
        assert result_units <= 4096
        assert len(result) < len(text)

    def test_emoji_message_under_utf16_limit_passes_through(self):
        """A 100-emoji message is 200 UTF-16 units; under 4096 -> unchanged."""
        text = "\U0001f600" * 100
        result = truncate_for_telegram(text, 4096, "MarkdownV2")
        assert result == text

    def test_markdown_v2_truncated_emoji_keeps_escaped_ellipsis(self):
        """When truncation does happen on an emoji payload, the escaped
        ellipsis is still appended and the slice does not split a surrogate
        pair (each emoji is included whole or dropped whole)."""
        text = "\U0001f600" * 4096
        result = truncate_for_telegram(text, 4096, "MarkdownV2")
        assert result.endswith(_ESCAPED_ELLIPSIS_PATTERN)
        # Each remaining emoji must be intact (no orphan high/low surrogate).
        body = result.removesuffix(_ESCAPED_ELLIPSIS_PATTERN)
        # Every character of the body should be the full emoji code point.
        for ch in body:
            assert ord(ch) == 0x1F600


class TestEndsWithOrphanBackslash:
    def test_returns_true_for_single_trailing_backslash(self):
        assert ends_with_orphan_backslash("abc\\") is True

    def test_returns_false_for_double_trailing_backslash(self):
        assert ends_with_orphan_backslash("abc\\\\") is False

    def test_returns_true_for_triple_trailing_backslash(self):
        assert ends_with_orphan_backslash("abc\\\\\\") is True

    def test_returns_false_for_no_trailing_backslash(self):
        assert ends_with_orphan_backslash("abc") is False

    def test_returns_false_for_empty_string(self):
        assert ends_with_orphan_backslash("") is False


class TestAdapterTruncateMessageMarkdownV2:
    """End-to-end tests for ``TelegramAdapter.truncate_message`` /
    ``truncate_caption`` plumbing parse_mode through to
    ``truncate_for_telegram``."""

    def test_truncate_message_markdown_v2_uses_safe_truncator(self):
        """A 5000-char MarkdownV2 message lands under 4096 with the
        escaped ellipsis (not literal ``...`` which would be a parse
        error on its own).

        What to fix if this fails: ``TelegramAdapter.truncate_message``
        in ``src/chat_sdk/adapters/telegram/adapter.py`` must dispatch
        to ``truncate_for_telegram`` when ``parse_mode == "MarkdownV2"``.
        """
        adapter = _make_adapter()
        text = "a" * 5000
        result = adapter.truncate_message(text, "MarkdownV2")
        assert len(result) <= 4096
        assert result.endswith(_ESCAPED_ELLIPSIS_PATTERN)
        assert not result.endswith("...")  # not the legacy literal

    def test_truncate_caption_markdown_v2_uses_safe_truncator(self):
        """Same as above for the 1024-char caption limit.

        What to fix if this fails: ``TelegramAdapter.truncate_caption``
        in ``src/chat_sdk/adapters/telegram/adapter.py``.
        """
        adapter = _make_adapter()
        text = "a" * 2000
        result = adapter.truncate_caption(text, "MarkdownV2")
        assert len(result) <= 1024
        assert result.endswith(_ESCAPED_ELLIPSIS_PATTERN)

    def test_truncate_message_plain_keeps_legacy_ellipsis(self):
        """Plain mode still uses literal ``...`` for backward compat."""
        adapter = _make_adapter()
        text = "a" * 5000
        result = adapter.truncate_message(text)
        assert result.endswith("...")
        # Length is measured in UTF-16 code units for plain mode (legacy).
        assert len(result) <= 4096


class TestMarkdownV2LinkUrlBackticksSurviveSends:
    """index.test.ts (vercel/chat#915): a link URL holding a backtick ships
    intact through the MarkdownV2 send, edit and caption paths. The chat#446
    under-limit trim used to cut the message at the URL's backtick."""

    MARKDOWN = "before [x](https://example.com/a`b) after"

    # it("preserves URL backticks and trailing text in a legacy MarkdownV2 post")
    @pytest.mark.asyncio
    async def test_preserves_url_backticks_and_trailing_text_in_a_legacy_markdown_v2_post(self):
        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter.telegram_fetch = AsyncMock(
            side_effect=[
                ValidationError("telegram", "Not Found: method not found"),
                _make_telegram_message(),
            ]
        )

        await adapter.post_message(THREAD_ID, {"markdown": self.MARKDOWN})

        method, payload = adapter.telegram_fetch.call_args_list[1][0][:2]
        assert method == "sendMessage"
        assert payload["parse_mode"] == "MarkdownV2"
        assert payload["text"] == self.MARKDOWN
        assert "rich_message" not in payload

    # it("preserves URL backticks and trailing text in a legacy MarkdownV2 edit")
    @pytest.mark.asyncio
    async def test_preserves_url_backticks_and_trailing_text_in_a_legacy_markdown_v2_edit(self):
        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter.telegram_fetch = AsyncMock(
            side_effect=[
                ValidationError("telegram", "Bad Request: rich message is unsupported"),
                _make_telegram_message(),
            ]
        )

        await adapter.edit_message(THREAD_ID, COMPOSITE_MESSAGE_ID, {"markdown": self.MARKDOWN})

        method, payload = adapter.telegram_fetch.call_args_list[1][0][:2]
        assert method == "editMessageText"
        assert payload["parse_mode"] == "MarkdownV2"
        assert payload["text"] == self.MARKDOWN
        assert "rich_message" not in payload

    # it("preserves URL backticks and trailing text in a MarkdownV2 file caption")
    @pytest.mark.asyncio
    async def test_preserves_url_backticks_and_trailing_text_in_a_markdown_v2_file_caption(self):
        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter.telegram_fetch = AsyncMock(return_value=_make_telegram_message())

        await adapter.post_message(
            THREAD_ID,
            PostableMarkdown(
                markdown=self.MARKDOWN,
                files=[FileUpload(data=b"payload", filename="report.txt", mime_type="text/plain")],
            ),
        )

        method, form_data = adapter.telegram_fetch.call_args[0][:2]
        assert method == "sendDocument"
        fields = _form_fields(form_data)
        assert fields["parse_mode"] == "MarkdownV2"
        assert fields["caption"] == self.MARKDOWN


# =============================================================================
# Tests -- decode_composite_message_id edge cases
# =============================================================================


class TestDecodeCompositeMessageId:
    def test_simple_numeric_with_expected_chat_id(self):
        adapter = _make_adapter()
        result = adapter.decode_composite_message_id("42", CHAT_ID)
        assert result["chat_id"] == CHAT_ID
        assert result["message_id"] == 42

    def test_invalid_message_id_raises(self):
        adapter = _make_adapter()
        with pytest.raises(ValidationError):
            adapter.decode_composite_message_id("not-a-number", CHAT_ID)

    def test_no_expected_chat_id_no_composite_raises(self):
        adapter = _make_adapter()
        with pytest.raises(ValidationError):
            adapter.decode_composite_message_id("just-text")

    def test_chat_id_mismatch_raises(self):
        adapter = _make_adapter()
        with pytest.raises(ValidationError, match="mismatch"):
            adapter.decode_composite_message_id(f"wrong:{MESSAGE_ID_INT}", CHAT_ID)

    # Python-specific: ``re.$`` matches before a trailing newline and ``\d``
    # matches non-ASCII digits; upstream's JS pattern accepts neither.
    @pytest.mark.parametrize("message_id", ["123:7\n", "123:７", "123:٧"])
    def test_rejects_composite_ids_upstream_would_not_match(self, message_id: str):
        adapter = _make_adapter()
        with pytest.raises(ValidationError, match="<chatId>:<messageId> format"):
            adapter.decode_composite_message_id(message_id)


# =============================================================================
# Tests -- throw_telegram_api_error additional branches
# =============================================================================


class TestThrowTelegramApiErrorBranches:
    def test_error_404_raises_not_found(self):
        from chat_sdk.shared.errors import ResourceNotFoundError

        adapter = _make_adapter()
        with pytest.raises(ResourceNotFoundError):
            adapter.throw_telegram_api_error("getChat", 404, {"ok": False, "error_code": 404})

    def test_error_400_raises_validation(self):
        adapter = _make_adapter()
        with pytest.raises(ValidationError):
            adapter.throw_telegram_api_error(
                "sendMessage", 400, {"ok": False, "error_code": 400, "description": "Bad Request"}
            )

    def test_error_500_raises_network(self):
        from chat_sdk.shared.errors import NetworkError

        adapter = _make_adapter()
        with pytest.raises(NetworkError):
            adapter.throw_telegram_api_error(
                "sendMessage", 500, {"ok": False, "error_code": 500, "description": "Server error"}
            )


# =============================================================================
# Tests -- fetch_channel_messages
# =============================================================================


class TestFetchChannelMessages:
    """fetch_channel_messages aggregates from cache."""

    @pytest.mark.asyncio
    async def test_fetch_channel_messages_from_cache(self):
        adapter = _make_adapter()
        _init_adapter(adapter)

        # Populate cache with messages in the thread
        msg1 = _make_telegram_message(text="Msg 1", message_id=1)
        msg2 = _make_telegram_message(text="Msg 2", message_id=2)
        adapter.parse_message(msg1)
        adapter.parse_message(msg2)

        result = await adapter.fetch_channel_messages(CHAT_ID)
        assert len(result.messages) == 2


# =============================================================================
# Tests -- normalize_user_name
# =============================================================================


class TestNormalizeUserName:
    def test_strips_leading_at(self):
        adapter = _make_adapter()
        assert adapter.normalize_user_name("@mybot") == "mybot"

    def test_strips_multiple_at(self):
        adapter = _make_adapter()
        assert adapter.normalize_user_name("@@@@mybot") == "mybot"

    def test_non_string_returns_bot(self):
        adapter = _make_adapter()
        assert adapter.normalize_user_name(None) == "bot"
        assert adapter.normalize_user_name(123) == "bot"

    def test_empty_string_returns_bot(self):
        adapter = _make_adapter()
        assert adapter.normalize_user_name("@") == "bot"


# =============================================================================
# Tests -- resolve_polling_config
# =============================================================================


class TestResolvePollingConfig:
    def test_default_config(self):
        adapter = _make_adapter()
        config = adapter.resolve_polling_config()
        assert config.limit == 100
        assert config.timeout == 30
        assert config.delete_webhook is True
        assert config.drop_pending_updates is False

    def test_override_config(self):
        from chat_sdk.adapters.telegram.types import TelegramLongPollingConfig

        adapter = _make_adapter()
        config = adapter.resolve_polling_config(TelegramLongPollingConfig(limit=50, timeout=10, delete_webhook=False))
        assert config.limit == 50
        assert config.timeout == 10
        assert config.delete_webhook is False


# =============================================================================
# Tests -- clamp_integer
# =============================================================================


class TestClampInteger:
    def test_clamps_too_high(self):
        assert TelegramAdapter.clamp_integer(200, 100, 1, 100) == 100

    def test_clamps_too_low(self):
        assert TelegramAdapter.clamp_integer(-5, 100, 1, 100) == 1

    def test_none_returns_fallback(self):
        assert TelegramAdapter.clamp_integer(None, 42, 0, 100) == 42

    def test_float_truncated(self):
        assert TelegramAdapter.clamp_integer(3.7, 10, 0, 100) == 3

    def test_nan_returns_fallback(self):
        assert TelegramAdapter.clamp_integer(float("nan"), 10, 0, 100) == 10


# =============================================================================
# Tests -- _resolve_thread_id
# =============================================================================


class TestResolveThreadId:
    def test_with_prefix(self):
        adapter = _make_adapter()
        result = adapter._resolve_thread_id(THREAD_ID)
        assert result.chat_id == CHAT_ID

    def test_without_prefix(self):
        adapter = _make_adapter()
        result = adapter._resolve_thread_id(CHAT_ID)
        assert result.chat_id == CHAT_ID


# =============================================================================
# Tests -- channel_id_from_thread_id
# =============================================================================


class TestChannelIdFromThreadId:
    def test_strips_topic(self):
        adapter = _make_adapter()
        thread = f"telegram:{CHAT_ID}:42"
        result = adapter.channel_id_from_thread_id(thread)
        assert result == f"telegram:{CHAT_ID}"


# =============================================================================
# Tests -- open_dm
# =============================================================================


class TestOpenDm:
    @pytest.mark.asyncio
    async def test_open_dm(self):
        adapter = _make_adapter()
        result = await adapter.open_dm("12345")
        assert result == "telegram:12345"


# =============================================================================
# Tests -- is_dm
# =============================================================================


class TestIsDm:
    def test_positive_chat_id_is_dm(self):
        adapter = _make_adapter()
        assert adapter.is_dm("telegram:12345") is True

    def test_negative_chat_id_is_not_dm(self):
        adapter = _make_adapter()
        assert adapter.is_dm(THREAD_ID) is False


# =============================================================================
# Tests -- post_channel_message
# =============================================================================


class TestPostChannelMessage:
    @pytest.mark.asyncio
    async def test_delegates_to_post_message(self):
        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter.telegram_fetch = AsyncMock(return_value=_make_telegram_message(text="chan"))

        result = await adapter.post_channel_message(THREAD_ID, {"markdown": "chan"})
        assert result.id == COMPOSITE_MESSAGE_ID
        assert result.thread_id == THREAD_ID
        # Verify it delegated to telegram_fetch (i.e. post_message internally);
        # a ``{markdown}`` payload routes through the rich endpoint.
        adapter.telegram_fetch.assert_called_once()
        call_args = adapter.telegram_fetch.call_args
        assert call_args[0][0] == "sendRichMessage"


# =============================================================================
# Tests -- _get_request_body / _get_header helpers
# =============================================================================


class TestRequestHelpers:
    @pytest.mark.asyncio
    async def test_get_body_from_body_bytes(self):
        class Req:
            body = b"raw bytes"

        result = await TelegramAdapter._get_request_body(Req())
        assert result == "raw bytes"

    @pytest.mark.asyncio
    async def test_get_body_from_body_callable(self):
        class Req:
            async def body(self):
                return b"async bytes"

        # body is callable
        result = await TelegramAdapter._get_request_body(Req())
        assert result == "async bytes"

    @pytest.mark.asyncio
    async def test_get_body_empty(self):
        class Req:
            pass

        result = await TelegramAdapter._get_request_body(Req())
        assert result == ""

    def test_get_header_dict(self):
        class Req:
            headers = {"X-Custom": "value"}

        result = TelegramAdapter._get_header(Req(), "x-custom")
        assert result == "value"

    def test_get_header_none(self):
        class Req:
            pass

        assert TelegramAdapter._get_header(Req(), "x-any") is None

    def test_get_header_mapping(self):
        class Headers:
            def get(self, name):
                return "mapped" if name == "x-test" else None

        class Req:
            headers = Headers()

        assert TelegramAdapter._get_header(Req(), "x-test") == "mapped"


# =============================================================================
# Tests -- compare_messages / message_sequence
# =============================================================================


class TestMessageHelpers:
    def test_message_sequence(self):
        adapter = _make_adapter()
        assert adapter.message_sequence(f"{CHAT_ID}:42") == 42
        assert adapter.message_sequence("no-sequence") == 0

    def test_compare_messages_by_time(self):
        from datetime import datetime, timezone

        from chat_sdk.types import Author, FormattedContent, Message, MessageMetadata

        adapter = _make_adapter()
        fmt: FormattedContent = {"type": "root", "children": []}
        author = Author(user_id="u", user_name="u", full_name="u", is_bot=False, is_me=False)
        a = Message(
            id="1",
            thread_id=THREAD_ID,
            text="a",
            formatted=fmt,
            raw={},
            author=author,
            metadata=MessageMetadata(date_sent=datetime(2024, 1, 1, tzinfo=timezone.utc)),
        )
        b = Message(
            id="2",
            thread_id=THREAD_ID,
            text="b",
            formatted=fmt,
            raw={},
            author=author,
            metadata=MessageMetadata(date_sent=datetime(2024, 1, 2, tzinfo=timezone.utc)),
        )
        assert adapter.compare_messages(a, b) == -1
        assert adapter.compare_messages(b, a) == 1


# =============================================================================
# Tests -- paginate_messages
# =============================================================================


class TestPaginateMessages:
    def test_empty_messages(self):
        from chat_sdk.types import FetchOptions

        adapter = _make_adapter()
        result = adapter.paginate_messages([], FetchOptions())
        assert result.messages == []

    def test_backward_pagination(self):
        from datetime import datetime, timezone

        from chat_sdk.types import Author, FetchOptions, Message, MessageMetadata

        adapter = _make_adapter()

        fmt: Any = {"type": "root", "children": []}

        def _msg(i: int) -> Message:
            return Message(
                id=f"{CHAT_ID}:{i}",
                thread_id=THREAD_ID,
                text=f"msg{i}",
                formatted=fmt,
                raw={},
                author=Author(user_id="u", user_name="u", full_name="u", is_bot=False, is_me=False),
                metadata=MessageMetadata(date_sent=datetime(2024, 1, i + 1, tzinfo=timezone.utc)),
            )

        msgs = [_msg(i) for i in range(5)]
        result = adapter.paginate_messages(msgs, FetchOptions(limit=2, direction="backward"))
        assert len(result.messages) == 2
        # Should have next_cursor since there are more
        assert result.next_cursor is not None

    def test_forward_pagination(self):
        from datetime import datetime, timezone

        from chat_sdk.types import Author, FetchOptions, Message, MessageMetadata

        adapter = _make_adapter()

        fmt: Any = {"type": "root", "children": []}

        def _msg(i: int) -> Message:
            return Message(
                id=f"{CHAT_ID}:{i}",
                thread_id=THREAD_ID,
                text=f"msg{i}",
                formatted=fmt,
                raw={},
                author=Author(user_id="u", user_name="u", full_name="u", is_bot=False, is_me=False),
                metadata=MessageMetadata(date_sent=datetime(2024, 1, i + 1, tzinfo=timezone.utc)),
            )

        msgs = [_msg(i) for i in range(5)]
        result = adapter.paginate_messages(msgs, FetchOptions(limit=2, direction="forward"))
        assert len(result.messages) == 2
        assert result.next_cursor is not None


# =============================================================================
# Tests -- cache operations
# =============================================================================


class TestCacheOperations:
    def test_cache_update_existing(self):
        adapter = _make_adapter()
        _init_adapter(adapter)

        msg = _make_telegram_message(text="v1")
        parsed = adapter.parse_message(msg)

        # Update same message
        msg2 = _make_telegram_message(text="v2")
        parsed2 = adapter.parse_telegram_message(msg2, THREAD_ID)
        adapter.cache_message(parsed2)

        found = adapter.find_cached_message(parsed.id)
        assert found is not None
        assert found.text == "v2"

    def test_delete_cached_message(self):
        adapter = _make_adapter()
        _init_adapter(adapter)

        msg = _make_telegram_message(text="to delete")
        parsed = adapter.parse_message(msg)

        adapter.delete_cached_message(parsed.id)
        assert adapter.find_cached_message(parsed.id) is None

    def test_delete_last_message_removes_thread(self):
        adapter = _make_adapter()
        _init_adapter(adapter)

        msg = _make_telegram_message(text="only one")
        parsed = adapter.parse_message(msg)

        adapter.delete_cached_message(parsed.id)
        assert THREAD_ID not in adapter._message_cache

    def test_find_cached_message_not_found(self):
        adapter = _make_adapter()
        assert adapter.find_cached_message("nonexistent") is None


# =============================================================================
# Tests -- disconnect
# =============================================================================


class TestDisconnect:
    @pytest.mark.asyncio
    async def test_disconnect_when_not_polling(self):
        adapter = _make_adapter()
        _init_adapter(adapter)
        # Disconnect completes without raising when not in polling mode
        await adapter.disconnect()


# =============================================================================
# Tests -- chat_display_name
# =============================================================================


class TestChatDisplayName:
    def test_title(self):
        adapter = _make_adapter()
        assert adapter.chat_display_name({"id": 1, "type": "group", "title": "My Group"}) == "My Group"

    def test_private_name(self):
        adapter = _make_adapter()
        assert (
            adapter.chat_display_name({"id": 1, "type": "private", "first_name": "John", "last_name": "Doe"})
            == "John Doe"
        )

    def test_username_fallback(self):
        adapter = _make_adapter()
        assert adapter.chat_display_name({"id": 1, "type": "private", "username": "jdoe"}) == "jdoe"

    def test_none_fallback(self):
        adapter = _make_adapter()
        assert adapter.chat_display_name({"id": 1, "type": "private"}) is None


# =============================================================================
# Tests -- native replies (vercel/chat#833, #228)
# =============================================================================

REPLY_THREAD_ID = "telegram:123"
EXPECTED_REPLY_PARAMETERS = {"message_id": 7, "allow_sending_without_reply": True}


def _reply_adapter(*responses: Any) -> TelegramAdapter:
    """``createReplyAdapter`` with ``telegram_fetch`` answering *responses* in order."""
    adapter = _make_adapter(user_name="mybot")
    _init_adapter(adapter)
    adapter.telegram_fetch = AsyncMock(  # type: ignore[method-assign]
        side_effect=list(responses) or [_make_telegram_message(chat_id="123", message_id=11)]
    )
    return adapter


def _only_call(adapter: TelegramAdapter) -> tuple[str, Any]:
    """The single Bot API call a reply made."""
    adapter.telegram_fetch.assert_awaited_once()  # type: ignore[attr-defined]
    method, payload = adapter.telegram_fetch.await_args.args  # type: ignore[attr-defined]
    return method, payload


class TestReply:
    """Ports of ``describe("reply")``: every send path carries ``reply_parameters``."""

    @pytest.mark.asyncio
    async def test_threads_a_rich_text_message_to_its_target(self):
        adapter = _reply_adapter()

        await adapter.reply(REPLY_THREAD_ID, "123:7", {"markdown": "hello"})

        method, payload = _only_call(adapter)
        assert method == "sendRichMessage"
        assert payload["reply_parameters"] == EXPECTED_REPLY_PARAMETERS

    @pytest.mark.asyncio
    async def test_threads_a_plain_string_message_through_send_message(self):
        adapter = _reply_adapter()

        await adapter.reply(REPLY_THREAD_ID, "123:7", "hello")

        method, payload = _only_call(adapter)
        assert method == "sendMessage"
        assert payload["reply_parameters"] == EXPECTED_REPLY_PARAMETERS

    @pytest.mark.asyncio
    async def test_threads_a_document_upload_to_its_target(self):
        adapter = _reply_adapter()

        await adapter.reply(
            REPLY_THREAD_ID,
            "123:7",
            PostableRaw(raw="", files=[FileUpload(data=b"doc", filename="doc.txt")]),
        )

        method, form_data = _only_call(adapter)
        assert method == "sendDocument"
        assert json.loads(_form_fields(form_data)["reply_parameters"]) == EXPECTED_REPLY_PARAMETERS

    @pytest.mark.asyncio
    async def test_threads_a_url_attachment_without_an_inline_keyboard(self):
        adapter = _reply_adapter()

        await adapter.reply(
            REPLY_THREAD_ID,
            "123:7",
            PostableMarkdown(
                markdown="picture",
                attachments=[
                    Attachment(
                        type="image",
                        mime_type="image/png",
                        name="pic.png",
                        url="https://cdn.example.com/pic.png",
                    )
                ],
            ),
        )

        method, payload = _only_call(adapter)
        assert method == "sendPhoto"
        assert payload["reply_parameters"] == EXPECTED_REPLY_PARAMETERS
        assert "reply_markup" not in payload

    @pytest.mark.asyncio
    async def test_threads_a_buffer_attachment_to_its_target(self):
        adapter = _reply_adapter()

        await adapter.reply(
            REPLY_THREAD_ID,
            "123:7",
            PostableMarkdown(
                markdown="picture",
                attachments=[Attachment(type="image", data=b"payload", mime_type="image/png", name="pic.png")],
            ),
        )

        method, form_data = _only_call(adapter)
        assert method == "sendPhoto"
        assert json.loads(_form_fields(form_data)["reply_parameters"]) == EXPECTED_REPLY_PARAMETERS

    @pytest.mark.asyncio
    async def test_threads_a_media_group_to_its_target(self):
        adapter = _reply_adapter(
            [
                _make_telegram_message(chat_id="123", message_id=11),
                _make_telegram_message(chat_id="123", message_id=12),
            ]
        )

        await adapter.reply(
            REPLY_THREAD_ID,
            "123:7",
            PostableMarkdown(
                markdown="album",
                attachments=[
                    Attachment(type="image", data=b"one", name="one.png"),
                    Attachment(type="image", data=b"two", name="two.png"),
                ],
            ),
        )

        method, form_data = _only_call(adapter)
        assert method == "sendMediaGroup"
        assert json.loads(_form_fields(form_data)["reply_parameters"]) == EXPECTED_REPLY_PARAMETERS

    @pytest.mark.asyncio
    async def test_falls_back_to_a_regular_send_when_the_rich_endpoint_rejects_reply_parameters(self):
        # The error a 400 from the Bot API maps to.
        with pytest.raises(ValidationError) as rejected:
            _make_adapter().throw_telegram_api_error(
                "sendRichMessage",
                400,
                {"ok": False, "error_code": 400, "description": "Bad Request: unknown field reply_parameters"},
            )
        adapter = _reply_adapter(rejected.value, _make_telegram_message(chat_id="123", message_id=11))

        await adapter.reply(REPLY_THREAD_ID, "123:7", {"markdown": "hello"})

        calls = adapter.telegram_fetch.await_args_list  # type: ignore[attr-defined]
        assert [call.args[0] for call in calls] == ["sendRichMessage", "sendMessage"]
        assert calls[1].args[1]["reply_parameters"] == EXPECTED_REPLY_PARAMETERS

    @pytest.mark.asyncio
    async def test_leaves_a_plain_post_message_unthreaded(self):
        adapter = _reply_adapter()

        await adapter.post_message(REPLY_THREAD_ID, {"markdown": "hello"})

        _method, payload = _only_call(adapter)
        assert "reply_parameters" not in payload

    # Python-specific: upstream's ``JSON.stringify`` drops ``undefined``; here the
    # ``is not None`` guards are what keep ``reply_parameters`` off upload paths.
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("message", "expected_method"),
        [
            pytest.param(
                PostableMarkdown(
                    markdown="picture",
                    attachments=[
                        Attachment(
                            type="image",
                            mime_type="image/png",
                            name="pic.png",
                            url="https://cdn.example.com/pic.png",
                        )
                    ],
                ),
                "sendPhoto",
                id="url-attachment",
            ),
            pytest.param(
                PostableMarkdown(
                    markdown="picture",
                    attachments=[Attachment(type="image", data=b"payload", mime_type="image/png", name="pic.png")],
                ),
                "sendPhoto",
                id="bytes-attachment",
            ),
            pytest.param(
                PostableRaw(raw="", files=[FileUpload(data=b"doc", filename="doc.txt")]),
                "sendDocument",
                id="file-upload",
            ),
        ],
    )
    async def test_leaves_plain_post_message_uploads_unthreaded(self, message: Any, expected_method: str):
        adapter = _reply_adapter()

        await adapter.post_message(REPLY_THREAD_ID, message)

        method, body = _only_call(adapter)
        assert method == expected_method
        fields = body if isinstance(body, dict) else _form_fields(body)
        assert "reply_parameters" not in fields
        # Guard against a vacuous pass: the body really is the upload request.
        assert fields["chat_id"] in ("123", 123)

    @pytest.mark.asyncio
    async def test_refuses_a_target_that_belongs_to_another_chat(self):
        adapter = _reply_adapter()
        # Python-specific: the target is checked before any attachment download.
        fetch_data = AsyncMock(return_value=b"payload")

        with pytest.raises(ValidationError, match="chat mismatch"):
            await adapter.reply(
                REPLY_THREAD_ID,
                "999:7",
                PostableMarkdown(markdown="hello", attachments=[Attachment(type="image", fetch_data=fetch_data)]),
            )

        adapter.telegram_fetch.assert_not_awaited()  # type: ignore[attr-defined]
        fetch_data.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("target", ["7abc", "7.9", " 7"])
    async def test_rejects_the_malformed_bare_message_id(self, target: str):
        adapter = _reply_adapter()

        with pytest.raises(ValidationError, match="Invalid Telegram message ID"):
            await adapter.reply(REPLY_THREAD_ID, target, {"markdown": "hello"})

        adapter.telegram_fetch.assert_not_awaited()  # type: ignore[attr-defined]

    # Python-specific: ``int()`` accepts these, upstream's ``/^\d+$/`` does not.
    @pytest.mark.asyncio
    @pytest.mark.parametrize("target", ["+7", "1_0", "７", "7\n", "123:7\n"])
    async def test_rejects_ids_that_python_int_would_accept(self, target: str):
        adapter = _reply_adapter()

        with pytest.raises(ValidationError, match="Invalid Telegram message ID"):
            await adapter.reply(REPLY_THREAD_ID, target, {"markdown": "hello"})

        adapter.telegram_fetch.assert_not_awaited()  # type: ignore[attr-defined]

    # Python-specific: the ``Thread.reply`` -> adapter hook path end to end.
    @pytest.mark.asyncio
    async def test_thread_reply_sends_one_threaded_call_and_post_stays_unthreaded(self):
        adapter = _reply_adapter(
            _make_telegram_message(chat_id="123", message_id=11),
            _make_telegram_message(chat_id="123", message_id=12),
        )
        thread = ThreadImpl(
            _ThreadImplConfig(
                id=REPLY_THREAD_ID,
                adapter=adapter,  # type: ignore[arg-type]
                state_adapter=create_mock_state(),
                channel_id="telegram:123",
            )
        )
        target = adapter.parse_message(_make_telegram_message(chat_id="123", message_id=7, text="question"))

        sent = await thread.reply(target, "answer")
        await thread.post("not a reply")

        calls = adapter.telegram_fetch.await_args_list  # type: ignore[attr-defined]
        assert [call.args[0] for call in calls] == ["sendMessage", "sendMessage"]
        assert calls[0].args[1]["reply_parameters"] == EXPECTED_REPLY_PARAMETERS
        assert "reply_parameters" not in calls[1].args[1]
        assert sent.id == "123:11"
        assert sent.reply_to is target
