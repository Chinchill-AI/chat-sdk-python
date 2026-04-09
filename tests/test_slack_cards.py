"""Tests for Slack Block Kit card conversion.

Port of packages/adapter-slack/src/cards.test.ts.
"""

from __future__ import annotations

from chat_sdk.adapters.slack.cards import card_to_block_kit
from chat_sdk.cards import (
    Actions,
    Button,
    Card,
    CardLink,
    CardText,
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
    def test_table_to_block_kit(self):
        blocks = card_to_block_kit(
            Card(
                children=[
                    Table(headers=["Name", "Age"], rows=[["Alice", "30"], ["Bob", "25"]]),
                ]
            )
        )
        assert len(blocks) == 1
        assert blocks[0]["type"] == "table"
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
        assert blocks[0]["type"] == "table"
        assert blocks[1]["type"] == "section"
        assert "```" in blocks[1]["text"]["text"]
