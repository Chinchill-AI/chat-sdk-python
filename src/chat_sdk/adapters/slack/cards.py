"""Slack Block Kit converter for cross-platform cards.

Converts CardElement to Slack Block Kit blocks.
Port of cards.ts from the Vercel Chat SDK Slack adapter.

@see https://api.slack.com/block-kit
"""

from __future__ import annotations

from typing import Any

from chat_sdk.cards import (
    ActionsElement,
    ButtonElement,
    CardChild,
    CardElement,
    DividerElement,
    FieldsElement,
    ImageElement,
    LinkButtonElement,
    LinkElement,
    SectionElement,
    TableElement,
    TextElement,
    card_child_to_fallback_text,
    table_element_to_ascii,
)
from chat_sdk.modals import SelectElement
from chat_sdk.shared import card_to_fallback_text as shared_card_to_fallback_text, create_emoji_converter, map_button_style

# Type aliases for Slack Block Kit structures
SlackBlock = dict[str, Any]
SlackTextObject = dict[str, Any]
SlackButtonElement_ = dict[str, Any]
SlackLinkButtonElement_ = dict[str, Any]
SlackOptionObject = dict[str, Any]
SlackSelectElement_ = dict[str, Any]
SlackRadioSelectElement_ = dict[str, Any]
SlackActionElement = dict[str, Any]


# Convert emoji placeholders in text to Slack format.
convert_emoji = create_emoji_converter("slack")


def card_to_block_kit(card: CardElement) -> list[SlackBlock]:
    """Convert a CardElement to Slack Block Kit blocks."""
    blocks: list[SlackBlock] = []

    # Add header if title is present
    title = card.get("title")
    if title:
        blocks.append(
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": convert_emoji(title),
                    "emoji": True,
                },
            }
        )

    # Add subtitle as context if present
    subtitle = card.get("subtitle")
    if subtitle:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": convert_emoji(subtitle),
                    },
                ],
            }
        )

    # Add header image if present
    image_url = card.get("image_url")
    if image_url:
        blocks.append(
            {
                "type": "image",
                "image_url": image_url,
                "alt_text": title or "Card image",
            }
        )

    # Convert children -- track whether native table block has been used
    # (Slack allows at most one table block per message)
    state = {"used_native_table": False}
    for child in card.get("children", []):
        child_blocks = _convert_child_to_blocks(child, state)
        blocks.extend(child_blocks)

    return blocks


def _convert_child_to_blocks(child: CardChild, state: dict[str, bool]) -> list[SlackBlock]:
    """Convert a card child element to Slack blocks."""
    child_type = child.get("type", "")

    if child_type == "text":
        return [convert_text_to_block(child)]  # type: ignore[arg-type]
    if child_type == "image":
        return [_convert_image_to_block(child)]  # type: ignore[arg-type]
    if child_type == "divider":
        return [_convert_divider_to_block(child)]  # type: ignore[arg-type]
    if child_type == "actions":
        return [_convert_actions_to_block(child)]  # type: ignore[arg-type]
    if child_type == "section":
        return _convert_section_to_blocks(child, state)  # type: ignore[arg-type]
    if child_type == "fields":
        return [convert_fields_to_block(child)]  # type: ignore[arg-type]
    if child_type == "link":
        return [_convert_link_to_block(child)]  # type: ignore[arg-type]
    if child_type == "table":
        return _convert_table_to_blocks(child, state)  # type: ignore[arg-type]

    # Unknown type -- try fallback
    text = card_child_to_fallback_text(child)
    if text:
        return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    return []


def _markdown_to_mrkdwn(text: str) -> str:
    """Convert standard Markdown formatting to Slack mrkdwn.

    **bold** -> *bold*
    """
    import re

    return re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)


def convert_text_to_block(element: TextElement) -> SlackBlock:
    """Convert a TextElement to a Slack block."""
    text = _markdown_to_mrkdwn(convert_emoji(element.get("content", "")))
    formatted_text = text

    style = element.get("style")
    if style == "bold":
        formatted_text = f"*{text}*"
    elif style == "muted":
        # Slack doesn't have a muted style, use context block
        return {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": text}],
        }

    return {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": formatted_text,
        },
    }


def _convert_link_to_block(element: LinkElement) -> SlackBlock:
    """Convert a LinkElement to a Slack block."""
    return {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": f"<{element['url']}|{convert_emoji(element['label'])}>",
        },
    }


def _convert_image_to_block(element: ImageElement) -> SlackBlock:
    """Convert an ImageElement to a Slack block."""
    return {
        "type": "image",
        "image_url": element["url"],
        "alt_text": element.get("alt") or "Image",
    }


def _convert_divider_to_block(_element: DividerElement) -> SlackBlock:
    """Convert a DividerElement to a Slack block."""
    return {"type": "divider"}


def _convert_actions_to_block(element: ActionsElement) -> SlackBlock:
    """Convert an ActionsElement to a Slack block."""
    elements: list[SlackActionElement] = []
    for child in element.get("children", []):
        child_type = child.get("type", "")
        if child_type == "link-button":
            elements.append(_convert_link_button_to_element(child))
        elif child_type == "select":
            elements.append(_convert_select_to_element(child))
        elif child_type == "radio_select":
            elements.append(_convert_radio_select_to_element(child))
        else:
            elements.append(_convert_button_to_element(child))

    return {"type": "actions", "elements": elements}


