"""Teams Adaptive Card converter for cross-platform cards.

Converts CardElement to Microsoft Adaptive Cards format.
See: https://adaptivecards.io/
"""

from __future__ import annotations

from typing import Any

from chat_sdk.cards import (
    ActionsElement,
    ButtonElement,
    CardChild,
    CardElement,
    FieldsElement,
    ImageElement,
    LinkButtonElement,
    SectionElement,
    TableElement,
    TextElement,
    card_child_to_fallback_text,
)
from chat_sdk.emoji import convert_emoji_placeholders

ADAPTIVE_CARD_SCHEMA = "http://adaptivecards.io/schemas/adaptive-card.json"
ADAPTIVE_CARD_VERSION = "1.4"


def _convert_emoji(text: str) -> str:
    """Convert emoji placeholders to Teams format."""
    return convert_emoji_placeholders(text, "teams")


def _map_button_style(style: str | None) -> str | None:
    """Map button style to Teams adaptive card style."""
    if style == "danger":
        return "destructive"
    if style == "primary":
        return "positive"
    return None


def card_to_adaptive_card(card: CardElement) -> dict[str, Any]:
    """Convert a CardElement to a Teams Adaptive Card.

    Returns a dict representing the Adaptive Card JSON.
    """
    body: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []

    # Add title as TextBlock
    title = card.get("title")
    if title:
        body.append(
            {
                "type": "TextBlock",
                "text": _convert_emoji(title),
                "weight": "bolder",
                "size": "large",
                "wrap": True,
            }
        )

    # Add subtitle as TextBlock
    subtitle = card.get("subtitle")
    if subtitle:
        body.append(
            {
                "type": "TextBlock",
                "text": _convert_emoji(subtitle),
                "isSubtle": True,
                "wrap": True,
            }
        )

    # Add header image if present
    image_url = card.get("image_url") or card.get("imageUrl")
    if image_url:
        body.append(
            {
                "type": "Image",
                "url": image_url,
                "size": "stretch",
            }
        )

    # Convert children
    for child in card.get("children", []):
        result = _convert_child_to_adaptive(child)
        body.extend(result["elements"])
        actions.extend(result["actions"])

    adaptive_card: dict[str, Any] = {
        "type": "AdaptiveCard",
        "$schema": ADAPTIVE_CARD_SCHEMA,
        "version": ADAPTIVE_CARD_VERSION,
        "body": body,
    }

    if actions:
        adaptive_card["actions"] = actions

    return adaptive_card


def _convert_child_to_adaptive(child: CardChild) -> dict[str, Any]:
    """Convert a card child element to Adaptive Card elements.

    Returns dict with 'elements' and 'actions' lists.
    """
    child_type = child.get("type", "")

    if child_type == "text":
        return {"elements": [_convert_text_to_element(child)], "actions": []}  # type: ignore[arg-type]
    if child_type == "image":
        return {"elements": [_convert_image_to_element(child)], "actions": []}  # type: ignore[arg-type]
    if child_type == "divider":
        return {"elements": [{"type": "Container", "separator": True, "items": []}], "actions": []}
    if child_type == "actions":
        return _convert_actions_to_elements(child)  # type: ignore[arg-type]
    if child_type == "section":
        return _convert_section_to_elements(child)  # type: ignore[arg-type]
    if child_type == "fields":
        return {"elements": [_convert_fields_to_element(child)], "actions": []}  # type: ignore[arg-type]
    if child_type == "link":
        label = child.get("label", "")  # type: ignore[union-attr]
        url = child.get("url", "")  # type: ignore[union-attr]
        return {
            "elements": [
                {
                    "type": "TextBlock",
                    "text": f"[{_convert_emoji(label)}]({url})",
                    "wrap": True,
                }
            ],
            "actions": [],
        }
    if child_type == "table":
        return {"elements": [_convert_table_to_element(child)], "actions": []}  # type: ignore[arg-type]

    text = card_child_to_fallback_text(child)
    if text:
        return {"elements": [{"type": "TextBlock", "text": text, "wrap": True}], "actions": []}
    return {"elements": [], "actions": []}


