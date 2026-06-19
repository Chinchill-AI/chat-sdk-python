"""Slack format conversion.

Port of markdown.ts from the Vercel Chat SDK Slack adapter.

Outgoing: Slack now natively renders markdown via the ``markdown_text`` field
on chat.postMessage / postEphemeral / update / scheduleMessage. We pass
markdown through there and let Slack handle it. Interactive ``response_url``
payloads do not accept ``markdown_text``, so those still use Slack mrkdwn text.

Incoming: Slack ``message`` events still deliver text as mrkdwn
(``*bold*``, ``<@U123>``, ``<url|text>``), so the ``to_ast`` parser stays.
"""

from __future__ import annotations

import re
from typing import Any

from chat_sdk.emoji import convert_emoji_placeholders
from chat_sdk.shared.base_format_converter import (
    BaseFormatConverter,
    Content,
    Root,
    parse_markdown,
    stringify_markdown,
    table_to_ascii,
)

# Match bare @mentions (e.g. "@george") to rewrite as Slack's `<@george>`.
# The fixed-width lookbehind excludes:
#   - `<` (already-formatted mentions like `<@U123>`),
#   - any word character (so email addresses like `user@example.com` are left
#     alone), and
#   - `/` (so a schemeless host path like `mastodon.social/@user` — which the
#     URL matcher in `_link_bare_mentions_outside_urls` does NOT catch — is also
#     preserved). Mirrors upstream's `(?<![<\w/])` (vercel/chat a8bf99a).
# ``re.ASCII`` keeps `\w` ASCII-only to match upstream's JS ASCII `\w` exactly:
# Python's `\w` is Unicode-aware by default, which would otherwise diverge on
# non-ASCII handles/boundaries.
BARE_MENTION_REGEX = re.compile(r"(?<![<\w/])@(\w+)", re.ASCII)

# Match an `http(s)` URL up to the first whitespace or angle bracket so the span
# can be excluded from mention linking. A bare `@handle` anywhere inside a URL
# (path, query string, or fragment) must NOT be rewritten into a `<@handle>`
# Slack mention, which would corrupt the link. Mirrors upstream's `URL_PATTERN`.
URL_REGEX = re.compile(r"\bhttps?://[^\s<>]+")


def _link_bare_mentions_outside_urls(text: str) -> str:
    """Rewrite bare ``@handle`` mentions, but only outside ``http(s)`` URL spans.

    A bare ``@handle`` anywhere inside a URL (path, query string, or fragment)
    is preserved verbatim; mention linking is applied only to the text slices
    between (and after) URL spans. Mirrors upstream's
    ``linkBareMentionsOutsideUrls`` (vercel/chat a8bf99a). The schemeless host
    path case (``mastodon.social/@user``), which ``URL_REGEX`` does not match,
    is additionally guarded by the ``/`` in ``BARE_MENTION_REGEX``'s lookbehind.
    """
    result: list[str] = []
    last_index = 0
    for match in URL_REGEX.finditer(text):
        start = match.start()
        result.append(BARE_MENTION_REGEX.sub(r"<@\1>", text[last_index:start]))
        result.append(match.group(0))
        last_index = match.end()
    result.append(BARE_MENTION_REGEX.sub(r"<@\1>", text[last_index:]))
    return "".join(result)


