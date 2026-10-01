"""Port of adapter-whatsapp/src/cards.test.ts -- WhatsApp card rendering tests.

Tests card_to_whatsapp_text, card_to_whatsapp, card_to_plain_text,
encode/decode callback data.
"""

from __future__ import annotations

from chat_sdk.adapters.whatsapp.cards import (
    card_link_button_lines,
    card_to_plain_text,
    card_to_whatsapp,
    card_to_whatsapp_text,
    decode_whatsapp_callback_data,
    encode_whatsapp_callback_data,
)

# ---------------------------------------------------------------------------
# cardToWhatsAppText
# ---------------------------------------------------------------------------


class TestCardToWhatsAppText:
    """Tests for card_to_whatsapp_text."""

    def test_simple_card_with_title(self):
        card = {"type": "card", "title": "Hello World", "children": []}
        result = card_to_whatsapp_text(card)
        assert result == "*Hello World*"

    def test_card_with_title_and_subtitle(self):
        card = {
            "type": "card",
            "title": "Order #1234",
            "subtitle": "Status update",
            "children": [],
        }
        result = card_to_whatsapp_text(card)
        assert result == "*Order #1234*\nStatus update"

    def test_card_with_text_content(self):
        card = {
            "type": "card",
            "title": "Notification",
            "children": [{"type": "text", "content": "Your order has been shipped!"}],
        }
        result = card_to_whatsapp_text(card)
        assert result == "*Notification*\n\nYour order has been shipped!"

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
        result = card_to_whatsapp_text(card)
        assert "*Order ID:* 12345" in result
        assert "*Status:* Shipped" in result

    def test_card_with_link_buttons(self):
        card = {
            "type": "card",
            "title": "Actions",
            "children": [
                {
                    "type": "actions",
                    "children": [
                        {
                            "type": "link-button",
                            "url": "https://example.com/track",
                            "label": "Track Order",
                        },
                        {
                            "type": "link-button",
                            "url": "https://example.com/help",
                            "label": "Get Help",
                        },
                    ],
                }
            ],
        }
        result = card_to_whatsapp_text(card)
        assert "Track Order: https://example.com/track" in result
        assert "Get Help: https://example.com/help" in result

    def test_card_with_action_buttons(self):
        card = {
            "type": "card",
            "title": "Approve?",
            "children": [
                {
                    "type": "actions",
                    "children": [
                        {
                            "type": "button",
                            "id": "approve",
                            "label": "Approve",
                            "style": "primary",
                        },
                        {
                            "type": "button",
                            "id": "reject",
                            "label": "Reject",
                            "style": "danger",
                        },
                    ],
                }
            ],
        }
        result = card_to_whatsapp_text(card)
        assert "[Approve]" in result
        assert "[Reject]" in result

    def test_card_with_image(self):
        card = {
            "type": "card",
            "title": "Image Card",
            "children": [
                {
                    "type": "image",
                    "url": "https://example.com/image.png",
                    "alt": "Example image",
                }
            ],
        }
        result = card_to_whatsapp_text(card)
        assert "Example image: https://example.com/image.png" in result

    def test_card_with_divider(self):
        card = {
            "type": "card",
            "children": [
                {"type": "text", "content": "Before"},
                {"type": "divider"},
                {"type": "text", "content": "After"},
            ],
        }
        result = card_to_whatsapp_text(card)
        assert "---" in result

    def test_card_with_section(self):
        card = {
            "type": "card",
            "children": [
                {
                    "type": "section",
                    "children": [{"type": "text", "content": "Section content"}],
                }
            ],
        }
        result = card_to_whatsapp_text(card)
        assert "Section content" in result

    def test_text_with_styles(self):
        card = {
            "type": "card",
            "children": [
                {"type": "text", "content": "Normal text"},
                {"type": "text", "content": "Bold text", "style": "bold"},
                {"type": "text", "content": "Muted text", "style": "muted"},
            ],
        }
        result = card_to_whatsapp_text(card)
        assert "Normal text" in result
        assert "*Bold text*" in result
        assert "_Muted text_" in result


# ---------------------------------------------------------------------------
# cardToWhatsApp (interactive vs text fallback)
# ---------------------------------------------------------------------------


