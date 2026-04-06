"""Discord Embed and Component converter for cross-platform cards.

Converts CardElement to Discord Embeds and Action Row Components.
See: https://discord.com/developers/docs/resources/message#embed-object
See: https://discord.com/developers/docs/interactions/message-components
"""

from __future__ import annotations

from typing import Any

from chat_sdk.adapters.discord.types import DiscordActionRow, DiscordButton
from chat_sdk.cards import (
    ActionsElement,
    ButtonElement,
    CardChild,
    CardElement,
    FieldsElement,
    LinkButtonElement,
    SectionElement,
    TextElement,
    card_child_to_fallback_text,
)
from chat_sdk.emoji import convert_emoji_placeholders
from chat_sdk.shared.card_utils import render_gfm_table

# Discord button styles (discord-api-types/v10 ButtonStyle)
BUTTON_STYLE_PRIMARY = 1
BUTTON_STYLE_SECONDARY = 2
BUTTON_STYLE_DANGER = 4
BUTTON_STYLE_LINK = 5

# Discord blurple color
DISCORD_BLURPLE = 0x5865F2


def _convert_emoji(text: str) -> str:
    """Convert emoji placeholders to Discord format."""
    return convert_emoji_placeholders(text, "discord")


def card_to_discord_payload(card: CardElement) -> dict[str, Any]:
    """Convert a CardElement to Discord message payload (embeds + components).

    Returns a dict with 'embeds' (list) and 'components' (list of action rows).
    """
    embed: dict[str, Any] = {}
    fields: list[dict[str, Any]] = []
    components: list[DiscordActionRow] = []

    # Set title and description (with emoji conversion)
    title = card.get("title")
    if title:
        embed["title"] = _convert_emoji(title)

    subtitle = card.get("subtitle")
    if subtitle:
        embed["description"] = _convert_emoji(subtitle)

    # Set header image
    image_url = card.get("image_url") or card.get("imageUrl")
    if image_url:
        embed["image"] = {"url": image_url}

    # Set color (default to Discord blurple)
    embed["color"] = DISCORD_BLURPLE

    # Process children
    text_parts: list[str] = []

    for child in card.get("children", []):
        _process_child(child, text_parts, fields, components)

    # If we have text parts and no description, set them as description
    if text_parts:
        if embed.get("description"):
            joined = "\n\n".join(text_parts)
            embed["description"] += f"\n\n{joined}"
        else:
            embed["description"] = "\n\n".join(text_parts)

    # Add fields if we have any
    if fields:
        embed["fields"] = fields

    return {
        "embeds": [embed],
        "components": components,
    }


def _process_child(
    child: CardChild,
    text_parts: list[str],
    fields: list[dict[str, Any]],
    components: list[DiscordActionRow],
) -> None:
    """Process a card child element."""
    child_type = child.get("type", "")

    if child_type == "text":
        text_parts.append(_convert_text_element(child))  # type: ignore[arg-type]
    elif child_type == "image":
        # Discord embeds can only have one image, handled at card level
        pass
    elif child_type == "divider":
        text_parts.append("\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500")
    elif child_type == "actions":
        components.extend(_convert_actions_to_rows(child))  # type: ignore[arg-type]
    elif child_type == "section":
        _process_section_element(child, text_parts, fields, components)  # type: ignore[arg-type]
    elif child_type == "fields":
        _convert_fields_element(child, fields)  # type: ignore[arg-type]
    elif child_type == "link":
        label = child.get("label", "")  # type: ignore[union-attr]
        url = child.get("url", "")  # type: ignore[union-attr]
        text_parts.append(f"[{_convert_emoji(label)}]({url})")
    elif child_type == "table":
        headers = child.get("headers", [])  # type: ignore[union-attr]
        rows = child.get("rows", [])  # type: ignore[union-attr]
        text_parts.append("\n".join(render_gfm_table(headers, rows)))
    else:
        text = card_child_to_fallback_text(child)
        if text:
            text_parts.append(text)


def _convert_text_element(element: TextElement) -> str:
    """Convert a text element to Discord markdown."""
    content = element.get("content", "")
    text = _convert_emoji(content)
    style = element.get("style", "")

    if style == "bold":
        text = f"**{text}**"
    elif style == "muted":
        # Discord doesn't have muted, use italic as approximation
        text = f"*{text}*"

    return text


