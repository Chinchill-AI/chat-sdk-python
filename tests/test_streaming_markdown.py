"""Tests for StreamingMarkdownRenderer: get_committable_text, held-back tables, finish().

Ported from packages/chat/src/streaming-markdown.test.ts.
"""

from __future__ import annotations

import re

from chat_sdk.shared.streaming_markdown import (
    TABLE_ROW_RE,
    StreamingMarkdownRenderer,
    _is_inside_code_fence,
    _remend,
)

CODE_FENCE_SPLIT_RE = re.compile(r"```|~~~")


# ---------------------------------------------------------------------------
# Helper: simulate append-only streaming
# ---------------------------------------------------------------------------


def _simulate_append_stream(
    chunks: list[str],
    *,
    wrap_tables_for_append: bool = True,
) -> dict[str, object]:
    """Push chunks one at a time, computing deltas from get_committable_text().

    Returns dict with appendedText, finalText, and deltas list.
    """
    r = StreamingMarkdownRenderer(wrap_tables_for_append=wrap_tables_for_append)
    last_appended = ""
    deltas: list[str] = []

    for chunk in chunks:
        r.push(chunk)
        committable = r.get_committable_text()
        delta = committable[len(last_appended) :]
        if len(delta) > 0:
            deltas.append(delta)
            last_appended = committable

    # Final flush
    r.finish()
    final_committable = r.get_committable_text()
    final_delta = final_committable[len(last_appended) :]
    if len(final_delta) > 0:
        deltas.append(final_delta)

    return {
        "appendedText": "".join(deltas),
        "finalText": r.get_text(),
        "deltas": deltas,
    }


# ============================================================================
# Basic text accumulation
# ============================================================================


class TestStreamingMarkdownBasic:
    """Basic text accumulation and rendering tests."""

    def test_should_accumulate_basic_text(self):
        r = StreamingMarkdownRenderer()
        r.push("Hello")
        r.push(" World")
        assert r.render() == "Hello World"

    def test_should_heal_inline_markers_with_remend(self):
        r = StreamingMarkdownRenderer()
        r.push("Hello **wor")
        result = r.render()
        assert result == "Hello **wor**"

    def test_should_be_idempotent_when_no_push_between_renders(self):
        r = StreamingMarkdownRenderer()
        r.push("Hello **wor")
        first = r.render()
        second = r.render()
        assert first == second

    def test_should_return_raw_text_from_gettext_without_remend(self):
        r = StreamingMarkdownRenderer()
        r.push("Hello **wor")
        r.render()  # trigger render
        assert r.get_text() == "Hello **wor"

    def test_should_handle_empty_input(self):
        r = StreamingMarkdownRenderer()
        assert r.render() == ""
        assert r.get_text() == ""
        assert r.finish() == ""

    def test_should_handle_text_with_no_trailing_newline(self):
        r = StreamingMarkdownRenderer()
        r.push("Hello world")
        assert r.render() == "Hello world"

    def test_should_not_buffer_lines_that_dont_match_table_pattern(self):
        r = StreamingMarkdownRenderer()
        r.push("Just normal text\n")
        assert "Just normal text" in r.render()


# ============================================================================
# Table holding / confirming / releasing
# ============================================================================


