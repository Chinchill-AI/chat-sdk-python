"""Tests for Google Chat card conversion.

Port of packages/adapter-gchat/src/cards.test.ts.
"""

from __future__ import annotations

from chat_sdk.adapters.google_chat.cards import card_to_google_card
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
from chat_sdk.shared import card_to_fallback_text

# ---------------------------------------------------------------------------
# cardToGoogleCard
# ---------------------------------------------------------------------------


class TestCardToGoogleCard:
    def test_creates_valid_structure(self):
        card = Card(title="Test")
        gchat_card = card_to_google_card(card)
        assert "card" in gchat_card
        assert isinstance(gchat_card["card"].get("sections"), list)

    def test_accepts_optional_card_id(self):
        card = Card(title="Test")
        gchat_card = card_to_google_card(card, "my-card-id")
        assert gchat_card.get("cardId") == "my-card-id"

    def test_converts_card_with_title(self):
        card = Card(title="Welcome Message")
        gchat_card = card_to_google_card(card)
        assert gchat_card["card"]["header"]["title"] == "Welcome Message"

    def test_converts_card_with_title_and_subtitle(self):
        card = Card(title="Order Update", subtitle="Your package is on its way")
        gchat_card = card_to_google_card(card)
        header = gchat_card["card"]["header"]
        assert header["title"] == "Order Update"
        assert header["subtitle"] == "Your package is on its way"

    def test_converts_card_with_header_image(self):
        card = Card(title="Product", image_url="https://example.com/product.png")
        gchat_card = card_to_google_card(card)
        header = gchat_card["card"]["header"]
        assert header["title"] == "Product"
        assert header["imageUrl"] == "https://example.com/product.png"
        assert header["imageType"] == "SQUARE"

    def test_converts_text_to_text_paragraph(self):
        card = Card(
            children=[
                CardText("Regular text"),
                CardText("Bold text", style="bold"),
            ]
        )
        gchat_card = card_to_google_card(card)
        sections = gchat_card["card"]["sections"]
        assert len(sections) == 1
        widgets = sections[0]["widgets"]
        assert len(widgets) == 2
        assert widgets[0] == {"textParagraph": {"text": "Regular text"}}
        assert widgets[1] == {"textParagraph": {"text": "*Bold text*"}}

    def test_converts_image_elements(self):
        card = Card(
            children=[
                Image(url="https://example.com/img.png", alt="My image"),
            ]
        )
        gchat_card = card_to_google_card(card)
        widgets = gchat_card["card"]["sections"][0]["widgets"]
        assert len(widgets) == 1
        assert widgets[0] == {
            "image": {
                "imageUrl": "https://example.com/img.png",
                "altText": "My image",
            },
        }

    def test_converts_divider_elements(self):
        card = Card(children=[Divider()])
        gchat_card = card_to_google_card(card)
        widgets = gchat_card["card"]["sections"][0]["widgets"]
        assert len(widgets) == 1
        assert widgets[0] == {"divider": {}}

    def test_converts_actions_with_buttons(self):
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
        gchat_card = card_to_google_card(card)
        widgets = gchat_card["card"]["sections"][0]["widgets"]
        assert len(widgets) == 1
        button_list = widgets[0]["buttonList"]
        assert len(button_list["buttons"]) == 3

        # Primary button
        btn0 = button_list["buttons"][0]
        assert btn0["text"] == "Approve"
        assert btn0["onClick"]["action"]["function"] == "approve"
        assert {"key": "actionId", "value": "approve"} in btn0["onClick"]["action"]["parameters"]

        # Danger button with value
        btn1 = button_list["buttons"][1]
        assert btn1["text"] == "Reject"
        assert {"key": "value", "value": "data-123"} in btn1["onClick"]["action"]["parameters"]

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
        gchat_card = card_to_google_card(card)
        widgets = gchat_card["card"]["sections"][0]["widgets"]
        button_list = widgets[0]["buttonList"]
        assert button_list["buttons"][0].get("disabled") is True
        assert "disabled" not in button_list["buttons"][1] or button_list["buttons"][1].get("disabled") is not True

    def test_endpoint_url_as_function(self):
        card = Card(
            children=[
                Actions(
                    [
                        Button(id="approve", label="Approve"),
                        Button(id="reject", label="Reject", value="data-123"),
                    ]
                ),
            ]
        )
        gchat_card = card_to_google_card(card, {"endpoint_url": "https://example.com/api/webhooks/gchat"})
        widgets = gchat_card["card"]["sections"][0]["widgets"]
        button_list = widgets[0]["buttonList"]

        btn0 = button_list["buttons"][0]
        assert btn0["onClick"]["action"]["function"] == "https://example.com/api/webhooks/gchat"
        assert {"key": "actionId", "value": "approve"} in btn0["onClick"]["action"]["parameters"]

    def test_link_button_with_open_link(self):
        card = Card(
            children=[
                Actions(
                    [
                        LinkButton(url="https://example.com/docs", label="View Docs", style="primary"),
                    ]
                ),
            ]
        )
        gchat_card = card_to_google_card(card)
        widgets = gchat_card["card"]["sections"][0]["widgets"]
        button_list = widgets[0]["buttonList"]
        assert len(button_list["buttons"]) == 1
        btn = button_list["buttons"][0]
        assert btn["text"] == "View Docs"
        assert btn["onClick"]["openLink"]["url"] == "https://example.com/docs"

    def test_converts_fields_to_decorated_text(self):
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
        gchat_card = card_to_google_card(card)
        widgets = gchat_card["card"]["sections"][0]["widgets"]
        assert len(widgets) == 2
        assert widgets[0] == {"decoratedText": {"topLabel": "Status", "text": "Active"}}
        assert widgets[1] == {"decoratedText": {"topLabel": "Priority", "text": "High"}}

    def test_section_creates_separate_sections(self):
        card = Card(
            children=[
                CardText("Before section"),
                Section([CardText("Inside section")]),
                CardText("After section"),
            ]
        )
        gchat_card = card_to_google_card(card)
        sections = gchat_card["card"]["sections"]
        assert len(sections) == 3
        assert sections[0]["widgets"][0]["textParagraph"]["text"] == "Before section"
        assert sections[1]["widgets"][0]["textParagraph"]["text"] == "Inside section"
        assert sections[2]["widgets"][0]["textParagraph"]["text"] == "After section"

    def test_converts_complete_card(self):
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
                Actions(
                    [
                        Button(id="track", label="Track Package", style="primary"),
                    ]
                ),
            ],
        )
        gchat_card = card_to_google_card(card)
        assert gchat_card["card"]["header"]["title"] == "Order #1234"
        assert gchat_card["card"]["header"]["subtitle"] == "Status update"
        sections = gchat_card["card"]["sections"]
        assert len(sections) == 1
        widgets = sections[0]["widgets"]
        # text + 2 fields + buttonList = 4 widgets
        assert len(widgets) == 4

    def test_empty_card_has_placeholder(self):
        card = Card()
        gchat_card = card_to_google_card(card)
        sections = gchat_card["card"]["sections"]
        assert len(sections) == 1
        assert len(sections[0]["widgets"]) == 1


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
        assert "Order Update" in text
        assert "Status changed" in text
        assert "Your order is ready" in text
        assert "Order ID" in text and "#1234" in text
        assert "Status" in text and "Ready" in text

    def test_card_with_only_title(self):
        card = Card(title="Simple Card")
        text = card_to_fallback_text(card)
        assert "Simple Card" in text


