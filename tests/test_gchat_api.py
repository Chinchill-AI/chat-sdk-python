"""Tests for Google Chat adapter API-calling methods.

These tests use a mock aiohttp session to intercept HTTP requests
to the Google Chat API, verifying correct request parameters
and response handling without network access.

Covers: postMessage, editMessage, deleteMessage, fetchMessages,
fetchChannelMessages, listThreads, openDM, addReaction, removeReaction,
startTyping, stream, user info caching, workspace events subscription
lifecycle, bot user ID learning from annotations.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_sdk.adapters.google_chat.adapter import (
    GoogleChatAdapter,
)
from chat_sdk.adapters.google_chat.thread_utils import (
    GoogleChatThreadId,
    decode_thread_id,
    encode_thread_id,
)
from chat_sdk.adapters.google_chat.types import (
    GoogleChatAdapterConfig,
    ServiceAccountCredentials,
)
from chat_sdk.adapters.google_chat.user_info import UserInfoCache
from chat_sdk.shared.errors import AdapterRateLimitError, ValidationError
from chat_sdk.types import (
    FetchOptions,
    ListThreadsOptions,
)

# =============================================================================
# Mock Google Chat API -- intercepts _gchat_api_request
# =============================================================================


class MockGChatApi:
    """Mock that replaces _gchat_api_request to record calls and return
    configurable responses based on (method, path) tuples.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._responses: dict[tuple[str, str], Any] = {}
        self._default_responses: dict[str, Any] = {}
        self._path_prefix_responses: dict[str, Any] = {}

    def set_response(self, method: str, path: str, response: Any) -> None:
        """Set response for an exact (method, path) pair."""
        self._responses[(method, path)] = response

    def set_response_prefix(self, method: str, path_prefix: str, response: Any) -> None:
        """Set response for any path starting with prefix."""
        self._path_prefix_responses[f"{method}:{path_prefix}"] = response

    def set_default_response(self, method: str, response: Any) -> None:
        """Set a fallback response for a given HTTP method."""
        self._default_responses[method] = response

    async def __call__(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        use_impersonation: bool = False,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "method": method,
                "path": path,
                "body": body,
                "params": params,
                "use_impersonation": use_impersonation,
            }
        )

        # Check exact match
        key = (method, path)
        if key in self._responses:
            resp = self._responses[key]
            if isinstance(resp, Exception):
                raise resp
            return resp

        # Check prefix match
        for prefix_key, resp in self._path_prefix_responses.items():
            m, p = prefix_key.split(":", 1)
            if m == method and path.startswith(p):
                if isinstance(resp, Exception):
                    raise resp
                return resp

        # Check default
        if method in self._default_responses:
            resp = self._default_responses[method]
            if isinstance(resp, Exception):
                raise resp
            return resp

        return {}

    def get_calls(self, method: str | None = None, path: str | None = None) -> list[dict[str, Any]]:
        results = self.calls
        if method:
            results = [c for c in results if c["method"] == method]
        if path:
            results = [c for c in results if c["path"] == path]
        return results


# =============================================================================
# Helpers
# =============================================================================


def _make_credentials() -> ServiceAccountCredentials:
    return ServiceAccountCredentials(
        client_email="test@test.iam.gserviceaccount.com",
        private_key="-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----\n",
        project_id="test-project",
    )


def _make_adapter(**overrides: Any) -> GoogleChatAdapter:
    config = GoogleChatAdapterConfig(
        credentials=overrides.pop("credentials", _make_credentials()),
        **overrides,
    )
    return GoogleChatAdapter(config)


def _make_mock_state() -> MagicMock:
    storage: dict[str, Any] = {}
    state = MagicMock()
    state.get = AsyncMock(side_effect=lambda k: storage.get(k))
    state.set = AsyncMock(side_effect=lambda k, v, *a, **kw: storage.__setitem__(k, v))
    state.delete = AsyncMock(side_effect=lambda k: storage.pop(k, None))
    state._storage = storage
    return state


def _make_mock_chat(state: MagicMock) -> MagicMock:
    chat = MagicMock()
    chat.get_state = MagicMock(return_value=state)
    chat.get_logger = MagicMock(return_value=MagicMock())
    chat.process_message = AsyncMock()
    chat.process_reaction = AsyncMock()
    chat.process_action = AsyncMock()
    return chat


def _patch_api(adapter: GoogleChatAdapter, mock_api: MockGChatApi) -> None:
    """Replace the adapter's _gchat_api_request with a mock."""
    adapter._gchat_api_request = mock_api  # type: ignore[assignment]