class TestStreamingMarkdownTables:
    """Table header hold-back and confirmation tests."""

    def test_should_hold_back_trailing_table_header_lines(self):
        r = StreamingMarkdownRenderer()
        r.push("Text\n\n| A | B |\n")
        result = r.render()
        assert "| A | B |" not in result
        assert "Text" in result

    def test_should_confirm_table_when_separator_and_body_row_arrive(self):
        r = StreamingMarkdownRenderer()
        r.push("Text\n\n| A | B |\n")
        assert "| A | B |" not in r.render()

        # Separator alone with no body row is still held back: append-only
        # consumers (e.g. Slack chat.appendStream) parse each delta
        # independently and a header+separator emission with zero rows is
        # broken syntax. See issue #69.
        r.push("|---|---|\n")
        assert "| A | B |" not in r.render()
        assert "|---|---|" not in r.render()

        # First body row releases the entire confirmed table atomically.
        r.push("| 1 | 2 |\n")
        result = r.render()
        assert "| A | B |" in result
        assert "|---|---|" in result
        assert "| 1 | 2 |" in result

    def test_should_release_held_lines_when_next_line_is_not_a_table_row(self):
        r = StreamingMarkdownRenderer()
        r.push("Text\n\n| A | B |\n")
        assert "| A | B |" not in r.render()

        r.push("Not a table\n")
        result = r.render()
        assert "| A | B |" in result
        assert "Not a table" in result

    def test_should_not_hold_back_pipe_lines_inside_code_fences(self):
        r = StreamingMarkdownRenderer()
        r.push("```\n| A |\n")
        result = r.render()
        assert "| A |" in result

    def test_should_flush_held_lines_on_finish(self):
        r = StreamingMarkdownRenderer()
        r.push("Text\n\n| A | B |\n")
        assert "| A | B |" not in r.render()

        final = r.finish()
        assert "| A | B |" in final

    def test_should_handle_table_with_data_rows_after_separator(self):
        r = StreamingMarkdownRenderer()
        r.push("| A | B |\n|---|---|\n| 1 | 2 |\n")
        result = r.render()
        assert "| A | B |" in result
        assert "|---|---|" in result
        assert "| 1 | 2 |" in result

    def test_should_handle_multiple_consecutive_table_rows_held_back(self):
        r = StreamingMarkdownRenderer()
        r.push("Intro\n\n| A | B |\n| C | D |\n")
        result = r.render()
        assert "| A | B |" not in result
        assert "| C | D |" not in result

    def test_should_handle_code_fence_with_tilde_syntax(self):
        r = StreamingMarkdownRenderer()
        r.push("~~~\n| A |\n")
        result = r.render()
        assert "| A |" in result

    def test_should_resume_buffering_after_code_fence_closes(self):
        r = StreamingMarkdownRenderer()
        r.push("```\n| inside |\n```\n| A | B |\n")
        result = r.render()
        assert "| inside |" in result
        assert "| A | B |" not in result

    def test_should_handle_table_header_without_trailing_newline_incomplete_line(self):
        r = StreamingMarkdownRenderer()
        r.push("Text\n\n| A | B |")
        result = r.render()
        assert "Text" in result

    def test_should_break_held_block_at_empty_line(self):
        r = StreamingMarkdownRenderer()
        r.push("| A | B |\n\n| C | D |\n")
        result = r.render()
        # First pipe row is before the empty line, not held
        assert "| A | B |" in result
        # Second pipe row is after empty line and is the trailing held block
        assert "| C | D |" not in result

    def test_should_hold_table_at_very_start_of_text_no_preceding_content(self):
        r = StreamingMarkdownRenderer()
        r.push("| A | B |\n")
        result = r.render()
        assert "| A | B |" not in result

    def test_should_hold_second_table_after_confirmed_first_table(self):
        r = StreamingMarkdownRenderer()
        r.push("| A | B |\n|---|---|\n| 1 | 2 |\n")
        assert "|---|---|" in r.render()

        r.push("\n| X | Y |\n")
        result = r.render()
        assert "| A | B |" in result
        assert "| 1 | 2 |" in result
        assert "| X | Y |" not in result

    def test_should_handle_held_released_new_hold_sequence(self):
        r = StreamingMarkdownRenderer()

        # Phase 1: hold
        r.push("| A | B |\n")
        assert "| A | B |" not in r.render()

        # Phase 2: released (non-table line denies)
        r.push("Normal text\n")
        assert "| A | B |" in r.render()
        assert "Normal text" in r.render()

        # Phase 3: new hold
        r.push("| X | Y |\n")
        result = r.render()
        assert "| A | B |" in result
        assert "Normal text" in result
        assert "| X | Y |" not in result

    def test_should_confirm_table_with_alignment_markers_in_separator(self):
        r = StreamingMarkdownRenderer()
        r.push("| Left | Center | Right |\n")
        assert "| Left |" not in r.render()

        # Separator alone is held; first body row releases the table.
        r.push("|:---|:---:|---:|\n")
        assert "| Left |" not in r.render()

        r.push("| 1 | 2 | 3 |\n")
        result = r.render()
        assert "| Left | Center | Right |" in result
        assert "|:---|:---:|---:|" in result
        assert "| 1 | 2 | 3 |" in result

    def test_should_not_hold_data_rows_after_confirmed_separator(self):
        r = StreamingMarkdownRenderer()
        # Header+separator alone is held until a body row arrives (issue #69).
        r.push("| A | B |\n|---|---|\n")
        assert "|---|---|" not in r.render()

        # First body row releases the entire table block atomically.
        r.push("| 1 | 2 |\n")
        result = r.render()
        assert "| A | B |" in result
        assert "|---|---|" in result
        assert "| 1 | 2 |" in result

        # Subsequent rows commit immediately.
        r.push("| 3 | 4 |\n")
        result = r.render()
        assert "| 3 | 4 |" in result

    def test_should_handle_multiple_push_calls_before_single_render(self):
        r = StreamingMarkdownRenderer()
        r.push("| A ")
        r.push("| B |\n")
        r.push("|---|---|\n")
        r.push("| 1 | 2 |\n")
        result = r.render()
        assert "| A | B |" in result
        assert "|---|---|" in result
        assert "| 1 | 2 |" in result

    def test_should_handle_table_header_split_across_chunks(self):
        r = StreamingMarkdownRenderer()
        r.push("Text\n\n| A")
        assert "Text" in r.render()

        r.push(" | B |\n")
        assert "| A | B |" not in r.render()

        # Separator alone keeps the table held (issue #69).
        r.push("|---|---|\n")
        assert "| A | B |" not in r.render()

        # First body row releases the assembled table.
        r.push("| 1 | 2 |\n")
        assert "| A | B |" in r.render()


# ============================================================================
# Buffer state transition edge cases
# ============================================================================


