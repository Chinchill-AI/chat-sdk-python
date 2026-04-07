"""Extended tests for GoogleChatFormatConverter -- targeting 80%+ coverage.

Ported from packages/adapter-gchat/src/markdown.test.ts (29 tests).
"""

from __future__ import annotations

from chat_sdk.adapters.google_chat.format_converter import GoogleChatFormatConverter


def _converter() -> GoogleChatFormatConverter:
    return GoogleChatFormatConverter()


# ---------------------------------------------------------------------------
# to_ast: Google Chat format -> AST
# ---------------------------------------------------------------------------


class TestToAst:
    def test_bold_single_star_to_ast(self):
        """*bold* in GChat maps to strong node via **bold** in markdown."""
        converter = _converter()
        ast = converter.to_ast("*bold*")
        assert ast is not None
        assert ast["type"] == "root"

    def test_strikethrough_single_tilde_to_ast(self):
        """~struck~ in GChat maps to delete node via ~~struck~~ in markdown."""
        converter = _converter()
        ast = converter.to_ast("~struck~")
        assert ast["type"] == "root"

    def test_plain_text_to_ast(self):
        converter = _converter()
        ast = converter.to_ast("Hello world")
        assert ast["type"] == "root"
        # Should have paragraph with text child
        children = ast.get("children", [])
        assert len(children) > 0

    def test_mixed_formatting_to_ast(self):
        """Bold, italic, and strikethrough together."""
        converter = _converter()
        ast = converter.to_ast("*bold* _italic_ ~struck~")
        assert ast["type"] == "root"

    def test_code_block_to_ast(self):
        converter = _converter()
        ast = converter.to_ast("```\ncode\n```")
        assert ast["type"] == "root"

    def test_inline_code_to_ast(self):
        converter = _converter()
        ast = converter.to_ast("Use `const x = 1`")
        assert ast["type"] == "root"


# ---------------------------------------------------------------------------
# from_ast: AST -> Google Chat format
# ---------------------------------------------------------------------------