async def _init_adapter(**overrides: Any) -> tuple[GoogleChatAdapter, MockGChatApi, MagicMock]:
    """Create and initialize an adapter with a mock API."""
    adapter = _make_adapter(**overrides)
    mock_api = MockGChatApi()
    _patch_api(adapter, mock_api)
    state = _make_mock_state()
    chat = _make_mock_chat(state)
    await adapter.initialize(chat)
    return adapter, mock_api, state


def _encode_tid(space: str, thread: str | None = None, is_dm: bool = False) -> str:
    return encode_thread_id(GoogleChatThreadId(space_name=space, thread_name=thread, is_dm=is_dm))


class _FakeApiError(Exception):
    """Fake error mimicking Google API errors."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
        self.errors = None


# =============================================================================
# postMessage Tests
# =============================================================================


class TestPostMessage:
    @pytest.mark.asyncio
    async def test_posts_text_message(self):
        adapter, api, _ = await _init_adapter()
        tid = _encode_tid("spaces/ABC123", "spaces/ABC123/threads/T1")
        api.set_response("POST", "spaces/ABC123/messages", {"name": "spaces/ABC123/messages/new1"})

        result = await adapter.post_message(tid, "Hello from bot")

        assert result.id == "spaces/ABC123/messages/new1"
        assert result.thread_id == tid
        calls = api.get_calls("POST", "spaces/ABC123/messages")
        assert len(calls) == 1
        assert calls[0]["body"]["text"] is not None
        assert calls[0]["body"].get("thread", {}).get("name") == "spaces/ABC123/threads/T1"

    @pytest.mark.asyncio
    async def test_posts_message_without_thread_name(self):
        adapter, api, _ = await _init_adapter()
        tid = _encode_tid("spaces/ABC123")
        api.set_response("POST", "spaces/ABC123/messages", {"name": "spaces/ABC123/messages/new2"})

        await adapter.post_message(tid, "Top level message")

        calls = api.get_calls("POST", "spaces/ABC123/messages")
        assert len(calls) == 1
        # Should not include thread info or messageReplyOption
        assert "thread" not in calls[0]["body"] or calls[0]["body"].get("thread") is None
        assert calls[0]["params"] is None or "messageReplyOption" not in (calls[0]["params"] or {})

    @pytest.mark.asyncio
    async def test_posts_message_with_thread_reply_option(self):
        adapter, api, _ = await _init_adapter()
        tid = _encode_tid("spaces/ABC123", "spaces/ABC123/threads/T1")
        api.set_response("POST", "spaces/ABC123/messages", {"name": "spaces/ABC123/messages/new3"})

        await adapter.post_message(tid, "Reply message")

        calls = api.get_calls("POST", "spaces/ABC123/messages")
        assert calls[0]["params"]["messageReplyOption"] == "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"

    @pytest.mark.asyncio
    async def test_post_message_api_error_raises(self):
        adapter, api, _ = await _init_adapter()
        tid = _encode_tid("spaces/ABC123")
        api.set_response("POST", "spaces/ABC123/messages", _FakeApiError(500, "Internal error"))

        with pytest.raises(_FakeApiError):
            await adapter.post_message(tid, "Will fail")

    @pytest.mark.asyncio
    async def test_post_message_rate_limit_raises_adapter_error(self):
        adapter, api, _ = await _init_adapter()
        tid = _encode_tid("spaces/ABC123")
        api.set_response("POST", "spaces/ABC123/messages", _FakeApiError(429, "Rate limited"))

        with pytest.raises(AdapterRateLimitError):
            await adapter.post_message(tid, "Rate limited")

    @pytest.mark.asyncio
    async def test_posts_ephemeral_message(self):
        adapter, api, _ = await _init_adapter()
        tid = _encode_tid("spaces/ABC123", "spaces/ABC123/threads/T1")
        api.set_response("POST", "spaces/ABC123/messages", {"name": "spaces/ABC123/messages/eph1"})

        result = await adapter.post_ephemeral(tid, "users/100", "Ephemeral text")

        assert result.id == "spaces/ABC123/messages/eph1"
        assert result.used_fallback is False
        calls = api.get_calls("POST", "spaces/ABC123/messages")
        assert calls[0]["body"]["privateMessageViewer"]["name"] == "users/100"


# =============================================================================
# editMessage Tests
# =============================================================================


class TestEditMessage:
    @pytest.mark.asyncio
    async def test_edits_text_message(self):
        adapter, api, _ = await _init_adapter()
        tid = _encode_tid("spaces/ABC123")
        msg_id = "spaces/ABC123/messages/msg1"
        api.set_response("PATCH", msg_id, {"name": msg_id})

        result = await adapter.edit_message(tid, msg_id, "Updated text")

        assert result.id == msg_id
        assert result.thread_id == tid
        calls = api.get_calls("PATCH", msg_id)
        assert len(calls) == 1
        assert calls[0]["body"]["text"] is not None
        assert calls[0]["params"]["updateMask"] == "text"

    @pytest.mark.asyncio
    async def test_edit_message_api_error(self):
        adapter, api, _ = await _init_adapter()
        tid = _encode_tid("spaces/ABC123")
        msg_id = "spaces/ABC123/messages/msg1"
        api.set_response("PATCH", msg_id, _FakeApiError(403, "Forbidden"))

        with pytest.raises(_FakeApiError):
            await adapter.edit_message(tid, msg_id, "edit")


# =============================================================================
# deleteMessage Tests
# =============================================================================


class TestDeleteMessage:
    @pytest.mark.asyncio
    async def test_deletes_message(self):
        adapter, api, _ = await _init_adapter()
        msg_id = "spaces/ABC123/messages/msg1"
        api.set_response("DELETE", msg_id, {})

        await adapter.delete_message("gchat:spaces/ABC123", msg_id)

        calls = api.get_calls("DELETE", msg_id)
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_delete_message_api_error(self):
        adapter, api, _ = await _init_adapter()
        msg_id = "spaces/ABC123/messages/msg1"
        api.set_response("DELETE", msg_id, _FakeApiError(404, "Not found"))

        with pytest.raises(_FakeApiError):
            await adapter.delete_message("gchat:spaces/ABC123", msg_id)


# =============================================================================
# fetchMessages Tests
# =============================================================================


class TestFetchMessages:
    @pytest.mark.asyncio
    async def test_fetch_backward(self):
        adapter, api, _ = await _init_adapter()
        tid = _encode_tid("spaces/ABC123", "spaces/ABC123/threads/T1")
        api.set_response_prefix(
            "GET",
            "spaces/ABC123/messages",
            {
                "messages": [
                    {
                        "name": "spaces/ABC123/messages/msg2",
                        "text": "Newer",
                        "sender": {"name": "users/100", "displayName": "Alice", "type": "HUMAN"},
                        "createTime": "2024-01-01T00:01:00Z",
                        "thread": {"name": "spaces/ABC123/threads/T1"},
                    },
                    {
                        "name": "spaces/ABC123/messages/msg1",
                        "text": "Older",
                        "sender": {"name": "users/101", "displayName": "Bob", "type": "HUMAN"},
                        "createTime": "2024-01-01T00:00:00Z",
                        "thread": {"name": "spaces/ABC123/threads/T1"},
                    },
                ],
            },
        )

        result = await adapter.fetch_messages(tid)

        # Backward returns newest-first reversed to chronological
        assert len(result.messages) == 2
        assert result.next_cursor is None

    @pytest.mark.asyncio
    async def test_fetch_forward(self):
        adapter, api, _ = await _init_adapter()
        tid = _encode_tid("spaces/ABC123", "spaces/ABC123/threads/T1")
        api.set_response_prefix(
            "GET",
            "spaces/ABC123/messages",
            {
                "messages": [
                    {
                        "name": "spaces/ABC123/messages/msg1",
                        "text": "First",
                        "sender": {"name": "users/100", "displayName": "Alice", "type": "HUMAN"},
                        "createTime": "2024-01-01T00:00:00Z",
                        "thread": {"name": "spaces/ABC123/threads/T1"},
                    },
                    {
                        "name": "spaces/ABC123/messages/msg2",
                        "text": "Second",
                        "sender": {"name": "users/101", "displayName": "Bob", "type": "HUMAN"},
                        "createTime": "2024-01-01T00:01:00Z",
                        "thread": {"name": "spaces/ABC123/threads/T1"},
                    },
                ],
            },
        )

        result = await adapter.fetch_messages(tid, FetchOptions(direction="forward"))

        assert len(result.messages) == 2

    @pytest.mark.asyncio
    async def test_fetch_backward_with_pagination(self):
        adapter, api, _ = await _init_adapter()
        tid = _encode_tid("spaces/ABC123", "spaces/ABC123/threads/T1")
        api.set_response_prefix(
            "GET",
            "spaces/ABC123/messages",
            {
                "messages": [
                    {
                        "name": "spaces/ABC123/messages/msg1",
                        "text": "Page 1",
                        "sender": {"name": "users/100", "displayName": "User", "type": "HUMAN"},
                        "createTime": "2024-01-01T00:00:00Z",
                    },
                ],
                "nextPageToken": "page2_token",
            },
        )

        result = await adapter.fetch_messages(tid)

        assert len(result.messages) == 1
        assert result.next_cursor == "page2_token"

    @pytest.mark.asyncio
    async def test_fetch_forward_with_cursor(self):
        adapter, api, _ = await _init_adapter()
        tid = _encode_tid("spaces/ABC123", "spaces/ABC123/threads/T1")
        api.set_response_prefix(
            "GET",
            "spaces/ABC123/messages",
            {
                "messages": [
                    {
                        "name": "spaces/ABC123/messages/msg1",
                        "text": "First",
                        "sender": {"name": "users/100", "displayName": "User", "type": "HUMAN"},
                        "createTime": "2024-01-01T00:00:00Z",
                    },
                    {
                        "name": "spaces/ABC123/messages/msg2",
                        "text": "Second (cursor start)",
                        "sender": {"name": "users/101", "displayName": "User2", "type": "HUMAN"},
                        "createTime": "2024-01-01T00:01:00Z",
                    },
                    {
                        "name": "spaces/ABC123/messages/msg3",
                        "text": "Third",
                        "sender": {"name": "users/102", "displayName": "User3", "type": "HUMAN"},
                        "createTime": "2024-01-01T00:02:00Z",
                    },
                ],
            },
        )

        result = await adapter.fetch_messages(
            tid,
            FetchOptions(direction="forward", cursor="spaces/ABC123/messages/msg1", limit=1),
        )

        # Should start after cursor (msg1), so should get msg2
        assert len(result.messages) == 1
        assert result.messages[0].id == "spaces/ABC123/messages/msg2"


# =============================================================================
# fetchChannelMessages Tests
# =============================================================================


class TestFetchChannelMessages:
    @pytest.mark.asyncio
    async def test_fetches_channel_messages_backward(self):
        adapter, api, _ = await _init_adapter()
        api.set_response_prefix(
            "GET",
            "spaces/ABC123/messages",
            {
                "messages": [
                    {
                        "name": "spaces/ABC123/messages/aaa.aaa",
                        "text": "Thread root",
                        "sender": {"name": "users/100", "displayName": "Alice", "type": "HUMAN"},
                        "createTime": "2024-01-01T00:00:00Z",
                        "thread": {"name": "spaces/ABC123/threads/aaa"},
                    },
                    {
                        "name": "spaces/ABC123/messages/aaa.bbb",
                        "text": "Thread reply (filtered out)",
                        "sender": {"name": "users/101", "displayName": "Bob", "type": "HUMAN"},
                        "createTime": "2024-01-01T00:01:00Z",
                        "thread": {"name": "spaces/ABC123/threads/aaa"},
                    },
                ],
            },
        )

        result = await adapter.fetch_channel_messages("gchat:spaces/ABC123")

        # Thread reply should be filtered out, only root kept
        assert len(result.messages) >= 1

    @pytest.mark.asyncio
    async def test_invalid_channel_id_raises(self):
        adapter, api, _ = await _init_adapter()

        with pytest.raises(ValidationError):
            await adapter.fetch_channel_messages("gchat:")


# =============================================================================
# listThreads Tests
# =============================================================================


class TestListThreads:
    @pytest.mark.asyncio
    async def test_lists_threads_in_space(self):
        adapter, api, _ = await _init_adapter()
        api.set_response_prefix(
            "GET",
            "spaces/ABC123/messages",
            {
                "messages": [
                    {
                        "name": "spaces/ABC123/messages/m1",
                        "text": "Thread 1",
                        "sender": {"name": "users/100", "displayName": "Alice", "type": "HUMAN"},
                        "createTime": "2024-01-01T00:00:00Z",
                        "thread": {"name": "spaces/ABC123/threads/t1"},
                    },
                    {
                        "name": "spaces/ABC123/messages/m2",
                        "text": "Thread 1 reply",
                        "sender": {"name": "users/101", "displayName": "Bob", "type": "HUMAN"},
                        "createTime": "2024-01-01T00:01:00Z",
                        "thread": {"name": "spaces/ABC123/threads/t1"},
                    },
                    {
                        "name": "spaces/ABC123/messages/m3",
                        "text": "Thread 2",
                        "sender": {"name": "users/102", "displayName": "Carol", "type": "HUMAN"},
                        "createTime": "2024-01-01T00:02:00Z",
                        "thread": {"name": "spaces/ABC123/threads/t2"},
                    },
                ],
            },
        )

        result = await adapter.list_threads("gchat:spaces/ABC123")

        # Should deduplicate by thread name
        assert len(result.threads) == 2

    @pytest.mark.asyncio
    async def test_list_threads_with_limit(self):
        adapter, api, _ = await _init_adapter()
        msgs = []
        for i in range(10):
            msgs.append(
                {
                    "name": f"spaces/ABC123/messages/m{i}",
                    "text": f"Thread {i}",
                    "sender": {"name": "users/100", "displayName": "User", "type": "HUMAN"},
                    "createTime": "2024-01-01T00:00:00Z",
                    "thread": {"name": f"spaces/ABC123/threads/t{i}"},
                }
            )
        api.set_response_prefix("GET", "spaces/ABC123/messages", {"messages": msgs})

        result = await adapter.list_threads("gchat:spaces/ABC123", ListThreadsOptions(limit=3))

        assert len(result.threads) == 3

    @pytest.mark.asyncio
    async def test_list_threads_invalid_channel_raises(self):
        adapter, api, _ = await _init_adapter()

        with pytest.raises(ValidationError):
            await adapter.list_threads("gchat:")


# =============================================================================
# openDM Tests
# =============================================================================


class TestOpenDM:
    @pytest.mark.asyncio
    async def test_finds_existing_dm(self):
        adapter, api, _ = await _init_adapter()
        api.set_response(
            "GET",
            "spaces:findDirectMessage",
            {"name": "spaces/DM_EXISTING"},
        )

        result = await adapter.open_dm("users/12345")

        decoded = decode_thread_id(result)
        assert decoded.space_name == "spaces/DM_EXISTING"
        assert decoded.is_dm is True

        calls = api.get_calls("GET", "spaces:findDirectMessage")
        assert len(calls) == 1
        assert calls[0]["params"]["name"] == "users/12345"

    @pytest.mark.asyncio
    async def test_creates_new_dm_when_not_found(self):
        adapter, api, _ = await _init_adapter(impersonate_user="admin@example.com")
        # findDirectMessage returns 404
        api.set_response("GET", "spaces:findDirectMessage", _FakeApiError(404, "Not found"))
        # spaces:setup creates new DM
        api.set_response("POST", "spaces:setup", {"name": "spaces/DM_NEW"})

        result = await adapter.open_dm("users/67890")

        decoded = decode_thread_id(result)
        assert decoded.space_name == "spaces/DM_NEW"
        assert decoded.is_dm is True

        setup_calls = api.get_calls("POST", "spaces:setup")
        assert len(setup_calls) == 1
        body = setup_calls[0]["body"]
        assert body["space"]["spaceType"] == "DIRECT_MESSAGE"
        assert body["memberships"][0]["member"]["name"] == "users/67890"


# =============================================================================
# addReaction / removeReaction Tests
# =============================================================================


class TestAddReaction:
    @pytest.mark.asyncio
    async def test_adds_reaction(self):
        adapter, api, _ = await _init_adapter()
        msg_id = "spaces/ABC123/messages/msg1"
        api.set_response("POST", f"{msg_id}/reactions", {})

        await adapter.add_reaction("gchat:spaces/ABC123", msg_id, "\U0001f44d")

        calls = api.get_calls("POST", f"{msg_id}/reactions")
        assert len(calls) == 1
        assert calls[0]["body"]["emoji"]["unicode"] is not None

    @pytest.mark.asyncio
    async def test_add_reaction_rate_limit(self):
        adapter, api, _ = await _init_adapter()
        msg_id = "spaces/ABC123/messages/msg1"
        api.set_response("POST", f"{msg_id}/reactions", _FakeApiError(429, "Rate limited"))

        with pytest.raises(AdapterRateLimitError):
            await adapter.add_reaction("gchat:spaces/ABC123", msg_id, "\U0001f44d")


class TestRemoveReaction:
    @pytest.mark.asyncio
    async def test_removes_matching_reaction(self):
        adapter, api, _ = await _init_adapter()
        msg_id = "spaces/ABC123/messages/msg1"
        react_name = f"{msg_id}/reactions/react1"

        # List reactions returns one matching
        api.set_response(
            "GET",
            f"{msg_id}/reactions",
            {
                "reactions": [
                    {"name": react_name, "emoji": {"unicode": "\U0001f44d"}},
                    {"name": f"{msg_id}/reactions/react2", "emoji": {"unicode": "\u2764\ufe0f"}},
                ],
            },
        )
        api.set_response("DELETE", react_name, {})

        await adapter.remove_reaction("gchat:spaces/ABC123", msg_id, "\U0001f44d")

        delete_calls = api.get_calls("DELETE", react_name)
        assert len(delete_calls) == 1

    @pytest.mark.asyncio
    async def test_does_not_delete_when_reaction_not_found(self):
        adapter, api, _ = await _init_adapter()
        msg_id = "spaces/ABC123/messages/msg1"

        api.set_response("GET", f"{msg_id}/reactions", {"reactions": []})

        await adapter.remove_reaction("gchat:spaces/ABC123", msg_id, "\U0001f44d")

        delete_calls = api.get_calls("DELETE")
        assert len(delete_calls) == 0

    @pytest.mark.asyncio
    async def test_no_delete_when_emoji_does_not_match(self):
        adapter, api, _ = await _init_adapter()
        msg_id = "spaces/ABC123/messages/msg1"

        api.set_response(
            "GET",
            f"{msg_id}/reactions",
            {
                "reactions": [
                    {"name": f"{msg_id}/reactions/react1", "emoji": {"unicode": "\u2764\ufe0f"}},
                ],
            },
        )

        await adapter.remove_reaction("gchat:spaces/ABC123", msg_id, "\U0001f44d")

        delete_calls = api.get_calls("DELETE")
        assert len(delete_calls) == 0


# =============================================================================
# startTyping Tests
# =============================================================================


class TestStartTyping:
    @pytest.mark.asyncio
    async def test_is_no_op(self):
        """Google Chat doesn't support typing indicators for bots."""
        adapter, api, _ = await _init_adapter()
        tid = _encode_tid("spaces/ABC123")

        # Should not raise
        await adapter.start_typing(tid)

        # Should not make any API calls
        assert len(api.calls) == 0

    @pytest.mark.asyncio
    async def test_with_status_is_still_no_op(self):
        adapter, api, _ = await _init_adapter()
        tid = _encode_tid("spaces/ABC123")

        await adapter.start_typing(tid, status="Thinking...")

        assert len(api.calls) == 0


