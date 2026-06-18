"""Port of adapter-twilio/src/cards.test.ts -- SMS card fallback rendering."""

from __future__ import annotations

from chat_sdk.adapters.twilio.cards import card_to_twilio_text
from chat_sdk.cards import CardElement


def _deploy_card() -> CardElement:
    return {
        "type": "card",
        "title": "Deploy",
        "children": [
            {
                "type": "section",
                "children": [
                    {"type": "text", "content": "Approve production deploy?"},
                    {
                        "type": "fields",
                        "children": [
                            {"type": "field", "label": "version", "value": "1.2.3"},
                        ],
                    },
                ],
            },
            {
                "type": "actions",
                "children": [
                    {"type": "button", "id": "approve", "label": "Approve"},
                ],
            },
        ],
    }


class TestCardToTwilioText:
    """Tests for card_to_twilio_text."""

    def test_renders_cards_as_plain_sms_fallback_text(self):
        text = card_to_twilio_text(_deploy_card())
        assert "Deploy" in text
        assert "Approve production deploy?" in text
        assert "version: 1.2.3" in text
        assert "[Approve]" not in text

    def test_strips_bold_markers_from_titles(self):
        # The shared fallback emits "*Deploy*"; SMS clients render asterisks
        # literally, so the Twilio variant must strip them entirely.
        text = card_to_twilio_text(_deploy_card())
        assert "*" not in text
        assert text.startswith("Deploy")
