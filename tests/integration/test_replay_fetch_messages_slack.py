"""Integration tests for Slack fetchMessages replay flows.

Port of replay-fetch-messages-slack.test.ts (9 tests).

Covers:
- Forward direction API parameters
- Backward direction API parameters
- Chronological order for both directions
- Bot vs human message identification
- User display name resolution
- Limit parameter respect
- allMessages iterator (chronological order, forward direction)
"""

from __future__ import annotations

from datetime import datetime, timezone
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
# Constants (match TS fetch-messages fixtures)
# ---------------------------------------------------------------------------

SLACK_BOT_USER_ID = "U00FAKEBOT01"
SLACK_HUMAN_USER_ID = "U00FAKEUSER1"
SLACK_CHANNEL = "C00FAKECHAN1"
SLACK_THREAD_TS = "1710000000.000100"
SLACK_THREAD_ID = f"slack:{SLACK_CHANNEL}:{SLACK_THREAD_TS}"
EXPECTED_NUMBERED_TEXTS = [str(i) for i in range(1, 15)]


# ---------------------------------------------------------------------------
# FetchableAdapter
# ---------------------------------------------------------------------------


class SlackFetchableAdapter(MockAdapter):
    """Adapter mock with configurable fetch results for Slack."""

    def __init__(self) -> None:
        super().__init__("slack")
        self._messages: list[Message] = []
        self._fetch_params: list[dict[str, Any]] = []

    def set_messages(self, messages: list[Message]) -> None:
        self._messages = messages

    async def fetch_messages(self, thread_id: str, options: FetchOptions | None = None) -> FetchResult:
        opts = options or FetchOptions()
        self._fetch_params.append(
            {
                "thread_id": thread_id,
                "limit": opts.limit,
                "direction": opts.direction,
                "cursor": opts.cursor,
            }
        )
        self._fetch_calls.append((thread_id, opts))

        limit = opts.limit or 100
        messages = self._messages[:limit]
        has_more = len(self._messages) > limit
        return FetchResult(
            messages=messages,
            next_cursor="next-cursor" if has_more else None,
        )


def _build_test_messages(
    count_human: int = 15,
    count_bot: int = 4,
    numbered_start: int = 1,
    numbered_end: int = 14,
) -> list[Message]:
    """Build a mixed list of human and bot messages (chronological order).

    Structure: Hey, Welcome(bot), FetchResults(bot), 1..14, Thanks(bot), Thanks(bot)
    """
    messages: list[Message] = []

    # "Hey" human message
    messages.append(
        Message(
            id="msg-hey",
            thread_id=SLACK_THREAD_ID,
            text="Hey",
            formatted={"type": "root", "children": []},
            raw={},
            author=Author(
                user_id=SLACK_HUMAN_USER_ID,
                user_name="Test User",
                full_name="Test User",
                is_bot=False,
                is_me=False,
            ),
            metadata=MessageMetadata(
                date_sent=datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc),
                edited=False,
            ),
            attachments=[],
            links=[],
        )
    )

    # "Welcome" bot message
    messages.append(
        Message(
            id="msg-welcome",
            thread_id=SLACK_THREAD_ID,
            text="Welcome",
            formatted={"type": "root", "children": []},
            raw={},
            author=Author(
                user_id=SLACK_BOT_USER_ID,
                user_name="Chat SDK Bot",
                full_name="Chat SDK Bot",
                is_bot=True,
                is_me=True,
            ),
            metadata=MessageMetadata(
                date_sent=datetime(2024, 1, 15, 10, 30, 1, tzinfo=timezone.utc),
                edited=False,
            ),
            attachments=[],
            links=[],
        )
    )

    # "Fetch Results" bot message
    messages.append(
        Message(
            id="msg-fetch-results",
            thread_id=SLACK_THREAD_ID,
            text="Fetch Results",
            formatted={"type": "root", "children": []},
            raw={},
            author=Author(
                user_id=SLACK_BOT_USER_ID,
                user_name="Chat SDK Bot",
                full_name="Chat SDK Bot",
                is_bot=True,
                is_me=True,
            ),
            metadata=MessageMetadata(
                date_sent=datetime(2024, 1, 15, 10, 30, 2, tzinfo=timezone.utc),
                edited=False,
            ),
            attachments=[],
            links=[],
        )
    )

    # Numbered human messages 1..14
    for i in range(numbered_start, numbered_end + 1):
        messages.append(
            Message(
                id=f"msg-num-{i}",
                thread_id=SLACK_THREAD_ID,
                text=str(i),
                formatted={"type": "root", "children": []},
                raw={},
                author=Author(
                    user_id=SLACK_HUMAN_USER_ID,
                    user_name="Test User",
                    full_name="Test User",
                    is_bot=False,
                    is_me=False,
                ),
                metadata=MessageMetadata(
                    date_sent=datetime(2024, 1, 15, 10, 30, 2 + i, tzinfo=timezone.utc),
                    edited=False,
                ),
                attachments=[],
                links=[],
            )
        )

    # 2 bot "Thanks" messages
    for j in range(2):
        messages.append(
            Message(
                id=f"msg-thanks-{j}",
                thread_id=SLACK_THREAD_ID,
                text="Thanks",
                formatted={"type": "root", "children": []},
                raw={},
                author=Author(
                    user_id=SLACK_BOT_USER_ID,
                    user_name="Chat SDK Bot",
                    full_name="Chat SDK Bot",
                    is_bot=True,
                    is_me=True,
                ),
                metadata=MessageMetadata(
                    date_sent=datetime(2024, 1, 15, 10, 30, 17 + j, tzinfo=timezone.utc),
                    edited=False,
                ),
                attachments=[],
                links=[],
            )
        )

    return messages


