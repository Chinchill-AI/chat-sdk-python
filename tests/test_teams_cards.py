"""Tests for Teams Adaptive Card conversion and fallback text.

Ported from packages/adapter-teams/src/cards.test.ts.
"""

from __future__ import annotations

from typing import Any

import pytest

from chat_sdk.adapters.teams.cards import AUTO_SUBMIT_ACTION_ID, card_to_adaptive_card, card_to_fallback_text
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
)
from chat_sdk.modals import RadioSelect, Select, SelectOption

# ---------------------------------------------------------------------------
# cardToAdaptiveCard
# ---------------------------------------------------------------------------


class TestCardToAdaptiveCard:
    def test_valid_adaptive_card_structure(self):
        card = Card(title="Test")
        adaptive = card_to_adaptive_card(card)
        assert adaptive["type"] == "AdaptiveCard"
        assert adaptive["$schema"] == "http://adaptivecards.io/schemas/adaptive-card.json"
        assert adaptive["version"] == "1.5"
        assert isinstance(adaptive["body"], list)

    def test_card_with_title(self):
        card = Card(title="Welcome Message")
        adaptive = card_to_adaptive_card(card)
        assert len(adaptive["body"]) == 1
        assert adaptive["body"][0] == {
            "type": "TextBlock",
            "text": "Welcome Message",
            "weight": "bolder",
            "size": "large",
            "wrap": True,
        }

    def test_card_with_title_and_subtitle(self):
        card = Card(title="Order Update", subtitle="Your package is on its way")
        adaptive = card_to_adaptive_card(card)
        assert len(adaptive["body"]) == 2
        assert adaptive["body"][1] == {
            "type": "TextBlock",
            "text": "Your package is on its way",
            "isSubtle": True,
            "wrap": True,
        }

    def test_card_with_header_image(self):
        card = Card(title="Product", image_url="https://example.com/product.png")
        adaptive = card_to_adaptive_card(card)
        assert len(adaptive["body"]) == 2
        assert adaptive["body"][1] == {
            "type": "Image",
            "url": "https://example.com/product.png",
            "size": "stretch",
        }

    def test_text_elements(self):
        card = Card(
            children=[
                CardText("Regular text"),
                CardText("Bold text", style="bold"),
                CardText("Muted text", style="muted"),
            ]
        )
        adaptive = card_to_adaptive_card(card)
        assert len(adaptive["body"]) == 3
        assert adaptive["body"][0] == {"type": "TextBlock", "text": "Regular text", "wrap": True}
        assert adaptive["body"][1] == {"type": "TextBlock", "text": "Bold text", "wrap": True, "weight": "bolder"}
        assert adaptive["body"][2] == {"type": "TextBlock", "text": "Muted text", "wrap": True, "isSubtle": True}

    def test_image_elements(self):
        card = Card(children=[Image(url="https://example.com/img.png", alt="My image")])
        adaptive = card_to_adaptive_card(card)
        assert len(adaptive["body"]) == 1
        assert adaptive["body"][0] == {
            "type": "Image",
            "url": "https://example.com/img.png",
            "altText": "My image",
            "size": "auto",
        }

    def test_divider_hoists_separator_onto_next_sibling(self):
        """Regression test for issue #45: a divider between siblings should set
        ``separator: True`` on the following element rather than emitting an
        empty Container (which Teams renders at zero height).
        """
        card = Card(
            children=[
                CardText("above"),
                Divider(),
                CardText("below"),
            ]
        )
        adaptive = card_to_adaptive_card(card)
        assert len(adaptive["body"]) == 2
        assert adaptive["body"][0] == {"type": "TextBlock", "text": "above", "wrap": True}
        assert adaptive["body"][1] == {
            "type": "TextBlock",
            "text": "below",
            "wrap": True,
            "separator": True,
        }

    def test_divider_leading_hoists_onto_first_sibling(self):
        card = Card(children=[Divider(), CardText("only")])
        adaptive = card_to_adaptive_card(card)
        assert len(adaptive["body"]) == 1
        assert adaptive["body"][0] == {
            "type": "TextBlock",
            "text": "only",
            "wrap": True,
            "separator": True,
        }

    def test_divider_trailing_falls_back_to_non_empty_container(self):
        """A divider with no following sibling must still be visible — an
        empty Container with ``separator: True`` renders at zero height, so
        emit a minimal non-empty Container instead.
        """
        card = Card(children=[CardText("above"), Divider()])
        adaptive = card_to_adaptive_card(card)
        assert len(adaptive["body"]) == 2
        assert adaptive["body"][0] == {"type": "TextBlock", "text": "above", "wrap": True}
        trailing = adaptive["body"][1]
        assert trailing["type"] == "Container"
        assert trailing["separator"] is True
        assert trailing["items"], "trailing-divider Container must not be empty"

    def test_divider_never_leaks_internal_marker_key(self):
        """The internal marker key used during conversion must never appear
        in the final Adaptive Card payload sent to Teams.
        """
        card = Card(
            children=[
                CardText("a"),
                Divider(),
                Divider(),
                CardText("b"),
                Divider(),
            ]
        )
        adaptive = card_to_adaptive_card(card)

        def _walk(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    assert not key.startswith("__chatSdk"), f"leaked marker key: {key}"
                    _walk(value)
            elif isinstance(node, list):
                for item in node:
                    _walk(item)

        _walk(adaptive)

    def test_actions_with_buttons(self):
        card = Card(
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
        adaptive = card_to_adaptive_card(card)
        assert len(adaptive["body"]) == 0
        assert len(adaptive["actions"]) == 3

        assert adaptive["actions"][0] == {
            "type": "Action.Submit",
            "title": "Approve",
            "data": {"actionId": "approve", "value": None},
            "style": "positive",
        }
        assert adaptive["actions"][1] == {
            "type": "Action.Submit",
            "title": "Reject",
            "data": {"actionId": "reject", "value": "data-123"},
            "style": "destructive",
        }
        assert adaptive["actions"][2] == {
            "type": "Action.Submit",
            "title": "Skip",
            "data": {"actionId": "skip", "value": None},
        }

    def test_link_buttons(self):
        card = Card(
            children=[
                Actions([LinkButton(url="https://example.com/docs", label="View Docs", style="primary")]),
            ]
        )
        adaptive = card_to_adaptive_card(card)
        assert len(adaptive["actions"]) == 1
        assert adaptive["actions"][0] == {
            "type": "Action.OpenUrl",
            "title": "View Docs",
            "url": "https://example.com/docs",
            "style": "positive",
        }

    def test_fields_to_factset(self):
        card = Card(
            children=[
                Fields(
                    [
                        Field(label="Status", value="Active"),
                        Field(label="Priority", value="High"),
                    ]
                ),
            ]
        )
        adaptive = card_to_adaptive_card(card)
        assert len(adaptive["body"]) == 1
        assert adaptive["body"][0] == {
            "type": "FactSet",
            "facts": [
                {"title": "Status", "value": "Active"},
                {"title": "Priority", "value": "High"},
            ],
        }

    def test_section_wrapped_in_container(self):
        card = Card(children=[Section([CardText("Inside section")])])
        adaptive = card_to_adaptive_card(card)
        assert len(adaptive["body"]) == 1
        assert adaptive["body"][0]["type"] == "Container"
        assert len(adaptive["body"][0]["items"]) == 1

    def test_complete_card(self):
        card = Card(
            title="Order #1234",
            subtitle="Status update",
            children=[
                CardText("Your order has been shipped!"),
                Fields(
                    [
                        Field(label="Tracking", value="ABC123"),
                        Field(label="ETA", value="Dec 25"),
                    ]
                ),
                Actions([Button(id="track", label="Track Package", style="primary")]),
            ],
        )
        adaptive = card_to_adaptive_card(card)
        assert len(adaptive["body"]) == 4
        assert adaptive["body"][0]["type"] == "TextBlock"  # title
        assert adaptive["body"][1]["type"] == "TextBlock"  # subtitle
        assert adaptive["body"][2]["type"] == "TextBlock"  # text
        assert adaptive["body"][3]["type"] == "FactSet"  # fields
        assert len(adaptive["actions"]) == 1
        assert adaptive["actions"][0]["title"] == "Track Package"

    def test_card_link(self):
        card = Card(children=[CardLink(url="https://example.com", label="Click here")])
        adaptive = card_to_adaptive_card(card)
        assert len(adaptive["body"]) == 1
        assert adaptive["body"][0] == {
            "type": "TextBlock",
            "text": "[Click here](https://example.com)",
            "wrap": True,
        }


# ---------------------------------------------------------------------------
# Select and RadioSelect in Actions
# ---------------------------------------------------------------------------


class TestCardToAdaptiveCardSelectAndRadioSelect:
    """Ported from cards.test.ts: cardToAdaptiveCard with select and radio_select in Actions."""

    def test_converts_select_to_compact_choice_set_input(self):
        card = Card(
            children=[
                Actions(
                    [
                        Select(
                            id="color",
                            label="Pick a color",
                            options=[
                                SelectOption(label="Red", value="red"),
                                SelectOption(label="Blue", value="blue"),
                            ],
                            placeholder="Choose...",
                        ),
                    ]
                ),
            ],
        )
        adaptive = card_to_adaptive_card(card)

        assert len(adaptive["body"]) == 1
        choice_set = adaptive["body"][0]
        assert choice_set["type"] == "Input.ChoiceSet"
        assert choice_set["id"] == "color"
        assert choice_set["label"] == "Pick a color"
        assert choice_set["style"] == "compact"
        assert choice_set["isRequired"] is True
        assert choice_set["placeholder"] == "Choose..."

        assert len(choice_set["choices"]) == 2
        assert choice_set["choices"][0] == {"title": "Red", "value": "red"}
        assert choice_set["choices"][1] == {"title": "Blue", "value": "blue"}

        # Auto-injects submit button since there are no explicit buttons
        assert len(adaptive["actions"]) == 1
        assert adaptive["actions"][0] == {
            "type": "Action.Submit",
            "title": "Submit",
            "data": {"actionId": AUTO_SUBMIT_ACTION_ID},
        }

    def test_converts_radio_select_to_expanded_choice_set_input(self):
        card = Card(
            children=[
                Actions(
                    [
                        RadioSelect(
                            id="plan",
                            label="Choose Plan",
                            options=[
                                SelectOption(label="Free", value="free"),
                                SelectOption(label="Pro", value="pro"),
                            ],
                        ),
                    ]
                ),
            ],
        )
        adaptive = card_to_adaptive_card(card)

        assert len(adaptive["body"]) == 1
        choice_set = adaptive["body"][0]
        assert choice_set["type"] == "Input.ChoiceSet"
        assert choice_set["id"] == "plan"
        assert choice_set["label"] == "Choose Plan"
        assert choice_set["style"] == "expanded"
        assert choice_set["isRequired"] is True

        # Auto-injects submit button
        assert len(adaptive["actions"]) == 1
        assert adaptive["actions"][0] == {
            "type": "Action.Submit",
            "title": "Submit",
            "data": {"actionId": AUTO_SUBMIT_ACTION_ID},
        }

    def test_does_not_auto_inject_submit_when_buttons_present(self):
        card = Card(
            children=[
                Actions(
                    [
                        Select(
                            id="color",
                            label="Color",
                            options=[SelectOption(label="Red", value="red")],
                        ),
                        Button(id="submit", label="Submit", style="primary"),
                    ]
                ),
            ],
        )
        adaptive = card_to_adaptive_card(card)

        # Select goes to body, button goes to actions
        assert len(adaptive["body"]) == 1
        assert adaptive["body"][0]["type"] == "Input.ChoiceSet"
        assert adaptive["body"][0]["id"] == "color"

        assert len(adaptive["actions"]) == 1
        assert adaptive["actions"][0]["type"] == "Action.Submit"
        assert adaptive["actions"][0]["title"] == "Submit"


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
                Actions(
                    [
                        Button(id="pickup", label="Schedule Pickup"),
                        Button(id="delay", label="Delay"),
                    ]
                ),
            ],
        )
        text = card_to_fallback_text(card)
        assert "**Order Update**" in text
        assert "Status changed" in text
        assert "Your order is ready" in text
        assert "Order ID" in text
        assert "#1234" in text
        assert "Status" in text
        assert "Ready" in text

    def test_card_with_only_title(self):
        card = Card(title="Simple Card")
        text = card_to_fallback_text(card)
        assert text == "**Simple Card**"