def _convert_actions_to_rows(element: ActionsElement) -> list[DiscordActionRow]:
    """Convert an actions element to Discord action rows.

    Discord limits each action row to 5 components, so we chunk buttons.
    """
    buttons: list[DiscordButton] = []
    for child in element.get("children", []):
        child_type = child.get("type", "")
        if child_type == "link-button":
            buttons.append(_convert_link_button_element(child))  # type: ignore[arg-type]
        elif child_type == "button":
            buttons.append(_convert_button_element(child))  # type: ignore[arg-type]

    # Discord allows max 5 buttons per action row
    rows: list[DiscordActionRow] = []
    for i in range(0, len(buttons), 5):
        rows.append(
            {
                "type": 1,  # Action Row
                "components": buttons[i : i + 5],
            }
        )
    return rows


def _convert_button_element(button: ButtonElement) -> DiscordButton:
    """Convert a button element to a Discord button."""
    discord_button: DiscordButton = {
        "type": 2,  # Button
        "style": _get_button_style(button.get("style")),
        "label": button.get("label", ""),
        "custom_id": button.get("id", ""),
    }

    if button.get("disabled"):
        discord_button["disabled"] = True

    return discord_button


def _convert_link_button_element(button: LinkButtonElement) -> DiscordButton:
    """Convert a link button element to a Discord link button."""
    return {
        "type": 2,  # Button
        "style": BUTTON_STYLE_LINK,
        "label": button.get("label", ""),
        "url": button.get("url", ""),
    }


def _get_button_style(style: str | None) -> int:
    """Map button style to Discord button style."""
    if style == "primary":
        return BUTTON_STYLE_PRIMARY
    if style == "danger":
        return BUTTON_STYLE_DANGER
    return BUTTON_STYLE_SECONDARY


def _process_section_element(
    element: SectionElement,
    text_parts: list[str],
    fields: list[dict[str, Any]],
    components: list[DiscordActionRow],
) -> None:
    """Process a section element."""
    for child in element.get("children", []):
        _process_child(child, text_parts, fields, components)


def _convert_fields_element(
    element: FieldsElement,
    fields: list[dict[str, Any]],
) -> None:
    """Convert fields element to Discord embed fields."""
    for f in element.get("children", []):
        fields.append(
            {
                "name": _convert_emoji(f.get("label", "")),
                "value": _convert_emoji(f.get("value", "")),
                "inline": True,
            }
        )


def card_to_fallback_text(card: CardElement) -> str:
    """Generate fallback text from a card element.

    Used when embeds aren't supported or for notifications.
    """
    parts: list[str] = []

    title = card.get("title")
    if title:
        parts.append(f"**{_convert_emoji(title)}**")

    subtitle = card.get("subtitle")
    if subtitle:
        parts.append(_convert_emoji(subtitle))

    for child in card.get("children", []):
        text = _child_to_fallback_text(child)
        if text:
            parts.append(text)

    return "\n\n".join(parts)


def _child_to_fallback_text(child: CardChild) -> str | None:
    """Convert a card child element to fallback text."""
    child_type = child.get("type", "")

    if child_type == "text":
        return _convert_emoji(child.get("content", ""))  # type: ignore[union-attr]
    if child_type == "fields":
        return "\n".join(
            f"**{_convert_emoji(f.get('label', ''))}**: {_convert_emoji(f.get('value', ''))}"
            for f in child.get("children", [])  # type: ignore[union-attr]
        )
    if child_type == "actions":
        # Actions are interactive-only -- exclude from fallback text.
        return None
    if child_type == "section":
        return "\n".join(
            t
            for c in child.get("children", [])  # type: ignore[union-attr]
            if (t := _child_to_fallback_text(c))
        )
    if child_type == "table":
        headers = child.get("headers", [])  # type: ignore[union-attr]
        rows = child.get("rows", [])  # type: ignore[union-attr]
        from chat_sdk.shared.base_format_converter import table_to_ascii

        return f"```\n{table_to_ascii({'type': 'table', 'children': [], 'headers': headers, 'rows': rows})}\n```"
    if child_type == "divider":
        return "---"

    return card_child_to_fallback_text(child)
