"""Convert CardElement to WhatsApp interactive messages or text fallback.

WhatsApp supports two types of interactive messages:
- Reply buttons: up to 3 buttons (title max 20 chars)
- List messages: up to 10 rows across sections (title max 24 chars)

Cards that exceed these limits fall back to formatted text messages.

See: https://developers.facebook.com/docs/whatsapp/cloud-api/messages/interactive-messages
"""

from __future__ import annotations

import json
from typing import Any, Literal, TypedDict, Union

from chat_sdk.adapters.whatsapp.types import WhatsAppInteractiveMessage
from chat_sdk.cards import (
    ActionsElement,
    ButtonElement,
    CardChild,
    CardElement,
    FieldsElement,
    TextElement,
)

CALLBACK_DATA_PREFIX = "chat:"

# Maximum number of reply buttons WhatsApp allows
MAX_REPLY_BUTTONS = 3

# Maximum character length for a button title
MAX_BUTTON_TITLE_LENGTH = 20

# Maximum character length for the body text
MAX_BODY_LENGTH = 1024


class _WhatsAppCardActionPayload(TypedDict, total=False):
    a: str
    v: str


class WhatsAppCardResultInteractive(TypedDict):
    """Interactive card result."""

    type: Literal["interactive"]
    interactive: WhatsAppInteractiveMessage


class WhatsAppCardResultText(TypedDict):
    """Text fallback card result."""

    type: Literal["text"]
    text: str


WhatsAppCardResult = Union[WhatsAppCardResultInteractive, WhatsAppCardResultText]


def encode_whatsapp_callback_data(action_id: str, value: str | None = None) -> str:
    """Encode an action ID and optional value into a callback data string.

    Format: "chat:{json}" where json is {"a": action_id, "v"?: value}
    """
    payload: dict[str, str] = {"a": action_id}
    if isinstance(value, str):
        payload["v"] = value
    return f"{CALLBACK_DATA_PREFIX}{json.dumps(payload)}"


def decode_whatsapp_callback_data(data: str | None = None) -> dict[str, str | None]:
    """Decode callback data from a WhatsApp interactive reply.

    Returns dict with 'action_id' and 'value' keys.
    """
    if not data:
        return {"action_id": "whatsapp_callback", "value": None}

    # Passthrough for legacy or externally-generated button IDs that don't
    # use the chat: prefix -- treat the raw string as both action_id and value.
    if not data.startswith(CALLBACK_DATA_PREFIX):
        return {"action_id": data, "value": data}

    try:
        decoded = json.loads(data[len(CALLBACK_DATA_PREFIX) :])

        if isinstance(decoded.get("a"), str) and decoded["a"]:
            return {
                "action_id": decoded["a"],
                "value": decoded["v"] if isinstance(decoded.get("v"), str) else None,
            }
    except (json.JSONDecodeError, KeyError, TypeError):
        # Malformed JSON after prefix -- fall back to passthrough.
        pass

    # Same passthrough as non-prefixed data: treat raw string as both fields.
    return {"action_id": data, "value": data}


def card_to_whatsapp(card: CardElement) -> WhatsAppCardResult:
    """Convert a CardElement to a WhatsApp message payload.

    If the card has action buttons that fit WhatsApp's constraints
    (max 3 buttons, titles max 20 chars), produces an interactive
    button message. Otherwise, produces a text fallback.
    """
    actions = _find_actions(card.get("children", []))
    action_buttons = _extract_reply_buttons(actions) if actions else None

    # If we have valid buttons, produce an interactive message
    if action_buttons and len(action_buttons) > 0:
        body_text = _build_body_text(card)

        interactive: dict[str, Any] = {
            "type": "button",
            "body": {
                "text": _truncate(body_text or "Please choose an option", MAX_BODY_LENGTH),
            },
            "action": {
                "buttons": [
                    {
                        "type": "reply",
                        "reply": {
                            "id": encode_whatsapp_callback_data(btn.get("id", ""), btn.get("value")),
                            "title": _truncate(btn.get("label", ""), MAX_BUTTON_TITLE_LENGTH),
                        },
                    }
                    for btn in action_buttons
                ],
            },
        }

        title = card.get("title")
        if title:
            interactive["header"] = {"type": "text", "text": _truncate(title, 60)}

        return {"type": "interactive", "interactive": interactive}  # type: ignore[typeddict-item]

    # Fallback to text
    return {"type": "text", "text": card_to_whatsapp_text(card)}


def card_to_whatsapp_text(card: CardElement) -> str:
    """Convert a CardElement to WhatsApp-formatted text.

    Used as fallback when interactive messages can't represent the card.
    Uses WhatsApp markdown: *bold*, _italic_, ~strikethrough~.
    """
    lines: list[str] = []

    title = card.get("title")
    subtitle = card.get("subtitle")
    children = card.get("children", [])
    image_url = card.get("image_url")

    if title:
        lines.append(f"*{_escape_whatsapp(title)}*")

    if subtitle:
        lines.append(_escape_whatsapp(subtitle))

    if (title or subtitle) and len(children) > 0:
        lines.append("")

    if image_url:
        lines.append(image_url)
        lines.append("")

    for i, child in enumerate(children):
        child_lines = _render_child(child)

        if len(child_lines) > 0:
            lines.extend(child_lines)

            if i < len(children) - 1:
                lines.append("")

    return "\n".join(lines)


