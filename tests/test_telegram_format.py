"""Port of adapter-telegram/src/markdown.test.ts -- Telegram format converter tests.

Tests TelegramFormatConverter's fromAst, toAst, renderPostable, extractPlainText,
and roundtrip behavior.
"""

from __future__ import annotations

import re

from chat_sdk.adapters.telegram.format_converter import TelegramFormatConverter

# ---------------------------------------------------------------------------
# Shared instance
# ---------------------------------------------------------------------------

converter = TelegramFormatConverter()
TABLE_PIPE_PATTERN = re.compile(r"\|.*Name.*\|")


# ---------------------------------------------------------------------------
# fromAst (AST -> markdown string)
# ---------------------------------------------------------------------------


class TestTelegramFromAst:
    """Tests for TelegramFormatConverter.from_ast."""

    def test_plain_text_paragraph(self):
        ast = converter.to_ast("Hello world")
        result = converter.from_ast(ast)
        assert "Hello world" in result

    def test_bold(self):
        ast = converter.to_ast("**bold text**")
        result = converter.from_ast(ast)
        assert "**bold text**" in result

    def test_italic(self):
        ast = converter.to_ast("*italic text*")
        result = converter.from_ast(ast)
        assert "*italic text*" in result

    def test_strikethrough(self):
        ast = converter.to_ast("~~strikethrough~~")
        result = converter.from_ast(ast)
        assert "~~strikethrough~~" in result

    def test_links(self):
        ast = converter.to_ast("[link text](https://example.com)")
        result = converter.from_ast(ast)
        assert "[link text](https://example.com)" in result

    def test_inline_code(self):
        ast = converter.to_ast("Use `const x = 1`")
        result = converter.from_ast(ast)
        assert "`const x = 1`" in result

    def test_code_blocks(self):
        input_text = "```js\nconst x = 1;\n```"
        ast = converter.to_ast(input_text)
        output = converter.from_ast(ast)
        assert "```" in output
        assert "const x = 1;" in output

    def test_tables_to_code_blocks(self):
        ast = converter.to_ast("| Name | Age |\n|------|-----|\n| Alice | 30 |")
        result = converter.from_ast(ast)
        assert "```" in result
        assert "Name" in result
        assert "Alice" in result
        assert not TABLE_PIPE_PATTERN.search(result)


# ---------------------------------------------------------------------------
# toAst (markdown -> AST)
# ---------------------------------------------------------------------------


class TestTelegramToAst:
    """Tests for TelegramFormatConverter.to_ast."""

    def test_plain_text(self):
        ast = converter.to_ast("Hello world")
        assert ast["type"] == "root"
        assert len(ast.get("children", [])) > 0

    def test_bold(self):
        ast = converter.to_ast("**bold**")
        assert ast["type"] == "root"
        assert len(ast.get("children", [])) > 0

    def test_italic(self):
        ast = converter.to_ast("*italic*")
        assert ast["type"] == "root"
        assert len(ast.get("children", [])) > 0

    def test_inline_code(self):
        ast = converter.to_ast("`code`")
        assert ast["type"] == "root"
        assert len(ast.get("children", [])) > 0


# ---------------------------------------------------------------------------
# renderPostable
# ---------------------------------------------------------------------------


class TestTelegramRenderPostable:
    """Tests for TelegramFormatConverter.render_postable."""

    def test_plain_string(self):
        result = converter.render_postable("Hello world")
        assert result == "Hello world"

    def test_empty_string(self):
        result = converter.render_postable("")
        assert result == ""

    def test_raw_message(self):
        result = converter.render_postable({"raw": "raw content"})
        assert result == "raw content"

    def test_markdown_message(self):
        result = converter.render_postable({"markdown": "**bold** text"})
        assert "bold" in result

    def test_ast_message(self):
        ast = converter.to_ast("Hello from AST")
        result = converter.render_postable({"ast": ast})
        assert "Hello from AST" in result

    def test_bold_and_italic(self):
        result = converter.render_postable({"markdown": "**bold** and *italic*"})
        assert "**bold**" in result
        assert "*italic*" in result

    def test_markdown_table_as_code_block(self):
        result = converter.render_postable({"markdown": "| A | B |\n| --- | --- |\n| 1 | 2 |"})
        assert "```" in result
        assert "A" in result


# ---------------------------------------------------------------------------
# extractPlainText
# ---------------------------------------------------------------------------


class TestTelegramExtractPlainText:
    """Tests for TelegramFormatConverter.extract_plain_text."""

    def test_remove_bold(self):
        assert converter.extract_plain_text("Hello **world**!") == "Hello world!"

    def test_remove_italic(self):
        assert converter.extract_plain_text("Hello *world*!") == "Hello world!"

    def test_remove_strikethrough(self):
        assert converter.extract_plain_text("Hello ~~world~~!") == "Hello world!"

    def test_extract_link_text(self):
        assert converter.extract_plain_text("Check [this](https://example.com)") == "Check this"

    def test_inline_code(self):
        result = converter.extract_plain_text("Use `const x = 1`")
        assert "const x = 1" in result

    def test_code_blocks(self):
        result = converter.extract_plain_text("```js\nconst x = 1;\n```")
        assert "const x = 1;" in result

    def test_plain_text(self):
        assert converter.extract_plain_text("Hello world") == "Hello world"

    def test_empty_string(self):
        assert converter.extract_plain_text("") == ""

    def test_complex_input(self):
        input_text = "**Bold** and *italic* with [link](https://x.com)"
        result = converter.extract_plain_text(input_text)
        assert "Bold" in result
        assert "italic" in result
        assert "link" in result
        assert "**" not in result
        assert "](" not in result


# ---------------------------------------------------------------------------
# roundtrip
# ---------------------------------------------------------------------------


class TestTelegramRoundtrip:
    """Roundtrip tests (toAst -> fromAst)."""

    def test_plain_text(self):
        result = converter.from_ast(converter.to_ast("Hello world"))
        assert "Hello world" in result

    def test_bold(self):
        result = converter.from_ast(converter.to_ast("**bold text**"))
        assert "**bold text**" in result

    def test_links(self):
        result = converter.from_ast(converter.to_ast("[click here](https://example.com)"))
        assert "[click here](https://example.com)" in result

    def test_code_blocks(self):
        result = converter.from_ast(converter.to_ast("```\nconst x = 1;\n```"))
        assert "const x = 1;" in result

    def test_table_to_code_block(self):
        result = converter.from_ast(converter.to_ast("| Col1 | Col2 |\n|------|------|\n| A | B |"))
        assert "```" in result
        assert "Col1" in result
        assert "A" in result
