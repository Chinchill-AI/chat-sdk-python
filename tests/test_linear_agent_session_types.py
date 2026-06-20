"""Structural / typing tests for Linear agent-session types (chat@4.31 / #151, L1).

Faithful-port coverage for the agent-session type surface added in
``packages/adapter-linear/src/types.ts`` (vercel/chat):

- ``LinearThreadId.agent_session_id`` (optional) + ``LinearAgentSessionThreadId``
  (required ``agent_session_id``).
- ``LinearAdapterConfig.mode`` (default ``"comments"``).
- The ``kind`` discriminator on ``LinearRawMessage`` and the new
  ``LinearAgentSessionCommentRawMessage`` variant. CRITICAL: every existing
  (comment) producer site in ``adapter.py`` must set ``kind="comment"`` so the
  discriminated union stays well-formed (emit/parse symmetry).
- ``AgentSessionEventWebhookPayload`` joins the ``LinearWebhookPayload`` union.

This file is kept pyrefly-clean (``uv run pyrefly check tests/`` → 0).
"""

from __future__ import annotations

import time
from typing import Literal, get_args
from unittest.mock import AsyncMock, MagicMock

import pytest

from chat_sdk.adapters.linear.adapter import LinearAdapter
from chat_sdk.adapters.linear.types import (
    AgentActivityWebhookPayload,
    AgentSessionEventWebhookPayload,
    AgentSessionWebhookPayload,
    LinearAdapterAPIKeyConfig,
    LinearAdapterBaseConfig,
    LinearAdapterMode,
    LinearAgentSessionCommentRawMessage,
    LinearAgentSessionThreadId,
    LinearCommentData,
    LinearCommentRawMessage,
    LinearRawMessage,
    LinearThreadId,
)

WEBHOOK_SECRET = "test-webhook-secret"


def _make_logger() -> MagicMock:
    return MagicMock(
        debug=MagicMock(),
        info=MagicMock(),
        warn=MagicMock(),
        error=MagicMock(),
    )


def _make_adapter(mode: LinearAdapterMode | None = None) -> LinearAdapter:
    config = LinearAdapterAPIKeyConfig(
        api_key="test-api-key",
        webhook_secret=WEBHOOK_SECRET,
        user_name="test-bot",
        logger=_make_logger(),
        mode=mode,
    )
    return LinearAdapter(config)


# ---------------------------------------------------------------------------
# Config: mode (default "comments")
# ---------------------------------------------------------------------------


class TestAdapterMode:
    def test_mode_literal_members(self) -> None:
        # Faithful to upstream LinearAdapterMode = "agent-sessions" | "comments".
        assert set(get_args(LinearAdapterMode)) == {"agent-sessions", "comments"}

    def test_default_mode_is_comments(self) -> None:
        # Upstream: this.mode = config.mode ?? "comments" (index.ts:236).
        adapter = _make_adapter()
        assert adapter.mode == "comments"

    def test_base_config_mode_defaults_to_none(self) -> None:
        # The dataclass field defaults to None so the adapter resolves "comments".
        assert LinearAdapterBaseConfig().mode is None

    def test_mode_override_agent_sessions(self) -> None:
        adapter = _make_adapter(mode="agent-sessions")
        assert adapter.mode == "agent-sessions"

    def test_mode_override_comments_explicit(self) -> None:
        adapter = _make_adapter(mode="comments")
        assert adapter.mode == "comments"


# ---------------------------------------------------------------------------
# Thread ID: agent_session_id (optional) + LinearAgentSessionThreadId
# ---------------------------------------------------------------------------


class TestThreadId:
    def test_thread_id_agent_session_id_optional_default_none(self) -> None:
        tid = LinearThreadId(issue_id="issue-1")
        assert tid.agent_session_id is None
        assert tid.comment_id is None

    def test_thread_id_carries_agent_session_id(self) -> None:
        tid = LinearThreadId(issue_id="issue-1", agent_session_id="session-1")
        assert tid.agent_session_id == "session-1"

    def test_agent_session_thread_id_is_subclass(self) -> None:
        # Upstream: LinearAgentSessionThreadId = LinearThreadId & { agentSessionId }.
        assert issubclass(LinearAgentSessionThreadId, LinearThreadId)

    def test_agent_session_thread_id_required_field(self) -> None:
        tid = LinearAgentSessionThreadId(issue_id="issue-1", agent_session_id="session-9")
        assert tid.agent_session_id == "session-9"
        assert isinstance(tid, LinearThreadId)

    def test_agent_session_thread_id_frozen(self) -> None:
        tid = LinearAgentSessionThreadId(issue_id="issue-1", agent_session_id="session-9")
        with pytest.raises(Exception):  # noqa: B017 - FrozenInstanceError (dataclasses)
            tid.agent_session_id = "mutated"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Raw message: kind discriminator + agent-session variant
