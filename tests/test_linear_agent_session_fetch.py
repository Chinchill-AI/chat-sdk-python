"""Tests for the Linear agent-session FETCH / read path.

Ported from packages/adapter-linear/src/index.test.ts (chat@4.31 / #151, the
L5 fetch surface). The Python adapter has no ``@linear/sdk`` — upstream's
``linear.agentSession(id)`` + ``linear.comments({filter})`` calls
(``fetchAgentSessionMessages``, index.ts:1771) are ported as raw GraphQL
queries against the published Linear schema:

- ``agentSession(id: String!): AgentSession!`` — the ``AgentSession`` type has
  NO scalar ``issueId`` field (only the ``issue`` relation), so the issue id is
  read off ``issue { id }`` (equivalent to upstream's ``agentSession.issueId``).
  The nullable ``comment: Comment`` relation is the root comment.
- ``comments(filter: CommentFilter, first/last): CommentConnection!`` with
  the ``{parent: {id: {eq: root_comment.id}}}`` filter — ``forward`` paginates
  with ``first``, every other direction with ``last``. Upstream passes ONLY
  ``first``/``last`` (it never reads ``options.cursor``), so no ``after`` is
  forwarded — matching the sibling issue/comment fetch paths.

The session's issue must match the thread's issue (chat@4.41.1,
vercel/chat#974): a missing or foreign ``issue.id`` raises ``ValidationError``
before the root comment or the children are loaded.

Since vercel/chat#885 (chat@4.40.0) every session message carries the stable
``linear:{issue}:s:{session}`` thread id, and a session without a root comment
reads its history from ``agentSession.activities`` (``first``/``after`` forward,
``last``/``before`` otherwise).

Each test pins behaviour so a regression — a forward/backward (first↔last) swap,
a per-comment thread id coming back, a dropped ownership check, a missing
append-only guard, or a ``hasNextPage`` cursor-logic flip — fails the
assertion. The append-only edit/delete guards (index.ts:1408 / 1464) are
covered here too.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from chat_sdk.adapters.linear.adapter import LinearAdapter, _render_activity
from chat_sdk.adapters.linear.types import LinearAdapterAPIKeyConfig, LinearAgentSessionThreadId
from chat_sdk.shared.errors import AdapterError, ValidationError
from chat_sdk.types import FetchOptions

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WEBHOOK_SECRET = "test-webhook-secret"

_SESSION_THREAD = "linear:issue-123:s:session-789"
_ISSUE_THREAD = "linear:issue-123"
_COMMENT_THREAD = "linear:issue-123:c:comment-root"


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
    adapter._bot_user_id = "bot-user-id"
    adapter._default_organization_id = "org-123"
    # ``_ensure_valid_token`` runs a viewer query before fetch; stub it out so the
    # only ``_graphql_query`` calls under test are the two fetch queries.
    adapter._ensure_valid_token = AsyncMock(return_value=None)  # type: ignore[method-assign]
    return adapter


def _user_comment(
    *,
    comment_id: str,
    body: str = "hello",
    parent_id: str | None = None,
) -> dict[str, Any]:
    """A comment authored by a human user (``user`` present, no ``botActor``)."""
    comment: dict[str, Any] = {
        "id": comment_id,
        "body": body,
        "parentId": parent_id,
        "createdAt": "2025-06-01T12:00:00.000Z",
        "updatedAt": "2025-06-01T12:00:00.000Z",
        "url": f"https://linear.app/comment/{comment_id}",
        "user": {
            "id": "human-user-1",
            "displayName": "ada",
            "name": "Ada Lovelace",
            "email": "ada@example.com",
            "avatarUrl": "https://linear.app/avatar/ada.png",
        },
    }
    return comment


def _bot_comment(
    *,
    comment_id: str,
    body: str = "agent reply",
    parent_id: str | None = "comment-root",
) -> dict[str, Any]:
    """A comment created by the app (no ``user``, a ``botActor`` fallback)."""
    return {
        "id": comment_id,
        "body": body,
        "parentId": parent_id,
        "createdAt": "2025-06-01T12:00:05.000Z",
        "updatedAt": "2025-06-01T12:00:09.000Z",
        "url": f"https://linear.app/comment/{comment_id}",
        "botActor": {
            "id": "bot-user-id",
            "name": "Test Bot",
            "userDisplayName": "Test Bot",
        },
    }


def _session_return(
    *,
    issue_id: str | None = "issue-123",
    root_comment: Any = "default",
    session_id: str = "session-789",
) -> dict[str, Any]:
    """Wrap an ``agentSession`` node as a ``_graphql_query`` return value.

    The issue id is carried under the ``issue { id }`` RELATION — the real
    server shape. ``AgentSession`` exposes NO scalar ``issueId`` field, so a
    fixture emitting a flat ``issueId`` would fabricate a server-rejected field.
    ``issue_id=None`` models a session whose issue relation is ``null`` (an
    ownership-check failure).
    """
    if root_comment == "default":
        root_comment = _user_comment(comment_id="comment-root", body="root prompt")
    agent_session: dict[str, Any] = {
        "id": session_id,
        "issue": {"id": issue_id} if issue_id is not None else None,
        "comment": root_comment,
    }
    return {"data": {"agentSession": agent_session}}


def _children_return(
    *,
    nodes: list[dict[str, Any]] | None = None,
    has_next_page: bool = False,
    end_cursor: str | None = None,
) -> dict[str, Any]:
    """Wrap a ``comments`` connection as a ``_graphql_query`` return value."""
    return {
        "data": {
            "comments": {
                "nodes": nodes or [],
                "pageInfo": {"hasNextPage": has_next_page, "endCursor": end_cursor},
            }
        }
    }


def _query_router(*returns: dict[str, Any]) -> AsyncMock:
    """An ``_graphql_query`` AsyncMock returning ``returns`` in call order.

    The fetch path issues exactly two queries — the session query first, the
    children query second — so a 2-tuple side-effect pins both.
    """
    return AsyncMock(side_effect=list(returns))


# ===========================================================================
# _fetch_agent_session_messages — happy path
# ===========================================================================


class TestFetchAgentSessionMessagesHappyPath:
    @pytest.mark.asyncio
    async def test_root_plus_children_become_messages(self) -> None:
        adapter = _make_adapter()
        child_a = _bot_comment(comment_id="comment-a", body="first reply")
        child_b = _bot_comment(comment_id="comment-b", body="second reply")
        adapter._graphql_query = _query_router(  # type: ignore[method-assign]
            _session_return(),
            _children_return(nodes=[child_a, child_b]),
        )

        result = await adapter.fetch_messages(_SESSION_THREAD)

        # Root comment is the first message, then each child in order.
        assert [m.id for m in result.messages] == ["comment-root", "comment-a", "comment-b"]
        assert [m.text for m in result.messages] == ["root prompt", "first reply", "second reply"]

        # Stable session thread (vercel/chat#885): every message shares
        # ``linear:{issue}:s:{session}``; no per-comment ``:c:`` segment.
        assert [m.thread_id for m in result.messages] == [_SESSION_THREAD] * 3

        # Agent-session comments directly target the bot → every message is a
        # mention (upstream ``parseMessage`` sets ``isMention`` for the
        # ``agent_session_comment`` kind).
        assert all(m.is_mention for m in result.messages)

    @pytest.mark.asyncio
    async def test_author_resolution_user_vs_bot(self) -> None:
        adapter = _make_adapter()
        # Root authored by a human user; child created by the app (botActor).
        root = _user_comment(comment_id="comment-root", body="human prompt")
        child = _bot_comment(comment_id="comment-a", body="bot reply")
        adapter._graphql_query = _query_router(  # type: ignore[method-assign]
            _session_return(root_comment=root),
            _children_return(nodes=[child]),
        )

        result = await adapter.fetch_messages(_SESSION_THREAD)

        root_msg, child_msg = result.messages
        # User author: not a bot, not me, display name from the comment's user.
        assert root_msg.author.is_bot is False
        assert root_msg.author.user_id == "human-user-1"
        assert root_msg.author.user_name == "ada"
        assert root_msg.author.is_me is False
        # Bot author: botActor fallback, bot-user-id matches → is_me true.
        assert child_msg.author.is_bot is True
        assert child_msg.author.user_id == "bot-user-id"
        assert child_msg.author.is_me is True

    @pytest.mark.asyncio
    async def test_dispatch_calls_session_then_children_queries(self) -> None:
        adapter = _make_adapter()
        adapter._graphql_query = _query_router(  # type: ignore[method-assign]
            _session_return(),
            _children_return(),
        )

        await adapter.fetch_messages(_SESSION_THREAD)

        # First query resolves the agent session by id and selects ``issue { id }``
        # (NOT a scalar ``issueId`` field, which would server-reject the query).
        first_query, first_vars = adapter._graphql_query.call_args_list[0][0]
        assert "agentSession(id: $id)" in first_query
        assert "issue {" in first_query
        # The scalar ``issueId`` must NOT be selected on AgentSession.
        assert "issueId" not in first_query
        assert first_vars == {"id": "session-789"}

        # Second query filters children by parent id and selects pageInfo.
        second_query, second_vars = adapter._graphql_query.call_args_list[1][0]
        assert "comments(" in second_query
        assert "hasNextPage" in second_query
        assert second_vars["filter"] == {"parent": {"id": {"eq": "comment-root"}}}


# ===========================================================================
# _fetch_agent_session_messages — issue ownership (chat@4.41.1 / vercel/chat#974)
# ===========================================================================


class _FakeLinearResponse:
    def __init__(self, body: dict[str, Any]) -> None:
        self.ok = True
        self.status = 200
        self._body = body

    async def json(self) -> dict[str, Any]:
        return self._body

    async def __aenter__(self) -> _FakeLinearResponse:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeLinearHttp:
    """Stands in for the aiohttp session under the REAL ``_graphql_query``.

    Replays ``bodies`` in order and records each request's JSON payload.
    """

    def __init__(self, *bodies: dict[str, Any]) -> None:
        self._bodies = list(bodies)
        self.requests: list[dict[str, Any]] = []

    def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any]) -> _FakeLinearResponse:
        self.requests.append(json)
        return _FakeLinearResponse(self._bodies.pop(0))


class _ReadTrackingDict(dict):
    """A GraphQL node that records which keys the adapter read.

    Upstream asserts the lazy ``agentSession.comment`` getter is never invoked
    for a foreign session. The raw-GraphQL port receives the node as a dict, so
    the equivalent proof is that ``comment`` is never READ off it.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.reads: list[str] = []

    def get(self, key: Any, default: Any = None) -> Any:
        self.reads.append(key)
        return super().get(key, default)

    def __getitem__(self, key: Any) -> Any:
        self.reads.append(key)
        return super().__getitem__(key)


