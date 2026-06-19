"""Tests for Slack format conversion.

Port of packages/adapter-slack/src/markdown.test.ts (chat@4.29.0).

Outgoing messages route through ``to_slack_payload`` (native ``markdown_text``)
or ``to_response_url_text`` (legacy mrkdwn -- response_url rejects
``markdown_text``). Incoming mrkdwn still parses via ``to_ast``.
"""

from __future__ import annotations

from chat_sdk.adapters.slack.format_converter import SlackFormatConverter

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
# toSlackPayload
# ---------------------------------------------------------------------------


class TestToSlackPayload:
    def setup_method(self):
        self.converter = SlackFormatConverter()

    def test_routes_plain_strings_to_text_preserving_literal_markdown_chars(self):
        assert self.converter.to_slack_payload("Use *foo* literally") == {"text": "Use *foo* literally"}

    def test_routes_raw_strings_to_text(self):
        assert self.converter.to_slack_payload({"raw": "*already mrkdwn*"}) == {"text": "*already mrkdwn*"}

    def test_routes_markdown_to_markdown_text(self):
        assert self.converter.to_slack_payload({"markdown": "## Heading\n\n- a\n- b"}) == {
            "markdown_text": "## Heading\n\n- a\n- b"
        }

    def test_routes_ast_to_markdown_text_via_stringify_markdown(self):
        ast = {
            "type": "root",
            "children": [
                {
                    "type": "paragraph",
                    "children": [{"type": "strong", "children": [{"type": "text", "value": "bold"}]}],
                }
            ],
        }
        result = self.converter.to_slack_payload({"ast": ast})
        assert "markdown_text" in result
        assert "**bold**" in result["markdown_text"]

    def test_preserves_tables_when_rendering_ast_to_markdown_text(self):
        ast = {
            "type": "root",
            "children": [
                {
                    "type": "table",
                    "align": [None, None],
                    "children": [
                        {
                            "type": "tableRow",
                            "children": [
                                {"type": "tableCell", "children": [{"type": "text", "value": "A"}]},
                                {"type": "tableCell", "children": [{"type": "text", "value": "B"}]},
                            ],
                        },
                        {
                            "type": "tableRow",
                            "children": [
                                {"type": "tableCell", "children": [{"type": "text", "value": "1"}]},
                                {"type": "tableCell", "children": [{"type": "text", "value": "2"}]},
                            ],
                        },
                    ],
                }
            ],
        }
        result = self.converter.to_slack_payload({"ast": ast})
        assert "markdown_text" in result
        lines = result["markdown_text"].strip().splitlines()
        # Markdown pipe-table rows survive (not the legacy ASCII ``` fallback).
        assert lines[0].startswith("|")
        assert "A" in lines[0] and "B" in lines[0]
        assert "1" in lines[2] and "2" in lines[2]
        assert "```" not in result["markdown_text"]

    def test_postable_raw_dataclass_routes_to_text(self):
        from chat_sdk.types import PostableRaw

        assert self.converter.to_slack_payload(PostableRaw(raw="*already mrkdwn*")) == {"text": "*already mrkdwn*"}

    def test_postable_markdown_dataclass_routes_to_markdown_text(self):
        from chat_sdk.types import PostableMarkdown

        result = self.converter.to_slack_payload(PostableMarkdown(markdown="**Bold** and [link](https://x.com)"))
        assert result == {"markdown_text": "**Bold** and [link](https://x.com)"}

    def test_postable_ast_dataclass_routes_to_markdown_text(self):
        from chat_sdk.shared.base_format_converter import parse_markdown
        from chat_sdk.types import PostableAst

        result = self.converter.to_slack_payload(PostableAst(ast=parse_markdown("Hello **world**!")))
        assert "markdown_text" in result
        assert result["markdown_text"].strip() == "Hello **world**!"

    def test_unrecognized_message_falls_back_to_empty_text(self):
        assert self.converter.to_slack_payload({"something": "else"}) == {"text": ""}


# ---------------------------------------------------------------------------
# toResponseUrlText
# ---------------------------------------------------------------------------


class TestToResponseUrlText:
    def setup_method(self):
        self.converter = SlackFormatConverter()

    def test_renders_markdown_to_slack_mrkdwn_text(self):
        result = self.converter.to_response_url_text({"markdown": "**Bold** and [link](https://example.com)"})
        assert result == "*Bold* and <https://example.com|link>"

    def test_renders_markdown_tables_as_ascii_code_blocks(self):
        result = self.converter.to_response_url_text({"markdown": "| A | B |\n|---|---|\n| 1 | 2 |"})
        assert "```\n" in result

    def test_plain_string_passes_through_with_mention_wrapping(self):
        assert self.converter.to_response_url_text("Hey @george") == "Hey <@george>"

    def test_postable_markdown_dataclass_renders_to_mrkdwn(self):
        from chat_sdk.types import PostableMarkdown

        result = self.converter.to_response_url_text(PostableMarkdown(markdown="Hello **world**!"))
        assert result == "Hello *world*!"

    def test_renders_nested_lists_with_slack_bullets(self):
        """The mrkdwn list renderer (bullet + indent) is still live for the
        response_url surface; markdown lists must not leak `-` markers."""
        result = self.converter.to_response_url_text({"markdown": "- parent\n  - child 1\n  - child 2"})
        assert result == "• parent\n  • child 1\n  • child 2"

    def test_unrecognized_message_falls_back_to_empty_string(self):
        assert self.converter.to_response_url_text({"something": "else"}) == ""