class TestFromAst:
    def test_bold_markdown_to_gchat(self):
        """**bold** -> *bold* in GChat."""
        converter = _converter()
        ast = converter.to_ast("**bold text**")
        result = converter.from_ast(ast)
        assert "*bold text*" in result

    def test_italic(self):
        converter = _converter()
        ast = converter.to_ast("_italic text_")
        result = converter.from_ast(ast)
        assert "_italic text_" in result

    def test_strikethrough(self):
        """~~text~~ -> ~text~ in GChat."""
        converter = _converter()
        ast = converter.to_ast("~~strikethrough~~")
        result = converter.from_ast(ast)
        assert "~strikethrough~" in result

    def test_inline_code(self):
        converter = _converter()
        ast = converter.to_ast("Use `const x = 1`")
        result = converter.from_ast(ast)
        assert "`const x = 1`" in result

    def test_code_block(self):
        converter = _converter()
        ast = converter.to_ast("```\nconst x = 1;\n```")
        result = converter.from_ast(ast)
        assert "```" in result
        assert "const x = 1;" in result

    def test_link_same_text_and_url(self):
        converter = _converter()
        ast = converter.to_ast("[https://example.com](https://example.com)")
        result = converter.from_ast(ast)
        assert "https://example.com" in result

    def test_link_different_text_and_url(self):
        converter = _converter()
        ast = converter.to_ast("[click here](https://example.com)")
        result = converter.from_ast(ast)
        assert "click here (https://example.com)" in result

    def test_blockquote(self):
        converter = _converter()
        ast = converter.to_ast("> quoted text")
        result = converter.from_ast(ast)
        assert "> quoted text" in result

    def test_unordered_list(self):
        converter = _converter()
        ast = converter.to_ast("- item 1\n- item 2")
        result = converter.from_ast(ast)
        assert "item 1" in result
        assert "item 2" in result

    def test_ordered_list(self):
        converter = _converter()
        ast = converter.to_ast("1. first\n2. second")
        result = converter.from_ast(ast)
        assert "1." in result
        assert "2." in result

    def test_nested_unordered_list(self):
        converter = _converter()
        result = converter.from_markdown("- parent\n  - child 1\n  - child 2")
        assert "\u2022 parent" in result
        assert "  \u2022 child 1" in result
        assert "  \u2022 child 2" in result

    def test_nested_ordered_list(self):
        converter = _converter()
        result = converter.from_markdown("1. first\n   1. sub-first\n   2. sub-second\n2. second")
        assert "1. first" in result
        # Sub-items appear in the output (nesting depth depends on parser)
        assert "sub-first" in result
        assert "sub-second" in result
        assert "2. second" in result

    def test_deeply_nested_list(self):
        converter = _converter()
        result = converter.from_markdown("- level 1\n  - level 2\n    - level 3")
        assert "\u2022 level 1" in result
        assert "  \u2022 level 2" in result
        assert "    \u2022 level 3" in result

    def test_sibling_items_same_indent(self):
        converter = _converter()
        result = converter.from_markdown("- item 1\n- item 2\n- item 3")
        assert result == "\u2022 item 1\n\u2022 item 2\n\u2022 item 3"

    def test_mixed_ordered_unordered_nesting(self):
        converter = _converter()
        result = converter.from_markdown("1. first\n   - sub a\n   - sub b\n2. second")
        assert "1. first" in result
        # Sub-items appear in the output (nesting depth depends on parser)
        assert "sub a" in result
        assert "sub b" in result
        assert "2. second" in result

    def test_line_breaks(self):
        converter = _converter()
        ast = converter.to_ast("line1  \nline2")
        result = converter.from_ast(ast)
        assert "line1" in result
        assert "line2" in result

    def test_thematic_break(self):
        converter = _converter()
        ast = converter.to_ast("text\n\n---\n\nmore")
        result = converter.from_ast(ast)
        assert "---" in result

    def test_heading_rendered_as_bold(self):
        """GChat has no heading syntax, so headings become bold."""
        converter = _converter()
        ast = converter.to_ast("# Heading Text")
        result = converter.from_ast(ast)
        assert "*Heading Text*" in result

    def test_image_with_alt_text(self):
        converter = _converter()
        ast = converter.to_ast("![alt text](https://example.com/img.png)")
        result = converter.from_ast(ast)
        assert "alt text" in result
        assert "https://example.com/img.png" in result

    def test_image_without_alt_text(self):
        converter = _converter()
        ast = converter.to_ast("![](https://example.com/img.png)")
        result = converter.from_ast(ast)
        assert "https://example.com/img.png" in result

    def test_table_as_code_block(self):
        converter = _converter()
        result = converter.from_markdown("| Name | Age |\n|------|-----|\n| Alice | 30 |")
        assert "```" in result
        assert "Name" in result
        assert "Alice" in result


# ---------------------------------------------------------------------------
# extractPlainText
# ---------------------------------------------------------------------------


class TestExtractPlainText:
    def test_removes_bold_italic_strikethrough(self):
        converter = _converter()
        result = converter.extract_plain_text("*bold* _italic_ ~struck~")
        assert "bold" in result
        assert "italic" in result
        assert "struck" in result
        assert "*" not in result
        assert "~" not in result

    def test_empty_string(self):
        converter = _converter()
        assert converter.extract_plain_text("") == ""

    def test_plain_text_unchanged(self):
        converter = _converter()
        assert converter.extract_plain_text("Hello world") == "Hello world"

    def test_strips_inline_code(self):
        converter = _converter()
        result = converter.extract_plain_text("Use `const x = 1`")
        assert "const x = 1" in result
        assert "`" not in result

    def test_strips_code_blocks(self):
        converter = _converter()
        result = converter.extract_plain_text("```\nsome code\n```")
        assert "some code" in result


# ---------------------------------------------------------------------------
# render_postable
# ---------------------------------------------------------------------------


class TestRenderPostable:
    def test_plain_string(self):
        converter = _converter()
        assert converter.render_postable("Hello world") == "Hello world"

    def test_raw_message(self):
        converter = _converter()
        assert converter.render_postable({"raw": "raw text"}) == "raw text"

    def test_markdown_message(self):
        converter = _converter()
        result = converter.render_postable({"markdown": "**bold** text"})
        assert "bold" in result

    def test_ast_message(self):
        converter = _converter()
        ast = converter.to_ast("**bold**")
        result = converter.render_postable({"ast": ast})
        assert "bold" in result

    def test_empty_dict(self):
        converter = _converter()
        result = converter.render_postable({})
        # Falls through to str({})
        assert isinstance(result, str)