# ``issue`` relation shapes that must all be rejected for thread issue
# ``issue-public``: a foreign issue, an absent relation (JS ``undefined``), a
# ``null`` relation / id (JS ``null``), and an empty id.
_UNVERIFIED_SESSION_ISSUES = {
    "issue-private": {"issue": {"id": "issue-private"}},
    "undefined": {},
    "null": {"issue": {"id": None}},
    "empty": {"issue": {"id": ""}},
}


class TestAgentSessionOwnership:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "thread_id",
        [
            "linear:issue-public:s:private-session",
            "linear:issue-public:c:source-comment:s:private-session",
        ],
    )
    @pytest.mark.parametrize("issue_case", list(_UNVERIFIED_SESSION_ISSUES))
    async def test_rejects_an_unverified_issue_id_before_loading_content(self, thread_id: str, issue_case: str) -> None:
        # Ported: describe.each("agent session ownership for %s") →
        # it.each("rejects an unverified issueId before loading content: %s").
        adapter = _make_adapter()
        agent_session = _ReadTrackingDict(
            id="private-session",
            comment=_user_comment(comment_id="source-comment", body="Private issue content"),
            **_UNVERIFIED_SESSION_ISSUES[issue_case],
        )
        adapter._graphql_query = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                {"data": {"agentSession": agent_session}},
                AssertionError("no content query may run for a foreign session"),
            ]
        )

        with pytest.raises(ValidationError) as exc_info:
            await adapter.fetch_messages(thread_id)

        assert str(exc_info.value) == "Agent session does not belong to this issue"
        assert exc_info.value.adapter == "linear"
        # Only the session lookup ran — no children (or activities) query.
        assert adapter._graphql_query.await_count == 1
        first_query, first_vars = adapter._graphql_query.call_args_list[0][0]
        assert "agentSession(id: $id)" in first_query
        assert first_vars == {"id": "private-session"}
        # The root comment was never read off the foreign session.
        assert "comment" not in agent_session.reads

    @pytest.mark.asyncio
    @pytest.mark.parametrize("issue_id", ["issue-private", None, "issue-public"])
    async def test_validates_agent_session_ownership_through_the_transport(self, issue_id: str | None) -> None:
        # Ported: it.each("validates agent session ownership through the Linear
        # SDK: %s"). Upstream stubs ``fetch`` beneath the SDK; the Python
        # equivalent stubs the aiohttp session beneath the REAL
        # ``_graphql_query``, so the JSON → ``issue { id }`` mapping is exercised
        # end to end. The owned, rootless session resolves via the activities
        # query; the others are rejected after the session lookup alone.
        adapter = _make_adapter()
        http = _FakeLinearHttp(
            {
                "data": {
                    "agentSession": {
                        "id": "session-1",
                        "issue": {"id": issue_id} if issue_id else None,
                        "comment": None,
                    }
                }
            },
            {
                "data": {
                    "agentSession": {
                        "activities": {
                            "nodes": [],
                            "pageInfo": {"hasPreviousPage": False, "startCursor": None},
                        }
                    }
                }
            },
        )
        adapter._get_http_session = AsyncMock(return_value=http)  # type: ignore[method-assign]

        if issue_id == "issue-public":
            result = await adapter.fetch_messages("linear:issue-public:s:session-1")
            assert result.messages == []
            assert result.next_cursor is None
            assert len(http.requests) == 2
            assert "activities(" in http.requests[1]["query"]
            assert http.requests[1]["variables"] == {"id": "session-1", "last": 50}
        else:
            with pytest.raises(ValidationError) as exc_info:
                await adapter.fetch_messages("linear:issue-public:s:session-1")
            assert str(exc_info.value) == "Agent session does not belong to this issue"
            # Exactly one request went out, and it selected the session's issue id.
            assert len(http.requests) == 1
        assert http.requests[0]["variables"] == {"id": "session-1"}
        assert "issue {" in http.requests[0]["query"]

    @pytest.mark.asyncio
    async def test_rejects_when_session_and_thread_issue_ids_are_both_empty(self) -> None:
        """``"" == ""`` must NOT count as ownership: the ``not issue_id`` term
        rejects an empty session issue id even when the (degenerate, directly
        constructed) thread issue id is also empty. Dropping that term would let
        the equality check pass and load the session's content.
        """
        adapter = _make_adapter()
        adapter._graphql_query = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                _session_return(issue_id=""),
                AssertionError("children query must not run for an unverified session"),
            ]
        )
        thread = LinearAgentSessionThreadId(issue_id="", agent_session_id="session-789")

        with pytest.raises(ValidationError) as exc_info:
            await adapter._fetch_agent_session_messages(thread)

        assert str(exc_info.value) == "Agent session does not belong to this issue"
        assert adapter._graphql_query.await_count == 1


