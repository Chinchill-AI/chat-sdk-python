"""Card elements for cross-platform rich messaging."""

from __future__ import annotations

from typing import Any, Literal, TypedDict, TypeGuard

# Button style options
ButtonStyle = Literal["primary", "danger", "default"]

# Text style options
TextStyle = Literal["plain", "bold", "muted"]

# Table column alignment
TableAlignment = Literal["left", "center", "right"]


class _ButtonRequired(TypedDict):
    """Required fields for ButtonElement."""

    type: str  # "button"
    id: str
    label: str


class ButtonElement(_ButtonRequired, total=False):
    """Button element for interactive actions."""

    style: ButtonStyle
    value: str
    disabled: bool
    action_type: Literal["action", "modal"] | None


class _LinkButtonRequired(TypedDict):
    """Required fields for LinkButtonElement."""

    type: str  # "link-button"
    label: str
    url: str


class LinkButtonElement(_LinkButtonRequired, total=False):
    """Link button element that opens a URL."""

    style: ButtonStyle


class _TextRequired(TypedDict):
    """Required fields for TextElement."""

    type: str  # "text"
    content: str


class TextElement(_TextRequired, total=False):
    """Text content element."""

    style: TextStyle


class _ImageRequired(TypedDict):
    """Required fields for ImageElement."""

    type: str  # "image"
    url: str


class ImageElement(_ImageRequired, total=False):
    """Image element."""

    alt: str


class DividerElement(TypedDict):
    """Visual divider/separator."""

    type: str  # "divider"


class FieldElement(TypedDict):
    """Field for key-value display."""

    type: str  # "field"
    label: str
    value: str


class FieldsElement(TypedDict):
    """Fields container for multi-column layout."""

    type: str  # "fields"
    children: list[FieldElement]


class LinkElement(TypedDict):
    """Inline hyperlink element."""

    type: str  # "link"
    label: str
    url: str


class TableElement(TypedDict, total=False):
    """Table element for structured data display."""

    type: str  # "table"
    headers: list[str]
    rows: list[list[str]]
    align: list[TableAlignment]


class ActionsElement(TypedDict):
    """Container for action buttons and selects."""

    type: str  # "actions"
    children: list[Any]  # ButtonElement | LinkButtonElement | SelectElement | RadioSelectElement


class SectionElement(TypedDict):
    """Section container for grouping elements."""

    type: str  # "section"
    children: list[Any]  # CardChild (forward ref)


# Union of all card child element types
CardChild = (
    TextElement
    | ImageElement
    | DividerElement
    | ActionsElement
    | SectionElement
    | FieldsElement
    | LinkElement
    | TableElement
)


class CardElement(TypedDict, total=False):
    """Root card element."""

    type: str  # "card"
    title: str
    subtitle: str
    image_url: str
    children: list[CardChild]


def is_card_element(value: Any) -> TypeGuard[CardElement]:
    """Check if a value is a CardElement."""
    return isinstance(value, dict) and value.get("type") == "card"


def table_element_to_ascii(headers: list[str], rows: list[list[str]]) -> str:
    """Render headers + rows as a padded ASCII table.

    Delegates to :func:`chat_sdk.shared.markdown_parser.table_element_to_ascii`,
    which is the canonical implementation shared by card fallback rendering
    and mdast table nodes.
    """
    from chat_sdk.shared.markdown_parser import (
        table_element_to_ascii as _impl,
    )

    return _impl(headers, rows)


# ============================================================================
# Builder Functions (PascalCase primary — matches source TS SDK)
# ============================================================================


def Card(
    *,
    title: str | None = None,
    subtitle: str | None = None,
    image_url: str | None = None,
    children: list[CardChild] | None = None,
) -> CardElement:
    """Create a Card element.

    Example::

        Card(title="Welcome", children=[Text("Hello!")])
    """
    element: CardElement = {"type": "card", "children": children or []}
    if title is not None:
        element["title"] = title
    if subtitle is not None:
        element["subtitle"] = subtitle
    if image_url is not None:
        element["image_url"] = image_url
    return element


def Text(content: str, *, style: TextStyle | None = None) -> TextElement:
    """Create a Text element.

    Example::

        Text("Hello, world!")
        Text("Important", style="bold")
    """
    element: TextElement = {"type": "text", "content": content}
    if style is not None:
        element["style"] = style
    return element


def Image(*, url: str, alt: str | None = None) -> ImageElement:
    """Create an Image element.

    Example::

        Image(url="https://example.com/image.png", alt="Description")
    """
    element: ImageElement = {"type": "image", "url": url}
    if alt is not None:
        element["alt"] = alt
    return element


def Divider() -> DividerElement:
    """Create a Divider element."""
    return {"type": "divider"}


def Section(children: list[CardChild]) -> SectionElement:
    """Create a Section container.

    Example::

        Section([Text("Grouped content"), Image(url="...")])
    """
    return {"type": "section", "children": children}


