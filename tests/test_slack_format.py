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
# renderPostable — PostableMarkdown uses AST path (issue #81)
# ---------------------------------------------------------------------------


class TestRenderPostable:
    """render_postable with PostableMarkdown must use the AST path (from_markdown),
    not the regex _markdown_to_mrkdwn, to match the TS SDK's fromAst(parseMarkdown())
    behavior. Regression guard for issue #81.
    """

    def setup_method(self):
        self.converter = SlackFormatConverter()

    def test_postable_markdown_converts_link(self):
        """[text](url) -> <url|text> via AST, not regex."""
        from chat_sdk.types import PostableMarkdown

        result = self.converter.render_postable(PostableMarkdown(markdown="Check [this](https://example.com)"))
        assert result == "Check <https://example.com|this>"

    def test_dict_markdown_converts_link(self):
        result = self.converter.render_postable({"markdown": "Check [this](https://example.com)"})
        assert result == "Check <https://example.com|this>"

    def test_postable_markdown_converts_bold(self):
        from chat_sdk.types import PostableMarkdown

        result = self.converter.render_postable(PostableMarkdown(markdown="Hello **world**!"))
        assert result == "Hello *world*!"

    def test_postable_markdown_converts_mixed(self):
        from chat_sdk.types import PostableMarkdown

        result = self.converter.render_postable(PostableMarkdown(markdown="**Bold** and [link](https://x.com)"))
        assert "*Bold*" in result
        assert "<https://x.com|link>" in result

    def test_postable_markdown_link_with_query_string(self):
        """URL with query params (no parens) converts correctly."""
        from chat_sdk.types import PostableMarkdown

        result = self.converter.render_postable(
            PostableMarkdown(markdown="See [results](https://example.com/search?q=foo&page=2)")
        )
        assert "<https://example.com/search?q=foo&page=2|results>" in result

    def test_str_passthrough_only_converts_mentions(self):
        """str input is treated as already-mrkdwn; only @mentions are wrapped."""
        result = self.converter.render_postable("Hello *world* and @george")
        assert "*world*" in result
        assert "<@george>" in result

    def test_postable_raw_bypasses_conversion(self):
        """PostableRaw reaches Slack byte-for-byte (only mention wrapping)."""
        from chat_sdk.types import PostableRaw

        result = self.converter.render_postable(PostableRaw(raw="Already *mrkdwn* text"))
        assert result == "Already *mrkdwn* text"

    def test_dict_ast_converts_via_from_ast(self):
        """{"ast": <root>} is rendered via from_ast."""
        from chat_sdk.shared.base_format_converter import parse_markdown

        ast = parse_markdown("Hello **world**!")
        result = self.converter.render_postable({"ast": ast})
        assert result == "Hello *world*!"

    def test_dict_card_uses_fallback_text(self):
        """{"card": <payload>} extracts plain text via card_to_fallback_text."""
        card_payload = {"type": "card", "title": "My Card", "body": [{"type": "text", "text": "Card body"}]}
        result = self.converter.render_postable({"card": card_payload})
        assert isinstance(result, str)
        assert len(result) > 0

    def test_object_with_card_attr_uses_fallback_text(self):
        """Object with .card attribute extracts plain text via card_to_fallback_text."""

        class FakeMessage:
            card = {"type": "card", "title": "Attr Card", "body": [{"type": "text", "text": "body text"}]}

        result = self.converter.render_postable(FakeMessage())
        assert isinstance(result, str)
        assert len(result) > 0


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

    def test_should_replace_empty_table_cells_with_a_space_to_satisfy_slack_api(self):
        ast = self.converter.to_ast("| Kind | Label |\n|------|-------|\n| FORM | Form Submission |\n| and more... | |")
        blocks = self.converter.to_blocks_with_table(ast)
        assert blocks is not None
        table_block = blocks[0]
        assert table_block["type"] == "table"
        for row in table_block["rows"]:
            for cell in row:
                assert len(cell["text"]) > 0
        assert table_block["rows"][2][1]["text"] == " "

    def test_should_handle_empty_header_cells_with_parse_markdown_production_path(self):
        from chat_sdk.shared.markdown_parser import parse_markdown

        markdown = "Here is a table:\n\n|  | Header2 |\n|---------|----------|\n| Data1 | Data2 |"
        ast = parse_markdown(markdown)
        blocks = self.converter.to_blocks_with_table(ast)
        assert blocks is not None
        assert len(blocks) == 2
        assert blocks[0]["type"] == "section"
        assert blocks[1]["type"] == "table"
        table_block = blocks[1]
        assert table_block["rows"][0][0]["text"] == " "
        for row in table_block["rows"]:
            for cell in row:
                assert len(cell["text"]) > 0


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


# ---------------------------------------------------------------------------
# render_postable — remaining branch coverage
# ---------------------------------------------------------------------------


