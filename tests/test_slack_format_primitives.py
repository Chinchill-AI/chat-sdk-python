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
            == "Hey @jane in #general, see [this](https://example.com) and **bold** ~~done~~"
        )

    def test_normalizes_bare_slack_links_to_markdown_urls(self):
        assert slack_mrkdwn_to_markdown("See <https://example.com>") == "See https://example.com"

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
