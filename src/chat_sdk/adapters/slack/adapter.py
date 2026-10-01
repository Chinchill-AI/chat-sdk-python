"""Slack adapter for chat-sdk.

Supports single-workspace (bot token) and multi-workspace (OAuth) modes.
All conversations use Slack threads as the unit of isolation.

Python port of packages/adapter-slack/src/index.ts.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import contextvars
import hmac
import inspect
import json
import os
import re
import time
import warnings
from collections import OrderedDict
from collections.abc import AsyncIterable, Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Literal, NoReturn, TypedDict, cast
from urllib.parse import parse_qs

from chat_sdk.adapters.slack.api import (
    _is_trusted_slack_response_url,
    is_slack_auth_url,
    is_trusted_slack_file_url,
    resolve_slack_bot_token,
)
from chat_sdk.adapters.slack.cards import (
    card_to_block_kit,
    card_to_fallback_text,
)
from chat_sdk.adapters.slack.crypto import (
    EncryptedTokenData,
    decode_key,
    decrypt_token,
    encrypt_token,
    is_encrypted_token_data,
)
from chat_sdk.adapters.slack.format import escape_slack_text, unescape_slack_text
from chat_sdk.adapters.slack.format_converter import SlackFormatConverter
from chat_sdk.adapters.slack.modals import (
    ModalMetadata,
    SlackModalResponse,
    decode_modal_metadata,
    encode_modal_metadata,
    modal_to_slack_view,
)
from chat_sdk.adapters.slack.types import (
    RequestContext,
    SlackAdapterConfig,
    SlackAdapterMode,
    SlackBotToken,
    SlackBotTokenResolver,
    SlackInstallation,
    SlackInstallationProvider,
    SlackThreadId,
    SlackWebhookVerifier,
)
from chat_sdk.adapters.slack.webhook import (
    read_slack_request_body,
    verify_slack_request,
)
from chat_sdk.cards import _js_number_to_string
from chat_sdk.emoji import emoji_to_slack, resolve_emoji_from_slack
from chat_sdk.logger import ConsoleLogger, Logger
from chat_sdk.modals import ModalElement, OptionsLoadGroup, SelectOptionElement
from chat_sdk.shared._js_compat import JS_WHITESPACE
from chat_sdk.shared.adapter_utils import (
    extract_card,
    extract_files,
    is_thinking_chunk,
    maybe_render_thinking,
)
from chat_sdk.shared.download import (
    AttachmentResponse,
    AttachmentTransport,
    download_attachment,
    validate_attachment_url,
)
from chat_sdk.shared.errors import (
    AdapterError,
    AdapterRateLimitError,
    AuthenticationError,
    NetworkError,
    ValidationError,
)
from chat_sdk.shared.log_utils import utf8_byte_length
from chat_sdk.shared.markdown_parser import Content, ast_to_plain_text
from chat_sdk.shared.mentions import mask_code_spans, replace_bare_mentions
from chat_sdk.types import (
    ActionEvent,
    AdapterPostableMessage,
    AppHomeOpenedEvent,
    AssistantContextChangedEvent,
    AssistantThreadStartedEvent,
    Attachment,
    Author,
    ChannelInfo,
    ChannelVisibility,
    ChatInstance,
    EmojiValue,
    EphemeralMessage,
    FetchOptions,
    FetchResult,
    FileUpload,
    FormattedContent,
    LinkPreview,
    ListThreadsOptions,
    ListThreadsResult,
    LockScope,
    MemberJoinedChannelEvent,
    Message,
    MessageDeletedEvent,
    MessageMetadata,
    ModalCloseEvent,
    ModalResponse,
    ModalSubmitEvent,
    OptionsLoadEvent,
    PostableMarkdown,
    RawMessage,
    ReactionEvent,
    ScheduledMessage,
    SlashCommandEvent,
    StreamChunk,
    StreamInput,
    StreamOptions,
    ThinkingChunk,
    ThreadInfo,
    ThreadSummary,
    UserInfo,
    WebhookOptions,
)

# Slack expects block_suggestion responses within 3s. Leave headroom for
# network latency so the HTTP response lands before Slack gives up.
OPTIONS_LOAD_TIMEOUT_MS = 2500

# Strong-reference set for fire-and-forget tasks to prevent GC collection.
_background_tasks: set[asyncio.Task[Any]] = set()


def _pin_task(task: asyncio.Task[Any]) -> None:
    """Pin a fire-and-forget task so the GC doesn't collect it."""
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SLACK_USER_ID_PATTERN = re.compile(r"^[A-Z0-9_]+$")
SLACK_USER_ID_EXACT_PATTERN = re.compile(r"^U[A-Z0-9]+$")

SLACK_MESSAGE_URL_PATTERN = re.compile(r"^https?://[^/]+\.slack\.com/archives/([A-Z0-9]+)/p(\d+)(?:\?.*)?$")
# Bracketed URL in message text; length-bounded to keep the scan linear on
# adversarial input (port of upstream ``BRACKETED_URL_PATTERN``, vercel/chat#779).
_BRACKETED_URL_PATTERN = re.compile(r"<(https?://[^>]{1,2048})>")

# Cache TTLs (milliseconds)
_USER_CACHE_TTL_MS = 8 * 24 * 60 * 60 * 1000  # 8 days
_CHANNEL_CACHE_TTL_MS = 8 * 24 * 60 * 60 * 1000
_REVERSE_INDEX_TTL_MS = 8 * 24 * 60 * 60 * 1000


class SlackUserCacheEntry(TypedDict, total=False):
    """Cached user shape returned by :meth:`SlackAdapter._lookup_user`.

    The first five keys always exist on a successful lookup or
    cache hit. ``_lookup_failed`` appears only on the failure path
    (API exception or empty user payload) — callers like
    :meth:`SlackAdapter.get_user` use it to return ``None`` instead of
    a fallback ``UserInfo``. ``total=False`` because both the cache hit
    branch and the failure branch omit ``_lookup_failed``.
    """

    display_name: str
    real_name: str
    email: str | None
    avatar_url: str | None
    is_bot: bool | None
    _lookup_failed: bool


def _make_slack_lookup_failed(user_id: str) -> SlackUserCacheEntry:
    """Build the sentinel cache entry for a failed Slack user lookup.

    Shared between the ``except`` path and the empty-user-payload path
    so both produce the exact same fallback shape (and neither caches
    it — see :meth:`SlackAdapter._lookup_user`).
    """
    return {
        "display_name": user_id,
        "real_name": user_id,
        "email": None,
        "avatar_url": None,
        "is_bot": None,
        "_lookup_failed": True,
    }


# Ignored message subtypes (system/meta events).
# `message_changed` / `message_deleted` are NOT in this set — they are routed
# to `_handle_message_changed` / `_handle_message_deleted` (unfurl caching and
# the `on_message_updated` / `on_message_deleted` lifecycle handlers).
_IGNORED_SUBTYPES = frozenset(
    {
        "message_replied",
        "channel_join",
        "channel_leave",
        "channel_topic",
        "channel_purpose",
        "channel_name",
        "channel_archive",
        "channel_unarchive",
        "group_join",
        "group_leave",
        "group_topic",
        "group_purpose",
        "group_name",
        "group_archive",
        "group_unarchive",
        "ekm_access_denied",
        "tombstone",
    }
)


def _edited_ts(message: dict[str, Any]) -> Any:
    """``message.edited?.ts`` (``None`` when ``edited`` is missing or not an object)."""
    edited = message.get("edited")
    return edited.get("ts") if isinstance(edited, dict) else None


def _with_inherited(message: dict[str, Any], **fallbacks: Any) -> dict[str, Any]:
    """Upstream ``{...message, key: message.key ?? fallback}`` for each fallback.

    A key whose resolved value is ``None`` is not added: upstream's ``??``
    yields ``undefined`` there, which never reaches the serialized payload, so
    ``Message.raw`` must not gain ``None`` keys Slack never sent.
    """
    out = dict(message)
    for key, fallback in fallbacks.items():
        value = message.get(key)
        if value is None:
            value = fallback
        if value is not None:
            out[key] = value
    return out


# Link-unfurl wait window: Slack delivers unfurled attachments via a
# separate `message_changed` event ~100-2000ms after the original. We
# poll briefly so the message handler sees enriched links instead of
# bare URLs.
_TRAILING_SLASH_PATTERN = re.compile(r"/$")
_UNFURL_WAIT_MS = 2000
_UNFURL_POLL_MS = 150
_UNFURL_CACHE_TTL_MS = 60 * 60 * 1000  # 1 hour


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _InstallationInfo:
    """Installation identity extracted from an interactive payload.

    ``installation_id`` is the team ID -- or the enterprise ID for
    Enterprise Grid org-wide installs (``is_enterprise_install``).
    """

    installation_id: str
    is_enterprise_install: bool
    enterprise_id: str | None = None


def _json_stringify(value: Any) -> str:
    """Compact, non-ASCII-preserving JSON like JS ``JSON.stringify`` (for log and error text)."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str)


def _bot_profile_user_id(event: dict[str, Any]) -> str | None:
    """``event.bot_profile?.user_id`` -- the bot's user (``U…``) id, if any."""
    profile = event.get("bot_profile")
    if not isinstance(profile, dict):
        return None
    user_id = profile.get("user_id")
    return user_id if isinstance(user_id, str) and user_id else None


# Slack's reserved user for platform-generated messages (channel archived,
# reminders, ...). Upstream ``SLACK_SYSTEM_USER_ID``.
_SLACK_SYSTEM_USER_ID = "USLACK"

# How content refers to the bot: as a mention Slack renders, as a literal
# token (inside code or display text), or not at all. ``literal`` explains an
# ``app_mention`` event without making it an invocation; ``none`` leaves the
# event unexplained. Upstream ``SlackMentionEvidence``.
_MentionEvidence = Literal["literal", "mention", "none"]


def _mention_token_pattern(user_id: str) -> re.Pattern[str]:
    """The opening ``<@U…`` / ``<@!U…`` of Slack's mention syntax for *user_id*.

    Upstream matches ``/<@!?{id}(?:\\|[^>]*)?>/i`` in one regex. That regex
    backtracks quadratically on a run of unclosed ``<@id|`` tokens, so
    :meth:`_MentionMatcher.search` matches this prefix and checks the tail
    separately; the two accept exactly the same strings. ``re.ASCII`` keeps
    ``IGNORECASE`` to ASCII case folding, like JS's ``i`` flag without ``u``.
    """
    return re.compile(rf"<@!?{re.escape(user_id)}", re.IGNORECASE | re.ASCII)


@dataclass(frozen=True)
class _MentionMatcher:
    """The bot to look for, compiled once per message (upstream ``mentionMatcher``).

    ``user_id`` is uppercased so a structured ``user`` element compares the
    way the case-insensitive token pattern matches.
    """

    prefix: re.Pattern[str]
    user_id: str

    def search(self, text: str) -> bool:
        """Whether *text* holds ``<@id>`` or ``<@id|…>`` (upstream ``token.test``).

        ``(?:\\|[^>]*)?>`` after the prefix means: a ``>`` right away, or a
        ``|`` with any ``>`` later in the text (``[^>]*`` stops at the first).
        """
        last_close = text.rfind(">")
        for match in self.prefix.finditer(text):
            end = match.end()
            if end <= last_close and (text[end] == ">" or text[end] == "|"):
                return True
        return False


def _mention_matcher(user_id: str) -> _MentionMatcher:
    return _MentionMatcher(prefix=_mention_token_pattern(user_id), user_id=user_id.upper())


def _classify_mrkdwn_mention(text: str, matcher: _MentionMatcher) -> _MentionEvidence:
    """Classify a mrkdwn string, where code is carried as backticks."""
    if not matcher.search(text):
        return "none"
    return "mention" if matcher.search(mask_code_spans(text)) else "literal"


def _classify_attachment_part(part: _AttachmentPart, matcher: _MentionMatcher) -> _MentionEvidence:
    """Classify one legacy attachment part.

    Literal parts render only Slack control sequences, so a ``<@U…>`` token
    there is a mention; mrkdwn parts carry code as backticks.
    """
    if part.mrkdwn:
        return _classify_mrkdwn_mention(part.text, matcher)
    return "mention" if matcher.search(part.text) else "none"


def _classify_blocks_mention(blocks: list[Any], matcher: _MentionMatcher) -> _MentionEvidence:
    """Classify how *blocks* refer to the matched user (upstream ``classifyBlocksMention``).

    Slack renders a mention from a ``user`` element, and from a ``<@U…>``
    token in a text object explicitly typed ``mrkdwn``. A rich-text ``text``
    element, a link label, and a ``raw_text`` table cell are display text: a
    token there stays literal. Inline code (``style.code``) and preformatted
    elements render literally too, whatever they hold.

    Walks with an explicit stack rather than recursion so a deeply nested
    payload cannot raise ``RecursionError``; the result does not depend on
    visit order (any mention wins, otherwise any literal).
    """
    literal = False
    stack: list[tuple[Any, bool]] = [(block, False) for block in blocks]
    while stack:
        value, in_code = stack.pop()
        if isinstance(value, list):
            stack.extend((item, in_code) for item in value)
            continue
        if not isinstance(value, dict):
            continue

        style = value.get("style")
        is_code = (
            in_code
            or value.get("type") == "rich_text_preformatted"
            or (isinstance(style, dict) and style.get("code") is True)
        )

        user_id = value.get("user_id")
        text = value.get("text")
        if value.get("type") == "user" and isinstance(user_id, str) and user_id.upper() == matcher.user_id:
            if not is_code:
                return "mention"
            literal = True
        elif isinstance(text, str) and matcher.search(text):
            if not is_code and value.get("type") == "mrkdwn" and matcher.search(mask_code_spans(text)):
                return "mention"
            literal = True

        for key in ("elements", "rows", "fields", "text"):
            child = value.get(key)
            if isinstance(child, (list, dict)):
                stack.append((child, is_code))

    return "literal" if literal else "none"


@dataclass(frozen=True)
class _AttachmentPart:
    """One piece of legacy attachment text (upstream ``SlackAttachmentPart``).

    ``mrkdwn`` parts go through the format converter (formatting characters
    are markup); literal parts render as plain text where only Slack control
    sequences (``<@U…>``, ``<url|label>``, entity escapes) are honored, so
    literal ``*``/``_``/backticks survive.
    """

    text: str
    mrkdwn: bool


@dataclass(frozen=True)
class _TableData:
    """A table extracted from a Slack table block, one mrkdwn string per cell.

    ``headerless`` is true when the source rows carry no header styling. GFM
    tables always render their first row as a header, so headerless tables
    get an empty header row prepended instead of promoting the first data
    row. Upstream ``SlackTableData``.
    """

    headerless: bool
    rows: list[list[str]]


@dataclass(frozen=True)
class _EventTables:
    """The message's own tables, split by position (upstream ``SlackEventTables``).

    ``leading`` tables appear before any other block and render above the
    text; ``trailing`` holds all the rest and renders below it.
    """

    leading: list[_TableData]
    trailing: list[_TableData]


@dataclass(frozen=True)
class _AttachmentContent:
    """Renderable content of one attachment (upstream ``SlackAttachmentContent``).

    ``blocks`` is the structured content (when present, Slack renders only
    the blocks), ``parts`` the legacy text parts and ``tables`` the tables
    extracted from the blocks.
    """

    blocks: list[Any]
    parts: list[_AttachmentPart]
    tables: list[_TableData] = field(default_factory=list)


@dataclass(frozen=True)
class _MentionNames:
    """Resolved display names for mention tokens (upstream ``SlackMentionNames``)."""

    users: dict[str, str] = field(default_factory=dict)
    channels: dict[str, str] = field(default_factory=dict)


_TABLE_BLOCK_TYPES = frozenset({"table", "data_table"})
_HTTP_URL_PREFIX_PATTERN = re.compile(r"^https?://")


def _str(value: Any) -> str | None:
    """``value`` when it is a string (upstream ``str``)."""
    return value if isinstance(value, str) else None


def _block_text_leaf(value: Any) -> str | list[Any]:
    """Render one table-cell element, or return the child list to flatten.

    Returns the element's mrkdwn string, or its ``elements`` list when the
    element is a container whose text is the join of its children (see
    :func:`_block_text`). Mirrors upstream ``blocktext`` case by case.
    """
    if not (isinstance(value, dict) and isinstance(value.get("type"), str)):
        return ""

    value_type = value["type"]
    text = _str(value.get("text"))
    if value_type == "raw_text":
        # A raw_text cell is plain text: Slack reserves mentions and links for
        # rich_text cells, so its control characters render literally. Kept
        # aligned with ``_classify_blocks_mention``, which does not scan it.
        # Upstream parity (adapter-slack/src/index.ts:722-726, 7106-7124): only
        # ``&``/``<``/``>`` are escaped and the cell then goes through the
        # mrkdwn converter, so ``*``/``_``/``---`` in a raw cell still read as
        # formatting there too.
        return escape_slack_text(text or "")
    if value_type == "link":
        url = _str(value.get("url"))
        if not url:
            return text or ""
        return f"<{url}|{text}>" if text else f"<{url}>"
    if value_type == "emoji":
        name = _str(value.get("name"))
        return f":{name}:" if name else ""
    if value_type == "user":
        user_id = _str(value.get("user_id"))
        return f"<@{user_id}>" if user_id else ""
    if value_type == "broadcast":
        broadcast_range = _str(value.get("range"))
        return f"@{broadcast_range}" if broadcast_range else ""
    if value_type == "channel":
        channel_id = _str(value.get("channel_id"))
        return f"<#{channel_id}>" if channel_id else ""
    if value_type == "usergroup":
        usergroup_id = _str(value.get("usergroup_id"))
        return f"<!subteam^{usergroup_id}>" if usergroup_id else ""
    if value_type == "date":
        fallback = _str(value.get("fallback"))
        if fallback:
            return fallback
        # Format the timestamp rather than leaking the raw format template
        # (e.g. "{date_num}") when Slack omits the fallback. UTC, like JS
        # ``toISOString()``; never a naive local date.
        timestamp = value.get("timestamp")
        if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool):
            try:
                return datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat()
            except (OverflowError, OSError, ValueError):
                # Outside ``datetime``'s range (upstream ``toISOString`` throws).
                return ""
        return ""
    if value_type == "color":
        return _str(value.get("value")) or ""
    if value_type == "team":
        return _str(value.get("team_id")) or ""

    if text is not None:
        return text
    for key in ("label", "url", "file_id"):
        fallback = _str(value.get(key))
        if fallback is not None:
            return fallback
    # Raw value cells (raw_number, raw_date, raw_currency, raw_percent,
    # raw_boolean, …) carry a primitive value when text is absent. ``0``,
    # ``False`` and ``""`` are values, so test the type, never truthiness;
    # numbers and booleans format like JS ``String()``.
    raw_value = value.get("value")
    if isinstance(raw_value, str):
        return raw_value
    if isinstance(raw_value, (int, float)):
        return _js_number_to_string(raw_value)
    elements = value.get("elements")
    return elements if isinstance(elements, list) else ""


def _block_text(value: Any) -> str:
    """Flatten a table cell (or any rich text element in one) to mrkdwn.

    Port of upstream ``blocktext``. Mentions, channels and links are emitted
    as mrkdwn tokens so the format converter renders them the same way it
    renders body text. Children of ``rich_text`` / ``rich_text_list`` join
    with ``\\n``; every other container's children join directly.

    Walks with an explicit stack instead of recursing, so a deeply nested
    cell cannot raise ``RecursionError`` and drop the whole message (the
    same choice ``_classify_blocks_mention`` makes).
    """
    # One frame per open container: its separator and rendered children.
    frames: list[tuple[str, list[str]]] = [("", [])]
    work: list[tuple[bool, Any]] = [(False, value)]
    while work:
        closing, item = work.pop()
        if closing:
            separator, parts = frames.pop()
            frames[-1][1].append(separator.join(parts))
            continue
        rendered = _block_text_leaf(item)
        if isinstance(rendered, str):
            frames[-1][1].append(rendered)
            continue
        frames.append(("\n" if item["type"] in ("rich_text", "rich_text_list") else "", []))
        work.append((True, None))
        work.extend((False, child) for child in reversed(rendered))
    return "".join(frames[0][1])


def _has_bold_text(value: Any) -> bool:
    """Whether any element in *value* is styled bold (upstream ``hasBoldText``)."""
    stack = [value]
    while stack:
        item = stack.pop()
        if not isinstance(item, dict):
            continue
        style = item.get("style")
        if isinstance(style, dict) and style.get("bold") is True:
            return True
        elements = item.get("elements")
        if isinstance(elements, list):
            stack.extend(elements)
    return False


def _table_data(block: Any) -> _TableData | None:
    """Parse a ``table`` / ``data_table`` block (upstream ``tableData``)."""
    if not (isinstance(block, dict) and block.get("type") in _TABLE_BLOCK_TYPES):
        return None
    rows = block.get("rows")
    if not isinstance(rows, list):
        return None

    source_rows = [row for row in rows if isinstance(row, list) and row]
    if not source_rows:
        return None

    # data_table rows always start with a header row (the outbound renderer
    # relies on this); pasted ``table`` blocks mark headers only via bold
    # cell styling.
    headerless = block["type"] == "table" and not any(_has_bold_text(cell) for cell in source_rows[0])
    return _TableData(headerless=headerless, rows=[[_block_text(cell) for cell in row] for row in source_rows])


def _tables_in(blocks: list[Any]) -> list[_TableData]:
    """The tables parsed from *blocks*, in order, skipping malformed ones."""
    return [data for data in (_table_data(block) for block in blocks) if data is not None]


def _event_tables(event: dict[str, Any]) -> _EventTables:
    """Collect table blocks from the message's own blocks, split by position.

    Slack flattens all rich text into ``event["text"]``, so exact
    interleaving can't be reconstructed; tables pasted above the text at
    least stay above it. Attachment tables are handled per attachment (see
    :func:`_attachment_content`) so they stay next to their text.
    """
    raw_blocks = event.get("blocks")
    blocks = raw_blocks if isinstance(raw_blocks, list) else []
    split_idx = next(
        (
            idx
            for idx, block in enumerate(blocks)
            if not (isinstance(block, dict) and block.get("type") in _TABLE_BLOCK_TYPES)
        ),
        len(blocks),
    )
    return _EventTables(leading=_tables_in(blocks[:split_idx]), trailing=_tables_in(blocks[split_idx:]))


def _is_foreign_attachment(attachment: dict[str, Any]) -> bool:
    """Unfurls carry content that is not the message author's."""
    return bool(
        attachment.get("is_msg_unfurl")
        or attachment.get("is_app_unfurl")
        or attachment.get("from_url")
        or attachment.get("original_url")
    )


