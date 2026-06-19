"""Teams format primitives — a lightweight, runtime-free subpath.

Port of ``packages/adapter-teams/src/format/index.ts`` (vercel/chat@4.31,
commit 8c71411), exposed upstream as ``@chat-adapter/teams/format``. Provides
runtime-free primitives for escaping/unescaping Teams text, building and
normalizing ``<at>`` mentions, and converting between Teams' restricted HTML
subset and Markdown-ish text — without the full Teams adapter, the
``microsoft_teams`` SDK, or the chat runtime.

This is intentionally distinct from :mod:`chat_sdk.adapters.teams.format_converter`,
which is a higher-level AST-based converter. The helpers here are low-level
string primitives that operate purely on text.

Importing this module never imports the ``microsoft_teams`` SDK, an HTTP
client, or the high-level :mod:`chat_sdk.adapters.teams.adapter` module. Emoji
placeholder conversion delegates to :mod:`chat_sdk.emoji` so the emoji map is
never duplicated here.

Python-specific hardening (divergence from upstream, see
``docs/UPSTREAM_SYNC.md``): :func:`markdown_to_teams_html` gates link hrefs
through an exact ``{http, https, mailto}`` protocol allowlist using
:func:`urllib.parse.urlparse` (port of the upstream ``URL().protocol`` check),
rejecting ``javascript:``, ``data:``, relative, and other unsafe hrefs so they
render as plain text rather than active links (SSRF / injection guard). Because
``urlparse`` is more lenient than the upstream ``new URL(...)`` (which throws on
a bare-scheme href like ``http:`` or ``https://``), the ``http``/``https``
branch additionally requires a non-empty host so those malformed hrefs are
rejected to parity.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from chat_sdk.emoji import convert_emoji_placeholders

__all__ = [
    "convert_teams_emoji_placeholders",
    "escape_teams_text",
    "format_teams_mention",
    "markdown_to_teams_html",
    "safe_link_href",
    "teams_html_to_markdown",
    "teams_mention_to_plain_text",
    "unescape_teams_text",
]

# JS source patterns ported 1:1. The `gis` flags become DOTALL | IGNORECASE in
# Python; JS `g` (replace-all) is the default for `re.sub`/`str.replace`.
_HTML_ESCAPE_PATTERN = re.compile(r"[&<>\"]")
_MARKDOWN_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_TEAMS_MENTION_PATTERN = re.compile(r"<at\b[^>]*>(.*?)</at>", re.DOTALL | re.IGNORECASE)
_TAG_PATTERN = re.compile(r"<[^>]+>")

# Order matters: `&` is escaped via the single-pass regex below so an already
# present `&` is not double-escaped. Matches upstream `HTML_ESCAPES`.
_HTML_ESCAPES: dict[str, str] = {
    '"': "&quot;",
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
}

# Upstream `EMOJI_PLACEHOLDERS` maps Slack-style colon placeholders to unicode.
# Rather than re-declare the unicode (which would duplicate the emoji map), we
# map each upstream placeholder to its normalized name in :mod:`chat_sdk.emoji`
# and delegate the unicode lookup to that single source of truth.
_PLACEHOLDER_TO_NORMALIZED: dict[str, str] = {
    ":red_circle:": "red_circle",
    ":warning:": "warning",
    ":white_check_mark:": "check",
    ":x:": "x",
}

# Exact protocol allowlist for Markdown link hrefs (port of upstream
# `SAFE_LINK_PROTOCOLS`). `urlparse` lowercases the scheme and yields it
# without the trailing colon, so these are bare scheme names.
_SAFE_LINK_PROTOCOLS: frozenset[str] = frozenset({"http", "https", "mailto"})


def escape_teams_text(text: str) -> str:
    """Escape the Teams HTML control characters (``&``, ``<``, ``>``, ``"``).

    Run this before inserting any HTML tags so user-supplied ``<`` cannot
    forge markup.
    """
    return _HTML_ESCAPE_PATTERN.sub(lambda m: _HTML_ESCAPES.get(m.group(0), m.group(0)), text)


def unescape_teams_text(text: str) -> str:
    """Reverse :func:`escape_teams_text`.

    Entities are replaced in reverse order (``&amp;`` last) so a literal
    ``&amp;lt;`` does not collapse into ``<``.
    """
    return text.replace("&quot;", '"').replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def format_teams_mention(name: str) -> str:
    """Wrap a display name in an escaped Teams ``<at>`` mention tag."""
    return f"<at>{escape_teams_text(name)}</at>"


def teams_mention_to_plain_text(text: str) -> str:
    """Replace Teams ``<at>...</at>`` mention tags with ``@name`` plain text."""

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        return f"@{unescape_teams_text(_strip_tags(name).strip())}"

    return _TEAMS_MENTION_PATTERN.sub(_replace, text)


def teams_html_to_markdown(html: str) -> str:
    """Convert Teams' restricted HTML subset to Markdown-ish text."""
    text = teams_mention_to_plain_text(html)
    text = re.sub(r"<strong\b[^>]*>(.*?)</strong>", r"**\1**", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<b\b[^>]*>(.*?)</b>", r"**\1**", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<em\b[^>]*>(.*?)</em>", r"_\1_", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<i\b[^>]*>(.*?)</i>", r"_\1_", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<s\b[^>]*>(.*?)</s>", r"~~\1~~", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<strike\b[^>]*>(.*?)</strike>", r"~~\1~~", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<code\b[^>]*>(.*?)</code>", r"`\1`", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(
        r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
        r"[\2](\1)",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>\s*<p[^>]*>", "\n\n", text, flags=re.IGNORECASE)
    text = _TAG_PATTERN.sub("", text)
    text = text.replace(" ", " ")
    return unescape_teams_text(text).strip()


def markdown_to_teams_html(markdown: str) -> str:
    """Convert Markdown-ish text to Teams' restricted HTML subset.

    The input is escaped *before* any tag insertion so user-supplied ``<``
    cannot forge HTML. Link hrefs are gated through :func:`safe_link_href`;
    unsafe hrefs render as plain label text.
    """
    text = convert_teams_emoji_placeholders(escape_teams_text(markdown))
    text = re.sub(r"\*\*(.*?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"_(.*?)_", r"<em>\1</em>", text)
    text = re.sub(r"~~(.*?)~~", r"<s>\1</s>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)

    def _link(match: re.Match[str]) -> str:
        label, href = match.group(1), match.group(2)
        return f'<a href="{href}">{label}</a>' if safe_link_href(href) else label

    text = _MARKDOWN_LINK_PATTERN.sub(_link, text)
    return text.replace("\n", "<br>")


def convert_teams_emoji_placeholders(text: str) -> str:
    """Convert Teams' Slack-style colon emoji placeholders to unicode.

    Delegates the unicode lookup to :mod:`chat_sdk.emoji` so the emoji map is
    never duplicated. Each upstream placeholder (``:white_check_mark:`` etc.)
    is mapped to its normalized SDK emoji name and resolved to the platform
    (unicode) form via :func:`chat_sdk.emoji.convert_emoji_placeholders`.
    """
    converted = text
    for placeholder, normalized in _PLACEHOLDER_TO_NORMALIZED.items():
        converted = converted.replace(
            placeholder,
            convert_emoji_placeholders(f"{{{{emoji:{normalized}}}}}", "teams"),
        )
    return converted


def _strip_tags(text: str) -> str:
    return _TAG_PATTERN.sub("", text)


def safe_link_href(href: str) -> bool:
    """Return ``True`` only for ``http``/``https``/``mailto`` hrefs.

    Port of the upstream ``safeLinkHref`` protocol check using
    :func:`urllib.parse.urlparse`. Rejects ``javascript:``, ``data:``,
    relative, and any other scheme (SSRF / injection guard).

    Upstream parses the href with ``new URL(href)``, which *throws* for a
    malformed bare-scheme href like ``http:`` or ``https://`` (no authority),
    so such hrefs are rejected and render as plain label text.
    :func:`urllib.parse.urlparse` is lenient and would yield a matching scheme
    with an empty ``netloc`` for those, so the ``http``/``https`` branch
    additionally requires a non-empty host to match upstream. ``mailto:`` has
    no ``netloc`` by design and stays allowed.
    """
    try:
        parsed = urlparse(href)
    except ValueError:
        return False
    if parsed.scheme not in _SAFE_LINK_PROTOCOLS:
        return False
    if parsed.scheme in ("http", "https"):
        return bool(parsed.netloc)
    return True
