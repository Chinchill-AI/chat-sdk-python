"""Base class for platform-specific format converters.

Python port of BaseFormatConverter from the Vercel Chat SDK ``markdown.ts``.

The AST (mdast Root dict) is the canonical representation.
All conversions go through the AST::

    Platform Format  <->  AST  <->  Markdown String

Adapters subclass :class:`BaseFormatConverter` and implement
:meth:`from_ast` and :meth:`to_ast` for their platform-specific format.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from chat_sdk.shared.markdown_parser import (
    Content,
    Root,
    ast_to_plain_text,
    get_node_children,
    get_node_value,
    parse_markdown,
    stringify_markdown,
    table_to_ascii,
    walk_ast,
)

# Re-export commonly used items so adapters can import from here
__all__ = [
    "BaseFormatConverter",
    "Content",
    "Root",
    "ast_to_plain_text",
    "get_node_children",
    "get_node_value",
    "parse_markdown",
    "stringify_markdown",
    "table_to_ascii",
    "walk_ast",
]

# Type alias matching the TS ``AdapterPostableMessage``
PostableMessageInput = Any


class BaseFormatConverter(ABC):
    """Abstract base class for format converters.

    Subclasses must implement:
      - ``from_ast(ast)`` -- render AST to platform format
      - ``to_ast(text)`` -- parse platform text into AST
    """

    # ------------------------------------------------------------------
    # Abstract methods
    # ------------------------------------------------------------------

    @abstractmethod
    def from_ast(self, ast: Root) -> str:
        """Render an AST to the platform's native format.

        This is the primary method used when *sending* messages.
        """

    @abstractmethod
    def to_ast(self, platform_text: str) -> Root:
        """Parse the platform's native format into an AST.

        This is the primary method used when *receiving* messages.
        """

    # ------------------------------------------------------------------
    # Template helpers (protected)
    # ------------------------------------------------------------------

    def _from_ast_with_node_converter(
        self,
        ast: Root,
        node_converter: Callable[[Content], str],
    ) -> str:
        """Template method: iterate AST children through *node_converter*.

        Joins results with double newlines (standard paragraph separation).
        """
        parts: list[str] = []
        for node in ast.get("children", []):
            parts.append(node_converter(node))
        return "\n\n".join(parts)

    def _render_list(
        self,
        node: Content,
        depth: int,
        node_converter: Callable[[Content], str],
        unordered_bullet: str = "-",
    ) -> str:
        """Render a list node with proper indentation.

        Handles ordered and unordered lists and recurses into nested lists.
        """
        indent = "  " * depth
        start = node.get("start", 1)
        ordered = node.get("ordered", False)
        lines: list[str] = []

        for i, item in enumerate(get_node_children(node)):
            prefix = f"{start + i}." if ordered else unordered_bullet
            is_first_content = True
            for child in get_node_children(item):
                if child.get("type") == "list":
                    lines.append(self._render_list(child, depth + 1, node_converter, unordered_bullet))
                    continue
                text = node_converter(child)
                if not text.strip():
                    continue
                if is_first_content:
                    lines.append(f"{indent}{prefix} {text}")
                    is_first_content = False
                else:
                    lines.append(f"{indent}  {text}")

        return "\n".join(lines)

    def _default_node_to_text(
        self,
        node: Content,
        node_converter: Callable[[Content], str],
    ) -> str:
        """Default fallback for converting an unknown AST node to text.

        Recursively converts children if present, otherwise extracts the
        node value. Adapters should call this in their ``_node_to_X()``
        default case.
        """
        children = get_node_children(node)
        if children:
            return "".join(node_converter(c) for c in children)
        return get_node_value(node)

    # ------------------------------------------------------------------
    # Convenience methods
    # ------------------------------------------------------------------

    def extract_plain_text(self, platform_text: str) -> str:
        """Extract plain text from platform format.

        Default implementation: ``to_ast`` then ``ast_to_plain_text``.
        """
        return ast_to_plain_text(self.to_ast(platform_text))

    def from_markdown(self, markdown: str) -> str:
        """Convert a standard markdown string to platform format."""
        return self.from_ast(parse_markdown(markdown))

    def to_markdown(self, platform_text: str) -> str:
        """Convert platform text to standard markdown."""
        return stringify_markdown(self.to_ast(platform_text))

    def render_postable(self, message: PostableMessageInput) -> str:
        """Render an ``AdapterPostableMessage`` to platform format.

        Handles the union of message shapes:
          - ``str`` -- passed through as raw text
          - ``{"raw": str}`` / ``.raw`` -- raw pass-through
          - ``{"markdown": str}`` / ``.markdown`` -- converted via AST
          - ``{"ast": Root}`` / ``.ast`` -- rendered from AST
          - ``{"card": ...}`` / ``.card`` -- fallback text
        """
        if isinstance(message, str):
            return message

        # Dict-based messages
        if isinstance(message, dict):
            if "raw" in message:
                return message["raw"]
            if "markdown" in message:
                return self.from_markdown(message["markdown"])
            if "ast" in message:
                return self.from_ast(message["ast"])
            if "card" in message:
                return message.get("fallback_text") or message.get("fallbackText") or ""
            if message.get("type") == "card":
                return ""
            return str(message)

        # Dataclass / object-style messages
        if hasattr(message, "raw"):
            return message.raw
        if hasattr(message, "markdown"):
            return self.from_markdown(message.markdown)
        if hasattr(message, "ast"):
            return self.from_ast(message.ast)
        if hasattr(message, "card"):
            fallback = getattr(message, "fallback_text", None)
            if fallback:
                return fallback
            return ""

        return str(message)
