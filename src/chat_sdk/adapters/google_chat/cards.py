"""Google Chat Card converter for cross-platform cards.

Converts CardElement to Google Chat Card v2 format.
See: https://developers.google.com/chat/api/reference/rest/v1/cards

Python port of cards.ts.
"""

from __future__ import annotations

from typing import Any, cast

from chat_sdk.adapters.google_chat.format_converter import GoogleChatFormatConverter
from chat_sdk.cards import (
    CardChild,
    CardElement,
    card_child_to_fallback_text,
    table_element_to_ascii,
)
from chat_sdk.shared import card_to_fallback_text as shared_card_to_fallback_text
from chat_sdk.shared import create_emoji_converter
from chat_sdk.shared.base_format_converter import parse_markdown

# Convert emoji placeholders in text to GChat format (Unicode).
convert_emoji = create_emoji_converter("gchat")

_gchat_converter = GoogleChatFormatConverter()


def _render_markdown_as_gchat(text: str) -> str:
    """Parse standard markdown and render as Google Chat formatted text."""
    return _gchat_converter.from_ast(parse_markdown(text))


def card_to_google_card(
    card: CardElement,
    options: dict[str, Any] | str | None = None,
) -> dict[str, Any]:
    """Convert a CardElement to Google Chat Card v2 format.

    Args:
        card: The card element to convert.
        options: CardConversionOptions dict or legacy cardId string.

    Returns:
        Google Chat Card v2 dict.
    """
    # Support legacy signature where second arg is cardId string
    if isinstance(options, str):
        opts: dict[str, Any] = {"card_id": options}
    elif options is None:
        opts = {}
    else:
        opts = options

    sections: list[dict[str, Any]] = []

    # Build header
    header: dict[str, Any] | None = None
    title = card.get("title")
    subtitle = card.get("subtitle")
    image_url = card.get("image_url") or card.get("imageUrl")

    if title or subtitle or image_url:
        header = {
            "title": convert_emoji(title or ""),
        }
        if subtitle:
            header["subtitle"] = convert_emoji(subtitle)
        if image_url:
            header["imageUrl"] = image_url
            header["imageType"] = "SQUARE"

    # Group children into sections
    # GChat cards require widgets to be inside sections
    current_widgets: list[dict[str, Any]] = []

    for child in card.get("children", []):
        if child.get("type") == "section":
            # If we have pending widgets, flush them to a section
            if current_widgets:
                sections.append({"widgets": current_widgets})
                current_widgets = []
            # Convert section as its own section
            section_widgets = _convert_section_to_widgets(cast("dict[str, Any]", child), opts.get("endpoint_url"))
            sections.append({"widgets": section_widgets})
        else:
            # Add to current widgets
            widgets = _convert_child_to_widgets(child, opts.get("endpoint_url"))
            current_widgets.extend(widgets)

    # Flush remaining widgets
    if current_widgets:
        sections.append({"widgets": current_widgets})

    # GChat requires at least one section with at least one widget
    if not sections:
        sections.append({"widgets": [{"textParagraph": {"text": ""}}]})

    google_card: dict[str, Any] = {
        "card": {
            "sections": sections,
        },
    }

    if header:
        google_card["card"]["header"] = header

    card_id = opts.get("card_id") or opts.get("cardId")
    if card_id:
        google_card["cardId"] = card_id

    return google_card


def _convert_child_to_widgets(
    child: CardChild,
    endpoint_url: str | None = None,
) -> list[dict[str, Any]]:
    """Convert a card child element to Google Chat widgets.

    The per-type helpers below accept `dict[str, Any]` because they reach
    for variant-specific keys (`content`, `label`, `url`, etc.). The
    `child_type` check narrows the union, so the `cast` is safe and lets
    the helpers stay simple.
    """
    child_type = child.get("type")

    if child_type == "text":
        return [_convert_text_to_widget(cast("dict[str, Any]", child))]
    elif child_type == "image":
        return [_convert_image_to_widget(cast("dict[str, Any]", child))]
    elif child_type == "divider":
        return [_convert_divider_to_widget()]
    elif child_type == "actions":
        return [_convert_actions_to_widget(cast("dict[str, Any]", child), endpoint_url)]
    elif child_type == "section":
        return _convert_section_to_widgets(cast("dict[str, Any]", child), endpoint_url)
    elif child_type == "fields":
        return _convert_fields_to_widgets(cast("dict[str, Any]", child))
    elif child_type == "link":
        label = cast("str", child.get("label", ""))
        url = cast("str", child.get("url", ""))
        return [
            {
                "textParagraph": {
                    "text": f'<a href="{url}">{convert_emoji(label)}</a>',
                },
            },
        ]
    elif child_type == "table":
        return [_convert_table_to_widget(cast("dict[str, Any]", child))]
    else:
        text = card_child_to_fallback_text(child)
        if text:
            return [{"textParagraph": {"text": text}}]
        return []


