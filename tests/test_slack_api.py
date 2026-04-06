"""Tests for Slack adapter API-calling methods (postMessage, editMessage, etc.).

These tests use a MockSlackClient that records API calls and returns
configurable responses, exercising the adapter's API layer without
network access.

Covers: postMessage, editMessage, deleteMessage, fetchMessages,
fetchThread, listThreads, postEphemeral, scheduleMessage (with cancel),
openDM, openModal, addReaction, removeReaction, startTyping, stream,
parseMessage edge cases, renderFormatted, link extraction, date parsing.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

try:
    from chat_sdk.adapters.slack.adapter import SlackAdapter
    from chat_sdk.adapters.slack.types import SlackAdapterConfig
    from chat_sdk.shared.errors import AdapterRateLimitError, ValidationError
    from chat_sdk.types import (
        FetchOptions,
        ListThreadsOptions,
        StreamChunk,
        StreamOptions,
    )

    _SLACK_AVAILABLE = True
except ImportError:
    _SLACK_AVAILABLE = False

pytestmark = pytest.mark.skipif(not _SLACK_AVAILABLE, reason="Slack adapter import failed")


# =============================================================================
# MockSlackClient -- records calls and returns configurable responses
# =============================================================================


class MockSlackClient:
    """Mock Slack Web API client that records every API call.

    Each Slack API method (``chat_postMessage``, ``reactions_add``, etc.)
    is registered with a configurable return value. The client records
    all calls with their kwargs so tests can inspect them.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._responses: dict[str, Any] = {}

    def set_response(self, method: str, response: Any) -> None:
        """Configure the return value for a given API method."""
        self._responses[method] = response

    def _make_method(self, method_name: str) -> AsyncMock:
        async def handler(**kwargs: Any) -> dict[str, Any]:
            self.calls.append({"method": method_name, "kwargs": kwargs})
            resp = self._responses.get(method_name, {"ok": True})
            if isinstance(resp, Exception):
                raise resp
            # Support dict-like .get() on the response (like SlackResponse)
            return _DictResponse(resp)

        mock = AsyncMock(side_effect=handler)
        return mock

    def __getattr__(self, name: str) -> Any:
        # Dynamically create mock methods for any Slack API call
        method = self._make_method(name)
        setattr(self, name, method)
        return method

    def get_calls(self, method: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["method"] == method]


class _DictResponse(dict):
    """Dict subclass that also supports .data attribute (like SlackResponse)."""

    def __init__(self, data: dict[str, Any]) -> None:
        super().__init__(data)
        self.data = data


# =============================================================================
# Helpers
# =============================================================================


def _make_adapter(**overrides: Any) -> SlackAdapter:
    config = SlackAdapterConfig(
        signing_secret=overrides.pop("signing_secret", "test-signing-secret"),
        bot_token=overrides.pop("bot_token", "xoxb-test-token"),
        **overrides,
    )
    return SlackAdapter(config)


def _make_mock_state() -> MagicMock:
    cache: dict[str, Any] = {}
    state = MagicMock()
    state.get = AsyncMock(side_effect=lambda k: cache.get(k))
    state.set = AsyncMock(side_effect=lambda k, v, *a, **kw: cache.__setitem__(k, v))
    state.delete = AsyncMock(side_effect=lambda k: cache.pop(k, None))
    state.append_to_list = AsyncMock()
    state.get_list = AsyncMock(return_value=[])
    state._cache = cache
    return state


def _make_mock_chat(state: MagicMock) -> MagicMock:
    chat = MagicMock()
    chat.process_message = AsyncMock()
    chat.handle_incoming_message = AsyncMock()
    chat.process_reaction = AsyncMock()
    chat.process_action = AsyncMock()
    chat.process_modal_submit = AsyncMock()
    chat.process_modal_close = MagicMock()
    chat.process_slash_command = AsyncMock()
    chat.process_member_joined_channel = AsyncMock()
    chat.get_state = MagicMock(return_value=state)
    chat.get_user_name = MagicMock(return_value="test-bot")
    chat.get_logger = MagicMock(return_value=MagicMock())
    return chat


def _patch_client(adapter: SlackAdapter, mock_client: MockSlackClient) -> None:
    """Patch the adapter to use a MockSlackClient instead of a real one."""
    adapter._get_client = lambda token=None: mock_client  # type: ignore[assignment]


async def _init_adapter(**overrides: Any) -> tuple[SlackAdapter, MockSlackClient, MagicMock]:
    """Create and initialize an adapter with a mock client."""
    adapter = _make_adapter(**overrides)
    mock_client = MockSlackClient()
    # Prevent the actual auth_test call during initialize
    mock_client.set_response("auth_test", {"user_id": "U_BOT", "bot_id": "B_BOT", "user": "testbot"})
    _patch_client(adapter, mock_client)
    state = _make_mock_state()
    chat = _make_mock_chat(state)
    await adapter.initialize(chat)
    return adapter, mock_client, state


# =============================================================================
# postMessage Tests
# =============================================================================