class TestCardToAdaptiveCardWithTeamsSpecificHints:
    """Port of upstream ``describe("cardToAdaptiveCard with Teams-specific hints")`` (chat@4.40, #895)."""

    def test_sets_msteams_width_when_the_card_asks_for_full_width(self):
        adaptive = card_to_adaptive_card(Card(title="Wide", width="full"))
        assert adaptive["msteams"] == {"width": "full"}

    def test_leaves_msteams_unset_by_default(self):
        assert "msteams" not in card_to_adaptive_card(Card(title="Default"))
        assert "msteams" not in card_to_adaptive_card(Card(title="Explicit default", width="default"))

    def test_forwards_button_tooltips_to_the_actions(self):
        card = Card(
            children=[
                Actions(
                    [
                        Button(id="approve", label="Approve", tooltip="Approve the request"),
                        LinkButton(url="https://example.com/docs", label="View Docs", tooltip="Opens the docs"),
                    ]
                )
            ]
        )
        actions = card_to_adaptive_card(card)["actions"]
        assert actions[0]["type"] == "Action.Submit"
        assert actions[0]["tooltip"] == "Approve the request"
        assert actions[1]["type"] == "Action.OpenUrl"
        assert actions[1]["tooltip"] == "Opens the docs"

    def test_leaves_tooltip_unset_when_none_is_given(self):
        card = Card(children=[Actions([Button(id="ok", label="OK"), LinkButton(url="https://e.com", label="L")])])
        actions = card_to_adaptive_card(card)["actions"]
        assert "tooltip" not in actions[0]
        assert "tooltip" not in actions[1]