class TestStreamingMarkdownBufferEdgeCases:
    """Buffer state edge cases: finish, push-after-finish, idempotency."""

    def test_should_still_work_after_push_following_finish(self):
        r = StreamingMarkdownRenderer()
        r.push("Hello")
        r.finish()
        r.push(" World")
        result = r.render()
        assert "Hello World" in result

    def test_should_be_idempotent_for_render_after_finish(self):
        r = StreamingMarkdownRenderer()
        r.push("Text\n\n| A | B |\n")
        r.finish()
        first = r.render()
        second = r.render()
        assert first == second
        assert "| A | B |" in first

    def test_should_handle_finish_with_no_held_lines(self):
        r = StreamingMarkdownRenderer()
        r.push("Just plain text\n")
        rendered = r.render()
        finished = r.finish()
        assert "Just plain text" in rendered
        assert "Just plain text" in finished

    def test_should_track_dirty_flag_correctly_across_pushrenderpushrender(self):
        r = StreamingMarkdownRenderer()
        r.push("Hello")
        r1 = r.render()
        assert r1 == "Hello"

        # No push -- should return cached
        assert r.render() == r1

        r.push(" **bold")
        r2 = r.render()
        assert r2 != r1
        assert "Hello **bold" in r2


# ============================================================================
# getCommittableText tests
# ============================================================================


class TestGetCommittableText:
    """Tests for get_committable_text (append-only streaming)."""

    def test_getcommittabletext_should_hold_back_incomplete_line_with_unclosed_bold(self):
        r = StreamingMarkdownRenderer()
        r.push("Hello **wor")
        assert r.get_committable_text() == ""

    def test_getcommittabletext_should_hold_back_unclosed_bold_on_complete_line(self):
        r = StreamingMarkdownRenderer()
        r.push("Hello **wor\n")
        committable = r.get_committable_text()
        # Line with unclosed ** is held back (not clean)
        assert committable == "Hello "

    def test_getcommittabletext_should_release_when_bold_closes(self):
        r = StreamingMarkdownRenderer()
        r.push("Hello **wor")
        assert r.get_committable_text() == ""

        r.push("ld** done\n")
        assert r.get_committable_text() == "Hello **world** done\n"

    def test_getcommittabletext_should_hold_back_unclosed_italic(self):
        r = StreamingMarkdownRenderer()
        r.push("Hello *ita\n")
        assert r.get_committable_text() == "Hello "

    def test_getcommittabletext_should_hold_back_unclosed_strikethrough(self):
        r = StreamingMarkdownRenderer()
        r.push("Hello ~~str\n")
        assert r.get_committable_text() == "Hello "

    def test_getcommittabletext_should_hold_back_unclosed_inline_code(self):
        r = StreamingMarkdownRenderer()
        r.push("Hello `cod\n")
        assert r.get_committable_text() == "Hello "

    def test_getcommittabletext_should_hold_back_unclosed_link(self):
        r = StreamingMarkdownRenderer()
        r.push("See [link text\n")
        assert r.get_committable_text() == "See "

    def test_getcommittabletext_should_release_when_link_closes(self):
        r = StreamingMarkdownRenderer()
        r.push("See [link text\n")
        assert r.get_committable_text() == "See "

        r.push("](https://example.com)\n")
        committable = r.get_committable_text()
        assert "See " in committable

    def test_getcommittabletext_should_return_clean_text_when_all_markers_balanced(self):
        r = StreamingMarkdownRenderer()
        r.push("Hello **world** and *italic* done\n")
        assert r.get_committable_text() == "Hello **world** and *italic* done\n"

    def test_getcommittabletext_should_hold_back_table_rows(self):
        r = StreamingMarkdownRenderer()
        r.push("Text\n\n| A | B |\n")
        committable = r.get_committable_text()
        assert "| A | B |" not in committable
        assert "Text" in committable

    def test_getcommittabletext_should_wrap_confirmed_table_in_code_fence(self):
        r = StreamingMarkdownRenderer()
        r.push("Text\n\n| A | B |\n|---|---|\n| 1 | 2 |\n")
        committable = r.get_committable_text()
        assert "```" in committable
        assert "| A | B |" in committable
        assert "| 1 | 2 |" in committable
        assert "Text" in committable

    def test_getcommittabletext_should_not_buffer_inside_code_fence(self):
        r = StreamingMarkdownRenderer()
        r.push("```\n| A |\n")
        assert "| A |" in r.get_committable_text()

    def test_getcommittabletext_should_return_full_text_after_finish(self):
        r = StreamingMarkdownRenderer()
        r.push("Text\n\n| A | B |\n")
        assert "| A | B |" not in r.get_committable_text()
        r.finish()
        assert "| A | B |" in r.get_committable_text()

    def test_getcommittabletext_should_flush_unclosed_markers_after_finish(self):
        r = StreamingMarkdownRenderer()
        r.push("Hello **wor\n")
        before_finish = r.get_committable_text()
        assert "Hello " in before_finish
        r.finish()
        # After finish, everything is flushed
        assert r.get_committable_text() == "Hello **wor\n"


# ============================================================================
# getCommittableText delta tests
# ============================================================================


