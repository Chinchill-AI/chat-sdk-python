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
_TEXT_OBJECT_MAX_LENGTH = 3000


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

    Rewrites user/channel mentions, links, bold, and strikethrough, then
    unescapes Slack's ``&amp;``/``&lt;``/``&gt;`` entities.
    """
    markdown = mrkdwn
    # User mentions: <@U123|name> -> @name or <@U123> -> @U123
    markdown = re.sub(r"<@([A-Z0-9_]+)\|([^<>]+)>", r"@\2", markdown)
    markdown = re.sub(r"<@([A-Z0-9_]+)>", r"@\1", markdown)
    # Channel mentions: <#C123|name> -> #name
    markdown = re.sub(r"<#[A-Z0-9_]+\|([^<>]+)>", r"#\1", markdown)
    markdown = re.sub(r"<#([A-Z0-9_]+)>", r"#\1", markdown)
    # Links: <url|text> -> [text](url)
    markdown = re.sub(r"<(https?://[^|<>]+)\|([^<>]+)>", r"[\2](\1)", markdown)
    # Bare links: <url> -> url
    markdown = re.sub(r"<(https?://[^<>]+)>", r"\1", markdown)
    # Bold: *text* -> **text** (Slack uses single * for bold)
    markdown = re.sub(r"(?<![_*\\])\*([^*\n]+)\*(?![_*])", r"**\1**", markdown)
    # Strikethrough: ~text~ -> ~~text~~
    markdown = re.sub(r"(?<!~)~([^~\n]+)~(?!~)", r"~~\1~~", markdown)
    return unescape_slack_text(markdown)


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
