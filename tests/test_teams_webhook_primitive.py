"""Tests for the Teams webhook primitives subpath.

Port of ``packages/adapter-teams/src/webhook/index.test.ts`` and
``webhook/boundary.test.ts`` (chat@4.31, commit 8c71411).

Covers the 12 ``it`` cases from ``index.test.ts`` plus the 1 ``it`` source
boundary case (13 upstream cases total), and adds adversarial/divergence
cases that exercise the camelCase ``raw`` passthrough, the ``is not None``
fallbacks, and explicit-``null`` content handling.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from chat_sdk.adapters.teams.webhook import (
    TeamsCardActionPayload,
    TeamsContinuation,
    TeamsConversationUpdatePayload,
    TeamsDialogOpenPayload,
    TeamsDialogSubmitPayload,
    TeamsInstallationUpdatePayload,
    TeamsMessagePayload,
    TeamsMessageReactionPayload,
    TeamsParseOptions,
    TeamsUnsupportedPayload,
    TeamsWebhookParseError,
    TeamsWebhookUser,
    extract_teams_attachments,
    extract_teams_continuation,
    extract_teams_user,
    is_teams_mention,
    parse_teams_webhook_body,
    read_teams_webhook,
)

BASE_ACTIVITY: dict[str, Any] = {
    "channelData": {
        "channel": {"id": "channel-id"},
        "team": {"id": "team-id"},
        "tenant": {"id": "tenant-id"},
    },
    "conversation": {"id": "conversation-id"},
    "from": {"aadObjectId": "aad-id", "id": "user-id", "name": "Ada"},
    "id": "activity-id",
    "serviceUrl": "https://smba.example/",
}


def _activity(**overrides: Any) -> dict[str, Any]:
    return {**BASE_ACTIVITY, **overrides}


class _FakeRequest:
    """Duck-typed request exposing an async ``text()`` (Fetch-style)."""

    def __init__(self, body: str) -> None:
        self._body = body

    async def text(self) -> str:
        return self._body


# ---------------------------------------------------------------------------
# Upstream: "parses message activities with continuation and mention state"
# ---------------------------------------------------------------------------
class TestParseMessage:
    def test_parses_message_with_continuation_and_mention_state(self):
        payload = parse_teams_webhook_body(
            _activity(
                entities=[
                    {
                        "mentioned": {"id": "28:bot-id", "name": "Bot"},
                        "text": "<at>Bot</at>",
                        "type": "mention",
                    }
                ],
                text="<at>Bot</at> hello",
                type="message",
            ),
            TeamsParseOptions(bot_app_id="bot-id"),
        )

        assert isinstance(payload, TeamsMessagePayload)
        assert payload.kind == "message"
        assert payload.is_mention is True
        assert payload.text == "<at>Bot</at> hello"
        assert payload.user == TeamsWebhookUser(aad_object_id="aad-id", id="user-id", name="Ada")
        assert payload.continuation == TeamsContinuation(
            activity_id="activity-id",
            channel_id="channel-id",
            conversation_id="conversation-id",
            reply_to_id=None,
            service_url="https://smba.example/",
            team_id="team-id",
            tenant_id="tenant-id",
        )

    def test_message_text_defaults_to_empty_string_when_absent(self):
        # Divergence guard: upstream ``activity.text ?? ""`` — a missing/non-str
        # text becomes "" rather than ``None``.
        payload = parse_teams_webhook_body(_activity(type="message"))
        assert isinstance(payload, TeamsMessagePayload)
        assert payload.text == ""
        assert payload.is_mention is False  # no bot_app_id given

    def test_message_keeps_camelcase_wire_keys_in_raw_passthrough(self):
        # Hazard: typed result is snake_case, but ``raw`` preserves camelCase.
        activity = _activity(
            channelData={"teamsChannelId": "tc", "teamsTeamId": "tt"},
            replyToId="reply-99",
            type="message",
            text="hi",
        )
        payload = parse_teams_webhook_body(activity)
        assert isinstance(payload, TeamsMessagePayload)
        assert payload.raw is activity
        assert payload.raw["channelData"]["teamsChannelId"] == "tc"
        assert payload.raw["replyToId"] == "reply-99"
        assert payload.raw["from"]["aadObjectId"] == "aad-id"
        # ... while the typed continuation resolves the fallbacks to snake_case.
        assert payload.continuation is not None
        assert payload.continuation.channel_id == "tc"
        assert payload.continuation.team_id == "tt"
        assert payload.continuation.reply_to_id == "reply-99"


# ---------------------------------------------------------------------------
# Upstream: "classifies card actions and dialogs"
# ---------------------------------------------------------------------------
class TestCardActionsAndDialogs:
    def test_adaptive_card_action_invoke(self):
        payload = parse_teams_webhook_body(
            _activity(name="adaptiveCard/action", type="invoke", value={"actionId": "approve"})
        )
        assert isinstance(payload, TeamsCardActionPayload)
        assert payload.kind == "card_action"
        assert payload.action_id == "approve"
        assert payload.value == {"actionId": "approve"}

    def test_task_fetch_is_dialog_open(self):
        payload = parse_teams_webhook_body(_activity(name="task/fetch", type="invoke"))
        assert isinstance(payload, TeamsDialogOpenPayload)
        assert payload.kind == "dialog_open"

    def test_task_submit_is_dialog_submit(self):
        payload = parse_teams_webhook_body(_activity(name="task/submit", type="invoke"))
        assert isinstance(payload, TeamsDialogSubmitPayload)
        assert payload.kind == "dialog_submit"


# ---------------------------------------------------------------------------
# Upstream: "classifies reaction and lifecycle activities"
# ---------------------------------------------------------------------------
class TestReactionAndLifecycle:
    def test_message_reaction(self):
        payload = parse_teams_webhook_body(_activity(action="add", replyToId="message-id", type="messageReaction"))
        assert isinstance(payload, TeamsMessageReactionPayload)
        assert payload.kind == "message_reaction"
        assert payload.action == "add"
        assert payload.message_id == "message-id"

    def test_message_reaction_falls_back_to_activity_id(self):
        # Divergence guard: ``replyToId ?? id`` — without replyToId, the id is used.
        payload = parse_teams_webhook_body(_activity(action="remove", type="messageReaction"))
        assert isinstance(payload, TeamsMessageReactionPayload)
        assert payload.message_id == "activity-id"
        assert payload.action == "remove"

    def test_conversation_update(self):
        payload = parse_teams_webhook_body(_activity(type="conversationUpdate"))
        assert isinstance(payload, TeamsConversationUpdatePayload)
        assert payload.kind == "conversation_update"

    def test_installation_update(self):
        payload = parse_teams_webhook_body(_activity(action="add", type="installationUpdate"))
        assert isinstance(payload, TeamsInstallationUpdatePayload)
        assert payload.kind == "installation_update"
        assert payload.action == "add"

    def test_installation_update_non_string_action_is_none(self):
        # ``typeof action === "string" ? action : undefined``
        payload = parse_teams_webhook_body(_activity(action=123, type="installationUpdate"))
        assert isinstance(payload, TeamsInstallationUpdatePayload)
        assert payload.action is None


# ---------------------------------------------------------------------------
# Upstream: "reads request bodies without verifying JWTs"
# ---------------------------------------------------------------------------
class TestReadTeamsWebhook:
    @pytest.mark.asyncio
    async def test_reads_request_body_without_verifying_jwts(self):
        request = _FakeRequest(json.dumps(_activity(text="hello", type="message")))
        payload = await read_teams_webhook(request)
        assert payload.kind == "message"

    @pytest.mark.asyncio
    async def test_read_teams_webhook_threads_options(self):
        # The options (bot_app_id) must reach the parser through the reader.
        body = json.dumps(
            _activity(
                entities=[{"mentioned": {"id": "28:bot-id"}, "type": "mention"}],
                text="hi",
                type="message",
            )
        )
        payload = await read_teams_webhook(_FakeRequest(body), TeamsParseOptions(bot_app_id="bot-id"))
        assert isinstance(payload, TeamsMessagePayload)
        assert payload.is_mention is True


# ---------------------------------------------------------------------------
# Upstream: "extracts continuation from channelData fallbacks"
# ---------------------------------------------------------------------------
class TestContinuationFallbacks:
    def test_extracts_continuation_from_channeldata_fallbacks(self):
        continuation = extract_teams_continuation(
            {
                "channelData": {"teamsChannelId": "teams-channel", "teamsTeamId": "teams-team"},
                "conversation": {"id": "conversation", "tenantId": "tenant"},
                "serviceUrl": "service",
            }
        )
        assert continuation.channel_id == "teams-channel"
        assert continuation.conversation_id == "conversation"
        assert continuation.service_url == "service"
        assert continuation.team_id == "teams-team"
        assert continuation.tenant_id == "tenant"

    def test_continuation_defaults_when_fields_absent(self):
        # Divergence guard: ``conversation_id``/``service_url`` default to "".
        continuation = extract_teams_continuation({})
        assert continuation.conversation_id == ""
        assert continuation.service_url == ""
        assert continuation.activity_id is None
        assert continuation.channel_id is None
        assert continuation.team_id is None
        assert continuation.tenant_id is None

    def test_continuation_channeldata_wins_over_conversation(self):
        # Upstream builds the continuation via an object spread where the later
        # source wins: ``teamsChannelId`` beats ``channel.id``; ``teamsTeamId``
        # beats ``team.id``; ``channelData.tenant.id`` beats
        # ``conversation.tenantId``.
        continuation = extract_teams_continuation(
            {
                "channelData": {
                    "channel": {"id": "channel-id"},
                    "team": {"id": "team-id"},
                    "teamsChannelId": "teams-channel-id",
                    "teamsTeamId": "teams-team-id",
                    "tenant": {"id": "channeldata-tenant"},
                },
                "conversation": {"id": "c", "tenantId": "conversation-tenant"},
            }
        )
        assert continuation.channel_id == "teams-channel-id"
        assert continuation.team_id == "teams-team-id"
        assert continuation.tenant_id == "channeldata-tenant"


# ---------------------------------------------------------------------------
# Upstream: "throws parse errors for invalid JSON"
# ---------------------------------------------------------------------------
class TestParseErrors:
    def test_throws_parse_error_for_invalid_json(self):
        with pytest.raises(TeamsWebhookParseError):
            parse_teams_webhook_body("{")

    def test_invalid_json_message_matches_upstream(self):
        with pytest.raises(TeamsWebhookParseError, match="Invalid Teams webhook JSON body"):
            parse_teams_webhook_body("{")


# ---------------------------------------------------------------------------
# Upstream: "rejects non-object JSON and non-string bodies"
# ---------------------------------------------------------------------------
class TestNonObjectBodies:
    def test_rejects_non_object_json_string(self):
        with pytest.raises(TeamsWebhookParseError):
            parse_teams_webhook_body("null")

    def test_rejects_non_string_body(self):
        with pytest.raises(TeamsWebhookParseError):
            parse_teams_webhook_body(42)

    def test_non_object_body_message_matches_upstream(self):
        with pytest.raises(TeamsWebhookParseError, match="Teams webhook body must be an object"):
            parse_teams_webhook_body(42)

    def test_accepts_already_decoded_mapping(self):
        # A non-string mapping body is accepted directly (no JSON parse).
        payload = parse_teams_webhook_body(_activity(type="conversationUpdate"))
        assert payload.kind == "conversation_update"


# ---------------------------------------------------------------------------
# Upstream: "classifies Action.Submit messages and msteams payloads as card actions"
# ---------------------------------------------------------------------------
class TestActionSubmitMessages:
    def test_message_with_action_id_value_is_card_action(self):
        payload = parse_teams_webhook_body(_activity(type="message", value={"actionId": "approve"}))
        assert isinstance(payload, TeamsCardActionPayload)
        assert payload.kind == "card_action"
        assert payload.action_id == "approve"

    def test_message_with_msteams_value_is_card_action_without_action_id(self):
        payload = parse_teams_webhook_body(_activity(type="message", value={"msteams": {"type": "messageBack"}}))
        assert isinstance(payload, TeamsCardActionPayload)
        assert payload.kind == "card_action"
        assert payload.action_id is None

    def test_message_with_unrelated_value_stays_a_message(self):
        # Divergence guard: a ``value`` without actionId/msteams keys is NOT a
        # card action — it remains a plain message.
        payload = parse_teams_webhook_body(_activity(type="message", text="hi", value={"foo": 1}))
        assert isinstance(payload, TeamsMessagePayload)
        assert payload.kind == "message"

    def test_card_action_non_string_action_id_is_none(self):
        # ``readActionId``: non-string actionId yields ``None``.
        payload = parse_teams_webhook_body(_activity(type="message", value={"actionId": 7}))
        assert isinstance(payload, TeamsCardActionPayload)
        assert payload.action_id is None


# ---------------------------------------------------------------------------
# Upstream: "marks unknown and unhandled activity types as unsupported"
# ---------------------------------------------------------------------------
class TestUnsupported:
    def test_unknown_type_is_unsupported_with_reason(self):
        payload = parse_teams_webhook_body(_activity(type="typing"))
        assert isinstance(payload, TeamsUnsupportedPayload)
        assert payload.kind == "unsupported"
        assert payload.reason == "Unsupported Teams activity type: typing"

    def test_unhandled_invoke_name_is_unsupported(self):
        payload = parse_teams_webhook_body(_activity(name="signin/verifyState", type="invoke"))
        assert isinstance(payload, TeamsUnsupportedPayload)
        assert payload.kind == "unsupported"

    def test_missing_type_reports_unknown(self):
        payload = parse_teams_webhook_body(_activity())
        assert isinstance(payload, TeamsUnsupportedPayload)
        assert payload.reason == "Unsupported Teams activity type: unknown"


# ---------------------------------------------------------------------------
# Upstream: "extracts a user only when an id is present"
# ---------------------------------------------------------------------------
class TestExtractUser:
    def test_user_without_id_is_none(self):
        assert extract_teams_user({"from": {"name": "Ada"}}) is None

    def test_user_with_id_only(self):
        assert extract_teams_user({"from": {"id": "user-id"}}) == TeamsWebhookUser(id="user-id")

    def test_missing_from_is_none(self):
        assert extract_teams_user({}) is None

    def test_empty_string_id_is_none(self):
        # ``!activity.from?.id`` — an empty-string id is falsy → None.
        assert extract_teams_user({"from": {"id": ""}}) is None


# ---------------------------------------------------------------------------
# Upstream: "normalizes attachments and skips non-object entries"
# ---------------------------------------------------------------------------
class TestExtractAttachments:
    def test_normalizes_and_skips_non_object_entries(self):
        attachments = extract_teams_attachments(
            {
                "attachments": [
                    None,
                    "bad",
                    {
                        "content": {"foo": 1},
                        "contentType": "application/json",
                        "contentUrl": "https://example.com/file",
                        "name": "file",
                    },
                ]
            }
        )
        assert len(attachments) == 1
        only = attachments[0]
        assert only.content == {"foo": 1}
        assert only.content_type == "application/json"
        assert only.content_url == "https://example.com/file"
        assert only.name == "file"
        assert only.raw == {
            "content": {"foo": 1},
            "contentType": "application/json",
            "contentUrl": "https://example.com/file",
            "name": "file",
        }

    def test_non_list_attachments_yields_empty(self):
        assert extract_teams_attachments({}) == []
        assert extract_teams_attachments({"attachments": "nope"}) == []

    def test_explicit_null_content_is_preserved(self):
        # Divergence guard: ``typeof content === "undefined" ? {} : { content }``
        # — an explicit ``null`` content survives, a missing key stays None.
        [with_null] = extract_teams_attachments({"attachments": [{"content": None}]})
        assert with_null.content is None
        assert "content" in with_null.raw
        [without] = extract_teams_attachments({"attachments": [{"name": "x"}]})
        assert without.content is None
        assert "content" not in without.raw

    def test_non_string_content_type_url_name_dropped(self):
        [attachment] = extract_teams_attachments(
            {"attachments": [{"contentType": 1, "contentUrl": 2, "name": 3, "content": "keep"}]}
        )
        assert attachment.content == "keep"
        assert attachment.content_type is None
        assert attachment.content_url is None
        assert attachment.name is None


# ---------------------------------------------------------------------------
# Upstream: "detects mentions by id suffix and ignores them without a botAppId"
# ---------------------------------------------------------------------------
class TestIsTeamsMention:
    def test_detects_mention_by_id_suffix(self):
        activity = {"entities": [{"mentioned": {"id": "28:bot-id"}, "type": "mention"}]}
        assert is_teams_mention(activity, "bot-id") is True

    def test_ignores_without_bot_app_id(self):
        activity = {"entities": [{"mentioned": {"id": "28:bot-id"}, "type": "mention"}]}
        assert is_teams_mention(activity) is False

    def test_exact_id_match(self):
        activity = {"entities": [{"mentioned": {"id": "bot-id"}, "type": "mention"}]}
        assert is_teams_mention(activity, "bot-id") is True

    def test_partial_suffix_without_colon_does_not_match(self):
        # Adversarial: ``endsWith(":" + botAppId)`` requires the exact ":" join.
        # "29-bot-id" ends with "bot-id" but NOT with ":bot-id".
        activity = {"entities": [{"mentioned": {"id": "29-bot-id"}, "type": "mention"}]}
        assert is_teams_mention(activity, "bot-id") is False

    def test_non_mention_entity_ignored(self):
        activity = {"entities": [{"mentioned": {"id": "28:bot-id"}, "type": "clientInfo"}]}
        assert is_teams_mention(activity, "bot-id") is False

    def test_non_list_entities_is_false(self):
        assert is_teams_mention({"entities": "nope"}, "bot-id") is False
        assert is_teams_mention({}, "bot-id") is False


# ---------------------------------------------------------------------------
# Upstream boundary.test.ts: "does not import the full adapter or runtime packages"
# ---------------------------------------------------------------------------
class TestWebhookImportBoundary:
    def test_source_does_not_import_sdk_or_adapter(self):
        """Source-scan boundary (faithful port of upstream's
        ``webhook/boundary.test.ts``): the webhook subpackage's own ``.py``
        files must not import the ``microsoft_teams`` SDK, an HTTP client, the
        shared runtime, or the high-level Teams adapter.

        Upstream's boundary test is itself a source scan, so this mirrors it.
        The complementary runtime ``sys.modules`` boundary — now usable because
        the Teams package ``__init__`` is PEP-562 lazy and no longer eagerly
        imports the adapter — is asserted for all six primitive subpaths in
        ``tests/test_teams_primitives_packaging.py``.
        """
        directory = Path(__file__).resolve().parents[1] / "src" / "chat_sdk" / "adapters" / "teams" / "webhook"
        sources = [path for path in directory.glob("*.py")]
        assert sources, f"no webhook source files found under {directory}"
        # Scan only the *import lines* (stripped) so prose docstrings that name
        # ``microsoft_teams`` as a thing-we-don't-import are not false positives
        # — mirroring upstream's intent (it scans for ``from "@microsoft/..."``).
        import_lines = [
            line.strip()
            for path in sources
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        combined_imports = "\n".join(import_lines)

        forbidden = [
            "microsoft_teams",
            "import httpx",
            "import aiohttp",
            "import requests",
            "from chat_sdk.adapters.teams.adapter",
            "import chat_sdk.adapters.teams.adapter",
            "from chat_sdk.adapters.teams.cards",
            "from chat_sdk.adapters.teams.bridge",
        ]
        hits = [token for token in forbidden if token in combined_imports]
        assert not hits, f"webhook subpath source imports forbidden modules: {hits}"

    def test_subpackage_imports_only_within_itself(self):
        # Every ``chat_sdk`` import in the subpackage stays inside the webhook
        # subpath (no reach into sibling Teams modules).
        directory = Path(__file__).resolve().parents[1] / "src" / "chat_sdk" / "adapters" / "teams" / "webhook"
        for path in directory.glob("*.py"):
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith(("from chat_sdk", "import chat_sdk")):
                    assert "chat_sdk.adapters.teams.webhook" in stripped, (
                        f"{path.name}: non-webhook chat_sdk import: {stripped}"
                    )