# ===========================================================================
# _fetch_agent_session_activities — rootless sessions (vercel/chat#885)
# ===========================================================================


def _activity(
    *,
    activity_id: str,
    created_at: str,
    content: dict[str, Any],
    user: dict[str, Any] | None = None,
    source_comment_id: str | None = None,
) -> dict[str, Any]:
    """An ``AgentActivity`` node as the raw activities query returns it."""
    return {
        "id": activity_id,
        "createdAt": created_at,
        "updatedAt": created_at,
        "sourceComment": {"id": source_comment_id} if source_comment_id is not None else None,
        "user": user,
        "content": content,
    }


def _activities_return(
    nodes: list[dict[str, Any]],
    page_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if page_info is None:
        page_info = {"hasNextPage": False, "hasPreviousPage": False, "endCursor": None, "startCursor": None}
    return {"data": {"agentSession": {"activities": {"nodes": nodes, "pageInfo": page_info}}}}


def _rootless_session(*, issue_id: str = "issue-123", session_id: str = "session-789") -> dict[str, Any]:
    session = _session_return(issue_id=issue_id, root_comment=None, session_id=session_id)
    session["data"]["agentSession"]["url"] = f"https://linear.app/session/{session_id}"
    return session


class TestFetchAgentSessionActivities:
    @pytest.mark.asyncio
    async def test_should_fetch_activities_for_agent_sessions_without_a_root_comment(self) -> None:
        # Ported: "should fetch activities for agent sessions without a root
        # comment". Nodes arrive out of order and are sorted by createdAt.
        adapter = _make_adapter()
        adapter._bot_user_id = "bot-id"
        adapter._default_organization_id = "org-xyz"
        bot_user = {"id": "bot-id", "displayName": "Test Bot", "name": "Test Bot"}
        adapter._graphql_query = _query_router(  # type: ignore[method-assign]
            _rootless_session(issue_id="issue-abc"),
            _activities_return(
                [
                    _activity(
                        activity_id="response-activity",
                        created_at="2025-06-01T10:02:00.000Z",
                        content={"type": "response", "body": "Agent response"},
                        user=bot_user,
                    ),
                    _activity(
                        activity_id="prompt-activity",
                        created_at="2025-06-01T10:00:00.000Z",
                        content={"type": "prompt", "body": "User prompt"},
                        user={"id": "user-1", "displayName": "Alice", "name": "Alice Smith"},
                    ),
                    _activity(
                        activity_id="action-activity",
                        created_at="2025-06-01T10:01:00.000Z",
                        content={
                            "type": "action",
                            "action": "Searching",
                            "parameter": "Chat SDK",
                            "result": "Found documentation",
                        },
                        user=bot_user,
                    ),
                ]
            ),
        )

        result = await adapter.fetch_messages("linear:issue-abc:s:session-789")

        activities_query, activities_vars = adapter._graphql_query.call_args_list[1][0]
        assert "activities(" in activities_query
        assert activities_vars == {"id": "session-789", "last": 50}
        assert [m.text for m in result.messages] == [
            "User prompt",
            "Searching: Chat SDK\nFound documentation",
            "Agent response",
        ]
        assert [m.id for m in result.messages] == ["prompt-activity", "action-activity", "response-activity"]
        assert result.messages[0].author.is_bot is False
        assert result.messages[0].author.is_me is False
        assert result.messages[1].author.is_bot is True
        assert result.messages[1].author.is_me is True
        assert all(m.thread_id == "linear:issue-abc:s:session-789" for m in result.messages)
        assert all(m.is_mention is True for m in result.messages)
        assert result.messages[0].raw["organizationId"] == "org-xyz"
        assert result.messages[0].raw["comment"]["url"] == "https://linear.app/session/session-789"
        assert result.next_cursor is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("options", "expected_vars", "page_info", "expected_cursor"),
        [
            pytest.param(
                FetchOptions(direction="forward", limit=10, cursor="cursor-in"),
                {"id": "session-789", "first": 10, "after": "cursor-in"},
                {"hasNextPage": True, "hasPreviousPage": True, "endCursor": "end", "startCursor": "start"},
                "end",
                id="forward-after-endCursor",
            ),
            pytest.param(
                FetchOptions(direction="backward", limit=10, cursor="cursor-in"),
                {"id": "session-789", "last": 10, "before": "cursor-in"},
                {"hasNextPage": True, "hasPreviousPage": True, "endCursor": "end", "startCursor": "start"},
                "start",
                id="backward-before-startCursor",
            ),
            pytest.param(
                FetchOptions(direction="forward", cursor=""),
                {"id": "session-789", "first": 50},
                {"hasNextPage": False, "hasPreviousPage": True, "endCursor": "end", "startCursor": "start"},
                None,
                id="forward-no-next-page",
            ),
            pytest.param(
                FetchOptions(direction="backward"),
                {"id": "session-789", "last": 50},
                {"hasNextPage": True, "hasPreviousPage": False, "endCursor": "end", "startCursor": "start"},
                None,
                id="backward-no-previous-page",
            ),
        ],
    )
    async def test_activities_cursor_forwarding_and_next_cursor(
        self,
        options: FetchOptions,
        expected_vars: dict[str, Any],
        page_info: dict[str, Any],
        expected_cursor: str | None,
    ) -> None:
        # Unlike the children path, the activities fallback forwards the
        # inbound cursor (``after`` forward, ``before`` otherwise; an empty
        # cursor is not sent) and returns the cursor for the paging direction.
        adapter = _make_adapter()
        adapter._graphql_query = _query_router(  # type: ignore[method-assign]
            _rootless_session(),
            _activities_return([], page_info),
        )

        result = await adapter.fetch_messages(_SESSION_THREAD, options)

        _, activities_vars = adapter._graphql_query.call_args_list[1][0]
        assert activities_vars == expected_vars
        assert result.next_cursor == expected_cursor

    @pytest.mark.asyncio
    async def test_activity_authors_and_ids_fall_back(self) -> None:
        # ``user?.id ?? (isBot ? botUserId : "unknown")``, display-name fallback
        # ``isBot ? userName : "unknown"``, and ``sourceCommentId ?? id``.
        adapter = _make_adapter()
        adapter._graphql_query = _query_router(  # type: ignore[method-assign]
            _rootless_session(),
            _activities_return(
                [
                    _activity(
                        activity_id="prompt-activity",
                        created_at="2025-06-01T10:00:00.000Z",
                        content={"type": "prompt", "body": "hi"},
                        source_comment_id="source-comment-1",
                    ),
                    _activity(
                        activity_id="thought-activity",
                        created_at="2025-06-01T10:01:00.000Z",
                        content={"type": "thought", "body": "thinking"},
                    ),
                ]
            ),
        )

        result = await adapter.fetch_messages(_SESSION_THREAD)

        prompt, thought = result.messages
        assert prompt.id == "source-comment-1"
        assert (prompt.author.user_id, prompt.author.user_name, prompt.author.full_name) == (
            "unknown",
            "unknown",
            "unknown",
        )
        assert prompt.author.is_bot is False
        assert thought.id == "thought-activity"
        assert (thought.author.user_id, thought.author.user_name, thought.author.full_name) == (
            "bot-user-id",
            "test-bot",
            "test-bot",
        )
        assert thought.author.is_bot is True
        assert thought.author.is_me is True


