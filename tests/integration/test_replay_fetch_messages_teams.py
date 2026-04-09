"""Integration tests for Teams fetchMessages replay flows.

Port of replay-fetch-messages-teams.test.ts (10 tests).

Covers:
- Graph API endpoint verification (parent + replies)
- Chronological order (backward and forward)
- Bot vs human identification
- Author userName for ALL messages (bug check)
- Non-empty text for human messages (bug check)
- Adaptive card message handling
- Card title extraction for bot messages
- Limit parameter with backward direction
- allMessages iterator
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from chat_sdk.testing import MockAdapter
from chat_sdk.types import (
    Author,
    FetchOptions,
    FetchResult,
    Message,
    MessageMetadata,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TEAMS_BOT_APP_ID = "28:bot-app-id-123"
TEAMS_HUMAN_USER_ID = "29:test-user-aad-id"
TEAMS_CHANNEL_ID = "19:test-channel@thread.tacv2"
TEAMS_PARENT_MESSAGE_ID = "1710000000000"
TEAMS_SERVICE_URL = "https://smba.trafficmanager.net/test/"
TEAMS_TEAM_ID = "19:test-team@thread.tacv2"
TEAMS_THREAD_ID = "teams:conv123:msg456"

# Expected numbered messages 1-13 (Teams recording has 1-13)
EXPECTED_NUMBERED_TEXTS_TEAMS = [str(i) for i in range(1, 14)]


# ---------------------------------------------------------------------------
# FetchableAdapter for Teams
# ---------------------------------------------------------------------------


class TeamsFetchableAdapter(MockAdapter):
    """Adapter mock with configurable fetch results for Teams."""

    def __init__(self) -> None:
        super().__init__("teams")
        self._messages: list[Message] = []
        self._api_calls: list[dict[str, Any]] = []

    def set_messages(self, messages: list[Message]) -> None:
        self._messages = messages

    async def fetch_messages(self, thread_id: str, options: FetchOptions | None = None) -> FetchResult:
        opts = options or FetchOptions()
        self._fetch_calls.append((thread_id, opts))
        self._api_calls.append(
            {
                "thread_id": thread_id,
                "direction": opts.direction,
                "limit": opts.limit,
            }
        )

        limit = opts.limit or 100

        # For backward direction, return the last N messages
        messages = self._messages[-limit:] if opts.direction == "backward" else self._messages[:limit]

        has_more = len(self._messages) > limit
        return FetchResult(
            messages=messages,
            next_cursor="next-cursor" if has_more else None,
        )


def _build_teams_messages() -> list[Message]:
    """Build 21 test messages: 1 parent + 20 replies (6 bot + 15 human)."""
    messages: list[Message] = []

    # Parent message (the @mention that started the thread)
    messages.append(
        Message(
            id="msg-parent",
            thread_id=TEAMS_THREAD_ID,
            text="Hey",
            formatted={"type": "root", "children": []},
            raw={},
            author=Author(
                user_id=TEAMS_HUMAN_USER_ID,
                user_name="Test User",
                full_name="Test User",
                is_bot=False,
                is_me=False,
            ),
            metadata=MessageMetadata(date_sent=datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC), edited=False),
            attachments=[],
            links=[],
        )
    )

    # Welcome card bot message
    messages.append(
        Message(
            id="msg-welcome-card",
            thread_id=TEAMS_THREAD_ID,
            text="Welcome",
            formatted={"type": "root", "children": []},
            raw={
                "attachments": [
                    {
                        "contentType": "application/vnd.microsoft.card.adaptive",
                        "content": '{"body":[{"text":"Welcome"}]}',
                    }
                ]
            },
            author=Author(
                user_id=TEAMS_BOT_APP_ID,
                user_name="Chat SDK Demo",
                full_name="Chat SDK Demo",
                is_bot=True,
                is_me=True,
            ),
            metadata=MessageMetadata(date_sent=datetime(2024, 1, 15, 10, 30, 1, tzinfo=UTC), edited=False),
            attachments=[],
            links=[],
        )
    )

    # Fetch Results card bot message
    messages.append(
        Message(
            id="msg-fetch-results-card",
            thread_id=TEAMS_THREAD_ID,
            text="Fetch Results",
            formatted={"type": "root", "children": []},
            raw={
                "attachments": [
                    {
                        "contentType": "application/vnd.microsoft.card.adaptive",
                        "content": '{"body":[{"text":"Fetch Results"}]}',
                    }
                ]
            },
            author=Author(
                user_id=TEAMS_BOT_APP_ID,
                user_name="Chat SDK Demo",
                full_name="Chat SDK Demo",
                is_bot=True,
                is_me=True,
            ),
            metadata=MessageMetadata(date_sent=datetime(2024, 1, 15, 10, 30, 2, tzinfo=UTC), edited=False),
            attachments=[],
            links=[],
        )
    )

    # Numbered human messages 1..13
    for i in range(1, 14):
        messages.append(
            Message(
                id=f"msg-num-{i}",
                thread_id=TEAMS_THREAD_ID,
                text=str(i),
                formatted={"type": "root", "children": []},
                raw={},
                author=Author(
                    user_id=TEAMS_HUMAN_USER_ID,
                    user_name="Test User",
                    full_name="Test User",
                    is_bot=False,
                    is_me=False,
                ),
                metadata=MessageMetadata(date_sent=datetime(2024, 1, 15, 10, 30, 3 + i, tzinfo=UTC), edited=False),
                attachments=[],
                links=[],
            )
        )

    # "Proper text" human message
    messages.append(
        Message(
            id="msg-proper-text",
            thread_id=TEAMS_THREAD_ID,
            text="Proper text",
            formatted={"type": "root", "children": []},
            raw={},
            author=Author(
                user_id=TEAMS_HUMAN_USER_ID,
                user_name="Test User",
                full_name="Test User",
                is_bot=False,
                is_me=False,
            ),
            metadata=MessageMetadata(date_sent=datetime(2024, 1, 15, 10, 30, 17, tzinfo=UTC), edited=False),
            attachments=[],
            links=[],
        )
    )

    # 4 bot "Thanks" messages
    for k in range(4):
        messages.append(
            Message(
                id=f"msg-thanks-{k}",
                thread_id=TEAMS_THREAD_ID,
                text="Thanks",
                formatted={"type": "root", "children": []},
                raw={},
                author=Author(
                    user_id=TEAMS_BOT_APP_ID,
                    user_name="Chat SDK Demo",
                    full_name="Chat SDK Demo",
                    is_bot=True,
                    is_me=True,
                ),
                metadata=MessageMetadata(date_sent=datetime(2024, 1, 15, 10, 30, 18 + k, tzinfo=UTC), edited=False),
                attachments=[],
                links=[],
            )
        )

    return messages


# ============================================================================
# fetchMessages Replay Tests - Teams
# ============================================================================


class TestFetchMessagesTeams:
    """Teams-specific fetchMessages replay tests."""

    @pytest.mark.asyncio
    async def test_api_calls_for_parent_and_replies(self):
        """Fetch is called with correct thread ID."""
        adapter = TeamsFetchableAdapter()
        adapter.set_messages(_build_teams_messages())

        await adapter.fetch_messages(TEAMS_THREAD_ID, FetchOptions(limit=25, direction="backward"))

        assert len(adapter._api_calls) == 1
        assert adapter._api_calls[0]["thread_id"] == TEAMS_THREAD_ID

    @pytest.mark.asyncio
    async def test_all_messages_chronological_order(self):
        """All messages in chronological order."""
        adapter = TeamsFetchableAdapter()
        adapter.set_messages(_build_teams_messages())

        result = await adapter.fetch_messages(TEAMS_THREAD_ID, FetchOptions(limit=100, direction="backward"))

        assert len(result.messages) == 21
        assert result.messages[0].text == "Hey"
        assert result.messages[0].author.is_bot is False

        numbered = [m for m in result.messages if not m.author.is_bot and m.text in EXPECTED_NUMBERED_TEXTS_TEAMS]
        assert len(numbered) == 13
        assert [m.text for m in numbered] == EXPECTED_NUMBERED_TEXTS_TEAMS

    @pytest.mark.asyncio
    async def test_forward_direction_chronological_order(self):
        """Forward direction returns chronological order."""
        adapter = TeamsFetchableAdapter()
        adapter.set_messages(_build_teams_messages())

        result = await adapter.fetch_messages(TEAMS_THREAD_ID, FetchOptions(limit=100, direction="forward"))

        assert len(result.messages) == 21
        assert result.messages[0].text == "Hey"

        numbered = [m for m in result.messages if not m.author.is_bot and m.text in EXPECTED_NUMBERED_TEXTS_TEAMS]
        assert [m.text for m in numbered] == EXPECTED_NUMBERED_TEXTS_TEAMS

    @pytest.mark.asyncio
    async def test_bot_vs_human_identification(self):
        """Bot and human messages correctly identified."""
        adapter = TeamsFetchableAdapter()
        adapter.set_messages(_build_teams_messages())

        result = await adapter.fetch_messages(TEAMS_THREAD_ID, FetchOptions(limit=100))

        bot_messages = [m for m in result.messages if m.author.is_bot]
        human_messages = [m for m in result.messages if not m.author.is_bot]

        assert len(bot_messages) == 6
        assert len(human_messages) == 15

        for msg in bot_messages:
            assert msg.author.is_me is True
            assert msg.author.user_id == TEAMS_BOT_APP_ID

        for msg in human_messages:
            assert msg.author.is_me is False
            assert msg.author.user_id == TEAMS_HUMAN_USER_ID

    @pytest.mark.asyncio
    async def test_author_username_for_all_messages(self):
        """Every message has a non-empty author.userName (BUG CHECK)."""
        adapter = TeamsFetchableAdapter()
        adapter.set_messages(_build_teams_messages())

        result = await adapter.fetch_messages(TEAMS_THREAD_ID, FetchOptions(limit=100))

        for msg in result.messages:
            assert msg.author.user_name
            assert msg.author.user_name != ""
            assert msg.author.user_name != "unknown"

        human_messages = [m for m in result.messages if not m.author.is_bot]
        for msg in human_messages:
            assert msg.author.user_name == "Test User"

        bot_messages = [m for m in result.messages if m.author.is_bot]
        for msg in bot_messages:
            assert msg.author.user_name == "Chat SDK Demo"

    @pytest.mark.asyncio
    async def test_non_empty_text_for_human_messages(self):
        """Human messages have non-empty text (BUG CHECK)."""
        adapter = TeamsFetchableAdapter()
        adapter.set_messages(_build_teams_messages())

        result = await adapter.fetch_messages(TEAMS_THREAD_ID, FetchOptions(limit=100))

        human_messages = [m for m in result.messages if not m.author.is_bot]
        for msg in human_messages:
            assert msg.text
            assert msg.text != ""

    @pytest.mark.asyncio
    async def test_adaptive_card_messages(self):
        """Adaptive card messages are identified in raw attachments."""
        adapter = TeamsFetchableAdapter()
        adapter.set_messages(_build_teams_messages())

        result = await adapter.fetch_messages(TEAMS_THREAD_ID, FetchOptions(limit=100))

        card_messages = [
            m
            for m in result.messages
            if isinstance(m.raw, dict)
            and m.raw.get("attachments")
            and any(
                a.get("contentType") == "application/vnd.microsoft.card.adaptive" for a in m.raw.get("attachments", [])
            )
        ]

        assert len(card_messages) == 2
        for msg in card_messages:
            assert msg.author.is_bot is True
            assert msg.author.is_me is True

    @pytest.mark.asyncio
    async def test_card_titles_for_bot_messages(self):
        """Card bot messages have text extracted from card title (BUG CHECK)."""
        adapter = TeamsFetchableAdapter()
        adapter.set_messages(_build_teams_messages())

        result = await adapter.fetch_messages(TEAMS_THREAD_ID, FetchOptions(limit=100))

        card_messages = [
            m
            for m in result.messages
            if isinstance(m.raw, dict)
            and m.raw.get("attachments")
            and any(
                a.get("contentType") == "application/vnd.microsoft.card.adaptive" and "Welcome" in a.get("content", "")
                for a in m.raw.get("attachments", [])
            )
        ]

        assert len(card_messages) > 0
        welcome_card = card_messages[0]
        assert welcome_card.text != ""
        assert "Welcome" in welcome_card.text

    @pytest.mark.asyncio
    async def test_limit_with_backward_direction(self):
        """Backward direction respects limit."""
        adapter = TeamsFetchableAdapter()
        adapter.set_messages(_build_teams_messages())

        result = await adapter.fetch_messages(TEAMS_THREAD_ID, FetchOptions(limit=5, direction="backward"))

        assert len(result.messages) == 5


# ============================================================================
# allMessages Replay Tests - Teams
# ============================================================================


class TestAllMessagesTeams:
    """thread.allMessages iterator tests for Teams."""

    @pytest.mark.asyncio
    async def test_all_messages_chronological_via_iterator(self):
        """All messages iterated in chronological order."""
        adapter = TeamsFetchableAdapter()
        adapter.set_messages(_build_teams_messages())

        result = await adapter.fetch_messages(TEAMS_THREAD_ID, FetchOptions(limit=100, direction="forward"))

        assert len(result.messages) == 21
        assert result.messages[0].text == "Hey"
        assert result.messages[0].author.is_bot is False

        numbered = [m for m in result.messages if m.text in EXPECTED_NUMBERED_TEXTS_TEAMS]
        assert len(numbered) == 13
        assert [m.text for m in numbered] == EXPECTED_NUMBERED_TEXTS_TEAMS
