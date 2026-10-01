"""Discord-specific format conversion using AST-based parsing.

Discord uses standard markdown with some extensions:
- Bold: **text** (standard)
- Italic: *text* or _text_ (standard)
- Strikethrough: ~~text~~ (standard GFM)
- Links: [text](url) (standard)
- User mentions: <@userId>
- Channel mentions: <#channelId>
- Role mentions: <@&roleId>
- Custom emoji: <:name:id> or <a:name:id> (animated)
- Spoiler: ||text||
"""

from __future__ import annotations

import re

from chat_sdk.shared.base_format_converter import (
    BaseFormatConverter,
    Content,
    Root,
    get_node_children,
    get_node_value,
    parse_markdown,
    table_to_ascii,
)
from chat_sdk.shared.mentions import replace_bare_mentions

# ``[text](<https://...>)``: an angle-bracketed http(s) destination, which
# Discord renders as a masked link without an embed preview.
_SUPPRESSED_MASKED_URL = re.compile(r"^<(https?://[^<>\s]+)>$", re.IGNORECASE)

# Values of a link node's ``data["discordLinkStyle"]`` (upstream
# ``DiscordLinkStyle``).
SUPPRESSED_AUTOLINK = "suppressed-autolink"
SUPPRESSED_MASKED_LINK = "suppressed-masked-link"


