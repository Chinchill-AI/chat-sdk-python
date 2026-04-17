"""Google Chat-specific format conversion using AST-based parsing.

Google Chat supports a subset of text formatting:
- Bold: *text*
- Italic: _text_
- Strikethrough: ~text~
- Monospace: `text`
- Code blocks: ```text```
- Links are auto-detected

Very similar to Slack's mrkdwn format.
"""

from __future__ import annotations

import re

from chat_sdk.shared.base_format_converter import (
    BaseFormatConverter,
    Content,
    Root,
    parse_markdown,
    table_to_ascii,
)


class GoogleChatFormatConverter(BaseFormatConverter):
    """Format converter between standard markdown AST and Google Chat format.

    Uses the shared AST infrastructure with platform-specific node rendering.
    """

    def from_ast(self, ast: Root) -> str:
        """Render an AST to Google Chat format."""
        return self._from_ast_with_node_converter(ast, self._node_to_gchat)

    def to_ast(self, gchat_text: str) -> Root:
        """Parse Google Chat message into an AST.

        Converts Google Chat format to standard markdown, then parses
        with the shared parser.
        """
        markdown = gchat_text

        # Google Chat custom link syntax <url|text> -> [text](url). Must run
        # before bold/strikethrough so the `|` inside a link label isn't
        # matched by those patterns.
        markdown = re.sub(r"<(https?://[^|\s>]+)\|([^>]+)>", r"[\2](\1)", markdown)

        # Bold: *text* -> **text**
        markdown = re.sub(r"(?<![_*\\])\*([^*\n]+)\*(?![_*])", r"**\1**", markdown)

        # Strikethrough: ~text~ -> ~~text~~
        markdown = re.sub(r"(?<!~)~([^~\n]+)~(?!~)", r"~~\1~~", markdown)

        # Italic and code are the same format as markdown
        return parse_markdown(markdown)

    def extract_plain_text(self, text: str) -> str:
        """Extract plain text from Google Chat formatted text.

        Strips formatting markers while preserving the text content.
        """
        # Remove code blocks
        result = re.sub(r"```[\s\S]*?```", lambda m: m.group(0).strip("`").strip(), text)
        # Inline code
        result = re.sub(r"`([^`]+)`", r"\1", result)
        # Google Chat custom link syntax: <url|text> -> text
        result = re.sub(r"<https?://[^|\s>]+\|([^>]+)>", r"\1", result)
        # Bold markers (*text*)
        result = re.sub(r"\*([^*]+)\*", r"\1", result)
        # Italic markers (_text_)
        result = re.sub(r"_([^_]+)_", r"\1", result)
        # Strikethrough markers (~text~)
        result = re.sub(r"~([^~]+)~", r"\1", result)
        return result.strip()

    def _node_to_gchat(self, node: Content) -> str:
        """Convert an AST node to Google Chat format."""
        node_type = node.get("type", "")

        if node_type == "paragraph":
            children = node.get("children", [])
            return "".join(self._node_to_gchat(child) for child in children)

        if node_type == "text":
            return node.get("value", "")

        if node_type == "strong":
            # Markdown **text** -> GChat *text*
            content = "".join(self._node_to_gchat(child) for child in node.get("children", []))
            return f"*{content}*"

        if node_type == "emphasis":
            # Both use _text_
            content = "".join(self._node_to_gchat(child) for child in node.get("children", []))
            return f"_{content}_"

        if node_type == "delete":
            # Markdown ~~text~~ -> GChat ~text~
            content = "".join(self._node_to_gchat(child) for child in node.get("children", []))
            return f"~{content}~"

        if node_type == "inlineCode":
            return f"`{node.get('value', '')}`"

        if node_type == "code":
            return f"```\n{node.get('value', '')}\n```"

        if node_type == "link":
            # Google Chat supports custom link labels using <url|text> syntax.
            children = node.get("children", [])
            link_text = "".join(self._node_to_gchat(child) for child in children)
            url = node.get("url", "")
            if link_text == url:
                return url
            return f"<{url}|{link_text}>"

        if node_type == "heading":
            # Intentional improvement over TS SDK: Google Chat has no heading
            # syntax, so we wrap headings in bold (*...*) for visual emphasis.
            children = node.get("children", [])
            content = "".join(self._node_to_gchat(child) for child in children)
            return f"*{content}*"

        if node_type == "blockquote":
            # Google Chat doesn't have native blockquote, use > prefix
            children = node.get("children", [])
            lines = []
            for child in children:
                lines.append(f"> {self._node_to_gchat(child)}")
            return "\n".join(lines)

        if node_type == "list":
            return self._render_list(node, 0, self._node_to_gchat, "\u2022")

        if node_type == "break":
            return "\n"

        if node_type == "thematicBreak":
            return "---"

        if node_type == "table":
            return f"```\n{table_to_ascii(node)}\n```"

        if node_type == "image":
            alt = node.get("alt", "")
            url = node.get("url", "")
            if alt:
                return f"{alt} ({url})"
            return url

        # Default: try to render children
        return self._default_node_to_text(node, self._node_to_gchat)