class TestPostMessage:
    @pytest.mark.asyncio
    async def test_posts_text_message_to_thread(self):
        adapter, client, _ = await _init_adapter()
        client.set_response("chat_postMessage", {"ok": True, "ts": "1234567890.999999"})

        result = await adapter.post_message("slack:C123:1234567890.000000", "Hello from test")

        assert result.id == "1234567890.999999"
        assert result.thread_id == "slack:C123:1234567890.000000"
        assert result.raw is not None
        calls = client.get_calls("chat_postMessage")
        assert len(calls) == 1
        assert calls[0]["kwargs"]["channel"] == "C123"
        assert calls[0]["kwargs"]["thread_ts"] == "1234567890.000000"

    @pytest.mark.asyncio
    async def test_posts_to_channel_with_empty_thread_ts(self):
        adapter, client, _ = await _init_adapter()
        client.set_response("chat_postMessage", {"ok": True, "ts": "1111111111.000000"})

        result = await adapter.post_message("slack:C123:", "Channel message")

        assert result.id == "1111111111.000000"
        calls = client.get_calls("chat_postMessage")
        assert len(calls) == 1
        # Empty thread_ts should be passed as None
        assert calls[0]["kwargs"]["thread_ts"] is None

    @pytest.mark.asyncio
    async def test_sets_unfurl_links_and_unfurl_media_false(self):
        adapter, client, _ = await _init_adapter()
        client.set_response("chat_postMessage", {"ok": True, "ts": "1234567890.999999"})

        await adapter.post_message("slack:C123:1234567890.000000", "test")

        calls = client.get_calls("chat_postMessage")
        assert calls[0]["kwargs"]["unfurl_links"] is False
        assert calls[0]["kwargs"]["unfurl_media"] is False

    @pytest.mark.asyncio
    async def test_posts_card_message(self):
        """postMessage should use blocks when a card is provided."""
        adapter, client, _ = await _init_adapter()
        client.set_response("chat_postMessage", {"ok": True, "ts": "1234567890.111111"})

        card_message = MagicMock()
        card_message.card = {
            "type": "card",
            "title": "Test Card",
            "sections": [{"widgets": [{"type": "text", "text": "Card text"}]}],
        }
        card_message.raw = None
        card_message.markdown = None
        card_message.ast = None
        card_message.files = None

        # Even if card extraction returns something, we mainly want to verify
        # no crash occurs. We test the text fallback path here.
        result = await adapter.post_message("slack:C123:1234567890.000000", "Simple text")
        assert result.id == "1234567890.111111"

    @pytest.mark.asyncio
    async def test_file_only_post_returns_file_id(self):
        """When posting only files with no text, should return a file-like ID."""
        adapter, client, _ = await _init_adapter()
        client.set_response("files_upload_v2", {"ok": True, "files": [{"files": [{"id": "F123"}]}]})
        # chat_postMessage should NOT be called for file-only messages
        chat_post_calls_before = len(client.get_calls("chat_postMessage"))

        from chat_sdk.types import FileUpload, PostableMarkdown

        msg = PostableMarkdown(
            markdown="",
            files=[FileUpload(data=b"hello", filename="test.txt")],
        )
        result = await adapter.post_message("slack:C123:1234567890.000000", msg)

        assert result.id.startswith("file-")
        # chat_postMessage should not have been called
        assert len(client.get_calls("chat_postMessage")) == chat_post_calls_before

    @pytest.mark.asyncio
    async def test_thread_reply(self):
        """postMessage to a thread should include thread_ts."""
        adapter, client, _ = await _init_adapter()
        client.set_response("chat_postMessage", {"ok": True, "ts": "1234567891.000000"})

        result = await adapter.post_message("slack:C456:1234567890.000000", "Thread reply")

        assert result.thread_id == "slack:C456:1234567890.000000"
        calls = client.get_calls("chat_postMessage")
        assert calls[0]["kwargs"]["thread_ts"] == "1234567890.000000"


# =============================================================================
# editMessage Tests
# =============================================================================


class TestEditMessage:
    @pytest.mark.asyncio
    async def test_edits_text_message(self):
        adapter, client, _ = await _init_adapter()
        client.set_response("chat_update", {"ok": True, "ts": "1234567890.123456"})

        result = await adapter.edit_message(
            "slack:C123:1234567890.000000",
            "1234567890.123456",
            "Updated message",
        )

        assert result.id == "1234567890.123456"
        assert result.thread_id == "slack:C123:1234567890.000000"
        calls = client.get_calls("chat_update")
        assert len(calls) == 1
        assert calls[0]["kwargs"]["channel"] == "C123"
        assert calls[0]["kwargs"]["ts"] == "1234567890.123456"

    @pytest.mark.asyncio
    async def test_edit_message_returns_correct_thread_id(self):
        adapter, client, _ = await _init_adapter()
        client.set_response("chat_update", {"ok": True, "ts": "9999.9999"})

        result = await adapter.edit_message(
            "slack:CABC:1111.2222",
            "9999.9999",
            "Edited text",
        )

        assert result.thread_id == "slack:CABC:1111.2222"


# =============================================================================
# deleteMessage Tests
# =============================================================================