class DiscordFormatConverter(BaseFormatConverter):
    """Discord-specific format converter.

    Transforms between standard markdown AST and Discord's markdown format.
    """

    def from_ast(self, ast: Root) -> str:
        """Render an AST to Discord markdown format."""
        return self._from_ast_with_node_converter(ast, self._node_to_discord_markdown)

    def to_ast(self, platform_text: str) -> Root:
        """Parse Discord markdown into an AST.

        Converts Discord-specific formats to standard markdown, then parses.
        """
        markdown = platform_text

        # User mentions: <@userId> or <@!userId> -> @userId
        markdown = re.sub(r"<@!?(\w+)>", r"@\1", markdown)

        # Channel mentions: <#channelId> -> #channelId
        markdown = re.sub(r"<#(\w+)>", r"#\1", markdown)

        # Role mentions: <@&roleId> -> @&roleId
        markdown = re.sub(r"<@&(\w+)>", r"@&\1", markdown)

        # Custom emoji: <:name:id> or <a:name:id> -> :name:
        markdown = re.sub(r"<a?:(\w+):\d+>", r":\1:", markdown)

        # Spoiler tags: ||text|| -> [spoiler: text]
        markdown = re.sub(r"\|\|([^|]+)\|\|", r"[spoiler: \1]", markdown)

        return self._parse_discord_markdown(markdown)

    def _parse_discord_markdown(self, markdown: str) -> Root:
        """Parse markdown, recording Discord's preview-suppressed link style.

        Upstream (#726) reads each link node's source span and tags
        ``[text](<https://...>)`` / ``<https://...>`` in ``node.data`` so
        :meth:`from_ast` can re-emit the angle brackets. The Python parser
        keeps no source positions, so the masked form is recognized from the
        destination instead: CommonMark's ``<...>`` delimiters are stripped
        from ``url`` (other adapters never see ``url="<https://...>"``) and
        the style recorded in ``data["discordLinkStyle"]``.

        ``<https://...>`` needs no marking: the Python parser has no autolink
        support, so it stays a text node whose brackets render verbatim (see
        docs/UPSTREAM_SYNC.md).
        """
        ast = parse_markdown(markdown)
        self._mark_discord_link_styles(get_node_children(ast))
        return ast

    def _mark_discord_link_styles(self, nodes: list[Content]) -> None:
        for node in nodes:
            if node.get("type") == "link":
                url = node.get("url") or ""
                if len(url) >= 2 and url.startswith("<") and url.endswith(">"):
                    inner = url[1:-1]
                    node["url"] = inner
                    # Upstream's source regex ends at ``)`` right after the
                    # ``>``, so a titled link is not marked either.
                    if _SUPPRESSED_MASKED_URL.match(url) and not node.get("title"):
                        node["data"] = {**(node.get("data") or {}), "discordLinkStyle": SUPPRESSED_MASKED_LINK}
            self._mark_discord_link_styles(get_node_children(node))

    def render_postable(self, message: object) -> str:
        """Override renderPostable to convert @mentions in plain strings.

        Extends the base implementation with Discord mention conversion
        and dataclass-style message support.
        """
        if isinstance(message, str):
            return self._convert_mentions_to_discord(message)
        if isinstance(message, dict):
            if "raw" in message:
                return self._convert_mentions_to_discord(message["raw"])
            if "markdown" in message:
                return self.from_ast(self._parse_discord_markdown(message["markdown"]))
            if "ast" in message:
                return self.from_ast(message["ast"])
            if "card" in message or message.get("type") == "card":
                return super().render_postable(message)
            return ""
        # Dataclass / object-style messages
        if hasattr(message, "raw"):
            return self._convert_mentions_to_discord(message.raw)
        if hasattr(message, "markdown"):
            return self.from_ast(self._parse_discord_markdown(message.markdown))
        if hasattr(message, "ast"):
            return self.from_ast(message.ast)
        # Fall back to base implementation for remaining cases (e.g. card objects)
        return super().render_postable(message)

    def _convert_mentions_to_discord(self, text: str) -> str:
        """Convert bare ``@mentions`` to Discord format (``@name`` -> ``<@name>``).

        Uses the shared scanner, which leaves emails, URLs, code spans and
        existing ``<@...>`` / ``<#...>`` / ``<https://...>`` tokens untouched.
        """
        return replace_bare_mentions(text, lambda _mention, name: f"<@{name}>")

    def _node_to_discord_markdown(self, node: Content) -> str:
        """Convert an AST node to Discord markdown."""
        node_type = node.get("type", "")

        if node_type == "paragraph":
            return "".join(self._node_to_discord_markdown(child) for child in get_node_children(node))

        if node_type == "text":
            # Convert bare @mentions to Discord format <@mention>. Upstream
            # parity (markdown.ts:106 + :164, verified under Node at
            # chat@4.41.1): a native mention glued to a word character, e.g.
            # ``<@1><@2>`` -> to_ast ``@1@2``, round-trips as ``<@1>@2``.
            return self._convert_mentions_to_discord(get_node_value(node))

        if node_type == "strong":
            content = "".join(self._node_to_discord_markdown(child) for child in get_node_children(node))
            return f"**{content}**"

        if node_type == "emphasis":
            content = "".join(self._node_to_discord_markdown(child) for child in get_node_children(node))
            return f"*{content}*"

        if node_type == "delete":
            content = "".join(self._node_to_discord_markdown(child) for child in get_node_children(node))
            return f"~~{content}~~"

        if node_type == "inlineCode":
            return f"`{get_node_value(node)}`"

        if node_type == "code":
            lang = node.get("lang", "") or ""
            return f"```{lang}\n{get_node_value(node)}\n```"

        if node_type == "link":
            link_text = "".join(self._node_to_discord_markdown(child) for child in get_node_children(node))
            url = node.get("url", "")
            link_style = (node.get("data") or {}).get("discordLinkStyle")
            if link_style == SUPPRESSED_AUTOLINK:
                return f"<{url}>"
            if link_style == SUPPRESSED_MASKED_LINK:
                return f"[{link_text}](<{url}>)"
            # Bare URLs (label == url) must stay bare: Discord renders masked
            # links ``[text](url)`` only in embeds, so ``[url](url)`` in a
            # normal message shows up as literal text.
            if link_text == url:
                return url
            return f"[{link_text}]({url})"

        if node_type == "blockquote":
            return "\n".join(f"> {self._node_to_discord_markdown(child)}" for child in get_node_children(node))

        if node_type == "list":
            return self._render_list(node, 0, self._node_to_discord_markdown)

        if node_type == "break":
            return "\n"

        if node_type == "thematicBreak":
            return "---"

        if node_type == "table":
            return f"```\n{table_to_ascii(node)}\n```"

        return self._default_node_to_text(node, self._node_to_discord_markdown)
