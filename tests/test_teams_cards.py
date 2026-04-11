"""Tests for Teams Adaptive Card conversion and fallback text.

Ported from packages/adapter-teams/src/cards.test.ts.
"""

from __future__ import annotations

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
        assert adaptive["version"] == "1.4"
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

    def test_divider_elements(self):
        card = Card(children=[Divider()])
        adaptive = card_to_adaptive_card(card)
        assert len(adaptive["body"]) == 1
        assert adaptive["body"][0] == {"type": "Container", "separator": True, "items": []}

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

        # 3. Verify process_action received the selected values
        mock_chat.process_action.assert_called_once()
        action_event = mock_chat.process_action.call_args[0][0]
        assert action_event.action_id == AUTO_SUBMIT_ACTION_ID
        assert action_event.value == {"color_select": "blue"}
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
