"""Telegram adapter for chat SDK.

Supports messaging via the Telegram Bot API using either webhook or
long-polling mode.  All conversations are keyed by Telegram chat ID
(with optional forum-topic thread IDs).

Python port of packages/adapter-telegram/src/index.ts.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import inspect
import json
import math
import os
import re
import time
from collections.abc import AsyncIterable, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, TypeVar, cast

from chat_sdk.adapters.telegram.cards import (
    card_to_telegram_inline_keyboard,
    decode_telegram_callback_data,
    empty_telegram_inline_keyboard,
)
from chat_sdk.adapters.telegram.format_converter import TelegramFormatConverter
from chat_sdk.adapters.telegram.rich import (
    rich_message_media,
    rich_message_to_markdown,
    rich_message_to_text,
    truncate_rich_markdown,
)
from chat_sdk.adapters.telegram.types import (
    TelegramAdapterConfig,
    TelegramApiResponse,
    TelegramCallbackQuery,
    TelegramChat,
    TelegramFile,
    TelegramInlineKeyboardMarkup,
    TelegramLongPollingConfig,
    TelegramMessage,
    TelegramMessageEntity,
    TelegramMessageReactionUpdated,
    TelegramRawMessage,
    TelegramReactionType,
    TelegramStickerFile,
    TelegramThreadId,
    TelegramUpdate,
    TelegramUser,
    TelegramWebhookInfo,
)
from chat_sdk.emoji import convert_emoji_placeholders, emoji_to_unicode, get_emoji
from chat_sdk.errors import ChatNotImplementedError
from chat_sdk.logger import ConsoleLogger, Logger
from chat_sdk.shared._js_compat import JS_WHITESPACE
from chat_sdk.shared.adapter_utils import (
    extract_card,
    extract_files,
    extract_postable_attachments,
)
from chat_sdk.shared.card_utils import card_to_fallback_text
from chat_sdk.shared.download import DEFAULT_LIMIT, DEFAULT_TIMEOUT_MS
from chat_sdk.shared.errors import (
    AdapterPermissionError,
    AdapterRateLimitError,
    AuthenticationError,
    NetworkError,
    ResourceNotFoundError,
    ValidationError,
)
from chat_sdk.shared.markdown_parser import ast_to_plain_text, parse_markdown, stringify_markdown
from chat_sdk.shared.streaming_markdown import StreamingMarkdownRenderer
from chat_sdk.types import (
    ActionEvent,
    AdapterPostableMessage,
    Attachment,
    Author,
    ChannelInfo,
    ChatInstance,
    EmojiValue,
    FetchOptions,
    FetchResult,
    FormattedContent,
    LockScope,
    Message,
    MessageMetadata,
    RawMessage,
    ReactionEvent,
    SlashCommandEvent,
    StreamInput,
    StreamOptions,
    ThreadInfo,
    UserInfo,
    WebhookOptions,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TELEGRAM_API_BASE = "https://api.telegram.org"
TELEGRAM_MESSAGE_LIMIT = 4096
TELEGRAM_CAPTION_LIMIT = 1024
# Inbound file downloads are capped at 25 MB and 30 s (vercel/chat#865),
# the same limits as the shared attachment downloader.
TELEGRAM_FILE_LIMIT = DEFAULT_LIMIT
TELEGRAM_FILE_TIMEOUT_MS = DEFAULT_TIMEOUT_MS
TELEGRAM_SECRET_TOKEN_HEADER = "x-telegram-bot-api-secret-token"  # pragma: allowlist secret
TELEGRAM_WEBHOOK_VERIFICATION_ERROR = (
    "secret_token is required in webhook mode. Set TELEGRAM_WEBHOOK_SECRET_TOKEN or provide "
    "secret_token. To accept unverified webhooks, set allow_unverified_webhooks=True or "
    "TELEGRAM_ALLOW_UNVERIFIED_WEBHOOKS=true."
)
# Claimed ``update_id`` keys live for 24h — Telegram stops redelivering a
# failed webhook update well within that window (vercel/chat#799).
TELEGRAM_WEBHOOK_UPDATE_TTL_MS = 24 * 60 * 60 * 1000
MESSAGE_ID_PATTERN = re.compile(r"^([^:]+):(\d+)$")
TELEGRAM_MARKDOWN_PARSE_MODE = "MarkdownV2"
MESSAGE_SEQUENCE_PATTERN = re.compile(r":(\d+)$")
# Map a normalized Attachment.type to the Telegram Bot API media method and
# its multipart form field. Port of upstream ATTACHMENT_UPLOADS (vercel/chat#485).
ATTACHMENT_UPLOADS: dict[str, dict[str, str]] = {
    "audio": {"field": "audio", "method": "sendAudio"},
    "file": {"field": "document", "method": "sendDocument"},
    "image": {"field": "photo", "method": "sendPhoto"},
    "video": {"field": "video", "method": "sendVideo"},
}
LEADING_AT_PATTERN = re.compile(r"^@+")
EMOJI_PLACEHOLDER_PATTERN = re.compile(r"^\{\{emoji:([a-z0-9_]+)\}\}$", re.IGNORECASE)
EMOJI_NAME_PATTERN = re.compile(r"^[a-z0-9_+-]+$", re.IGNORECASE)

TELEGRAM_DEFAULT_POLLING_TIMEOUT_SECONDS = 30
TELEGRAM_DEFAULT_POLLING_LIMIT = 100
TELEGRAM_DEFAULT_POLLING_RETRY_DELAY_MS = 1000
TELEGRAM_DEFAULT_STREAM_UPDATE_INTERVAL_MS = 250
# Incoming albums (vercel/chat#760): parts are buffered in state until no new
# part has arrived for the settle window, then emitted as one message.
TELEGRAM_INCOMING_MEDIA_GROUP_BUFFER_TTL_MS = 30_000
TELEGRAM_INCOMING_MEDIA_GROUP_LOCK_TTL_MS = 5_000
TELEGRAM_INCOMING_MEDIA_GROUP_RETRY_MS = 50
TELEGRAM_INCOMING_MEDIA_GROUP_SETTLE_MS = 1_000
# Albums hold at most 10 items; the incoming buffer keeps the newest 10.
TELEGRAM_MEDIA_GROUP_MAX = 10
# Upper bound for both the request-failure backoff and the per-update retry
# backoff of the polling loop (vercel/chat#942).
TELEGRAM_POLLING_MAX_BACKOFF_MS = 30_000
# Telegram rejects unparseable MarkdownV2 with a 400 whose description reads
# "Bad Request: can't parse entities: ..." ("caption entities" for media
# captions). Matched case-insensitively as a substring, like upstream's
# /can't parse (?:caption )?entities/i test().
TELEGRAM_MARKDOWN_PARSE_ERROR_PATTERN = re.compile(r"can't parse (?:caption )?entities", re.IGNORECASE)
TELEGRAM_MAX_POLLING_LIMIT = 100
TELEGRAM_MIN_POLLING_LIMIT = 1
TELEGRAM_MIN_POLLING_TIMEOUT_SECONDS = 0
TELEGRAM_MAX_POLLING_TIMEOUT_SECONDS = 300

# ---------------------------------------------------------------------------
# Internal data structures
# ---------------------------------------------------------------------------


@dataclass
class TelegramMessageAuthor:
    """Author information extracted from a Telegram user / chat."""

    full_name: str
    is_bot: bool | str  # bool or "unknown"
    is_me: bool
    user_id: str
    user_name: str


@dataclass
class ResolvedTelegramLongPollingConfig:
    """Fully resolved long-polling configuration."""

    allowed_updates: list[str] | None
    delete_webhook: bool
    drop_pending_updates: bool
    limit: int
    retry_delay_ms: int
    timeout: int


@dataclass
class TelegramParsedContent:
    """Pre-resolved formatted/plain text for a parsed Telegram message.

    Supplied by the outbound post/edit/stream paths when the SDK already
    rendered a rich message locally, so :meth:`parse_telegram_message`
    reuses that AST/text instead of re-deriving it from the raw payload.
    Port of upstream's ``content?`` argument to ``parseTelegramMessage``.
    """

    formatted: FormattedContent
    text: str


@dataclass
class RichMessageResolution:
    """Resolved native rich-message payload for an outgoing message.

    Produced by :meth:`TelegramAdapter.resolve_rich_message` when a markdown
    or AST postable is eligible for the ``sendRichMessage`` /
    ``sendRichMessageDraft`` Bot API endpoints. ``markdown`` is the
    rich-limit-truncated, emoji-resolved body sent verbatim to Telegram;
    ``formatted`` / ``text`` are the locally pre-rendered AST and plain text
    reused by :meth:`parse_telegram_message` (so the response isn't
    re-parsed). Port of upstream ``resolveRichMessage``'s return shape.
    """

    formatted: FormattedContent
    markdown: str
    text: str


TelegramRuntimeMode = str  # "webhook" | "polling"

_T = TypeVar("_T")


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _markdown_to_plain_text(markdown: str) -> str:
    """Extract plain text from a markdown string, stripping all formatting.

    Local equivalent of upstream's ``markdownToPlainText`` chat export
    (``parseMarkdown`` + ``mdast-util-to-string``). Unclosed inline markers
    (``**broken``) stay literal text in our parser, so they survive into
    the plain-text rendering — matching remark, which also treats them as
    literal when no closer exists.
    """
    return ast_to_plain_text(parse_markdown(markdown))


def _utf16_len(text: str) -> int:
    """Return the length of *text* measured in UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


def _slice_to_utf16_units(text: str, units: int) -> str:
    """Return the longest prefix of *text* whose UTF-16 length is ``<= units``.

    Never splits a non-BMP codepoint mid-surrogate-pair: each astral
    character contributes 2 UTF-16 code units and is either included
    fully or dropped entirely.
    """
    if units <= 0:
        return ""
    count = 0
    for i, ch in enumerate(text):
        ch_units = 2 if ord(ch) > 0xFFFF else 1
        if count + ch_units > units:
            return text[:i]
        count += ch_units
    return text


def _truncate_to_utf16(text: str, limit: int, ellipsis: str = "...") -> str:
    """Truncate *text* so its UTF-16 length does not exceed *limit*.

    When truncation is needed the last characters are replaced with *ellipsis*.
    """
    if _utf16_len(text) <= limit:
        return text
    ellipsis_units = _utf16_len(ellipsis)
    budget = limit - ellipsis_units
    count = 0
    cut = 0
    for i, ch in enumerate(text):
        units = 2 if ord(ch) > 0xFFFF else 1
        if count + units > budget:
            cut = i
            break
        count += units
    else:
        cut = len(text)
    return text[:cut] + ellipsis


# ---------------------------------------------------------------------------
# MarkdownV2-safe truncation
# ---------------------------------------------------------------------------
#
# Port of packages/adapter-telegram/src/markdown.ts (chat@4.27.0).
#
# Naive ``slice + "..."`` produces invalid MarkdownV2: ``.`` is a reserved
# character (must be escaped as ``\.``); a slice can leave an orphan
# trailing ``\`` that escapes the ellipsis or nothing; and a slice can cut
# through a paired entity (``*bold*``, `` `code` ``, ``[label](url)``)
# leaving it unclosed. Telegram rejects all three with
# ``Bad Request: can't parse entities``.
#
# These helpers walk back past unbalanced delimiters and orphan backslashes
# before appending an escaped ellipsis. They also run on
# under-the-limit MarkdownV2 inputs (per upstream f46a6fb / chat#446) so
# streamed chunks that arrive with a transiently unpaired opener are
# trimmed back to a parseable boundary.

# Entity delimiters whose opener/closer pairing must be preserved when
# truncating a rendered MarkdownV2 string.
_MARKDOWN_V2_ENTITY_MARKERS: tuple[str, ...] = ("*", "_", "~", "`")

_MARKDOWN_V2_ELLIPSIS = "\\.\\.\\."
_PLAIN_ELLIPSIS = "..."


def find_unescaped_positions(text: str, marker: str) -> list[int]:
    """Return indices of every occurrence of *marker* in *text* not preceded
    by an odd number of backslashes (i.e. not escaped)."""
    positions: list[int] = []
    for i, ch in enumerate(text):
        if ch != marker:
            continue
        backslashes = 0
        j = i - 1
        while j >= 0 and text[j] == "\\":
            backslashes += 1
            j -= 1
        if backslashes % 2 == 0:
            positions.append(i)
    return positions


def _find_unescaped_positions_outside_code(
    text: str,
    marker: str,
    *,
    skip_link_dest: bool = False,
) -> list[int]:
    """Like :func:`find_unescaped_positions` but skips occurrences inside
    fenced code blocks (```````) or inline code spans
    (`````). Inside those regions Telegram treats ``*``, ``_``, ``~``,
    ``[``, ``]`` as literal text.

    When ``skip_link_dest`` is True, also skips occurrences inside a
    MarkdownV2 link destination region (the ``(...)`` immediately
    following ``]``). Per Telegram's MarkdownV2 spec, only ``)`` and
    ``\\`` need escaping inside the destination -- ``_``, ``*``, ``~``
    are literal text and must not be counted as unbalanced entity
    delimiters by the safe-boundary trimmer. Without this, an under-limit
    link like ``[x](https://example.com/foo_bar)`` would be truncated to
    ``[x](https://example.com/foo`` because the trimmer saw the ``_`` as
    an unpaired italic opener.

    Port of upstream ``findUnescapedPositionsOutsideCode`` (chat#446).
    """
    positions: list[int] = []
    in_fence = False
    in_inline = False
    in_link_dest = False
    backslashes = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]

        if ch == "\\":
            backslashes += 1
            i += 1
            continue

        escaped = backslashes % 2 == 1
        backslashes = 0

        if ch == "`" and not escaped and not in_link_dest:
            is_triple = text[i + 1 : i + 2] == "`" and text[i + 2 : i + 3] == "`"
            if is_triple and not in_inline:
                in_fence = not in_fence
                i += 3
                continue
            if not in_fence:
                in_inline = not in_inline
            i += 1
            continue

        # Enter a link destination when we see ``](`` outside code -- the
        # ``]`` itself is still counted (the caller scans brackets in a
        # separate pass), but the URL inside ``(...)`` is treated as
        # literal text for delimiter-balance purposes.
        if (
            skip_link_dest
            and not in_link_dest
            and not in_fence
            and not in_inline
            and not escaped
            and ch == "]"
            and text[i + 1 : i + 2] == "("
        ):
            if ch == marker:
                positions.append(i)
            in_link_dest = True
            i += 2  # consume ``](``
            continue

        if in_link_dest and not escaped and ch == ")":
            in_link_dest = False
            i += 1
            continue

        if ch == marker and not escaped and not in_fence and not in_inline and not in_link_dest:
            positions.append(i)
        i += 1

    return positions


def ends_with_orphan_backslash(text: str) -> bool:
    """Return True if *text* ends with an odd number of trailing ``\\``."""
    trailing = 0
    i = len(text) - 1
    while i >= 0 and text[i] == "\\":
        trailing += 1
        i -= 1
    return trailing % 2 == 1


def _find_unclosed_link_dest_open_bracket(text: str) -> int | None:
    """Return the position of the ``[`` that opens an inline link whose
    destination ``(`` is never closed by ``)``.

    A truncated chunk like ``[label](https://example.com/very-long`` has
    balanced ``[]`` brackets but a dangling ``(`` -- Telegram rejects this
    as invalid MarkdownV2. The detector walks the text honouring fenced/
    inline code regions and escape backslashes, finds each ``](`` pair,
    and reports the corresponding ``[`` position if no unescaped ``)``
    closes the destination before end-of-string.
    """
    n = len(text)
    in_fence = False
    in_inline = False
    backslashes = 0
    bracket_stack: list[int] = []  # positions of unmatched ``[`` outside code
    i = 0
    while i < n:
        ch = text[i]

        if ch == "\\":
            backslashes += 1
            i += 1
            continue

        escaped = backslashes % 2 == 1
        backslashes = 0

        if ch == "`" and not escaped:
            is_triple = text[i + 1 : i + 2] == "`" and text[i + 2 : i + 3] == "`"
            if is_triple and not in_inline:
                in_fence = not in_fence
                i += 3
                continue
            if not in_fence:
                in_inline = not in_inline
            i += 1
            continue

        if escaped or in_fence or in_inline:
            i += 1
            continue

        if ch == "[":
            bracket_stack.append(i)
            i += 1
            continue

        if ch == "]" and bracket_stack:
            open_pos = bracket_stack.pop()
            # If immediately followed by ``(``, this is an inline link
            # destination. Scan forward to verify there's an unescaped
            # closing ``)`` before EOS.
            if text[i + 1 : i + 2] == "(":
                j = i + 2
                inner_backslashes = 0
                closed = False
                while j < n:
                    cj = text[j]
                    if cj == "\\":
                        inner_backslashes += 1
                        j += 1
                        continue
                    inner_escaped = inner_backslashes % 2 == 1
                    inner_backslashes = 0
                    if cj == ")" and not inner_escaped:
                        closed = True
                        break
                    j += 1
                if not closed:
                    return open_pos
            i += 1
            continue

        i += 1

    return None


def _trim_to_markdown_v2_safe_boundary(text: str) -> str:
    """Drop trailing characters that would produce invalid MarkdownV2.

    Drops:
      - orphan trailing ``\\`` (would escape the appended ellipsis or nothing)
      - unclosed entity delimiter (``*``, ``_``, ``~``, `` ` ``) whose closer
        was cut off
      - unmatched ``[`` from a link whose closer was cut off
      - inline link with balanced ``[]`` but unclosed ``(`` destination
        (e.g. ``[label](https://example.com/very-long``) -- chunk is
        trimmed back to the opening ``[``

    Best-effort: may drop more than strictly necessary in edge cases, but
    guarantees the output is parseable MarkdownV2 (when the input was).
    """
    current = text
    max_iterations = len(current) + 1

    for _ in range(max_iterations):
        if ends_with_orphan_backslash(current):
            current = current[:-1]
            continue

        min_unsafe_position = len(current)

        for marker in _MARKDOWN_V2_ENTITY_MARKERS:
            if marker == "`":
                positions = find_unescaped_positions(current, marker)
            else:
                positions = _find_unescaped_positions_outside_code(current, marker, skip_link_dest=True)
            if len(positions) % 2 == 1:
                last_unpaired = positions[-1] if positions else len(current)
                if last_unpaired < min_unsafe_position:
                    min_unsafe_position = last_unpaired

        open_brackets = _find_unescaped_positions_outside_code(current, "[")
        close_brackets = _find_unescaped_positions_outside_code(current, "]")
        if len(open_brackets) > len(close_brackets):
            last_open = open_brackets[-1] if open_brackets else len(current)
            if last_open < min_unsafe_position:
                min_unsafe_position = last_open

        unclosed_link_open = _find_unclosed_link_dest_open_bracket(current)
        if unclosed_link_open is not None and unclosed_link_open < min_unsafe_position:
            min_unsafe_position = unclosed_link_open

        if min_unsafe_position >= len(current):
            return current

        current = current[:min_unsafe_position]

    return current


