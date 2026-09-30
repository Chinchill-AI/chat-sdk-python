"""Tests for chat_sdk.cards module."""

from __future__ import annotations

import json
import math
from decimal import Decimal
from fractions import Fraction

import pytest

from chat_sdk.cards import (
    Button,
    Card,
    CardElement,
    Chart,
    LinkButton,
    Table,
    card_child_to_fallback_text,
    card_to_fallback_text,
    chart_element_to_fallback_text,
    is_card_element,
    table_element_to_ascii,
)
from chat_sdk.shared.card_utils import card_to_fallback_text as shared_card_to_fallback_text
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


class TestLinkButtonId:
    """Regression tests for the optional stable LinkButton ``id`` field.

    Port of upstream stable-id-for-link-buttons (chat@4.31.0, commit 171657a).
    cards.test.ts is byte-identical 4.30->4.31, so upstream ships no test for
    this; these are Python-only regressions that pin our emit/parse behavior.
    Upstream sets ``id: options.id`` unconditionally and lets ``JSON.stringify``
    drop ``undefined`` — Python must only write the key when ``id_`` is given.
    """

    def test_id_written_when_provided(self):
        btn = LinkButton(url="https://example.com/docs", label="Docs", id="open-docs")
        assert btn["id"] == "open-docs"

    def test_no_id_key_when_omitted(self):
        # Emit/parse symmetry guard: an unset id must NOT serialize as a key
        # (no literal None/null), so old persisted cards round-trip unchanged.
        btn = LinkButton(url="https://example.com/docs", label="Docs")
        assert "id" not in btn

    def test_empty_string_id_is_emitted(self):
        # Explicit empty string is distinct from unset and must survive
        # (this is exactly why we use ``is not None`` and not ``id or ...``).
        btn = LinkButton(url="https://example.com/docs", label="Docs", id="")
        assert "id" in btn
        assert btn["id"] == ""

    def test_id_survives_wire_serialization(self):
        btn = LinkButton(url="https://example.com/docs", label="Docs", id="open-docs")
        round_tripped = json.loads(json.dumps(btn))
        assert round_tripped["id"] == "open-docs"
        assert round_tripped["type"] == "link-button"


# ---------------------------------------------------------------------------
# chat@4.34-4.41 card additions (#202): Card width, button tooltips, Table
# options, Chart. Class/test names follow upstream cards.test.ts.
# ---------------------------------------------------------------------------


class TestCard:
    def test_creates_a_card_with_a_width_hint(self):
        assert Card(title="Wide", width="full")["width"] == "full"
        # Unset width is omitted, so existing cards serialize unchanged.
        assert Card(title="Default") == {"type": "card", "children": [], "title": "Default"}


class TestButton:
    def test_creates_a_button_with_a_tooltip(self):
        btn = Button(id="ok", label="OK", tooltip="Confirm the order")
        assert btn["tooltip"] == "Confirm the order"
        assert "tooltip" not in Button(id="ok", label="OK")


class TestLinkButton:
    def test_creates_a_link_button_with_a_tooltip(self):
        btn = LinkButton(url="https://example.com", label="Visit Site", tooltip="Opens example.com")
        assert btn["tooltip"] == "Opens example.com"
        assert "tooltip" not in LinkButton(url="https://example.com", label="Visit Site")


class TestTable:
    def test_creates_a_table_with_caption_and_pagesize(self):
        table = Table(headers=["Name", "Score"], rows=[["Ada", "10"]], caption="Scores", page_size=25)
        assert table["type"] == "table"
        assert table["caption"] == "Scores"
        assert table["page_size"] == 25

    # Python-specific: falsy but set values are kept, not dropped by truthiness.
    def test_keeps_empty_caption_and_zero_page_size(self):
        table = Table(headers=["A"], rows=[["1"]], caption="", page_size=0, widths=[])
        assert table["caption"] == ""
        assert table["page_size"] == 0
        assert table["widths"] == []

    def test_leaves_caption_and_pagesize_undefined_when_omitted(self):
        table = Table(headers=["A"], rows=[["1"]])
        assert "caption" not in table
        assert "page_size" not in table

    def test_carries_the_teamsonly_rendering_options(self):
        table = Table(
            headers=["Name", "Score"],
            rows=[["Ada", "10"]],
            align=["left", "right"],
            widths=[3, 1],
            vertical_align="bottom",
            grid_lines=False,
            grid_style="emphasis",
        )
        assert table == {
            "type": "table",
            "headers": ["Name", "Score"],
            "rows": [["Ada", "10"]],
            "align": ["left", "right"],
            "widths": [3, 1],
            "vertical_align": "bottom",
            # Falsy but set: must be kept, not dropped by a truthiness check.
            "grid_lines": False,
            "grid_style": "emphasis",
        }

    def test_leaves_the_rendering_options_undefined_when_omitted(self):
        assert Table(headers=["A"], rows=[["1"]]) == {"type": "table", "headers": ["A"], "rows": [["1"]]}