class TestRenderActivity:
    @pytest.mark.parametrize(
        ("content", "expected"),
        [
            pytest.param({"type": "error", "body": "  kept as is  "}, "  kept as is  ", id="body-verbatim"),
            pytest.param({"type": "action", "action": "Read", "parameter": "file.py"}, "Read: file.py", id="no-result"),
            pytest.param(
                {"type": "action", "action": " \u3000 ", "parameter": "", "result": None},
                "Action",
                id="empty-after-trim-action",
            ),
            pytest.param(
                {"type": "action", "action": " Run ", "parameter": "  ", "result": " \ufeff "},
                "Run",
                id="blank-parameter-and-result-dropped",
            ),
            pytest.param(
                {"type": "action", "action": "\x1cRun", "parameter": "x", "result": "done\x85"},
                "\x1cRun: x\ndone\x85",
                id="js-trim-set-keeps-non-js-whitespace",
            ),
        ],
    )
    def test_render_activity(self, content: dict[str, Any], expected: str) -> None:
        # JS ``.trim()`` (``_JS_WHITESPACE``), not ``str.strip()``: U+3000 and
        # the BOM are stripped, U+001C and U+0085 are kept.
        assert _render_activity(content) == expected


# ===========================================================================
# _fetch_agent_session_messages — pagination (forward → first, backward → last)
# ===========================================================================


