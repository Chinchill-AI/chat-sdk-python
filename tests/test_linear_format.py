"""Tests for Linear format conversion -- AST parsing, rendering, and postable messages.

Ported from packages/adapter-linear/src/markdown.test.ts.
"""

from __future__ import annotations

import pytest

from chat_sdk.adapters.linear.format_converter import LinearFormatConverter


@pytest.fixture
def converter():
    return LinearFormatConverter()


# ---------------------------------------------------------------------------
# toAst
# ---------------------------------------------------------------------------


class TestToAst:
    def test_plain_text(self, converter: LinearFormatConverter):
        ast = converter.to_ast("Hello world")
        assert ast["type"] == "root"
        assert len(ast["children"]) > 0

    def test_bold(self, converter: LinearFormatConverter):
        ast = converter.to_ast("**bold text**")
        assert ast["type"] == "root"

    def test_italic(self, converter: LinearFormatConverter):
        ast = converter.to_ast("_italic text_")
        assert ast["type"] == "root"

    def test_links(self, converter: LinearFormatConverter):
        ast = converter.to_ast("[Link](https://example.com)")
        assert ast["type"] == "root"

    def test_code_blocks(self, converter: LinearFormatConverter):
        ast = converter.to_ast("```\ncode\n```")
        assert ast["type"] == "root"

    def test_lists(self, converter: LinearFormatConverter):
        ast = converter.to_ast("- item 1\n- item 2\n- item 3")
        assert ast["type"] == "root"


# ---------------------------------------------------------------------------
# fromAst
# ---------------------------------------------------------------------------


class TestFromAst:
    def test_simple_ast(self, converter: LinearFormatConverter):
        ast = converter.to_ast("Hello world")
        result = converter.from_ast(ast)
        assert "Hello world" in result

    def test_round_trip_bold(self, converter: LinearFormatConverter):
        ast = converter.to_ast("**bold text**")
        result = converter.from_ast(ast)
        assert "**bold text**" in result

    def test_round_trip_links(self, converter: LinearFormatConverter):
        ast = converter.to_ast("[Link](https://example.com)")
        result = converter.from_ast(ast)
        assert "[Link](https://example.com)" in result


# ---------------------------------------------------------------------------
# renderPostable
# ---------------------------------------------------------------------------


class TestRenderPostable:
    def test_plain_string(self, converter: LinearFormatConverter):
        result = converter.render_postable("Hello world")
        assert result == "Hello world"

    def test_raw_message(self, converter: LinearFormatConverter):
        result = converter.render_postable({"raw": "raw content"})
        assert result == "raw content"

    def test_markdown_message(self, converter: LinearFormatConverter):
        result = converter.render_postable({"markdown": "**bold** text"})
        assert "bold" in result

    def test_ast_message(self, converter: LinearFormatConverter):
        ast = converter.to_ast("Hello from AST")
        result = converter.render_postable({"ast": ast})
        assert "Hello from AST" in result