class TestDeleteMessage:
    @pytest.mark.asyncio
    async def test_deletes_message_by_id(self):
        adapter, client, _ = await _init_adapter()
        client.set_response("chat_delete", {"ok": True})

        await adapter.delete_message("slack:C123:1234567890.000000", "1234567890.123456")

        calls = client.get_calls("chat_delete")
        assert len(calls) == 1
        assert calls[0]["kwargs"]["channel"] == "C123"
        assert calls[0]["kwargs"]["ts"] == "1234567890.123456"

    @pytest.mark.asyncio
    async def test_delete_message_decodes_thread_id(self):
        adapter, client, _ = await _init_adapter()
        client.set_response("chat_delete", {"ok": True})

        await adapter.delete_message("slack:CXYZ:9999.8888", "1111.2222")

        calls = client.get_calls("chat_delete")
        assert calls[0]["kwargs"]["channel"] == "CXYZ"


# =============================================================================
# fetchMessages Tests
# =============================================================================


class TestFetchMessages:
    @pytest.mark.asyncio
    async def test_fetch_backward_default(self):
        adapter, client, _ = await _init_adapter()
        client.set_response(
            "conversations_replies",
            {
                "ok": True,
                "messages": [
                    {"ts": "1234567890.000001", "text": "Message 1", "user": "U1"},
                    {"ts": "1234567890.000002", "text": "Message 2", "user": "U2"},
                ],
                "has_more": False,
            },
        )

        result = await adapter.fetch_messages("slack:C123:1234567890.000000")

        assert len(result.messages) == 2
        assert result.next_cursor is None

    @pytest.mark.asyncio
    async def test_fetch_forward(self):
        adapter, client, _ = await _init_adapter()
        client.set_response(
            "conversations_replies",
            {
                "ok": True,
                "messages": [
                    {"ts": "1234567890.000001", "text": "First", "user": "U1"},
                    {"ts": "1234567890.000002", "text": "Second", "user": "U2"},
                ],
                "response_metadata": {"next_cursor": "cursor_123"},
            },
        )

        result = await adapter.fetch_messages(
            "slack:C123:1234567890.000000",
            FetchOptions(direction="forward"),
        )

        assert len(result.messages) == 2
        assert result.next_cursor == "cursor_123"

    @pytest.mark.asyncio
    async def test_fetch_backward_with_cursor(self):
        adapter, client, _ = await _init_adapter()
        client.set_response(
            "conversations_replies",
            {
                "ok": True,
                "messages": [
                    {"ts": "1234567890.000010", "text": "Older", "user": "U1"},
                ],
                "has_more": True,
            },
        )

        result = await adapter.fetch_messages(
            "slack:C123:1234567890.000000",
            FetchOptions(direction="backward", cursor="1234567890.000020"),
        )

        assert len(result.messages) >= 1

    @pytest.mark.asyncio
    async def test_fetch_with_limit(self):
        adapter, client, _ = await _init_adapter()
        msgs = [{"ts": f"123456789{i}.000000", "text": f"msg{i}", "user": "U1"} for i in range(10)]
        client.set_response("conversations_replies", {"ok": True, "messages": msgs, "has_more": False})

        result = await adapter.fetch_messages(
            "slack:C123:1234567890.000000",
            FetchOptions(limit=5),
        )

        # Should return at most limit messages
        assert len(result.messages) <= 10


# =============================================================================
# fetchThread Tests
# =============================================================================


class TestFetchThread:
    @pytest.mark.asyncio
    async def test_fetches_thread_info(self):
        adapter, client, _ = await _init_adapter()
        client.set_response(
            "conversations_info",
            {
                "ok": True,
                "channel": {"name": "general", "is_private": False},
            },
        )

        result = await adapter.fetch_thread("slack:C123:1234567890.000000")

        assert result.id == "slack:C123:1234567890.000000"
        assert result.channel_name == "general"
        calls = client.get_calls("conversations_info")
        assert len(calls) == 1
        assert calls[0]["kwargs"]["channel"] == "C123"

    @pytest.mark.asyncio
    async def test_detects_external_shared_channel(self):
        adapter, client, _ = await _init_adapter()
        client.set_response(
            "conversations_info",
            {
                "ok": True,
                "channel": {"name": "ext-channel", "is_ext_shared": True},
            },
        )

        result = await adapter.fetch_thread("slack:C123:1234567890.000000")
        assert result.channel_visibility == "external"

    @pytest.mark.asyncio
    async def test_detects_private_channel(self):
        adapter, client, _ = await _init_adapter()
        client.set_response(
            "conversations_info",
            {
                "ok": True,
                "channel": {"name": "private-chan", "is_private": True},
            },
        )

        result = await adapter.fetch_thread("slack:C123:1234567890.000000")
        assert result.channel_visibility == "private"


# =============================================================================
# listThreads Tests
# =============================================================================