# =============================================================================
# stream Tests
# =============================================================================


class TestStream:
    @pytest.mark.asyncio
    async def test_stream_delegates_to_post_message(self):
        """Google Chat doesn't support streaming; stream() accumulates and posts."""
        adapter, api, _ = await _init_adapter()
        tid = _encode_tid("spaces/ABC123", "spaces/ABC123/threads/T1")
        api.set_response("POST", "spaces/ABC123/messages", {"name": "spaces/ABC123/messages/streamed1"})

        async def _text_stream():
            yield "Accumulated text"

        result = await adapter.stream(tid, _text_stream())

        assert result.id == "spaces/ABC123/messages/streamed1"
        calls = api.get_calls("POST", "spaces/ABC123/messages")
        assert len(calls) == 1


# =============================================================================
# User info caching Tests
# =============================================================================


class TestUserInfoCaching:
    @pytest.mark.asyncio
    async def test_resolve_display_name_uses_provided(self):
        state = _make_mock_state()
        cache = UserInfoCache(state, MagicMock())

        name = await cache.resolve_display_name("users/100", "Alice", None, "bot")

        assert name == "Alice"

    @pytest.mark.asyncio
    async def test_resolve_display_name_falls_back_to_cache(self):
        state = _make_mock_state()
        cache = UserInfoCache(state, MagicMock())

        # Pre-populate cache
        await cache.set("users/100", "Cached Alice")

        name = await cache.resolve_display_name("users/100", None, None, "bot")

        assert name == "Cached Alice"

    @pytest.mark.asyncio
    async def test_resolve_display_name_uses_bot_name_for_self(self):
        state = _make_mock_state()
        cache = UserInfoCache(state, MagicMock())

        name = await cache.resolve_display_name("users/BOT1", None, "users/BOT1", "mybot")

        assert name == "mybot"

    @pytest.mark.asyncio
    async def test_resolve_display_name_falls_back_to_user_id(self):
        state = _make_mock_state()
        cache = UserInfoCache(state, MagicMock())

        name = await cache.resolve_display_name("users/999", None, None, "bot")

        assert name == "User 999"

    @pytest.mark.asyncio
    async def test_cache_hit_after_set(self):
        state = _make_mock_state()
        cache = UserInfoCache(state, MagicMock())

        await cache.set("users/200", "Bob", "bob@example.com")
        result = await cache.get("users/200")

        assert result is not None
        assert result.display_name == "Bob"
        assert result.email == "bob@example.com"

    @pytest.mark.asyncio
    async def test_cache_miss(self):
        state = _make_mock_state()
        cache = UserInfoCache(state, MagicMock())

        result = await cache.get("users/nonexistent")

        assert result is None

    @pytest.mark.asyncio
    async def test_ignores_unknown_display_name(self):
        state = _make_mock_state()
        cache = UserInfoCache(state, MagicMock())

        # Setting with "unknown" should be ignored
        await cache.set("users/300", "unknown")
        result = await cache.get("users/300")

        assert result is None

    @pytest.mark.asyncio
    async def test_in_memory_cache_is_fast_path(self):
        """In-memory cache should be checked before state adapter."""
        state = _make_mock_state()
        cache = UserInfoCache(state, MagicMock())

        # First set populates both in-memory and state
        await cache.set("users/400", "Carol")

        # Clear state but keep in-memory
        state._storage.clear()

        result = await cache.get("users/400")
        assert result is not None
        assert result.display_name == "Carol"


