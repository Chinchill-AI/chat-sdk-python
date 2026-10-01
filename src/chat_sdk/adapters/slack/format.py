"""Slack format primitives — a lightweight, runtime-free subpath.

Port of ``packages/adapter-slack/src/format/index.ts`` (vercel/chat#547),
exposed upstream as ``@chat-adapter/slack/format``. Provides runtime-free
primitives for Slack text objects, mrkdwn escaping, mentions, links, dates,
and basic mrkdwn normalization — without the full Slack adapter,
``slack_sdk``, or the chat runtime.

Importing this module never imports ``slack_sdk``, HTTP clients, or the
high-level :mod:`chat_sdk.adapters.slack.adapter`.
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Literal, NotRequired, TypedDict

from chat_sdk.shared._js_compat import JS_WHITESPACE as _JS_WHITESPACE


class SlackPlainTextObject(TypedDict):
    """A Slack ``plain_text`` composition object."""

    emoji: NotRequired[bool]
    text: str
    type: Literal["plain_text"]


class SlackMrkdwnTextObject(TypedDict):
    """A Slack ``mrkdwn`` composition object."""

    text: str
    type: Literal["mrkdwn"]
    verbatim: NotRequired[bool]


SlackTextObject = SlackMrkdwnTextObject | SlackPlainTextObject

_CONTROL_PATTERN = re.compile(r"[<>|]")
_DATE_CONTROL_PATTERN = re.compile(r"[\^|>]")
_SLACK_ID_PATTERN = re.compile(r"^[A-Z0-9_]+$")
_SLACK_USER_TOKEN_PATTERN = re.compile(r"(?<![<\w])@([A-Z][A-Z0-9_]+)")
# ``\A``/``\Z`` are JS ``^``/``$`` without the ``m`` flag (Python's ``$`` also
# matches before a trailing newline).
_SPECIAL_MENTION_PATTERN = re.compile(r"\A<!(here|channel|everyone)(?:\|[^<>]*)?>\Z")
_LABELED_GROUP_PATTERN = re.compile(r"\A<!subteam\^([A-Z0-9_]+)\|@?([^<>]+)>\Z")
_GROUP_PATTERN = re.compile(r"\A<!subteam\^([A-Z0-9_]+)>\Z")
_TEXT_OBJECT_MAX_LENGTH = 3000
_CODE_FENCE = "```"
_LEADING_WHITESPACE_PATTERN = re.compile(r"^[ \t]+")
# Line prefixes CommonMark promotes to a block construct (blockquote,
# heading, list item, fence, thematic break, HTML) -- in pre-unescape form.
# ``[0-9]`` is JS ``\d`` (Python's ``\d`` matches every Unicode digit).
_BLOCK_MARKER_PATTERN = re.compile(
    r"^(?:&gt;|&lt;|#{1,6}(?=[ \t\n]|\Z)|[-+*](?=[ \t\n]|\Z)|`{3,}|~{3,}|(?:[-*_][ \t]*){3,}(?=\n|\Z))"
)
_ORDERED_LIST_MARKER_PATTERN = re.compile(r"^([0-9]{1,9})([.)])(?=[ \t\n]|\Z)")
# First character that ends a ``<...>`` token scan: its close, or a line break.
_ANGLE_TOKEN_STOP = re.compile(r"[>\n\r]")
# What JS ``trimStart`` removes.
_JS_LEADING_WHITESPACE = re.compile(f"[{re.escape(_JS_WHITESPACE)}]*")


def escape_slack_text(text: str) -> str:
    """Escape Slack mrkdwn control characters (``&``, ``<``, ``>``)."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def unescape_slack_text(text: str) -> str:
    """Reverse :func:`escape_slack_text`."""
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def create_slack_plain_text(text: str, *, emoji: bool | None = None) -> SlackPlainTextObject:
    """Create a ``plain_text`` text object (1–3000 characters)."""
    _assert_slack_text_object_text(text)
    obj: SlackPlainTextObject = {"text": text, "type": "plain_text"}
    if emoji is not None:
        obj = {"emoji": emoji, "text": text, "type": "plain_text"}
    return obj


def create_slack_mrkdwn(text: str, *, verbatim: bool | None = None) -> SlackMrkdwnTextObject:
    """Create a ``mrkdwn`` text object (1–3000 characters)."""
    _assert_slack_text_object_text(text)
    obj: SlackMrkdwnTextObject = {"text": text, "type": "mrkdwn"}
    if verbatim is not None:
        obj["verbatim"] = verbatim
    return obj


