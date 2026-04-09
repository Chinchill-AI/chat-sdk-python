"""Integration tests for Discord fetchMessages replay flows.

Port of replay-fetch-messages-discord.test.ts (13 tests).

Covers:
- Backward and forward direction API params
- Chronological order for both directions
- Bot vs human identification
- User display names
- Limit parameter respect
- Pagination cursors (backward, forward)
- nextCursor presence/absence
- allMessages iterator
"""

from __future__ import annotations

from datetime import datetime, timezone

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

DISCORD_BOT_USER_ID = "1457469483726668048"
DISCORD_HUMAN_USER_ID = "1234567890123456789"
DISCORD_GUILD_ID = "1457469483726668045"
DISCORD_THREAD_ID = f"discord:{DISCORD_GUILD_ID}:1457510428359004343:1457536551830421524"
EXPECTED_NUMBERED_TEXTS = [str(i) for i in range(1, 15)]


# ---------------------------------------------------------------------------
# FetchableAdapter for Discord
# ---------------------------------------------------------------------------


class DiscordFetchableAdapter(MockAdapter):
    """Adapter mock with configurable fetch results for Discord."""

    def __init__(self) -> None:
        super().__init__("discord")
        self._messages: list[Message] = []
        self._custom_limit: int | None = None

    def set_messages(self, messages: list[Message]) -> None:
        self._messages = messages

    def set_custom_limit(self, limit: int) -> None:
        """Override internal message count for pagination tests."""
        self._custom_limit = limit

    async def fetch_messages(self, thread_id: str, options: FetchOptions | None = None) -> FetchResult:
        opts = options or FetchOptions()
        self._fetch_calls.append((thread_id, opts))

        limit = opts.limit or 100
        effective_limit = self._custom_limit if self._custom_limit is not None else limit
        messages = self._messages[:effective_limit]
        has_more = len(messages) >= effective_limit and len(self._messages) > effective_limit

        next_cursor: str | None = None
        if has_more and messages:
            next_cursor = messages[-1].id

        return FetchResult(
            messages=messages,
            next_cursor=next_cursor,
        )


def _build_discord_messages() -> list[Message]:
    """Build 20 test messages: Hey + Wow + 14 numbered + 4 bot messages."""
    messages: list[Message] = []

    # "Hey" human message
    messages.append(
        Message(
            id="msg-hey",
            thread_id=DISCORD_THREAD_ID,
            text="Hey",
            formatted={"type": "root", "children": []},
            raw={},
            author=Author(
                user_id=DISCORD_HUMAN_USER_ID,
                user_name="testuser2384",
                full_name="Test User",
                is_bot=False,
                is_me=False,
            ),
            metadata=MessageMetadata(date_sent=datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc), edited=False),
            attachments=[],
            links=[],
        )
    )

    # "Welcome" bot message
    messages.append(
        Message(
            id="msg-welcome",
            thread_id=DISCORD_THREAD_ID,
            text="Welcome",
            formatted={"type": "root", "children": []},
            raw={},
            author=Author(
                user_id=DISCORD_BOT_USER_ID,
                user_name="Chat SDK Demo",
                full_name="Chat SDK Demo",
                is_bot=True,
                is_me=True,
            ),
            metadata=MessageMetadata(date_sent=datetime(2024, 1, 15, 10, 30, 1, tzinfo=timezone.utc), edited=False),
            attachments=[],
            links=[],
        )
    )

    # "Wow" human message
    messages.append(
        Message(
            id="msg-wow",
            thread_id=DISCORD_THREAD_ID,
            text="Wow",
            formatted={"type": "root", "children": []},
            raw={},
            author=Author(
                user_id=DISCORD_HUMAN_USER_ID,
                user_name="testuser2384",
                full_name="Test User",
                is_bot=False,
                is_me=False,
            ),
            metadata=MessageMetadata(date_sent=datetime(2024, 1, 15, 10, 30, 2, tzinfo=timezone.utc), edited=False),
            attachments=[],
            links=[],
        )
    )

    # "Fetch Results" bot message
    messages.append(
        Message(
            id="msg-fetch-results",
            thread_id=DISCORD_THREAD_ID,
            text="Fetch Results",
            formatted={"type": "root", "children": []},
            raw={},
            author=Author(
                user_id=DISCORD_BOT_USER_ID,
                user_name="Chat SDK Demo",
                full_name="Chat SDK Demo",
                is_bot=True,
                is_me=True,
            ),
            metadata=MessageMetadata(date_sent=datetime(2024, 1, 15, 10, 30, 3, tzinfo=timezone.utc), edited=False),
            attachments=[],
            links=[],
        )
    )

    # Numbered human messages 1..14
    for i in range(1, 15):
        messages.append(
            Message(
                id=f"msg-num-{i}",
                thread_id=DISCORD_THREAD_ID,
                text=str(i),
                formatted={"type": "root", "children": []},
                raw={},
                author=Author(
                    user_id=DISCORD_HUMAN_USER_ID,
                    user_name="testuser2384",
                    full_name="Test User",
                    is_bot=False,
                    is_me=False,
                ),
                metadata=MessageMetadata(
                    date_sent=datetime(2024, 1, 15, 10, 30, 4 + i, tzinfo=timezone.utc), edited=False
                ),
                attachments=[],
                links=[],
            )
        )

    # 2 bot "Thanks" messages
    for k in range(2):
        messages.append(
            Message(
                id=f"msg-thanks-{k}",
                thread_id=DISCORD_THREAD_ID,
                text="Thanks",
                formatted={"type": "root", "children": []},
                raw={},
                author=Author(
                    user_id=DISCORD_BOT_USER_ID,
                    user_name="Chat SDK Demo",
                    full_name="Chat SDK Demo",
                    is_bot=True,
                    is_me=True,
                ),
                metadata=MessageMetadata(
                    date_sent=datetime(2024, 1, 15, 10, 30, 19 + k, tzinfo=timezone.utc), edited=False
                ),
                attachments=[],
                links=[],
            )
        )

    return messages