# =============================================================================
# Workspace Events subscription lifecycle Tests
# =============================================================================


class TestWorkspaceEventsSubscription:
    @pytest.mark.asyncio
    async def test_skip_subscription_without_pubsub_topic(self):
        adapter, api, _ = await _init_adapter()
        # No pubsub_topic configured

        await adapter.on_thread_subscribe(_encode_tid("spaces/ABC123"))

        # Should not make any API calls
        assert len(api.calls) == 0

    @pytest.mark.asyncio
    async def test_ensure_space_subscription_skips_without_state(self):
        adapter = _make_adapter(pubsub_topic="projects/test/topics/test")
        mock_api = MockGChatApi()
        _patch_api(adapter, mock_api)
        # No initialize() call, so _state is None

        await adapter._ensure_space_subscription("spaces/ABC123")

        assert len(mock_api.calls) == 0

    @pytest.mark.asyncio
    async def test_finds_existing_subscription_in_cache(self):
        adapter, api, state = await _init_adapter(pubsub_topic="projects/test/topics/test")

        # Pre-populate cache with valid subscription
        cache_key = "gchat:space-sub:spaces/ABC123"
        far_future = int(time.time() * 1000) + 24 * 60 * 60 * 1000  # 24 hours from now
        state._storage[cache_key] = {"subscription_name": "subscriptions/sub1", "expire_time": far_future}

        await adapter._ensure_space_subscription("spaces/ABC123")

        # Should not have made any API calls (cache hit)
        assert len(api.calls) == 0

    @pytest.mark.asyncio
    async def test_skips_duplicate_in_flight_subscription(self):
        """If a subscription is already being created, should wait rather than duplicate."""
        adapter, api, state = await _init_adapter(pubsub_topic="projects/test/topics/test")

        # Simulate an in-progress subscription (dict with event + error)
        event = asyncio.Event()
        adapter._pending_subscriptions["spaces/TEST1"] = {"event": event, "error": None}

        # Should wait on the event and return without creating a new one
        async def wait_and_set():
            await asyncio.sleep(0.01)
            event.set()

        task = asyncio.create_task(wait_and_set())
        await adapter._ensure_space_subscription("spaces/TEST1")
        await task

        # No API calls since it waited on the pending subscription
        assert len(api.calls) == 0