def format_slack_user(user_id: str) -> str:
    """Format a user mention: ``<@U123>``."""
    _assert_slack_id(user_id, "user_id")
    return f"<@{user_id}>"


def format_slack_channel(channel_id: str) -> str:
    """Format a channel mention: ``<#C123>``."""
    _assert_slack_id(channel_id, "channel_id")
    return f"<#{channel_id}>"


def format_slack_user_group(user_group_id: str) -> str:
    """Format a user-group mention: ``<!subteam^S123>``."""
    _assert_slack_id(user_group_id, "user_group_id")
    return f"<!subteam^{user_group_id}>"


def format_slack_special_mention(mention: Literal["channel", "everyone", "here"]) -> str:
    """Format a special mention: ``<!here>`` / ``<!channel>`` / ``<!everyone>``."""
    return f"<!{mention}>"


def format_slack_link(url: str, label: str | None = None) -> str:
    """Format a link, escaping the label: ``<url|label>`` or ``<url>``."""
    _assert_no_slack_control(url, "url")
    return f"<{url}|{escape_slack_text(label)}>" if label else f"<{url}>"


def format_slack_date(
    timestamp: datetime | int | float,
    token: str,
    fallback: str,
    *,
    link: str | None = None,
) -> str:
    """Format a localized date token: ``<!date^ts^token[^link]|fallback>``.

    ``timestamp`` is an integer unix timestamp (seconds) or a
    :class:`~datetime.datetime` (pass timezone-aware values; naive datetimes
    are interpreted in local time by :meth:`datetime.timestamp`).
    """
    _assert_no_slack_date_control(token, "token")
    if isinstance(timestamp, datetime):
        seconds = math.floor(timestamp.timestamp())
    elif isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
        raise TypeError("timestamp must be an integer unix timestamp or datetime")
    elif isinstance(timestamp, float):
        if not timestamp.is_integer():
            raise TypeError("timestamp must be an integer unix timestamp or datetime")
        seconds = int(timestamp)
    else:
        seconds = timestamp
    link_part = f"^{_assert_slack_date_link(link)}" if link else ""
    return f"<!date^{seconds}^{token}{link_part}|{escape_slack_text(fallback)}>"


def slack_mrkdwn_to_markdown(mrkdwn: str) -> str:
    """Normalize Slack mrkdwn to standard Markdown.

    Rewrites user, channel and special mentions, links (including the
    inverted ``<label|https://...>`` form), bold, and strikethrough; puts
    each paired ```` ``` ```` fence on its own lines (Slack treats text right
    after an opening fence as code, CommonMark as the info string); then
    unescapes Slack's ``&amp;``/``&lt;``/``&gt;`` entities.
    """
    markdown = _convert_mrkdwn_with_code_fences(mrkdwn) if _CODE_FENCE in mrkdwn else _convert_mrkdwn_text(mrkdwn)
    return unescape_slack_text(markdown)


def _convert_slack_tokens(mrkdwn: str) -> str:
    # User mentions: <@U123|name> -> @name or <@U123> -> @U123
    markdown = re.sub(r"<@([A-Z0-9_]+)\|([^<>]+)>", r"@\2", mrkdwn)
    markdown = re.sub(r"<@([A-Z0-9_]+)>", r"@\1", markdown)
    # Channel mentions keep the id: <#C123|name> -> #name (C123)
    markdown = re.sub(r"<#([A-Z0-9_]+)\|([^<>]+)>", r"#\2 (\1)", markdown)
    markdown = re.sub(r"<#([A-Z0-9_]+)>", r"#\1", markdown)
    # Inverted links (frequently hallucinated): <label|https://...> -> <https://...|label>
    markdown = re.sub(r"<(?!https?://)([^<>|]+)\|(https?://[^|<>]+)>", r"<\2|\1>", markdown)
    # Links: <url|text> -> [text](url)
    markdown = re.sub(r"<(https?://[^|<>]+)\|([^<>]+)>", r"[\2](\1)", markdown)
    # Bare links: <url> -> url
    return re.sub(r"<(https?://[^<>]+)>", r"\1", markdown)


