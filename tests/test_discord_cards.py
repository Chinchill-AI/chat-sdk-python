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
    decode_discord_custom_id,
    encode_discord_custom_id,
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
from chat_sdk.shared.errors import ValidationError

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
        # Button values are packed into custom_id (vercel/chat#454)
        assert buttons[1]["custom_id"] == "reject\ndata-123"

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


# ---------------------------------------------------------------------------
# encodeDiscordCustomId / decodeDiscordCustomId (vercel/chat#454)
# ---------------------------------------------------------------------------


class TestEncodeDecodeDiscordCustomId:
    """describe("encodeDiscordCustomId / decodeDiscordCustomId")"""

    # it("encodes actionId only when no value")
    def test_encodes_actionid_only_when_no_value(self):
        assert encode_discord_custom_id("approve") == "approve"

    # it("encodes actionId with value")
    def test_encodes_actionid_with_value(self):
        assert encode_discord_custom_id("approve", "order-123") == "approve\norder-123"

    # it("skips encoding when empty value")
    def test_skips_encoding_when_empty_value(self):
        assert encode_discord_custom_id("approve", "") == "approve"

    # it("throws when actionId is empty")
    def test_throws_when_actionid_is_empty(self):
        with pytest.raises(ValidationError):
            encode_discord_custom_id("")

    # it("throws when actionId exceeds 100 chars")
    def test_throws_when_actionid_exceeds_100_chars(self):
        with pytest.raises(ValidationError):
            encode_discord_custom_id("x" * 101)

    # it("throws when encoded custom_id exceeds 100 chars")
    def test_throws_when_encoded_custom_id_exceeds_100_chars(self):
        long_value = "x" * 100
        with pytest.raises(ValidationError):
            encode_discord_custom_id("btn", long_value)

    # it("throws when a button value makes custom_id too long")
    def test_throws_when_a_button_value_makes_custom_id_too_long(self):
        card = Card(
            children=[
                Actions(
                    [
                        Button(
                            id="x" * 90,
                            label="Approve",
                            value="__cb:1234567890abcdef",
                        )
                    ]
                ),
            ]
        )

        with pytest.raises(ValidationError):
            card_to_discord_payload(card)

    # it("decodes actionId only")
    def test_decodes_actionid_only(self):
        decoded = decode_discord_custom_id("approve")
        assert decoded.action_id == "approve"
        assert decoded.value is None

    # it("decodes actionId with value")
    def test_decodes_actionid_with_value(self):
        decoded = decode_discord_custom_id("approve\norder-123")
        assert decoded.action_id == "approve"
        assert decoded.value == "order-123"

    # it("round-trips encode/decode")
    def test_roundtrips_encodedecode(self):
        encoded = encode_discord_custom_id("btn", "__cb:a1b2c3d4e5f6g7h8")
        decoded = decode_discord_custom_id(encoded)
        assert decoded.action_id == "btn"
        assert decoded.value == "__cb:a1b2c3d4e5f6g7h8"

    # it("preserves embedded delimiter chars in the value (decoder splits on first only)")
    def test_preserves_embedded_delimiter_chars_in_the_value_decoder_splits_on_first_only(self):
        decoded = decode_discord_custom_id("btn\nfirst\nsecond")
        assert decoded.action_id == "btn"
        assert decoded.value == "first\nsecond"

    # it("treats explicitly null value as no value")
    def test_treats_explicitly_null_value_as_no_value(self):
        assert encode_discord_custom_id("approve", None) == "approve"

    # it("encodes a custom_id at the 100 char boundary")
    def test_encodes_a_custom_id_at_the_100_char_boundary(self):
        action_id = "a" * 50
        value = "b" * 49
        encoded = encode_discord_custom_id(action_id, value)
        assert len(encoded) == 100
        decoded = decode_discord_custom_id(encoded)
        assert decoded.action_id == action_id
        assert decoded.value == value

    # it("rejects a custom_id one char past the boundary")
    def test_rejects_a_custom_id_one_char_past_the_boundary(self):
        action_id = "a" * 50
        value = "b" * 50
        with pytest.raises(ValidationError):
            encode_discord_custom_id(action_id, value)

    # it("renders cards with values into Discord button payloads")
    def test_renders_cards_with_values_into_discord_button_payloads(self):
        card = Card(
            children=[
                Actions(
                    [
                        Button(id="approve", label="Approve", value="order-99"),
                        Button(id="deny", label="Deny"),
                    ]
                ),
            ]
        )

        payload = card_to_discord_payload(card)
        buttons = payload["components"][0]["components"]

        assert buttons[0]["custom_id"] == "approve\norder-99"
        assert buttons[1]["custom_id"] == "deny"