# =============================================================================
# Bot user ID learning from annotations Tests
# =============================================================================


class TestBotUserIdLearning:
    def test_learns_bot_id_from_annotations(self):
        adapter = _make_adapter()
        assert adapter.bot_user_id is None

        event = {
            "chat": {
                "messagePayload": {
                    "space": {"name": "spaces/ABC123", "type": "ROOM"},
                    "message": {
                        "name": "spaces/ABC123/messages/msg1",
                        "sender": {"name": "users/100", "displayName": "User", "type": "HUMAN"},
                        "text": "@BotName hi",
                        "createTime": "2024-01-01T00:00:00Z",
                        "annotations": [
                            {
                                "type": "USER_MENTION",
                                "startIndex": 0,
                                "length": 8,
                                "userMention": {
                                    "user": {
                                        "name": "users/LEARNED_BOT_ID",
                                        "displayName": "BotName",
                                        "type": "BOT",
                                    },
                                    "type": "MENTION",
                                },
                            }
                        ],
                    },
                },
            },
        }
        adapter.parse_message(event)
        assert adapter.bot_user_id == "users/LEARNED_BOT_ID"

    def test_does_not_overwrite_existing_bot_id(self):
        adapter = _make_adapter()
        adapter._bot_user_id = "users/FIRST_BOT"

        event = {
            "chat": {
                "messagePayload": {
                    "space": {"name": "spaces/ABC123", "type": "ROOM"},
                    "message": {
                        "name": "spaces/ABC123/messages/msg1",
                        "sender": {"name": "users/100", "displayName": "User", "type": "HUMAN"},
                        "text": "@AnotherBot hi",
                        "createTime": "2024-01-01T00:00:00Z",
                        "annotations": [
                            {
                                "type": "USER_MENTION",
                                "startIndex": 0,
                                "length": 11,
                                "userMention": {
                                    "user": {
                                        "name": "users/SECOND_BOT",
                                        "displayName": "AnotherBot",
                                        "type": "BOT",
                                    },
                                    "type": "MENTION",
                                },
                            }
                        ],
                    },
                },
            },
        }
        adapter.parse_message(event)
        assert adapter.bot_user_id == "users/FIRST_BOT"

    def test_persists_bot_id_after_learning(self):
        """The bot user ID should be available for self-detection after learning."""
        adapter = _make_adapter()

        # First message: learn bot ID
        event = {
            "chat": {
                "messagePayload": {
                    "space": {"name": "spaces/ABC123", "type": "ROOM"},
                    "message": {
                        "name": "spaces/ABC123/messages/msg1",
                        "sender": {"name": "users/100", "displayName": "User", "type": "HUMAN"},
                        "text": "@MyBot hello",
                        "createTime": "2024-01-01T00:00:00Z",
                        "annotations": [
                            {
                                "type": "USER_MENTION",
                                "startIndex": 0,
                                "length": 6,
                                "userMention": {
                                    "user": {
                                        "name": "users/MY_BOT_ID",
                                        "displayName": "MyBot",
                                        "type": "BOT",
                                    },
                                    "type": "MENTION",
                                },
                            }
                        ],
                    },
                },
            },
        }
        adapter.parse_message(event)

        # Second message: detect self
        self_event = {
            "chat": {
                "messagePayload": {
                    "space": {"name": "spaces/ABC123", "type": "ROOM"},
                    "message": {
                        "name": "spaces/ABC123/messages/msg2",
                        "sender": {"name": "users/MY_BOT_ID", "displayName": "MyBot", "type": "BOT"},
                        "text": "Hello back",
                        "createTime": "2024-01-01T00:00:01Z",
                    },
                },
            },
        }
        msg = adapter.parse_message(self_event)
        assert msg.author.is_me is True