class TestGetCommittableTextDelta:
    """Delta tests for commit-based streaming."""

    def test_getcommittabletext_delta_should_stream_table_in_code_fence(self):
        r = StreamingMarkdownRenderer()
        last_appended = ""

        # Push intro
        r.push("Hello\n\n")
        committable = r.get_committable_text()
        delta = committable[len(last_appended) :]
        assert delta == "Hello\n\n"
        last_appended = committable

        # Push table header -- held back
        r.push("| A | B |\n")
        committable = r.get_committable_text()
        delta = committable[len(last_appended) :]
        assert delta == ""

        # Push separator -- still held; header+separator without a body row
        # would be broken markup for append-only consumers (issue #69).
        r.push("|---|---|\n")
        committable = r.get_committable_text()
        delta = committable[len(last_appended) :]
        assert delta == ""

        # First data row releases header+separator+row atomically.
        r.push("| 1 | 2 |\n")
        committable = r.get_committable_text()
        delta = committable[len(last_appended) :]
        assert "```" in delta
        assert "| A | B |" in delta
        assert "|---|---|" in delta
        assert "| 1 | 2 |" in delta
        # Should NOT have a closing ```
        assert "```\n```" not in delta
        last_appended = committable

        # Blank line ends table -- closes code fence
        r.push("\nMore text\n")
        committable = r.get_committable_text()
        delta = committable[len(last_appended) :]
        assert "```" in delta
        assert "More text" in delta

    def test_getcommittabletext_delta_should_work_for_inline_markers_in_appendonly_streaming(self):
        r = StreamingMarkdownRenderer()
        last_appended = ""

        r.push("Hello ")
        assert r.get_committable_text() == ""

        r.push("**world** done\n")
        committable = r.get_committable_text()
        delta = committable[len(last_appended) :]
        assert delta == "Hello **world** done\n"
        last_appended = committable

        # Push new line with unclosed bold
        r.push("More **text")
        committable = r.get_committable_text()
        delta = committable[len(last_appended) :]
        assert delta == ""

        # Close bold on same line
        r.push("** end\n")
        committable = r.get_committable_text()
        delta = committable[len(last_appended) :]
        assert delta == "More **text** end\n"


# ============================================================================
# Append-only stream integration tests
# ============================================================================


class TestAppendOnlyStreaming:
    """Integration tests simulating the exact Slack adapter pattern."""

    def test_appendonly_plain_text_streams_without_modification(self):
        result = _simulate_append_stream(["Hello ", "World", "!\n"])
        assert result["appendedText"] == "Hello World!\n"

    def test_appendonly_bold_markers_are_held_then_released(self):
        result = _simulate_append_stream(["Hello ", "**bold", "** text\n"])
        text = result["appendedText"]
        assert "**bold**" in text
        assert "Hello " in text

    def test_appendonly_table_is_wrapped_in_code_fence(self):
        result = _simulate_append_stream(
            [
                "Intro\n\n",
                "| A | B |\n",
                "|---|---|\n",
                "| 1 | 2 |\n",
                "| 3 | 4 |\n",
                "\nAfter table\n",
            ]
        )
        text = result["appendedText"]
        assert "```\n| A | B |" in text
        assert "| 1 | 2 |" in text
        assert "| 3 | 4 |" in text
        assert "```\n\nAfter table" in text
        # Intro should be outside the code fence
        assert text.index("Intro") < text.index("```")

    def test_appendonly_table_can_stream_without_code_fence_when_wrapping_disabled(self):
        result = _simulate_append_stream(
            [
                "Intro\n\n",
                "| A | B |\n",
                "|---|---|\n",
                "| 1 | 2 |\n",
                "| 3 | 4 |\n",
                "\nAfter table\n",
            ],
            wrap_tables_for_append=False,
        )
        text = result["appendedText"]
        assert isinstance(text, str)
        assert "| A | B |" in text
        assert "| 1 | 2 |" in text
        assert "| 3 | 4 |" in text
        assert "After table" in text
        assert "```" not in text

    def test_appendonly_table_at_end_of_stream_is_flushed_on_finish(self):
        result = _simulate_append_stream(
            [
                "Text\n\n",
                "| A | B |\n",
                "|---|---|\n",
                "| 1 | 2 |\n",
            ]
        )
        text = result["appendedText"]
        assert "| A | B |" in text
        assert "```" in text
        # The final delta should include remaining content
        assert result["deltas"][-1]

    def test_appendonly_concatenated_deltas_equal_getcommittabletext_after_finish(self):
        result = _simulate_append_stream(
            [
                "Hello **world**\n",
                "\n",
                "| H1 | H2 |\n",
                "| - | - |\n",
                "| a | b |\n",
                "| c | d |\n",
                "\nDone\n",
            ]
        )
        text = result["appendedText"]
        assert "Hello **world**" in text
        assert "| H1 | H2 |" in text
        assert "| a | b |" in text
        assert "| c | d |" in text
        assert "Done" in text
        assert "```" in text

    def test_appendonly_concatenated_deltas_equal_final_text_when_wrapping_disabled(self):
        result = _simulate_append_stream(
            [
                "Hello **world**\n",
                "\n",
                "| H1 | H2 |\n",
                "| - | - |\n",
                "| a | b |\n",
                "| c | d |\n",
                "\nDone\n",
            ],
            wrap_tables_for_append=False,
        )
        text = result["appendedText"]
        assert isinstance(text, str)
        assert "Hello **world**" in text
        assert "| H1 | H2 |" in text
        assert "| a | b |" in text
        assert "| c | d |" in text
        assert "Done" in text
        assert "```" not in text

    def test_appendonly_concatenated_deltas_are_monotonic_each_is_a_suffix(self):
        """Core invariant: concatenated deltas must equal final output."""
        r = StreamingMarkdownRenderer()
        last_appended = ""
        deltas: list[str] = []
        chunks = [
            "Hello **world**\n",
            "\n",
            "| A | B |\n",
            "| - | - |\n",
            "| 1 | 2 |\n",
            "\nDone\n",
        ]

        for chunk in chunks:
            r.push(chunk)
            committable = r.get_committable_text()
            # Verify monotonicity
            assert committable.startswith(last_appended), (
                "Monotonicity broke: committable does not start with last_appended"
            )
            delta = committable[len(last_appended) :]
            if len(delta) > 0:
                deltas.append(delta)
                last_appended = committable

        r.finish()
        final_committable = r.get_committable_text()
        assert final_committable.startswith(last_appended)
        final_delta = final_committable[len(last_appended) :]
        if len(final_delta) > 0:
            deltas.append(final_delta)

        assert "".join(deltas) == final_committable

    def test_appendonly_final_flush_uses_transformed_text_not_raw_text(self):
        r = StreamingMarkdownRenderer()
        last_appended = ""

        for chunk in [
            "Intro\n\n",
            "| ID | Name |\n",
            "|---|---|\n",
            "| 1 | Alice |\n",
        ]:
            r.push(chunk)
            committable = r.get_committable_text()
            delta = committable[len(last_appended) :]
            if len(delta) > 0:
                last_appended = committable

        r.finish()
        raw = r.get_text()
        transformed = r.get_committable_text()

        assert "```" in transformed
        assert len(transformed) > len(raw)

        correct_delta = transformed[len(last_appended) :]
        assert last_appended + correct_delta == transformed

        # The buggy approach using raw text would NOT match
        buggy_delta = raw[len(last_appended) :]
        assert last_appended + buggy_delta != transformed

    def test_appendonly_table_rows_split_midtoken_stream_correctly(self):
        result = _simulate_append_stream(
            [
                "Text\n\n",
                "| A",
                " | B |\n",
                "|---|",
                "---|\n",
                "| 1 | ",
                "2 |\n",
            ]
        )
        text = result["appendedText"]
        assert "```" in text
        assert "| A | B |" in text
        assert "| 1 | 2 |" in text
        # No partial content outside the code fence
        before_fence = text[: text.index("```")]
        assert "| A" not in before_fence

    def test_appendonly_multiple_tables_in_sequence(self):
        result = _simulate_append_stream(
            [
                "First table:\n\n",
                "| A |\n",
                "|---|\n",
                "| 1 |\n",
                "\nSecond table:\n\n",
                "| X |\n",
                "|---|\n",
                "| 9 |\n",
                "\nDone\n",
            ]
        )
        text = result["appendedText"]
        fence_count = len(re.findall(r"```", text))
        assert fence_count == 4  # open+close for each table
        assert "| 1 |" in text
        assert "| 9 |" in text


