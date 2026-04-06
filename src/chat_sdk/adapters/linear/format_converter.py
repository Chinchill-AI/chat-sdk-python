"""Linear-specific format conversion using AST-based parsing.

Linear uses standard Markdown for comments, which is very close
to the mdast format used by the SDK. This converter is mostly
pass-through, similar to the GitHub adapter.

See: https://linear.app/docs/comment-on-issues
"""

from __future__ import annotations

from chat_sdk.shared.base_format_converter import (
    BaseFormatConverter,
    Root,
    parse_markdown,
    stringify_markdown,
)


class LinearFormatConverter(BaseFormatConverter):
    """Linear-specific format converter.

    Linear uses standard markdown, so conversions are mostly pass-through.
    """

    def from_ast(self, ast: Root) -> str:
        """Convert an AST to standard markdown.

        Linear uses standard markdown, so we use stringify directly.
        """
        return stringify_markdown(ast).strip()

    def to_ast(self, platform_text: str) -> Root:
        """Parse markdown into an AST.

        Linear uses standard markdown, so we use the standard parser.
        """
        return parse_markdown(platform_text)

    def render_postable(self, message: object) -> str:
        """Render a postable message to Linear markdown string."""
        if isinstance(message, str):
            return message
        if isinstance(message, dict):
            if "raw" in message:
                return message["raw"]
            if "markdown" in message:
                return self.from_ast(parse_markdown(message["markdown"]))
            if "ast" in message:
                return self.from_ast(message["ast"])
        return ""
