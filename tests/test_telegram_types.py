"""Structural / typing tests for Telegram rich-message wire types (Bot API 10.1).

These types are TypedDict wire shapes mirroring upstream
``adapter-telegram/src/types.ts`` (chat@4.31.0, commit 4662309). The runtime
logic that consumes them (``rich.py``) lands in a later PR; here we only verify
that:

- the recursive ``TelegramRichText`` / ``TelegramRichBlock`` aliases resolve at
  runtime (the quoted forward-ref TypeAlias strings do not raise), and
- representative nested ``rich_message`` payloads — taken from the upstream
  ``rich.test.ts`` fixtures — construct with the expected snake_case keys.

pyrefly is the primary gate for these types (recursive unions); the assertions
below also catch a typo'd wire key at runtime.

Typing note: ``TelegramRichText`` / ``TelegramRichBlock`` are recursive *unions*
that pyrefly cannot narrow from a bare dict literal. Each fixture is therefore
annotated with its concrete variant TypedDict (e.g. ``TelegramRichBlockText``),
and recursion into a nested union member is bound to a typed intermediate local
of the concrete variant so subscripting type-checks. The runtime ``type`` /
``text`` assertions still exercise the recursive shapes end to end.
"""

from __future__ import annotations

from chat_sdk.adapters.telegram.types import (
    TelegramAnimation,
    TelegramAudio,
    TelegramLocation,
    TelegramMessage,
    TelegramRichBlock,
    TelegramRichBlockAnimation,
    TelegramRichBlockAudio,
    TelegramRichBlockBlockquote,
    TelegramRichBlockHeading,
    TelegramRichBlockList,
    TelegramRichBlockMap,
    TelegramRichBlockTable,
    TelegramRichBlockText,
    TelegramRichBlockVideo,
    TelegramRichBlockVoiceNote,
    TelegramRichItem,
    TelegramRichMessage,
    TelegramRichText,
    TelegramRichTextStyled,
    TelegramRichTextUrl,
    TelegramVideo,
    TelegramVideoQuality,
    TelegramVoice,
)


def test_recursive_aliases_resolve_at_runtime() -> None:
    """The quoted forward-ref TypeAlias strings must not raise on import/use."""
    # Module-level aliases evaluate to the unioned string at runtime (they are
    # quoted forward refs), but they must exist and be referenceable.
    assert isinstance(TelegramRichText, str)
    assert "TelegramRichText" in TelegramRichText
    assert isinstance(TelegramRichBlock, str)
    assert "TelegramRichBlock" in TelegramRichBlock


def test_rich_text_nested_spans() -> None:
    """A TelegramRichText may be a plain string, a list, or a styled span dict."""
    plain: TelegramRichText = "hello"
    url_span: TelegramRichTextUrl = {
        "type": "url",
        "text": "the guide",
        "url": "https://example.com",
    }
    bold_span: TelegramRichTextStyled = {"type": "bold", "text": "continue"}
    nested: list[TelegramRichText] = ["Read ", url_span, " and ", bold_span]
    assert plain == "hello"
    assert isinstance(nested, list)
    assert nested[1] == {
        "type": "url",
        "text": "the guide",
        "url": "https://example.com",
    }
    # Recursion: a span's text may itself be a list of spans.
    inner_bold: TelegramRichTextStyled = {"type": "bold", "text": "x"}
    deep: TelegramRichTextStyled = {"type": "italic", "text": [inner_bold]}
    deep_text = deep["text"]
    assert isinstance(deep_text, list)
    first: TelegramRichTextStyled = deep_text[0]  # type: ignore[assignment]
    assert first["type"] == "bold"


def test_rich_message_with_heading_paragraph_and_table() -> None:
    """Mirror of the upstream rich.test.ts structured-blocks fixture."""
    heading: TelegramRichBlockHeading = {
        "type": "heading",
        "size": 2,
        "text": "Summary",
    }
    url_span: TelegramRichTextUrl = {
        "type": "url",
        "text": "the guide",
        "url": "https://example.com",
    }
    bold_span: TelegramRichTextStyled = {"type": "bold", "text": "continue"}
    paragraph: TelegramRichBlockText = {
        "type": "paragraph",
        "text": ["Read ", url_span, " and ", bold_span],
    }
    table: TelegramRichBlockTable = {
        "type": "table",
        "cells": [
            [
                {
                    "align": "left",
                    "is_header": True,
                    "text": "Name",
                    "valign": "top",
                },
                {
                    "align": "right",
                    "is_header": True,
                    "text": "Status",
                    "valign": "top",
                },
            ],
            [
                {"align": "left", "text": "Build", "valign": "top"},
                {"align": "right", "text": "Ready", "valign": "top"},
            ],
        ],
    }
    message: TelegramRichMessage = {"blocks": [heading, paragraph, table]}
    blocks = message["blocks"]
    head_block: TelegramRichBlockHeading = blocks[0]  # type: ignore[assignment]
    assert head_block["type"] == "heading"
    assert head_block["size"] == 2
    # Recursion into a table cell's text.
    table_block: TelegramRichBlockTable = blocks[2]  # type: ignore[assignment]
    assert table_block["cells"][0][0]["is_header"] is True
    assert table_block["cells"][1][1]["text"] == "Ready"


