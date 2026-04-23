"""Convert CardElement to GitHub-flavored markdown.

Since GitHub doesn't support rich cards natively, we render cards
as formatted markdown with bold text, dividers, and links.
"""

from __future__ import annotations

from typing import Any, cast

from chat_sdk.cards import (
    CardChild,
    CardElement,
    card_child_to_fallback_text,
)
from chat_sdk.shared import render_gfm_table


def card_to_github_markdown(card: CardElement) -> str:
    """Convert a CardElement to GitHub-flavored markdown.

    Cards are rendered as clean markdown with:
    - Bold title and subtitle
    - Text content
    - Fields as key-value pairs
    - Buttons as markdown links (action buttons become bold text since
      GitHub has no interactivity)

    Example::

        card = {
            "type": "card",
            "title": "Order #1234",
            "subtitle": "Status update",
            "children": [
                {"type": "text", "content": "Your order has been shipped!"},
                {"type": "fields", "children": [
                    {"type": "field", "label": "Tracking", "value": "ABC123"},
                ]},
                {"type": "actions", "children": [
                    {"type": "link-button", "url": "https://track.example.com",
                     "label": "Track Order"},
                ]},
            ],
        }

        # Output:
        # **Order \\#1234**
        # Status update
        #
        # Your order has been shipped!
        #
        # **Tracking:** ABC123
        #
        # [Track Order](https://track.example.com)
    """
    lines: list[str] = []

    # Title (bold)
    title = card.get("title")
    if title:
        lines.append(f"**{_escape_markdown(title)}**")

    # Subtitle
    subtitle = card.get("subtitle")
    if subtitle:
        lines.append(_escape_markdown(subtitle))

    # Add spacing after header if there are children
    children = card.get("children", [])
    if (title or subtitle) and len(children) > 0:
        lines.append("")

    # Header image
    image_url = card.get("image_url")
    if image_url:
        lines.append(f"![]({image_url})")
        lines.append("")

    # Children
    for i, child in enumerate(children):
        child_lines = _render_child(child)

        if child_lines:
            lines.extend(child_lines)

            # Add spacing between children (except last)
            if i < len(children) - 1:
                lines.append("")

    return "\n".join(lines)


def _render_child(child: CardChild) -> list[str]:
    """Render a card child element to markdown lines.

    The per-type helpers below accept `dict[str, Any]` because they access
    dynamic keys (`content`, `label`, etc.) that are specific to one
    variant of the `CardChild` union. The narrowing happens via the
    `child_type` check, so casting to `dict[str, Any]` at the call sites
    is safe and keeps the helpers simple.
    """
    child_type = child.get("type", "")

    if child_type == "text":
        return _render_text(cast("dict[str, Any]", child))

    if child_type == "fields":
        return _render_fields(cast("dict[str, Any]", child))

    if child_type == "actions":
        return _render_actions(cast("dict[str, Any]", child))

    if child_type == "section":
        # Flatten section children
        result: list[str] = []
        section_children = cast("list[CardChild]", child.get("children", []))
        for section_child in section_children:
            result.extend(_render_child(section_child))
        return result

    if child_type == "image":
        alt = cast("str", child.get("alt", ""))
        url = cast("str", child.get("url", ""))
        if alt:
            return [f"![{_escape_markdown(alt)}]({url})"]
        return [f"![]({url})"]

    if child_type == "link":
        label = cast("str", child.get("label", ""))
        url = cast("str", child.get("url", ""))
        return [f"[{_escape_markdown(label)}]({url})"]

    if child_type == "divider":
        return ["---"]

    if child_type == "table":
        return _render_table(cast("dict[str, Any]", child))

    # Fallback
    text = card_child_to_fallback_text(child)
    if text:
        return [text]
    return []


def _render_text(text: dict[str, Any]) -> list[str]:
    """Render text element."""
    content = text.get("content", "")
    style = text.get("style")

    if style == "bold":
        return [f"**{content}**"]
    if style == "muted":
        # Use italic for muted text
        return [f"_{content}_"]
    return [content]


def _render_fields(fields: dict[str, Any]) -> list[str]:
    """Render fields as key-value pairs."""
    return [
        f"**{_escape_markdown(f.get('label', ''))}:** {_escape_markdown(f.get('value', ''))}"
        for f in fields.get("children", [])
    ]


def _render_table(table: dict[str, Any]) -> list[str]:
    """Render table as GFM markdown table."""
    headers = table.get("headers", [])
    rows = table.get("rows", [])
    return render_gfm_table(headers, rows)


def _render_actions(actions: dict[str, Any]) -> list[str]:
    """Render actions (buttons) as markdown links or bold text."""
    button_texts: list[str] = []
    for button in actions.get("children", []):
        if button.get("type") == "link-button":
            # Link buttons become markdown links
            label = _escape_markdown(button.get("label", ""))
            url = button.get("url", "")
            button_texts.append(f"[{label}]({url})")
        else:
            # Action buttons become bold text (no interactivity in GitHub comments)
            label = _escape_markdown(button.get("label", ""))
            button_texts.append(f"**[{label}]**")

    # Join buttons with separator
    return [" \u2022 ".join(button_texts)]


def _escape_markdown(text: str) -> str:
    r"""Escape special markdown characters in text.

    Only escapes characters that could break the formatting.
    Deliberately light-handed to preserve intentional markdown.
    Backslash must be escaped first to avoid double-escaping.
    """
    return text.replace("\\", "\\\\").replace("*", "\\*").replace("_", "\\_").replace("[", "\\[").replace("]", "\\]")


def card_to_plain_text(card: CardElement) -> str:
    """Generate plain text fallback from a card (no markdown).

    Used for alt text or plain text contexts.
    """
    parts: list[str] = []

    title = card.get("title")
    if title:
        parts.append(title)

    subtitle = card.get("subtitle")
    if subtitle:
        parts.append(subtitle)

    for child in card.get("children", []):
        text = _child_to_plain_text(child)
        if text:
            parts.append(text)

    return "\n".join(parts)


def _child_to_plain_text(child: CardChild) -> str | None:
    """Convert card child to plain text."""
    child_type = child.get("type", "")

    if child_type == "text":
        return child.get("content", "")  # type: ignore[union-attr]

    if child_type == "fields":
        return "\n".join(
            f"{f.get('label', '')}: {f.get('value', '')}"
            for f in child.get("children", [])  # type: ignore[union-attr]
        )

    if child_type == "actions":
        # Actions are interactive-only - exclude from fallback text.
        return None

    if child_type == "table":
        return "\n".join(_render_table(cast("dict[str, Any]", child)))

    if child_type == "section":
        return "\n".join(
            filter(
                None,
                (_child_to_plain_text(c) for c in child.get("children", [])),  # type: ignore[union-attr]
            )
        )

    return card_child_to_fallback_text(child)