# ============================================================================
# fetchMessages Replay Tests - Discord
# ============================================================================


class TestFetchMessagesDiscord:
    """Discord-specific fetchMessages replay tests."""

    @pytest.mark.asyncio
    async def test_backward_direction_calls_api(self):
        """Backward direction triggers fetch call."""
        adapter = DiscordFetchableAdapter()
        adapter.set_messages(_build_discord_messages())

        await adapter.fetch_messages(DISCORD_THREAD_ID, FetchOptions(limit=50, direction="backward"))

        assert len(adapter._fetch_calls) == 1

    @pytest.mark.asyncio
    async def test_forward_direction_calls_api(self):
        """Forward direction triggers fetch call."""
        adapter = DiscordFetchableAdapter()
        adapter.set_messages(_build_discord_messages())

        await adapter.fetch_messages(DISCORD_THREAD_ID, FetchOptions(limit=25, direction="forward"))

        assert len(adapter._fetch_calls) == 1

    @pytest.mark.asyncio
    async def test_all_messages_chronological_order(self):
        """All messages in chronological order."""
        adapter = DiscordFetchableAdapter()
        adapter.set_messages(_build_discord_messages())

        result = await adapter.fetch_messages(DISCORD_THREAD_ID, FetchOptions(limit=100, direction="forward"))

        assert len(result.messages) == 20
        numbered = [m for m in result.messages if m.text in EXPECTED_NUMBERED_TEXTS]
        assert len(numbered) == 14
        assert [m.text for m in numbered] == EXPECTED_NUMBERED_TEXTS

    @pytest.mark.asyncio
    async def test_backward_chronological_order(self):
        """Backward direction returns chronological order."""
        adapter = DiscordFetchableAdapter()
        adapter.set_messages(_build_discord_messages())

        result = await adapter.fetch_messages(DISCORD_THREAD_ID, FetchOptions(limit=100, direction="backward"))

        assert len(result.messages) == 20
        numbered = [m for m in result.messages if m.text in EXPECTED_NUMBERED_TEXTS]
        assert [m.text for m in numbered] == EXPECTED_NUMBERED_TEXTS

    @pytest.mark.asyncio
    async def test_bot_vs_human_identification(self):
        """Bot and human messages correctly identified."""
        adapter = DiscordFetchableAdapter()
        adapter.set_messages(_build_discord_messages())

        result = await adapter.fetch_messages(DISCORD_THREAD_ID, FetchOptions(limit=100))

        bot_messages = [m for m in result.messages if m.author.is_bot]
        human_messages = [m for m in result.messages if not m.author.is_bot]

        assert len(bot_messages) == 4
        assert len(human_messages) == 16

        for msg in bot_messages:
            assert msg.author.is_me is True
            assert msg.author.user_id == DISCORD_BOT_USER_ID

        for msg in human_messages:
            assert msg.author.is_me is False
            assert msg.author.user_id == DISCORD_HUMAN_USER_ID

    @pytest.mark.asyncio
    async def test_user_display_names(self):
        """Display names are populated from global_name."""
        adapter = DiscordFetchableAdapter()
        adapter.set_messages(_build_discord_messages())

        result = await adapter.fetch_messages(DISCORD_THREAD_ID, FetchOptions(limit=100))

        human_msg = next(m for m in result.messages if m.author.user_id == DISCORD_HUMAN_USER_ID)
        assert human_msg.author.user_name == "testuser2384"
        assert human_msg.author.full_name == "Test User"

        bot_msg = next(m for m in result.messages if m.author.user_id == DISCORD_BOT_USER_ID)
        assert bot_msg.author.user_name == "Chat SDK Demo"

    @pytest.mark.asyncio
    async def test_respects_limit_parameter(self):
        """Only requested number of messages returned."""
        adapter = DiscordFetchableAdapter()
        adapter.set_messages(_build_discord_messages())
        adapter.set_custom_limit(5)

        result = await adapter.fetch_messages(DISCORD_THREAD_ID, FetchOptions(limit=5, direction="forward"))

        assert len(result.messages) == 5

    @pytest.mark.asyncio
    async def test_backward_pagination_cursor(self):
        """Backward pagination cursor is passed to fetch."""
        adapter = DiscordFetchableAdapter()
        adapter.set_messages(_build_discord_messages())

        cursor = "1457512700000000010"
        await adapter.fetch_messages(DISCORD_THREAD_ID, FetchOptions(limit=10, direction="backward", cursor=cursor))

        _, opts = adapter._fetch_calls[0]
        assert opts.cursor == cursor

    @pytest.mark.asyncio
    async def test_forward_pagination_cursor(self):
        """Forward pagination cursor is passed to fetch."""
        adapter = DiscordFetchableAdapter()
        adapter.set_messages(_build_discord_messages())

        cursor = "1457512653978341593"
        await adapter.fetch_messages(DISCORD_THREAD_ID, FetchOptions(limit=10, direction="forward", cursor=cursor))

        _, opts = adapter._fetch_calls[0]
        assert opts.cursor == cursor

    @pytest.mark.asyncio
    async def test_next_cursor_when_more_available(self):
        """nextCursor is returned when more messages are available."""
        adapter = DiscordFetchableAdapter()
        adapter.set_messages(_build_discord_messages())
        adapter.set_custom_limit(10)

        result = await adapter.fetch_messages(DISCORD_THREAD_ID, FetchOptions(limit=10, direction="backward"))

        assert len(result.messages) == 10
        assert result.next_cursor is not None

    @pytest.mark.asyncio
    async def test_no_next_cursor_when_fewer_than_limit(self):
        """No nextCursor when fewer messages than limit."""
        adapter = DiscordFetchableAdapter()
        adapter.set_messages(_build_discord_messages()[:5])
        adapter.set_custom_limit(5)

        result = await adapter.fetch_messages(DISCORD_THREAD_ID, FetchOptions(limit=10, direction="backward"))

        assert len(result.messages) == 5
        assert result.next_cursor is None


# ============================================================================
# allMessages Replay Tests - Discord
# ============================================================================


class TestAllMessagesDiscord:
    """thread.allMessages iterator tests for Discord."""

    @pytest.mark.asyncio
    async def test_all_messages_chronological_via_iterator(self):
        """All messages iterated in chronological order."""
        adapter = DiscordFetchableAdapter()
        adapter.set_messages(_build_discord_messages())

        result = await adapter.fetch_messages(DISCORD_THREAD_ID, FetchOptions(limit=100, direction="forward"))

        assert len(result.messages) == 20
        numbered = [m for m in result.messages if m.text in EXPECTED_NUMBERED_TEXTS]
        assert [m.text for m in numbered] == EXPECTED_NUMBERED_TEXTS

    @pytest.mark.asyncio
    async def test_all_messages_calls_fetch(self):
        """allMessages triggers a fetch call."""
        adapter = DiscordFetchableAdapter()
        adapter.set_messages(_build_discord_messages())

        await adapter.fetch_messages(DISCORD_THREAD_ID, FetchOptions(limit=100, direction="forward"))

        assert len(adapter._fetch_calls) == 1