def test_rich_block_nested_blocks_recursion() -> None:
    """Blockquote / list blocks nest further TelegramRichBlock values."""
    quoted: TelegramRichBlockText = {"type": "paragraph", "text": "quoted"}
    item_para: TelegramRichBlockText = {"type": "paragraph", "text": "a"}
    list_item: TelegramRichItem = {"label": "first", "blocks": [item_para]}
    list_block: TelegramRichBlockList = {"type": "list", "items": [list_item]}
    block: TelegramRichBlockBlockquote = {
        "type": "blockquote",
        "blocks": [quoted, list_block],
        "credit": "someone",
    }
    inner: TelegramRichBlockList = block["blocks"][1]  # type: ignore[assignment]
    assert inner["type"] == "list"
    # Recursion: list item blocks are themselves TelegramRichBlock values.
    nested_para: TelegramRichBlockText = inner["items"][0]["blocks"][0]  # type: ignore[assignment]
    assert nested_para["text"] == "a"


def test_rich_block_media_blocks() -> None:
    """Media blocks carry the new named media wire types with snake_case keys."""
    animation: TelegramAnimation = {
        "file_id": "anim1",
        "duration": 3,
        "height": 480,
        "width": 640,
    }
    audio: TelegramAudio = {"file_id": "aud1", "duration": 12, "title": "Song"}
    voice: TelegramVoice = {"file_id": "v1", "duration": 5, "mime_type": "audio/ogg"}
    quality: TelegramVideoQuality = {
        "file_id": "q1",
        "codec": "h264",
        "height": 720,
        "width": 1280,
    }
    video: TelegramVideo = {
        "file_id": "vid1",
        "duration": 30,
        "height": 1080,
        "width": 1920,
        "qualities": [quality],
        "start_timestamp": 5,
    }
    anim_block: TelegramRichBlockAnimation = {
        "type": "animation",
        "animation": animation,
        "has_spoiler": True,
    }
    audio_block: TelegramRichBlockAudio = {"type": "audio", "audio": audio}
    video_block: TelegramRichBlockVideo = {
        "type": "video",
        "video": video,
        "has_spoiler": True,
    }
    voice_block: TelegramRichBlockVoiceNote = {
        "type": "voice_note",
        "voice_note": voice,
    }
    message: TelegramRichMessage = {
        "blocks": [anim_block, audio_block, video_block, voice_block],
    }
    block0: TelegramRichBlockAnimation = message["blocks"][0]  # type: ignore[assignment]
    block2: TelegramRichBlockVideo = message["blocks"][2]  # type: ignore[assignment]
    block3: TelegramRichBlockVoiceNote = message["blocks"][3]  # type: ignore[assignment]
    assert block0["animation"]["width"] == 640
    assert block2["video"]["qualities"][0]["codec"] == "h264"
    assert block3["voice_note"]["mime_type"] == "audio/ogg"


def test_rich_block_map_uses_location() -> None:
    """The map block embeds a TelegramLocation with float lat/long."""
    location: TelegramLocation = {"latitude": 40.0, "longitude": -73.0}
    block: TelegramRichBlockMap = {
        "type": "map",
        "height": 200,
        "width": 300,
        "zoom": 12,
        "location": location,
    }
    assert block["location"]["latitude"] == 40.0
    assert block["zoom"] == 12


def test_telegram_message_carries_rich_message() -> None:
    """TelegramMessage gains an optional rich_message payload in 4.31."""
    paragraph: TelegramRichBlockText = {"type": "paragraph", "text": "hi"}
    rich: TelegramRichMessage = {"blocks": [paragraph], "is_rtl": False}
    message: TelegramMessage = {
        "chat": {"id": 1, "type": "private"},
        "date": 0,
        "message_id": 7,
        "rich_message": rich,
    }
    first_block: TelegramRichBlockText = message["rich_message"]["blocks"][0]  # type: ignore[assignment]
    assert first_block["text"] == "hi"
    assert message["rich_message"]["is_rtl"] is False


def test_telegram_message_video_is_widened() -> None:
    """The message ``video`` field is the widened TelegramVideo in 4.31."""
    message: TelegramMessage = {
        "chat": {"id": 1, "type": "private"},
        "date": 0,
        "message_id": 8,
        "video": {
            "file_id": "vid",
            "duration": 10,
            "height": 720,
            "width": 1280,
            "cover": [{"file_id": "c", "height": 720, "width": 1280}],
        },
    }
    assert message["video"]["cover"][0]["file_id"] == "c"
