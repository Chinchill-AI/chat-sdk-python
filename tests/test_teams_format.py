"""Tests for Teams format conversion -- markdown AST round-trips, HTML parsing, plain text.

Ported from packages/adapter-teams/src/markdown.test.ts.
"""

from __future__ import annotations

import pytest

from chat_sdk.adapters.teams.format_converter import TeamsFormatConverter


@pytest.fixture
def converter():
    return TeamsFormatConverter()


# ---------------------------------------------------------------------------
# fromAst (AST -> Teams format)
# ---------------------------------------------------------------------------


class TestFromAst:
    def test_bold(self, converter: TeamsFormatConverter):
        ast = converter.to_ast("**bold text**")
        result = converter.from_ast(ast)
        assert "**bold text**" in result

    def test_italic(self, converter: TeamsFormatConverter):
        ast = converter.to_ast("_italic text_")
        result = converter.from_ast(ast)
        assert "_italic text_" in result

    def test_strikethrough(self, converter: TeamsFormatConverter):
        ast = converter.to_ast("~~strikethrough~~")
        result = converter.from_ast(ast)
        assert "~~strikethrough~~" in result

    def test_inline_code(self, converter: TeamsFormatConverter):
        ast = converter.to_ast("Use `const x = 1`")
        result = converter.from_ast(ast)
        assert "`const x = 1`" in result

    def test_code_blocks(self, converter: TeamsFormatConverter):
        input_text = "```js\nconst x = 1;\n```"
        ast = converter.to_ast(input_text)
        output = converter.from_ast(ast)
        assert "```" in output
        assert "const x = 1;" in output

    def test_links(self, converter: TeamsFormatConverter):
        ast = converter.to_ast("[link text](https://example.com)")
        result = converter.from_ast(ast)
        assert "[link text](https://example.com)" in result

    def test_blockquotes(self, converter: TeamsFormatConverter):
        ast = converter.to_ast("> quoted text")
        result = converter.from_ast(ast)
        assert "> quoted text" in result

    def test_unordered_lists(self, converter: TeamsFormatConverter):
        ast = converter.to_ast("- item 1\n- item 2")
        result = converter.from_ast(ast)
        assert "- item 1" in result
        assert "- item 2" in result

    def test_ordered_lists(self, converter: TeamsFormatConverter):
        ast = converter.to_ast("1. first\n2. second")
        result = converter.from_ast(ast)
        assert "1." in result
        assert "2." in result

    def test_nested_unordered(self, converter: TeamsFormatConverter):
        result = converter.from_markdown("- parent\n  - child 1\n  - child 2")
        assert result == "- parent\n  - child 1\n  - child 2"

    def test_nested_ordered(self, converter: TeamsFormatConverter):
        result = converter.from_markdown("1. first\n   1. sub-first\n   2. sub-second\n2. second")
        assert "1. first" in result
        assert "1. sub-first" in result
        assert "2. sub-second" in result
        assert "2. second" in result

    def test_deeply_nested(self, converter: TeamsFormatConverter):
        result = converter.from_markdown("- level 1\n  - level 2\n    - level 3")
        assert "- level 1" in result
        assert "  - level 2" in result
        assert "    - level 3" in result

    def test_sibling_items_same_indent(self, converter: TeamsFormatConverter):
        result = converter.from_markdown("- item 1\n- item 2\n- item 3")
        assert result == "- item 1\n- item 2\n- item 3"

    def test_mixed_ordered_unordered_nesting(self, converter: TeamsFormatConverter):
        result = converter.from_markdown("1. first\n   - sub a\n   - sub b\n2. second")
        assert "1. first" in result
        assert "- sub a" in result
        assert "- sub b" in result
        assert "2. second" in result

    def test_mentions_to_at_tag(self, converter: TeamsFormatConverter):
        ast = converter.to_ast("Hello @someone")
        result = converter.from_ast(ast)
        assert "<at>someone</at>" in result

    def test_thematic_breaks(self, converter: TeamsFormatConverter):
        ast = converter.to_ast("text\n\n---\n\nmore")
        result = converter.from_ast(ast)
        assert "---" in result


# ---------------------------------------------------------------------------
# toAst (Teams HTML -> AST)
# ---------------------------------------------------------------------------