# ============================================================================
# Real-world progressive table rendering
# ============================================================================


class TestStreamingMarkdownRealWorld:
    """Real-world progressive table streaming tests."""

    def test_should_render_realworld_table_with_singledash_separators_progressively(self):
        r = StreamingMarkdownRenderer()

        r.push("Here's a table with 20 rows of sample data:\n\n")
        assert "Here's a table" in r.render()

        r.push("| ID | Name | Department | Age | Salary | City | Join Date | Status |\n")
        result = r.render()
        assert "| ID |" not in result
        assert "Here's a table" in result

        # Separator alone is held -- need a body row to confirm (issue #69).
        r.push("| - | - | - | - | - | - | - | - |\n")
        result = r.render()
        assert "| ID |" not in result
        assert "| - |" not in result

        # First body row releases header+separator+row atomically.
        r.push("| 1 | Sarah Johnson | Engineering | 32 | $95,000 | Seattle | 2019-03-15 | Active |\n")
        result = r.render()
        assert "| ID |" in result
        assert "| - |" in result
        assert "Sarah Johnson" in result

        r.push("| 2 | Michael")
        result = r.render()
        assert "Sarah Johnson" in result

        r.push(" Chen | Marketing | 28 | $72,000 | Austin | 2020-07-22 | Active |\n")
        result = r.render()
        assert "Michael Chen" in result

    def test_appendonly_realworld_20row_table_streams_correctly(self):
        header = "| ID | Name | Department | Age | Salary | City | Join Date |\n"
        sep = "| - | - | - | - | - | - | - |\n"
        rows = [
            "| 1 | Alice Johnson | Engineering | 28 | $75,000 | New York | 2021-03-15 |\n",
            "| 2 | Bob Smith | Marketing | 35 | $68,000 | Los Angeles | 2019-07-22 |\n",
            "| 3 | Carol Davis | Finance | 31 | $82,000 | Chicago | 2021-01-10 |\n",
        ]

        chunks = ["Here's a table:\n\n", header, sep, *rows]
        result = _simulate_append_stream(chunks)
        text = result["appendedText"]

        assert "Alice Johnson" in text
        assert "Bob Smith" in text
        assert "Carol Davis" in text
        assert "```" in text
        # No garbled text
        assert "Join Date" in text
        assert "JoinJoin" not in text
        # Raw text has all content
        assert "Alice Johnson" in result["finalText"]
        assert "| 3 |" in result["finalText"]