def truncate_for_telegram(text: str, limit: int, parse_mode: str | None) -> str:
    """Truncate *text* to *limit* UTF-16 code units, appending an ellipsis.

    For MarkdownV2 (``parse_mode == "MarkdownV2"``), uses an escaped
    ellipsis (``\\.\\.\\.``) and trims back past any unbalanced entity
    delimiter or orphan backslash before appending. Plain text gets a
    literal ``...``.

    Even when *text* is under the limit, MarkdownV2 inputs go through
    :func:`_trim_to_markdown_v2_safe_boundary` so that streamed chunks
    with transiently unpaired entity markers don't trigger Telegram's
    ``can't parse entities`` 400 (port of chat#446 / upstream f46a6fb).

    ``limit`` is interpreted in UTF-16 code units to match Telegram's
    documented 4096 / 1024 caps and upstream JavaScript's ``string.length``
    semantics. Non-BMP characters (e.g. emoji) consume 2 UTF-16 code
    units each, so a 4096-emoji MarkdownV2 message would otherwise sail
    past this check and be rejected by Telegram as too long.
    """
    is_markdown_v2 = parse_mode == "MarkdownV2"

    if _utf16_len(text) <= limit:
        return _trim_to_markdown_v2_safe_boundary(text) if is_markdown_v2 else text

    ellipsis = _MARKDOWN_V2_ELLIPSIS if is_markdown_v2 else _PLAIN_ELLIPSIS
    sliced = _slice_to_utf16_units(text, limit - _utf16_len(ellipsis))

    if is_markdown_v2:
        sliced = _trim_to_markdown_v2_safe_boundary(sliced)

    return f"{sliced}{ellipsis}"


def _trim_trailing_slashes(url: str) -> str:
    """Remove trailing ``/`` characters from *url*."""
    end = len(url)
    while end > 0 and url[end - 1] == "/":
        end -= 1
    return url[:end]


def _integral_update_id(value: Any) -> int | None:
    """Return ``value`` as an ``int`` when JS ``Number.isInteger`` would accept it.

    ``json.loads`` keeps ``7.0`` / ``7e0`` as floats, but in JS they are the
    number 7 and render as ``"7"`` in the claim key, so they are normalised to
    ``int`` here. ``bool`` (an ``int`` subclass) and non-finite or fractional
    floats are rejected.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _js_number_str(value: Any) -> str:
    """Render a JSON number the way JavaScript's ``String(number)`` does.

    Python's ``str(float)`` differs from JS for integral floats (``51.0`` vs
    ``51``) and switches to exponent notation at other thresholds (``1e-05``
    vs ``0.00001``). ``repr`` already yields the shortest round-tripping
    digits (the same digits JS picks), so only the notation is rewritten:
    fixed-point when the leading digit's exponent is in ``(-7, 21)``,
    otherwise ``d[.ddd]e+n`` / ``d[.ddd]e-n``.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    if value == 0:
        return "0"
    number = Decimal(repr(value)).normalize()
    exponent = number.adjusted()
    if -7 < exponent < 21:
        return format(number, "f")
    sign, digits, _ = number.as_tuple()
    text = "".join(str(d) for d in digits)
    mantissa = text[0] + (f".{text[1:]}" if len(text) > 1 else "")
    return f"{'-' if sign else ''}{mantissa}e{'+' if exponent >= 0 else '-'}{abs(exponent)}"


# An invoice's total_amount is in the currency's smallest unit, and the
# exponent varies per currency: Telegram's own list
# (https://core.telegram.org/bots/payments/currencies.json) has these seven
# at 0 and three at 3, with everything else at 2. XTR (Telegram Stars) is
# not in that list and counts whole Stars.
_ZERO_EXPONENT_CURRENCIES = frozenset({"CLP", "ISK", "JPY", "KRW", "PYG", "UGX", "VND", "XTR"})
_THREE_EXPONENT_CURRENCIES = frozenset({"BHD", "IQD", "JOD"})


def _format_invoice_amount(total_amount: float, currency: str) -> str:
    """Port of upstream ``formatInvoiceAmount``: ``(amount / 10**e).toFixed(e)``."""
    exponent = 2
    if currency in _ZERO_EXPONENT_CURRENCIES:
        exponent = 0
    elif currency in _THREE_EXPONENT_CURRENCIES:
        exponent = 3
    return f"{total_amount / 10**exponent:.{exponent}f}"


def _describe_non_file_content(raw: TelegramMessage) -> str | None:
    """Readable text for message kinds that carry neither text nor a file.

    Port of upstream ``describeNonFileContent`` (vercel/chat#836). Without it
    a location, poll, etc. arrives with ``text == ""`` and a handler cannot
    tell it from an empty delivery; the structured payload stays on ``raw``.
    Presence checks are ``is not None`` because a JS object is always truthy.
    """
    # Telegram sets ``location`` alongside ``venue`` for backward
    # compatibility, so the venue check has to come first.
    venue = raw.get("venue")
    if venue is not None:
        return f"📍 {venue.get('title')}, {venue.get('address')}"
    location = raw.get("location")
    if location is not None:
        latitude = _js_number_str(location.get("latitude"))
        longitude = _js_number_str(location.get("longitude"))
        return f"📍 {latitude}, {longitude}"
    contact = raw.get("contact")
    if contact is not None:
        name = " ".join(part for part in (contact.get("first_name"), contact.get("last_name")) if part)
        return f"👤 {name} {contact.get('phone_number')}".strip(JS_WHITESPACE)
    poll = raw.get("poll")
    if poll is not None:
        return f"📊 {poll.get('question')}"
    dice = raw.get("dice")
    if dice is not None:
        return f"{dice.get('emoji')} {_js_number_str(dice.get('value'))}"
    game = raw.get("game")
    if game is not None:
        return f"🎮 {game.get('title')}"
    invoice = raw.get("invoice")
    if invoice is not None:
        currency = invoice.get("currency", "")
        amount = _format_invoice_amount(invoice.get("total_amount", 0), currency)
        return f"🧾 {invoice.get('title')} — {amount} {currency}"
    if raw.get("story") is not None:
        return "📖 Story"
    return None


def _sticker_attachment_format(sticker: TelegramStickerFile) -> tuple[str, str]:
    """Attachment ``(type, mime_type)`` for a sticker's real file format.

    A still sticker is WebP, a video sticker WebM and an animated (Lottie)
    one the TGS container. The type follows the real format so type-driven
    consumers never route a WebM through ``sendPhoto``. Port of upstream
    ``stickerAttachmentFormat``.
    """
    if sticker.get("is_video"):
        return "video", "video/webm"
    if sticker.get("is_animated"):
        return "file", "application/x-tgsticker"
    return "image", "image/webp"


def _escape_markdown_in_entity(text: str) -> str:
    """Escape markdown-special characters inside entity text."""
    return re.sub(r"([\[\]()\\])", r"\\\1", text)


def apply_telegram_entities(
    text: str,
    entities: list[TelegramMessageEntity],
) -> str:
    """Convert Telegram message entities to markdown.

    Telegram delivers formatting as separate entity objects alongside plain
    text.  This function reconstructs markdown so that links, bold, italic,
    code, etc. are preserved when the text is later parsed as markdown.

    Entities use UTF-16 offsets which match JavaScript string indexing.
    Python strings are UTF-32, so we encode to UTF-16-LE for correct slicing.
    """
    if not entities:
        return text

    # Encode to UTF-16 LE for correct offset handling (2 bytes per code unit)
    utf16_bytes = text.encode("utf-16-le")

    # Sort entities by offset descending so replacements don't shift later offsets.
    # For entities at the same offset, apply the shorter (inner) one first.
    sorted_entities = sorted(
        entities,
        key=lambda e: (-e.get("offset", 0), e.get("length", 0)),
    )

    for entity in sorted_entities:
        offset = entity.get("offset", 0)
        length = entity.get("length", 0)
        start_byte = offset * 2
        end_byte = (offset + length) * 2
        entity_text = utf16_bytes[start_byte:end_byte].decode("utf-16-le")

        replacement: str | None = None
        entity_type = entity.get("type", "")

        if entity_type == "text_link":
            url = entity.get("url")
            if url:
                replacement = f"[{_escape_markdown_in_entity(entity_text)}]({url})"
        elif entity_type == "bold":
            replacement = f"**{entity_text}**"
        elif entity_type == "italic":
            replacement = f"*{entity_text}*"
        elif entity_type == "code":
            replacement = f"`{entity_text}`"
        elif entity_type == "pre":
            lang = entity.get("language") or ""
            replacement = f"```{lang}\n{entity_text}\n```"
        elif entity_type == "strikethrough":
            replacement = f"~~{entity_text}~~"

        if replacement is not None:
            replacement_bytes = replacement.encode("utf-16-le")
            utf16_bytes = utf16_bytes[:start_byte] + replacement_bytes + utf16_bytes[end_byte:]

    return utf16_bytes.decode("utf-16-le")


# =========================================================================
# TelegramAdapter
# =========================================================================


