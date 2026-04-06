"""Tests for Discord format conversion -- markdown AST round-trips and plain text extraction.

Ported from packages/adapter-discord/src/markdown.test.ts.
"""

from __future__ import annotations

import pytest

from chat_sdk.adapters.discord.format_converter import DiscordFormatConverter


@pytest.fixture
def converter():
    return DiscordFormatConverter()


# ---------------------------------------------------------------------------
# fromAst (AST -> Discord markdown)
# ---------------------------------------------------------------------------


class TestFromAst:
    def test_bold(self, converter: DiscordFormatConverter):
        ast = converter.to_ast("**bold text**")
        result = converter.from_ast(ast)
        assert "**bold text**" in result

    def test_italic(self, converter: DiscordFormatConverter):
        ast = converter.to_ast("*italic text*")
        result = converter.from_ast(ast)
        assert "*italic text*" in result

    def test_strikethrough(self, converter: DiscordFormatConverter):
        ast = converter.to_ast("~~strikethrough~~")
        result = converter.from_ast(ast)
        assert "~~strikethrough~~" in result

    def test_links(self, converter: DiscordFormatConverter):
        ast = converter.to_ast("[link text](https://example.com)")
        result = converter.from_ast(ast)
        assert "[link text](https://example.com)" in result

    def test_inline_code(self, converter: DiscordFormatConverter):
        ast = converter.to_ast("Use `const x = 1`")
        result = converter.from_ast(ast)
        assert "`const x = 1`" in result

    def test_code_blocks(self, converter: DiscordFormatConverter):
        input_text = "```js\nconst x = 1;\n```"
        ast = converter.to_ast(input_text)
        output = converter.from_ast(ast)
        assert "```" in output
        assert "const x = 1;" in output

    def test_mixed_formatting(self, converter: DiscordFormatConverter):
        input_text = "**Bold** and *italic* and [link](https://x.com)"
        ast = converter.to_ast(input_text)
        output = converter.from_ast(ast)
        assert "**Bold**" in output
        assert "*italic*" in output
        assert "[link](https://x.com)" in output

    def test_mentions_to_discord_format(self, converter: DiscordFormatConverter):
        ast = converter.to_ast("Hello @someone")
        result = converter.from_ast(ast)
        assert "<@someone>" in result

    def test_blockquotes(self, converter: DiscordFormatConverter):
        ast = converter.to_ast("> quoted text")
        result = converter.from_ast(ast)
        assert "> quoted text" in result

    def test_unordered_lists(self, converter: DiscordFormatConverter):
        ast = converter.to_ast("- item 1\n- item 2")
        result = converter.from_ast(ast)
        assert "- item 1" in result
        assert "- item 2" in result

    def test_ordered_lists(self, converter: DiscordFormatConverter):
        ast = converter.to_ast("1. item 1\n2. item 2")
        result = converter.from_ast(ast)
        assert "1." in result
        assert "2." in result

    def test_thematic_break(self, converter: DiscordFormatConverter):
        ast = converter.to_ast("text\n\n---\n\nmore text")
        result = converter.from_ast(ast)
        assert "---" in result


# ---------------------------------------------------------------------------
# toAst (Discord markdown -> AST)
# ---------------------------------------------------------------------------


class TestToAst:
    def test_bold_returns_root(self, converter: DiscordFormatConverter):
        ast = converter.to_ast("Hello **world**!")
        assert ast is not None
        assert ast["type"] == "root"

    def test_user_mentions(self, converter: DiscordFormatConverter):
        text = converter.extract_plain_text("Hello <@123456789>")
        assert text == "Hello @123456789"

    def test_user_mentions_with_nickname(self, converter: DiscordFormatConverter):
        text = converter.extract_plain_text("Hello <@!123456789>")
        assert text == "Hello @123456789"

    def test_channel_mentions(self, converter: DiscordFormatConverter):
        text = converter.extract_plain_text("Check <#987654321>")
        assert text == "Check #987654321"

    def test_role_mentions(self, converter: DiscordFormatConverter):
        text = converter.extract_plain_text("Hey <@&111222333>")
        assert text == "Hey @&111222333"

    def test_custom_emoji(self, converter: DiscordFormatConverter):
        text = converter.extract_plain_text("Nice <:thumbsup:123>")
        assert text == "Nice :thumbsup:"

    def test_animated_custom_emoji(self, converter: DiscordFormatConverter):
        text = converter.extract_plain_text("Cool <a:wave:456>")
        assert text == "Cool :wave:"

    def test_spoiler_tags(self, converter: DiscordFormatConverter):
        text = converter.extract_plain_text("Secret ||hidden text||")
        assert "hidden text" in text


