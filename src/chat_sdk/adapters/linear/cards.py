"""Convert CardElement to Linear-compatible markdown.

Since Linear doesn't support rich cards natively, we render cards
as clean formatted markdown. Linear comments support standard
markdown syntax.

See: https://linear.app/docs/comment-on-issues
"""

from __future__ import annotations

from chat_sdk.cards import (
    ActionsElement,
    CardChild,
    CardElement,
    FieldsElement,
    TableElement,
    TextElement,
    card_child_to_fallback_text,
)
from chat_sdk.shared.card_utils import render_gfm_table


def _escape_markdown(text: str) -> str:
    """Escape special markdown characters in text."""
    # Backslash must be escaped first to avoid double-escaping
    return text.replace("\\", "\\\\").replace("*", "\\*").replace("_", "\\_").replace("[", "\\[").replace("]", "\\]")


def card_to_linear_markdown(card: CardElement) -> str:
    """Convert a CardElement to Linear-compatible markdown.

    Cards are rendered as clean markdown with:
    - Bold title and subtitle
    - Text content
    - Fields as key-value pairs
    - Buttons as markdown links (action buttons become bold text)
    """
    lines: list[str] = []

    title = card.get("title")
    if title:
        lines.append(f"**{_escape_markdown(title)}**")

    subtitle = card.get("subtitle")
    if subtitle:
        lines.append(_escape_markdown(subtitle))

    children = card.get("children", [])

    # Add spacing after header if there are children
    if (title or subtitle) and children:
        lines.append("")

    # Header image
    image_url = card.get("image_url") or card.get("imageUrl")
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
    """Render a card child element to markdown lines."""
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
            return [f"![{_escape_markdown(alt)}]({url})"]
        return [f"![]({url})"]
    if child_type == "link":
        label = child.get("label", "")  # type: ignore[union-attr]
        url = child.get("url", "")  # type: ignore[union-attr]
        return [f"[{_escape_markdown(label)}]({url})"]
    if child_type == "divider":
        return ["---"]
    if child_type == "table":
        return _render_table(child)  # type: ignore[arg-type]

    text = card_child_to_fallback_text(child)
    if text:
        return [text]
    return []


def _render_text(text: TextElement) -> list[str]:
    """Render text element."""
    content = text.get("content", "")
    style = text.get("style", "")

    if style == "bold":
        return [f"**{content}**"]
    if style == "muted":
        return [f"_{content}_"]
    return [content]


def _render_fields(fields: FieldsElement) -> list[str]:
    """Render fields as key-value pairs."""
    return [
        f"**{_escape_markdown(f.get('label', ''))}:** {_escape_markdown(f.get('value', ''))}"
        for f in fields.get("children", [])
    ]


def _render_table(table: TableElement) -> list[str]:
    """Render table as GFM markdown table."""
    headers = table.get("headers", [])
    rows = table.get("rows", [])
    return render_gfm_table(headers, rows)


def _render_actions(actions: ActionsElement) -> list[str]:
    """Render actions (buttons) as markdown links or bold text."""
    button_texts: list[str] = []
    for button in actions.get("children", []):
        if button.get("type") == "link-button":
            label = button.get("label", "")
            url = button.get("url", "")
            button_texts.append(f"[{_escape_markdown(label)}]({url})")
        else:
            label = button.get("label", "")
            button_texts.append(f"**[{_escape_markdown(label)}]**")

    return [" \u2022 ".join(button_texts)] if button_texts else []


def card_to_plain_text(card: CardElement) -> str:
    """Generate plain text fallback from a card (no markdown)."""
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
        # Actions are interactive-only -- exclude from fallback text.
        return None
    if child_type == "table":
        headers = child.get("headers", [])  # type: ignore[union-attr]
        rows = child.get("rows", [])  # type: ignore[union-attr]
        return "\n".join(render_gfm_table(headers, rows))
    if child_type == "section":
        parts = [
            _child_to_plain_text(c)
            for c in child.get("children", [])  # type: ignore[union-attr]
        ]
        return "\n".join(p for p in parts if p)

    return card_child_to_fallback_text(child)
