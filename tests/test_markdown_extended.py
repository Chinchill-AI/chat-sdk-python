"""Extended markdown tests: type guards, BaseFormatConverter, cards fallback, round-trips.

Ported from remaining describe blocks in packages/chat/src/markdown.test.ts.
"""

from __future__ import annotations

from typing import Any

from chat_sdk.cards import (
    Actions,
    Button,
    Card,
    CardText,
    Divider,
    Field,
    Fields,
    Section,
    Table,
    card_to_fallback_text,
    is_card_element,
)
from chat_sdk.shared.base_format_converter import BaseFormatConverter
from chat_sdk.shared.markdown_parser import (
    Content,
    Root,
    ast_to_plain_text,
    make_blockquote,
    make_code,
    make_delete,
    make_emphasis,
    make_inline_code,
    make_link,
    make_paragraph,
    make_root,
    make_strong,
    make_text,
    parse_markdown,
    stringify_markdown,
    table_element_to_ascii,
    table_to_ascii,
    walk_ast,
)


# ============================================================================
# Concrete test converter (mirrors TS TestConverter)
# ============================================================================


class _TestConverter(BaseFormatConverter):
    """Simple converter: from_ast = toPlainText, to_ast = parseMarkdown."""

    def from_ast(self, ast: Root) -> str:
        return ast_to_plain_text(ast)

    def to_ast(self, platform_text: str) -> Root:
        return parse_markdown(platform_text)


_converter = _TestConverter()


# ============================================================================
# Type Guard Tests (dict-based equivalents of TS isXxxNode)
# ============================================================================


class TestTypeGuardTextNode:
    """isTextNode equivalent: node['type'] == 'text'."""

    def test_returns_true_for_text_nodes(self):
        node: Content = {"type": "text", "value": "hello"}
        assert node["type"] == "text"

    def test_returns_false_for_non_text_nodes(self):
        node: Content = {"type": "paragraph", "children": []}
        assert node["type"] != "text"


class TestTypeGuardParagraphNode:
    """isParagraphNode equivalent."""

    def test_returns_true_for_paragraph_nodes(self):
        node: Content = {"type": "paragraph", "children": []}
        assert node["type"] == "paragraph"

    def test_returns_false_for_non_paragraph_nodes(self):
        node: Content = {"type": "text", "value": "hello"}
        assert node["type"] != "paragraph"


class TestTypeGuardStrongNode:
    """isStrongNode equivalent."""

    def test_returns_true_for_strong_nodes(self):
        node: Content = {"type": "strong", "children": [{"type": "text", "value": "bold"}]}
        assert node["type"] == "strong"

    def test_returns_false_for_non_strong_nodes(self):
        node: Content = {"type": "emphasis", "children": [{"type": "text", "value": "italic"}]}
        assert node["type"] != "strong"


class TestTypeGuardEmphasisNode:
    """isEmphasisNode equivalent."""

    def test_returns_true_for_emphasis_nodes(self):
        node: Content = {"type": "emphasis", "children": [{"type": "text", "value": "italic"}]}
        assert node["type"] == "emphasis"

    def test_returns_false_for_non_emphasis_nodes(self):
        node: Content = {"type": "text", "value": "hello"}
        assert node["type"] != "emphasis"


class TestTypeGuardDeleteNode:
    """isDeleteNode equivalent."""

    def test_returns_true_for_delete_nodes(self):
        node: Content = {"type": "delete", "children": [{"type": "text", "value": "deleted"}]}
        assert node["type"] == "delete"

    def test_returns_false_for_non_delete_nodes(self):
        node: Content = {"type": "text", "value": "hello"}
        assert node["type"] != "delete"


class TestTypeGuardInlineCodeNode:
    """isInlineCodeNode equivalent."""

    def test_returns_true_for_inline_code_nodes(self):
        node: Content = {"type": "inlineCode", "value": "code"}
        assert node["type"] == "inlineCode"

    def test_returns_false_for_non_inline_code_nodes(self):
        node: Content = {"type": "code", "value": "block code"}
        assert node["type"] != "inlineCode"


class TestTypeGuardCodeNode:
    """isCodeNode equivalent."""

    def test_returns_true_for_code_block_nodes(self):
        node: Content = {"type": "code", "value": "const x = 1"}
        assert node["type"] == "code"

    def test_returns_false_for_inline_code_nodes(self):
        node: Content = {"type": "inlineCode", "value": "code"}
        assert node["type"] != "code"