# ---------------------------------------------------------------------------
# Mentions
# ---------------------------------------------------------------------------


class TestMentions:
    def setup_method(self):
        self.converter = SlackFormatConverter()

    def test_no_double_wrap_existing_mentions_in_plain_strings(self):
        assert self.converter.to_slack_payload("Hey <@U12345>. Please select") == {
            "text": "Hey <@U12345>. Please select"
        }

    def test_no_double_wrap_existing_mentions_in_markdown(self):
        assert self.converter.to_slack_payload({"markdown": "Hey <@U12345>. Please select"}) == {
            "markdown_text": "Hey <@U12345>. Please select"
        }

    def test_rewrites_bare_at_mentions_in_plain_strings(self):
        assert self.converter.to_slack_payload("Hey @george. Please select") == {"text": "Hey <@george>. Please select"}

    def test_rewrites_bare_at_mentions_in_markdown(self):
        assert self.converter.to_slack_payload({"markdown": "Hey @george. Please select"}) == {
            "markdown_text": "Hey <@george>. Please select"
        }

    def test_does_not_mangle_email_addresses_in_plain_strings(self):
        assert self.converter.to_slack_payload("Contact user@example.com for help") == {
            "text": "Contact user@example.com for help"
        }

    def test_does_not_mangle_email_addresses_in_markdown(self):
        """`@` preceded by a word char is part of an email address, not a
        mention -- and markdown now passes through unparsed, so the address
        is not rewritten to a mailto autolink either."""
        assert self.converter.to_slack_payload({"markdown": "Contact alice@example.com"}) == {
            "markdown_text": "Contact alice@example.com"
        }

    def test_does_not_mangle_mailto_links(self):
        assert self.converter.to_slack_payload("Email <mailto:user@example.com>") == {
            "text": "Email <mailto:user@example.com>"
        }

    def test_converts_mentions_adjacent_to_non_word_punctuation(self):
        assert self.converter.to_slack_payload("(cc @george, @anne)") == {"text": "(cc <@george>, <@anne>)"}

    # -- @mention-in-URL fix (chat@4.31, a8bf99a) --------------------------------
    # A bare ``@handle`` inside a URL (path/query/fragment) or a schemeless host
    # path must NOT be rewritten into a ``<@handle>`` Slack mention, which would
    # corrupt the link. These mirror upstream markdown.test.ts:189-230 and are
    # regression guards for a confirmed pre-existing converter bug — they FAIL on
    # pre-fix code (where ``@jkyang`` etc. were wrapped into ``<@jkyang>``).
    # The string surface exercises ``_finalize``; the mrkdwn surface (via
    # ``to_response_url_text``) exercises the ``_node_to_mrkdwn`` text branch.

    def test_does_not_mangle_mention_in_url_path_plain_string(self):
        assert self.converter.to_slack_payload("See https://hackmd.io/@jkyang/B1W69XA-fe") == {
            "text": "See https://hackmd.io/@jkyang/B1W69XA-fe"
        }

    def test_does_not_mangle_mention_in_url_path_markdown(self):
        assert self.converter.to_slack_payload({"markdown": "See https://mastodon.social/@user for updates"}) == {
            "markdown_text": "See https://mastodon.social/@user for updates"
        }

    def test_does_not_mangle_mention_in_url_query_string(self):
        assert self.converter.to_slack_payload("Profile https://example.com/p?user=@george") == {
            "text": "Profile https://example.com/p?user=@george"
        }

    def test_does_not_mangle_mention_in_url_fragment(self):
        assert self.converter.to_slack_payload("Jump https://example.com/docs#@george") == {
            "text": "Jump https://example.com/docs#@george"
        }

    def test_does_not_mangle_mention_in_schemeless_host_path(self):
        # ``URL_REGEX`` does not match a schemeless host; the ``/`` in the
        # mention lookbehind guards this case.
        assert self.converter.to_slack_payload("See hackmd.io/@jkyang/abc") == {"text": "See hackmd.io/@jkyang/abc"}

    def test_still_rewrites_real_mention_after_url(self):
        # A genuine mention AFTER a URL (in the trailing slice) is still linked.
        assert self.converter.to_slack_payload("See https://hackmd.io/@jkyang/abc cc @george") == {
            "text": "See https://hackmd.io/@jkyang/abc cc <@george>"
        }

    # -- mrkdwn (node) surface: both substitution sites must carry the fix -------

    def test_url_mention_preserved_on_mrkdwn_surface(self):
        # ``_node_to_mrkdwn`` text branch must skip the URL span too.
        assert (
            self.converter.to_response_url_text({"markdown": "See https://hackmd.io/@jkyang/abc"})
            == "See https://hackmd.io/@jkyang/abc"
        )

    def test_real_mention_after_url_rewritten_on_mrkdwn_surface(self):
        assert (
            self.converter.to_response_url_text({"markdown": "See https://hackmd.io/@jkyang/abc cc @george"})
            == "See https://hackmd.io/@jkyang/abc cc <@george>"
        )

    # -- adversarial budget (docs/SELF_REVIEW.md) -------------------------------

    def test_email_address_still_preserved_after_fix(self):
        # The ``\w`` lookbehind still protects email local parts.
        assert self.converter.to_slack_payload("Contact user@example.com for help") == {
            "text": "Contact user@example.com for help"
        }

    def test_mailto_link_still_preserved_after_fix(self):
        assert self.converter.to_slack_payload("Email <mailto:user@example.com>") == {
            "text": "Email <mailto:user@example.com>"
        }

    def test_cc_george_regression_guard(self):
        # The original real-mention behavior must survive the URL-skip rewrite.
        assert self.converter.to_slack_payload("(cc @george)") == {"text": "(cc <@george>)"}

    def test_url_at_start_of_text(self):
        assert self.converter.to_slack_payload("https://example.com/@george done") == {
            "text": "https://example.com/@george done"
        }

    def test_url_at_end_of_text(self):
        # Mention before the URL is rewritten; the @handle inside the URL is not.
        assert self.converter.to_slack_payload("ping @anne https://example.com/@george") == {
            "text": "ping <@anne> https://example.com/@george"
        }

    def test_multiple_urls_with_mention_between(self):
        assert self.converter.to_slack_payload("a https://x.io/@one then @two and https://y.io/@three") == {
            "text": "a https://x.io/@one then <@two> and https://y.io/@three"
        }

    def test_already_wrapped_mention_in_url_not_double_wrapped(self):
        # An already-``<@>``-wrapped handle is left intact, and the URL handle is
        # preserved — neither is double-wrapped.
        assert self.converter.to_slack_payload("hi <@U123> see https://x.io/@bob") == {
            "text": "hi <@U123> see https://x.io/@bob"
        }


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
# render_postable -- base-class behavior after the Slack override was removed
# ---------------------------------------------------------------------------