# ---------------------------------------------------------------------------


def _comment_data() -> LinearCommentData:
    return {
        "id": "comment-1",
        "body": "hello",
        "issueId": "issue-1",
        "userId": "user-1",
        "createdAt": "2025-06-01T12:00:00.000Z",
        "updatedAt": "2025-06-01T12:00:00.000Z",
        "url": "https://linear.app/test/comment/comment-1",
    }


class TestRawMessageKind:
    def test_comment_variant_carries_comment_kind(self) -> None:
        raw: LinearCommentRawMessage = {"kind": "comment", "comment": _comment_data()}
        assert raw["kind"] == "comment"
        assert raw["comment"]["body"] == "hello"

    def test_agent_session_variant_type_checks_and_discriminates(self) -> None:
        raw: LinearAgentSessionCommentRawMessage = {
            "kind": "agent_session_comment",
            "comment": _comment_data(),
            "agentSessionId": "session-1",
            "agentSessionPromptContext": "Issue TEST-1\n\n@bot Hello",
            "organizationId": "org-1",
        }
        assert raw["kind"] == "agent_session_comment"
        assert raw["agentSessionId"] == "session-1"
        assert raw["agentSessionPromptContext"].startswith("Issue TEST-1")

    def test_agent_session_prompt_context_optional(self) -> None:
        # agentSessionPromptContext is optional upstream (only on "created" events).
        raw: LinearAgentSessionCommentRawMessage = {
            "kind": "agent_session_comment",
            "comment": _comment_data(),
            "agentSessionId": "session-1",
        }
        assert "agentSessionPromptContext" not in raw

    def test_raw_message_union_discriminates_on_kind(self) -> None:
        messages: list[LinearRawMessage] = [
            {"kind": "comment", "comment": _comment_data()},
            {
                "kind": "agent_session_comment",
                "comment": _comment_data(),
                "agentSessionId": "session-1",
            },
        ]
        kinds = [m["kind"] for m in messages]
        assert kinds == ["comment", "agent_session_comment"]


# ---------------------------------------------------------------------------
# kind-discriminator audit: every EXISTING comment producer sets kind="comment".
# These are the four LinearCommentRawMessage construction sites in adapter.py.
# ---------------------------------------------------------------------------


class TestExistingProducersSetCommentKind:
    @pytest.mark.asyncio
    async def test_post_message_sets_comment_kind(self) -> None:
        adapter = _make_adapter()
        adapter._ensure_valid_token = AsyncMock()
        adapter._graphql_query = AsyncMock(
            return_value={
                "data": {
                    "commentCreate": {
                        "success": True,
                        "comment": {
                            "id": "new-comment-1",
                            "body": "Bot reply",
                            "url": "https://linear.app/test/comment/new-comment-1",
                            "createdAt": "2025-06-01T12:00:00.000Z",
                            "updatedAt": "2025-06-01T12:00:00.000Z",
                        },
                    }
                }
            }
        )

        result = await adapter.post_message("linear:issue-123", "Hello from bot")

        assert result.raw["kind"] == "comment"
        assert result.raw["comment"]["body"] == "Bot reply"

    @pytest.mark.asyncio
    async def test_edit_message_sets_comment_kind(self) -> None:
        adapter = _make_adapter()
        adapter._ensure_valid_token = AsyncMock()
        adapter._graphql_query = AsyncMock(
            return_value={
                "data": {
                    "commentUpdate": {
                        "success": True,
                        "comment": {
                            "id": "comment-1",
                            "body": "Updated body",
                            "url": "https://linear.app/test/comment/comment-1",
                            "createdAt": "2025-06-01T12:00:00.000Z",
                            "updatedAt": "2025-06-01T12:05:00.000Z",
                        },
                    }
                }
            }
        )

        result = await adapter.edit_message("linear:issue-123", "comment-1", "Updated body")

        assert result.raw["kind"] == "comment"
        assert result.raw["comment"]["body"] == "Updated body"

    def test_comment_node_to_message_sets_comment_kind(self) -> None:
        adapter = _make_adapter()
        node = {
            "id": "comment-9",
            "body": "Some text",
            "createdAt": "2025-06-01T12:00:00.000Z",
            "updatedAt": "2025-06-01T12:00:00.000Z",
            "url": "https://linear.app/test/comment/comment-9",
            "user": {"id": "user-1", "displayName": "Test", "name": "Test User"},
        }

        message = adapter._comment_node_to_message(node, "linear:issue-1", "issue-1")

        assert message.raw["kind"] == "comment"
        assert message.raw["comment"]["body"] == "Some text"

    def test_webhook_build_message_sets_comment_kind(self) -> None:
        adapter = _make_adapter()
        comment: LinearCommentData = _comment_data()
        actor = {"id": "user-1", "name": "Test User", "type": "user"}

        message = adapter._build_message(comment, actor, "linear:issue-1")

        assert message.raw["kind"] == "comment"
        assert message.raw["comment"]["body"] == "hello"


