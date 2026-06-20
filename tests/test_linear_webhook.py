"""Tests for the Linear agent-session webhook parse + routing (chat@4.31/#151 — L3).

Ported from packages/adapter-linear/src/index.test.ts (the
``handleWebhook - agent session events`` describe block, index.test.ts:1243-1471)
plus the ``getUserNameFromProfileUrl`` util (utils.ts:40).

Covers:
- mode-gating (AgentSessionEvent only in agent-sessions mode; Comment only in
  comments mode), including the inversion both ways;
- ``_parse_message_from_agent_session_event`` for the ``created`` and
  ``prompted`` actions, the null-return + warn paths, and the bot-author
  fallback;
- app-ownership guard (own bot vs. foreign bot);
- ``createdAt`` carried as a raw string;
- no-auto-acknowledge (process_message routed, but no outbound API call);
- ``get_user_name_from_profile_url`` regex.

Each test is written to FAIL under a plausible mutation — in particular a
``??`` → ``or`` truthiness swap or a mode-gate inversion (see the
``test_*_mutation_*`` docstrings).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from chat_sdk.adapters.linear.adapter import (
    LinearAdapter,
    get_user_name_from_profile_url,
)
from chat_sdk.adapters.linear.types import LinearAdapterAPIKeyConfig

WEBHOOK_SECRET = "test-webhook-secret"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_logger() -> MagicMock:
    return MagicMock(
        debug=MagicMock(),
        info=MagicMock(),
        warn=MagicMock(),
        error=MagicMock(),
    )


def _make_chat() -> MagicMock:
    """A mock ChatInstance.

    ``process_message`` is a sync MagicMock (the adapter calls it
    synchronously, matching upstream ``chat.processMessage(...)``); async
    surface methods are AsyncMock so an accidental missing-await surfaces.
    """
    chat = MagicMock()
    chat.process_message = MagicMock()
    chat.handle_incoming_message = AsyncMock()
    chat.get_state = MagicMock(return_value=None)
    chat.get_user_name = MagicMock(return_value="test-bot")
    return chat


def _make_webhook_adapter(mode: str, logger: MagicMock | None = None) -> LinearAdapter:
    if logger is None:
        logger = _make_logger()
    config = LinearAdapterAPIKeyConfig(
        api_key="test-api-key",
        webhook_secret=WEBHOOK_SECRET,
        user_name="test-bot",
        mode=mode,  # type: ignore[arg-type]
        logger=logger,
    )
    adapter = LinearAdapter(config)
    # Single-tenant bot-user-id is set by ``initialize`` (a viewer query) in
    # production; pre-set it here so the app-ownership guard has something to
    # compare against (mirrors upstream's ``setBotUserId`` test seam).
    adapter._bot_user_id = "bot-user-id"
    return adapter


def _sign_payload(body: str, secret: str = WEBHOOK_SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()


class _FakeRequest:
    def __init__(self, body: str, headers: dict[str, str] | None = None):
        self._body = body
        self.headers = headers or {}

    async def text(self) -> str:
        return self._body


def _build_webhook_request(body: str, signature: str | None = None) -> _FakeRequest:
    headers: dict[str, str] = {"content-type": "application/json"}
    if signature is not None:
        headers["linear-signature"] = signature
    return _FakeRequest(body, headers)


def _signed_request(payload: dict[str, Any]) -> _FakeRequest:
    body = json.dumps(payload)
    return _build_webhook_request(body, _sign_payload(body))


# Sentinel distinguishing "key absent" from "value is None" for the creator /
# sourceCommentId overrides (mirrors upstream's ``"creator" in overrides``).
_UNSET = object()


def _create_agent_session_payload(
    *,
    action: str = "created",
    activity_body: str = "Hello from app actor",
    app_user_id: str = "bot-user-id",
    comment_id: str = "comment-root",
    creator: Any = _UNSET,
    issue_id: str = "issue-123",
    prompt_context: str = "Issue TEST-1\n\n@get-bot Hello there",
    session_id: str = "agent-session-1",
    source_comment_body: str = "@test-bot Hello there",
    source_comment_id: Any = _UNSET,
    session_url: Any = None,
) -> dict[str, Any]:
    """Faithful port of ``createAgentSessionPayload`` (index.test.ts:446)."""
    resolved_source_comment_id = "comment-source" if source_comment_id is _UNSET else source_comment_id
    resolved_creator: Any
    if creator is _UNSET:
        resolved_creator = {
            "id": "user-456",
            "name": "Test User",
            "email": None,
            "avatarUrl": None,
            "url": "https://linear.app/test/profiles/test-user",
        }
    else:
        resolved_creator = creator

    return {
        "type": "AgentSessionEvent",
        "action": action,
        "createdAt": "2025-06-01T12:00:00.000Z",
        "appUserId": app_user_id,
        "oauthClientId": "oauth-client-123",
        "organizationId": "org-123",
        "webhookId": "webhook-agent-1",
        "webhookTimestamp": int(time.time() * 1000),
        "promptContext": prompt_context,
        "agentSession": {
            "id": session_id,
            "appUserId": app_user_id,
            "issueId": issue_id,
            "commentId": comment_id,
            "sourceCommentId": resolved_source_comment_id,
            "comment": {
                "id": comment_id,
                "body": source_comment_body,
                "userId": resolved_creator["id"] if resolved_creator else None,
            },
            "creator": resolved_creator,
            "status": "active",
            "summary": "Help with the issue",
            "url": session_url,
        },
        "agentActivity": {
            "id": "agent-activity-1",
            "createdAt": "2025-06-01T12:00:00.000Z",
            "updatedAt": "2025-06-01T12:00:00.000Z",
            "content": {
                "type": "prompt",
                "body": activity_body,
            },
        },
        "actor": {
            "id": "user-456",
            "name": "Test User",
            "type": "user",
        },
    }


def _create_comment_payload(
    *,
    body: str = "@test-bot hello",
    comment_id: str = "comment-abc",
    issue_id: str = "issue-123",
    user_id: str = "user-456",
) -> dict[str, Any]:
    return {
        "type": "Comment",
        "action": "create",
        "createdAt": "2025-06-01T12:00:00.000Z",
        "organizationId": "org-123",
        "url": "https://linear.app/test/issue/TEST-1#comment-abc",
        "webhookId": "webhook-1",
        "webhookTimestamp": int(time.time() * 1000),
        "data": {
            "id": comment_id,
            "body": body,
            "issueId": issue_id,
            "userId": user_id,
            "createdAt": "2025-06-01T12:00:00.000Z",
            "updatedAt": "2025-06-01T12:00:00.000Z",
            "parentId": None,
        },
        "actor": {
            "id": user_id,
            "name": "Test User",
            "type": "user",
        },
    }


# ---------------------------------------------------------------------------
# get_user_name_from_profile_url
# ---------------------------------------------------------------------------


class TestGetUserNameFromProfileUrl:
    def test_extracts_slug_after_profiles(self):
        assert get_user_name_from_profile_url("https://linear.app/test/profiles/test-user") == "test-user"

    def test_stops_at_query_string(self):
        # `[^/?#]+` excludes the query — a `??`/regex slip that captured the
        # whole tail would return "john?tab=activity".
        assert get_user_name_from_profile_url("https://linear.app/acme/profiles/john?tab=activity") == "john"

    def test_stops_at_fragment(self):
        assert get_user_name_from_profile_url("https://linear.app/acme/profiles/jane#bio") == "jane"

    def test_stops_at_trailing_path_segment(self):
        assert get_user_name_from_profile_url("https://linear.app/acme/profiles/sam/activity") == "sam"

    def test_returns_empty_string_on_non_match(self):
        # Returns "" (NOT None) — a mutation to `return None` would break the
        # non-Optional ``str`` contract callers rely on.
        assert get_user_name_from_profile_url("https://example.com/profiles/sam") == ""

    def test_returns_empty_string_when_no_profiles_segment(self):
        assert get_user_name_from_profile_url("https://linear.app/acme/issue/TEST-1") == ""

    def test_requires_https_anchor(self):
        # Anchored at `^https://` — an http URL must not match.
        assert get_user_name_from_profile_url("http://linear.app/acme/profiles/sam") == ""


# ---------------------------------------------------------------------------
# Mode-gating
# ---------------------------------------------------------------------------


class TestModeGating:
    @pytest.mark.asyncio
    async def test_ignores_agent_session_events_in_comment_mode(self):
        logger = _make_logger()
        adapter = _make_webhook_adapter("comments", logger)
        chat = _make_chat()
        adapter._chat = chat

        response = await adapter.handle_webhook(_signed_request(_create_agent_session_payload()))

        assert response["status"] == 200
        chat.process_message.assert_not_called()
        logger.warn.assert_any_call(
            "Received AgentSessionEvent webhook but adapter is not in agent-sessions mode, ignoring"
        )

    @pytest.mark.asyncio
    async def test_ignores_comment_webhooks_in_agent_session_mode(self):
        logger = _make_logger()
        adapter = _make_webhook_adapter("agent-sessions", logger)
        chat = _make_chat()
        adapter._chat = chat

        response = await adapter.handle_webhook(
            _signed_request(_create_comment_payload(body="@test-bot hello", comment_id="comment-source"))
        )

        assert response["status"] == 200
        chat.process_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_comment_webhooks_in_agent_session_mode_even_if_mentions_username(self):
        # Mirrors index.test.ts:1443 — a @-mention of the bot's userName must
        # still be dropped while in agent-sessions mode.
        logger = _make_logger()
        adapter = _make_webhook_adapter("agent-sessions", logger)
        chat = _make_chat()
        chat.get_user_name = MagicMock(return_value="getsquad-dev-samy")
        adapter._chat = chat

        response = await adapter.handle_webhook(
            _signed_request(_create_comment_payload(body="@getsquad-dev-samy hello", comment_id="comment-abc"))
        )

        assert response["status"] == 200
        chat.process_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_comment_still_dispatched_in_comment_mode(self):
        # Anchors the gate: comments DO flow through when mode == "comments".
        # If the gate were inverted, this would fail (no dispatch).
        logger = _make_logger()
        adapter = _make_webhook_adapter("comments", logger)
        chat = _make_chat()
        adapter._chat = chat

        response = await adapter.handle_webhook(_signed_request(_create_comment_payload()))

        assert response["status"] == 200
        chat.process_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_mutation_mode_gate_inversion_would_be_caught(self):
        """A single test combining both gate directions.

        If the AgentSessionEvent gate were inverted (handled in "comments"
        mode), the comment-mode adapter would dispatch the agent event — the
        ``assert_not_called`` below catches that. Paired with
        ``test_dispatches_created_events_*`` (agent-sessions mode DOES
        dispatch), the inversion is fully pinned in both directions.
        """
        logger = _make_logger()
        adapter = _make_webhook_adapter("comments", logger)
        chat = _make_chat()
        adapter._chat = chat

        await adapter.handle_webhook(_signed_request(_create_agent_session_payload(action="created")))

        chat.process_message.assert_not_called()


# ---------------------------------------------------------------------------
# created action
# ---------------------------------------------------------------------------


class TestCreatedAction:
    @pytest.mark.asyncio
    async def test_dispatches_created_events_when_owned_by_this_bot(self):
        logger = _make_logger()
        adapter = _make_webhook_adapter("agent-sessions", logger)
        chat = _make_chat()
        adapter._chat = chat

        response = await adapter.handle_webhook(_signed_request(_create_agent_session_payload()))

        assert response["status"] == 200
        chat.process_message.assert_called_once()
        message = chat.process_message.call_args[0][2]
        assert message.thread_id == "linear:issue-123:c:comment-root:s:agent-session-1"
        assert message.author.user_id == "user-456"
        # userName comes from the creator's profile URL (.../profiles/test-user).
        assert message.author.user_name == "test-user"
        assert message.author.is_bot is False
        assert message.author.is_me is False

    @pytest.mark.asyncio
    async def test_created_event_is_treated_as_a_mention(self):
        # Agent-session comments directly target the bot → isMention True.
        adapter = _make_webhook_adapter("agent-sessions")
        chat = _make_chat()
        adapter._chat = chat

        await adapter.handle_webhook(_signed_request(_create_agent_session_payload()))

        message = chat.process_message.call_args[0][2]
        assert message.is_mention is True

    @pytest.mark.asyncio
    async def test_routed_thread_id_matches_message_thread_id(self):
        # ``_handle_agent_session_event`` routes on ``message.thread_id`` (the
        # 2nd positional arg), which must equal the encoded session thread.
        adapter = _make_webhook_adapter("agent-sessions")
        chat = _make_chat()
        adapter._chat = chat

        await adapter.handle_webhook(_signed_request(_create_agent_session_payload()))

        routed_thread_id = chat.process_message.call_args[0][1]
        message = chat.process_message.call_args[0][2]
        assert routed_thread_id == "linear:issue-123:c:comment-root:s:agent-session-1"
        assert routed_thread_id == message.thread_id

    @pytest.mark.asyncio
    async def test_falls_back_to_bot_author_when_created_session_has_no_creator(self):
        adapter = _make_webhook_adapter("agent-sessions")
        chat = _make_chat()
        adapter._chat = chat

        await adapter.handle_webhook(_signed_request(_create_agent_session_payload(creator=None)))

        chat.process_message.assert_called_once()
        message = chat.process_message.call_args[0][2]
        assert message.author.user_id == "bot-user-id"
        assert message.author.user_name == "test-bot"
        assert message.author.is_bot is True
        assert message.author.is_me is True

    @pytest.mark.asyncio
    async def test_ignores_created_events_that_belong_to_another_bot(self):
        logger = _make_logger()
        adapter = _make_webhook_adapter("agent-sessions", logger)
        chat = _make_chat()
        adapter._chat = chat

        await adapter.handle_webhook(_signed_request(_create_agent_session_payload(app_user_id="other-bot-id")))

        chat.process_message.assert_not_called()
        logger.warn.assert_any_call(
            "Ignoring agent session event from another bot",
            {"agentSessionId": "agent-session-1", "appUserId": "other-bot-id"},
        )

    @pytest.mark.asyncio
    async def test_created_event_carries_created_at_as_string_in_metadata(self):
        # The ``created`` branch reads ``payload.createdAt`` as a raw STRING
        # (no Date cast upstream). The parsed metadata.date_sent reflects it.
        adapter = _make_webhook_adapter("agent-sessions")
        chat = _make_chat()
        adapter._chat = chat

        payload = _create_agent_session_payload()
        payload["createdAt"] = "2025-03-15T10:30:00.000Z"
        await adapter.handle_webhook(_signed_request(payload))

        message = chat.process_message.call_args[0][2]
        assert message.metadata.date_sent.year == 2025
        assert message.metadata.date_sent.month == 3
        assert message.metadata.date_sent.day == 15
        # createdAt == updatedAt (both from payload.createdAt) → not edited.
        assert message.metadata.edited is False

    @pytest.mark.asyncio
    async def test_no_automatic_acknowledgement_for_created_events(self):
        # No-auto-ack: the adapter routes the message but performs no outbound
        # API call (no agentActivityCreate; that lands in L4). Stub the GraphQL
        # transport and assert it is never touched.
        adapter = _make_webhook_adapter("agent-sessions")
        chat = _make_chat()
        adapter._chat = chat
        graphql = AsyncMock()
        adapter._graphql_query = graphql  # type: ignore[method-assign]

        response = await adapter.handle_webhook(_signed_request(_create_agent_session_payload()))

        assert response["status"] == 200
        chat.process_message.assert_called_once()
        graphql.assert_not_called()


# ---------------------------------------------------------------------------
# prompted action + null-return / warn paths
# ---------------------------------------------------------------------------


class TestPromptedAction:
    @pytest.mark.asyncio
    async def test_prompted_without_activity_source_comment_id_returns_null(self):
        # The default agent-activity payload has NO ``sourceCommentId`` on the
        # activity (only the session carries one). The prompted branch warns
        # and returns null → no dispatch. Mirrors index.test.ts:1286.
        logger = _make_logger()
        adapter = _make_webhook_adapter("agent-sessions", logger)
        chat = _make_chat()
        adapter._chat = chat

        response = await adapter.handle_webhook(
            _signed_request(_create_agent_session_payload(action="prompted", activity_body="Can you elaborate?"))
        )

        assert response["status"] == 200
        chat.process_message.assert_not_called()
        logger.warn.assert_any_call(
            "Missing source comment ID for agent activity",
            {"agentSessionId": "agent-session-1", "agentActivityId": "agent-activity-1"},
        )

    @pytest.mark.asyncio
    async def test_prompted_with_source_comment_id_dispatches(self):
        # When the activity DOES carry a sourceCommentId, the prompted branch
        # builds a message (no mention flag inversion — it's still a mention).
        logger = _make_logger()
        adapter = _make_webhook_adapter("agent-sessions", logger)
        chat = _make_chat()
        adapter._chat = chat

        payload = _create_agent_session_payload(action="prompted", activity_body="Can you elaborate?")
        payload["agentActivity"]["sourceCommentId"] = "activity-comment-99"
        payload["agentActivity"]["user"] = {
            "id": "user-789",
            "name": "Prompter",
            "email": None,
            "avatarUrl": None,
            "url": "https://linear.app/test/profiles/prompter",
        }

        await adapter.handle_webhook(_signed_request(payload))

        chat.process_message.assert_called_once()
        message = chat.process_message.call_args[0][2]
        assert message.id == "activity-comment-99"
        assert message.text == "Can you elaborate?"
        assert message.author.user_id == "user-789"
        assert message.author.user_name == "prompter"
        assert message.is_mention is True
        # parse_message encodes the thread comment segment from the raw
        # comment's id, which for "prompted" is the activity's sourceCommentId.
        assert message.thread_id == "linear:issue-123:c:activity-comment-99:s:agent-session-1"

    @pytest.mark.asyncio
    async def test_prompted_without_agent_activity_returns_null(self):
        logger = _make_logger()
        adapter = _make_webhook_adapter("agent-sessions", logger)
        chat = _make_chat()
        adapter._chat = chat

        payload = _create_agent_session_payload(action="prompted")
        payload["agentActivity"] = None

        response = await adapter.handle_webhook(_signed_request(payload))

        assert response["status"] == 200
        chat.process_message.assert_not_called()
        logger.warn.assert_any_call(
            "Missing agent activity for prompted action",
            {"agentSessionId": "agent-session-1"},
        )


# ---------------------------------------------------------------------------
# null-return paths shared across actions
# ---------------------------------------------------------------------------


class TestNullReturnPaths:
    @pytest.mark.asyncio
    async def test_missing_issue_id_returns_null(self):
        # `issueId ?? issue?.id` — when both are absent, parse returns null and
        # _handle_agent_session_event warns "Unable to build message".
        logger = _make_logger()
        adapter = _make_webhook_adapter("agent-sessions", logger)
        chat = _make_chat()
        adapter._chat = chat

        payload = _create_agent_session_payload()
        del payload["agentSession"]["issueId"]

        await adapter.handle_webhook(_signed_request(payload))

        chat.process_message.assert_not_called()
        logger.warn.assert_any_call(
            "Unable to build message for Linear agent session event",
            {"agentSessionId": "agent-session-1"},
        )

    @pytest.mark.asyncio
    async def test_falls_back_to_nested_issue_id(self):
        # `issueId ?? issue?.id` (nullish): absent top-level issueId → use the
        # nested issue.id. A `or` swap would behave the same here, so this is
        # paired with ``test_empty_issue_id_does_not_use_nested`` to pin the
        # nullish (vs. truthy) semantics.
        adapter = _make_webhook_adapter("agent-sessions")
        chat = _make_chat()
        adapter._chat = chat

        payload = _create_agent_session_payload()
        del payload["agentSession"]["issueId"]
        payload["agentSession"]["issue"] = {"id": "issue-from-nested"}

        await adapter.handle_webhook(_signed_request(payload))

        chat.process_message.assert_called_once()
        message = chat.process_message.call_args[0][2]
        assert message.thread_id == "linear:issue-from-nested:c:comment-root:s:agent-session-1"

    @pytest.mark.asyncio
    async def test_missing_comment_for_created_session_returns_null(self):
        logger = _make_logger()
        adapter = _make_webhook_adapter("agent-sessions", logger)
        chat = _make_chat()
        adapter._chat = chat

        payload = _create_agent_session_payload()
        del payload["agentSession"]["comment"]

        await adapter.handle_webhook(_signed_request(payload))

        chat.process_message.assert_not_called()
        logger.warn.assert_any_call(
            "Missing comment for agent session",
            {"agentSessionId": "agent-session-1"},
        )

    @pytest.mark.asyncio
    async def test_unsupported_action_returns_null_and_warns(self):
        logger = _make_logger()
        adapter = _make_webhook_adapter("agent-sessions", logger)
        chat = _make_chat()
        adapter._chat = chat

        payload = _create_agent_session_payload(action="completed")

        await adapter.handle_webhook(_signed_request(payload))

        chat.process_message.assert_not_called()
        logger.warn.assert_any_call(
            "Unsupported agent session event action",
            {"action": "completed", "agentSessionId": "agent-session-1", "issueId": "issue-123"},
        )

    @pytest.mark.asyncio
    async def test_chat_not_initialized_warns_and_returns(self):
        logger = _make_logger()
        adapter = _make_webhook_adapter("agent-sessions", logger)
        adapter._chat = None

        response = await adapter.handle_webhook(_signed_request(_create_agent_session_payload()))

        assert response["status"] == 200
        logger.warn.assert_any_call("Chat instance not initialized, ignoring agent session event")


# ---------------------------------------------------------------------------
# nullish-vs-truthy regression guards (the ??→or mutation crux)
# ---------------------------------------------------------------------------


class TestNullishSemantics:
    @pytest.mark.asyncio
    async def test_empty_issue_id_does_not_use_nested(self):
        """`issueId ?? issue?.id` is nullish, NOT truthy.

        With an EMPTY-STRING top-level issueId and a non-empty nested issue.id,
        the nullish operator keeps the empty string (a real value) → the
        `!issueId` guard then bails (returns null, no dispatch). A `??` → `or`
        mutation would instead fall through to the nested id and DISPATCH —
        this test would then see a process_message call and fail.
        """
        adapter = _make_webhook_adapter("agent-sessions")
        chat = _make_chat()
        adapter._chat = chat

        payload = _create_agent_session_payload()
        payload["agentSession"]["issueId"] = ""
        payload["agentSession"]["issue"] = {"id": "issue-from-nested"}

        await adapter.handle_webhook(_signed_request(payload))

        chat.process_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_session_url_falsy_empty_string_is_preserved(self):
        """`payload.agentSession.url ?? undefined` is nullish.

        An empty-string session url is a real value: it must be carried onto
        the raw comment's ``url`` rather than dropped. A `url or None`
        truthiness swap would drop the empty string. We assert the raw carries
        ``url == ""``.
        """
        adapter = _make_webhook_adapter("agent-sessions")
        chat = _make_chat()
        adapter._chat = chat

        payload = _create_agent_session_payload(session_url="")
        await adapter.handle_webhook(_signed_request(payload))

        message = chat.process_message.call_args[0][2]
        assert message.raw["comment"]["url"] == ""

    @pytest.mark.asyncio
    async def test_session_url_none_is_omitted(self):
        # `url ?? undefined`: a None url is omitted (key absent), not stored.
        adapter = _make_webhook_adapter("agent-sessions")
        chat = _make_chat()
        adapter._chat = chat

        payload = _create_agent_session_payload(session_url=None)
        await adapter.handle_webhook(_signed_request(payload))

        message = chat.process_message.call_args[0][2]
        assert "url" not in message.raw["comment"]

    @pytest.mark.asyncio
    async def test_app_ownership_none_app_user_id_is_rejected_against_real_bot(self):
        """App-ownership guard: a None appUserId must NOT match a real botUserId.

        `agentSession.appUserId !== this.botUserId`. With bot_user_id set and a
        missing/None appUserId, the comparison is a mismatch → ignored. (Guards
        the inverse of the None-botUserId hazard.)
        """
        logger = _make_logger()
        adapter = _make_webhook_adapter("agent-sessions", logger)
        chat = _make_chat()
        adapter._chat = chat

        payload = _create_agent_session_payload()
        payload["agentSession"]["appUserId"] = None
        payload["appUserId"] = None

        await adapter.handle_webhook(_signed_request(payload))

        chat.process_message.assert_not_called()
        logger.warn.assert_any_call(
            "Ignoring agent session event from another bot",
            {"agentSessionId": "agent-session-1", "appUserId": None},
        )