def _render_table(**options: Any) -> dict[str, Any]:
    return card_to_adaptive_card(Card(children=[Table(**options)]))["body"][0]


def _cell_texts(table: dict[str, Any], row_index: int) -> list[str]:
    return [cell["items"][0]["text"] for cell in table["rows"][row_index]["cells"]]


class TestCardToAdaptiveCardWithTable:
    """Port of upstream ``describe("cardToAdaptiveCard with Table")`` (chat@4.41, #906).

    Upstream's ``it.each`` "emits the same Table as the cards subpath for
    $name" is not ported: upstream has two independent converters (the SDK
    one and the plain-object ``cards-primitives`` one), while Python's
    ``teams/cards.py`` is the single converter behind both surfaces.
    """

    def test_renders_a_native_table_with_grid_lines_and_a_bold_header_row_by_default(self):
        table = _render_table(headers=["Name", "Score"], rows=[["Alice", "98"], ["Bob", "87"]])

        assert table["type"] == "Table"
        assert table["showGridLines"] is True
        assert table["firstRowAsHeaders"] is True
        assert "gridStyle" not in table
        assert "horizontalCellContentAlignment" not in table
        assert "verticalCellContentAlignment" not in table
        assert len(table["columns"]) == 2
        assert len(table["rows"]) == 3
        assert table["rows"][0] == {
            "type": "TableRow",
            "cells": [
                {
                    "type": "TableCell",
                    "items": [{"type": "TextBlock", "text": "Name", "weight": "Bolder", "wrap": True}],
                },
                {
                    "type": "TableCell",
                    "items": [{"type": "TextBlock", "text": "Score", "weight": "Bolder", "wrap": True}],
                },
            ],
        }
        assert table["rows"][1] == {
            "type": "TableRow",
            "cells": [
                {"type": "TableCell", "items": [{"type": "TextBlock", "text": "Alice", "wrap": True}]},
                {"type": "TableCell", "items": [{"type": "TextBlock", "text": "98", "wrap": True}]},
            ],
        }

    def test_weights_every_column_1_unless_widths_says_otherwise(self):
        assert _render_table(headers=["A", "B"], rows=[])["columns"] == [{"width": 1}, {"width": 1}]
        assert _render_table(headers=["A", "B", "C"], rows=[], widths=[3, 1])["columns"] == [
            {"width": 3},
            {"width": 1},
            {"width": 1},
        ]

    def test_falls_back_to_weight_1_for_a_width_that_is_not_a_positive_integer(self):
        table = _render_table(headers=["A", "B", "C", "D", "E"], rows=[], widths=[0, -1, 1.5, float("nan"), 2])
        assert table["columns"] == [{"width": 1}, {"width": 1}, {"width": 1}, {"width": 1}, {"width": 2}]

    def test_python_width_weights_reject_bool_and_accept_integral_floats(self):
        # Python-specific: ``bool`` is an ``int`` subclass but not a number
        # upstream, and JS ``Number.isInteger(2.0)`` is true — emitted as the
        # int ``2`` so the wire JSON reads ``2`` as it does upstream.
        table = _render_table(headers=["A", "B", "C", "D"], rows=[], widths=[True, 2.0, float("inf"), "3"])
        assert table["columns"] == [{"width": 1}, {"width": 2}, {"width": 1}, {"width": 1}]
        assert type(table["columns"][1]["width"]) is int

    def test_maps_per_column_align_onto_the_column_definitions(self):
        table = _render_table(headers=["A", "B", "C"], rows=[["1", "2", "3"]], align=["left", "center", "right"])
        assert [column.get("horizontalCellContentAlignment") for column in table["columns"]] == [
            "Left",
            "Center",
            "Right",
        ]
        assert "horizontalCellContentAlignment" not in table

    @pytest.mark.parametrize(
        ("vertical_align", "expected"), [("top", "Top"), ("center", "Center"), ("bottom", "Bottom")]
    )
    def test_maps_vertical_align_to_vertical_cell_content_alignment(self, vertical_align: str, expected: str):
        """it.each("maps verticalAlign %s to %s")."""
        table = _render_table(headers=["A"], rows=[], vertical_align=vertical_align)
        assert table["verticalCellContentAlignment"] == expected

    def test_omits_the_header_row_and_the_header_flag_for_a_headerless_table(self):
        table = _render_table(headers=[], rows=[["Alice", "98"]])
        assert table["firstRowAsHeaders"] is False
        assert len(table["columns"]) == 2
        assert len(table["rows"]) == 1
        assert _cell_texts(table, 0) == ["Alice", "98"]
        assert "weight" not in table["rows"][0]["cells"][0]["items"][0]

    def test_emits_no_element_for_a_table_with_no_columns(self):
        def render(rows: list[list[str]]) -> list[dict[str, Any]]:
            return card_to_adaptive_card(Card(children=[Table(headers=[], rows=rows)]))["body"]

        assert render([]) == []
        assert render([[]]) == []

    def test_turns_grid_lines_off_on_request(self):
        # An explicit ``False`` must win over the ``True`` default.
        assert _render_table(headers=["A"], rows=[], grid_lines=False)["showGridLines"] is False

    def test_passes_grid_style_through(self):
        assert _render_table(headers=["A"], rows=[], grid_style="emphasis")["gridStyle"] == "emphasis"

    def test_pads_a_ragged_row_with_empty_cells(self):
        table = _render_table(headers=["A", "B", "C"], rows=[["1"], ["1", "2", "3", "4"]])
        assert len(table["columns"]) == 4
        assert _cell_texts(table, 0) == ["A", "B", "C", ""]
        assert _cell_texts(table, 1) == ["1", "", "", ""]
        assert _cell_texts(table, 2) == ["1", "2", "3", "4"]

    def test_converts_emoji_placeholders_in_headers_and_cells(self):
        table = _render_table(headers=["Status {{emoji:check}}"], rows=[["Done {{emoji:check}}"]])
        assert _cell_texts(table, 0) == ["Status ✅"]
        assert _cell_texts(table, 1) == ["Done ✅"]

    def test_keeps_the_ascii_fallback_text(self):
        text = card_to_fallback_text(
            Card(
                children=[
                    Table(headers=["Name", "Score"], rows=[["Alice", "98"]], widths=[3, 1], grid_style="emphasis"),
                ]
            )
        )
        assert "Name  | Score\n------|------\nAlice | 98" in text