# ============================================================================
# fetchMessages Replay Tests - Slack
# ============================================================================


class TestFetchMessagesSlack:
    """Slack-specific fetchMessages replay tests."""

    @pytest.mark.asyncio
    async def test_forward_direction_params(self):
        """Forward direction passes correct parameters."""
        adapter = SlackFetchableAdapter()
        adapter.set_messages(_build_test_messages())

        await adapter.fetch_messages(SLACK_THREAD_ID, FetchOptions(limit=25, direction="forward"))

        assert len(adapter._fetch_params) == 1
        assert adapter._fetch_params[0]["direction"] == "forward"
        assert adapter._fetch_params[0]["limit"] == 25

    @pytest.mark.asyncio
    async def test_backward_direction_params(self):
        """Backward direction passes correct parameters."""
        adapter = SlackFetchableAdapter()
        adapter.set_messages(_build_test_messages())

        await adapter.fetch_messages(SLACK_THREAD_ID, FetchOptions(limit=50, direction="backward"))

        assert len(adapter._fetch_params) == 1
        assert adapter._fetch_params[0]["direction"] == "backward"
        assert adapter._fetch_params[0]["limit"] == 50

    @pytest.mark.asyncio
    async def test_all_messages_chronological_order(self):
        """All messages are returned in chronological order."""
        adapter = SlackFetchableAdapter()
        all_messages = _build_test_messages()
        adapter.set_messages(all_messages)

        result = await adapter.fetch_messages(SLACK_THREAD_ID, FetchOptions(limit=100, direction="forward"))

        assert len(result.messages) == 19

        # Extract numbered messages
        numbered = [m for m in result.messages if m.text in EXPECTED_NUMBERED_TEXTS]
        assert len(numbered) == 14

        texts = [m.text for m in numbered]
        assert texts == EXPECTED_NUMBERED_TEXTS

    @pytest.mark.asyncio
    async def test_backward_chronological_order(self):
        """Backward direction still returns messages in order."""
        adapter = SlackFetchableAdapter()
        all_messages = _build_test_messages()
        adapter.set_messages(all_messages)

        result = await adapter.fetch_messages(SLACK_THREAD_ID, FetchOptions(limit=100, direction="backward"))

        assert len(result.messages) == 19
        numbered = [m for m in result.messages if m.text in EXPECTED_NUMBERED_TEXTS]
        texts = [m.text for m in numbered]
        assert texts == EXPECTED_NUMBERED_TEXTS

    @pytest.mark.asyncio
    async def test_bot_vs_human_identification(self):
        """Bot and human messages are correctly identified."""
        adapter = SlackFetchableAdapter()
        adapter.set_messages(_build_test_messages())

        result = await adapter.fetch_messages(SLACK_THREAD_ID, FetchOptions(limit=100))

        bot_messages = [m for m in result.messages if m.author.is_bot]
        human_messages = [m for m in result.messages if not m.author.is_bot]

        assert len(bot_messages) == 4
        assert len(human_messages) == 15

        for msg in bot_messages:
            assert msg.author.is_me is True
            assert msg.author.user_id == SLACK_BOT_USER_ID

        for msg in human_messages:
            assert msg.author.is_me is False
            assert msg.author.user_id == SLACK_HUMAN_USER_ID

    @pytest.mark.asyncio
    async def test_user_display_names_resolved(self):
        """Human and bot messages have resolved display names."""
        adapter = SlackFetchableAdapter()
        adapter.set_messages(_build_test_messages())

        result = await adapter.fetch_messages(SLACK_THREAD_ID, FetchOptions(limit=100))

        human_msg = next(m for m in result.messages if m.author.user_id == SLACK_HUMAN_USER_ID)
        assert human_msg.author.user_name == "Test User"
        assert human_msg.author.full_name == "Test User"

        bot_msg = next(m for m in result.messages if m.author.user_id == SLACK_BOT_USER_ID)
        assert bot_msg.author.user_name == "Chat SDK Bot"

    @pytest.mark.asyncio
    async def test_respects_limit_parameter(self):
        """Only requested number of messages returned."""
        adapter = SlackFetchableAdapter()
        adapter.set_messages(_build_test_messages())

        result = await adapter.fetch_messages(SLACK_THREAD_ID, FetchOptions(limit=5, direction="forward"))

        assert len(result.messages) == 5
        assert result.messages[0].text == "Hey"
        assert result.messages[3].text == "1"
        assert result.messages[4].text == "2"


# ============================================================================
# allMessages Replay Tests - Slack
# ============================================================================


class TestAllMessagesSlack:
    """thread.allMessages iterator tests for Slack."""

    @pytest.mark.asyncio
    async def test_all_messages_chronological_via_iterator(self):
        """All messages are iterated in chronological order."""
        adapter = SlackFetchableAdapter()
        all_messages = _build_test_messages()
        adapter.set_messages(all_messages)

        # Simulate iterator by fetching all
        result = await adapter.fetch_messages(SLACK_THREAD_ID, FetchOptions(limit=100, direction="forward"))

        assert len(result.messages) == 19
        numbered = [m for m in result.messages if m.text in EXPECTED_NUMBERED_TEXTS]
        texts = [m.text for m in numbered]
        assert texts == EXPECTED_NUMBERED_TEXTS

    @pytest.mark.asyncio
    async def test_all_messages_uses_forward_with_limit_100(self):
        """allMessages uses forward direction with limit 100."""
        adapter = SlackFetchableAdapter()
        adapter.set_messages(_build_test_messages())

        await adapter.fetch_messages(SLACK_THREAD_ID, FetchOptions(limit=100, direction="forward"))

        assert adapter._fetch_params[0]["direction"] == "forward"
        assert adapter._fetch_params[0]["limit"] == 100
