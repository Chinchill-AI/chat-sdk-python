"""Telegram card conversion utilities."""

from __future__ import annotations

import json

from chat_sdk.adapters.telegram.types import (
    TelegramInlineKeyboardButton,
    TelegramInlineKeyboardMarkup,
)
from chat_sdk.cards import ActionsElement, CardChild, CardElement
from chat_sdk.emoji import convert_emoji_placeholders
from chat_sdk.shared import ValidationError

CALLBACK_DATA_PREFIX = "chat:"
TELEGRAM_CALLBACK_DATA_LIMIT_BYTES = 64


def _convert_label(label: str) -> str:
    """Convert emoji placeholders in a button label to gchat format."""
    return convert_emoji_placeholders(label, "gchat")


def _to_inline_keyboard_row(
    actions: ActionsElement,
) -> list[TelegramInlineKeyboardButton]:
    """Convert an ActionsElement to a row of inline keyboard buttons."""
    row: list[TelegramInlineKeyboardButton] = []

    for action in actions.get("children", []):
        action_type = action.get("type") if isinstance(action, dict) else None

        if action_type == "button":
            row.append(
                TelegramInlineKeyboardButton(
                    text=_convert_label(action.get("label", "")),
                    callback_data=encode_telegram_callback_data(action.get("id", ""), action.get("value")),
                )
            )
            continue

        if action_type == "link-button":
            row.append(
                TelegramInlineKeyboardButton(
                    text=_convert_label(action.get("label", "")),
                    url=action.get("url", ""),
                )
            )

    return row


def _collect_inline_keyboard_rows(
    children: list[CardChild],
    rows: list[list[TelegramInlineKeyboardButton]],
) -> None:
    """Recursively collect inline keyboard rows from card children."""
    for child in children:
        child_type = child.get("type") if isinstance(child, dict) else None

        if child_type == "actions":
            row = _to_inline_keyboard_row(child)  # type: ignore[arg-type]
            if row:
                rows.append(row)
            continue

        if child_type == "section":
            _collect_inline_keyboard_rows(
                child.get("children", []),  # type: ignore[union-attr]
                rows,
            )


def card_to_telegram_inline_keyboard(
    card: CardElement,
) -> TelegramInlineKeyboardMarkup | None:
    """Convert a CardElement to a Telegram inline keyboard markup.

    Returns None if there are no actionable buttons in the card.
    """
    rows: list[list[TelegramInlineKeyboardButton]] = []
    _collect_inline_keyboard_rows(card.get("children", []), rows)
    if not rows:
        return None

    return TelegramInlineKeyboardMarkup(inline_keyboard=rows)


def empty_telegram_inline_keyboard() -> TelegramInlineKeyboardMarkup:
    """Return an empty inline keyboard markup (used to clear keyboards)."""
    return TelegramInlineKeyboardMarkup(inline_keyboard=[])


def encode_telegram_callback_data(action_id: str, value: str | None = None) -> str:
    """Encode an action ID and optional value into Telegram callback data.

    Raises ValidationError if the encoded payload exceeds Telegram's 64-byte limit.
    """
    payload: dict[str, str] = {"a": action_id}
    if isinstance(value, str):
        payload["v"] = value

    callback_data = f"{CALLBACK_DATA_PREFIX}{json.dumps(payload, separators=(',', ':'))}"
    if len(callback_data.encode("utf-8")) > TELEGRAM_CALLBACK_DATA_LIMIT_BYTES:
        raise ValidationError(
            "telegram",
            f"Callback payload too large for Telegram (max {TELEGRAM_CALLBACK_DATA_LIMIT_BYTES} bytes).",
        )

    return callback_data


def decode_telegram_callback_data(
    data: str | None,
) -> dict[str, str | None]:
    """Decode Telegram callback data into action_id and value.

    Returns a dict with 'action_id' and 'value' keys.
    """
    if not data:
        return {"action_id": "telegram_callback", "value": None}

    if not data.startswith(CALLBACK_DATA_PREFIX):
        return {"action_id": data, "value": data}

    try:
        decoded = json.loads(data[len(CALLBACK_DATA_PREFIX) :])

        if isinstance(decoded, dict) and isinstance(decoded.get("a"), str) and decoded["a"]:
            return {
                "action_id": decoded["a"],
                "value": decoded.get("v") if isinstance(decoded.get("v"), str) else None,
            }
    except (json.JSONDecodeError, KeyError, TypeError):
        # Fall back to legacy passthrough behavior below.
        pass

    return {"action_id": data, "value": data}
