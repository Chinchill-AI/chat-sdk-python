"""WhatsApp-specific format conversion using AST-based parsing.

WhatsApp uses a markdown-like format with some differences from standard:
- Bold: *text* (single asterisk, not double)
- Italic: _text_
- Strikethrough: ~text~ (single tilde, not double)
- Monospace: ```text```

See: https://faq.whatsapp.com/539178204879377
"""

from __future__ import annotations

import copy
import re

from chat_sdk.shared.base_format_converter import (
    BaseFormatConverter,
    Content,
    Root,
    parse_markdown,
    stringify_markdown,
    table_to_ascii,
    walk_ast,
)


class WhatsAppFormatConverter(BaseFormatConverter):
    """WhatsApp-specific format converter.

    Transforms between standard markdown AST and WhatsApp's custom
    markdown format.
    """

    def from_ast(self, ast: Root) -> str:
        """Convert an AST to WhatsApp markdown format.

        Transforms unsupported nodes (headings, thematic breaks, tables)
        into WhatsApp-compatible equivalents, then converts standard markdown
        bold/strikethrough to WhatsApp syntax.
        """

        def visitor(node: Content) -> Content:
            # Headings -> bold paragraph (flatten nested strong to avoid ***)
            if node.get("type") == "heading":
                children = node.get("children", [])
                flattened: list[Content] = []
                for child in children:
                    if child.get("type") == "strong":
                        flattened.extend(child.get("children", []))
                    else:
                        flattened.append(child)
                return {
                    "type": "paragraph",
                    "children": [{"type": "strong", "children": flattened}],
                }

            # Thematic breaks -> text separator
            if node.get("type") == "thematicBreak":
                return {
                    "type": "paragraph",
                    "children": [{"type": "text", "value": "\u2501\u2501\u2501"}],
                }

            # Tables -> code blocks
            if node.get("type") == "table":
                return {
                    "type": "code",
                    "value": table_to_ascii(node),
                    "lang": None,
                }

            return node

        transformed = walk_ast(copy.deepcopy(ast), visitor)

        # Use _ for emphasis and - for bullets so the only * in output is **strong**
        markdown = stringify_markdown(transformed, emphasis="_", bullet="-").strip()

        return self._to_whatsapp_format(markdown)

    def to_ast(self, platform_text: str) -> Root:
        """Parse WhatsApp markdown into an AST.

        Transforms WhatsApp-specific formatting to standard markdown first,
        then parses with the shared parser.
        """
        standard_markdown = self._from_whatsapp_format(platform_text)
        return parse_markdown(standard_markdown)

    def _to_whatsapp_format(self, text: str) -> str:
        """Convert remaining standard markdown markers to WhatsApp format.

        The stringifier already outputs _italic_ and - bullets.
        This only converts **bold** -> *bold* and ~~strike~~ -> ~strike~.
        """
        result = text
        # Convert **bold** -> *bold*
        result = re.sub(r"\*\*(.+?)\*\*", r"*\1*", result)
        # Convert ~~strikethrough~~ -> ~strikethrough~
        result = re.sub(r"~~(.+?)~~", r"~\1~", result)
        return result

    def _from_whatsapp_format(self, text: str) -> str:
        """Convert WhatsApp format to standard markdown.

        Converts single-asterisk bold to double-asterisk bold,
        and single-tilde strikethrough to double-tilde strikethrough.

        Careful not to convert _italic_ (which is the same in both formats).
        """
        # Convert *bold* to **bold** (single * not preceded/followed by *, no newlines)
        result = re.sub(r"(?<!\*)\*(?!\*)([^\n*]+?)(?<!\*)\*(?!\*)", r"**\1**", text)
        # Convert ~strike~ to ~~strike~~ (single ~ not preceded/followed by ~, no newlines)
        result = re.sub(r"(?<!~)~(?!~)([^\n~]+?)(?<!~)~(?!~)", r"~~\1~~", result)
        return result