class TestRenderPostableRemainingBranches:
    def setup_method(self):
        self.converter = SlackFormatConverter()

    def test_dict_raw_treated_as_mrkdwn_with_mention_wrapping(self):
        """{"raw": ...} is treated as already-mrkdwn; only @mentions are wrapped."""
        result = self.converter.render_postable({"raw": "Already *mrkdwn* @george"})
        assert result == "Already *mrkdwn* <@george>"

    def test_card_element_dict_renders_via_fallback_text(self):
        """{"type": "card", ...} CardElement dict uses card_to_fallback_text."""
        from chat_sdk.cards import Card

        card = Card(title="My Card")
        result = self.converter.render_postable(card)
        assert "My Card" in result

    def test_object_with_ast_attr_renders_via_from_ast(self):
        """Object with .ast attribute is rendered via from_ast."""
        from chat_sdk.shared.base_format_converter import parse_markdown

        class FakeMsg:
            ast = parse_markdown("Hello **world**!")

        result = self.converter.render_postable(FakeMsg())
        assert result == "Hello *world*!"

    def test_arbitrary_object_falls_back_to_str(self):
        """Objects with no recognized attributes fall back to str()."""

        class Opaque:
            def __str__(self):
                return "opaque output"

        result = self.converter.render_postable(Opaque())
        assert result == "opaque output"

    def test_multiple_at_mentions_in_str_all_wrapped(self):
        """All bare @mentions in a str input are converted, not just the first."""
        result = self.converter.render_postable("Ping @alice and @bob please")
        assert "<@alice>" in result
        assert "<@bob>" in result


# ---------------------------------------------------------------------------
# _node_to_mrkdwn — individual node type rendering
# ---------------------------------------------------------------------------


class TestNodeRendering:
    def setup_method(self):
        self.converter = SlackFormatConverter()

    def test_heading_renders_as_bold(self):
        assert self.converter.from_markdown("# My Heading") == "*My Heading*"

    def test_h2_heading_renders_as_bold(self):
        assert self.converter.from_markdown("## Section Title") == "*Section Title*"

    def test_blockquote_renders_with_gt_prefix(self):
        result = self.converter.from_markdown("> quoted text")
        assert result == "> quoted text"

    def test_thematic_break_renders_as_dashes(self):
        result = self.converter.from_markdown("before\n\n---\n\nafter")
        assert "---" in result
        assert "before" in result
        assert "after" in result

    def test_image_with_alt_renders_alt_and_url(self):
        result = self.converter.from_markdown("![alt text](https://example.com/img.png)")
        assert result == "alt text (https://example.com/img.png)"

    def test_image_without_alt_renders_url_only(self):
        result = self.converter.from_markdown("![](https://example.com/img.png)")
        assert result == "https://example.com/img.png"


# ---------------------------------------------------------------------------
# extract_plain_text — additional cases
# ---------------------------------------------------------------------------


class TestExtractPlainTextAdditional:
    def setup_method(self):
        self.converter = SlackFormatConverter()

    def test_removes_strikethrough_markers(self):
        assert self.converter.extract_plain_text("Hello ~world~!") == "Hello world!"

    def test_extracts_bare_url(self):
        assert self.converter.extract_plain_text("Visit <https://example.com>") == "Visit https://example.com"

    def test_extracts_channel_mention_with_name(self):
        assert self.converter.extract_plain_text("Join <#C123|general>") == "Join #general"

    def test_extracts_bare_channel_mention(self):
        assert self.converter.extract_plain_text("Join <#C123>") == "Join #C123"

    def test_user_mention_with_name_extracted(self):
        result = self.converter.extract_plain_text("Hey <@U123|john>!")
        assert result == "Hey @john!"


# ---------------------------------------------------------------------------
# to_blocks_with_table — additional cases
# ---------------------------------------------------------------------------


class TestToBlocksWithTableAdditional:
    def setup_method(self):
        self.converter = SlackFormatConverter()

    def test_returns_none_for_non_dict_ast(self):
        assert self.converter.to_blocks_with_table("not a dict") is None  # type: ignore[arg-type]
        assert self.converter.to_blocks_with_table(None) is None  # type: ignore[arg-type]

    def test_standalone_table_emits_no_extra_section_blocks(self):
        """A table with no surrounding text produces exactly one block."""
        ast = self.converter.to_ast("| A | B |\n|---|---|\n| 1 | 2 |")
        blocks = self.converter.to_blocks_with_table(ast)
        assert blocks is not None
        assert len(blocks) == 1
        assert blocks[0]["type"] == "table"

    def test_table_with_column_alignment_sets_column_settings(self):
        """Aligned table columns produce column_settings on the table block."""
        md = "| Left | Center | Right |\n|:-----|:------:|------:|\n| a | b | c |"
        ast = self.converter.to_ast(md)
        blocks = self.converter.to_blocks_with_table(ast)
        assert blocks is not None
        settings = blocks[0].get("column_settings")
        assert settings == [{"align": "left"}, {"align": "center"}, {"align": "right"}]
