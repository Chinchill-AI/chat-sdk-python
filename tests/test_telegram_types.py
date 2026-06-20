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
"""

from __future__ import annotations

from chat_sdk.adapters.telegram.types import (
    TelegramAnimation,
    TelegramAudio,
    TelegramLocation,
    TelegramMessage,
    TelegramRichBlock,
    TelegramRichMessage,
    TelegramRichText,
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
    nested: TelegramRichText = [
        "Read ",
        {"type": "url", "text": "the guide", "url": "https://example.com"},
        " and ",
        {"type": "bold", "text": "continue"},
    ]
    assert plain == "hello"
    assert isinstance(nested, list)
    assert nested[1] == {
        "type": "url",
        "text": "the guide",
        "url": "https://example.com",
    }
    # Recursion: a span's text may itself be a list of spans.
    deep: TelegramRichText = {
        "type": "italic",
        "text": [{"type": "bold", "text": "x"}],
    }
    assert deep["text"][0]["type"] == "bold"


def test_rich_message_with_heading_paragraph_and_table() -> None:
    """Mirror of the upstream rich.test.ts structured-blocks fixture."""
    message: TelegramRichMessage = {
        "blocks": [
            {"type": "heading", "size": 2, "text": "Summary"},
            {
                "type": "paragraph",
                "text": [
                    "Read ",
                    {"type": "url", "text": "the guide", "url": "https://example.com"},
                    " and ",
                    {"type": "bold", "text": "continue"},
                ],
            },
            {
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
            },
        ],
    }
    blocks = message["blocks"]
    assert blocks[0]["type"] == "heading"
    assert blocks[0]["size"] == 2
    # Recursion into a table cell's text.
    assert blocks[2]["cells"][0][0]["is_header"] is True
    assert blocks[2]["cells"][1][1]["text"] == "Ready"


def test_rich_block_nested_blocks_recursion() -> None:
    """Blockquote / list blocks nest further TelegramRichBlock values."""
    block: TelegramRichBlock = {
        "type": "blockquote",
        "blocks": [
            {"type": "paragraph", "text": "quoted"},
            {
                "type": "list",
                "items": [
                    {"label": "first", "blocks": [{"type": "paragraph", "text": "a"}]},
                ],
            },
        ],
        "credit": "someone",
    }
    inner = block["blocks"][1]
    assert inner["type"] == "list"
    # Recursion: list item blocks are themselves TelegramRichBlock values.
    assert inner["items"][0]["blocks"][0]["text"] == "a"


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
    message: TelegramRichMessage = {
        "blocks": [
            {"type": "animation", "animation": animation, "has_spoiler": True},
            {"type": "audio", "audio": audio},
            {"type": "video", "video": video, "has_spoiler": True},
            {"type": "voice_note", "voice_note": voice},
        ],
    }
    assert message["blocks"][0]["animation"]["width"] == 640
    assert message["blocks"][2]["video"]["qualities"][0]["codec"] == "h264"
    assert message["blocks"][3]["voice_note"]["mime_type"] == "audio/ogg"


def test_rich_block_map_uses_location() -> None:
    """The map block embeds a TelegramLocation with float lat/long."""
    location: TelegramLocation = {"latitude": 40.0, "longitude": -73.0}
    block: TelegramRichBlock = {
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
    message: TelegramMessage = {
        "chat": {"id": 1, "type": "private"},
        "date": 0,
        "message_id": 7,
        "rich_message": {
            "blocks": [{"type": "paragraph", "text": "hi"}],
            "is_rtl": False,
        },
    }
    assert message["rich_message"]["blocks"][0]["text"] == "hi"
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