class TestFetchAgentSessionMessagesPagination:
    @pytest.mark.asyncio
    async def test_forward_uses_first(self) -> None:
        adapter = _make_adapter()
        adapter._graphql_query = _query_router(  # type: ignore[method-assign]
            _session_return(),
            _children_return(),
        )

        await adapter.fetch_messages(_SESSION_THREAD, FetchOptions(direction="forward", limit=10))

        _, children_vars = adapter._graphql_query.call_args_list[1][0]
        # forward → ``first`` carries the limit, ``last`` is None.
        assert children_vars["first"] == 10
        assert children_vars["last"] is None

    @pytest.mark.asyncio
    async def test_backward_uses_last(self) -> None:
        adapter = _make_adapter()
        adapter._graphql_query = _query_router(  # type: ignore[method-assign]
            _session_return(),
            _children_return(),
        )

        await adapter.fetch_messages(_SESSION_THREAD, FetchOptions(direction="backward", limit=10))

        _, children_vars = adapter._graphql_query.call_args_list[1][0]
        # backward → ``last`` carries the limit, ``first`` is None. A forward/
        # backward swap would flip these.
        assert children_vars["last"] == 10
        assert children_vars["first"] is None

    @pytest.mark.asyncio
    async def test_default_direction_uses_last(self) -> None:
        adapter = _make_adapter()
        adapter._graphql_query = _query_router(  # type: ignore[method-assign]
            _session_return(),
            _children_return(),
        )

        # No options → not forward → ``last``, default limit 50.
        await adapter.fetch_messages(_SESSION_THREAD)

        _, children_vars = adapter._graphql_query.call_args_list[1][0]
        assert children_vars["last"] == 50
        assert children_vars["first"] is None

    @pytest.mark.asyncio
    async def test_inbound_cursor_is_not_forwarded_as_after(self) -> None:
        """Faithfulness: upstream ``fetchAgentSessionMessages`` passes ONLY
        ``first``/``last`` — it never reads ``options.cursor`` — and the sibling
        ``_fetch_issue_comments``/``_fetch_comment_thread`` paths forward no
        cursor either. So an inbound ``cursor`` must NOT be plumbed to the
        children query as ``after``. A regression that re-introduced
        ``"after": options.cursor`` (or restored ``$after`` to the query) would
        surface here.
        """
        adapter = _make_adapter()
        adapter._graphql_query = _query_router(  # type: ignore[method-assign]
            _session_return(),
            _children_return(),
        )

        await adapter.fetch_messages(_SESSION_THREAD, FetchOptions(cursor="cursor-xyz"))

        children_query, children_vars = adapter._graphql_query.call_args_list[1][0]
        # No ``after`` variable is sent, and the query declares no ``$after`` param.
        assert "after" not in children_vars
        assert "$after" not in children_query
        assert "after:" not in children_query
        # Only the pagination bounds and filter are forwarded.
        assert set(children_vars) == {"filter", "first", "last"}