def _convert_mrkdwn_text(mrkdwn: str) -> str:
    markdown = _convert_slack_tokens(_convert_special_mentions(mrkdwn))
    # Bold: *text* -> **text** (Slack uses single * for bold)
    markdown = re.sub(r"(?<![_*\\])\*([^*\n]+)\*(?![_*])", r"**\1**", markdown)
    # Strikethrough: ~text~ -> ~~text~~
    return re.sub(r"(?<!~)~([^~\n]+)~(?!~)", r"~~\1~~", markdown)


class _AngleTokenScanner:
    """Upstream ``findAngleTokenEnd``, remembering the last failed scan.

    A failed scan stops at a line break or the end of the text, and every
    ``<`` before that stop fails the same way (no ``>`` lies in between).
    Remembering the stop keeps a run of unclosed ``<`` linear instead of
    rescanning to the end of the line for each one; results are identical.
    """

    __slots__ = ("_fail_until", "_text")

    def __init__(self, text: str) -> None:
        self._text = text
        self._fail_until = -1

    def end(self, index: int) -> int:
        """Index just past the ``>`` closing the token at *index*, or ``-1``."""
        if index < self._fail_until:
            return -1
        match = _ANGLE_TOKEN_STOP.search(self._text, index + 1)
        if match is not None and match.group() == ">":
            return match.end()
        self._fail_until = match.start() if match is not None else len(self._text)
        return -1


def _convert_special_mentions(mrkdwn: str) -> str:
    """``<!here>`` -> ``@here``, ``<!subteam^S1|@eng>`` -> ``@eng``; code stays literal."""
    parts: list[str] = []
    start = 0
    cursor = 0
    length = len(mrkdwn)
    angle = _AngleTokenScanner(mrkdwn)

    while cursor < length:
        if mrkdwn.startswith(_CODE_FENCE, cursor):
            cursor += len(_CODE_FENCE)
            continue
        char = mrkdwn[cursor]
        if char == "`":
            code_end = _find_inline_code_end(mrkdwn, cursor)
            cursor = cursor + 1 if code_end == -1 else code_end
            continue
        if char != "<":
            cursor += 1
            continue
        end = angle.end(cursor)
        if end == -1:
            cursor += 1
            continue
        token = mrkdwn[cursor:end]
        token = _SPECIAL_MENTION_PATTERN.sub(r"@\1", token, count=1)
        token = _LABELED_GROUP_PATTERN.sub(r"@\2", token, count=1)
        token = _GROUP_PATTERN.sub(r"@\1", token, count=1)
        parts.append(mrkdwn[start:cursor])
        parts.append(token)
        start = end
        cursor = end

    parts.append(mrkdwn[start:])
    return "".join(parts)


def _convert_mrkdwn_with_code_fences(mrkdwn: str) -> str:
    """Rewrite each paired ```` ``` ```` fence onto its own lines.

    Slack treats text immediately after an opening fence as code, while
    CommonMark treats it as the fence's info string. Everything Slack renders
    literally -- unpaired fences, fences inside inline code or ``<...>``
    tokens, and fences on blockquote lines -- stays plain text. Fence content
    skips the emphasis rewrites so code like ``*a`` survives verbatim.

    Mirrors :func:`chat_sdk.shared.code_fences.normalize_code_fences` with
    mrkdwn's entity escaping (``&gt;`` blockquotes, ``<...>`` control
    tokens), as upstream does. Keep the two in sync.
    """
    parts: list[str] = []
    # ``result.length`` / ``result.endsWith("\n")`` without re-joining ``parts``.
    result_len = 0
    result_ends_with_newline = False
    text_start = 0
    cursor = 0
    length = len(mrkdwn)
    angle = _AngleTokenScanner(mrkdwn)
    # Set when the closing fence splits a line: the text after it lands at the
    # start of a new line, where CommonMark would promote a leading block
    # marker Slack rendered inline.
    moved_to_own_line = False

    def append(value: str) -> None:
        nonlocal result_len, result_ends_with_newline
        if value:
            parts.append(value)
            result_len += len(value)
            result_ends_with_newline = value.endswith("\n")

    def flush_text_before(end: int) -> None:
        nonlocal moved_to_own_line
        text = mrkdwn[text_start:end]
        if moved_to_own_line:
            text = _escape_leading_block_marker(text)
            moved_to_own_line = False
        append(_convert_mrkdwn_text(text))

    while cursor < length:
        char = mrkdwn[cursor]
        if char == "<":
            token_end = angle.end(cursor)
            cursor = cursor + 1 if token_end == -1 else token_end
            continue
        if char != "`":
            cursor += 1
            continue
        if not mrkdwn.startswith(_CODE_FENCE, cursor):
            span_end = _find_inline_code_end(mrkdwn, cursor)
            cursor = cursor + 1 if span_end == -1 else span_end
            continue

        content_start = cursor + len(_CODE_FENCE)
        content_end = mrkdwn.find(_CODE_FENCE, content_start)
        if content_end == -1 or _is_on_blockquote_line(mrkdwn, cursor):
            # Slack renders an unpaired or quoted ``` literally.
            cursor = content_start
            continue

        flush_text_before(cursor)
        if result_len > 0 and not result_ends_with_newline:
            append("\n")
        content = mrkdwn[content_start:content_end]
        append(_CODE_FENCE)
        if not content.startswith("\n"):
            append("\n")
        append(_convert_slack_tokens(content))
        if not content.endswith("\n"):
            append("\n")
        append(_CODE_FENCE)

        cursor = content_end + len(_CODE_FENCE)
        text_start = cursor
        if cursor < length and mrkdwn[cursor] != "\n":
            append("\n")
            moved_to_own_line = True

    flush_text_before(length)
    return "".join(parts)