def _convert_text_to_element(element: TextElement) -> dict[str, Any]:
    """Convert a text element to an Adaptive Card TextBlock."""
    content = element.get("content", "")
    text_block: dict[str, Any] = {
        "type": "TextBlock",
        "text": _convert_emoji(content),
        "wrap": True,
    }

    style = element.get("style", "")
    if style == "bold":
        text_block["weight"] = "bolder"
    elif style == "muted":
        text_block["isSubtle"] = True

    return text_block


def _convert_image_to_element(element: ImageElement) -> dict[str, Any]:
    """Convert an image element to an Adaptive Card Image."""
    return {
        "type": "Image",
        "url": element.get("url", ""),
        "altText": element.get("alt", "Image"),
        "size": "auto",
    }


def _convert_actions_to_elements(element: ActionsElement) -> dict[str, Any]:
    """Convert actions to Adaptive Card actions (card-level, not inline)."""
    actions: list[dict[str, Any]] = []
    for child in element.get("children", []):
        child_type = child.get("type", "")
        if child_type == "link-button":
            actions.append(_convert_link_button_to_action(child))  # type: ignore[arg-type]
        elif child_type == "button":
            actions.append(_convert_button_to_action(child))  # type: ignore[arg-type]

    return {"elements": [], "actions": actions}


def _convert_button_to_action(button: ButtonElement) -> dict[str, Any]:
    """Convert a button to an Adaptive Card Action.Submit."""
    action: dict[str, Any] = {
        "type": "Action.Submit",
        "title": _convert_emoji(button.get("label", "")),
        "data": {
            "actionId": button.get("id", ""),
            "value": button.get("value"),
        },
    }

    style = _map_button_style(button.get("style"))
    if style:
        action["style"] = style

    return action


def _convert_link_button_to_action(button: LinkButtonElement) -> dict[str, Any]:
    """Convert a link button to an Adaptive Card Action.OpenUrl."""
    action: dict[str, Any] = {
        "type": "Action.OpenUrl",
        "title": _convert_emoji(button.get("label", "")),
        "url": button.get("url", ""),
    }

    style = _map_button_style(button.get("style"))
    if style:
        action["style"] = style

    return action


def _convert_section_to_elements(element: SectionElement) -> dict[str, Any]:
    """Convert a section to Adaptive Card Container."""
    elements: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []

    container_items: list[dict[str, Any]] = []

    for child in element.get("children", []):
        result = _convert_child_to_adaptive(child)
        container_items.extend(result["elements"])
        actions.extend(result["actions"])

    if container_items:
        elements.append({"type": "Container", "items": container_items})

    return {"elements": elements, "actions": actions}


def _convert_table_to_element(element: TableElement) -> dict[str, Any]:
    """Convert a table to Adaptive Card ColumnSets."""
    headers = element.get("headers", [])
    rows = element.get("rows", [])

    columns = [
        {
            "type": "Column",
            "width": "stretch",
            "items": [
                {
                    "type": "TextBlock",
                    "text": _convert_emoji(header),
                    "weight": "bolder",
                    "wrap": True,
                }
            ],
        }
        for header in headers
    ]

    header_row = {"type": "ColumnSet", "columns": columns}

    data_rows = [
        {
            "type": "ColumnSet",
            "columns": [
                {
                    "type": "Column",
                    "width": "stretch",
                    "items": [
                        {
                            "type": "TextBlock",
                            "text": _convert_emoji(cell),
                            "wrap": True,
                        }
                    ],
                }
                for cell in row
            ],
        }
        for row in rows
    ]

    return {"type": "Container", "items": [header_row, *data_rows]}


def _convert_fields_to_element(element: FieldsElement) -> dict[str, Any]:
    """Convert fields to an Adaptive Card FactSet."""
    facts = [
        {
            "title": _convert_emoji(f.get("label", "")),
            "value": _convert_emoji(f.get("value", "")),
        }
        for f in element.get("children", [])
    ]

    return {"type": "FactSet", "facts": facts}


def card_to_fallback_text(card: CardElement) -> str:
    """Generate fallback text from a card element.

    Used when adaptive cards aren't supported.
    Delegates to the shared implementation which handles emoji conversion
    and renders all child types (including tables, fields, etc.) correctly.
    """
    from chat_sdk.shared.card_utils import card_to_fallback_text as shared_card_to_fallback_text

    return shared_card_to_fallback_text(card, bold_format="**", line_break="\n\n", platform="teams")