class TestCardToWhatsApp:
    """Tests for card_to_whatsapp."""

    def test_interactive_with_1_to_3_buttons(self):
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
        result = card_to_whatsapp(card)
        assert result["type"] == "interactive"
        interactive = result["interactive"]
        assert interactive["type"] == "button"
        assert interactive["header"]["text"] == "Choose an action"
        assert len(interactive["action"]["buttons"]) == 2

    def test_truncate_to_3_buttons(self):
        card = {
            "type": "card",
            "title": "Too many buttons",
            "children": [
                {
                    "type": "actions",
                    "children": [{"type": "button", "id": f"btn_{i}", "label": str(i)} for i in range(4)],
                }
            ],
        }
        result = card_to_whatsapp(card)
        assert result["type"] == "interactive"
        assert len(result["interactive"]["action"]["buttons"]) == 3

    # -- cta_url (port of upstream 6abf4807, vercel/chat#781) -----------------

    def test_should_produce_cta_url_interactive_for_a_single_link_button(self):
        card = {
            "type": "card",
            "title": "Links only",
            "children": [
                {
                    "type": "actions",
                    "children": [{"type": "link-button", "url": "https://example.com", "label": "Visit"}],
                }
            ],
        }
        result = card_to_whatsapp(card)
        assert result["type"] == "interactive"
        interactive = result["interactive"]
        assert interactive["type"] == "cta_url"
        assert interactive["header"] == {"type": "text", "text": "Links only"}
        assert interactive["body"]["text"] == "Open link"
        assert interactive["action"] == {
            "name": "cta_url",
            "parameters": {"display_text": "Visit", "url": "https://example.com"},
        }

    def test_should_include_body_text_from_card_content_for_cta_url_messages(self):
        card = {
            "type": "card",
            "title": "Workshop dates",
            "subtitle": "Dates subject to change",
            "children": [
                {"type": "text", "content": "Tap the button below to see available dates."},
                {
                    "type": "actions",
                    "children": [{"type": "link-button", "url": "https://example.com/dates", "label": "See Dates"}],
                },
            ],
        }
        result = card_to_whatsapp(card)
        assert result["type"] == "interactive"
        interactive = result["interactive"]
        assert interactive["type"] == "cta_url"
        assert interactive["body"]["text"] == "Dates subject to change\nTap the button below to see available dates."
        assert interactive["action"]["parameters"]["display_text"] == "See Dates"
        assert interactive["action"]["parameters"]["url"] == "https://example.com/dates"

    def test_should_truncate_long_cta_url_button_labels_to_20_chars(self):
        card = {
            "type": "card",
            "children": [
                {
                    "type": "actions",
                    "children": [
                        {
                            "type": "link-button",
                            "url": "https://example.com",
                            "label": "This is a very long CTA button label",
                        }
                    ],
                }
            ],
        }
        result = card_to_whatsapp(card)
        assert result["type"] == "interactive"
        interactive = result["interactive"]
        assert interactive["type"] == "cta_url"
        assert interactive["action"]["parameters"]["display_text"] == "This is a very long…"
        # No title, so no header; no body content, so the default body.
        assert "header" not in interactive
        assert interactive["body"]["text"] == "Open link"

    def test_should_fall_back_to_text_for_multiple_link_buttons(self):
        card = {
            "type": "card",
            "title": "Links only",
            "children": [
                {
                    "type": "actions",
                    "children": [
                        {"type": "link-button", "url": "https://example.com", "label": "Visit"},
                        {"type": "link-button", "url": "https://example.com/help", "label": "Help"},
                    ],
                }
            ],
        }
        result = card_to_whatsapp(card)
        assert result["type"] == "text"

    def test_should_not_promote_to_cta_url_when_allow_cta_url_is_false(self):
        card = {
            "type": "card",
            "title": "Links only",
            "children": [
                {
                    "type": "actions",
                    "children": [{"type": "link-button", "url": "https://example.com", "label": "Visit"}],
                }
            ],
        }
        result = card_to_whatsapp(card, allow_cta_url=False)
        assert result["type"] == "text"
        assert "Visit: https://example.com" in result["text"]

    def test_should_fall_back_to_text_when_the_link_url_is_not_http_s(self):
        card = {
            "type": "card",
            "children": [
                {
                    "type": "actions",
                    "children": [{"type": "link-button", "url": "mailto:hi@example.com", "label": "Email us"}],
                }
            ],
        }
        result = card_to_whatsapp(card)
        assert result["type"] == "text"
        assert "Email us: mailto:hi@example.com" in result["text"]

    def test_should_fall_back_to_text_when_the_link_label_is_blank(self):
        card = {
            "type": "card",
            "children": [
                {
                    "type": "actions",
                    "children": [{"type": "link-button", "url": "https://example.com", "label": "   "}],
                }
            ],
        }
        result = card_to_whatsapp(card)
        assert result["type"] == "text"

    def test_should_fall_back_to_text_when_the_card_has_a_header_image_url(self):
        card = {
            "type": "card",
            "title": "Receipt",
            "image_url": "https://cdn.example.com/receipt.png",
            "children": [
                {
                    "type": "actions",
                    "children": [{"type": "link-button", "url": "https://example.com/track", "label": "Track"}],
                }
            ],
        }
        result = card_to_whatsapp(card)
        assert result["type"] == "text"
        assert "https://cdn.example.com/receipt.png" in result["text"]

    def test_should_fall_back_to_text_when_the_card_contains_an_image_element(self):
        card = {
            "type": "card",
            "children": [
                {"type": "image", "url": "https://cdn.example.com/photo.png", "alt": "Photo"},
                {
                    "type": "actions",
                    "children": [{"type": "link-button", "url": "https://example.com", "label": "Visit"}],
                },
            ],
        }
        result = card_to_whatsapp(card)
        assert result["type"] == "text"
        assert "https://cdn.example.com/photo.png" in result["text"]

    def test_should_fall_back_to_text_when_the_card_contains_a_table(self):
        card = {
            "type": "card",
            "children": [
                {"type": "table", "headers": ["Item", "Qty"], "rows": [["Widget", "2"]]},
                {
                    "type": "actions",
                    "children": [{"type": "link-button", "url": "https://example.com", "label": "Visit"}],
                },
            ],
        }
        result = card_to_whatsapp(card)
        assert result["type"] == "text"

    def test_should_fall_back_to_text_when_a_select_accompanies_the_link_button(self):
        card = {
            "type": "card",
            "children": [
                {
                    "type": "actions",
                    "children": [
                        {
                            "type": "select",
                            "id": "size",
                            "label": "Size",
                            "options": [{"label": "Small", "value": "s"}, {"label": "Large", "value": "l"}],
                        },
                        {"type": "link-button", "url": "https://example.com", "label": "Docs"},
                    ],
                }
            ],
        }
        result = card_to_whatsapp(card)
        assert result["type"] == "text"

    def test_should_fall_back_to_text_when_a_second_actions_row_has_a_reply_button(self):
        card = {
            "type": "card",
            "children": [
                {
                    "type": "actions",
                    "children": [{"type": "link-button", "url": "https://example.com/docs", "label": "Docs"}],
                },
                {"type": "actions", "children": [{"type": "button", "id": "approve", "label": "Approve"}]},
            ],
        }
        result = card_to_whatsapp(card)
        assert result["type"] == "text"
        assert "Docs: https://example.com/docs" in result["text"]
        assert "[Approve]" in result["text"]

    def test_should_promote_a_link_button_nested_inside_a_section(self):
        card = {
            "type": "card",
            "children": [
                {
                    "type": "section",
                    "children": [
                        {"type": "text", "content": "More info"},
                        {
                            "type": "actions",
                            "children": [{"type": "link-button", "url": "https://example.com/info", "label": "Read"}],
                        },
                    ],
                }
            ],
        }
        result = card_to_whatsapp(card)
        assert result["type"] == "interactive"
        interactive = result["interactive"]
        assert interactive["type"] == "cta_url"
        assert interactive["body"]["text"] == "More info"
        assert interactive["action"]["parameters"] == {"display_text": "Read", "url": "https://example.com/info"}

    def test_should_keep_reply_button_path_when_mixed_with_a_link_button(self):
        card = {
            "type": "card",
            "title": "Choose",
            "children": [
                {"type": "text", "content": "Pick one"},
                {
                    "type": "actions",
                    "children": [
                        {"type": "button", "id": "approve", "label": "Approve"},
                        {"type": "link-button", "url": "https://example.com/docs", "label": "Docs"},
                    ],
                },
            ],
        }
        result = card_to_whatsapp(card)
        assert result["type"] == "interactive"
        interactive = result["interactive"]
        assert interactive["type"] == "button"
        assert interactive["body"]["text"] == "Pick one\nDocs: https://example.com/docs"
        buttons = interactive["action"]["buttons"]
        assert len(buttons) == 1
        assert buttons[0]["reply"]["id"] == encode_whatsapp_callback_data("approve", None)

    def test_fallback_to_text_without_actions(self):
        card = {
            "type": "card",
            "title": "Info only",
            "children": [{"type": "text", "content": "Just some info"}],
        }
        result = card_to_whatsapp(card)
        assert result["type"] == "text"

    def test_truncate_long_button_titles(self):
        card = {
            "type": "card",
            "children": [
                {"type": "text", "content": "Choose"},
                {
                    "type": "actions",
                    "children": [
                        {
                            "type": "button",
                            "id": "btn_long",
                            "label": "This is a very long button title that exceeds the limit",
                        }
                    ],
                },
            ],
        }
        result = card_to_whatsapp(card)
        assert result["type"] == "interactive"
        btn_title = result["interactive"]["action"]["buttons"][0]["reply"]["title"]
        assert len(btn_title) <= 20


