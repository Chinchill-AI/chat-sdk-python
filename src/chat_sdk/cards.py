"""Card elements for cross-platform rich messaging."""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any, Literal, TypedDict

# Button style options
ButtonStyle = Literal["primary", "danger", "default"]

# Text style options
TextStyle = Literal["plain", "bold", "muted"]

# Table column alignment
TableAlignment = Literal["left", "center", "right"]

# Vertical alignment of table cell content (rendered by Teams only)
TableVerticalAlignment = Literal["top", "center", "bottom"]

# Style of the grid lines drawn between table cells (rendered by Teams only)
TableGridStyle = Literal["default", "emphasis", "accent", "good", "attention", "warning"]

# Card width hint (rendered by Teams only)
CardWidth = Literal["default", "full"]


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
    # URL to POST action data to when this button is clicked
    callback_url: str
    # Hover text for the button. Rendered by Teams only; other adapters ignore it
    tooltip: str


class _LinkButtonRequired(TypedDict):
    """Required fields for LinkButtonElement."""

    type: str  # "link-button"
    label: str
    url: str


class LinkButtonElement(_LinkButtonRequired, total=False):
    """Link button element that opens a URL."""

    # Optional action identifier emitted by platforms that report link clicks
    id: str
    style: ButtonStyle
    # Hover text for the button. Rendered by Teams only; other adapters ignore it
    tooltip: str


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
    # Accessible table caption (used by platforms with native table support)
    caption: str
    # Rows per page on platforms that paginate tables (Slack: 1-100, default 5)
    page_size: int
    # Relative column widths, one positive integer weight per column.
    # Rendered by Teams only; other adapters ignore it
    widths: list[int]
    # Vertical alignment of cell content. Rendered by Teams only
    vertical_align: TableVerticalAlignment
    # Draw grid lines between cells (default true). Rendered by Teams only
    grid_lines: bool
    # Style of the grid lines between cells. Rendered by Teams only
    grid_style: TableGridStyle


class ChartSegment(TypedDict):
    """Chart segment for pie charts."""

    # Legend label (Slack: max 20 characters)
    label: str
    # Segment value; must be greater than 0. Rendered as a percentage of the total.
    value: float


class ChartDataPoint(TypedDict):
    """A single data point within a chart series."""

    # Category label; must match an entry in the chart's ``categories``
    label: str
    # Y-axis value (negative values are permitted)
    value: float


class ChartSeries(TypedDict):
    """A named data series for bar, area, and line charts."""

    # One data point per category
    data: list[ChartDataPoint]
    # Legend label; must be unique within the chart (Slack: max 20 characters)
    name: str


class PieChartDefinition(TypedDict):
    """Pie chart definition."""

    type: Literal["pie"]
    # Pie segments (Slack: 1-12)
    segments: list[ChartSegment]


class _SeriesChartDefinitionRequired(TypedDict):
    """Required fields for SeriesChartDefinition."""

    type: Literal["area", "bar", "line"]
    # X-axis category labels in display order (Slack: max 20 characters each)
    categories: list[str]
    # Data series (Slack: 1-12); each series needs one point per category
    series: list[ChartSeries]


class SeriesChartDefinition(_SeriesChartDefinitionRequired, total=False):
    """Bar, area, or line chart definition."""

    # X-axis title (Slack: max 50 characters)
    x_label: str
    # Y-axis title (Slack: max 50 characters)
    y_label: str


# Chart definition, discriminated by chart ``type``
ChartDefinition = PieChartDefinition | SeriesChartDefinition


class ChartElement(TypedDict):
    """Chart element for data visualization."""

    type: str  # "chart"
    # Chart title (Slack: max 50 characters)
    title: str
    chart: ChartDefinition


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
    | ChartElement
)


class CardElement(TypedDict, total=False):
    """Root card element."""

    type: str  # "card"
    title: str
    subtitle: str
    image_url: str
    # Width hint for platforms that can render a card wider than the default (Teams)
    width: CardWidth
    children: list[CardChild]


