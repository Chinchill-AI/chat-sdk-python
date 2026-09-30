"""Port of packages/adapter-shared/src/code-fences.test.ts (chat@4.41.1).

Plus Python-specific checks for the regex/whitespace porting hazards
(JS ``$`` vs Python ``$``, Unicode ``\\d``, JS ``trimStart`` whitespace set).
"""

from __future__ import annotations

import re

import pytest

from chat_sdk.shared.code_fences import normalize_code_fences

_STAR = re.compile(r"\*")


def _double_stars(text: str) -> str:
    return _STAR.sub("**", text)


class TestNormalizeCodeFences:
    def test_puts_fences_on_their_own_lines_so_the_first_code_line_survives(self):
        assert normalize_code_fences("```first line\nsecond line```") == "```\nfirst line\nsecond line\n```"

    def test_separates_fences_from_surrounding_text(self):
        assert normalize_code_fences("before ```code``` after") == "before \n```\ncode\n```\n after"

    def test_returns_text_without_fences_unchanged(self):
        assert normalize_code_fences("plain text") == "plain text"

    def test_keeps_an_unpaired_fence_as_literal_text(self):
        assert normalize_code_fences("use ``` to fence code") == "use ``` to fence code"

    def test_keeps_a_fence_inside_an_inline_code_span_as_literal_text(self):
        assert normalize_code_fences("`use ``` here`") == "`use ``` here`"

    def test_keeps_a_fence_on_a_blockquote_line_as_literal_text(self):
        assert normalize_code_fences("> a ```c``` b") == "> a ```c``` b"

    def test_escapes_trailing_text_that_would_become_a_block_construct(self):
        assert normalize_code_fences("```x``` > note") == "```\nx\n```\n \\> note"
        assert normalize_code_fences("```x``` # heading") == "```\nx\n```\n \\# heading"
        assert normalize_code_fences("```x``` - item") == "```\nx\n```\n \\- item"
        assert normalize_code_fences("```x``` 1. item") == "```\nx\n```\n 1\\. item"
        assert normalize_code_fences("```a``` ``` b") == "```\na\n```\n \\``` b"

    def test_collapses_trailing_indentation_that_would_become_indented_code(self):
        assert normalize_code_fences("see ```x```\tresult is 5") == "see \n```\nx\n```\n result is 5"
        assert normalize_code_fences("see ```x```     result is 5") == "see \n```\nx\n```\n result is 5"

    def test_applies_convert_text_to_text_segments_only(self):
        assert normalize_code_fences("*a* ```*b*``` *c*", convert_text=_double_stars) == "**a** \n```\n*b*\n```\n **c**"

    def test_applies_convert_text_on_the_fenceless_fast_path(self):
        assert normalize_code_fences("*a*", convert_text=_double_stars) == "**a**"

    def test_applies_convert_code_to_fence_content(self):
        result = normalize_code_fences("```<@U1>```", convert_code=lambda code: code.replace("<@U1>", "@jane", 1))
        assert result == "```\n@jane\n```"


# ============================================================================
# Python-specific: porting-hazard checks (outputs verified against the TS
# implementation at chat@4.41.1)
# ============================================================================


class TestNormalizeCodeFencesPortingHazards:
    def test_thematic_break_before_trailing_newline_is_escaped(self):
        # JS ``$`` (no ``m`` flag) is end-of-input only; the lookahead must
        # also accept the newline itself.
        assert normalize_code_fences("```x``` ---\n") == "```\nx\n```\n \\---\n"

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("```x``` ***", "```\nx\n```\n \\***"),
            ("```x``` * * *", "```\nx\n```\n \\* * *"),
            ("```x``` ~~~", "```\nx\n```\n \\~~~"),
            ("```x``` <div>", "```\nx\n```\n \\<div>"),
            ("```x``` 1.", "```\nx\n```\n 1\\."),
        ],
    )
    def test_other_block_markers_are_escaped(self, text: str, expected: str):
        assert normalize_code_fences(text) == expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            # Not a heading: needs whitespace after 1-6 hashes.
            ("```x``` #nohash", "```\nx\n```\n #nohash"),
            ("```x``` ####### seven", "```\nx\n```\n ####### seven"),
            # Only ASCII digits start an ordered list (Python ``\d`` would match).
            ("```x``` \u0661. item", "```\nx\n```\n \u0661. item"),
        ],
    )
    def test_non_markers_are_left_unescaped(self, text: str, expected: str):
        assert normalize_code_fences(text) == expected

    def test_text_already_on_its_own_line_is_not_escaped(self):
        assert normalize_code_fences("```x```\n1. item") == "```\nx\n```\n1. item"

    def test_bom_before_blockquote_marker_counts_as_blockquote_line(self):
        # JS ``trimStart`` strips U+FEFF (Python ``lstrip()`` does not).
        assert normalize_code_fences("\ufeff> a ```c``` b") == "\ufeff> a ```c``` b"

    def test_c0_separator_before_gt_is_not_a_blockquote_line(self):
        # Python ``lstrip()`` strips U+001C; JS ``trimStart`` does not.
        assert normalize_code_fences("\x1c> a ```c``` b") == "\x1c> a \n```\nc\n```\n b"

    def test_indented_blockquote_line_keeps_fence_literal(self):
        assert normalize_code_fences("a\n  > b ```c``` d") == "a\n  > b ```c``` d"

    def test_empty_and_adjacent_fences(self):
        assert normalize_code_fences("``````") == "```\n\n```"
        assert normalize_code_fences("```\n```") == "```\n```"
        assert normalize_code_fences("a```b```c```d```e") == "a\n```\nb\n```\nc\n```\nd\n```\ne"

    def test_trailing_whitespace_only_after_fence_collapses_to_one_space(self):
        assert normalize_code_fences("```x```   ") == "```\nx\n```\n "

    def test_convert_text_sees_empty_segments(self):
        seen: list[str] = []

        def record(segment: str) -> str:
            seen.append(segment)
            return segment

        assert normalize_code_fences("```a```", convert_text=record) == "```\na\n```"
        assert seen == ["", ""]

    def test_no_extra_newline_before_a_fence_that_already_starts_a_line(self):
        assert normalize_code_fences("a\n```x```") == "a\n```\nx\n```"

    def test_empty_text_segment_between_adjacent_fences_adds_no_blank_line(self):
        # The empty converted segment must not reset the "ends with newline"
        # bookkeeping that stands in for JS ``result.endsWith("\n")``.
        assert normalize_code_fences("```a``````b```") == "```\na\n```\n```\nb\n```"

    def test_blockquote_check_only_looks_at_the_fences_own_line(self):
        assert normalize_code_fences("> a ```c``` b\nx") == "> a ```c``` b\nx"
        assert normalize_code_fences("> q\na ```c``` b") == "> q\na \n```\nc\n```\n b"

    def test_inline_code_span_does_not_cross_a_newline(self):
        # The lone backtick is literal, so the fence on the next line pairs.
        assert normalize_code_fences("`a\n```b```") == "`a\n```\nb\n```"