class TestTypeGuardLinkNode:
    """isLinkNode equivalent."""

    def test_returns_true_for_link_nodes(self):
        node: Content = {
            "type": "link",
            "url": "https://example.com",
            "children": [{"type": "text", "value": "link"}],
        }
        assert node["type"] == "link"

    def test_returns_false_for_non_link_nodes(self):
        node: Content = {"type": "text", "value": "hello"}
        assert node["type"] != "link"


class TestTypeGuardBlockquoteNode:
    """isBlockquoteNode equivalent."""

    def test_returns_true_for_blockquote_nodes(self):
        node: Content = {
            "type": "blockquote",
            "children": [
                {"type": "paragraph", "children": [{"type": "text", "value": "quoted"}]},
            ],
        }
        assert node["type"] == "blockquote"

    def test_returns_false_for_non_blockquote_nodes(self):
        node: Content = {"type": "text", "value": "hello"}
        assert node["type"] != "blockquote"


class TestTypeGuardListNode:
    """isListNode equivalent."""

    def test_returns_true_for_list_nodes(self):
        ast = parse_markdown("- item 1\n- item 2")
        list_node = ast["children"][0]
        assert list_node["type"] == "list"

    def test_returns_false_for_non_list_nodes(self):
        node: Content = {"type": "text", "value": "hello"}
        assert node["type"] != "list"


class TestTypeGuardListItemNode:
    """isListItemNode equivalent."""

    def test_returns_true_for_list_item_nodes(self):
        ast = parse_markdown("- item 1")
        list_node = ast["children"][0]
        list_item = list_node["children"][0]
        assert list_item["type"] == "listItem"

    def test_returns_false_for_non_list_item_nodes(self):
        node: Content = {"type": "text", "value": "hello"}
        assert node["type"] != "listItem"


class TestTypeGuardTableNodes:
    """isTableNode, isTableRowNode, isTableCellNode equivalents."""

    def test_is_table_node_identifies_table(self):
        ast = parse_markdown("| A | B |\n|---|---|\n| 1 | 2 |")
        table = ast["children"][0]
        assert table["type"] == "table"
        assert {"type": "paragraph", "children": []}["type"] != "table"

    def test_is_table_row_node_identifies_row(self):
        ast = parse_markdown("| A | B |\n|---|---|\n| 1 | 2 |")
        table = ast["children"][0]
        row = table["children"][0]
        assert row["type"] == "tableRow"

    def test_is_table_cell_node_identifies_cell(self):
        ast = parse_markdown("| A | B |\n|---|---|\n| 1 | 2 |")
        table = ast["children"][0]
        row = table["children"][0]
        cell = row["children"][0]
        assert cell["type"] == "tableCell"


# ============================================================================
# BaseFormatConverter.extractPlainText
# ============================================================================


class TestExtractPlainText:
    """BaseFormatConverter.extract_plain_text tests."""

    def test_extracts_plain_text_from_platform_format(self):
        result = _converter.extract_plain_text("**bold** text")
        assert result == "bold text"


# ============================================================================
# BaseFormatConverter.fromMarkdown
# ============================================================================


class TestFromMarkdown:
    """BaseFormatConverter.from_markdown tests."""

    def test_converts_markdown_to_platform_format(self):
        result = _converter.from_markdown("**bold**")
        assert result == "bold"

    def test_handles_complex_multi_block_document(self):
        md = "# Title\n\n**bold** and *italic*\n\n```python\ncode\n```\n\n> quote"
        result = _converter.from_markdown(md)
        assert "Title" in result
        assert "bold" in result
        assert "italic" in result
        assert "code" in result
        assert "quote" in result

    def test_handles_multiple_paragraphs(self):
        md = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
        result = _converter.from_markdown(md)
        assert "First" in result
        assert "Second" in result
        assert "Third" in result


# ============================================================================
# BaseFormatConverter.toMarkdown
# ============================================================================


