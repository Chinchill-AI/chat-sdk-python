"""Convert CardElement to WhatsApp interactive messages or text fallback.

WhatsApp supports interactive messages including:
- Reply buttons: up to 3 buttons (title max 20 chars)
- List messages: up to 10 rows across sections (title max 24 chars)
- CTA URL: a single link button mapped to interactive.type "cta_url"

Cards that exceed these limits fall back to formatted text messages.

See: https://developers.facebook.com/docs/whatsapp/cloud-api/messages/interactive-messages
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal, TypedDict, cast

from chat_sdk.adapters.whatsapp.types import WhatsAppInteractiveMessage
from chat_sdk.cards import (
    ActionsElement,
    ButtonElement,
    CardChild,
    CardElement,
    FieldsElement,
    LinkButtonElement,
    TextElement,
)
from chat_sdk.shared._js_compat import JS_WHITESPACE

CALLBACK_DATA_PREFIX = "chat:"

# cta_url URLs must be web links -- Meta rejects other schemes.
# ``re.match`` anchors at the start like JS ``/^https?:\/\//i``. ``re.ASCII``
# matters: without it Python's IGNORECASE folds U+017F (long s) to ``s``, which
# a JS regex without the ``u`` flag never does.
_HTTP_URL_REGEX = re.compile(r"https?://", re.IGNORECASE | re.ASCII)

# Maximum number of reply buttons WhatsApp allows
MAX_REPLY_BUTTONS = 3

# Maximum character length for a button title
MAX_BUTTON_TITLE_LENGTH = 20

# Maximum character length for the body text
MAX_BODY_LENGTH = 1024

# Maximum character length for a text header
MAX_HEADER_LENGTH = 60


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


WhatsAppCardResult = WhatsAppCardResultInteractive | WhatsAppCardResultText


def encode_whatsapp_callback_data(action_id: str, value: str | None = None) -> str:
    """Encode an action ID and optional value into a callback data string.

    Format: "chat:{json}" where json is {"a": action_id, "v"?: value}
    """
    payload: dict[str, str] = {"a": action_id}
    if isinstance(value, str):
        payload["v"] = value
    return f"{CALLBACK_DATA_PREFIX}{json.dumps(payload, separators=(',', ':'))}"


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


def card_to_whatsapp(card: CardElement, *, allow_cta_url: bool = True) -> WhatsAppCardResult:
    """Convert a CardElement to a WhatsApp message payload.

    If the card has action buttons that fit WhatsApp's constraints
    (max 3 buttons, titles max 20 chars), produces an interactive
    button message. A card whose only interactive element is a single
    LinkButton with an http(s) URL -- and whose content the interactive
    body can carry without loss -- becomes a native ``cta_url`` message.
    Otherwise, produces a text fallback.

    ``allow_cta_url=False`` disables the ``cta_url`` promotion. The media
    path uses it: there the text fallback doubles as the media caption, and
    an extra interactive send would change the delivery shape.
    """
    actions = _find_actions(card.get("children", []))
    action_buttons = _extract_reply_buttons(actions) if actions else None

    if action_buttons is not None:
        # Link buttons can't be WhatsApp reply buttons and cta_url can't mix
        # with them -- keep their URLs reachable by appending them to the body.
        body_text = "\n".join(part for part in [_build_body_text(card), *card_link_button_lines(card)] if part)

        interactive: dict[str, Any] = {
            "type": "button",
            **_build_interactive_envelope(card, body_text or "Please choose an option"),
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
        return {"type": "interactive", "interactive": cast(WhatsAppInteractiveMessage, interactive)}

    if allow_cta_url:
        cta_link = _find_promotable_cta_link(card)
        if cta_link is not None:
            cta_interactive: dict[str, Any] = {
                "type": "cta_url",
                **_build_interactive_envelope(card, _build_body_text(card) or "Open link"),
                "action": {
                    "name": "cta_url",
                    "parameters": {
                        "display_text": _truncate(cta_link["label"], MAX_BUTTON_TITLE_LENGTH),
                        "url": cta_link["url"],
                    },
                },
            }
            return {"type": "interactive", "interactive": cast(WhatsAppInteractiveMessage, cta_interactive)}

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


def card_link_button_lines(card: CardElement) -> list[str]:
    """Render ``"Label: url"`` lines for every LinkButton in a card.

    Includes link buttons nested inside sections. Caption/fallback text
    excludes action elements, so these lines keep link URLs reachable.
    """
    return [
        f"{link.get('label', '')}: {link.get('url', '')}" for link in _collect_link_buttons(card.get("children", []))
    ]


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
        alt = cast("str", child.get("alt", ""))
        url = cast("str", child.get("url", ""))
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

    Returns ``None`` (never ``[]``) when there are no reply buttons, so the
    caller's ``is not None`` check matches upstream's ``if (actionButtons)``.
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


def _build_interactive_envelope(card: CardElement, body_text: str) -> dict[str, Any]:
    """Build the header/body envelope shared by all interactive messages."""
    envelope: dict[str, Any] = {}
    title = card.get("title")
    if title:
        envelope["header"] = {"type": "text", "text": _truncate(title, MAX_HEADER_LENGTH)}
    envelope["body"] = {"text": _truncate(body_text, MAX_BODY_LENGTH)}
    return envelope


def _find_promotable_cta_link(card: CardElement) -> LinkButtonElement | None:
    """Find a LinkButton that can be promoted to a native cta_url message.

    Meta CTA URL messages support exactly one URL button and cannot mix
    with reply buttons, selects, or radio selects, so promotion requires
    the card's only interactive element (across every actions row) to be
    a single LinkButton. The URL must be http(s) and the label non-empty,
    or the Cloud API rejects the send with a 400 -- anything else keeps
    the always-deliverable text fallback.
    """
    if card.get("image_url"):
        return None

    interactive_children = [
        child for actions in _collect_actions(card.get("children", [])) for child in actions.get("children", [])
    ]
    if len(interactive_children) != 1:
        return None

    candidate = interactive_children[0]
    if candidate.get("type") != "link-button":
        return None

    url = candidate.get("url")
    label = candidate.get("label")
    # ``strip(JS_WHITESPACE)`` mirrors JS ``String.prototype.trim``.
    if not (
        isinstance(url, str) and _HTTP_URL_REGEX.match(url) and isinstance(label, str) and label.strip(JS_WHITESPACE)
    ):
        return None

    # Upstream parity (adapter-whatsapp/src/cards.ts findPromotableCtaLink at
    # chat@4.41.1): eligibility is by element type only. A title over 60 or a
    # body over 1024 characters is truncated by ``_build_interactive_envelope``
    # exactly as upstream's ``buildInteractiveEnvelope`` does, not kept as text.
    if not _children_fit_cta_body(card.get("children", [])):
        return None

    return cast(LinkButtonElement, candidate)


def _children_fit_cta_body(children: list[CardChild]) -> bool:
    """Whether ``_build_body_text`` can carry every child without loss.

    Images, tables, charts, and inline links would be silently dropped from
    an interactive body, so cards containing them keep the text fallback.
    """
    for child in children:
        child_type = child.get("type")
        if child_type == "section":
            if not _children_fit_cta_body(child.get("children", [])):  # type: ignore[union-attr]
                return False
        elif child_type not in ("actions", "divider", "fields", "text"):
            return False
    return True


def _collect_actions(children: list[CardChild]) -> list[ActionsElement]:
    """Collect every ActionsElement in a list of card children, including
    those nested inside sections.
    """
    found: list[ActionsElement] = []
    for child in children:
        if child.get("type") == "actions":
            found.append(child)  # type: ignore[arg-type]
        elif child.get("type") == "section":
            found.extend(_collect_actions(child.get("children", [])))  # type: ignore[union-attr]
    return found


def _collect_link_buttons(children: list[CardChild]) -> list[LinkButtonElement]:
    """Collect every LinkButton across all actions rows in a card."""
    return [
        child
        for actions in _collect_actions(children)
        for child in actions.get("children", [])
        if child.get("type") == "link-button"
    ]


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
