"""Teams Adaptive Card converter for cross-platform cards.

Converts CardElement to Microsoft Adaptive Cards format.
See: https://adaptivecards.io/
"""

from __future__ import annotations

from typing import Any, cast

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
from chat_sdk.modals import RadioSelectElement, SelectElement

ADAPTIVE_CARD_SCHEMA = "http://adaptivecards.io/schemas/adaptive-card.json"
ADAPTIVE_CARD_VERSION = "1.5"

# Sentinel action ID for auto-injected submit buttons.
# Used when a card has select/radio_select inputs but no submit button.
AUTO_SUBMIT_ACTION_ID = "__auto_submit"

# Internal marker key for divider placeholders. Stripped by
# ``_hoist_dividers`` before the card is serialized — never reaches Teams.
_DIVIDER_MARKER = "__chatSdkDivider"


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

    body = _hoist_dividers(body)

    adaptive_card: dict[str, Any] = {
        "type": "AdaptiveCard",
        "$schema": ADAPTIVE_CARD_SCHEMA,
        "version": ADAPTIVE_CARD_VERSION,
        "body": body,
    }

    if actions:
        adaptive_card["actions"] = actions

    # Width hint (upstream ``card.width === "full"``); Teams renders the card
    # across the full conversation width.
    if card.get("width") == "full":
        adaptive_card["msteams"] = {"width": "full"}

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
        # Emit an internal marker instead of the final Container. The
        # post-processing pass (_hoist_dividers) either moves ``separator``
        # onto the next sibling (preferred — renders as a full-width line)
        # or, for a trailing divider with no next sibling, replaces it with
        # a minimal non-empty Container so the separator is visible. An
        # empty ``Container`` with ``separator: True`` renders at zero
        # height in Microsoft Teams.
        return {"elements": [{_DIVIDER_MARKER: True}], "actions": []}
    if child_type == "actions":
        return _convert_actions_to_elements(child)  # type: ignore[arg-type]
    if child_type == "section":
        return _convert_section_to_elements(child)  # type: ignore[arg-type]
    if child_type == "fields":
        return {"elements": [_convert_fields_to_element(child)], "actions": []}  # type: ignore[arg-type]
    if child_type == "link":
        label = cast("str", child.get("label", ""))
        url = cast("str", child.get("url", ""))
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
        return {"elements": _convert_table_to_elements(child), "actions": []}  # type: ignore[arg-type]

    text = card_child_to_fallback_text(child)
    if text:
        return {"elements": [{"type": "TextBlock", "text": text, "wrap": True}], "actions": []}
    return {"elements": [], "actions": []}


