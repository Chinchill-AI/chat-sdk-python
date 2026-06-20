"""Tests for the Linear agent-session EMIT path (post / typing / stream).

Ported from packages/adapter-linear/src/index.test.ts (chat@4.31 / #151,
the L4 emit surface). The Python adapter has no ``@linear/sdk`` — upstream's
``createAgentActivity`` / ``updateAgentSession`` calls are ported as raw
GraphQL mutations against the published Linear schema:

- ``agentActivityCreate(input: AgentActivityCreateInput!)`` where ``content``
  is a ``JSONObject!`` scalar carrying the LOWERCASE ``type`` enum
  ("response" / "thought" / "error" / "action") plus body/action fields.
- ``agentSessionUpdate(id, input: AgentSessionUpdateInput!)`` for plan updates.

Each test pins the exact mutation payload so a regression — an enum swap
(response→thought), an ephemeral flip, a ``status ?? default`` → ``status or
default`` swap, or a dropped final force-flush — fails the assertion.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from chat_sdk.adapters.linear.adapter import LinearAdapter
from chat_sdk.adapters.linear.types import LinearAdapterAPIKeyConfig
from chat_sdk.shared.errors import AdapterError
from chat_sdk.types import MarkdownTextChunk, PlanUpdateChunk, TaskUpdateChunk

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WEBHOOK_SECRET = "test-webhook-secret"

_SESSION_THREAD = "linear:issue-123:c:comment-root:s:session-789"


def _make_logger() -> MagicMock:
    return MagicMock(
        debug=MagicMock(),
        info=MagicMock(),
        warn=MagicMock(),
        error=MagicMock(),
    )


def _make_adapter(logger: MagicMock | None = None) -> LinearAdapter:
    """Agent-sessions-mode adapter with a known bot-user-id and default org."""
    if logger is None:
        logger = _make_logger()
    config = LinearAdapterAPIKeyConfig(
        api_key="test-api-key",
        webhook_secret=WEBHOOK_SECRET,
        user_name="test-bot",
        mode="agent-sessions",  # type: ignore[arg-type]
        logger=logger,
    )
    adapter = LinearAdapter(config)
    # ``initialize`` (a viewer query) sets these in production; pre-set the
    # single-tenant defaults so the emit path resolves an author + org id.
    adapter._bot_user_id = "bot-user-id"
    adapter._default_organization_id = "org-123"
    return adapter


def _source_comment(
    *,
    activity_id: str = "activity-123",
    body: str = "Agent response",
    with_bot_actor: bool = True,
    with_user: bool = False,
) -> dict[str, Any]:
    """A resolved ``sourceComment`` node, shaped like the raw GraphQL return.

    Mirrors upstream's ``createMockAgentActivityPayload`` ``sourceComment``:
    a comment created by the app (no ``user``, a ``botActor`` fallback) maps to
    a bot author with ``is_me`` true (the bot user id matches).
    """
    comment: dict[str, Any] = {
        "id": f"comment-{activity_id}",
        "body": body,
        "parentId": "comment-root",
        "createdAt": "2025-06-01T12:00:01.000Z",
        "updatedAt": "2025-06-01T12:00:01.000Z",
        "url": f"https://linear.app/comment/{activity_id}",
    }
    if with_user:
        comment["user"] = {
            "id": "human-user-1",
            "displayName": "Ada",
            "name": "Ada Lovelace",
            "email": "ada@example.com",
            "avatarUrl": "https://linear.app/avatar/ada.png",
        }
    if with_bot_actor:
        comment["botActor"] = {
            "id": "bot-user-id",
            "name": "Test Bot",
            "userDisplayName": "Test Bot",
        }
    return comment


def _activity_payload(
    *,
    activity_id: str = "activity-123",
    body: str = "Agent response",
    success: bool = True,
    agent_session_id: str | None = "session-789",
    source_comment: Any = "default",
) -> dict[str, Any]:
    """The ``agentActivityCreate`` payload node (``{success, agentActivity}``).

    The session id is carried under the ``agentSession { id }`` RELATION — the
    real server shape. ``agentSessionId`` is NOT a scalar field on Linear's
    ``AgentActivity`` type (the schema exposes only ``agentSession:
    AgentSession!``), so a fixture emitting a flat ``agentSessionId`` would
    fabricate a field the server never returns. ``agent_session_id=None``
    models a payload with no resolvable ``agentSession.id`` (no relation node).
    """
    agent_session: dict[str, Any] | None = {"id": agent_session_id} if agent_session_id is not None else None
    activity: dict[str, Any] | None = {
        "id": activity_id,
        "agentSession": agent_session,
        "sourceComment": _source_comment(activity_id=activity_id, body=body)
        if source_comment == "default"
        else source_comment,
    }
    return {"success": success, "agentActivity": activity}


def _graphql_return(activity: dict[str, Any]) -> dict[str, Any]:
    """Wrap an activity payload as a ``_graphql_query`` return value."""
    return {"data": {"agentActivityCreate": activity}}


async def _astream(*chunks: Any) -> Any:
    """Build an async iterator from the given chunks."""
    for chunk in chunks:
        yield chunk


# ===========================================================================
# post_message — agent-session branch
# ===========================================================================


class TestPostMessageAgentSession:
    @pytest.mark.asyncio
    async def test_uses_agent_activity_create_with_response(self) -> None:
        adapter = _make_adapter()
        adapter._graphql_query = AsyncMock(return_value=_graphql_return(_activity_payload()))

        result = await adapter.post_message(_SESSION_THREAD, "Agent response")

        call_args = adapter._graphql_query.call_args
        query = call_args[0][0]
        variables = call_args[0][1]
        assert "agentActivityCreate" in query
        # GraphQL-selection proof (fix #1): the return selection requests the
        # ``agentSession { id }`` RELATION, NOT a non-existent scalar
        # ``agentSessionId`` field (which would server-reject the mutation under
        # strict selection validation). FAILS if the selection regresses to
        # ``agentSessionId``.
        assert "agentSession {" in query
        assert "agentSessionId" not in query
        assert variables["input"]["agentSessionId"] == "session-789"
        # Enum-swap proof: the content type MUST be the lowercase "response".
        assert variables["input"]["content"] == {"type": "response", "body": "Agent response"}
        # ephemeral is absent (not False) for a response activity.
        assert "ephemeral" not in variables["input"]

        # The resolved message is built off the source comment.
        assert result.id == "comment-activity-123"
        # thread_id is encoded from the source comment's OWN id (NOT its
        # parentId). Source comment id == "comment-activity-123",
        # parentId == "comment-root" — the encoded ``:c:`` segment MUST be the
        # own id. (Old parentId code would emit ``:c:comment-root:``.)
        assert result.thread_id == "linear:issue-123:c:comment-activity-123:s:session-789"
        assert result.raw["kind"] == "agent_session_comment"
        assert result.raw["organizationId"] == "org-123"
        assert result.raw["agentSessionId"] == "session-789"
        assert result.raw["comment"]["body"] == "Agent response"
        # botActor → bot author, matching bot-user-id → is_me semantics.
        assert result.raw["comment"]["user"]["type"] == "bot"
        assert result.raw["comment"]["user"]["id"] == "bot-user-id"

    @pytest.mark.asyncio
    async def test_calls_ensure_valid_token(self) -> None:
        adapter = _make_adapter()
        adapter._ensure_valid_token = AsyncMock()
        adapter._graphql_query = AsyncMock(return_value=_graphql_return(_activity_payload()))

        await adapter.post_message(_SESSION_THREAD, "Agent response")

        assert adapter._ensure_valid_token.call_count == 1

    @pytest.mark.asyncio
    async def test_comment_branch_unchanged_for_non_session_thread(self) -> None:
        """Regression: a non-session thread still uses commentCreate, NOT agentActivityCreate."""
        adapter = _make_adapter()
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

        result = await adapter.post_message("linear:issue-123:c:parent-comment", "Hello")

        query = adapter._graphql_query.call_args[0][0]
        assert "commentCreate" in query
        assert "agentActivityCreate" not in query
        assert result.raw["kind"] == "comment"


# ===========================================================================
# _parse_message_from_agent_activity
# ===========================================================================


class TestParseMessageFromAgentActivity:
    @pytest.mark.asyncio
    async def test_resolves_user_author_when_user_present(self) -> None:
        adapter = _make_adapter()
        adapter._graphql_query = AsyncMock(
            return_value=_graphql_return(
                _activity_payload(source_comment=_source_comment(with_user=True, with_bot_actor=False))
            )
        )

        result = await adapter.post_message(_SESSION_THREAD, "Agent response")

        user = result.raw["comment"]["user"]
        assert user["type"] == "user"
        assert user["id"] == "human-user-1"
        assert user["displayName"] == "Ada"
        assert user["fullName"] == "Ada Lovelace"

    @pytest.mark.asyncio
    async def test_raises_when_activity_not_successful(self) -> None:
        adapter = _make_adapter()
        adapter._graphql_query = AsyncMock(return_value=_graphql_return(_activity_payload(success=False)))

        with pytest.raises(AdapterError, match="Failed to create Linear agent activity for session session-789"):
            await adapter.post_message(_SESSION_THREAD, "Agent response")

    @pytest.mark.asyncio
    async def test_raises_when_activity_missing(self) -> None:
        adapter = _make_adapter()
        adapter._graphql_query = AsyncMock(return_value=_graphql_return({"success": True, "agentActivity": None}))

        with pytest.raises(AdapterError, match="Failed to create Linear agent activity"):
            await adapter.post_message(_SESSION_THREAD, "Agent response")

    @pytest.mark.asyncio
    async def test_raises_when_source_comment_missing(self) -> None:
        adapter = _make_adapter()
        adapter._graphql_query = AsyncMock(return_value=_graphql_return(_activity_payload(source_comment=None)))

        with pytest.raises(AdapterError, match="Failed to resolve source comment for Linear agent activity"):
            await adapter.post_message(_SESSION_THREAD, "Agent response")

    @pytest.mark.asyncio
    async def test_raises_when_agent_session_id_missing(self) -> None:
        """No resolvable ``agentSession.id`` on the activity → ``AdapterError``.

        The session id is read None-safely off the ``agentSession { id }``
        RELATION (``(activity.get("agentSession") or {}).get("id")``). When the
        relation node is absent (``agent_session_id=None`` → ``agentSession:
        None``), the read yields ``None`` and the guard raises. This must FAIL on
        the old code that read a flat (non-existent) ``activity["agentSessionId"]``
        scalar — with the corrected ``agentSession { id }`` fixture there is no
        such key, so the old read would surface ``None`` differently / KeyError
        rather than this guarded message.
        """
        adapter = _make_adapter()
        adapter._graphql_query = AsyncMock(return_value=_graphql_return(_activity_payload(agent_session_id=None)))

        with pytest.raises(AdapterError, match="Missing agentSessionId"):
            await adapter.post_message(_SESSION_THREAD, "Agent response")

    @pytest.mark.asyncio
    async def test_bot_author_nullish_fallbacks(self) -> None:
        """``botActor.id ?? bot_user_id`` / display/name chained-nullish fallbacks.

        A botActor with ``id=None`` / ``userDisplayName=None`` / ``name=None``
        exercises the three ``is not None`` chains:
        - ``id`` None → falls back to the adapter's ``_bot_user_id``.
        - ``userDisplayName`` None then ``name`` None → "unknown".
        - ``name`` None then ``userDisplayName`` None → "unknown".

        Must FAIL if any chain is swapped from ``is not None`` to ``or`` /
        truthiness (which would coerce identically here only because the values
        are None — but a partial fixture below pins the precedence so a swap
        that drops the *first* operand is caught).
        """
        adapter = _make_adapter()
        bot_comment = _source_comment(with_bot_actor=False)
        bot_comment["botActor"] = {"id": None, "name": None, "userDisplayName": None}
        adapter._graphql_query = AsyncMock(return_value=_graphql_return(_activity_payload(source_comment=bot_comment)))

        result = await adapter.post_message(_SESSION_THREAD, "Agent response")

        user = result.raw["comment"]["user"]
        assert user["type"] == "bot"
        # id None → bot_user_id fallback (NOT "").
        assert user["id"] == "bot-user-id"
        assert user["displayName"] == "unknown"
        assert user["fullName"] == "unknown"

    @pytest.mark.asyncio
    async def test_bot_author_nullish_precedence_keeps_first_present_operand(self) -> None:
        """``userDisplayName ?? name`` keeps ``userDisplayName`` when it is "" (present).

        ``is not None`` keeps an EMPTY-STRING ``userDisplayName`` (it is present,
        just falsy); a truthiness ``or`` swap would wrongly fall through to
        ``name``. Conversely ``fullName`` = ``name ?? userDisplayName`` keeps the
        empty ``name``. This pins the ``is not None`` precedence so a ``or`` swap
        FAILS.
        """
        adapter = _make_adapter()
        bot_comment = _source_comment(with_bot_actor=False)
        bot_comment["botActor"] = {"id": "ba-1", "name": "", "userDisplayName": ""}
        adapter._graphql_query = AsyncMock(return_value=_graphql_return(_activity_payload(source_comment=bot_comment)))

        result = await adapter.post_message(_SESSION_THREAD, "Agent response")

        user = result.raw["comment"]["user"]
        assert user["id"] == "ba-1"
        # "" is present → kept (NOT replaced by name / "unknown").
        assert user["displayName"] == ""
        assert user["fullName"] == ""


# ===========================================================================
# start_typing — agent-session branch
# ===========================================================================


class TestStartTyping:
    @pytest.mark.asyncio
    async def test_emits_ephemeral_thought_with_default_body(self) -> None:
        adapter = _make_adapter()
        adapter._graphql_query = AsyncMock(return_value=_graphql_return(_activity_payload()))

        await adapter.start_typing(_SESSION_THREAD)

        variables = adapter._graphql_query.call_args[0][1]
        assert variables["input"]["agentSessionId"] == "session-789"
        # Enum-swap proof: typing is a "thought", not "response".
        assert variables["input"]["content"] == {"type": "thought", "body": "Thinking..."}
        # ephemeral-flip proof: typing activities MUST be ephemeral.
        assert variables["input"]["ephemeral"] is True

    @pytest.mark.asyncio
    async def test_uses_explicit_status_body(self) -> None:
        adapter = _make_adapter()
        adapter._graphql_query = AsyncMock(return_value=_graphql_return(_activity_payload()))

        await adapter.start_typing(_SESSION_THREAD, "Looking things up...")

        variables = adapter._graphql_query.call_args[0][1]
        assert variables["input"]["content"]["body"] == "Looking things up..."

    @pytest.mark.asyncio
    async def test_empty_string_status_stays_empty(self) -> None:
        """``status ?? "Thinking..."`` is nullish: an empty string is NOT replaced.

        ``status or "Thinking..."`` (truthiness) would wrongly swap "" for the
        default — this asserts the ``is not None`` guard.
        """
        adapter = _make_adapter()
        adapter._graphql_query = AsyncMock(return_value=_graphql_return(_activity_payload()))

        await adapter.start_typing(_SESSION_THREAD, "")

        variables = adapter._graphql_query.call_args[0][1]
        assert variables["input"]["content"]["body"] == ""

    @pytest.mark.asyncio
    async def test_warn_and_noop_for_comment_thread(self) -> None:
        logger = _make_logger()
        adapter = _make_adapter(logger=logger)
        adapter._graphql_query = AsyncMock()

        await adapter.start_typing("linear:issue-123:c:comment-root")

        adapter._graphql_query.assert_not_called()
        logger.warn.assert_called_once()
        assert "only supported in agent session threads" in logger.warn.call_args[0][0]


# ===========================================================================
# _stream_in_agent_session
# ===========================================================================


class TestStreamInAgentSession:
    @pytest.mark.asyncio
    async def test_task_update_creates_action_activity_with_ephemeral_by_status(self) -> None:
        adapter = _make_adapter()
        adapter._graphql_query = AsyncMock(
            side_effect=[
                _graphql_return(_activity_payload(activity_id="opening", body="Hello")),
                _graphql_return(_activity_payload(activity_id="task")),
                _graphql_return(_activity_payload(activity_id="final", body="world")),
            ]
        )

        result = await adapter.stream(
            _SESSION_THREAD,
            _astream(
                "Hello\n",
                TaskUpdateChunk(id="task-1", title="Search docs", status="in_progress", output="Started"),
                MarkdownTextChunk(text="world"),
            ),
        )

        inputs = [call.args[1]["input"] for call in adapter._graphql_query.call_args_list]
        # 1) buffered markdown flushed as a "thought" delta (the "Hello" line).
        assert inputs[0]["content"] == {"type": "thought", "body": "Hello"}
        # 2) task_update → action; non-complete status → ephemeral True.
        assert inputs[1]["content"] == {
            "type": "action",
            "action": "Search docs",
            "parameter": "",
            "result": "Started",
        }
        assert inputs[1]["ephemeral"] is True
        # 3) final force-flush of "world" as a "response".
        assert inputs[2]["content"] == {"type": "response", "body": "world"}
        assert result.raw["kind"] == "agent_session_comment"
        assert result.id == "comment-final"

    @pytest.mark.asyncio
    async def test_complete_task_update_is_not_ephemeral(self) -> None:
        """``ephemeral: status != "complete"`` — a completed action persists."""
        adapter = _make_adapter()
        adapter._graphql_query = AsyncMock(
            side_effect=[
                _graphql_return(_activity_payload(activity_id="task")),
                _graphql_return(_activity_payload(activity_id="final", body="done")),
            ]
        )

        await adapter.stream(
            _SESSION_THREAD,
            _astream(
                TaskUpdateChunk(id="t", title="Build", status="complete", output="ok"),
                MarkdownTextChunk(text="done"),
            ),
        )

        action_input = adapter._graphql_query.call_args_list[0].args[1]["input"]
        assert action_input["content"]["type"] == "action"
        assert action_input["ephemeral"] is False

    @pytest.mark.asyncio
    async def test_error_task_update_creates_error_activity(self) -> None:
        adapter = _make_adapter()
        adapter._graphql_query = AsyncMock(
            side_effect=[
                _graphql_return(_activity_payload(activity_id="err")),
                _graphql_return(_activity_payload(activity_id="final", body="world")),
            ]
        )

        await adapter.stream(
            _SESSION_THREAD,
            _astream(
                TaskUpdateChunk(id="t", title="Search docs", status="error", output="Boom"),
                MarkdownTextChunk(text="world"),
            ),
        )

        error_input = adapter._graphql_query.call_args_list[0].args[1]["input"]
        # Enum-swap proof: error path → "error", not "action".
        assert error_input["content"]["type"] == "error"
        # [title, output].filter(Boolean).join("\n").
        assert error_input["content"]["body"] == "Search docs\nBoom"
        assert "ephemeral" not in error_input

    @pytest.mark.asyncio
    async def test_error_task_update_drops_empty_fields_via_filter_boolean(self) -> None:
        """filter(Boolean) drops None AND empty-string title/output."""
        adapter = _make_adapter()
        adapter._graphql_query = AsyncMock(
            side_effect=[
                _graphql_return(_activity_payload(activity_id="err")),
                _graphql_return(_activity_payload(activity_id="final", body="x")),
            ]
        )

        await adapter.stream(
            _SESSION_THREAD,
            _astream(
                TaskUpdateChunk(id="t", title="", status="error", output="Boom"),
                MarkdownTextChunk(text="x"),
            ),
        )

        error_input = adapter._graphql_query.call_args_list[0].args[1]["input"]
        assert error_input["content"]["body"] == "Boom"

    @pytest.mark.asyncio
    async def test_plan_update_calls_agent_session_update(self) -> None:
        adapter = _make_adapter()
        adapter._graphql_query = AsyncMock(
            side_effect=[
                {"data": {"agentSessionUpdate": {"success": True}}},
                _graphql_return(_activity_payload(activity_id="final", body="Hello world")),
            ]
        )

        result = await adapter.stream(
            _SESSION_THREAD,
            _astream(
                PlanUpdateChunk(title="Search docs"),
                "Hello world",
            ),
        )

        plan_call = adapter._graphql_query.call_args_list[0]
        assert "agentSessionUpdate" in plan_call.args[0]
        assert plan_call.args[1] == {
            "id": "session-789",
            "input": {"plan": [{"content": "Search docs", "status": "completed"}]},
        }
        # The final response still posts after the plan update.
        final_call = adapter._graphql_query.call_args_list[1]
        assert final_call.args[1]["input"]["content"] == {"type": "response", "body": "Hello world"}
        assert result.id == "comment-final"

    @pytest.mark.asyncio
    async def test_final_force_flush_posts_response_even_with_no_delta(self) -> None:
        """The final flush is forced, so it emits even when the delta is empty.

        Drop-the-force-flush proof: a single string chunk is flushed once at the
        end as a forced "response"; there is exactly one mutation call.
        """
        adapter = _make_adapter()
        adapter._graphql_query = AsyncMock(
            return_value=_graphql_return(_activity_payload(activity_id="final", body="Hello world"))
        )

        result = await adapter.stream(_SESSION_THREAD, _astream("Hello world"))

        assert adapter._graphql_query.call_count == 1
        only_input = adapter._graphql_query.call_args.args[1]["input"]
        assert only_input["content"] == {"type": "response", "body": "Hello world"}
        assert result.raw["kind"] == "agent_session_comment"

    @pytest.mark.asyncio
    async def test_empty_delta_flush_is_noop_when_not_forced(self) -> None:
        """A whitespace-only buffer produces no intermediate flush.

        Two task updates fire back-to-back with no new committable markdown
        between them; the pre-action ``flush_markdown("thought")`` must NOT emit
        a thought activity (empty delta, not forced) — only the two actions and
        the final forced response are sent.
        """
        adapter = _make_adapter()
        adapter._graphql_query = AsyncMock(
            side_effect=[
                _graphql_return(_activity_payload(activity_id="a1")),
                _graphql_return(_activity_payload(activity_id="a2")),
                _graphql_return(_activity_payload(activity_id="final", body="")),
            ]
        )

        await adapter.stream(
            _SESSION_THREAD,
            _astream(
                TaskUpdateChunk(id="t1", title="One", status="in_progress", output="a"),
                TaskUpdateChunk(id="t2", title="Two", status="in_progress", output="b"),
            ),
        )

        types = [c.args[1]["input"]["content"]["type"] for c in adapter._graphql_query.call_args_list]
        # No "thought" flush slipped in: action, action, response.
        assert types == ["action", "action", "response"]

    @pytest.mark.asyncio
    async def test_raises_runtime_error_when_final_flush_returns_nothing(self) -> None:
        """``if not finalActivity`` → ``RuntimeError`` (force-flush resolved an empty node).

        The final force-flush calls ``_create_agent_activity``, which returns
        ``result["data"].get("agentActivityCreate", {})``. When the mutation
        response LACKS the ``agentActivityCreate`` key, that read yields an empty
        ``{}`` (falsy) → ``if not final_activity`` fires the bare
        ``RuntimeError`` (upstream's missing-final-flush ``throw new Error``),
        NOT the later ``AdapterError`` parse path. This is a DISTINCT branch from
        the ``{success:False, agentActivity:None}`` parse failure below.
        """
        adapter = _make_adapter()
        adapter._graphql_query = AsyncMock(return_value={"data": {}})

        with pytest.raises(RuntimeError, match="Failed to flush final markdown delta"):
            await adapter.stream(_SESSION_THREAD, _astream("Hello world"))

    @pytest.mark.asyncio
    async def test_raises_adapter_error_when_final_activity_parse_fails(self) -> None:
        """``{success:False, agentActivity:None}`` is a truthy node that FAILS to parse.

        Here the force-flush DOES return a non-empty payload node
        (``{success:False, agentActivity:None}`` is truthy, so ``if not
        final_activity`` is False), and ``_parse_message_from_agent_activity``
        raises ``AdapterError`` on the unsuccessful activity. Separate branch
        from the ``RuntimeError`` force-flush case above.
        """
        adapter = _make_adapter()
        adapter._graphql_query = AsyncMock(
            return_value={"data": {"agentActivityCreate": {"success": False, "agentActivity": None}}}
        )

        with pytest.raises(AdapterError, match="Failed to create Linear agent activity"):
            await adapter.stream(_SESSION_THREAD, _astream("Hello world"))

    @pytest.mark.asyncio
    async def test_stream_thread_id_uses_source_comment_own_id(self) -> None:
        """The streamed result's ``thread_id`` encodes the source comment's OWN id.

        The final-flush source comment has ``id="comment-final"`` and
        ``parentId="comment-root"`` (id != parentId). The encoded ``:c:`` segment
        MUST be the own id ``comment-final`` — NOT ``comment-root``. This FAILS
        on the old code that encoded ``comment_id=comment_data["parentId"]``.
        """
        adapter = _make_adapter()
        adapter._graphql_query = AsyncMock(
            return_value=_graphql_return(_activity_payload(activity_id="final", body="Hello world"))
        )

        result = await adapter.stream(_SESSION_THREAD, _astream("Hello world"))

        assert result.thread_id == "linear:issue-123:c:comment-final:s:session-789"

    @pytest.mark.asyncio
    async def test_task_update_omits_result_key_when_output_none(self) -> None:
        """``result: chunk.output`` with ``output=None`` → key OMITTED on the wire.

        Upstream passes ``result: chunk.output`` (string|undefined); JSON.stringify
        OMITS an undefined key. So a non-error task_update with ``output=None`` must
        build an action content dict WITHOUT a ``result`` key (NOT ``"result":
        None``). FAILS on the old code that always set ``"result": output``.
        """
        adapter = _make_adapter()
        adapter._graphql_query = AsyncMock(
            side_effect=[
                _graphql_return(_activity_payload(activity_id="task")),
                _graphql_return(_activity_payload(activity_id="final", body="done")),
            ]
        )

        await adapter.stream(
            _SESSION_THREAD,
            _astream(
                TaskUpdateChunk(id="t", title="Search docs", status="in_progress", output=None),
                MarkdownTextChunk(text="done"),
            ),
        )

        action_content = adapter._graphql_query.call_args_list[0].args[1]["input"]["content"]
        assert action_content == {"type": "action", "action": "Search docs", "parameter": ""}
        assert "result" not in action_content

    @pytest.mark.asyncio
    async def test_task_update_includes_result_key_when_output_present(self) -> None:
        """Companion to the omit case: a present ``output`` keeps the ``result`` key."""
        adapter = _make_adapter()
        adapter._graphql_query = AsyncMock(
            side_effect=[
                _graphql_return(_activity_payload(activity_id="task")),
                _graphql_return(_activity_payload(activity_id="final", body="done")),
            ]
        )

        await adapter.stream(
            _SESSION_THREAD,
            _astream(
                TaskUpdateChunk(id="t", title="Search docs", status="in_progress", output="Found 3"),
                MarkdownTextChunk(text="done"),
            ),
        )

        action_content = adapter._graphql_query.call_args_list[0].args[1]["input"]["content"]
        assert action_content == {
            "type": "action",
            "action": "Search docs",
            "parameter": "",
            "result": "Found 3",
        }

    @pytest.mark.asyncio
    async def test_final_delta_retains_nel_per_js_trim(self) -> None:
        """``.strip(_JS_WHITESPACE)`` KEEPS NEL (``\\x85``) — JS ``.trim()`` does not strip it.

        NEL is NOT in the JS-``.trim()`` whitespace set, so the trailing
        ``\\x85`` survives the delta trim. Python's bare ``str.strip()`` WOULD
        remove it, so this FAILS if ``.strip(_JS_WHITESPACE)`` is mutated to a
        bare ``.strip()``.
        """
        adapter = _make_adapter()
        adapter._graphql_query = AsyncMock(
            return_value=_graphql_return(_activity_payload(activity_id="final", body="x"))
        )

        await adapter.stream(_SESSION_THREAD, _astream("Hello world\x85"))

        only_input = adapter._graphql_query.call_args.args[1]["input"]
        assert only_input["content"] == {"type": "response", "body": "Hello world\x85"}

    @pytest.mark.asyncio
    async def test_final_delta_strips_bom_per_js_trim(self) -> None:
        """``.strip(_JS_WHITESPACE)`` STRIPS the BOM (``\\ufeff``) — JS ``.trim()`` removes it.

        The BOM IS in the JS-``.trim()`` whitespace set, so a trailing
        ``\\ufeff`` is trimmed off the delta. Python's bare ``str.strip()`` does
        NOT strip the BOM, so this FAILS if ``.strip(_JS_WHITESPACE)`` is mutated
        to a bare ``.strip()`` (the BOM would survive).
        """
        adapter = _make_adapter()
        adapter._graphql_query = AsyncMock(
            return_value=_graphql_return(_activity_payload(activity_id="final", body="x"))
        )

        await adapter.stream(_SESSION_THREAD, _astream("Hello world﻿"))

        only_input = adapter._graphql_query.call_args.args[1]["input"]
        assert only_input["content"] == {"type": "response", "body": "Hello world"}

    @pytest.mark.asyncio
    async def test_dispatches_to_comment_stream_for_non_session_thread(self) -> None:
        """Regression: a non-session thread uses the comment-path stream, NOT agentActivityCreate."""
        adapter = _make_adapter()
        adapter._graphql_query = AsyncMock(
            return_value={
                "data": {
                    "commentCreate": {
                        "success": True,
                        "comment": {
                            "id": "c1",
                            "body": "Hello world",
                            "url": "https://linear.app/test/comment/c1",
                            "createdAt": "2025-06-01T12:00:00.000Z",
                            "updatedAt": "2025-06-01T12:00:00.000Z",
                        },
                    }
                }
            }
        )

        result = await adapter.stream("linear:issue-123:c:comment-root", _astream("Hello world"))

        query = adapter._graphql_query.call_args[0][0]
        assert "commentCreate" in query
        assert "agentActivityCreate" not in query
        assert result.id == "c1"
