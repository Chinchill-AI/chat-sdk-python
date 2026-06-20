"""Teams modals primitive — a lightweight, runtime-free subpath.

Port of ``packages/adapter-teams/src/modals-primitives/{index,types}.ts`` (NEW
in vercel/chat@4.31.0, commit ``8c71411``), exposed upstream as the
``modals-primitives`` surface. Renders a structured ``Modal`` description into a
Teams Adaptive Card, parses the values out of a Teams dialog-submit payload, and
builds the ``task module`` response envelope for the close / errors / push /
update variants.

This is intentionally distinct from the SDK-bound modal handling in
:mod:`chat_sdk.adapters.teams.adapter`. The helpers here are low-level
primitives that operate purely on plain dicts and stdlib — importing this module
never imports the ``microsoft_teams`` SDK, an HTTP client, or anything that
performs network I/O.

Reuse, not duplication:

* Text escaping / emoji conversion delegates to
  :func:`chat_sdk.adapters.teams.format.convert_teams_emoji_placeholders` (the
  format primitive), exactly as upstream's ``index.ts`` imports
  ``convertTeamsEmojiPlaceholders`` from ``../format``.
* The ``fields`` modal child renders :class:`TeamsFieldElement` (``label`` /
  ``value``) — the same field shape used by the cards primitive
  (``TeamsFieldElement`` from ``../cards-primitives`` upstream). It is a plain
  two-key ``TypedDict``; the modal-specific input element shapes
  (``text_input`` / ``select`` / ``radio_select``) are defined here because
  upstream defines them in ``modals-primitives/types.ts``, distinct from the
  ``cards-primitives`` input request shapes in
  :mod:`chat_sdk.adapters.teams.cards_input`.

Wire-key fidelity (HAZARDS):

* The reserved dialog-submit keys ``__callbackId`` / ``__contextId`` /
  ``msteams`` are **literal** — they are *not* snake_cased. They round-trip the
  Teams dialog submit and cross the SDK-state boundary, so they must match the
  bytes Teams sends/expects verbatim. :func:`parse_teams_dialog_submit_values`
  skips exactly those three reserved keys and nothing else.
* The Adaptive Card content type is the literal
  ``"application/vnd.microsoft.card.adaptive"``.
* ``action == "close"`` (and a missing response) yields ``None`` — no card.
* Only string-typed values pass the value filter (mirrors upstream's
  ``typeof value === "string"``).

Truthiness mirrors upstream intentionally: optional emit guards
(``maxLength`` / ``placeholder`` / ``initialValue`` / ``initialOption`` /
``contextId``) use truthiness so an empty string / ``maxLength: 0`` is omitted,
byte-for-byte with the upstream ``...(x ? { ... } : {})`` spreads. The
nullish-coalescing reads (``callbackId`` fallback, ``submitLabel`` default,
``multiline`` / ``optional`` defaults) use ``is not None`` so an explicit empty
string survives, matching upstream's ``??``.
"""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

from chat_sdk.adapters.teams.format import convert_teams_emoji_placeholders

__all__ = [
    "ADAPTIVE_CARD_CONTENT_TYPE",
    "TeamsDialogSubmitValues",
    "TeamsFieldElement",
    "TeamsFieldsModalElement",
    "TeamsModalChild",
    "TeamsModalElement",
    "TeamsModalRadioSelectElement",
    "TeamsModalResponse",
    "TeamsModalSelectElement",
    "TeamsModalSelectOption",
    "TeamsModalTextElement",
    "TeamsModalTextInputElement",
    "TeamsTaskModuleResponse",
    "modal_to_adaptive_card",
    "parse_teams_dialog_submit_values",
    "to_teams_task_module_response",
]

# The Adaptive Card content type Teams expects in a task-module ``continue``
# envelope. Literal — must match Teams' wire format verbatim.
ADAPTIVE_CARD_CONTENT_TYPE = "application/vnd.microsoft.card.adaptive"

_ADAPTIVE_CARD_SCHEMA = "http://adaptivecards.io/schemas/adaptive-card.json"
_ADAPTIVE_CARD_VERSION = "1.4"


class TeamsFieldElement(TypedDict):
    """One ``label`` / ``value`` row of a ``fields`` modal child.

    Mirrors the ``TeamsFieldElement`` shared with the cards primitive
    (upstream ``../cards-primitives``).
    """

    label: str
    value: str


class TeamsModalTextElement(TypedDict):
    """A block of (optionally styled) text in a modal."""

    content: str
    type: Literal["text"]
    style: NotRequired[Literal["bold", "muted", "plain"]]


class TeamsFieldsModalElement(TypedDict):
    """A ``FactSet`` of ``label`` / ``value`` rows."""

    children: list[TeamsFieldElement]
    type: Literal["fields"]


class TeamsModalSelectOption(TypedDict):
    """One selectable option in a ``select`` / ``radio_select`` element."""

    label: str
    value: str


