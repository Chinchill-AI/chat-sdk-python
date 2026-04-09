"""Port of adapter-telegram/src/cards.test.ts -- Telegram card rendering tests.

Tests cardToTelegramInlineKeyboard, encode/decode callback data,
and emptyTelegramInlineKeyboard.
"""

from __future__ import annotations

import pytest

from chat_sdk.adapters.telegram.cards import (
    card_to_telegram_inline_keyboard,
    decode_telegram_callback_data,
    empty_telegram_inline_keyboard,
    encode_telegram_callback_data,
)
from chat_sdk.shared.errors import ValidationError

# ---------------------------------------------------------------------------
# cardToTelegramInlineKeyboard
# ---------------------------------------------------------------------------


class TestCardToTelegramInlineKeyboard:
    """Tests for card_to_telegram_inline_keyboard."""

    def test_no_actions_returns_none(self):
        keyboard = card_to_telegram_inline_keyboard(
            {
                "type": "card",
                "title": "No actions",
                "children": [{"type": "text", "content": "hi"}],
            }
        )
        assert keyboard is None

    def test_multiple_action_blocks(self):
        keyboard = card_to_telegram_inline_keyboard(
            {
                "type": "card",
                "children": [
                    {
                        "type": "actions",
                        "children": [
                            {"type": "button", "id": "a", "label": "A"},
                            {"type": "button", "id": "b", "label": "B"},
                        ],
                    },
                    {
                        "type": "section",
                        "children": [
                            {
                                "type": "actions",
                                "children": [
                                    {
                                        "type": "link-button",
                                        "label": "Docs",
                                        "url": "https://chat-sdk.dev",
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        )
        assert keyboard is not None
        rows = keyboard["inline_keyboard"]
        assert len(rows) == 2
        # First row: 2 callback buttons
        assert len(rows[0]) == 2
        assert rows[0][0]["text"] == "A"
        assert rows[0][0]["callback_data"] == encode_telegram_callback_data("a")
        assert rows[0][1]["text"] == "B"
        assert rows[0][1]["callback_data"] == encode_telegram_callback_data("b")
        # Second row: link button
        assert len(rows[1]) == 1
        assert rows[1][0]["text"] == "Docs"
        assert rows[1][0]["url"] == "https://chat-sdk.dev"

    def test_ignores_unsupported_controls(self):
        keyboard = card_to_telegram_inline_keyboard(
            {
                "type": "card",
                "children": [
                    {
                        "type": "actions",
                        "children": [
                            {
                                "type": "select",
                                "id": "priority",
                                "label": "Priority",
                                "options": [{"label": "High", "value": "high"}],
                            }
                        ],
                    }
                ],
            }
        )
        assert keyboard is None


# ---------------------------------------------------------------------------
# Callback payload encoding/decoding
# ---------------------------------------------------------------------------


class TestTelegramCallbackPayloadEncoding:
    """Tests for encode/decode Telegram callback data."""

    def test_encode_and_decode_with_value(self):
        encoded = encode_telegram_callback_data("approve", "request-123")
        decoded = decode_telegram_callback_data(encoded)
        assert decoded == {"action_id": "approve", "value": "request-123"}

    def test_decode_empty_returns_fallback(self):
        decoded = decode_telegram_callback_data(None)
        assert decoded == {"action_id": "telegram_callback", "value": None}

    def test_decode_malformed_falls_back(self):
        decoded = decode_telegram_callback_data("chat:{not-json")
        assert decoded == {"action_id": "chat:{not-json", "value": "chat:{not-json"}

    def test_decode_non_encoded_passthrough(self):
        decoded = decode_telegram_callback_data("legacy_action")
        assert decoded == {"action_id": "legacy_action", "value": "legacy_action"}

    def test_encode_exceeds_limit_raises(self):
        very_long = "x" * 200
        with pytest.raises(ValidationError):
            encode_telegram_callback_data(very_long)


# ---------------------------------------------------------------------------
# emptyTelegramInlineKeyboard
# ---------------------------------------------------------------------------


class TestEmptyTelegramInlineKeyboard:
    """Tests for empty_telegram_inline_keyboard."""

    def test_returns_empty_keyboard(self):
        result = empty_telegram_inline_keyboard()
        assert result == {"inline_keyboard": []}
