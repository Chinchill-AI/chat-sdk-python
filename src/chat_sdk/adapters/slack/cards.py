"""Slack Block Kit converter for cross-platform cards.

Converts CardElement to Slack Block Kit blocks.
Port of cards.ts from the Vercel Chat SDK Slack adapter.

@see https://api.slack.com/block-kit
"""

from __future__ import annotations

import math
from typing import Any, TypedDict

from chat_sdk.cards import (
    ActionsElement,
    ButtonElement,
    CardChild,
    CardElement,
    ChartElement,
    DividerElement,
    FieldsElement,
    ImageElement,
    LinkButtonElement,
    LinkElement,
    SectionElement,
    TableElement,
    TextElement,
    _chart_value_to_json_number,
    card_child_to_fallback_text,
    chart_element_to_fallback_text,
    table_element_to_ascii,
)
from chat_sdk.modals import SelectElement
from chat_sdk.shared import card_to_fallback_text as shared_card_to_fallback_text
from chat_sdk.shared import create_emoji_converter, map_button_style

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

    # Convert children -- track native table/chart block usage (Slack allows
    # at most one table block and two data_visualization blocks per message)
    state: _CardRenderState = {"used_native_table": False, "chart_count": 0}
    for child in card.get("children", []):
        child_blocks = _convert_child_to_blocks(child, state)
        blocks.extend(child_blocks)

    return blocks


class _CardRenderState(TypedDict):
    """Per-message rendering state for Slack's native block usage limits."""

    chart_count: int
    used_native_table: bool


