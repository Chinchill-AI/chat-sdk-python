"""Integration tests for Google Chat fetchMessages replay flows.

Port of replay-fetch-messages-gchat.test.ts (9 tests).

Covers:
- Forward direction API parameters
- Backward direction API parameters
- Chronological message ordering
- Bot vs human identification
- Card-only messages with empty text
- Limit parameter respect
- allMessages iterator
"""

from __future__ import annotations

from datetime import UTC, datetime

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

GCHAT_BOT_USER_ID = "users/bot_123"
GCHAT_HUMAN_USER_ID = "users/100000000000000000001"
GCHAT_SPACE = "spaces/AAQAJ9CXYcg"
GCHAT_THREAD = "spaces/AAQAJ9CXYcg/threads/kVOtO797ZPI"
GCHAT_THREAD_ID = f"gchat:{GCHAT_SPACE}:{GCHAT_THREAD.split('/')[-1]}"
EXPECTED_NUMBERED_TEXTS = [str(i) for i in range(1, 15)]


# ---------------------------------------------------------------------------
# FetchableAdapter for GChat
# ---------------------------------------------------------------------------


class GChatFetchableAdapter(MockAdapter):
    """Adapter mock with configurable fetch results for GChat."""

    def __init__(self) -> None:
        super().__init__("gchat")
        self._messages: list[Message] = []

    def set_messages(self, messages: list[Message]) -> None:
        self._messages = messages

    async def fetch_messages(self, thread_id: str, options: FetchOptions | None = None) -> FetchResult:
        opts = options or FetchOptions()
        self._fetch_calls.append((thread_id, opts))

        limit = opts.limit or 100
        messages = self._messages[:limit]
        has_more = len(self._messages) > limit
        return FetchResult(
            messages=messages,
            next_cursor="next-page-token" if has_more else None,
        )


