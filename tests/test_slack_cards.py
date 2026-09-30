"""Tests for Slack Block Kit card conversion.

Port of packages/adapter-slack/src/cards.test.ts.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest

from chat_sdk.adapters.slack.cards import card_to_block_kit
from chat_sdk.adapters.slack.cards import card_to_fallback_text as slack_card_to_fallback_text
from chat_sdk.cards import (
    Actions,
    Button,
    Card,
    CardLink,
    CardText,
    Chart,
    Divider,
    Field,
    Fields,
    Image,
    LinkButton,
    Section,
    Table,
    card_to_fallback_text,
)
from chat_sdk.modals import RadioSelectElement, SelectElement, SelectOptionElement

# ---------------------------------------------------------------------------
# Helpers to create select/radio elements (TypedDicts, no builder functions)
# ---------------------------------------------------------------------------


def _select(
    *,
    id: str,
    label: str,
    options: list[SelectOptionElement],
    placeholder: str | None = None,
    initial_option: str | None = None,
) -> SelectElement:
    el: SelectElement = {"type": "select", "id": id, "label": label, "options": options}
    if placeholder is not None:
        el["placeholder"] = placeholder
    if initial_option is not None:
        el["initial_option"] = initial_option
    return el


def _radio_select(
    *,
    id: str,
    label: str,
    options: list[SelectOptionElement],
    initial_option: str | None = None,
) -> RadioSelectElement:
    el: RadioSelectElement = {"type": "radio_select", "id": id, "label": label, "options": options}
    if initial_option is not None:
        el["initial_option"] = initial_option
    return el


def _option(*, label: str, value: str, description: str | None = None) -> SelectOptionElement:
    opt: SelectOptionElement = {"label": label, "value": value}
    if description is not None:
        opt["description"] = description
    return opt


# ---------------------------------------------------------------------------
# cardToBlockKit
# ---------------------------------------------------------------------------


class TestCardToBlockKit:
    def test_simple_card_with_title(self):
        blocks = card_to_block_kit(Card(title="Welcome"))
        assert len(blocks) == 1
        assert blocks[0] == {
            "type": "header",
            "text": {"type": "plain_text", "text": "Welcome", "emoji": True},
        }

    def test_card_with_title_and_subtitle(self):
        blocks = card_to_block_kit(Card(title="Order Update", subtitle="Your order is on its way"))
        assert len(blocks) == 2
        assert blocks[0]["type"] == "header"
        assert blocks[1] == {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "Your order is on its way"}],
        }

    def test_card_with_header_image(self):
        blocks = card_to_block_kit(Card(title="Product", image_url="https://example.com/product.png"))
        assert len(blocks) == 2
        assert blocks[1] == {
            "type": "image",
            "image_url": "https://example.com/product.png",
            "alt_text": "Product",
        }

    def test_text_elements(self):
        blocks = card_to_block_kit(
            Card(
                children=[
                    CardText("Regular text"),
                    CardText("Bold text", style="bold"),
                    CardText("Muted text", style="muted"),
                ]
            )
        )
        assert len(blocks) == 3
        assert blocks[0] == {"type": "section", "text": {"type": "mrkdwn", "text": "Regular text"}}
        assert blocks[1] == {"type": "section", "text": {"type": "mrkdwn", "text": "*Bold text*"}}
        assert blocks[2] == {"type": "context", "elements": [{"type": "mrkdwn", "text": "Muted text"}]}

    def test_image_elements(self):
        blocks = card_to_block_kit(
            Card(
                children=[
                    Image(url="https://example.com/img.png", alt="My image"),
                ]
            )
        )
        assert len(blocks) == 1
        assert blocks[0] == {
            "type": "image",
            "image_url": "https://example.com/img.png",
            "alt_text": "My image",
        }

    def test_divider_elements(self):
        blocks = card_to_block_kit(Card(children=[Divider()]))
        assert len(blocks) == 1
        assert blocks[0] == {"type": "divider"}

    def test_actions_with_buttons(self):
        blocks = card_to_block_kit(
            Card(
                children=[
                    Actions(
                        [
                            Button(id="approve", label="Approve", style="primary"),
                            Button(id="reject", label="Reject", style="danger", value="data-123"),
                            Button(id="skip", label="Skip"),
                        ]
                    ),
                ]
            )
        )
        assert len(blocks) == 1
        assert blocks[0]["type"] == "actions"
        elements = blocks[0]["elements"]
        assert len(elements) == 3
        assert elements[0] == {
            "type": "button",
            "text": {"type": "plain_text", "text": "Approve", "emoji": True},
            "action_id": "approve",
            "style": "primary",
        }
        assert elements[1] == {
            "type": "button",
            "text": {"type": "plain_text", "text": "Reject", "emoji": True},
            "action_id": "reject",
            "value": "data-123",
            "style": "danger",
        }
        assert elements[2] == {
            "type": "button",
            "text": {"type": "plain_text", "text": "Skip", "emoji": True},
            "action_id": "skip",
        }

    def test_link_buttons(self):
        blocks = card_to_block_kit(
            Card(
                children=[
                    Actions(
                        [
                            LinkButton(url="https://example.com/docs", label="View Docs", style="primary"),
                        ]
                    ),
                ]
            )
        )
        assert len(blocks) == 1
        assert blocks[0]["type"] == "actions"
        el = blocks[0]["elements"][0]
        assert el["type"] == "button"
        assert el["text"] == {"type": "plain_text", "text": "View Docs", "emoji": True}
        assert el["url"] == "https://example.com/docs"
        assert el["style"] == "primary"
        # No explicit id → action_id falls back to the URL-derived form.
        assert el["action_id"] == "link-https://example.com/docs"

    def test_link_button_uses_custom_action_id(self):
        # chat@4.31 (171657a): an explicit ``id`` is used verbatim as the
        # action_id, so platforms that report link clicks get a stable id.
        blocks = card_to_block_kit(
            Card(
                children=[
                    Actions(
                        [
                            LinkButton(
                                id="agent_slack_auth_signin",
                                url="https://vercel.com/oauth/authorize",
                                label="Sign in",
                            ),
                        ]
                    ),
                ]
            )
        )
        el = blocks[0]["elements"][0]
        assert el["action_id"] == "agent_slack_auth_signin"

    def test_link_button_empty_id_is_used_verbatim(self):
        # ``??`` semantics (NOT ``or``): an explicit empty-string id is used
        # as-is and does NOT fall back to ``link-{url}``. Locks ``is not None``.
        blocks = card_to_block_kit(
            Card(
                children=[
                    Actions(
                        [
                            LinkButton(id="", url="https://example.com/docs", label="View Docs"),
                        ]
                    ),
                ]
            )
        )
        el = blocks[0]["elements"][0]
        assert el["action_id"] == ""

    def test_fields(self):
        blocks = card_to_block_kit(
            Card(
                children=[
                    Fields(
                        [
                            Field(label="Status", value="Active"),
                            Field(label="Priority", value="High"),
                        ]
                    ),
                ]
            )
        )
        assert len(blocks) == 1
        assert blocks[0]["type"] == "section"
        assert blocks[0]["fields"] == [
            {"type": "mrkdwn", "text": "*Status*\nActive"},
            {"type": "mrkdwn", "text": "*Priority*\nHigh"},
        ]

    def test_section_flattens(self):
        blocks = card_to_block_kit(
            Card(
                children=[
                    Section([CardText("Inside section"), Divider()]),
                ]
            )
        )
        assert len(blocks) == 2
        assert blocks[0]["type"] == "section"
        assert blocks[1]["type"] == "divider"

    def test_complete_card(self):
        blocks = card_to_block_kit(
            Card(
                title="Order #1234",
                subtitle="Status update",
                children=[
                    CardText("Your order has been shipped!"),
                    Divider(),
                    Fields(
                        [
                            Field(label="Tracking", value="ABC123"),
                            Field(label="ETA", value="Dec 25"),
                        ]
                    ),
                    Actions([Button(id="track", label="Track Package", style="primary")]),
                ],
            )
        )
        assert len(blocks) == 6
        assert blocks[0]["type"] == "header"
        assert blocks[1]["type"] == "context"
        assert blocks[2]["type"] == "section"
        assert blocks[3]["type"] == "divider"
        assert blocks[4]["type"] == "section"
        assert blocks[5]["type"] == "actions"


# ---------------------------------------------------------------------------
# cardToFallbackText
# ---------------------------------------------------------------------------


class TestCardToFallbackText:
    def test_generates_fallback_text(self):
        card = Card(
            title="Order Update",
            subtitle="Status changed",
            children=[
                CardText("Your order is ready"),
                Fields(
                    [
                        Field(label="Order ID", value="#1234"),
                        Field(label="Status", value="Ready"),
                    ]
                ),
                Actions([Button(id="pickup", label="Schedule Pickup")]),
            ],
        )
        text = card_to_fallback_text(card)
        assert "Order Update" in text
        assert "Status changed" in text
        assert "Your order is ready" in text
        assert "Order ID" in text and "#1234" in text

    def test_card_with_only_title(self):
        card = Card(title="Simple Card")
        text = card_to_fallback_text(card)
        assert "Simple Card" in text


# ---------------------------------------------------------------------------
# Select elements
# ---------------------------------------------------------------------------


class TestSelectElements:
    def test_select_in_actions(self):
        blocks = card_to_block_kit(
            Card(
                children=[
                    Actions(
                        [
                            _select(
                                id="priority",
                                label="Priority",
                                placeholder="Select priority",
                                options=[
                                    _option(label="High", value="high"),
                                    _option(label="Medium", value="medium"),
                                    _option(label="Low", value="low"),
                                ],
                                initial_option="medium",
                            ),
                        ]
                    ),
                ]
            )
        )
        assert len(blocks) == 1
        assert blocks[0]["type"] == "actions"
        el = blocks[0]["elements"][0]
        assert el["type"] == "static_select"
        assert el["action_id"] == "priority"
        assert el["placeholder"] == {"type": "plain_text", "text": "Select priority"}
        assert len(el["options"]) == 3
        assert el["options"][0] == {"text": {"type": "plain_text", "text": "High"}, "value": "high"}
        assert el["initial_option"] == {"text": {"type": "plain_text", "text": "Medium"}, "value": "medium"}

    def test_select_without_placeholder_or_initial(self):
        blocks = card_to_block_kit(
            Card(
                children=[
                    Actions(
                        [
                            _select(
                                id="category",
                                label="Category",
                                options=[
                                    _option(label="Bug", value="bug"),
                                    _option(label="Feature", value="feature"),
                                ],
                            ),
                        ]
                    ),
                ]
            )
        )
        el = blocks[0]["elements"][0]
        assert el["type"] == "static_select"
        assert el.get("placeholder") is None
        assert el.get("initial_option") is None


# ---------------------------------------------------------------------------
# Radio select elements
# ---------------------------------------------------------------------------


class TestRadioSelectElements:
    def test_radio_select(self):
        blocks = card_to_block_kit(
            Card(
                children=[
                    Actions(
                        [
                            _radio_select(
                                id="plan",
                                label="Choose Plan",
                                options=[
                                    _option(label="Basic", value="basic"),
                                    _option(label="Pro", value="pro"),
                                    _option(label="Enterprise", value="enterprise"),
                                ],
                            ),
                        ]
                    ),
                ]
            )
        )
        assert len(blocks) == 1
        assert blocks[0]["type"] == "actions"
        el = blocks[0]["elements"][0]
        assert el["type"] == "radio_buttons"
        assert el["action_id"] == "plan"
        assert len(el["options"]) == 3

    def test_radio_select_uses_mrkdwn(self):
        blocks = card_to_block_kit(
            Card(
                children=[
                    Actions(
                        [
                            _radio_select(
                                id="option",
                                label="Choose",
                                options=[_option(label="Option A", value="a")],
                            ),
                        ]
                    ),
                ]
            )
        )
        el = blocks[0]["elements"][0]
        assert el["options"][0]["text"]["type"] == "mrkdwn"

    def test_radio_select_limits_to_10(self):
        options = [_option(label=f"Option {i + 1}", value=f"opt{i + 1}") for i in range(15)]
        blocks = card_to_block_kit(
            Card(
                children=[
                    Actions([_radio_select(id="many", label="Many Options", options=options)]),
                ]
            )
        )
        el = blocks[0]["elements"][0]
        assert len(el["options"]) == 10


# ---------------------------------------------------------------------------
# Select option descriptions
# ---------------------------------------------------------------------------


class TestSelectOptionDescriptions:
    def test_select_option_with_description(self):
        blocks = card_to_block_kit(
            Card(
                children=[
                    Actions(
                        [
                            _select(
                                id="plan",
                                label="Plan",
                                options=[
                                    _option(label="Basic", value="basic", description="For individuals"),
                                    _option(label="Pro", value="pro", description="For teams"),
                                ],
                            ),
                        ]
                    ),
                ]
            )
        )
        el = blocks[0]["elements"][0]
        assert el["options"][0]["description"] == {"type": "plain_text", "text": "For individuals"}
        assert el["options"][1]["description"] == {"type": "plain_text", "text": "For teams"}

    def test_radio_option_with_description_mrkdwn(self):
        blocks = card_to_block_kit(
            Card(
                children=[
                    Actions(
                        [
                            _radio_select(
                                id="plan",
                                label="Plan",
                                options=[
                                    _option(label="Basic", value="basic", description="For *individuals*"),
                                    _option(label="Pro", value="pro", description="For _teams_"),
                                ],
                            ),
                        ]
                    ),
                ]
            )
        )
        el = blocks[0]["elements"][0]
        assert el["options"][0]["description"] == {"type": "mrkdwn", "text": "For *individuals*"}
        assert el["options"][1]["description"] == {"type": "mrkdwn", "text": "For _teams_"}

    def test_no_description_when_not_provided(self):
        blocks = card_to_block_kit(
            Card(
                children=[
                    Actions(
                        [
                            _select(
                                id="category",
                                label="Category",
                                options=[
                                    _option(label="Bug", value="bug"),
                                    _option(label="Feature", value="feature"),
                                ],
                            ),
                        ]
                    ),
                ]
            )
        )
        el = blocks[0]["elements"][0]
        assert el["options"][0].get("description") is None
        assert el["options"][1].get("description") is None


# ---------------------------------------------------------------------------
# Markdown bold conversion
# ---------------------------------------------------------------------------


class TestMarkdownBoldConversion:
    def test_double_to_single_bold(self):
        blocks = card_to_block_kit(Card(children=[CardText("The **domain** is example.com")]))
        assert blocks[0] == {"type": "section", "text": {"type": "mrkdwn", "text": "The *domain* is example.com"}}

    def test_multiple_bold_segments(self):
        blocks = card_to_block_kit(
            Card(
                children=[
                    CardText("**Project**: my-app, **Status**: active, **Branch**: main"),
                ]
            )
        )
        assert blocks[0]["text"]["text"] == "*Project*: my-app, *Status*: active, *Branch*: main"

    def test_bold_across_multiple_lines(self):
        blocks = card_to_block_kit(
            Card(
                children=[
                    CardText("**Domain**: example.com\n**Project**: my-app\n**Status**: deployed"),
                ]
            )
        )
        assert blocks[0]["text"]["text"] == "*Domain*: example.com\n*Project*: my-app\n*Status*: deployed"

    def test_preserves_single_asterisk(self):
        blocks = card_to_block_kit(Card(children=[CardText("Already *bold* in Slack format")]))
        assert blocks[0]["text"]["text"] == "Already *bold* in Slack format"

    def test_plain_text_no_change(self):
        blocks = card_to_block_kit(Card(children=[CardText("Plain text with no formatting")]))
        assert blocks[0]["text"]["text"] == "Plain text with no formatting"

    def test_bold_in_muted_text(self):
        blocks = card_to_block_kit(Card(children=[CardText("Info about **thing**", style="muted")]))
        assert blocks[0] == {"type": "context", "elements": [{"type": "mrkdwn", "text": "Info about *thing*"}]}

    def test_bold_in_field_values(self):
        blocks = card_to_block_kit(Card(children=[Fields([Field(label="Status", value="**Active**")])]))
        assert "*Active*" in blocks[0]["fields"][0]["text"]
        assert "**Active**" not in blocks[0]["fields"][0]["text"]

    def test_empty_double_asterisks_not_converted(self):
        blocks = card_to_block_kit(Card(children=[CardText("text **** more")]))
        assert blocks[0]["text"]["text"] == "text **** more"

    def test_bold_at_start_and_end(self):
        blocks = card_to_block_kit(Card(children=[CardText("**Start** and **end**")]))
        assert blocks[0]["text"]["text"] == "*Start* and *end*"


# ---------------------------------------------------------------------------
# CardLink
# ---------------------------------------------------------------------------


class TestCardLink:
    def test_converts_to_mrkdwn_link(self):
        blocks = card_to_block_kit(
            Card(
                children=[
                    CardLink(url="https://example.com", label="Click here"),
                ]
            )
        )
        assert len(blocks) == 1
        assert blocks[0] == {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "<https://example.com|Click here>"},
        }

    def test_card_link_alongside_other_children(self):
        blocks = card_to_block_kit(
            Card(
                title="Test",
                children=[
                    CardText("Hello"),
                    CardLink(url="https://example.com", label="Link"),
                ],
            )
        )
        # header + text section + link section
        assert len(blocks) == 3
        assert blocks[2] == {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "<https://example.com|Link>"},
        }


# ---------------------------------------------------------------------------
# Table
# ---------------------------------------------------------------------------


class TestTable:
    def test_converts_a_card_with_table_element_to_a_block_kit_data_table(self):
        blocks = card_to_block_kit(
            Card(
                children=[
                    Table(headers=["Name", "Age"], rows=[["Alice", "30"], ["Bob", "25"]]),
                ]
            )
        )
        assert len(blocks) == 1
        assert blocks[0]["type"] == "data_table"
        assert blocks[0]["caption"] == "Table"
        assert "page_size" not in blocks[0]
        assert blocks[0]["rows"] == [
            [{"type": "raw_text", "text": "Name"}, {"type": "raw_text", "text": "Age"}],
            [{"type": "raw_text", "text": "Alice"}, {"type": "raw_text", "text": "30"}],
            [{"type": "raw_text", "text": "Bob"}, {"type": "raw_text", "text": "25"}],
        ]

    def test_second_table_falls_back_to_ascii(self):
        blocks = card_to_block_kit(
            Card(
                children=[
                    Table(headers=["A"], rows=[["1"]]),
                    Table(headers=["B"], rows=[["2"]]),
                ]
            )
        )
        assert len(blocks) == 2
        assert blocks[0]["type"] == "data_table"
        assert blocks[1]["type"] == "section"
        assert "```" in blocks[1]["text"]["text"]


# ---------------------------------------------------------------------------
# Data tables (upstream describe("cardToBlockKit with data tables"))
# ---------------------------------------------------------------------------


class TestCardToBlockKitWithDataTables:
    def test_passes_caption_and_clamped_page_size_through(self):
        blocks = card_to_block_kit(
            Card(children=[Table(headers=["Name"], rows=[["Ada"]], caption="People", page_size=250)])
        )
        assert blocks[0]["type"] == "data_table"
        assert blocks[0]["caption"] == "People"
        assert blocks[0]["page_size"] == 100

    def test_floors_fractional_page_size_and_defaults_an_empty_caption(self):
        # Upstream ``Math.floor(pageSize)`` and ``caption || "Table"``.
        block = card_to_block_kit(Card(children=[Table(headers=["Name"], rows=[["Ada"]], caption="", page_size=2.7)]))[
            0
        ]
        assert block["caption"] == "Table"
        assert block["page_size"] == 2

    def test_renders_header_only_tables_as_a_plain_table_block(self):
        blocks = card_to_block_kit(Card(children=[Table(headers=["Name", "Age"], rows=[])]))
        assert blocks[0] == {
            "type": "table",
            "rows": [[{"type": "raw_text", "text": "Name"}, {"type": "raw_text", "text": "Age"}]],
        }

    def test_falls_back_to_ascii_when_combined_cells_exceed_10000_characters(self):
        big_cell = "x" * 5001
        blocks = card_to_block_kit(Card(children=[Table(headers=["A"], rows=[[big_cell], [big_cell]])]))
        assert blocks[0]["type"] == "section"
        assert blocks[0]["text"]["text"].startswith("```\nA")

    def test_keeps_a_native_table_at_exactly_10000_characters(self):
        blocks = card_to_block_kit(Card(children=[Table(headers=["A"], rows=[["x" * 4999], ["x" * 5000]])]))
        assert blocks[0]["type"] == "data_table"

    def test_counts_table_characters_in_code_points(self):
        # Divergence from upstream (docs/UPSTREAM_SYNC.md): 1 + 9,999 code points is
        # within the limit here; upstream counts 1 + 19,998 UTF-16 units and falls back.
        blocks = card_to_block_kit(Card(children=[Table(headers=["A"], rows=[["\U0001f600" * 9999]])]))
        assert blocks[0]["type"] == "data_table"

    def test_truncates_ascii_fallback_to_slacks_3000_char_section_limit(self):
        big_cell = "x" * 5001
        text = card_to_block_kit(Card(children=[Table(headers=["A"], rows=[[big_cell], [big_cell]])]))[0]["text"][
            "text"
        ]
        assert len(text) == 3000
        # Closing code fence survives truncation, after the ellipsis marker
        assert text.endswith("…\n```")


# ---------------------------------------------------------------------------
# Charts (upstream describe("cardToBlockKit with charts"))
# ---------------------------------------------------------------------------


def _pie(title: str, value: float = 1) -> Any:
    return Chart(title=title, chart={"type": "pie", "segments": [{"label": "A", "value": value}]})


class TestCardToBlockKitWithCharts:
    def test_converts_a_pie_chart_to_a_data_visualization_block(self):
        blocks = card_to_block_kit(
            Card(
                children=[
                    Chart(
                        title="Candy Bars",
                        chart={
                            "type": "pie",
                            "segments": [{"label": "Kit Kat", "value": 45}, {"label": "Twix", "value": 28}],
                        },
                    )
                ]
            )
        )
        assert blocks == [
            {
                "type": "data_visualization",
                "title": "Candy Bars",
                "chart": {
                    "type": "pie",
                    "segments": [{"label": "Kit Kat", "value": 45}, {"label": "Twix", "value": 28}],
                },
            }
        ]

    def test_converts_a_bar_chart_with_axis_config_and_normalizes_point_order(self):
        blocks = card_to_block_kit(
            Card(
                children=[
                    Chart(
                        title="DAU",
                        chart={
                            "type": "bar",
                            "categories": ["Mon", "Tue"],
                            "x_label": "Day",
                            "y_label": "Users",
                            "series": [
                                {
                                    "name": "Mobile",
                                    "data": [{"label": "Tue", "value": 60}, {"label": "Mon", "value": 50}],
                                }
                            ],
                        },
                    )
                ]
            )
        )
        assert blocks[0] == {
            "type": "data_visualization",
            "title": "DAU",
            "chart": {
                "type": "bar",
                "series": [{"name": "Mobile", "data": [{"label": "Mon", "value": 50}, {"label": "Tue", "value": 60}]}],
                "axis_config": {"categories": ["Mon", "Tue"], "x_label": "Day", "y_label": "Users"},
            },
        }

    def test_omits_axis_labels_that_are_not_provided(self):
        blocks = card_to_block_kit(
            Card(
                children=[
                    Chart(
                        title="Sales",
                        chart={
                            "type": "line",
                            "categories": ["W1"],
                            "series": [{"name": "A", "data": [{"label": "W1", "value": 1}]}],
                        },
                    )
                ]
            )
        )
        assert blocks[0]["chart"] == {
            "type": "line",
            "series": [{"name": "A", "data": [{"label": "W1", "value": 1}]}],
            "axis_config": {"categories": ["W1"]},
        }

    def test_falls_back_to_text_when_a_pie_segment_value_is_not_positive(self):
        blocks = card_to_block_kit(Card(children=[_pie("Bad Pie", value=0)]))
        assert blocks[0]["type"] == "section"
        assert "Bad Pie" in blocks[0]["text"]["text"]
        assert "```" in blocks[0]["text"]["text"]

    def test_falls_back_to_text_when_a_series_misses_a_category(self):
        blocks = card_to_block_kit(
            Card(
                children=[
                    Chart(
                        title="Gaps",
                        chart={
                            "type": "area",
                            "categories": ["Mon", "Tue"],
                            "series": [{"name": "A", "data": [{"label": "Mon", "value": 1}]}],
                        },
                    )
                ]
            )
        )
        assert blocks[0]["type"] == "section"
        assert "```" in blocks[0]["text"]["text"]

    def test_falls_back_when_same_length_series_repeats_a_label_instead_of_covering_a_category(self):
        # Point count matches the categories, but "Tue" is missing: a partial chart is never sent.
        blocks = card_to_block_kit(
            Card(
                children=[
                    Chart(
                        title="Dupes",
                        chart={
                            "type": "bar",
                            "categories": ["Mon", "Tue"],
                            "series": [
                                {"name": "A", "data": [{"label": "Mon", "value": 1}, {"label": "Mon", "value": 2}]}
                            ],
                        },
                    )
                ]
            )
        )
        assert blocks[0]["type"] == "section"

    def test_falls_back_to_text_when_the_title_exceeds_50_characters(self):
        blocks = card_to_block_kit(Card(children=[_pie("t" * 51)]))
        assert blocks[0]["type"] == "section"

    def test_validates_the_title_length_after_emoji_conversion(self):
        # ``{{emoji:x}}`` (11 chars) converts to ``:x:`` (3 chars): 48 + 3 = 51 > 50.
        blocks = card_to_block_kit(Card(children=[_pie("t" * 48 + "{{emoji:x}}")]))
        assert blocks[0]["type"] == "section"
        blocks = card_to_block_kit(Card(children=[_pie("t" * 47 + "{{emoji:x}}")]))
        assert blocks[0] == {
            "type": "data_visualization",
            "title": "t" * 47 + ":x:",
            "chart": {"type": "pie", "segments": [{"label": "A", "value": 1}]},
        }

    def test_falls_back_to_text_when_there_are_more_than_12_segments(self):
        blocks = card_to_block_kit(
            Card(
                children=[
                    Chart(
                        title="Too Many",
                        chart={"type": "pie", "segments": [{"label": f"S{i}", "value": i + 1} for i in range(13)]},
                    )
                ]
            )
        )
        assert blocks[0]["type"] == "section"

    def test_falls_back_to_text_from_the_third_chart_in_one_message(self):
        blocks = card_to_block_kit(Card(children=[_pie("One"), _pie("Two"), _pie("Three")]))
        assert [b["type"] for b in blocks] == ["data_visualization", "data_visualization", "section"]
        assert "Three" in blocks[2]["text"]["text"]

    def test_an_invalid_chart_does_not_use_up_the_per_message_chart_budget(self):
        blocks = card_to_block_kit(Card(children=[_pie("Bad", value=0), _pie("One"), _pie("Two")]))
        assert [b["type"] for b in blocks] == ["section", "data_visualization", "data_visualization"]

    def test_includes_chart_data_in_card_fallback_text(self):
        text = slack_card_to_fallback_text(
            Card(
                title="Report",
                children=[
                    Chart(title="Candy Bars", chart={"type": "pie", "segments": [{"label": "Kit Kat", "value": 45}]})
                ],
            )
        )
        assert "Candy Bars" in text
        assert "Kit Kat" in text


# ---------------------------------------------------------------------------
# Slack constraint edges (each case breaks exactly one constraint)
# ---------------------------------------------------------------------------


def _series_chart(
    *,
    title: str = "Sales",
    categories: list[str] | None = None,
    series: list[dict[str, Any]] | None = None,
    **axis: str,
) -> Any:
    cats = categories if categories is not None else ["Mon", "Tue"]
    default_series = [{"name": "A", "data": [{"label": c, "value": 1} for c in cats]}]
    return Chart(
        title=title,
        chart={"type": "bar", "categories": cats, "series": series if series is not None else default_series, **axis},
    )


def _points(categories: list[str]) -> list[dict[str, Any]]:
    return [{"label": c, "value": 1} for c in categories]


class TestSlackChartAndTableConstraintEdges:
    def test_renders_charts_and_tables_natively_at_every_limit(self):
        cats = [f"c{i:0>19}" for i in range(20)]  # 20 unique 20-char categories
        blocks = card_to_block_kit(
            Card(
                children=[
                    _series_chart(
                        categories=cats,
                        series=[{"name": f"s{i:0>19}", "data": _points(cats)} for i in range(12)],
                        x_label="x" * 50,
                        y_label="y" * 50,
                    ),
                    Table(headers=["N"], rows=[[str(i)] for i in range(100)]),
                ]
            )
        )
        assert [b["type"] for b in blocks] == ["data_visualization", "data_table"]
        assert len(blocks[0]["chart"]["series"]) == 12
        assert len(blocks[1]["rows"]) == 101

    @pytest.mark.parametrize(
        "chart",
        [
            pytest.param(_series_chart(title=""), id="empty-title"),
            pytest.param(_series_chart(categories=[""]), id="empty-category-label"),
            pytest.param(_series_chart(categories=["c" * 21]), id="21-char-category-label"),
            pytest.param(
                _series_chart(series=[{"name": "n" * 21, "data": _points(["Mon", "Tue"])}]), id="21-char-series-name"
            ),
            pytest.param(_series_chart(categories=["Mon", "Mon"]), id="duplicate-category"),
            pytest.param(
                _series_chart(series=[{"name": "A", "data": _points(["Mon", "Tue"])}] * 2), id="duplicate-series-name"
            ),
            pytest.param(
                _series_chart(series=[{"name": f"S{i}", "data": _points(["Mon", "Tue"])} for i in range(13)]),
                id="13-series",
            ),
            pytest.param(_series_chart(categories=[f"C{i}" for i in range(21)]), id="21-categories"),
            pytest.param(_series_chart(x_label="x" * 51), id="51-char-x-label"),
            pytest.param(_series_chart(y_label="y" * 51), id="51-char-y-label"),
            pytest.param(
                _series_chart(categories=["Mon"], series=[{"name": "A", "data": _points(["Mon", "Tue"])}]),
                id="extra-point",
            ),
            pytest.param(
                Chart(title="Pie", chart={"type": "pie", "segments": [{"label": "l" * 21, "value": 1}]}),
                id="21-char-segment-label",
            ),
        ],
    )
    def test_falls_back_to_text_when_a_chart_breaks_one_slack_constraint(self, chart: Any):
        blocks = card_to_block_kit(Card(children=[chart]))
        assert blocks[0]["type"] == "section"
        assert blocks[0]["text"]["text"].startswith("```\n")

    def test_falls_back_to_ascii_for_more_than_100_data_rows(self):
        blocks = card_to_block_kit(Card(children=[Table(headers=["N"], rows=[[str(i)] for i in range(101)])]))
        assert blocks[0]["type"] == "section"

    def test_clamps_page_size_up_to_1_and_converts_emoji_in_the_caption(self):
        block = card_to_block_kit(
            Card(children=[Table(headers=["N"], rows=[["1"]], caption="{{emoji:wave}} Hi", page_size=0)])
        )[0]
        assert block["page_size"] == 1
        assert block["caption"] == ":wave: Hi"

    def test_sends_non_float_numeric_chart_values_as_json_numbers(self):
        # Decimal (Postgres NUMERIC) is not JSON-serializable; slack_sdk would raise TypeError.
        blocks = card_to_block_kit(
            Card(
                children=[
                    Chart(title="Pie", chart={"type": "pie", "segments": [{"label": "EU", "value": Decimal("12.50")}]}),
                    _series_chart(
                        series=[
                            {
                                "name": "A",
                                "data": [
                                    {"label": "Mon", "value": Decimal("3")},
                                    {"label": "Tue", "value": Decimal("4.25")},
                                ],
                            }
                        ]
                    ),
                ]
            )
        )
        assert blocks[0]["chart"]["segments"] == [{"label": "EU", "value": 12.5}]
        assert blocks[1]["chart"]["series"][0]["data"] == [
            {"label": "Mon", "value": 3.0},
            {"label": "Tue", "value": 4.25},
        ]
        assert json.loads(json.dumps(blocks)) == blocks

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), Decimal("NaN"), True, "5"])
    def test_falls_back_to_text_for_values_that_are_not_finite_json_numbers(self, value: Any):
        blocks = card_to_block_kit(
            Card(
                children=[
                    _series_chart(
                        categories=["Mon"], series=[{"name": "A", "data": [{"label": "Mon", "value": value}]}]
                    )
                ]
            )
        )
        assert blocks[0]["type"] == "section"