class TestToMarkdown:
    """BaseFormatConverter.to_markdown tests."""

    def test_converts_platform_format_to_markdown(self):
        result = _converter.to_markdown("plain text")
        assert result.strip() == "plain text"

    def test_round_trips_bold_text(self):
        md = "**bold**"
        ast = parse_markdown(md)
        result = stringify_markdown(ast)
        assert "**bold**" in result

    def test_round_trips_italic_text(self):
        md = "*italic*"
        ast = parse_markdown(md)
        result = stringify_markdown(ast)
        assert "*italic*" in result

    def test_round_trips_inline_code(self):
        md = "`code`"
        ast = parse_markdown(md)
        result = stringify_markdown(ast)
        assert "`code`" in result

    def test_round_trips_link(self):
        md = "[link](https://example.com)"
        ast = parse_markdown(md)
        result = stringify_markdown(ast)
        assert "[link](https://example.com)" in result

    def test_round_trips_code_block(self):
        md = "```python\nprint('hello')\n```"
        ast = parse_markdown(md)
        result = stringify_markdown(ast)
        assert "python" in result
        assert "print" in result

    def test_round_trips_blockquote(self):
        md = "> quoted text"
        ast = parse_markdown(md)
        result = stringify_markdown(ast)
        assert "> quoted text" in result

    def test_round_trips_strikethrough(self):
        md = "~~deleted~~"
        ast = parse_markdown(md)
        result = stringify_markdown(ast)
        assert "~~deleted~~" in result


# ============================================================================
# BaseFormatConverter.renderPostable
# ============================================================================


class TestRenderPostable:
    """BaseFormatConverter.render_postable tests."""

    def test_handles_string_input(self):
        result = _converter.render_postable("plain string")
        assert result == "plain string"

    def test_handles_raw_message(self):
        result = _converter.render_postable({"raw": "raw text"})
        assert result == "raw text"

    def test_handles_markdown_message(self):
        result = _converter.render_postable({"markdown": "**bold**"})
        assert result == "bold"

    def test_handles_ast_message(self):
        ast = make_root([make_paragraph([make_text("from ast")])])
        result = _converter.render_postable({"ast": ast})
        assert result == "from ast"

    def test_handles_card_with_fallback_text(self):
        card_elem = Card(title="Title", children=[CardText("Content")])
        result = _converter.render_postable({"card": card_elem})
        # card_to_fallback_text produces **Title**\nContent
        assert "Title" in result
        assert "Content" in result

    def test_generates_fallback_text_from_card(self):
        card_elem = Card(
            title="Order Status",
            subtitle="Your order details",
            children=[CardText("Processing your order...")],
        )
        result = _converter.render_postable({"card": card_elem})
        assert "Order Status" in result
        assert "Your order details" in result
        assert "Processing your order..." in result

    def test_handles_card_with_actions_excluded(self):
        card_elem = Card(
            title="Confirm",
            children=[
                Actions(
                    [
                        Button(id="yes", label="Yes"),
                        Button(id="no", label="No"),
                    ]
                ),
            ],
        )
        result = _converter.render_postable({"card": card_elem})
        assert "Confirm" in result
        # Actions are excluded from fallback
        assert "Yes" not in result
        assert "No" not in result

    def test_handles_card_with_fields(self):
        card_elem = Card(
            children=[
                Fields(
                    [
                        Field(label="Name", value="John"),
                        Field(label="Email", value="john@example.com"),
                    ]
                ),
            ],
        )
        result = _converter.render_postable({"card": card_elem})
        assert "Name" in result
        assert "John" in result
        assert "Email" in result
        assert "john@example.com" in result

    def test_handles_direct_card_element(self):
        card_elem = Card(title="Direct Card")
        result = _converter.render_postable(card_elem)
        assert "Direct Card" in result

    def test_handles_card_with_table_element(self):
        card_elem = Card(
            children=[
                Table(
                    headers=["Name", "Age"],
                    rows=[["Alice", "30"], ["Bob", "25"]],
                ),
            ],
        )
        result = _converter.render_postable({"card": card_elem})
        assert "Name" in result
        assert "Age" in result
        assert "Alice" in result
        assert "30" in result


# ============================================================================
# Deprecated toPlainText method
# ============================================================================


class TestDeprecatedToPlainText:
    """BaseFormatConverter.extract_plain_text (deprecated toPlainText alias)."""

    def test_extracts_plain_text_from_platform_format(self):
        result = _converter.extract_plain_text("**bold** text")
        assert result == "bold text"


# ============================================================================
# fromAstWithNodeConverter
# ============================================================================


