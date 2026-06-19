"""Generic input-request Adaptive Card helpers for the Teams cards primitive.

Port of ``packages/adapter-teams/src/cards-primitives/input.ts`` (NEW in
chat@4.31.0, commit ``8c71411``), the net-new input surface that ships
alongside the cards primitive (``cards-primitives/index.ts`` is already
covered SDK-free by :mod:`chat_sdk.adapters.teams.cards`).

Builds Adaptive Card JSON for prompt/option input requests (buttons, radio,
select, and freeform text) and parses the corresponding ``Action.Submit``
payloads back into structured responses. Input shapes are TypedDicts with
snake_case keys (``request_id``, ``allow_freeform``, ``option_id``,
``action_id``) — upstream uses camelCase. The emitted Adaptive Card dicts and
the inbound ``data`` keys keep Teams' on-the-wire camelCase field names
(``actionId``, ``isMultiSelect``, ``isMultiline`` …) so the bytes match
upstream exactly.

Importing this module never imports the ``microsoft_teams`` SDK, an HTTP
client, or the high-level :mod:`chat_sdk.adapters.teams.adapter` module. Like
upstream's ``input.ts``, the input helpers perform no escaping (they emit raw
prompt / label / option strings into ``TextBlock`` / ``Input.ChoiceSet``
fields); the :mod:`chat_sdk.adapters.teams.format` primitive is the place to
reach for when a caller needs escaping, but the faithful port does not apply it
here.
"""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

__all__ = [
    "TEAMS_FREEFORM_ACTION_ID",
    "TEAMS_INPUT_ACTION_PREFIX",
    "TeamsInputAction",
    "TeamsInputOption",
    "TeamsInputRequest",
    "TeamsInputResponse",
    "input_request_to_teams_adaptive_card",
    "parse_teams_input_response",
]

# Upstream constants (cards-primitives/input.ts). The option ChoiceSet id and
# every Action.Submit ``actionId`` are prefixed with this; the freeform
# Input.Text element uses the fixed freeform id below. Both are exact — the
# adapter's action router matches on them verbatim.
TEAMS_INPUT_ACTION_PREFIX = "input:"
TEAMS_FREEFORM_ACTION_ID = "input-freeform"

_ADAPTIVE_CARD_SCHEMA = "http://adaptivecards.io/schemas/adaptive-card.json"
_ADAPTIVE_CARD_VERSION = "1.4"


class TeamsInputOption(TypedDict):
    """One selectable option in an input request."""

    id: str
    label: str
    description: NotRequired[str]
    style: NotRequired[Literal["danger", "default", "primary"]]


class TeamsInputRequest(TypedDict):
    """A prompt with optional options rendered as an Adaptive Card."""

    prompt: str
    request_id: str
    allow_freeform: NotRequired[bool]
    display: NotRequired[Literal["buttons", "radio", "select"]]
    options: NotRequired[list[TeamsInputOption]]


class TeamsInputAction(TypedDict):
    """An inbound ``Action.Submit`` payload to parse into a response."""

    action_id: NotRequired[str]
    value: NotRequired[Any]


class TeamsInputResponse(TypedDict):
    """The parsed result of an input interaction."""

    request_id: str
    option_id: NotRequired[str]
    value: NotRequired[str]


def input_request_to_teams_adaptive_card(request: TeamsInputRequest) -> dict[str, Any]:
    """Render an input request as a Teams Adaptive Card dict.

    Port of ``inputRequestToTeamsAdaptiveCard``. ``select`` / ``radio`` render
    a single ``Input.ChoiceSet`` plus one ``Submit`` action; otherwise each
    option becomes its own ``Action.Submit`` button. ``allow_freeform`` adds an
    ``Input.Text`` element and a dedicated "Submit answer" action.
    """
    request_id = request["request_id"]
    input_action_id = f"{TEAMS_INPUT_ACTION_PREFIX}{request_id}"
    body: list[dict[str, Any]] = [
        {
            "text": request["prompt"],
            "type": "TextBlock",
            "wrap": True,
        }
    ]
    actions: list[dict[str, Any]] = []
    options = request.get("options") or []

    display = request.get("display")
    if display in ("select", "radio"):
        body.append(
            {
                "choices": [{"title": option["label"], "value": option["id"]} for option in options],
                "id": input_action_id,
                "isMultiSelect": False,
                "style": "expanded" if display == "radio" else "compact",
                "type": "Input.ChoiceSet",
            }
        )
        actions.append(
            {
                "data": {"actionId": input_action_id},
                "title": "Submit",
                "type": "Action.Submit",
            }
        )
    else:
        for option in options:
            actions.append(
                {
                    "data": {
                        "actionId": input_action_id,
                        "value": option["id"],
                    },
                    "title": option["label"],
                    "type": "Action.Submit",
                }
            )

    if request.get("allow_freeform"):
        body.append(
            {
                "id": TEAMS_FREEFORM_ACTION_ID,
                "isMultiline": True,
                "placeholder": "Type your answer",
                "type": "Input.Text",
            }
        )
        actions.append(
            {
                "data": {
                    "actionId": input_action_id,
                    "freeform": True,
                },
                "title": "Submit answer",
                "type": "Action.Submit",
            }
        )

    return {
        "$schema": _ADAPTIVE_CARD_SCHEMA,
        "actions": actions,
        "body": body,
        "type": "AdaptiveCard",
        "version": _ADAPTIVE_CARD_VERSION,
    }


def parse_teams_input_response(action: TeamsInputAction) -> TeamsInputResponse | None:
    """Parse an inbound ``Action.Submit`` payload, or ``None``.

    Port of ``parseTeamsInputResponse``. Returns ``None`` unless ``action_id``
    starts with :data:`TEAMS_INPUT_ACTION_PREFIX`. A top-level string ``value``
    is treated as the chosen ``option_id`` (button submit); otherwise the
    option id is read from the request-scoped ChoiceSet key and the freeform
    answer from the freeform key. Only string-typed, non-empty values pass —
    matching upstream's ``typeof === "string"`` reads and truthiness guards.
    """
    action_id = action.get("action_id")
    if action_id is None or not action_id.startswith(TEAMS_INPUT_ACTION_PREFIX):
        return None

    request_id = action_id[len(TEAMS_INPUT_ACTION_PREFIX) :]
    input_action_id = f"{TEAMS_INPUT_ACTION_PREFIX}{request_id}"
    value = action.get("value")

    if isinstance(value, str):
        option_id: str | None = value
        freeform_value: str | None = None
    else:
        option_id = _read_string_value(value, input_action_id)
        freeform_value = _read_string_value(value, TEAMS_FREEFORM_ACTION_ID)

    response: TeamsInputResponse = {"request_id": request_id}
    if option_id:
        response["option_id"] = option_id
        response["value"] = option_id
    if freeform_value:
        response["value"] = freeform_value
    return response


def _read_string_value(value: Any, key: str) -> str | None:
    """Read ``value[key]`` only when it is a string in a dict-like ``value``."""
    if not (isinstance(value, dict) and key in value):
        return None
    field = value[key]
    return field if isinstance(field, str) else None
