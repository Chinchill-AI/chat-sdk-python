"""Tests for the Teams modals primitive (``chat_sdk.adapters.teams.modals``).

Port of upstream ``packages/adapter-teams/src/modals-primitives/index.test.ts``
and ``modals-primitives/boundary.test.ts`` (NEW in vercel/chat@4.31.0, commit
``8c71411``). One Python test per upstream ``it(...)`` plus the boundary
source-scan / fresh-interpreter no-eager-import test.

Upstream uses ``toMatchObject`` (partial, deep) and ``toEqual`` (exact). Where
upstream is partial we assert the load-bearing subset explicitly; where it is
exact (``toEqual`` / ``toBeUndefined``) we assert equality / ``None`` so the
test fails on any divergence.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from chat_sdk.adapters.teams.modals import (
    TeamsModalElement,
    TeamsModalResponse,
    modal_to_adaptive_card,
    parse_teams_dialog_submit_values,
    to_teams_task_module_response,
)

# Shared fixture mirroring the upstream module-level ``modal`` const.
MODAL: TeamsModalElement = {
    "callbackId": "deploy-modal",
    "children": [
        {"content": "Deploy?", "style": "bold", "type": "text"},
        {
            "id": "reason",
            "label": "Reason",
            "placeholder": "Why?",
            "type": "text_input",
        },
    ],
    "title": "Deploy",
    "type": "modal",
}


class TestTeamsModalPrimitives:
    """Mirror of upstream ``describe("Teams modal primitives")``."""

    def test_converts_modal_objects_to_adaptive_cards(self) -> None:
        """it("converts modal objects to Adaptive Cards")."""
        card = modal_to_adaptive_card(MODAL, {"contextId": "ctx"})

        assert card["type"] == "AdaptiveCard"
        assert card["actions"] == [
            {
                "data": {"__callbackId": "deploy-modal", "__contextId": "ctx"},
                "style": "positive",
                "title": "Submit",
                "type": "Action.Submit",
            }
        ]
        # First body element: bold text → Bolder weight.
        assert card["body"][0] == {
            "text": "Deploy?",
            "type": "TextBlock",
            "weight": "Bolder",
            "wrap": True,
        }
        # Second body element: the text input.
        assert card["body"][1]["id"] == "reason"
        assert card["body"][1]["label"] == "Reason"
        assert card["body"][1]["type"] == "Input.Text"

    def test_parses_dialog_submit_values(self) -> None:
        """it("parses dialog submit values")."""
        assert parse_teams_dialog_submit_values(
            {
                "__callbackId": "cb",
                "__contextId": "ctx",
                "msteams": {},
                "reason": "approved",
            }
        ) == {
            "callbackId": "cb",
            "contextId": "ctx",
            "values": {"reason": "approved"},
        }

    def test_creates_task_module_responses(self) -> None:
        """it("creates task module responses")."""
        update: TeamsModalResponse = {"action": "update", "modal": MODAL}
        response = to_teams_task_module_response(update, {"contextId": "ctx"})
        assert response is not None
        assert response["task"]["type"] == "continue"
        assert response["task"]["value"]["card"]["contentType"] == "application/vnd.microsoft.card.adaptive"
        assert response["task"]["value"]["title"] == "Deploy"

        close: TeamsModalResponse = {"action": "close"}
        assert to_teams_task_module_response(close) is None

    def test_renders_validation_errors_as_a_continue_response(self) -> None:
        """it("renders validation errors as a continue response")."""
        errors: TeamsModalResponse = {"action": "errors", "errors": {"reason": "Required"}}
        response = to_teams_task_module_response(errors)
        assert response is not None
        assert response["task"]["value"]["title"] == "Validation Error"
        body = response["task"]["value"]["card"]["content"]["body"]
        assert any(block.get("text") == "**reason**: Required" for block in body)

    def test_converts_every_modal_child_type_with_options_and_styles(self) -> None:
        """it("converts every modal child type with options and styles")."""
        card = modal_to_adaptive_card(
            {
                "callbackId": "cb",
                "children": [
                    {"content": "Muted", "style": "muted", "type": "text"},
                    {
                        "children": [{"label": "Owner", "value": "Ada"}],
                        "type": "fields",
                    },
                    {
                        "id": "summary",
                        "initialValue": "init",
                        "label": "Summary",
                        "maxLength": 200,
                        "multiline": True,
                        "placeholder": "Describe",
                        "type": "text_input",
                    },
                    {
                        "id": "env",
                        "initialOption": "prod",
                        "label": "Env",
                        "optional": True,
                        "options": [{"label": "Prod", "value": "prod"}],
                        "placeholder": "Pick",
                        "type": "select",
                    },
                    {
                        "id": "strategy",
                        "label": "Strategy",
                        "options": [{"label": "BG", "value": "bg"}],
                        "type": "radio_select",
                    },
                ],
                "submitLabel": "Go",
                "title": "All",
                "type": "modal",
            },
            {},
        )

        # Submit action uses the modal callbackId (no option override / contextId).
        assert card["actions"][0]["data"] == {"__callbackId": "cb"}
        assert card["actions"][0]["title"] == "Go"

        body = card["body"]
        by_id = {block.get("id"): block for block in body if "id" in block}

        # Muted text → isSubtle (no weight key).
        muted = next(b for b in body if b.get("text") == "Muted")
        assert muted["isSubtle"] is True
        assert "weight" not in muted

        # FactSet from the fields child.
        fact_set = next(b for b in body if b.get("type") == "FactSet")
        assert fact_set["facts"] == [{"title": "Owner", "value": "Ada"}]

        # Multiline, required text input with maxLength / placeholder / value.
        summary = by_id["summary"]
        assert summary["isMultiline"] is True
        assert summary["isRequired"] is True
        assert summary["maxLength"] == 200
        assert summary["placeholder"] == "Describe"
        assert summary["type"] == "Input.Text"
        assert summary["value"] == "init"

        # Optional compact ChoiceSet → not required, initialOption → value.
        env = by_id["env"]
        assert env["isRequired"] is False
        assert env["placeholder"] == "Pick"
        assert env["style"] == "compact"
        assert env["type"] == "Input.ChoiceSet"
        assert env["value"] == "prod"

        # Radio → expanded ChoiceSet, required (no optional flag).
        strategy = by_id["strategy"]
        assert strategy["isRequired"] is True
        assert strategy["style"] == "expanded"
        assert strategy["type"] == "Input.ChoiceSet"

    def test_prefers_the_callback_id_option_over_the_modal_callback_id(self) -> None:
        """it("prefers the callbackId option over the modal callbackId")."""
        card = modal_to_adaptive_card(MODAL, {"callbackId": "override"})
        assert card["actions"][0]["data"] == {"__callbackId": "override"}

    def test_returns_empty_submit_values_when_data_is_missing(self) -> None:
        """it("returns empty submit values when data is missing")."""
        assert parse_teams_dialog_submit_values(None) == {
            "callbackId": None,
            "contextId": None,
            "values": {},
        }

    def test_stringifies_numeric_submit_values_and_ignores_other_non_strings(self) -> None:
        """it("stringifies numeric submit values and ignores other non-strings") (chat@4.36)."""
        assert parse_teams_dialog_submit_values({"count": 5, "note": "ok", "flag": True}) == {
            "callbackId": None,
            "contextId": None,
            "values": {"count": "5", "note": "ok"},
        }

    def test_creates_continue_responses_for_push_actions(self) -> None:
        """it("creates continue responses for push actions")."""
        push: TeamsModalResponse = {"action": "push", "modal": MODAL}
        response = to_teams_task_module_response(push)
        assert response is not None
        assert response["task"]["type"] == "continue"
        assert response["task"]["value"]["title"] == "Deploy"

    def test_returns_undefined_when_there_is_no_response(self) -> None:
        """it("returns undefined when there is no response")."""
        assert to_teams_task_module_response(None) is None


class TestDateAndNumberInputs:
    """Port of upstream ``modals.test.ts`` ``describe("date and number inputs")`` (chat@4.36, #757).

    Upstream runs these against the SDK-bound ``modalToAdaptiveCard``; Python
    has no SDK-bound modal converter (adapter-level dialogs are a known gap),
    so they run against the modals primitive with its camelCase child keys.
    """

    @staticmethod
    def _render(child: dict[str, Any]) -> dict[str, Any]:
        modal: TeamsModalElement = {
            "callbackId": "cb-1",
            "children": [child],  # type: ignore[list-item]
            "title": "Renewal",
            "type": "modal",
        }
        return modal_to_adaptive_card(modal, {"contextId": "ctx-1"})["body"][0]

    def test_renders_a_date_input_as_input_date(self) -> None:
        assert self._render(
            {
                "id": "renewal_date",
                "initialValue": "2026-08-01",
                "label": "Renewal Date",
                "placeholder": "Pick a date",
                "type": "date_input",
            }
        ) == {
            "id": "renewal_date",
            "isRequired": True,
            "label": "Renewal Date",
            "placeholder": "Pick a date",
            "type": "Input.Date",
            "value": "2026-08-01",
        }

    def test_marks_an_optional_date_input_as_not_required(self) -> None:
        # Empty placeholder / initialValue are omitted (upstream truthy spreads).
        assert self._render(
            {
                "id": "renewal_date",
                "initialValue": "",
                "label": "Renewal Date",
                "optional": True,
                "placeholder": "",
                "type": "date_input",
            }
        ) == {"id": "renewal_date", "isRequired": False, "label": "Renewal Date", "type": "Input.Date"}

    def test_renders_a_number_input_as_input_number_with_numeric_bounds(self) -> None:
        assert self._render(
            {
                "id": "quantity",
                "initialValue": 3,
                "label": "Quantity",
                "max": 10,
                "min": 1,
                "placeholder": "How many?",
                "type": "number_input",
            }
        ) == {
            "id": "quantity",
            "isRequired": True,
            "label": "Quantity",
            "max": 10,
            "min": 1,
            "placeholder": "How many?",
            "type": "Input.Number",
            "value": 3,
        }

    def test_number_input_keeps_zero_bounds_and_value_and_omits_unset_ones(self) -> None:
        # Python-specific guard: ``0`` is a valid bound / value (upstream
        # ``=== undefined`` checks), so a truthiness check would drop it. An
        # empty placeholder is still omitted (upstream truthy spread).
        assert self._render(
            {"id": "n", "initialValue": 0, "label": "N", "max": 0, "min": 0, "placeholder": "", "type": "number_input"}
        ) == {
            "id": "n",
            "isRequired": True,
            "label": "N",
            "max": 0,
            "min": 0,
            "type": "Input.Number",
            "value": 0,
        }

    def test_stringifies_numeric_submit_values(self) -> None:
        parsed = parse_teams_dialog_submit_values(
            {
                "__callbackId": "cb-1",
                "__contextId": "ctx-1",
                "quantity": 3,
                "ratio": 0,
                "renewal_date": "2026-08-01",
            }
        )
        assert parsed["values"] == {"quantity": "3", "ratio": "0", "renewal_date": "2026-08-01"}

    def test_python_numeric_submit_values_format_as_js_string(self) -> None:
        # Python-specific: JSON ``5.0`` parses to a float, which must read
        # "5" as JS ``String(5)`` does, not "5.0"; ``False`` is a bool, not a
        # number. JSON ``1e400`` is a float infinity in Python and
        # ``Infinity`` after JSON.parse, as is an over-long int literal once
        # JS holds it as a double: all render as JS ``String(Infinity)``.
        parsed = parse_teams_dialog_submit_values(
            {
                "a": 5.0,
                "b": 2.5,
                "c": 1e21,
                "d": False,
                "e": float("-inf"),
                "f": float("inf"),
                "g": None,
                "h": [1],
                "i": 10**400,
            }
        )
        assert parsed["values"] == {
            "a": "5",
            "b": "2.5",
            "c": "1e+21",
            "e": "-Infinity",
            "f": "Infinity",
            "i": "Infinity",
        }


class TestModalsImportBoundary:
    """Port of upstream ``modals-primitives/boundary.test.ts``.

    Upstream's boundary test is a static source-scan: it reads every non-test
    ``.ts`` in the directory and asserts the source never imports the full
    adapter (``"chat"``), the shared runtime, or ``@microsoft/teams.apps``. We
    port that source-scan over the modals primitive's ``.py`` file, and add a
    fresh-interpreter assertion that importing the subpath never eagerly loads
    the ``microsoft_teams`` SDK or an HTTP client (httpx / aiohttp). The
    cross-primitive import from ``chat_sdk.adapters.teams.format`` is expected
    and allowed (it mirrors upstream's ``import ... from "../format"``); only
    the high-level adapter / SDK / HTTP imports are forbidden.
    """

    def test_modals_source_does_not_import_the_adapter_sdk_or_runtime(self) -> None:
        from chat_sdk.adapters.teams import modals as modals_mod

        source = Path(modals_mod.__file__).read_text(encoding="utf-8")

        # No Teams SDK import in any form.
        assert "import microsoft_teams" not in source
        assert "from microsoft_teams" not in source
        # No high-level adapter / shared-runtime / cards-runtime imports.
        assert "from chat_sdk.adapters.teams.adapter" not in source
        assert "import chat_sdk.adapters.teams.adapter" not in source
        assert "from chat_sdk.adapters.teams.bridge" not in source
        # No eager HTTP-client import.
        assert "\nimport httpx" not in source
        assert "\nimport aiohttp" not in source

    def test_importing_modals_does_not_eagerly_import_sdk_or_http_client(self) -> None:
        code = (
            "import sys\n"
            "import chat_sdk.adapters.teams.modals\n"
            "forbidden = ['microsoft_teams', 'httpx', 'aiohttp']\n"
            "loaded = [name for name in forbidden if name in sys.modules]\n"
            "assert not loaded, f'modals subpath eagerly imported: {loaded}'\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
