"""Tests for chat_sdk.cards module."""

from __future__ import annotations

from chat_sdk.cards import (
    CardElement,
    card_child_to_fallback_text,
    is_card_element,
    table_element_to_ascii,
)
from chat_sdk.shared.card_utils import escape_table_cell, render_gfm_table


class TestIsCardElement:
    """Tests for is_card_element."""

    def test_valid_card(self):
        card: CardElement = {"type": "card", "title": "Test"}
        assert is_card_element(card) is True

    def test_card_with_children(self):
        card: CardElement = {
            "type": "card",
            "title": "With children",
            "children": [{"type": "text", "content": "Hello"}],
        }
        assert is_card_element(card) is True

    def test_not_a_dict(self):
        assert is_card_element("card") is False
        assert is_card_element(42) is False
        assert is_card_element(None) is False
        assert is_card_element([]) is False

    def test_dict_wrong_type(self):
        assert is_card_element({"type": "text"}) is False
        assert is_card_element({"type": "button"}) is False

    def test_dict_no_type(self):
        assert is_card_element({"title": "No type"}) is False

    def test_empty_dict(self):
        assert is_card_element({}) is False


class TestTableElementToAscii:
    """Tests for table_element_to_ascii."""

    def test_basic_table(self):
        result = table_element_to_ascii(
            ["Name", "Age"],
            [["Alice", "30"], ["Bob", "25"]],
        )
        lines = result.split("\n")
        assert len(lines) == 4  # header, separator, 2 data rows
        assert "Name" in lines[0]
        assert "Age" in lines[0]
        assert "---" in lines[1] or "- -" in lines[1]
        assert "Alice" in lines[2]
        assert "Bob" in lines[3]

    def test_empty_headers(self):
        result = table_element_to_ascii([], [["a", "b"]])
        assert result == ""

    def test_empty_rows(self):
        result = table_element_to_ascii(["Col1", "Col2"], [])
        lines = result.split("\n")
        assert len(lines) == 2  # header + separator only

    def test_column_width_expansion(self):
        result = table_element_to_ascii(
            ["X", "Y"],
            [["LongValue", "Short"]],
        )
        lines = result.split("\n")
        # The header row should be padded to accommodate "LongValue"
        assert "LongValue" in lines[2]

    def test_missing_cells_in_row(self):
        result = table_element_to_ascii(
            ["A", "B", "C"],
            [["only_one"]],
        )
        lines = result.split("\n")
        assert len(lines) == 3
        assert "only_one" in lines[2]

    def test_single_column(self):
        result = table_element_to_ascii(["Status"], [["OK"], ["FAIL"]])
        lines = result.split("\n")
        assert len(lines) == 4
        assert "OK" in lines[2]
        assert "FAIL" in lines[3]


class TestCardChildToFallbackText:
    """Tests for card_child_to_fallback_text."""

    def test_text_element(self):
        child = {"type": "text", "content": "Hello, world!"}
        assert card_child_to_fallback_text(child) == "Hello, world!"

    def test_link_element(self):
        child = {"type": "link", "label": "Click here", "url": "https://example.com"}
        assert card_child_to_fallback_text(child) == "Click here (https://example.com)"

    def test_divider_element(self):
        child = {"type": "divider"}
        assert card_child_to_fallback_text(child) is None

    def test_fields_element(self):
        child = {
            "type": "fields",
            "children": [
                {"type": "field", "label": "Name", "value": "Alice"},
                {"type": "field", "label": "Role", "value": "Engineer"},
            ],
        }
        result = card_child_to_fallback_text(child)
        assert "Name: Alice" in result
        assert "Role: Engineer" in result

    def test_table_element(self):
        child = {
            "type": "table",
            "headers": ["Col1", "Col2"],
            "rows": [["a", "b"]],
        }
        result = card_child_to_fallback_text(child)
        assert result is not None
        assert "Col1" in result
        assert "a" in result

    def test_section_element(self):
        child = {
            "type": "section",
            "children": [
                {"type": "text", "content": "First"},
                {"type": "text", "content": "Second"},
            ],
        }
        result = card_child_to_fallback_text(child)
        assert result is not None
        assert "First" in result
        assert "Second" in result

    def test_image_element_with_alt(self):
        child = {"type": "image", "url": "https://example.com/img.png", "alt": "Logo"}
        assert card_child_to_fallback_text(child) is None

    def test_image_element_without_alt(self):
        child = {"type": "image", "url": "https://example.com/img.png", "alt": ""}
        assert card_child_to_fallback_text(child) is None

    def test_unknown_element(self):
        child = {"type": "custom_widget"}
        assert card_child_to_fallback_text(child) is None

    def test_button_element_returns_none(self):
        child = {"type": "button", "label": "Click me"}
        assert card_child_to_fallback_text(child) is None


class TestEscapeTableCell:
    """Tests for shared.card_utils.escape_table_cell."""

    def test_plain_text_passthrough(self):
        assert escape_table_cell("hello world") == "hello world"

    def test_pipe_escaped(self):
        assert escape_table_cell("a|b") == r"a\|b"

    def test_backslash_doubled_before_pipe_escape(self):
        # Backslash must be doubled FIRST so that a literal `\|` in input
        # doesn't collide with the subsequent pipe-escape.
        assert escape_table_cell(r"a\b") == r"a\\b"
        assert escape_table_cell(r"a\|b") == r"a\\\|b"

    def test_newline_collapsed_to_space(self):
        assert escape_table_cell("line1\nline2") == "line1 line2"

    def test_multiple_substitutions(self):
        assert escape_table_cell("a|b\nc\\d") == r"a\|b c\\d"

    def test_empty_string(self):
        assert escape_table_cell("") == ""


class TestRenderGfmTable:
    """Tests for shared.card_utils.render_gfm_table."""

    def test_basic_table(self):
        lines = render_gfm_table(["h1", "h2"], [["a", "b"], ["c", "d"]])
        assert lines == [
            "| h1 | h2 |",
            "| --- | --- |",
            "| a | b |",
            "| c | d |",
        ]

    def test_cells_are_escaped(self):
        lines = render_gfm_table(["col"], [["pipe|inside"], ["has\nnewline"]])
        assert r"pipe\|inside" in lines[2]
        assert "has newline" in lines[3]

    def test_empty_rows(self):
        # No data rows — only header + separator.
        lines = render_gfm_table(["only"], [])
        assert lines == ["| only |", "| --- |"]