def is_card_element(value: Any) -> bool:
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


def _js_number_to_string(value: Any) -> str:
    """Format a chart value the way JavaScript's ``String(value)`` does.

    Upstream renders chart values with ``String(v)``, so ``45`` and ``45.0``
    both render as ``"45"`` and ``0.00001`` as ``"0.00001"`` (Python's
    ``str`` gives ``"45.0"`` / ``"1e-05"``). Implements ECMAScript
    ``Number::toString`` over the shortest round-trip digits, which
    ``repr(float)`` also produces. ``bool`` is not treated as a number
    (JS ``String(true)`` is ``"true"``); ``None`` (no value) renders as ``""``.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        if abs(value) < 10**21:
            return str(value)
        # JS switches to exponent notation at 1e21; format as the double JS would hold.
        try:
            value = float(value)
        except OverflowError:
            return "Infinity" if value > 0 else "-Infinity"
    if not isinstance(value, float):
        return str(value)
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    if value == 0:
        return "0"  # also -0.0: JS String(-0) is "0"
    sign = "-" if value < 0 else ""
    _, digit_tuple, exponent = Decimal(repr(abs(value))).as_tuple()
    digits = "".join(map(str, digit_tuple)).rstrip("0")
    k = len(digits)
    # value == digits * 10**(n - k)  (ECMAScript's s, k and n)
    n = int(exponent) + len(digit_tuple)
    if k <= n <= 21:
        return sign + digits + "0" * (n - k)
    if 0 < n <= 21:
        return sign + digits[:n] + "." + digits[n:]
    if -6 < n <= 0:
        return sign + "0." + "0" * (-n) + digits
    e = n - 1
    exp = f"e{'+' if e >= 0 else '-'}{abs(e)}"
    if k == 1:
        return sign + digits + exp
    return sign + digits[0] + "." + digits[1:] + exp


def chart_element_to_fallback_text(element: ChartElement) -> str:
    """Render a chart element as its title followed by its data as a padded ASCII table.

    Used for card :class:`ChartElement` fallback rendering on platforms
    without native chart support. A pie chart renders ``Label``/``Value``
    rows. A bar/area/line chart renders one row per category and one column
    per series; each series' point is looked up by category label (not by
    position), and a missing point renders as an empty cell.
    """
    definition: Any = element.get("chart", {})
    title = element.get("title", "")

    if definition.get("type") == "pie":
        table = table_element_to_ascii(
            ["Label", "Value"],
            [[s.get("label", ""), _js_number_to_string(s.get("value"))] for s in definition.get("segments", [])],
        )
        return f"{title}\n{table}"

    x_label = definition.get("x_label")
    series_list = definition.get("series", [])
    headers = [x_label if x_label is not None else "", *(s.get("name", "") for s in series_list)]
    rows: list[list[str]] = []
    for category in definition.get("categories", []):
        row = [category]
        for s in series_list:
            point = next((p for p in s.get("data", []) if p.get("label") == category), None)
            row.append(_js_number_to_string(point.get("value")) if point is not None else "")
        rows.append(row)
    return f"{title}\n{table_element_to_ascii(headers, rows)}"


# ============================================================================
# Builder Functions (PascalCase primary — matches source TS SDK)
# ============================================================================


def Card(
    *,
    title: str | None = None,
    subtitle: str | None = None,
    image_url: str | None = None,
    width: CardWidth | None = None,
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
    if width is not None:
        element["width"] = width
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
    callback_url: str | None = None,
    tooltip: str | None = None,
) -> ButtonElement:
    """Create a Button element.

    Example::

        Button(id="submit", label="Submit", style="primary")
        Button(id="delete", label="Delete", style="danger", value="item-123")
        Button(id="open", label="Open", action_type="modal")
        Button(id="approve", label="Approve", callback_url="https://example.com/hook")
        Button(id="ok", label="OK", tooltip="Confirm the order")

    ``tooltip`` is hover text rendered by Teams only; other adapters ignore it.
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
    if callback_url is not None:
        element["callback_url"] = callback_url
    if tooltip is not None:
        element["tooltip"] = tooltip
    return element