class _NodeConverterTestConverter(BaseFormatConverter):
    """Converter that wraps paragraphs in [para:...] markers."""

    def from_ast(self, ast: Root) -> str:
        return self._from_ast_with_node_converter(ast, self._convert_node)

    def to_ast(self, platform_text: str) -> Root:
        return parse_markdown(platform_text)

    @staticmethod
    def _convert_node(node: Content) -> str:
        if node["type"] == "paragraph":
            return f"[para:{ast_to_plain_text({'type': 'root', 'children': [node]})}]"
        return ast_to_plain_text({"type": "root", "children": [node]})


_node_converter = _NodeConverterTestConverter()


class TestFromAstWithNodeConverter:
    """_from_ast_with_node_converter tests."""

    def test_joins_multiple_paragraphs_with_double_newlines(self):
        ast = make_root(
            [
                make_paragraph([make_text("First")]),
                make_paragraph([make_text("Second")]),
            ]
        )
        result = _node_converter.from_ast(ast)
        assert result == "[para:First]\n\n[para:Second]"

    def test_handles_single_paragraph(self):
        ast = make_root([make_paragraph([make_text("Only")])])
        result = _node_converter.from_ast(ast)
        assert result == "[para:Only]"

    def test_handles_empty_ast(self):
        ast = make_root([])
        result = _node_converter.from_ast(ast)
        assert result == ""

    def test_joins_three_paragraphs(self):
        ast = make_root(
            [
                make_paragraph([make_text("First")]),
                make_paragraph([make_text("Second")]),
                make_paragraph([make_text("Third")]),
            ]
        )
        result = _converter.from_ast(ast)
        assert "First" in result
        assert "Second" in result
        assert "Third" in result


# ============================================================================
# cardToFallbackText via renderPostable
# ============================================================================


class TestCardToFallbackTextViaRenderPostable:
    """card_to_fallback_text edge cases tested through render_postable."""

    def test_handles_card_with_section_children(self):
        card_elem = Card(
            children=[
                Section([CardText("Section content"), CardText("More content")]),
            ],
        )
        result = _converter.render_postable({"card": card_elem})
        assert "Section content" in result
        assert "More content" in result

    def test_handles_card_with_only_title(self):
        card_elem = Card(title="Title Only")
        result = _converter.render_postable({"card": card_elem})
        assert "Title Only" in result

    def test_handles_card_with_divider(self):
        card_elem = Card(title="With Divider", children=[Divider()])
        result = _converter.render_postable({"card": card_elem})
        # Divider produces "---" in fallback, title is present
        assert "With Divider" in result

    def test_handles_card_with_mixed_children_actions_excluded(self):
        card_elem = Card(
            title="Mixed",
            children=[
                CardText("Visible text"),
                Actions([Button(id="ok", label="OK")]),
                Fields([Field(label="Key", value="Val")]),
            ],
        )
        result = _converter.render_postable({"card": card_elem})
        assert "Visible text" in result
        # Actions are excluded
        assert "OK" not in result
        assert "Key" in result
        assert "Val" in result


# ============================================================================
# extractPlainText edge cases
# ============================================================================


class TestExtractPlainTextEdgeCases:
    """Edge cases for extract_plain_text / ast_to_plain_text."""

    def test_nested_formatting_extracts_correctly(self):
        result = ast_to_plain_text(parse_markdown("**_bold italic_**"))
        assert result == "bold italic"

    def test_code_blocks_extract_value(self):
        result = ast_to_plain_text(parse_markdown("```js\nconsole.log('hi')\n```"))
        assert "console.log" in result

    def test_links_extract_link_text(self):
        result = ast_to_plain_text(parse_markdown("[click here](https://example.com)"))
        assert result == "click here"

    def test_mixed_inline_formatting(self):
        result = ast_to_plain_text(parse_markdown("**bold** and *italic* and `code`"))
        assert result == "bold and italic and code"

    def test_deeply_nested_blockquote_strong_emphasis(self):
        result = ast_to_plain_text(parse_markdown("> **_deeply nested_**"))
        assert result == "deeply nested"

    def test_multiple_code_blocks(self):
        md = "```\nblock1\n```\n\n```\nblock2\n```"
        result = ast_to_plain_text(parse_markdown(md))
        assert "block1" in result
        assert "block2" in result


# ============================================================================
# Additional builder tests
# ============================================================================


