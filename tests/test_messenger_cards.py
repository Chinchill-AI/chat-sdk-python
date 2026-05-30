"""Port of adapter-messenger/src/cards.test.ts -- Messenger card rendering tests.

Tests card_to_messenger, card_to_messenger_text, and encode/decode callback
data (generic-vs-button selection, truncation caps, text fallback for
unsupported elements, callback-data round-trip + passthrough).
"""

from __future__ import annotations

from chat_sdk.adapters.messenger.cards import (
    card_to_messenger,
    card_to_messenger_text,
    decode_messenger_callback_data,
    encode_messenger_callback_data,
)

# ---------------------------------------------------------------------------
# Text fallback rendering
# ---------------------------------------------------------------------------


class TestCardToMessengerText:
    """Tests for card_to_messenger_text."""

    def test_simple_card_with_title(self):
        card = {"type": "card", "title": "Hello World", "children": []}
        assert card_to_messenger_text(card) == "Hello World"

    def test_card_with_title_and_subtitle(self):
        card = {
            "type": "card",
            "title": "Order #1234",
            "subtitle": "Status update",
            "children": [],
        }
        assert card_to_messenger_text(card) == "Order #1234\nStatus update"

    def test_card_with_text_content(self):
        card = {
            "type": "card",
            "title": "Notification",
            "children": [{"type": "text", "content": "Your order has been shipped!"}],
        }
        assert card_to_messenger_text(card) == "Notification\n\nYour order has been shipped!"

    def test_card_with_fields(self):
        card = {
            "type": "card",
            "title": "Order Details",
            "children": [
                {
                    "type": "fields",
                    "children": [
                        {"type": "field", "label": "Order ID", "value": "12345"},
                        {"type": "field", "label": "Status", "value": "Shipped"},
                    ],
                }
            ],
        }
        result = card_to_messenger_text(card)
        assert "Order ID: 12345" in result
        assert "Status: Shipped" in result

    def test_card_with_link_buttons_as_text_with_urls(self):
        card = {
            "type": "card",
            "title": "Actions",
            "children": [
                {
                    "type": "actions",
                    "children": [
                        {"type": "link-button", "url": "https://example.com/track", "label": "Track Order"},
                        {"type": "link-button", "url": "https://example.com/help", "label": "Get Help"},
                    ],
                }
            ],
        }
        result = card_to_messenger_text(card)
        assert "Track Order: https://example.com/track" in result
        assert "Get Help: https://example.com/help" in result

    def test_card_with_action_buttons_as_bracketed_text(self):
        card = {
            "type": "card",
            "title": "Approve?",
            "children": [
                {
                    "type": "actions",
                    "children": [
                        {"type": "button", "id": "approve", "label": "Approve", "style": "primary"},
                        {"type": "button", "id": "reject", "label": "Reject", "style": "danger"},
                    ],
                }
            ],
        }
        result = card_to_messenger_text(card)
        assert "[Approve]" in result
        assert "[Reject]" in result

    def test_card_with_inline_image(self):
        card = {
            "type": "card",
            "title": "Image Card",
            "children": [
                {"type": "image", "url": "https://example.com/image.png", "alt": "Example image"},
            ],
        }
        result = card_to_messenger_text(card)
        assert "Example image: https://example.com/image.png" in result

    def test_image_url_without_alt_text(self):
        card = {
            "type": "card",
            "children": [{"type": "image", "url": "https://example.com/photo.jpg"}],
        }
        assert card_to_messenger_text(card) == "https://example.com/photo.jpg"

    def test_card_with_divider(self):
        card = {
            "type": "card",
            "children": [
                {"type": "text", "content": "Before"},
                {"type": "divider"},
                {"type": "text", "content": "After"},
            ],
        }
        assert "---" in card_to_messenger_text(card)

    def test_card_with_section(self):
        card = {
            "type": "card",
            "children": [
                {"type": "section", "children": [{"type": "text", "content": "Section content"}]},
            ],
        }
        assert "Section content" in card_to_messenger_text(card)

    def test_card_with_link_element(self):
        card = {
            "type": "card",
            "children": [{"type": "link", "url": "https://example.com", "label": "Example Link"}],
        }
        assert "Example Link: https://example.com" in card_to_messenger_text(card)

    def test_card_with_table(self):
        card = {
            "type": "card",
            "children": [
                {
                    "type": "table",
                    "headers": ["Name", "Age"],
                    "rows": [["Alice", "30"], ["Bob", "25"]],
                }
            ],
        }
        result = card_to_messenger_text(card)
        assert "Name | Age" in result
        assert "Alice | 30" in result
        assert "Bob | 25" in result

    def test_card_image_url(self):
        card = {
            "type": "card",
            "title": "Card with Header Image",
            "image_url": "https://example.com/header.png",
            "children": [],
        }
        assert "https://example.com/header.png" in card_to_messenger_text(card)