class SlackFormatConverter(BaseFormatConverter):
    """Convert between Slack formats and standard markdown / plain text."""

    # -------------------------------------------------------------------------
    # Core AST methods (required by BaseFormatConverter)
    # -------------------------------------------------------------------------

    def from_ast(self, ast: Root) -> str:
        """Render an AST to standard markdown.

        Slack accepts this directly via ``markdown_text`` and the
        ``markdown`` block.
        """
        return stringify_markdown(ast)

    def to_ast(self, platform_text: str) -> Root:
        """Parse Slack mrkdwn into an AST. Used for incoming ``message`` events."""
        markdown = platform_text

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

        return parse_markdown(markdown)

    # -------------------------------------------------------------------------
    # Outgoing payload builders
    # -------------------------------------------------------------------------

    def to_slack_payload(self, message: Any) -> dict[str, str]:
        """Build the Slack API payload fields for a message.

        - ``str`` / ``{"raw"}`` -> ``{"text"}`` (plain -- preserves literal
          ``*``, ``_``, etc.)
        - ``{"markdown"}`` / ``{"ast"}`` -> ``{"markdown_text"}`` (Slack
          renders natively)

        Bare ``@user`` mentions are rewritten to ``<@user>`` and ``:emoji:``
        placeholders are normalized for Slack in all branches.

        Note: ``markdown_text`` has a 12,000 character limit; ``text`` allows
        ~40,000.
        Note: ``markdown_text`` is mutually exclusive with ``text`` and
        ``blocks``.
        """
        if isinstance(message, str):
            return {"text": self._finalize(message)}
        if isinstance(message, dict):
            if "raw" in message:
                return {"text": self._finalize(message["raw"])}
            if "markdown" in message:
                return {"markdown_text": self._finalize(message["markdown"])}
            if "ast" in message:
                return {"markdown_text": self._finalize(stringify_markdown(message["ast"]))}
            return {"text": ""}
        # Dataclass / object-style messages
        if getattr(message, "raw", None) is not None:
            return {"text": self._finalize(message.raw)}
        if getattr(message, "markdown", None) is not None:
            return {"markdown_text": self._finalize(message.markdown)}
        if getattr(message, "ast", None) is not None:
            return {"markdown_text": self._finalize(stringify_markdown(message.ast))}
        return {"text": ""}

    def to_response_url_text(self, message: Any) -> str:
        """Build text for Slack response_url payloads.

        Slack rejects ``markdown_text`` on response_url (``no_text``), so
        markdown/AST messages are rendered to Slack's legacy mrkdwn format
        for this surface.
        """
        if isinstance(message, str):
            return self._finalize(message)
        if isinstance(message, dict):
            if "raw" in message:
                return self._finalize(message["raw"])
            if "markdown" in message:
                return convert_emoji_placeholders(self._ast_to_mrkdwn(parse_markdown(message["markdown"])), "slack")
            if "ast" in message:
                return convert_emoji_placeholders(self._ast_to_mrkdwn(message["ast"]), "slack")
            return ""
        # Dataclass / object-style messages
        if getattr(message, "raw", None) is not None:
            return self._finalize(message.raw)
        if getattr(message, "markdown", None) is not None:
            return convert_emoji_placeholders(self._ast_to_mrkdwn(parse_markdown(message.markdown)), "slack")
        if getattr(message, "ast", None) is not None:
            return convert_emoji_placeholders(self._ast_to_mrkdwn(message.ast), "slack")
        return ""

    # -------------------------------------------------------------------------
    # Overrides
    # -------------------------------------------------------------------------

    def extract_plain_text(self, platform_text: str) -> str:
        """Extract plain text from Slack mrkdwn by stripping formatting."""
        text = platform_text

        # Remove user mentions formatting: <@U123|name> -> @name, <@U123> -> @U123
        text = re.sub(r"<@([A-Z0-9_]+)\|([^<>]+)>", r"@\2", text)
        text = re.sub(r"<@([A-Z0-9_]+)>", r"@\1", text)

        # Remove channel mentions: <#C123|name> -> #name
        text = re.sub(r"<#[A-Z0-9_]+\|([^<>]+)>", r"#\1", text)
        text = re.sub(r"<#([A-Z0-9_]+)>", r"#\1", text)

        # Remove links formatting: <url|text> -> text, <url> -> url
        text = re.sub(r"<(https?://[^|<>]+)\|([^<>]+)>", r"\2", text)
        text = re.sub(r"<(https?://[^<>]+)>", r"\1", text)

        # Remove bold/italic/strikethrough markers
        text = re.sub(r"\*([^*]+)\*", r"\1", text)
        text = re.sub(r"_([^_]+)_", r"\1", text)
        text = re.sub(r"~([^~]+)~", r"\1", text)

        return text

    # -------------------------------------------------------------------------
    # Private helpers
    # -------------------------------------------------------------------------

    def _finalize(self, text: str) -> str:
        """Rewrite bare @mentions and normalize emoji placeholders for Slack."""
        return convert_emoji_placeholders(_link_bare_mentions_outside_urls(text), "slack")

    def _ast_to_mrkdwn(self, ast: Root) -> str:
        """Render an AST to Slack's legacy mrkdwn (response_url surface only)."""
        return self._from_ast_with_node_converter(ast, self._node_to_mrkdwn)

    def _node_to_mrkdwn(self, node: Content) -> str:
        """Convert a single AST node to Slack mrkdwn."""
        if not isinstance(node, dict):
            return str(node) if node else ""

        node_type = node.get("type", "")
        children = node.get("children", [])

        if node_type == "paragraph":
            return "".join(self._node_to_mrkdwn(c) for c in children)

        if node_type == "text":
            value = node.get("value", "")
            return _link_bare_mentions_outside_urls(value)

        if node_type == "strong":
            content = "".join(self._node_to_mrkdwn(c) for c in children)
            return f"*{content}*"

        if node_type == "emphasis":
            content = "".join(self._node_to_mrkdwn(c) for c in children)
            return f"_{content}_"

        if node_type == "delete":
            content = "".join(self._node_to_mrkdwn(c) for c in children)
            return f"~{content}~"

        if node_type == "inlineCode":
            return f"`{node.get('value', '')}`"

        if node_type == "code":
            lang = node.get("lang", "")
            return f"```{lang}\n{node.get('value', '')}\n```"

        if node_type == "link":
            link_text = "".join(self._node_to_mrkdwn(c) for c in children)
            return f"<{node.get('url', '')}|{link_text}>"

        if node_type == "heading":
            # Intentional improvement over TS SDK: Slack mrkdwn has no heading
            # syntax, so we wrap headings in bold (*...*) for visual emphasis.
            content = "".join(self._node_to_mrkdwn(c) for c in children)
            return f"*{content}*"

        if node_type == "blockquote":
            return "\n".join(f"> {self._node_to_mrkdwn(c)}" for c in children)

        if node_type == "list":
            return self._render_list(node, 0, self._node_to_mrkdwn, "•")

        if node_type == "break":
            return "\n"

        if node_type == "thematicBreak":
            return "---"

        if node_type == "table":
            return f"```\n{table_to_ascii(node)}\n```"

        if node_type == "image":
            url = node.get("url", "")
            alt = node.get("alt", "")
            if alt:
                return f"{alt} ({url})"
            return url

        # Default fallback for any node with children
        return self._default_node_to_text(node, self._node_to_mrkdwn)
