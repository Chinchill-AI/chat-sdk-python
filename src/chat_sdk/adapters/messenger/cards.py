"""Convert CardElement to Messenger templates or text fallback.

Messenger supports two template types for buttons:
- Generic Template: title, subtitle, image, up to 3 buttons
- Button Template: text with up to 3 buttons (no image)

Cards that exceed constraints fall back to formatted text messages.

See:
- https://developers.facebook.com/docs/messenger-platform/send-messages/template/generic/
- https://developers.facebook.com/docs/messenger-platform/send-messages/buttons/

Ported from upstream ``adapter-messenger/src/cards.ts``.
"""

from __future__ import annotations

import json
from typing import Literal, TypedDict, cast

from chat_sdk.adapters.messenger.types import (
    MessengerButton,
    MessengerButtonTemplatePayload,
    MessengerGenericTemplatePayload,
    MessengerTemplateElement,
    MessengerTemplatePayload,
)
from chat_sdk.cards import (
    ActionsElement,
    ButtonElement,
    CardChild,
    CardElement,
    FieldsElement,
    LinkButtonElement,
    LinkElement,
    TableElement,
    TextElement,
)

CALLBACK_DATA_PREFIX = "chat:"

# Maximum number of buttons Messenger allows per template
MAX_BUTTONS = 3

# Maximum character length for a button title
MAX_BUTTON_TITLE_LENGTH = 20

# Maximum character length for subtitle in Generic Template
MAX_SUBTITLE_LENGTH = 80

# Maximum character length for text in Button Template
MAX_BUTTON_TEMPLATE_TEXT_LENGTH = 640

# Maximum character length for title in Generic Template
MAX_TITLE_LENGTH = 80

# Maximum character length for a plain text message
MAX_TEXT_LENGTH = 2000


class _MessengerCardActionPayload(TypedDict, total=False):
    a: str
    v: str


class MessengerCardResultTemplate(TypedDict):
    """Template card result."""

    type: Literal["template"]
    payload: MessengerTemplatePayload


class MessengerCardResultText(TypedDict):
    """Text fallback card result."""

    type: Literal["text"]
    text: str


MessengerCardResult = MessengerCardResultTemplate | MessengerCardResultText


def encode_messenger_callback_data(action_id: str, value: str | None = None) -> str:
    """Encode an action ID and optional value into a callback data string.

    Format: ``"chat:{json}"`` where json is ``{"a": action_id, "v"?: value}``.
    """
    payload: _MessengerCardActionPayload = {"a": action_id}
    if isinstance(value, str):
        payload["v"] = value
    return f"{CALLBACK_DATA_PREFIX}{json.dumps(payload, separators=(',', ':'))}"


def decode_messenger_callback_data(data: str | None = None) -> dict[str, str | None]:
    """Decode callback data from a Messenger postback.

    Returns a dict with ``action_id`` and ``value`` keys.
    """
    if not data:
        return {"action_id": "messenger_callback", "value": None}

    # Divergence-candidate (see #110): passthrough for legacy or
    # externally-generated payloads that don't use the chat: prefix -- upstream
    # treats the raw string as BOTH action_id and value. Mirrored exactly here;
    # do not tighten without resolving #110.
    if not data.startswith(CALLBACK_DATA_PREFIX):
        return {"action_id": data, "value": data}

    try:
        decoded = json.loads(data[len(CALLBACK_DATA_PREFIX) :])

        if isinstance(decoded.get("a"), str) and decoded["a"]:
            return {
                "action_id": decoded["a"],
                "value": decoded["v"] if isinstance(decoded.get("v"), str) else None,
            }
    except (json.JSONDecodeError, AttributeError, KeyError, TypeError):
        # Malformed JSON after prefix -- fall back to passthrough.
        pass

    # Divergence-candidate (see #110): same passthrough as non-prefixed data --
    # raw string as both fields. Mirrors upstream exactly.
    return {"action_id": data, "value": data}


def card_to_messenger(card: CardElement) -> MessengerCardResult:
    """Convert a CardElement to a Messenger message payload.

    If the card has action buttons that fit Messenger's constraints
    (max 3 buttons, titles max 20 chars), produces a template message.
    Otherwise, produces a text fallback.
    """
    children = card.get("children", [])

    # Check for unsupported elements that force text fallback
    if _has_unsupported_elements(children):
        return {"type": "text", "text": card_to_messenger_text(card)}

    actions = _find_actions(children)
    buttons = _extract_buttons(actions) if actions else None

    # If we have valid buttons within constraints
    if buttons and 0 < len(buttons) <= MAX_BUTTONS:
        # Check if any button title exceeds the limit
        all_buttons_fit = all(len(btn.get("title", "")) <= MAX_BUTTON_TITLE_LENGTH for btn in buttons)

        if all_buttons_fit:
            # Use Generic Template if card has title or image
            if card.get("title") or card.get("image_url"):
                return {
                    "type": "template",
                    "payload": _build_generic_template(card, buttons),
                }

            # Use Button Template for text-only cards with buttons
            body_text = _build_body_text(card)
            if body_text:
                return {
                    "type": "template",
                    "payload": _build_button_template(body_text, buttons),
                }

    # Fallback to text
    return {"type": "text", "text": card_to_messenger_text(card)}


def card_to_messenger_text(card: CardElement) -> str:
    """Convert a CardElement to Messenger-formatted plain text.

    Used as fallback when templates can't represent the card.
    Messenger doesn't support markdown formatting in regular messages.
    """
    lines: list[str] = []

    title = card.get("title")
    subtitle = card.get("subtitle")
    children = card.get("children", [])
    image_url = card.get("image_url")

    if title:
        lines.append(title)

    if subtitle:
        lines.append(subtitle)

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


