"""Teams-specific format conversion using AST-based parsing.

Teams supports a subset of HTML for formatting:
- Bold: <b> or <strong>
- Italic: <i> or <em>
- Strikethrough: <s> or <strike>
- Links: <a href="url">text</a>
- Code: <pre> and <code>

Teams also accepts standard markdown in most cases.
"""

from __future__ import annotations

import re

from chat_sdk.adapters.teams.format import strip_html_tags
from chat_sdk.shared.base_format_converter import (
    BaseFormatConverter,
    Content,
    Root,
    get_node_children,
    get_node_value,
    parse_markdown,
)
from chat_sdk.shared.card_utils import escape_table_cell


class TeamsFormatConverter(BaseFormatConverter):
    """Teams-specific format converter.

    Transforms between standard markdown AST and Teams format.
    """

    def from_ast(self, ast: Root) -> str:
        """Render an AST to Teams format.

        Teams accepts standard markdown, so we just stringify cleanly.
        """
        return self._from_ast_with_node_converter(ast, self._node_to_teams)

    def to_ast(self, platform_text: str) -> Root:
        """Parse Teams message into an AST.

        Converts Teams HTML/mentions to standard markdown format.
        """
        markdown = platform_text

        # Convert @mentions from Teams format: <at>Name</at> -> @Name
        markdown = re.sub(r"<at>([^<]+)</at>", r"@\1", markdown, flags=re.IGNORECASE)

        # Convert HTML tags to markdown
        # Bold: <b>, <strong> -> **text**
        markdown = re.sub(r"<(b|strong)>([^<]+)</(b|strong)>", r"**\2**", markdown, flags=re.IGNORECASE)

        # Italic: <i>, <em> -> _text_
        markdown = re.sub(r"<(i|em)>([^<]+)</(i|em)>", r"_\2_", markdown, flags=re.IGNORECASE)

        # Strikethrough: <s>, <strike> -> ~~text~~
        markdown = re.sub(r"<(s|strike)>([^<]+)</(s|strike)>", r"~~\2~~", markdown, flags=re.IGNORECASE)

        # Links: <a href="url">text</a> -> [text](url)
        markdown = re.sub(r'<a[^>]+href="([^"]+)"[^>]*>([^<]+)</a>', r"[\2](\1)", markdown, flags=re.IGNORECASE)

        # Code: <code>text</code> -> `text`
        markdown = re.sub(r"<code>([^<]+)</code>", r"`\1`", markdown, flags=re.IGNORECASE)

        # Pre: <pre>text</pre> -> ```text```
        markdown = re.sub(r"<pre>([^<]+)</pre>", r"```\n\1\n```", markdown, flags=re.IGNORECASE)

        # Strip remaining HTML tags (loop to handle nested/reconstructed tags).
        # Divergence from upstream — see docs/UPSTREAM_SYNC.md: upstream's
        # ``toAst`` still loops an unbounded ``<[^>]+>``; this uses the bounded
        # ``strip_html_tags`` primitive (chat@4.37.0) like the other Teams
        # HTML-to-text paths.
        markdown = strip_html_tags(markdown)

        # Decode HTML entities in a single pass
        entity_map = {
            "&lt;": "<",
            "&gt;": ">",
            "&amp;": "&",
            "&quot;": '"',
            "&#39;": "'",
        }
        markdown = re.sub(
            r"&(?:lt|gt|amp|quot|#39);",
            lambda m: entity_map.get(m.group(), m.group()),
            markdown,
        )

        return parse_markdown(markdown)

    def render_postable(self, message: object) -> str:
        """Render text messages, returning plain strings and raw text unchanged.

        Outgoing ``@name`` text is never rewritten to ``<at>name</at>``
        (upstream chat@4.40.0, vercel/chat#898): Teams only notifies a user
        when the activity also carries a mention entity, so the markup alone
        notified no one and mangled emails and URLs (``user@example.com``).
        Extends the base implementation with dataclass-style message support.
        """
        if isinstance(message, str):
            return message
        if isinstance(message, dict):
            if "raw" in message:
                return message["raw"]
            if "markdown" in message:
                return self.from_ast(parse_markdown(message["markdown"]))
            if "ast" in message:
                return self.from_ast(message["ast"])
            if "card" in message or message.get("type") == "card":
                return super().render_postable(message)
            return ""
        # Dataclass / object-style messages
        if hasattr(message, "raw"):
            return message.raw
        if hasattr(message, "markdown"):
            return self.from_ast(parse_markdown(message.markdown))
        if hasattr(message, "ast"):
            return self.from_ast(message.ast)
        # Fall back to base implementation for remaining cases (e.g. card objects)
        return super().render_postable(message)

    def _node_to_teams(self, node: Content) -> str:
        """Convert an AST node to Teams markdown."""
        node_type = node.get("type", "")

        if node_type == "paragraph":
            return "".join(self._node_to_teams(child) for child in get_node_children(node))

        if node_type == "text":
            return get_node_value(node)

        if node_type == "strong":
            content = "".join(self._node_to_teams(child) for child in get_node_children(node))
            return f"**{content}**"

        if node_type == "emphasis":
            content = "".join(self._node_to_teams(child) for child in get_node_children(node))
            return f"_{content}_"

        if node_type == "delete":
            content = "".join(self._node_to_teams(child) for child in get_node_children(node))
            return f"~~{content}~~"

        if node_type == "inlineCode":
            return f"`{get_node_value(node)}`"

        if node_type == "code":
            lang = node.get("lang", "") or ""
            return f"```{lang}\n{get_node_value(node)}\n```"

        if node_type == "link":
            link_text = "".join(self._node_to_teams(child) for child in get_node_children(node))
            return f"[{link_text}]({node.get('url', '')})"

        if node_type == "blockquote":
            return "\n".join(f"> {self._node_to_teams(child)}" for child in get_node_children(node))

        if node_type == "list":
            return self._render_list(node, 0, self._node_to_teams)

        if node_type == "break":
            return "\n"

        if node_type == "thematicBreak":
            return "---"

        if node_type == "table":
            return self._table_to_gfm(node)

        return self._default_node_to_text(node, self._node_to_teams)

    def _table_to_gfm(self, node: Content) -> str:
        """Render an mdast table node as a GFM markdown table.

        Teams renders markdown tables natively.
        """
        rows: list[list[str]] = []
        for row in node.get("children", []):
            cells: list[str] = []
            for cell in row.get("children", []):
                cell_content = "".join(self._node_to_teams(child) for child in get_node_children(cell))
                cells.append(cell_content)
            rows.append(cells)

        if not rows:
            return ""

        lines: list[str] = []
        # Header row
        lines.append(f"| {' | '.join(escape_table_cell(c) for c in rows[0])} |")
        # Separator
        separators = ["---"] * len(rows[0])
        lines.append(f"| {' | '.join(separators)} |")
        # Data rows
        for row in rows[1:]:
            lines.append(f"| {' | '.join(escape_table_cell(c) for c in row)} |")
        return "\n".join(lines)