def _convert_child_to_blocks(child: CardChild, state: _CardRenderState) -> list[SlackBlock]:
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
    if child_type == "chart":
        return [_convert_chart_to_block(child, state)]  # type: ignore[arg-type]

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
    # `??` semantics: an explicit (even empty-string) id is used verbatim;
    # only a missing/None id falls back to the URL-derived action_id.
    button_id = button.get("id")
    element: SlackLinkButtonElement_ = {
        "type": "button",
        "text": {
            "type": "plain_text",
            "text": convert_emoji(button.get("label", "")),
            "emoji": True,
        },
        "action_id": button_id if button_id is not None else f"link-{url[:200]}",
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


# Slack's section text object limit
_SECTION_TEXT_MAX_CHARS = 3000


def _ascii_fallback_block(content: str) -> SlackBlock:
    """Wrap ASCII fallback content in a fenced code block inside a section.

    Truncates the content so the section text stays within Slack's
    3,000-character limit while keeping the closing fence intact. Lengths are
    counted in code points; upstream counts UTF-16 code units (see
    docs/UPSTREAM_SYNC.md).
    """

    def fence(body: str) -> str:
        return f"```\n{body}\n```"

    budget = _SECTION_TEXT_MAX_CHARS - len(fence(""))
    text = fence(f"{content[: budget - 1]}…") if len(content) > budget else fence(content)
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


_DATA_TABLE_MAX_ROWS = 100
_DATA_TABLE_MAX_COLS = 20
# A single table (all cells combined) can't exceed 10,000 characters
_DATA_TABLE_MAX_CHARS = 10_000
_DATA_TABLE_MIN_PAGE_SIZE = 1
_DATA_TABLE_MAX_PAGE_SIZE = 100


def _clamp_page_size(page_size: float) -> int:
    """Upstream ``Math.min(100, Math.max(1, Math.floor(pageSize)))``.

    Clamps before flooring, which gives the same result for every finite value
    (the bounds are integers) and keeps ``inf`` / ``nan`` from raising in
    ``math.floor``.
    """
    return math.floor(max(_DATA_TABLE_MIN_PAGE_SIZE, min(_DATA_TABLE_MAX_PAGE_SIZE, page_size)))


def _convert_table_to_blocks(element: TableElement, state: _CardRenderState) -> list[SlackBlock]:
    """Convert a table element to Slack Block Kit blocks.

    Uses the data table block (paginated + sortable) with first-row-as-headers
    schema when the table has at least one data row. Falls back to the plain
    table block for header-only tables, and to an ASCII code block for tables
    exceeding Slack limits (100 data rows, 20 columns, 10,000 characters) or
    when a native table block has already been used in this message.

    @see https://docs.slack.dev/reference/block-kit/blocks/data-table-block/
    """
    headers = element.get("headers", [])
    rows = element.get("rows", [])
    # Divergence from upstream — see docs/UPSTREAM_SYNC.md: counts code points,
    # where upstream's ``.length`` counts UTF-16 code units.
    cell_char_count = sum(len(cell) for cell in headers) + sum(len(cell) for row in rows for cell in row)

    if (
        state["used_native_table"]
        or len(rows) > _DATA_TABLE_MAX_ROWS
        or len(headers) > _DATA_TABLE_MAX_COLS
        or cell_char_count > _DATA_TABLE_MAX_CHARS
    ):
        # Fall back to ASCII table in a code block
        return [_ascii_fallback_block(table_element_to_ascii(headers, rows))]

    state["used_native_table"] = True

    # First row is headers, subsequent rows are data
    header_row = [{"type": "raw_text", "text": convert_emoji(h) or " "} for h in headers]
    data_rows = [[{"type": "raw_text", "text": convert_emoji(cell) or " "} for cell in row] for row in rows]

    # The data table block requires a header row plus at least one data row
    if len(data_rows) == 0:
        return [{"type": "table", "rows": [header_row]}]

    block: SlackBlock = {
        "type": "data_table",
        # Upstream ``caption || "Table"``: an empty caption also gets the default.
        "caption": convert_emoji(element.get("caption") or "Table"),
        "rows": [header_row, *data_rows],
    }
    page_size = element.get("page_size")
    if page_size is not None:
        block["page_size"] = _clamp_page_size(page_size)
    return [block]


_CHART_MAX_TITLE_CHARS = 50
_CHART_MAX_LABEL_CHARS = 20
_CHART_MAX_SEGMENTS = 12
_CHART_MAX_SERIES = 12
_CHART_MAX_DATA_POINTS = 20
# Slack rejects messages with more than 2 data_visualization blocks
# (undocumented; enforced by the API as of July 2026)
_CHART_MAX_PER_MESSAGE = 2


def _convert_chart_to_block(element: ChartElement, state: _CardRenderState) -> SlackBlock:
    """Convert a chart element to a Slack data visualization block.

    Falls back to the chart's data rendered as an ASCII table in a code block
    when the chart violates Slack constraints (label lengths, series counts,
    category/data-point mismatches, more than 2 charts per message), since
    Slack rejects invalid charts outright rather than truncating them.

    @see https://docs.slack.dev/reference/block-kit/blocks/data-visualization-block/
    """
    block = _chart_to_data_visualization(element) if state["chart_count"] < _CHART_MAX_PER_MESSAGE else None
    if block is not None:
        state["chart_count"] += 1
        return block
    return _ascii_fallback_block(chart_element_to_fallback_text(element))


def _chart_to_data_visualization(element: ChartElement) -> SlackBlock | None:
    """Build a data_visualization block, or return ``None`` if the chart violates Slack constraints."""
    title = convert_emoji(element.get("title", ""))
    if len(title) == 0 or len(title) > _CHART_MAX_TITLE_CHARS:
        return None

    chart: Any = element.get("chart", {})

    if chart.get("type") == "pie":
        segments = chart.get("segments", [])
        # JSON-safe values: see ``_chart_value_to_json_number``.
        values = [_chart_value_to_json_number(segment["value"]) for segment in segments]
        valid_segments = 1 <= len(segments) <= _CHART_MAX_SEGMENTS and all(
            _is_valid_chart_label(segment["label"]) and value is not None and value > 0
            for segment, value in zip(segments, values, strict=True)
        )
        if not valid_segments:
            return None
        return {
            "type": "data_visualization",
            "title": title,
            "chart": {
                "type": "pie",
                "segments": [
                    {"label": segment["label"], "value": value} for segment, value in zip(segments, values, strict=True)
                ],
            },
        }

    categories = chart.get("categories", [])
    series = chart.get("series", [])
    x_label = chart.get("x_label")
    y_label = chart.get("y_label")
    valid_shape = (
        1 <= len(categories) <= _CHART_MAX_DATA_POINTS
        and all(_is_valid_chart_label(category) for category in categories)
        and len(set(categories)) == len(categories)
        and 1 <= len(series) <= _CHART_MAX_SERIES
        and all(_is_valid_chart_label(s["name"]) for s in series)
        and len({s["name"] for s in series}) == len(series)
        and (x_label is None or len(x_label) <= _CHART_MAX_TITLE_CHARS)
        and (y_label is None or len(y_label) <= _CHART_MAX_TITLE_CHARS)
    )
    if not valid_shape:
        return None

    # Each series needs exactly one data point per category; normalize
    # point order to the category order Slack expects.
    normalized_series: list[dict[str, Any]] = []
    for s in series:
        points = s["data"]
        if len(points) != len(categories):
            return None
        # A later duplicate label wins, as with upstream's ``new Map(...)``.
        by_label = {point["label"]: point for point in points}
        data: list[dict[str, Any]] = []
        for category in categories:
            point = by_label.get(category)
            if point is None:
                return None
            value = _chart_value_to_json_number(point["value"])
            if value is None:
                return None
            data.append({"label": category, "value": value})
        normalized_series.append({"name": s["name"], "data": data})

    axis_config: dict[str, Any] = {"categories": categories}
    if x_label is not None:
        axis_config["x_label"] = x_label
    if y_label is not None:
        axis_config["y_label"] = y_label

    return {
        "type": "data_visualization",
        "title": title,
        "chart": {
            "type": chart.get("type"),
            "series": normalized_series,
            "axis_config": axis_config,
        },
    }


def _is_valid_chart_label(label: str) -> bool:
    return 1 <= len(label) <= _CHART_MAX_LABEL_CHARS


def _convert_section_to_blocks(element: SectionElement, state: _CardRenderState) -> list[SlackBlock]:
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
                "text": (
                    f"*{_markdown_to_mrkdwn(convert_emoji(f['label']))}*"
                    f"\n{_markdown_to_mrkdwn(convert_emoji(f['value']))}"
                ),
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