def _convert_button_to_element(button: ButtonElement) -> SlackButtonElement_:
    """Convert a ButtonElement to a Slack button element."""
    element: SlackButtonElement_ = {
        "type": "button",
        "text": {
            "type": "plain_text",
            "text": convert_emoji(button.get("label", "")),
            "emoji": True,
        },
        "action_id": button.get("id", ""),
    }

    value = button.get("value")
    if value:
        element["value"] = value

    style = map_button_style(button.get("style"), "slack")
    if style:
        element["style"] = style

    return element


def _convert_link_button_to_element(button: LinkButtonElement) -> SlackLinkButtonElement_:
    """Convert a LinkButtonElement to a Slack link button element."""
    url = button.get("url", "")
    element: SlackLinkButtonElement_ = {
        "type": "button",
        "text": {
            "type": "plain_text",
            "text": convert_emoji(button.get("label", "")),
            "emoji": True,
        },
        "action_id": f"link-{url[:200]}",
        "url": url,
    }

    style = map_button_style(button.get("style"), "slack")
    if style:
        element["style"] = style

    return element


def _convert_select_to_element(select: SelectElement) -> SlackSelectElement_:
    """Convert a SelectElement to a Slack select element."""
    options: list[SlackOptionObject] = []
    for opt in select.get("options", []):
        option: SlackOptionObject = {
            "text": {"type": "plain_text", "text": convert_emoji(opt.get("label", ""))},
            "value": opt.get("value", ""),
        }
        desc = opt.get("description")
        if desc:
            option["description"] = {"type": "plain_text", "text": convert_emoji(desc)}
        options.append(option)

    element: SlackSelectElement_ = {
        "type": "static_select",
        "action_id": select.get("id", ""),
        "options": options,
    }

    placeholder = select.get("placeholder")
    if placeholder:
        element["placeholder"] = {"type": "plain_text", "text": convert_emoji(placeholder)}

    initial_option = select.get("initial_option")
    if initial_option:
        initial_opt = next((o for o in options if o["value"] == initial_option), None)
        if initial_opt:
            element["initial_option"] = initial_opt

    return element


def _convert_radio_select_to_element(radio_select: Any) -> SlackRadioSelectElement_:
    """Convert a RadioSelectElement to a Slack radio buttons element."""
    limited_options = radio_select.get("options", [])[:10]
    options: list[SlackOptionObject] = []
    for opt in limited_options:
        option: SlackOptionObject = {
            "text": {"type": "mrkdwn", "text": convert_emoji(opt.get("label", ""))},
            "value": opt.get("value", ""),
        }
        desc = opt.get("description")
        if desc:
            option["description"] = {"type": "mrkdwn", "text": convert_emoji(desc)}
        options.append(option)

    element: SlackRadioSelectElement_ = {
        "type": "radio_buttons",
        "action_id": radio_select.get("id", ""),
        "options": options,
    }

    initial_option = radio_select.get("initial_option")
    if initial_option:
        initial_opt = next((o for o in options if o["value"] == initial_option), None)
        if initial_opt:
            element["initial_option"] = initial_opt

    return element


def _convert_table_to_blocks(element: TableElement, state: dict[str, bool]) -> list[SlackBlock]:
    """Convert a table element to Slack Block Kit blocks.

    Uses the native table block with first-row-as-headers schema.
    Falls back to code block for tables exceeding Slack limits (100 rows, 20 columns)
    or when a native table block has already been used in this message.

    @see https://docs.slack.dev/reference/block-kit/blocks/table-block/
    """
    MAX_ROWS = 100
    MAX_COLS = 20

    headers = element.get("headers", [])
    rows = element.get("rows", [])

    if state["used_native_table"] or len(rows) > MAX_ROWS or len(headers) > MAX_COLS:
        # Fall back to ASCII table in a code block
        return [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"```\n{table_element_to_ascii(headers, rows)}\n```",
                },
            }
        ]

    state["used_native_table"] = True

    # First row is headers, subsequent rows are data
    header_row = [{"type": "raw_text", "text": convert_emoji(h)} for h in headers]
    data_rows = [[{"type": "raw_text", "text": convert_emoji(cell)} for cell in row] for row in rows]

    return [
        {
            "type": "table",
            "rows": [header_row, *data_rows],
        }
    ]


def _convert_section_to_blocks(element: SectionElement, state: dict[str, bool]) -> list[SlackBlock]:
    """Convert a SectionElement by flattening its children into blocks."""
    blocks: list[SlackBlock] = []
    for child in element.get("children", []):
        blocks.extend(_convert_child_to_blocks(child, state))
    return blocks


def convert_fields_to_block(element: FieldsElement) -> SlackBlock:
    """Convert a FieldsElement to a Slack section block with fields."""
    fields: list[SlackTextObject] = []

    for f in element.get("children", []):
        fields.append(
            {
                "type": "mrkdwn",
                "text": f"*{_markdown_to_mrkdwn(convert_emoji(f['label']))}*\n{_markdown_to_mrkdwn(convert_emoji(f['value']))}",
            }
        )

    return {"type": "section", "fields": fields}


def card_to_fallback_text(card: CardElement) -> str:
    """Generate fallback text from a card element.

    Used when blocks aren't supported or for notifications.
    """
    return shared_card_to_fallback_text(
        card,
        bold_format="*",
        line_break="\n",
        platform="slack",
    )