# ============================================================================
# Exhaustive prefix invariants
# ============================================================================


class TestExhaustivePrefixInvariants:
    """Feed complex markdown char-by-char and verify invariants."""

    COMPLEX_MARKDOWN = (
        "# Heading\n"
        "\n"
        "Some **bold** and *italic* text with `inline code` here.\n"
        "\n"
        "A [link](https://example.com) and ~~deleted~~ stuff.\n"
        "\n"
        "## Table section\n"
        "\n"
        "| Name | Age | City |\n"
        "| - | - | - |\n"
        "| Alice | 30 | NYC |\n"
        "| Bob | 25 | LA |\n"
        "\n"
        "Text after table with **bold again**.\n"
        "\n"
        "```\n"
        "code block with | pipes | inside\n"
        "and **markers** that are literal\n"
        "```\n"
        "\n"
        "Final paragraph.\n"
    )

    def test_render_output_is_always_valid_markdown_remend_is_idempotent(self):
        """render() output is always valid (remend is idempotent)."""
        r = StreamingMarkdownRenderer()
        for i in range(len(self.COMPLEX_MARKDOWN)):
            r.push(self.COMPLEX_MARKDOWN[i])
            rendered = r.render()
            double_remended = _remend(rendered)
            assert len(double_remended) <= len(rendered), (
                f"render() at position {i} produced text that remend would still modify"
            )

    def test_getcommittabletext_output_is_always_monotonic_appendonly_safe(self):
        """get_committable_text() output is always monotonic (append-only safe)."""
        r = StreamingMarkdownRenderer()
        prev = ""
        for i in range(len(self.COMPLEX_MARKDOWN)):
            r.push(self.COMPLEX_MARKDOWN[i])
            committable = r.get_committable_text()
            assert committable.startswith(prev), f"Monotonicity broke at char {i} ({repr(self.COMPLEX_MARKDOWN[i])})"
            prev = committable

    def test_getcommittabletext_never_contains_raw_table_pipes_outside_code_fences(self):
        r = StreamingMarkdownRenderer()
        for i in range(len(self.COMPLEX_MARKDOWN)):
            r.push(self.COMPLEX_MARKDOWN[i])
            committable = r.get_committable_text()

            # Extract text outside ALL code fences
            sections = CODE_FENCE_SPLIT_RE.split(committable)
            for s in range(0, len(sections), 2):
                outside = sections[s] if s < len(sections) else ""
                if not outside:
                    continue
                for line in outside.split("\n"):
                    trimmed = line.strip()
                    if trimmed == "":
                        continue
                    looks_like_table = TABLE_ROW_RE.match(trimmed) is not None and trimmed.count("|") >= 3
                    assert not looks_like_table, f'Table-like line outside code fence at char {i}: "{trimmed}"'

    def test_finish_always_produces_the_full_text(self):
        """Test at various cut points that finish() returns everything."""
        cut_points = [0, 10, 50, 100, 150, len(self.COMPLEX_MARKDOWN)]
        for cut in cut_points:
            if cut > len(self.COMPLEX_MARKDOWN):
                continue
            r = StreamingMarkdownRenderer()
            r.push(self.COMPLEX_MARKDOWN[:cut])
            r.finish()
            finished = r.get_text()
            assert finished == self.COMPLEX_MARKDOWN[:cut]

    def test_appendonly_delta_reconstruction_works_for_characterbycharacter_streaming(self):
        r = StreamingMarkdownRenderer()
        last_appended = ""
        deltas: list[str] = []

        for i in range(len(self.COMPLEX_MARKDOWN)):
            r.push(self.COMPLEX_MARKDOWN[i])
            committable = r.get_committable_text()
            assert committable.startswith(last_appended), f"Delta broke monotonicity at char {i}"
            delta = committable[len(last_appended) :]
            if len(delta) > 0:
                deltas.append(delta)
                last_appended = committable

        r.finish()
        final_committable = r.get_committable_text()
        assert final_committable.startswith(last_appended), "Final flush broke monotonicity"
        final_delta = final_committable[len(last_appended) :]
        if len(final_delta) > 0:
            deltas.append(final_delta)

        # Concatenated deltas must equal the final output
        assert "".join(deltas) == final_committable

        # All original content must be recoverable from get_text()
        assert r.get_text() == self.COMPLEX_MARKDOWN

    # TS: "getCommittableText() is always clean (remend would not add markers)"
    def test_getcommittabletext_is_always_clean_remend_would_not_add_markers(self):
        """get_committable_text() should never contain unclosed markers that remend would fix."""
        r = StreamingMarkdownRenderer()
        for i in range(len(self.COMPLEX_MARKDOWN)):
            r.push(self.COMPLEX_MARKDOWN[i])
            committable = r.get_committable_text()
            if len(committable) == 0:
                continue
            # Skip check if we're inside a code fence (markers are literal there)
            if _is_inside_code_fence(committable):
                continue
            assert len(_remend(committable)) <= len(committable), (
                f"get_committable_text() at position {i} "
                f'("{self.COMPLEX_MARKDOWN[: i + 1][-20:]}") has unclosed markers: '
                f'"{committable[-40:]}"'
            )