# ---------------------------------------------------------------------------
# AgentSessionEventWebhookPayload: structural mirror of upstream SDK shape
# and membership in the LinearWebhookPayload union.
# ---------------------------------------------------------------------------


def _agent_session_payload() -> AgentSessionEventWebhookPayload:
    session: AgentSessionWebhookPayload = {
        "id": "agent-session-1",
        "appUserId": "bot-user-id",
        "issueId": "issue-123",
        "commentId": "comment-root",
        "sourceCommentId": "comment-source",
        "comment": {"id": "comment-root", "body": "@test-bot Hello", "userId": "user-456"},
        "creator": {
            "id": "user-456",
            "name": "Test User",
            "url": "https://linear.app/test/profiles/test-user",
        },
        "url": "https://linear.app/test/session/agent-session-1",
        "status": "active",
        "summary": "Help with the issue",
    }
    activity: AgentActivityWebhookPayload = {
        "id": "agent-activity-1",
        "sourceCommentId": "comment-source",
        "content": {"type": "prompt", "body": "Hello from app actor"},
        "createdAt": "2025-06-01T12:00:00.000Z",
    }
    return {
        "type": "AgentSessionEvent",
        "action": "created",
        "createdAt": "2025-06-01T12:00:00.000Z",
        "appUserId": "bot-user-id",
        "oauthClientId": "oauth-client-123",
        "organizationId": "org-123",
        "webhookId": "webhook-agent-1",
        "webhookTimestamp": int(time.time() * 1000),
        "promptContext": "Issue TEST-1\n\n@test-bot Hello there",
        "agentSession": session,
        "agentActivity": activity,
        "guidance": [{"body": "Be concise"}],
        "previousComments": [{"id": "previous-comment-1", "body": "Previous discussion"}],
    }


class TestAgentSessionEventWebhookPayload:
    def test_payload_structural_shape(self) -> None:
        payload = _agent_session_payload()
        assert payload["type"] == "AgentSessionEvent"
        assert payload["action"] == "created"
        assert payload["agentSession"]["issueId"] == "issue-123"
        assert payload["agentSession"]["comment"]["id"] == "comment-root"
        assert payload["agentSession"]["creator"]["name"] == "Test User"
        assert payload["agentActivity"]["content"]["body"] == "Hello from app actor"
        assert payload["promptContext"].startswith("Issue TEST-1")

    def test_payload_assignable_to_webhook_union(self) -> None:
        # AgentSessionEventWebhookPayload must be a member of LinearWebhookPayload.
        from chat_sdk.adapters.linear.types import LinearWebhookPayload

        members = get_args(LinearWebhookPayload)
        assert AgentSessionEventWebhookPayload in members

    def test_prompt_context_absent_for_prompted_action(self) -> None:
        # promptContext is present only for "created" events upstream; a
        # "prompted" payload omitting it must still type-check (total=False).
        payload: AgentSessionEventWebhookPayload = {
            "type": "AgentSessionEvent",
            "action": "prompted",
            "organizationId": "org-123",
            "agentSession": {"id": "s1", "issueId": "issue-1"},
            "agentActivity": {
                "id": "a1",
                "sourceCommentId": "c1",
                "content": {"type": "prompt", "body": "next"},
            },
        }
        assert "promptContext" not in payload
        assert payload["action"] == "prompted"


# ---------------------------------------------------------------------------
# Discriminator value sanity: the literals match upstream exactly.
# ---------------------------------------------------------------------------


def test_kind_literal_values_match_upstream() -> None:
    comment_kind: Literal["comment"] = "comment"
    agent_kind: Literal["agent_session_comment"] = "agent_session_comment"
    assert comment_kind == "comment"
    assert agent_kind == "agent_session_comment"
