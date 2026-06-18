"""Messenger format conversion.

Messenger regular messages do not render markdown, so there is no custom
platform syntax to emit. ``from_ast`` simply stringifies the AST (preserving
markdown markers as plain literal text) and ``to_ast`` parses incoming text
with the shared parser. Mirrors upstream ``adapter-messenger/src/markdown.ts``.
"""

from __future__ import annotations

from chat_sdk.shared.base_format_converter import (
    BaseFormatConverter,
    PostableMessageInput,
    Root,
    parse_markdown,
    stringify_markdown,
)


class MessengerFormatConverter(BaseFormatConverter):
    """Format converter for the Messenger adapter."""

    def from_ast(self, ast: Root) -> str:
        """Stringify an AST to text (Messenger renders no markdown)."""
        return stringify_markdown(ast).strip()

    def to_ast(self, platform_text: str) -> Root:
        """Parse plain text / markdown into an AST using the shared parser."""
        return parse_markdown(platform_text)

    def render_postable(self, message: PostableMessageInput) -> str:
        """Render an ``AdapterPostableMessage`` to Messenger text.

        Handles ``str`` / ``raw`` / ``markdown`` / ``ast`` shapes directly and
        defers to the base implementation for anything else (e.g. ``card``).
        """
        if isinstance(message, str):
            return message
        if isinstance(message, dict):
            if "raw" in message:
                return message["raw"]
            if "markdown" in message:
                return self.from_markdown(message["markdown"])
            if "ast" in message:
                return self.from_ast(message["ast"])
        return super().render_postable(message)