def card_to_plain_text(card: CardElement) -> str:
    """Generate plain text fallback from a card (no formatting)."""
    parts: list[str] = []

    title = card.get("title")
    subtitle = card.get("subtitle")

    if title:
        parts.append(title)

    if subtitle:
        parts.append(subtitle)

    for child in card.get("children", []):
        text = _child_to_plain_text(child)
        if text:
            parts.append(text)

    return "\n".join(parts)


# =============================================================================
# Private helpers
# =============================================================================


def _render_child(child: CardChild) -> list[str]:
    child_type = child.get("type", "")

    if child_type == "text":
        return _render_text(child)  # type: ignore[arg-type]

    if child_type == "fields":
        return _render_fields(child)  # type: ignore[arg-type]

    if child_type == "actions":
        return _render_actions(child)  # type: ignore[arg-type]

    if child_type == "section":
        result: list[str] = []
        for c in child.get("children", []):  # type: ignore[union-attr]
            result.extend(_render_child(c))
        return result

    if child_type == "image":
        alt = child.get("alt", "")  # type: ignore[union-attr]
        url = child.get("url", "")  # type: ignore[union-attr]
        if alt:
            return [f"{alt}: {url}"]
        return [url]

    if child_type == "divider":
        return ["---"]

    return []


def _render_text(text: TextElement) -> list[str]:
    style = text.get("style", "")
    content = text.get("content", "")

    if style == "bold":
        return [f"*{_escape_whatsapp(content)}*"]
    if style == "muted":
        return [f"_{_escape_whatsapp(content)}_"]
    return [_escape_whatsapp(content)]


def _render_fields(fields: FieldsElement) -> list[str]:
    return [f"*{_escape_whatsapp(f['label'])}:* {_escape_whatsapp(f['value'])}" for f in fields.get("children", [])]


def _render_actions(actions: ActionsElement) -> list[str]:
    button_texts: list[str] = []
    for button in actions.get("children", []):
        if button.get("type") == "link-button":
            button_texts.append(f"{_escape_whatsapp(button.get('label', ''))}: {button.get('url', '')}")
        else:
            button_texts.append(f"[{_escape_whatsapp(button.get('label', ''))}]")

    return [" | ".join(button_texts)]


def _child_to_plain_text(child: CardChild) -> str | None:
    child_type = child.get("type", "")

    if child_type == "text":
        return child.get("content", "")  # type: ignore[union-attr]

    if child_type == "fields":
        return "\n".join(
            f"{f['label']}: {f['value']}"
            for f in child.get("children", [])  # type: ignore[union-attr]
        )

    if child_type == "actions":
        return None

    if child_type == "section":
        parts = [
            _child_to_plain_text(c)
            for c in child.get("children", [])  # type: ignore[union-attr]
        ]
        return "\n".join(p for p in parts if p)

    return None


def _find_actions(children: list[CardChild]) -> ActionsElement | None:
    """Find the first ActionsElement in a list of card children."""
    for child in children:
        if child.get("type") == "actions":
            return child  # type: ignore[return-value]
        if child.get("type") == "section":
            nested = _find_actions(child.get("children", []))  # type: ignore[union-attr]
            if nested:
                return nested
    return None


def _extract_reply_buttons(actions: ActionsElement) -> list[ButtonElement] | None:
    """Extract reply buttons from an ActionsElement, only if they fit
    WhatsApp constraints (max 3 buttons, each with an ID).
    """
    buttons: list[ButtonElement] = []

    for child in actions.get("children", []):
        if child.get("type") == "button" and child.get("id"):
            buttons.append(child)
        # Link buttons can't be WhatsApp reply buttons -- skip them

    if len(buttons) == 0:
        return None

    # WhatsApp allows max 3 reply buttons -- take the first 3
    return buttons[:MAX_REPLY_BUTTONS]


def _build_body_text(card: CardElement) -> str:
    """Build body text from card content (excluding actions)."""
    parts: list[str] = []

    subtitle = card.get("subtitle")
    if subtitle:
        parts.append(subtitle)

    for child in card.get("children", []):
        if child.get("type") == "actions":
            continue
        text = _child_to_plain_text(child)
        if text:
            parts.append(text)

    return "\n".join(parts)


def _escape_whatsapp(text: str) -> str:
    """Escape WhatsApp formatting characters."""
    return text.replace("\\", "\\\\").replace("*", "\\*").replace("_", "\\_").replace("~", "\\~").replace("`", "\\`")


def _truncate(text: str, max_length: int) -> str:
    """Truncate text to a maximum length, adding ellipsis if needed."""
    if len(text) <= max_length:
        return text
    return f"{text[: max_length - 1]}\u2026"