def _author_attachments(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Attachments authored by the message sender, in order.

    Unfurls are excluded everywhere author attachments are read -- content,
    tables, mention classification and links alike -- because their content
    is not the author's.
    """
    attachments = event.get("attachments")
    if not isinstance(attachments, list):
        return []
    return [a for a in attachments if isinstance(a, dict) and not _is_foreign_attachment(a)]


def _attachment_content(attachment: dict[str, Any]) -> _AttachmentContent:
    """Content of a legacy attachment (upstream ``attachmentContent``).

    Alerting integrations (Sentry, PagerDuty, GitHub) put the real payload in
    ``pretext``/``title``/``text``/``fields``. Slack renders those as plain
    text unless they are named in ``mrkdwn_in``; ``title`` is always plain
    text and links to ``title_link``.

    When the attachment carries table blocks, Slack renders only the blocks
    and ignores the legacy fields, so no parts are built. Block types that
    can't be rendered fall back to the legacy fields, and ``fallback`` fills
    in last, only when nothing else renders.
    """
    raw_blocks = attachment.get("blocks")
    blocks = raw_blocks if isinstance(raw_blocks, list) else []
    tables = _tables_in(blocks)
    parts: list[_AttachmentPart] = []
    if tables:
        return _AttachmentContent(blocks=blocks, parts=parts, tables=tables)

    raw_mrkdwn_in = attachment.get("mrkdwn_in")
    # Only string entries name fields (``set()`` of a dict entry would raise).
    mrkdwn_in = {name for name in raw_mrkdwn_in if isinstance(name, str)} if isinstance(raw_mrkdwn_in, list) else set()

    def push(value: Any, mrkdwn: bool) -> None:
        trimmed = value.strip(JS_WHITESPACE) if isinstance(value, str) else ""
        if trimmed:
            parts.append(_AttachmentPart(text=trimmed, mrkdwn=mrkdwn))

    push(attachment.get("pretext"), "pretext" in mrkdwn_in)
    raw_title = attachment.get("title")
    title = raw_title.strip(JS_WHITESPACE) if isinstance(raw_title, str) else ""
    title_link = attachment.get("title_link")
    if isinstance(title_link, str) and title_link:
        # Fold the link in as a control sequence so the title renders as a
        # link node and the URL survives into the normalized message.
        push(f"<{title_link}|{escape_slack_text(title)}>" if title else f"<{title_link}>", False)
    else:
        push(title, False)
    push(attachment.get("text"), "text" in mrkdwn_in)
    fields = attachment.get("fields")
    for entry in fields if isinstance(fields, list) else []:
        if not isinstance(entry, dict):
            continue
        raw_field_title = entry.get("title")
        raw_value = entry.get("value")
        field_title = raw_field_title.strip(JS_WHITESPACE) if isinstance(raw_field_title, str) else ""
        value = raw_value.strip(JS_WHITESPACE) if isinstance(raw_value, str) else ""
        push(f"{field_title}: {value}" if field_title and value else field_title or value, "fields" in mrkdwn_in)

    if not parts:
        push(attachment.get("fallback"), False)
    return _AttachmentContent(blocks=blocks, parts=parts, tables=tables)


def _collect_mention_ids(text: str, user_ids: set[str], channel_ids: set[str]) -> None:
    """Collect the user and channel ids referenced by ``<@U…>`` / ``<#C…>`` tokens.

    Upstream ``collectMentionIds``. Parses by splitting on ``<`` (no regex
    over user text, so no ReDoS).
    """
    for segment in text.split("<"):
        end = segment.find(">")
        if end == -1:
            continue
        inner = segment[:end]
        if inner.startswith("@"):
            rest = inner[1:]
            pipe_idx = rest.find("|")
            uid = rest[:pipe_idx] if pipe_idx >= 0 else rest
            if SLACK_USER_ID_PATTERN.fullmatch(uid):
                user_ids.add(uid)
        elif inner.startswith("#"):
            rest = inner[1:]
            # Only collect bare channel ids (no label already present)
            if "|" not in rest and SLACK_USER_ID_PATTERN.fullmatch(rest):
                channel_ids.add(rest)


def _apply_mention_names(text: str, names: _MentionNames) -> str:
    """Replace ``<@U123>``, ``<@U123|old>`` and ``<#C123>`` with resolved names.

    Upstream ``applyMentionNames``. Tokens without a resolved name are left
    untouched. Scans with ``str.find`` (no regex over user text, so no ReDoS).
    Upstream re-slices the remaining text per token, which is quadratic with
    Python's copying slices; this walks indices instead, with the same output.
    """
    if not names.users and not names.channels:
        return text

    out: list[str] = []
    pos = 0
    next_at = text.find("<@")
    next_hash = text.find("<#")
    while True:
        if next_at != -1 and next_at < pos:
            next_at = text.find("<@", pos)
        if next_hash != -1 and next_hash < pos:
            next_hash = text.find("<#", pos)
        start = max(next_at, next_hash) if next_at == -1 or next_hash == -1 else min(next_at, next_hash)
        if start == -1:
            break
        end = text.find(">", start)
        if end == -1:
            break
        out.append(text[pos:start])
        prefix = text[start + 1]  # '@' or '#'
        inner = text[start + 2 : end]
        pipe_idx = inner.find("|")
        id_str = inner[:pipe_idx] if pipe_idx >= 0 else inner
        token = text[start : end + 1]
        if prefix == "@" and SLACK_USER_ID_PATTERN.fullmatch(id_str):
            name = names.users.get(id_str)
            out.append(f"<@{id_str}|{name}>" if name else token)
        elif prefix == "#" and pipe_idx == -1 and id_str in names.channels:
            out.append(f"<#{id_str}|{names.channels[id_str]}>")
        else:
            out.append(token)
        pos = end + 1
    out.append(text[pos:])
    return "".join(out)


def _content_mention_ids(
    tables: _EventTables,
    attachments: list[_AttachmentContent],
) -> tuple[set[str], set[str]]:
    """``(user_ids, channel_ids)`` across table cells and attachment parts.

    Upstream ``mentionIds``.
    """
    user_ids: set[str] = set()
    channel_ids: set[str] = set()
    all_tables = [*tables.leading, *tables.trailing]
    for attachment in attachments:
        all_tables.extend(attachment.tables)
        for part in attachment.parts:
            _collect_mention_ids(part.text, user_ids, channel_ids)
    for data in all_tables:
        for row in data.rows:
            for cell in row:
                _collect_mention_ids(cell, user_ids, channel_ids)
    return user_ids, channel_ids


def _literal_phrasing(line: str) -> list[Content]:
    """Render one line of plain-text Slack content to phrasing nodes.

    Upstream ``literalPhrasing``. Control sequences are honored the way the
    mrkdwn converter renders them (``<@U…|name>`` -> ``@name``,
    ``<url|label>`` -> link), but nothing is parsed as markdown, so
    formatting characters stay literal.
    """
    children: list[Content] = []
    plain: list[str] = []

    def flush_plain() -> None:
        value = "".join(plain)
        if value:
            children.append({"type": "text", "value": unescape_slack_text(value)})
        plain.clear()

    # Index walk rather than upstream's per-token re-slicing (quadratic with
    # Python's copying slices); same output.
    pos = 0
    while pos < len(line):
        start = line.find("<", pos)
        end = -1 if start == -1 else line.find(">", start + 1)
        if end == -1:
            plain.append(line[pos:])
            break
        plain.append(line[pos:start])
        token = line[start : end + 1]
        inner = line[start + 1 : end]
        pos = end + 1

        pipe_idx = inner.find("|")
        target = inner if pipe_idx == -1 else inner[:pipe_idx]
        label = None if pipe_idx == -1 else inner[pipe_idx + 1 :]
        id_str = target[1:]
        if target.startswith("@") and SLACK_USER_ID_PATTERN.fullmatch(id_str):
            plain.append(f"@{label if label is not None else id_str}")
        elif target.startswith("#") and SLACK_USER_ID_PATTERN.fullmatch(id_str):
            plain.append(f"#{label} ({id_str})" if label else f"#{id_str}")
        elif _HTTP_URL_PREFIX_PATTERN.match(target):
            flush_plain()
            link_text = unescape_slack_text(label) if label else target
            children.append({"type": "link", "url": target, "children": [{"type": "text", "value": link_text}]})
        else:
            plain.append(token)
    flush_plain()
    return children


def _normalize_bot_token_provider(
    value: SlackBotToken | None,
) -> SlackBotTokenResolver | None:
    """Normalize a ``bot_token`` config value to a zero-arg resolver.

    Mirrors upstream's ``normalizeBotTokenProvider``. A static string becomes
    a resolver that returns the same string; a callable is returned as-is so
    rotation or async lookup is preserved. ``None`` -> ``None`` (no token).
    """
    if value is None:
        return None
    if callable(value):
        # Already a resolver — keep the user's callable so rotation works.
        return value
    static = value

    def _provider() -> str:
        return static

    return _provider


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class SlackAdapter:
    """Slack adapter for chat-sdk.

    Implements the Adapter interface for the Slack Web API.
    Supports both single-workspace (static bot token) and multi-workspace
    (per-team OAuth token lookup) modes.
    """

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(self, config: SlackAdapterConfig | None = None) -> None:
        # ContextVar replaces Node AsyncLocalStorage for per-request token context.
        # Created per-instance so multiple SlackAdapter instances don't share state.
        self._request_context: ContextVar[RequestContext | None] = ContextVar(
            f"slack_request_context_{id(self)}", default=None
        )
        # Per-request cache of the resolved default bot token. Populated at the
        # top of ``handle_webhook`` (after the signature/verifier check) and
        # read by the sync ``_get_token`` path. Each concurrent request gets
        # its own contextvar copy so dynamic resolvers returning different
        # values per call cannot bleed between in-flight requests.
        self._resolved_default_token: ContextVar[str | None] = ContextVar(
            f"slack_resolved_default_token_{id(self)}", default=None
        )
        if config is None:
            config = SlackAdapterConfig()

        mode = config.mode or "webhook"

        # ``webhook_verifier`` takes precedence over ``signing_secret`` (config)
        # and the ``SLACK_SIGNING_SECRET`` env var. When the caller wires up a
        # verifier we ignore both so an env-configured deployment can't silently
        # shadow it (mirrors upstream vercel/chat#468, which reversed the
        # original direction the Python port shipped in PR #87).
        #
        # Empty-string ``signing_secret`` is rejected outright below;
        # empty ``SLACK_SIGNING_SECRET`` env values are normalized to ``None``
        # so they can't masquerade as a configured secret.
        webhook_verifier = config.webhook_verifier
        # Reject an explicit empty-string ``signing_secret`` at construction —
        # even when a ``webhook_verifier`` is set. An explicit ``""`` is a
        # config typo (e.g. an unset env var interpolated into the field), and
        # silently normalizing it to ``None`` would flip the adapter from the
        # built-in HMAC check to the custom verifier *without the caller's
        # knowledge*. Fail fast here so the typo surfaces at init rather than
        # silently altering which verification path runs in production. (An
        # unset/``None`` signing_secret still legitimately defers to the
        # verifier or the env fallback below — only the explicit ``""`` is a
        # hard error.)
        if config.signing_secret == "":
            raise ValidationError(
                "slack",
                "signing_secret must be a non-empty string when provided.",
            )
        # Reject a non-callable ``webhook_verifier`` at construction. A typo
        # such as ``webhook_verifier=""`` / ``False`` / ``123`` passes the
        # ``is not None`` guard below, then ``handle_webhook`` tries to *call*
        # it, the resulting ``TypeError`` is caught and reported as an invalid
        # signature, and every webhook fails closed with 401 — an opaque
        # production outage from a one-character mistake. ``None`` (unset) is
        # fine; anything else must be callable.
        if webhook_verifier is not None and not callable(webhook_verifier):
            raise ValidationError(
                "slack",
                "webhook_verifier must be callable.",
            )
        if webhook_verifier is not None:
            # Verifier wins: drop both the config ``signing_secret`` and the
            # ``SLACK_SIGNING_SECRET`` env fallback. Mirrors upstream
            # vercel/chat#468 (``webhookVerifier`` ?? undefined : (...)).
            signing_secret: str | None = None
        else:
            signing_secret = (
                config.signing_secret if config.signing_secret is not None else os.environ.get("SLACK_SIGNING_SECRET")
            )
            if signing_secret == "":
                signing_secret = None
        # ``signing_secret`` is required in webhook mode (unless a
        # ``webhook_verifier`` replaces the built-in HMAC check). Socket mode
        # legitimately runs without a signing secret because Slack does not
        # sign events delivered over the WebSocket — only HTTP-forwarded events
        # need a separate ``socket_forwarding_secret`` bearer check.
        if mode == "webhook" and signing_secret is None and webhook_verifier is None:
            raise ValidationError(
                "slack",
                "signingSecret or webhookVerifier is required for webhook mode. Set "
                "SLACK_SIGNING_SECRET, provide a non-empty signing_secret in config, "
                "or provide a webhook_verifier.",
            )

        # Auth fields: botToken presence selects single-workspace mode.
        # Explicit ``is not None`` to mirror the truthiness-trap rule above:
        # an explicit empty string for any of these should still count as
        # "config provided" and disable the env-fallback path. ``app_token``
        # also participates (so socket-mode-only configs disable env fallbacks
        # for the other secrets the same way bot-token-only configs do).
        zero_config = (
            config.signing_secret is None
            and config.bot_token is None
            and config.client_id is None
            and config.client_secret is None
            and config.app_token is None
        )

        bot_token_config: SlackBotToken | None = config.bot_token
        # Reject explicit empty-string ``bot_token`` at init for the same
        # reason ``signing_secret=""`` is rejected: it would prime
        # ``_default_bot_token_cache`` with ``""`` and the sync ``_get_token``
        # path would happily return it, producing ``Authorization: Bearer ``
        # API calls and opaque ``invalid_auth`` errors from Slack. (The async
        # resolver path catches this later, but failing fast at construction
        # is strictly better.) Callable resolvers may legitimately *return*
        # non-string values at resolve time — that case is already validated
        # in ``_resolve_default_token``.
        if isinstance(bot_token_config, str) and bot_token_config == "":
            raise ValidationError(
                "slack",
                "bot_token must be a non-empty string or a callable resolver; got an empty string.",
            )
        if bot_token_config is None and zero_config:
            env_token = os.environ.get("SLACK_BOT_TOKEN")
            # Same empty-string-as-missing rule as ``signing_secret``: an empty
            # SLACK_BOT_TOKEN would cache ``""`` in
            # ``_default_bot_token_cache`` and ``_get_token`` would happily
            # return it, producing opaque "invalid_auth" errors from Slack on
            # every API call. Treat ``""`` as unset so the adapter falls
            # through to multi-workspace mode (or fails clearly later).
            if env_token is not None and env_token != "":
                bot_token_config = env_token

        bot_token_provider = _normalize_bot_token_provider(bot_token_config)

        self._name = "slack"
        self._signing_secret: str | None = signing_secret
        # ``webhook_verifier`` takes precedence; ``signing_secret`` is only used
        # when no verifier is configured (matches upstream vercel/chat#468).
        self._webhook_verifier: SlackWebhookVerifier | None = webhook_verifier
        # Resolver returning the default (single-workspace) bot token. ``None`` in
        # multi-workspace mode where the token is resolved per-team from the
        # InstallationStore. Single-workspace mode with a static string still
        # uses a resolver under the hood (returns the same string each call).
        self._default_bot_token_provider: SlackBotTokenResolver | None = bot_token_provider
        # Last successfully resolved default token, kept for the sync ``current_token``
        # accessor and for any code path that needs a token outside an awaitable
        # context. Populated lazily on first await of the resolver and refreshed
        # on each subsequent webhook entry. Static-string configs prime this at
        # construction time so sync access works before any webhook fires.
        self._default_bot_token_cache: str | None = bot_token_config if isinstance(bot_token_config, str) else None
        # True when the user passed a callable ``bot_token`` (sync or async).
        # The config contract says callable resolvers are invoked on each use
        # to support rotation, so sync access goes through a fresh-invoke
        # branch instead of reading ``_default_bot_token_cache``. Static
        # strings have nothing to rotate and stay on the cached fast path.
        self._is_dynamic_bot_token: bool = callable(bot_token_config)

        # ------------------------------------------------------------------
        # Socket mode wiring (PR #86). Resolved AFTER the bot-token resolver
        # is set up so the eventual socket client can read the resolved token
        # via ``_get_token`` / ``current_token_async``. ``app_token`` is the
        # long-lived app-level secret used to open the WebSocket; the bot
        # token (potentially resolver-backed) is still used for API calls.
        # ------------------------------------------------------------------
        app_token = config.app_token if config.app_token is not None else os.environ.get("SLACK_APP_TOKEN")
        if app_token == "":
            app_token = None
        if mode == "socket":
            if not app_token:
                raise ValidationError(
                    "slack",
                    "appToken is required for socket mode. Set SLACK_APP_TOKEN or provide it in config.",
                )
            # Hazard #12: validate the long-lived secret format on init so a
            # typo'd bot token (xoxb-) doesn't get silently used as an app
            # token. Slack app-level tokens always start with ``xapp-``.
            if not app_token.startswith("xapp-"):
                raise ValidationError(
                    "slack",
                    "appToken must start with 'xapp-' (Slack app-level token). "
                    "Bot tokens (xoxb-) are not valid for socket mode.",
                )

        # Socket mode state
        self._mode: SlackAdapterMode = mode
        self._app_token: str | None = app_token
        self._socket_forwarding_secret: str | None = (
            config.socket_forwarding_secret or os.environ.get("SLACK_SOCKET_FORWARDING_SECRET") or app_token
        )
        # The active SocketModeClient instance (when running in socket mode).
        # Typed as ``Any`` because slack_sdk is an optional dependency.
        self._socket_client: Any = None
        # Background task that runs the connect/run/reconnect loop. Tracked so
        # ``disconnect()`` can cancel it cleanly (hazard #5).
        self._socket_task: asyncio.Task[None] | None = None
        # Set when shutdown is requested so the reconnect loop knows to exit
        # rather than retry on a clean disconnect. The Event also wakes up
        # ``_socket_sleep_with_backoff`` immediately so ``stop_socket_mode``
        # doesn't have to wait the full backoff window.
        self._socket_shutdown_event: asyncio.Event = asyncio.Event()
        # Default backoff schedule in seconds. Kept short so tests run fast,
        # but capped low enough that a flapping Slack connection doesn't busy
        # loop. Slack's recommended pattern is exponential backoff with jitter;
        # our minimal schedule mirrors that behavior with explicit caps.
        self._socket_initial_backoff_s = 1.0
        self._socket_max_backoff_s = 30.0
        # Bound the initial Socket Mode handshake so ``initialize()`` doesn't
        # block forever if slack_sdk's ``connect()`` hangs (hazard #11). The
        # config field is typed ``float`` with a 30s default, so this is just
        # a read.
        self._socket_connect_timeout_s: float = config.connect_timeout_s
        self._logger: Logger = config.logger or ConsoleLogger("info")
        self._user_name: str = config.user_name or "bot"
        self._bot_user_id: str | None = config.bot_user_id or None
        self._bot_id: str | None = None  # Bot app ID (B_xxx)
        self._chat: ChatInstance | None = None
        self._format_converter = SlackFormatConverter()
        self._lock_scope: LockScope = "thread"
        self._persist_message_history = False

        # Channel external/shared cache
        self._external_channels: set[str] = set()

        # Cache of AsyncWebClient instances keyed by bot token (LRU-bounded)
        self._client_cache: OrderedDict[str, Any] = OrderedDict()
        self._client_cache_max = config.client_cache_max if config.client_cache_max is not None else 100

        # Cache of synchronous slack_sdk.WebClient instances keyed by bot
        # token, backing the public ``web_client`` property (the direct port
        # of upstream's ``getClientForToken``). Kept separate from
        # ``_client_cache`` because that one holds async ``AsyncWebClient``
        # instances used by the adapter's own API calls; the two client types
        # are not interchangeable. Mirrors upstream's plain (unbounded) Map —
        # one entry per distinct token — since callers reach for this escape
        # hatch rarely and tokens are low-cardinality.
        self._web_client_cache: dict[str, Any] = {}

        # Multi-workspace OAuth fields.
        # ``is not None`` (not truthiness) so an explicit empty-string user
        # config does not silently fall back to env (hazard #1). Empty env
        # values are treated as "unset" (mirrors SLACK_BOT_TOKEN env rule):
        # an empty SLACK_CLIENT_ID would be useless downstream and produce
        # opaque OAuth failures rather than a clear "not configured" state.
        if config.client_id is not None:
            self._client_id: str | None = config.client_id
        elif zero_config:
            env_client_id = os.environ.get("SLACK_CLIENT_ID")
            self._client_id = env_client_id if env_client_id else None
        else:
            self._client_id = None
        if config.client_secret is not None:
            self._client_secret: str | None = config.client_secret
        elif zero_config:
            env_client_secret = os.environ.get("SLACK_CLIENT_SECRET")
            self._client_secret = env_client_secret if env_client_secret else None
        else:
            self._client_secret = None
        self._installation_key_prefix = config.installation_key_prefix or "slack:installation"
        # External installation provider (e.g. Vercel Connect). When set,
        # per-installation token lookups bypass internal StateAdapter storage.
        self._installation_provider: SlackInstallationProvider | None = config.installation_provider

        # ``is not None`` (not truthiness) so an explicit ``encryption_key=""``
        # is treated as "user explicitly opted out" and is NOT silently
        # shadowed by ``SLACK_ENCRYPTION_KEY`` from the env. Mirrors the
        # client_id / client_secret rule above (hazard #1). An empty user
        # config still short-circuits ``decode_key`` via the final
        # ``if encryption_key_raw`` guard, so no broken key is built.
        if config.encryption_key is not None:
            encryption_key_raw: str | None = config.encryption_key
        else:
            encryption_key_raw = os.environ.get("SLACK_ENCRYPTION_KEY")
        self._encryption_key: bytes | None = None
        if encryption_key_raw:
            self._encryption_key = decode_key(encryption_key_raw)

        # Custom Slack Web API base URL (e.g. proxy, mock, Enterprise routing).
        # ``config.apiUrl ?? process.env.SLACK_API_URL`` upstream (index.ts:617),
        # consumed via the truthy spread ``...(this.slackApiUrl ? {...} : {})``
        # at every client construction — so an empty string falls back to the
        # built-in default. We mirror that here: an empty ``apiUrl`` (or env)
        # resolves to ``None``. When ``None`` we omit ``base_url`` from the
        # client constructors entirely, so slack_sdk keeps its built-in
        # ``https://slack.com/api/`` default (it rejects ``base_url=None``).
        # Threaded into BOTH the async ``AsyncWebClient`` cache and the
        # synchronous ``web_client`` escape hatch.
        self._slack_api_url: str | None = (config.api_url or os.environ.get("SLACK_API_URL")) or None

        # Extra kwargs forwarded to every slack_sdk client (default async,
        # per-token async cache, and synchronous WebClient). Mirrors upstream
        # ``this.webClientOptions = config.webClientOptions`` (vercel/chat
        # 8336a3e). Stored as-is (may be ``None``); spread is gated on
        # ``is not None`` so an explicit ``{}`` still spreads. See
        # ``_web_client_kwargs`` for the per-client deep-copy of ``headers``.
        self._web_client_options: dict[str, Any] | None = config.web_client_options
        # Egress plumbing (vercel/chat 6adca361): the guarded-download
        # transport (``_create_file_transport`` returns it unless a subclass
        # overrides) and the httpx client factory for ``response_url`` posts.
        self._file_transport: AttachmentTransport | None = config.file_transport
        self._http_client_factory: Callable[[], Any] | None = config.http_client_factory

    def _web_client_kwargs(self) -> dict[str, Any]:
        """Return a fresh copy of ``web_client_options`` for one client.

        Spreads the configured options (gated on ``is not None`` so an empty
        ``{}`` still applies) and **deep-copies any nested ``headers`` dict**,
        so cached per-token clients never share a mutable ``headers`` object
        and the caller's input dict is never mutated. Mirrors upstream's
        per-client ``{ ...headers ? { headers: { ...headers } } : {} }`` spread.
        """
        if self._web_client_options is None:
            return {}
        kwargs = dict(self._web_client_options)
        headers = self._web_client_options.get("headers")
        if headers is not None:
            kwargs["headers"] = dict(headers)
        return kwargs

    # ------------------------------------------------------------------
    # Properties (Adapter protocol)
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return self._name

    @property
    def user_name(self) -> str:
        return self._user_name

    @property
    def bot_user_id(self) -> str | None:
        ctx = self._request_context.get()
        if ctx and ctx.bot_user_id:
            return ctx.bot_user_id
        return self._bot_user_id

    @property
    def lock_scope(self) -> LockScope:
        return self._lock_scope

    @property
    def persist_message_history(self) -> bool:
        return self._persist_message_history

    @property
    def mode(self) -> SlackAdapterMode:
        """Connection mode (``"webhook"`` or ``"socket"``)."""
        return self._mode

    @property
    def is_socket_mode(self) -> bool:
        """``True`` when the adapter is configured for Socket Mode."""
        return self._mode == "socket"

    # ------------------------------------------------------------------
    # Public request-context accessors
    #
    # These are Python-only extensions to the Adapter surface. They let
    # code running inside a handler call the Slack Web API directly —
    # e.g. ``users.info`` for caller-email resolution — without
    # reaching into the underscore-prefixed ``_get_token`` /
    # ``_get_client`` helpers. See docs/UPSTREAM_SYNC.md.
    # ------------------------------------------------------------------

    @property
    def current_token(self) -> str:
        """Return the bot token bound to the current request context.

        In multi-workspace mode this is the token resolved by the
        ``InstallationStore`` for the current request; in single-workspace
        mode it is the default bot token (or the most recently resolved
        token when a dynamic ``bot_token`` resolver is configured).

        Synchronous: when ``bot_token`` is a resolver and this property is
        accessed before the resolver has run for the first time, an
        :class:`AuthenticationError` is raised. Use
        :meth:`current_token_async` from async contexts to invoke the
        resolver on demand.
        """
        return self._get_token()

    async def current_token_async(self) -> str:
        """Async variant of :attr:`current_token` that invokes the resolver.

        Prefer this over :attr:`current_token` in async code paths when a
        dynamic ``bot_token`` resolver is configured — it ensures the
        resolver is awaited rather than relying on the cached value.
        """
        return await self._resolve_token_async()

    @property
    def current_client(self) -> Any:
        """Return an ``AsyncWebClient`` preconfigured with :attr:`current_token`.

        Return type is ``Any`` (rather than the concrete
        ``AsyncWebClient``) because ``slack_sdk`` is an optional
        dependency — consumers who install the SDK without the `slack`
        extra shouldn't pay a type-check-time import cost. Docstring
        captures the actual runtime type for tooling that reads it.

        The returned client is LRU-cached by token. Raises
        :class:`AuthenticationError` when no token is available.
        """
        return self._get_client()

    @property
    def web_client(self) -> Any:
        """Direct access to a synchronous ``slack_sdk.WebClient``.

        Bound to the bot token for the current request context
        (multi-workspace) or the configured default token
        (single-workspace). Use for any Slack Web API call not covered by
        the adapter's high-level methods — e.g.
        ``adapter.web_client.pins_add(...)`` or
        ``adapter.web_client.usergroups_list(...)``.

        Resolution order (the standard 3-level resolver):

        1. Token from the current request context (set during webhook
           handling, or by :meth:`with_bot_token` / :meth:`with_bot_token_async`).
        2. The default bot token, when configured as a static string or
           already-resolved value.
        3. Otherwise raise :class:`AuthenticationError`.

        Raises :class:`AuthenticationError` if neither is available —
        typical causes are accessing ``web_client`` outside any
        webhook / :meth:`with_bot_token` context in multi-workspace mode,
        or having configured ``bot_token`` as an async resolver that has
        not run yet. In the latter case await
        :meth:`current_token_async` (or process the work inside the
        webhook flow) so the resolver primes the token first.

        Return type is ``Any`` (rather than the concrete ``WebClient``)
        because ``slack_sdk`` is an optional dependency — consumers who do
        not install the ``slack`` extra should not pay an import cost.

        This is the direct port of upstream's ``adapter.webClient`` getter
        (vercel/chat ``2f108bd``). Unlike :attr:`current_client` it returns
        the *synchronous* ``WebClient`` (the analog of the single TS
        ``WebClient``), so its methods are not awaitables.
        """
        return self._get_web_client_for_token(self._get_token())

    @property
    def client(self) -> Any:
        """Deprecated alias for :attr:`web_client`.

        .. deprecated::
            Use :attr:`web_client` instead. This alias mirrors upstream's
            pre-rename ``adapter.client`` (vercel/chat ``8366b8b``) and is
            kept for one release for backwards compatibility; it will be
            removed in a future version. Emits :class:`DeprecationWarning`.
        """
        warnings.warn(
            "SlackAdapter.client is deprecated; use SlackAdapter.web_client instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.web_client

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    def _get_token(self) -> str:
        """Return the current bot token for API calls (sync path).

        Checks (in order):

        1. Multi-workspace request context (``_request_context``)
        2. Per-request resolved default token (``_resolved_default_token``)
           — primed by ``handle_webhook`` after invoking the resolver
        3. Sync dynamic resolver — invoked **fresh on every call** to honor
           the rotation contract in :attr:`SlackAdapterConfig.bot_token`
           ("called on each use to support rotation")
        4. Static default token cache (set by the constructor for
           string-typed ``bot_token`` configs, or by the async path after
           ``_resolve_default_token`` runs)
        5. Raises :class:`AuthenticationError`

        Async resolvers cannot be awaited from a sync context — sync access
        outside a webhook scope falls back to the process-wide cache, or
        raises if the async path has not run yet. Use
        :meth:`current_token_async` or enter via :meth:`handle_webhook` so
        the resolver runs first.
        """
        ctx = self._request_context.get()
        if ctx and ctx.token:
            return ctx.token
        per_request = self._resolved_default_token.get()
        if per_request is not None:
            return per_request
        # Sync dynamic resolver: invoke fresh every call to honor rotation.
        # Static strings (no rotation possible) and async resolvers (which
        # need a webhook entry to be awaited) fall through to the cache.
        provider = self._default_bot_token_provider
        if self._is_dynamic_bot_token and provider is not None and not inspect.iscoroutinefunction(provider):
            resolved = provider()
            # Defensive: a "sync" callable may still *return* a coroutine
            # (e.g. ``lambda: some_async_fn()``) and ``iscoroutinefunction``
            # would not catch that. Refuse to use such a value in a sync
            # context — and close the awaitable to suppress the
            # ``coroutine was never awaited`` RuntimeWarning before raising.
            if inspect.isawaitable(resolved):
                close = getattr(resolved, "close", None)
                if callable(close):
                    close()
                raise AuthenticationError(
                    "slack",
                    "Bot token resolver returned an awaitable in a sync "
                    "context. Use the async API (handle_webhook / "
                    "current_token_async) so the resolver can be awaited.",
                )
            if not isinstance(resolved, str) or not resolved:
                raise AuthenticationError(
                    "slack",
                    "Bot token resolver returned an empty or non-string value.",
                )
            # Intentionally do NOT write ``_default_bot_token_cache``: caching
            # would break the rotation contract for the next sync access.
            return resolved
        if self._default_bot_token_cache is not None:
            return self._default_bot_token_cache
        if provider is not None:
            # Async resolver configured but never awaited. ``handle_webhook``
            # or ``current_token_async`` must run first to prime the cache.
            raise AuthenticationError(
                "slack",
                "Async bot token resolver has not been invoked yet. Use the "
                "async API (handle_webhook / current_token_async) so the "
                "resolver runs first.",
            )
        raise AuthenticationError(
            "slack",
            "No bot token available. In multi-workspace mode, ensure the webhook is being processed.",
        )

    async def _resolve_token_async(self) -> str:
        """Async equivalent of :meth:`_get_token` that invokes the resolver.

        Calls the configured ``bot_token`` resolver (if any), refreshes the
        per-request cache used by :meth:`_get_token`, and returns the
        resulting token. Multi-workspace request context still wins.
        """
        ctx = self._request_context.get()
        if ctx and ctx.token:
            return ctx.token
        if self._default_bot_token_provider is not None:
            return await self._resolve_default_token()
        if self._default_bot_token_cache is not None:
            return self._default_bot_token_cache
        raise AuthenticationError(
            "slack",
            "No bot token available. In multi-workspace mode, ensure the webhook is being processed.",
        )

    async def _resolve_default_token(self) -> str:
        """Invoke the ``bot_token`` resolver and prime the per-request cache.

        Per upstream, the resolver is called *every time a token is needed*
        (it is the resolver's responsibility to memoize if desired) — this
        method does not memoize across calls within a process. The result is
        stashed in the per-request :attr:`_resolved_default_token` ContextVar
        so the sync ``_get_token`` path inside that request sees the value
        without re-invoking the resolver, while concurrent requests with
        their own ContextVar copies are isolated from each other.

        For static-string ``bot_token`` configs the resolver returns the
        same string each call, but we still update the per-request cache to
        keep the code path uniform.

        Raises :class:`AuthenticationError` if no resolver is configured.
        """
        provider = self._default_bot_token_provider
        if provider is None:
            raise AuthenticationError(
                "slack",
                "No default bot token resolver configured (multi-workspace mode).",
            )
        try:
            result = provider()
            if inspect.isawaitable(result):
                token = await result
            else:
                token = result
        except Exception as exc:
            self._logger.error("Bot token resolver raised", {"error": exc})
            raise
        if not isinstance(token, str) or not token:
            raise AuthenticationError(
                "slack",
                "Bot token resolver returned an empty or non-string value.",
            )
        # Refresh the process-wide cache so sync ``current_token`` /
        # ``current_client`` access outside the request ContextVar scope
        # (e.g. callers reading the most recently resolved token from a
        # different task) still observes the freshly resolved value.
        self._default_bot_token_cache = token
        # Per-request cache for downstream sync ``_get_token`` calls.
        self._resolved_default_token.set(token)
        return token

    @property
    def _is_single_workspace(self) -> bool:
        """``True`` when a default bot token (static or resolver) is configured.

        Multi-workspace mode is the absence of a default token, in which
        case tokens are resolved per-team via the InstallationStore.
        """
        return self._default_bot_token_provider is not None

    def _get_client(self, token: str | None = None) -> Any:
        """Return an ``AsyncWebClient`` for the given (or current) token.

        Clients are cached by token so we avoid creating a new instance on
        every request.  The import is deferred so that ``slack_sdk`` is only
        required at call-time.

        When *token* is explicitly passed (even as ``""``) it is used as-is;
        only when *token* is ``None`` do we fall back to ``_get_token()``.
        """
        resolved_token = self._get_token() if token is None else token

        if resolved_token in self._client_cache:
            self._client_cache.move_to_end(resolved_token)
            return self._client_cache[resolved_token]

        from slack_sdk.web.async_client import AsyncWebClient

        # Only pass ``base_url`` when a truthy override is configured — slack_sdk
        # rejects ``base_url=None`` (it requires a string), and an empty string
        # must fall back to the built-in default, so mirroring upstream's truthy
        # spread keeps the default otherwise.
        client_kwargs: dict[str, Any] = {**self._web_client_kwargs(), "token": resolved_token}
        if self._slack_api_url:
            client_kwargs["base_url"] = self._slack_api_url
        client = AsyncWebClient(**client_kwargs)
        self._client_cache[resolved_token] = client
        if len(self._client_cache) > self._client_cache_max:
            # Evict oldest (LRU).  We intentionally do NOT close the evicted
            # client's session here because other concurrent requests may still
            # hold a reference to the evicted AsyncWebClient instance.  The
            # underlying aiohttp.ClientSession will be closed by the garbage
            # collector (via __del__) once all references are released.
            self._client_cache.popitem(last=False)
        return client

    def _invalidate_client(self, token: str) -> None:
        """Remove a cached client (e.g., on token revocation).

        For dynamic-resolver configs also clears the resolved-token caches
        so the next access re-invokes the resolver instead of serving the
        revoked token. Static-string configs intentionally retain their
        cache: there is no refresh path, so clearing would just make every
        subsequent sync access raise.
        """
        self._client_cache.pop(token, None)
        self._web_client_cache.pop(token, None)
        if self._is_dynamic_bot_token:
            if self._default_bot_token_cache == token:
                self._default_bot_token_cache = None
            if self._resolved_default_token.get() == token:
                self._resolved_default_token.set(None)

    def _get_web_client_for_token(self, token: str) -> Any:
        """Return a synchronous ``slack_sdk.WebClient`` for *token*, cached.

        Backs the public :attr:`web_client` property and is the direct port
        of upstream's ``getClientForToken`` (vercel/chat ``2f108bd``): one
        cached ``WebClient`` instance per distinct token. The import is
        deferred so ``slack_sdk`` stays an optional dependency (hazard #10).

        Distinct from :meth:`_get_client`, which caches the *async*
        ``AsyncWebClient`` used by the adapter's own API calls.
        """
        client = self._web_client_cache.get(token)
        if client is None:
            from slack_sdk import WebClient

            # Same truthy ``base_url`` rule as ``_get_client`` — pass the
            # override only when truthy so slack_sdk keeps its default.
            web_client_kwargs: dict[str, Any] = {**self._web_client_kwargs(), "token": token}
            if self._slack_api_url:
                web_client_kwargs["base_url"] = self._slack_api_url
            client = WebClient(**web_client_kwargs)
            self._web_client_cache[token] = client
        return client

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    async def initialize(self, chat: ChatInstance) -> None:
        """Initialize the adapter and optionally fetch bot identity."""
        self._chat = chat

        # Single-workspace: fetch bot user ID via auth.test. We resolve the
        # bot token via the resolver here so dynamic resolvers work at init
        # time without forcing the caller to seed a static token first.
        if self._is_single_workspace and not self._bot_user_id:
            try:
                token = await self._resolve_default_token()
                client = self._get_client(token)
                auth_result = await client.auth_test()
                self._bot_user_id = auth_result.get("user_id")
                self._bot_id = auth_result.get("bot_id") or None
                user = auth_result.get("user")
                if user:
                    self._user_name = user
                self._logger.info(
                    "Slack auth completed",
                    {"botUserId": self._bot_user_id, "botId": self._bot_id},
                )
            except Exception as exc:
                self._logger.warn("Could not fetch bot user ID", {"error": exc})

        if not self._is_single_workspace:
            self._logger.info("Slack adapter initialized in multi-workspace mode")

        if self._mode == "socket":
            await self.start_socket_mode()

    async def disconnect(self) -> None:
        """Close any persistent connections held by the adapter.

        In webhook mode this is a no-op. In socket mode it cancels the
        background reconnect loop, closes the active ``SocketModeClient``,
        and waits for the loop to settle. Idempotent — calling it twice or
        before ``initialize()`` is safe.
        """
        await self.stop_socket_mode()

    # ==================================================================
    # Multi-workspace installation management
    # ==================================================================

    def _installation_key(self, team_id: str) -> str:
        return f"{self._installation_key_prefix}:{team_id}"

    async def set_installation(self, team_id: str, installation: SlackInstallation) -> None:
        """Save a workspace installation (call from your OAuth callback)."""
        if not self._chat:
            raise ValidationError(
                "slack",
                "Adapter not initialized. Ensure chat.initialize() has been called first.",
            )

        state = self._chat.get_state()
        key = self._installation_key(team_id)

        if self._encryption_key:
            encrypted = encrypt_token(installation.bot_token, self._encryption_key)
            data_to_store: dict[str, Any] = {
                "botToken": {
                    "iv": encrypted.iv,
                    "data": encrypted.data,
                    "tag": encrypted.tag,
                },
                "botUserId": installation.bot_user_id,
                "teamName": installation.team_name,
            }
        else:
            data_to_store = {
                "botToken": installation.bot_token,
                "botUserId": installation.bot_user_id,
                "teamName": installation.team_name,
            }

        await state.set(key, data_to_store)
        self._logger.info(
            "Slack installation saved",
            {"teamId": team_id, "teamName": installation.team_name},
        )

    async def get_installation(self, team_id: str) -> SlackInstallation | None:
        """Retrieve a workspace installation."""
        if not self._chat:
            raise ValidationError(
                "slack",
                "Adapter not initialized. Ensure chat.initialize() has been called first.",
            )

        state = self._chat.get_state()
        key = self._installation_key(team_id)
        stored = await state.get(key)

        if not stored:
            return None

        bot_token_raw = (stored.get("botToken") or stored.get("bot_token")) if isinstance(stored, dict) else None
        bot_user_id = (stored.get("botUserId") or stored.get("bot_user_id") or "") if isinstance(stored, dict) else ""
        team_name = (stored.get("teamName") or stored.get("team_name") or "") if isinstance(stored, dict) else ""
        if self._encryption_key and is_encrypted_token_data(bot_token_raw):
            # `is_encrypted_token_data` is a runtime type guard but doesn't
            # carry TypeGuard narrowing, so pyrefly still sees `None`. Assert
            # to collapse the Optional for the field access below.
            assert bot_token_raw is not None
            decrypted = decrypt_token(
                EncryptedTokenData(
                    iv=bot_token_raw["iv"],
                    data=bot_token_raw["data"],
                    tag=bot_token_raw["tag"],
                ),
                self._encryption_key,
            )
            return SlackInstallation(
                bot_token=decrypted,
                bot_user_id=bot_user_id,
                team_name=team_name,
            )

        return SlackInstallation(
            bot_token=bot_token_raw if isinstance(bot_token_raw, str) else "",
            bot_user_id=bot_user_id,
            team_name=team_name,
        )

    async def handle_oauth_callback(
        self,
        request: Any,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Handle the Slack OAuth V2 callback.

        Args:
            request: The incoming HTTP request containing the OAuth callback.
            options: Optional dict with ``redirect_uri`` key to send to Slack
                during the code exchange. When provided it takes priority over
                any ``redirect_uri`` query parameter in the callback URL.

        Returns ``{"team_id": ..., "installation": SlackInstallation}``.
        """
        if not (self._client_id and self._client_secret):
            raise ValidationError(
                "slack",
                "client_id and client_secret are required for OAuth. Pass them in create_slack_adapter().",
            )

        # Extract query params from request
        url: str = getattr(request, "url", "")
        if isinstance(url, str) and "?" in url:
            query = dict(parse_qs(url.split("?", 1)[1]))
            code = query.get("code", [None])[0] if isinstance(query.get("code"), list) else query.get("code")
            query_redirect_uri = (
                query.get("redirect_uri", [None])[0]
                if isinstance(query.get("redirect_uri"), list)
                else query.get("redirect_uri")
            )
        else:
            code = None
            query_redirect_uri = None

        if not code:
            raise ValidationError(
                "slack",
                "Missing 'code' query parameter in OAuth callback request.",
            )

        # Options redirect_uri takes priority over the query param
        redirect_uri = (options or {}).get("redirect_uri") or query_redirect_uri

        client = self._get_client("")
        kwargs: dict[str, Any] = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "code": code,
        }
        if redirect_uri:
            kwargs["redirect_uri"] = redirect_uri
        result = await client.oauth_v2_access(**kwargs)

        if not (result.get("ok") and result.get("access_token") and result.get("team", {}).get("id")):
            raise AuthenticationError(
                "slack",
                f"Slack OAuth failed: {result.get('error') or 'missing access_token or team.id'}",
            )

        team_id = result["team"]["id"]
        installation = SlackInstallation(
            bot_token=result["access_token"],
            bot_user_id=result.get("bot_user_id"),
            team_name=result.get("team", {}).get("name"),
        )

        await self.set_installation(team_id, installation)
        return {"team_id": team_id, "installation": installation}

    async def delete_installation(self, team_id: str) -> None:
        """Remove a workspace installation."""
        if not self._chat:
            raise ValidationError(
                "slack",
                "Adapter not initialized. Ensure chat.initialize() has been called first.",
            )
        state = self._chat.get_state()
        await state.delete(self._installation_key(team_id))
        self._logger.info("Slack installation deleted", {"teamId": team_id})

    def with_bot_token(self, token: str, fn: Callable[[], Any]) -> Any:
        """Run *fn* with a specific bot token in context (for cron jobs, etc.)."""
        tok = self._request_context.set(RequestContext(token=token))
        try:
            return fn()
        finally:
            self._request_context.reset(tok)

    async def with_bot_token_async(self, token: str, fn: Callable[[], Awaitable[Any]]) -> Any:
        """Run an async function with a specific bot token in context."""
        tok = self._request_context.set(RequestContext(token=token))
        try:
            return await fn()
        finally:
            self._request_context.reset(tok)

    # ==================================================================
    # Private helpers - token resolution
    # ==================================================================

    async def _resolve_token_for_team(
        self, installation_id: str, is_enterprise_install: bool = False
    ) -> RequestContext | None:
        """Resolve the bot token for an installation.

        Checks the external installation provider first (e.g. Vercel
        Connect); when no provider is configured, falls back to the
        internal state adapter.

        ``installation_id`` is the ``team_id`` -- or the ``enterprise_id``
        for Enterprise Grid org-wide installs (``is_enterprise_install``).
        """
        try:
            # Check external installation provider first (e.g. Vercel Connect)
            if self._installation_provider is not None:
                installation = await self._installation_provider.get_installation(
                    installation_id, is_enterprise_install
                )
                if installation:
                    return RequestContext(
                        token=installation.bot_token,
                        bot_user_id=installation.bot_user_id,
                    )
                self._logger.warn(
                    "No installation found from provider",
                    {"installationId": installation_id, "isEnterpriseInstall": is_enterprise_install},
                )
                return None
            # Fall back to internal state adapter
            installation = await self.get_installation(installation_id)
            if installation:
                return RequestContext(
                    token=installation.bot_token,
                    bot_user_id=installation.bot_user_id,
                )
            self._logger.warn(
                "No installation found for team",
                {"installationId": installation_id, "isEnterpriseInstall": is_enterprise_install},
            )
            return None
        except Exception as exc:
            self._logger.error(
                "Failed to resolve token for team",
                {"installationId": installation_id, "isEnterpriseInstall": is_enterprise_install, "error": exc},
            )
            return None

    def _extract_installation_from_interactive(self, body: str) -> _InstallationInfo | None:
        """Extract installation info from an interactive payload (form-urlencoded).

        For Enterprise Grid org-wide installs, the installation ID is the
        enterprise ID; otherwise it is the team ID.
        """
        try:
            params = parse_qs(body)
            payload_str = params.get("payload", [None])[0]
            if not payload_str:
                return None
            payload = json.loads(payload_str)
            is_enterprise_install = bool(payload.get("is_enterprise_install"))
            enterprise = payload.get("enterprise") or {}
            enterprise_id = enterprise.get("id") or payload.get("enterprise_id") or None
            team = payload.get("team") or {}
            team_id = team.get("id") or payload.get("team_id") or None
            installation_id = enterprise_id if is_enterprise_install else team_id

            if not installation_id:
                return None
            return _InstallationInfo(
                installation_id=installation_id,
                is_enterprise_install=is_enterprise_install,
                enterprise_id=enterprise_id,
            )
        except Exception:
            return None

    # ==================================================================
    # User / Channel lookup with caching
    # ==================================================================

    def _installation_cache_scope(self) -> str:
        """Scope prefix for installation-owned cache keys.

        Port of upstream ``installationCacheScope`` (vercel/chat#724, #877).
        In multi-workspace deployments user profiles, display-name indexes,
        channel names and unfurl metadata must not be shared across
        installations. Single-workspace mode (and code running outside a
        webhook context) uses the unscoped key.

        Read from the ContextVar at call time: event dispatch runs in a
        ``contextvars.copy_context()`` and tasks copy the context at
        creation, so work spawned from handlers inherits the installation.
        Only a truthy ``installation_id`` scopes the key (upstream
        ``installationId ? ... : ""``).
        """
        ctx = self._request_context.get()
        installation_id = ctx.installation_id if ctx is not None else None
        return f"{installation_id}:" if installation_id else ""

    def _unfurl_cache_key(self, channel_id: str, message_ts: str) -> str:
        """State key for unfurl metadata, scoped by installation and channel."""
        return f"slack:unfurls:{self._installation_cache_scope()}{channel_id}:{message_ts}"

    async def _lookup_user(self, user_id: str) -> SlackUserCacheEntry:
        """Look up user info from Slack API with caching.

        Returns a dict with keys ``display_name``, ``real_name``, and
        (when available from the Slack API or from a cached entry) the
        optional fields ``email``, ``avatar_url``, ``is_bot``.

        On API failure — or when the API returns success but with an
        empty/missing ``user`` payload — the returned dict is a fallback
        shape (``display_name`` / ``real_name`` populated with the user
        ID) and carries the private ``_lookup_failed: True`` sentinel so
        callers that need to distinguish "really not found" from "fall
        back to ID" — like :meth:`get_user` — can return ``None``
        instead. The fallback entry is **not** cached so a subsequent
        call retries the lookup.
        """
        cache_key = f"slack:user:{self._installation_cache_scope()}{user_id}"

        if self._chat:
            cached = await self._chat.get_state().get(cache_key)
            if cached and isinstance(cached, dict):
                return {
                    "display_name": cached.get("display_name", user_id),
                    "real_name": cached.get("real_name", user_id),
                    "email": cached.get("email"),
                    "avatar_url": cached.get("avatar_url"),
                    "is_bot": cached.get("is_bot"),
                }

        try:
            client = self._get_client()
            result = await client.users_info(user=user_id)
            user = result.get("user") or {}
            # Slack can return `{"ok": True, "user": {}}` in some edge cases
            # (rare, but observed when scopes are partial or the workspace
            # rejects the lookup post-success). Treat a missing/empty user
            # payload as a lookup failure so we don't poison the cache
            # with a `UserInfo("Uxxx", "Uxxx", "Uxxx")` shape that
            # `get_user` would then convert into a non-null fallback —
            # diverging from the null-on-failure contract callers expect.
            if not user:
                self._logger.warn(
                    "Slack users.info returned empty user payload",
                    {"userId": user_id},
                )
                return _make_slack_lookup_failed(user_id)
            profile = user.get("profile", {})

            display_name = (
                profile.get("display_name")
                or profile.get("real_name")
                or user.get("real_name")
                or user.get("name")
                or user_id
            )
            real_name = user.get("real_name") or profile.get("real_name") or display_name
            email = profile.get("email")
            # Upstream chose `image_192` (vs the older `image_72`) for
            # better avatar quality — see vercel/chat#391.
            avatar_url = profile.get("image_192")
            is_bot = user.get("is_bot")

            cached_entry: SlackUserCacheEntry = {
                "display_name": display_name,
                "real_name": real_name,
                "email": email,
                "avatar_url": avatar_url,
                "is_bot": is_bot,
            }

            if self._chat:
                await self._chat.get_state().set(
                    cache_key,
                    cached_entry,
                    _USER_CACHE_TTL_MS,
                )
                # Reverse index: display name -> user IDs
                normalized_name = display_name.lower()
                reverse_key = f"slack:user-by-name:{self._installation_cache_scope()}{normalized_name}"
                existing = await self._chat.get_state().get_list(reverse_key)
                if user_id not in existing:
                    await self._chat.get_state().append_to_list(
                        reverse_key,
                        user_id,
                        max_length=50,
                        ttl_ms=_REVERSE_INDEX_TTL_MS,
                    )

            self._logger.debug(
                "Fetched user info",
                {"userId": user_id, "displayName": display_name, "realName": real_name},
            )
            return cached_entry
        except Exception as exc:
            self._logger.warn("Could not fetch user info", {"userId": user_id, "error": exc})
            # Keep the fallback dict shape so existing callers (mention
            # resolution, slash command author binding, message parsing)
            # don't change behavior on transient lookup failures — they
            # already used `display_name`/`real_name` and would have
            # received the user ID either way. The private sentinel lets
            # `get_user` distinguish "API failed" from "API returned data".
            return _make_slack_lookup_failed(user_id)

    async def _lookup_channel(self, channel_id: str) -> str:
        """Look up channel name from Slack API with caching."""
        cache_key = f"slack:channel:{self._installation_cache_scope()}{channel_id}"

        if self._chat:
            cached = await self._chat.get_state().get(cache_key)
            if cached and isinstance(cached, dict):
                return cached.get("name", channel_id)

        try:
            client = self._get_client()
            result = await client.conversations_info(channel=channel_id)
            channel = result.get("channel", {})
            name = channel.get("name", channel_id)

            if self._chat:
                await self._chat.get_state().set(cache_key, {"name": name}, _CHANNEL_CACHE_TTL_MS)

            self._logger.debug("Fetched channel info", {"channelId": channel_id, "name": name})
            return name
        except Exception as exc:
            self._logger.warn("Could not fetch channel info", {"channelId": channel_id, "error": exc})
            return channel_id

    # ==================================================================
    # Public user lookup (chat.get_user)
    # ==================================================================

    async def get_user(self, user_id: str) -> UserInfo | None:
        """Look up Slack user info via ``users.info``.

        Returns ``None`` when the Slack API call fails (network error,
        rate limit, missing scopes, unknown user). ``email`` requires the
        ``users:read.email`` scope; ``avatar_url`` is the high-quality
        ``image_192`` from the user's Slack profile.

        Resolves the bot token via :meth:`_resolve_token_async` first so
        callable ``bot_token`` resolvers work outside ``handle_webhook``
        (cron jobs, background tasks) without forcing the caller to seed
        the per-request cache themselves. Static-string ``bot_token`` and
        in-webhook flow are unaffected (the resolver path is a no-op when
        the per-request cache is already primed).

        Mirrors upstream ``SlackAdapter.getUser`` (vercel/chat#391).
        """
        try:
            # Prime the per-request token cache so the sync `_get_token`
            # path inside `_lookup_user` -> `_get_client` finds a value
            # even when called outside `handle_webhook` with a callable
            # `bot_token` resolver. Mirrors the resolver pattern used by
            # other public adapter methods reachable from background
            # contexts (see #87 / docs/UPSTREAM_SYNC.md "bot_token
            # resolver invocation site" row).
            await self._resolve_token_async()
        except AuthenticationError:
            return None
        try:
            cached = await self._lookup_user(user_id)
        except Exception:
            return None
        if cached.get("_lookup_failed"):
            return None
        return UserInfo(
            user_id=user_id,
            user_name=cached.get("display_name") or user_id,
            full_name=cached.get("real_name") or user_id,
            is_bot=bool(cached.get("is_bot")) if cached.get("is_bot") is not None else False,
            email=cached.get("email"),
            avatar_url=cached.get("avatar_url"),
        )

    # ==================================================================
    # Webhook handling
    # ==================================================================

    async def handle_webhook(self, request: Any, options: WebhookOptions | None = None) -> dict[str, Any]:
        """Handle incoming webhooks from Slack.

        Handles URL verification, event callbacks, interactive payloads,
        and slash commands.

        Returns a dict with ``body`` and ``status`` keys.
        """
        # Read the raw body via the shared webhook primitive (the Python
        # stand-in for the Fetch API's ``await request.text()``) so the
        # adapter and the low-level ``webhook`` subpath use one
        # implementation for duck-typed framework requests.
        body: str = await read_slack_request_body(request)

        # Extract headers
        headers = getattr(request, "headers", {})

        # Forwarded socket-mode events bypass Slack signature verification —
        # they're authenticated by a shared bearer secret instead. This lets a
        # separate process run the WebSocket and POST events back to the
        # webhook endpoint over HTTP. Handled BEFORE any signature / verifier /
        # resolver work because the auth model is entirely different: an
        # ``x-slack-socket-token`` request never carries a Slack signature
        # and shouldn't pay the resolver-call cost (or surface resolver
        # failures) when its bearer is invalid. Hazard #12: refuse if no
        # secret is configured rather than treating an empty header match as
        # success.
        socket_token = headers.get("x-slack-socket-token") or headers.get("X-Slack-Socket-Token")
        if socket_token:
            # Constant-time bearer comparison (upstream timingSafeStringEqual,
            # 9824d33 / PR #441). Encode both operands to UTF-8 bytes so
            # ``compare_digest`` mirrors upstream's ``Buffer.from(x, "utf8")``
            # comparison: it returns False on length mismatch (the secret's
            # length is fixed at config time, so that is not a leak) and never
            # raises on non-ASCII tokens the way str comparison would.
            if not self._socket_forwarding_secret or not hmac.compare_digest(
                socket_token.encode(), self._socket_forwarding_secret.encode()
            ):
                self._logger.warn("Invalid socket forwarding token")
                return {"body": "Invalid socket token", "status": 401}
            try:
                event = json.loads(body)
            except (json.JSONDecodeError, ValueError):
                return {"body": "Invalid JSON", "status": 400}
            # Hazard #12 (replay): the shared bearer alone is not enough —
            # without a freshness check, an old captured forwarded event
            # could be replayed indefinitely. Mirror the 5-minute window
            # ``verify_slack_signature`` enforces on signed webhook traffic.
            #
            # Wire format: upstream's ``forwardSocketEvent`` always emits
            # ``timestamp: Date.now()`` — milliseconds since the Unix epoch
            # (~1.78e12 today). Python's ``time.time()`` returns seconds.
            # Auto-detect the unit by magnitude (anything > 10**11 is
            # certainly milliseconds — that crossed in 2001) so we accept
            # both the JS-emitted ms shape AND a Python-emitted seconds
            # shape if a future ``forward_socket_event`` listener lands.
            ts_raw = event.get("timestamp") if isinstance(event, dict) else None
            try:
                ts_int = int(ts_raw) if ts_raw is not None else None
            except (TypeError, ValueError):
                ts_int = None
            ts_seconds = ts_int // 1000 if ts_int is not None and ts_int > 10**11 else ts_int
            if ts_seconds is None or abs(int(time.time()) - ts_seconds) > 300:
                self._logger.warn(
                    "Forwarded socket event outside freshness window",
                    {"timestamp": ts_raw},
                )
                return {"body": "Stale socket event", "status": 401}
            await self._handle_forwarded_socket_event(event, options)
            return {"body": "ok", "status": 200}

        # In socket mode, refuse direct webhook POSTs — Slack delivers events
        # over the WebSocket instead. We still allow forwarded events above.
        if self._mode == "socket":
            return {"body": "Webhooks are disabled in socket mode", "status": 405}

        # Verify the request via the shared webhook primitive (vercel/chat#538
        # extracted this from the adapter) — when a custom ``webhook_verifier``
        # is configured it takes precedence over ``signing_secret`` /
        # ``SLACK_SIGNING_SECRET`` (matches upstream vercel/chat#468). The
        # verifier may also return a string that replaces the body for
        # downstream parsing (e.g. canonicalization).
        try:
            body = await verify_slack_request(
                request,
                body=body,
                signing_secret=self._signing_secret,
                webhook_verifier=self._webhook_verifier,
            )
        except Exception as exc:
            self._logger.warn("Webhook verifier rejected request", {"error": exc})
            return {"body": "Invalid signature", "status": 401}
        # Request-shape metadata only, and only once the request is verified
        # (upstream f485255b) — never the body or any slice of it.
        self._logger.debug("Slack webhook received", {"bodyLength": utf8_byte_length(body)})

        # URL verification is special: Slack sends a JSON ``url_verification``
        # ping at app-install / event-subscription time and only expects the
        # ``challenge`` echo. No token / API call is required — so resolve
        # the JSON-payload short-circuit BEFORE invoking the bot-token
        # resolver. A broken or down resolver (secrets manager outage, key
        # rotation in flight) must NOT prevent URL verification from
        # succeeding — that would block app installation and re-subscription.
        # Mirrors upstream parity where ``getToken`` is only called at
        # per-API-call sites, not at webhook entry.
        content_type = headers.get("content-type") or headers.get("Content-Type") or ""
        early_payload: dict[str, Any] | None = None
        if "application/json" in content_type or (not content_type and body and body.lstrip().startswith(("{", "["))):
            try:
                maybe = json.loads(body)
                if isinstance(maybe, dict):
                    early_payload = maybe
            except (json.JSONDecodeError, ValueError):
                early_payload = None
            if (
                early_payload is not None
                and early_payload.get("type") == "url_verification"
                and early_payload.get("challenge")
            ):
                return {
                    "body": json.dumps({"challenge": early_payload["challenge"]}),
                    "status": 200,
                    "headers": {"Content-Type": "application/json"},
                }

        # Resolve the default bot token via the (possibly async) resolver
        # before dispatching, so synchronous ``_get_token`` call sites
        # downstream see a primed cache. For static-string configs this is
        # already primed at construction, so the resolver is a no-op identity
        # closure. For dynamic resolvers we re-resolve on every webhook entry
        # so rotation is observed.
        if self._is_single_workspace:
            try:
                await self._resolve_default_token()
            except Exception as exc:
                # Resolver failures here would manifest as auth errors deeper
                # in dispatch; surface them at the entry point so the caller
                # gets a clean 500 instead of partial processing. Re-raise.
                self._logger.error("Bot token resolver failed", {"error": exc})
                raise

        # Form-urlencoded payloads (interactive + slash commands)
        if "application/x-www-form-urlencoded" in content_type:
            params = parse_qs(body, keep_blank_values=True)

            # Slash command
            if "command" in params and "payload" not in params:
                if not self._is_single_workspace:
                    # For Enterprise Grid org-wide installs, use enterprise_id;
                    # otherwise use team_id.
                    is_enterprise_install = (params.get("is_enterprise_install") or [None])[0] == "true"
                    enterprise_id = (params.get("enterprise_id") or [None])[0]
                    team_id = (params.get("team_id") or [None])[0]
                    installation_id = enterprise_id if is_enterprise_install else team_id

                    if installation_id:
                        ctx = await self._resolve_token_for_team(installation_id, is_enterprise_install)
                        if ctx:
                            ctx = replace(
                                ctx,
                                enterprise_id=enterprise_id,
                                is_enterprise_install=is_enterprise_install,
                                installation_id=installation_id,
                            )
                            tok = self._request_context.set(ctx)
                            try:
                                return await self._handle_slash_command(params, options)
                            finally:
                                self._request_context.reset(tok)
                        self._logger.warn(
                            "Could not resolve token for slash command",
                            {"installationId": installation_id, "isEnterpriseInstall": is_enterprise_install},
                        )
                    # Missing or unresolved installation: acknowledge without
                    # dispatching, so handlers never run with no token context
                    # (vercel/chat#877).
                    return {"body": "", "status": 200}
                return await self._handle_slash_command(params, options)

            # Interactive payload
            if not self._is_single_workspace:
                installation_info = self._extract_installation_from_interactive(body)
                if installation_info:
                    ctx = await self._resolve_token_for_team(
                        installation_info.installation_id,
                        installation_info.is_enterprise_install,
                    )
                    if ctx:
                        ctx = replace(
                            ctx,
                            enterprise_id=installation_info.enterprise_id,
                            is_enterprise_install=installation_info.is_enterprise_install,
                            installation_id=installation_info.installation_id,
                        )
                        tok = self._request_context.set(ctx)
                        try:
                            return await self._handle_interactive_payload(body, options)
                        finally:
                            self._request_context.reset(tok)
                self._logger.warn("Could not resolve token for interactive payload")
                # Missing or unresolved installation: acknowledge without
                # dispatching (vercel/chat#877).
                return {"body": "", "status": 200}
            return await self._handle_interactive_payload(body, options)

        # JSON payload
        try:
            payload: dict[str, Any] = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return {"body": "Invalid JSON", "status": 400}

        # URL verification challenge
        if payload.get("type") == "url_verification" and payload.get("challenge"):
            return {
                "body": json.dumps({"challenge": payload["challenge"]}),
                "status": 200,
                "headers": {"Content-Type": "application/json"},
            }

        # Multi-workspace: resolve token before processing events.
        # Use contextvars.copy_context() so the ContextVar value persists into
        # any async tasks spawned by _process_event_payload (e.g. process_message
        # creates a task via asyncio.create_task).  The copied context is
        # isolated -- the ContextVar change does not leak back to the caller
        # and does not need an explicit reset.
        if not self._is_single_workspace and payload.get("type") == "event_callback":
            # For Enterprise Grid org-wide installs, use enterprise_id;
            # otherwise use team_id.
            is_enterprise_install = bool(payload.get("is_enterprise_install"))
            installation_id = payload.get("enterprise_id") if is_enterprise_install else payload.get("team_id")

            if installation_id:
                ctx = await self._resolve_token_for_team(installation_id, is_enterprise_install)
                if ctx:
                    ctx = replace(
                        ctx,
                        enterprise_id=payload.get("enterprise_id"),
                        is_enterprise_install=is_enterprise_install,
                        installation_id=installation_id,
                    )
                    isolated = contextvars.copy_context()
                    isolated.run(self._request_context.set, ctx)
                    isolated.run(self._process_event_payload, payload, options)
                    return {"body": "ok", "status": 200}
                self._logger.warn(
                    "Could not resolve token for installation",
                    {"installationId": installation_id, "isEnterpriseInstall": is_enterprise_install},
                )
                return {"body": "ok", "status": 200}

        # Single-workspace mode or fallback
        self._process_event_payload(payload, options)
        return {"body": "ok", "status": 200}

    # ==================================================================
    # Event dispatch
    # ==================================================================

    def _process_event_payload(self, payload: dict[str, Any], options: WebhookOptions | None = None) -> None:
        """Extract and dispatch events from a validated payload."""
        if payload.get("type") != "event_callback" or not payload.get("event"):
            return

        event: dict[str, Any] = payload["event"]

        # Track external/shared channel status. Note: socket-mode payloads
        # synthesized in ``_route_socket_event`` never carry this field, which
        # mirrors upstream's ``routeSocketEvent`` shape. Socket-mode adapters
        # therefore won't populate ``_external_channels`` from this path —
        # documented as a known divergence in ``docs/UPSTREAM_SYNC.md``.
        if payload.get("is_ext_shared_channel"):
            channel_id = event.get("channel") or (event.get("item", {}).get("channel") if "item" in event else None)
            if channel_id:
                self._external_channels.add(channel_id)

        event_type = event.get("type", "")

        if event_type in ("message", "app_mention"):
            if not (event.get("team") or event.get("team_id")) and payload.get("team_id"):
                event["team_id"] = payload["team_id"]
            self._handle_message_event(event, options)
        elif event_type in ("reaction_added", "reaction_removed"):
            self._handle_reaction_event(event, options)
        elif event_type == "assistant_thread_started":
            self._handle_assistant_thread_started(event, options)
        elif event_type == "assistant_thread_context_changed":
            self._handle_assistant_context_changed(event, options)
        elif event_type == "app_home_opened" and event.get("tab") == "home":
            self._handle_app_home_opened(event, options)
        elif event_type == "member_joined_channel":
            self._handle_member_joined_channel(event, options)
        elif event_type == "user_change":
            self._handle_user_change(event)

    # ==================================================================
    # Interactive payloads
    # ==================================================================

    async def _handle_interactive_payload(self, body: str, options: WebhookOptions | None = None) -> dict[str, Any]:
        params = parse_qs(body, keep_blank_values=True)
        payload_str = (params.get("payload") or [None])[0]

        if not payload_str:
            return {"body": "Missing payload", "status": 400}

        try:
            payload: dict[str, Any] = json.loads(payload_str)
        except (json.JSONDecodeError, ValueError):
            return {"body": "Invalid payload JSON", "status": 400}

        return await self._dispatch_interactive_payload(payload, options)

    async def _dispatch_interactive_payload(
        self,
        payload: dict[str, Any],
        options: WebhookOptions | None = None,
    ) -> dict[str, Any]:
        """Dispatch a pre-parsed interactive payload to the right handler.

        Used by both the webhook path (after form-decoding) and the socket
        mode path (which receives the payload as a JSON object directly).
        """
        payload_type = payload.get("type")

        if payload_type == "block_actions":
            self._handle_block_actions(payload, options)
            return {"body": "", "status": 200}
        elif payload_type == "block_suggestion":
            return await self._handle_block_suggestion(payload, options)
        elif payload_type == "view_submission":
            return await self._handle_view_submission(payload, options)
        elif payload_type == "view_closed":
            self._handle_view_closed(payload, options)
            return {"body": "", "status": 200}

        return {"body": "", "status": 200}

    # ==================================================================
    # Slash commands
    # ==================================================================

    async def _handle_slash_command(
        self,
        params: dict[str, list[str]],
        options: WebhookOptions | None = None,
    ) -> dict[str, Any]:
        if not self._chat:
            self._logger.warn("Chat instance not initialized, ignoring slash command")
            return {"body": "", "status": 200}

        command = (params.get("command") or [""])[0]
        text = (params.get("text") or [""])[0]
        user_id = (params.get("user_id") or [""])[0]
        channel_id = (params.get("channel_id") or [""])[0]
        trigger_id = (params.get("trigger_id") or [None])[0]

        # Divergence from upstream — see docs/UPSTREAM_SYNC.md: log the
        # command text's length, not its content.
        self._logger.debug(
            "Processing Slack slash command",
            {"command": command, "textLength": len(text), "userId": user_id, "channelId": channel_id},
        )
        user_info = await self._lookup_user(user_id)
        event = SlashCommandEvent(
            command=command,
            text=text,
            user=Author(
                user_id=user_id,
                user_name=user_info["display_name"],
                full_name=user_info["real_name"],
                is_bot=False,
                is_me=False,
            ),
            adapter=self,
            channel=None,  # pyrefly: ignore[bad-argument-type]  # filled in by Chat
            raw={k: v[0] for k, v in params.items()} if params else {},
            trigger_id=trigger_id,
        )
        # Attach channel_id so chat.py can build a ChannelImpl
        event.channel_id = f"slack:{channel_id}" if channel_id else ""  # type: ignore[attr-defined]
        self._chat.process_slash_command(event, options)
        return {"body": "", "status": 200}

    # ==================================================================
    # Block actions
    # ==================================================================

    def _handle_block_actions(self, payload: dict[str, Any], options: WebhookOptions | None = None) -> None:
        if not self._chat:
            self._logger.warn("Chat instance not initialized, ignoring action")
            return

        channel = (payload.get("channel") or {}).get("id") or (payload.get("container") or {}).get("channel_id")
        message_ts = (payload.get("message") or {}).get("ts") or (payload.get("container") or {}).get("message_ts")
        # DMs have no threads: a button click on a top-level DM message must not
        # thread the response. Mirror _handle_message_event's DM handling — keep a
        # real in-DM thread_ts, but never fall back to the clicked message's own
        # ts, which would spawn a phantom "1 reply" thread in the DM.
        is_dm = bool(channel) and channel.startswith("D")
        thread_ts = (
            (payload.get("message") or {}).get("thread_ts")
            or (payload.get("container") or {}).get("thread_ts")
            or ("" if is_dm else message_ts)
        )

        is_view_action = (payload.get("container") or {}).get("type") == "view"

        if not (is_view_action or channel):
            self._logger.warn("Missing channel in block_actions", {"channel": channel})
            return

        thread_id = ""
        if channel and (thread_ts or message_ts):
            thread_id = self.encode_thread_id(
                SlackThreadId(
                    channel=channel,
                    thread_ts=thread_ts if thread_ts is not None else "",
                )
            )

        is_ephemeral = (payload.get("container") or {}).get("is_ephemeral") is True
        response_url = payload.get("response_url")
        user_ref = payload.get("user") or {}
        message_id: str
        if is_ephemeral and response_url and message_ts:
            message_id = self._encode_ephemeral_message_id(message_ts, response_url, user_ref.get("id", ""))
        else:
            message_id = message_ts or ""

        for action in payload.get("actions", []):
            # Upstream ``selected_option?.value ?? value``: a selected option
            # whose value is "" reports "", not the action's ``value``.
            action_value = (action.get("selected_option") or {}).get("value")
            if action_value is None:
                action_value = action.get("value")
            action_event = ActionEvent(
                action_id=action.get("action_id", ""),
                value=action_value,
                user=Author(
                    user_id=user_ref.get("id", ""),
                    user_name=user_ref.get("username") or user_ref.get("name") or "unknown",
                    full_name=user_ref.get("name") or user_ref.get("username") or "unknown",
                    is_bot=False,
                    is_me=False,
                ),
                message_id=message_id,
                thread_id=thread_id,
                thread=None,  # pyrefly: ignore[bad-argument-type]  # filled in by Chat
                adapter=self,
                raw=payload,
                trigger_id=payload.get("trigger_id"),
            )

            self._logger.debug(
                "Processing Slack block action",
                {
                    "actionId": action.get("action_id"),
                    "value": action.get("value"),
                    "messageId": message_ts,
                    "threadId": thread_id,
                    "triggerId": payload.get("trigger_id"),
                },
            )
            self._chat.process_action(action_event, options)

    # ==================================================================
    # Block suggestion (external-select options load)
    # ==================================================================

    async def _handle_block_suggestion(
        self, payload: dict[str, Any], options: WebhookOptions | None = None
    ) -> dict[str, Any]:
        """Handle a Slack block_suggestion interactive payload.

        Slack requires a response within 3s for block_suggestion and does not
        support an async ack pattern — options must be in the response body.
        Race the handler against a 2.5s budget and fall back to an empty 200
        so the menu shows "No results" instead of hanging for the user.
        """
        if not self._chat:
            self._logger.warn("Chat instance not initialized, ignoring block suggestion")
            return self._options_load_response([])

        user_ref = payload.get("user") or {}
        user_id = user_ref.get("id", "")
        username = user_ref.get("username")
        name = user_ref.get("name")
        # Upstream uses `||` truthy-fallthrough intentionally: an empty-string
        # username falls through to name, then user_id. See upstream
        # packages/adapter-slack/src/index.ts lines ~1258-1260.
        user_name = username or name or user_id
        full_name = name or username or user_id

        action_id = payload.get("action_id", "")
        val = payload.get("value")
        event = OptionsLoadEvent(
            action_id=action_id,
            query=val if val is not None else "",
            user=Author(
                user_id=user_id,
                user_name=user_name,
                full_name=full_name,
                is_bot=False,
                is_me=False,
            ),
            adapter=self,
            raw=payload,
        )

        # Use asyncio.shield so the orphaned task still runs (and logs errors)
        # if we time out. `wait_for` cancels the awaitable on timeout; shielding
        # prevents that cancellation from propagating into the handler task.
        # Use asyncio.ensure_future — process_options_load is typed as returning
        # Awaitable (matching sibling process_* methods on the ChatInstance
        # Protocol); create_task() would require narrowing to Coroutine.
        load_task = asyncio.ensure_future(self._chat.process_options_load(event, options))

        try:
            result = await asyncio.wait_for(asyncio.shield(load_task), timeout=OPTIONS_LOAD_TIMEOUT_MS / 1000.0)
        except asyncio.TimeoutError:
            self._logger.warn(
                "Options load handler timed out",
                {"action_id": action_id, "timeout_ms": OPTIONS_LOAD_TIMEOUT_MS},
            )

            def _late_error(t: asyncio.Task[Any]) -> None:
                if t.cancelled():
                    return
                exc = t.exception()
                if exc is not None:
                    self._logger.error(
                        "Options load handler error after timeout",
                        {"action_id": action_id, "error": str(exc)},
                    )

            load_task.add_done_callback(_late_error)
            _pin_task(load_task)
            # Register with wait_until so serverless/webhook runtimes
            # (e.g. Vercel) keep the task alive past the HTTP response;
            # otherwise the late-error logging path above can be killed
            # before it runs. wait_until is user/runtime-provided, so
            # guard against it raising — we still want to return the
            # empty-options HTTP 200 fallback.
            if options and options.wait_until:
                try:
                    options.wait_until(load_task)
                except Exception as err:
                    self._logger.warn(
                        "wait_until raised while registering timed-out options load task",
                        {"action_id": action_id, "error": str(err)},
                    )
            return self._options_load_response([])

        return self._options_load_response(result if result is not None else [])

    def _options_load_response(
        self,
        result: list[SelectOptionElement] | list[OptionsLoadGroup],
    ) -> dict[str, Any]:
        """Serialize a flat option list or grouped option list to a Slack JSON response.

        Mirrors upstream ``optionsLoadResponse``: when the first entry has an
        ``options`` key it is treated as a list of :class:`OptionsLoadGroup`
        and rendered as ``option_groups``; otherwise it's a flat list of
        :class:`SelectOptionElement` rendered as ``options``. Slack's spec is
        explicit that the two are mutually exclusive (only one may appear in
        the response body).
        """
        # Detect grouped form (TS: ``"options" in result[0]``). A grouped
        # entry is a dict with an ``options`` list inside it; a flat entry is
        # a dict with ``label``/``value`` keys.
        is_groups = (
            len(result) > 0
            and isinstance(result[0], dict)
            and "options" in result[0]
            and isinstance(result[0].get("options"), list)
        )

        if is_groups:
            groups_in = cast("list[OptionsLoadGroup]", result)[:100]
            slack_groups: list[dict[str, Any]] = []
            for group in groups_in:
                group_options = group.get("options", [])[:100]
                slack_groups.append(
                    {
                        # Slack spec: group label is plain_text, max 75 chars.
                        "label": {"type": "plain_text", "text": group.get("label", "")[:75]},
                        "options": [self._select_option_to_slack(opt) for opt in group_options],
                    }
                )
            return {
                "body": json.dumps({"option_groups": slack_groups}),
                "status": 200,
                "headers": {"Content-Type": "application/json"},
            }

        flat = cast("list[SelectOptionElement]", result)[:100]
        return {
            "body": json.dumps({"options": [self._select_option_to_slack(opt) for opt in flat]}),
            "status": 200,
            "headers": {"Content-Type": "application/json"},
        }

    @staticmethod
    def _select_option_to_slack(opt: SelectOptionElement) -> dict[str, Any]:
        """Convert a :class:`SelectOptionElement` to Slack's option object shape.

        Mirrors upstream ``selectOptionToSlackOption`` — the ``description``
        key is omitted (not set to ``null``) when not provided.
        """
        entry: dict[str, Any] = {
            "text": {"type": "plain_text", "text": opt.get("label", "")},
            "value": opt.get("value", ""),
        }
        desc = opt.get("description")
        if desc:
            entry["description"] = {"type": "plain_text", "text": desc}
        return entry

    # ==================================================================
    # View submission / close
    # ==================================================================

    async def _handle_view_submission(
        self, payload: dict[str, Any], options: WebhookOptions | None = None
    ) -> dict[str, Any]:
        if not self._chat:
            self._logger.warn("Chat instance not initialized, ignoring view submission")
            return {"body": "", "status": 200}

        view = payload.get("view", {})
        state_values = view.get("state", {}).get("values", {})

        # Flatten values. Upstream ``value ?? selected_date ??
        # selected_option?.value ?? ""``: a cleared text input submits ``""``
        # (not the next fallback); a datepicker reports ``selected_date``.
        values: dict[str, str] = {}
        for block_values in state_values.values():
            for action_id, input_val in block_values.items():
                submitted = input_val.get("value")
                if submitted is None:
                    submitted = input_val.get("selected_date")
                if submitted is None:
                    submitted = (input_val.get("selected_option") or {}).get("value")
                values[action_id] = submitted if submitted is not None else ""

        meta = decode_modal_metadata(view.get("private_metadata") or None)
        user_ref = payload.get("user", {})

        event = ModalSubmitEvent(
            callback_id=view.get("callback_id", ""),
            view_id=view.get("id", ""),
            values=values,
            private_metadata=meta.private_metadata,
            user=Author(
                user_id=user_ref.get("id", ""),
                user_name=user_ref.get("username") or user_ref.get("name") or "unknown",
                full_name=user_ref.get("name") or user_ref.get("username") or "unknown",
                is_bot=False,
                is_me=False,
            ),
            adapter=self,
            raw=payload,
        )

        response = await self._chat.process_modal_submit(event, meta.context_id, options)

        if response:
            slack_response = self._modal_response_to_slack(response, meta.context_id)
            return {
                "body": json.dumps(slack_response),
                "status": 200,
                "headers": {"Content-Type": "application/json"},
            }

        return {"body": "", "status": 200}

    def _handle_view_closed(self, payload: dict[str, Any], options: WebhookOptions | None = None) -> None:
        if not self._chat:
            self._logger.warn("Chat instance not initialized, ignoring view closed")
            return

        view = payload.get("view", {})
        meta = decode_modal_metadata(view.get("private_metadata") or None)
        user_ref = payload.get("user", {})

        event = ModalCloseEvent(
            callback_id=view.get("callback_id", ""),
            view_id=view.get("id", ""),
            private_metadata=meta.private_metadata,
            user=Author(
                user_id=user_ref.get("id", ""),
                user_name=user_ref.get("username") or user_ref.get("name") or "unknown",
                full_name=user_ref.get("name") or user_ref.get("username") or "unknown",
                is_bot=False,
                is_me=False,
            ),
            adapter=self,
            raw=payload,
        )

        self._chat.process_modal_close(event, meta.context_id, options)

    def _modal_response_to_slack(self, response: ModalResponse, context_id: str | None = None) -> SlackModalResponse:
        if response.action == "close":
            return {}
        if response.action == "clear":
            # Close the entire modal view stack (Slack ``response_action: clear``).
            return {"response_action": "clear"}
        if response.action == "errors":
            return {"response_action": "errors", "errors": response.errors or {}}
        if response.action in ("update", "push"):
            modal = response.modal
            if isinstance(modal, dict):
                metadata = encode_modal_metadata(
                    ModalMetadata(
                        context_id=context_id,
                        private_metadata=modal.get("private_metadata"),
                    )
                )
                view = modal_to_slack_view(cast(ModalElement, modal), metadata)
                return {"response_action": response.action, "view": view}
        return {}

    # ==================================================================
    # Socket Mode
    # ==================================================================

    async def start_socket_mode(self) -> None:
        """Open a Slack Socket Mode WebSocket and dispatch events.

        Spawns a tracked background task that connects, runs the message
        loop, and reconnects with exponential backoff on disconnect (per
        Slack's recommendation). Returns once the initial connection has
        been established.

        Raises :class:`ValidationError` if the adapter wasn't configured
        with ``app_token`` (must start with ``xapp-``).

        Idempotent: a second call while connected is a no-op.
        """
        if not self._app_token:
            raise ValidationError(
                "slack",
                "appToken is required for socket mode. Set SLACK_APP_TOKEN or provide it in config.",
            )

        if self._socket_task is not None and not self._socket_task.done():
            # Already running.
            return

        # Lazy import (hazard #10) — slack_sdk is an optional dependency.
        try:
            from slack_sdk.socket_mode.aiohttp import SocketModeClient  # noqa: F401
        except ImportError as exc:
            raise ValidationError(
                "slack",
                "slack_sdk Socket Mode dependencies are not installed. "
                "Install with `pip install chat-sdk[slack-socket]`.",
            ) from exc

        self._socket_shutdown_event.clear()
        connected = asyncio.Event()
        loop = asyncio.get_running_loop()
        # Hazard #5: track the task explicitly so ``stop_socket_mode`` can
        # cancel it cleanly. Don't use ``asyncio.ensure_future`` without
        # tracking — a stray reference loss would orphan the WebSocket.
        self._socket_task = loop.create_task(self._socket_mode_loop(connected))

        # Wait for either the first successful connect or for the loop to
        # exit (which means the very first connect raised). Re-raise so the
        # caller learns about a hard config failure (bad app token, network
        # offline) instead of silently spinning forever. Bound the wait with
        # ``connect_timeout_s`` so a hung handshake (slack_sdk's ``connect()``
        # never returning) doesn't make ``initialize()`` block indefinitely
        # (hazard #11).
        wait_task = asyncio.create_task(connected.wait())
        try:
            # ``shield`` keeps the inner ``asyncio.wait`` alive when
            # ``wait_for`` times out, so we can deterministically tear
            # ``wait_task`` and ``_socket_task`` down ourselves in the
            # except branch. Both tasks are cancelled there (``wait_task``
            # directly, ``_socket_task`` via ``stop_socket_mode``), so the
            # shielded inner wait always resolves shortly after — no orphan.
            first_done, pending = await asyncio.wait_for(
                asyncio.shield(
                    asyncio.wait(
                        {wait_task, self._socket_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                ),
                timeout=self._socket_connect_timeout_s,
            )
        except asyncio.TimeoutError:
            # Don't leak the wait task or the still-running socket loop —
            # tear them down before surfacing the failure.
            wait_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                # ``await`` is load-bearing here: it drains the cancelled
                # task before this function returns so the asyncio loop
                # doesn't emit "Task was destroyed but it is pending!"
                # warnings at shutdown, and so callers can rely on a
                # synchronous "task fully gone" contract after timeout.
                # ``contextlib.suppress`` absorbs the expected
                # ``CancelledError`` that wait_task raises on cancel.
                # Static analyzers (github-code-quality, etc.) sometimes
                # flag this as "statement has no effect" because they
                # model ``await`` syntactically rather than as a
                # side-effecting suspension — that's a false positive;
                # do not remove this line.
                await wait_task
            await self.stop_socket_mode()
            raise TimeoutError(f"Slack Socket Mode connect timed out after {self._socket_connect_timeout_s}s") from None
        # Hazard #5: if the loop task finished first, cancel the wait task
        # explicitly so the orphan ``connected.wait()`` doesn't sit forever.
        if wait_task in pending:
            wait_task.cancel()
        if connected.is_set():
            return
        # Socket loop exited before connecting — surface its exception.
        for done in first_done:
            if done is self._socket_task:
                exc = done.exception()
                if exc is not None:
                    raise exc

    async def stop_socket_mode(self) -> None:
        """Close the Socket Mode connection and cancel the reconnect loop.

        Idempotent. Safe to call from any task; it disconnects the active
        client and waits for the background task to finish.
        """
        self._socket_shutdown_event.set()

        client = self._socket_client
        self._socket_client = None
        if client is not None:
            try:
                await client.disconnect()
            except Exception as exc:  # pragma: no cover - best-effort cleanup
                self._logger.warn("Error disconnecting Slack socket client", {"error": str(exc)})

        task = self._socket_task
        self._socket_task = None
        if task is not None and not task.done():
            task.cancel()
            # Cancellation is expected on shutdown; surface anything else so
            # surprising loop crashes aren't silently swallowed.
            with contextlib.suppress(asyncio.CancelledError):
                # ``await`` is load-bearing: deterministically drains the
                # cancelled loop task before ``stop_socket_mode()``
                # returns. Without it, ``stop_socket_mode`` can return
                # while the loop task is still tearing down, which:
                #   - breaks ``test_stop_idempotent`` (the second call
                #     can race the first's cleanup)
                #   - risks "Task was destroyed but it is pending!"
                #     warnings if the loop holds GC-only references
                # ``contextlib.suppress`` absorbs the expected
                # ``CancelledError``. Static analyzers sometimes flag
                # this as "statement has no effect" because they model
                # ``await`` syntactically — that's a false positive; do
                # not remove this line.
                await task
        if task is not None:
            self._logger.info("Slack socket mode disconnected")

    async def _socket_mode_loop(self, connected: asyncio.Event) -> None:
        """Connect/run/reconnect loop for Socket Mode.

        Slack's Socket Mode WebSocket is long-lived but can disconnect for
        many reasons (refresh, network blip, restart). We retry with
        exponential backoff (with jitter) and reset the backoff once a
        connection holds for any non-trivial time.
        """
        from slack_sdk.socket_mode.aiohttp import SocketModeClient

        backoff = self._socket_initial_backoff_s
        try:
            while not self._socket_shutdown_event.is_set():
                client = SocketModeClient(**self._socket_client_kwargs())
                # Register our request handler. ``socket_mode_request_listeners``
                # is the documented public extension point on the slack_sdk
                # client; each listener is ``async (client, request) -> None``.
                client.socket_mode_request_listeners.append(self._on_socket_request)
                self._socket_client = client
                try:
                    await client.connect()
                except Exception as exc:
                    self._logger.error(
                        "Slack socket mode connect failed",
                        {"error": str(exc)},
                    )
                    self._socket_client = None
                    if self._socket_shutdown_event.is_set():
                        return
                    if not connected.is_set():
                        # First connect failed and nobody's listening yet —
                        # surface the error to the caller of start_socket_mode.
                        raise
                    await self._socket_sleep_with_backoff(backoff)
                    backoff = min(backoff * 2, self._socket_max_backoff_s)
                    continue

                # Connection established (or in progress) — let the caller of
                # start_socket_mode resume.
                self._logger.info("Slack socket mode connected")
                connected.set()
                backoff = self._socket_initial_backoff_s

                # Wait until the socket disconnects or shutdown is requested.
                while not self._socket_shutdown_event.is_set():
                    if not client.is_connected():
                        break
                    await asyncio.sleep(1.0)

                # Tear down the current client before reconnecting.
                self._socket_client = None
                try:
                    await client.disconnect()
                except Exception as exc:  # pragma: no cover - best-effort
                    self._logger.warn(
                        "Error disconnecting Slack socket client during reconnect",
                        {"error": str(exc)},
                    )

                if self._socket_shutdown_event.is_set():
                    return
                self._logger.info("Slack socket mode disconnected, reconnecting")
                await self._socket_sleep_with_backoff(backoff)
                backoff = min(backoff * 2, self._socket_max_backoff_s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Make sure first-connect failures propagate to the caller of
            # start_socket_mode, but also log everything else loudly.
            if not connected.is_set():
                raise
            self._logger.error(
                "Slack socket mode loop crashed",
                {"error": str(exc)},
            )
        finally:
            client = self._socket_client
            self._socket_client = None
            if client is not None:
                with contextlib.suppress(Exception):  # pragma: no cover - best-effort
                    await client.disconnect()

    def _socket_client_kwargs(self) -> dict[str, Any]:
        """Constructor kwargs for one ``SocketModeClient`` (vercel/chat 6adca361).

        Port of upstream ``socketTransportOptions``: only the transport-shaped
        options reach Socket Mode. ``web_client_options["proxy"]`` is passed as
        ``proxy=`` (used for the WebSocket), and a dedicated ``web_client`` for
        ``apps.connections.open`` gets the ``proxy``/``ssl`` subset plus the
        ``api_url`` override as ``base_url``. Headers, timeouts and retry
        handlers are not forwarded, and every call builds a fresh client so
        no state is shared across reconnects. With none of those configured
        the kwargs are just ``app_token`` (slack_sdk defaults).
        """
        kwargs: dict[str, Any] = {"app_token": cast(str, self._app_token)}
        options = self._web_client_options if self._web_client_options is not None else {}
        web_kwargs: dict[str, Any] = {key: options[key] for key in ("proxy", "ssl") if options.get(key) is not None}
        if self._slack_api_url:
            web_kwargs["base_url"] = self._slack_api_url
        if "proxy" in web_kwargs:
            kwargs["proxy"] = web_kwargs["proxy"]
        if web_kwargs:
            from slack_sdk.web.async_client import AsyncWebClient

            kwargs["web_client"] = AsyncWebClient(**web_kwargs)
        return kwargs

    async def _socket_sleep_with_backoff(self, seconds: float) -> None:
        """Sleep for ``seconds`` but wake immediately on shutdown.

        Uses the per-adapter ``_socket_shutdown_event`` so ``stop_socket_mode``
        can interrupt the backoff window without polling — wakeup latency is
        bounded by event-loop scheduling, not the previous 0.25s poll.
        """
        try:
            await asyncio.wait_for(self._socket_shutdown_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return

    async def _on_socket_request(self, client: Any, request: Any) -> None:
        """Listener invoked by ``SocketModeClient`` for each socket message.

        Signature matches slack_sdk's documented hook:
        ``async (client: SocketModeClient, request: SocketModeRequest) -> None``.
        """
        from slack_sdk.socket_mode.response import SocketModeResponse

        envelope_id = getattr(request, "envelope_id", "") or ""
        event_type = getattr(request, "type", "") or ""
        payload = getattr(request, "payload", None) or {}
        retry_attempt = getattr(request, "retry_attempt", 0) or 0

        async def ack(response_payload: dict[str, Any] | None = None) -> None:
            try:
                await client.send_socket_mode_response(
                    SocketModeResponse(envelope_id=envelope_id, payload=response_payload)
                )
            except Exception as exc:  # pragma: no cover - best-effort
                self._logger.warn(
                    "Failed to send socket mode ack",
                    {"envelope_id": envelope_id, "error": str(exc)},
                )

        # Slack re-delivers events that weren't acked in time. Skip retries
        # so we don't double-process — but still ack so Slack stops resending.
        if retry_attempt and retry_attempt > 0:
            await ack()
            self._logger.debug("Skipping socket mode retry", {"retry_attempt": retry_attempt})
            return

        await self._route_socket_event(payload, event_type, ack)

    async def _route_socket_event(
        self,
        body: dict[str, Any],
        event_type: str,
        ack: Callable[..., Awaitable[None]],
        options: WebhookOptions | None = None,
    ) -> None:
        """Route a socket-mode event to the same handler the webhook path uses.

        Mirrors upstream's ``routeSocketEvent``. The ``ack`` callback delivers
        the SocketModeResponse back to Slack — for events_api and
        slash_commands we ack immediately and let processing run in the
        background; for interactive payloads we may attach a response body
        (e.g. modal ``view_submission`` errors) onto the ack.
        """

        def wrap_async(coro: Awaitable[Any]) -> None:
            """Run ``coro`` either via ``waitUntil`` or as a tracked task."""
            if options is not None and options.wait_until is not None:
                # ``wait_until`` semantics: caller takes ownership.
                options.wait_until(cast(Any, coro))
                return
            task = asyncio.get_running_loop().create_task(cast(Any, coro))

            def _log_exc(t: asyncio.Task[Any]) -> None:
                if t.cancelled():
                    return
                exc = t.exception()
                if exc is not None:
                    self._logger.error(
                        "Error in socket mode async handler",
                        {"error": str(exc)},
                    )

            task.add_done_callback(_log_exc)
            _pin_task(task)

        if event_type == "events_api":
            await ack()
            event = body.get("event")
            if not isinstance(event, dict):
                self._logger.warn(
                    "Socket mode events_api missing event field",
                    {"body_type": type(body).__name__},
                )
                return
            # Match the webhook path's synthesized payload exactly. Upstream
            # doesn't include ``is_ext_shared_channel`` here, and the webhook
            # JSON we pass into ``_process_event_payload`` doesn't either —
            # adding it on the socket path is a quiet socket-vs-webhook
            # divergence (hazard #7). Keep the keys that flow into
            # downstream handlers, drop the rest.
            payload: dict[str, Any] = {
                "type": "event_callback",
                "event": event,
                "team_id": body.get("team_id"),
                "event_id": body.get("event_id"),
                "event_time": body.get("event_time"),
            }
            # Multi-workspace: resolve token before dispatch (mirrors webhook
            # path). copy_context() keeps the ContextVar set on tasks spawned
            # by handlers (hazard #6).
            team_id_event = payload.get("team_id")
            try:
                if not self._is_single_workspace and team_id_event:
                    ctx = await self._resolve_token_for_team(team_id_event)
                    if ctx is None:
                        self._logger.warn(
                            "Could not resolve token for team",
                            {"teamId": team_id_event},
                        )
                        return
                    ctx = replace(ctx, installation_id=team_id_event)
                    isolated = contextvars.copy_context()
                    isolated.run(self._request_context.set, ctx)
                    isolated.run(self._process_event_payload, payload, options)
                else:
                    self._process_event_payload(payload, options)
            except Exception as exc:
                self._logger.error(
                    "Error processing socket mode events_api",
                    {"error": str(exc)},
                )
            return

        if event_type == "slash_commands":
            # Slash responses are out-of-band via ``response_url`` (Slack's
            # delayed-response pattern), not the WebSocket ack. The empty ack
            # here just tells Slack we received the command; the body of the
            # reply flows through ``_handle_slash_command`` posting to the
            # response_url. Matches upstream's `routeSocketEvent`.
            await ack()
            # slash_commands payload is a flat dict mirroring the
            # form-urlencoded fields; convert to the parse_qs shape that
            # _handle_slash_command expects (each value wrapped in a list).
            params: dict[str, list[str]] = {k: [v] for k, v in body.items() if isinstance(v, str)}

            async def run_slash() -> None:
                if self._is_single_workspace:
                    await self._handle_slash_command(params, options)
                    return
                team_id_slash = (params.get("team_id") or [None])[0]
                ctx = await self._resolve_token_for_team(team_id_slash) if team_id_slash else None
                if ctx is None:
                    # Missing or unresolved installation: already acked, do
                    # not dispatch without a token context (vercel/chat#877).
                    self._logger.warn("Could not resolve token for slash command")
                    return
                ctx = replace(ctx, installation_id=team_id_slash)
                tok = self._request_context.set(ctx)
                try:
                    await self._handle_slash_command(params, options)
                finally:
                    self._request_context.reset(tok)

            wrap_async(run_slash())
            return

        if event_type == "interactive":
            try:
                # Multi-workspace: scope token resolution to the dispatch.
                team_ref = body.get("team")
                # Upstream ``team?.id || payload.team_id``: a ``team`` object
                # without an id still falls back to the top-level field.
                team_id_interactive = (team_ref.get("id") if isinstance(team_ref, dict) else None) or body.get(
                    "team_id"
                )
                if not self._is_single_workspace:
                    ctx = await self._resolve_token_for_team(team_id_interactive) if team_id_interactive else None
                    if ctx is None:
                        # Missing or unresolved installation: ack without
                        # dispatching (vercel/chat#877).
                        self._logger.warn("Could not resolve token for socket interactive payload")
                        await ack()
                        return
                    ctx = replace(ctx, installation_id=team_id_interactive)
                    tok = self._request_context.set(ctx)
                    try:
                        result = await self._dispatch_interactive_payload(body, options)
                    finally:
                        self._request_context.reset(tok)
                else:
                    result = await self._dispatch_interactive_payload(body, options)
            except Exception as exc:
                self._logger.error(
                    "Error processing socket mode interactive",
                    {"error": str(exc)},
                )
                # Hazard #15 (UX): an empty ack on view_submission silently
                # closes the modal, so the user has no signal anything went
                # wrong. Return ``response_action=errors`` so Slack keeps the
                # modal open with a visible message. Safe for non-modal
                # interactive types too — Slack ignores the field when the
                # payload type doesn't expect it.
                await ack({"response_action": "errors", "errors": {"_": "internal error"}})
                return

            response_body: dict[str, Any] | None = None
            body_str = result.get("body") if isinstance(result, dict) else None
            if isinstance(body_str, str) and body_str:
                content_type = result.get("headers", {}).get("Content-Type", "") if isinstance(result, dict) else ""
                if "application/json" in content_type:
                    try:
                        parsed = json.loads(body_str)
                        if isinstance(parsed, dict):
                            response_body = parsed
                    except (json.JSONDecodeError, ValueError):
                        response_body = None
            await ack(response_body)
            return

        # Unknown event type — still ack so Slack doesn't redeliver.
        await ack()
        self._logger.debug("Unhandled socket mode event type", {"type": event_type})

    async def _handle_forwarded_socket_event(
        self,
        event: dict[str, Any],
        options: WebhookOptions | None = None,
    ) -> None:
        """Process a socket-mode event forwarded over HTTP.

        Companion to :meth:`_route_socket_event` for the serverless pattern
        where a long-running listener runs in one process and posts events
        to a webhook handler elsewhere. The ack already happened on the
        listener side; we just route to the same handler dispatch.
        """

        async def noop_ack(_response: dict[str, Any] | None = None) -> None:
            return None

        body = event.get("body")
        event_type = event.get("eventType") or event.get("event_type") or ""
        if not isinstance(body, dict) or not isinstance(event_type, str):
            self._logger.warn(
                "Forwarded socket event has invalid shape",
                {"event_type": type(event_type).__name__},
            )
            return
        await self._route_socket_event(body, event_type, noop_ack, options)

    # ==================================================================
    # Message events
    # ==================================================================

    def _handle_message_event(self, event: dict[str, Any], options: WebhookOptions | None = None) -> None:
        if not self._chat:
            self._logger.warn("Chat instance not initialized, ignoring event")
            return

        subtype = event.get("subtype")
        if subtype == "message_changed":
            self._handle_message_changed(event, options)
            return
        if subtype == "message_deleted":
            self._handle_message_deleted(event, options)
            return
        if subtype and subtype in _IGNORED_SUBTYPES:
            self._logger.debug("Ignoring message subtype", {"subtype": subtype})
            return

        if not (event.get("channel") and event.get("ts")):
            self._logger.debug(
                "Ignoring event without channel or ts",
                {"channel": event.get("channel"), "ts": event.get("ts")},
            )
            return

        # See _thread_id_for_message_event for the DM/channel rule.
        thread_id = self._thread_id_for_message_event(event)

        # ``is_mention`` comes from the message content (``_detect_self_mention``):
        # Slack fires ``app_mention`` even for a bot id inside code, so the
        # event type alone is not trusted (upstream vercel/chat#947).
        async def factory() -> Message:
            return await self._parse_slack_message(event, thread_id)

        self._chat.process_message(self, thread_id, factory, options)

    def _thread_id_for_message_event(self, event: dict[str, Any]) -> str:
        """Thread ID for a message-shaped event (upstream ``threadIdForMessageEvent``).

        Single source of truth for how message, edit and delete events map
        onto a thread: they must agree, or an edit dispatches to a different
        thread than the message it edits.

        DMs: top-level messages use an empty ``thread_ts`` (matching openDM
        subscriptions); thread replies use ``thread_ts``. Channels: always
        ``thread_ts`` or ``ts``. Upstream uses ``||`` here, so an empty
        ``thread_ts`` falls through to ``ts`` (hence ``or``, not ``is not None``).
        """
        is_dm = event.get("channel_type") == "im"
        # agent_view hook (#214): upstream applies the DM rule only when
        # ``!this.agentView``; under agent_view a DM uses ``thread_ts || ts``
        # like a channel. Add that condition here when agent_view lands.
        thread_ts = (event.get("thread_ts") or "") if is_dm else (event.get("thread_ts") or event.get("ts") or "")
        return self.encode_thread_id(SlackThreadId(channel=event.get("channel") or "", thread_ts=thread_ts))

    # ==================================================================
    # Reaction events
    # ==================================================================

    def _handle_reaction_event(self, event: dict[str, Any], options: WebhookOptions | None = None) -> None:
        if not self._chat:
            self._logger.warn("Chat instance not initialized, ignoring reaction")
            return

        item = event.get("item", {})
        if item.get("type") != "message":
            self._logger.debug("Ignoring reaction to non-message item", {"itemType": item.get("type")})
            return

        channel = item.get("channel", "")
        message_id = item.get("ts", "")
        raw_emoji = event.get("reaction", "")
        normalized_emoji = resolve_emoji_from_slack(raw_emoji)

        # Check if reaction is from this bot
        ctx = self._request_context.get()
        user_id = event.get("user", "")
        is_me = (
            (ctx is not None and ctx.bot_user_id and user_id == ctx.bot_user_id)
            or (self._bot_user_id is not None and user_id == self._bot_user_id)
            or (self._bot_id is not None and user_id == self._bot_id)
        )

        chat = self._chat

        async def _resolve_and_process() -> None:
            # Resolve the actual parent thread_ts via conversations.replies.
            # item.ts may be a reply rather than the root message, so we
            # need to look up the thread_ts of the message to find the
            # conversation root.
            parent_ts = message_id
            try:
                client = self._get_client()
                result = await client.conversations_replies(
                    channel=channel,
                    ts=message_id,
                    limit=1,
                    inclusive=True,
                )
                msgs = result.get("messages", [])
                if msgs:
                    parent_ts = msgs[0].get("thread_ts") or msgs[0].get("ts") or message_id
            except Exception as err:
                self._logger.debug(
                    "Could not resolve parent thread_ts for reaction, using item.ts",
                    {"error": str(err), "channel": channel, "ts": message_id},
                )

            thread_id = self.encode_thread_id(SlackThreadId(channel=channel, thread_ts=parent_ts))

            # Resolve display names from the cached users.info lookup so
            # reaction handlers see real names instead of raw user IDs
            # (vercel/chat#523). Falls back to the user ID on lookup failure.
            user_info = await self._lookup_user(user_id)
            display_name = user_info.get("display_name")
            user_name = display_name if display_name is not None else user_id
            real_name = user_info.get("real_name")
            full_name = real_name if real_name is not None else user_name
            is_bot = user_info.get("is_bot")

            reaction_event = ReactionEvent(
                emoji=normalized_emoji,
                raw_emoji=raw_emoji,
                added=event.get("type") == "reaction_added",
                user=Author(
                    user_id=user_id,
                    user_name=user_name,
                    full_name=full_name,
                    is_bot=is_bot if is_bot is not None else False,
                    is_me=is_me,
                ),
                message_id=message_id,
                thread_id=thread_id,
                thread=None,  # pyrefly: ignore[bad-argument-type]  # filled in by Chat
                raw=event,
                adapter=self,
            )

            chat.process_reaction(reaction_event, options)

        try:
            task = asyncio.get_running_loop().create_task(_resolve_and_process())
        except RuntimeError:
            return  # No running event loop
        task.add_done_callback(
            lambda t: (
                self._logger.error("Reaction resolve error", {"error": str(t.exception())}) if t.exception() else None
            )
        )
        if options and options.wait_until:
            options.wait_until(task)

    # ==================================================================
    # Assistant events
    # ==================================================================

    def _handle_assistant_thread_started(self, event: dict[str, Any], options: WebhookOptions | None = None) -> None:
        if not self._chat:
            self._logger.warn("Chat instance not initialized, ignoring assistant_thread_started")
            return

        assistant_thread = event.get("assistant_thread")
        if not assistant_thread:
            self._logger.warn("Malformed assistant_thread_started: missing assistant_thread")
            return

        channel_id = assistant_thread.get("channel_id", "")
        thread_ts = assistant_thread.get("thread_ts", "")
        user_id = assistant_thread.get("user_id", "")
        context = assistant_thread.get("context", {})

        thread_id = self.encode_thread_id(SlackThreadId(channel=channel_id, thread_ts=thread_ts))

        self._chat.process_assistant_thread_started(
            AssistantThreadStartedEvent(
                adapter=self,
                thread_id=thread_id,
                user_id=user_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                context={
                    "channel_id": context.get("channel_id"),
                    "team_id": context.get("team_id"),
                    "enterprise_id": context.get("enterprise_id"),
                    "thread_entry_point": context.get("thread_entry_point"),
                    "force_search": context.get("force_search"),
                },
            ),
            options,
        )

    def _handle_assistant_context_changed(self, event: dict[str, Any], options: WebhookOptions | None = None) -> None:
        if not self._chat:
            self._logger.warn("Chat instance not initialized, ignoring assistant_thread_context_changed")
            return

        assistant_thread = event.get("assistant_thread")
        if not assistant_thread:
            self._logger.warn("Malformed assistant_thread_context_changed: missing assistant_thread")
            return

        channel_id = assistant_thread.get("channel_id", "")
        thread_ts = assistant_thread.get("thread_ts", "")
        user_id = assistant_thread.get("user_id", "")
        context = assistant_thread.get("context", {})

        thread_id = self.encode_thread_id(SlackThreadId(channel=channel_id, thread_ts=thread_ts))

        self._chat.process_assistant_context_changed(
            AssistantContextChangedEvent(
                adapter=self,
                thread_id=thread_id,
                user_id=user_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                context={
                    "channel_id": context.get("channel_id"),
                    "team_id": context.get("team_id"),
                    "enterprise_id": context.get("enterprise_id"),
                    "thread_entry_point": context.get("thread_entry_point"),
                    "force_search": context.get("force_search"),
                },
            ),
            options,
        )

    # ==================================================================
    # App home / member joined
    # ==================================================================

    def _handle_app_home_opened(self, event: dict[str, Any], options: WebhookOptions | None = None) -> None:
        if not self._chat:
            self._logger.warn("Chat instance not initialized, ignoring app_home_opened")
            return

        self._chat.process_app_home_opened(
            AppHomeOpenedEvent(
                adapter=self,
                user_id=event.get("user", ""),
                channel_id=event.get("channel", ""),
            ),
            options,
        )

    def _handle_member_joined_channel(self, event: dict[str, Any], options: WebhookOptions | None = None) -> None:
        if not self._chat:
            self._logger.warn("Chat instance not initialized, ignoring member_joined_channel")
            return

        self._chat.process_member_joined_channel(
            MemberJoinedChannelEvent(
                adapter=self,
                user_id=event.get("user", ""),
                channel_id=self.encode_thread_id(SlackThreadId(channel=event.get("channel", ""), thread_ts="")),
                inviter_id=event.get("inviter"),
            ),
            options,
        )

    def _handle_user_change(self, event: dict[str, Any]) -> None:
        if not self._chat:
            return
        user_info = event.get("user", {})
        user_id = user_info.get("id")
        if user_id:
            try:
                # Fire and forget cache invalidation. The key is built here,
                # under the dispatching request context, so it names the
                # same installation-scoped entry ``_lookup_user`` wrote.
                cache_key = f"slack:user:{self._installation_cache_scope()}{user_id}"
                _pin_task(asyncio.get_running_loop().create_task(self._chat.get_state().delete(cache_key)))
            except RuntimeError:
                pass  # No running event loop
            except Exception as exc:
                self._logger.warn(
                    "Failed to invalidate user cache",
                    {"userId": user_id, "error": exc},
                )

    # ==================================================================
    # Publish Home view / Assistant helpers
    # ==================================================================

    async def publish_home_view(self, user_id: str, view: dict[str, Any]) -> None:
        """Publish a Home tab view for a user."""
        client = self._get_client()
        await client.views_publish(user_id=user_id, view=view)

    async def set_suggested_prompts(
        self,
        channel_id: str,
        thread_ts: str | None,
        prompts: list[dict[str, str]],
        title: str | None = None,
    ) -> None:
        """Set suggested prompts for an assistant thread.

        ``thread_ts`` is optional under the Agent messaging experience
        (agent_view), where prompts sit at the top of the agent conversation
        without a thread; it is omitted from the request when falsy.
        """
        client = self._get_client()
        payload: dict[str, Any] = {
            "channel_id": channel_id,
            "prompts": prompts,
        }
        if thread_ts:
            payload["thread_ts"] = thread_ts
        if title:
            payload["title"] = title
        # Python-specific: go through ``api_call`` (what the generated
        # ``assistant_threads_setSuggestedPrompts`` sends) because that helper
        # requires ``thread_ts`` before slack-sdk 3.43.0 (and is absent in
        # older versions our ``slack-sdk>=3.27.0`` floor still allows).
        await client.api_call(api_method="assistant.threads.setSuggestedPrompts", json=payload)

    async def set_assistant_status(
        self,
        channel_id: str,
        thread_ts: str,
        status: str,
        loading_messages: list[str] | None = None,
    ) -> None:
        """Set status/thinking indicator for an assistant thread."""
        client = self._get_client()
        kwargs: dict[str, Any] = {
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "status": status,
        }
        if loading_messages:
            kwargs["loading_messages"] = loading_messages
        await client.assistant_threads_setStatus(**kwargs)

    async def set_assistant_title(self, channel_id: str, thread_ts: str, title: str) -> None:
        """Set title for an assistant thread (shown in History tab)."""
        client = self._get_client()
        await client.assistant_threads_setTitle(channel_id=channel_id, thread_ts=thread_ts, title=title)

    # ==================================================================
    # Mention resolution
    # ==================================================================

    async def _resolve_inline_mentions(self, text: str) -> str:
        """Resolve inline user/channel mentions to display names.

        Converts ``<@U123>`` to ``<@U123|displayName>`` so downstream parsers
        render them as ``@displayName`` instead of ``@U123``. The bot's own
        mention is decoded too (upstream vercel/chat#891):
        :meth:`_detect_self_mention` classifies it from the raw event before
        the id markup is replaced.
        """
        user_ids: set[str] = set()
        channel_ids: set[str] = set()
        _collect_mention_ids(text, user_ids, channel_ids)
        names = await self._lookup_mention_names(user_ids, channel_ids)
        return _apply_mention_names(text, names)

    async def _lookup_mention_names(self, user_ids: set[str], channel_ids: set[str]) -> _MentionNames:
        """Look up display names for collected mention ids in one parallel wave."""
        if not user_ids and not channel_ids:
            return _MentionNames()

        users = list(user_ids)
        channels = list(channel_ids)
        user_lookups, channel_lookups = await asyncio.gather(
            asyncio.gather(*(self._lookup_user_name(uid) for uid in users)),
            asyncio.gather(*(self._lookup_channel_name(cid) for cid in channels)),
        )
        return _MentionNames(
            users=dict(zip(users, user_lookups, strict=True)),
            channels=dict(zip(channels, channel_lookups, strict=True)),
        )

    async def _lookup_user_name(self, user_id: str) -> str:
        """Look up a user's display name (helper for parallel resolution)."""
        info = await self._lookup_user(user_id)
        return info["display_name"]

    async def _lookup_channel_name(self, channel_id: str) -> str:
        """Look up a channel name (helper for parallel resolution)."""
        return await self._lookup_channel(channel_id)

    # ==================================================================
    # Outgoing mention resolution
    # ==================================================================

    async def _resolve_outgoing_mentions(self, text: str, thread_id: str) -> str:
        """Resolve ``@name`` mentions in text to Slack ``<@USER_ID>`` format.

        Uses the reverse user cache; when several users share a display name,
        prefers the one who participates in *thread_id*. The shared scanner
        (:func:`chat_sdk.shared.mentions.replace_bare_mentions`) skips inline
        code, fenced code, URLs, ``<...>`` tokens and email addresses, and
        matches names with ASCII word characters only (upstream JS
        semantics).
        """
        if not self._chat:
            return text
        state = self._chat.get_state()
        mentions: dict[str, list[str]] = {}

        def collect(mention: str, name: str) -> str:
            if not SLACK_USER_ID_EXACT_PATTERN.match(name):
                mentions.setdefault(name.lower(), [])
            return mention

        replace_bare_mentions(text, collect)

        # Lines without a bare mention never touch state (the native stream
        # path calls this once per committed line).
        if not mentions:
            return text

        # Look up user IDs for each mentioned name
        for name in list(mentions.keys()):
            user_ids = await state.get_list(f"slack:user-by-name:{self._installation_cache_scope()}{name}")
            # Dedup, keeping first-seen order (JS ``[...new Set(ids)]``).
            mentions[name] = list(dict.fromkeys(user_ids))

        # Load thread participants only if needed (ambiguous mentions)
        participants: set[str] | None = None
        if any(len(ids) > 1 for ids in mentions.values()):
            participant_list = await state.get_list(f"slack:thread-participants:{thread_id}")
            participants = set(participant_list)

        def resolve(mention: str, name: str) -> str:
            if SLACK_USER_ID_EXACT_PATTERN.match(name):
                return mention
            user_ids = mentions.get(name.lower())
            if not user_ids:
                return mention
            if len(user_ids) == 1:
                return f"<@{user_ids[0]}>"
            # Disambiguate using thread participants
            if participants:
                in_thread = [uid for uid in user_ids if uid in participants]
                if len(in_thread) == 1:
                    return f"<@{in_thread[0]}>"
            return mention

        return replace_bare_mentions(text, resolve)

    async def _resolve_message_mentions(
        self, message: AdapterPostableMessage, thread_id: str
    ) -> AdapterPostableMessage:
        """Pre-process outgoing message to resolve @name mentions."""
        if not self._chat:
            return message
        if isinstance(message, str):
            return await self._resolve_outgoing_mentions(message, thread_id)
        if hasattr(message, "raw") and isinstance(getattr(message, "raw", None), str):
            resolved = await self._resolve_outgoing_mentions(message.raw, thread_id)  # type: ignore[union-attr]
            return type(message)(**{**message.__dict__, "raw": resolved})  # type: ignore[arg-type]
        if hasattr(message, "markdown") and isinstance(getattr(message, "markdown", None), str):
            resolved = await self._resolve_outgoing_mentions(message.markdown, thread_id)  # type: ignore[union-attr]
            return type(message)(**{**message.__dict__, "markdown": resolved})  # type: ignore[arg-type]
        return message

    # ==================================================================
    # Link extraction
    # ==================================================================

    def _extract_links(self, event: dict[str, Any]) -> list[LinkPreview]:
        """Extract link URLs from a Slack event.

        Also merges any inline unfurl metadata that Slack already attached to
        this same event (legacy ``attachments`` array). Cross-event unfurl
        metadata (delivered later via ``message_changed``) is merged
        asynchronously via :meth:`_enrich_links`.
        """
        urls: set[str] = set()

        for block in event.get("blocks", []):
            if block.get("type") == "rich_text" and block.get("elements"):
                for section in block["elements"]:
                    for element in section.get("elements", []):
                        if element.get("type") == "link" and element.get("url"):
                            urls.add(element["url"])

        if not urls and event.get("text"):
            for match in _BRACKETED_URL_PATTERN.finditer(event["text"]):
                raw = match.group(1)
                pipe_idx = raw.find("|")
                urls.add(raw[:pipe_idx] if pipe_idx >= 0 else raw)

        # Build unfurl metadata index from inline (same-event) attachments.
        unfurls: dict[str, dict[str, str | None]] = {}
        for att in event.get("attachments") or []:
            if not isinstance(att, dict):
                continue
            att_url = att.get("from_url") or att.get("original_url")
            if att_url and (att.get("title") or att.get("text")):
                unfurls[att_url] = {
                    "title": att.get("title"),
                    "description": att.get("text"),
                    "image_url": att.get("image_url") or att.get("thumb_url"),
                    "site_name": att.get("service_name"),
                }
                urls.add(att_url)
            # Alert attachments link their title (e.g. the Sentry issue URL);
            # surface it so handlers can reach what the Slack UI links to.
            # Upstream parity (adapter-slack/src/index.ts:4466-4470): the
            # preview carries no title, so on a webhook ``_enrich_links`` may
            # wait for an unfurl like it does for any untitled link. Fetched
            # (history) messages wait too, but only because of the Python
            # ``_unfurl_channel_for`` fallback (upstream returns at once there,
            # index.ts:4704); that applies to every untitled link and is
            # tracked with that divergence in docs/UPSTREAM_SYNC.md (#292).
            title_link = att.get("title_link")
            if isinstance(title_link, str) and title_link and not _is_foreign_attachment(att):
                urls.add(title_link)

        previews: list[LinkPreview] = []
        for url in urls:
            preview = self._create_link_preview(url)
            # TS uses ``url.replace(TRAILING_SLASH_PATTERN, "")`` (no ``g``
            # flag) which strips a single trailing ``/``. Python's
            # ``re.sub`` defaults to replacing all occurrences, so we
            # pin ``count=1`` for parity. (Practically the regex anchors
            # at end-of-string so only one match exists, but locking
            # this in prevents drift if the pattern ever loosens.)
            unfurl = (
                unfurls.get(url) or unfurls.get(_TRAILING_SLASH_PATTERN.sub("", url, count=1)) or unfurls.get(f"{url}/")
            )
            if unfurl:
                preview = self._merge_unfurl_into_preview(preview, unfurl)
            previews.append(preview)
        return previews

    @staticmethod
    def _merge_unfurl_into_preview(preview: LinkPreview, unfurl: dict[str, str | None]) -> LinkPreview:
        """Return a new LinkPreview with unfurl metadata merged in.

        Mirrors the TS spread ``{ ...preview, ...unfurl }``: the unfurl
        values OVERRIDE the preview's ``description`` / ``image_url`` /
        ``site_name`` (the unfurled attachment is the authoritative
        source). ``title`` is short-circuited by callers (``_enrich_links``
        skips merging when the preview already has a title), but for the
        same-event ``_extract_links`` path the unfurl's title also wins
        when present. ``fetch_message`` is never present on the unfurl
        and is preserved from the preview.
        """
        return LinkPreview(
            url=preview.url,
            title=unfurl.get("title") if unfurl.get("title") is not None else preview.title,
            description=unfurl.get("description") if unfurl.get("description") is not None else preview.description,
            image_url=unfurl.get("image_url") if unfurl.get("image_url") is not None else preview.image_url,
            site_name=unfurl.get("site_name") if unfurl.get("site_name") is not None else preview.site_name,
            fetch_message=preview.fetch_message,
        )

    def _handle_message_changed(self, event: dict[str, Any], options: WebhookOptions | None = None) -> None:
        """Handle a ``message_changed`` event (upstream ``handleMessageChanged``).

        Caches link-unfurl metadata as a side step (Slack delivers unfurls by
        editing the original message), then dispatches real edits to
        ``process_message_updated``. Unfurl-only updates, hidden thread
        metadata updates, language-detection updates with no content change
        and ``tombstone`` replacements are not reported as edits.
        """
        inner = event.get("message")
        channel = event.get("channel")
        if not (isinstance(inner, dict) and channel):
            return

        normalized = _with_inherited(
            inner,
            channel=channel,
            channel_type=event.get("channel_type"),
            team=event.get("team"),
            team_id=event.get("team_id"),
            type="message",
        )

        # Slack does not document ``tombstone`` and upstream has no captured
        # payload for it, so it is not claimed to mean "deleted". It arrives
        # with a ``previous_message`` and changed text, so it would otherwise
        # pass the hidden-edit check below.
        if inner.get("subtype") == "tombstone":
            self._logger.debug("Ignoring tombstone message_changed")
            return

        self._cache_unfurls_from_message_changed(channel, inner)

        previous = event.get("previous_message")
        has_previous = isinstance(previous, dict)
        is_hidden_message_edit = isinstance(previous, dict) and (
            _edited_ts(inner) != _edited_ts(previous) or inner.get("text") != previous.get("text")
        )

        # Slack link unfurls arrive as hidden message_changed events. Some
        # real edits are hidden too, but they carry a previous snapshot and
        # changed content/edit metadata. Hidden thread metadata updates after
        # deletes carry neither change, so they are ignored here.
        if event.get("hidden") is True and not is_hidden_message_edit:
            return

        # Slack's automatic language detection updates locale metadata and
        # dispatches message_changed without touching the message.
        if has_previous and not is_hidden_message_edit:
            self._logger.debug("Ignoring message_changed with no content change")
            return

        if not (self._chat and normalized.get("channel") and normalized.get("ts")):
            return

        # Divergence from upstream — see docs/UPSTREAM_SYNC.md. Core skips the
        # bot's own edits only after resolving the message factory (user
        # lookup, participant write, up to _UNFURL_WAIT_MS of unfurl polling),
        # and post+edit / native streaming emit one message_changed per
        # update. The same check core applies (``author.is_me`` comes from
        # ``_is_message_from_self``) is made here first; handlers see the same.
        if self._is_message_from_self(normalized):
            self._logger.debug("Skipping message_changed from self")
            return

        thread_id = self._thread_id_for_message_event(normalized)

        async def parse_message() -> Message:
            return await self._parse_slack_message(normalized, thread_id)

        parse_previous: Callable[[], Awaitable[Message]] | None = None
        if isinstance(previous, dict):
            before = previous

            # Parse the pre-edit snapshot through the same async path as the
            # new message so mentions render identically on both sides.
            # Upstream parity (chat@4.41.1 adapter-slack index.ts:3670-3675):
            # the snapshot inherits only channel / channel_type / type, not
            # team / team_id (unlike ``normalized`` and the delete path).
            async def _parse_previous() -> Message:
                snapshot = _with_inherited(
                    before,
                    channel=normalized.get("channel"),
                    channel_type=normalized.get("channel_type"),
                    type="message",
                )
                try:
                    return await self._parse_slack_message(snapshot, thread_id)
                except Exception as exc:
                    # Never let a lookup failure on the old snapshot drop the
                    # edit. ``Exception`` lets ``CancelledError`` propagate.
                    self._logger.warn(
                        "Falling back to sync parse for pre-edit message",
                        {"error": exc, "threadId": thread_id},
                    )
                    return self._parse_slack_message_sync(snapshot, thread_id)

            parse_previous = _parse_previous

        self._chat.process_message_updated(
            self,
            thread_id,
            parse_message,
            previous_message=parse_previous,
            options=options,
        )

    def _cache_unfurls_from_message_changed(self, channel: str, inner: dict[str, Any]) -> None:
        """Cache link-unfurl metadata carried by a ``message_changed`` inner message.

        Stored keyed by installation, channel and inner ``ts`` so
        :meth:`_enrich_links` can pick it up for the original message. A side
        step only: ``_handle_message_changed`` continues either way.
        """
        attachments = inner.get("attachments")
        if not isinstance(attachments, list):
            return
        has_unfurls = any(
            isinstance(att, dict) and (att.get("from_url") or att.get("original_url")) for att in attachments
        )
        ts = inner.get("ts")
        if not (has_unfurls and self._chat and ts):
            return

        self._logger.debug(
            "Processing message_changed for link unfurls",
            {"channel": channel, "ts": ts, "attachmentCount": len(attachments)},
        )

        unfurls: dict[str, dict[str, str | None]] = {}
        for att in attachments:
            if not isinstance(att, dict):
                continue
            att_url = att.get("from_url") or att.get("original_url")
            if att_url and (att.get("title") or att.get("text")):
                unfurls[att_url] = {
                    "title": att.get("title"),
                    "description": att.get("text"),
                    "image_url": att.get("image_url") or att.get("thumb_url"),
                    "site_name": att.get("service_name"),
                }

        if not unfurls:
            return

        # Keyed by installation, channel and ts (vercel/chat#877) so unfurl
        # metadata from one workspace/channel is never merged into another's
        # message that happens to share the same ``ts``.
        unfurl_key = self._unfurl_cache_key(channel, ts)

        async def _store() -> None:
            try:
                await self._chat.get_state().set(  # type: ignore[union-attr]
                    unfurl_key,
                    unfurls,
                    _UNFURL_CACHE_TTL_MS,
                )
            except Exception as exc:
                self._logger.error("Failed to cache unfurl metadata", {"error": exc})

        try:
            task = asyncio.get_running_loop().create_task(_store())
            task.add_done_callback(
                lambda t: (
                    self._logger.error("Unfurl cache task failed", {"error": t.exception()}) if t.exception() else None
                )
            )
        except RuntimeError:
            # No running loop (sync test context) — skip silently.
            self._logger.debug("No running loop; skipping unfurl cache write")

    def _handle_message_deleted(self, event: dict[str, Any], options: WebhookOptions | None = None) -> None:
        """Dispatch a ``message_deleted`` event to ``process_message_deleted``.

        Upstream ``handleMessageDeleted``. The deleted ts is the first
        non-``None`` of ``deleted_ts``, ``message.ts`` and
        ``previous_message.ts`` (upstream ``??``).
        """
        inner = event.get("message")
        previous_raw = event.get("previous_message")
        deleted_ts = event.get("deleted_ts")
        if deleted_ts is None and isinstance(inner, dict):
            deleted_ts = inner.get("ts")
        if deleted_ts is None and isinstance(previous_raw, dict):
            deleted_ts = previous_raw.get("ts")
        channel = event.get("channel")
        if not (self._chat and channel and deleted_ts):
            return

        previous: dict[str, Any] | None = None
        if isinstance(previous_raw, dict):
            previous = _with_inherited(
                previous_raw,
                channel=channel,
                channel_type=event.get("channel_type"),
                team=event.get("team"),
                team_id=event.get("team_id"),
                type="message",
            )

        previous_ts = previous.get("ts") if previous is not None else None
        thread_id = self._thread_id_for_message_event(
            {
                "channel": channel,
                "channel_type": previous.get("channel_type") if previous is not None else event.get("channel_type"),
                "thread_ts": previous.get("thread_ts") if previous is not None else None,
                "ts": previous_ts if previous_ts is not None else deleted_ts,
            }
        )
        event_ts = event.get("event_ts")
        deleted_at = self._parse_slack_timestamp(event_ts if event_ts is not None else event.get("ts"))

        self._chat.process_message_deleted(
            MessageDeletedEvent(
                adapter=self,
                channel_id=channel,
                deleted_at=deleted_at,
                message_id=deleted_ts,
                previous_message=(
                    self._parse_slack_message_sync(previous, thread_id) if previous is not None else None
                ),
                raw=event,
                thread_id=thread_id,
            ),
            options,
        )

    async def _enrich_links(
        self,
        links: list[LinkPreview],
        channel_id: str | None,
        message_ts: str | None,
    ) -> list[LinkPreview]:
        """Enrich ``links`` with unfurl metadata from a ``message_changed`` cache.

        Polls the state cache for up to ``_UNFURL_WAIT_MS`` to give Slack
        time to deliver the cross-event ``message_changed`` payload.
        Returns the original list (untouched) when there is nothing to wait
        for, or when the channel or ts needed to build the scoped cache key
        is missing.
        """
        if not (self._chat and channel_id and message_ts) or not links:
            return links

        all_have_metadata = all((link.title is not None) or (link.fetch_message is not None) for link in links)
        if all_have_metadata:
            return links

        deadline = time.monotonic() + (_UNFURL_WAIT_MS / 1000.0)
        state = self._chat.get_state()
        stored: dict[str, dict[str, str | None]] | None = None
        unfurl_key = self._unfurl_cache_key(channel_id, message_ts)
        while True:
            try:
                stored = await state.get(unfurl_key)
            except Exception as exc:
                self._logger.warn(
                    "Failed to read unfurl data from state",
                    {"error": str(exc), "message_ts": message_ts},
                )
                return links
            if stored or time.monotonic() >= deadline:
                break
            await asyncio.sleep(_UNFURL_POLL_MS / 1000.0)

        if not stored:
            return links

        out: list[LinkPreview] = []
        for link in links:
            if link.title is not None:
                out.append(link)
                continue
            unfurl = (
                stored.get(link.url)
                or stored.get(_TRAILING_SLASH_PATTERN.sub("", link.url, count=1))
                or stored.get(f"{link.url}/")
            )
            if unfurl:
                out.append(self._merge_unfurl_into_preview(link, unfurl))
            else:
                out.append(link)
        return out

    def _create_link_preview(self, url: str) -> LinkPreview:
        """Create a LinkPreview for a URL.

        If the URL points to a Slack message, includes a ``fetch_message``
        callback.
        """
        match = SLACK_MESSAGE_URL_PATTERN.match(url)
        if not match:
            return LinkPreview(url=url)

        channel = match.group(1)
        raw_ts = match.group(2)
        ts = f"{raw_ts[: len(raw_ts) - 6]}.{raw_ts[len(raw_ts) - 6 :]}"
        thread_id = self.encode_thread_id(SlackThreadId(channel=channel, thread_ts=ts))

        async def fetch_message() -> Message:
            client = self._get_client()
            result = await client.conversations_history(channel=channel, latest=ts, inclusive=True, limit=1)
            messages = result.get("messages", [])
            target = next((m for m in messages if m.get("ts") == ts), None)
            if not target:
                raise RuntimeError(f"Message not found: {url}")
            return await self._parse_slack_message(target, thread_id)

        return LinkPreview(url=url, fetch_message=fetch_message)

    # ==================================================================
    # Message parsing
    # ==================================================================

    def _detect_self_mention(
        self,
        event: dict[str, Any],
        raw_text: str,
        attachments: list[_AttachmentContent],
    ) -> bool | None:
        """Whether the message invokes the bot (upstream ``detectSelfMention``).

        Slack fires ``app_mention`` for a bot id that only appears inside
        code, so the invocation is classified from the message content: a
        ``user`` element for the bot outside code, or a ``<@U…>`` token
        outside code in mrkdwn content, is a mention. Inline (``style.code``)
        and preformatted content renders literally, so a bot id there is not.

        Returns ``True`` for an invocation and ``False`` when the content
        refers to the bot only literally, or not at all. An ``app_mention``
        whose content never shows the known bot id is still trusted (Slack
        saw a mention under an id the adapter does not know, such as an
        Enterprise Grid ``W…`` id). Without any bot id, ``app_mention`` is
        trusted the same way and other messages return ``None`` so the core
        text-based fallback decides. Never collapse the tri-state with ``or``.
        """
        # Request-scoped id first (multi-workspace), then the configured one.
        bot_user_id = self.bot_user_id
        if not bot_user_id:
            return True if event.get("type") == "app_mention" else None

        matcher = _mention_matcher(bot_user_id)
        literal = False

        def found(evidence: _MentionEvidence) -> bool:
            nonlocal literal
            if evidence == "literal":
                literal = True
            return evidence == "mention"

        raw_blocks = event.get("blocks")
        blocks = raw_blocks if isinstance(raw_blocks, list) else []
        # Blocks model the message body. The flattened ``text`` field loses
        # the code/literal distinction, so it is only consulted without them.
        body = _classify_blocks_mention(blocks, matcher) if blocks else _classify_mrkdwn_mention(raw_text, matcher)
        if found(body):
            return True

        for attachment in attachments:
            if attachment.blocks:
                # Structured attachment content is authoritative for that
                # attachment, so its legacy fallback cannot add evidence.
                if found(_classify_blocks_mention(attachment.blocks, matcher)):
                    return True
                continue
            for part in attachment.parts:
                if found(_classify_attachment_part(part, matcher)):
                    return True

        # An ``app_mention`` no literal token explains still stands. Otherwise
        # Slack mentions are user-id tokens: with the content inspected, the
        # absence of one is a definitive non-mention, so a display name in
        # plain or code-styled text cannot fall through to name matching.
        return event.get("type") == "app_mention" and not literal

    def _author_fields(self, event: dict[str, Any]) -> tuple[str, bool]:
        """``(user_id, is_system)`` for a message author (both parse paths).

        Bot messages carry the bot *user* id in ``bot_profile.user_id``;
        prefer it to the app-level ``bot_id`` (upstream vercel/chat#883).
        ``USLACK`` is Slack's own system user (upstream vercel/chat#707).
        """
        user = event.get("user")
        user_id = user or _bot_profile_user_id(event) or event.get("bot_id") or "unknown"
        return user_id, user == _SLACK_SYSTEM_USER_ID

    async def _parse_slack_message(
        self,
        event: dict[str, Any],
        thread_id: str,
    ) -> Message:
        """Parse a Slack event into a normalized Message (async with user lookup)."""
        is_me = self._is_message_from_self(event)
        raw_text = event.get("text") or ""

        user_name = event.get("username", "unknown")
        full_name = event.get("username", "unknown")
        email: str | None = None

        if event.get("user") and not event.get("username"):
            user_info = await self._lookup_user(event["user"])
            user_name = user_info["display_name"]
            full_name = user_info["real_name"]
            email = user_info.get("email")

        # Track thread participants
        if event.get("user") and self._chat:
            try:
                participant_key = f"slack:thread-participants:{thread_id}"
                participants = await self._chat.get_state().get_list(participant_key)
                if event["user"] not in participants:
                    await self._chat.get_state().append_to_list(
                        participant_key,
                        event["user"],
                        max_length=100,
                        ttl_ms=_REVERSE_INDEX_TTL_MS,
                    )
            except Exception as exc:
                self._logger.warn(
                    "Failed to track thread participant",
                    {"threadId": thread_id, "userId": event.get("user"), "error": exc},
                )

        # Classify the bot's own mention from the raw event before resolution
        # replaces the id markup with display names.
        attachments = [_attachment_content(a) for a in _author_attachments(event)]
        is_mention = self._detect_self_mention(event, raw_text, attachments)

        # Resolve inline @mentions (the bot's own included) to display names,
        # then fold in tables and attachment content (their mentions resolve
        # in one more lookup wave).
        text = await self._resolve_inline_mentions(raw_text)
        formatted, plain_text = await self._resolved_content(event, text, attachments)
        author_id, is_system = self._author_fields(event)

        ts_str = event.get("ts", "0")
        try:
            date_sent = datetime.fromtimestamp(float(ts_str), tz=timezone.utc)
        except (ValueError, TypeError, OSError):
            date_sent = datetime.now(tz=timezone.utc)

        edited_at: datetime | None = None
        if event.get("edited"):
            try:
                edited_at = datetime.fromtimestamp(float(event["edited"].get("ts", "0")), tz=timezone.utc)
            except (ValueError, TypeError, OSError):
                edited_at = None

        return Message(
            id=event.get("ts", ""),
            thread_id=thread_id,
            text=plain_text,
            formatted=formatted,
            raw=event,
            is_mention=is_mention,
            author=Author(
                user_id=author_id,
                user_name=user_name,
                full_name=full_name,
                is_bot=bool(event.get("bot_id")),
                is_me=is_me,
                email=email,
                is_system=is_system,
            ),
            metadata=MessageMetadata(
                date_sent=date_sent,
                edited=bool(event.get("edited")),
                edited_at=edited_at,
            ),
            attachments=[
                self._create_attachment(f, team_id=event.get("team") or event.get("team_id"))
                for f in event.get("files", [])
            ],
            # ``_enrich_links`` polls the unfurl cache for up to
            # ``_UNFURL_WAIT_MS`` (2000 ms) before giving up, so every
            # message containing a not-yet-unfurled link adds up to
            # ~2s of latency to message handling worst-case (it returns
            # immediately when the cache is already populated or when
            # there are no links to enrich).
            links=await self._enrich_links(
                self._extract_links(event),
                self._unfurl_channel_for(event, thread_id),
                event.get("ts"),
            ),
        )

    def _unfurl_channel_for(self, event: dict[str, Any], thread_id: str) -> str | None:
        """Channel for the unfurl cache key of a message being parsed.

        Webhook events carry ``channel``, but messages returned by
        ``conversations.history`` / ``conversations.replies`` (``fetch_message``,
        ``fetch_messages``, channel history, ``list_threads``) do not. Those are
        always parsed with a ``thread_id`` built from the channel they were
        fetched from, so fall back to it; otherwise ``_enrich_links`` would
        return early and fetched messages would lose the unfurl metadata that
        ``message_changed`` cached for them.

        Python divergence: upstream passes ``event.channel`` only (4.40+),
        which silently drops enrichment for fetched messages.
        """
        channel = event.get("channel")
        if isinstance(channel, str) and channel:
            return channel
        try:
            decoded = self.decode_thread_id(thread_id)
        except ValidationError:
            return None
        return decoded.channel or None

    def _parse_slack_message_sync(self, event: dict[str, Any], thread_id: str) -> Message:
        """Synchronous message parsing (no user lookup, falls back to user ID)."""
        is_me = self._is_message_from_self(event)
        text = event.get("text") or ""
        attachments = [_attachment_content(a) for a in _author_attachments(event)]
        # Classified the same way as the async path, so an edit's pre-edit
        # snapshot cannot disagree with the edited message about it.
        is_mention = self._detect_self_mention(event, text, attachments)
        formatted, plain_text = self._content(event, text, attachments)
        author_id, is_system = self._author_fields(event)
        user_name = event.get("username") or event.get("user") or "unknown"
        full_name = event.get("username") or event.get("user") or "unknown"

        ts_str = event.get("ts", "0")
        try:
            date_sent = datetime.fromtimestamp(float(ts_str), tz=timezone.utc)
        except (ValueError, TypeError, OSError):
            date_sent = datetime.now(tz=timezone.utc)

        edited_at: datetime | None = None
        if event.get("edited"):
            try:
                edited_at = datetime.fromtimestamp(float(event["edited"].get("ts", "0")), tz=timezone.utc)
            except (ValueError, TypeError, OSError):
                edited_at = None

        return Message(
            id=event.get("ts", ""),
            thread_id=thread_id,
            text=plain_text,
            formatted=formatted,
            raw=event,
            is_mention=is_mention,
            author=Author(
                user_id=author_id,
                user_name=user_name,
                full_name=full_name,
                is_bot=bool(event.get("bot_id")),
                is_me=is_me,
                is_system=is_system,
            ),
            metadata=MessageMetadata(
                date_sent=date_sent,
                edited=bool(event.get("edited")),
                edited_at=edited_at,
            ),
            attachments=[
                self._create_attachment(f, team_id=event.get("team") or event.get("team_id"))
                for f in event.get("files", [])
            ],
            links=self._extract_links(event),
        )

    # ==================================================================
    # Message content (body, tables, attachments)
    # ==================================================================

    def _content(
        self,
        event: dict[str, Any],
        text: str,
        attachments: list[_AttachmentContent] | None = None,
    ) -> tuple[FormattedContent, str]:
        """The message's AST (body text, its tables and attachment content) and plain text.

        Upstream ``content`` (sync, no mention lookups). See
        :meth:`_assemble_content` for how the plain text is derived.
        """
        if attachments is None:
            attachments = [_attachment_content(a) for a in _author_attachments(event)]
        return self._assemble_content(
            text,
            _event_tables(event),
            [node for attachment in attachments for node in self._attachment_nodes(attachment)],
        )

    async def _resolved_content(
        self,
        event: dict[str, Any],
        text: str,
        attachments: list[_AttachmentContent] | None = None,
    ) -> tuple[FormattedContent, str]:
        """Like :meth:`_content`, resolving mentions in cells and attachments.

        Upstream ``resolvedContent``. User and channel mentions inside table
        cells and attachment parts resolve the way
        :meth:`_resolve_inline_mentions` resolves body text. All ids are
        collected up front and looked up in a single parallel wave, so no id
        is fetched twice.
        """
        if attachments is None:
            attachments = [_attachment_content(a) for a in _author_attachments(event)]
        tables = _event_tables(event)
        user_ids, channel_ids = _content_mention_ids(tables, attachments)
        names = await self._lookup_mention_names(user_ids, channel_ids)

        def resolve_table(data: _TableData) -> _TableData:
            rows = [[_apply_mention_names(cell, names) if cell else cell for cell in row] for row in data.rows]
            return replace(data, rows=rows)

        def resolve_part(part: _AttachmentPart) -> _AttachmentPart:
            return replace(part, text=_apply_mention_names(part.text, names))

        nodes: list[Content] = []
        for attachment in attachments:
            resolved = replace(
                attachment,
                parts=[resolve_part(part) for part in attachment.parts],
                tables=[resolve_table(data) for data in attachment.tables],
            )
            nodes.extend(self._attachment_nodes(resolved))
        return self._assemble_content(
            text,
            _EventTables(
                leading=[resolve_table(data) for data in tables.leading],
                trailing=[resolve_table(data) for data in tables.trailing],
            ),
            nodes,
        )

    def _assemble_content(
        self,
        text: str,
        tables: _EventTables,
        attachment_nodes: list[Content],
    ) -> tuple[FormattedContent, str]:
        """Body AST with leading tables above it, then trailing tables and attachments.

        Returns ``(formatted, plain_text)``. Upstream derives ``message.text``
        as ``toPlainText(formatted)``. Until #283 ports upstream's inbound
        mrkdwn normalization (``slackMrkdwnToMarkdown``), our ``to_ast`` drops
        code on a fence's opening line (the body ``"```npm test```"`` parses to
        nothing), so the body's share of the plain text keeps the regex
        ``extract_plain_text`` used before #210, and only the table and
        attachment nodes go through ``ast_to_plain_text``. A message without
        tables or attachments gets exactly the pre-#210 text. Otherwise the
        pieces are joined with a blank line, skipping empty ones, as
        ``toPlainText`` joins root children (the body loses trailing
        whitespace there, as a parsed paragraph would). Temporary divergence:
        #283 replaces this with ``ast_to_plain_text(formatted)``.
        """
        before = [self._table_node(data) for data in tables.leading]
        after = [*(self._table_node(data) for data in tables.trailing), *attachment_nodes]
        formatted = self._format_converter.to_ast(text)
        formatted["children"] = [*before, *formatted.get("children", []), *after]
        body = self._format_converter.extract_plain_text(text)
        if not (before or after):
            return formatted, body
        pieces = [
            *(ast_to_plain_text(node) for node in before),
            body.rstrip(JS_WHITESPACE),
            *(ast_to_plain_text(node) for node in after),
        ]
        return formatted, "\n\n".join(piece for piece in pieces if piece)

    def _attachment_nodes(self, content: _AttachmentContent) -> list[Content]:
        """Render one attachment's content to block nodes (upstream ``attachmentNodes``).

        Literal lines share a paragraph (separated by hard breaks) so an
        attachment reads as one block; mrkdwn parts are parsed in isolation so
        an unclosed code fence in the body or another attachment can't swallow
        this one's content. Tables from the attachment's blocks follow its
        text, keeping each attachment's content together.
        """
        nodes: list[Content] = []
        lines: list[list[Content]] = []

        def flush() -> None:
            if not lines:
                return
            children: list[Content] = []
            for line in lines:
                if children:
                    children.append({"type": "break"})
                children.extend(line)
            nodes.append({"type": "paragraph", "children": children})
            lines.clear()

        for part in content.parts:
            if part.mrkdwn:
                flush()
                nodes.extend(self._format_converter.to_ast(part.text).get("children", []))
                continue
            for line in part.text.split("\n"):
                if line.strip(JS_WHITESPACE):
                    lines.append(_literal_phrasing(line))
                else:
                    # A blank line inside a literal part starts a new paragraph
                    flush()
        flush()
        nodes.extend(self._table_node(data) for data in content.tables)
        return nodes

    def _table_node(self, data: _TableData) -> Content:
        """An mdast ``table`` for *data* (upstream ``tableNode``)."""
        rows: list[Content] = [
            {
                "type": "tableRow",
                "children": [{"type": "tableCell", "children": self._cell_children(cell)} for cell in row],
            }
            for row in data.rows
        ]
        if data.headerless:
            width = max(len(row) for row in data.rows)
            header = {"type": "tableRow", "children": [{"type": "tableCell", "children": []} for _ in range(width)]}
            rows.insert(0, header)
        return {"type": "table", "children": rows}

    def _cell_children(self, cell: str) -> list[Content]:
        """Phrasing content for a cell's mrkdwn (upstream ``cellChildren``).

        Uses the same format converter as body text, so mentions, links and
        emoji render consistently. Non-paragraph blocks flatten to plain text.
        """
        if not cell:
            return []
        children: list[Content] = []
        for node in self._format_converter.to_ast(cell).get("children", []):
            if children:
                children.append({"type": "text", "value": "\n"})
            if node.get("type") == "paragraph":
                children.extend(node.get("children", []))
            else:
                children.append({"type": "text", "value": ast_to_plain_text({"type": "root", "children": [node]})})
        return children

    @staticmethod
    def _parse_slack_timestamp(ts: str | None) -> datetime | None:
        """Parse a Slack ``ts`` (``"1700000000.123456"``) into an aware UTC datetime.

        Upstream ``parseSlackTimestamp``: ``None`` for a missing, empty or
        non-numeric value (JS ``NaN`` -> ``undefined``).
        """
        if not ts:
            return None
        try:
            return datetime.fromtimestamp(float(ts), tz=timezone.utc)
        except (ValueError, TypeError, OverflowError, OSError):
            return None

    def _create_attachment(self, file: dict[str, Any], team_id: str | None = None) -> Attachment:
        """Create an Attachment from a Slack file object.

        ``team_id`` identifies the workspace the file belongs to and is
        stored in ``fetch_metadata`` so :meth:`rehydrate_attachment` can
        rebuild the download closure (with workspace-specific token) after
        the queue/debounce path JSON-serializes the message.
        """
        url = file.get("url_private")
        # Capture per-request context (token + Enterprise Grid info) from the
        # active webhook context so ``fetch_data`` can run later without being
        # inside the ContextVar frame (e.g. after the message has been queued
        # + rehydrated), and ``rehydrate_attachment`` can resolve tokens
        # through the same lookup logic on a different process invocation.
        # For single-workspace mode the default provider is re-resolved at
        # fetch time so dynamic ``bot_token`` resolvers honor rotation.
        ctx = self._request_context.get()
        ctx_token: str | None = ctx.token if ctx and ctx.token else None
        ctx_enterprise_id: str | None = ctx.enterprise_id if ctx else None
        ctx_is_enterprise_install: bool = bool(ctx.is_enterprise_install) if ctx else False

        mimetype = file.get("mimetype", "")
        att_type: str = "file"
        if mimetype.startswith("image/"):
            att_type = "image"
        elif mimetype.startswith("video/"):
            att_type = "video"
        elif mimetype.startswith("audio/"):
            att_type = "audio"

        async def fetch_data() -> bytes:
            # The token is resolved lazily by ``_fetch_slack_file``, and only
            # when the URL is on a Slack auth origin (vercel/chat 7c269653).
            token: SlackBotToken = ctx_token if ctx_token is not None else self._resolve_token_async
            return await self._fetch_slack_file(cast(str, url), token)

        fetch_meta: dict[str, str] = {}
        if url:
            fetch_meta["url"] = url
        if team_id:
            fetch_meta["teamId"] = team_id
        # Omit the Enterprise Grid keys entirely when absent (hazard #7:
        # omitted keys, not serialized ``None``/false values).
        if ctx_enterprise_id:
            fetch_meta["enterpriseId"] = ctx_enterprise_id
        if ctx_is_enterprise_install:
            fetch_meta["isEnterpriseInstall"] = "true"

        return Attachment(
            type=att_type,  # type: ignore[arg-type]
            url=url,
            name=file.get("name"),
            mime_type=file.get("mimetype"),
            size=file.get("size"),
            width=file.get("original_w"),
            height=file.get("original_h"),
            fetch_data=fetch_data if url else None,
            fetch_metadata=fetch_meta or None,
        )

    @staticmethod
    def _is_trusted_slack_download_url(url: str, api_url: str | None = None) -> bool:
        """Gate Slack file downloads to known Slack-owned hosts.

        We refuse to start a download (and so to resolve or forward the bot
        token) from an arbitrary URL. After ``rehydrate_attachment``
        reconstructs the fetch closure from serialized ``fetch_metadata``,
        that URL may have been tampered with in the state store.

        This is a Python-first divergence: upstream fetches other URLs
        without credentials instead of refusing them. The host list is shared
        with the ``api`` subpath (:func:`is_trusted_slack_file_url`): https
        Slack file hosts (commercial, GovSlack, ``slack-files``), Slack-owned
        subdomains, and the configured ``api_url`` origin. See
        ``docs/UPSTREAM_SYNC.md`` Known Non-Parity.
        """
        return is_trusted_slack_file_url(url, api_url=api_url)

    @staticmethod
    def _is_slack_auth_url(url: str, api_url: str | None = None) -> bool:
        """Whether ``url`` may receive the bot token (upstream ``isSlackAuthUrl``).

        Exact-origin match against the Slack file/API origins (commercial and
        GovSlack) plus the ``api_url`` origin; see :func:`is_slack_auth_url`.
        """
        return is_slack_auth_url(url, api_url)

    async def _fetch_slack_file(self, url: str, token: SlackBotToken) -> bytes:
        """Download a Slack file through the guarded downloader.

        Port of upstream ``fetchSlackFile`` (vercel/chat ``b6fa24c6`` #865,
        ``7c269653`` #859, ``6adca361`` #916). ``token`` is a string or a
        zero-arg (sync or async) resolver, resolved only when ``url`` is on
        a Slack auth origin. The bot token is then attached per redirect hop,
        only to hops whose origin is a Slack auth origin, so a redirect never
        carries it to another host. The shared downloader refuses internal
        addresses, caps the decoded body at 25 MB and bounds the whole
        download (every hop and the body) at 30 s. An HTML response (Slack's
        login page when the app lacks ``files:read``) is rejected before the
        body is read. Failures raise :class:`NetworkError`.

        Shared by :meth:`_create_attachment` (direct fetch closure) and
        :meth:`rehydrate_attachment` (reconstructed closure after JSON
        roundtrip).
        """
        api_url = self._slack_api_url
        # The URL as the downloader will request it (WHATWG-normalized), so the
        # allowlist and token decisions see the same host the request goes to.
        try:
            target: str | None = validate_attachment_url(url, "slack")
        except NetworkError:
            target = None
        # Divergence from upstream — see docs/UPSTREAM_SYNC.md: a URL off the
        # Slack allowlist is refused before any token resolution or I/O.
        if target is None or not self._is_trusted_slack_download_url(target, api_url):
            raise ValidationError(
                "slack",
                f"Refusing to fetch Slack file from untrusted URL: {url}",
            )

        value: str | None = None
        if self._is_slack_auth_url(target, api_url):
            value = await resolve_slack_bot_token(token)

        def headers(target: str) -> dict[str, str] | None:
            # The bot token is sent only on hops to trusted Slack origins, so a
            # redirect cannot carry it to another host.
            if value and self._is_slack_auth_url(target, api_url):
                return {"authorization": f"Bearer {value}"}
            return None

        def on_response(response: AttachmentResponse) -> None:
            content_type = next(
                (item for key, item in response.headers.items() if key.lower() == "content-type"),
                "",
            )
            if "text/html" in content_type:
                raise NetworkError(
                    "slack",
                    "Failed to download file from Slack: received HTML login page instead of file data. "
                    'Ensure your Slack app has the "files:read" OAuth scope. '
                    f"URL: {url}",
                )

        try:
            return await download_attachment(
                target,
                adapter="slack",
                headers=headers,
                transport=self._create_file_transport(),
                on_response=on_response,
            )
        except NetworkError:
            raise
        except Exception as error:
            raise NetworkError("slack", "Failed to fetch Slack file", error) from error

    def _create_file_transport(self) -> AttachmentTransport | None:
        """Transport for guarded file downloads (upstream ``createFileTransport``).

        Returns the configured ``file_transport`` (``None`` selects the
        downloader's DNS-pinned aiohttp default). Subclasses can override it
        to return a custom :class:`AttachmentTransport`, which takes
        precedence over the configured one.
        """
        return self._file_transport

    def rehydrate_attachment(self, attachment: Attachment) -> Attachment:
        """Reconstruct ``fetch_data`` on a deserialized Slack attachment.

        Matches the upstream TS implementation: looks up the download URL
        (and optional ``teamId`` for multi-workspace installations) from
        ``attachment.fetch_metadata``, and rebuilds a ``fetch_data`` closure
        that resolves the workspace-specific bot token at call time.

        Returns the attachment unchanged when no URL is available.  The
        URL is re-validated inside the closure (by ``_fetch_slack_file``)
        rather than here so that a trusted-at-serialize-time URL still
        fails closed if the allowlist tightens later.
        """
        meta = attachment.fetch_metadata if attachment.fetch_metadata is not None else {}
        meta_url = meta.get("url")
        url = meta_url if meta_url is not None else attachment.url
        team_id = meta.get("teamId")
        enterprise_id = meta.get("enterpriseId")
        is_enterprise_install = meta.get("isEnterpriseInstall") == "true"
        if not url:
            return attachment

        adapter = self

        async def resolve_token() -> str:
            installation_id = enterprise_id if is_enterprise_install else team_id
            if installation_id:
                # Route through ``_resolve_token_for_team`` so
                # ``installation_provider`` (when configured) is honored --
                # otherwise this falls back to internal state via
                # ``get_installation``, matching the prior behavior.
                ctx = await adapter._resolve_token_for_team(installation_id, is_enterprise_install)
                if ctx is None:
                    raise AuthenticationError(
                        "slack",
                        f"Installation not found for "
                        f"{'enterprise' if is_enterprise_install else 'team'} {installation_id}",
                    )
                return ctx.token
            # Use the async resolver so a dynamic ``bot_token`` provider
            # is invoked at fetch time (rotation-safe).
            return await adapter._resolve_token_async()

        async def fetch_data() -> bytes:
            # Lazy: ``_fetch_slack_file`` resolves the installation token only
            # for a Slack auth origin, so a non-Slack URL never looks one up.
            return await adapter._fetch_slack_file(url, resolve_token)

        return Attachment(
            type=attachment.type,
            url=attachment.url,
            name=attachment.name,
            mime_type=attachment.mime_type,
            size=attachment.size,
            width=attachment.width,
            height=attachment.height,
            data=attachment.data,
            fetch_data=fetch_data,
            fetch_metadata=attachment.fetch_metadata,
        )

    def _is_message_from_self(self, event: dict[str, Any]) -> bool:
        """Check if a Slack event is from this bot.

        The bot user id (``U…``) matches ``event.user`` or, on bot messages,
        ``event.bot_profile.user_id`` (upstream vercel/chat#883); the app bot
        id (``B…``) matches ``event.bot_id``.
        """
        user_id = event.get("user") or _bot_profile_user_id(event)
        ctx = self._request_context.get()
        if ctx and ctx.bot_user_id and user_id == ctx.bot_user_id:
            return True
        if self._bot_user_id and user_id == self._bot_user_id:
            return True
        return bool(self._bot_id and event.get("bot_id") == self._bot_id)

    # ==================================================================
    # Post / Edit / Delete messages
    # ==================================================================

    async def post_message(self, thread_id: str, message: AdapterPostableMessage) -> RawMessage:
        """Post a message to a Slack thread."""
        message = await self._resolve_message_mentions(message, thread_id)
        decoded = self.decode_thread_id(thread_id)
        channel = decoded.channel
        thread_ts = decoded.thread_ts

        try:
            client = self._get_client()

            # Check for files to upload. ``files_upload_v2`` returns the
            # Slack-confirmed file IDs; we surface them on ``RawMessage.raw``
            # under the camelCase ``uploadedFileIds`` key (upstream
            # chat@4.30.0 adopted this same surface) so consumers can
            # gate on actual delivery (parity with discord/telegram, which
            # upload inline and expose the platform response naturally).
            # ``None`` means no upload happened; an empty list means Slack
            # confirmed zero attachments (a real signal).
            uploaded_file_ids: list[str] | None = None
            files = extract_files(message)
            if files:
                uploaded_file_ids = await self._upload_files(files, channel, thread_ts or None)
                has_text = (
                    isinstance(message, str)
                    or (hasattr(message, "raw") and getattr(message, "raw", None))
                    or (hasattr(message, "markdown") and getattr(message, "markdown", None))
                    or (hasattr(message, "ast") and getattr(message, "ast", None))
                )
                card = extract_card(message)
                if not (has_text or card):
                    return RawMessage(
                        id=f"file-{int(time.time() * 1000)}",
                        thread_id=thread_id,
                        raw=self._augment_raw_with_uploads({"files": files}, uploaded_file_ids),
                    )

            card = extract_card(message)
            if card:
                blocks = card_to_block_kit(card)
                fallback_text = card_to_fallback_text(card)
                self._logger.debug(
                    "Slack API: chat.postMessage (blocks)",
                    {"channel": channel, "threadTs": thread_ts, "blockCount": len(blocks)},
                )
                try:
                    result = await client.chat_postMessage(
                        channel=channel,
                        thread_ts=thread_ts or None,
                        text=fallback_text,
                        blocks=blocks,
                        unfurl_links=False,
                        unfurl_media=False,
                    )
                except Exception as error:
                    enriched = self._enrich_invalid_blocks_error(error, blocks)
                    if enriched is None:
                        raise
                    raise enriched from error
                return RawMessage(
                    id=result.get("ts", ""),
                    thread_id=thread_id,
                    raw=self._augment_raw_with_uploads(
                        result.data if hasattr(result, "data") else result,
                        uploaded_file_ids,
                    ),
                )

            payload = self._format_converter.to_slack_payload(message)
            self._logger.debug(
                "Slack API: chat.postMessage",
                {
                    "channel": channel,
                    "threadTs": thread_ts,
                    "payloadKey": "markdown_text" if "markdown_text" in payload else "text",
                },
            )
            result = await client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts or None,
                unfurl_links=False,
                unfurl_media=False,
                **payload,
            )
            return RawMessage(
                id=result.get("ts", ""),
                thread_id=thread_id,
                raw=self._augment_raw_with_uploads(
                    result.data if hasattr(result, "data") else result,
                    uploaded_file_ids,
                ),
            )
        except Exception as error:
            self._handle_slack_error(error)

    @staticmethod
    def _augment_raw_with_uploads(raw: Any, uploaded_file_ids: list[str] | None) -> Any:
        """Add Slack-confirmed file IDs to a ``RawMessage.raw`` payload.

        Returns ``raw`` unchanged when no upload occurred (``uploaded_file_ids``
        is ``None``). Otherwise returns a NEW dict that merges the existing raw
        (Slack never returns an ``uploadedFileIds`` key, so this is additive
        and non-breaking) with the confirmed IDs. An empty list is preserved —
        it signals that Slack confirmed zero attachments.

        The key is emitted in camelCase (``uploadedFileIds``) to match the
        surface upstream adopted in chat@4.30.0; ``uploaded_file_ids`` is the
        internal (snake_case) variable, camelCase only at this serialization
        boundary.
        """
        if uploaded_file_ids is None:
            return raw
        base = raw if isinstance(raw, dict) else {}
        return {**base, "uploadedFileIds": uploaded_file_ids}

    async def edit_message(
        self,
        thread_id: str,
        message_id: str,
        message: AdapterPostableMessage,
    ) -> RawMessage:
        """Edit a message in a Slack thread."""
        message = await self._resolve_message_mentions(message, thread_id)

        # Handle ephemeral messages via response_url
        ephemeral = self._decode_ephemeral_message_id(message_id)
        if ephemeral:
            decoded = self.decode_thread_id(thread_id)
            result = await self._send_to_response_url(
                ephemeral["response_url"],
                "replace",
                message=message,
                thread_ts=decoded.thread_ts,
            )
            return RawMessage(
                id=ephemeral["message_ts"],
                thread_id=thread_id,
                raw={"ephemeral": True, **result},
            )
        if message_id.startswith("ephemeral:"):
            # Undecodable / untrusted ephemeral id: never fall through to
            # ``chat.update`` with it (vercel/chat#876).
            raise ValidationError("slack", "Invalid Slack ephemeral message ID")

        decoded = self.decode_thread_id(thread_id)
        channel = decoded.channel

        try:
            client = self._get_client()
            card = extract_card(message)

            if card:
                blocks = card_to_block_kit(card)
                fallback_text = card_to_fallback_text(card)
                result = await client.chat_update(channel=channel, ts=message_id, text=fallback_text, blocks=blocks)
                return RawMessage(
                    id=result.get("ts", ""),
                    thread_id=thread_id,
                    raw=result.data if hasattr(result, "data") else result,
                )

            payload = self._format_converter.to_slack_payload(message)
            self._logger.debug(
                "Slack API: chat.update",
                {
                    "channel": channel,
                    "messageId": message_id,
                    "payloadKey": "markdown_text" if "markdown_text" in payload else "text",
                },
            )
            result = await client.chat_update(channel=channel, ts=message_id, **payload)
            return RawMessage(
                id=result.get("ts", ""),
                thread_id=thread_id,
                raw=result.data if hasattr(result, "data") else result,
            )
        except Exception as error:
            self._handle_slack_error(error)

    async def delete_message(self, thread_id: str, message_id: str) -> None:
        """Delete a message from a Slack thread."""
        ephemeral = self._decode_ephemeral_message_id(message_id)
        if ephemeral:
            await self._send_to_response_url(ephemeral["response_url"], "delete")
            return
        if message_id.startswith("ephemeral:"):
            raise ValidationError("slack", "Invalid Slack ephemeral message ID")

        decoded = self.decode_thread_id(thread_id)
        channel = decoded.channel

        try:
            client = self._get_client()
            self._logger.debug("Slack API: chat.delete", {"channel": channel, "messageId": message_id})
            await client.chat_delete(channel=channel, ts=message_id)
        except Exception as error:
            self._handle_slack_error(error)

    # ==================================================================
    # Reactions
    # ==================================================================

    async def add_reaction(self, thread_id: str, message_id: str, emoji: EmojiValue | str) -> None:
        """Add a reaction to a message."""
        decoded = self.decode_thread_id(thread_id)
        channel = decoded.channel
        slack_emoji = emoji_to_slack(emoji)
        name = slack_emoji.replace(":", "")

        try:
            client = self._get_client()
            self._logger.debug(
                "Slack API: reactions.add",
                {"channel": channel, "messageId": message_id, "emoji": name},
            )
            await client.reactions_add(channel=channel, timestamp=message_id, name=name)
        except Exception as error:
            self._handle_slack_error(error)

    async def remove_reaction(self, thread_id: str, message_id: str, emoji: EmojiValue | str) -> None:
        """Remove a reaction from a message."""
        decoded = self.decode_thread_id(thread_id)
        channel = decoded.channel
        slack_emoji = emoji_to_slack(emoji)
        name = slack_emoji.replace(":", "")

        try:
            client = self._get_client()
            self._logger.debug(
                "Slack API: reactions.remove",
                {"channel": channel, "messageId": message_id, "emoji": name},
            )
            await client.reactions_remove(channel=channel, timestamp=message_id, name=name)
        except Exception as error:
            self._handle_slack_error(error)

    # ==================================================================
    # Typing indicator
    # ==================================================================

    async def start_typing(self, thread_id: str, status: str | None = None) -> None:
        """Show typing / status indicator in the thread.

        Uses Slack's ``assistant.threads.setStatus`` API when available.
        """
        decoded = self.decode_thread_id(thread_id)
        channel = decoded.channel
        thread_ts = decoded.thread_ts
        if not thread_ts:
            self._logger.debug("Slack: startTyping skipped - no thread context")
            return

        status_text = status or "Typing..."
        self._logger.debug(
            "Slack API: assistant.threads.setStatus",
            {"channel": channel, "threadTs": thread_ts, "status": status_text},
        )
        try:
            client = self._get_client()
            await client.assistant_threads_setStatus(
                channel_id=channel,
                thread_ts=thread_ts,
                status=status_text,
                loading_messages=[status_text],
            )
        except Exception as exc:
            self._logger.warn(
                "Slack API: assistant.threads.setStatus failed",
                {"channel": channel, "threadTs": thread_ts, "error": exc},
            )

    # ==================================================================
    # Streaming
    # ==================================================================

    async def stream(
        self,
        thread_id: str,
        text_stream: AsyncIterable[StreamInput],
        options: StreamOptions | None = None,
    ) -> RawMessage:
        """Stream a message using Slack's native streaming API.

        Consumes an async iterable of text chunks and/or structured
        ``StreamChunk`` objects and streams them to Slack.

        Requires ``recipient_user_id`` and ``recipient_team_id`` in *options*.
        """
        if not options or not (options.recipient_user_id and options.recipient_team_id):
            raise ValidationError(
                "slack",
                "Slack streaming requires recipient_user_id and recipient_team_id in options",
            )

        decoded = self.decode_thread_id(thread_id)
        channel = decoded.channel
        # Normalize empty thread_ts to None to avoid Slack API "invalid_thread_ts" errors.
        # ``chat.startStream`` rejects an empty thread_ts (top-level DMs have no
        # parent thread to attach to), but ``chat.postMessage`` accepts it.
        # Degrade DMs to a single accumulated post_message call so streaming
        # replies aren't silently dropped (chat-sdk-python#94).
        thread_ts = decoded.thread_ts or None
        if not thread_ts:
            self._logger.debug(
                "Slack: stream degraded to post_message - no thread context",
                {"channel": channel},
            )
            accumulated = ""
            async for chunk in text_stream:
                if isinstance(chunk, str):
                    accumulated += chunk
                elif hasattr(chunk, "type") and chunk.type == "markdown_text":  # type: ignore[union-attr]
                    accumulated += chunk.text  # type: ignore[union-attr]
            return await self.post_message(thread_id, PostableMarkdown(markdown=accumulated))
        self._logger.debug("Slack: starting stream", {"channel": channel, "threadTs": thread_ts})

        token = self._get_token()
        client = self._get_client(token)

        stream_kwargs: dict[str, Any] = {
            "channel": channel,
            "thread_ts": thread_ts,
            "recipient_user_id": options.recipient_user_id,
            "recipient_team_id": options.recipient_team_id,
            # Enterprise Grid disambiguation (chat-sdk-python#95). On Grid
            # orgs ``chat.startStream`` fails with ``team_not_found`` unless
            # the workspace ``team_id`` is supplied explicitly — the
            # per-workspace bot token alone is not sufficient (whereas
            # ``chat.postMessage`` succeeds without it). slack_sdk's
            # ``chat_stream`` stashes unknown kwargs in ``_stream_args`` and
            # forwards them to BOTH ``chat.startStream`` call sites (the
            # eager first-flush path and the lazy stop-without-flush path),
            # so passing ``team_id`` here threads it through to the only API
            # call that needs it. ``chat.appendStream``/``chat.stopStream``
            # do not receive ``_stream_args`` and do not require ``team_id``.
            # ``recipient_team_id`` is the ``team.id`` extracted on the
            # inbound path (the workspace where the interaction happened =
            # the streaming target workspace). Harmless on non-Grid
            # workspaces: passing the correct ``team_id`` is always valid.
            "team_id": options.recipient_team_id,
        }
        if options.task_display_mode:
            stream_kwargs["task_display_mode"] = options.task_display_mode

        streamer = await client.chat_stream(**stream_kwargs)

        last_appended = ""

        # Use StreamingMarkdownRenderer for safe incremental rendering
        from chat_sdk.shared.streaming_markdown import StreamingMarkdownRenderer

        renderer = StreamingMarkdownRenderer(wrap_tables_for_append=False)
        structured_chunks_supported = True

        # Outgoing @name mention resolution for the native streaming path
        # (vercel/chat 6f0d2f02). post_message/edit_message resolve mentions
        # themselves, so the committed renderer text is resolved here before
        # deltas are calculated. ``resolved_source_done`` indexes into the
        # renderer's committable text; ``resolved_committed`` is its resolved
        # counterpart and the coordinate space ``last_appended`` tracks.
        # Mixing the two spaces would duplicate or drop text as soon as a
        # replacement changes length (``@alice`` -> ``<@U123>``).
        resolved_committed = ""
        resolved_source_done = 0
        inside_resolved_fence = False

        def is_fence_line(line: str) -> bool:
            # Python ``lstrip()`` rather than JS ``trimStart()`` (different
            # whitespace sets, e.g. U+FEFF / U+001C-U+001F) on purpose: it
            # matches the Python StreamingMarkdownRenderer's own fence
            # tracking, which decides what gets committed mid-fence.
            trimmed = line.lstrip()
            return trimmed.startswith("```") or trimmed.startswith("~~~")

        async def resolve_committed(committable: str) -> None:
            """Extend ``resolved_committed`` with newly committed renderer text.

            Works line by line, tracking code-fence state. The renderer only
            commits partial lines inside fences (where mentions stay literal,
            matching ``_resolve_outgoing_mentions``) or at inline-marker
            holdback cuts (which never split a bare mention), so every bare
            mention reaches the resolver whole even when it spans source
            chunks. No resolution is cached across lines: the participant
            list can change mid-stream.

            Upstream parity: a segment that starts at a holdback cut is
            scanned without the text before the cut, so a URL cut at an
            unclosed marker (``https://x.io/*@alice``) can resolve the
            handle after it. Upstream ``resolveCommitted`` behaves the same.
            """
            nonlocal resolved_committed, resolved_source_done, inside_resolved_fence
            while resolved_source_done < len(committable):
                # JS ``lastIndexOf("\n", done - 1)``: last newline before
                # ``done``. (At ``done == 0`` JS clamps to index 0; the only
                # difference is a leading "\n" line, which is never a fence.)
                line_start = committable.rfind("\n", 0, resolved_source_done) + 1
                newline_at = committable.find("\n", resolved_source_done)
                line_end = len(committable) if newline_at == -1 else newline_at + 1
                segment = committable[resolved_source_done:line_end]
                fence_line = is_fence_line(committable[line_start:line_end])
                if inside_resolved_fence or fence_line:
                    # Fence delimiters and fenced content are literal.
                    resolved_committed += segment
                else:
                    resolved_committed += await self._resolve_outgoing_mentions(segment, thread_id)
                # Toggle only once the fence line's newline is committed.
                if newline_at != -1 and fence_line:
                    inside_resolved_fence = not inside_resolved_fence
                resolved_source_done = line_end

        # The resolved bot token is passed on EVERY append and on stop
        # (vercel/chat#573). Passing it only on the first append left
        # chat.startStream/chat.stopStream unauthenticated ("not_authed")
        # whenever the stream reached stop() before a token-bearing append
        # had flushed (e.g. fully buffered markdown). In multi-workspace
        # mode `token` is the per-request installation token resolved by
        # _get_token() at stream entry.
        async def flush_markdown_delta(delta: str) -> None:
            if not delta:
                return
            await streamer.append(markdown_text=delta, token=token)

        # Accepts the residual stream-input union: ``is_thinking_chunk`` filters
        # ``ThinkingChunk`` out before this runs (it is never reached with one),
        # but it stays in the static type since that runtime guard does not
        # narrow it. The generic ``_read``-based body handles any chunk shape.
        async def send_structured_chunk(chunk: StreamChunk | ThinkingChunk | dict[str, Any]) -> None:
            nonlocal last_appended, structured_chunks_supported
            if not structured_chunks_supported:
                return
            await resolve_committed(renderer.get_committable_text())
            delta = resolved_committed[len(last_appended) :]
            await flush_markdown_delta(delta)
            last_appended = resolved_committed

            def _read(name: str) -> Any:
                if isinstance(chunk, dict):
                    return chunk.get(name)
                return getattr(chunk, name, None)

            chunk_type = _read("type")
            if not chunk_type:
                self._logger.warn(
                    "Slack stream: ignoring chunk with no `type` field",
                    {"chunkRepr": repr(chunk)[:200]},
                )
                return

            try:
                chunk_data: dict[str, Any] = {"type": chunk_type}
                for field_name in ("id", "title", "status", "output", "text"):
                    value = _read(field_name)
                    if value is not None:
                        chunk_data[field_name] = value

                await streamer.append(chunks=[chunk_data], token=token)
            except Exception as exc:
                structured_chunks_supported = False
                self._logger.warn(
                    "Slack stream: structured-chunk append failed, falling back to "
                    "text-only for the rest of this stream. Likely causes: missing "
                    "`assistant_view` / `assistant:write` scope on the app manifest, "
                    "malformed chunk payload, or a transient Slack API error.",
                    {"chunkType": chunk_type, "error": exc},
                )

        async def push_text_and_flush(text: str) -> None:
            nonlocal last_appended
            renderer.push(text)
            await resolve_committed(renderer.get_committable_text())
            delta = resolved_committed[len(last_appended) :]
            await flush_markdown_delta(delta)
            last_appended = resolved_committed

        async for chunk in text_stream:
            if isinstance(chunk, str):
                await push_text_and_flush(chunk)
            elif isinstance(chunk, dict) and chunk.get("type") == "markdown_text":
                # Dict-shaped StreamChunks are part of the contract: the
                # `_from_full_stream` normalizer in thread.py forwards dict
                # `{type: "markdown_text", ...}` items unchanged, so adapters
                # must handle them the same as the dataclass form.
                text_value = chunk.get("text")
                if isinstance(text_value, str) and text_value:
                    await push_text_and_flush(text_value)
            elif hasattr(chunk, "type") and chunk.type == "markdown_text":  # type: ignore[union-attr]
                await push_text_and_flush(chunk.text)  # type: ignore[union-attr]
            elif is_thinking_chunk(chunk):
                # Python-only divergence: ``ThinkingChunk`` is streaming-only
                # agent reasoning, NOT message content. By default it is
                # skipped (no effect on the posted message). An adapter/consumer
                # that wants to display thinking sets ``self.render_thinking``;
                # only then is it invoked. Skipping (not routing to
                # ``send_structured_chunk``) keeps the default posted message
                # byte-identical and avoids disabling structured-chunk support.
                await maybe_render_thinking(getattr(self, "render_thinking", None), chunk)
            else:
                await send_structured_chunk(chunk)

        # Flush remaining (finish releases all held-back content). As
        # upstream, the final delta comes from ``get_committable_text()``
        # (the raw accumulated text), not ``finish()``'s remend'd render:
        # the resolved buffer is built incrementally from committable text,
        # so the final source must extend the same prefix.
        renderer.finish()
        await resolve_committed(renderer.get_committable_text())
        final_delta = resolved_committed[len(last_appended) :]
        await flush_markdown_delta(final_delta)

        stop_kwargs: dict[str, Any] = {"token": token}
        if options.stop_blocks:
            stop_kwargs["blocks"] = options.stop_blocks
        result = await streamer.stop(**stop_kwargs)

        message_ts = ""
        if isinstance(result, dict):
            message_ts = (result.get("message") or {}).get("ts") or result.get("ts", "")
        elif hasattr(result, "data"):
            data = result.data
            message_ts = (data.get("message") or {}).get("ts") or data.get("ts", "")

        self._logger.debug("Slack: stream complete", {"messageId": message_ts})
        return RawMessage(id=message_ts, thread_id=thread_id, raw=result)

    # ==================================================================
    # Ephemeral messages
    # ==================================================================

    async def post_ephemeral(
        self,
        thread_id: str,
        user_id: str,
        message: AdapterPostableMessage,
    ) -> EphemeralMessage:
        """Post an ephemeral (user-only visible) message."""
        message = await self._resolve_message_mentions(message, thread_id)
        decoded = self.decode_thread_id(thread_id)
        channel = decoded.channel
        thread_ts = decoded.thread_ts

        try:
            client = self._get_client()
            card = extract_card(message)

            if card:
                blocks = card_to_block_kit(card)
                fallback_text = card_to_fallback_text(card)
                result = await client.chat_postEphemeral(
                    channel=channel,
                    thread_ts=thread_ts or None,
                    user=user_id,
                    text=fallback_text,
                    blocks=blocks,
                )
                return EphemeralMessage(
                    id=result.get("message_ts", ""),
                    thread_id=thread_id,
                    used_fallback=False,
                    raw=result.data if hasattr(result, "data") else result,
                )

            payload = self._format_converter.to_slack_payload(message)
            self._logger.debug(
                "Slack API: chat.postEphemeral",
                {
                    "channel": channel,
                    "threadTs": thread_ts,
                    "userId": user_id,
                    "payloadKey": "markdown_text" if "markdown_text" in payload else "text",
                },
            )
            result = await client.chat_postEphemeral(
                channel=channel,
                thread_ts=thread_ts or None,
                user=user_id,
                **payload,
            )
            return EphemeralMessage(
                id=result.get("message_ts", ""),
                thread_id=thread_id,
                used_fallback=False,
                raw=result.data if hasattr(result, "data") else result,
            )
        except Exception as error:
            self._handle_slack_error(error)

    # ==================================================================
    # Schedule messages
    # ==================================================================

    async def schedule_message(
        self,
        thread_id: str,
        message: AdapterPostableMessage,
        post_at: datetime,
    ) -> ScheduledMessage:
        """Schedule a message for future delivery."""
        message = await self._resolve_message_mentions(message, thread_id)
        decoded = self.decode_thread_id(thread_id)
        channel = decoded.channel
        thread_ts = decoded.thread_ts
        post_at_unix = int(post_at.timestamp())

        if post_at_unix <= int(time.time()):
            raise ValidationError("slack", "post_at must be in the future")

        files = extract_files(message)
        if files:
            raise ValidationError("slack", "File uploads are not supported in scheduled messages")

        # For multi-workspace mode, snapshot the per-team token from the
        # active request context — ``cancel()`` may run outside the
        # ContextVar frame. For single-workspace mode, defer resolution to
        # ``cancel()`` so dynamic resolvers honor token rotation between
        # ``schedule_message()`` and ``cancel()`` (Slack rotated tokens have
        # a 12h TTL and scheduled messages can outlive their schedule-time
        # token).
        ctx = self._request_context.get()
        ctx_token: str | None = ctx.token if ctx and ctx.token else None
        token = ctx_token if ctx_token is not None else await self._resolve_token_async()

        try:
            client = self._get_client(token)
            card = extract_card(message)

            if card:
                blocks = card_to_block_kit(card)
                fallback_text = card_to_fallback_text(card)
                result = await client.chat_scheduleMessage(
                    channel=channel,
                    thread_ts=thread_ts or None,
                    post_at=post_at_unix,
                    text=fallback_text,
                    blocks=blocks,
                    unfurl_links=False,
                    unfurl_media=False,
                )
            else:
                payload = self._format_converter.to_slack_payload(message)
                self._logger.debug(
                    "Slack API: chat.scheduleMessage",
                    {
                        "channel": channel,
                        "threadTs": thread_ts,
                        "postAt": post_at_unix,
                        "payloadKey": "markdown_text" if "markdown_text" in payload else "text",
                    },
                )
                result = await client.chat_scheduleMessage(
                    channel=channel,
                    thread_ts=thread_ts or None,
                    post_at=post_at_unix,
                    unfurl_links=False,
                    unfurl_media=False,
                    **payload,
                )

            scheduled_message_id = result.get("scheduled_message_id", "")
            adapter = self

            async def cancel() -> None:
                # Multi-workspace: use the snapshotted ctx_token (resolver
                # runs outside the request frame). Single-workspace: re-
                # resolve so token rotation between schedule + cancel works.
                cancel_token = ctx_token if ctx_token is not None else await adapter._resolve_token_async()
                c = adapter._get_client(cancel_token)
                await c.chat_deleteScheduledMessage(channel=channel, scheduled_message_id=scheduled_message_id)

            return ScheduledMessage(
                scheduled_message_id=scheduled_message_id,
                channel_id=channel,
                post_at=post_at,
                raw=result.data if hasattr(result, "data") else result,
                _cancel=cancel,
            )
        except Exception as error:
            self._handle_slack_error(error)

    # ==================================================================
    # Open DM
    # ==================================================================

    async def open_dm(self, user_id: str) -> str:
        """Open a DM conversation with a user. Returns a thread ID."""
        try:
            client = self._get_client()
            self._logger.debug("Slack API: conversations.open", {"userId": user_id})
            result = await client.conversations_open(users=user_id)
            channel_info = result.get("channel", {})
            channel_id = channel_info.get("id")
            if not channel_id:
                raise RuntimeError("Failed to open DM - no channel returned")

            return self.encode_thread_id(SlackThreadId(channel=channel_id, thread_ts=""))
        except Exception as error:
            self._handle_slack_error(error)

    # ==================================================================
    # Open modal
    # ==================================================================

    async def open_modal(self, trigger_id: str, modal: dict[str, Any], context_id: str | None = None) -> dict[str, str]:
        """Open a Slack modal using views.open."""
        metadata = encode_modal_metadata(
            ModalMetadata(
                context_id=context_id,
                private_metadata=modal.get("private_metadata"),
            )
        )
        view = modal_to_slack_view(cast(ModalElement, modal), metadata)

        self._logger.debug(
            "Slack API: views.open",
            {"triggerId": trigger_id, "callbackId": modal.get("callback_id")},
        )

        try:
            client = self._get_client()
            result = await client.views_open(trigger_id=trigger_id, view=view)
            view_id = (result.get("view") or {}).get("id", "")
            return {"viewId": view_id}
        except Exception as error:
            self._handle_slack_error(error)

    async def update_modal(self, view_id: str, modal: dict[str, Any]) -> dict[str, str]:
        """Update an existing modal using views.update."""
        view = modal_to_slack_view(cast(ModalElement, modal))

        try:
            client = self._get_client()
            result = await client.views_update(view_id=view_id, view=view)
            new_view_id = (result.get("view") or {}).get("id", "")
            return {"viewId": new_view_id}
        except Exception as error:
            self._handle_slack_error(error)

    # ==================================================================
    # File uploads
    # ==================================================================

    async def _upload_files(
        self,
        files: list[FileUpload],
        channel: str,
        thread_ts: str | None = None,
    ) -> list[str]:
        """Upload files to Slack and share them to a channel."""
        file_uploads = []
        for file in files:
            try:
                file_uploads.append({"file": file.data, "filename": file.filename})
            except Exception as exc:
                self._logger.error(
                    "Failed to prepare file for upload",
                    {"filename": file.filename, "error": exc},
                )

        if not file_uploads:
            return []

        self._logger.debug(
            "Slack API: files.uploadV2 (batch)",
            {"fileCount": len(file_uploads), "filenames": [f["filename"] for f in file_uploads]},
        )

        client = self._get_client()
        kwargs: dict[str, Any] = {"channel": channel, "file_uploads": file_uploads}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts

        result = await client.files_upload_v2(**kwargs)
        file_ids: list[str] = []
        result_data = result.data if hasattr(result, "data") else result
        for uploaded in result_data.get("files") or []:
            for f in uploaded.get("files") or []:
                if f.get("id"):
                    file_ids.append(f["id"])

        return file_ids

    # ==================================================================
    # Fetch messages
    # ==================================================================

    async def fetch_messages(self, thread_id: str, options: FetchOptions | None = None) -> FetchResult:
        """Fetch messages from a Slack thread."""
        opts = options or FetchOptions()
        decoded = self.decode_thread_id(thread_id)
        channel = decoded.channel
        thread_ts = decoded.thread_ts
        direction = getattr(opts, "direction", "backward") or "backward"
        limit = getattr(opts, "limit", 100) if getattr(opts, "limit", 100) is not None else 100
        cursor = getattr(opts, "cursor", None)

        # Divergence (chat-sdk-python#138): a top-level DM root encodes an empty
        # thread_ts (slack:Dxxx:). conversations.replies(ts="") returns no replies
        # and loses the DM root context, so route empty thread_ts to the channel
        # history path (conversations.history), where the channel *is* the
        # conversation. Upstream's fetchMessages has no empty-thread_ts guard.
        try:
            if not thread_ts:
                if direction == "forward":
                    return await self._fetch_channel_messages_forward(channel, limit, cursor)
                return await self._fetch_channel_messages_backward(channel, limit, cursor)
            if direction == "forward":
                return await self._fetch_messages_forward(channel, thread_ts, thread_id, limit, cursor)
            return await self._fetch_messages_backward(channel, thread_ts, thread_id, limit, cursor)
        except Exception as error:
            self._handle_slack_error(error)

    async def _fetch_messages_forward(
        self,
        channel: str,
        thread_ts: str,
        thread_id: str,
        limit: int,
        cursor: str | None = None,
    ) -> FetchResult:
        client = self._get_client()
        result = await client.conversations_replies(channel=channel, ts=thread_ts, limit=limit, cursor=cursor)
        slack_messages = result.get("messages", [])
        next_cursor = (result.get("response_metadata") or {}).get("next_cursor")

        messages = await asyncio.gather(*(self._parse_slack_message(msg, thread_id) for msg in slack_messages))
        return FetchResult(messages=list(messages), next_cursor=next_cursor or None)

    async def _fetch_messages_backward(
        self,
        channel: str,
        thread_ts: str,
        thread_id: str,
        limit: int,
        cursor: str | None = None,
    ) -> FetchResult:
        latest = cursor or None
        fetch_limit = min(1000, max(limit * 2, 200))

        client = self._get_client()
        result = await client.conversations_replies(
            channel=channel, ts=thread_ts, limit=fetch_limit, latest=latest, inclusive=False
        )
        slack_messages = result.get("messages", [])

        start_index = max(0, len(slack_messages) - limit)
        selected = slack_messages[start_index:]

        messages = await asyncio.gather(*(self._parse_slack_message(msg, thread_id) for msg in selected))

        next_cursor: str | None = None
        if (start_index > 0 or result.get("has_more")) and selected:
            oldest = selected[0]
            if oldest.get("ts"):
                next_cursor = oldest["ts"]

        return FetchResult(messages=list(messages), next_cursor=next_cursor)

    async def fetch_message(self, thread_id: str, message_id: str) -> Message | None:
        """Fetch a single message by ID (timestamp)."""
        decoded = self.decode_thread_id(thread_id)
        channel = decoded.channel
        thread_ts = decoded.thread_ts

        try:
            client = self._get_client()
            # Divergence (chat-sdk-python#138): a DM root encodes an empty
            # thread_ts, so conversations.replies(ts="") cannot locate the
            # message. Fetch the single message from conversations.history
            # instead (mirrors the link-preview fetch_message at ~3293).
            if not thread_ts:
                result = await client.conversations_history(channel=channel, latest=message_id, inclusive=True, limit=1)
            else:
                result = await client.conversations_replies(
                    channel=channel, ts=thread_ts, oldest=message_id, inclusive=True, limit=1
                )
            messages = result.get("messages", [])
            target = next((m for m in messages if m.get("ts") == message_id), None)
            if not target:
                return None
            return await self._parse_slack_message(target, thread_id)
        except Exception as error:
            self._handle_slack_error(error)

    async def fetch_thread(self, thread_id: str) -> ThreadInfo:
        """Fetch thread info."""
        decoded = self.decode_thread_id(thread_id)
        channel = decoded.channel
        thread_ts = decoded.thread_ts

        try:
            client = self._get_client()
            result = await client.conversations_info(channel=channel)
            channel_info = result.get("channel", {})

            if channel_info.get("is_ext_shared"):
                self._external_channels.add(channel)

            visibility: ChannelVisibility = "unknown"
            if channel_info.get("is_ext_shared"):
                visibility = "external"
            elif channel_info.get("is_private") or channel.startswith("D"):
                visibility = "private"
            elif channel.startswith("C"):
                visibility = "workspace"

            return ThreadInfo(
                id=thread_id,
                channel_id=channel,
                channel_name=channel_info.get("name"),
                channel_visibility=visibility,
                metadata={"threadTs": thread_ts, "channel": channel_info},
            )
        except Exception as error:
            self._handle_slack_error(error)

    # ==================================================================
    # Channel-level methods
    # ==================================================================

    def channel_id_from_thread_id(self, thread_id: str) -> str:
        """Derive channel ID from a Slack thread ID."""
        decoded = self.decode_thread_id(thread_id)
        return f"slack:{decoded.channel}"

    async def fetch_channel_messages(self, channel_id: str, options: FetchOptions | None = None) -> FetchResult:
        """Fetch channel-level messages (conversations.history)."""
        channel = channel_id.split(":")[1] if ":" in channel_id else channel_id
        if not channel:
            raise ValidationError("slack", f"Invalid Slack channel ID: {channel_id}")

        opts = options or FetchOptions()
        direction = getattr(opts, "direction", "backward") or "backward"
        limit = getattr(opts, "limit", 100) if getattr(opts, "limit", 100) is not None else 100

        try:
            if direction == "forward":
                return await self._fetch_channel_messages_forward(channel, limit, getattr(opts, "cursor", None))
            return await self._fetch_channel_messages_backward(channel, limit, getattr(opts, "cursor", None))
        except Exception as error:
            self._handle_slack_error(error)

    async def _fetch_channel_messages_forward(self, channel: str, limit: int, cursor: str | None = None) -> FetchResult:
        client = self._get_client()
        kwargs: dict[str, Any] = {"channel": channel, "limit": limit}
        if cursor:
            kwargs["oldest"] = cursor
            kwargs["inclusive"] = False
        result = await client.conversations_history(**kwargs)

        slack_messages = list(reversed(result.get("messages", [])))
        messages = await asyncio.gather(
            *(
                self._parse_slack_message(
                    msg,
                    f"slack:{channel}:{msg.get('thread_ts') or msg.get('ts', '')}",
                )
                for msg in slack_messages
            )
        )

        next_cursor: str | None = None
        if result.get("has_more") and slack_messages:
            newest = slack_messages[-1]
            if newest.get("ts"):
                next_cursor = newest["ts"]

        return FetchResult(messages=list(messages), next_cursor=next_cursor)

    async def _fetch_channel_messages_backward(
        self, channel: str, limit: int, cursor: str | None = None
    ) -> FetchResult:
        client = self._get_client()
        kwargs: dict[str, Any] = {"channel": channel, "limit": limit}
        if cursor:
            kwargs["latest"] = cursor
            kwargs["inclusive"] = False
        result = await client.conversations_history(**kwargs)

        slack_messages = result.get("messages", [])
        chronological = list(reversed(slack_messages))

        messages = await asyncio.gather(
            *(
                self._parse_slack_message(
                    msg,
                    f"slack:{channel}:{msg.get('thread_ts') or msg.get('ts', '')}",
                )
                for msg in chronological
            )
        )

        next_cursor: str | None = None
        if result.get("has_more") and chronological:
            oldest = chronological[0]
            if oldest.get("ts"):
                next_cursor = oldest["ts"]

        return FetchResult(messages=list(messages), next_cursor=next_cursor)

    async def list_threads(self, channel_id: str, options: ListThreadsOptions | None = None) -> ListThreadsResult:
        """List threads in a Slack channel."""
        channel = channel_id.split(":")[1] if ":" in channel_id else channel_id
        if not channel:
            raise ValidationError("slack", f"Invalid Slack channel ID: {channel_id}")

        opts = options or ListThreadsOptions()
        limit = getattr(opts, "limit", 50) if getattr(opts, "limit", 50) is not None else 50

        try:
            client = self._get_client()
            result = await client.conversations_history(
                channel=channel,
                limit=min(limit * 3, 200),
                cursor=getattr(opts, "cursor", None),
            )

            slack_messages = result.get("messages", [])
            thread_messages = [m for m in slack_messages if (m.get("reply_count") or 0) > 0]
            selected = thread_messages[:limit]

            threads: list[ThreadSummary] = []
            for msg in selected:
                thread_ts = msg.get("ts", "")
                tid = f"slack:{channel}:{thread_ts}"
                root_message = await self._parse_slack_message(msg, tid)

                last_reply_at: datetime | None = None
                if msg.get("latest_reply"):
                    try:
                        last_reply_at = datetime.fromtimestamp(float(msg["latest_reply"]), tz=timezone.utc)
                    except (ValueError, TypeError, OSError):
                        last_reply_at = None

                threads.append(
                    ThreadSummary(
                        id=tid,
                        root_message=root_message,
                        reply_count=msg.get("reply_count"),
                        last_reply_at=last_reply_at,
                    )
                )

            next_cursor = (result.get("response_metadata") or {}).get("next_cursor")
            return ListThreadsResult(threads=threads, next_cursor=next_cursor or None)
        except Exception as error:
            self._handle_slack_error(error)

    async def fetch_channel_info(self, channel_id: str) -> ChannelInfo:
        """Fetch Slack channel info/metadata."""
        channel = channel_id.split(":")[1] if ":" in channel_id else channel_id
        if not channel:
            raise ValidationError("slack", f"Invalid Slack channel ID: {channel_id}")

        try:
            client = self._get_client()
            result = await client.conversations_info(channel=channel)
            info = result.get("channel", {})

            if info.get("is_ext_shared"):
                self._external_channels.add(channel)

            visibility: ChannelVisibility = "unknown"
            if info.get("is_ext_shared"):
                visibility = "external"
            elif info.get("is_im") or info.get("is_mpim") or info.get("is_private") or channel.startswith("D"):
                visibility = "private"
            elif channel.startswith("C"):
                visibility = "workspace"

            return ChannelInfo(
                id=channel_id,
                name=f"#{info['name']}" if info.get("name") else None,
                is_dm=bool(info.get("is_im") or info.get("is_mpim")),
                channel_visibility=visibility,
                member_count=info.get("num_members"),
                metadata={
                    "purpose": (info.get("purpose") or {}).get("value"),
                    "topic": (info.get("topic") or {}).get("value"),
                },
            )
        except Exception as error:
            self._handle_slack_error(error)

    async def post_channel_message(self, channel_id: str, message: AdapterPostableMessage) -> RawMessage:
        """Post a top-level message to a channel (not in a thread)."""
        channel = channel_id.split(":")[1] if ":" in channel_id else channel_id
        if not channel:
            raise ValidationError("slack", f"Invalid Slack channel ID: {channel_id}")

        synthetic_thread_id = f"slack:{channel}:"
        return await self.post_message(synthetic_thread_id, message)

    # ==================================================================
    # Thread ID encoding / decoding
    # ==================================================================

    def encode_thread_id(self, platform_data: SlackThreadId) -> str:
        """Encode a SlackThreadId to a string."""
        return f"slack:{platform_data.channel}:{platform_data.thread_ts}"

    def decode_thread_id(self, thread_id: str) -> SlackThreadId:
        """Decode a thread ID string to SlackThreadId."""
        parts = thread_id.split(":")
        if len(parts) < 2 or len(parts) > 3 or parts[0] != "slack":
            raise ValidationError("slack", f"Invalid Slack thread ID: {thread_id}")
        return SlackThreadId(
            channel=parts[1],
            thread_ts=parts[2] if len(parts) == 3 else "",
        )

    def is_dm(self, thread_id: str) -> bool:
        """Check if a thread is a direct message conversation."""
        decoded = self.decode_thread_id(thread_id)
        return decoded.channel.startswith("D")

    def get_channel_visibility(self, thread_id: str) -> ChannelVisibility:
        """Get the visibility scope of the channel containing the thread."""
        decoded = self.decode_thread_id(thread_id)
        channel = decoded.channel

        if channel in self._external_channels:
            return "external"
        if channel.startswith("G") or channel.startswith("D"):
            return "private"
        if channel.startswith("C"):
            return "workspace"
        return "unknown"

    def parse_message(self, raw: dict[str, Any]) -> Message:
        """Parse a raw Slack event into a Message (synchronous)."""
        event = raw
        thread_ts = event.get("thread_ts") or event.get("ts", "")
        thread_id = self.encode_thread_id(SlackThreadId(channel=event.get("channel", ""), thread_ts=thread_ts))
        return self._parse_slack_message_sync(event, thread_id)

    def render_formatted(self, content: FormattedContent) -> str:
        """Render formatted content (AST) to standard markdown.

        Slack now accepts markdown natively via ``markdown_text``.
        """
        return self._format_converter.from_ast(content)

    # ==================================================================
    # Error handling
    # ==================================================================

    def _enrich_invalid_blocks_error(self, error: Exception, blocks: list[Any]) -> AdapterError | None:
        """Surface Slack's per-block validation details on ``invalid_blocks`` errors.

        Port of upstream ``enrichInvalidBlocksError``. The Slack error alone
        just says ``invalid_blocks``; the actionable details (which block,
        which field) live in the response's ``errors`` and
        ``response_metadata.messages``. Returns ``None`` for any other error.
        Upstream throws a plain ``Error`` with the original as ``cause``; the
        port raises :class:`AdapterError` (code ``"invalid_blocks"``) with the
        original as ``__cause__``.
        """
        resp = getattr(error, "response", None)
        data: Any = None
        if resp is not None and isinstance(getattr(resp, "data", None), dict):
            data = resp.data
        elif isinstance(resp, dict):
            data = resp
        if not isinstance(data, dict) or data.get("error") != "invalid_blocks":
            return None
        errors = data.get("errors")
        metadata = data.get("response_metadata")
        messages = metadata.get("messages") if isinstance(metadata, dict) else None
        details = [
            *(errors if isinstance(errors, list) else []),
            *(messages if isinstance(messages, list) else []),
        ]
        # Upstream parity: the serialized blocks are logged at error level on
        # purpose (chat@4.41.1 adapter-slack/src/index.ts:173-176) so the
        # rejected payload can be debugged. Redact via a custom ``logger``.
        self._logger.error(
            "Slack rejected blocks (invalid_blocks)",
            {"details": details, "blocks": _json_stringify(blocks)},
        )
        return AdapterError(
            f"Slack rejected blocks (invalid_blocks): {_json_stringify(details)}",
            "slack",
            "invalid_blocks",
        )

    def _handle_slack_error(self, error: Any) -> NoReturn:
        """Re-raise Slack errors with appropriate SDK error types.

        Always raises — the `NoReturn` annotation lets type checkers skip
        the "missing return" warning for callers that rely on this to
        propagate out of a `try/except` block.
        """
        # slack_sdk's SlackApiError has a .response attribute (SlackResponse)
        # SlackResponse has a .data dict and an .get() method
        resp = getattr(error, "response", None)
        error_code: str | None = None
        if resp is not None:
            # SlackResponse has .data dict or direct attribute access
            if hasattr(resp, "data") and isinstance(resp.data, dict):
                error_code = resp.data.get("error")
            elif isinstance(resp, dict):
                error_code = resp.get("error")

        # Invalidate cached client on auth errors (token revocation / invalid_auth)
        if error_code in ("invalid_auth", "token_revoked", "account_inactive"):
            try:
                token = self._get_token()
                self._invalidate_client(token)
            except AuthenticationError:
                pass

        # Check for rate limiting
        if error_code == "ratelimited":
            retry_after = None
            if hasattr(resp, "headers"):
                retry_after = resp.headers.get("Retry-After")
            elif isinstance(resp, dict):
                retry_after = resp.get("headers", {}).get("Retry-After")
            retry_val = None
            if retry_after:
                try:
                    retry_val = int(retry_after)
                except (ValueError, TypeError):
                    retry_val = None
            raise AdapterRateLimitError("slack", retry_val) from error

        raise error  # type: ignore[misc]

    # ==================================================================
    # Ephemeral message ID encoding
    # ==================================================================

    def _encode_ephemeral_message_id(self, message_ts: str, response_url: str, user_id: str) -> str:
        # Only Slack-issued response URLs may be embedded in a message id
        # that ``edit_message`` / ``delete_message`` later POST to
        # (vercel/chat#876).
        if not _is_trusted_slack_response_url(response_url):
            raise ValidationError("slack", "Refusing to encode an untrusted Slack response_url")
        data = json.dumps({"responseUrl": response_url, "userId": user_id})
        encoded = base64.b64encode(data.encode("utf-8")).decode("ascii")
        return f"ephemeral:{message_ts}:{encoded}"

    def _decode_ephemeral_message_id(self, message_id: str) -> dict[str, str] | None:
        if not message_id.startswith("ephemeral:"):
            return None
        parts = message_id.split(":", 2)
        if len(parts) < 3:
            return None
        message_ts = parts[1]
        encoded_data = parts[2]
        try:
            decoded = base64.b64decode(encoded_data).decode("utf-8")
            try:
                data = json.loads(decoded)
            except (json.JSONDecodeError, ValueError):
                # No raw-string fallback: the legacy non-JSON format carried
                # an unvalidated URL (vercel/chat#876).
                return None
            if not isinstance(data, dict):
                return None
            response_url = data.get("responseUrl")
            user_id = data.get("userId")
            if (
                isinstance(response_url, str)
                and _is_trusted_slack_response_url(response_url)
                and isinstance(user_id, str)
                and user_id
            ):
                return {
                    "message_ts": message_ts,
                    "response_url": response_url,
                    "user_id": user_id,
                }
            return None
        except Exception:
            self._logger.warn("Failed to decode ephemeral messageId", {"messageId": message_id})
            return None

    # ==================================================================
    # Response URL
    # ==================================================================

    def _create_http_client(self) -> Any:
        """The ``httpx.AsyncClient`` for one ``response_url`` request.

        Uses the configured ``http_client_factory`` (upstream ``fetch``,
        vercel/chat 6adca361) so egress-proxied deployments can route these
        posts; otherwise a default client. httpx is imported lazily, and only
        when no factory is configured.
        """
        if self._http_client_factory is not None:
            return self._http_client_factory()
        import httpx

        return httpx.AsyncClient()

    async def _send_to_response_url(
        self,
        response_url: str,
        action: str,
        *,
        message: AdapterPostableMessage | None = None,
        thread_ts: str | None = None,
    ) -> dict[str, Any]:
        """Send a request to Slack's response_url to modify an ephemeral message."""
        # Defend the fetch sink itself (vercel/chat#876): only
        # ``hooks.slack.com`` / ``hooks.slack-gov.com`` over https, with no
        # userinfo or explicit port. Checked before httpx is even imported.
        if not _is_trusted_slack_response_url(response_url):
            raise ValidationError("slack", "Refusing to send content to an untrusted Slack response_url")

        payload: dict[str, Any]

        if action == "delete":
            payload = {"delete_original": True}
        else:
            if not message:
                raise ValidationError("slack", "Message required for replace action")

            card = extract_card(message)
            if card:
                payload = {
                    "replace_original": True,
                    "text": card_to_fallback_text(card),
                    "blocks": card_to_block_kit(card),
                }
            else:
                # Slack rejects `markdown_text` on response_url payloads
                # (`no_text`), so markdown/AST messages are rendered to
                # Slack's legacy mrkdwn format for this surface.
                payload = {
                    "replace_original": True,
                    "text": self._format_converter.to_response_url_text(message),
                }

            if thread_ts:
                payload["thread_ts"] = thread_ts

        self._logger.debug(
            "Slack response_url request",
            {"action": action, "threadTs": thread_ts},
        )

        async with self._create_http_client() as http:
            resp = await http.post(
                response_url,
                json=payload,
                headers={"Content-Type": "application/json"},
            )

            if not resp.is_success:
                error_text = resp.text
                self._logger.error(
                    "Slack response_url failed",
                    {"action": action, "status": resp.status_code, "body": error_text},
                )
                raise RuntimeError(f"Failed to {action} via response_url: {error_text}")

            response_text = resp.text
            if response_text:
                try:
                    return json.loads(response_text)
                except (json.JSONDecodeError, ValueError):
                    return {"raw": response_text}
            return {}


# ==================================================================
# Factory
# ==================================================================


def create_slack_adapter(config: SlackAdapterConfig | None = None) -> SlackAdapter:
    """Create a new SlackAdapter instance.

    For socket mode, the factory rejects multi-workspace setups upfront —
    Socket Mode is a single-workspace transport (the WebSocket carries one
    app's events for one workspace) and silently mixing the two would mask
    a config mistake.
    """
    if config is not None and (config.mode or "webhook") == "socket" and (config.client_id or config.client_secret):
        raise ValidationError(
            "slack",
            "Multi-workspace (clientId/clientSecret) is not supported in socket mode.",
        )
    return SlackAdapter(config)
