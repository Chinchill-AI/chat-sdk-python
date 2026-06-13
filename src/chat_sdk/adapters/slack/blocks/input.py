"""Generic input-request Block Kit helpers for the Slack blocks subpath.

Port of ``packages/adapter-slack/src/blocks/input.ts`` (vercel/chat#559).

Builds Block Kit for prompt/option input requests (buttons, select, radio,
and freeform-text modals) and parses the corresponding interaction
payloads back into structured responses. Input shapes are TypedDicts with
snake_case keys (``request_id``, ``allow_freeform``, ``option_id``,
``selected_option_value``, ``action_id``, ``block_id``) — upstream uses
camelCase. Emitted Block Kit dicts keep Slack's API field names.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal, NotRequired, TypedDict

from chat_sdk.adapters.slack.blocks.limits import LIMITS
from chat_sdk.adapters.slack.blocks.types import SlackBlock, SlackButtonStyle

__all__ = [
    "SLACK_FREEFORM_ACTION_ID",
    "SLACK_FREEFORM_ACTION_PREFIX",
    "SLACK_FREEFORM_BLOCK_ID",
    "SLACK_FREEFORM_CALLBACK_ID",
    "SLACK_INPUT_ACTION_PREFIX",
    "SlackAnsweredInputOptions",
    "SlackFreeformValueEntry",
    "SlackFreeformViewOptions",
    "SlackInputAction",
    "SlackInputOption",
    "SlackInputRequest",
    "SlackInputResponse",
    "answered_slack_input_blocks",
    "build_slack_freeform_view",
    "input_request_to_slack_blocks",
    "parse_slack_freeform_value",
    "parse_slack_input_response",
]

SLACK_INPUT_ACTION_PREFIX = "input:"
SLACK_FREEFORM_ACTION_PREFIX = "input-freeform:"
SLACK_FREEFORM_CALLBACK_ID = "input-freeform-submit"
SLACK_FREEFORM_BLOCK_ID = "input-freeform-block"
SLACK_FREEFORM_ACTION_ID = "input-freeform-text"

# Matches an option button action id of the form ``<requestId>:button:<n>``.
_BUTTON_ACTION_PATTERN = re.compile(r"^(?P<request_id>.+):button:\d+$")


class SlackInputOption(TypedDict):
    """One selectable option in an input request."""

    id: str
    label: str
    description: NotRequired[str]
    style: NotRequired[SlackButtonStyle]


class SlackInputRequest(TypedDict):
    """A prompt with optional options rendered as Block Kit."""

    prompt: str
    request_id: str
    allow_freeform: NotRequired[bool]
    display: NotRequired[Literal["buttons", "radio", "select"]]
    options: NotRequired[list[SlackInputOption]]


class SlackInputAction(TypedDict):
    """An inbound interaction action to parse into a response."""

    action_id: str
    selected_option_value: NotRequired[str]
    value: NotRequired[str]


class SlackInputResponse(TypedDict):
    """The parsed result of an input interaction."""

    request_id: str
    option_id: NotRequired[str]


class SlackFreeformViewOptions(TypedDict):
    """Options for :func:`build_slack_freeform_view`."""

    metadata: Any
    prompt: NotRequired[str]
    title: NotRequired[str]


class SlackFreeformValueEntry(TypedDict):
    """One submitted view-state value entry."""

    action_id: str
    block_id: str
    value: NotRequired[str]


class SlackAnsweredInputOptions(TypedDict):
    """Options for :func:`answered_slack_input_blocks`."""

    answer: str
    prompt_block: NotRequired[Any]
    user_id: NotRequired[str]


def input_request_to_slack_blocks(request: SlackInputRequest) -> list[SlackBlock]:
    """Render an input request as Slack Block Kit blocks."""
    prompt: SlackBlock = {
        "text": {
            "text": _truncate(request["prompt"], LIMITS.section_text),
            "type": "mrkdwn",
        },
        "type": "section",
    }
    options = request.get("options") or []
    if len(options) == 0:
        return [
            prompt,
            {
                "elements": [_freeform_button(request["request_id"])],
                "type": "actions",
            },
        ]
    extras = [_freeform_button(request["request_id"])] if request.get("allow_freeform") else []
    if request.get("display") == "radio":
        return [
            prompt,
            {
                "elements": [_radio_element(request), *extras],
                "type": "actions",
            },
        ]
    if request.get("display") == "select":
        return [
            prompt,
            {
                "elements": [_select_element(request), *extras],
                "type": "actions",
            },
        ]
    limit = LIMITS.actions_elements - 1 if len(extras) > 0 else LIMITS.actions_elements
    elements = [_button_element(request["request_id"], option, index) for index, option in enumerate(options[:limit])]
    elements.extend(extras)
    return [
        prompt,
        {"elements": elements, "type": "actions"},
    ]


def parse_slack_input_response(action: SlackInputAction) -> SlackInputResponse | None:
    """Parse an inbound action into a structured input response, or ``None``."""
    action_id = action["action_id"]
    if not action_id.startswith(SLACK_INPUT_ACTION_PREFIX):
        return None
    request_id = action_id[len(SLACK_INPUT_ACTION_PREFIX) :]
    selected = action.get("selected_option_value")
    if selected is not None:
        if request_id:
            return {"option_id": selected, "request_id": request_id}
        return None
    match = _BUTTON_ACTION_PATTERN.match(request_id)
    value = action.get("value")
    if match is not None and value is not None:
        return {"option_id": value, "request_id": match.group("request_id")}
    return None


def build_slack_freeform_view(options: SlackFreeformViewOptions) -> dict[str, Any]:
    """Build a freeform-text input modal view payload."""
    title_source = options.get("title")
    if title_source is None:
        title_source = options.get("prompt")
    if title_source is None:
        title_source = "Your answer"
    title = _truncate(title_source, 24)
    blocks: list[Any] = []
    prompt = options.get("prompt")
    if prompt:
        blocks.append(
            {
                "text": {
                    "text": _truncate(prompt, LIMITS.section_text),
                    "type": "mrkdwn",
                },
                "type": "section",
            }
        )
    blocks.append(
        {
            "block_id": SLACK_FREEFORM_BLOCK_ID,
            "element": {
                "action_id": SLACK_FREEFORM_ACTION_ID,
                "multiline": True,
                "type": "plain_text_input",
            },
            "label": {"text": "Answer", "type": "plain_text"},
            "type": "input",
        }
    )
    metadata = options["metadata"]
    private_metadata = metadata if isinstance(metadata, str) else json.dumps(metadata, separators=(",", ":"))
    return {
        "blocks": blocks,
        "callback_id": SLACK_FREEFORM_CALLBACK_ID,
        "close": {"text": "Cancel", "type": "plain_text"},
        "private_metadata": private_metadata,
        "submit": {"text": "Submit", "type": "plain_text"},
        "title": {"text": title, "type": "plain_text"},
        "type": "modal",
    }


def parse_slack_freeform_value(
    values: list[SlackFreeformValueEntry],
) -> str | None:
    """Extract the freeform answer from submitted view-state values."""
    for value in values:
        if value["block_id"] == SLACK_FREEFORM_BLOCK_ID and value["action_id"] == SLACK_FREEFORM_ACTION_ID:
            return value.get("value")
    return None


def answered_slack_input_blocks(
    input: SlackAnsweredInputOptions,
) -> list[SlackBlock]:
    """Render the confirmation blocks shown after an input is answered."""
    blocks: list[SlackBlock] = []
    prompt_block = input.get("prompt_block")
    if prompt_block and isinstance(prompt_block, dict):
        blocks.append(prompt_block)
    answer = input["answer"]
    blocks.append(
        {
            "text": {"text": f":white_check_mark: *{answer}*", "type": "mrkdwn"},
            "type": "section",
        }
    )
    user_id = input.get("user_id")
    if user_id:
        blocks.append(
            {
                "elements": [{"text": f"Answered by <@{user_id}>", "type": "mrkdwn"}],
                "type": "context",
            }
        )
    return blocks


def _freeform_button(request_id: str) -> dict[str, Any]:
    return {
        "action_id": f"{SLACK_FREEFORM_ACTION_PREFIX}{request_id}",
        "style": "primary",
        "text": {"text": "Type your answer", "type": "plain_text"},
        "type": "button",
        "value": request_id,
    }


def _button_element(
    request_id: str,
    option: SlackInputOption,
    index: int,
) -> dict[str, Any]:
    style = option.get("style")
    return _compact(
        {
            "action_id": f"{SLACK_INPUT_ACTION_PREFIX}{request_id}:button:{index}",
            "style": style if style in ("primary", "danger") else None,
            "text": {
                "text": _truncate(option["label"], LIMITS.button_text),
                "type": "plain_text",
            },
            "type": "button",
            "value": _truncate(option["id"], LIMITS.button_value),
        }
    )


def _select_element(request: SlackInputRequest) -> dict[str, Any]:
    options = [
        {
            "text": {
                "text": _truncate(option["label"], LIMITS.option_text),
                "type": "plain_text",
            },
            "value": _truncate(option["id"], LIMITS.option_value),
        }
        for option in (request.get("options") or [])
    ]
    return {
        "action_id": f"{SLACK_INPUT_ACTION_PREFIX}{request['request_id']}",
        "options": options[: LIMITS.options],
        "placeholder": {"text": "Choose an option", "type": "plain_text"},
        "type": "static_select",
    }


def _radio_element(request: SlackInputRequest) -> dict[str, Any]:
    options = [
        {
            "text": {
                "text": _truncate(option["label"], LIMITS.option_text),
                "type": "plain_text",
            },
            "value": _truncate(option["id"], LIMITS.option_value),
        }
        for option in (request.get("options") or [])
    ]
    if len(options) > LIMITS.radio_options:
        return _select_element(request)
    return {
        "action_id": f"{SLACK_INPUT_ACTION_PREFIX}{request['request_id']}",
        "options": options,
        "type": "radio_buttons",
    }


def _truncate(value: str, limit: int) -> str:
    return value[:limit] if len(value) > limit else value


def _compact(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}
