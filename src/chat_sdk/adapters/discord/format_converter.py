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

        return parse_markdown(markdown)

    def render_postable(self, message: object) -> str:
        """Override renderPostable to convert @mentions in plain strings."""
        if isinstance(message, str):
            return self._convert_mentions_to_discord(message)
        if isinstance(message, dict):
            if "raw" in message:
                return self._convert_mentions_to_discord(message["raw"])
            if "markdown" in message:
                return self.from_ast(parse_markdown(message["markdown"]))
            if "ast" in message:
                return self.from_ast(message["ast"])
        return ""

    def _convert_mentions_to_discord(self, text: str) -> str:
        """Convert @mentions to Discord format: @name -> <@name>."""
        return re.sub(r"@(\w+)", r"<@\1>", text)

    def _node_to_discord_markdown(self, node: Content) -> str:
        """Convert an AST node to Discord markdown."""
        node_type = node.get("type", "")

        if node_type == "paragraph":
            return "".join(self._node_to_discord_markdown(child) for child in get_node_children(node))

        if node_type == "text":
            # Convert @mentions to Discord format <@mention>
            return re.sub(r"@(\w+)", r"<@\1>", get_node_value(node))

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
            return f"[{link_text}]({node.get('url', '')})"

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