# ============================================================================
# Issue #69 regressions: list-marker awareness + table chunk-boundary
# ============================================================================


class TestIssue69Regressions:
    """Pin the three production bugs from issue #69 (comment 4514752058).

    The hand-rolled ``_remend`` previously confused line-leading bullet
    markers with italic openers, and ``_get_committable_prefix`` emitted
    table header+separator without a body row -- both produced visible
    corruption in Slack streaming.
    """

    def test_remend_does_not_treat_line_leading_star_as_italic(self):
        # Single bullet item -- a literal `* item one\n` is unchanged.
        assert _remend("* item one\n") == "* item one\n"

    def test_remend_does_not_close_italic_on_multi_bullet_list(self):
        # Three bullets, odd count: previously appended a stray `*`.
        text = "* item one\n* item two\n* item three\n"
        assert _remend(text) == text

    def test_finish_on_odd_count_bullet_list_does_not_corrupt(self):
        r = StreamingMarkdownRenderer(wrap_tables_for_append=False)
        r.push("* item one\n* item two\n* item three\n")
        assert r.finish() == "* item one\n* item two\n* item three\n"

    def test_remend_still_closes_genuine_italic(self):
        # *hello (no following whitespace) is genuine emphasis, not a list.
        assert _remend("*hello") == "*hello*"

    def test_remend_still_closes_genuine_bold(self):
        assert _remend("**bold") == "**bold**"

    def test_remend_handles_bold_inside_list_item(self):
        # Bullet marker is skipped; bold inside the item is balanced.
        text = "* **important** item\n"
        assert _remend(text) == text

    def test_remend_closes_unclosed_bold_inside_list_item(self):
        # Bullet skipped, then an unclosed `**` should still get closed.
        assert _remend("* **important") == "* **important**"

    def test_remend_handles_indented_bullet(self):
        # Up to leading whitespace before the bullet -- still a list marker.
        assert _remend("  * nested item\n") == "  * nested item\n"

    def test_remend_skips_whitespace_flanked_asterisk_mid_line(self):
        # CommonMark: `*` flanked by whitespace on both sides isn't a valid
        # delimiter. Previously counted as an italic opener.
        assert _remend("use the * operator") == "use the * operator"

    def test_remend_skips_trailing_asterisk_at_end_of_line(self):
        # `*\n` -- next char is whitespace, prev is whitespace -- not a delimiter.
        assert _remend("trailing star *\nmore text") == "trailing star *\nmore text"

    def test_remend_skips_bare_asterisk_at_end_of_buffer(self):
        # `*` at end of stream with whitespace before -- not a delimiter yet.
        assert _remend("partial *") == "partial *"

    def test_table_header_plus_separator_alone_is_held(self):
        # Header+separator without a body row would be emitted as broken
        # markup to append-only consumers. Hold the whole block.
        r = StreamingMarkdownRenderer(wrap_tables_for_append=False)
        r.push("| ID | Status |\n|---|---|\n")
        assert r.get_committable_text() == ""

    def test_table_chunk_boundary_emits_atomic_header_separator_row(self):
        # Reproduces the exact chunk sequence from issue #69 comment.
        r = StreamingMarkdownRenderer(wrap_tables_for_append=False)
        chunks = [
            "Header:\n\n",
            "| ID",
            " | Status |\n",
            "|---|---|\n",
            "| 1 | Open |\n",
            "| 2 | Closed |\n",
        ]
        last = ""
        deltas: list[str] = []
        for c in chunks:
            r.push(c)
            cur = r.get_committable_text()
            deltas.append(cur[len(last) :])
            last = cur

        assert deltas[0] == "Header:\n\n"
        # Header line, separator line, and split-header chunk all held.
        assert deltas[1] == ""
        assert deltas[2] == ""
        assert deltas[3] == ""
        # First body row releases the assembled table atomically.
        assert deltas[4] == "| ID | Status |\n|---|---|\n| 1 | Open |\n"
        # Subsequent rows commit immediately.
        assert deltas[5] == "| 2 | Closed |\n"

    def test_table_with_preceding_text_holds_only_table_block(self):
        # Prose above an unconfirmed table commits; only the table holds.
        r = StreamingMarkdownRenderer(wrap_tables_for_append=False)
        r.push("Intro paragraph.\n\n| H |\n|---|\n")
        assert r.get_committable_text() == "Intro paragraph.\n\n"

    def test_table_held_block_flushes_on_finish_even_without_body_row(self):
        # finish() is the terminal flush -- even an incomplete table is emitted.
        r = StreamingMarkdownRenderer(wrap_tables_for_append=False)
        r.push("| H |\n|---|\n")
        final = r.finish()
        assert "| H |" in final
        assert "|---|" in final

    def test_back_to_back_tables_keep_committable_monotonic(self):
        # PR #99 review #2: when a second table's separator arrives in a
        # stream that already had one confirmed table (and no blank line
        # between them), the backward "hold pre-separator block" walk
        # would roll back into the previously-committed first-table
        # body. Verify get_committable_text() never shrinks.
        r = StreamingMarkdownRenderer(wrap_tables_for_append=False)
        chunks = [
            "|A|B|\n|---|---|\n|1|2|\n",  # full first table
            "|C|D|\n",  # second table's header (committed as body of first)
            "|---|---|\n",  # second table's separator -- must not roll back
        ]
        last = ""
        for chunk in chunks:
            r.push(chunk)
            cur = r.get_committable_text()
            assert cur.startswith(last), (
                f"monotonicity violated: prior committed prefix={last!r} not a prefix of new committed={cur!r}"
            )
            last = cur

    def test_second_table_after_blank_line_still_holds_header(self):
        # The fix above must NOT regress the well-formed multi-table
        # case where tables are separated by a blank line.
        r = StreamingMarkdownRenderer(wrap_tables_for_append=False)
        r.push("| A | B |\n|---|---|\n| 1 | 2 |\n")
        r.push("\n| X | Y |\n")  # blank line then new header
        r.push("|---|---|\n")  # new separator
        # `| X | Y |` is still held -- second table has no body row yet.
        committable = r.get_committable_text()
        assert "| X | Y |" not in committable
        assert "| 1 | 2 |" in committable