# ---------------------------------------------------------------------------
# Markdown bold conversion
# ---------------------------------------------------------------------------


class TestMarkdownBoldConversion:
    def test_converts_double_bold_to_single(self):
        card = Card(children=[CardText("The **domain** is example.com")])
        gchat_card = card_to_google_card(card)
        widgets = gchat_card["card"]["sections"][0]["widgets"]
        assert widgets[0]["textParagraph"]["text"] == "The *domain* is example.com"

    def test_converts_multiple_bold_segments(self):
        card = Card(children=[CardText("**Project**: my-app, **Status**: active")])
        gchat_card = card_to_google_card(card)
        widgets = gchat_card["card"]["sections"][0]["widgets"]
        assert widgets[0]["textParagraph"]["text"] == "*Project*: my-app, *Status*: active"

    def test_preserves_single_asterisk(self):
        card = Card(children=[CardText("Already *bold* in GChat format")])
        gchat_card = card_to_google_card(card)
        widgets = gchat_card["card"]["sections"][0]["widgets"]
        assert widgets[0]["textParagraph"]["text"] == "Already *bold* in GChat format"

    def test_converts_bold_in_field_values(self):
        card = Card(children=[Fields([Field(label="Status", value="**Active**")])])
        gchat_card = card_to_google_card(card)
        widgets = gchat_card["card"]["sections"][0]["widgets"]
        dt = widgets[0]["decoratedText"]
        assert dt["text"] == "*Active*"
        assert "**" not in dt["text"]

    def test_converts_bold_in_field_labels(self):
        card = Card(children=[Fields([Field(label="**Important**", value="value")])])
        gchat_card = card_to_google_card(card)
        widgets = gchat_card["card"]["sections"][0]["widgets"]
        assert widgets[0]["decoratedText"]["topLabel"] == "*Important*"

    def test_plain_text_no_change(self):
        card = Card(children=[CardText("Plain text")])
        gchat_card = card_to_google_card(card)
        widgets = gchat_card["card"]["sections"][0]["widgets"]
        assert widgets[0]["textParagraph"]["text"] == "Plain text"


# ---------------------------------------------------------------------------
# CardLink
# ---------------------------------------------------------------------------


class TestCardLink:
    def test_card_link_converts_to_html_link(self):
        card = Card(children=[CardLink(url="https://example.com", label="Click here")])
        gchat_card = card_to_google_card(card)
        sections = gchat_card["card"]["sections"]
        assert len(sections) == 1
        assert len(sections[0]["widgets"]) == 1
        assert sections[0]["widgets"][0] == {
            "textParagraph": {
                "text": '<a href="https://example.com">Click here</a>',
            },
        }