class TestCtaUrlPythonSpecific:
    """Translation details of the cta_url port that upstream gets from JS."""

    def test_bom_only_label_is_blank_like_js_trim(self):
        # JS ``trim`` strips U+FEFF; Python's bare ``str.strip`` does not.
        card = {
            "type": "card",
            "children": [
                {
                    "type": "actions",
                    "children": [{"type": "link-button", "url": "https://example.com", "label": "\ufeff "}],
                }
            ],
        }
        assert card_to_whatsapp(card)["type"] == "text"

    def test_url_scheme_check_is_case_insensitive(self):
        card = {
            "type": "card",
            "children": [
                {
                    "type": "actions",
                    "children": [{"type": "link-button", "url": "HTTPS://example.com/x", "label": "Go"}],
                }
            ],
        }
        result = card_to_whatsapp(card)
        assert result["type"] == "interactive"
        assert result["interactive"]["action"]["parameters"]["url"] == "HTTPS://example.com/x"

    def test_card_link_button_lines_walks_every_actions_row_and_section(self):
        card = {
            "type": "card",
            "children": [
                {
                    "type": "actions",
                    "children": [
                        {"type": "button", "id": "a", "label": "A"},
                        {"type": "link-button", "url": "https://example.com/1", "label": "One"},
                    ],
                },
                {
                    "type": "section",
                    "children": [
                        {
                            "type": "actions",
                            "children": [{"type": "link-button", "url": "https://example.com/2", "label": "Two"}],
                        }
                    ],
                },
            ],
        }
        assert card_link_button_lines(card) == ["One: https://example.com/1", "Two: https://example.com/2"]


