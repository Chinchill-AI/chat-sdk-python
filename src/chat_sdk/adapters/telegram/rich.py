"""Telegram rich-message → Markdown / plain-text rendering (Bot API 10.1).

Python port of ``packages/adapter-telegram/src/rich.ts`` (chat@4.31.0,
commit 4662309). Regex/parse-heavy module ported character-for-character.

The rich-message wire types live in :mod:`chat_sdk.adapters.telegram.types`
(TG1). This module converts the recursive ``TelegramRichBlock`` /
``TelegramRichText`` shapes into Markdown (for sending) and plain text (for
search/snippets), and extracts media attachments.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from chat_sdk.shared.streaming_markdown import StreamingMarkdownRenderer

from .types import (
    TelegramFile,
    TelegramRichBlock,
    TelegramRichBlockTable,
    TelegramRichCaption,
    TelegramRichCell,
    TelegramRichItem,
    TelegramRichMessage,
    TelegramRichText,
)

TELEGRAM_RICH_MESSAGE_LIMIT = 32_768

# Upstream: /[!-/:-@[-`{-~]/g -- the four ASCII punctuation ranges
#   "!"-"/"  ":"-"@"  "["-"`"  "{"-"~"
# A char class, so re.sub matches a single punctuation char at a time.
MARKDOWN_PUNCTUATION = re.compile(r"[!-/:-@[-`{-~]")
# Upstream: /[\r\n]/g
LINE_BREAKS = re.compile(r"[\r\n]")
# Upstream: /`+/g -- runs of one or more backticks.
BACKTICKS = re.compile(r"`+")


def truncate_rich_markdown(markdown: str) -> str:
    """Truncate *markdown* to the Telegram rich-message character limit.

    Truncation is code-point aware: ``list(str)`` yields one element per
    Unicode code point (matching JS ``Array.from(str)``), so a slice on a
    list index never splits a surrogate pair / non-BMP character. The
    held-back tail is re-rendered through :class:`StreamingMarkdownRenderer`
    so the truncated prefix stays valid markdown, with ``"..."`` appended.
    """
    characters = list(markdown)
    if len(characters) <= TELEGRAM_RICH_MESSAGE_LIMIT:
        return markdown

    end = TELEGRAM_RICH_MESSAGE_LIMIT - 3
    while end > 0:
        renderer = StreamingMarkdownRenderer()
        renderer.push("".join(characters[:end]))
        rendered = f"{renderer.finish()}..."
        if len(rendered) <= TELEGRAM_RICH_MESSAGE_LIMIT:
            return rendered
        end -= len(rendered) - TELEGRAM_RICH_MESSAGE_LIMIT

    return "..."


def _escape_text(value: str) -> str:
    """Backslash-escape every Markdown punctuation char in *value*.

    Upstream: ``value.replace(MARKDOWN_PUNCTUATION, "\\$&")`` -- each matched
    punctuation char is prefixed with a single backslash. ``\\g<0>`` is the
    Python equivalent of JS ``$&`` (the whole match).
    """
    return MARKDOWN_PUNCTUATION.sub(r"\\\g<0>", value)


def _inline_code(value: str) -> str:
    if not value:
        return ""
    runs = BACKTICKS.findall(value)
    size = max(1, *(len(run) + 1 for run in runs)) if runs else 1
    marker = "`" * size
    has_boundary_space = value.startswith(" ") and value.endswith(" ") and len(value.strip()) > 0
    padding = " " if value.startswith("`") or value.endswith("`") or has_boundary_space else ""
    return f"{marker}{padding}{value}{padding}{marker}"


def _code_block(value: str, language: str | None = None) -> str:
    runs = BACKTICKS.findall(value)
    size = max(3, *(len(run) + 1 for run in runs)) if runs else 3
    marker = "`" * size
    info = LINE_BREAKS.sub(" ", language).replace("`", "") if language else ""
    return f"{marker}{info}\n{value}\n{marker}"


def _link_destination(value: str) -> str:
    return (
        "<"
        + value.replace("\\", "%5C").replace("<", "%3C").replace(">", "%3E").replace("\r", "%0D").replace("\n", "%0A")
        + ">"
    )


def _text(markdown: TelegramRichText) -> str:
    if isinstance(markdown, str):
        return _escape_text(markdown)
    if isinstance(markdown, list):
        return "".join(_text(part) for part in markdown)

    match markdown["type"]:
        case "bold":
            return f"**{_text(markdown['text'])}**"
        case "italic":
            return f"_{_text(markdown['text'])}_"
        case "underline":
            return f"<u>{_text(markdown['text'])}</u>"
        case "strikethrough":
            return f"~~{_text(markdown['text'])}~~"
        case "spoiler":
            return f"||{_text(markdown['text'])}||"
        case "subscript":
            return f"<sub>{_text(markdown['text'])}</sub>"
        case "superscript":
            return f"<sup>{_text(markdown['text'])}</sup>"
        case "marked":
            return f"=={_text(markdown['text'])}=="
        case "code":
            return _inline_code(_plain(markdown["text"]))
        case "date_time" | "text_mention":
            return _text(markdown["text"])
        case "bank_card_number" | "bot_command" | "cashtag" | "hashtag" | "mention":
            return _text(markdown["text"])
        case "custom_emoji":
            return markdown["alternative_text"]
        case "mathematical_expression":
            return f"${markdown['expression']}$"
        case "url":
            return f"[{_text(markdown['text'])}]({_link_destination(markdown['url'])})"
        case "email_address":
            dest = _link_destination(f"mailto:{markdown['email_address']}")
            return f"[{_text(markdown['text'])}]({dest})"
        case "phone_number":
            dest = _link_destination(f"tel:{markdown['phone_number']}")
            return f"[{_text(markdown['text'])}]({dest})"
        case "anchor":
            return ""
        case "anchor_link" | "reference" | "reference_link":
            return _text(markdown["text"])
        case _:
            return ""


def _plain(markdown: TelegramRichText) -> str:
    if isinstance(markdown, str):
        return markdown
    if isinstance(markdown, list):
        return "".join(_plain(part) for part in markdown)

    match markdown["type"]:
        case (
            "bold"
            | "italic"
            | "underline"
            | "strikethrough"
            | "spoiler"
            | "subscript"
            | "superscript"
            | "marked"
            | "code"
            | "date_time"
            | "text_mention"
            | "url"
            | "email_address"
            | "phone_number"
            | "bank_card_number"
            | "mention"
            | "hashtag"
            | "cashtag"
            | "bot_command"
            | "anchor_link"
            | "reference"
            | "reference_link"
        ):
            return _plain(markdown["text"])
        case "custom_emoji":
            return markdown["alternative_text"]
        case "mathematical_expression":
            return markdown["expression"]
        case "anchor":
            return ""
        case _:
            return ""


def _caption(value: TelegramRichCaption | None = None) -> str:
    if not value:
        return ""
    credit = f"\n{_text(value['credit'])}" if value.get("credit") else ""
    return f"{_text(value['text'])}{credit}"


def _plain_caption(value: TelegramRichCaption | None = None) -> str:
    if not value:
        return ""
    credit = f"\n{_plain(value['credit'])}" if value.get("credit") else ""
    return f"{_plain(value['text'])}{credit}"


def _cell(value: TelegramRichCell) -> str:
    return _text(value["text"]) if value.get("text") else ""


def _item(value: TelegramRichItem) -> str:
    checked = ""
    if value.get("has_checkbox"):
        checked = "[x] " if value.get("is_checked") else "[ ] "
    content = "\n\n".join(_block(b) for b in value["blocks"]).replace("\n", "\n  ")
    return f"{value['label']} {checked}{content}".rstrip()


def _plain_item(value: TelegramRichItem) -> str:
    checked = ""
    if value.get("has_checkbox"):
        checked = "[x] " if value.get("is_checked") else "[ ] "
    content = "\n".join(b for b in (_plain_block(blk) for blk in value["blocks"]) if b).replace("\n", "\n  ")
    return f"{value['label']} {checked}{content}".rstrip()


def _table(value: TelegramRichBlockTable) -> str:
    rows = [f"| {' | '.join(_cell(c) for c in row)} |" for row in value["cells"]]
    if len(rows) == 0:
        return _text(value["caption"]) if value.get("caption") else ""

    columns = max(len(row) for row in value["cells"])
    separator = f"| {' | '.join('---' for _ in range(columns))} |"
    content = "\n".join([rows[0], separator, *rows[1:]])
    return f"{_text(value['caption'])}\n\n{content}" if value.get("caption") else content


def _quote(value: str) -> str:
    return "\n".join(f"> {line}" for line in value.split("\n"))


def _block(value: TelegramRichBlock) -> str:
    match value["type"]:
        case "paragraph" | "footer" | "thinking":
            return _text(value["text"])
        case "heading":
            level = min(6, max(1, value["size"]))
            return f"{'#' * level} {_text(value['text'])}"
        case "pre":
            return _code_block(_plain(value["text"]), value.get("language"))
        case "divider":
            return "---"
        case "mathematical_expression":
            return f"$${value['expression']}$$"
        case "anchor":
            return ""
        case "list":
            return "\n".join(_item(i) for i in value["items"])
        case "blockquote":
            content = "\n\n".join(_block(b) for b in value["blocks"])
            credit = f"\n\n{_text(value['credit'])}" if value.get("credit") else ""
            return _quote(f"{content}{credit}")
        case "pullquote":
            credit = f"\n\n{_text(value['credit'])}" if value.get("credit") else ""
            return _quote(f"{_text(value['text'])}{credit}")
        case "collage" | "slideshow":
            content = "\n\n".join(b for b in (_block(blk) for blk in value["blocks"]) if b)
            description = _caption(value.get("caption"))
            return "\n\n".join(part for part in (content, description) if part)
        case "table":
            return _table(value)
        case "details":
            body = "\n\n".join(_block(b) for b in value["blocks"])
            return f"{_text(value['summary'])}\n\n{body}"
        case "map":
            return _caption(value.get("caption"))
        case "animation" | "audio" | "photo" | "video" | "voice_note":
            return _caption(value.get("caption"))
        case _:
            return ""


def _plain_block(value: TelegramRichBlock) -> str:
    match value["type"]:
        case "paragraph" | "footer" | "thinking" | "heading" | "pre":
            return _plain(value["text"])
        case "divider" | "anchor":
            return ""
        case "mathematical_expression":
            return value["expression"]
        case "list":
            return "\n".join(_plain_item(i) for i in value["items"])
        case "blockquote":
            content = "\n\n".join(b for b in (_plain_block(blk) for blk in value["blocks"]) if b)
            credit = f"\n\n{_plain(value['credit'])}" if value.get("credit") else ""
            return f"{content}{credit}"
        case "pullquote":
            credit = f"\n\n{_plain(value['credit'])}" if value.get("credit") else ""
            return f"{_plain(value['text'])}{credit}"
        case "collage" | "slideshow":
            content = "\n\n".join(b for b in (_plain_block(blk) for blk in value["blocks"]) if b)
            description = _plain_caption(value.get("caption"))
            return "\n\n".join(part for part in (content, description) if part)
        case "table":
            rows = [
                "\t".join(_plain(entry["text"]) if entry.get("text") else "" for entry in row) for row in value["cells"]
            ]
            content = "\n".join(rows)
            return f"{_plain(value['caption'])}\n\n{content}" if value.get("caption") else content
        case "details":
            body = "\n\n".join(b for b in (_plain_block(blk) for blk in value["blocks"]) if b)
            return f"{_plain(value['summary'])}\n\n{body}"
        case "map":
            return _plain_caption(value.get("caption"))
        case "animation" | "audio" | "photo" | "video" | "voice_note":
            return _plain_caption(value.get("caption"))
        case _:
            return ""


@dataclass
class RichMedia:
    """A media attachment extracted from a rich message."""

    file: TelegramFile
    type: Literal["image", "file", "video", "audio"]
    height: int | None = None
    mime_type: str | None = None
    name: str | None = None
    width: int | None = None


def _media(blocks: list[TelegramRichBlock], result: list[RichMedia]) -> None:
    for value in blocks:
        match value["type"]:
            case "list":
                for entry in value["items"]:
                    _media(entry["blocks"], result)
            case "blockquote" | "collage" | "slideshow" | "details":
                _media(value["blocks"], result)
            case "animation":
                animation = value["animation"]
                mime_type = animation.get("mime_type")
                result.append(
                    RichMedia(
                        file=animation,
                        height=animation.get("height"),
                        mime_type=mime_type,
                        name=animation.get("file_name"),
                        type="image" if mime_type is not None and mime_type.startswith("image/") else "video",
                        width=animation.get("width"),
                    )
                )
            case "audio":
                audio = value["audio"]
                result.append(
                    RichMedia(
                        file=audio,
                        mime_type=audio.get("mime_type"),
                        name=audio.get("file_name"),
                        type="audio",
                    )
                )
            case "photo":
                photos = value["photo"]
                photo = photos[-1] if photos else None
                if photo:
                    result.append(
                        RichMedia(
                            file=photo,
                            height=photo.get("height"),
                            type="image",
                            width=photo.get("width"),
                        )
                    )
            case "video":
                video = value["video"]
                result.append(
                    RichMedia(
                        file=video,
                        height=video.get("height"),
                        mime_type=video.get("mime_type"),
                        name=video.get("file_name"),
                        type="video",
                        width=video.get("width"),
                    )
                )
            case "voice_note":
                voice_note = value["voice_note"]
                result.append(
                    RichMedia(
                        file=voice_note,
                        mime_type=voice_note.get("mime_type"),
                        type="audio",
                    )
                )
            case _:
                pass


def rich_message_to_markdown(message: TelegramRichMessage) -> str:
    """Render a Telegram rich message to Markdown."""
    return "\n\n".join(b for b in (_block(blk) for blk in message["blocks"]) if b).strip()


def rich_message_to_text(message: TelegramRichMessage) -> str:
    """Render a Telegram rich message to plain text."""
    return "\n\n".join(b for b in (_plain_block(blk) for blk in message["blocks"]) if b).strip()


def rich_message_media(message: TelegramRichMessage) -> list[RichMedia]:
    """Extract media attachments from a Telegram rich message."""
    result: list[RichMedia] = []
    _media(message["blocks"], result)
    return result