class TestTeamsCardPrimitivesTables:
    """Ports of upstream ``cards-primitives/index.test.ts`` table and hint cases.

    Python's ``teams/cards.py`` serves both the adapter and the SDK-free
    cards-primitives surface, so primitive cases that duplicate a
    ``cards.test.ts`` port above are skipped: "omits the header row of a
    headerless table and honours gridLines", "falls back to weight 1 for a
    width that is not a positive integer" and "emits no element for a table
    with no columns".

    Upstream's primitive converter resolves Slack-style ``:white_check_mark:``
    shortcodes; the shared Python converter resolves the SDK's
    ``{{emoji:check}}`` placeholders (pre-existing), so these ports use the
    placeholder form.
    """

    def test_renders_tables_as_the_adaptive_card_table_element(self):
        def cell(text: str, **options: Any) -> dict[str, Any]:
            return {"items": [{"text": text, "type": "TextBlock", "wrap": True, **options}], "type": "TableCell"}

        card = card_to_adaptive_card(
            {
                "type": "card",
                "children": [
                    {
                        "type": "table",
                        "align": ["left", "right"],
                        "grid_style": "emphasis",
                        "headers": ["Name", "Score"],
                        "rows": [["Ada {{emoji:check}}", "10"], ["Bob"]],
                        "vertical_align": "top",
                        "widths": [3, 1],
                    }
                ],
            }
        )

        assert card["body"] == [
            {
                "columns": [
                    {"horizontalCellContentAlignment": "Left", "width": 3},
                    {"horizontalCellContentAlignment": "Right", "width": 1},
                ],
                "firstRowAsHeaders": True,
                "gridStyle": "emphasis",
                "rows": [
                    {"cells": [cell("Name", weight="Bolder"), cell("Score", weight="Bolder")], "type": "TableRow"},
                    {"cells": [cell("Ada ✅"), cell("10")], "type": "TableRow"},
                    {"cells": [cell("Bob"), cell("")], "type": "TableRow"},
                ],
                "showGridLines": True,
                "type": "Table",
                "verticalCellContentAlignment": "Top",
            }
        ]

    def test_forwards_the_width_hint_and_button_tooltips(self):
        card = card_to_adaptive_card(
            {
                "type": "card",
                "width": "full",
                "children": [
                    {
                        "type": "actions",
                        "children": [
                            {
                                "type": "button",
                                "id": "approve",
                                "label": "Approve",
                                "tooltip": "Approve the request {{emoji:check}}",
                            },
                            {
                                "type": "link-button",
                                "label": "Docs",
                                "tooltip": "Opens the docs",
                                "url": "https://example.com",
                            },
                        ],
                    }
                ],
            }
        )

        assert card["msteams"] == {"width": "full"}
        assert [action["tooltip"] for action in card["actions"]] == ["Approve the request ✅", "Opens the docs"]


