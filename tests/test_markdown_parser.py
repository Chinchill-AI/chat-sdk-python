"""Tests for markdown parsing, AST building, stringify round-trips, and walk_ast.

Ported from packages/chat/src/markdown.test.ts.
"""

from __future__ import annotations

from chat_sdk.shared.markdown_parser import (
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

# ============================================================================
# parse_markdown tests
# ============================================================================


class TestParseMarkdown:
    """Tests for the parse_markdown function."""

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
        assert ast["children"][0]["ordered"] is False

    def test_parses_ordered_lists(self):
        ast = parse_markdown("1. first\n2. second")
        assert ast["children"][0]["type"] == "list"
        assert ast["children"][0]["ordered"] is True

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
# stringify_markdown tests
# ============================================================================


class TestStringifyMarkdown:
    """Tests for the stringify_markdown function."""

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

    def test_round_trips_markdown_correctly(self):
        original = "**bold** and _italic_ and `code`"
        ast = parse_markdown(original)
        result = stringify_markdown(ast)
        # Parse again to normalize
        reparsed = parse_markdown(result)
        assert len(reparsed["children"]) == len(ast["children"])


# ============================================================================
# ast_to_plain_text tests (toPlainText)
# ============================================================================


class TestAstToPlainText:
    """Tests for the ast_to_plain_text function."""

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

    def test_handles_empty_ast(self):
        ast = make_root([])
        result = ast_to_plain_text(ast)
        assert result == ""


# ============================================================================
# markdownToPlainText tests
# ============================================================================


class TestMarkdownToPlainText:
    """Tests that parsing then extracting plain text works end-to-end."""

    def test_converts_markdown_to_plain_text_directly(self):
        result = ast_to_plain_text(parse_markdown("**bold** and _italic_"))
        assert result == "bold and italic"

    def test_handles_complex_markdown(self):
        result = ast_to_plain_text(parse_markdown("# Heading\n\nParagraph with `code`."))
        assert "Heading" in result
        assert "Paragraph with code" in result


# ============================================================================
# walk_ast tests
# ============================================================================


class TestWalkAst:
    """Tests for the walk_ast function."""

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

    def test_allows_filtering_nodes_by_returning_none(self):
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
                return {**node, "value": node.get("value", "").upper()}
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

    def test_handles_empty_ast(self):
        ast = make_root([])
        visited: list[str] = []

        def visitor(node):
            visited.append(node["type"])
            return node

        walk_ast(ast, visitor)
        assert len(visited) == 0


# ============================================================================
# AST Builder Functions tests
# ============================================================================


class TestASTBuilderFunctions:
    """Tests for node constructor functions."""

    def test_text_creates_a_text_node(self):
        node = make_text("hello")
        assert node["type"] == "text"
        assert node["value"] == "hello"

    def test_text_handles_empty_string(self):
        node = make_text("")
        assert node["value"] == ""

    def test_text_handles_special_characters(self):
        node = make_text('hello & world < > "')
        assert node["value"] == 'hello & world < > "'

    def test_strong_creates_a_strong_node(self):
        node = make_strong([make_text("bold")])
        assert node["type"] == "strong"
        assert len(node["children"]) == 1

    def test_strong_handles_nested_content(self):
        node = make_strong([make_emphasis([make_text("bold italic")])])
        assert node["children"][0]["type"] == "emphasis"

    def test_emphasis_creates_an_emphasis_node(self):
        node = make_emphasis([make_text("italic")])
        assert node["type"] == "emphasis"
        assert len(node["children"]) == 1

    def test_strikethrough_creates_a_delete_node(self):
        node = make_delete([make_text("deleted")])
        assert node["type"] == "delete"
        assert len(node["children"]) == 1

    def test_inline_code_creates_an_inline_code_node(self):
        node = make_inline_code("const x = 1")
        assert node["type"] == "inlineCode"
        assert node["value"] == "const x = 1"

    def test_code_block_creates_a_code_node(self):
        node = make_code("function() {}", "javascript")
        assert node["type"] == "code"
        assert node["value"] == "function() {}"
        assert node["lang"] == "javascript"

    def test_code_block_handles_missing_language(self):
        node = make_code("plain code")
        assert node["lang"] is None

    def test_link_creates_a_link_node(self):
        node = make_link("https://example.com", [make_text("Example")])
        assert node["type"] == "link"
        assert node["url"] == "https://example.com"
        assert len(node["children"]) == 1

    def test_link_handles_title(self):
        node = make_link("https://example.com", [make_text("Example")], "Title")
        assert node["title"] == "Title"

    def test_blockquote_creates_a_blockquote_node(self):
        node = make_blockquote([make_paragraph([make_text("quoted")])])
        assert node["type"] == "blockquote"
        assert len(node["children"]) == 1

    def test_paragraph_creates_a_paragraph_node(self):
        node = make_paragraph([make_text("content")])
        assert node["type"] == "paragraph"
        assert len(node["children"]) == 1

    def test_root_creates_a_root_node(self):
        node = make_root([make_paragraph([make_text("content")])])
        assert node["type"] == "root"
        assert len(node["children"]) == 1

    def test_root_handles_empty_children(self):
        node = make_root([])
        assert len(node["children"]) == 0


# ============================================================================
# Table Parsing and Rendering Tests
# ============================================================================


class TestParseMarkdownTables:
    """Tests for GFM table parsing."""

    def test_parses_gfm_tables(self):
        ast = parse_markdown("| A | B |\n|---|---|\n| 1 | 2 |")
        assert ast["children"][0]["type"] == "table"

    def test_parses_table_with_multiple_rows(self):
        md = "| Name | Age |\n|------|-----|\n| Alice | 30 |\n| Bob | 25 |"
        ast = parse_markdown(md)
        table = ast["children"][0]
        assert table["type"] == "table"
        assert len(table["children"]) == 3  # header + 2 data rows


class TestTableToAscii:
    """Tests for table_to_ascii function."""

    def test_renders_a_simple_table(self):
        ast = parse_markdown("| A | B |\n|---|---|\n| 1 | 2 |")
        table = ast["children"][0]
        result = table_to_ascii(table)
        assert "A" in result
        assert "B" in result
        assert "1" in result
        assert "2" in result
        # Separator line with dashes
        assert "-|" in result

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
    """Tests for table_element_to_ascii function."""

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
# Helper Function Tests
# ============================================================================


class TestGetNodeChildren:
    """Tests for get_node_children function."""

    def test_returns_children_for_paragraph_node(self):
        node = make_paragraph([make_text("hello"), make_text(" world")])
        children = get_node_children(node)
        assert len(children) == 2
        assert children[0]["value"] == "hello"

    def test_returns_children_for_strong_node(self):
        node = make_strong([make_text("bold")])
        children = get_node_children(node)
        assert len(children) == 1

    def test_returns_empty_array_for_text_node(self):
        node = make_text("hello")
        children = get_node_children(node)
        assert children == []

    def test_returns_empty_array_for_inline_code_node(self):
        node = make_inline_code("code")
        children = get_node_children(node)
        assert children == []

    def test_returns_empty_array_for_code_block_node(self):
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
    """Tests for get_node_value function."""

    def test_returns_value_for_text_node(self):
        node = make_text("hello")
        assert get_node_value(node) == "hello"

    def test_returns_value_for_inline_code_node(self):
        node = make_inline_code("const x = 1")
        assert get_node_value(node) == "const x = 1"

    def test_returns_value_for_code_block_node(self):
        node = make_code("function() {}")
        assert get_node_value(node) == "function() {}"

    def test_returns_empty_string_for_paragraph_node(self):
        node = make_paragraph([make_text("hello")])
        assert get_node_value(node) == ""

    def test_returns_empty_string_for_strong_node(self):
        node = make_strong([make_text("bold")])
        assert get_node_value(node) == ""

    def test_returns_empty_string_for_emphasis_node(self):
        node = make_emphasis([make_text("italic")])
        assert get_node_value(node) == ""

    def test_returns_empty_string_for_blockquote(self):
        node = make_blockquote([make_paragraph([make_text("quoted")])])
        assert get_node_value(node) == ""

    def test_returns_value_for_text_with_empty_string(self):
        node = make_text("")
        assert get_node_value(node) == ""


# ============================================================================
# Additional parseMarkdown edge cases
# ============================================================================


class TestParseMarkdownEdgeCases:
    """Edge case tests for parse_markdown."""

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

    def test_handles_markdown_with_thematic_break(self):
        ast = parse_markdown("before\n\n---\n\nafter")
        assert len(ast["children"]) >= 3
        types = [c["type"] for c in ast["children"]]
        assert "thematicBreak" in types
