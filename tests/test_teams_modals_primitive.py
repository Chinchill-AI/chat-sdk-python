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

    def test_ignores_non_string_submit_values(self) -> None:
        """it("ignores non-string submit values")."""
        assert parse_teams_dialog_submit_values({"count": 5, "note": "ok"}) == {
            "callbackId": None,
            "contextId": None,
            "values": {"note": "ok"},
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