# ---------------------------------------------------------------------------
# Template conversion -- Generic Template
# ---------------------------------------------------------------------------


class TestGenericTemplate:
    """Tests for Generic Template selection (title or image present)."""

    def test_produces_template_for_card_with_title_and_buttons(self):
        card = {
            "type": "card",
            "title": "Choose an action",
            "children": [
                {"type": "text", "content": "What would you like to do?"},
                {
                    "type": "actions",
                    "children": [
                        {"type": "button", "id": "btn_yes", "label": "Yes"},
                        {"type": "button", "id": "btn_no", "label": "No"},
                    ],
                },
            ],
        }
        result = card_to_messenger(card)
        assert result["type"] == "template"
        payload = result["payload"]
        assert payload["template_type"] == "generic"
        assert len(payload["elements"]) == 1
        assert payload["elements"][0]["title"] == "Choose an action"
        assert len(payload["elements"][0]["buttons"]) == 2
        assert payload["elements"][0]["buttons"][0]["type"] == "postback"
        assert payload["elements"][0]["buttons"][0]["title"] == "Yes"

    def test_produces_template_for_card_with_image_url(self):
        card = {
            "type": "card",
            "title": "Product",
            "image_url": "https://example.com/product.jpg",
            "children": [
                {"type": "actions", "children": [{"type": "button", "id": "buy", "label": "Buy Now"}]},
            ],
        }
        result = card_to_messenger(card)
        assert result["type"] == "template"
        assert result["payload"]["elements"][0]["image_url"] == "https://example.com/product.jpg"

    def test_includes_subtitle_in_generic_template(self):
        card = {
            "type": "card",
            "title": "Order #123",
            "subtitle": "Your order is ready",
            "children": [
                {"type": "actions", "children": [{"type": "button", "id": "view", "label": "View"}]},
            ],
        }
        result = card_to_messenger(card)
        assert result["type"] == "template"
        assert result["payload"]["elements"][0]["subtitle"] == "Your order is ready"

    def test_supports_link_buttons_as_web_url_type(self):
        card = {
            "type": "card",
            "title": "Resources",
            "children": [
                {
                    "type": "actions",
                    "children": [
                        {"type": "link-button", "url": "https://example.com/docs", "label": "View Docs"},
                    ],
                },
            ],
        }
        result = card_to_messenger(card)
        assert result["type"] == "template"
        button = result["payload"]["elements"][0]["buttons"][0]
        assert button["type"] == "web_url"
        assert button["url"] == "https://example.com/docs"

    def test_mixes_postback_and_web_url_buttons(self):
        card = {
            "type": "card",
            "title": "Options",
            "children": [
                {
                    "type": "actions",
                    "children": [
                        {"type": "button", "id": "action1", "label": "Do Action"},
                        {"type": "link-button", "url": "https://example.com", "label": "Learn More"},
                    ],
                },
            ],
        }
        result = card_to_messenger(card)
        assert result["type"] == "template"
        buttons = result["payload"]["elements"][0]["buttons"]
        assert len(buttons) == 2
        assert buttons[0]["type"] == "postback"
        assert buttons[1]["type"] == "web_url"