def _hoist_dividers(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace internal divider markers with ``separator: True`` on the next sibling.

    An Adaptive Card ``Container`` with ``separator: True`` and no ``items``
    renders at zero height in Microsoft Teams — the separator line is
    effectively invisible. Instead, hoist the separator onto the following
    sibling so it renders as a full-width line above that element. For a
    trailing divider with no next sibling, emit a non-empty Container so
    the separator is still visible.
    """
    result: list[dict[str, Any]] = []
    pending = False
    for el in elements:
        if el.get(_DIVIDER_MARKER):
            pending = True
            continue
        if pending:
            el = {**el, "separator": True}
            pending = False
        result.append(el)
    if pending:
        result.append(
            {
                "type": "Container",
                "separator": True,
                "items": [{"type": "TextBlock", "text": " ", "wrap": False}],
            }
        )
    return result


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
    """Convert actions to Adaptive Card actions (card-level, not inline).

    Select and RadioSelect elements become Input.ChoiceSet body elements.
    When inputs are present but no explicit buttons, an auto-submit
    Action.Submit is injected (Teams inputs need an explicit submit action).
    """
    actions: list[dict[str, Any]] = []
    elements: list[dict[str, Any]] = []
    has_buttons = False
    has_inputs = False

    for child in element.get("children", []):
        child_type = child.get("type", "")
        if child_type == "button":
            has_buttons = True
            actions.append(_convert_button_to_action(child))  # type: ignore[arg-type]
        elif child_type == "link-button":
            actions.append(_convert_link_button_to_action(child))  # type: ignore[arg-type]
        elif child_type == "select":
            has_inputs = True
            elements.append(_convert_select_to_element(child))  # type: ignore[arg-type]
        elif child_type == "radio_select":
            has_inputs = True
            elements.append(_convert_radio_select_to_element(child))  # type: ignore[arg-type]

    # Auto-inject a submit button when there are inputs but no buttons.
    # Teams inputs don't auto-submit like Slack -- they need an Action.Submit.
    if has_inputs and not has_buttons:
        actions.append(
            {
                "type": "Action.Submit",
                "title": "Submit",
                "data": {"actionId": AUTO_SUBMIT_ACTION_ID},
            }
        )

    return {"elements": elements, "actions": actions}


def _convert_select_to_element(select: SelectElement) -> dict[str, Any]:
    """Convert a SelectElement to an Adaptive Card Input.ChoiceSet (compact)."""
    choices = [
        {"title": _convert_emoji(opt.get("label", "")), "value": opt.get("value", "")}
        for opt in select.get("options", [])
    ]

    result: dict[str, Any] = {
        "type": "Input.ChoiceSet",
        "id": select.get("id", ""),
        "label": _convert_emoji(select.get("label", "")),
        "style": "compact",
        "isRequired": not select.get("optional", False),
        "choices": choices,
    }

    placeholder = select.get("placeholder")
    if placeholder:
        result["placeholder"] = placeholder

    initial_option = select.get("initial_option")
    if initial_option:
        result["value"] = initial_option

    return result


def _convert_radio_select_to_element(radio_select: RadioSelectElement) -> dict[str, Any]:
    """Convert a RadioSelectElement to an Adaptive Card Input.ChoiceSet (expanded)."""
    choices = [
        {"title": _convert_emoji(opt.get("label", "")), "value": opt.get("value", "")}
        for opt in radio_select.get("options", [])
    ]

    result: dict[str, Any] = {
        "type": "Input.ChoiceSet",
        "id": radio_select.get("id", ""),
        "label": _convert_emoji(radio_select.get("label", "")),
        "style": "expanded",
        "isRequired": not radio_select.get("optional", False),
        "choices": choices,
    }

    initial_option = radio_select.get("initial_option")
    if initial_option:
        result["value"] = initial_option

    return result


def _action_options(button: ButtonElement | LinkButtonElement) -> dict[str, Any]:
    """Options shared by Action.Submit and Action.OpenUrl: title, style, tooltip."""
    options: dict[str, Any] = {"title": _convert_emoji(button.get("label", ""))}

    style = _map_button_style(button.get("style"))
    if style:
        options["style"] = style

    tooltip = button.get("tooltip")
    if tooltip:
        options["tooltip"] = _convert_emoji(tooltip)

    return options


def _convert_button_to_action(button: ButtonElement) -> dict[str, Any]:
    """Convert a button to an Adaptive Card Action.Submit."""
    data: dict[str, Any] = {
        "actionId": button.get("id", ""),
        "value": button.get("value"),
    }

    # Add task/fetch hint for dialog-opening buttons
    if button.get("action_type") == "modal":
        data["msteams"] = {"type": "task/fetch"}

    return {"type": "Action.Submit", **_action_options(button), "data": data}


def _convert_link_button_to_action(button: LinkButtonElement) -> dict[str, Any]:
    """Convert a link button to an Adaptive Card Action.OpenUrl."""
    return {"type": "Action.OpenUrl", **_action_options(button), "url": button.get("url", "")}


def _convert_section_to_elements(element: SectionElement) -> dict[str, Any]:
    """Convert a section to Adaptive Card Container."""
    elements: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []

    container_items: list[dict[str, Any]] = []

    for child in element.get("children", []):
        result = _convert_child_to_adaptive(child)
        container_items.extend(result["elements"])
        actions.extend(result["actions"])

    container_items = _hoist_dividers(container_items)
    if container_items:
        elements.append({"type": "Container", "items": container_items})

    return {"elements": elements, "actions": actions}


_TABLE_HORIZONTAL_ALIGNMENT: dict[str, str] = {
    "left": "Left",
    "center": "Center",
    "right": "Right",
}

_TABLE_VERTICAL_ALIGNMENT: dict[str, str] = {
    "top": "Top",
    "center": "Center",
    "bottom": "Bottom",
}


def _column_weight(width: Any) -> int:
    """Return a column's relative weight, or 1 when it is not a positive integer.

    Adaptive Cards treats a column width as a relative weight only when it is
    a positive integer; Teams desktop and mobile disagree on anything else, so
    an invalid weight falls back to the default instead of reaching the wire.
    Upstream ``Number.isInteger(width) && width > 0``: ``bool`` is not a
    number, and an integral float (``2.0``) is accepted and emitted as ``2``
    (JS has one number type, so ``2.0`` serializes as ``2`` upstream).
    """
    if isinstance(width, bool):
        return 1
    if isinstance(width, int):
        return width if width > 0 else 1
    if isinstance(width, float) and width.is_integer() and width > 0:
        return int(width)
    return 1


def _convert_table_to_elements(element: TableElement) -> list[dict[str, Any]]:
    """Convert a table to the Adaptive Card 1.5 ``Table`` element.

    Returns a list so "no columns means nothing to draw" needs no special case
    at the call site, matching the empty ASCII fallback. The widest row (or the
    header) sets the column count; a short row is padded with empty cells
    instead of shifting the grid.
    """
    headers = element.get("headers") or []
    rows = element.get("rows") or []

    column_count = len(headers)
    for row in rows:
        column_count = max(column_count, len(row))
    if column_count == 0:
        return []

    widths = element.get("widths") or []
    align = element.get("align") or []

    columns: list[dict[str, Any]] = []
    for index in range(column_count):
        column: dict[str, Any] = {"width": _column_weight(widths[index] if index < len(widths) else None)}
        column_align = _TABLE_HORIZONTAL_ALIGNMENT.get(align[index]) if index < len(align) else None
        if column_align:
            column["horizontalCellContentAlignment"] = column_align
        columns.append(column)

    def to_row(cells: list[str], text_options: dict[str, Any] | None = None) -> dict[str, Any]:
        row_cells: list[dict[str, Any]] = []
        for index in range(column_count):
            cell = cells[index] if index < len(cells) else None
            text_block: dict[str, Any] = {
                "type": "TextBlock",
                "text": _convert_emoji(cell if cell is not None else ""),
                "wrap": True,
            }
            if text_options:
                text_block.update(text_options)
            row_cells.append({"type": "TableCell", "items": [text_block]})
        return {"type": "TableRow", "cells": row_cells}

    has_header = len(headers) > 0
    table_rows = [to_row(row) for row in rows]
    if has_header:
        table_rows.insert(0, to_row(headers, {"weight": "Bolder"}))

    grid_lines = element.get("grid_lines")
    table: dict[str, Any] = {
        "type": "Table",
        "columns": columns,
        "rows": table_rows,
        # Adaptive Cards spells this ``firstRowAsHeaders`` (plural): that is
        # the name renderers read and every Microsoft Table sample uses (the
        # prose property table says ``firstRowAsHeader``). Do not "correct" it
        # — the property defaults to ``true`` when absent, so the singular
        # would silently give a headerless table a header row.
        "firstRowAsHeaders": has_header,
        "showGridLines": grid_lines if grid_lines is not None else True,
    }

    grid_style = element.get("grid_style")
    if grid_style:
        table["gridStyle"] = grid_style

    vertical_align = element.get("vertical_align")
    vertical = _TABLE_VERTICAL_ALIGNMENT.get(vertical_align) if vertical_align else None
    if vertical:
        table["verticalCellContentAlignment"] = vertical

    return [table]


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
