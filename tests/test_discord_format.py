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

    # -- links (vercel/chat #567, #726) ------------------------------------

    def test_should_render_a_bare_url_as_a_bare_url_not_a_masked_link(self, converter: DiscordFormatConverter):
        # The Python parser has no GFM autolinks, so build the link node
        # upstream's parser would produce for a bare URL (label == url).
        ast = {
            "type": "root",
            "children": [
                {
                    "type": "paragraph",
                    "children": [
                        {
                            "type": "link",
                            "url": "https://example.com",
                            "children": [{"type": "text", "value": "https://example.com"}],
                        }
                    ],
                }
            ],
        }
        assert converter.from_ast(ast) == "https://example.com"
        assert converter.from_ast(converter.to_ast("https://example.com")) == "https://example.com"

    def test_should_preserve_angle_brackets_on_an_autolink_to_suppress_its_embed(
        self, converter: DiscordFormatConverter
    ):
        ast = converter.to_ast("<https://example.com>")
        assert converter.from_ast(ast) == "<https://example.com>"

    def test_should_preserve_angle_brackets_in_a_masked_link_destination(self, converter: DiscordFormatConverter):
        ast = converter.to_ast("[link text](<https://example.com>)")
        assert converter.from_ast(ast) == "[link text](<https://example.com>)"

    def test_should_preserve_a_masked_link_whose_label_matches_its_url(self, converter: DiscordFormatConverter):
        source = "[https://example.com](<https://example.com>)"
        assert converter.from_ast(converter.to_ast(source)) == source

    def test_suppressed_masked_link_url_has_no_angle_brackets_in_the_ast(self, converter: DiscordFormatConverter):
        # The style lives in ``data``; the URL other adapters read is clean.
        link = converter.to_ast("[t](<https://example.com/a>)")["children"][0]["children"][0]
        assert link["url"] == "https://example.com/a"
        assert link["data"] == {"discordLinkStyle": "suppressed-masked-link"}

    def test_titled_or_non_http_angle_destinations_are_not_marked(self, converter: DiscordFormatConverter):
        # Upstream's source regex needs ``>)`` at the end and an http(s) URL.
        assert converter.from_ast(converter.to_ast('[t](<https://x.com> "ti")')) == "[t](https://x.com)"
        assert converter.from_ast(converter.to_ast("[t](<mailto:a@b.co>)")) == "[t](mailto:a@b.co)"

    def test_suppressed_autolink_style_renders_angle_brackets(self, converter: DiscordFormatConverter):
        # An AST carrying upstream's autolink style (e.g. built by TS) renders
        # the preview-suppressed form even though our parser never sets it.
        ast = {
            "type": "root",
            "children": [
                {
                    "type": "paragraph",
                    "children": [
                        {
                            "type": "link",
                            "url": "https://example.com",
                            "data": {"discordLinkStyle": "suppressed-autolink"},
                            "children": [{"type": "text", "value": "https://example.com"}],
                        }
                    ],
                }
            ],
        }
        assert converter.from_ast(ast) == "<https://example.com>"

    # -- mentions (vercel/chat #651, #652) ---------------------------------

    def test_should_not_turn_email_addresses_into_mentions(self, converter: DiscordFormatConverter):
        result = converter.from_ast(converter.to_ast("Contact me at user@example.com"))
        assert result == "Contact me at user@example.com"

    def test_should_still_convert_a_bare_mention_that_follows_a_period(self, converter: DiscordFormatConverter):
        result = converter.from_ast(converter.to_ast("read the docs.@everyone please"))
        assert result == "read the docs.<@everyone> please"

    def test_should_not_mangle_an_handle_inside_a_url(self, converter: DiscordFormatConverter):
        result = converter.render_postable({"markdown": "see https://github.com/@vercel here"})
        assert result == "see https://github.com/@vercel here"

    def test_should_not_mangle_a_mention_inside_an_inline_code_span(self, converter: DiscordFormatConverter):
        result = converter.render_postable({"markdown": "run `ping @here`"})
        assert result == "run `ping @here`"

    def test_userinfo_url_and_fenced_code_are_left_alone(self, converter: DiscordFormatConverter):
        text = "fetch https://user@host.example/x and\n```\n@bot run\n```\nthen @ops"
        assert converter.render_postable({"raw": text}) == text.replace("then @ops", "then <@ops>")

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

    def test_should_convert_a_bare_mention_in_raw_text(self, converter: DiscordFormatConverter):
        result = converter.render_postable({"raw": "Hello @user"})
        assert result == "Hello <@user>"

    def test_should_preserve_a_preview_suppressed_link_in_markdown(self, converter: DiscordFormatConverter):
        assert converter.render_postable({"markdown": "<https://example.com>"}) == "<https://example.com>"

    def test_should_preserve_a_preview_suppressed_masked_link_in_markdown(self, converter: DiscordFormatConverter):
        result = converter.render_postable({"markdown": "[link text](<https://example.com>)"})
        assert result == "[link text](<https://example.com>)"

    def test_should_not_double_wrap_an_already_formatted_mention_in_raw_text(self, converter: DiscordFormatConverter):
        result = converter.render_postable({"raw": "ping <@123> <@!456> <#789> now"})
        assert result == "ping <@123> <@!456> <#789> now"

    def test_should_leave_email_addresses_in_raw_text_untouched(self, converter: DiscordFormatConverter):
        assert converter.render_postable({"raw": "email support@vercel.com"}) == "email support@vercel.com"

    def test_should_not_mangle_an_handle_inside_a_url_in_raw_text(self, converter: DiscordFormatConverter):
        assert converter.render_postable({"raw": "see twitter.com/@jack"}) == "see twitter.com/@jack"

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