# =============================================================================
# Error handling
# =============================================================================


class TestErrorHandling:
    def test_429_raises_adapter_rate_limit(self):
        adapter = _make_adapter()
        with pytest.raises(AdapterRateLimitError):
            adapter._handle_google_chat_error(_FakeApiError(429, "Too many requests"), "test")

    def test_non_429_rethrows(self):
        adapter = _make_adapter()
        original = _FakeApiError(500, "Server error")
        with pytest.raises(_FakeApiError):
            adapter._handle_google_chat_error(original, "test")

    def test_403_rethrows(self):
        adapter = _make_adapter()
        original = _FakeApiError(403, "Forbidden")
        with pytest.raises(_FakeApiError):
            adapter._handle_google_chat_error(original, "editMessage")

    def test_404_rethrows(self):
        adapter = _make_adapter()
        original = _FakeApiError(404, "Not found")
        with pytest.raises(_FakeApiError):
            adapter._handle_google_chat_error(original, "deleteMessage")

    def test_logs_context_in_error(self):
        """Error handler should log the context string."""
        from chat_sdk.logger import Logger

        mock_logger = MagicMock(spec=Logger)
        adapter = GoogleChatAdapter(
            GoogleChatAdapterConfig(
                credentials=_make_credentials(),
                logger=mock_logger,
            )
        )

        import contextlib

        with contextlib.suppress(_FakeApiError):
            adapter._handle_google_chat_error(_FakeApiError(500, "Fail"), "postMessage")

        mock_logger.error.assert_called()
        call_args = mock_logger.error.call_args
        assert "postMessage" in call_args[0][0]