# ---------------------------------------------------------------------------
# Template conversion -- Button Template
# ---------------------------------------------------------------------------


class TestButtonTemplate:
    """Tests for Button Template selection (no title/image, has body + buttons)."""

    def test_produces_template_for_card_without_title_but_with_text_and_buttons(self):
        card = {
            "type": "card",
            "children": [
                {"type": "text", "content": "Please select an option:"},
                {
                    "type": "actions",
                    "children": [
                        {"type": "button", "id": "opt1", "label": "Option 1"},
                        {"type": "button", "id": "opt2", "label": "Option 2"},
                    ],
                },
            ],
        }
        result = card_to_messenger(card)
        assert result["type"] == "template"
        payload = result["payload"]
        assert payload["template_type"] == "button"
        assert payload["text"] == "Please select an option:"
        assert len(payload["buttons"]) == 2

    def test_builds_body_text_from_fields_element(self):
        card = {
            "type": "card",
            "children": [
                {
                    "type": "fields",
                    "children": [
                        {"type": "field", "label": "Status", "value": "Active"},
                        {"type": "field", "label": "Priority", "value": "High"},
                    ],
                },
                {"type": "actions", "children": [{"type": "button", "id": "ok", "label": "OK"}]},
            ],
        }
        result = card_to_messenger(card)
        assert result["type"] == "template"
        assert result["payload"]["template_type"] == "button"
        assert "Status: Active" in result["payload"]["text"]
        assert "Priority: High" in result["payload"]["text"]

    def test_builds_body_text_from_link_element(self):
        card = {
            "type": "card",
            "children": [
                {"type": "link", "url": "https://example.com/docs", "label": "Documentation"},
                {"type": "actions", "children": [{"type": "button", "id": "view", "label": "View"}]},
            ],
        }
        result = card_to_messenger(card)
        assert result["type"] == "template"
        assert result["payload"]["template_type"] == "button"
        assert "Documentation: https://example.com/docs" in result["payload"]["text"]

    def test_builds_body_text_from_section_containing_fields(self):
        card = {
            "type": "card",
            "children": [
                {
                    "type": "section",
                    "children": [
                        {"type": "fields", "children": [{"type": "field", "label": "Name", "value": "Test"}]},
                    ],
                },
                {"type": "actions", "children": [{"type": "button", "id": "submit", "label": "Submit"}]},
            ],
        }
        result = card_to_messenger(card)
        assert result["type"] == "template"
        assert result["payload"]["template_type"] == "button"
        assert "Name: Test" in result["payload"]["text"]


# ---------------------------------------------------------------------------
# Constraint handling
# ---------------------------------------------------------------------------


