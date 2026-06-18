"""Port of adapter-messenger/src/markdown.test.ts -- Messenger format converter tests.

Tests MessengerFormatConverter's to_ast, from_ast, render_postable, and
extract_plain_text methods. Messenger renders no markdown, so from_ast simply
stringifies the AST (markers preserved as literal text).
"""

from __future__ import annotations

from chat_sdk.adapters.messenger.format_converter import MessengerFormatConverter

converter = MessengerFormatConverter()


# ---------------------------------------------------------------------------
# to_ast
# ---------------------------------------------------------------------------


class TestMessengerToAst:
    """Tests for MessengerFormatConverter.to_ast."""

    def test_parses_plain_text(self):
        ast = converter.to_ast("Hello world")
        assert ast["type"] == "root"
        assert len(ast.get("children", [])) > 0

    def test_parses_markdown_bold(self):
        ast = converter.to_ast("**bold**")
        assert ast["type"] == "root"

    def test_handles_empty_text(self):
        ast = converter.to_ast("")
        assert ast["type"] == "root"


# ---------------------------------------------------------------------------
# from_ast
# ---------------------------------------------------------------------------


class TestMessengerFromAst:
    """Tests for MessengerFormatConverter.from_ast."""

    def test_roundtrips_plain_text(self):
        text = "Hello world"
        ast = converter.to_ast(text)
        result = converter.from_ast(ast)
        assert result == text

    def test_roundtrips_markdown_formatting(self):
        text = "**bold** and *italic*"
        ast = converter.to_ast(text)
        result = converter.from_ast(ast)
        assert "bold" in result
        assert "italic" in result


# ---------------------------------------------------------------------------
# render_postable
# ---------------------------------------------------------------------------


class TestMessengerRenderPostable:
    """Tests for MessengerFormatConverter.render_postable."""

    def test_renders_string_messages(self):
        assert converter.render_postable("hello") == "hello"

    def test_renders_raw_messages(self):
        assert converter.render_postable({"raw": "raw text"}) == "raw text"

    def test_renders_markdown_messages(self):
        result = converter.render_postable({"markdown": "**bold**"})
        assert "bold" in result

    def test_renders_ast_messages(self):
        ast = converter.to_ast("hello from ast")
        result = converter.render_postable({"ast": ast})
        assert "hello from ast" in result

    def test_falls_back_for_invalid_postable_message_shapes(self):
        # Divergence from upstream: TS renderPostable throws on unknown shapes;
        # the shared Python BaseFormatConverter degrades to str(message) instead
        # so a stray message can't crash delivery. Pin the Python behavior.
        result = converter.render_postable({"unknown": "value"})
        assert result == str({"unknown": "value"})


# ---------------------------------------------------------------------------
# extract_plain_text
# ---------------------------------------------------------------------------


class TestMessengerExtractPlainText:
    """Tests for MessengerFormatConverter.extract_plain_text."""

    def test_extracts_plain_text_from_markdown(self):
        result = converter.extract_plain_text("**bold** text")
        assert "bold" in result
        assert "text" in result
        # extract_plain_text strips markers (unlike from_ast)
        assert "**" not in result
