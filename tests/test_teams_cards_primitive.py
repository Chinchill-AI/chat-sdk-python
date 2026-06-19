"""Tests for the Teams cards-primitive input helpers (chat@4.31, 8c71411).

Ports the input-specific ``it`` blocks from upstream
``packages/adapter-teams/src/cards-primitives/index.test.ts`` plus the
``boundary.test.ts`` import-isolation check. The card-emit ``it`` blocks
(``cardToAdaptiveCard`` / ``cardToTeamsFallbackText``) are already exercised
by ``tests/test_teams_cards.py`` against :mod:`chat_sdk.adapters.teams.cards`,
so they are not re-ported here to avoid cross-file duplication.
"""

from __future__ import annotations

from pathlib import Path

from chat_sdk.adapters.teams.cards_input import (
    TEAMS_FREEFORM_ACTION_ID,
    TEAMS_INPUT_ACTION_PREFIX,
    input_request_to_teams_adaptive_card,
    parse_teams_input_response,
)


class TestInputRequestRoundTrip:
    """Build + parse input request cards (port of upstream input ``it`` blocks)."""

    def test_builds_and_parses_input_request_cards(self) -> None:
        """it("builds and parses input request cards")."""
        card = input_request_to_teams_adaptive_card(
            {
                "options": [{"id": "approve", "label": "Approve"}],
                "prompt": "Approve?",
                "request_id": "deploy",
            }
        )

        assert card["actions"] == [
            {
                "data": {"actionId": "input:deploy", "value": "approve"},
                "title": "Approve",
                "type": "Action.Submit",
            }
        ]
        assert parse_teams_input_response({"action_id": "input:deploy", "value": "approve"}) == {
            "option_id": "approve",
            "request_id": "deploy",
            "value": "approve",
        }

    def test_parses_radio_input_values_submitted_under_action_id(self) -> None:
        """it.each(["radio"])("parses %s input values submitted under the action id")."""
        card = input_request_to_teams_adaptive_card(
            {
                "display": "radio",
                "options": [{"id": "approve", "label": "Approve"}],
                "prompt": "Approve?",
                "request_id": "deploy",
            }
        )

        choice_sets = [element for element in card["body"] if element.get("type") == "Input.ChoiceSet"]
        assert choice_sets == [
            {
                "choices": [{"title": "Approve", "value": "approve"}],
                "id": "input:deploy",
                "isMultiSelect": False,
                "style": "expanded",
                "type": "Input.ChoiceSet",
            }
        ]
        assert parse_teams_input_response({"action_id": "input:deploy", "value": {"input:deploy": "approve"}}) == {
            "option_id": "approve",
            "request_id": "deploy",
            "value": "approve",
        }

    def test_parses_select_input_values_submitted_under_action_id(self) -> None:
        """it.each(["select"])("parses %s input values submitted under the action id")."""
        card = input_request_to_teams_adaptive_card(
            {
                "display": "select",
                "options": [{"id": "approve", "label": "Approve"}],
                "prompt": "Approve?",
                "request_id": "deploy",
            }
        )

        choice_sets = [element for element in card["body"] if element.get("type") == "Input.ChoiceSet"]
        assert choice_sets == [
            {
                "choices": [{"title": "Approve", "value": "approve"}],
                "id": "input:deploy",
                "isMultiSelect": False,
                "style": "compact",
                "type": "Input.ChoiceSet",
            }
        ]
        # The single submit action carries only the request-scoped actionId.
        assert card["actions"] == [
            {
                "data": {"actionId": "input:deploy"},
                "title": "Submit",
                "type": "Action.Submit",
            }
        ]
        assert parse_teams_input_response({"action_id": "input:deploy", "value": {"input:deploy": "approve"}}) == {
            "option_id": "approve",
            "request_id": "deploy",
            "value": "approve",
        }

    def test_parses_freeform_text_submitted_under_freeform_input_id(self) -> None:
        """it("parses freeform text submitted under the freeform input id")."""
        card = input_request_to_teams_adaptive_card(
            {
                "allow_freeform": True,
                "options": [{"id": "approve", "label": "Approve"}],
                "prompt": "Approve or explain?",
                "request_id": "deploy",
            }
        )

        text_inputs = [element for element in card["body"] if element.get("type") == "Input.Text"]
        assert text_inputs == [
            {
                "id": "input-freeform",
                "isMultiline": True,
                "placeholder": "Type your answer",
                "type": "Input.Text",
            }
        ]
        # A dedicated freeform submit action plus the per-option button.
        assert {
            "data": {"actionId": "input:deploy", "freeform": True},
            "title": "Submit answer",
            "type": "Action.Submit",
        } in card["actions"]
        assert parse_teams_input_response(
            {
                "action_id": "input:deploy",
                "value": {"input-freeform": "Needs more testing"},
            }
        ) == {"request_id": "deploy", "value": "Needs more testing"}

    def test_returns_parse_failures_for_unknown_action_ids(self) -> None:
        """it("returns parse failures for unknown action ids")."""
        assert parse_teams_input_response({"action_id": "other:deploy"}) is None
        assert parse_teams_input_response({}) is None


