"""Tests for the Slack format primitives subpath.

Port of ``packages/adapter-slack/src/format/index.test.ts`` and
``format/boundary.test.ts`` (vercel/chat#547).
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone

import pytest

from chat_sdk.adapters.slack.format import (
    create_slack_mrkdwn,
    create_slack_plain_text,
    escape_slack_text,
    format_slack_channel,
    format_slack_date,
    format_slack_link,
    format_slack_special_mention,
    format_slack_user,
    format_slack_user_group,
    link_bare_slack_mentions,
    markdown_bold_to_slack_mrkdwn,
    slack_mrkdwn_to_markdown,
    unescape_slack_text,
)


class TestSlackFormatPrimitives:
    def test_escapes_slack_mrkdwn_control_characters(self):
        assert escape_slack_text("a & <b>") == "a &amp; &lt;b&gt;"

    def test_unescapes_slack_mrkdwn_control_characters(self):
        assert unescape_slack_text("a &amp; &lt;b&gt;") == "a & <b>"

    def test_creates_plain_text_objects(self):
        assert create_slack_plain_text("hello", emoji=True) == {
            "emoji": True,
            "text": "hello",
            "type": "plain_text",
        }

    def test_omits_optional_text_object_flags_when_unset(self):
        assert create_slack_plain_text("hello") == {"text": "hello", "type": "plain_text"}
        assert create_slack_mrkdwn("hello") == {"text": "hello", "type": "mrkdwn"}

    def test_rejects_invalid_text_object_lengths(self):
        with pytest.raises(TypeError):
            create_slack_plain_text("")
        with pytest.raises(TypeError):
            create_slack_mrkdwn("x" * 3001)

    def test_creates_mrkdwn_objects(self):
        assert create_slack_mrkdwn("*hello*", verbatim=True) == {
            "text": "*hello*",
            "type": "mrkdwn",
            "verbatim": True,
        }

    def test_formats_slack_user_mentions(self):
        assert format_slack_user("U123") == "<@U123>"

    def test_formats_slack_channel_mentions(self):
        assert format_slack_channel("C123") == "<#C123>"

    def test_formats_slack_user_group_mentions(self):
        assert format_slack_user_group("S123") == "<!subteam^S123>"

    def test_formats_slack_special_mentions(self):
        assert format_slack_special_mention("here") == "<!here>"

    def test_rejects_non_slack_ids_in_mentions(self):
        with pytest.raises(TypeError):
            format_slack_user("u123 lowercase")

    def test_formats_slack_links(self):
        assert format_slack_link("https://example.com?a=1&b=2") == "<https://example.com?a=1&b=2>"
        assert format_slack_link("https://example.com", "read <this>") == "<https://example.com|read &lt;this&gt;>"

    def test_rejects_unsafe_slack_link_control_characters(self):
        with pytest.raises(TypeError):
            format_slack_link("https://example.com|bad")

    def test_formats_slack_dates(self):
        assert format_slack_date(1_710_000_000, "{date_short}", "Mar 9") == "<!date^1710000000^{date_short}|Mar 9>"
        assert (
            format_slack_date(
                datetime(2024, 3, 9, 16, 0, 0, tzinfo=timezone.utc),
                "{time}",
                "4pm",
                link="https://example.com",
            )
            == "<!date^1710000000^{time}^https://example.com|4pm>"
        )

    def test_rejects_non_integer_date_timestamps(self):
        with pytest.raises(TypeError):
            format_slack_date(1710000000.5, "{date_short}", "Mar 9")
        with pytest.raises(TypeError):
            format_slack_date("1710000000", "{date_short}", "Mar 9")  # type: ignore[arg-type]

    def test_rejects_date_control_characters_in_tokens_and_links(self):
        with pytest.raises(TypeError):
            format_slack_date(1_710_000_000, "{date^short}", "Mar 9")
        with pytest.raises(TypeError):
            format_slack_date(1_710_000_000, "{date_short}", "Mar 9", link="https://example.com|x")

    def test_normalizes_slack_mrkdwn_to_markdown(self):
        assert (
            slack_mrkdwn_to_markdown(
                "Hey <@U123|jane> in <#C123|general>, see <https://example.com|this> and *bold* ~done~"
            )
            == "Hey @jane in #general (C123), see [this](https://example.com) and **bold** ~~done~~"
        )

    # -- special mentions (vercel/chat#960) --------------------------------

    def test_normalizes_special_mentions_and_user_groups(self):
        assert (
            slack_mrkdwn_to_markdown("<!here> <!channel> <!everyone|everyone> <!subteam^S123|@devs> <!subteam^S456>")
            == "@here @channel @everyone @devs @S456"
        )

    @pytest.mark.parametrize(
        "token",
        ["<!here>", "<!channel>", "<!everyone|everyone>", "<!subteam^S123|@devs>", "<!subteam^S456>"],
    )
    def test_preserves_inside_code_while_converting_surrounding_mentions(self, token: str):
        assert slack_mrkdwn_to_markdown(f"<!here> `{token}` <!channel>") == f"@here `{token}` @channel"
        assert slack_mrkdwn_to_markdown(f"<!here> ```{token}``` <!channel>") == f"@here \n```\n{token}\n```\n @channel"

    def test_converts_special_mentions_around_multiple_inline_code_spans(self):
        assert (
            slack_mrkdwn_to_markdown("`<!here>` <!channel> `<!everyone>` <!subteam^S123|@devs>")
            == "`<!here>` @channel `<!everyone>` @devs"
        )

    def test_does_not_treat_an_unmatched_backtick_as_a_code_span(self):
        assert slack_mrkdwn_to_markdown("use ` then <!here>") == "use ` then @here"
        assert slack_mrkdwn_to_markdown("`first\n<!here> `last") == "`first\n@here `last"

    def test_preserves_escaped_special_mentions(self):
        assert slack_mrkdwn_to_markdown("&lt;!here&gt; <!channel>") == "<!here> @channel"

    def test_keeps_link_label_backticks_from_hiding_special_mentions(self):
        assert (
            slack_mrkdwn_to_markdown("<https://example.com|`label> <!here> `open")
            == "[`label](https://example.com) @here `open"
        )

    def test_preserves_emphasis_across_inline_code(self):
        assert slack_mrkdwn_to_markdown("*before `<!here>` after* <!channel>") == "**before `<!here>` after** @channel"

    # -- code fences (vercel/chat#843) -------------------------------------

    def test_normalizes_slack_code_fences_for_commonmark_parsing(self):
        assert slack_mrkdwn_to_markdown("```first line\nsecond line\n```") == "```\nfirst line\nsecond line\n```"

    def test_puts_slack_code_fences_on_separate_lines_from_surrounding_text(self):
        assert slack_mrkdwn_to_markdown("before ```code``` after") == "before \n```\ncode\n```\n after"

    def test_keeps_an_unpaired_as_literal_text(self):
        # TS: "keeps an unpaired ``` as literal text"
        assert slack_mrkdwn_to_markdown("use ``` to fence code, *see*?") == "use ``` to fence code, **see**?"

    def test_keeps_a_inside_an_inline_code_span_as_literal_text(self):
        # TS: "keeps a ``` inside an inline code span as literal text"
        assert slack_mrkdwn_to_markdown("`use ``` here`") == "`use ``` here`"

    def test_keeps_a_on_a_blockquote_line_as_literal_text(self):
        # TS: "keeps a ``` on a blockquote line as literal text"
        assert slack_mrkdwn_to_markdown("&gt; a ```c``` b") == "> a ```c``` b"

    def test_keeps_a_inside_a_link_token_as_part_of_the_label(self):
        # TS: "keeps a ``` inside a link token as part of the label"
        assert slack_mrkdwn_to_markdown("<https://x.com|```code```>") == "[```code```](https://x.com)"

    def test_does_not_rewrite_emphasis_inside_fenced_code(self):
        assert slack_mrkdwn_to_markdown("```int *a = *b;```") == "```\nint *a = *b;\n```"
        assert slack_mrkdwn_to_markdown("```keep ~x~ raw```") == "```\nkeep ~x~ raw\n```"

    def test_still_resolves_mention_tokens_inside_fenced_code(self):
        assert slack_mrkdwn_to_markdown("```ping <@U123|jane>```") == "```\nping @jane\n```"

    def test_escapes_trailing_text_that_would_become_a_block_construct(self):
        assert slack_mrkdwn_to_markdown("```x``` &gt; note") == "```\nx\n```\n \\> note"
        assert slack_mrkdwn_to_markdown("```x``` # heading") == "```\nx\n```\n \\# heading"
        assert slack_mrkdwn_to_markdown("```x``` - item") == "```\nx\n```\n \\- item"
        assert slack_mrkdwn_to_markdown("```x``` 1. item") == "```\nx\n```\n 1\\. item"

    def test_collapses_trailing_indentation_that_would_become_indented_code(self):
        assert slack_mrkdwn_to_markdown("see ```x```\tresult is 5") == "see \n```\nx\n```\n result is 5"
        assert slack_mrkdwn_to_markdown("see ```x```     result is 5") == "see \n```\nx\n```\n result is 5"

    # -- channel ids and inverted links (vercel/chat#756) ------------------

    def test_preserves_the_channel_id_for_labeled_channel_tokens(self):
        assert slack_mrkdwn_to_markdown("Post in <#C042BLND6R6|general>") == "Post in #general (C042BLND6R6)"
        assert slack_mrkdwn_to_markdown("Post in <#C042BLND6R6>") == "Post in #C042BLND6R6"

    def test_normalizes_bare_slack_links_to_markdown_urls(self):
        assert slack_mrkdwn_to_markdown("See <https://example.com>") == "See https://example.com"

    def test_normalizes_inverted_slack_link_tokens_before_markdown_conversion(self):
        assert (
            slack_mrkdwn_to_markdown("See <docs|https://example.com> and <https://a.com|A>")
            == "See [docs](https://example.com) and [A](https://a.com)"
        )

    def test_does_not_invert_links_whose_display_label_is_itself_a_url(self):
        assert slack_mrkdwn_to_markdown("See <https://a.com|https://b.com>") == "See [https://b.com](https://a.com)"

    # -- Python-specific: adversarial inputs stay linear --------------------

    def test_adversarial_inputs_return_the_expected_output(self):
        """Unclosed ``<`` runs, unbalanced backticks and many quoted fences on
        one line must convert (the scanners memoize failed ``<`` scans and
        bound backtick/newline searches) -- asserted on output, not time."""
        n = 50_000
        assert slack_mrkdwn_to_markdown("<" * n) == "<" * n
        assert slack_mrkdwn_to_markdown("`x" * 25_000 + " <!here>") == "`x" * 25_000 + " @here"
        assert slack_mrkdwn_to_markdown("` <!here>\n" * 5_000) == "` @here\n" * 5_000
        quoted = "&gt; " + "```" * 10_000
        assert slack_mrkdwn_to_markdown(quoted) == "> " + "```" * 10_000
        assert slack_mrkdwn_to_markdown("<!here " + "<" * n) == "<!here " + "<" * n
        indented_quote = " " * 20_000 + "&gt;" + "```" * 6_665
        assert slack_mrkdwn_to_markdown(indented_quote) == " " * 20_000 + ">" + "```" * 6_665

    @pytest.mark.parametrize(
        ("mrkdwn", "expected"),
        [
            # The quoted-line answer resets on each new line, both ways.
            ("&gt; ```a``` b\nsee ```x``` y", "> ```a``` b\nsee \n```\nx\n```\n y"),
            ("see ```x``` y\n&gt; ```a``` b", "see \n```\nx\n```\n y\n> ```a``` b"),
            ("  &gt; ```q``` x\n  ```c``` y", "  > ```q``` x\n  \n```\nc\n```\n y"),
            # JS ``trimStart`` whitespace includes NBSP before ``&gt;``.
            ("\u00a0&gt; a ```c``` b", "\u00a0> a ```c``` b"),
            # A failed ``<`` scan stops at ``\n`` / ``\r``; tokens after it still convert.
            ("a < b\n<!here> ping", "a < b\n@here ping"),
            ("a <b\r<!here>", "a <b\r@here"),
            ("a <b\r```code```>", "a <b\r\n```\ncode\n```\n>"),
            # A ``` run is no inline-code opener for the mention pass.
            ("``` <!here> `", "``` @here `"),
        ],
    )
    def test_memoized_scanners_match_upstream_across_lines(self, mrkdwn: str, expected: str):
        """Exact upstream ``slackMrkdwnToMarkdown`` output for inputs that
        exercise the scanners' remembered state (``_BlockquoteLines`` and
        ``_AngleTokenScanner``) across line boundaries."""
        assert slack_mrkdwn_to_markdown(mrkdwn) == expected

    def test_converts_basic_markdown_bold_to_slack_mrkdwn_bold(self):
        assert markdown_bold_to_slack_mrkdwn("The **domain** is example.com") == "The *domain* is example.com"

    def test_links_bare_mention_like_tokens_without_touching_emails(self):
        assert link_bare_slack_mentions("(cc @U123, @U456)") == "(cc <@U123>, <@U456>)"
        assert link_bare_slack_mentions("@george") == "@george"
        assert link_bare_slack_mentions("user@example.com") == "user@example.com"


class TestFormatImportBoundary:
    def test_does_not_import_the_full_adapter_or_runtime_packages(self):
        """Importing the format subpath must not pull in slack_sdk, HTTP
        clients, or the high-level adapter module (port of upstream's
        ``format/boundary.test.ts``)."""
        code = (
            "import sys\n"
            "import chat_sdk.adapters.slack.format\n"
            "forbidden = [\n"
            "    'slack_sdk',\n"
            "    'httpx',\n"
            "    'aiohttp',\n"
            "    'chat_sdk.adapters.slack.adapter',\n"
            "]\n"
            "loaded = [name for name in forbidden if name in sys.modules]\n"
            "assert not loaded, f'format subpath imported runtime modules: {loaded}'\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