class TestConstraintHandling:
    """Tests for fallback and truncation constraints."""

    def test_falls_back_to_text_for_table_nested_in_section(self):
        card = {
            "type": "card",
            "title": "Nested Table",
            "children": [
                {
                    "type": "section",
                    "children": [{"type": "table", "headers": ["A", "B"], "rows": [["1", "2"]]}],
                },
                {"type": "actions", "children": [{"type": "button", "id": "btn", "label": "Click"}]},
            ],
        }
        assert card_to_messenger(card)["type"] == "text"

    def test_falls_back_to_text_when_actions_contain_only_select(self):
        card = {
            "type": "card",
            "title": "Select Only",
            "children": [
                {
                    "type": "actions",
                    "children": [
                        {
                            "type": "select",
                            "id": "sel1",
                            "label": "Choose one",
                            "options": [
                                {"label": "Option A", "value": "a"},
                                {"label": "Option B", "value": "b"},
                            ],
                        }
                    ],
                }
            ],
        }
        assert card_to_messenger(card)["type"] == "text"

    def test_limits_to_3_buttons_max(self):
        card = {
            "type": "card",
            "title": "Many buttons",
            "children": [
                {
                    "type": "actions",
                    "children": [
                        {"type": "button", "id": "btn1", "label": "One"},
                        {"type": "button", "id": "btn2", "label": "Two"},
                        {"type": "button", "id": "btn3", "label": "Three"},
                        {"type": "button", "id": "btn4", "label": "Four"},
                    ],
                }
            ],
        }
        result = card_to_messenger(card)
        assert result["type"] == "template"
        assert len(result["payload"]["elements"][0]["buttons"]) == 3

    def test_truncates_long_button_titles_to_20_chars(self):
        card = {
            "type": "card",
            "title": "Long titles",
            "children": [
                {
                    "type": "actions",
                    "children": [
                        {"type": "button", "id": "btn_long", "label": "This is a very long button title"},
                    ],
                }
            ],
        }
        result = card_to_messenger(card)
        assert result["type"] == "template"
        button_title = result["payload"]["elements"][0]["buttons"][0]["title"]
        assert len(button_title) <= 20
        assert "…" in button_title

    def test_falls_back_to_text_for_cards_without_buttons(self):
        card = {
            "type": "card",
            "title": "Info only",
            "children": [{"type": "text", "content": "Just some info"}],
        }
        assert card_to_messenger(card)["type"] == "text"

    def test_falls_back_to_text_for_cards_with_only_link_buttons_and_no_title(self):
        card = {
            "type": "card",
            "children": [
                {
                    "type": "actions",
                    "children": [{"type": "link-button", "url": "https://example.com", "label": "Visit"}],
                }
            ],
        }
        assert card_to_messenger(card)["type"] == "text"

    def test_falls_back_to_text_for_cards_with_select_elements(self):
        card = {
            "type": "card",
            "title": "With select",
            "children": [
                {
                    "type": "actions",
                    "children": [
                        {"type": "select", "id": "sel1", "label": "Choose", "options": [{"label": "A", "value": "a"}]},
                    ],
                }
            ],
        }
        assert card_to_messenger(card)["type"] == "text"

    def test_falls_back_to_text_for_cards_with_radio_select_elements(self):
        card = {
            "type": "card",
            "title": "With radio",
            "children": [
                {
                    "type": "actions",
                    "children": [
                        {
                            "type": "radio_select",
                            "id": "radio1",
                            "label": "Pick one",
                            "options": [{"label": "X", "value": "x"}],
                        },
                    ],
                }
            ],
        }
        assert card_to_messenger(card)["type"] == "text"

    def test_falls_back_to_text_for_cards_with_table_elements(self):
        card = {
            "type": "card",
            "title": "With table",
            "children": [
                {"type": "table", "headers": ["Col1", "Col2"], "rows": [["A", "B"]]},
                {"type": "actions", "children": [{"type": "button", "id": "btn", "label": "Click"}]},
            ],
        }
        assert card_to_messenger(card)["type"] == "text"

    def test_truncates_long_subtitles_to_80_chars(self):
        long_subtitle = (
            "This is an extremely long subtitle that definitely exceeds the 80 character limit imposed by Messenger"
        )
        card = {
            "type": "card",
            "title": "Test",
            "subtitle": long_subtitle,
            "children": [
                {"type": "actions", "children": [{"type": "button", "id": "btn", "label": "Click"}]},
            ],
        }
        result = card_to_messenger(card)
        assert result["type"] == "template"
        subtitle = result["payload"]["elements"][0]["subtitle"]
        assert len(subtitle) <= 80
        assert "…" in subtitle

    def test_handles_nested_actions_in_sections(self):
        card = {
            "type": "card",
            "title": "Nested",
            "children": [
                {
                    "type": "section",
                    "children": [
                        {"type": "actions", "children": [{"type": "button", "id": "nested", "label": "Nested"}]},
                    ],
                },
            ],
        }
        result = card_to_messenger(card)
        assert result["type"] == "template"
        assert len(result["payload"]["elements"][0]["buttons"]) == 1
        assert result["payload"]["elements"][0]["buttons"][0]["title"] == "Nested"


