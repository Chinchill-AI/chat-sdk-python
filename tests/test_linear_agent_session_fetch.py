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
- ``comments(filter: CommentFilter, first/last/after): CommentConnection!`` with
  the ``{parent: {id: {eq: root_comment.id}}}`` filter — ``forward`` paginates
  with ``first``, every other direction with ``last``.

Each test pins behaviour so a regression — a forward/backward (first↔last) swap,
a per-comment-id → fixed-thread-id collapse, a nullish (``??``) → ``or`` swap, a
missing append-only guard, or a ``hasNextPage`` cursor-logic flip — fails the
assertion. The append-only edit/delete guards (index.ts:1408 / 1464) are
covered here too.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from chat_sdk.adapters.linear.adapter import LinearAdapter
from chat_sdk.adapters.linear.types import LinearAdapterAPIKeyConfig, LinearAgentSessionThreadId
from chat_sdk.shared.errors import AdapterError
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
    ``issue_id=None`` models a session whose issue relation is absent (so the
    ``thread.issue_id`` fallback / missing-issueId raise is exercised).
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

        # PER-COMMENT thread_id proof: each message encodes its OWN comment id
        # plus the session segment — NOT a single fixed thread_id shared by all.
        # A regression that passed one fixed thread_id (e.g. the root's) to every
        # message would collapse these to identical strings.
        assert [m.thread_id for m in result.messages] == [
            "linear:issue-123:c:comment-root:s:session-789",
            "linear:issue-123:c:comment-a:s:session-789",
            "linear:issue-123:c:comment-b:s:session-789",
        ]
        assert len({m.thread_id for m in result.messages}) == 3

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
# _fetch_agent_session_messages — issueId fallback + raises
# ===========================================================================


class TestFetchAgentSessionMessagesIssueId:
    @pytest.mark.asyncio
    async def test_falls_back_to_thread_issue_id_when_session_issue_absent(self) -> None:
        adapter = _make_adapter()
        # Session has no ``issue`` relation; the thread's own issue id is used.
        adapter._graphql_query = _query_router(  # type: ignore[method-assign]
            _session_return(issue_id=None),
            _children_return(),
        )

        result = await adapter.fetch_messages(_SESSION_THREAD)

        # The root message's thread id is built from the THREAD's issue id
        # (issue-123) — proving the ``agentSession.issue.id ?? thread.issue_id``
        # fallback fired.
        assert result.messages[0].thread_id == "linear:issue-123:c:comment-root:s:session-789"

    @pytest.mark.asyncio
    async def test_raises_when_issue_id_missing_everywhere(self) -> None:
        adapter = _make_adapter()
        # Thread carries no issue id AND the session has no issue relation, so
        # the ``agentSession.issue.id ?? thread.issue_id`` fallback yields
        # nothing. (Called directly: a thread id can't encode an empty issue id,
        # so this guard is reached via a degenerate decoded thread.)
        adapter._graphql_query = AsyncMock(return_value=_session_return(issue_id=None))  # type: ignore[method-assign]
        thread = LinearAgentSessionThreadId(issue_id="", agent_session_id="session-789")

        with pytest.raises(AdapterError) as exc_info:
            await adapter._fetch_agent_session_messages(thread)
        assert "missing issueId" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_raises_when_root_comment_missing(self) -> None:
        adapter = _make_adapter()
        adapter._graphql_query = AsyncMock(return_value=_session_return(root_comment=None))

        with pytest.raises(AdapterError) as exc_info:
            await adapter.fetch_messages(_SESSION_THREAD)
        assert "missing a root comment" in str(exc_info.value)


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
    async def test_cursor_passed_as_after(self) -> None:
        adapter = _make_adapter()
        adapter._graphql_query = _query_router(  # type: ignore[method-assign]
            _session_return(),
            _children_return(),
        )

        await adapter.fetch_messages(_SESSION_THREAD, FetchOptions(cursor="cursor-xyz"))

        _, children_vars = adapter._graphql_query.call_args_list[1][0]
        assert children_vars["after"] == "cursor-xyz"


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
    async def test_next_cursor_absent_when_no_next_page(self) -> None:
        adapter = _make_adapter()
        # endCursor is present, but hasNextPage is False → next_cursor is None.
        # A regression that returned endCursor regardless of hasNextPage would
        # leak ``cursor-stale`` here.
        adapter._graphql_query = _query_router(  # type: ignore[method-assign]
            _session_return(),
            _children_return(has_next_page=False, end_cursor="cursor-stale"),
        )

        result = await adapter.fetch_messages(_SESSION_THREAD)

        assert result.next_cursor is None


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
        """A ``:c:`` thread still routes to the comment-thread fetch unchanged."""
        adapter = _make_adapter()
        adapter._graphql_query = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "data": {
                    "comment": {
                        "id": "comment-root",
                        "body": "root",
                        "createdAt": "2025-06-01T12:00:00.000Z",
                        "updatedAt": "2025-06-01T12:00:00.000Z",
                        "url": "https://linear.app/comment/comment-root",
                        "user": {"id": "u1", "displayName": "u", "name": "User"},
                        "children": {
                            "nodes": [],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        },
                    }
                }
            }
        )

        result = await adapter.fetch_messages(_COMMENT_THREAD)

        query = adapter._graphql_query.call_args[0][0]
        assert "comment(id: $commentId)" in query
        assert "agentSession" not in query
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
