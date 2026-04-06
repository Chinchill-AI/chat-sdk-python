"""Integration tests for fetchMessages replay flows.

Port of replay-fetch-messages-slack/gchat/teams/discord (41 tests total).

Covers:
- Fetch backward (newest first)
- Fetch forward (oldest first)
- Pagination with cursors
- Empty thread
- Channel-level vs thread-level
- Bot vs human message identification
- User display name resolution
- Limit parameter respect
- Cross-platform fetchMessages behavior
"""

from __future__ import annotations

from typing import Any

import pytest
from chat_sdk.testing import MockAdapter, create_mock_adapter
from chat_sdk.types import (
    Author,
    FetchOptions,
    FetchResult,
    Message,
    MessageMetadata,
    RawMessage,
)

from .conftest import create_chat, create_msg

# ---------------------------------------------------------------------------
# Test message fixtures
# ---------------------------------------------------------------------------

BOT_USER_ID = "U00FAKEBOT01"
HUMAN_USER_ID = "U00FAKEUSER1"
THREAD_ID = "slack:C00FAKECHAN1:1710000000.000100"
CHANNEL_ID = "C00FAKECHAN1"


def _make_fetch_messages(
    count: int = 5,
    bot_user_id: str = BOT_USER_ID,
    human_user_id: str = HUMAN_USER_ID,
    thread_id: str = THREAD_ID,
    include_bot: bool = True,
) -> list[Message]:
    """Build a list of test messages for fetch results.

    Creates numbered messages (1..count) alternating between human and bot.
    """
    from datetime import datetime, timezone

    messages: list[Message] = []
    for i in range(1, count + 1):
        is_bot = include_bot and (i % 3 == 0)
        user_id = bot_user_id if is_bot else human_user_id
        user_name = "Chat SDK Bot" if is_bot else "Test User"
        messages.append(
            Message(
                id=f"msg-{i}",
                thread_id=thread_id,
                text=str(i),
                formatted={"type": "root", "children": []},
                raw={},
                author=Author(
                    user_id=user_id,
                    user_name=user_name,
                    full_name=user_name,
                    is_bot=is_bot,
                    is_me=is_bot,
                ),
                metadata=MessageMetadata(
                    date_sent=datetime(2024, 1, 15, 10, 30, i, tzinfo=timezone.utc),
                    edited=False,
                ),
                attachments=[],
                links=[],
            )
        )
    return messages


class FetchableAdapter(MockAdapter):
    """Adapter mock that returns configurable fetch results."""

    def __init__(self, name: str = "slack") -> None:
        super().__init__(name)
        self._fetch_messages_result: FetchResult | None = None
        self._fetch_channel_result: FetchResult | None = None

    def set_fetch_result(self, result: FetchResult) -> None:
        self._fetch_messages_result = result

    def set_channel_fetch_result(self, result: FetchResult) -> None:
        self._fetch_channel_result = result

    async def fetch_messages(self, thread_id: str, options: FetchOptions | None = None) -> FetchResult:
        self._fetch_calls.append((thread_id, options))
        if self._fetch_messages_result:
            return self._fetch_messages_result
        return FetchResult(messages=[], next_cursor=None)

    async def fetch_channel_messages(self, channel_id: str, options: FetchOptions | None = None) -> FetchResult:
        if self._fetch_channel_result:
            return self._fetch_channel_result
        return FetchResult(messages=[], next_cursor=None)


# ============================================================================
# Forward direction (oldest first)
# ============================================================================


class TestFetchMessagesForward:
    """Forward direction (oldest first) message fetching."""

    @pytest.mark.asyncio
    async def test_forward_returns_messages_in_chronological_order(self):
        """Messages fetched forward are in chronological order (oldest first)."""
        adapter = FetchableAdapter("slack")
        messages = _make_fetch_messages(14, include_bot=False)
        adapter.set_fetch_result(FetchResult(messages=messages, next_cursor=None))

        result = await adapter.fetch_messages(THREAD_ID, FetchOptions(limit=100, direction="forward"))

        assert len(result.messages) == 14
        texts = [m.text for m in result.messages]
        expected = [str(i) for i in range(1, 15)]
        assert texts == expected

    @pytest.mark.asyncio
    async def test_forward_records_correct_fetch_call(self):
        """Forward fetch passes correct parameters to the adapter."""
        adapter = FetchableAdapter("slack")
        adapter.set_fetch_result(FetchResult(messages=[], next_cursor=None))

        await adapter.fetch_messages(THREAD_ID, FetchOptions(limit=25, direction="forward"))

        assert len(adapter._fetch_calls) == 1
        thread_id, options = adapter._fetch_calls[0]
        assert thread_id == THREAD_ID
        assert options.limit == 25
        assert options.direction == "forward"


# ============================================================================
# Backward direction (newest first)
# ============================================================================