class TeamsModalTextInputElement(TypedDict):
    """A single- or multi-line text input."""

    id: str
    label: str
    type: Literal["text_input"]
    initialValue: NotRequired[str]
    maxLength: NotRequired[int]
    multiline: NotRequired[bool]
    optional: NotRequired[bool]
    placeholder: NotRequired[str]


class TeamsModalSelectElement(TypedDict):
    """A dropdown (``compact``) choice set."""

    id: str
    label: str
    options: list[TeamsModalSelectOption]
    type: Literal["select"]
    initialOption: NotRequired[str]
    optional: NotRequired[bool]
    placeholder: NotRequired[str]


class TeamsModalRadioSelectElement(TypedDict):
    """A radio (``expanded``) choice set — same fields as ``select``."""

    id: str
    label: str
    options: list[TeamsModalSelectOption]
    type: Literal["radio_select"]
    initialOption: NotRequired[str]
    optional: NotRequired[bool]
    placeholder: NotRequired[str]


TeamsModalChild = (
    TeamsFieldsModalElement
    | TeamsModalTextElement
    | TeamsModalTextInputElement
    | TeamsModalSelectElement
    | TeamsModalRadioSelectElement
)


class TeamsModalElement(TypedDict):
    """A complete modal description."""

    callbackId: str
    children: list[TeamsModalChild]
    title: str
    type: Literal["modal"]
    submitLabel: NotRequired[str]


class _TaskModuleValueCard(TypedDict):
    content: Any
    contentType: Literal["application/vnd.microsoft.card.adaptive"]


class _TaskModuleValue(TypedDict):
    card: _TaskModuleValueCard
    title: str


class _TaskModuleTask(TypedDict):
    type: Literal["continue"]
    value: _TaskModuleValue


class TeamsTaskModuleResponse(TypedDict):
    """The ``task module`` response envelope returned to Teams."""

    task: _TaskModuleTask


class _CloseResponse(TypedDict):
    action: Literal["close"]


class _ErrorsResponse(TypedDict):
    action: Literal["errors"]
    errors: dict[str, str]


class _PushUpdateResponse(TypedDict):
    action: Literal["push", "update"]
    modal: TeamsModalElement


TeamsModalResponse = _CloseResponse | _ErrorsResponse | _PushUpdateResponse


class TeamsDialogSubmitValues(TypedDict):
    """The parsed result of a Teams dialog submit."""

    callbackId: str | None
    contextId: str | None
    values: dict[str, str]


