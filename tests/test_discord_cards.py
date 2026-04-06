"""Tests for Discord card conversion -- embeds, components, and fallback text.

Ported from packages/adapter-discord/src/cards.test.ts.
"""

from __future__ import annotations

import pytest

from chat_sdk.adapters.discord.cards import (
    BUTTON_STYLE_DANGER,
    BUTTON_STYLE_LINK,
    BUTTON_STYLE_PRIMARY,
    BUTTON_STYLE_SECONDARY,
    DISCORD_BLURPLE,
    card_to_discord_payload,
    card_to_fallback_text,
)
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
# cardToDiscordPayload
# ---------------------------------------------------------------------------


class TestCardToDiscordPayload:
    def test_simple_card_with_title(self):
        card = Card(title="Welcome")
        result = card_to_discord_payload(card)
        assert len(result["embeds"]) == 1
        assert result["embeds"][0]["title"] == "Welcome"
        assert len(result["components"]) == 0

    def test_card_with_title_and_subtitle(self):
        card = Card(title="Order Update", subtitle="Your order is on its way")
        result = card_to_discord_payload(card)
        assert len(result["embeds"]) == 1
        assert result["embeds"][0]["title"] == "Order Update"
        assert "Your order is on its way" in result["embeds"][0]["description"]

    def test_card_with_header_image(self):
        card = Card(title="Product", image_url="https://example.com/product.png")
        result = card_to_discord_payload(card)
        assert result["embeds"][0]["image"] == {"url": "https://example.com/product.png"}

    def test_default_color_is_blurple(self):
        card = Card(title="Test")
        result = card_to_discord_payload(card)
        assert result["embeds"][0]["color"] == DISCORD_BLURPLE

    def test_text_elements(self):
        card = Card(
            children=[
                CardText("Regular text"),
                CardText("Bold text", style="bold"),
                CardText("Muted text", style="muted"),
            ]
        )
        result = card_to_discord_payload(card)
        desc = result["embeds"][0]["description"]
        assert "Regular text" in desc
        assert "**Bold text**" in desc
        assert "*Muted text*" in desc

    def test_image_elements_in_children(self):
        card = Card(children=[Image(url="https://example.com/img.png", alt="My image")])
        result = card_to_discord_payload(card)
        assert len(result["embeds"]) == 1

    def test_divider_elements(self):
        card = Card(children=[CardText("Before"), Divider(), CardText("After")])
        result = card_to_discord_payload(card)
        desc = result["embeds"][0]["description"]
        assert "Before" in desc
        # Divider rendered as horizontal line
        assert "\u2500" in desc
        assert "After" in desc

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
        result = card_to_discord_payload(card)
        assert len(result["components"]) == 1
        assert result["components"][0]["type"] == 1  # Action Row

        buttons = result["components"][0]["components"]
        assert len(buttons) == 3

        assert buttons[0]["type"] == 2
        assert buttons[0]["style"] == BUTTON_STYLE_PRIMARY
        assert buttons[0]["label"] == "Approve"
        assert buttons[0]["custom_id"] == "approve"

        assert buttons[1]["type"] == 2
        assert buttons[1]["style"] == BUTTON_STYLE_DANGER
        assert buttons[1]["label"] == "Reject"
        assert buttons[1]["custom_id"] == "reject"

        assert buttons[2]["type"] == 2
        assert buttons[2]["style"] == BUTTON_STYLE_SECONDARY
        assert buttons[2]["label"] == "Skip"
        assert buttons[2]["custom_id"] == "skip"

    def test_disabled_button(self):
        card = Card(
            children=[
                Actions(
                    [
                        Button(id="cancel", label="Cancelled", style="danger", disabled=True),
                        Button(id="retry", label="Retry"),
                    ]
                ),
            ]
        )
        result = card_to_discord_payload(card)
        buttons = result["components"][0]["components"]
        assert len(buttons) == 2
        assert buttons[0]["disabled"] is True
        assert "disabled" not in buttons[1]

    def test_link_buttons(self):
        card = Card(
            children=[
                Actions([LinkButton(url="https://example.com/docs", label="View Docs")]),
            ]
        )
        result = card_to_discord_payload(card)
        buttons = result["components"][0]["components"]
        assert len(buttons) == 1
        assert buttons[0]["style"] == BUTTON_STYLE_LINK
        assert buttons[0]["label"] == "View Docs"
        assert buttons[0]["url"] == "https://example.com/docs"

    def test_fields_to_embed_fields(self):
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
        result = card_to_discord_payload(card)
        fields = result["embeds"][0]["fields"]
        assert len(fields) == 2
        assert fields[0] == {"name": "Status", "value": "Active", "inline": True}
        assert fields[1] == {"name": "Priority", "value": "High", "inline": True}

    def test_section_children_flattened(self):
        card = Card(children=[Section([CardText("Inside section"), Divider()])])
        result = card_to_discord_payload(card)
        desc = result["embeds"][0]["description"]
        assert "Inside section" in desc
        assert "\u2500" in desc

    def test_complete_card(self):
        card = Card(
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
        result = card_to_discord_payload(card)
        assert len(result["embeds"]) == 1
        assert result["embeds"][0]["title"] == "Order #1234"
        assert "Status update" in result["embeds"][0]["description"]
        assert "Your order has been shipped!" in result["embeds"][0]["description"]
        assert "\u2500" in result["embeds"][0]["description"]
        assert len(result["embeds"][0]["fields"]) == 2
        assert len(result["components"]) == 1

    def test_card_with_no_title_or_subtitle(self):
        card = Card(children=[CardText("Just content")])
        result = card_to_discord_payload(card)
        assert "title" not in result["embeds"][0] or result["embeds"][0].get("title") is None
        assert result["embeds"][0]["description"] == "Just content"

    def test_card_link(self):
        card = Card(children=[CardLink(url="https://example.com", label="Click here")])
        result = card_to_discord_payload(card)
        assert result["embeds"][0]["description"] == "[Click here](https://example.com)"


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
        assert "**Order ID**" in text
        assert "#1234" in text
        assert "**Status**" in text
        assert "Ready" in text

    def test_card_with_only_title(self):
        card = Card(title="Simple Card")
        text = card_to_fallback_text(card)
        assert text == "**Simple Card**"

    def test_card_with_subtitle_only(self):
        card = Card(subtitle="Just a subtitle")
        text = card_to_fallback_text(card)
        assert text == "Just a subtitle"

    def test_divider_elements(self):
        card = Card(children=[CardText("Before"), Divider(), CardText("After")])
        text = card_to_fallback_text(card)
        assert "Before" in text
        assert "---" in text
        assert "After" in text

    def test_section_elements(self):
        card = Card(children=[Section([CardText("Section content")])])
        text = card_to_fallback_text(card)
        assert "Section content" in text

    def test_empty_card(self):
        card = Card()
        text = card_to_fallback_text(card)
        assert text == ""
