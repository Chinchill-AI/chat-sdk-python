"""Tests for production bug fixes that lacked coverage.

Covers:
1. Token refresh race conditions (double-check-after-lock pattern)
2. HTTP session reuse across API calls
3. Task cancellation on shutdown
6. _fallback_stream intermediate edits use StreamingMarkdownRenderer
7. OnLockConflict callback returning "drop" string
9. Slack installation camelCase key reading
10. Cache size limits (Discord thread_parent_cache, GChat UserInfoCache,
    GitHub _installation_token_cache)
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chat_sdk.chat import Chat
from chat_sdk.errors import LockError
from chat_sdk.testing import (
    MockAdapter,
    MockLogger,
    MockStateAdapter,
    create_mock_adapter,
    create_mock_state,
    create_test_message,
)
from chat_sdk.thread import ThreadImpl, _ThreadImplConfig
from chat_sdk.types import (
    ChatConfig,
    PostableMarkdown,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_chat(
    adapter: MockAdapter | None = None,
    state: MockStateAdapter | None = None,
    **overrides: Any,
) -> tuple[Chat, MockAdapter, MockStateAdapter]:
    adapter = adapter or create_mock_adapter("slack")
    state = state or create_mock_state()
    config = ChatConfig(
        user_name="testbot",
        adapters={"slack": adapter},
        state=state,
        logger=MockLogger(),
        **overrides,
    )
    return Chat(config), adapter, state


async def _init_chat(
    adapter: MockAdapter | None = None,
    state: MockStateAdapter | None = None,
    **overrides: Any,
) -> tuple[Chat, MockAdapter, MockStateAdapter]:
    chat, adapter, state = _make_chat(adapter, state, **overrides)
    await chat.webhooks["slack"]("request")
    return chat, adapter, state


def _make_thread(
    adapter: MockAdapter | None = None,
    state: MockStateAdapter | None = None,
    *,
    thread_id: str = "slack:C123:1234.5678",
    fallback_streaming_placeholder_text: str | None = "...",
    streaming_update_interval_ms: int = 500,
    **kwargs: Any,
) -> ThreadImpl:
    adapter = adapter or create_mock_adapter()
    state = state or create_mock_state()
    return ThreadImpl(
        _ThreadImplConfig(
            id=thread_id,
            adapter=adapter,
            state_adapter=state,
            channel_id="C123",
            fallback_streaming_placeholder_text=fallback_streaming_placeholder_text,
            streaming_update_interval_ms=streaming_update_interval_ms,
            **kwargs,
        )
    )


# ===========================================================================
# 1. Token refresh race conditions
# ===========================================================================


class TestTokenRefreshRaceCondition:
    """Verify concurrent token refreshes don't issue duplicate HTTP requests.

    All adapters (GChat, Teams, GitHub) use the double-check-after-lock
    pattern: check cache, acquire asyncio.Lock, check cache again, then
    refresh.  This test exercises the pattern in the GChat adapter.
    """

    @pytest.mark.asyncio
    async def test_concurrent_refreshes_only_issue_one_http_request(self):
        """Two concurrent _get_access_token calls should only trigger one refresh."""
        from chat_sdk.adapters.google_chat.adapter import GoogleChatAdapter

        with patch.dict("os.environ", {"GOOGLE_CHAT_CREDENTIALS": '{"client_email":"a@b.iam","private_key":"fake"}'}):
            adapter = GoogleChatAdapter()

        call_count = 0

        async def _mock_get_sa_token(creds, scopes, subject=None):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.05)  # simulate network latency
            return "fresh-token"

        adapter._get_service_account_token = _mock_get_sa_token  # type: ignore[assignment]
        adapter._access_token = None
        adapter._access_token_expires = 0

        # Launch two concurrent calls
        results = await asyncio.gather(
            adapter._get_access_token(),
            adapter._get_access_token(),
        )

        # Both should get the same token
        assert results[0] == "fresh-token"
        assert results[1] == "fresh-token"
        # Only one HTTP call should have been made
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_github_concurrent_token_refreshes_single_request(self):
        """Two concurrent GitHub _get_installation_token calls should only refresh once."""
        from chat_sdk.adapters.github.adapter import GitHubAdapter

        adapter = GitHubAdapter({"webhook_secret": "test-secret", "token": "pat-123"})

        call_count = 0

        # Create a mock session
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(
            return_value={
                "token": "ghs-test-token",
                "expires_at": "2099-01-01T00:00:00Z",
            }
        )
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()

        def _mock_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return mock_response

        mock_session.post = _mock_post
        mock_session.closed = False

        adapter._http_session = mock_session
        adapter._app_credentials = {"app_id": "123", "private_key": "fake-key"}
        adapter._installation_id = 42
        adapter._installation_token_cache = {}

        # Mock JWT generation
        adapter._generate_app_jwt = lambda: "fake-jwt"  # type: ignore[assignment]

        results = await asyncio.gather(
            adapter._get_installation_token(42),
            adapter._get_installation_token(42),
        )

        assert results[0] == "ghs-test-token"
        assert results[1] == "ghs-test-token"
        assert call_count == 1


# ===========================================================================
# 2. Session reuse
# ===========================================================================


class TestSessionReuse:
    """Verify adapters reuse their HTTP session across multiple API calls."""

    @pytest.mark.asyncio
    async def test_github_reuses_http_session(self):
        """GitHub adapter should return the same session object on repeated calls."""
        from chat_sdk.adapters.github.adapter import GitHubAdapter

        adapter = GitHubAdapter({"webhook_secret": "test-secret", "token": "pat-123"})
        session1 = await adapter._get_http_session()
        session2 = await adapter._get_http_session()
        assert session1 is session2
        await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_teams_reuses_http_session(self):
        """Teams adapter should return the same session object on repeated calls."""
        from chat_sdk.adapters.teams.adapter import TeamsAdapter
        from chat_sdk.adapters.teams.types import TeamsAdapterConfig

        adapter = TeamsAdapter(
            TeamsAdapterConfig(
                app_id="test-app",
                app_password="test-pw",
            )
        )
        session1 = await adapter._get_http_session()
        session2 = await adapter._get_http_session()
        assert session1 is session2
        await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_discord_reuses_http_session(self):
        """Discord adapter should return the same session object on repeated calls."""
        from chat_sdk.adapters.discord.adapter import DiscordAdapter
        from chat_sdk.adapters.discord.types import DiscordAdapterConfig

        adapter = DiscordAdapter(
            DiscordAdapterConfig(
                bot_token="test-bot-token",
                public_key="a" * 64,
                application_id="test-app-id",
            )
        )
        session1 = await adapter._get_http_session()
        session2 = await adapter._get_http_session()
        assert session1 is session2
        await adapter.disconnect()


# ===========================================================================
# 3. Task cancellation on shutdown
# ===========================================================================


class TestShutdownCancelsHandlerTasks:
    """Verify Chat.shutdown() cancels in-flight handler tasks."""

    @pytest.mark.asyncio
    async def test_shutdown_cancels_active_handler_tasks(self):
        chat, adapter, state = await _init_chat()

        handler_started = asyncio.Event()
        handler_cancelled = False

        @chat.on_mention
        async def handler(thread, message, context=None):
            handler_started.set()
            try:
                await asyncio.sleep(999)  # simulate long-running handler
            except asyncio.CancelledError:
                nonlocal handler_cancelled
                handler_cancelled = True
                raise

        # Dispatch a message so the handler starts running as a task
        msg = create_test_message("msg-cancel-1", "Hey @slack-bot")
        chat.process_message(adapter, "slack:C123:1234.5678", msg)

        # Wait for handler to start
        await asyncio.wait_for(handler_started.wait(), timeout=2.0)
        assert len(chat._active_tasks) >= 1

        # Shutdown should cancel the task
        await chat.shutdown()
        assert handler_cancelled

    @pytest.mark.asyncio
    async def test_shutdown_clears_active_tasks_set(self):
        chat, adapter, state = await _init_chat()

        handler_started = asyncio.Event()

        @chat.on_mention
        async def handler(thread, message, context=None):
            handler_started.set()
            await asyncio.sleep(999)

        msg = create_test_message("msg-cancel-2", "Hey @slack-bot")
        chat.process_message(adapter, "slack:C123:1234.5678", msg)
        await asyncio.wait_for(handler_started.wait(), timeout=2.0)

        await chat.shutdown()
        # All tasks should have been cleaned up
        assert len(chat._active_tasks) == 0


# ===========================================================================
# 6. _fallback_stream intermediate edits use StreamingMarkdownRenderer
# ===========================================================================


class TestFallbackStreamRendererUsage:
    """Verify intermediate edits in _fallback_stream use get_committable_text."""

    @pytest.mark.asyncio
    async def test_intermediate_edits_use_committable_text(self):
        """Intermediate edits should use renderer.get_committable_text(),
        which holds back incomplete markdown structures."""
        adapter = create_mock_adapter()
        state = create_mock_state()

        async def slow_stream() -> AsyncIterator[str]:
            yield "Hello **wor"
            await asyncio.sleep(0.05)
            yield "ld** done"

        # Use a very short interval so the edit loop fires while stream is paused
        thread = _make_thread(adapter, state, streaming_update_interval_ms=10)
        result = await thread.post(slow_stream())

        # The final text is plain text extracted from the parsed markdown AST
        assert result.text == "Hello world done"

        # There should be at least one intermediate edit (from the edit loop)
        # and the final edit.  Intermediate edits use get_committable_text()
        # which flows through the renderer.
        assert len(adapter._edit_calls) >= 1

        # Final edit should have balanced markdown
        last_edit = adapter._edit_calls[-1]
        final_md = last_edit[2].markdown if isinstance(last_edit[2], PostableMarkdown) else last_edit[2]
        assert final_md.count("**") % 2 == 0


# ===========================================================================
# 7. OnLockConflict callback returning "drop" string
# ===========================================================================


class TestOnLockConflictDropString:
    """Verify that a callback returning 'drop' (string) drops the message."""

    @pytest.mark.asyncio
    async def test_callback_returning_drop_string_drops_message(self):
        """When a callback returns 'drop' (the string literal), the message
        should be dropped and not processed -- same behavior as returning None."""
        state = create_mock_state()
        adapter = create_mock_adapter("slack")

        chat, _, _ = await _init_chat(
            adapter=adapter,
            state=state,
            on_lock_conflict=lambda _tid, _msg: "drop",
        )
        calls: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            calls.append(message.id)

        await state.acquire_lock("slack:C123:1234.5678", 30000)

        msg = create_test_message("msg-lock-drop-str", "Hey @slack-bot")
        with pytest.raises(LockError):
            await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        # Handler should NOT have been called -- "drop" means don't force
        assert len(calls) == 0

    @pytest.mark.asyncio
    async def test_async_callback_returning_drop_string_drops_message(self):
        """Async callback returning 'drop' should also drop the message."""
        state = create_mock_state()
        adapter = create_mock_adapter("slack")

        async def _async_drop(_tid, _msg):
            return "drop"

        chat, _, _ = await _init_chat(
            adapter=adapter,
            state=state,
            on_lock_conflict=_async_drop,
        )
        calls: list[str] = []

        @chat.on_mention
        async def handler(thread, message, context=None):
            calls.append(message.id)

        await state.acquire_lock("slack:C123:1234.5678", 30000)

        msg = create_test_message("msg-lock-async-drop", "Hey @slack-bot")
        with pytest.raises(LockError):
            await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        assert len(calls) == 0
        # Verify force_release_lock was NOT called
        assert "slack:C123:1234.5678" not in state._force_release_lock_calls


# ===========================================================================
# 9. Slack installation camelCase key reading
# ===========================================================================


class TestSlackInstallationCamelCaseKeys:
    """Verify get_installation reads camelCase (TS-format) installation data."""

    @pytest.mark.asyncio
    async def test_reads_camelcase_installation_data(self):
        """get_installation should read 'botToken', 'botUserId', 'teamName'
        (camelCase keys from the TS SDK format)."""
        import sys
        from types import ModuleType

        _fake_slack_sdk = ModuleType("slack_sdk")
        _fake_web = ModuleType("slack_sdk.web")
        _fake_async = ModuleType("slack_sdk.web.async_client")

        class _FakeClient:
            def __init__(self, *, token=""):
                self.token = token

        _fake_async.AsyncWebClient = _FakeClient  # type: ignore[attr-defined]
        _fake_web.async_client = _fake_async  # type: ignore[attr-defined]
        _fake_slack_sdk.web = _fake_web  # type: ignore[attr-defined]
        sys.modules.setdefault("slack_sdk", _fake_slack_sdk)
        sys.modules.setdefault("slack_sdk.web", _fake_web)
        sys.modules.setdefault("slack_sdk.web.async_client", _fake_async)

        from chat_sdk.adapters.slack.adapter import SlackAdapter
        from chat_sdk.adapters.slack.types import SlackAdapterConfig

        # Create a mock state that returns camelCase installation data
        mock_state = create_mock_state()
        await mock_state.set(
            "slack:installation:T_CAMEL",
            {
                "botToken": "xoxb-camel-token",
                "botUserId": "U_CAMEL",
                "teamName": "CamelTeam",
            },
        )

        mock_chat = MagicMock()
        mock_chat.get_state.return_value = mock_state

        adapter = SlackAdapter(SlackAdapterConfig(signing_secret="sec"))
        await adapter.initialize(mock_chat)

        result = await adapter.get_installation("T_CAMEL")
        assert result is not None
        assert result.bot_token == "xoxb-camel-token"
        assert result.bot_user_id == "U_CAMEL"
        assert result.team_name == "CamelTeam"

    @pytest.mark.asyncio
    async def test_reads_snake_case_installation_data(self):
        """get_installation should also accept snake_case keys."""
        import sys
        from types import ModuleType

        _fake_slack_sdk = ModuleType("slack_sdk")
        _fake_web = ModuleType("slack_sdk.web")
        _fake_async = ModuleType("slack_sdk.web.async_client")

        class _FakeClient:
            def __init__(self, *, token=""):
                self.token = token

        _fake_async.AsyncWebClient = _FakeClient  # type: ignore[attr-defined]
        _fake_web.async_client = _fake_async  # type: ignore[attr-defined]
        _fake_slack_sdk.web = _fake_web  # type: ignore[attr-defined]
        sys.modules.setdefault("slack_sdk", _fake_slack_sdk)
        sys.modules.setdefault("slack_sdk.web", _fake_web)
        sys.modules.setdefault("slack_sdk.web.async_client", _fake_async)

        from chat_sdk.adapters.slack.adapter import SlackAdapter
        from chat_sdk.adapters.slack.types import SlackAdapterConfig

        mock_state = create_mock_state()
        await mock_state.set(
            "slack:installation:T_SNAKE",
            {
                "bot_token": "xoxb-snake-token",
                "bot_user_id": "U_SNAKE",
                "team_name": "SnakeTeam",
            },
        )

        mock_chat = MagicMock()
        mock_chat.get_state.return_value = mock_state

        adapter = SlackAdapter(SlackAdapterConfig(signing_secret="sec"))
        await adapter.initialize(mock_chat)

        result = await adapter.get_installation("T_SNAKE")
        assert result is not None
        assert result.bot_token == "xoxb-snake-token"
        assert result.bot_user_id == "U_SNAKE"
        assert result.team_name == "SnakeTeam"


# ===========================================================================
# 10. Cache size limits (Discord, GChat, GitHub)
# ===========================================================================


class TestDiscordThreadParentCacheEviction:
    """Discord thread parent cache should not grow unboundedly."""

    async def test_thread_parent_cache_eviction_on_overflow(self):
        """When cache exceeds 1000 entries during reaction handling, expired entries are purged."""
        from unittest.mock import AsyncMock, MagicMock

        from chat_sdk.adapters.discord.adapter import (
            CHANNEL_TYPE_PUBLIC_THREAD,
            DiscordAdapter,
        )
        from chat_sdk.adapters.discord.types import DiscordAdapterConfig

        adapter = DiscordAdapter(
            DiscordAdapterConfig(
                bot_token="test-token",
                public_key="a" * 64,
                application_id="test-app",
            )
        )
        adapter._chat = MagicMock()
        adapter._bot_user_id = "bot-user"

        # Fill cache with 1001 expired entries
        past = time.time() - 100
        for i in range(1001):
            adapter._thread_parent_cache[f"channel-{i}"] = {
                "parent_id": f"parent-{i}",
                "expires_at": past,
            }
        assert len(adapter._thread_parent_cache) == 1001

        # Trigger the production code path: _handle_forwarded_reaction fetches
        # channel info for threads, which triggers cache insertion + eviction
        adapter._discord_fetch = AsyncMock(return_value={"parent_id": "real-parent"})
        await adapter._handle_forwarded_reaction(
            {
                "channel_id": "new-thread-channel",
                "message_id": "msg-1",
                "user_id": "user-1",
                "emoji": {"name": "👍"},
                "channel_type": CHANNEL_TYPE_PUBLIC_THREAD,
            },
            added=True,
        )

        # After eviction, all 1001 expired entries should be purged,
        # leaving only the newly inserted one
        assert len(adapter._thread_parent_cache) <= 2
        assert "new-thread-channel" in adapter._thread_parent_cache


class TestGChatUserInfoCacheEviction:
    """GChat UserInfoCache should evict oldest entries at max size."""

    @pytest.mark.asyncio
    async def test_evicts_oldest_entries_past_max_size(self):
        from chat_sdk.adapters.google_chat.user_info import UserInfoCache

        logger = MockLogger()
        cache = UserInfoCache(state=None, logger=logger)

        # Override max size for testing
        original_max = UserInfoCache._MAX_CACHE_SIZE
        UserInfoCache._MAX_CACHE_SIZE = 5

        try:
            # Insert 6 entries (exceeds max of 5)
            for i in range(6):
                await cache.set(f"users/{i}", f"User {i}")

            # The oldest entry (users/0) should have been evicted
            result = await cache.get("users/0")
            assert result is None, "Oldest entry should be evicted"

            # The newest entries should still be present
            for i in range(1, 6):
                result = await cache.get(f"users/{i}")
                assert result is not None, f"users/{i} should still be cached"
                assert result.display_name == f"User {i}"
        finally:
            UserInfoCache._MAX_CACHE_SIZE = original_max


class TestGitHubInstallationTokenCachePurge:
    """GitHub installation token cache should purge expired entries."""

    async def test_expired_entries_purged_on_access(self):
        """Calling _get_installation_token purges expired entries from the cache."""
        from chat_sdk.adapters.github.adapter import GitHubAdapter

        adapter = GitHubAdapter({"webhook_secret": "test-secret", "token": "pat-123"})

        # Populate cache with expired entries
        past = time.time() - 100
        for i in range(10):
            adapter._installation_token_cache[i] = (f"token-{i}", past)

        # Add one valid entry (expires in 1 hour, with 60s buffer = still valid)
        future = time.time() + 3600
        adapter._installation_token_cache[999] = ("valid-token", future)

        assert len(adapter._installation_token_cache) == 11

        # Call the real method — it purges expired entries at the top,
        # then finds the valid cached token for installation_id=999
        token = await adapter._get_installation_token(999)

        assert token == "valid-token"
        assert len(adapter._installation_token_cache) == 1
        assert 999 in adapter._installation_token_cache