class TestParseEdgeCases:
    """Adversarial parse cases beyond the upstream happy paths."""

    def test_freeform_overrides_option_id_when_both_present(self) -> None:
        # Upstream spread order: optionId sets {optionId, value}, then a
        # truthy freeformValue overwrites value. optionId is retained.
        result = parse_teams_input_response(
            {
                "action_id": "input:deploy",
                "value": {
                    "input:deploy": "approve",
                    "input-freeform": "but check logs",
                },
            }
        )
        assert result == {
            "option_id": "approve",
            "request_id": "deploy",
            "value": "but check logs",
        }

    def test_empty_request_id_still_parses(self) -> None:
        # The prefix alone yields an empty request_id; upstream keeps it.
        assert parse_teams_input_response({"action_id": "input:"}) == {"request_id": ""}

    def test_non_string_choice_value_is_ignored(self) -> None:
        # A numeric ChoiceSet value is not a string → no option_id/value.
        assert parse_teams_input_response({"action_id": "input:deploy", "value": {"input:deploy": 42}}) == {
            "request_id": "deploy"
        }

    def test_empty_string_value_does_not_set_option_id(self) -> None:
        # Falsy (empty) string fails the truthiness guard → no option_id.
        assert parse_teams_input_response({"action_id": "input:deploy", "value": ""}) == {"request_id": "deploy"}

    def test_missing_value_yields_request_id_only(self) -> None:
        assert parse_teams_input_response({"action_id": "input:deploy"}) == {"request_id": "deploy"}

    def test_dict_value_without_matching_keys_yields_request_id_only(self) -> None:
        assert parse_teams_input_response({"action_id": "input:deploy", "value": {"unrelated": "x"}}) == {
            "request_id": "deploy"
        }

    def test_buttons_display_emits_one_submit_per_option(self) -> None:
        card = input_request_to_teams_adaptive_card(
            {
                "display": "buttons",
                "options": [
                    {"id": "a", "label": "A"},
                    {"id": "b", "label": "B"},
                ],
                "prompt": "Pick",
                "request_id": "r1",
            }
        )
        assert card["actions"] == [
            {
                "data": {"actionId": "input:r1", "value": "a"},
                "title": "A",
                "type": "Action.Submit",
            },
            {
                "data": {"actionId": "input:r1", "value": "b"},
                "title": "B",
                "type": "Action.Submit",
            },
        ]
        # No ChoiceSet / Text input in the body for the buttons display.
        assert all(element.get("type") == "TextBlock" for element in card["body"])

    def test_card_envelope_constants_and_schema(self) -> None:
        card = input_request_to_teams_adaptive_card({"prompt": "Hi", "request_id": "x"})
        assert card["$schema"] == "http://adaptivecards.io/schemas/adaptive-card.json"
        assert card["type"] == "AdaptiveCard"
        assert card["version"] == "1.4"
        # Empty options + no freeform → only the prompt TextBlock, no actions.
        assert card["body"] == [{"text": "Hi", "type": "TextBlock", "wrap": True}]
        assert card["actions"] == []

    def test_exact_constant_values(self) -> None:
        # Unforgeable sentinels: the wire prefix / freeform id must be exact.
        assert TEAMS_INPUT_ACTION_PREFIX == "input:"
        assert TEAMS_FREEFORM_ACTION_ID == "input-freeform"


class TestCardsPrimitiveBoundary:
    """Port of cards-primitives/boundary.test.ts — import isolation."""

    def test_input_module_does_not_import_sdk_or_runtime(self) -> None:
        """it("does not import the full adapter or runtime packages")."""
        path = Path(__file__).resolve().parents[1] / "src" / "chat_sdk" / "adapters" / "teams" / "cards_input.py"
        # Only consider import-statement lines so a docstring mention of
        # ``microsoft_teams`` is not a false positive (mirrors the Round-1
        # webhook/api boundary source scans).
        imports = "\n".join(
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.lstrip().startswith(("import ", "from "))
        )

        assert "microsoft_teams" not in imports
        assert "chat_sdk.adapters.teams.adapter" not in imports
        assert "httpx" not in imports

    def test_importing_input_module_does_not_load_sdk_or_http_client(self) -> None:
        """Importing the input helpers in a fresh interpreter must not load the
        ``microsoft_teams`` SDK or any HTTP client.

        Run in a *subprocess* so the shared test process's ``sys.modules`` is
        never mutated (a global mutation here would pollute downstream
        module-identity assertions). Mirrors the Round-1 api/webhook boundary
        tests: we do NOT assert the high-level adapter is absent, because the
        eager ``teams/__init__.py`` pulls it in transitively until the lazy
        subpath registration lands in packaging PR T7 — the source scan above
        already proves the input module itself never imports the adapter.
        """
        import subprocess
        import sys

        code = (
            "import importlib, sys\n"
            "importlib.import_module('chat_sdk.adapters.teams.cards_input')\n"
            "forbidden = ['microsoft_teams', 'httpx', 'aiohttp']\n"
            "loaded = [n for n in forbidden if n in sys.modules]\n"
            "assert not loaded, f'input module eagerly imported: {loaded}'\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