class TestTeamsCardInputEndToEnd:
    """End-to-end: render card with Select -> submit Action.Submit -> verify process_action values."""

    async def test_select_submit_round_trip(self):
        """Card with Select renders ChoiceSet; submitted values reach process_action."""
        from unittest.mock import MagicMock

        from chat_sdk.adapters.teams.adapter import TeamsAdapter
        from chat_sdk.adapters.teams.cards import AUTO_SUBMIT_ACTION_ID
        from chat_sdk.adapters.teams.types import TeamsAdapterConfig

        adapter = TeamsAdapter(
            TeamsAdapterConfig(
                app_id="test-app",
                app_password="test-pass",
            )
        )
        mock_chat = MagicMock()
        adapter._chat = mock_chat

        # 1. Render a card with a Select
        card_element = Card(
            title="Pick a color",
            children=[
                Actions(
                    [
                        Select(
                            id="color_select",
                            label="Color",
                            options=[
                                SelectOption(label="Red", value="red"),
                                SelectOption(label="Blue", value="blue"),
                            ],
                        )
                    ]
                )
            ],
        )
        adaptive = card_to_adaptive_card(card_element)

        # Verify ChoiceSet was rendered
        body = adaptive.get("body", [])
        choice_set = next((b for b in body if b.get("type") == "Input.ChoiceSet"), None)
        assert choice_set is not None
        assert choice_set["id"] == "color_select"
        assert len(choice_set["choices"]) == 2

        # Verify auto-submit action exists
        actions = adaptive.get("actions", [])
        submit_action = next((a for a in actions if a.get("type") == "Action.Submit"), None)
        assert submit_action is not None

        # 2. Simulate Teams sending Action.Submit with the selected value
        activity = {
            "type": "message",
            "from": {"id": "user-1", "name": "Test User"},
            "conversation": {"id": "conv-1"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
            "value": {
                "actionId": AUTO_SUBMIT_ACTION_ID,
                "color_select": "blue",
            },
        }
        await adapter._handle_message_activity(activity)

        # 3. The __auto_submit sentinel fans out into one process_action per
        #    input key (adapter-teams/src/index.ts:404-471 + fanOutAutoSubmit),
        #    so a handler registered as on_action("color_select") fires with the
        #    selected value — the sentinel itself is never surfaced as an action ID.
        mock_chat.process_action.assert_called_once()
        action_event = mock_chat.process_action.call_args[0][0]
        assert action_event.action_id == "color_select"
        assert action_event.action_id != AUTO_SUBMIT_ACTION_ID
        assert action_event.value == "blue"
        assert action_event.user.user_id == "user-1"

    async def test_button_click_still_works(self):
        """Plain button Action.Submit still passes value correctly."""
        from unittest.mock import MagicMock

        from chat_sdk.adapters.teams.adapter import TeamsAdapter
        from chat_sdk.adapters.teams.types import TeamsAdapterConfig

        adapter = TeamsAdapter(
            TeamsAdapterConfig(
                app_id="test-app",
                app_password="test-pass",
            )
        )
        mock_chat = MagicMock()
        adapter._chat = mock_chat

        activity = {
            "type": "message",
            "from": {"id": "user-1", "name": "Test User"},
            "conversation": {"id": "conv-1"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
            "value": {
                "actionId": "approve_btn",
                "value": "approved",
            },
        }
        await adapter._handle_message_activity(activity)

        action_event = mock_chat.process_action.call_args[0][0]
        assert action_event.action_id == "approve_btn"
        # Single "value" key gets unwrapped for backward compat
        assert action_event.value == "approved"

    async def test_modal_button_strips_msteams_transport_key(self):
        """Buttons with action_type=modal include msteams metadata that must be stripped."""
        from unittest.mock import MagicMock

        from chat_sdk.adapters.teams.adapter import TeamsAdapter
        from chat_sdk.adapters.teams.types import TeamsAdapterConfig

        adapter = TeamsAdapter(
            TeamsAdapterConfig(
                app_id="test-app",
                app_password="test-pass",
            )
        )
        mock_chat = MagicMock()
        adapter._chat = mock_chat

        # Simulate payload from a modal button (has msteams transport key)
        activity = {
            "type": "message",
            "from": {"id": "user-1", "name": "Test User"},
            "conversation": {"id": "conv-1"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
            "value": {
                "actionId": "open_dialog",
                "value": "clicked",
                "msteams": {"type": "task/fetch"},
            },
        }
        await adapter._handle_message_activity(activity)

        action_event = mock_chat.process_action.call_args[0][0]
        assert action_event.action_id == "open_dialog"
        # msteams key should be stripped — only user value remains
        assert action_event.value == "clicked"