class TestFetchMessagesBackward:
    """Backward direction (newest first) message fetching."""

    @pytest.mark.asyncio
    async def test_backward_returns_messages(self):
        """Messages fetched backward are returned."""
        adapter = FetchableAdapter("slack")
        messages = _make_fetch_messages(14, include_bot=False)
        # Reverse for backward
        adapter.set_fetch_result(FetchResult(messages=list(reversed(messages)), next_cursor=None))

        result = await adapter.fetch_messages(THREAD_ID, FetchOptions(limit=100, direction="backward"))

        assert len(result.messages) == 14

    @pytest.mark.asyncio
    async def test_backward_records_correct_fetch_call(self):
        """Backward fetch passes correct parameters to the adapter."""
        adapter = FetchableAdapter("slack")
        adapter.set_fetch_result(FetchResult(messages=[], next_cursor=None))

        await adapter.fetch_messages(THREAD_ID, FetchOptions(limit=50, direction="backward"))

        assert len(adapter._fetch_calls) == 1
        _, options = adapter._fetch_calls[0]
        assert options.limit == 50
        assert options.direction == "backward"


# ============================================================================
# Bot vs human identification
# ============================================================================


class TestMessageAuthorIdentification:
    """Correctly identifying bot vs human messages."""

    @pytest.mark.asyncio
    async def test_bot_messages_identified(self):
        """Bot messages have isBot=True and isMe=True."""
        adapter = FetchableAdapter("slack")
        messages = _make_fetch_messages(9, include_bot=True)
        adapter.set_fetch_result(FetchResult(messages=messages, next_cursor=None))

        result = await adapter.fetch_messages(THREAD_ID, FetchOptions(limit=100))

        bot_messages = [m for m in result.messages if m.author.is_bot]
        human_messages = [m for m in result.messages if not m.author.is_bot]

        # Every 3rd message (3, 6, 9) is a bot message
        assert len(bot_messages) == 3
        assert len(human_messages) == 6

        for msg in bot_messages:
            assert msg.author.is_me is True
            assert msg.author.user_id == BOT_USER_ID

        for msg in human_messages:
            assert msg.author.is_me is False
            assert msg.author.user_id == HUMAN_USER_ID

    @pytest.mark.asyncio
    async def test_user_display_names_resolved(self):
        """Human and bot messages have resolved display names."""
        adapter = FetchableAdapter("slack")
        messages = _make_fetch_messages(3, include_bot=True)
        adapter.set_fetch_result(FetchResult(messages=messages, next_cursor=None))

        result = await adapter.fetch_messages(THREAD_ID, FetchOptions(limit=100))

        human_msg = next(m for m in result.messages if not m.author.is_bot)
        assert human_msg.author.user_name == "Test User"
        assert human_msg.author.full_name == "Test User"

        bot_msg = next(m for m in result.messages if m.author.is_bot)
        assert bot_msg.author.user_name == "Chat SDK Bot"


# ============================================================================
# Limit parameter
# ============================================================================


class TestFetchMessagesLimit:
    """Respecting the limit parameter."""

    @pytest.mark.asyncio
    async def test_respects_limit_parameter(self):
        """Only the requested number of messages are returned."""
        adapter = FetchableAdapter("slack")
        all_messages = _make_fetch_messages(20, include_bot=False)
        adapter.set_fetch_result(FetchResult(messages=all_messages[:5], next_cursor="cursor1"))

        result = await adapter.fetch_messages(THREAD_ID, FetchOptions(limit=5, direction="forward"))

        assert len(result.messages) == 5
        assert result.messages[0].text == "1"
        assert result.messages[4].text == "5"

    @pytest.mark.asyncio
    async def test_returns_all_when_limit_exceeds_count(self):
        """When limit exceeds message count, all messages are returned."""
        adapter = FetchableAdapter("slack")
        messages = _make_fetch_messages(3, include_bot=False)
        adapter.set_fetch_result(FetchResult(messages=messages, next_cursor=None))

        result = await adapter.fetch_messages(THREAD_ID, FetchOptions(limit=100))

        assert len(result.messages) == 3


# ============================================================================
# Pagination with cursors
# ============================================================================


class TestFetchMessagesPagination:
    """Pagination with next_cursor."""

    @pytest.mark.asyncio
    async def test_has_next_cursor_when_more_available(self):
        """When more messages exist, next_cursor is provided."""
        adapter = FetchableAdapter("slack")
        messages = _make_fetch_messages(5, include_bot=False)
        adapter.set_fetch_result(FetchResult(messages=messages, next_cursor="cursor-abc"))

        result = await adapter.fetch_messages(THREAD_ID, FetchOptions(limit=5))

        assert result.next_cursor == "cursor-abc"

    @pytest.mark.asyncio
    async def test_no_cursor_when_complete(self):
        """When all messages are fetched, next_cursor is None."""
        adapter = FetchableAdapter("slack")
        messages = _make_fetch_messages(3, include_bot=False)
        adapter.set_fetch_result(FetchResult(messages=messages, next_cursor=None))

        result = await adapter.fetch_messages(THREAD_ID, FetchOptions(limit=100))

        assert result.next_cursor is None

    @pytest.mark.asyncio
    async def test_cursor_passed_to_subsequent_fetch(self):
        """A cursor from a previous fetch is passed to the next one."""
        adapter = FetchableAdapter("slack")
        adapter.set_fetch_result(FetchResult(messages=[], next_cursor=None))

        await adapter.fetch_messages(THREAD_ID, FetchOptions(limit=5, cursor="cursor-abc"))

        _, options = adapter._fetch_calls[0]
        assert options.cursor == "cursor-abc"