# ===========================================================================
# _fetch_agent_session_messages — next_cursor by hasNextPage
# ===========================================================================


class TestFetchAgentSessionMessagesNextCursor:
    @pytest.mark.asyncio
    async def test_next_cursor_present_when_has_next_page(self) -> None:
        adapter = _make_adapter()
        adapter._graphql_query = _query_router(  # type: ignore[method-assign]
            _session_return(),
            _children_return(has_next_page=True, end_cursor="cursor-next"),
        )

        result = await adapter.fetch_messages(_SESSION_THREAD)

        assert result.next_cursor == "cursor-next"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("has_next_page", "end_cursor"),
        [
            # endCursor present but hasNextPage False → suppressed by the
            # ``hasNextPage`` term. A mutation returning endCursor unconditionally
            # (``next_cursor=end_cursor``, or dropping the ``hasNextPage`` guard)
            # would leak ``cursor-stale`` here.
            (False, "cursor-stale"),
            # hasNextPage True but endCursor absent → ``next_cursor`` is None,
            # matching upstream's ``hasNextPage ? (endCursor ?? undefined)
            # : undefined`` (here ``end_cursor if hasNextPage and end_cursor is
            # not None else None``). Pins the ``and end_cursor is not None`` term
            # so a present-cursor regression cannot fabricate a cursor here.
            (True, None),
        ],
    )
    async def test_next_cursor_none_unless_both_has_next_page_and_cursor(
        self, has_next_page: bool, end_cursor: str | None
    ) -> None:
        adapter = _make_adapter()
        adapter._graphql_query = _query_router(  # type: ignore[method-assign]
            _session_return(),
            _children_return(has_next_page=has_next_page, end_cursor=end_cursor),
        )

        result = await adapter.fetch_messages(_SESSION_THREAD)

        # next_cursor is set ONLY when BOTH hasNextPage is true AND endCursor is
        # present; neither case above satisfies both, so it is None.
        assert result.next_cursor is None