def Actions(children: list[Any]) -> ActionsElement:
    """Create an Actions container for buttons and selects.

    Example::

        Actions([
            Button(id="ok", label="OK"),
            Button(id="cancel", label="Cancel"),
        ])
    """
    return {"type": "actions", "children": children}


def Button(
    *,
    id: str,
    label: str,
    style: ButtonStyle | None = None,
    value: str | None = None,
    disabled: bool | None = None,
    action_type: Literal["action", "modal"] | None = None,
) -> ButtonElement:
    """Create a Button element.

    Example::

        Button(id="submit", label="Submit", style="primary")
        Button(id="delete", label="Delete", style="danger", value="item-123")
        Button(id="open", label="Open", action_type="modal")
    """
    element: ButtonElement = {"type": "button", "id": id, "label": label}
    if style is not None:
        element["style"] = style
    if value is not None:
        element["value"] = value
    if disabled is not None:
        element["disabled"] = disabled
    if action_type is not None:
        element["action_type"] = action_type
    return element


def LinkButton(
    *,
    url: str,
    label: str,
    style: ButtonStyle | None = None,
) -> LinkButtonElement:
    """Create a LinkButton element that opens a URL when clicked.

    Example::

        LinkButton(url="https://example.com", label="View Docs")
    """
    element: LinkButtonElement = {"type": "link-button", "url": url, "label": label}
    if style is not None:
        element["style"] = style
    return element


def Field(*, label: str, value: str) -> FieldElement:
    """Create a Field element for key-value display.

    Example::

        Field(label="Status", value="Active")
    """
    return {"type": "field", "label": label, "value": value}


def Fields(children: list[FieldElement]) -> FieldsElement:
    """Create a Fields container for multi-column layout.

    Example::

        Fields([
            Field(label="Name", value="John"),
            Field(label="Email", value="john@example.com"),
        ])
    """
    return {"type": "fields", "children": children}


def Table(
    *,
    headers: list[str],
    rows: list[list[str]],
    align: list[TableAlignment] | None = None,
) -> TableElement:
    """Create a Table element for structured data display.

    Example::

        Table(
            headers=["Name", "Age", "Role"],
            rows=[["Alice", "30", "Engineer"], ["Bob", "25", "Designer"]],
        )
    """
    element: TableElement = {"type": "table", "headers": headers, "rows": rows}
    if align is not None:
        element["align"] = align
    return element


def CardLink(*, url: str, label: str) -> LinkElement:
    """Create a CardLink element for inline hyperlinks.

    Example::

        CardLink(url="https://example.com", label="Visit Site")
    """
    return {"type": "link", "url": url, "label": label}


def CardText(content: str, *, style: TextStyle | None = None) -> TextElement:
    """Alias for :func:`Text` to avoid conflicts with builtins."""
    return Text(content, style=style)


# ============================================================================
# snake_case aliases for PEP 8 purists
# ============================================================================

card = Card
text_element = Text
image = Image
divider = Divider
section = Section
actions = Actions
button = Button
link_button = LinkButton
field = Field
fields = Fields
table = Table
card_link = CardLink
card_text = CardText


# ============================================================================
# Fallback Text Generation
# ============================================================================


def card_to_fallback_text(card: CardElement) -> str:
    """Generate plain text fallback from a CardElement.

    Used for platforms/clients that can't render rich cards,
    and for the ``SentMessage.text`` property.
    """
    parts: list[str] = []

    title = card.get("title")
    if title:
        parts.append(f"**{title}**")

    subtitle = card.get("subtitle")
    if subtitle:
        parts.append(subtitle)

    for child in card.get("children", []):
        text = card_child_to_fallback_text(child)
        if text:
            parts.append(text)

    return "\n".join(parts)


def card_child_to_fallback_text(child: CardChild) -> str | None:
    """Convert a card child to fallback text."""
    child_type = child.get("type", "")
    if child_type == "text":
        return child.get("content", "")  # type: ignore[union-attr]
    if child_type == "link":
        return f"{child.get('label', '')} ({child.get('url', '')})"  # type: ignore[union-attr]
    if child_type == "fields":
        return "\n".join(
            f"{f['label']}: {f['value']}"
            for f in child.get("children", [])  # type: ignore[union-attr]
        )
    if child_type == "divider":
        return None
    if child_type == "table":
        return table_element_to_ascii(
            child.get("headers", []),  # type: ignore[union-attr]
            child.get("rows", []),  # type: ignore[union-attr]
        )
    if child_type == "section":
        parts = []
        for c in child.get("children", []):  # type: ignore[union-attr]
            text = card_child_to_fallback_text(c)
            if text:
                parts.append(text)
        return "\n".join(parts)
    if child_type == "image":
        return None
    return None