class TestRenderPostableFallbacks:
    def setup_method(self):
        self.converter = SlackFormatConverter()

    def test_markdown_renders_to_standard_markdown(self):
        """render_postable now goes through from_ast = stringify_markdown."""
        from chat_sdk.types import PostableMarkdown

        result = self.converter.render_postable(PostableMarkdown(markdown="Hello **world**!"))
        assert result.strip() == "Hello **world**!"

    def test_card_element_dict_renders_via_fallback_text(self):
        """CardElement dicts still render via card_to_fallback_text."""
        from chat_sdk.cards import Card

        card = Card(title="My Card")
        result = self.converter.render_postable(card)
        assert "My Card" in result

    def test_object_with_ast_attr_renders_via_from_ast(self):
        """Object with .ast attribute is rendered to standard markdown."""
        from chat_sdk.shared.base_format_converter import parse_markdown

        class FakeMsg:
            ast = parse_markdown("Hello **world**!")

        result = self.converter.render_postable(FakeMsg())
        assert result.strip() == "Hello **world**!"

    def test_arbitrary_object_falls_back_to_str(self):
        """Objects with no recognized attributes fall back to str()."""

        class Opaque:
            def __str__(self):
                return "opaque output"

        result = self.converter.render_postable(Opaque())
        assert result == "opaque output"


# ---------------------------------------------------------------------------
# _node_to_mrkdwn -- node rendering on the response_url surface
# ---------------------------------------------------------------------------


class TestResponseUrlNodeRendering:
    def setup_method(self):
        self.converter = SlackFormatConverter()

    def test_heading_renders_as_bold(self):
        assert self.converter.to_response_url_text({"markdown": "# My Heading"}) == "*My Heading*"

    def test_blockquote_renders_with_gt_prefix(self):
        result = self.converter.to_response_url_text({"markdown": "> quoted text"})
        assert result == "> quoted text"

    def test_thematic_break_renders_as_dashes(self):
        result = self.converter.to_response_url_text({"markdown": "before\n\n---\n\nafter"})
        assert "---" in result
        assert "before" in result
        assert "after" in result

    def test_image_with_alt_renders_alt_and_url(self):
        result = self.converter.to_response_url_text({"markdown": "![alt text](https://example.com/img.png)"})
        assert result == "alt text (https://example.com/img.png)"

    def test_image_without_alt_renders_url_only(self):
        result = self.converter.to_response_url_text({"markdown": "![](https://example.com/img.png)"})
        assert result == "https://example.com/img.png"