class TelegramAdapter:
    """Telegram adapter for chat SDK.

    Implements the Adapter interface for the Telegram Bot API.
    """

    def __init__(self, config: TelegramAdapterConfig | None = None) -> None:
        if config is None:
            config = TelegramAdapterConfig()

        bot_token = config.bot_token or os.environ.get("TELEGRAM_BOT_TOKEN")
        if not bot_token:
            raise ValidationError(
                "telegram",
                "botToken is required. Set TELEGRAM_BOT_TOKEN or provide it in config.",
            )

        self._name: str = "telegram"
        self._lock_scope: LockScope = "channel"
        self._persist_thread_history: bool = True

        self._bot_token: str = bot_token
        self._api_base_url: str = _trim_trailing_slashes(
            config.api_base_url or os.environ.get("TELEGRAM_API_BASE_URL") or TELEGRAM_API_BASE
        )
        # ``??`` semantics (vercel/chat#858): an explicit empty-string secret is
        # kept (and is falsy, so verification is still required) rather than
        # silently falling back to the env var.
        self._secret_token: str | None = (
            config.secret_token if config.secret_token is not None else os.environ.get("TELEGRAM_WEBHOOK_SECRET_TOKEN")
        )
        # Only the exact env string "true" opts out; an explicit config value
        # (including False) always wins over the env var.
        # A non-bool value (e.g. the string "false" copied from a settings
        # file) is rejected rather than coerced with ``bool()``, which would
        # silently fail open on this security flag.
        if config.allow_unverified_webhooks is not None and not isinstance(config.allow_unverified_webhooks, bool):
            raise ValidationError(
                "telegram",
                f"allow_unverified_webhooks must be a bool, got {type(config.allow_unverified_webhooks).__name__}",
            )
        self._allow_unverified_webhooks: bool = (
            config.allow_unverified_webhooks
            if config.allow_unverified_webhooks is not None
            else os.environ.get("TELEGRAM_ALLOW_UNVERIFIED_WEBHOOKS") == "true"
        )
        self._warned_no_verification: bool = False
        # User allowlist (vercel/chat#742): ``allowedUserIds ??
        # TELEGRAM_ALLOWED_USER_IDS.split(",")``, each id stringified and
        # trimmed, empties dropped. An empty result allows everyone (None).
        raw_allowed_user_ids: list[int | str] | None = config.allowed_user_ids
        # Python-only guard: a bare string (e.g. ``"123,456"`` straight from an
        # env var) would iterate per character; upstream's ``.map`` throws.
        if raw_allowed_user_ids is not None and not isinstance(raw_allowed_user_ids, list | tuple | set | frozenset):
            raise ValidationError(
                "telegram",
                f"allowed_user_ids must be a list of user IDs, got {type(raw_allowed_user_ids).__name__}",
            )
        if raw_allowed_user_ids is None:
            env_allowed_user_ids = os.environ.get("TELEGRAM_ALLOWED_USER_IDS")
            if env_allowed_user_ids is not None:
                raw_allowed_user_ids = list(env_allowed_user_ids.split(","))
        normalized_allowed_user_ids = (
            [
                normalized
                for normalized in (_js_number_str(value).strip(JS_WHITESPACE) for value in raw_allowed_user_ids)
                if normalized
            ]
            if raw_allowed_user_ids is not None
            else []
        )
        self._allowed_user_ids: set[str] | None = (
            set(normalized_allowed_user_ids) if normalized_allowed_user_ids else None
        )
        self._logger: Logger = config.logger or ConsoleLogger("info").child("telegram")
        # Mention regex compiled once per username (vercel/chat#706).
        self._mention_regex: re.Pattern[str] | None = None
        self._mention_regex_username: str | None = None
        # Strong references to fire-and-forget typing tasks so they are not
        # garbage-collected mid-flight when no ``wait_until`` is supplied.
        self._typing_tasks: set[asyncio.Task[None]] = set()
        self._format_converter: TelegramFormatConverter = TelegramFormatConverter()
        self._message_cache: dict[str, list[Message]] = {}

        self._chat: ChatInstance | None = None
        self._bot_user_id: str | None = None
        # Shared in-flight ``getMe`` (vercel/chat#813) and the stable
        # ``sha256(bot_user_id)`` scope used to key webhook update claims.
        self._bot_identity_task: asyncio.Task[None] | None = None
        self._webhook_scope: str | None = None
        # Username reported by ``getMe``. ``_ensure_bot_identity`` short-circuits
        # once the scope is cached, so a re-``initialize`` (``Chat.shutdown`` then
        # ``Chat.initialize``) must restore it here after applying the Chat-level
        # name, or ``@real_bot`` mentions / ``/cmd@real_bot`` stop routing.
        self._resolved_user_name: str | None = None

        explicit_user_name = config.user_name or os.environ.get("TELEGRAM_BOT_USERNAME")
        self._user_name: str = self.normalize_user_name(explicit_user_name or "bot")
        self._has_explicit_user_name: bool = bool(explicit_user_name)

        self._mode: str = config.mode or "auto"
        self._long_polling: TelegramLongPollingConfig | None = config.long_polling

        self._runtime_mode: TelegramRuntimeMode = "webhook"
        self._polling_task: asyncio.Task[None] | None = None
        self._polling_active: bool = False
        # True only while the polling loop is parked in ``getUpdates`` or one
        # of its sleeps: the awaits upstream's per-iteration AbortController
        # reaches. ``stop_polling`` cancels the loop only then, so in-flight
        # handlers and checkpoint writes finish first, as upstream.
        self._polling_interruptible: bool = False
        # Strong references to webhook album tasks (vercel/chat#760) so they
        # are not garbage-collected mid-settle when no ``wait_until`` is given.
        self._media_group_tasks: set[asyncio.Task[None]] = set()

        # Draft-id counter for native DM draft streaming (vercel/chat#340).
        # Seeded from wall-clock millis (mod int32 max) so concurrent bot
        # restarts don't reuse the previous process's draft ids; wraps to 1
        # at 2_147_483_647 to stay within Telegram's signed-int32 range.
        self._next_draft_id: int = max(1, int(time.time() * 1000) % 2_147_483_647)

        # Native rich-message availability (vercel/chat#479). Starts True and
        # latches to False the first time Telegram reports the rich-message
        # endpoints are missing/unsupported (a 404 ``method not found`` or a
        # ``rich message ... unsupported`` validation error), so subsequent
        # sends skip the rich attempt entirely and go straight to MarkdownV2.
        # Transient ``can't parse`` failures fall back per-message without
        # flipping this flag.
        self._rich_messages_available: bool = True

        # Shared aiohttp session for connection pooling
        self._http_session: Any | None = None

        if self._mode not in ("auto", "webhook", "polling"):
            raise ValidationError(
                "telegram",
                f'Invalid mode: {self._mode}. Expected "auto", "webhook", or "polling".',
            )
        if self._mode == "webhook" and not self._webhook_verification_configured():
            raise ValidationError("telegram", TELEGRAM_WEBHOOK_VERIFICATION_ERROR)

    # -- Properties ----------------------------------------------------------

    @property
    def name(self) -> str:
        return self._name

    @property
    def lock_scope(self) -> LockScope:
        return self._lock_scope

    @property
    def persist_thread_history(self) -> bool:
        return self._persist_thread_history

    @property
    def bot_user_id(self) -> str | None:
        return self._bot_user_id

    @property
    def user_name(self) -> str:
        return self._user_name

    @property
    def is_polling(self) -> bool:
        return self._polling_active

    @property
    def runtime_mode(self) -> TelegramRuntimeMode:
        return self._runtime_mode

    # -- Lifecycle -----------------------------------------------------------

    def _webhook_verification_configured(self) -> bool:
        """Whether webhook requests are verified or explicitly allowed unverified."""
        return bool(self._secret_token) or self._allow_unverified_webhooks

    async def _ensure_bot_identity(self) -> None:
        """Resolve the bot identity via ``getMe`` once, sharing any in-flight call.

        On success sets ``bot_user_id``, refines the username (unless one was
        configured explicitly) and derives the webhook dedupe scope from
        ``sha256(bot_user_id)`` — not the token, so claims survive token
        rotation. The pending task is cleared on success *and* failure so a
        failed lookup is retried on the next call.

        Port of upstream ``ensureBotIdentity`` (vercel/chat#813).
        """
        if self._webhook_scope:
            return
        task = self._bot_identity_task
        # A finished task here can only be a failed/cancelled lookup whose
        # clearing callback has not run yet (success sets the scope above):
        # start a fresh lookup rather than re-raising the stale failure.
        if task is None or task.done():
            task = asyncio.get_running_loop().create_task(self._fetch_bot_identity())
            self._bot_identity_task = task

            def _clear(done: asyncio.Task[None]) -> None:
                if self._bot_identity_task is done:
                    self._bot_identity_task = None
                # Mark the exception retrieved so a failure nobody else awaits
                # does not log "Task exception was never retrieved".
                if not done.cancelled():
                    done.exception()

            task.add_done_callback(_clear)
        # Shield so cancelling one waiter (e.g. a timed-out webhook request)
        # does not cancel the shared lookup for the others.
        await asyncio.shield(task)

    async def _fetch_bot_identity(self) -> None:
        me: TelegramUser = await self.telegram_fetch("getMe")
        bot_user_id = str(me.get("id", ""))
        self._bot_user_id = bot_user_id
        self._webhook_scope = hashlib.sha256(bot_user_id.encode("utf-8")).hexdigest()
        if not self._has_explicit_user_name and me.get("username"):
            self._resolved_user_name = self.normalize_user_name(me["username"])
            self._user_name = self._resolved_user_name
        self._logger.info(
            "Telegram bot identity resolved",
            {"botUserId": self._bot_user_id, "userName": self._user_name},
        )

    async def initialize(self, chat: ChatInstance) -> None:
        """Initialize the adapter and fetch bot identity via ``getMe``."""
        self._chat = chat

        if not self._has_explicit_user_name:
            chat_user_name = getattr(chat, "get_user_name", None)
            if callable(chat_user_name):
                resolved = chat_user_name()
                if isinstance(resolved, str) and resolved.strip():
                    self._user_name = self.normalize_user_name(resolved)
            # ``getMe`` refines the Chat-level name; when the identity is
            # already cached the lookup below is skipped, so reapply it here.
            if self._resolved_user_name:
                self._user_name = self._resolved_user_name

        try:
            await self._ensure_bot_identity()
        except Exception as error:
            self._logger.warn(
                "Failed to fetch Telegram bot identity",
                {"error": str(error)},
            )

        runtime_mode = await self.resolve_runtime_mode()
        self._runtime_mode = runtime_mode

        # Fail closed (vercel/chat#858): auto mode that resolves to webhook
        # needs a secret or an explicit opt-out, same as ``mode="webhook"``.
        if runtime_mode == "webhook" and not self._webhook_verification_configured():
            raise ValidationError("telegram", TELEGRAM_WEBHOOK_VERIFICATION_ERROR)

        if runtime_mode == "polling":
            polling_config = self._long_polling
            if self._mode == "auto":
                if polling_config:
                    merged = TelegramLongPollingConfig(
                        allowed_updates=polling_config.allowed_updates,
                        delete_webhook=False,
                        drop_pending_updates=polling_config.drop_pending_updates,
                        limit=polling_config.limit,
                        retry_delay_ms=polling_config.retry_delay_ms,
                        timeout=polling_config.timeout,
                    )
                    await self.start_polling(merged)
                else:
                    await self.start_polling(TelegramLongPollingConfig(delete_webhook=False))
            else:
                await self.start_polling(polling_config)

    async def get_user(self, user_id: str) -> UserInfo | None:
        """Look up a Telegram user via ``getChat``.

        Telegram has no public ``users.get`` API for bots — ``getChat``
        with a ``chat_id`` of the user is the closest equivalent and only
        succeeds when the user has interacted with the bot at least once
        (so the bot has a private chat record). We restrict resolution to
        ``type == "private"`` chats so a group/supergroup ID never gets
        misreported as a user.

        ``is_bot`` is always ``False`` because ``getChat`` does not expose
        that field on the chat shape — callers needing bot detection
        should use ``message.author.is_bot`` from incoming events.

        Mirrors upstream ``TelegramAdapter.getUser`` (vercel/chat#391).
        """
        try:
            chat = await self.telegram_fetch("getChat", {"chat_id": user_id})
        except Exception:
            return None
        if not isinstance(chat, dict) or chat.get("type") != "private":
            return None
        first = chat.get("first_name") or ""
        last = chat.get("last_name") or ""
        full_name = " ".join(part for part in (first, last) if part)
        chat_id_str = str(chat.get("id", user_id))
        return UserInfo(
            user_id=chat_id_str,
            user_name=chat.get("username") or chat.get("first_name") or chat_id_str,
            full_name=full_name or chat_id_str,
            # Documented divergence from upstream parity: getChat doesn't
            # expose is_bot. See docstring above.
            is_bot=False,
            email=None,
            avatar_url=None,
        )

    async def handle_webhook(
        self,
        request: Any,
        options: WebhookOptions | None = None,
    ) -> Any:
        """Handle an incoming Telegram webhook request.

        Validates the secret token header, parses the JSON update, claims its
        ``update_id`` so redeliveries are dispatched once, and dispatches it to
        ``process_update``.
        """
        # Fail closed before reading the body (vercel/chat#858).
        if not self._webhook_verification_configured():
            self._logger.warn(
                "Telegram webhook rejected because verification is not configured",
            )
            return self._make_response("Webhook verification required", 401)
        if self._secret_token:
            header_token = self._get_header(request, TELEGRAM_SECRET_TOKEN_HEADER)
            valid = False
            try:
                if header_token:
                    valid = hmac.compare_digest(header_token, self._secret_token)
            except Exception:
                pass
            if not valid:
                self._logger.warn(
                    "Telegram webhook rejected due to invalid secret token",
                )
                return self._make_response("Invalid secret token", 401)
        elif not self._warned_no_verification:
            self._warned_no_verification = True
            self._logger.warn("Telegram webhook verification is explicitly disabled")

        try:
            body = await self._get_request_body(request)
            update: TelegramUpdate = json.loads(body)
        except Exception:
            return self._make_response("Invalid JSON", 400)

        if not self._chat:
            self._logger.warn(
                "Chat instance not initialized, ignoring Telegram webhook",
            )
            return self._make_response("OK", 200)

        # Deduplicate redeliveries by ``update_id`` (vercel/chat#799). Core
        # message dedupe does not cover callback queries, so without this a
        # Telegram retry could run a button action twice. Only integral ids are
        # claimed, mirroring ``Number.isInteger``: an integral JSON float
        # (``7.0`` / ``7e0``) is the JS number 7 and shares the ``...:7`` key;
        # ``bool`` is excluded (an ``int`` subclass in Python). Anything else is
        # dispatched unclaimed, matching upstream.
        raw_update_id = update.get("update_id") if isinstance(update, dict) else None
        update_id = _integral_update_id(raw_update_id)
        if update_id is not None:
            webhook_scope = self._webhook_scope
            if not webhook_scope:
                try:
                    await self._ensure_bot_identity()
                    webhook_scope = self._webhook_scope
                except Exception as error:
                    self._logger.warn(
                        "Telegram webhook update could not resolve bot identity",
                        {"error": str(error)},
                    )
                    return self._make_response("Service unavailable", 503)
            if not webhook_scope:
                return self._make_response("Service unavailable", 503)
            try:
                claimed = await self._chat.get_state().set_if_not_exists(
                    f"{self._name}:webhook-update:{webhook_scope}:{update_id}",
                    True,
                    TELEGRAM_WEBHOOK_UPDATE_TTL_MS,
                )
            except Exception as error:
                self._logger.warn(
                    "Failed to claim Telegram webhook update",
                    {"error": str(error), "updateId": update_id},
                )
                return self._make_response("Service unavailable", 503)
            if not claimed:
                self._logger.debug(
                    "Ignoring duplicate Telegram webhook update",
                    {"updateId": update_id},
                )
                return self._make_response("OK", 200)

        try:
            self.process_update(update, options)
        except Exception as error:
            self._logger.warn(
                "Failed to process Telegram webhook update",
                # Not ``update.get(...)``: an authenticated non-object body
                # (e.g. ``[1]``) lands here and must not raise from the log.
                {"error": str(error), "updateId": raw_update_id},
            )

        return self._make_response("OK", 200)

    async def _get_http_session(self) -> Any:
        """Return the shared aiohttp session, creating it lazily if needed."""
        import aiohttp

        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession()
        return self._http_session

    async def disconnect(self) -> None:
        """Disconnect the adapter, stop polling, and close the shared HTTP session."""
        # Python-only: ``Chat.shutdown`` cancels in-flight handler tasks before
        # disconnecting adapters, but an album still settling would dispatch
        # a new handler afterwards (and keep ``stop_polling`` waiting on it).
        # Cancel albums first; a polled album then stays in the checkpoint.
        media_group_tasks = [task for task in self._media_group_tasks if not task.done()]
        for task in media_group_tasks:
            task.cancel()
        if media_group_tasks:
            await asyncio.wait(media_group_tasks)
        await self.stop_polling()
        # Python-only: a pending receipt-typing task would otherwise reopen the
        # shared aiohttp session via ``_get_http_session`` after it is closed.
        typing_tasks = [task for task in self._typing_tasks if not task.done()]
        for task in typing_tasks:
            task.cancel()
        if typing_tasks:
            await asyncio.gather(*typing_tasks, return_exceptions=True)
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
            self._http_session = None

    # -- Polling -------------------------------------------------------------

    async def start_polling(self, config: TelegramLongPollingConfig | None = None) -> None:
        """Start long-polling for updates."""
        if not self._chat:
            raise ValidationError(
                "telegram",
                "Cannot start polling before initialize()",
            )

        if self._polling_active:
            self._logger.debug("Telegram polling already active")
            return

        resolved_config = self.resolve_polling_config(config)
        previous_runtime_mode = self._runtime_mode
        self._polling_active = True

        try:
            if resolved_config.delete_webhook:
                await self.reset_webhook(resolved_config.drop_pending_updates)
            self._runtime_mode = "polling"
        except Exception:
            self._polling_active = False
            self._runtime_mode = previous_runtime_mode
            raise

        self._logger.info(
            "Telegram polling started",
            {
                "limit": resolved_config.limit,
                "timeout": resolved_config.timeout,
                "allowedUpdates": resolved_config.allowed_updates,
            },
        )

        async def _run_polling() -> None:
            try:
                await self.polling_loop(resolved_config)
            finally:
                self._polling_active = False
                self._polling_task = None

        try:
            self._polling_task = asyncio.get_running_loop().create_task(_run_polling())
        except RuntimeError:
            self._polling_task = None
            self._polling_active = False
            self._logger.error("No running event loop to start polling task")

    async def stop_polling(self) -> None:
        """Stop long-polling.

        Like upstream's ``AbortController``, this interrupts only a pending
        ``getUpdates`` request or a polling sleep, so a blocked long poll does
        not hang shutdown for ~30 s. When the loop is waiting for update
        handlers or writing its checkpoint, that step finishes first and the
        loop then exits, so the checkpoint stays consistent.
        """
        if not self._polling_active:
            return
        self._polling_active = False
        task = self._polling_task
        if task is not None and not task.done():
            if self._polling_interruptible:
                task.cancel()
            # ``asyncio.wait`` rather than ``await task``: the loop's own
            # cancellation is expected, but cancelling *this* caller must
            # still propagate (``suppress(CancelledError)`` would swallow it).
            await asyncio.wait({task})
            if not task.cancelled():
                task.result()
        self._polling_task = None
        self._logger.info("Telegram polling stopped")

    async def reset_webhook(self, drop_pending_updates: bool = False) -> None:
        """Delete the current Telegram webhook."""
        await self.telegram_fetch(
            "deleteWebhook",
            {"drop_pending_updates": drop_pending_updates},
        )
        self._logger.info(
            "Telegram webhook reset",
            {"dropPendingUpdates": drop_pending_updates},
        )

    def _now_ms(self) -> int:
        """Epoch milliseconds for album settle windows and polling retries.

        Epoch time (upstream ``Date.now()``), not monotonic, because
        ``receivedAt`` / ``retryAt`` are persisted in state and compared after
        a restart or on another instance. Overridable so tests can inject a
        fake clock (together with :meth:`_sleep`).
        """
        return int(time.time() * 1000)

    async def _sleep(self, delay_ms: float) -> None:
        """Port of upstream ``sleep``; overridable so tests can fake time."""
        if delay_ms <= 0:
            return
        await asyncio.sleep(delay_ms / 1000)

    async def _polling_abortable(self, awaitable: Awaitable[_T]) -> _T:
        """Await *awaitable* as a point where ``stop_polling`` may cancel the loop.

        The Python stand-in for upstream's per-iteration ``AbortController``,
        whose signal reaches only ``getUpdates`` and the loop's sleeps.
        """
        self._polling_interruptible = True
        try:
            return await awaitable
        finally:
            self._polling_interruptible = False

    def _polling_group(self, update: TelegramUpdate) -> str | None:
        """Key shared by the polled parts of one album, or ``None``.

        Port of upstream ``pollingGroup``: parts are grouped by chat, forum
        topic and ``media_group_id``. With an allowlist, a part from a user
        outside it is not grouped (it is then dropped by :meth:`process_update`
        without ever being buffered in the checkpoint).
        """
        message = self._message_update(update)
        if self._allowed_user_ids is not None:
            sender = cast(
                "TelegramUser | None",
                None if message is None else (message.get("from_user") or message.get("from")),  # type: ignore[call-overload]
            )
            if sender is None or _js_number_str(sender.get("id")) not in self._allowed_user_ids:
                return None
        if message is None or not message.get("media_group_id"):
            return None
        return json.dumps([message["chat"]["id"], message.get("message_thread_id"), message.get("media_group_id")])

    def _polling_group_key(self, update: TelegramUpdate) -> str:
        group = self._polling_group(update)
        return group if group is not None else str(update.get("update_id"))

    @staticmethod
    async def _await_update_tasks(tasks: list[asyncio.Task[Any]]) -> None:
        """Wait for every handler task of one update, then raise the first failure.

        ``Promise.allSettled`` + rethrow. ``asyncio.wait`` never cancels the
        handler tasks, even if the polling loop itself is cancelled.
        """
        if not tasks:
            return
        await asyncio.wait(tasks)
        for task in tasks:
            if task.cancelled():
                # Python-only: ``Chat.shutdown`` cancels in-flight handler
                # tasks. Count that as a failure so the update stays in the
                # checkpoint instead of being acknowledged unhandled.
                raise RuntimeError("Telegram update handler was cancelled")
            error = task.exception()
            if error is not None:
                raise error

    async def _process_polling_updates(
        self,
        updates: list[TelegramUpdate],
        completed: set[int],
    ) -> list[tuple[TelegramUpdate, Exception | None]]:
        """Dispatch polled updates and wait for their handlers.

        Port of upstream ``processPollingUpdates``. The parts of one album
        succeed or fail together; updates already in *completed* are not
        dispatched again. Returns ``(update, error)`` pairs in ``update_id``
        order, where ``error`` is ``None`` for a handled update.
        """
        groups: dict[str, list[TelegramUpdate]] = {}
        for update in updates:
            groups.setdefault(self._polling_group_key(update), []).append(update)

        async def run_update(update: TelegramUpdate) -> None:
            if update["update_id"] in completed:
                return
            # The checkpoint, not core dedupe, deduplicates polled updates: a
            # retried update must not be dropped as a duplicate (#942).
            await self._await_update_tasks(self.process_update(update, WebhookOptions(deduplicate=False)))

        async def run_group(group: list[TelegramUpdate]) -> list[tuple[TelegramUpdate, Exception | None]]:
            try:
                results = await asyncio.gather(*(run_update(update) for update in group), return_exceptions=True)
                for result in results:
                    if isinstance(result, BaseException):
                        raise result
                for update in group:
                    completed.add(update["update_id"])
                return [(update, None) for update in group]
            except Exception as error:
                return [(update, error) for update in group]

        grouped = await asyncio.gather(*(run_group(group) for group in groups.values()))
        return sorted(
            (result for results in grouped for result in results),
            key=lambda result: result[0]["update_id"],
        )

    async def _fetch_polling_updates(self, params: dict[str, Any], collect_ms: int | None) -> list[TelegramUpdate]:
        """Call ``getUpdates``, giving up with no updates after *collect_ms*.

        Upstream aborts the long poll with a timer (``collecting``) once a
        pending album has settled or a retry falls due. Abandoned updates are
        not acknowledged, so Telegram returns them again on the next poll.
        """
        if collect_ms is None or collect_ms <= 0:
            return await self.telegram_fetch("getUpdates", params)
        fetch = asyncio.ensure_future(self.telegram_fetch("getUpdates", params))
        timer = asyncio.ensure_future(self._sleep(collect_ms))
        try:
            await asyncio.wait({fetch, timer}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            timer.cancel()
            if not fetch.done():
                fetch.cancel()
        # A cancelled fetch only finishes cancelling once it runs again.
        await asyncio.wait({fetch})
        if fetch.cancelled():
            return []
        return fetch.result()

    async def polling_loop(self, config: ResolvedTelegramLongPollingConfig) -> None:
        """Long-poll ``getUpdates``, acknowledging only settled updates.

        Port of upstream ``pollingLoop`` (vercel/chat#942). Each batch's
        handler tasks are awaited before ``offset`` moves past it. A failed
        update (or every part of a failed album) is saved in the
        ``telegram:polling:{scope}`` checkpoint with exponential backoff and
        retried; album parts wait there for the settle window and are then
        dispatched together. ``stop_polling`` cancels the loop only while it
        waits in ``getUpdates`` or a sleep (see :meth:`_polling_abortable`).
        """
        if not self._chat:
            return
        state = self._chat.get_state()
        checkpoint: dict[str, Any] | None = None
        consecutive_failures = 0
        drained = False
        completed: set[int] = set()

        def retry(entries: list[dict[str, Any]], error: Exception) -> dict[str, Any]:
            attempts = max((entry.get("attempts") if entry.get("attempts") is not None else 0) for entry in entries) + 1
            retry_after = error.retry_after if isinstance(error, AdapterRateLimitError) else None
            delay = max(
                min(
                    max(config.retry_delay_ms, TELEGRAM_DEFAULT_POLLING_RETRY_DELAY_MS) * 2 ** min(attempts - 1, 30),
                    TELEGRAM_POLLING_MAX_BACKOFF_MS,
                ),
                retry_after * 1000 if retry_after is not None else 0,
            )
            retry_at = self._now_ms() + int(delay)
            self._logger.warn(
                "Telegram polling update processing failed",
                {
                    "error": str(error),
                    "updateId": entries[0]["update"].get("update_id"),
                    "attempts": attempts,
                    "retryAt": retry_at,
                },
            )
            return {"attempts": attempts, "retryAt": retry_at}

        def deadline(entries: list[dict[str, Any]]) -> int:
            return max(
                max(
                    entry["receivedAt"]
                    + (TELEGRAM_INCOMING_MEDIA_GROUP_SETTLE_MS if self._polling_group(entry["update"]) else 0),
                    entry["retryAt"] if entry.get("retryAt") is not None else 0,
                )
                for entry in entries
            )

        while self._polling_active:
            try:
                # Retried every iteration, so a failed startup ``getMe``
                # recovers once Telegram is reachable.
                await self._ensure_bot_identity()
                key = f"{self._name}:polling:{self._webhook_scope}"
                if checkpoint is None:
                    stored: dict[str, Any] | None = await state.get(key)
                    checkpoint = stored if stored is not None else {"pending": []}
                offset: int | None = checkpoint.get("offset")
                acknowledged = offset if offset is not None else 0

                groups: dict[str, list[dict[str, Any]]] = {}
                for entry in checkpoint["pending"]:
                    groups.setdefault(self._polling_group_key(entry["update"]), []).append(entry)
                eligible = [
                    entries
                    for entries in groups.values()
                    if all(entry["update"]["update_id"] < acknowledged for entry in entries)
                ]
                ready = [entries for entries in eligible if deadline(entries) <= self._now_ms()]
                if ready and not drained:
                    results = await self._process_polling_updates(
                        [entry["update"] for entries in ready for entry in entries],
                        completed,
                    )
                    failures = {update["update_id"]: error for update, error in results if error is not None}
                    retries: dict[int, dict[str, Any]] = {}
                    for entries in ready:
                        failed = next((entry for entry in entries if entry["update"]["update_id"] in failures), None)
                        if failed is None:
                            continue
                        scheduled = retry(entries, failures[failed["update"]["update_id"]])
                        for entry in entries:
                            retries[entry["update"]["update_id"]] = scheduled
                    pending = [
                        {**entry, **retries.get(entry["update"]["update_id"], {})}
                        for entry in checkpoint["pending"]
                        if entry["update"]["update_id"] not in completed
                    ]
                    next_checkpoint: dict[str, Any] = {**checkpoint, "pending": pending}
                    if pending:
                        await state.set(key, next_checkpoint)
                    else:
                        await state.delete(key)
                    checkpoint = next_checkpoint
                    for update, _error in results:
                        completed.discard(update["update_id"])
                    consecutive_failures = 0
                    # Poll once before the next ready batch, so new updates
                    # are not starved by a run of retries.
                    drained = True
                    continue

                remaining = (
                    max(0, min(deadline(entries) for entries in eligible) - self._now_ms()) if eligible else None
                )
                if not self._polling_active:
                    # Stopped while the loop could not be cancelled; upstream's
                    # already-aborted signal fails this fetch at once.
                    return
                params: dict[str, Any] = {
                    "limit": config.limit,
                    # Ask for an immediate answer when a batch is already due.
                    "timeout": 0 if remaining == 0 else config.timeout,
                }
                if offset is not None:
                    params["offset"] = offset
                if config.allowed_updates is not None:
                    params["allowed_updates"] = config.allowed_updates
                updates: list[TelegramUpdate] = await self._polling_abortable(
                    self._fetch_polling_updates(params, remaining)
                )
                drained = False
                if not self._polling_active:
                    return

                results = await self._process_polling_updates(
                    [update for update in updates if self._polling_group(update) is None],
                    completed,
                )
                pending_by_id: dict[int, dict[str, Any]] = {
                    entry["update"]["update_id"]: entry for entry in checkpoint["pending"]
                }
                for update, error in results:
                    if error is not None:
                        failed_entry: dict[str, Any] = {"update": update, "receivedAt": self._now_ms()}
                        pending_by_id[update["update_id"]] = {**failed_entry, **retry([failed_entry], error)}
                for update in updates:
                    update_id = update["update_id"]
                    if (
                        self._polling_group(update) is not None
                        and update_id not in completed
                        and update_id not in pending_by_id
                    ):
                        pending_by_id[update_id] = {"update": update, "receivedAt": self._now_ms()}
                next_offset = offset
                for update in updates:
                    next_offset = update["update_id"] + 1
                next_checkpoint: dict[str, Any] = {"offset": next_offset, "pending": list(pending_by_id.values())}
                # Saved before the next ``getUpdates`` acknowledges these
                # updates; a failed write keeps the old offset in memory too.
                if next_checkpoint["pending"] or checkpoint["pending"]:
                    await state.set(key, next_checkpoint)
                checkpoint = next_checkpoint
                acknowledged = next_offset if next_offset is not None else 0
                completed.difference_update([update_id for update_id in completed if update_id < acknowledged])
                if not updates and remaining is not None and self._polling_active:
                    await self._polling_abortable(self._sleep(min(remaining, TELEGRAM_INCOMING_MEDIA_GROUP_RETRY_MS)))
                consecutive_failures = 0
            except Exception as error:
                consecutive_failures += 1
                backoff_ms = min(
                    config.retry_delay_ms * 2 ** (consecutive_failures - 1),
                    TELEGRAM_POLLING_MAX_BACKOFF_MS,
                )

                self._logger.warn(
                    "Telegram polling request failed",
                    {
                        "error": str(error),
                        "retryDelayMs": backoff_ms,
                        "consecutiveFailures": consecutive_failures,
                    },
                )

                if not self._polling_active:
                    return

                await self._polling_abortable(self._sleep(backoff_ms))

    # -- Runtime mode resolution ---------------------------------------------

    async def resolve_runtime_mode(self) -> TelegramRuntimeMode:
        """Determine whether to use webhook or polling mode."""
        if self._mode == "webhook":
            return "webhook"

        if self._mode == "polling":
            return "polling"

        webhook_info = await self._fetch_webhook_info()
        if not webhook_info:
            self._logger.warn(
                "Telegram auto mode could not verify webhook status; keeping webhook mode",
            )
            return "webhook"

        url = webhook_info.get("url", "")
        if isinstance(url, str) and url.strip():
            self._logger.debug(
                "Telegram auto mode selected webhook mode",
                {"webhookUrl": url},
            )
            return "webhook"

        if self.is_likely_serverless_runtime():
            self._logger.warn(
                "Telegram auto mode detected serverless runtime without webhook URL; keeping webhook mode",
            )
            return "webhook"

        self._logger.info("Telegram auto mode selected polling mode")
        return "polling"

    def is_likely_serverless_runtime(self) -> bool:
        """Heuristic check for serverless execution environments."""
        return bool(
            os.environ.get("VERCEL")
            or os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
            or (os.environ.get("AWS_EXECUTION_ENV") or "").find("AWS_Lambda") >= 0
            or os.environ.get("FUNCTIONS_WORKER_RUNTIME")
            or os.environ.get("NETLIFY")
            or os.environ.get("K_SERVICE")
        )

    async def _fetch_webhook_info(self) -> TelegramWebhookInfo | None:
        """Fetch the current webhook info from Telegram."""
        try:
            return await self.telegram_fetch("getWebhookInfo")
        except Exception as error:
            self._logger.warn(
                "Failed to fetch Telegram webhook info",
                {"error": str(error)},
            )
            return None

    # -- Update dispatching --------------------------------------------------

    @staticmethod
    def _message_update(update: TelegramUpdate) -> TelegramMessage | None:
        """``message ?? edited_message ?? channel_post ?? edited_channel_post``.

        The first present (non-None) payload, as upstream's nullish chain.
        """
        return next(
            (
                candidate
                for candidate in (
                    update.get("message"),
                    update.get("edited_message"),
                    update.get("channel_post"),
                    update.get("edited_channel_post"),
                )
                if candidate is not None
            ),
            None,
        )

    def process_update(
        self,
        update: TelegramUpdate,
        options: WebhookOptions | None = None,
    ) -> list[asyncio.Task[Any]]:
        """Dispatch a Telegram update to the appropriate handler.

        Returns every dispatched handler task (message, album, slash command,
        action, reactions) so the polling loop can wait for them before
        acknowledging the update (vercel/chat#942). The webhook path ignores
        the list.
        """
        tasks: list[asyncio.Task[Any]] = []
        message_update = self._message_update(update)

        # User allowlist (vercel/chat#742), checked before any routing. With an
        # allowlist set, an update with no acting user (e.g. an anonymous
        # channel post) is dropped too.
        if self._allowed_user_ids is not None:
            user_id = self._update_user_id(update, message_update)
            if user_id is None or _js_number_str(user_id) not in self._allowed_user_ids:
                return tasks

        # Slash commands are gated to fresh ``message`` updates only — edited
        # messages and channel posts never route to the slash-command
        # handlers, and neither do album parts (vercel/chat#760), whose
        # caption is the album's text. ``handle_slash_command_update`` returns
        # a truthy value when it consumed the update, in which case the
        # regular message path is skipped (upstream
        # ``messageUpdate && !handledSlashCommand``).
        message = update.get("message")
        handled_slash_command: asyncio.Task[Any] | bool = (
            message is not None
            and not message.get("media_group_id")
            and self.handle_slash_command_update(message, options)
        )
        if not isinstance(handled_slash_command, bool):
            tasks.append(handled_slash_command)

        if message_update is not None and not handled_slash_command:
            message_task = self.handle_incoming_message_update(message_update, options)
            if message_task is not None:
                tasks.append(message_task)

        if update.get("callback_query"):
            action_task = self.handle_callback_query(update["callback_query"], options)
            if action_task is not None:
                tasks.append(action_task)

        if update.get("message_reaction"):
            tasks.extend(self.handle_message_reaction_update(update["message_reaction"], options))

        return tasks

    @staticmethod
    def _update_user_id(update: TelegramUpdate, message_update: TelegramMessage | None) -> Any:
        """The acting user's id for the allowlist, or ``None`` when absent.

        ``callback_query.from.id ?? message_reaction.user?.id ??
        messageUpdate.from?.id`` — each step nullish (``is not None``).
        """

        def sender(payload: Any) -> Any:
            # Telegram's ``from`` key; typed as ``from_user`` (reserved word).
            return None if payload is None else (payload.get("from_user") or payload.get("from"))

        reaction = update.get("message_reaction")
        for user in (
            sender(update.get("callback_query")),
            reaction.get("user") if reaction is not None else None,
            sender(message_update),
        ):
            if user is not None and user.get("id") is not None:
                return user["id"]
        return None

    def handle_incoming_message_update(
        self,
        telegram_message: TelegramMessage,
        options: WebhookOptions | None = None,
    ) -> asyncio.Task[Any] | None:
        """Handle a new or edited message update.

        Returns the handler task, or for an album part the task that buffers
        it and, once the album has settled, dispatches the combined message.
        """
        if not self._chat:
            return None

        thread_id = self.encode_thread_id(
            TelegramThreadId(
                chat_id=str(telegram_message["chat"]["id"]),
                message_thread_id=telegram_message.get("message_thread_id"),
            )
        )

        if telegram_message.get("media_group_id"):
            return self._start_incoming_media_group(telegram_message, thread_id, options)

        self._start_typing_for_private_message(telegram_message, thread_id, options)

        parsed_message = self.parse_telegram_message(telegram_message, thread_id)
        self.cache_message(parsed_message)

        return self._chat.process_message(self, thread_id, parsed_message, options)

    def _start_incoming_media_group(
        self,
        telegram_message: TelegramMessage,
        thread_id: str,
        options: WebhookOptions | None,
    ) -> asyncio.Task[None] | None:
        """Run :meth:`_process_incoming_media_group` as a task.

        A failure is logged; ``wait_until`` gets a wrapper that settles
        without raising (upstream's ``task.catch(log)``), while the returned
        task still raises so the polling loop can retry the album.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        media_group_id = telegram_message.get("media_group_id")
        task = loop.create_task(self._process_incoming_media_group(telegram_message, thread_id, options))
        self._media_group_tasks.add(task)

        def _done(done: asyncio.Task[None]) -> None:
            self._media_group_tasks.discard(done)
            if done.cancelled():
                return
            error = done.exception()
            if error is not None:
                self._logger.warn(
                    "Failed to process incoming Telegram media group",
                    {"error": str(error), "mediaGroupId": media_group_id, "threadId": thread_id},
                )

        task.add_done_callback(_done)

        if options is not None and options.wait_until is not None:

            async def _settled() -> None:
                # Errors are logged by ``_done``; cancelling this wrapper does
                # not cancel the album task.
                with contextlib.suppress(Exception):
                    await asyncio.shield(task)

            options.wait_until(loop.create_task(_settled()))
        return task

    async def _process_incoming_media_group(
        self,
        telegram_message: TelegramMessage,
        thread_id: str,
        options: WebhookOptions | None = None,
    ) -> None:
        """Buffer one album part and dispatch the album once it has settled.

        Port of upstream ``processIncomingMediaGroup`` (vercel/chat#760).
        Parts are appended under a state lock to
        ``telegram:incoming-media-group:{thread}:{media_group_id}`` (shared
        across instances). Each part's task waits until no part has arrived
        for the settle window; the first to see it settled removes the buffer
        and dispatches one message carrying the newest part's id and raw, the
        first non-empty text, every attachment in ``message_id`` order and
        ``is_mention`` if any part mentions the bot. The others return.
        """
        media_group_id = telegram_message.get("media_group_id")
        if not (self._chat and media_group_id):
            return

        state = self._chat.get_state()
        # Upstream parity: the buffer key is not scoped to the bot identity
        # (upstream ``processIncomingMediaGroup`` uses the same key), so bots
        # sharing one state namespace must not share a group chat.
        media_group_key = f"{self._name}:incoming-media-group:{thread_id}:{media_group_id}"
        lock_key = f"{media_group_key}:lock"
        appended = False

        while True:
            lock = await state.acquire_lock(lock_key, TELEGRAM_INCOMING_MEDIA_GROUP_LOCK_TTL_MS)
            if not lock:
                await self._sleep(TELEGRAM_INCOMING_MEDIA_GROUP_RETRY_MS)
                continue

            entries: list[dict[str, Any]] = []
            remaining_settle_ms = 0
            try:
                stored = await state.get(media_group_key)
                entries = list(stored) if stored is not None else []
                if not appended:
                    entries = [
                        entry for entry in entries if entry["message"]["message_id"] != telegram_message["message_id"]
                    ]
                    entries.append({"message": telegram_message, "receivedAt": self._now_ms()})
                    await state.set(
                        media_group_key,
                        entries[-TELEGRAM_MEDIA_GROUP_MAX:],
                        TELEGRAM_INCOMING_MEDIA_GROUP_BUFFER_TTL_MS,
                    )
                    appended = True
                    remaining_settle_ms = TELEGRAM_INCOMING_MEDIA_GROUP_SETTLE_MS
                elif not entries:
                    return
                else:
                    newest_received_at = max(entry["receivedAt"] for entry in entries)
                    remaining_settle_ms = max(
                        0,
                        TELEGRAM_INCOMING_MEDIA_GROUP_SETTLE_MS - (self._now_ms() - newest_received_at),
                    )
                    if remaining_settle_ms == 0:
                        await state.delete(media_group_key)
            finally:
                await state.release_lock(lock)

            if remaining_settle_ms > 0:
                await self._sleep(remaining_settle_ms)
                continue

            ordered_messages = sorted(
                (cast("TelegramMessage", entry["message"]) for entry in entries),
                key=lambda message: message["message_id"],
            )
            parsed_messages = [self.parse_telegram_message(message, thread_id) for message in ordered_messages]
            if not parsed_messages:
                return
            latest_message = parsed_messages[-1]
            content_message = next((message for message in parsed_messages if message.text), latest_message)
            links = [link for message in parsed_messages for link in (message.links or [])]
            combined_message = Message(
                id=latest_message.id,
                thread_id=thread_id,
                text=content_message.text,
                formatted=content_message.formatted,
                raw=latest_message.raw,
                author=latest_message.author,
                metadata=latest_message.metadata,
                attachments=[attachment for message in parsed_messages for attachment in message.attachments],
                is_mention=any(message.is_mention for message in parsed_messages),
                links=links or None,
            )

            self._start_typing_for_private_message(ordered_messages[0], thread_id)
            self.cache_message(combined_message)
            task = self._chat.process_message(self, thread_id, combined_message, options)
            if task is not None:
                await task
            return

    def handle_slash_command_update(
        self,
        telegram_message: TelegramMessage,
        options: WebhookOptions | None = None,
    ) -> asyncio.Task[Any] | bool:
        """Route a leading ``/command`` message to the slash-command handlers.

        Returns the handler task (``True`` without a running loop) when the
        update was consumed as a slash command, so :meth:`process_update`
        skips the regular message path, and ``False`` otherwise. Like the Discord adapter, the event is built with
        ``channel=None`` and the resolved thread ID is attached as
        ``channel_id`` — ``Chat`` re-wraps it into a real ``Channel`` before
        invoking handlers.
        """
        if not self._chat:
            return False

        slash_command = self.parse_slash_command(telegram_message)
        if not slash_command:
            return False

        thread_id = self.encode_thread_id(
            TelegramThreadId(
                chat_id=str(telegram_message["chat"]["id"]),
                message_thread_id=telegram_message.get("message_thread_id"),
            )
        )

        self._start_typing_for_private_message(telegram_message, thread_id, options)

        parsed_message = self.parse_telegram_message(telegram_message, thread_id)
        self.cache_message(parsed_message)

        event = SlashCommandEvent(
            adapter=self,
            channel=None,  # pyrefly: ignore[bad-argument-type]  # filled in by Chat
            user=parsed_message.author,
            command=slash_command["command"],
            text=slash_command["text"],
            raw=telegram_message,
        )
        event.channel_id = thread_id  # type: ignore[attr-defined]
        task = self._chat.process_slash_command(event, options)

        return task if task is not None else True

    def _start_typing_for_private_message(
        self,
        telegram_message: TelegramMessage,
        thread_id: str,
        options: WebhookOptions | None = None,
    ) -> None:
        """Show ``typing`` on receipt of a private, non-bot message.

        Port of upstream ``startTypingForPrivateMessage`` (vercel/chat#612).
        The chat action is fired as a background task scheduled before the
        handler task, never awaited inline, so it cannot delay dispatch; a
        failure is only logged.
        """
        if telegram_message["chat"].get("type") != "private":
            return
        sender = cast(
            "TelegramUser | None",
            telegram_message.get("from_user") or telegram_message.get("from"),  # type: ignore[call-overload]
        )
        if sender and sender.get("is_bot"):
            return

        async def _typing() -> None:
            try:
                await self.start_typing(thread_id)
            except Exception as error:
                self._logger.warn(
                    "Failed to send Telegram typing action",
                    {"error": str(error), "threadId": thread_id},
                )

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(_typing())
        self._typing_tasks.add(task)
        task.add_done_callback(self._typing_tasks.discard)
        if options is not None and options.wait_until is not None:
            options.wait_until(task)

    def parse_slash_command(
        self,
        telegram_message: TelegramMessage,
    ) -> dict[str, str] | None:
        """Extract a leading ``/command`` (and trailing text) from a message.

        Returns ``None`` unless the message carries a ``bot_command`` entity
        at offset 0 (a command at any other offset routes to
        ``process_message``). ``@bot`` targeting is matched
        case-insensitively against :attr:`user_name`; a command addressed to
        another bot (``/ping@otherbot``) is ignored. Both the text/caption
        selection (``has_text = text is not None``, so an empty-string
        ``text`` still takes the text branch) and the command/trailing-text
        split use UTF-16-LE offsets, matching Telegram's entity indexing.
        """
        has_text = telegram_message.get("text") is not None
        text = telegram_message.get("text") if has_text else telegram_message.get("caption")
        entities = (
            (telegram_message.get("entities") or []) if has_text else (telegram_message.get("caption_entities") or [])
        )

        if not text:
            return None

        command_entity = next(
            (e for e in entities if e.get("type") == "bot_command" and e.get("offset", 0) == 0),
            None,
        )

        if not command_entity:
            return None

        raw_command = self.entity_text(text, command_entity)
        if not raw_command.startswith("/"):
            return None

        command_without_slash = raw_command[1:]
        at_index = command_without_slash.find("@")
        command_name = command_without_slash if at_index == -1 else command_without_slash[:at_index]
        target_bot = None if at_index == -1 else command_without_slash[at_index + 1 :]

        if not command_name:
            return None

        if target_bot and target_bot.lower() != self._user_name.lower():
            return None

        offset = command_entity.get("offset", 0)
        length = command_entity.get("length", 0)
        trailing = self._slice_utf16(text, offset + length).lstrip()

        return {"command": f"/{command_name}", "text": trailing}

    def handle_callback_query(
        self,
        callback_query: TelegramCallbackQuery,
        options: WebhookOptions | None = None,
    ) -> asyncio.Task[Any] | None:
        """Handle a callback query (inline keyboard button press).

        Returns the action handler task; the acknowledgement is not part of it.
        """
        if not (self._chat and callback_query.get("message")):
            return None

        message = callback_query["message"]
        thread_id = self.encode_thread_id(
            TelegramThreadId(
                chat_id=str(message["chat"]["id"]),
                message_thread_id=message.get("message_thread_id"),
            )
        )

        message_id = self.encode_message_id(
            str(message["chat"]["id"]),
            message["message_id"],
        )

        decoded = decode_telegram_callback_data(callback_query.get("data"))
        action_id = decoded["action_id"] or ""
        value = decoded["value"]

        # The TS source uses callback_query.from – in our types this is from_user
        from_user = cast(
            "TelegramUser | None",
            callback_query.get("from_user") or callback_query.get("from"),  # type: ignore[call-overload]
        )
        user = (
            self.to_author(from_user)
            if from_user
            else Author(
                full_name="unknown",
                is_bot="unknown",
                is_me=False,
                user_id="unknown",
                user_name="unknown",
            )
        )

        action_task = self._chat.process_action(
            ActionEvent(
                adapter=self,
                thread=None,  # pyrefly: ignore[bad-argument-type]  # filled in by Chat
                thread_id=thread_id,
                message_id=message_id,
                user=user,
                action_id=action_id,
                value=value,
                raw=callback_query,
            ),
            options,
        )

        # Fire-and-forget: acknowledge the callback query
        async def _ack() -> None:
            try:
                await self.telegram_fetch(
                    "answerCallbackQuery",
                    {"callback_query_id": callback_query["id"]},
                )
            except Exception as error:
                self._logger.warn(
                    "Failed to acknowledge Telegram callback query",
                    {"callbackQueryId": callback_query["id"], "error": str(error)},
                )

        wait_until = getattr(options, "wait_until", None) if options else None
        try:
            task = asyncio.get_running_loop().create_task(_ack())
        except RuntimeError:
            task = None
        if task and callable(wait_until):
            wait_until(task)
        return action_task

    def handle_message_reaction_update(
        self,
        reaction_update: TelegramMessageReactionUpdated,
        options: WebhookOptions | None = None,
    ) -> list[asyncio.Task[Any]]:
        """Handle a message reaction update; returns the reaction handler tasks."""
        tasks: list[asyncio.Task[Any]] = []
        if not self._chat:
            return tasks

        thread_id = self.encode_thread_id(
            TelegramThreadId(
                chat_id=str(reaction_update["chat"]["id"]),
                message_thread_id=reaction_update.get("message_thread_id"),
            )
        )

        message_id = self.encode_message_id(
            str(reaction_update["chat"]["id"]),
            reaction_update["message_id"],
        )

        old_reactions = {self.reaction_key(r) for r in reaction_update.get("old_reaction", [])}
        new_reactions = {self.reaction_key(r) for r in reaction_update.get("new_reaction", [])}

        user_field = reaction_update.get("user")
        actor = self.to_author(user_field) if user_field else self.to_reaction_actor_author(reaction_update["chat"])

        for reaction in reaction_update.get("new_reaction", []):
            key = self.reaction_key(reaction)
            if key not in old_reactions:
                added_task = self._chat.process_reaction(
                    ReactionEvent(
                        adapter=self,
                        thread=None,  # pyrefly: ignore[bad-argument-type]  # filled in by Chat
                        thread_id=thread_id,
                        message_id=message_id,
                        user=actor,
                        emoji=self.reaction_to_emoji_value(reaction),
                        raw_emoji=key,
                        added=True,
                        raw=reaction_update,
                    ),
                    options,
                )
                if added_task is not None:
                    tasks.append(added_task)

        for reaction in reaction_update.get("old_reaction", []):
            key = self.reaction_key(reaction)
            if key not in new_reactions:
                removed_task = self._chat.process_reaction(
                    ReactionEvent(
                        adapter=self,
                        thread=None,  # pyrefly: ignore[bad-argument-type]  # filled in by Chat
                        thread_id=thread_id,
                        message_id=message_id,
                        user=actor,
                        emoji=self.reaction_to_emoji_value(reaction),
                        raw_emoji=key,
                        added=False,
                        raw=reaction_update,
                    ),
                    options,
                )
                if removed_task is not None:
                    tasks.append(removed_task)
        return tasks

    # -- Posting / editing / deleting ----------------------------------------

    async def post_message(
        self,
        thread_id: str,
        message: AdapterPostableMessage,
    ) -> RawMessage:
        """Post a message to a Telegram thread."""
        parsed_thread = self._resolve_thread_id(thread_id)

        card = extract_card(message)
        reply_markup = card_to_telegram_inline_keyboard(card) if card else None
        parse_mode = self.resolve_parse_mode(message, card)
        # Plain-text rendering of the same message, used as the retry body
        # when Telegram rejects the MarkdownV2 entities (vercel/chat#340).
        plain_text = self.truncate_message(
            convert_emoji_placeholders(self.render_plain_text_message(message, card), "gchat"),
            None,
        )
        text = self.truncate_message(
            convert_emoji_placeholders(
                # Route the card's standard-markdown fallback through the
                # MarkdownV2 renderer so titles render as real bold instead
                # of literal ``**title**``.
                self._format_converter.from_markdown(card_to_fallback_text(card))
                if card
                else self._format_converter.render_postable(message),
                "gchat",
            ),
            parse_mode,
        )

        files = extract_files(message)
        if len(files) > 1:
            raise ValidationError(
                "telegram",
                "Telegram adapter supports a single file upload per message",
            )

        attachments = extract_postable_attachments(message)
        if len(attachments) > 1:
            raise ValidationError(
                "telegram",
                "Telegram adapter supports a single attachment upload per message",
            )

        if files and attachments:
            raise ValidationError(
                "telegram",
                "Telegram adapter does not support mixing file uploads and attachments in one message",
            )

        rich = self.resolve_rich_message(message, card, len(files), len(attachments))
        raw_message: TelegramMessage

        if len(files) == 1:
            file = files[0]
            if not file:
                raise ValidationError("telegram", "File upload payload is empty")
            raw_message = await self.send_document(parsed_thread, file, text, plain_text, reply_markup, parse_mode)
        elif len(attachments) == 1:
            attachment = attachments[0]
            if not attachment:
                raise ValidationError("telegram", "Attachment upload payload is empty")
            raw_message = await self.send_attachment(
                parsed_thread,
                attachment,
                text,
                plain_text,
                reply_markup,
                parse_mode,
            )
        else:
            if not text.strip():
                raise ValidationError("telegram", "Message text cannot be empty")

            async def _send_regular() -> TelegramMessage:
                return await self.send_regular_message(
                    parsed_thread,
                    text,
                    plain_text,
                    parse_mode,
                    reply_markup,
                    thread_id,
                )

            if rich is not None:
                rich_payload = rich

                async def _send_rich() -> TelegramMessage:
                    return await self.telegram_fetch(
                        "sendRichMessage",
                        {
                            "chat_id": parsed_thread.chat_id,
                            "message_thread_id": parsed_thread.message_thread_id,
                            "rich_message": {"markdown": rich_payload.markdown},
                            "reply_markup": reply_markup,
                        },
                    )

                raw_message = await self.with_telegram_rich_fallback(
                    _send_rich,
                    _send_regular,
                    method="sendRichMessage",
                    thread_id=thread_id,
                )
            else:
                raw_message = await _send_regular()

        resulting_thread_id = self.encode_thread_id(
            TelegramThreadId(
                chat_id=str(raw_message["chat"]["id"]),
                message_thread_id=(
                    raw_message.get("message_thread_id")
                    if raw_message.get("message_thread_id") is not None
                    else parsed_thread.message_thread_id
                ),
            )
        )

        # Reuse the locally pre-rendered AST/text when the rich path produced
        # the message, so Telegram's echo isn't re-parsed (upstream passes
        # ``{ formatted, text }`` into ``parseTelegramMessage``).
        parsed_content = TelegramParsedContent(formatted=rich.formatted, text=rich.text) if rich is not None else None
        parsed_message = self.parse_telegram_message(raw_message, resulting_thread_id, parsed_content)
        self.cache_message(parsed_message)

        return RawMessage(
            id=parsed_message.id,
            thread_id=parsed_message.thread_id,
            raw=raw_message,
        )

    async def post_channel_message(
        self,
        channel_id: str,
        message: AdapterPostableMessage,
    ) -> RawMessage:
        """Post a message to a Telegram channel."""
        return await self.post_message(channel_id, message)

    async def edit_message(
        self,
        thread_id: str,
        message_id: str,
        message: AdapterPostableMessage,
    ) -> RawMessage:
        """Edit an existing Telegram message."""
        parsed_thread = self._resolve_thread_id(thread_id)
        decoded = self.decode_composite_message_id(message_id, parsed_thread.chat_id)
        chat_id = decoded["chat_id"]
        telegram_message_id = decoded["message_id"]
        composite_id = decoded["composite_id"]

        card = extract_card(message)
        reply_markup = card_to_telegram_inline_keyboard(card) if card else None
        parse_mode = self.resolve_parse_mode(message, card)
        # Plain-text rendering of the same message, used as the retry body
        # when Telegram rejects the MarkdownV2 entities (vercel/chat#340).
        plain_text = self.truncate_message(
            convert_emoji_placeholders(self.render_plain_text_message(message, card), "gchat"),
            None,
        )
        text = self.truncate_message(
            convert_emoji_placeholders(
                self._format_converter.from_markdown(card_to_fallback_text(card))
                if card
                else self._format_converter.render_postable(message),
                "gchat",
            ),
            parse_mode,
        )
        rich = self.resolve_rich_message(message, card, 0, 0)

        if not text.strip():
            raise ValidationError("telegram", "Message text cannot be empty")

        # Returns ``Any`` because Telegram answers ``true`` (not a Message)
        # when editing inline messages — the ``result is True`` narrowing
        # below handles that shape, matching the pre-#340 untyped fetch.
        async def _edit_message_text(resolved_parse_mode: str | None, resolved_text: str) -> Any:
            return await self.telegram_fetch(
                "editMessageText",
                {
                    "chat_id": chat_id,
                    "message_id": telegram_message_id,
                    "text": resolved_text,
                    "reply_markup": reply_markup or empty_telegram_inline_keyboard(),
                    "parse_mode": resolved_parse_mode,
                },
            )

        async def _edit_regular() -> Any:
            return await self.with_telegram_markdown_fallback(
                parse_mode,
                _edit_message_text,
                initial_text=text,
                fallback_text=plain_text,
                message_id=message_id,
                method="editMessageText",
                thread_id=thread_id,
            )

        if rich is not None:
            rich_payload = rich

            async def _edit_rich() -> Any:
                return await self.telegram_fetch(
                    "editMessageText",
                    {
                        "chat_id": chat_id,
                        "message_id": telegram_message_id,
                        "rich_message": {"markdown": rich_payload.markdown},
                        "reply_markup": reply_markup or empty_telegram_inline_keyboard(),
                    },
                )

            result = await self.with_telegram_rich_fallback(
                _edit_rich,
                _edit_regular,
                message_id=message_id,
                method="editMessageText",
                thread_id=thread_id,
            )
        else:
            result = await _edit_regular()

        # Telegram returns ``true`` when editing inline messages
        if result is True:
            existing = self.find_cached_message(composite_id)
            if not existing:
                raise ChatNotImplementedError(
                    "Telegram returned a non-message edit result and no cached message was found",
                    "editMessage",
                )

            # ``rich?.text ?? text`` / ``rich?.formatted ?? toAst(text)``: when
            # the rich path supplied a pre-rendered AST/text, reuse it.
            updated = Message(
                id=existing.id,
                thread_id=existing.thread_id,
                text=rich.text if rich is not None else text,
                formatted=rich.formatted if rich is not None else self._format_converter.to_ast(text),
                raw=existing.raw,
                author=existing.author,
                metadata=MessageMetadata(
                    date_sent=existing.metadata.date_sent,
                    edited=True,
                    edited_at=datetime.now(timezone.utc),
                ),
                attachments=existing.attachments,
                is_mention=existing.is_mention,
            )

            self.cache_message(updated)

            return RawMessage(
                id=updated.id,
                thread_id=updated.thread_id,
                raw=updated.raw,
            )

        resulting_thread_id = self.encode_thread_id(
            TelegramThreadId(
                chat_id=str(result["chat"]["id"]),
                message_thread_id=(
                    result.get("message_thread_id")
                    if result.get("message_thread_id") is not None
                    else parsed_thread.message_thread_id
                ),
            )
        )

        parsed_content = TelegramParsedContent(formatted=rich.formatted, text=rich.text) if rich is not None else None
        parsed_message = self.parse_telegram_message(result, resulting_thread_id, parsed_content)
        self.cache_message(parsed_message)

        return RawMessage(
            id=parsed_message.id,
            thread_id=parsed_message.thread_id,
            raw=result,
        )

    async def delete_message(self, thread_id: str, message_id: str) -> None:
        """Delete a Telegram message."""
        parsed_thread = self._resolve_thread_id(thread_id)
        decoded = self.decode_composite_message_id(message_id, parsed_thread.chat_id)

        await self.telegram_fetch(
            "deleteMessage",
            {
                "chat_id": decoded["chat_id"],
                "message_id": decoded["message_id"],
            },
        )

        self.delete_cached_message(decoded["composite_id"])

    # -- Reactions -----------------------------------------------------------

    async def add_reaction(
        self,
        thread_id: str,
        message_id: str,
        emoji: EmojiValue | str,
    ) -> None:
        """Add a reaction to a Telegram message."""
        parsed_thread = self._resolve_thread_id(thread_id)
        decoded = self.decode_composite_message_id(message_id, parsed_thread.chat_id)

        await self.telegram_fetch(
            "setMessageReaction",
            {
                "chat_id": decoded["chat_id"],
                "message_id": decoded["message_id"],
                "reaction": [self.to_telegram_reaction(emoji)],
            },
        )

    async def remove_reaction(
        self,
        thread_id: str,
        message_id: str,
        emoji: EmojiValue | str,
    ) -> None:
        """Remove a reaction from a Telegram message."""
        parsed_thread = self._resolve_thread_id(thread_id)
        decoded = self.decode_composite_message_id(message_id, parsed_thread.chat_id)

        await self.telegram_fetch(
            "setMessageReaction",
            {
                "chat_id": decoded["chat_id"],
                "message_id": decoded["message_id"],
                "reaction": [],
            },
        )

    # -- Typing --------------------------------------------------------------

    async def start_typing(self, thread_id: str, status: str | None = None) -> None:
        """Send a ``typing`` chat action."""
        parsed_thread = self._resolve_thread_id(thread_id)
        await self.telegram_fetch(
            "sendChatAction",
            {
                "chat_id": parsed_thread.chat_id,
                "message_thread_id": parsed_thread.message_thread_id,
                "action": "typing",
            },
        )

    # -- Streaming -------------------------------------------------------------

    async def stream(
        self,
        thread_id: str,
        text_stream: AsyncIterable[StreamInput],
        options: StreamOptions | None = None,
    ) -> RawMessage | None:
        """Stream a message to a Telegram private chat via draft updates.

        Port of upstream ``TelegramAdapter.stream`` (vercel/chat#479, on top
        of the #340 draft-streaming foundation). Private chats (DMs) get
        native draft streaming: the draft bubble updates in place as chunks
        arrive, throttled to ``options.update_interval_ms`` (default
        :data:`TELEGRAM_DEFAULT_STREAM_UPDATE_INTERVAL_MS`), and a final send
        persists the message when the stream ends. Returns ``None`` for
        non-DM threads — before consuming any chunks — so the SDK's built-in
        post+edit fallback handles groups, supergroups, and channels.

        The outbound ladder is **rich → MarkdownV2 → plain**:

        * Drafts first go through ``sendRichMessageDraft`` (native rich
          markdown), and the final message through ``sendRichMessage``.
        * If the rich endpoint reports it can't parse / is missing /
          unsupported, the stream demotes to the MarkdownV2
          ``sendMessageDraft`` + ``sendMessage`` path (the #340 behaviour,
          now the second tier).
        * If Telegram then rejects the MarkdownV2 entities, the stream
          downgrades again to plain-text drafts and a plain final send.

        Any other (non-fallback) draft failure disables draft updates
        entirely but never fails the stream — the final message is always
        attempted. There is no longer an empty opening draft; the first
        bubble update carries real content.
        """
        if not self.is_dm(thread_id):
            return None

        parsed_thread = self._resolve_thread_id(thread_id)
        update_interval_ms = self.clamp_integer(
            options.update_interval_ms if options is not None else None,
            TELEGRAM_DEFAULT_STREAM_UPDATE_INTERVAL_MS,
            0,
            2**53 - 1,
        )

        renderer = StreamingMarkdownRenderer()
        draft_id = self.create_draft_id()
        accumulated = ""
        last_draft_text: str | None = None
        last_flush_at = 0.0
        draft_streaming_enabled = True
        stream_uses_markdown = True
        stream_uses_rich = self._rich_messages_available

        def _now_ms() -> float:
            return time.monotonic() * 1000.0

        def render_markdown_text(text: str) -> str:
            return self.truncate_message(
                convert_emoji_placeholders(self._format_converter.from_markdown(text), "gchat"),
                TELEGRAM_MARKDOWN_PARSE_MODE,
            )

        def render_plain_text(text: str) -> str:
            return self.truncate_message(
                self.resolve_telegram_fallback_text(text, _markdown_to_plain_text(text)),
                None,
            )

        def _draft_payload(text: str, *, markdown: bool) -> dict[str, Any]:
            payload: dict[str, Any] = {
                "chat_id": parsed_thread.chat_id,
                "draft_id": draft_id,
                "text": text,
            }
            # Omit absent optional keys: DMs have no forum-topic thread id,
            # and plain-text drafts ship without a parse_mode (mirrors
            # upstream, where JSON.stringify drops the undefined fields).
            if parsed_thread.message_thread_id is not None:
                payload["message_thread_id"] = parsed_thread.message_thread_id
            if markdown:
                payload["parse_mode"] = TELEGRAM_MARKDOWN_PARSE_MODE
            return payload

        async def send_draft(text: str, use_markdown: bool) -> None:
            nonlocal draft_streaming_enabled, last_draft_text, last_flush_at
            nonlocal stream_uses_markdown, stream_uses_rich
            if not draft_streaming_enabled or text == last_draft_text:
                return

            draft_text = text
            if stream_uses_rich:
                try:
                    await self.telegram_fetch(
                        "sendRichMessageDraft",
                        {
                            "chat_id": parsed_thread.chat_id,
                            "message_thread_id": parsed_thread.message_thread_id,
                            "draft_id": draft_id,
                            "rich_message": {"markdown": text},
                        },
                    )
                    last_draft_text = text
                    last_flush_at = _now_ms()
                    return
                except Exception as error:
                    if not self.can_fallback_from_rich_message(error, "sendRichMessageDraft"):
                        # Not a rich-availability problem (e.g. a network
                        # blip): stop draft updates but keep the stream alive
                        # for the final send. Rich stays enabled.
                        draft_streaming_enabled = False
                        self._logger.warn(
                            "Telegram rich draft streaming update failed",
                            {"error": str(error), "thread_id": thread_id},
                        )
                        return

                    # Rich is unavailable / can't-parse: remember (may latch
                    # rich off permanently), demote this stream to the
                    # MarkdownV2 draft path, and re-render the body.
                    self.remember_rich_message_failure(error, "sendRichMessageDraft")
                    self._logger.warn(
                        "Telegram rich draft failed; retrying with a regular draft",
                        {"error": str(error), "thread_id": thread_id},
                    )
                    stream_uses_rich = False
                    draft_text = (
                        render_markdown_text(renderer.render()) if use_markdown else render_plain_text(accumulated)
                    )

            try:
                await self.telegram_fetch("sendMessageDraft", _draft_payload(draft_text, markdown=use_markdown))
                last_draft_text = draft_text
                last_flush_at = _now_ms()
            except Exception as error:
                if use_markdown and self.is_telegram_markdown_parse_error(error):
                    # Telegram rejected the MarkdownV2 entities: downgrade
                    # this stream to plain-text drafts and retry once with
                    # the plain rendering of everything accumulated so far.
                    stream_uses_markdown = False
                    plain_draft_text = render_plain_text(accumulated)
                    try:
                        await self.telegram_fetch("sendMessageDraft", _draft_payload(plain_draft_text, markdown=False))
                        last_draft_text = plain_draft_text
                        last_flush_at = _now_ms()
                    except Exception as retry_error:
                        draft_streaming_enabled = False
                        self._logger.warn(
                            "Telegram draft streaming update failed",
                            {"error": str(retry_error), "thread_id": thread_id},
                        )
                    return

                draft_streaming_enabled = False
                self._logger.warn(
                    "Telegram draft streaming update failed",
                    {"error": str(error), "thread_id": thread_id},
                )

        async def flush_draft() -> None:
            if not draft_streaming_enabled:
                return
            if stream_uses_rich:
                draft_text = truncate_rich_markdown(renderer.render())
            elif stream_uses_markdown:
                draft_text = render_markdown_text(renderer.render())
            else:
                draft_text = render_plain_text(accumulated)
            await send_draft(draft_text, stream_uses_markdown)

        async for chunk in text_stream:
            text: str | None = None
            if isinstance(chunk, str):
                text = chunk
            elif isinstance(chunk, dict) and chunk.get("type") == "markdown_text":
                text = chunk.get("text", "")
            elif hasattr(chunk, "type") and getattr(chunk, "type", None) == "markdown_text":
                # Runtime-narrowed to a MarkdownTextChunk via the `type`
                # tag; only that variant has `.text`. Pyrefly doesn't do
                # tag-based union narrowing, so read via `getattr`.
                text = getattr(chunk, "text", "")

            if text is None:
                # Task/plan progress chunks have no draft representation.
                continue

            accumulated += text
            renderer.push(text)

            if _now_ms() - last_flush_at >= update_interval_ms:
                await flush_draft()

        if not accumulated.strip():
            raise ValidationError("telegram", "Telegram streaming requires text content")

        # Finalise the renderer FIRST, then flush — a trailing table-like
        # block only resolves to its final markdown once the renderer is
        # finished, so the last draft must reflect the post-finish render.
        final_markdown = renderer.finish()
        await flush_draft()

        if stream_uses_rich:
            markdown = truncate_rich_markdown(final_markdown)
            try:
                raw = await self.telegram_fetch(
                    "sendRichMessage",
                    {
                        "chat_id": parsed_thread.chat_id,
                        "message_thread_id": parsed_thread.message_thread_id,
                        "rich_message": {"markdown": markdown},
                    },
                )
                resulting_thread_id = self.encode_thread_id(
                    TelegramThreadId(
                        chat_id=str(raw["chat"]["id"]),
                        message_thread_id=(
                            raw.get("message_thread_id")
                            if raw.get("message_thread_id") is not None
                            else parsed_thread.message_thread_id
                        ),
                    )
                )
                formatted = self._format_converter.to_ast(accumulated)
                message = self.parse_telegram_message(
                    raw,
                    resulting_thread_id,
                    TelegramParsedContent(formatted=formatted, text=ast_to_plain_text(formatted)),
                )
                self.cache_message(message)
                return RawMessage(id=message.id, thread_id=message.thread_id, raw=raw)
            except Exception as error:
                if not self.can_fallback_from_rich_message(error, "sendRichMessage"):
                    raise
                self.remember_rich_message_failure(error, "sendRichMessage")

        regular_text = render_markdown_text(final_markdown) if stream_uses_markdown else render_plain_text(accumulated)
        plain_text = render_plain_text(accumulated)
        raw = await self.send_regular_message(
            parsed_thread,
            regular_text,
            plain_text,
            TELEGRAM_MARKDOWN_PARSE_MODE if stream_uses_markdown else None,
            None,
            thread_id,
        )
        resulting_thread_id = self.encode_thread_id(
            TelegramThreadId(
                chat_id=str(raw["chat"]["id"]),
                message_thread_id=(
                    raw.get("message_thread_id")
                    if raw.get("message_thread_id") is not None
                    else parsed_thread.message_thread_id
                ),
            )
        )
        message = self.parse_telegram_message(raw, resulting_thread_id)
        self.cache_message(message)
        return RawMessage(id=message.id, thread_id=message.thread_id, raw=raw)

    # -- Fetching messages ---------------------------------------------------

    async def fetch_messages(
        self,
        thread_id: str,
        options: FetchOptions | None = None,
    ) -> FetchResult:
        """Fetch cached messages for a thread."""
        if options is None:
            options = FetchOptions()

        messages = sorted(
            list(self._message_cache.get(thread_id, [])),
            key=lambda m: (m.metadata.date_sent, self.message_sequence(m.id)),
        )

        return self.paginate_messages(messages, options)

    async def fetch_channel_messages(
        self,
        channel_id: str,
        options: FetchOptions | None = None,
    ) -> FetchResult:
        """Fetch cached messages across all threads in a channel."""
        if options is None:
            options = FetchOptions()

        by_id: dict[str, Message] = {}

        for tid, messages in self._message_cache.items():
            try:
                decoded = self.decode_thread_id(tid)
            except Exception:
                continue

            if decoded.chat_id != channel_id:
                continue

            for msg in messages:
                by_id[msg.id] = msg

        all_messages = sorted(
            list(by_id.values()),
            key=lambda m: (m.metadata.date_sent, self.message_sequence(m.id)),
        )

        return self.paginate_messages(all_messages, options)

    async def fetch_message(
        self,
        _thread_id: str,
        message_id: str,
    ) -> Message | None:
        """Fetch a single cached message by ID."""
        return self.find_cached_message(message_id)

    async def fetch_thread(self, thread_id: str) -> ThreadInfo:
        """Fetch thread information from Telegram."""
        parsed_thread = self._resolve_thread_id(thread_id)
        chat: TelegramChat = await self.telegram_fetch(
            "getChat",
            {"chat_id": parsed_thread.chat_id},
        )

        return ThreadInfo(
            id=self.encode_thread_id(parsed_thread),
            channel_id=str(chat["id"]),
            channel_name=self.chat_display_name(chat) or str(chat["id"]),
            is_dm=chat.get("type") == "private",
            metadata={
                "chat": chat,
                "messageThreadId": parsed_thread.message_thread_id,
            },
        )

    async def fetch_channel_info(self, channel_id: str) -> ChannelInfo:
        """Fetch channel information from Telegram."""
        chat: TelegramChat = await self.telegram_fetch(
            "getChat",
            {"chat_id": channel_id},
        )

        member_count: int | None = None
        try:
            member_count = await self.telegram_fetch(
                "getChatMemberCount",
                {"chat_id": channel_id},
            )
        except Exception:
            member_count = None

        return ChannelInfo(
            id=str(chat["id"]),
            name=self.chat_display_name(chat) or str(chat["id"]),
            is_dm=chat.get("type") == "private",
            member_count=member_count,
            metadata={"chat": chat},
        )

    # -- Thread / channel ID helpers -----------------------------------------

    def encode_thread_id(self, platform_data: TelegramThreadId) -> str:
        """Encode a :class:`TelegramThreadId` into a string thread ID."""
        if isinstance(platform_data.message_thread_id, int):
            return f"telegram:{platform_data.chat_id}:{platform_data.message_thread_id}"
        return f"telegram:{platform_data.chat_id}"

    def decode_thread_id(self, thread_id: str) -> TelegramThreadId:
        """Decode a string thread ID into a :class:`TelegramThreadId`."""
        parts = thread_id.split(":")
        if parts[0] != "telegram" or len(parts) < 2 or len(parts) > 3:
            raise ValidationError(
                "telegram",
                f"Invalid Telegram thread ID: {thread_id}",
            )

        chat_id = parts[1] if len(parts) > 1 else ""
        if not chat_id:
            raise ValidationError(
                "telegram",
                f"Invalid Telegram thread ID: {thread_id}",
            )

        if len(parts) < 3 or not parts[2]:
            return TelegramThreadId(chat_id=chat_id)

        try:
            message_thread_id = int(parts[2])
        except (ValueError, TypeError) as exc:
            raise ValidationError(
                "telegram",
                f"Invalid Telegram thread topic ID in thread ID: {thread_id}",
            ) from exc

        if not math.isfinite(message_thread_id):
            raise ValidationError(
                "telegram",
                f"Invalid Telegram thread topic ID in thread ID: {thread_id}",
            )

        return TelegramThreadId(chat_id=chat_id, message_thread_id=message_thread_id)

    def channel_id_from_thread_id(self, thread_id: str) -> str:
        """Extract the channel ID from a thread ID."""
        resolved = self._resolve_thread_id(thread_id)
        return f"telegram:{resolved.chat_id}"

    async def open_dm(self, user_id: str) -> str:
        """Open a DM with a user by their Telegram user ID."""
        return self.encode_thread_id(TelegramThreadId(chat_id=user_id))

    def is_dm(self, thread_id: str) -> bool:
        """Check if a thread ID is a DM (positive chat ID)."""
        resolved = self._resolve_thread_id(thread_id)
        return not resolved.chat_id.startswith("-")

    # -- Message parsing -----------------------------------------------------

    def parse_message(self, raw: TelegramRawMessage) -> Message:
        """Parse a raw Telegram message into a :class:`Message`."""
        thread_id = self.encode_thread_id(
            TelegramThreadId(
                chat_id=str(raw["chat"]["id"]),
                message_thread_id=raw.get("message_thread_id"),
            )
        )
        message = self.parse_telegram_message(raw, thread_id)
        self.cache_message(message)
        return message

    def render_formatted(self, content: FormattedContent) -> str:
        """Render formatted content to plain markdown text."""
        return self._format_converter.from_ast(content)

    def parse_telegram_message(
        self,
        raw: TelegramMessage,
        thread_id: str,
        content: TelegramParsedContent | None = None,
    ) -> Message:
        """Parse a Telegram message into a normalised :class:`Message`."""
        rich_message = raw.get("rich_message")
        # `raw.rich_message ? ... : ""` -- presence (truthy) check on the field.
        rich_markdown = rich_message_to_markdown(rich_message) if rich_message else ""
        # Upstream chains `??` here (nullish): an empty-string text/caption is a
        # real value and short-circuits the chain. Port each `??` as `is not None`.
        content_text = content.text if content is not None else None
        raw_text = raw.get("text")
        raw_caption = raw.get("caption")
        sticker = raw.get("sticker")
        # A sticker carries no text, only the emoji it stands for; fall back to
        # the set name, then a plain label, so it never arrives empty
        # (vercel/chat#835). ``emoji ?? set_name ?? "sticker"`` is nullish.
        sticker_text: str | None = None
        if sticker is not None:
            sticker_emoji = sticker.get("emoji")
            sticker_set_name = sticker.get("set_name")
            if sticker_emoji is not None:
                sticker_text = sticker_emoji
            elif sticker_set_name is not None:
                sticker_text = sticker_set_name
            else:
                sticker_text = "sticker"
        if content_text is not None:
            plain_text = content_text
        elif raw_text is not None:
            plain_text = raw_text
        elif raw_caption is not None:
            plain_text = raw_caption
        elif sticker_text is not None:
            plain_text = sticker_text
        elif (non_file_text := _describe_non_file_content(raw)) is not None:
            plain_text = non_file_text
        elif rich_message:
            plain_text = rich_message_to_text(rich_message)
        else:
            plain_text = ""
        # `raw.entities ?? raw.caption_entities ?? []` -- nullish: present-but-empty
        # entity lists are honoured rather than falling through to caption_entities.
        entities = raw.get("entities")
        if entities is None:
            entities = raw.get("caption_entities")
        if entities is None:
            entities = []
        # `content?.text ? content.text : applyTelegramEntities(...)` -- TRUTHY `?`:
        # a present-but-empty `content.text` falls through to the entity-applied text.
        text = content.text if content is not None and content.text else apply_telegram_entities(plain_text, entities)

        # Determine author -- Telegram uses 'from' key which is a reserved word
        from_user = cast(
            "TelegramUser | None",
            raw.get("from_user") or raw.get("from"),  # type: ignore[call-overload]
        )
        sender_chat = raw.get("sender_chat")

        if from_user:
            author = self.to_author(from_user)
        elif sender_chat:
            author = self.to_reaction_actor_author(sender_chat)
        else:
            fallback_name = self.chat_display_name(raw["chat"]) or str(raw["chat"]["id"])
            author = Author(
                user_id=str(raw["chat"]["id"]),
                user_name=fallback_name,
                full_name=fallback_name,
                is_bot="unknown",
                is_me=False,
            )

        edit_date = raw.get("edit_date")

        # `content?.formatted ?? this.formatConverter.toAst(richMarkdown || text)`:
        # `??` (nullish) for the supplied AST, then `richMarkdown || text` is a
        # TRUTHY-OR -- a non-empty rendered rich markdown wins, else fall to text.
        if content is not None and content.formatted is not None:
            formatted = content.formatted
        else:
            formatted = self._format_converter.to_ast(rich_markdown or text)

        return Message(
            id=self.encode_message_id(str(raw["chat"]["id"]), raw["message_id"]),
            thread_id=thread_id,
            text=text,
            formatted=formatted,
            raw=raw,
            author=author,
            metadata=MessageMetadata(
                date_sent=datetime.fromtimestamp(raw["date"], tz=timezone.utc),
                edited=edit_date is not None,
                edited_at=(datetime.fromtimestamp(edit_date, tz=timezone.utc) if edit_date is not None else None),
            ),
            attachments=self.extract_attachments(raw),
            is_mention=self.is_bot_mentioned(raw, plain_text),
        )

    # -- Attachments ---------------------------------------------------------

    def extract_attachments(self, raw: TelegramMessage) -> list[Attachment]:
        """Extract file attachments from a Telegram message."""
        attachments: list[Attachment] = []

        photos = raw.get("photo")
        if photos:
            photo = photos[-1]  # Largest resolution
            attachments.append(
                self.create_attachment(
                    "image",
                    photo["file_id"],
                    size=photo.get("file_size"),
                    width=photo.get("width"),
                    height=photo.get("height"),
                    # Telegram re-encodes every photo size as JPEG (vercel/chat#752).
                    mime_type="image/jpeg",
                    file_unique_id=photo.get("file_unique_id"),
                )
            )

        video = raw.get("video")
        if video:
            attachments.append(
                self.create_attachment(
                    "video",
                    video["file_id"],
                    size=video.get("file_size"),
                    width=video.get("width"),
                    height=video.get("height"),
                    name=video.get("file_name"),
                    mime_type=video.get("mime_type"),
                    file_unique_id=video.get("file_unique_id"),
                )
            )

        audio = raw.get("audio")
        if audio:
            attachments.append(
                self.create_attachment(
                    "audio",
                    audio["file_id"],
                    size=audio.get("file_size"),
                    name=audio.get("file_name"),
                    mime_type=audio.get("mime_type"),
                    file_unique_id=audio.get("file_unique_id"),
                )
            )

        voice = raw.get("voice")
        if voice:
            attachments.append(
                self.create_attachment(
                    "audio",
                    voice["file_id"],
                    size=voice.get("file_size"),
                    mime_type=voice.get("mime_type"),
                    file_unique_id=voice.get("file_unique_id"),
                )
            )

        # When a message carries an animation, Telegram also sets `document`
        # for backward compatibility; it is the same file, so it is reported
        # once, as the animation below (vercel/chat#835).
        document = raw.get("document")
        animation = raw.get("animation")
        if document and not animation:
            attachments.append(
                self.create_attachment(
                    "file",
                    document["file_id"],
                    size=document.get("file_size"),
                    name=document.get("file_name"),
                    mime_type=document.get("mime_type"),
                    file_unique_id=document.get("file_unique_id"),
                )
            )

        # Round video messages (video_note) are a distinct Telegram field
        # from `video`. Port of vercel/chat#457: extract them as a "video"
        # attachment with width/height set to the clip's `length`.
        video_note = raw.get("video_note")
        if video_note:
            length = video_note.get("length")
            attachments.append(
                self.create_attachment(
                    "video",
                    video_note["file_id"],
                    size=video_note.get("file_size"),
                    width=length,
                    height=length,
                    file_unique_id=video_note.get("file_unique_id"),
                )
            )

        # An animation is Telegram's GIF: an MP4 without sound. Upstream parity
        # (chat@4.41.1 adapter-telegram index.ts:2798-2809): always a "video"
        # attachment, even for an ``image/gif`` mime type, so re-posting it
        # goes through ``sendVideo`` exactly as upstream's ATTACHMENT_UPLOADS
        # table does; no GIF-specific outbound route is added here.
        if animation:
            attachments.append(
                self.create_attachment(
                    "video",
                    animation["file_id"],
                    size=animation.get("file_size"),
                    width=animation.get("width"),
                    height=animation.get("height"),
                    name=animation.get("file_name"),
                    mime_type=animation.get("mime_type"),
                    file_unique_id=animation.get("file_unique_id"),
                )
            )

        # A still sticker is a WebP image; a video sticker is WebM and a
        # Lottie one the TGS container, neither of which renders as an image.
        sticker = raw.get("sticker")
        if sticker:
            sticker_type, sticker_mime_type = _sticker_attachment_format(sticker)
            attachments.append(
                self.create_attachment(
                    sticker_type,
                    sticker["file_id"],
                    size=sticker.get("file_size"),
                    width=sticker.get("width"),
                    height=sticker.get("height"),
                    mime_type=sticker_mime_type,
                    file_unique_id=sticker.get("file_unique_id"),
                )
            )

        # Bot API 10.1 rich messages carry their media inline in the block tree
        # (port of chat@4.31 4662309). `rich_message_media` walks the nested
        # blocks (lists, blockquotes, collages, slideshows, details) and yields
        # a flat `RichMedia` list; map each onto our `Attachment` shape. The
        # `mimeType` -> `mime_type` rename is the only field-name boundary.
        rich_message = raw.get("rich_message")
        if rich_message:
            for media in rich_message_media(rich_message):
                attachments.append(
                    self.create_attachment(
                        media.type,
                        media.file["file_id"],
                        size=media.file.get("file_size"),
                        width=media.width,
                        height=media.height,
                        name=media.name,
                        mime_type=media.mime_type,
                        file_unique_id=media.file.get("file_unique_id"),
                    )
                )

        return attachments

    def create_attachment(
        self,
        type_: str,
        file_id: str,
        *,
        size: int | None = None,
        width: int | None = None,
        height: int | None = None,
        name: str | None = None,
        mime_type: str | None = None,
        file_unique_id: str | None = None,
    ) -> Attachment:
        """Create an :class:`Attachment` with a lazy ``fetch_data`` callback.

        ``file_unique_id`` is Telegram's stable identity for a file (the
        ``file_id`` changes per resend); it is kept in ``fetch_metadata`` only
        when truthy, matching upstream's conditional spread (vercel/chat#752).
        """
        fetch_metadata: dict[str, Any] = {"fileId": file_id}
        if file_unique_id:
            fetch_metadata["fileUniqueId"] = file_unique_id
        return Attachment(
            type=type_,  # type: ignore[arg-type]
            size=size,
            width=width,
            height=height,
            name=name,
            mime_type=mime_type,
            fetch_data=lambda _fid=file_id: self.download_file(_fid),
            fetch_metadata=fetch_metadata,
        )

    def rehydrate_attachment(self, attachment: Attachment) -> Attachment:
        """Reconstruct ``fetch_data`` on a deserialized Telegram attachment.

        Pulls ``fileId`` from ``fetch_metadata`` and rebuilds the lazy
        ``download_file`` closure.  Returns the attachment unchanged when
        no file ID is present (e.g. a pre-serialized attachment that did
        not originate from this adapter).
        """
        meta = attachment.fetch_metadata if attachment.fetch_metadata is not None else {}
        file_id = meta.get("fileId")
        if not file_id:
            return attachment
        return Attachment(
            type=attachment.type,
            url=attachment.url,
            name=attachment.name,
            mime_type=attachment.mime_type,
            size=attachment.size,
            width=attachment.width,
            height=attachment.height,
            data=attachment.data,
            fetch_data=lambda _fid=file_id: self.download_file(_fid),
            fetch_metadata=attachment.fetch_metadata,
        )

    async def download_file(self, file_id: str) -> bytes:
        """Download a file from Telegram by its ``file_id``."""
        import aiohttp

        file_info: TelegramFile = await self.telegram_fetch(
            "getFile",
            {"file_id": file_id},
        )

        file_path = file_info.get("file_path")
        if not file_path:
            raise ResourceNotFoundError("telegram", "file", file_id)

        file_url = f"{self._api_base_url}/file/bot{self._bot_token}/{file_path}"

        # The host is operator-configured (self-hosted Bot API servers are
        # common) and the path comes from Telegram's getFile, so -- like
        # upstream (vercel/chat#865) -- the guard here is a size cap and a
        # timeout, not the shared downloader's public-host checks. The 30 s
        # total timeout covers connecting and reading the body.
        try:
            session = await self._get_http_session()
            async with session.get(
                file_url,
                timeout=aiohttp.ClientTimeout(total=TELEGRAM_FILE_TIMEOUT_MS / 1000),
            ) as response:
                if not response.ok:
                    raise NetworkError(
                        "telegram",
                        f"Failed to download Telegram file {file_id}: {response.status}",
                    )
                return await self._read_telegram_file(response, file_id)
        except (aiohttp.ClientError, TimeoutError) as error:
            raise NetworkError(
                "telegram",
                f"Failed to download Telegram file {file_id}",
                error,
            ) from error

    @staticmethod
    async def _read_telegram_file(response: Any, file_id: str) -> bytes:
        """Read a file body, refusing anything over :data:`TELEGRAM_FILE_LIMIT`.

        Port of upstream ``readTelegramFile``: a declared ``Content-Length``
        over the cap is refused before reading; otherwise a running byte
        count stops the read as soon as the cap is passed, so nothing beyond
        one chunk past the cap is ever buffered.
        """
        try:
            declared = response.content_length
        except ValueError:
            # An unparseable header is ignored, as ``Number(...)`` -> NaN is
            # upstream; the running count below still applies.
            declared = None
        if declared is not None and declared > TELEGRAM_FILE_LIMIT:
            raise NetworkError("telegram", f"Telegram file {file_id} exceeds the download limit")
        body = bytearray()
        async for chunk in response.content.iter_chunked(64 * 1024):
            if len(body) + len(chunk) > TELEGRAM_FILE_LIMIT:
                raise NetworkError("telegram", f"Telegram file {file_id} exceeds the download limit")
            body.extend(chunk)
        return bytes(body)

    async def send_document(
        self,
        thread: TelegramThreadId,
        file: Any,
        text: str,
        plain_text: str,
        reply_markup: TelegramInlineKeyboardMarkup | None = None,
        parse_mode: str | None = None,
    ) -> TelegramMessage:
        """Send a document (file upload) to Telegram."""
        data = getattr(file, "data", b"")
        if isinstance(data, memoryview) or not isinstance(data, bytes):
            data = bytes(data)

        async def _send(resolved_parse_mode: str | None, resolved_text: str) -> TelegramMessage:
            # FormData is rebuilt per attempt: aiohttp marks a FormData
            # instance processed after one send, so the markdown-parse
            # retry needs a fresh one (upstream rebuilds for the same
            # reason — undici FormData bodies are single-use streams).
            return await self.telegram_fetch(
                "sendDocument",
                self._create_telegram_document_form_data(
                    thread,
                    file,
                    data,
                    resolved_text,
                    reply_markup,
                    resolved_parse_mode,
                ),
            )

        return await self.with_telegram_markdown_fallback(
            parse_mode,
            _send,
            initial_text=text,
            fallback_text=plain_text,
            method="sendDocument",
            thread_id=self.encode_thread_id(thread),
        )

    def _create_telegram_document_form_data(
        self,
        thread: TelegramThreadId,
        file: Any,
        data: bytes,
        text: str,
        reply_markup: TelegramInlineKeyboardMarkup | None = None,
        parse_mode: str | None = None,
    ) -> Any:
        """Build the multipart form body for a ``sendDocument`` call."""
        import aiohttp

        form_data = aiohttp.FormData()
        form_data.add_field("chat_id", thread.chat_id)

        if isinstance(thread.message_thread_id, int):
            form_data.add_field("message_thread_id", str(thread.message_thread_id))

        if text.strip():
            form_data.add_field("caption", self.truncate_caption(text, parse_mode))
            if parse_mode:
                form_data.add_field("parse_mode", parse_mode)

        filename = getattr(file, "filename", "file")
        content_type = getattr(file, "mime_type", None) or "application/octet-stream"
        form_data.add_field(
            "document",
            data,
            filename=filename,
            content_type=content_type,
        )

        if reply_markup:
            form_data.add_field("reply_markup", json.dumps(reply_markup))

        return form_data

    async def send_attachment(
        self,
        thread: TelegramThreadId,
        attachment: Attachment,
        text: str,
        plain_text: str,
        reply_markup: TelegramInlineKeyboardMarkup | None = None,
        parse_mode: str | None = None,
    ) -> TelegramMessage:
        """Send a typed attachment using the Telegram media method for its type.

        Port of upstream ``sendAttachment`` (vercel/chat#485). Selects
        ``sendPhoto``/``sendAudio``/``sendVideo``/``sendDocument`` based on
        :data:`ATTACHMENT_UPLOADS`. Binary payloads (``data`` or ``fetch_data``)
        are sent as multipart uploads; URL-only attachments are passed through
        as a Telegram URL field (must be a public URL Telegram can fetch).
        """
        import aiohttp

        if attachment.type not in ATTACHMENT_UPLOADS:
            raise ValidationError(
                "telegram",
                f"Unsupported attachment type: {attachment.type}. Supported types: {', '.join(ATTACHMENT_UPLOADS)}",
            )
        upload = ATTACHMENT_UPLOADS[attachment.type]

        data = attachment.data
        if data is None and attachment.fetch_data is not None:
            data = await attachment.fetch_data()

        if data is None and not attachment.url:
            raise ValidationError(
                "telegram",
                f"Attachment data or URL required for {attachment.type}",
            )

        if data is not None and (isinstance(data, memoryview) or not isinstance(data, bytes)):
            data = bytes(data)

        async def _send(resolved_parse_mode: str | None, resolved_text: str) -> TelegramMessage:
            if data is None:
                payload: dict[str, Any] = {
                    "chat_id": thread.chat_id,
                    upload["field"]: attachment.url,
                }

                if isinstance(thread.message_thread_id, int):
                    payload["message_thread_id"] = thread.message_thread_id

                if resolved_text.strip():
                    payload["caption"] = self.truncate_caption(resolved_text, resolved_parse_mode)
                    if resolved_parse_mode:
                        payload["parse_mode"] = resolved_parse_mode

                if attachment.type == "video":
                    if isinstance(attachment.width, int):
                        payload["width"] = attachment.width
                    if isinstance(attachment.height, int):
                        payload["height"] = attachment.height

                if reply_markup:
                    payload["reply_markup"] = reply_markup

                return await self.telegram_fetch(upload["method"], payload)

            # FormData is rebuilt per attempt — see send_document.
            form_data = aiohttp.FormData()
            form_data.add_field("chat_id", thread.chat_id)

            if isinstance(thread.message_thread_id, int):
                form_data.add_field("message_thread_id", str(thread.message_thread_id))

            if resolved_text.strip():
                form_data.add_field("caption", self.truncate_caption(resolved_text, resolved_parse_mode))
                if resolved_parse_mode:
                    form_data.add_field("parse_mode", resolved_parse_mode)

            if attachment.type == "video":
                if isinstance(attachment.width, int):
                    form_data.add_field("width", str(attachment.width))
                if isinstance(attachment.height, int):
                    form_data.add_field("height", str(attachment.height))

            form_data.add_field(
                upload["field"],
                data,
                filename=attachment.name if attachment.name is not None else "attachment",
                content_type=attachment.mime_type if attachment.mime_type is not None else "application/octet-stream",
            )

            if reply_markup:
                form_data.add_field("reply_markup", json.dumps(reply_markup))

            return await self.telegram_fetch(upload["method"], form_data)

        return await self.with_telegram_markdown_fallback(
            parse_mode,
            _send,
            initial_text=text,
            fallback_text=plain_text,
            method=upload["method"],
            thread_id=self.encode_thread_id(thread),
        )

    # -- Message caching -----------------------------------------------------

    def cache_message(self, message: Message) -> None:
        """Store or update a message in the in-memory cache."""
        existing = self._message_cache.get(message.thread_id, [])
        index = next(
            (i for i, m in enumerate(existing) if m.id == message.id),
            -1,
        )

        if index >= 0:
            existing[index] = message
        else:
            existing.append(message)

        existing.sort(key=lambda m: (m.metadata.date_sent, self.message_sequence(m.id)))
        self._message_cache[message.thread_id] = existing

    def find_cached_message(self, message_id: str) -> Message | None:
        """Find a cached message by ID across all threads."""
        for messages in self._message_cache.values():
            for msg in messages:
                if msg.id == message_id:
                    return msg
        return None

    def delete_cached_message(self, message_id: str) -> None:
        """Remove a message from the cache."""
        for tid in list(self._message_cache.keys()):
            messages = self._message_cache[tid]
            filtered = [m for m in messages if m.id != message_id]
            if not filtered:
                del self._message_cache[tid]
            elif len(filtered) != len(messages):
                self._message_cache[tid] = filtered

    def compare_messages(self, a: Message, b: Message) -> int:
        """Compare two messages for sorting (older first)."""
        time_diff = a.metadata.date_sent.timestamp() - b.metadata.date_sent.timestamp()
        if time_diff != 0:
            return -1 if time_diff < 0 else 1

        return self.message_sequence(a.id) - self.message_sequence(b.id)

    def message_sequence(self, message_id: str) -> int:
        """Extract the numeric sequence from a composite message ID."""
        match = MESSAGE_SEQUENCE_PATTERN.search(message_id)
        return int(match.group(1)) if match else 0

    def create_draft_id(self) -> int:
        """Return the next draft id for native DM draft streaming.

        Monotonically increasing per adapter instance so concurrent streams
        to the same chat update distinct draft bubbles; wraps to 1 past
        Telegram's signed-int32 maximum.
        """
        self._next_draft_id = 1 if self._next_draft_id >= 2_147_483_647 else self._next_draft_id + 1
        return self._next_draft_id

    # -- Pagination ----------------------------------------------------------

    def paginate_messages(
        self,
        messages: list[Message],
        options: FetchOptions,
    ) -> FetchResult:
        """Paginate a list of messages according to fetch options."""
        limit = max(1, min(getattr(options, "limit", 50) if getattr(options, "limit", 50) is not None else 50, 100))
        direction = getattr(options, "direction", "backward") or "backward"

        if not messages:
            return FetchResult(messages=[])

        message_index_by_id: dict[str, int] = {m.id: i for i, m in enumerate(messages)}

        cursor = getattr(options, "cursor", None)

        if direction == "backward":
            end = message_index_by_id[cursor] if cursor and cursor in message_index_by_id else len(messages)
            start = max(0, end - limit)
            page = messages[start:end]

            return FetchResult(
                messages=page,
                next_cursor=page[0].id if (start > 0 and page) else None,
            )

        # forward
        start = message_index_by_id[cursor] + 1 if cursor and cursor in message_index_by_id else 0
        end = min(len(messages), start + limit)
        page = messages[start:end]

        return FetchResult(
            messages=page,
            next_cursor=page[-1].id if (end < len(messages) and page) else None,
        )

    # -- Message ID encoding / decoding --------------------------------------

    def encode_message_id(self, chat_id: str, message_id: int) -> str:
        """Encode a chat ID and message ID into a composite string."""
        return f"{chat_id}:{message_id}"

    def decode_composite_message_id(
        self,
        message_id: str,
        expected_chat_id: str | None = None,
    ) -> dict[str, Any]:
        """Decode a composite message ID string.

        Returns a dict with ``chat_id``, ``message_id`` (int), and ``composite_id``.
        """
        composite_match = MESSAGE_ID_PATTERN.match(message_id)

        if composite_match:
            chat_id = composite_match.group(1)
            raw_message_id = composite_match.group(2)
            parsed_message_id = int(raw_message_id)

            if expected_chat_id and chat_id != expected_chat_id:
                raise ValidationError(
                    "telegram",
                    f"Message ID chat mismatch: expected {expected_chat_id}, got {chat_id}",
                )

            return {
                "chat_id": chat_id,
                "message_id": parsed_message_id,
                "composite_id": f"{chat_id}:{parsed_message_id}",
            }

        if not expected_chat_id:
            raise ValidationError(
                "telegram",
                f"Telegram message ID must be in <chatId>:<messageId> format, got: {message_id}",
            )

        try:
            parsed_message_id = int(message_id)
        except (ValueError, TypeError) as exc:
            raise ValidationError(
                "telegram",
                f"Invalid Telegram message ID: {message_id}",
            ) from exc

        if not math.isfinite(parsed_message_id):
            raise ValidationError(
                "telegram",
                f"Invalid Telegram message ID: {message_id}",
            )

        return {
            "chat_id": expected_chat_id,
            "message_id": parsed_message_id,
            "composite_id": f"{expected_chat_id}:{parsed_message_id}",
        }

    # -- Author helpers ------------------------------------------------------

    def to_author(self, user: TelegramUser) -> Author:
        """Convert a Telegram user to an :class:`Author`."""
        full_name = " ".join(filter(None, [user.get("first_name"), user.get("last_name")])).strip()

        return Author(
            user_id=str(user.get("id", "")),
            user_name=user.get("username") or user.get("first_name") or str(user.get("id", "")),
            full_name=full_name or user.get("username") or str(user.get("id", "")),
            is_bot=user.get("is_bot", False),
            is_me=str(user.get("id", "")) == self._bot_user_id,
        )

    def to_reaction_actor_author(self, chat: TelegramChat) -> Author:
        """Convert a Telegram chat to an :class:`Author` for reaction events."""
        name = self.chat_display_name(chat) or str(chat.get("id", ""))
        return Author(
            user_id=f"chat:{chat.get('id', '')}",
            user_name=name,
            full_name=name,
            is_bot="unknown",
            is_me=False,
        )

    def chat_display_name(self, chat: TelegramChat) -> str | None:
        """Get the display name for a Telegram chat."""
        title = chat.get("title")
        if title:
            return title

        private_name = " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")])).strip()
        if private_name:
            return private_name

        return chat.get("username")

    # -- Mention detection ---------------------------------------------------

    def is_bot_mentioned(self, message: TelegramMessage, text: str) -> bool:
        """Check if the bot is mentioned in a message."""
        if not text:
            return False

        username = self._user_name
        entities = message.get("entities") or message.get("caption_entities") or []

        for entity in entities:
            entity_type = entity.get("type", "")

            if entity_type == "mention":
                mention_text = self.entity_text(text, entity)
                if mention_text.lower() == f"@{username.lower()}":
                    return True

            if entity_type == "text_mention":
                entity_user = entity.get("user")
                if entity_user and self._bot_user_id and str(entity_user.get("id", "")) == self._bot_user_id:
                    return True

            if entity_type == "bot_command":
                command_text = self.entity_text(text, entity)
                if command_text.lower().endswith(f"@{username.lower()}"):
                    return True

        return bool(self._get_mention_regex(username).search(text))

    def _get_mention_regex(self, username: str) -> re.Pattern[str]:
        """The ``@username`` pattern, compiled once per username.

        ``(?![\\w-])`` rather than ``\\b`` so ``@mybot-dev`` does not mention
        ``@mybot`` (vercel/chat#621); cached per username (vercel/chat#706).
        JS ``\\w`` (no ``u`` flag) is ASCII-only, so the lookahead spells out
        ``[A-Za-z0-9_-]``; ``re.ASCII`` is not used because JS ``/i`` still
        case-folds non-ASCII letters (``@ботик`` matches ``@БОТИК``).
        """
        if self._mention_regex is None or self._mention_regex_username != username:
            self._mention_regex_username = username
            self._mention_regex = re.compile(
                rf"@{self.escape_regex(username)}(?![A-Za-z0-9_-])",
                re.IGNORECASE,
            )
        return self._mention_regex

    def entity_text(self, text: str, entity: TelegramMessageEntity) -> str:
        """Extract entity text from a message using UTF-16 offsets."""
        offset = entity.get("offset", 0)
        length = entity.get("length", 0)
        # Use UTF-16 encoding for correct offset handling
        utf16 = text.encode("utf-16-le")
        return utf16[offset * 2 : (offset + length) * 2].decode("utf-16-le")

    @staticmethod
    def _slice_utf16(text: str, offset: int) -> str:
        """Return the substring of ``text`` from a UTF-16 code-unit ``offset``.

        Telegram entity offsets count UTF-16 code units (matching JavaScript
        ``String.prototype.slice``), so an astral-plane code point (e.g. an
        emoji) advances the offset by two. Naive Python ``str`` slicing counts
        code points and would mis-split such text — this encodes to UTF-16-LE
        and slices by byte, mirroring :meth:`entity_text`.
        """
        return text.encode("utf-16-le")[offset * 2 :].decode("utf-16-le")

    @staticmethod
    def escape_regex(input_str: str) -> str:
        """Escape regex special characters."""
        return re.escape(input_str)

    def normalize_user_name(self, value: Any) -> str:
        """Normalize a username by stripping leading ``@`` characters."""
        if not isinstance(value, str):
            return "bot"
        result = LEADING_AT_PATTERN.sub("", value).strip()
        return result or "bot"

    # -- Parse mode ----------------------------------------------------------

    def resolve_parse_mode(
        self,
        message: AdapterPostableMessage,
        card: Any,
    ) -> str | None:
        """Determine the Telegram ``parse_mode`` for an outgoing message.

        Cards and any message routed through the format converter are
        rendered as MarkdownV2, so Telegram must parse them with
        ``MarkdownV2``. Plain strings and ``{"raw": ...}`` payloads ship
        verbatim with no parse mode (Bot API field omitted).
        """
        if card:
            return TELEGRAM_MARKDOWN_PARSE_MODE
        # Plain strings ship as-is.
        if isinstance(message, str):
            return None
        # ``{"raw": ...}`` and dataclasses with ``.raw`` ship as-is.
        if isinstance(message, dict) and "raw" in message:
            return None
        if hasattr(message, "raw") and not isinstance(message, str):
            return None
        # Every other shape ({markdown}, {ast}, JSX, etc.) flows through
        # format_converter.render_postable, which emits MarkdownV2.
        return TELEGRAM_MARKDOWN_PARSE_MODE

    def render_plain_text_message(
        self,
        message: AdapterPostableMessage,
        card: Any,
    ) -> str:
        """Render a postable message as plain text (no MarkdownV2 markup).

        Port of upstream ``renderPlainTextMessage`` (vercel/chat#340): the
        retry body used when Telegram rejects the MarkdownV2 entities of
        the primary rendering. Handles both dict- and dataclass-shaped
        postables (mirroring ``render_postable``'s branch order: raw →
        markdown → ast).
        """
        if card:
            return card_to_fallback_text(card)
        if isinstance(message, str):
            return message
        if isinstance(message, dict):
            if "raw" in message:
                return message["raw"]
            if "markdown" in message:
                return self.resolve_telegram_fallback_text(
                    message["markdown"], _markdown_to_plain_text(message["markdown"])
                )
            if "ast" in message:
                return ast_to_plain_text(message["ast"])
        else:
            if hasattr(message, "raw"):
                return message.raw
            if hasattr(message, "markdown"):
                markdown = getattr(message, "markdown", "")
                return self.resolve_telegram_fallback_text(markdown, _markdown_to_plain_text(markdown))
            if hasattr(message, "ast"):
                return ast_to_plain_text(getattr(message, "ast", {}))
        return self._format_converter.render_postable(message)

    def resolve_telegram_fallback_text(self, original_text: str, fallback_text: str) -> str:
        """Prefer *fallback_text* unless stripping markup left it empty.

        A whitespace-only plain rendering (e.g. ``**`` — markers with no
        content) falls back to *original_text* so the retry never sends a
        body Telegram rejects as empty.
        """
        return fallback_text if fallback_text.strip() else original_text

    # -- Native rich messages (vercel/chat#479) ------------------------------

    def resolve_rich_message(
        self,
        message: AdapterPostableMessage,
        card: Any,
        file_count: int,
        attachment_count: int,
    ) -> RichMessageResolution | None:
        """Resolve a postable into a native rich-message payload, or ``None``.

        Port of upstream ``resolveRichMessage``. Returns ``None`` — disabling
        the rich path so the caller sends a regular MarkdownV2/plain message —
        when ANY of the following hold (a disjunction, ported exactly):

        * rich messages are unavailable (the cached endpoint-missing latch);
        * the message carries a card;
        * a file or attachment is being uploaded;
        * the message is a plain string;
        * the message carries a ``raw`` field (**key presence**, not
          truthiness — an empty ``raw`` still opts out of rich rendering).

        Only ``{"markdown": …}`` and ``{"ast": …}`` shapes resolve to a rich
        payload; everything else falls through to ``None``.
        """
        # ``"raw" in message`` — key PRESENCE, not truthiness: dict membership
        # for mapping payloads, attribute presence for dataclass payloads.
        has_raw = (isinstance(message, dict) and "raw" in message) or (
            not isinstance(message, str) and hasattr(message, "raw")
        )
        if (
            not self._rich_messages_available
            or card
            or file_count > 0
            or attachment_count > 0
            or isinstance(message, str)
            or has_raw
        ):
            return None

        markdown_value = self._postable_field(message, "markdown")
        if markdown_value is not None:
            formatted = self._format_converter.to_ast(markdown_value)
            return RichMessageResolution(
                formatted=formatted,
                markdown=truncate_rich_markdown(convert_emoji_placeholders(markdown_value, "gchat")),
                text=ast_to_plain_text(formatted),
            )

        ast_value = self._postable_field(message, "ast")
        if ast_value is not None:
            return RichMessageResolution(
                formatted=ast_value,
                markdown=truncate_rich_markdown(convert_emoji_placeholders(stringify_markdown(ast_value), "gchat")),
                text=ast_to_plain_text(ast_value),
            )

        return None

    @staticmethod
    def _postable_field(message: AdapterPostableMessage, field: str) -> Any:
        """Return ``message[field]`` for dict / dataclass postables, else ``None``.

        Mirrors upstream's ``"field" in message ? message.field : …`` key-
        presence narrowing across both the dict-shaped and dataclass-shaped
        postable representations.
        """
        if isinstance(message, dict):
            return message.get(field)
        if hasattr(message, field):
            return getattr(message, field)
        return None

    async def send_regular_message(
        self,
        thread: TelegramThreadId,
        text: str,
        plain_text: str,
        parse_mode: str | None,
        reply_markup: TelegramInlineKeyboardMarkup | None,
        thread_id: str,
    ) -> TelegramMessage:
        """Send a non-rich message via ``sendMessage`` with markdown fallback.

        Port of upstream ``sendRegularMessage``: the MarkdownV2-or-plain send
        path used both directly (plain strings, ``raw`` payloads, the
        rich-unavailable branch) and as the fallback operation passed to
        :meth:`with_telegram_rich_fallback`.
        """

        async def _send_message(resolved_parse_mode: str | None, resolved_text: str) -> TelegramMessage:
            return await self.telegram_fetch(
                "sendMessage",
                {
                    "chat_id": thread.chat_id,
                    "message_thread_id": thread.message_thread_id,
                    "text": resolved_text,
                    "reply_markup": reply_markup,
                    "parse_mode": resolved_parse_mode,
                },
            )

        return await self.with_telegram_markdown_fallback(
            parse_mode,
            _send_message,
            initial_text=text,
            fallback_text=plain_text,
            method="sendMessage",
            thread_id=thread_id,
        )

    def can_fallback_from_rich_message(self, error: object, method: str) -> bool:
        """Whether a rich-message failure may retry as a regular message.

        Port of upstream ``canFallbackFromRichMessage``. Returns ``True`` for
        the *transient and the permanent* rich failures alike — i.e. the
        message should be retried via the regular MarkdownV2/plain path:

        * a ``ResourceNotFoundError`` raised by a ``sendRichMessage*`` call
          (the 404 ``method not found``); OR
        * a :class:`ValidationError` whose message contains ``can't parse``,
          or ``method`` + ``not found``, or ``rich message`` + ``unsupported``.

        Distinct from :meth:`remember_rich_message_failure`, which decides the
        *narrower* set that additionally latches rich off permanently. A
        transient ``can't parse`` qualifies here but NOT there.
        """
        message = str(error).lower() if isinstance(error, ValidationError) else ""
        missing_method = "method" in message and "not found" in message
        unsupported_rich = "rich message" in message and "unsupported" in message

        return bool(
            (method.startswith("sendRichMessage") and isinstance(error, ResourceNotFoundError))
            or (isinstance(error, ValidationError) and ("can't parse" in message or missing_method or unsupported_rich))
        )

    def remember_rich_message_failure(self, error: object, method: str) -> None:
        """Latch rich messages off when a failure proves the endpoint is gone.

        Port of upstream ``rememberRichMessageFailure``. Flips
        ``_rich_messages_available`` to ``False`` — permanently, for the life
        of the adapter instance — ONLY for the endpoint-missing / unsupported
        class of failures:

        * a ``ResourceNotFoundError`` from a ``sendRichMessage*`` call; OR
        * a :class:`ValidationError` containing ``method`` + ``not found`` or
          ``rich message`` + ``unsupported``.

        A transient ``can't parse`` / generic validation failure does NOT
        match here, so rich remains enabled for subsequent messages — the key
        distinction from :meth:`can_fallback_from_rich_message`.
        """
        message = str(error).lower() if isinstance(error, ValidationError) else ""
        missing_method = "method" in message and "not found" in message
        unsupported_rich = "rich message" in message and "unsupported" in message

        if (method.startswith("sendRichMessage") and isinstance(error, ResourceNotFoundError)) or (
            isinstance(error, ValidationError) and (missing_method or unsupported_rich)
        ):
            self._rich_messages_available = False

    async def with_telegram_rich_fallback(
        self,
        operation: Callable[[], Awaitable[_T]],
        fallback: Callable[[], Awaitable[_T]],
        *,
        method: str,
        message_id: str | None = None,
        thread_id: str | None = None,
    ) -> _T:
        """Run *operation* (a rich send), retrying via *fallback* on rich errors.

        Port of upstream ``withTelegramRichFallback``. On failure: if
        :meth:`can_fallback_from_rich_message` rejects the error it re-raises
        unchanged (non-rich failures must surface); otherwise it records the
        failure via :meth:`remember_rich_message_failure` (which may latch
        rich off) and returns *fallback*'s result — the regular send.
        """
        try:
            return await operation()
        except Exception as error:
            if not self.can_fallback_from_rich_message(error, method):
                raise

            self.remember_rich_message_failure(error, method)
            log_context: dict[str, Any] = {"error": str(error), "method": method}
            if message_id is not None:
                log_context["message_id"] = message_id
            if thread_id is not None:
                log_context["thread_id"] = thread_id
            self._logger.warn(
                "Telegram rich message failed; retrying with a regular message",
                log_context,
            )
            return await fallback()

    # -- Truncation ----------------------------------------------------------

    def truncate_message(self, text: str, parse_mode: str | None = None) -> str:
        """Truncate message text to the Telegram message limit.

        For ``parse_mode == "MarkdownV2"`` uses :func:`truncate_for_telegram`,
        which escapes the ellipsis and walks back past any unbalanced
        entity delimiter or orphan backslash so the result is parseable.
        For plain text, falls back to UTF-16 truncation with a literal
        ``"..."`` ellipsis.
        """
        if parse_mode == "MarkdownV2":
            return truncate_for_telegram(text, TELEGRAM_MESSAGE_LIMIT, parse_mode)
        return _truncate_to_utf16(text, TELEGRAM_MESSAGE_LIMIT)

    def truncate_caption(self, text: str, parse_mode: str | None = None) -> str:
        """Truncate caption text to the Telegram caption limit.

        See :meth:`truncate_message` for parse-mode handling.
        """
        if parse_mode == "MarkdownV2":
            return truncate_for_telegram(text, TELEGRAM_CAPTION_LIMIT, parse_mode)
        return _truncate_to_utf16(text, TELEGRAM_CAPTION_LIMIT)

    # -- Emoji / reactions ---------------------------------------------------

    def to_telegram_reaction(self, emoji: EmojiValue | str) -> TelegramReactionType:
        """Convert an emoji value to a Telegram reaction type."""
        if not isinstance(emoji, str):
            return {
                "type": "emoji",
                "emoji": emoji_to_unicode(emoji),
            }

        if emoji.startswith("custom:"):
            return {
                "type": "custom_emoji",
                "custom_emoji_id": emoji[len("custom:") :],
            }

        placeholder_match = EMOJI_PLACEHOLDER_PATTERN.match(emoji)
        if placeholder_match:
            return {
                "type": "emoji",
                "emoji": emoji_to_unicode(placeholder_match.group(1)),
            }

        if EMOJI_NAME_PATTERN.match(emoji):
            return {
                "type": "emoji",
                "emoji": emoji_to_unicode(emoji.lower()),
            }

        return {
            "type": "emoji",
            "emoji": emoji,
        }

    def reaction_key(self, reaction: TelegramReactionType) -> str:
        """Compute a unique key for a Telegram reaction."""
        # `TelegramReactionType` is a TypedDict union; `.get()` returns the
        # union of all value-types, so narrow the strings we know are strings.
        if reaction.get("type") == "emoji":
            return cast("str", reaction.get("emoji", ""))
        return f"custom:{cast('str', reaction.get('custom_emoji_id', ''))}"

    def reaction_to_emoji_value(self, reaction: TelegramReactionType) -> EmojiValue:
        """Convert a Telegram reaction to an :class:`EmojiValue`."""
        if reaction.get("type") == "emoji":
            return get_emoji(cast("str", reaction.get("emoji", "")))
        return get_emoji(f"custom:{cast('str', reaction.get('custom_emoji_id', ''))}")

    # -- Telegram API --------------------------------------------------------

    async def telegram_fetch(
        self,
        method: str,
        payload: Any = None,
        *,
        signal: asyncio.Event | None = None,  # noqa: ARG002 - reserved for future cancellation support
    ) -> Any:
        """Call a Telegram Bot API method.

        Accepts either a dict payload (sent as JSON) or an
        ``aiohttp.FormData`` payload (sent as multipart/form-data).

        *signal* is currently unused but kept for API compatibility.
        """
        import aiohttp

        url = f"{self._api_base_url}/bot{self._bot_token}/{method}"
        is_form = False

        with contextlib.suppress(Exception):
            is_form = isinstance(payload, aiohttp.FormData)

        try:
            session = await self._get_http_session()
            if is_form:
                async with session.post(url, data=payload) as response:
                    data = await self._parse_telegram_response(method, response)
            else:
                async with session.post(
                    url,
                    json=payload or {},
                    headers={"Content-Type": "application/json"},
                ) as response:
                    data = await self._parse_telegram_response(method, response)
        except (aiohttp.ClientError, OSError) as error:
            raise NetworkError(
                "telegram",
                f"Network error calling Telegram {method}",
                error,
            ) from error

        return data

    async def _parse_telegram_response(
        self,
        method: str,
        response: Any,
    ) -> Any:
        """Parse a Telegram API response, raising appropriate errors."""
        try:
            data: TelegramApiResponse = await response.json(content_type=None)
        except Exception as exc:
            raise NetworkError(
                "telegram",
                f"Failed to parse Telegram API response for {method}",
            ) from exc

        if not (response.ok and data.get("ok")):
            self.throw_telegram_api_error(method, response.status, data)

        result = data.get("result")
        if result is None and "result" not in data:
            raise NetworkError(
                "telegram",
                f"Telegram API {method} returned no result",
            )

        return result

    def throw_telegram_api_error(
        self,
        method: str,
        status: int,
        data: TelegramApiResponse,
    ) -> None:
        """Raise the appropriate error for a Telegram API failure."""
        error_code = data.get("error_code") or status
        description = data.get("description") or f"Telegram API {method} failed"

        if error_code == 429:
            params = data.get("parameters") or {}
            raise AdapterRateLimitError("telegram", params.get("retry_after"))

        if error_code == 401:
            raise AuthenticationError("telegram", description)

        if error_code == 403:
            raise AdapterPermissionError("telegram", method)

        if error_code == 404:
            raise ResourceNotFoundError("telegram", method)

        if 400 <= error_code < 500:
            raise ValidationError("telegram", description)

        raise NetworkError(
            "telegram",
            f"{description} (status {status}, error {error_code})",
        )

    # -- Markdown parse-error fallback (vercel/chat#340) -----------------------

    async def with_telegram_markdown_fallback(
        self,
        parse_mode: str | None,
        operation: Callable[[str | None, str], Awaitable[_T]],
        *,
        initial_text: str,
        fallback_text: str,
        method: str,
        message_id: str | None = None,
        thread_id: str | None = None,
    ) -> _T:
        """Run *operation*, retrying without ``parse_mode`` on entity errors.

        ``operation`` receives ``(parse_mode, text)`` and must build its own
        request body per attempt (multipart bodies are single-use). The
        first attempt ships ``(parse_mode, initial_text)``; when Telegram
        rejects MarkdownV2 with a ``can't parse entities`` 400, the retry
        ships ``(None, fallback)`` — the plain rendering, or the original
        text when stripping markup left nothing (see
        :meth:`resolve_telegram_fallback_text`). Every other failure, and
        any failure for non-MarkdownV2 sends, propagates unchanged.
        """
        try:
            return await operation(parse_mode, initial_text)
        except Exception as error:
            if parse_mode != TELEGRAM_MARKDOWN_PARSE_MODE or not self.is_telegram_markdown_parse_error(error):
                raise

            log_context: dict[str, Any] = {
                "error": str(error),
                "initial_text": initial_text,
                "fallback_text": fallback_text,
                "method": method,
            }
            if message_id is not None:
                log_context["message_id"] = message_id
            if thread_id is not None:
                log_context["thread_id"] = thread_id
            self._logger.warn(
                "Telegram markdown parse failed; retrying without parse mode",
                log_context,
            )

            return await operation(None, self.resolve_telegram_fallback_text(initial_text, fallback_text))

    def is_telegram_markdown_parse_error(self, error: object) -> bool:
        """Whether *error* is Telegram rejecting MarkdownV2 entity parsing.

        Matches the ``can't parse entities`` / ``can't parse caption
        entities`` 400s that :meth:`throw_telegram_api_error` surfaces as
        :class:`ValidationError`. Other 4xx validation failures (wrong
        chat, empty text, …) do not qualify and must propagate.
        """
        return (
            isinstance(error, ValidationError)
            and error.adapter == "telegram"
            and TELEGRAM_MARKDOWN_PARSE_ERROR_PATTERN.search(str(error)) is not None
        )

    # -- Polling config resolution -------------------------------------------

    def resolve_polling_config(
        self,
        override: TelegramLongPollingConfig | None = None,
    ) -> ResolvedTelegramLongPollingConfig:
        """Merge and validate polling configuration."""
        base = self._long_polling or TelegramLongPollingConfig()
        merged_allowed_updates = (
            override.allowed_updates if override and override.allowed_updates else None
        ) or base.allowed_updates
        merged_delete_webhook = (
            override.delete_webhook if (override and override.delete_webhook is not None) else base.delete_webhook
        )
        merged_drop_pending = (
            override.drop_pending_updates
            if (override and override.drop_pending_updates is not None)
            else base.drop_pending_updates
        )
        merged_limit = override.limit if (override and override.limit is not None) else base.limit
        merged_retry_delay = (
            override.retry_delay_ms if (override and override.retry_delay_ms is not None) else base.retry_delay_ms
        )
        merged_timeout = override.timeout if (override and override.timeout is not None) else base.timeout

        return ResolvedTelegramLongPollingConfig(
            allowed_updates=(list(merged_allowed_updates) if merged_allowed_updates else None),
            delete_webhook=merged_delete_webhook if merged_delete_webhook is not None else True,
            drop_pending_updates=bool(merged_drop_pending) if merged_drop_pending is not None else False,
            limit=self.clamp_integer(
                merged_limit,
                TELEGRAM_DEFAULT_POLLING_LIMIT,
                TELEGRAM_MIN_POLLING_LIMIT,
                TELEGRAM_MAX_POLLING_LIMIT,
            ),
            retry_delay_ms=self.clamp_integer(
                merged_retry_delay,
                TELEGRAM_DEFAULT_POLLING_RETRY_DELAY_MS,
                0,
                2**53 - 1,
            ),
            timeout=self.clamp_integer(
                merged_timeout,
                TELEGRAM_DEFAULT_POLLING_TIMEOUT_SECONDS,
                TELEGRAM_MIN_POLLING_TIMEOUT_SECONDS,
                TELEGRAM_MAX_POLLING_TIMEOUT_SECONDS,
            ),
        )

    @staticmethod
    def clamp_integer(
        value: int | float | None,
        fallback: int,
        min_val: int,
        max_val: int,
    ) -> int:
        """Clamp a numeric value to an integer within ``[min_val, max_val]``."""
        if value is None or not isinstance(value, (int, float)) or not math.isfinite(value):
            return fallback
        parsed = int(math.trunc(value))
        return max(min_val, min(max_val, parsed))

    # -- Private helpers -----------------------------------------------------

    def _resolve_thread_id(self, value: str) -> TelegramThreadId:
        """Resolve a string to a :class:`TelegramThreadId`."""
        if value.startswith("telegram:"):
            return self.decode_thread_id(value)
        return TelegramThreadId(chat_id=value)

    @staticmethod
    async def _get_request_body(request: Any) -> str:
        """Extract body text from a framework-agnostic request object."""
        # `hasattr` narrows `Any` → `object` (not awaitable); `getattr(..., None)`
        # preserves `Any` for the duck-typed framework paths.
        # Handle both callable and non-callable `request.text` forms.
        # Gating entry on callability would drop populated string attrs.
        text_attr = getattr(request, "text", None)
        if text_attr is not None:
            if callable(text_attr):
                result = text_attr()
                text_attr = await result if inspect.isawaitable(result) else result
            return text_attr.decode("utf-8") if isinstance(text_attr, (bytes, bytearray)) else str(text_attr)
        body = getattr(request, "body", None)
        if body is not None:
            if callable(body):
                result = body()
                body = await result if inspect.isawaitable(result) else result
            if isinstance(body, (bytes, bytearray)):
                return body.decode("utf-8")
            return str(body)
        return ""

    @staticmethod
    def _get_header(request: Any, name: str) -> str | None:
        """Get a header value from a framework-agnostic request object."""
        if hasattr(request, "headers"):
            headers = request.headers
            if isinstance(headers, dict):
                for k, v in headers.items():
                    if k.lower() == name.lower():
                        return v
                return None
            return headers.get(name)
        return None

    @staticmethod
    def _make_response(body: str, status: int) -> dict[str, Any]:
        """Create a framework-agnostic response dict."""
        return {"body": body, "status": status}


# =========================================================================
# Factory function
# =========================================================================


def create_telegram_adapter(
    config: TelegramAdapterConfig | None = None,
) -> TelegramAdapter:
    """Create a new :class:`TelegramAdapter` instance."""
    return TelegramAdapter(config or TelegramAdapterConfig())