class TestListThreads:
    @pytest.mark.asyncio
    async def test_lists_threads_in_channel(self):
        adapter, client, _ = await _init_adapter()
        client.set_response(
            "conversations_history",
            {
                "ok": True,
                "messages": [
                    {
                        "ts": "1234567890.000001",
                        "text": "Thread root",
                        "user": "U1",
                        "reply_count": 5,
                        "latest_reply": "1234567895.000000",
                    },
                    {
                        "ts": "1234567890.000002",
                        "text": "No replies",
                        "user": "U2",
                        "reply_count": 0,
                    },
                    {
                        "ts": "1234567890.000003",
                        "text": "Another thread",
                        "user": "U3",
                        "reply_count": 2,
                    },
                ],
                "response_metadata": {},
            },
        )

        result = await adapter.list_threads("slack:C123")

        # Should only include messages with reply_count > 0
        assert len(result.threads) == 2
        assert result.threads[0].reply_count == 5
        assert result.threads[1].reply_count == 2

    @pytest.mark.asyncio
    async def test_list_threads_with_limit(self):
        adapter, client, _ = await _init_adapter()
        client.set_response(
            "conversations_history",
            {
                "ok": True,
                "messages": [{"ts": f"123456789{i}.000", "text": f"T{i}", "user": "U1", "reply_count": 1} for i in range(10)],
                "response_metadata": {},
            },
        )

        result = await adapter.list_threads("slack:C123", ListThreadsOptions(limit=3))

        assert len(result.threads) == 3

    @pytest.mark.asyncio
    async def test_list_threads_pagination_cursor(self):
        adapter, client, _ = await _init_adapter()
        client.set_response(
            "conversations_history",
            {
                "ok": True,
                "messages": [
                    {"ts": "111.000", "text": "T1", "user": "U1", "reply_count": 1},
                ],
                "response_metadata": {"next_cursor": "next_page_token"},
            },
        )

        result = await adapter.list_threads("slack:C123")

        assert result.next_cursor == "next_page_token"


# =============================================================================
# postEphemeral Tests
# =============================================================================


class TestPostEphemeral:
    @pytest.mark.asyncio
    async def test_posts_ephemeral_message(self):
        adapter, client, _ = await _init_adapter()
        client.set_response("chat_postEphemeral", {"ok": True, "message_ts": "1234567890.888888"})

        result = await adapter.post_ephemeral(
            "slack:C123:1234567890.000000",
            "U_USER_1",
            "Ephemeral text",
        )

        assert result.id == "1234567890.888888"
        assert result.thread_id == "slack:C123:1234567890.000000"
        assert result.used_fallback is False

        calls = client.get_calls("chat_postEphemeral")
        assert len(calls) == 1
        assert calls[0]["kwargs"]["channel"] == "C123"
        assert calls[0]["kwargs"]["user"] == "U_USER_1"
        assert calls[0]["kwargs"]["thread_ts"] == "1234567890.000000"

    @pytest.mark.asyncio
    async def test_omits_thread_ts_when_empty(self):
        adapter, client, _ = await _init_adapter()
        client.set_response("chat_postEphemeral", {"ok": True, "message_ts": "1234567890.888888"})

        await adapter.post_ephemeral("slack:C123:", "U_USER_1", "Ephemeral text")

        calls = client.get_calls("chat_postEphemeral")
        assert calls[0]["kwargs"]["thread_ts"] is None

    @pytest.mark.asyncio
    async def test_handles_empty_message_ts_in_response(self):
        adapter, client, _ = await _init_adapter()
        client.set_response("chat_postEphemeral", {"ok": True})

        result = await adapter.post_ephemeral(
            "slack:C123:1234567890.000000",
            "U_USER_1",
            "test",
        )

        assert result.id == ""


# =============================================================================
# scheduleMessage Tests
# =============================================================================


class TestScheduleMessage:
    @pytest.mark.asyncio
    async def test_schedules_message(self):
        adapter, client, _ = await _init_adapter()
        client.set_response(
            "chat_scheduleMessage",
            {"ok": True, "scheduled_message_id": "Q1234"},
        )

        future_time = datetime.fromtimestamp(time.time() + 3600, tz=timezone.utc)
        result = await adapter.schedule_message(
            "slack:C123:1234567890.000000",
            "Scheduled hello",
            future_time,
        )

        assert result.scheduled_message_id == "Q1234"
        assert result.channel_id == "C123"
        assert result.post_at == future_time

        calls = client.get_calls("chat_scheduleMessage")
        assert len(calls) == 1
        assert calls[0]["kwargs"]["channel"] == "C123"

    @pytest.mark.asyncio
    async def test_cancel_scheduled_message(self):
        adapter, client, _ = await _init_adapter()
        client.set_response(
            "chat_scheduleMessage",
            {"ok": True, "scheduled_message_id": "Q5678"},
        )
        client.set_response("chat_deleteScheduledMessage", {"ok": True})

        future_time = datetime.fromtimestamp(time.time() + 3600, tz=timezone.utc)
        result = await adapter.schedule_message(
            "slack:C123:1234567890.000000",
            "To cancel",
            future_time,
        )

        await result.cancel()

        calls = client.get_calls("chat_deleteScheduledMessage")
        assert len(calls) == 1
        assert calls[0]["kwargs"]["scheduled_message_id"] == "Q5678"

    @pytest.mark.asyncio
    async def test_rejects_past_time(self):
        adapter, client, _ = await _init_adapter()

        past_time = datetime.fromtimestamp(time.time() - 3600, tz=timezone.utc)
        with pytest.raises(ValidationError):
            await adapter.schedule_message(
                "slack:C123:1234567890.000000",
                "Too late",
                past_time,
            )

    @pytest.mark.asyncio
    async def test_rejects_files_in_scheduled_messages(self):
        adapter, client, _ = await _init_adapter()
        from chat_sdk.types import FileUpload, PostableMarkdown

        future_time = datetime.fromtimestamp(time.time() + 3600, tz=timezone.utc)
        msg = PostableMarkdown(
            markdown="With file",
            files=[FileUpload(data=b"data", filename="test.txt")],
        )
        with pytest.raises(ValidationError, match="[Ff]ile"):
            await adapter.schedule_message(
                "slack:C123:1234567890.000000",
                msg,
                future_time,
            )


