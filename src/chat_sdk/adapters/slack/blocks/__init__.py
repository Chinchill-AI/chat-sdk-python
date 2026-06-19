"""Runtime-free Slack Block Kit helpers — the ``blocks`` subpath.

Port of ``packages/adapter-slack/src/blocks/index.ts`` (vercel/chat#555;
input helpers from #559), exposed upstream as ``@chat-adapter/slack/blocks``.
Converts Chat SDK-style card objects into Slack Block Kit blocks (and a
Markdown fallback), with docs-backed Slack size limits and emoji-placeholder
conversion — without importing the full Slack adapter, ``slack_sdk``, or the
chat runtime. The only cross-module dependency is the sibling ``format``
subpath (``markdown_bold_to_slack_mrkdwn``), which is itself runtime-free.

Compatibility aliases ``card_to_block_kit`` / ``card_to_fallback_text`` mirror
upstream's ``cardToBlockKit`` / ``cardToFallbackText`` re-exports.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from chat_sdk.adapters.slack.blocks.errors import SlackBlockError
from chat_sdk.adapters.slack.blocks.input import (
    SLACK_FREEFORM_ACTION_ID,
    SLACK_FREEFORM_ACTION_PREFIX,
    SLACK_FREEFORM_BLOCK_ID,
    SLACK_FREEFORM_CALLBACK_ID,
    SLACK_INPUT_ACTION_PREFIX,
    SlackAnsweredInputOptions,
    SlackFreeformValueEntry,
    SlackFreeformViewOptions,
    SlackInputAction,
    SlackInputOption,
    SlackInputRequest,
    SlackInputResponse,
    answered_slack_input_blocks,
    build_slack_freeform_view,
    input_request_to_slack_blocks,
    parse_slack_freeform_value,
    parse_slack_input_response,
)
from chat_sdk.adapters.slack.blocks.limits import LIMITS
from chat_sdk.adapters.slack.blocks.types import (
    SlackActionsElement,
    SlackBlock,
    SlackBlocksOptions,
    SlackButtonElement,
    SlackButtonStyle,
    SlackCardChild,
    SlackCardElement,
    SlackDividerElement,
    SlackFieldElement,
    SlackFieldsElement,
    SlackImageElement,
    SlackLinkButtonElement,
    SlackLinkElement,
    SlackRadioSelectElement,
    SlackSectionElement,
    SlackSelectElement,
    SlackSelectOptionElement,
    SlackTableAlignment,
    SlackTableElement,
    SlackTextElement,
    SlackTextObject,
    SlackTextStyle,
)
from chat_sdk.adapters.slack.format import markdown_bold_to_slack_mrkdwn

__all__ = [
    "LIMITS",
    "SLACK_FREEFORM_ACTION_ID",
    "SLACK_FREEFORM_ACTION_PREFIX",
    "SLACK_FREEFORM_BLOCK_ID",
    "SLACK_FREEFORM_CALLBACK_ID",
    "SLACK_INPUT_ACTION_PREFIX",
    "SlackActionsElement",
    "SlackAnsweredInputOptions",
    "SlackBlock",
    "SlackBlockError",
    "SlackBlocksOptions",
    "SlackButtonElement",
    "SlackButtonStyle",
    "SlackCardChild",
    "SlackCardElement",
    "SlackDividerElement",
    "SlackFieldElement",
    "SlackFieldsElement",
    "SlackFreeformValueEntry",
    "SlackFreeformViewOptions",
    "SlackImageElement",
    "SlackInputAction",
    "SlackInputOption",
    "SlackInputRequest",
    "SlackInputResponse",
    "SlackLinkButtonElement",
    "SlackLinkElement",
    "SlackRadioSelectElement",
    "SlackSectionElement",
    "SlackSelectElement",
    "SlackSelectOptionElement",
    "SlackTableAlignment",
    "SlackTableElement",
    "SlackTextElement",
    "SlackTextObject",
    "SlackTextStyle",
    "answered_slack_input_blocks",
    "build_slack_freeform_view",
    "card_to_block_kit",
    "card_to_fallback_text",
    "card_to_slack_blocks",
    "card_to_slack_fallback_text",
    "convert_slack_emoji_placeholders",
    "input_request_to_slack_blocks",
    "parse_slack_freeform_value",
    "parse_slack_input_response",
]

_EMPTY_TEXT = " "
_EMOJI_PATTERN = re.compile(r"\{\{emoji:([a-zA-Z0-9_+-]+)\}\}")

# An emoji-placeholder converter: ``text -> text``.
_EmojiConverter = Callable[[str], str]


@dataclass
class _State:
    convert_emoji: _EmojiConverter
    max_blocks: int
    used_table: bool = False


def card_to_slack_blocks(
    card: SlackCardElement,
    options: SlackBlocksOptions | None = None,
) -> list[SlackBlock]:
    """Convert a card object into Slack Block Kit blocks."""
    options = options if options is not None else {}
    convert = options.get("convert_emoji")
    max_blocks = options.get("max_blocks")
    blocks: list[SlackBlock] = []
    state = _State(
        convert_emoji=convert if convert is not None else convert_slack_emoji_placeholders,
        max_blocks=max_blocks if max_blocks is not None else LIMITS.blocks,
    )

    title = card.get("title")
    if title:
        blocks.append(
            {
                "text": _plain_text(title, state.convert_emoji, LIMITS.header_text),
                "type": "header",
            }
        )
    subtitle = card.get("subtitle")
    if subtitle:
        blocks.append(
            {
                "elements": [_mrkdwn(subtitle, state.convert_emoji, LIMITS.text_object)],
                "type": "context",
            }
        )
    image_url = card.get("image_url")
    if image_url:
        blocks.append(
            {
                "alt_text": _truncate_text(
                    state.convert_emoji(title or "Card image"),
                    LIMITS.image_alt,
                ),
                "image_url": _truncate_text(image_url, LIMITS.image_url),
                "type": "image",
            }
        )
    for child in card["children"]:
        blocks.extend(_card_child_to_slack_blocks(child, state))
    return blocks[: state.max_blocks]


def card_to_slack_fallback_text(
    card: SlackCardElement,
    options: SlackBlocksOptions | None = None,
) -> str:
    """Render a card as plain Markdown fallback text."""
    options = options if options is not None else {}
    convert = options.get("convert_emoji")
    convert_emoji = convert if convert is not None else convert_slack_emoji_placeholders
    lines: list[str] = []
    title = card.get("title")
    if title:
        lines.append(f"*{convert_emoji(title)}*")
    subtitle = card.get("subtitle")
    if subtitle:
        lines.append(convert_emoji(subtitle))
    for child in card["children"]:
        text = _card_child_to_fallback_text(child, convert_emoji)
        if text:
            lines.append(text)
    return "\n".join(lines)


def convert_slack_emoji_placeholders(text: str) -> str:
    """Replace ``{{emoji:name}}`` placeholders with Slack ``:name:`` codes."""
    return _EMOJI_PATTERN.sub(r":\1:", text)


# Compatibility aliases (upstream ``cardToBlockKit`` / ``cardToFallbackText``).
card_to_block_kit = card_to_slack_blocks
card_to_fallback_text = card_to_slack_fallback_text


def _card_child_to_slack_blocks(child: SlackCardChild, state: _State) -> list[SlackBlock]:
    # Discriminated on the literal ``type`` key. The per-branch ignores mirror
    # the high-level adapter's card-child dispatch (cards.py): the union member
    # is narrowed by the runtime check, not by the static checker.
    kind = child.get("type")
    if kind == "actions":
        return [_actions_to_block(child, state.convert_emoji)]  # type: ignore[arg-type]
    if kind == "divider":
        return [{"type": "divider"}]
    if kind == "fields":
        return [_fields_to_block(child, state.convert_emoji)]  # type: ignore[arg-type]
    if kind == "image":
        return [_image_to_block(child, state.convert_emoji)]  # type: ignore[arg-type]
    if kind == "link":
        return [_link_to_block(child, state.convert_emoji)]  # type: ignore[arg-type]
    if kind == "section":
        section = cast(SlackSectionElement, child)
        result: list[SlackBlock] = []
        for nested in section["children"]:
            result.extend(_card_child_to_slack_blocks(nested, state))
        return result
    if kind == "table":
        return _table_to_blocks(child, state)  # type: ignore[arg-type]
    if kind == "text":
        return [_text_to_block(child, state.convert_emoji)]  # type: ignore[arg-type]
    return cast("list[SlackBlock]", _assert_never(child))


def _text_to_block(element: SlackTextElement, convert_emoji: _EmojiConverter) -> SlackBlock:
    text = markdown_bold_to_slack_mrkdwn(convert_emoji(element["content"]))
    if element.get("style") == "muted":
        return {
            "elements": [_mrkdwn(text, _identity, LIMITS.text_object)],
            "type": "context",
        }
    return {
        "text": _mrkdwn(
            f"*{text}*" if element.get("style") == "bold" else text,
            _identity,
            LIMITS.section_text,
        ),
        "type": "section",
    }


def _image_to_block(element: SlackImageElement, convert_emoji: _EmojiConverter) -> SlackBlock:
    return {
        "alt_text": _truncate_text(convert_emoji(element.get("alt") or "Image"), LIMITS.image_alt),
        "image_url": _truncate_text(element["url"], LIMITS.image_url),
        "type": "image",
    }


def _link_to_block(element: SlackLinkElement, convert_emoji: _EmojiConverter) -> SlackBlock:
    return {
        "text": _mrkdwn(
            f"<{element['url']}|{convert_emoji(element['label'])}>",
            _identity,
            LIMITS.section_text,
        ),
        "type": "section",
    }


def _actions_to_block(element: SlackActionsElement, convert_emoji: _EmojiConverter) -> SlackBlock:
    return {
        "elements": [
            _action_to_element(child, convert_emoji) for child in element["children"][: LIMITS.actions_elements]
        ],
        "type": "actions",
    }


def _action_to_element(
    child: SlackButtonElement | SlackLinkButtonElement | SlackRadioSelectElement | SlackSelectElement,
    convert_emoji: _EmojiConverter,
) -> dict[str, object]:
    kind = child.get("type")
    if kind == "button":
        return _button_to_element(child, convert_emoji)  # type: ignore[arg-type]
    if kind == "link-button":
        return _link_button_to_element(child, convert_emoji)  # type: ignore[arg-type]
    if kind == "radio_select":
        return _radio_select_to_element(child, convert_emoji)  # type: ignore[arg-type]
    if kind == "select":
        return _select_to_element(child, convert_emoji)  # type: ignore[arg-type]
    return cast("dict[str, object]", _assert_never(child))


def _button_to_element(button: SlackButtonElement, convert_emoji: _EmojiConverter) -> dict[str, object]:
    value = button.get("value")
    return _compact(
        {
            "action_id": _truncate_text(button["id"], LIMITS.action_id),
            "style": _map_button_style(button.get("style")),
            "text": _plain_text(button["label"], convert_emoji, LIMITS.button_text),
            "type": "button",
            "value": None if value is None else _truncate_text(value, LIMITS.button_value),
        }
    )


def _link_button_to_element(button: SlackLinkButtonElement, convert_emoji: _EmojiConverter) -> dict[str, object]:
    # `??` semantics: an explicit (even empty-string) id is used verbatim;
    # only a missing/None id falls back to the URL-derived action_id.
    button_id = button.get("id")
    action_id = button_id if button_id is not None else f"link-{button['url']}"
    return _compact(
        {
            "action_id": _truncate_text(action_id, LIMITS.action_id),
            "style": _map_button_style(button.get("style")),
            "text": _plain_text(button["label"], convert_emoji, LIMITS.button_text),
            "type": "button",
            "url": _truncate_text(button["url"], LIMITS.button_url),
        }
    )


def _select_to_element(select: SlackSelectElement, convert_emoji: _EmojiConverter) -> dict[str, object]:
    options = [_option_object(option, convert_emoji, "plain_text") for option in select["options"][: LIMITS.options]]
    placeholder = select.get("placeholder")
    return _compact(
        {
            "action_id": _truncate_text(select["id"], LIMITS.action_id),
            "initial_option": _find_initial_option(options, select.get("initial_option")),
            "options": options,
            "placeholder": (_plain_text(placeholder, convert_emoji, LIMITS.placeholder) if placeholder else None),
            "type": "static_select",
        }
    )


def _radio_select_to_element(select: SlackRadioSelectElement, convert_emoji: _EmojiConverter) -> dict[str, object]:
    options = [_option_object(option, convert_emoji, "mrkdwn") for option in select["options"][: LIMITS.radio_options]]
    return _compact(
        {
            "action_id": _truncate_text(select["id"], LIMITS.action_id),
            "initial_option": _find_initial_option(options, select.get("initial_option")),
            "options": options,
            "type": "radio_buttons",
        }
    )


def _find_initial_option(
    options: list[dict[str, object]],
    initial_option: str | None,
) -> dict[str, object] | None:
    if initial_option is None:
        return None
    value = _truncate_text(initial_option, LIMITS.option_value)
    for option in options:
        if option.get("value") == value:
            return option
    return None


def _option_object(
    option: SlackSelectOptionElement,
    convert_emoji: _EmojiConverter,
    text_type: str,
) -> dict[str, object]:
    description = option.get("description")
    return _compact(
        {
            "description": (
                {
                    "text": _truncate_text(convert_emoji(description), LIMITS.option_description),
                    "type": text_type,
                }
                if description
                else None
            ),
            "text": {
                "text": _truncate_text(convert_emoji(option["label"]), LIMITS.option_text),
                "type": text_type,
            },
            "value": _truncate_text(option["value"], LIMITS.option_value),
        }
    )


def _fields_to_block(element: SlackFieldsElement, convert_emoji: _EmojiConverter) -> SlackBlock:
    return {
        "fields": [
            _mrkdwn(
                f"*{markdown_bold_to_slack_mrkdwn(convert_emoji(field['label']))}*"
                f"\n{markdown_bold_to_slack_mrkdwn(convert_emoji(field['value']))}",
                _identity,
                LIMITS.field_text,
            )
            for field in element["children"][: LIMITS.fields]
        ],
        "type": "section",
    }


def _table_to_blocks(element: SlackTableElement, state: _State) -> list[SlackBlock]:
    if (
        state.used_table
        or len(element["rows"]) + 1 > LIMITS.table_rows
        or len(element["headers"]) > LIMITS.table_columns
    ):
        return [
            {
                "text": _mrkdwn(
                    f"```\n{_table_to_ascii(element)}\n```",
                    _identity,
                    LIMITS.section_text,
                ),
                "type": "section",
            }
        ]
    state.used_table = True
    align = element.get("align")
    column_settings = (
        [({"align": value} if value else None) for value in align[: LIMITS.table_columns]]
        if align is not None
        else None
    )
    return [
        _compact(
            {
                "column_settings": column_settings,
                "rows": [
                    [_raw_text(header, state.convert_emoji) for header in element["headers"]],
                    *[[_raw_text(cell, state.convert_emoji) for cell in row] for row in element["rows"]],
                ],
                "type": "table",
            }
        )
    ]


def _card_child_to_fallback_text(child: SlackCardChild, convert_emoji: _EmojiConverter) -> str | None:
    # Per-branch casts narrow the union to the member the literal ``type``
    # guarantees, so the inline field reads stay statically checked.
    kind = child.get("type")
    if kind == "actions":
        return None
    if kind == "divider":
        return "---"
    if kind == "fields":
        fields = cast(SlackFieldsElement, child)
        return "\n".join(
            f"{convert_emoji(field['label'])}: {convert_emoji(field['value'])}" for field in fields["children"]
        )
    if kind == "image":
        alt = cast(SlackImageElement, child).get("alt")
        return convert_emoji(alt) if alt else None
    if kind == "link":
        link = cast(SlackLinkElement, child)
        return f"{convert_emoji(link['label'])} ({link['url']})"
    if kind == "section":
        section = cast(SlackSectionElement, child)
        nested_lines = [
            text for nested in section["children"] if (text := _card_child_to_fallback_text(nested, convert_emoji))
        ]
        return "\n".join(nested_lines)
    if kind == "table":
        return _table_to_ascii(cast(SlackTableElement, child))
    if kind == "text":
        return convert_emoji(cast(SlackTextElement, child)["content"])
    return cast("str | None", _assert_never(child))


def _mrkdwn(text: str, convert_emoji: _EmojiConverter, max_length: int) -> SlackTextObject:
    return {
        "text": _nonempty_text(_truncate_text(convert_emoji(text), max_length)),
        "type": "mrkdwn",
    }


def _plain_text(text: str, convert_emoji: _EmojiConverter, max_length: int) -> SlackTextObject:
    return {
        "emoji": True,
        "text": _nonempty_text(_truncate_text(convert_emoji(text), max_length)),
        "type": "plain_text",
    }


def _raw_text(text: str, convert_emoji: _EmojiConverter) -> dict[str, str]:
    return {
        "text": _nonempty_text(convert_emoji(text)),
        "type": "raw_text",
    }


def _map_button_style(style: SlackButtonStyle | None) -> str | None:
    return style if style in ("danger", "primary") else None


def _truncate_text(text: str, max_length: int) -> str:
    return text[:max_length] if len(text) > max_length else text


def _nonempty_text(text: str) -> str:
    return text if len(text) > 0 else _EMPTY_TEXT


def _identity(value: str) -> str:
    return value


def _assert_never(value: object) -> object:
    raise SlackBlockError(f"Unsupported Slack card element: {value}")


def _compact(value: dict[str, object]) -> dict[str, object]:
    return {key: item for key, item in value.items() if item is not None}


def _table_to_ascii(table: SlackTableElement) -> str:
    rows = [table["headers"], *table["rows"]]
    widths = [
        max((len(row[column]) if column < len(row) else 0) for row in rows) for column in range(len(table["headers"]))
    ]
    lines: list[str] = []
    for row in rows:
        cells = [
            (row[column] if column < len(row) else "").ljust(widths[column] if column < len(widths) else 0)
            for column in range(len(row))
        ]
        lines.append(" | ".join(cells).rstrip())
    return "\n".join(lines)