# ---------------------------------------------------------------------------
# Callback data
# ---------------------------------------------------------------------------


class TestCallbackDataEncoding:
    """Tests for encode_messenger_callback_data."""

    def test_encodes_action_id_only(self):
        assert encode_messenger_callback_data("my_action") == 'chat:{"a":"my_action"}'

    def test_encodes_action_id_and_value(self):
        assert encode_messenger_callback_data("my_action", "some_value") == 'chat:{"a":"my_action","v":"some_value"}'

    def test_handles_special_characters_in_action_id(self):
        assert encode_messenger_callback_data("action:with:colons") == 'chat:{"a":"action:with:colons"}'


class TestCallbackDataDecoding:
    """Tests for decode_messenger_callback_data."""

    def test_decodes_encoded_callback_data_with_value(self):
        encoded = encode_messenger_callback_data("my_action", "some_value")
        result = decode_messenger_callback_data(encoded)
        assert result["action_id"] == "my_action"
        assert result["value"] == "some_value"

    def test_decodes_action_id_without_value(self):
        encoded = encode_messenger_callback_data("my_action")
        result = decode_messenger_callback_data(encoded)
        assert result["action_id"] == "my_action"
        assert result["value"] is None

    def test_handles_non_prefixed_data_as_passthrough(self):
        # Divergence-candidate (see #110): non-"chat:" payloads return the raw
        # string as BOTH action_id and value. Pinning upstream behavior exactly.
        result = decode_messenger_callback_data("raw_payload")
        assert result["action_id"] == "raw_payload"
        assert result["value"] == "raw_payload"

    def test_handles_none_data(self):
        result = decode_messenger_callback_data(None)
        assert result["action_id"] == "messenger_callback"
        assert result["value"] is None

    def test_handles_malformed_json_after_prefix(self):
        # Divergence-candidate (see #110): malformed JSON after the "chat:"
        # prefix also falls back to the raw-string-as-both passthrough.
        result = decode_messenger_callback_data("chat:not-valid-json")
        assert result["action_id"] == "chat:not-valid-json"
        assert result["value"] == "chat:not-valid-json"

    def test_handles_empty_string_as_missing_data(self):
        result = decode_messenger_callback_data("")
        assert result["action_id"] == "messenger_callback"
        assert result["value"] is None

    def test_roundtrips_encode_decode(self):
        action_id = "test_action"
        value = "test_value"
        encoded = encode_messenger_callback_data(action_id, value)
        decoded = decode_messenger_callback_data(encoded)
        assert decoded["action_id"] == action_id
        assert decoded["value"] == value


class TestCallbackDataTemplateIntegration:
    """Tests that template buttons embed encoded callback data."""

    def test_encodes_button_id_and_value_in_postback_payload(self):
        card = {
            "type": "card",
            "title": "Test",
            "children": [
                {
                    "type": "actions",
                    "children": [
                        {"type": "button", "id": "action_id", "label": "Click", "value": "action_value"},
                    ],
                }
            ],
        }
        result = card_to_messenger(card)
        assert result["type"] == "template"
        button = result["payload"]["elements"][0]["buttons"][0]
        assert button["type"] == "postback"
        assert button["payload"] == encode_messenger_callback_data("action_id", "action_value")

    def test_encodes_button_id_without_value_when_value_is_undefined(self):
        card = {
            "type": "card",
            "title": "Test",
            "children": [
                {"type": "actions", "children": [{"type": "button", "id": "just_id", "label": "Click"}]},
            ],
        }
        result = card_to_messenger(card)
        assert result["type"] == "template"
        button = result["payload"]["elements"][0]["buttons"][0]
        assert button["payload"] == encode_messenger_callback_data("just_id")