class TestToAst:
    def test_at_mentions(self, converter: TeamsFormatConverter):
        text = converter.extract_plain_text("<at>John</at> said hi")
        assert "@John" in text

    def test_b_tags_to_bold(self, converter: TeamsFormatConverter):
        ast = converter.to_ast("<b>bold</b>")
        assert ast["type"] == "root"
        result = converter.from_ast(ast)
        assert "**bold**" in result

    def test_strong_tags_to_bold(self, converter: TeamsFormatConverter):
        ast = converter.to_ast("<strong>bold</strong>")
        result = converter.from_ast(ast)
        assert "**bold**" in result

    def test_i_tags_to_italic(self, converter: TeamsFormatConverter):
        ast = converter.to_ast("<i>italic</i>")
        result = converter.from_ast(ast)
        assert "_italic_" in result

    def test_em_tags_to_italic(self, converter: TeamsFormatConverter):
        ast = converter.to_ast("<em>italic</em>")
        result = converter.from_ast(ast)
        assert "_italic_" in result

    def test_s_tags_to_strikethrough(self, converter: TeamsFormatConverter):
        ast = converter.to_ast("<s>struck</s>")
        result = converter.from_ast(ast)
        assert "~~struck~~" in result

    def test_a_tags_to_links(self, converter: TeamsFormatConverter):
        ast = converter.to_ast('<a href="https://example.com">link</a>')
        result = converter.from_ast(ast)
        assert "[link](https://example.com)" in result

    def test_code_tags_to_inline_code(self, converter: TeamsFormatConverter):
        ast = converter.to_ast("<code>const x</code>")
        result = converter.from_ast(ast)
        assert "`const x`" in result

    def test_pre_tags_to_code_blocks(self, converter: TeamsFormatConverter):
        ast = converter.to_ast("<pre>const x = 1;</pre>")
        result = converter.from_ast(ast)
        assert "```" in result
        assert "const x = 1;" in result

    def test_strips_remaining_html(self, converter: TeamsFormatConverter):
        text = converter.extract_plain_text("<div><span>hello</span></div>")
        assert text == "hello"

    def test_decodes_html_entities(self, converter: TeamsFormatConverter):
        text = converter.extract_plain_text("&lt;b&gt;not bold&lt;/b&gt; &amp; &quot;quoted&quot;")
        assert "<b>" in text
        assert "&" in text
        assert '"quoted"' in text


# ---------------------------------------------------------------------------
# renderPostable
# ---------------------------------------------------------------------------


class TestRenderPostable:
    def test_plain_string_with_mentions(self, converter: TeamsFormatConverter):
        result = converter.render_postable("Hello @user")
        assert result == "Hello <at>user</at>"

    def test_raw_message_with_mentions(self, converter: TeamsFormatConverter):
        result = converter.render_postable({"raw": "Hello @user"})
        assert result == "Hello <at>user</at>"

    def test_markdown_messages(self, converter: TeamsFormatConverter):
        result = converter.render_postable({"markdown": "Hello **world**"})
        assert "**world**" in result

    def test_ast_messages(self, converter: TeamsFormatConverter):
        ast = converter.to_ast("Hello **world**")
        result = converter.render_postable({"ast": ast})
        assert "**world**" in result

    def test_empty_message(self, converter: TeamsFormatConverter):
        result = converter.render_postable("")
        assert result == ""


# ---------------------------------------------------------------------------
# extractPlainText
# ---------------------------------------------------------------------------


class TestExtractPlainText:
    def test_removes_bold(self, converter: TeamsFormatConverter):
        assert converter.extract_plain_text("Hello **world**!") == "Hello world!"

    def test_removes_italic(self, converter: TeamsFormatConverter):
        assert converter.extract_plain_text("Hello _world_!") == "Hello world!"

    def test_empty_string(self, converter: TeamsFormatConverter):
        assert converter.extract_plain_text("") == ""

    def test_plain_text(self, converter: TeamsFormatConverter):
        assert converter.extract_plain_text("Hello world") == "Hello world"

    def test_inline_code(self, converter: TeamsFormatConverter):
        result = converter.extract_plain_text("Use `const x = 1`")
        assert "const x = 1" in result


# ---------------------------------------------------------------------------
# Table rendering
# ---------------------------------------------------------------------------


class TestTableRendering:
    def test_renders_gfm_tables(self, converter: TeamsFormatConverter):
        result = converter.from_markdown("| Name | Age |\n|------|-----|\n| Alice | 30 |")
        assert "| Name | Age |" in result
        assert "| --- | --- |" in result
        assert "| Alice | 30 |" in result

    def test_tables_with_pipe_syntax(self, converter: TeamsFormatConverter):
        result = converter.from_markdown("| A | B |\n|---|---|\n| 1 | 2 |")
        assert "|" in result
        assert "```" not in result