# ---------------------------------------------------------------------------
# cardToPlainText
# ---------------------------------------------------------------------------


class TestCardToPlainText:
    """Tests for card_to_plain_text."""

    def test_plain_text_from_card(self):
        card = {
            "type": "card",
            "title": "Hello",
            "subtitle": "World",
            "children": [
                {"type": "text", "content": "Some content"},
                {
                    "type": "fields",
                    "children": [{"type": "field", "label": "Key", "value": "Value"}],
                },
            ],
        }
        result = card_to_plain_text(card)
        assert "Hello" in result
        assert "World" in result
        assert "Some content" in result
        assert "Key: Value" in result


# ---------------------------------------------------------------------------
# encode/decode WhatsApp callback data
# ---------------------------------------------------------------------------


class TestWhatsAppCallbackData:
    """Tests for encode/decode WhatsApp callback data."""

    def test_encode_action_only(self):
        result = encode_whatsapp_callback_data("my_action")
        assert result == 'chat:{"a":"my_action"}'

    def test_encode_action_and_value(self):
        result = encode_whatsapp_callback_data("my_action", "some_value")
        assert result == 'chat:{"a":"my_action","v":"some_value"}'

    def test_decode_encoded_data(self):
        encoded = encode_whatsapp_callback_data("my_action", "some_value")
        result = decode_whatsapp_callback_data(encoded)
        assert result["action_id"] == "my_action"
        assert result["value"] == "some_value"

    def test_decode_action_only(self):
        encoded = encode_whatsapp_callback_data("my_action")
        result = decode_whatsapp_callback_data(encoded)
        assert result["action_id"] == "my_action"
        assert result["value"] is None

    def test_decode_non_prefixed_passthrough(self):
        result = decode_whatsapp_callback_data("raw_id")
        assert result["action_id"] == "raw_id"
        assert result["value"] == "raw_id"

    def test_decode_undefined_data(self):
        result = decode_whatsapp_callback_data(None)
        assert result["action_id"] == "whatsapp_callback"
        assert result["value"] is None

    def test_decode_malformed_json(self):
        result = decode_whatsapp_callback_data("chat:not-json")
        assert result["action_id"] == "chat:not-json"
        assert result["value"] == "chat:not-json"