def modal_to_adaptive_card(
    modal: TeamsModalElement,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Render a :class:`TeamsModalElement` into a Teams Adaptive Card dict.

    Port of ``modalToAdaptiveCard``. ``options`` accepts ``callbackId`` and
    ``contextId``. The submit action's ``data`` carries the reserved
    ``__callbackId`` (and ``__contextId`` when present) literal keys so Teams
    echoes them back on submit. ``callbackId`` falls back to the modal's own
    ``callbackId`` (``??`` → ``is not None``); ``__contextId`` is only emitted
    when truthy (mirrors upstream's ``...(options.contextId ? ... : {})``).
    """
    opts = options or {}
    option_callback_id = opts.get("callbackId")
    context_id = opts.get("contextId")

    data: dict[str, Any] = {
        "__callbackId": option_callback_id if option_callback_id is not None else modal["callbackId"],
    }
    if context_id:
        data["__contextId"] = context_id

    submit_label = modal.get("submitLabel")
    body: list[Any] = []
    for child in modal["children"]:
        body.extend(_modal_child_to_adaptive_elements(child))

    return {
        "$schema": _ADAPTIVE_CARD_SCHEMA,
        "actions": [
            {
                "data": data,
                "style": "positive",
                "title": submit_label if submit_label is not None else "Submit",
                "type": "Action.Submit",
            }
        ],
        "body": body,
        "type": "AdaptiveCard",
        "version": _ADAPTIVE_CARD_VERSION,
    }


def parse_teams_dialog_submit_values(
    data: dict[str, Any] | None,
) -> TeamsDialogSubmitValues:
    """Extract the submitted values from a Teams dialog-submit payload.

    Port of ``parseTeamsDialogSubmitValues``. The reserved keys
    ``__callbackId`` / ``__contextId`` / ``msteams`` are skipped (exactly those
    three, nothing else) and surfaced as ``callbackId`` / ``contextId`` only
    when string-typed. Every other key is copied into ``values`` only when its
    value is a string (mirrors upstream's ``typeof value === "string"``).
    """
    if not data:
        return {"callbackId": None, "contextId": None, "values": {}}

    values: dict[str, str] = {}
    for key, value in data.items():
        if key in ("__callbackId", "__contextId", "msteams"):
            continue
        if isinstance(value, str):
            values[key] = value

    raw_callback_id = data.get("__callbackId")
    raw_context_id = data.get("__contextId")
    return {
        "callbackId": raw_callback_id if isinstance(raw_callback_id, str) else None,
        "contextId": raw_context_id if isinstance(raw_context_id, str) else None,
        "values": values,
    }


def to_teams_task_module_response(
    response: TeamsModalResponse | None,
    options: dict[str, Any] | None = None,
) -> TeamsTaskModuleResponse | None:
    """Build the task-module response for the close / errors / push / update variants.

    Port of ``toTeamsTaskModuleResponse``. Returns ``None`` for a missing
    response or ``action == "close"``. ``errors`` renders a validation card;
    ``push`` / ``update`` render the modal as a ``continue`` card. ``options``
    accepts ``contextId``, threaded into the rendered modal card.
    """
    opts = options or {}
    if not response or response.get("action") == "close":
        return None

    if response.get("action") == "errors":
        errors: dict[str, str] = response["errors"]  # type: ignore[typeddict-item]
        error_blocks: list[Any] = [
            {
                "text": "Please fix the following errors:",
                "type": "TextBlock",
                "weight": "Bolder",
                "wrap": True,
            }
        ]
        for field, message in errors.items():
            error_blocks.append(
                {
                    "color": "Attention",
                    "text": f"**{field}**: {message}",
                    "type": "TextBlock",
                    "wrap": True,
                }
            )
        return _continue_response(
            "Validation Error",
            {
                "$schema": _ADAPTIVE_CARD_SCHEMA,
                "body": error_blocks,
                "type": "AdaptiveCard",
                "version": _ADAPTIVE_CARD_VERSION,
            },
        )

    modal: TeamsModalElement = response["modal"]  # type: ignore[typeddict-item]
    return _continue_response(
        modal["title"],
        modal_to_adaptive_card(modal, {"contextId": opts.get("contextId")}),
    )


def _modal_child_to_adaptive_elements(child: TeamsModalChild) -> list[Any]:
    child_type = child.get("type")
    if child_type == "text":
        return [_text_block(child)]  # type: ignore[arg-type]
    if child_type == "fields":
        return [_fields_block(child)]  # type: ignore[arg-type]
    if child_type == "text_input":
        return [_text_input(child)]  # type: ignore[arg-type]
    if child_type == "select":
        return [_choice_set(child, "compact")]  # type: ignore[arg-type]
    if child_type == "radio_select":
        return [_choice_set(child, "expanded")]  # type: ignore[arg-type]
    return []


def _text_block(element: TeamsModalTextElement) -> dict[str, Any]:
    block: dict[str, Any] = {}
    style = element.get("style")
    if style == "bold":
        block["weight"] = "Bolder"
    if style == "muted":
        block["isSubtle"] = True
    block["text"] = convert_teams_emoji_placeholders(element["content"])
    block["type"] = "TextBlock"
    block["wrap"] = True
    return block


def _fields_block(element: TeamsFieldsModalElement) -> dict[str, Any]:
    return {
        "facts": [{"title": field["label"], "value": field["value"]} for field in element["children"]],
        "type": "FactSet",
    }


def _text_input(input_element: TeamsModalTextInputElement) -> dict[str, Any]:
    multiline = input_element.get("multiline")
    optional = input_element.get("optional")
    result: dict[str, Any] = {
        "id": input_element["id"],
        "isMultiline": multiline if multiline is not None else False,
        "isRequired": not (optional if optional is not None else False),
        "label": input_element["label"],
    }
    max_length = input_element.get("maxLength")
    placeholder = input_element.get("placeholder")
    initial_value = input_element.get("initialValue")
    if max_length:
        result["maxLength"] = max_length
    if placeholder:
        result["placeholder"] = placeholder
    if initial_value:
        result["value"] = initial_value
    result["type"] = "Input.Text"
    return result


def _choice_set(
    select: TeamsModalRadioSelectElement | TeamsModalSelectElement,
    style: Literal["compact", "expanded"],
) -> dict[str, Any]:
    optional = select.get("optional")
    result: dict[str, Any] = {
        "choices": [{"title": option["label"], "value": option["value"]} for option in select["options"]],
        "id": select["id"],
        "isRequired": not (optional if optional is not None else False),
        "label": select["label"],
    }
    placeholder = select.get("placeholder")
    initial_option = select.get("initialOption")
    if placeholder:
        result["placeholder"] = placeholder
    result["style"] = style
    if initial_option:
        result["value"] = initial_option
    result["type"] = "Input.ChoiceSet"
    return result


def _continue_response(title: str, card: Any) -> TeamsTaskModuleResponse:
    return {
        "task": {
            "type": "continue",
            "value": {
                "card": {
                    "content": card,
                    "contentType": ADAPTIVE_CARD_CONTENT_TYPE,
                },
                "title": title,
            },
        }
    }