# ---------------------------------------------------------------------------
# extractPlainText
# ---------------------------------------------------------------------------


class TestExtractPlainText:
    def test_removes_bold(self, converter: DiscordFormatConverter):
        assert converter.extract_plain_text("Hello **world**!") == "Hello world!"

    def test_removes_italic(self, converter: DiscordFormatConverter):
        assert converter.extract_plain_text("Hello *world*!") == "Hello world!"

    def test_removes_strikethrough(self, converter: DiscordFormatConverter):
        assert converter.extract_plain_text("Hello ~~world~~!") == "Hello world!"

    def test_extracts_link_text(self, converter: DiscordFormatConverter):
        assert converter.extract_plain_text("Check [this](https://example.com)") == "Check this"

    def test_format_user_mentions(self, converter: DiscordFormatConverter):
        result = converter.extract_plain_text("Hey <@U123>!")
        assert "@U123" in result

    def test_complex_messages(self, converter: DiscordFormatConverter):
        input_text = "**Bold** and *italic* with [link](https://x.com) and <@U123>"
        result = converter.extract_plain_text(input_text)
        assert "Bold" in result
        assert "italic" in result
        assert "link" in result
        assert "@U123" in result
        assert "**" not in result
        assert "<@" not in result

    def test_inline_code(self, converter: DiscordFormatConverter):
        result = converter.extract_plain_text("Use `const x = 1`")
        assert "const x = 1" in result

    def test_code_blocks(self, converter: DiscordFormatConverter):
        result = converter.extract_plain_text("```js\nconst x = 1;\n```")
        assert "const x = 1;" in result

    def test_empty_string(self, converter: DiscordFormatConverter):
        assert converter.extract_plain_text("") == ""

    def test_plain_text(self, converter: DiscordFormatConverter):
        assert converter.extract_plain_text("Hello world") == "Hello world"


# ---------------------------------------------------------------------------
# renderPostable
# ---------------------------------------------------------------------------


class TestRenderPostable:
    def test_plain_string_with_mention(self, converter: DiscordFormatConverter):
        result = converter.render_postable("Hello @user")
        assert result == "Hello <@user>"

    def test_raw_message_with_mention(self, converter: DiscordFormatConverter):
        result = converter.render_postable({"raw": "Hello @user"})
        assert result == "Hello <@user>"

    def test_markdown_message(self, converter: DiscordFormatConverter):
        result = converter.render_postable({"markdown": "Hello **world** @user"})
        assert "**world**" in result
        assert "<@user>" in result

    def test_empty_message(self, converter: DiscordFormatConverter):
        result = converter.render_postable("")
        assert result == ""

    def test_ast_message(self, converter: DiscordFormatConverter):
        ast = converter.to_ast("Hello **world**")
        result = converter.render_postable({"ast": ast})
        assert "**world**" in result


# ---------------------------------------------------------------------------
# Nested lists
# ---------------------------------------------------------------------------


class TestNestedLists:
    def test_nested_unordered(self, converter: DiscordFormatConverter):
        result = converter.from_markdown("- parent\n  - child 1\n  - child 2")
        assert result == "- parent\n  - child 1\n  - child 2"

    def test_nested_ordered(self, converter: DiscordFormatConverter):
        result = converter.from_markdown("1. first\n   1. sub-first\n   2. sub-second\n2. second")
        assert "1. first" in result
        assert "1. sub-first" in result
        assert "2. sub-second" in result
        assert "2. second" in result

    def test_deeply_nested(self, converter: DiscordFormatConverter):
        result = converter.from_markdown("- level 1\n  - level 2\n    - level 3")
        assert "- level 1" in result
        assert "  - level 2" in result
        assert "    - level 3" in result

    def test_sibling_items_same_indent(self, converter: DiscordFormatConverter):
        result = converter.from_markdown("- item 1\n- item 2\n- item 3")
        assert result == "- item 1\n- item 2\n- item 3"


# ---------------------------------------------------------------------------
# Table rendering
# ---------------------------------------------------------------------------


class TestTableRendering:
    def test_markdown_tables_as_code_blocks(self, converter: DiscordFormatConverter):
        result = converter.from_markdown("| Name | Age |\n|------|-----|\n| Alice | 30 |")
        assert "```" in result
        assert "Name" in result
        assert "Alice" in result
