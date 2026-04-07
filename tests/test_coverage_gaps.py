"""Tests covering previously-untested adapter methods identified by deep audit.

Tests:
1. github.add_reaction
2. github.remove_reaction
3. github.open_dm (BaseAdapter default -> ChatNotImplementedError)
4. telegram.fetch_messages
5. telegram.open_dm
6. teams.fetch_messages
7. teams.open_dm
8. linear.stream
9. linear.open_dm (not implemented -> ChatNotImplementedError)
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chat_sdk.adapters.github.adapter import GitHubAdapter
from chat_sdk.adapters.github.types import GitHubThreadId
from chat_sdk.adapters.linear.adapter import LinearAdapter
from chat_sdk.adapters.linear.types import LinearAdapterAPIKeyConfig
from chat_sdk.adapters.teams.adapter import TeamsAdapter
from chat_sdk.adapters.teams.types import TeamsAdapterConfig, TeamsThreadId
from chat_sdk.adapters.telegram.adapter import TelegramAdapter
from chat_sdk.adapters.telegram.types import TelegramAdapterConfig, TelegramThreadId
from chat_sdk.errors import ChatNotImplementedError
from chat_sdk.logger import ConsoleLogger
from chat_sdk.types import (
    Author,
    FetchOptions,
    Message,
    MessageMetadata,
    RawMessage,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_github_adapter(**overrides) -> GitHubAdapter:
    defaults = {
        "webhook_secret": "test-secret",
        "token": "ghp_testtoken",
        "logger": ConsoleLogger("error"),
    }
    defaults.update(overrides)
    return GitHubAdapter(defaults)


def _make_telegram_adapter(**overrides) -> TelegramAdapter:
    config = TelegramAdapterConfig(
        bot_token=overrides.pop("bot_token", "123456:ABC-DEF"),
        logger=overrides.pop("logger", ConsoleLogger("error")),
        **overrides,
    )
    return TelegramAdapter(config)


def _make_teams_adapter(**overrides) -> TeamsAdapter:
    config = TeamsAdapterConfig(
        app_id=overrides.pop("app_id", "test-app-id"),
        app_password=overrides.pop("app_password", "test-app-password"),
        app_tenant_id=overrides.pop("app_tenant_id", "test-tenant-id"),
        logger=overrides.pop("logger", ConsoleLogger("error")),
    )
    return TeamsAdapter(config)


def _make_linear_adapter(**overrides) -> LinearAdapter:
    config = LinearAdapterAPIKeyConfig(
        api_key=overrides.pop("api_key", "test-api-key"),
        webhook_secret=overrides.pop("webhook_secret", "test-secret"),
        user_name=overrides.pop("user_name", "test-bot"),
        logger=overrides.pop("logger", _make_mock_logger()),
    )
    return LinearAdapter(config)


def _make_mock_logger():
    return MagicMock(
        debug=MagicMock(),
        info=MagicMock(),
        warn=MagicMock(),
        error=MagicMock(),
    )


def _make_message(msg_id: str, thread_id: str, text: str = "hello") -> Message:
    return Message(
        id=msg_id,
        thread_id=thread_id,
        text=text,
        formatted=None,
        raw={},
        author=Author(
            user_id="u1",
            user_name="alice",
            full_name="Alice",
            is_bot=False,
            is_me=False,
        ),
        metadata=MessageMetadata(
            date_sent=datetime.now(timezone.utc),
        ),
        attachments=[],
    )


# ===========================================================================
# 1. GitHub add_reaction
# ===========================================================================


class TestGitHubAddReaction:
    @pytest.mark.asyncio
    async def test_add_reaction_pr_level(self):
        adapter = _make_github_adapter()
        adapter._github_api_request = AsyncMock(return_value=None)

        thread_id = adapter.encode_thread_id(GitHubThreadId(owner="octocat", repo="hello-world", pr_number=42))

        await adapter.add_reaction(thread_id, "100", "thumbs_up")

        adapter._github_api_request.assert_called_once_with(
            "POST",
            "/repos/octocat/hello-world/issues/comments/100/reactions",
            {"content": "+1"},
        )

    @pytest.mark.asyncio
    async def test_add_reaction_review_comment(self):
        adapter = _make_github_adapter()
        adapter._github_api_request = AsyncMock(return_value=None)

        thread_id = adapter.encode_thread_id(
            GitHubThreadId(
                owner="octocat",
                repo="hello-world",
                pr_number=42,
                review_comment_id=999,
            )
        )

        await adapter.add_reaction(thread_id, "200", "heart")

        adapter._github_api_request.assert_called_once_with(
            "POST",
            "/repos/octocat/hello-world/pulls/comments/200/reactions",
            {"content": "heart"},
        )


# ===========================================================================
# 2. GitHub remove_reaction
# ===========================================================================


class TestGitHubRemoveReaction:
    @pytest.mark.asyncio
    async def test_remove_reaction_pr_level(self):
        adapter = _make_github_adapter(bot_user_id=42)

        # Mock: list reactions returns one matching reaction
        adapter._github_api_request = AsyncMock(
            side_effect=[
                # GET reactions list
                [{"id": 777, "content": "+1", "user": {"id": 42}}],
                # DELETE reaction
                None,
            ]
        )

        thread_id = adapter.encode_thread_id(GitHubThreadId(owner="octocat", repo="hello-world", pr_number=10))

        await adapter.remove_reaction(thread_id, "100", "thumbs_up")

        assert adapter._github_api_request.call_count == 2
        # First call: GET reactions
        assert adapter._github_api_request.call_args_list[0].args == (
            "GET",
            "/repos/octocat/hello-world/issues/comments/100/reactions",
        )
        # Second call: DELETE reaction 777
        assert adapter._github_api_request.call_args_list[1].args == (
            "DELETE",
            "/repos/octocat/hello-world/issues/comments/100/reactions/777",
        )

    @pytest.mark.asyncio
    async def test_remove_reaction_no_matching_reaction(self):
        adapter = _make_github_adapter(bot_user_id=42)

        # Reactions list has no match for the bot user
        adapter._github_api_request = AsyncMock(return_value=[{"id": 777, "content": "+1", "user": {"id": 999}}])

        thread_id = adapter.encode_thread_id(GitHubThreadId(owner="octocat", repo="hello-world", pr_number=10))

        # Should not raise; simply does nothing when no matching reaction found
        await adapter.remove_reaction(thread_id, "100", "thumbs_up")

        # Only the GET call; no DELETE since no match
        assert adapter._github_api_request.call_count == 1

    @pytest.mark.asyncio
    async def test_remove_reaction_fetches_bot_user_id_if_missing(self):
        adapter = _make_github_adapter()
        adapter._bot_user_id = None

        adapter._github_api_request = AsyncMock(
            side_effect=[
                # GET /user to detect bot
                {"id": 42},
                # GET reactions list
                [{"id": 777, "content": "+1", "user": {"id": 42}}],
                # DELETE reaction
                None,
            ]
        )

        thread_id = adapter.encode_thread_id(GitHubThreadId(owner="octocat", repo="hello-world", pr_number=10))

        await adapter.remove_reaction(thread_id, "100", "thumbs_up")

        # First call fetches /user
        assert adapter._github_api_request.call_args_list[0].args == ("GET", "/user")
        assert adapter._bot_user_id == 42


# ===========================================================================
# 3. GitHub open_dm (not implemented)
# ===========================================================================


class TestGitHubOpenDM:
    @pytest.mark.asyncio
    async def test_github_has_no_open_dm(self):
        """GitHub adapter does not implement open_dm, verifying the method is absent."""
        adapter = _make_github_adapter()
        assert not hasattr(adapter, "open_dm"), (
            "GitHubAdapter should not implement open_dm since GitHub doesn't support DMs"
        )


# ===========================================================================
# 4. Telegram fetch_messages
# ===========================================================================


class TestTelegramFetchMessages:
    @pytest.mark.asyncio
    async def test_returns_cached_messages(self):
        adapter = _make_telegram_adapter()
        thread_id = adapter.encode_thread_id(TelegramThreadId(chat_id="12345"))

        msg1 = _make_message("1", thread_id, "first")
        msg2 = _make_message("2", thread_id, "second")

        adapter._message_cache[thread_id] = [msg1, msg2]

        result = await adapter.fetch_messages(thread_id)

        assert len(result.messages) == 2
        texts = [m.text for m in result.messages]
        assert "first" in texts
        assert "second" in texts

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_cache(self):
        adapter = _make_telegram_adapter()
        thread_id = adapter.encode_thread_id(TelegramThreadId(chat_id="99999"))

        result = await adapter.fetch_messages(thread_id)

        assert result.messages == []

    @pytest.mark.asyncio
    async def test_respects_limit_option(self):
        adapter = _make_telegram_adapter()
        thread_id = adapter.encode_thread_id(TelegramThreadId(chat_id="12345"))

        messages = [_make_message(str(i), thread_id, f"msg-{i}") for i in range(10)]
        adapter._message_cache[thread_id] = messages

        result = await adapter.fetch_messages(thread_id, FetchOptions(limit=3))

        assert len(result.messages) == 3


# ===========================================================================
# 5. Telegram open_dm
# ===========================================================================


class TestTelegramOpenDM:
    @pytest.mark.asyncio
    async def test_returns_encoded_thread_id(self):
        adapter = _make_telegram_adapter()

        thread_id = await adapter.open_dm("12345")

        assert thread_id == "telegram:12345"

    @pytest.mark.asyncio
    async def test_round_trips_with_decode(self):
        adapter = _make_telegram_adapter()

        thread_id = await adapter.open_dm("67890")
        decoded = adapter.decode_thread_id(thread_id)

        assert decoded.chat_id == "67890"
        assert decoded.message_thread_id is None


# ===========================================================================
# 6. Teams fetch_messages
# ===========================================================================


class TestTeamsFetchMessages:
    @pytest.mark.asyncio
    async def test_fetch_messages_dm_chat(self):
        adapter = _make_teams_adapter()
        adapter._chat = MagicMock()
        adapter._chat.get_state.return_value = {}

        thread_id = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:test-conv-id@thread.v2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )

        # Mock the Graph API methods
        adapter._get_graph_token = AsyncMock(return_value="fake-token")
        adapter._get_channel_context = AsyncMock(return_value=None)

        fake_graph_messages = [
            {
                "id": "msg-1",
                "body": {"contentType": "text", "content": "Hello"},
                "from": {
                    "user": {"id": "user-1", "displayName": "Alice"},
                },
                "createdDateTime": "2024-01-01T00:00:00Z",
            },
        ]

        adapter._graph_list_chat_messages = AsyncMock(return_value=fake_graph_messages)

        result = await adapter.fetch_messages(thread_id)

        assert len(result.messages) == 1
        adapter._graph_list_chat_messages.assert_called_once()


# ===========================================================================
# 7. Teams open_dm
# ===========================================================================


class TestTeamsOpenDM:
    @pytest.mark.asyncio
    async def test_open_dm_creates_conversation(self):
        adapter = _make_teams_adapter()
        adapter._chat = MagicMock()
        adapter._chat.get_state.return_value = {}

        # Mock the Bot Framework token acquisition
        adapter._get_access_token = AsyncMock(return_value="fake-bot-token")

        # Build a mock aiohttp module with a mock ClientSession
        mock_response = AsyncMock()
        mock_response.ok = True
        mock_response.json = AsyncMock(return_value={"id": "new-conv-id"})

        mock_post_ctx = AsyncMock()
        mock_post_ctx.__aenter__ = AsyncMock(return_value=mock_response)
        mock_post_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_post_ctx)

        mock_session_ctx = MagicMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_aiohttp = MagicMock()
        mock_aiohttp.ClientSession = MagicMock(return_value=mock_session_ctx)

        with patch.dict("sys.modules", {"aiohttp": mock_aiohttp}):
            thread_id = await adapter.open_dm("user-123")

        # Verify the returned thread_id decodes correctly
        decoded = adapter.decode_thread_id(thread_id)
        assert decoded.conversation_id == "new-conv-id"

    @pytest.mark.asyncio
    async def test_open_dm_requires_initialized_chat(self):
        adapter = _make_teams_adapter()
        adapter._chat = None

        with pytest.raises(ChatNotImplementedError):
            await adapter.open_dm("user-123")


# ===========================================================================
# 8. Linear stream
# ===========================================================================


class TestLinearStream:
    @pytest.mark.asyncio
    async def test_stream_accumulates_and_posts(self):
        adapter = _make_linear_adapter()
        adapter._access_token = "test-token"

        # Mock post_message to capture the accumulated text
        posted_raw = RawMessage(id="comment-1", thread_id="linear:issue-1", raw={"text": "hello world"})
        adapter.post_message = AsyncMock(return_value=posted_raw)

        async def text_gen():
            yield "hello "
            yield "world"

        thread_id = "linear:issue-1"
        result = await adapter.stream(thread_id, text_gen())

        adapter.post_message.assert_called_once()
        call_args = adapter.post_message.call_args
        assert call_args.args[0] == thread_id
        assert call_args.args[1]["raw"] == "hello world"
        assert result.id == "comment-1"

    @pytest.mark.asyncio
    async def test_stream_empty_produces_empty_result(self):
        adapter = _make_linear_adapter()
        adapter._access_token = "test-token"
        adapter.post_message = AsyncMock()

        async def empty_gen():
            return
            yield  # noqa: RET504 -- make it an async generator

        thread_id = "linear:issue-1"
        result = await adapter.stream(thread_id, empty_gen())

        adapter.post_message.assert_not_called()
        assert result.id == ""
        assert result.thread_id == thread_id

    @pytest.mark.asyncio
    async def test_stream_handles_dict_chunks(self):
        adapter = _make_linear_adapter()
        adapter._access_token = "test-token"

        posted_raw = RawMessage(id="c-2", thread_id="linear:issue-2", raw={"text": "chunk"})
        adapter.post_message = AsyncMock(return_value=posted_raw)

        async def dict_gen():
            yield {"type": "markdown_text", "text": "chunk"}

        result = await adapter.stream("linear:issue-2", dict_gen())

        adapter.post_message.assert_called_once()
        assert adapter.post_message.call_args.args[1]["raw"] == "chunk"


# ===========================================================================
# 9. Linear open_dm (not implemented)
# ===========================================================================


class TestLinearOpenDM:
    @pytest.mark.asyncio
    async def test_linear_has_no_open_dm(self):
        """Linear adapter does not implement open_dm since Linear doesn't support DMs."""
        adapter = _make_linear_adapter()
        assert not hasattr(adapter, "open_dm"), (
            "LinearAdapter should not implement open_dm since Linear doesn't support DMs"
        )