_PIE = {
    "type": "pie",
    "segments": [
        {"label": "Kit Kat", "value": 45},
        {"label": "Twix", "value": 28},
    ],
}


class TestChart:
    def test_creates_a_pie_chart(self):
        chart = Chart(title="Candy Bars", chart=_PIE)
        assert chart == {"type": "chart", "title": "Candy Bars", "chart": _PIE}
        assert chart["chart"]["type"] == "pie"

    def test_creates_a_line_chart_with_series_and_categories(self):
        definition = {
            "type": "line",
            "categories": ["Week 1", "Week 2"],
            "x_label": "Week",
            "y_label": "Sales",
            "series": [
                {
                    "name": "Scranton",
                    "data": [
                        {"label": "Week 1", "value": 120},
                        {"label": "Week 2", "value": 135},
                    ],
                },
            ],
        }
        chart = Chart(title="Weekly Sales", chart=definition)
        assert chart["chart"] == {
            "type": "line",
            "categories": ["Week 1", "Week 2"],
            "x_label": "Week",
            "y_label": "Sales",
            "series": [
                {
                    "name": "Scranton",
                    "data": [
                        {"label": "Week 1", "value": 120},
                        {"label": "Week 2", "value": 135},
                    ],
                },
            ],
        }


class TestChartFallbackText:
    def test_renders_pie_chart_data_as_a_labelled_ascii_table(self):
        text = card_child_to_fallback_text(Chart(title="Candy Bars", chart=_PIE))
        assert text == "Candy Bars\nLabel   | Value\n--------|------\nKit Kat | 45\nTwix    | 28"

    def test_renders_series_chart_data_with_one_column_per_series(self):
        text = card_child_to_fallback_text(
            Chart(
                title="DAU",
                chart={
                    "type": "area",
                    "categories": ["Mon", "Tue"],
                    "x_label": "Day",
                    "series": [
                        {"name": "Web", "data": [{"label": "Mon", "value": 100}, {"label": "Tue", "value": 110}]},
                        # Values align to categories even when point order differs
                        {"name": "Mobile", "data": [{"label": "Tue", "value": 60}, {"label": "Mon", "value": 50}]},
                    ],
                },
            )
        )
        assert text == "DAU\nDay | Web | Mobile\n----|-----|-------\nMon | 100 | 50\nTue | 110 | 60"

    # Python-specific below: upstream formats values with JS ``String(v)``.

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (45.0, "45"),
            (1.5, "1.5"),
            (-3.25, "-3.25"),
            (0.1, "0.1"),
            (-0.0, "0"),
            (0.00001, "0.00001"),
            (1e-7, "1e-7"),
            (1e20, "100000000000000000000"),
            (1e21, "1e+21"),
            (1.5e21, "1.5e+21"),
            (10**21, "1e+21"),
            (math.nan, "NaN"),
            (math.inf, "Infinity"),
            (Decimal("45.00"), "45"),
            (Decimal("-0.50"), "-0.5"),
            (Fraction(1, 2), "0.5"),
        ],
    )
    def test_chart_values_render_like_js_string(self, value, expected):
        text = chart_element_to_fallback_text(
            Chart(title="T", chart={"type": "pie", "segments": [{"label": "x", "value": value}]})
        )
        assert text.splitlines()[-1].split(" | ")[1].rstrip() == expected

    def test_missing_series_point_and_x_label_render_as_empty_cells(self):
        text = chart_element_to_fallback_text(
            Chart(
                title="Sales",
                chart={
                    "type": "bar",
                    "categories": ["Q1", "Q2"],
                    "series": [{"name": "East", "data": [{"label": "Q2", "value": 7}]}],
                },
            )
        )
        # ASCII table rows are right-trimmed, so Q1's empty East cell ends the line.
        assert text == "Sales\n   | East\n---|-----\nQ1 |\nQ2 | 7"

    def test_card_fallback_text_includes_the_chart(self):
        card = Card(title="Report", children=[Chart(title="Candy Bars", chart=_PIE)])
        body = "Candy Bars\nLabel   | Value\n--------|------\nKit Kat | 45\nTwix    | 28"
        assert card_to_fallback_text(card) == f"**Report**\n{body}"
        # The adapter-shared fallback (Slack/Teams/GChat/Discord text) renders it too.
        assert shared_card_to_fallback_text(card) == f"*Report*\n{body}"