# ===========================================================================
# Dispatch ordering — comment-session thread routes to the AGENT-SESSION fetch
# ===========================================================================


class TestFetchDispatchOrdering:
    @pytest.mark.asyncio
    async def test_comment_session_thread_dispatches_to_agent_session_fetch(self) -> None:
        """A ``linear:{issue}:c:{comment}:s:{session}`` thread id must dispatch to
        the AGENT-SESSION fetch, NOT the comment-thread fetch.

        ``fetch_messages`` tests ``decoded.agent_session_id`` BEFORE
        ``decoded.comment_id`` (upstream index.ts:1757 — the ``agentSessionId``
        branch precedes the ``commentId`` branch), and a comment-session thread id
        decodes to a ``LinearThreadId`` with BOTH ``comment_id`` and
        ``agent_session_id`` set. If the agent-session branch were moved BELOW the
        ``comment_id`` branch (a branch-swap mutation), this thread would wrongly
        route to ``_fetch_comment_thread`` and issue the ``comment(id:)`` query
        instead of the two-step ``agentSession``/``comments`` queries — failing the
        assertions below.
        """
        adapter = _make_adapter()
        comment_session_thread = "linear:issue-123:c:comment-root:s:session-789"
        # Sanity: the id genuinely decodes to all three segments (so the routing
        # decision is a real precedence choice, not a parse artifact).
        decoded = adapter.decode_thread_id(comment_session_thread)
        assert decoded.agent_session_id == "session-789"
        assert decoded.comment_id == "comment-root"

        child = _bot_comment(comment_id="comment-a", body="reply")
        adapter._graphql_query = _query_router(  # type: ignore[method-assign]
            _session_return(session_id="session-789"),
            _children_return(nodes=[child]),
        )

        result = await adapter.fetch_messages(comment_session_thread)

        # Two queries ran: the agent-session resolve, then the children connection.
        # The comment-thread fetch (``comment(id: $commentId)``) issues exactly ONE.
        assert adapter._graphql_query.await_count == 2
        first_query, first_vars = adapter._graphql_query.call_args_list[0][0]
        assert "agentSession(id: $id)" in first_query
        assert first_vars == {"id": "session-789"}
        second_query = adapter._graphql_query.call_args_list[1][0][0]
        assert "comments(" in second_query
        # The comment-thread query shape MUST NOT appear — proves we did not route
        # to ``_fetch_comment_thread``.
        assert "comment(id: $commentId)" not in first_query
        assert "comment(id: $commentId)" not in second_query
        # Session messages carry the stable session thread, not the requested
        # per-comment form.
        assert [m.thread_id for m in result.messages] == [_SESSION_THREAD] * 2


# ===========================================================================
# Null-session guard — message describes the real failure (not an ownership error)
# ===========================================================================


class TestNullSessionGuard:
    @pytest.mark.asyncio
    async def test_null_session_raises_not_found(self) -> None:
        """When the raw-GraphQL ``agentSession(id)`` resolves to ``null`` (the
        port-only branch — upstream's SDK throws its own not-found), the guard
        must describe the REAL failure: the session was not found — an
        ``AdapterError``, NOT the ownership ``ValidationError`` that only fires
        AFTER a session resolves with a missing or foreign issue id.
        """
        adapter = _make_adapter()
        adapter._graphql_query = AsyncMock(return_value={"data": {"agentSession": None}})  # type: ignore[method-assign]

        with pytest.raises(AdapterError) as exc_info:
            await adapter.fetch_messages(_SESSION_THREAD)
        assert str(exc_info.value) == "Linear agent session session-789 not found"
        assert not isinstance(exc_info.value, ValidationError)


# ===========================================================================
# Append-only guards — edit / delete
# ===========================================================================


class TestAppendOnlyGuards:
    @pytest.mark.asyncio
    async def test_edit_message_raises_for_agent_session(self) -> None:
        adapter = _make_adapter()
        # The guard must fire before any GraphQL mutation. A failing AsyncMock
        # proves no mutation was attempted (the guard short-circuits first).
        adapter._graphql_query = AsyncMock(side_effect=AssertionError("must not run a mutation"))  # type: ignore[method-assign]

        with pytest.raises(AdapterError) as exc_info:
            await adapter.edit_message(_SESSION_THREAD, "comment-a", "new body")
        assert str(exc_info.value) == "Linear agent session activities are append-only and cannot be edited"

    @pytest.mark.asyncio
    async def test_delete_message_raises_for_agent_session(self) -> None:
        adapter = _make_adapter()
        adapter._graphql_query = AsyncMock(side_effect=AssertionError("must not run a mutation"))  # type: ignore[method-assign]

        with pytest.raises(AdapterError) as exc_info:
            await adapter.delete_message(_SESSION_THREAD, "comment-a")
        assert str(exc_info.value) == "Linear agent session activities are append-only and cannot be deleted"

    @pytest.mark.asyncio
    async def test_edit_message_still_works_for_comment_thread(self) -> None:
        """Regression: the comment path edit must be UNCHANGED by the new guard."""
        adapter = _make_adapter()
        adapter._graphql_query = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "data": {
                    "commentUpdate": {
                        "success": True,
                        "comment": {
                            "id": "comment-root",
                            "body": "edited",
                            "url": "https://linear.app/comment/comment-root",
                            "createdAt": "2025-06-01T12:00:00.000Z",
                            "updatedAt": "2025-06-01T12:05:00.000Z",
                        },
                    }
                }
            }
        )

        result = await adapter.edit_message(_COMMENT_THREAD, "comment-root", "edited")

        assert result.id == "comment-root"
        assert adapter._graphql_query.await_count == 1

    @pytest.mark.asyncio
    async def test_delete_message_still_works_for_comment_thread(self) -> None:
        """Regression: the comment path delete must be UNCHANGED by the new guard."""
        adapter = _make_adapter()
        adapter._graphql_query = AsyncMock(return_value={"data": {"commentDelete": {"success": True}}})  # type: ignore[method-assign]

        await adapter.delete_message(_COMMENT_THREAD, "comment-root")

        assert adapter._graphql_query.await_count == 1


