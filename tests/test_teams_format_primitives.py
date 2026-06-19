"""Tests for the Teams format primitives subpath.

Port of ``packages/adapter-teams/src/format/index.test.ts`` and
``format/boundary.test.ts`` (vercel/chat@4.31, commit 8c71411).

Distinct from ``test_teams_format.py``, which covers the higher-level
AST-based ``TeamsFormatConverter``; this file covers the low-level
``chat_sdk.adapters.teams.format`` string primitives and mirrors the Slack
primitive test layout (``test_slack_format_primitives.py``).

Adversarial cases (per docs/SELF_REVIEW.md) extend the upstream suite with
forged-tag escaping and the disallowed-protocol SSRF gate.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from chat_sdk.adapters.teams.format import (
    convert_teams_emoji_placeholders,
    escape_teams_text,
    format_teams_mention,
    markdown_to_teams_html,
    safe_link_href,
    teams_html_to_markdown,
    teams_mention_to_plain_text,
    unescape_teams_text,
)


class TestTeamsFormatPrimitives:
    """Direct ports of the upstream ``Teams format primitives`` suite."""

    def test_escapes_and_unescapes_teams_text(self):
        escaped = escape_teams_text('<hello & "world">')
        assert escaped == "&lt;hello &amp; &quot;world&quot;&gt;"
        assert unescape_teams_text(escaped) == '<hello & "world">'

    def test_formats_and_normalizes_mentions(self):
        assert format_teams_mention("Ada & Ben") == "<at>Ada &amp; Ben</at>"
        assert teams_mention_to_plain_text("<at>Ada &amp; Ben</at> hi") == "@Ada & Ben hi"

    def test_converts_teams_html_to_markdown_ish_text(self):
        assert (
            teams_html_to_markdown('<p>Hello <strong>world</strong><br><a href="https://example.com">link</a></p>')
            == "Hello **world**\n[link](https://example.com)"
        )

    def test_converts_markdown_ish_text_to_teams_html(self):
        assert (
            markdown_to_teams_html("**Ship** [now](https://example.com)")
            == '<strong>Ship</strong> <a href="https://example.com">now</a>'
        )
        assert markdown_to_teams_html("[email](mailto:ada@example.com)") == '<a href="mailto:ada@example.com">email</a>'

    def test_renders_unsafe_markdown_links_as_plain_text(self):
        assert markdown_to_teams_html("[bad](javascript:alert)") == "bad"
        assert markdown_to_teams_html("[relative](/internal)") == "relative"

    def test_converts_common_emoji_placeholders(self):
        assert convert_teams_emoji_placeholders(":white_check_mark: done") == "✅ done"


class TestTeamsFormatAdversarial:
    """Adversarial escape / SSRF cases beyond the upstream suite."""

    def test_escape_runs_before_tag_insertion_so_forged_tags_are_inert(self):
        # User-supplied `<` must be escaped before any tag insertion, so a raw
        # `<script>` cannot survive as live markup (emit/parse symmetry).
        assert markdown_to_teams_html("<script>alert(1)</script>") == ("&lt;script&gt;alert(1)&lt;/script&gt;")

    def test_forged_anchor_in_label_does_not_emit_live_link(self):
        # A `<` inside the markdown link label is escaped first, so the only
        # anchor emitted is the real, protocol-gated one.
        out = markdown_to_teams_html("[<b>x</b>](https://ok.com)")
        assert out == '<a href="https://ok.com">&lt;b&gt;x&lt;/b&gt;</a>'

    def test_disallowed_protocols_are_rejected_by_the_ssrf_gate(self):
        # Paren-free hrefs (the upstream MARKDOWN_LINK_PATTERN href group is
        # `[^)]+`, so an inner `)` is outside the link grammar by design).
        for href in (
            "javascript:alert",
            "data:text/html,evil",
            "vbscript:msgbox",
            "file:///etc/passwd",
            "/relative",
            "ftp://example.com/x",
        ):
            assert markdown_to_teams_html(f"[label]({href})") == "label", href

    def test_allowed_protocols_pass_the_ssrf_gate(self):
        assert safe_link_href("http://example.com") is True
        assert safe_link_href("https://example.com") is True
        assert safe_link_href("mailto:ada@example.com") is True
        # urlparse lowercases the scheme, so the gate is case-insensitive.
        assert safe_link_href("HTTPS://EXAMPLE.COM") is True

    def test_disallowed_protocols_fail_the_ssrf_gate(self):
        assert safe_link_href("javascript:alert(1)") is False
        assert safe_link_href("data:text/html,x") is False
        assert safe_link_href("/internal") is False
        assert safe_link_href("") is False

    def test_unescape_does_not_collapse_double_escaped_ampersand(self):
        # `&amp;lt;` must round-trip to `&lt;`, not `<` (reverse-order unescape).
        assert unescape_teams_text("&amp;lt;") == "&lt;"


def _format_module_source() -> str:
    spec = importlib.util.find_spec("chat_sdk.adapters.teams.format")
    assert spec is not None and spec.origin is not None
    return Path(spec.origin).read_text(encoding="utf8")


class TestFormatImportBoundary:
    """Port of upstream ``format/boundary.test.ts``.

    The format subpath must stay runtime-free: its own source must not
    reference the ``microsoft_teams`` SDK, an HTTP client (``httpx`` /
    ``aiohttp``), or the high-level adapter / chat runtime. (Upstream's
    boundary test reads the module source and asserts it does not contain the
    forbidden imports — runtime ``sys.modules`` inspection is deferred until
    the ``teams/__init__.py`` lazy-subpath migration in the packaging PR.)
    """

    def test_source_does_not_import_the_sdk_runtime_or_adapter(self):
        # Inspect only the actual import statements, so the docstring (which
        # *mentions* these modules to describe the boundary) is not flagged.
        import_lines = [
            line.strip()
            for line in _format_module_source().splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        forbidden = (
            "microsoft_teams",
            "httpx",
            "aiohttp",
            "chat_sdk.adapters.teams.adapter",
            "chat_sdk.adapters.teams.types",
            "chat_sdk.adapters.teams.cards",
            "chat_sdk.chat",
        )
        present = [f"{token} :: {line}" for line in import_lines for token in forbidden if token in line]
        assert not present, f"format primitive imports forbidden modules: {present}"

    def test_emoji_reuse_does_not_duplicate_unicode(self):
        # The emoji map must come from chat_sdk.emoji, not be re-declared here.
        source = _format_module_source()
        assert "from chat_sdk.emoji import convert_emoji_placeholders" in source
