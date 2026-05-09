"""Telegram MarkdownV2 format conversion.

Renders markdown AST as Telegram MarkdownV2, which requires escaping
special characters outside of entities. This replaces the previous
approach of emitting standard markdown with legacy parse_mode "Markdown",
which was incompatible (standard markdown uses ``**bold**`` while Telegram
legacy uses ``*bold*``) and caused ``can't parse entities`` errors.

@see https://core.telegram.org/bots/api#markdownv2-style
"""

from __future__ import annotations

import copy
import re

from chat_sdk.shared.base_format_converter import (
    BaseFormatConverter,
    Content,
    Root,
    parse_markdown,
    table_to_ascii,
    walk_ast,
)

# MarkdownV2 requires escaping these characters in normal text:
# _ * [ ] ( ) ~ ` > # + - = | { } . ! \
_MARKDOWNV2_SPECIAL_CHARS = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")

# Inside ``` code blocks, only ` and \ need escaping.
_CODE_BLOCK_SPECIAL_CHARS = re.compile(r"([`\\])")

# Inside (...) of inline links, only ) and \ need escaping.
_LINK_URL_SPECIAL_CHARS = re.compile(r"([)\\])")


def escape_markdown_v2(text: str) -> str:
    """Escape text for use in normal MarkdownV2 context (outside entities)."""
    return _MARKDOWNV2_SPECIAL_CHARS.sub(r"\\\1", text)


def _escape_code_block(text: str) -> str:
    return _CODE_BLOCK_SPECIAL_CHARS.sub(r"\\\1", text)


def _escape_link_url(text: str) -> str:
    return _LINK_URL_SPECIAL_CHARS.sub(r"\\\1", text)


def _render_children(children: list[Content], join: str = "") -> str:
    return join.join(_render_markdown_v2(child) for child in children)


def _render_markdown_v2(node: Content) -> str:
    """Recursively render an mdast node as Telegram MarkdownV2 text."""
    if not isinstance(node, dict):
        return escape_markdown_v2(str(node)) if node else ""

    node_type = node.get("type", "")
    children = node.get("children", []) or []

    if node_type == "root":
        return _render_children(children, join="\n\n")

    if node_type == "paragraph":
        return _render_children(children)

    if node_type == "text":
        return escape_markdown_v2(node.get("value", ""))

    if node_type == "strong":
        return f"*{_render_children(children)}*"

    if node_type == "emphasis":
        return f"_{_render_children(children)}_"

    if node_type == "delete":
        return f"~{_render_children(children)}~"

    if node_type == "inlineCode":
        return f"`{_escape_code_block(node.get('value', ''))}`"

    if node_type == "code":
        lang = node.get("lang") or ""
        val = _escape_code_block(node.get("value", ""))
        return f"```{lang}\n{val}\n```"

    if node_type == "link":
        link_text = _render_children(children)
        url = _escape_link_url(node.get("url", ""))
        return f"[{link_text}]({url})"

    if node_type == "blockquote":
        inner = _render_children(children, join="\n")
        return "\n".join(f">{line}" for line in inner.split("\n"))

    if node_type == "list":
        ordered = bool(node.get("ordered"))
        rendered_items: list[str] = []
        for i, item in enumerate(children):
            if not isinstance(item, dict):
                continue
            item_children = item.get("children", []) or []
            content = _render_children(item_children, join="\n")
            if ordered:
                marker = escape_markdown_v2(f"{i + 1}.")
                rendered_items.append(f"{marker} {content}")
            else:
                rendered_items.append(f"\\- {content}")
        return "\n".join(rendered_items)

    if node_type == "listItem":
        return _render_children(children, join="\n")

    if node_type == "heading":
        # Telegram has no heading syntax; render as bold.
        return f"*{_render_children(children)}*"

    if node_type == "thematicBreak":
        return escape_markdown_v2("———")

    if node_type == "break":
        return "\n"

    if node_type == "image":
        alt = escape_markdown_v2(node.get("alt") or "")
        url = _escape_link_url(node.get("url", ""))
        return f"[{alt}]({url})"

    if node_type == "html":
        # Telegram MarkdownV2 parser rejects raw HTML; escape so it renders literally.
        return escape_markdown_v2(node.get("value", ""))

    if node_type in ("linkReference", "imageReference"):
        # Reference-style links/images lose their reference resolution here.
        if children:
            return _render_children(children)
        label = node.get("label") or node.get("identifier") or ""
        return escape_markdown_v2(label)

    if node_type in ("definition", "footnoteDefinition", "yaml"):
        return ""

    if node_type == "footnoteReference":
        label = node.get("label") or node.get("identifier") or ""
        return escape_markdown_v2(f"[^{label}]")

    if node_type in ("table", "tableRow", "tableCell"):
        # `from_ast` walks the AST and rewrites Table nodes to Code blocks
        # before this renderer runs. A table arriving here means that
        # preprocessing was skipped — render as ASCII to fail safely.
        if node_type == "table":
            return f"```\n{table_to_ascii(node)}\n```"
        return _render_children(children)

    # Fallback for unknown node types: render children if available, else
    # escape any value, else empty.
    #
    # Trade-off vs. upstream: TS uses ``node satisfies never`` to make
    # adding a new mdast node a *compile-time* failure here. In Python
    # we don't have that guarantee, so an unknown node is silently
    # converted to its escaped value or empty string rather than
    # raised. We accept this so that future mdast extensions don't
    # break message delivery (a stray unknown node should degrade to
    # plain text, not a crash) — at the cost of losing the upstream
    # signal that a renderer arm is missing. Test coverage of the
    # renderer should grow alongside any new node kinds we recognise.
    if children:
        return _render_children(children)
    value = node.get("value")
    if isinstance(value, str):
        return escape_markdown_v2(value)
    return ""


class TelegramFormatConverter(BaseFormatConverter):
    """Format converter for the Telegram adapter (MarkdownV2 output)."""

    def from_ast(self, ast: Root) -> str:
        """Convert an AST to Telegram MarkdownV2 text.

        Tables are pre-rewritten to ASCII code blocks (Telegram renders
        raw pipe syntax as garbled text and MarkdownV2 has no table
        syntax of its own).
        """

        def visitor(node: Content) -> Content:
            if isinstance(node, dict) and node.get("type") == "table":
                return {
                    "type": "code",
                    "value": table_to_ascii(node),
                    "lang": None,
                }
            return node

        transformed = walk_ast(copy.deepcopy(ast), visitor)
        return _render_markdown_v2(transformed).strip()

    def to_ast(self, platform_text: str) -> Root:
        """Parse plain text / markdown into an AST using the shared parser."""
        return parse_markdown(platform_text)
