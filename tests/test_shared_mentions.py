"""Port of packages/adapter-shared/src/mentions.test.ts (chat@4.41.1).

Plus Python-specific boundary sweeps for the porting hazards called out in
issue #193 (ASCII word class, JS whitespace set, case-insensitive scheme,
out-of-range look-behind/look-ahead).
"""

from __future__ import annotations

import pytest

from chat_sdk.shared.mentions import mask_code_spans, replace_bare_mentions


def to_token(text: str) -> str:
    """Render a bare @name as a generic <@name> token (the Discord/Slack shape)."""
    return replace_bare_mentions(text, lambda _mention, name: f"<@{name}>")


# ============================================================================
# replaceBareMentions
# ============================================================================


class TestReplaceBareMentionsBareMentions:
    def test_converts_a_mention_at_the_start_of_the_string(self):
        assert to_token("@alice hi") == "<@alice> hi"

    def test_converts_a_mention_after_whitespace(self):
        assert to_token("hey @alice") == "hey <@alice>"

    def test_converts_a_mention_that_follows_a_period(self):
        assert to_token("read the docs.@everyone please") == "read the docs.<@everyone> please"

    def test_converts_multiple_mentions_in_one_string(self):
        assert to_token("ping @one and @two") == "ping <@one> and <@two>"

    def test_leaves_a_lone_at_with_no_following_word_untouched(self):
        assert to_token("price @ $5") == "price @ $5"


class TestReplaceBareMentionsEmailsAndHandles:
    def test_does_not_turn_an_email_address_into_a_mention(self):
        assert to_token("Contact me at user@example.com") == "Contact me at user@example.com"

    def test_leaves_word_at_word_handles_intact(self):
        assert to_token("ping support@vercel.com now") == "ping support@vercel.com now"


class TestReplaceBareMentionsUrls:
    def test_does_not_mangle_an_handle_inside_an_https_url(self):
        assert to_token("see https://github.com/@vercel here") == "see https://github.com/@vercel here"

    def test_does_not_mangle_an_handle_inside_an_http_url(self):
        assert to_token("http://example.com/@team") == "http://example.com/@team"

    def test_does_not_mangle_an_handle_inside_a_schemeless_host_path(self):
        assert to_token("twitter.com/@jack") == "twitter.com/@jack"


class TestReplaceBareMentionsCode:
    def test_leaves_a_mention_inside_an_inline_code_span_untouched(self):
        assert to_token("run `ping @here` now") == "run `ping @here` now"

    def test_leaves_a_mention_inside_a_fenced_code_block_untouched(self):
        assert to_token("```\nping @here\n```") == "```\nping @here\n```"

    def test_still_converts_a_mention_outside_the_code_span(self):
        assert to_token("`code` then @alice") == "`code` then <@alice>"


class TestReplaceBareMentionsExistingTokens:
    def test_does_not_double_wrap_an_already_formatted_mention(self):
        assert to_token("ping <@123> now") == "ping <@123> now"

    def test_leaves_other_angle_bracket_tokens_untouched(self):
        assert to_token("see <at>bob</at>") == "see <at>bob</at>"


class TestReplaceBareMentionsReplacerContract:
    def test_passes_the_full_mention_and_the_bare_name(self):
        seen: list[tuple[str, str]] = []

        def replacer(mention: str, name: str) -> str:
            seen.append((mention, name))
            return mention

        replace_bare_mentions("hey @alice", replacer)
        assert seen == [("@alice", "alice")]

    def test_uses_the_replacers_return_value_verbatim(self):
        result = replace_bare_mentions("hey @alice", lambda _mention, name: f"<at>{name}</at>")
        assert result == "hey <at>alice</at>"


# ============================================================================
# maskCodeSpans
# ============================================================================


class TestMaskCodeSpans:
    def test_masks_an_inline_code_span(self):
        assert mask_code_spans("run `<@U1> help` now") == "run   now"

    def test_masks_a_fenced_code_block(self):
        assert mask_code_spans("see\n```\n<@U1> help\n```\nthanks") == "see\n \nthanks"

    def test_keeps_text_outside_code(self):
        assert mask_code_spans("<@U1> see `<@U2>`") == "<@U1> see  "

    def test_keeps_a_token_after_an_unterminated_fence(self):
        # Upstream asserts ``toContain("<@U1>")``. The exact output (checked
        # against the TS implementation) masks the "``" pair after the first
        # backtick, since the unterminated fence falls back to plain text.
        assert mask_code_spans("``` <@U1> help") == "`  <@U1> help"

    def test_treats_an_inline_span_that_crosses_a_line_break_as_plain_text(self):
        assert mask_code_spans("`<@U1>\nhelp`") == "`<@U1>\nhelp`"

    def test_uses_the_given_replacement(self):
        assert mask_code_spans("a `b` c", "") == "a  c"


