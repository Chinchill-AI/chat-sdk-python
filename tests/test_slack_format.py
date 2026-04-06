"""Tests for Slack markdown (mrkdwn) format conversion.

Port of packages/adapter-slack/src/markdown.test.ts.
"""

from __future__ import annotations

from chat_sdk.adapters.slack.format_converter import SlackFormatConverter


# ---------------------------------------------------------------------------
# fromMarkdown (markdown -> mrkdwn)
# ---------------------------------------------------------------------------


class TestFromMarkdown:
    def setup_method(self):
        self.converter = SlackFormatConverter()

    def test_converts_bold(self):
        assert self.converter.from_markdown("Hello **world**!") == "Hello *world*!"

    def test_converts_italic(self):
        result = self.converter.from_markdown("Hello _world_!")
        assert "_world_" in result

    def test_converts_strikethrough(self):
        assert self.converter.from_markdown("Hello ~~world~~!") == "Hello ~world~!"

    def test_converts_links(self):
        result = self.converter.from_markdown("Check [this](https://example.com)")
        assert "<https://example.com|this>" in result

    def test_preserves_inline_code(self):
        result = self.converter.from_markdown("Use `const x = 1`")
        assert "`const x = 1`" in result

    def test_handles_code_blocks(self):
        result = self.converter.from_markdown("```js\nconst x = 1;\n```")
        assert "```" in result
        assert "const x = 1;" in result

    def test_mixed_formatting(self):
        result = self.converter.from_markdown("**Bold** and _italic_ and [link](https://x.com)")
        assert "*Bold*" in result
        assert "_italic_" in result
        assert "<https://x.com|link>" in result


# ---------------------------------------------------------------------------
# toMarkdown (mrkdwn -> markdown)
# ---------------------------------------------------------------------------


class TestToMarkdown:
    def setup_method(self):
        self.converter = SlackFormatConverter()

    def test_converts_bold(self):
        result = self.converter.to_markdown("Hello *world*!")
        assert "**world**" in result

    def test_converts_strikethrough(self):
        result = self.converter.to_markdown("Hello ~world~!")
        assert "~~world~~" in result

    def test_converts_links_with_text(self):
        result = self.converter.to_markdown("Check <https://example.com|this>")
        assert "[this](https://example.com)" in result

    def test_converts_bare_links(self):
        result = self.converter.to_markdown("Visit <https://example.com>")
        assert "https://example.com" in result

    def test_converts_user_mentions(self):
        result = self.converter.to_markdown("Hey <@U123|john>!")
        assert "@john" in result

    def test_converts_channel_mentions(self):
        result = self.converter.to_markdown("Join <#C123|general>")
        assert "#general" in result

    def test_converts_bare_channel_mentions(self):
        result = self.converter.to_markdown("Join <#C123>")
        assert "#C123" in result


# ---------------------------------------------------------------------------
# Mentions
# ---------------------------------------------------------------------------


class TestMentions:
    def setup_method(self):
        self.converter = SlackFormatConverter()

    def test_no_double_wrap_existing_mentions(self):
        result = self.converter.render_postable("Hey <@U12345>. Please select")
        assert result == "Hey <@U12345>. Please select"

    def test_no_double_wrap_mentions_in_markdown(self):
        result = self.converter.render_postable({"markdown": "Hey <@U12345>. Please select"})
        assert result == "Hey <@U12345>. Please select"

    def test_converts_bare_at_mentions(self):
        result = self.converter.render_postable("Hey @george. Please select")
        assert "<@george>" in result

    def test_converts_bare_mentions_in_markdown(self):
        result = self.converter.render_postable({"markdown": "Hey @george. Please select"})
        assert "<@george>" in result

    def test_from_markdown_no_double_wrap(self):
        result = self.converter.from_markdown("Hey <@U12345>")
        assert result == "Hey <@U12345>"


# ---------------------------------------------------------------------------
# toPlainText
# ---------------------------------------------------------------------------


class TestExtractPlainText:
    def setup_method(self):
        self.converter = SlackFormatConverter()

    def test_removes_bold_markers(self):
        assert self.converter.extract_plain_text("Hello *world*!") == "Hello world!"

    def test_removes_italic_markers(self):
        assert self.converter.extract_plain_text("Hello _world_!") == "Hello world!"

    def test_extracts_link_text(self):
        result = self.converter.extract_plain_text("Check <https://example.com|this>")
        assert result == "Check this"

    def test_formats_user_mentions(self):
        result = self.converter.extract_plain_text("Hey <@U123>!")
        assert "@U123" in result

    def test_handles_complex_messages(self):
        result = self.converter.extract_plain_text("*Bold* and _italic_ with <https://x.com|link> and <@U123|user>")
        assert "Bold" in result
        assert "italic" in result
        assert "link" in result
        assert "user" in result
        assert "*" not in result
        assert "<" not in result