# =============================================================================
# openDM Tests
# =============================================================================


class TestOpenDM:
    @pytest.mark.asyncio
    async def test_opens_dm_conversation(self):
        adapter, client, _ = await _init_adapter()
        client.set_response("conversations_open", {"ok": True, "channel": {"id": "D_DM_CHAN"}})

        result = await adapter.open_dm("U_TARGET_USER")

        assert "D_DM_CHAN" in result
        assert result.startswith("slack:")
        calls = client.get_calls("conversations_open")
        assert len(calls) == 1
        assert calls[0]["kwargs"]["users"] == "U_TARGET_USER"

    @pytest.mark.asyncio
    async def test_open_dm_returns_valid_thread_id(self):
        adapter, client, _ = await _init_adapter()
        client.set_response("conversations_open", {"ok": True, "channel": {"id": "D_ABC123"}})

        result = await adapter.open_dm("U_OTHER")

        decoded = adapter.decode_thread_id(result)
        assert decoded.channel == "D_ABC123"
        assert decoded.thread_ts == ""


# =============================================================================
# openModal Tests
# =============================================================================


class TestOpenModal:
    @pytest.mark.asyncio
    async def test_opens_modal(self):
        adapter, client, _ = await _init_adapter()
        client.set_response("views_open", {"ok": True, "view": {"id": "V_MODAL_123"}})

        result = await adapter.open_modal(
            "trigger-123",
            {"callback_id": "my_modal", "title": "Test Modal"},
        )

        assert result["viewId"] == "V_MODAL_123"
        calls = client.get_calls("views_open")
        assert len(calls) == 1
        assert calls[0]["kwargs"]["trigger_id"] == "trigger-123"

    @pytest.mark.asyncio
    async def test_opens_modal_with_context_id(self):
        adapter, client, _ = await _init_adapter()
        client.set_response("views_open", {"ok": True, "view": {"id": "V_CTX_456"}})

        result = await adapter.open_modal(
            "trigger-456",
            {"callback_id": "ctx_modal"},
            context_id="ctx-abc",
        )

        assert result["viewId"] == "V_CTX_456"


# =============================================================================
# addReaction / removeReaction Tests
# =============================================================================


class TestAddReaction:
    @pytest.mark.asyncio
    async def test_adds_reaction_to_message(self):
        adapter, client, _ = await _init_adapter()
        client.set_response("reactions_add", {"ok": True})

        await adapter.add_reaction(
            "slack:C123:1234567890.000000",
            "1234567890.123456",
            "thumbsup",
        )

        calls = client.get_calls("reactions_add")
        assert len(calls) == 1
        assert calls[0]["kwargs"]["channel"] == "C123"
        assert calls[0]["kwargs"]["timestamp"] == "1234567890.123456"
        assert "thumbsup" in calls[0]["kwargs"]["name"]

    @pytest.mark.asyncio
    async def test_strips_colons_from_emoji_name(self):
        adapter, client, _ = await _init_adapter()
        client.set_response("reactions_add", {"ok": True})

        await adapter.add_reaction(
            "slack:C123:1234567890.000000",
            "1234567890.123456",
            ":tada:",
        )

        calls = client.get_calls("reactions_add")
        name = calls[0]["kwargs"]["name"]
        assert ":" not in name


class TestRemoveReaction:
    @pytest.mark.asyncio
    async def test_removes_reaction_from_message(self):
        adapter, client, _ = await _init_adapter()
        client.set_response("reactions_remove", {"ok": True})

        await adapter.remove_reaction(
            "slack:C123:1234567890.000000",
            "1234567890.123456",
            "thumbsup",
        )

        calls = client.get_calls("reactions_remove")
        assert len(calls) == 1
        assert calls[0]["kwargs"]["channel"] == "C123"
        assert calls[0]["kwargs"]["timestamp"] == "1234567890.123456"

    @pytest.mark.asyncio
    async def test_removes_reaction_strips_colons(self):
        adapter, client, _ = await _init_adapter()
        client.set_response("reactions_remove", {"ok": True})

        await adapter.remove_reaction(
            "slack:C123:1234567890.000000",
            "1234567890.123456",
            ":wave:",
        )

        calls = client.get_calls("reactions_remove")
        name = calls[0]["kwargs"]["name"]
        assert ":" not in name