# ===========================================================================
# Comment-path fetch — UNCHANGED regression
# ===========================================================================


class TestCommentPathFetchUnchanged:
    @pytest.mark.asyncio
    async def test_issue_thread_fetch_uses_issue_comments_query(self) -> None:
        """A non-session, non-comment thread still routes to issue-comments."""
        adapter = _make_adapter()
        adapter._graphql_query = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "data": {
                    "issue": {
                        "comments": {
                            "nodes": [
                                {
                                    "id": "comment-1",
                                    "body": "top-level",
                                    "createdAt": "2025-06-01T12:00:00.000Z",
                                    "updatedAt": "2025-06-01T12:00:00.000Z",
                                    "url": "https://linear.app/comment/comment-1",
                                    "user": {"id": "u1", "displayName": "u", "name": "User"},
                                }
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        )

        result = await adapter.fetch_messages(_ISSUE_THREAD)

        query = adapter._graphql_query.call_args[0][0]
        # Issue-comments query, NOT the agent-session query.
        assert "issue(id: $issueId)" in query
        assert "agentSession" not in query
        assert [m.id for m in result.messages] == ["comment-1"]
        # The comment path keeps the FIXED thread_id (the passed thread id) — it
        # is NOT re-encoded per comment like the session path.
        assert result.messages[0].thread_id == _ISSUE_THREAD

    @pytest.mark.asyncio
    async def test_comment_thread_fetch_uses_comment_query(self) -> None:
        """A ``:c:`` thread still routes to the comment-thread fetch."""
        adapter = _make_adapter()
        adapter._graphql_query = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                {
                    "data": {
                        "comment": {
                            "id": "comment-root",
                            "body": "root",
                            "createdAt": "2025-06-01T12:00:00.000Z",
                            "updatedAt": "2025-06-01T12:00:00.000Z",
                            "url": "https://linear.app/comment/comment-root",
                            "user": {"id": "u1", "displayName": "u", "name": "User"},
                            "issueId": "issue-123",
                        }
                    }
                },
                {
                    "data": {
                        "comment": {
                            "children": {
                                "nodes": [],
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                            }
                        }
                    }
                },
            ]
        )

        result = await adapter.fetch_messages(_COMMENT_THREAD)

        queries = [call[0][0] for call in adapter._graphql_query.call_args_list]
        assert all("comment(id: $commentId)" in query for query in queries)
        assert not any("agentSession" in query for query in queries)
        assert [m.id for m in result.messages] == ["comment-root"]
        # Comment path keeps the fixed thread_id.
        assert result.messages[0].thread_id == _COMMENT_THREAD


# ===========================================================================
# fetch_thread — agentSessionId metadata
# ===========================================================================


class TestFetchThreadAgentSessionId:
    @pytest.mark.asyncio
    async def test_metadata_includes_agent_session_id(self) -> None:
        adapter = _make_adapter()
        adapter._graphql_query = AsyncMock(  # type: ignore[method-assign]
            return_value={"data": {"issue": {"identifier": "ENG-1", "title": "Title", "url": "https://x"}}}
        )

        info = await adapter.fetch_thread(_SESSION_THREAD)

        assert info.metadata["agentSessionId"] == "session-789"
        assert info.metadata["issueId"] == "issue-123"

    @pytest.mark.asyncio
    async def test_metadata_agent_session_id_none_for_non_session(self) -> None:
        adapter = _make_adapter()
        adapter._graphql_query = AsyncMock(  # type: ignore[method-assign]
            return_value={"data": {"issue": {"identifier": "ENG-1", "title": "Title", "url": "https://x"}}}
        )

        info = await adapter.fetch_thread(_ISSUE_THREAD)

        assert info.metadata["agentSessionId"] is None
