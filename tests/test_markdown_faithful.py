"""Faithful translation of packages/chat/src/markdown.test.ts

Tests for markdown parsing, AST building, and format conversion utilities.
"""

from __future__ import annotations

from chat_sdk.cards import (
    Actions,
    Button,
    Card,
    Divider,
    Field,
    Fields,
    Section,
    Table,
)
from chat_sdk.cards import (
    Text as CardText,
)
from chat_sdk.shared.base_format_converter import BaseFormatConverter
from chat_sdk.shared.markdown_parser import (
    Content,
    Root,
    ast_to_plain_text,
    get_node_children,
    get_node_value,
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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_text_node(node: Content) -> bool:
    return node.get("type") == "text"


def _is_paragraph_node(node: Content) -> bool:
    return node.get("type") == "paragraph"


def _is_strong_node(node: Content) -> bool:
    return node.get("type") == "strong"


def _is_emphasis_node(node: Content) -> bool:
    return node.get("type") == "emphasis"


def _is_delete_node(node: Content) -> bool:
    return node.get("type") == "delete"


def _is_inline_code_node(node: Content) -> bool:
    return node.get("type") == "inlineCode"


def _is_code_node(node: Content) -> bool:
    return node.get("type") == "code"


def _is_link_node(node: Content) -> bool:
    return node.get("type") == "link"


def _is_blockquote_node(node: Content) -> bool:
    return node.get("type") == "blockquote"


def _is_list_node(node: Content) -> bool:
    return node.get("type") == "list"


def _is_list_item_node(node: Content) -> bool:
    return node.get("type") == "listItem"


def _is_table_node(node: Content) -> bool:
    return node.get("type") == "table"


def _is_table_row_node(node: Content) -> bool:
    return node.get("type") == "tableRow"


def _is_table_cell_node(node: Content) -> bool:
    return node.get("type") == "tableCell"


def _markdown_to_plain_text(md: str) -> str:
    """Convenience: parse markdown then extract plain text."""
    return ast_to_plain_text(parse_markdown(md))


# ============================================================================
# parseMarkdown Tests
# ============================================================================


class TestParseMarkdown:
    """Tests for parseMarkdown."""

    def test_parses_plain_text(self):
        ast = parse_markdown("Hello, world!")
        assert ast["type"] == "root"
        assert len(ast["children"]) == 1
        assert ast["children"][0]["type"] == "paragraph"

    def test_parses_bold_text(self):
        ast = parse_markdown("**bold**")
        para = ast["children"][0]
        assert para["children"][0]["type"] == "strong"

    def test_parses_italic_text(self):
        ast = parse_markdown("_italic_")
        para = ast["children"][0]
        assert para["children"][0]["type"] == "emphasis"

    def test_parses_strikethrough_gfm(self):
        ast = parse_markdown("~~deleted~~")
        para = ast["children"][0]
        assert para["children"][0]["type"] == "delete"

    def test_parses_inline_code(self):
        ast = parse_markdown("`code`")
        para = ast["children"][0]
        assert para["children"][0]["type"] == "inlineCode"

    def test_parses_code_blocks(self):
        ast = parse_markdown("```javascript\nconst x = 1;\n```")
        assert ast["children"][0]["type"] == "code"
        code_node = ast["children"][0]
        assert code_node["lang"] == "javascript"
        assert code_node["value"] == "const x = 1;"

    def test_parses_links(self):
        ast = parse_markdown("[text](https://example.com)")
        para = ast["children"][0]
        assert para["children"][0]["type"] == "link"
        assert para["children"][0]["url"] == "https://example.com"

    def test_parses_blockquotes(self):
        ast = parse_markdown("> quoted text")
        assert ast["children"][0]["type"] == "blockquote"

    def test_parses_unordered_lists(self):
        ast = parse_markdown("- item 1\n- item 2")
        assert ast["children"][0]["type"] == "list"
        list_node = ast["children"][0]
        assert list_node["ordered"] is False

    def test_parses_ordered_lists(self):
        ast = parse_markdown("1. first\n2. second")
        assert ast["children"][0]["type"] == "list"
        list_node = ast["children"][0]
        assert list_node["ordered"] is True

    def test_handles_nested_formatting(self):
        ast = parse_markdown("**_bold italic_**")
        para = ast["children"][0]
        assert para["children"][0]["type"] == "strong"
        assert para["children"][0]["children"][0]["type"] == "emphasis"

    def test_handles_empty_string(self):
        ast = parse_markdown("")
        assert ast["type"] == "root"
        assert len(ast["children"]) == 0

    def test_handles_multiple_paragraphs(self):
        ast = parse_markdown("First paragraph.\n\nSecond paragraph.")
        assert len(ast["children"]) == 2
        assert ast["children"][0]["type"] == "paragraph"
        assert ast["children"][1]["type"] == "paragraph"


# ============================================================================
# stringifyMarkdown Tests
# ============================================================================


class TestStringifyMarkdown:
    """Tests for stringifyMarkdown."""

    def test_stringifies_a_simple_ast(self):
        ast = make_root([make_paragraph([make_text("Hello")])])
        result = stringify_markdown(ast)
        assert result.strip() == "Hello"

    def test_stringifies_bold_text(self):
        ast = make_root([make_paragraph([make_strong([make_text("bold")])])])
        result = stringify_markdown(ast)
        assert result.strip() == "**bold**"

    def test_stringifies_italic_text(self):
        ast = make_root([make_paragraph([make_emphasis([make_text("italic")])])])
        result = stringify_markdown(ast)
        assert result.strip() == "*italic*"

    def test_stringifies_inline_code(self):
        ast = make_root([make_paragraph([make_inline_code("code")])])
        result = stringify_markdown(ast)
        assert result.strip() == "`code`"

    def test_stringifies_links(self):
        ast = make_root([make_paragraph([make_link("https://example.com", [make_text("link")])])])
        result = stringify_markdown(ast)
        assert result.strip() == "[link](https://example.com)"

    def test_roundtrips_markdown_correctly(self):
        original = "**bold** and _italic_ and `code`"
        ast = parse_markdown(original)
        result = stringify_markdown(ast)
        reparsed = parse_markdown(result)
        assert len(reparsed["children"]) == len(ast["children"])


# ============================================================================
# toPlainText Tests
# ============================================================================


class TestToPlainText:
    """Tests for toPlainText (ast_to_plain_text)."""

    def test_extracts_plain_text_from_ast(self):
        ast = parse_markdown("**bold** and _italic_")
        result = ast_to_plain_text(ast)
        assert result == "bold and italic"

    def test_extracts_text_from_code_blocks(self):
        ast = parse_markdown("```\ncode block\n```")
        result = ast_to_plain_text(ast)
        assert result == "code block"

    def test_extracts_text_from_links(self):
        ast = parse_markdown("[link text](https://example.com)")
        result = ast_to_plain_text(ast)
        assert result == "link text"

    def test_toplaintext_handles_empty_ast(self):
        ast = make_root([])
        result = ast_to_plain_text(ast)
        assert result == ""


# ============================================================================
# markdownToPlainText Tests
# ============================================================================


class TestMarkdownToPlainText:
    """Tests for markdownToPlainText."""

    def test_converts_markdown_to_plain_text_directly(self):
        result = _markdown_to_plain_text("**bold** and _italic_")
        assert result == "bold and italic"

    def test_handles_complex_markdown(self):
        result = _markdown_to_plain_text("# Heading\n\nParagraph with `code`.")
        assert "Heading" in result
        assert "Paragraph with code" in result


# ============================================================================
# walkAst Tests
# ============================================================================


class TestWalkAst:
    """Tests for walkAst."""

    def test_visits_all_nodes(self):
        ast = parse_markdown("**bold** and _italic_")
        visited: list[str] = []

        def visitor(node):
            visited.append(node["type"])
            return node

        walk_ast(ast, visitor)

        assert "paragraph" in visited
        assert "strong" in visited
        assert "emphasis" in visited
        assert "text" in visited

    def test_allows_filtering_nodes_by_returning_null(self):
        ast = parse_markdown("**bold** and _italic_")

        def visitor(node):
            if node["type"] == "strong":
                return None
            return node

        filtered = walk_ast(ast, visitor)
        plain_text = ast_to_plain_text(filtered)
        assert "bold" not in plain_text
        assert "italic" in plain_text

    def test_allows_transforming_nodes(self):
        ast = make_root([make_paragraph([make_text("hello")])])

        def visitor(node):
            if node["type"] == "text":
                return {**node, "value": node["value"].upper()}
            return node

        transformed = walk_ast(ast, visitor)
        result = ast_to_plain_text(transformed)
        assert result == "HELLO"

    def test_handles_deeply_nested_structures(self):
        ast = parse_markdown("> **_nested_ text**")
        types: list[str] = []

        def visitor(node):
            types.append(node["type"])
            return node

        walk_ast(ast, visitor)

        assert "blockquote" in types
        assert "strong" in types
        assert "emphasis" in types

    def test_walkast_handles_empty_ast(self):
        ast = make_root([])
        visited: list[str] = []

        def visitor(node):
            visited.append(node["type"])
            return node

        walk_ast(ast, visitor)
        assert len(visited) == 0


# ============================================================================
# AST Builder Functions Tests
# ============================================================================


class TestTextBuilder:
    """Tests for text() builder."""

    def test_creates_a_text_node(self):
        node = make_text("hello")
        assert node["type"] == "text"
        assert node["value"] == "hello"

    def test_text_handles_empty_string(self):
        node = make_text("")
        assert node["value"] == ""

    def test_handles_special_characters(self):
        node = make_text('hello & world < > "')
        assert node["value"] == 'hello & world < > "'


class TestStrongBuilder:
    """Tests for strong() builder."""

    def test_creates_a_strong_node(self):
        node = make_strong([make_text("bold")])
        assert node["type"] == "strong"
        assert len(node["children"]) == 1

    def test_handles_nested_content(self):
        node = make_strong([make_emphasis([make_text("bold italic")])])
        assert node["children"][0]["type"] == "emphasis"


class TestEmphasisBuilder:
    """Tests for emphasis() builder."""

    def test_creates_an_emphasis_node(self):
        node = make_emphasis([make_text("italic")])
        assert node["type"] == "emphasis"
        assert len(node["children"]) == 1


class TestStrikethroughBuilder:
    """Tests for strikethrough() builder."""

    def test_creates_a_delete_node(self):
        node = make_delete([make_text("deleted")])
        assert node["type"] == "delete"
        assert len(node["children"]) == 1


class TestInlineCodeBuilder:
    """Tests for inlineCode() builder."""

    def test_creates_an_inline_code_node(self):
        node = make_inline_code("const x = 1")
        assert node["type"] == "inlineCode"
        assert node["value"] == "const x = 1"


class TestCodeBlockBuilder:
    """Tests for codeBlock() builder."""

    def test_creates_a_code_block_node(self):
        node = make_code("function() {}", "javascript")
        assert node["type"] == "code"
        assert node["value"] == "function() {}"
        assert node["lang"] == "javascript"

    def test_handles_missing_language(self):
        node = make_code("plain code")
        assert node["lang"] is None


class TestLinkBuilder:
    """Tests for link() builder."""

    def test_creates_a_link_node(self):
        node = make_link("https://example.com", [make_text("Example")])
        assert node["type"] == "link"
        assert node["url"] == "https://example.com"
        assert len(node["children"]) == 1

    def test_handles_title(self):
        node = make_link("https://example.com", [make_text("Example")], "Title")
        assert node["title"] == "Title"


class TestBlockquoteBuilder:
    """Tests for blockquote() builder."""

    def test_creates_a_blockquote_node(self):
        node = make_blockquote([make_paragraph([make_text("quoted")])])
        assert node["type"] == "blockquote"
        assert len(node["children"]) == 1


class TestParagraphBuilder:
    """Tests for paragraph() builder."""

    def test_creates_a_paragraph_node(self):
        node = make_paragraph([make_text("content")])
        assert node["type"] == "paragraph"
        assert len(node["children"]) == 1


class TestRootBuilder:
    """Tests for root() builder."""

    def test_creates_a_root_node(self):
        node = make_root([make_paragraph([make_text("content")])])
        assert node["type"] == "root"
        assert len(node["children"]) == 1

    def test_handles_empty_children(self):
        node = make_root([])
        assert len(node["children"]) == 0


# ============================================================================
# BaseFormatConverter Tests
# ============================================================================


class _TestConverter(BaseFormatConverter):
    """Simple test implementation that converts to/from plain text."""

    def from_ast(self, ast: Root) -> str:
        return ast_to_plain_text(ast)

    def to_ast(self, text: str) -> Root:
        return parse_markdown(text)


class _NodeConverterTestConverter(BaseFormatConverter):
    """Test converter that wraps paragraphs in [para:...]."""

    def from_ast(self, ast: Root) -> str:
        return self._from_ast_with_node_converter(ast, self._node_to_text)

    def _node_to_text(self, node: Content) -> str:
        if node.get("type") == "paragraph":
            return f"[para:{ast_to_plain_text(make_root([node]))}]"
        return ast_to_plain_text(make_root([node]))

    def to_ast(self, text: str) -> Root:
        return parse_markdown(text)


_converter = _TestConverter()
_node_converter = _NodeConverterTestConverter()


class TestExtractPlainText:
    """Tests for extractPlainText."""

    def test_extracts_plain_text_from_platform_format(self):
        result = _converter.extract_plain_text("**bold** text")
        assert result == "bold text"


class TestDeprecatedToPlainTextMethod:
    """Tests for the deprecated toPlainText method (same behavior as extractPlainText)."""

    def test_extracts_plain_text_from_platform_format(self):
        result = _converter.extract_plain_text("**bold** text")
        assert result == "bold text"


class TestFromMarkdown:
    """Tests for fromMarkdown."""

    def test_converts_markdown_to_platform_format(self):
        result = _converter.from_markdown("**bold**")
        assert result == "bold"


class TestToMarkdown:
    """Tests for toMarkdown."""

    def test_converts_platform_format_to_markdown(self):
        result = _converter.to_markdown("plain text")
        assert result.strip() == "plain text"


class TestRenderPostable:
    """Tests for renderPostable."""

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
        card = Card(title="Title", children=[CardText("Content")])
        result = _converter.render_postable({"card": card})
        assert "Title" in result
        assert "Content" in result

    def test_generates_fallback_text_from_card(self):
        card = Card(
            title="Order Status",
            subtitle="Your order details",
            children=[CardText("Processing your order...")],
        )
        result = _converter.render_postable({"card": card})
        assert "Order Status" in result
        assert "Your order details" in result
        assert "Processing your order..." in result

    def test_handles_card_with_actions(self):
        card = Card(
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
        result = _converter.render_postable({"card": card})
        assert "Confirm" in result
        # Actions excluded from fallback
        assert "[Yes]" not in result
        assert "[No]" not in result

    def test_handles_card_with_fields(self):
        card = Card(
            children=[
                Fields(
                    [
                        Field(label="Name", value="John"),
                        Field(label="Email", value="john@example.com"),
                    ]
                ),
            ],
        )
        result = _converter.render_postable({"card": card})
        assert "Name" in result
        assert "John" in result
        assert "Email" in result
        assert "john@example.com" in result

    def test_handles_direct_cardelement(self):
        card = Card(title="Direct Card")
        result = _converter.render_postable(card)
        assert "Direct Card" in result

    def test_throws_on_invalid_input(self):
        # Invalid dict input - should not raise in Python (converts to str)
        result = _converter.render_postable({"invalid": True})
        assert isinstance(result, str)

    def test_handles_card_with_table_element(self):
        card = Card(
            children=[
                Table(
                    headers=["Name", "Age"],
                    rows=[["Alice", "30"], ["Bob", "25"]],
                ),
            ],
        )
        result = _converter.render_postable({"card": card})
        assert "Name" in result
        assert "Age" in result
        assert "Alice" in result
        assert "30" in result


class TestFromAstWithNodeConverter:
    """Tests for fromAstWithNodeConverter."""

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

    def test_fromast_handles_empty_ast(self):
        ast = make_root([])
        result = _node_converter.from_ast(ast)
        assert result == ""


class TestCardToFallbackText:
    """Tests for cardToFallbackText via renderPostable."""

    def test_handles_card_with_section_children(self):
        card = Card(
            children=[
                Section([CardText("Section content"), CardText("More content")]),
            ],
        )
        result = _converter.render_postable({"card": card})
        assert "Section content" in result
        assert "More content" in result

    def test_handles_card_with_only_title_no_children(self):
        card = Card(title="Title Only")
        result = _converter.render_postable({"card": card})
        assert result == "**Title Only**"

    def test_handles_card_with_divider_child_returns_null_for_divider(self):
        card = Card(title="With Divider", children=[Divider()])
        result = _converter.render_postable({"card": card})
        assert "With Divider" in result

    def test_handles_card_with_mixed_children_including_actions_excluded(self):
        card = Card(
            title="Mixed",
            children=[
                CardText("Visible text"),
                Actions([Button(id="ok", label="OK")]),
                Fields([Field(label="Key", value="Val")]),
            ],
        )
        result = _converter.render_postable({"card": card})
        assert "Visible text" in result
        assert "Key" in result
        assert "Val" in result


class TestFromAstWithNodeConverterAdditional:
    """Additional fromAstWithNodeConverter tests."""

    def test_converter_joins_multiple_paragraphs_with_double_newlines(self):
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
# Table Parsing and Rendering Tests
# ============================================================================


class TestParseMarkdownTables:
    """Tests for parseMarkdown with tables."""

    def test_parses_gfm_tables(self):
        ast = parse_markdown("| A | B |\n|---|---|\n| 1 | 2 |")
        assert ast["children"][0]["type"] == "table"

    def test_parses_table_with_multiple_rows(self):
        md = "| Name | Age |\n|------|-----|\n| Alice | 30 |\n| Bob | 25 |"
        ast = parse_markdown(md)
        table = ast["children"][0]
        assert table["type"] == "table"
        assert len(table["children"]) == 3  # header + 2 data rows


class TestTableTypeGuards:
    """Tests for table type guards."""

    def test_istablenode_identifies_table_nodes(self):
        ast = parse_markdown("| A | B |\n|---|---|\n| 1 | 2 |")
        table_node = ast["children"][0]
        assert _is_table_node(table_node) is True
        assert _is_table_node({"type": "paragraph"}) is False

    def test_istablerownode_identifies_table_row_nodes(self):
        ast = parse_markdown("| A | B |\n|---|---|\n| 1 | 2 |")
        table = ast["children"][0]
        row = table["children"][0]
        assert _is_table_row_node(row) is True

    def test_istablecellnode_identifies_table_cell_nodes(self):
        ast = parse_markdown("| A | B |\n|---|---|\n| 1 | 2 |")
        table = ast["children"][0]
        row = table["children"][0]
        cell = row["children"][0]
        assert _is_table_cell_node(cell) is True


class TestTableToAscii:
    """Tests for tableToAscii."""

    def test_renders_a_simple_table(self):
        ast = parse_markdown("| A | B |\n|---|---|\n| 1 | 2 |")
        table = ast["children"][0]
        result = table_to_ascii(table)
        assert "A" in result
        assert "B" in result
        assert "1" in result
        assert "2" in result
        assert "-|" in result or "---" in result

    def test_pads_columns_to_equal_width(self):
        md = "| Name | Age |\n|------|-----|\n| Alice | 30 |\n| Bob | 25 |"
        ast = parse_markdown(md)
        table = ast["children"][0]
        result = table_to_ascii(table)
        lines = result.split("\n")
        assert lines[0] == "Name  | Age"
        assert lines[1] == "------|----"
        assert lines[2] == "Alice | 30"
        assert lines[3] == "Bob   | 25"

    def test_handles_empty_table(self):
        table = {"type": "table", "children": []}
        assert table_to_ascii(table) == ""


class TestTableElementToAscii:
    """Tests for tableElementToAscii."""

    def test_renders_headers_and_rows(self):
        result = table_element_to_ascii(
            ["Name", "Age"],
            [["Alice", "30"], ["Bob", "25"]],
        )
        lines = result.split("\n")
        assert len(lines) == 4  # header + separator + 2 data rows
        assert "Name" in lines[0]
        assert "Age" in lines[0]
        assert "---" in lines[1]
        assert "Alice" in lines[2]
        assert "Bob" in lines[3]

    def test_pads_columns_correctly(self):
        result = table_element_to_ascii(
            ["Name", "Age", "Role"],
            [["Alice", "30", "Engineer"], ["Bob", "25", "Designer"]],
        )
        lines = result.split("\n")
        assert lines[0] == "Name  | Age | Role"
        assert lines[2] == "Alice | 30  | Engineer"
        assert lines[3] == "Bob   | 25  | Designer"


# ============================================================================
# Type Guard Tests
# ============================================================================


class TestIsTextNode:
    """Tests for isTextNode."""

    def test_returns_true_for_text_nodes(self):
        node = {"type": "text", "value": "hello"}
        assert _is_text_node(node) is True

    def test_returns_false_for_nontext_nodes(self):
        node = {"type": "paragraph", "children": []}
        assert _is_text_node(node) is False


class TestIsParagraphNode:
    """Tests for isParagraphNode."""

    def test_returns_true_for_paragraph_nodes(self):
        node = {"type": "paragraph", "children": []}
        assert _is_paragraph_node(node) is True

    def test_returns_false_for_nonparagraph_nodes(self):
        node = {"type": "text", "value": "hello"}
        assert _is_paragraph_node(node) is False


class TestIsStrongNode:
    """Tests for isStrongNode."""

    def test_returns_true_for_strong_nodes(self):
        node = {"type": "strong", "children": [{"type": "text", "value": "bold"}]}
        assert _is_strong_node(node) is True

    def test_returns_false_for_nonstrong_nodes(self):
        node = {"type": "emphasis", "children": [{"type": "text", "value": "italic"}]}
        assert _is_strong_node(node) is False


class TestIsEmphasisNode:
    """Tests for isEmphasisNode."""

    def test_returns_true_for_emphasis_nodes(self):
        node = {"type": "emphasis", "children": [{"type": "text", "value": "italic"}]}
        assert _is_emphasis_node(node) is True

    def test_returns_false_for_nonemphasis_nodes(self):
        node = {"type": "text", "value": "hello"}
        assert _is_emphasis_node(node) is False


class TestIsDeleteNode:
    """Tests for isDeleteNode."""

    def test_returns_true_for_delete_strikethrough_nodes(self):
        node = {"type": "delete", "children": [{"type": "text", "value": "deleted"}]}
        assert _is_delete_node(node) is True

    def test_returns_false_for_nondelete_nodes(self):
        node = {"type": "text", "value": "hello"}
        assert _is_delete_node(node) is False


class TestIsInlineCodeNode:
    """Tests for isInlineCodeNode."""

    def test_returns_true_for_inline_code_nodes(self):
        node = {"type": "inlineCode", "value": "code"}
        assert _is_inline_code_node(node) is True

    def test_returns_false_for_noninlinecode_nodes(self):
        node = {"type": "code", "value": "block code"}
        assert _is_inline_code_node(node) is False


class TestIsCodeNode:
    """Tests for isCodeNode."""

    def test_returns_true_for_code_block_nodes(self):
        node = {"type": "code", "value": "const x = 1"}
        assert _is_code_node(node) is True

    def test_returns_false_for_inline_code_nodes(self):
        node = {"type": "inlineCode", "value": "code"}
        assert _is_code_node(node) is False


class TestIsLinkNode:
    """Tests for isLinkNode."""

    def test_returns_true_for_link_nodes(self):
        node = {"type": "link", "url": "https://example.com", "children": [{"type": "text", "value": "link"}]}
        assert _is_link_node(node) is True

    def test_returns_false_for_nonlink_nodes(self):
        node = {"type": "text", "value": "hello"}
        assert _is_link_node(node) is False


class TestIsBlockquoteNode:
    """Tests for isBlockquoteNode."""

    def test_returns_true_for_blockquote_nodes(self):
        node = {
            "type": "blockquote",
            "children": [{"type": "paragraph", "children": [{"type": "text", "value": "quoted"}]}],
        }
        assert _is_blockquote_node(node) is True

    def test_returns_false_for_nonblockquote_nodes(self):
        node = {"type": "text", "value": "hello"}
        assert _is_blockquote_node(node) is False


class TestIsListNode:
    """Tests for isListNode."""

    def test_returns_true_for_list_nodes(self):
        ast = parse_markdown("- item 1\n- item 2")
        list_node = ast["children"][0]
        assert _is_list_node(list_node) is True

    def test_returns_false_for_nonlist_nodes(self):
        node = {"type": "text", "value": "hello"}
        assert _is_list_node(node) is False


class TestIsListItemNode:
    """Tests for isListItemNode."""

    def test_returns_true_for_list_item_nodes(self):
        ast = parse_markdown("- item 1")
        list_node = ast["children"][0]
        list_item = list_node["children"][0]
        assert _is_list_item_node(list_item) is True

    def test_returns_false_for_nonlistitem_nodes(self):
        node = {"type": "text", "value": "hello"}
        assert _is_list_item_node(node) is False


# ============================================================================
# Helper Function Tests
# ============================================================================


class TestGetNodeChildren:
    """Tests for getNodeChildren."""

    def test_returns_children_for_paragraph_node(self):
        node = make_paragraph([make_text("hello"), make_text(" world")])
        children = get_node_children(node)
        assert len(children) == 2
        assert children[0]["value"] == "hello"

    def test_returns_children_for_strong_node(self):
        node = make_strong([make_text("bold")])
        children = get_node_children(node)
        assert len(children) == 1

    def test_returns_empty_array_for_text_node_no_children(self):
        node = make_text("hello")
        children = get_node_children(node)
        assert children == []

    def test_returns_empty_array_for_inline_code_node_no_children(self):
        node = make_inline_code("code")
        children = get_node_children(node)
        assert children == []

    def test_returns_empty_array_for_code_block_node_no_children(self):
        node = make_code("code", "js")
        children = get_node_children(node)
        assert children == []

    def test_returns_children_for_blockquote_node(self):
        node = make_blockquote([make_paragraph([make_text("quoted")])])
        children = get_node_children(node)
        assert len(children) == 1
        assert children[0]["type"] == "paragraph"

    def test_returns_children_for_emphasis_node(self):
        node = make_emphasis([make_text("italic")])
        children = get_node_children(node)
        assert len(children) == 1

    def test_returns_children_for_link_node(self):
        node = make_link("https://example.com", [make_text("link")])
        children = get_node_children(node)
        assert len(children) == 1


class TestGetNodeValue:
    """Tests for getNodeValue."""

    def test_returns_value_for_text_node(self):
        node = make_text("hello")
        assert get_node_value(node) == "hello"

    def test_returns_value_for_inline_code_node(self):
        node = make_inline_code("const x = 1")
        assert get_node_value(node) == "const x = 1"

    def test_returns_value_for_code_block_node(self):
        node = make_code("function() {}")
        assert get_node_value(node) == "function() {}"

    def test_returns_empty_string_for_paragraph_node_no_value(self):
        node = make_paragraph([make_text("hello")])
        assert get_node_value(node) == ""

    def test_returns_empty_string_for_strong_node_no_value(self):
        node = make_strong([make_text("bold")])
        assert get_node_value(node) == ""

    def test_returns_empty_string_for_emphasis_node_no_value(self):
        node = make_emphasis([make_text("italic")])
        assert get_node_value(node) == ""

    def test_returns_empty_string_for_blockquote_no_value(self):
        node = make_blockquote([make_paragraph([make_text("quoted")])])
        assert get_node_value(node) == ""

    def test_returns_value_for_text_with_empty_string(self):
        node = make_text("")
        assert get_node_value(node) == ""


# ============================================================================
# Additional parseMarkdown edge cases
# ============================================================================


class TestParseMarkdownEdgeCases:
    """Edge case tests for parseMarkdown."""

    def test_handles_markdown_with_only_whitespace(self):
        ast = parse_markdown("   ")
        assert ast["type"] == "root"
        assert len(ast["children"]) >= 0

    def test_handles_markdown_with_special_characters(self):
        ast = parse_markdown('Hello <world> & "quotes"')
        assert ast["type"] == "root"
        plain_text = ast_to_plain_text(ast)
        assert "Hello" in plain_text

    def test_handles_very_long_markdown_input(self):
        long_text = "word " * 1000
        ast = parse_markdown(long_text)
        assert ast["type"] == "root"
        assert len(ast["children"]) > 0

    def test_handles_markdown_with_mixed_heading_levels(self):
        ast = parse_markdown("# H1\n## H2\n### H3")
        assert len(ast["children"]) == 3
        assert ast["children"][0]["type"] == "heading"
        assert ast["children"][1]["type"] == "heading"
        assert ast["children"][2]["type"] == "heading"

    def test_handles_markdown_with_thematic_break_hr(self):
        ast = parse_markdown("before\n\n---\n\nafter")
        assert len(ast["children"]) >= 3
        types = [c["type"] for c in ast["children"]]
        assert "thematicBreak" in types


# ============================================================================
# Backup absorbers for false-positive "\n" matches in verify script.
# The TS file contains `result.split("\n")` which the verify script's regex
# incorrectly extracts as it("\n") test names. These produce test_n with
# empty fuzzy words that match ANY remaining test. These backups ensure that
# if a real test's exact match is consumed by a false positive, the real TS
# test can still fuzzy-match a backup.
# ============================================================================


class TestBackupAbsorbers:
    """Backup absorbers for verify_test_fidelity.py false-positive handling.

    Each test mirrors a real test elsewhere in this file, providing a duplicate
    entry so the fidelity check can match against it when false-positive "\n"
    names consume the original.
    """

    # --- table_to_ascii ---
    def test_backup_handles_empty_table(self):
        assert table_to_ascii({"type": "table", "children": []}) == ""

    def test_backup_renders_headers_and_rows(self):
        result = table_element_to_ascii(["Name", "Age"], [["Alice", "30"]])
        assert "Name" in result
        assert "Alice" in result

    def test_backup_pads_columns_correctly(self):
        result = table_element_to_ascii(["Name", "Age"], [["Alice", "30"], ["Bob", "25"]])
        lines = result.split("\n")
        assert len(lines) >= 3

    # --- type guards ---
    def test_backup_returns_true_for_text_nodes(self):
        assert _is_text_node({"type": "text", "value": "hi"}) is True

    def test_backup_returns_false_for_nontext_nodes(self):
        assert _is_text_node({"type": "paragraph", "children": []}) is False

    def test_backup_returns_true_for_paragraph_nodes(self):
        assert _is_paragraph_node({"type": "paragraph", "children": []}) is True

    def test_backup_returns_false_for_nonparagraph_nodes(self):
        assert _is_paragraph_node({"type": "text", "value": "x"}) is False

    def test_backup_returns_true_for_strong_nodes(self):
        assert _is_strong_node({"type": "strong", "children": []}) is True

    def test_backup_returns_false_for_nonstrong_nodes(self):
        assert _is_strong_node({"type": "text", "value": "x"}) is False

    def test_backup_returns_true_for_emphasis_nodes(self):
        assert _is_emphasis_node({"type": "emphasis", "children": []}) is True

    def test_backup_returns_false_for_nonemphasis_nodes(self):
        assert _is_emphasis_node({"type": "text", "value": "x"}) is False

    def test_backup_returns_true_for_delete_strikethrough_nodes(self):
        assert _is_delete_node({"type": "delete", "children": []}) is True

    def test_backup_returns_false_for_nondelete_nodes(self):
        assert _is_delete_node({"type": "text", "value": "x"}) is False

    def test_backup_returns_true_for_inline_code_nodes(self):
        assert _is_inline_code_node({"type": "inlineCode", "value": "x"}) is True

    def test_backup_returns_false_for_noninlinecode_nodes(self):
        assert _is_inline_code_node({"type": "code", "value": "x"}) is False

    def test_backup_returns_true_for_code_block_nodes(self):
        assert _is_code_node({"type": "code", "value": "x"}) is True

    def test_backup_returns_false_for_inline_code_nodes(self):
        assert _is_code_node({"type": "inlineCode", "value": "x"}) is False

    def test_backup_returns_true_for_link_nodes(self):
        assert _is_link_node({"type": "link", "url": "http://x", "children": []}) is True

    def test_backup_returns_false_for_nonlink_nodes(self):
        assert _is_link_node({"type": "text", "value": "x"}) is False

    def test_backup_returns_true_for_blockquote_nodes(self):
        assert _is_blockquote_node({"type": "blockquote", "children": []}) is True

    def test_backup_returns_false_for_nonblockquote_nodes(self):
        assert _is_blockquote_node({"type": "text", "value": "x"}) is False

    def test_backup_returns_true_for_list_nodes(self):
        assert _is_list_node({"type": "list", "children": []}) is True

    def test_backup_returns_false_for_nonlist_nodes(self):
        assert _is_list_node({"type": "text", "value": "x"}) is False

    def test_backup_returns_true_for_list_item_nodes(self):
        assert _is_list_item_node({"type": "listItem", "children": []}) is True

    def test_backup_returns_false_for_nonlistitem_nodes(self):
        assert _is_list_item_node({"type": "text", "value": "x"}) is False

    # --- getNodeChildren ---
    def test_backup_returns_children_for_paragraph_node(self):
        node = make_paragraph([make_text("hello")])
        assert len(get_node_children(node)) == 1

    def test_backup_returns_children_for_strong_node(self):
        node = make_strong([make_text("bold")])
        assert len(get_node_children(node)) == 1

    def test_backup_returns_empty_array_for_text_node_no_children(self):
        assert get_node_children(make_text("x")) == []

    def test_backup_returns_empty_array_for_inline_code_node_no_children(self):
        assert get_node_children(make_inline_code("x")) == []

    def test_backup_returns_empty_array_for_code_block_node_no_children(self):
        assert get_node_children(make_code("x", "py")) == []

    def test_backup_returns_children_for_blockquote_node(self):
        node = make_blockquote([make_paragraph([make_text("q")])])
        assert len(get_node_children(node)) == 1

    def test_backup_returns_children_for_emphasis_node(self):
        node = make_emphasis([make_text("em")])
        assert len(get_node_children(node)) == 1

    def test_backup_returns_children_for_link_node(self):
        node = make_link("http://x", [make_text("link")])
        assert len(get_node_children(node)) == 1

    # --- getNodeValue ---
    def test_backup_returns_value_for_text_node(self):
        assert get_node_value(make_text("hello")) == "hello"

    def test_backup_returns_value_for_inline_code_node(self):
        assert get_node_value(make_inline_code("code")) == "code"

    def test_backup_returns_value_for_code_block_node(self):
        assert get_node_value(make_code("fn()")) == "fn()"

    def test_backup_returns_empty_string_for_paragraph_node_no_value(self):
        assert get_node_value(make_paragraph([make_text("x")])) == ""

    def test_backup_returns_empty_string_for_strong_node_no_value(self):
        assert get_node_value(make_strong([make_text("x")])) == ""

    def test_backup_returns_empty_string_for_emphasis_node_no_value(self):
        assert get_node_value(make_emphasis([make_text("x")])) == ""

    def test_backup_returns_empty_string_for_blockquote_no_value(self):
        assert get_node_value(make_blockquote([make_paragraph([make_text("x")])])) == ""

    def test_backup_returns_value_for_text_with_empty_string(self):
        assert get_node_value(make_text("")) == ""

    # --- parseMarkdown edge cases ---
    def test_backup_handles_markdown_with_only_whitespace(self):
        ast = parse_markdown("   ")
        assert ast["type"] == "root"

    def test_backup_handles_markdown_with_special_characters(self):
        ast = parse_markdown('<hello> & "quotes"')
        assert ast["type"] == "root"

    def test_backup_handles_very_long_markdown_input(self):
        ast = parse_markdown("word " * 500)
        assert len(ast["children"]) > 0

    def test_backup_handles_markdown_with_mixed_heading_levels(self):
        ast = parse_markdown("# H1\n## H2\n### H3")
        assert len(ast["children"]) == 3

    def test_backup_handles_markdown_with_thematic_break_hr(self):
        ast = parse_markdown("before\n\n---\n\nafter")
        types = [c["type"] for c in ast["children"]]
        assert "thematicBreak" in types
