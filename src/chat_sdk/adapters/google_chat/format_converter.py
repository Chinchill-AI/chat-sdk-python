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

_GCHAT_LINK_RE = re.compile(r"<([a-zA-Z][a-zA-Z0-9+.\-]*:[^|\s>]+)\|([^>]+)>")
# Private-use code point chosen as a placeholder token. PUA characters are
# guaranteed never to appear in user-authored text, so the round-trip
# substitution (emit placeholder → parse Markdown → inject link nodes) can
# use them as unique markers without collision risk.
_GCHAT_LINK_PLACEHOLDER = "\ue000LINK{idx}\ue000"
_GCHAT_LINK_PLACEHOLDER_RE = re.compile(r"\ue000LINK(\d+)\ue000")


def _inject_link_placeholders(node: dict, links: list[tuple[str, str]]) -> None:
    """Walk the AST in place and expand ``\\ue000LINK{idx}\\ue000`` placeholders
    in text nodes into real link nodes. Used by ``to_ast`` to bypass the
    Markdown parser for custom ``<url|text>`` tokens whose URLs may contain
    characters the parser can't round-trip (e.g. unescaped ``)``).
    """
    children = node.get("children")
    if not isinstance(children, list):
        return
    new_children: list = []
    for child in children:
        if isinstance(child, dict) and child.get("type") == "text":
            value = child.get("value", "")
            if "\ue000" not in value:
                new_children.append(child)
                continue
            parts = _GCHAT_LINK_PLACEHOLDER_RE.split(value)
            # parts alternates plain-text / placeholder-index / plain-text / ...
            for i, part in enumerate(parts):
                if i % 2 == 0:
                    if part:
                        new_children.append({"type": "text", "value": part})
                else:
                    url, text = links[int(part)]
                    new_children.append(
                        {
                            "type": "link",
                            "url": url,
                            "children": [{"type": "text", "value": text}],
                        }
                    )
        else:
            if isinstance(child, dict):
                _inject_link_placeholders(child, links)
            new_children.append(child)
    node["children"] = new_children


class GoogleChatFormatConverter(BaseFormatConverter):
    """Format converter between standard markdown AST and Google Chat format.

    Uses the shared AST infrastructure with platform-specific node rendering.
    """

    def from_ast(self, ast: Root) -> str:
        """Render an AST to Google Chat format."""
        return self._from_ast_with_node_converter(ast, self._node_to_gchat)

    def to_ast(self, platform_text: str) -> Root:
        """Parse Google Chat message into an AST.

        Converts Google Chat format to standard markdown, then parses
        with the shared parser.
        """
        markdown = platform_text

        # Divergence from upstream — see docs/UPSTREAM_SYNC.md.
        # Google Chat custom link syntax `<url|text>` has to survive our
        # Markdown parser, which doesn't implement CommonMark's balanced-
        # parens rule for link destinations. A naive regex substitution to
        # `[text](url)` would corrupt URLs containing `)` (e.g. Wikipedia-
        # style `https://en.wikipedia.org/wiki/Foo_(bar)`). Instead we
        # extract each match to a PUA placeholder, parse the rest as
        # Markdown, and inject real link nodes where the placeholders
        # land. Accepts any RFC 3986 scheme.
        links: list[tuple[str, str]] = []

        def _capture(match: re.Match[str]) -> str:
            idx = len(links)
            links.append((match.group(1), match.group(2)))
            return _GCHAT_LINK_PLACEHOLDER.format(idx=idx)

        markdown = _GCHAT_LINK_RE.sub(_capture, markdown)

        # Bold: *text* -> **text**
        markdown = re.sub(r"(?<![_*\\])\*([^*\n]+)\*(?![_*])", r"**\1**", markdown)

        # Strikethrough: ~text~ -> ~~text~~
        markdown = re.sub(r"(?<!~)~([^~\n]+)~(?!~)", r"~~\1~~", markdown)

        # Italic and code are the same format as markdown
        ast = parse_markdown(markdown)
        if links:
            _inject_link_placeholders(ast, links)
        return ast

    def extract_plain_text(self, platform_text: str) -> str:
        """Extract plain text from Google Chat formatted text.

        Strips formatting markers while preserving the text content.
        """
        # Remove code blocks
        result = re.sub(r"```[\s\S]*?```", lambda m: m.group(0).strip("`").strip(), platform_text)
        # Inline code
        result = re.sub(r"`([^`]+)`", r"\1", result)
        # Divergence from upstream — see docs/UPSTREAM_SYNC.md.
        # Google Chat custom link syntax: <url|text> -> text (any RFC 3986 scheme)
        result = re.sub(r"<[a-zA-Z][a-zA-Z0-9+.\-]*:[^|\s>]+\|([^>]+)>", r"\1", result)
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
            # Divergence from upstream — see docs/UPSTREAM_SYNC.md.
            # Labels containing `|`, `>`, `]`, or a newline can't be emitted
            # safely in <url|text> form: Google Chat (and our own round-trip
            # regex) stops at the first `>` / `|`, and `]` prematurely closes
            # the label when to_ast() converts `<url|text>` to Markdown
            # `[text](url)` form. Fall back to plain `text (url)` so the label
            # text is preserved and Google Chat's auto-link detection still
            # makes the URL clickable.
            if any(c in link_text for c in ("|", ">", "]", "\n")):
                return f"{link_text} ({url})"
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