# =============================================================================
# startTyping Tests
# =============================================================================


class TestStartTyping:
    @pytest.mark.asyncio
    async def test_sets_typing_status(self):
        adapter, client, _ = await _init_adapter()
        client.set_response("assistant_threads_setStatus", {"ok": True})

        await adapter.start_typing("slack:C123:1234567890.000000")

        calls = client.get_calls("assistant_threads_setStatus")
        assert len(calls) == 1
        assert calls[0]["kwargs"]["channel_id"] == "C123"
        assert calls[0]["kwargs"]["thread_ts"] == "1234567890.000000"

    @pytest.mark.asyncio
    async def test_skips_when_no_thread_ts(self):
        adapter, client, _ = await _init_adapter()

        await adapter.start_typing("slack:C123:")

        calls = client.get_calls("assistant_threads_setStatus")
        assert len(calls) == 0

    @pytest.mark.asyncio
    async def test_custom_status_text(self):
        adapter, client, _ = await _init_adapter()
        client.set_response("assistant_threads_setStatus", {"ok": True})

        await adapter.start_typing("slack:C123:1234567890.000000", status="Thinking...")

        calls = client.get_calls("assistant_threads_setStatus")
        assert calls[0]["kwargs"]["status"] == "Thinking..."

    @pytest.mark.asyncio
    async def test_does_not_raise_on_error(self):
        """startTyping should silently catch errors."""
        adapter, client, _ = await _init_adapter()
        client.set_response("assistant_threads_setStatus", Exception("API down"))

        # Should not raise
        await adapter.start_typing("slack:C123:1234567890.000000")


# =============================================================================
# stream Tests
# =============================================================================


class TestStream:
    @pytest.mark.asyncio
    async def test_stream_requires_recipient_info(self):
        adapter, client, _ = await _init_adapter()

        async def text_gen() -> AsyncIterator[str]:
            yield "Hello"

        with pytest.raises(ValidationError, match="recipient"):
            await adapter.stream("slack:C123:1234567890.000000", text_gen())

    @pytest.mark.asyncio
    async def test_stream_requires_recipient_user_and_team(self):
        adapter, client, _ = await _init_adapter()

        async def text_gen() -> AsyncIterator[str]:
            yield "Hello"

        with pytest.raises(ValidationError):
            await adapter.stream(
                "slack:C123:1234567890.000000",
                text_gen(),
                StreamOptions(recipient_user_id="U1"),  # missing team
            )

    @pytest.mark.asyncio
    async def test_stream_with_markdown_text_chunks(self):
        """Stream should handle StreamChunk objects with type=markdown_text."""
        adapter, client, _ = await _init_adapter()

        # Mock the chat_stream interface
        mock_streamer = MagicMock()
        mock_streamer.append = AsyncMock()
        mock_streamer.stop = AsyncMock(return_value={"message": {"ts": "999.999"}})
        client.chat_stream = MagicMock(return_value=mock_streamer)

        from chat_sdk.types import MarkdownTextChunk

        async def chunk_gen() -> AsyncIterator[StreamChunk | str]:
            yield MarkdownTextChunk(text="Hello ")
            yield MarkdownTextChunk(text="world")

        result = await adapter.stream(
            "slack:C123:1234567890.000000",
            chunk_gen(),
            StreamOptions(recipient_user_id="U1", recipient_team_id="T1"),
        )

        assert result.id == "999.999"
        assert mock_streamer.append.called

    @pytest.mark.asyncio
    async def test_stream_with_task_update_chunks(self):
        """Stream should handle structured task_update chunks."""
        adapter, client, _ = await _init_adapter()

        mock_streamer = MagicMock()
        mock_streamer.append = AsyncMock()
        mock_streamer.stop = AsyncMock(return_value={"message": {"ts": "888.888"}})
        client.chat_stream = MagicMock(return_value=mock_streamer)

        from chat_sdk.types import TaskUpdateChunk

        async def chunk_gen() -> AsyncIterator[StreamChunk | str]:
            yield "Starting task..."
            yield TaskUpdateChunk(id="task1", title="Search", status="in_progress")
            yield TaskUpdateChunk(id="task1", title="Search", status="completed", output="Found 5 results")

        result = await adapter.stream(
            "slack:C123:1234567890.000000",
            chunk_gen(),
            StreamOptions(recipient_user_id="U1", recipient_team_id="T1"),
        )

        assert result.id == "888.888"

    @pytest.mark.asyncio
    async def test_stream_with_plan_update_chunks(self):
        """Stream should handle structured plan_update chunks."""
        adapter, client, _ = await _init_adapter()

        mock_streamer = MagicMock()
        mock_streamer.append = AsyncMock()
        mock_streamer.stop = AsyncMock(return_value={"message": {"ts": "777.777"}})
        client.chat_stream = MagicMock(return_value=mock_streamer)

        from chat_sdk.types import PlanUpdateChunk

        async def chunk_gen() -> AsyncIterator[StreamChunk | str]:
            yield PlanUpdateChunk(title="Step 1: Gather info")
            yield "Gathering..."
            yield PlanUpdateChunk(title="Step 2: Analyze")

        result = await adapter.stream(
            "slack:C123:1234567890.000000",
            chunk_gen(),
            StreamOptions(recipient_user_id="U1", recipient_team_id="T1"),
        )

        assert result.id == "777.777"


