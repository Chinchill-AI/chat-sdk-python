"""Slack modal (view) converter.

Converts ModalElement to Slack Block Kit view format.
Port of modals.ts from the Vercel Chat SDK Slack adapter.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from chat_sdk.adapters.slack.cards import (
    SlackBlock,
    convert_fields_to_block,
    convert_text_to_block,
)
from chat_sdk.modals import ModalChild, ModalElement

# Slack view type aliases
SlackView = dict[str, Any]
SlackModalResponse = dict[str, Any]


# =============================================================================
# Private metadata encoding
# =============================================================================


@dataclass
class ModalMetadata:
    """Encoded metadata for Slack's private_metadata field."""

    context_id: str | None = None
    private_metadata: str | None = None


def encode_modal_metadata(meta: ModalMetadata) -> str | None:
    """Encode context_id and user private_metadata into a single string
    for Slack's private_metadata field.
    """
    if not (meta.context_id or meta.private_metadata):
        return None
    return json.dumps({"c": meta.context_id, "m": meta.private_metadata})


def decode_modal_metadata(raw: str | None = None) -> ModalMetadata:
    """Decode Slack's private_metadata back into context_id and user private_metadata.

    Falls back to treating the raw string as a plain context_id for backward compat.
    """
    if not raw:
        return ModalMetadata()
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and ("c" in parsed or "m" in parsed):
            return ModalMetadata(
                context_id=parsed.get("c") or None,
                private_metadata=parsed.get("m") or None,
            )
    except (json.JSONDecodeError, TypeError):
        # Not JSON -- treat as legacy plain context_id
        pass
    return ModalMetadata(context_id=raw)


# =============================================================================
# Modal view conversion
# =============================================================================


def modal_to_slack_view(modal: ModalElement, context_id: str | None = None) -> SlackView:
    """Convert a ModalElement to a Slack view payload."""
    title_text = modal.get("title", "")[:24]
    submit_label = modal.get("submit_label") or "Submit"
    close_label = modal.get("close_label") or "Cancel"

    view: SlackView = {
        "type": "modal",
        "callback_id": modal.get("callback_id", ""),
        "title": {"type": "plain_text", "text": title_text},
        "submit": {"type": "plain_text", "text": submit_label},
        "close": {"type": "plain_text", "text": close_label},
        "blocks": [_modal_child_to_block(child) for child in modal.get("children", [])],
    }

    notify_on_close = modal.get("notify_on_close")
    if notify_on_close is not None:
        view["notify_on_close"] = notify_on_close

    if context_id is not None:
        view["private_metadata"] = context_id

    return view


def _modal_child_to_block(child: ModalChild) -> SlackBlock:
    """Convert a modal child element to a Slack block."""
    child_type = child.get("type", "")

    if child_type == "text_input":
        return _text_input_to_block(child)  # type: ignore[arg-type]
    if child_type == "select":
        return _select_to_block(child)  # type: ignore[arg-type]
    if child_type == "radio_select":
        return _radio_select_to_block(child)  # type: ignore[arg-type]
    if child_type == "text":
        return convert_text_to_block(child)  # type: ignore[arg-type]
    if child_type == "fields":
        return convert_fields_to_block(child)  # type: ignore[arg-type]

    raise ValueError(f"Unknown modal child type: {child_type}")


def _text_input_to_block(input_el: dict[str, Any]) -> SlackBlock:
    """Convert a TextInputElement to a Slack input block."""
    element: dict[str, Any] = {
        "type": "plain_text_input",
        "action_id": input_el.get("id", ""),
        "multiline": input_el.get("multiline", False),
    }

    placeholder = input_el.get("placeholder")
    if placeholder:
        element["placeholder"] = {"type": "plain_text", "text": placeholder}

    initial_value = input_el.get("initial_value")
    if initial_value:
        element["initial_value"] = initial_value

    max_length = input_el.get("max_length")
    if max_length:
        element["max_length"] = max_length

    return {
        "type": "input",
        "block_id": input_el.get("id", ""),
        "optional": input_el.get("optional", False),
        "label": {"type": "plain_text", "text": input_el.get("label", "")},
        "element": element,
    }


def _select_to_block(select: dict[str, Any]) -> SlackBlock:
    """Convert a SelectElement to a Slack input block with static_select."""
    options: list[dict[str, Any]] = []
    for opt in select.get("options", []):
        option: dict[str, Any] = {
            "text": {"type": "plain_text", "text": opt.get("label", "")},
            "value": opt.get("value", ""),
        }
        desc = opt.get("description")
        if desc:
            option["description"] = {"type": "plain_text", "text": desc}
        options.append(option)

    element: dict[str, Any] = {
        "type": "static_select",
        "action_id": select.get("id", ""),
        "options": options,
    }

    placeholder = select.get("placeholder")
    if placeholder:
        element["placeholder"] = {"type": "plain_text", "text": placeholder}

    initial_option = select.get("initial_option")
    if initial_option:
        initial_opt = next((o for o in options if o["value"] == initial_option), None)
        if initial_opt:
            element["initial_option"] = initial_opt

    return {
        "type": "input",
        "block_id": select.get("id", ""),
        "optional": select.get("optional", False),
        "label": {"type": "plain_text", "text": select.get("label", "")},
        "element": element,
    }


def _radio_select_to_block(radio_select: dict[str, Any]) -> SlackBlock:
    """Convert a RadioSelectElement to a Slack input block with radio_buttons."""
    limited_options = radio_select.get("options", [])[:10]
    options: list[dict[str, Any]] = []
    for opt in limited_options:
        option: dict[str, Any] = {
            "text": {"type": "mrkdwn", "text": opt.get("label", "")},
            "value": opt.get("value", ""),
        }
        desc = opt.get("description")
        if desc:
            option["description"] = {"type": "mrkdwn", "text": desc}
        options.append(option)

    element: dict[str, Any] = {
        "type": "radio_buttons",
        "action_id": radio_select.get("id", ""),
        "options": options,
    }

    initial_option = radio_select.get("initial_option")
    if initial_option:
        initial_opt = next((o for o in options if o["value"] == initial_option), None)
        if initial_opt:
            element["initial_option"] = initial_opt

    return {
        "type": "input",
        "block_id": radio_select.get("id", ""),
        "optional": radio_select.get("optional", False),
        "label": {"type": "plain_text", "text": radio_select.get("label", "")},
        "element": element,
    }