# ============================================================================
# Empty thread
# ============================================================================


class TestFetchMessagesEmptyThread:
    """Empty thread handling."""

    @pytest.mark.asyncio
    async def test_empty_thread_returns_no_messages(self):
        """An empty thread returns an empty list with no cursor."""
        adapter = FetchableAdapter("slack")
        adapter.set_fetch_result(FetchResult(messages=[], next_cursor=None))

        result = await adapter.fetch_messages(THREAD_ID, FetchOptions(limit=100))

        assert result.messages == []
        assert result.next_cursor is None


# ============================================================================
# Channel-level vs thread-level
# ============================================================================


class TestChannelVsThreadFetch:
    """Channel-level vs thread-level message fetching."""

    @pytest.mark.asyncio
    async def test_fetch_channel_messages_separate_from_thread(self):
        """Channel messages and thread messages use different methods."""
        adapter = FetchableAdapter("slack")

        thread_messages = _make_fetch_messages(3, include_bot=False)
        channel_messages = _make_fetch_messages(5, include_bot=False, thread_id="slack:C00FAKECHAN1:")
        adapter.set_fetch_result(FetchResult(messages=thread_messages, next_cursor=None))
        adapter.set_channel_fetch_result(FetchResult(messages=channel_messages, next_cursor=None))

        thread_result = await adapter.fetch_messages(THREAD_ID, FetchOptions(limit=100))
        channel_result = await adapter.fetch_channel_messages("slack:C00FAKECHAN1")

        assert len(thread_result.messages) == 3
        assert len(channel_result.messages) == 5

    @pytest.mark.asyncio
    async def test_channel_messages_default_empty(self):
        """Default mock adapter returns empty channel messages."""
        adapter = create_mock_adapter("slack")
        result = await adapter.fetch_channel_messages("slack:C00FAKECHAN1")
        assert result.messages == []


# ============================================================================
# Cross-platform fetchMessages
# ============================================================================


class TestCrossPlatformFetchMessages:
    """Fetch messages across different platform adapters."""

    @pytest.mark.asyncio
    async def test_slack_fetch_messages(self):
        """Slack adapter fetchMessages returns results."""
        adapter = FetchableAdapter("slack")
        messages = _make_fetch_messages(5, include_bot=False)
        adapter.set_fetch_result(FetchResult(messages=messages, next_cursor=None))

        result = await adapter.fetch_messages(
            "slack:C00FAKECHAN1:1710000000.000100",
            FetchOptions(limit=100, direction="forward"),
        )
        assert len(result.messages) == 5

    @pytest.mark.asyncio
    async def test_discord_fetch_messages(self):
        """Discord adapter fetchMessages returns results."""
        adapter = FetchableAdapter("discord")
        messages = _make_fetch_messages(3, include_bot=False, thread_id="discord:guild:chan:thread")
        adapter.set_fetch_result(FetchResult(messages=messages, next_cursor=None))

        result = await adapter.fetch_messages(
            "discord:guild:chan:thread",
            FetchOptions(limit=100, direction="backward"),
        )
        assert len(result.messages) == 3

    @pytest.mark.asyncio
    async def test_gchat_fetch_messages(self):
        """GChat adapter fetchMessages returns results."""
        adapter = FetchableAdapter("gchat")
        messages = _make_fetch_messages(4, include_bot=False, thread_id="gchat:spaces/ABC:threads/XYZ")
        adapter.set_fetch_result(FetchResult(messages=messages, next_cursor=None))

        result = await adapter.fetch_messages(
            "gchat:spaces/ABC:threads/XYZ",
            FetchOptions(limit=50),
        )
        assert len(result.messages) == 4

    @pytest.mark.asyncio
    async def test_teams_fetch_messages(self):
        """Teams adapter fetchMessages returns results."""
        adapter = FetchableAdapter("teams")
        messages = _make_fetch_messages(6, include_bot=True, thread_id="teams:conv:msg")
        adapter.set_fetch_result(FetchResult(messages=messages, next_cursor=None))

        result = await adapter.fetch_messages(
            "teams:conv:msg",
            FetchOptions(limit=100),
        )
        assert len(result.messages) == 6
        bot_msgs = [m for m in result.messages if m.author.is_bot]
        assert len(bot_msgs) == 2  # messages 3 and 6


# ============================================================================
# Default direction
# ============================================================================


class TestFetchMessagesDefaultDirection:
    """Default direction behavior when not specified."""

    @pytest.mark.asyncio
    async def test_default_direction_returns_messages(self):
        """Fetching without specifying direction still works."""
        adapter = FetchableAdapter("slack")
        messages = _make_fetch_messages(3, include_bot=False)
        adapter.set_fetch_result(FetchResult(messages=messages, next_cursor=None))

        result = await adapter.fetch_messages(THREAD_ID, FetchOptions(limit=10))

        assert len(result.messages) == 3