# ============================================================================
# Chat / AI agent completeness (issue #69 follow-up)
# ============================================================================


class TestRemendChatCompleteness:
    """``_remend`` parity with upstream remend on chat-content patterns.

    Each gap below comes from the [issue #69 follow-up
    catalog](https://github.com/Chinchill-AI/chat-sdk-python/issues/69#issuecomment-4514898801).
    Word-internal asterisks (``5*3=15``) are tracked separately under
    Option A -- they need a proper CommonMark delimiter-stack algorithm
    to avoid breaking paired-emphasis cases like ``text*foo*``.
    """

    # --- Math regions ($...$, $$...$$) -----------------------------------

    def test_remend_skips_markers_inside_inline_math(self):
        assert _remend("$a^* + b^*$") == "$a^* + b^*$"

    def test_remend_skips_markers_inside_display_math(self):
        text = "$$\\int_0^* e^{-x} dx$$"
        assert _remend(text) == text

    def test_remend_still_closes_emphasis_outside_math(self):
        # Math region is stripped for counting; the trailing italic still
        # gets closed normally.
        assert _remend("$a^2$ and *italic") == "$a^2$ and *italic*"

    def test_remend_skips_strike_marker_inside_math(self):
        assert _remend("$a ~~ b$") == "$a ~~ b$"

    def test_remend_does_not_open_bracket_inside_math(self):
        # Math regions are dropped from the bracket walk too -- a literal
        # `[` inside math shouldn't add a phantom `]` closer.
        assert _remend("note $f[x]$ here") == "note $f[x]$ here"

    # --- Escape-aware tilde / backtick / bracket counters ----------------

    def test_remend_does_not_add_strike_for_escaped_tilde_pair(self):
        assert _remend(r"foo \~~bar") == r"foo \~~bar"

    def test_remend_does_not_add_bracket_for_escaped_open(self):
        assert _remend(r"see \[item") == r"see \[item"

    def test_remend_does_not_add_backtick_for_escaped(self):
        assert _remend(r"foo \` bar") == r"foo \` bar"

    def test_remend_handles_mixed_escaped_and_real_unclosed(self):
        # `\~~` is literal; the trailing real `~~` is unclosed -> close it.
        assert _remend(r"a \~~b and ~~real") == r"a \~~b and ~~real~~"

    def test_remend_does_not_affect_escape_outside_relevant_counters(self):
        # An escaped delimiter that wasn't going to imbalance anything
        # is still left untouched.
        assert _remend(r"foo \* bar") == r"foo \* bar"

    # --- Escape-before-math ordering (PR #101 review #1) ---------------------

    def test_remend_escaped_dollar_does_not_pair_with_unescaped_dollar(self):
        # Without the escape-strip-before-math-strip ordering, the math
        # regex would pair these two `$`s and eat the `*` opener inside,
        # leaving italic unclosed.
        text = r"\$opener *unclosed text closer\$"
        assert _remend(text) == text + "*"

    def test_remend_escaped_dollar_does_not_create_phantom_math_region(self):
        # `\$5` is a literal dollar amount; the `$10` later is not part of
        # any math region (one un-escaped `$` doesn't form `$...$`).
        # The italic at the end still gets closed normally.
        assert _remend(r"\$5 and $10 *italic") == r"\$5 and $10 *italic*"

    def test_remend_unescaped_currency_does_not_pair_as_math(self):
        # PR #101 review #1: text like `prices are $5 and $10` would
        # previously match `$5 and $10` as inline math (because the
        # regex didn't require non-whitespace around the delimiters).
        # The italic at end must still get closed normally.
        assert _remend("prices are $5 and $10 *italic") == "prices are $5 and $10 *italic*"