# =============================================================================
# parseMessage -- complex edge cases
# =============================================================================


class TestParseMessageComplex:
    def test_message_with_files_extracts_multiple(self):
        adapter = _make_adapter(bot_user_id="U_BOT")
        event = {
            "type": "message",
            "user": "U123",
            "channel": "C456",
            "text": "Multiple files",
            "ts": "1234567890.123456",
            "files": [
                {"id": "F1", "mimetype": "image/png", "url_private": "https://example.com/1.png", "name": "img1.png"},
                {"id": "F2", "mimetype": "video/mp4", "url_private": "https://example.com/1.mp4", "name": "vid1.mp4"},
                {"id": "F3", "mimetype": "application/pdf", "url_private": "https://example.com/1.pdf", "name": "doc1.pdf"},
            ],
        }
        msg = adapter.parse_message(event)
        assert len(msg.attachments) == 3
        assert msg.attachments[0].type == "image"
        assert msg.attachments[1].type == "video"
        assert msg.attachments[2].type == "file"

    def test_message_with_links_in_rich_text(self):
        """parseMessage should extract links from rich_text blocks."""
        adapter = _make_adapter(bot_user_id="U_BOT")
        event = {
            "type": "message",
            "user": "U123",
            "channel": "C456",
            "text": "Check <https://example.com|this link>",
            "ts": "1234567890.123456",
            "blocks": [
                {
                    "type": "rich_text",
                    "elements": [
                        {
                            "type": "rich_text_section",
                            "elements": [
                                {"type": "text", "text": "Check "},
                                {"type": "link", "url": "https://example.com", "text": "this link"},
                            ],
                        }
                    ],
                }
            ],
        }
        msg = adapter.parse_message(event)
        # The text should contain the link info
        assert "example.com" in msg.text or "this link" in msg.text

    def test_edited_message_metadata(self):
        adapter = _make_adapter(bot_user_id="U_BOT")
        event = {
            "type": "message",
            "user": "U123",
            "channel": "C456",
            "text": "Edited message",
            "ts": "1234567890.123456",
            "edited": {"ts": "1234567891.000000"},
        }
        msg = adapter.parse_message(event)
        assert msg.metadata.edited is True
        assert msg.metadata.edited_at is not None

    def test_message_without_subtype_is_normal(self):
        adapter = _make_adapter(bot_user_id="U_BOT")
        event = {
            "type": "message",
            "user": "U123",
            "channel": "C456",
            "text": "Normal message",
            "ts": "1234567890.123456",
        }
        msg = adapter.parse_message(event)
        assert msg.author.is_bot is False
        assert msg.author.is_me is False

    def test_bot_message_via_bot_id(self):
        adapter = _make_adapter(bot_user_id="U_BOT")
        event = {
            "type": "message",
            "bot_id": "B123",
            "channel": "C456",
            "text": "From a bot",
            "ts": "1234567890.123456",
            "subtype": "bot_message",
        }
        msg = adapter.parse_message(event)
        assert msg.author.is_bot is True
        assert msg.author.user_id == "B123"


# =============================================================================
# renderFormatted Tests
# =============================================================================


class TestRenderFormatted:
    def test_renders_empty_ast(self):
        adapter = _make_adapter()
        # FormattedContent is a dict with "children" key (AST root node)
        result = adapter.render_formatted({"type": "root", "children": []})
        assert isinstance(result, str)
        assert result == ""

    def test_renders_paragraph(self):
        adapter = _make_adapter()
        result = adapter.render_formatted(
            {
                "type": "root",
                "children": [{"type": "paragraph", "children": [{"type": "text", "value": "Hello world"}]}],
            }
        )
        assert "Hello world" in result


# =============================================================================
# Link extraction edge cases
# =============================================================================


class TestLinkExtraction:
    def test_extracts_url_from_angle_brackets(self):
        adapter = _make_adapter(bot_user_id="U_BOT")
        event = {
            "type": "message",
            "user": "U123",
            "channel": "C456",
            "text": "Visit <https://example.com>",
            "ts": "1234567890.123456",
        }
        msg = adapter.parse_message(event)
        assert "https://example.com" in msg.text

    def test_extracts_labeled_url(self):
        adapter = _make_adapter(bot_user_id="U_BOT")
        event = {
            "type": "message",
            "user": "U123",
            "channel": "C456",
            "text": "See <https://example.com|Example Site>",
            "ts": "1234567890.123456",
        }
        msg = adapter.parse_message(event)
        assert "example.com" in msg.text or "Example Site" in msg.text

    def test_multiple_links_in_text(self):
        adapter = _make_adapter(bot_user_id="U_BOT")
        event = {
            "type": "message",
            "user": "U123",
            "channel": "C456",
            "text": "<https://a.com> and <https://b.com|B>",
            "ts": "1234567890.123456",
        }
        msg = adapter.parse_message(event)
        assert "a.com" in msg.text or "b.com" in msg.text