# ---------------------------------------------------------------------------
# Table rendering
# ---------------------------------------------------------------------------


class TestTableRendering:
    def setup_method(self):
        self.converter = SlackFormatConverter()

    def test_renders_markdown_tables_as_code_blocks(self):
        result = self.converter.from_markdown("| Name | Age |\n|------|-----|\n| Alice | 30 |")
        assert "```" in result
        assert "Name" in result
        assert "Age" in result
        assert "Alice" in result
        assert "30" in result

    def test_preserves_table_structure_in_code_block(self):
        result = self.converter.from_markdown("| A | B |\n|---|---|\n| 1 | 2 |")
        assert result.startswith("```\n")
        assert result.endswith("\n```")


# ---------------------------------------------------------------------------
# toBlocksWithTable
# ---------------------------------------------------------------------------


class TestToBlocksWithTable:
    def setup_method(self):
        self.converter = SlackFormatConverter()

    def test_returns_none_when_no_tables(self):
        ast = self.converter.to_ast("Hello world")
        assert self.converter.to_blocks_with_table(ast) is None

    def test_returns_native_table_block(self):
        ast = self.converter.to_ast("| Name | Age |\n|------|-----|\n| Alice | 30 |")
        blocks = self.converter.to_blocks_with_table(ast)
        assert blocks is not None
        assert len(blocks) == 1
        assert blocks[0]["type"] == "table"
        assert blocks[0]["rows"] == [
            [{"type": "raw_text", "text": "Name"}, {"type": "raw_text", "text": "Age"}],
            [{"type": "raw_text", "text": "Alice"}, {"type": "raw_text", "text": "30"}],
        ]

    def test_includes_surrounding_text(self):
        md = "Here are the results:\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\nAll done."
        ast = self.converter.to_ast(md)
        blocks = self.converter.to_blocks_with_table(ast)
        assert blocks is not None
        assert len(blocks) == 3
        assert blocks[0]["type"] == "section"
        assert "Here are the results" in blocks[0]["text"]["text"]
        assert blocks[1]["type"] == "table"
        assert blocks[2]["type"] == "section"
        assert "All done" in blocks[2]["text"]["text"]

    def test_second_table_falls_back_to_ascii(self):
        md = "| A | B |\n|---|---|\n| 1 | 2 |\n\n| C | D |\n|---|---|\n| 3 | 4 |"
        ast = self.converter.to_ast(md)
        blocks = self.converter.to_blocks_with_table(ast)
        assert blocks is not None
        assert len(blocks) == 2
        assert blocks[0]["type"] == "table"
        assert blocks[1]["type"] == "section"
        assert "```" in blocks[1]["text"]["text"]


# ---------------------------------------------------------------------------
# Nested lists
# ---------------------------------------------------------------------------


class TestNestedLists:
    def setup_method(self):
        self.converter = SlackFormatConverter()

    def test_indent_nested_unordered_lists(self):
        result = self.converter.from_markdown("- parent\n  - child 1\n  - child 2")
        assert "\u2022 parent" in result
        assert "  \u2022 child 1" in result
        assert "  \u2022 child 2" in result

    def test_indent_nested_ordered_lists(self):
        result = self.converter.from_markdown("1. first\n   1. sub-first\n   2. sub-second\n2. second")
        assert "1. first" in result
        assert "sub-first" in result
        assert "sub-second" in result
        assert "2. second" in result

    def test_deeply_nested_lists(self):
        result = self.converter.from_markdown("- level 1\n  - level 2\n    - level 3")
        assert "\u2022 level 1" in result
        assert "\u2022 level 2" in result
        assert "\u2022 level 3" in result

    def test_sibling_items_same_indent(self):
        result = self.converter.from_markdown("- item 1\n- item 2\n- item 3")
        assert result == "\u2022 item 1\n\u2022 item 2\n\u2022 item 3"

    def test_mixed_ordered_and_unordered(self):
        result = self.converter.from_markdown("1. first\n   - sub a\n   - sub b\n2. second")
        assert "1. first" in result
        assert "sub a" in result
        assert "sub b" in result
        assert "2. second" in result
