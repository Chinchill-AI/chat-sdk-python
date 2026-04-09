"""Tests for Teams Adaptive Card conversion and fallback text.

Ported from packages/adapter-teams/src/cards.test.ts.
"""

from __future__ import annotations

from chat_sdk.adapters.teams.cards import card_to_adaptive_card, card_to_fallback_text
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
