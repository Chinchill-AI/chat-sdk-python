"""Shared card conversion utilities for adapters."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal, cast

from chat_sdk.cards import CardChild, CardElement, card_child_to_fallback_text, table_element_to_ascii
from chat_sdk.emoji import convert_emoji_placeholders

PlatformName = Literal["slack", "gchat", "teams", "discord"]

BUTTON_STYLE_MAPPINGS: dict[PlatformName, dict[str, str]] = {
    "slack": {"primary": "primary", "danger": "danger"},
    "gchat": {"primary": "primary", "danger": "danger"},
    "teams": {"primary": "positive", "danger": "destructive"},
    "discord": {"primary": "primary", "danger": "danger"},
}


def create_emoji_converter(platform: PlatformName) -> Callable[[str], str]:
    """Create a platform-specific emoji converter function."""

    def converter(text: str) -> str:
        return convert_emoji_placeholders(text, platform)

    return converter


def map_button_style(style: str | None, platform: PlatformName) -> str | None:
    """Map a button style to the platform-specific value."""
    if not style:
        return None
    return BUTTON_STYLE_MAPPINGS.get(platform, {}).get(style)


def card_to_fallback_text(
    card: CardElement,
    *,
    bold_format: str = "*",
    line_break: str = "\n",
    platform: PlatformName | None = None,
) -> str:
    """Generate fallback plain text from a card element."""
    convert_text = create_emoji_converter(platform) if platform else (lambda t: t)

    parts: list[str] = []
    title = card.get("title")
    if title:
        parts.append(f"{bold_format}{convert_text(title)}{bold_format}")

    subtitle = card.get("subtitle")
    if subtitle:
        parts.append(convert_text(subtitle))

    for child in card.get("children", []):
        text = _child_to_fallback_text(child, convert_text)
        if text:
            parts.append(text)

    return line_break.join(parts)


def _child_to_fallback_text(child: CardChild, convert_text: Callable[[str], str]) -> str | None:
    """Convert a card child element to fallback text.

    `child` is a `CardChild` union of TypedDicts; the `child_type` check
    narrows at runtime but pyrefly returns `object | str` from `.get()`
    on the union, so we `cast` on each branch to the specific value type.
    """
    child_type = child.get("type", "")
    if child_type == "text":
        return convert_text(cast("str", child.get("content", "")))
    if child_type == "link":
        return f"{convert_text(cast('str', child.get('label', '')))} ({cast('str', child.get('url', ''))})"
    if child_type == "fields":
        children = cast("list[dict[str, str]]", child.get("children", []))
        return "\n".join(f"{convert_text(f['label'])}: {convert_text(f['value'])}" for f in children)
    if child_type == "actions":
        return None
    if child_type == "section":
        section_children = cast("list[CardChild]", child.get("children", []))
        return "\n".join(filter(None, (_child_to_fallback_text(c, convert_text) for c in section_children)))
    if child_type == "table":
        return table_element_to_ascii(
            cast("list[str]", child.get("headers", [])),
            cast("list[list[str]]", child.get("rows", [])),
        )
    if child_type == "divider":
        return "---"
    return card_child_to_fallback_text(child)


def escape_table_cell(value: str) -> str:
    """Escape a cell value for use in a GFM pipe table."""
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def render_gfm_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    """Render a GFM markdown table with properly escaped cells."""
    escaped_headers = [escape_table_cell(h) for h in headers]
    lines: list[str] = []
    lines.append(f"| {' | '.join(escaped_headers)} |")
    lines.append(f"| {' | '.join('---' for _ in escaped_headers)} |")
    for row in rows:
        cells = [escape_table_cell(c) for c in row]
        lines.append(f"| {' | '.join(cells)} |")
    return lines
