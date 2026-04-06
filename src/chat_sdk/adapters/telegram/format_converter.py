"""Telegram format conversion.

Telegram supports Markdown/HTML parse modes, but to avoid
platform-specific escaping pitfalls this adapter emits normalized
markdown text as plain message text.

Tables are converted to code blocks since Telegram renders raw
pipe syntax as garbled text.
"""

from __future__ import annotations

import copy

from chat_sdk.shared.base_format_converter import (
    BaseFormatConverter,
    Content,
    Root,
    parse_markdown,
    stringify_markdown,
    table_to_ascii,
    walk_ast,
)


class TelegramFormatConverter(BaseFormatConverter):
    """Format converter for the Telegram adapter.

    Handles conversion between markdown text and AST representation.
    Tables are converted to code blocks since Telegram renders raw
    pipe syntax as garbled text.
    """

    def from_ast(self, ast: Root) -> str:
        """Convert an AST to plain markdown text.

        Replaces table nodes with code blocks, since Telegram renders
        raw pipe syntax as garbled text.
        """

        def visitor(node: Content) -> Content:
            if node.get("type") == "table":
                return {
                    "type": "code",
                    "value": table_to_ascii(node),
                    "lang": None,
                }
            return node

        transformed = walk_ast(copy.deepcopy(ast), visitor)
        return stringify_markdown(transformed).strip()

    def to_ast(self, text: str) -> Root:
        """Parse plain text / markdown into an AST using the shared parser."""
        return parse_markdown(text)