# ============================================================================
# Python-specific: porting-hazard sweeps (issue #193)
# ============================================================================


class TestReplaceBareMentionsIndexBoundaries:
    """JS reads ``text[-1]``/``text[len]`` as ``undefined``; Python must not wrap."""

    def test_mention_at_index_zero_does_not_look_behind_at_the_last_char(self):
        # ``text[-1]`` would be "x" (a word char) and suppress the mention.
        assert to_token("@a x") == "<@a> x"

    def test_single_letter_mention_is_the_whole_string(self):
        assert to_token("@a") == "<@a>"

    def test_trailing_at_sign_is_left_alone(self):
        assert to_token("hi @") == "hi @"

    def test_lone_at_sign(self):
        assert to_token("@") == "@"

    def test_lone_backtick_is_plain_text(self):
        assert to_token("`") == "`"
        assert mask_code_spans("`") == "`"

    def test_empty_string(self):
        assert to_token("") == ""
        assert mask_code_spans("") == ""

    def test_unclosed_angle_at_end_of_string(self):
        assert to_token("a <") == "a <"

    def test_unclosed_angle_suppresses_the_mention_right_after_it(self):
        # Upstream skips ``<@name`` even when the ``>`` never arrives.
        assert to_token("<@alice and @bob") == "<@alice and <@bob>"

    def test_unclosed_angle_stops_at_the_line_break(self):
        assert to_token("a <b\n@c") == "a <b\n<@c>"

    def test_host_at_end_of_string_without_separator(self):
        assert to_token("example.com @x") == "example.com <@x>"


class TestReplaceBareMentionsAsciiWordClass:
    def test_non_ascii_letter_before_at_does_not_block_the_mention(self):
        # Upstream's isWord is ASCII-only, so "é" is a boundary, not a word char.
        assert to_token("é@x") == "é<@x>"

    def test_non_ascii_letter_after_at_is_not_a_mention(self):
        assert to_token("@édouard") == "@édouard"

    def test_mention_name_stops_at_the_first_non_ascii_char(self):
        assert to_token("@abcé") == "<@abc>é"

    def test_hyphen_ends_the_mention_name(self):
        assert to_token("@test-bot") == "<@test>-bot"


class TestReplaceBareMentionsCaseInsensitiveScheme:
    @pytest.mark.parametrize("url", ["HTTPS://host/@x", "Http://host/@x", "hTtPs://github.com/@vercel"])
    def test_uppercase_scheme_is_still_a_url(self, url: str):
        assert to_token(url) == url

    def test_bare_scheme_with_nothing_after_it_is_not_a_url(self):
        # ``index + prefix >= end`` -> not a URL; the text passes through.
        assert to_token("https://") == "https://"


class TestReplaceBareMentionsJsWhitespaceBoundary:
    """URL scanning stops at JS ``trim()`` whitespace, not Python ``str.strip()``."""

    def test_bom_ends_a_url_like_js_whitespace(self):
        # U+FEFF is JS whitespace (Python's strip() keeps it), so the URL ends
        # before it and the following mention is converted.
        assert to_token("https://a.com/x﻿@bob") == "https://a.com/x﻿<@bob>"

    @pytest.mark.parametrize("sep", ["\x1c", "\x1d", "\x1e", "\x1f"])
    def test_c0_separators_do_not_end_a_url(self, sep: str):
        # U+001C..U+001F are Python whitespace but not JS whitespace, so the
        # URL continues through them and swallows the handle.
        text = f"https://a.com/x{sep}@bob"
        assert to_token(text) == text

    def test_angle_bracket_ends_a_url(self):
        assert to_token("https://a.com/x<@U1> @bob") == "https://a.com/x<@U1> <@bob>"


class TestReplaceBareMentionsHostHeuristics:
    def test_numeric_tld_is_not_a_host(self):
        assert to_token("1.2/@x") == "1.2/<@x>"

    def test_single_letter_tld_is_not_a_host(self):
        assert to_token("a.b/@x") == "a.b/<@x>"

    def test_host_without_dot_is_not_a_host(self):
        assert to_token("localhost/@x") == "localhost/<@x>"

    @pytest.mark.parametrize("sep", ["/", "?", "#"])
    def test_host_path_separators(self, sep: str):
        text = f"example.com{sep}@x"
        assert to_token(text) == text


class TestReplaceBareMentionsCodeSpans:
    def test_inline_span_crossing_a_newline_is_plain_text(self):
        assert to_token("`a\n@b`") == "`a\n<@b>`"

    def test_unterminated_fence_is_plain_text(self):
        assert to_token("``` @x") == "``` <@x>"

    def test_code_span_inside_unclosed_angle_run(self):
        assert to_token("<a `@x` @y") == "<a `@x` <@y>"
