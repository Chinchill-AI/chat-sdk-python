"""Slack-specific format conversion using AST-based parsing.

Port of markdown.ts from the Vercel Chat SDK Slack adapter.

Slack uses "mrkdwn" format which is similar but not identical to markdown:
- Bold: *text* (not **text**)
- Italic: _text_ (same)
- Strikethrough: ~text~ (not ~~text~~)
- Links: <url|text> (not [text](url))
- User mentions: <@U123>
- Channel mentions: <#C123|name>
"""

from __future__ import annotations

import re
from typing import Any

from chat_sdk.adapters.slack.cards import SlackBlock
from chat_sdk.shared.base_format_converter import (
    BaseFormatConverter,
    Content,
    Root,
    parse_markdown,
    table_to_ascii,
)


class SlackFormatConverter(BaseFormatConverter):
    """Convert between Slack mrkdwn and standard markdown / plain text."""

    # -------------------------------------------------------------------------
    # Core AST methods (required by BaseFormatConverter)
    # -------------------------------------------------------------------------

    def from_ast(self, ast: Root) -> str:
        """Render an AST to Slack mrkdwn format."""
        return self._from_ast_with_node_converter(ast, self._node_to_mrkdwn)

    def to_ast(self, platform_text: str) -> Root:
        """Parse Slack mrkdwn into an AST.

        Converts Slack-specific syntax to standard markdown, then parses
        with the shared parser.
        """
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

        # Bold: *text* -> **text** (careful with emphasis)
        markdown = re.sub(r"(?<![_*\\])\*([^*\n]+)\*(?![_*])", r"**\1**", markdown)

        # Strikethrough: ~text~ -> ~~text~~
        markdown = re.sub(r"(?<!~)~([^~\n]+)~(?!~)", r"~~\1~~", markdown)

        return parse_markdown(markdown)

    # -------------------------------------------------------------------------
    # Overrides
    # -------------------------------------------------------------------------

    def render_postable(self, message: Any) -> str:
        """Render a postable message to Slack mrkdwn string.

        Supports str, ``{"raw": ...}``, ``{"markdown": ...}``, and ``{"ast": ...}``.
        """
        if isinstance(message, str):
            return self._convert_mentions_to_slack(message)
        if hasattr(message, "raw"):
            return self._convert_mentions_to_slack(message.raw)
        if isinstance(message, dict):
            if "raw" in message:
                return self._convert_mentions_to_slack(message["raw"])
            if "markdown" in message:
                return self.from_markdown(message["markdown"])
            if "ast" in message:
                return self.from_ast(message["ast"])
        # Dataclass-style objects
        if hasattr(message, "markdown"):
            return self.from_markdown(message.markdown)
        if hasattr(message, "ast"):
            return self.from_ast(message.ast)
        return ""

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
    # Slack table block support
    # -------------------------------------------------------------------------

    def to_blocks_with_table(self, ast: Root) -> list[SlackBlock] | None:
        """Convert AST to Slack blocks, using native table block for the first table.

        Returns None if the AST contains no tables (caller should use regular text).
        Slack allows at most one table block per message; additional tables use ASCII.
        """
        if not isinstance(ast, dict):
            return None

        children = ast.get("children", [])
        has_table = any(isinstance(node, dict) and node.get("type") == "table" for node in children)
        if not has_table:
            return None

        blocks: list[SlackBlock] = []
        used_native_table = False
        text_buffer: list[str] = []

        def flush_text() -> None:
            nonlocal text_buffer
            if text_buffer:
                text = "\n\n".join(text_buffer)
                if text.strip():
                    blocks.append(
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": text},
                        }
                    )
                text_buffer = []

        for child in children:
            node = child if isinstance(child, dict) else {}
            if node.get("type") == "table":
                flush_text()
                if used_native_table:
                    # Additional tables fall back to ASCII in a code block
                    ascii_table = table_to_ascii(node)
                    blocks.append(
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"```\n{ascii_table}\n```",
                            },
                        }
                    )
                else:
                    blocks.append(self._mdast_table_to_slack_block(node))
                    used_native_table = True
            else:
                text_buffer.append(self._node_to_mrkdwn(node))

        flush_text()
        return blocks

    # -------------------------------------------------------------------------
    # Private helpers
    # -------------------------------------------------------------------------

    def _convert_mentions_to_slack(self, text: str) -> str:
        """Convert @mentions to Slack format: @name -> <@name>."""
        return re.sub(r"(?<!<)@(\w+)", r"<@\1>", text)

    def _markdown_to_mrkdwn(self, text: str) -> str:
        """Convert standard Markdown to Slack mrkdwn."""
        result = text

        # Bold: **text** -> *text*
        result = re.sub(r"\*\*(.+?)\*\*", r"*\1*", result)

        # Strikethrough: ~~text~~ -> ~text~
        result = re.sub(r"~~(.+?)~~", r"~\1~", result)

        # Links: [text](url) -> <url|text>
        result = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<\2|\1>", result)

        # Mentions
        result = self._convert_mentions_to_slack(result)

        return result

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
            return re.sub(r"(?<!<)@(\w+)", r"<@\1>", value)

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
            return self._render_list(node, 0, self._node_to_mrkdwn, "\u2022")

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

    def _mdast_table_to_slack_block(self, node: Content) -> SlackBlock:
        """Convert a table AST node to a Slack table block.

        @see https://docs.slack.dev/reference/block-kit/blocks/table-block/
        """
        rows_data: list[list[dict[str, str]]] = []

        for row in node.get("children", []):
            cells = []
            for cell in row.get("children", []):
                # Convert cell children to text, defaulting to a space if empty.
                # Slack API requires table cell text to be at least 1 character.
                # Use an explicit length check rather than a truthiness check to
                # avoid substituting valid strings like "0".
                raw_text = "".join(self._node_to_mrkdwn(c) for c in cell.get("children", []))
                text = raw_text if len(raw_text) > 0 else " "
                cells.append({"type": "raw_text", "text": text})
            rows_data.append(cells)

        block: SlackBlock = {"type": "table", "rows": rows_data}

        align = node.get("align")
        if align:
            column_settings = [{"align": a or "left"} for a in align]
            block["column_settings"] = column_settings

        return block


# Backwards compatibility alias
SlackMarkdownConverter = SlackFormatConverter