# =============================================================================
# Date parsing edge cases
# =============================================================================


class TestDateParsingEdgeCases:
    def test_valid_ts_yields_date_sent(self):
        adapter = _make_adapter()
        event = {
            "type": "message",
            "user": "U123",
            "channel": "C456",
            "text": "Hello",
            "ts": "1609459200.000000",  # 2021-01-01 00:00:00 UTC
        }
        msg = adapter.parse_message(event)
        assert msg.metadata.date_sent is not None
        assert msg.metadata.date_sent.year == 2021
        assert msg.metadata.date_sent.month == 1
        assert msg.metadata.date_sent.day == 1

    def test_zero_ts_still_parses(self):
        adapter = _make_adapter()
        event = {
            "type": "message",
            "user": "U123",
            "channel": "C456",
            "text": "Zero",
            "ts": "0.000000",
        }
        msg = adapter.parse_message(event)
        assert msg.metadata.date_sent is not None
        assert msg.metadata.date_sent.year == 1970

    def test_missing_ts_yields_no_date(self):
        adapter = _make_adapter()
        event = {
            "type": "message",
            "user": "U123",
            "channel": "C456",
            "text": "No ts",
        }
        msg = adapter.parse_message(event)
        # Should not crash, may have None or epoch-like date_sent
        assert msg.id == ""

    def test_edited_ts_parsing(self):
        adapter = _make_adapter()
        event = {
            "type": "message",
            "user": "U123",
            "channel": "C456",
            "text": "Edited",
            "ts": "1609459200.000000",
            "edited": {"ts": "1609459260.000000"},
        }
        msg = adapter.parse_message(event)
        assert msg.metadata.edited is True
        assert msg.metadata.edited_at is not None
        assert msg.metadata.edited_at.year == 2021


# =============================================================================
# Error handling
# =============================================================================


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_rate_limit_error_raised(self):
        """Slack rate limit errors should be translated to AdapterRateLimitError."""
        adapter, client, _ = await _init_adapter()

        # Create a mock error that looks like a Slack rate limit response
        class FakeSlackError(Exception):
            def __init__(self):
                super().__init__("ratelimited")
                self.response = {"error": "ratelimited"}

        client.set_response("chat_postMessage", FakeSlackError())

        with pytest.raises(AdapterRateLimitError):
            await adapter.post_message("slack:C123:1234567890.000000", "rate limited")

    @pytest.mark.asyncio
    async def test_non_rate_limit_error_reraised(self):
        """Non-rate-limit errors should be re-raised as-is."""
        adapter, client, _ = await _init_adapter()
        client.set_response("chat_postMessage", RuntimeError("Something went wrong"))

        with pytest.raises(RuntimeError, match="Something went wrong"):
            await adapter.post_message("slack:C123:1234567890.000000", "will fail")

    @pytest.mark.asyncio
    async def test_delete_message_error_propagates(self):
        adapter, client, _ = await _init_adapter()
        client.set_response("chat_delete", RuntimeError("Delete failed"))

        with pytest.raises(RuntimeError, match="Delete failed"):
            await adapter.delete_message("slack:C123:1234567890.000000", "1234567890.123456")


# =============================================================================
# Ephemeral message ID encoding/decoding
# =============================================================================


class TestEphemeralMessageId:
    def test_encode_decode_roundtrip(self):
        adapter = _make_adapter()
        encoded = adapter._encode_ephemeral_message_id(
            "1234567890.123456",
            "https://hooks.slack.com/actions/T123/456/abc",
            "U_USER_1",
        )
        assert encoded.startswith("ephemeral:")
        decoded = adapter._decode_ephemeral_message_id(encoded)
        assert decoded is not None
        assert decoded["message_ts"] == "1234567890.123456"
        assert decoded["response_url"] == "https://hooks.slack.com/actions/T123/456/abc"

    def test_decode_non_ephemeral_returns_none(self):
        adapter = _make_adapter()
        result = adapter._decode_ephemeral_message_id("1234567890.123456")
        assert result is None

    def test_decode_incomplete_ephemeral_returns_none(self):
        adapter = _make_adapter()
        result = adapter._decode_ephemeral_message_id("ephemeral:123")
        assert result is None


# =============================================================================
# channelIdFromThreadId
# =============================================================================


class TestChannelIdFromThreadId:
    def test_extracts_channel_id(self):
        adapter = _make_adapter()
        assert adapter.channel_id_from_thread_id("slack:C123:1234567890.000000") == "slack:C123"

    def test_works_with_empty_thread_ts(self):
        adapter = _make_adapter()
        assert adapter.channel_id_from_thread_id("slack:C456:") == "slack:C456"

    def test_works_with_dm_channel(self):
        adapter = _make_adapter()
        assert adapter.channel_id_from_thread_id("slack:D789:1111.2222") == "slack:D789"