class TestTextNodeBuilder:
    """Additional text node builder tests."""

    def test_creates_text_with_unicode(self):
        node = make_text("hello world")
        assert node["type"] == "text"
        assert node["value"] == "hello world"

    def test_creates_text_with_newlines(self):
        node = make_text("line1\nline2")
        assert node["value"] == "line1\nline2"


class TestStrongNodeBuilder:
    """Additional strong node builder tests."""

    def test_wraps_multiple_children(self):
        node = make_strong([make_text("a"), make_text("b")])
        assert node["type"] == "strong"
        assert len(node["children"]) == 2

    def test_wraps_inline_code(self):
        node = make_strong([make_inline_code("code")])
        assert node["children"][0]["type"] == "inlineCode"


class TestEmphasisNodeBuilder:
    """Additional emphasis node builder tests."""

    def test_wraps_strong_child(self):
        node = make_emphasis([make_strong([make_text("bold italic")])])
        assert node["type"] == "emphasis"
        assert node["children"][0]["type"] == "strong"

    def test_handles_empty_children(self):
        node = make_emphasis([])
        assert node["type"] == "emphasis"
        assert len(node["children"]) == 0


class TestDeleteNodeBuilder:
    """Additional delete (strikethrough) node builder tests."""

    def test_wraps_text_children(self):
        node = make_delete([make_text("strike1"), make_text("strike2")])
        assert node["type"] == "delete"
        assert len(node["children"]) == 2

    def test_handles_empty_children(self):
        node = make_delete([])
        assert len(node["children"]) == 0


class TestInlineCodeNodeBuilder:
    """Additional inlineCode node builder tests."""

    def test_preserves_special_characters(self):
        node = make_inline_code("a < b && c > d")
        assert node["value"] == "a < b && c > d"

    def test_empty_value(self):
        node = make_inline_code("")
        assert node["value"] == ""


class TestCodeBlockNodeBuilder:
    """Additional codeBlock node builder tests."""

    def test_multi_line_value(self):
        node = make_code("line1\nline2\nline3", "python")
        assert node["value"] == "line1\nline2\nline3"
        assert node["lang"] == "python"

    def test_no_lang_is_none(self):
        node = make_code("just code")
        assert node["lang"] is None


class TestLinkNodeBuilder:
    """Additional link node builder tests."""

    def test_empty_children(self):
        node = make_link("https://example.com", [])
        assert node["type"] == "link"
        assert len(node["children"]) == 0

    def test_multiple_children(self):
        node = make_link("https://example.com", [make_text("a"), make_strong([make_text("b")])])
        assert len(node["children"]) == 2


class TestBlockquoteNodeBuilder:
    """Additional blockquote node builder tests."""

    def test_nested_blockquote(self):
        inner = make_blockquote([make_paragraph([make_text("inner")])])
        outer = make_blockquote([inner])
        assert outer["type"] == "blockquote"
        assert outer["children"][0]["type"] == "blockquote"

    def test_empty_blockquote(self):
        node = make_blockquote([])
        assert len(node["children"]) == 0


class TestParagraphNodeBuilder:
    """Additional paragraph node builder tests."""

    def test_multiple_inline_children(self):
        node = make_paragraph(
            [
                make_text("start "),
                make_strong([make_text("bold")]),
                make_text(" end"),
            ]
        )
        assert node["type"] == "paragraph"
        assert len(node["children"]) == 3


class TestRootNodeBuilder:
    """Additional root node builder tests."""

    def test_root_with_mixed_block_children(self):
        node = make_root(
            [
                make_paragraph([make_text("para")]),
                make_code("code", "js"),
                make_blockquote([make_paragraph([make_text("quote")])]),
            ]
        )
        assert node["type"] == "root"
        assert len(node["children"]) == 3
        assert node["children"][0]["type"] == "paragraph"
        assert node["children"][1]["type"] == "code"
        assert node["children"][2]["type"] == "blockquote"


# ============================================================================
# is_card_element
# ============================================================================


class TestIsCardElement:
    """is_card_element utility tests."""

    def test_returns_true_for_card_dict(self):
        assert is_card_element({"type": "card", "children": []}) is True

    def test_returns_false_for_non_card_dict(self):
        assert is_card_element({"type": "text", "value": "hello"}) is False

    def test_returns_false_for_non_dict(self):
        assert is_card_element("not a card") is False

    def test_returns_false_for_none(self):
        assert is_card_element(None) is False