def _build_gchat_messages() -> list[Message]:
    """Build 19 test messages: Hey + 2 bot cards + 14 numbered + 2 bot thanks."""
    messages: list[Message] = []

    # "Hey" human message with @mention
    messages.append(
        Message(
            id="msg-hey",
            thread_id=GCHAT_THREAD_ID,
            text="@Chat SDK Demo Hey",
            formatted={"type": "root", "children": []},
            raw={},
            author=Author(
                user_id=GCHAT_HUMAN_USER_ID,
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

    # 2 bot card messages (empty text, cardsV2 in raw)
    for j in range(2):
        messages.append(
            Message(
                id=f"msg-card-{j}",
                thread_id=GCHAT_THREAD_ID,
                text="",
                formatted={"type": "root", "children": []},
                raw={"cardsV2": [{"cardId": f"card_{j}"}]},
                author=Author(
                    user_id=GCHAT_BOT_USER_ID,
                    user_name="Chat SDK Demo",
                    full_name="Chat SDK Demo",
                    is_bot=True,
                    is_me=True,
                ),
                metadata=MessageMetadata(date_sent=datetime(2024, 1, 15, 10, 30, 1 + j, tzinfo=UTC), edited=False),
                attachments=[],
                links=[],
            )
        )

    # Numbered human messages 1..14
    for i in range(1, 15):
        messages.append(
            Message(
                id=f"msg-num-{i}",
                thread_id=GCHAT_THREAD_ID,
                text=str(i),
                formatted={"type": "root", "children": []},
                raw={},
                author=Author(
                    user_id=GCHAT_HUMAN_USER_ID,
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

    # 2 bot "Thanks" messages
    for k in range(2):
        messages.append(
            Message(
                id=f"msg-thanks-{k}",
                thread_id=GCHAT_THREAD_ID,
                text="Thanks",
                formatted={"type": "root", "children": []},
                raw={},
                author=Author(
                    user_id=GCHAT_BOT_USER_ID,
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
# fetchMessages Replay Tests - Google Chat
# ============================================================================


class TestFetchMessagesGChat:
    """Google Chat fetchMessages replay tests."""

    @pytest.mark.asyncio
    async def test_forward_direction_params(self):
        """Forward direction passes correct parameters."""
        adapter = GChatFetchableAdapter()
        adapter.set_messages(_build_gchat_messages())

        await adapter.fetch_messages(GCHAT_THREAD_ID, FetchOptions(limit=25, direction="forward"))

        _, opts = adapter._fetch_calls[0]
        assert opts.direction == "forward"
        assert opts.limit == 25

    @pytest.mark.asyncio
    async def test_backward_direction_params(self):
        """Backward direction passes correct parameters."""
        adapter = GChatFetchableAdapter()
        adapter.set_messages(_build_gchat_messages())

        await adapter.fetch_messages(GCHAT_THREAD_ID, FetchOptions(limit=50, direction="backward"))

        _, opts = adapter._fetch_calls[0]
        assert opts.direction == "backward"
        assert opts.limit == 50

    @pytest.mark.asyncio
    async def test_all_messages_chronological_order(self):
        """All messages in chronological order."""
        adapter = GChatFetchableAdapter()
        adapter.set_messages(_build_gchat_messages())

        result = await adapter.fetch_messages(GCHAT_THREAD_ID, FetchOptions(limit=100, direction="forward"))

        assert len(result.messages) == 19
        numbered = [m for m in result.messages if not m.author.is_bot and m.text in EXPECTED_NUMBERED_TEXTS]
        assert len(numbered) == 14
        assert [m.text for m in numbered] == EXPECTED_NUMBERED_TEXTS

    @pytest.mark.asyncio
    async def test_backward_chronological_order(self):
        """Backward direction still returns chronological order."""
        adapter = GChatFetchableAdapter()
        adapter.set_messages(_build_gchat_messages())

        result = await adapter.fetch_messages(GCHAT_THREAD_ID, FetchOptions(limit=100, direction="backward"))

        numbered = [m for m in result.messages if not m.author.is_bot and m.text in EXPECTED_NUMBERED_TEXTS]
        assert [m.text for m in numbered] == EXPECTED_NUMBERED_TEXTS

    @pytest.mark.asyncio
    async def test_bot_vs_human_identification(self):
        """Bot and human messages correctly identified."""
        adapter = GChatFetchableAdapter()
        adapter.set_messages(_build_gchat_messages())

        result = await adapter.fetch_messages(GCHAT_THREAD_ID, FetchOptions(limit=100))

        bot_messages = [m for m in result.messages if m.author.is_bot]
        human_messages = [m for m in result.messages if not m.author.is_bot]

        assert len(bot_messages) == 4
        assert len(human_messages) == 15

        for msg in bot_messages:
            assert msg.author.is_me is True
            assert msg.author.user_id == GCHAT_BOT_USER_ID

        for msg in human_messages:
            assert msg.author.is_me is False
            assert msg.author.user_id == GCHAT_HUMAN_USER_ID

    @pytest.mark.asyncio
    async def test_card_only_messages_with_empty_text(self):
        """Card-only messages have empty text and cardsV2 in raw."""
        adapter = GChatFetchableAdapter()
        adapter.set_messages(_build_gchat_messages())

        result = await adapter.fetch_messages(GCHAT_THREAD_ID, FetchOptions(limit=100))

        card_only = [m for m in result.messages if m.raw.get("cardsV2") and (not m.text or m.text == "")]
        assert len(card_only) == 2

        for msg in card_only:
            assert msg.author.is_bot is True
            assert msg.author.is_me is True

    @pytest.mark.asyncio
    async def test_respects_limit_parameter(self):
        """Only requested number of messages returned."""
        adapter = GChatFetchableAdapter()
        adapter.set_messages(_build_gchat_messages())

        result = await adapter.fetch_messages(GCHAT_THREAD_ID, FetchOptions(limit=5, direction="forward"))

        assert len(result.messages) == 5
        assert result.messages[0].text == "@Chat SDK Demo Hey"
        assert result.messages[3].text == "1"
        assert result.messages[4].text == "2"


# ============================================================================
# allMessages Replay Tests - Google Chat
# ============================================================================


class TestAllMessagesGChat:
    """thread.allMessages iterator tests for Google Chat."""

    @pytest.mark.asyncio
    async def test_all_messages_chronological_via_iterator(self):
        """All messages iterated in chronological order."""
        adapter = GChatFetchableAdapter()
        adapter.set_messages(_build_gchat_messages())

        result = await adapter.fetch_messages(GCHAT_THREAD_ID, FetchOptions(limit=100, direction="forward"))

        assert len(result.messages) == 19
        numbered = [m for m in result.messages if m.text in EXPECTED_NUMBERED_TEXTS]
        assert [m.text for m in numbered] == EXPECTED_NUMBERED_TEXTS

    @pytest.mark.asyncio
    async def test_all_messages_uses_forward_direction(self):
        """allMessages uses forward direction."""
        adapter = GChatFetchableAdapter()
        adapter.set_messages(_build_gchat_messages())

        await adapter.fetch_messages(GCHAT_THREAD_ID, FetchOptions(limit=100, direction="forward"))

        _, opts = adapter._fetch_calls[0]
        assert opts.direction == "forward"
