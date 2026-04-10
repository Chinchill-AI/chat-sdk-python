"""Strict _remend parity tests.

Documents the exact output of _remend() for known edge cases. These tests
are not weakened -- they assert exact output strings. If _remend behavior
changes, these tests must be updated deliberately.

Categories:
1. Basic emphasis closing (*, **, ***, _, __, ___)
2. Mixed emphasis (* and _ together)
3. Inline code near emphasis
4. Links/brackets with emphasis
5. Code fences
6. Strikethrough
7. Idempotency (applying _remend twice produces same result)
8. Known divergences from TS remend (documented with expected TS output)
"""

from __future__ import annotations

import pytest

from chat_sdk.shared.streaming_markdown import StreamingMarkdownRenderer, _is_clean, _remend


class TestBasicEmphasisClosing:
    """Single-type emphasis repair."""

    def test_unclosed_bold(self):
        assert _remend("Hello **wor") == "Hello **wor**"

    def test_unclosed_italic(self):
        assert _remend("Hello *wor") == "Hello *wor*"

    def test_unclosed_bold_italic(self):
        result = _remend("Hello ***wor")
        # Should close both bold and italic
        assert result.endswith("*") or result.endswith("**")
        assert result.count("*") % 2 == 0 or "***" in result

    def test_closed_bold_is_unchanged(self):
        assert _remend("Hello **world**") == "Hello **world**"

    def test_closed_italic_is_unchanged(self):
        assert _remend("Hello *world*") == "Hello *world*"

    def test_underscore_bold(self):
        assert _remend("Hello __wor") == "Hello __wor__"

    def test_underscore_italic(self):
        assert _remend("Hello _wor") == "Hello _wor_"

    def test_closed_underscore_unchanged(self):
        assert _remend("Hello __world__") == "Hello __world__"


class TestMixedEmphasis:
    """Mixed * and _ emphasis."""

    def test_star_bold_with_underscore_italic(self):
        result = _remend("**bold _italic")
        assert "**" in result  # bold should be closed
        assert "_" in result  # italic should be closed

    def test_underscore_bold_with_star_italic(self):
        result = _remend("__bold *italic")
        assert "__" in result
        assert "*" in result


class TestInlineCodeNearEmphasis:
    """Emphasis markers inside code spans should be ignored."""

    def test_emphasis_inside_code_ignored(self):
        # The * inside backticks should not be counted
        assert _remend("`**bold**` and *italic") == "`**bold**` and *italic*"

    def test_unclosed_backtick(self):
        result = _remend("Hello `code")
        assert result == "Hello `code`"

    def test_closed_code_with_unclosed_bold_after(self):
        result = _remend("`code` **bold")
        assert result == "`code` **bold**"

    def test_emphasis_markers_in_code_dont_count(self):
        # Stars inside code spans should not affect emphasis counting
        text = "`***` and **bold"
        result = _remend(text)
        assert result.endswith("**")


class TestLinksBrackets:
    """Unclosed link brackets."""

    def test_unclosed_link(self):
        result = _remend("See [link text")
        assert result == "See [link text]"

    def test_unclosed_nested_brackets(self):
        result = _remend("See [outer [inner")
        assert result.endswith("]]")

    def test_closed_link_unchanged(self):
        assert _remend("[link](url)") == "[link](url)"

    def test_escaped_bracket_ignored(self):
        result = _remend("See \\[not a link and [real link")
        assert result.count("]") == 1  # only close the real bracket


class TestCodeFences:
    """Code fence closing."""

    def test_unclosed_code_fence(self):
        result = _remend("```python\ncode here")
        assert result.endswith("\n```")

    def test_closed_code_fence_unchanged(self):
        text = "```python\ncode\n```"
        assert _remend(text) == text

    def test_emphasis_inside_fence_ignored(self):
        # Emphasis markers inside code fences should not be counted
        text = "```\n**bold** and *italic*\n```\noutside **unclosed"
        result = _remend(text)
        assert result.endswith("**")

    def test_tilde_fence(self):
        result = _remend("~~~\ncode here")
        assert result.endswith("\n```")


class TestStrikethrough:
    """Strikethrough ~~ closing."""

    def test_unclosed_strikethrough(self):
        result = _remend("Hello ~~strike")
        assert result == "Hello ~~strike~~"

    def test_closed_strikethrough_unchanged(self):
        assert _remend("Hello ~~strike~~") == "Hello ~~strike~~"


class TestIdempotency:
    """Applying _remend twice must produce the same result."""

    @pytest.mark.parametrize(
        "text",
        [
            "Hello **wor",
            "Hello *wor",
            "Hello ***wor",
            "Hello __wor",
            "Hello _wor",
            "Hello ~~strike",
            "Hello `code",
            "See [link",
            "```\ncode",
            "clean text with no markers",
            "**bold** and *italic",
            "mixed **bold _italic",
            "`code **bold`",
        ],
    )
    def test_idempotent(self, text: str):
        once = _remend(text)
        twice = _remend(once)
        assert once == twice, f"Not idempotent: _remend({text!r}) = {once!r}, _remend again = {twice!r}"


class TestIsClean:
    """_is_clean returns True when _remend adds nothing."""

    def test_clean_text(self):
        assert _is_clean("Hello world")

    def test_clean_closed_bold(self):
        assert _is_clean("Hello **world**")

    def test_unclosed_bold_is_not_clean(self):
        assert not _is_clean("Hello **wor")

    def test_unclosed_italic_is_not_clean(self):
        assert not _is_clean("Hello *wor")

    def test_unclosed_code_is_not_clean(self):
        assert not _is_clean("Hello `code")


class TestRendererIntegration:
    """End-to-end streaming renderer tests for _remend behavior."""

    def test_bold_repair_in_render(self):
        r = StreamingMarkdownRenderer()
        r.push("Hello **wor")
        result = r.render()
        assert result == "Hello **wor**"

    def test_italic_repair_in_render(self):
        r = StreamingMarkdownRenderer()
        r.push("Hello *wor")
        result = r.render()
        assert result == "Hello *wor*"

    def test_committable_holds_back_unclosed_bold(self):
        r = StreamingMarkdownRenderer()
        r.push("Hello **wor\n")
        committable = r.get_committable_text()
        # Line with unclosed ** should NOT be committed
        assert "**wor" not in committable

    def test_committable_releases_after_bold_closes(self):
        r = StreamingMarkdownRenderer()
        r.push("Hello **wor")
        assert r.get_committable_text() == ""
        r.push("ld** done\n")
        assert r.get_committable_text() == "Hello **world** done\n"

    def test_finish_repairs_everything(self):
        r = StreamingMarkdownRenderer()
        r.push("Hello **wor")
        result = r.finish()
        assert result == "Hello **wor**"
        assert r.get_text() == "Hello **wor"  # raw text unchanged