# =============================================================================
# Private helpers
# =============================================================================


def _build_generic_template(card: CardElement, buttons: list[MessengerButton]) -> MessengerTemplatePayload:
    """Build a Generic Template payload."""
    body_text = _build_body_text(card)
    title = card.get("title") or body_text or "Menu"
    # Only add subtitle if it provides new information (not duplicating title)
    subtitle = card.get("subtitle") or (body_text if (card.get("title") and body_text) else None)

    element: MessengerTemplateElement = {"title": _truncate(title, MAX_TITLE_LENGTH)}
    if subtitle:
        element["subtitle"] = _truncate(subtitle, MAX_SUBTITLE_LENGTH)
    image_url = card.get("image_url")
    if image_url:
        element["image_url"] = image_url
    element["buttons"] = buttons

    payload: MessengerGenericTemplatePayload = {
        "template_type": "generic",
        "elements": [element],
    }
    return payload


def _build_button_template(text: str, buttons: list[MessengerButton]) -> MessengerTemplatePayload:
    """Build a Button Template payload."""
    payload: MessengerButtonTemplatePayload = {
        "template_type": "button",
        "text": _truncate(text, MAX_BUTTON_TEMPLATE_TEXT_LENGTH),
        "buttons": buttons,
    }
    return payload


def _has_unsupported_elements(children: list[CardChild]) -> bool:
    """Check if children contain elements that can't be represented in templates."""
    for child in children:
        child_type = child.get("type")
        if child_type == "table":
            return True
        if child_type == "section" and _has_unsupported_elements(child.get("children", [])):  # type: ignore[union-attr]
            return True
        if child_type == "actions":
            for action in child.get("children", []):  # type: ignore[union-attr]
                if action.get("type") in ("select", "radio_select"):
                    return True
    return False


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


def _extract_buttons(actions: ActionsElement) -> list[MessengerButton] | None:
    """Extract Messenger buttons from an ActionsElement.

    Converts SDK Button to ``postback`` and LinkButton to ``web_url``.
    """
    buttons: list[MessengerButton] = []

    for child in actions.get("children", []):
        if child.get("type") == "button" and child.get("id"):
            buttons.append(_convert_button(child))
        elif child.get("type") == "link-button":
            buttons.append(_convert_link_button(child))

    if len(buttons) == 0:
        return None

    # Messenger allows max 3 buttons -- take the first 3
    return buttons[:MAX_BUTTONS]


def _convert_button(button: ButtonElement) -> MessengerButton:
    """Convert an SDK Button to a Messenger postback button."""
    return {
        "type": "postback",
        "title": _truncate(button.get("label", ""), MAX_BUTTON_TITLE_LENGTH),
        "payload": encode_messenger_callback_data(button.get("id", ""), button.get("value")),
    }


def _convert_link_button(button: LinkButtonElement) -> MessengerButton:
    """Convert an SDK LinkButton to a Messenger web_url button."""
    return {
        "type": "web_url",
        "title": _truncate(button.get("label", ""), MAX_BUTTON_TITLE_LENGTH),
        "url": button.get("url", ""),
    }


def _build_body_text(card: CardElement) -> str:
    """Build body text from card content (excluding actions)."""
    parts: list[str] = []

    for child in card.get("children", []):
        if child.get("type") == "actions":
            continue
        text = _child_to_plain_text(child)
        if text:
            parts.append(text)

    return "\n".join(parts)


def _render_child(child: CardChild) -> list[str]:
    """Render a card child to text lines."""
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
        alt = cast("str", child.get("alt", ""))
        url = cast("str", child.get("url", ""))
        if alt:
            return [f"{alt}: {url}"]
        return [url]

    if child_type == "divider":
        return ["---"]

    if child_type == "link":
        return [f"{child.get('label', '')}: {child.get('url', '')}"]

    if child_type == "table":
        return _render_table(child)  # type: ignore[arg-type]

    return []


def _render_text(text: TextElement) -> list[str]:
    """Render text element."""
    return [text.get("content", "")]


def _render_fields(fields: FieldsElement) -> list[str]:
    """Render fields as ``Label: Value`` lines."""
    return [f"{f['label']}: {f['value']}" for f in fields.get("children", [])]


def _render_actions(actions: ActionsElement) -> list[str]:
    """Render actions as button labels for text fallback."""
    button_texts: list[str] = []
    for button in actions.get("children", []):
        if button.get("type") == "link-button":
            button_texts.append(f"{button.get('label', '')}: {button.get('url', '')}")
        else:
            # Buttons, selects, and radio selects all render as bracketed labels
            button_texts.append(f"[{button.get('label', '')}]")

    return [" | ".join(button_texts)]


def _render_table(table: TableElement) -> list[str]:
    """Render a table as ASCII text."""
    lines: list[str] = []

    headers = table.get("headers", [])
    if len(headers) > 0:
        lines.append(" | ".join(headers))
        lines.append(" | ".join("---" for _ in headers))

    for row in table.get("rows", []):
        lines.append(" | ".join(row))

    return lines


def _child_to_plain_text(child: CardChild) -> str | None:
    """Convert a card child to plain text."""
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

    if child_type == "link":
        link = cast("LinkElement", child)
        return f"{link.get('label', '')}: {link.get('url', '')}"

    return None


def _truncate(text: str, max_length: int) -> str:
    """Truncate text to a maximum length, adding ellipsis if needed."""
    if len(text) <= max_length:
        return text
    return f"{text[: max_length - 1]}…"