def LinkButton(
    *,
    url: str,
    label: str,
    style: ButtonStyle | None = None,
    id: str | None = None,
    tooltip: str | None = None,
) -> LinkButtonElement:
    """Create a LinkButton element that opens a URL when clicked.

    Example::

        LinkButton(url="https://example.com", label="View Docs")

    ``id`` is an optional action identifier emitted by platforms that report
    link clicks (matching the ``Button``/``Select`` ``id`` convention). Upstream
    sets ``id`` unconditionally and relies on ``JSON.stringify`` dropping
    ``undefined``; in Python we only write the key when it is provided so an
    unset id never serializes as ``null``. ``tooltip`` is hover text rendered
    by Teams only; other adapters ignore it.
    """
    element: LinkButtonElement = {"type": "link-button", "url": url, "label": label}
    if id is not None:
        element["id"] = id
    if style is not None:
        element["style"] = style
    if tooltip is not None:
        element["tooltip"] = tooltip
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
    caption: str | None = None,
    page_size: int | None = None,
    widths: list[int] | None = None,
    vertical_align: TableVerticalAlignment | None = None,
    grid_lines: bool | None = None,
    grid_style: TableGridStyle | None = None,
) -> TableElement:
    """Create a Table element for structured data display.

    Example::

        Table(
            headers=["Name", "Age", "Role"],
            rows=[["Alice", "30", "Engineer"], ["Bob", "25", "Designer"]],
        )

    ``caption`` and ``page_size`` are used by platforms with native table
    support (Slack paginates at ``page_size`` rows). On Teams the table renders
    as a native Adaptive Card table; ``widths``, ``vertical_align``,
    ``grid_lines`` and ``grid_style`` tune that rendering and are ignored
    elsewhere::

        Table(
            headers=["Service", "Status"],
            rows=[["api", "ok"]],
            widths=[3, 1],
            grid_style="emphasis",
        )

    Unset options are omitted from the element; falsy values such as
    ``grid_lines=False`` are kept.
    """
    element: TableElement = {"type": "table", "headers": headers, "rows": rows}
    if align is not None:
        element["align"] = align
    if caption is not None:
        element["caption"] = caption
    if page_size is not None:
        element["page_size"] = page_size
    if widths is not None:
        element["widths"] = widths
    if vertical_align is not None:
        element["vertical_align"] = vertical_align
    if grid_lines is not None:
        element["grid_lines"] = grid_lines
    if grid_style is not None:
        element["grid_style"] = grid_style
    return element


def Chart(*, title: str, chart: ChartDefinition) -> ChartElement:
    """Create a Chart element for data visualization.

    Pie chart::

        Chart(
            title="My Favorite Candy Bars",
            chart={
                "type": "pie",
                "segments": [
                    {"label": "Kit Kat", "value": 45},
                    {"label": "Twix", "value": 28},
                ],
            },
        )

    Line chart::

        Chart(
            title="Weekly Sales",
            chart={
                "type": "line",
                "categories": ["Week 1", "Week 2"],
                "x_label": "Week",
                "y_label": "Sales",
                "series": [
                    {
                        "name": "Scranton",
                        "data": [
                            {"label": "Week 1", "value": 120},
                            {"label": "Week 2", "value": 135},
                        ],
                    },
                ],
            },
        )

    Upstream does no validation here (Slack enforces its limits when
    rendering). Platforms without native chart support render the data as
    an ASCII table (see :func:`chart_element_to_fallback_text`).
    """
    return {"type": "chart", "title": title, "chart": chart}


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
chart = Chart
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
    if child_type == "chart":
        return chart_element_to_fallback_text(child)  # type: ignore[arg-type]
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