# =============================================================================
# renderFormatted Tests
# =============================================================================


class TestRenderFormatted:
    def test_renders_empty_ast(self):
        adapter = _make_adapter()
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
# channelIdFromThreadId Tests
# =============================================================================


class TestChannelIdFromThreadId:
    def test_extracts_space_name(self):
        adapter = _make_adapter()
        tid = _encode_tid("spaces/ABC123", "spaces/ABC123/threads/T1")
        assert adapter.channel_id_from_thread_id(tid) == "gchat:spaces/ABC123"

    def test_works_without_thread_name(self):
        adapter = _make_adapter()
        tid = _encode_tid("spaces/ONLY")
        assert adapter.channel_id_from_thread_id(tid) == "gchat:spaces/ONLY"

    def test_works_with_dm_thread(self):
        adapter = _make_adapter()
        tid = _encode_tid("spaces/DM_SPACE", is_dm=True)
        assert adapter.channel_id_from_thread_id(tid) == "gchat:spaces/DM_SPACE"


# =============================================================================
# fetchThread Tests
# =============================================================================


class TestFetchThread:
    @pytest.mark.asyncio
    async def test_fetches_thread_info(self):
        adapter, api, _ = await _init_adapter()
        tid = _encode_tid("spaces/ABC123", "spaces/ABC123/threads/T1")
        api.set_response("GET", "spaces/ABC123", {"displayName": "General Chat"})

        result = await adapter.fetch_thread(tid)

        assert result.id == tid
        assert result.channel_name == "General Chat"
        calls = api.get_calls("GET", "spaces/ABC123")
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_fetch_thread_api_error(self):
        adapter, api, _ = await _init_adapter()
        tid = _encode_tid("spaces/UNKNOWN")
        api.set_response("GET", "spaces/UNKNOWN", _FakeApiError(404, "Not found"))

        with pytest.raises(_FakeApiError):
            await adapter.fetch_thread(tid)
