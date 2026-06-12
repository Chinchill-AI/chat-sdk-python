"""Twilio format conversion and SMS text helpers.

SMS has no rich-text rendering, so markdown is passed through as plain
literal text; only tables are rewritten (to ASCII blocks) because pipe
tables are unreadable on a phone. Mirrors upstream
``adapter-twilio/src/markdown.ts`` plus the ``src/format/index.ts``
length helpers.
"""

from __future__ import annotations

from dataclasses import dataclass

from chat_sdk.shared.base_format_converter import (
    BaseFormatConverter,
    Content,
    PostableMessageInput,
    Root,
    parse_markdown,
    stringify_markdown,
    table_to_ascii,
    walk_ast,
)

# Maximum body length accepted by the Messages API in a single message.
TWILIO_MESSAGE_LIMIT = 1600


@dataclass
class TwilioTextResult:
    """Result of :func:`truncate_twilio_text`."""

    text: str
    truncated: bool


def truncate_twilio_text(text: str, *, limit: int | None = None) -> TwilioTextResult:
    """Truncate ``text`` to ``limit`` characters (default 1600).

    Mirrors upstream ``truncateTwilioText``: a non-integer or sub-1 limit
    raises ``TypeError``. ``bool`` is rejected explicitly because Python
    bools are ints (``Number.isInteger(true)`` is false upstream).
    """
    resolved_limit = limit if limit is not None else TWILIO_MESSAGE_LIMIT
    if isinstance(resolved_limit, bool) or not isinstance(resolved_limit, int) or resolved_limit < 1:
        raise TypeError("limit must be a positive integer")
    if len(text) <= resolved_limit:
        return TwilioTextResult(text=text, truncated=False)
    return TwilioTextResult(text=text[:resolved_limit], truncated=True)


def twilio_text_or_placeholder(text: str) -> str:
    """Return ``text``, or a single space when empty.

    The Messages API rejects empty ``Body`` values; mirrors upstream
    ``twilioTextOrPlaceholder``.
    """
    return text if len(text) > 0 else " "


class TwilioFormatConverter(BaseFormatConverter):
    """Format converter for the Twilio adapter."""

    def to_ast(self, platform_text: str) -> Root:
        """Parse inbound SMS text into an AST using the shared parser."""
        return parse_markdown(platform_text)

    def from_ast(self, ast: Root) -> str:
        """Stringify an AST to SMS text, rewriting tables to ASCII blocks."""

        def visitor(node: Content) -> Content:
            if node.get("type") == "table":
                return {
                    "type": "code",
                    "value": table_to_ascii(node),
                    "lang": None,
                }
            return node

        # ``walk_ast`` deep-copies, covering upstream's ``structuredClone``.
        transformed = walk_ast(ast, visitor)
        return stringify_markdown(transformed).strip()

    def render_postable(self, message: PostableMessageInput) -> str:
        """Render an ``AdapterPostableMessage`` to Twilio SMS text.

        Handles ``str`` / ``raw`` / ``markdown`` / ``ast`` shapes directly
        and defers to the base implementation for anything else.
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