def _find_inline_code_end(mrkdwn: str, index: int) -> int:
    """End of the inline code span opening at *index*, or ``-1``.

    Slack inline code spans never cross line breaks. The newline search is
    bounded by the close (same result as upstream's ``newline < close``)
    so a long line of backticks stays linear.
    """
    close = mrkdwn.find("`", index + 1)
    if close == -1:
        return -1
    if mrkdwn.find("\n", index + 1, close) != -1:
        return -1
    return close + 1


def _is_on_blockquote_line(mrkdwn: str, index: int) -> bool:
    # Upstream ``slice(lineStart, index).trimStart().startsWith("&gt;")``,
    # matched in place so many fences on one long line copy nothing.
    line_start = mrkdwn.rfind("\n", 0, index) + 1
    indent = _JS_LEADING_WHITESPACE.match(mrkdwn, line_start, index)
    content_start = indent.end() if indent is not None else line_start
    return mrkdwn.startswith("&gt;", content_start, index)


def _escape_leading_block_marker(text: str) -> str:
    match = _LEADING_WHITESPACE_PATTERN.match(text)
    whitespace = match.group(0) if match else ""
    # Collapse the leading separator so it cannot become an indented code
    # block, then defuse any block marker now sitting at the line start.
    prefix = " " if whitespace else ""
    rest = text[len(whitespace) :]
    if _BLOCK_MARKER_PATTERN.match(rest):
        return f"{prefix}\\{rest}"
    return prefix + _ORDERED_LIST_MARKER_PATTERN.sub(r"\1\\\2", rest, count=1)


def markdown_bold_to_slack_mrkdwn(markdown: str) -> str:
    """Convert basic Markdown bold (``**text**``) to mrkdwn bold (``*text*``)."""
    return re.sub(r"\*\*(.+?)\*\*", r"*\1*", markdown)


def link_bare_slack_mentions(text: str) -> str:
    """Wrap bare Slack-ID-shaped mention tokens (``@U123``) as ``<@U123>``.

    ID-based to match Slack docs — emails and lowercase names are untouched.
    """
    return _SLACK_USER_TOKEN_PATTERN.sub(r"<@\1>", text)


def _assert_slack_text_object_text(text: str) -> None:
    if len(text) < 1 or len(text) > _TEXT_OBJECT_MAX_LENGTH:
        raise TypeError(f"text must be between 1 and {_TEXT_OBJECT_MAX_LENGTH} characters")


def _assert_slack_id(value: str, name: str) -> None:
    if not _SLACK_ID_PATTERN.match(value):
        raise TypeError(f"{name} must be a Slack ID")


def _assert_no_slack_control(value: str, name: str) -> None:
    if _CONTROL_PATTERN.search(value):
        raise TypeError(f"{name} cannot contain Slack control characters")


def _assert_no_slack_date_control(value: str, name: str) -> None:
    if _DATE_CONTROL_PATTERN.search(value):
        raise TypeError(f"{name} cannot contain Slack date control characters")


def _assert_slack_date_link(value: str) -> str:
    _assert_no_slack_date_control(value, "link")
    return value