def _convert_text_to_widget(element: dict[str, Any]) -> dict[str, Any]:
    """Convert a text element to a widget."""
    text = _render_markdown_as_gchat(convert_emoji(element.get("content", "")))

    if element.get("style") == "bold":
        text = f"*{text}*"
    elif element.get("style") == "muted":
        text = convert_emoji(element.get("content", ""))

    return {"textParagraph": {"text": text}}


def _convert_image_to_widget(element: dict[str, Any]) -> dict[str, Any]:
    """Convert an image element to a widget."""
    return {
        "image": {
            "imageUrl": element.get("url", ""),
            "altText": element.get("alt", "Image"),
        },
    }


def _convert_divider_to_widget() -> dict[str, Any]:
    """Convert a divider element to a widget."""
    return {"divider": {}}


def _convert_actions_to_widget(
    element: dict[str, Any],
    endpoint_url: str | None = None,
) -> dict[str, Any]:
    """Convert an actions element to a widget."""
    buttons: list[dict[str, Any]] = []
    for child in element.get("children", []):
        child_type = child.get("type")
        if child_type == "link-button":
            buttons.append(_convert_link_button_to_google_button(child))
        elif child_type == "button":
            buttons.append(_convert_button_to_google_button(child, endpoint_url))

    return {"buttonList": {"buttons": buttons}}


def _convert_button_to_google_button(
    button: dict[str, Any],
    endpoint_url: str | None = None,
) -> dict[str, Any]:
    """Convert a button element to a Google Chat button.

    For HTTP endpoint apps, the function field must be the endpoint URL,
    and the action ID is passed via parameters.
    """
    parameters: list[dict[str, str]] = [
        {"key": "actionId", "value": button.get("id", "")},
    ]
    value = button.get("value")
    if value:
        parameters.append({"key": "value", "value": value})

    google_button: dict[str, Any] = {
        "text": convert_emoji(button.get("label", "")),
        "onClick": {
            "action": {
                # For HTTP endpoints, function must be the full URL
                # For other deployments (Apps Script, etc.), use just the action ID
                "function": endpoint_url or button.get("id", ""),
                "parameters": parameters,
            },
        },
    }

    # Apply button style colors
    style = button.get("style")
    if style == "primary":
        # Blue color for primary
        google_button["color"] = {"red": 0.2, "green": 0.5, "blue": 0.9}
    elif style == "danger":
        # Red color for danger
        google_button["color"] = {"red": 0.9, "green": 0.2, "blue": 0.2}

    if button.get("disabled"):
        google_button["disabled"] = True

    return google_button


def _convert_link_button_to_google_button(
    button: dict[str, Any],
) -> dict[str, Any]:
    """Convert a link button element to a Google Chat link button."""
    google_button: dict[str, Any] = {
        "text": convert_emoji(button.get("label", "")),
        "onClick": {
            "openLink": {
                "url": button.get("url", ""),
            },
        },
    }

    # Apply button style colors
    style = button.get("style")
    if style == "primary":
        google_button["color"] = {"red": 0.2, "green": 0.5, "blue": 0.9}
    elif style == "danger":
        google_button["color"] = {"red": 0.9, "green": 0.2, "blue": 0.2}

    return google_button


def _convert_section_to_widgets(
    element: dict[str, Any],
    endpoint_url: str | None = None,
) -> list[dict[str, Any]]:
    """Convert a section element to widgets."""
    widgets: list[dict[str, Any]] = []
    for child in element.get("children", []):
        widgets.extend(_convert_child_to_widgets(child, endpoint_url))
    return widgets


def _convert_table_to_widget(element: dict[str, Any]) -> dict[str, Any]:
    """Convert a table element to a widget.

    Renders as monospace text (ASCII table) in a TextParagraph widget.
    """
    headers = element.get("headers", [])
    rows = element.get("rows", [])
    ascii_table = table_element_to_ascii(headers, rows)
    return {
        "textParagraph": {
            "text": f'<font face="monospace">{ascii_table}</font>',
        },
    }


def _convert_fields_to_widgets(
    element: dict[str, Any],
) -> list[dict[str, Any]]:
    """Convert fields element to decorated text widgets."""
    return [
        {
            "decoratedText": {
                "topLabel": _render_markdown_as_gchat(convert_emoji(field.get("label", ""))),
                "text": _render_markdown_as_gchat(convert_emoji(field.get("value", ""))),
            },
        }
        for field in element.get("children", [])
    ]


def card_to_fallback_text(card: CardElement) -> str:
    """Generate fallback text from a card element.

    Used when cards aren't supported.
    """
    return shared_card_to_fallback_text(
        card,
        bold_format="*",
        line_break="\n",
        platform="gchat",
    )
