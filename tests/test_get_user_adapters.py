"""Per-adapter `get_user` integration tests for the chat.get_user port.

Mirrors the per-adapter `getUser` tests added in vercel/chat#391:

* packages/adapter-slack/src/index.test.ts (Slack getUser describe block)
* packages/adapter-discord/src/index.test.ts (Discord getUser)
* packages/adapter-gchat/src/index.test.ts (Google Chat getUser)
* packages/adapter-github/src/index.test.ts (GitHub getUser)
* packages/adapter-linear/src/index.test.ts (Linear getUser)
* packages/adapter-telegram/src/index.test.ts (Telegram getUser)
* packages/adapter-teams/src/index.test.ts (Teams getUser)

Tests mock at the appropriate per-adapter HTTP boundary (Slack Web API
client, `_discord_fetch`, `_github_api_request`, `_graphql_query`,
`telegram_fetch`, etc.) so we exercise the full `get_user` codepath
including auth/token plumbing and response shape mapping. Each adapter
gets a happy-path test plus an error path (API failure or not-found),
and one adversarial test for inputs that could escape the URL or pivot
the request (Hazard #12).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# =============================================================================
# Shared helpers
# =============================================================================


def _mock_state() -> MagicMock:
    cache: dict[str, Any] = {}
    state = MagicMock()
    state.get = AsyncMock(side_effect=lambda k: cache.get(k))
    state.set = AsyncMock(side_effect=lambda k, v, *a, **kw: cache.__setitem__(k, v))
    state.delete = AsyncMock(side_effect=lambda k: cache.pop(k, None))
    state.append_to_list = AsyncMock()
    state.get_list = AsyncMock(return_value=[])
    state._cache = cache
    return state


def _mock_chat(state: MagicMock) -> MagicMock:
    chat = MagicMock()
    chat.get_state = MagicMock(return_value=state)
    chat.get_user_name = MagicMock(return_value="test-bot")
    chat.get_logger = MagicMock(return_value=MagicMock())
    return chat


# =============================================================================
# Slack
# =============================================================================


class TestSlackGetUser:
    @pytest.mark.asyncio
    async def test_returns_user_info_with_email_and_avatar(self):
        from chat_sdk.adapters.slack.adapter import SlackAdapter
        from chat_sdk.adapters.slack.types import SlackAdapterConfig

        adapter = SlackAdapter(
            SlackAdapterConfig(signing_secret="s", bot_token="xoxb-test"),
        )
        state = _mock_state()
        chat = _mock_chat(state)
        await adapter.initialize(chat)

        client = MagicMock()
        client.users_info = AsyncMock(
            return_value={
                "user": {
                    "is_bot": False,
                    "name": "alice",
                    "real_name": "Alice Smith",
                    "profile": {
                        "display_name": "alice",
                        "real_name": "Alice Smith",
                        "email": "alice@example.com",
                        "image_192": "https://example.com/alice_192.png",
                    },
                }
            }
        )
        # Patch the per-call client factory.
        adapter._get_client = lambda token=None: client  # type: ignore[assignment]

        user = await adapter.get_user("U123")
        assert user is not None
        assert user.user_id == "U123"
        assert user.user_name == "alice"
        assert user.full_name == "Alice Smith"
        assert user.email == "alice@example.com"
        assert user.avatar_url == "https://example.com/alice_192.png"
        assert user.is_bot is False
        client.users_info.assert_awaited_once_with(user="U123")

    @pytest.mark.asyncio
    async def test_returns_none_when_lookup_fails(self):
        from chat_sdk.adapters.slack.adapter import SlackAdapter
        from chat_sdk.adapters.slack.types import SlackAdapterConfig

        adapter = SlackAdapter(
            SlackAdapterConfig(signing_secret="s", bot_token="xoxb-test"),
        )
        state = _mock_state()
        chat = _mock_chat(state)
        await adapter.initialize(chat)

        client = MagicMock()
        client.users_info = AsyncMock(side_effect=RuntimeError("user_not_found"))
        adapter._get_client = lambda token=None: client  # type: ignore[assignment]

        user = await adapter.get_user("U_DOES_NOT_EXIST")
        assert user is None

    @pytest.mark.asyncio
    async def test_uses_image_192_not_image_72(self):
        """Upstream cite: vercel/chat#391 — switched from image_72 to
        image_192 for better avatar quality. Lock the field choice in.
        """
        from chat_sdk.adapters.slack.adapter import SlackAdapter
        from chat_sdk.adapters.slack.types import SlackAdapterConfig

        adapter = SlackAdapter(
            SlackAdapterConfig(signing_secret="s", bot_token="xoxb-test"),
        )
        state = _mock_state()
        await adapter.initialize(_mock_chat(state))

        client = MagicMock()
        client.users_info = AsyncMock(
            return_value={
                "user": {
                    "name": "bob",
                    "real_name": "Bob",
                    "profile": {
                        "display_name": "bob",
                        "real_name": "Bob",
                        "image_72": "https://example.com/bob_72.png",
                        "image_192": "https://example.com/bob_192.png",
                    },
                }
            }
        )
        adapter._get_client = lambda token=None: client  # type: ignore[assignment]

        user = await adapter.get_user("U2")
        assert user is not None
        assert user.avatar_url == "https://example.com/bob_192.png"


# =============================================================================
# Discord
# =============================================================================


class TestDiscordGetUser:
    def _make_adapter(self):
        from chat_sdk.adapters.discord.adapter import DiscordAdapter
        from chat_sdk.adapters.discord.types import DiscordAdapterConfig

        return DiscordAdapter(
            DiscordAdapterConfig(
                bot_token="bot-token",
                public_key="0" * 64,
                application_id="app-id",
            )
        )

    @pytest.mark.asyncio
    async def test_returns_user_info(self):
        adapter = self._make_adapter()
        adapter._discord_fetch = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "id": "175928847299117063",
                "username": "discordbot",
                "global_name": "Discord User",
                "bot": False,
                "avatar": "abc123",
            }
        )
        user = await adapter.get_user("175928847299117063")
        assert user is not None
        assert user.user_id == "175928847299117063"
        assert user.user_name == "discordbot"
        assert user.full_name == "Discord User"
        assert user.is_bot is False
        assert user.avatar_url == "https://cdn.discordapp.com/avatars/175928847299117063/abc123.png"
        # Hazard #12 — user_id reaches the URL path; ensure URL-encoded.
        path_arg = adapter._discord_fetch.call_args.args[0]
        assert path_arg == "/users/175928847299117063"

    @pytest.mark.asyncio
    async def test_returns_none_on_api_failure(self):
        adapter = self._make_adapter()
        adapter._discord_fetch = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
        user = await adapter.get_user("175928847299117063")
        assert user is None

    @pytest.mark.asyncio
    async def test_rejects_non_numeric_user_id(self):
        """Hazard #12 — ``user_id`` containing a path separator must not
        escape the URL and pivot the request."""
        adapter = self._make_adapter()
        adapter._discord_fetch = AsyncMock(return_value={"id": "x", "username": "y"})  # type: ignore[method-assign]
        user = await adapter.get_user("175928847299117063/../guilds/leak")
        assert user is None
        adapter._discord_fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_falls_back_to_username_without_global_name(self):
        adapter = self._make_adapter()
        adapter._discord_fetch = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "id": "999",
                "username": "legacy",
                "global_name": None,
                "bot": True,
            }
        )
        user = await adapter.get_user("999")
        assert user is not None
        assert user.full_name == "legacy"
        assert user.is_bot is True
        assert user.avatar_url is None


# =============================================================================
# Google Chat
# =============================================================================


class TestGoogleChatGetUser:
    def _make_adapter(self):
        from chat_sdk.adapters.google_chat.adapter import GoogleChatAdapter
        from chat_sdk.adapters.google_chat.types import (
            GoogleChatAdapterConfig,
            ServiceAccountCredentials,
        )

        return GoogleChatAdapter(
            GoogleChatAdapterConfig(
                credentials=ServiceAccountCredentials(
                    client_email="bot@example.iam.gserviceaccount.com",
                    private_key="-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----",
                    project_id="test-project",
                ),
            )
        )

    @pytest.mark.asyncio
    async def test_returns_cached_user_info(self):
        adapter = self._make_adapter()
        # Seed the cache via the new is_bot/avatar_url path.
        await adapter._user_info_cache.set(
            "users/123",
            "Alice",
            "alice@example.com",
            False,
            "https://lh3.googleusercontent.com/alice",
        )
        user = await adapter.get_user("users/123")
        assert user is not None
        assert user.user_id == "users/123"
        assert user.user_name == "Alice"
        assert user.full_name == "Alice"
        assert user.email == "alice@example.com"
        assert user.avatar_url == "https://lh3.googleusercontent.com/alice"
        assert user.is_bot is False

    @pytest.mark.asyncio
    async def test_returns_none_when_user_not_cached(self):
        adapter = self._make_adapter()
        user = await adapter.get_user("users/has-never-interacted")
        assert user is None

    @pytest.mark.asyncio
    async def test_returns_none_when_cache_raises(self):
        adapter = self._make_adapter()
        adapter._user_info_cache.get = AsyncMock(side_effect=RuntimeError("state down"))  # type: ignore[method-assign]
        user = await adapter.get_user("users/123")
        assert user is None


# =============================================================================
# GitHub
# =============================================================================


class TestGitHubGetUser:
    def _make_adapter(self):
        from chat_sdk.adapters.github.adapter import GitHubAdapter

        return GitHubAdapter(
            {
                "webhook_secret": "test-webhook-secret",
                "token": "ghp_testtoken",
            }
        )

    @pytest.mark.asyncio
    async def test_returns_user_info(self):
        adapter = self._make_adapter()
        adapter._github_api_request = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "id": 583231,
                "login": "octocat",
                "name": "The Octocat",
                "email": "octocat@github.com",
                "avatar_url": "https://avatars.githubusercontent.com/u/583231?v=4",
                "type": "User",
            }
        )
        user = await adapter.get_user("583231")
        assert user is not None
        assert user.user_id == "583231"
        assert user.user_name == "octocat"
        assert user.full_name == "The Octocat"
        assert user.email == "octocat@github.com"
        assert user.avatar_url == "https://avatars.githubusercontent.com/u/583231?v=4"
        assert user.is_bot is False
        adapter._github_api_request.assert_awaited_once_with("GET", "/user/583231")

    @pytest.mark.asyncio
    async def test_returns_none_on_api_failure(self):
        adapter = self._make_adapter()
        adapter._github_api_request = AsyncMock(side_effect=RuntimeError("404"))  # type: ignore[method-assign]
        user = await adapter.get_user("999999")
        assert user is None

    @pytest.mark.asyncio
    async def test_marks_bot_account_type(self):
        adapter = self._make_adapter()
        adapter._github_api_request = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "id": 12345,
                "login": "dependabot[bot]",
                "type": "Bot",
                "avatar_url": "https://avatars.githubusercontent.com/in/29110",
            }
        )
        user = await adapter.get_user("12345")
        assert user is not None
        assert user.is_bot is True

    @pytest.mark.asyncio
    async def test_rejects_non_numeric_user_id(self):
        """Hazard #12 — `octocat` is a login, not an account_id;
        passing it should not produce a `/user/octocat/../leak` request."""
        adapter = self._make_adapter()
        adapter._github_api_request = AsyncMock()  # type: ignore[method-assign]
        user = await adapter.get_user("octocat")
        assert user is None
        adapter._github_api_request.assert_not_called()


# =============================================================================
# Linear
# =============================================================================


class TestLinearGetUser:
    def _make_adapter(self):
        from chat_sdk.adapters.linear.adapter import LinearAdapter
        from chat_sdk.adapters.linear.types import LinearAdapterAPIKeyConfig
        from chat_sdk.logger import ConsoleLogger

        return LinearAdapter(
            LinearAdapterAPIKeyConfig(
                api_key="test-api-key",
                webhook_secret="test-secret",
                user_name="test-bot",
                logger=ConsoleLogger("error"),
            )
        )

    @pytest.mark.asyncio
    async def test_returns_user_info(self):
        adapter = self._make_adapter()
        adapter._graphql_query = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "data": {
                    "user": {
                        "id": "8f1f3c7e-d4e1-4f9a-bf2b-1c3d4e5f6a7b",
                        "displayName": "ben",
                        "name": "Ben Sabic",
                        "email": "ben@example.com",
                        "avatarUrl": "https://linear.app/avatar/ben.png",
                    }
                }
            }
        )
        user = await adapter.get_user("8f1f3c7e-d4e1-4f9a-bf2b-1c3d4e5f6a7b")
        assert user is not None
        assert user.user_id == "8f1f3c7e-d4e1-4f9a-bf2b-1c3d4e5f6a7b"
        assert user.user_name == "ben"
        assert user.full_name == "Ben Sabic"
        assert user.email == "ben@example.com"
        assert user.avatar_url == "https://linear.app/avatar/ben.png"
        assert user.is_bot is False
        # Verify variables actually included the user id (no string concat).
        call = adapter._graphql_query.call_args
        assert call.args[1] == {"id": "8f1f3c7e-d4e1-4f9a-bf2b-1c3d4e5f6a7b"}

    @pytest.mark.asyncio
    async def test_returns_none_when_user_missing(self):
        adapter = self._make_adapter()
        adapter._graphql_query = AsyncMock(return_value={"data": {"user": None}})  # type: ignore[method-assign]
        user = await adapter.get_user("00000000-0000-0000-0000-000000000000")
        assert user is None

    @pytest.mark.asyncio
    async def test_returns_none_on_graphql_error(self):
        adapter = self._make_adapter()
        adapter._graphql_query = AsyncMock(side_effect=RuntimeError("403"))  # type: ignore[method-assign]
        user = await adapter.get_user("8f1f3c7e-d4e1-4f9a-bf2b-1c3d4e5f6a7b")
        assert user is None


# =============================================================================
# Telegram
# =============================================================================


class TestTelegramGetUser:
    def _make_adapter(self):
        from chat_sdk.adapters.telegram.adapter import TelegramAdapter
        from chat_sdk.adapters.telegram.types import TelegramAdapterConfig

        return TelegramAdapter(
            TelegramAdapterConfig(
                bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            )
        )

    @pytest.mark.asyncio
    async def test_returns_user_info_for_private_chat(self):
        adapter = self._make_adapter()
        adapter.telegram_fetch = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "id": 987654321,
                "type": "private",
                "first_name": "Alice",
                "last_name": "Smith",
                "username": "alice",
            }
        )
        user = await adapter.get_user("987654321")
        assert user is not None
        assert user.user_id == "987654321"
        assert user.user_name == "alice"
        assert user.full_name == "Alice Smith"
        # Documented divergence: getChat does not expose is_bot.
        assert user.is_bot is False
        adapter.telegram_fetch.assert_awaited_once_with("getChat", {"chat_id": "987654321"})

    @pytest.mark.asyncio
    async def test_returns_none_for_group_chat(self):
        """Telegram chat IDs can identify groups too; group chats are not users."""
        adapter = self._make_adapter()
        adapter.telegram_fetch = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "id": -100123,
                "type": "supergroup",
                "title": "Engineering",
            }
        )
        user = await adapter.get_user("-100123")
        assert user is None

    @pytest.mark.asyncio
    async def test_returns_none_on_api_error(self):
        adapter = self._make_adapter()
        adapter.telegram_fetch = AsyncMock(side_effect=RuntimeError("Bad Request"))  # type: ignore[method-assign]
        user = await adapter.get_user("not-a-real-user")
        assert user is None


# =============================================================================
# Teams
# =============================================================================


class TestTeamsGetUser:
    def _make_adapter(self):
        from chat_sdk.adapters.teams.adapter import TeamsAdapter
        from chat_sdk.adapters.teams.types import TeamsAdapterConfig

        return TeamsAdapter(
            TeamsAdapterConfig(
                app_id="11111111-2222-3333-4444-555555555555",
                app_password="app-secret",
            )
        )

    def _seed_chat_state(self, adapter, mapping: dict[str, Any]) -> MagicMock:
        state = _mock_state()
        for k, v in mapping.items():
            state._cache[k] = v
        chat = _mock_chat(state)
        adapter._chat = chat
        return state

    @pytest.mark.asyncio
    async def test_returns_none_when_chat_not_initialized(self):
        adapter = self._make_adapter()
        adapter._chat = None
        user = await adapter.get_user("29:abc")
        assert user is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_cached_aad_object_id(self):
        adapter = self._make_adapter()
        self._seed_chat_state(adapter, {})
        user = await adapter.get_user("29:never-interacted")
        assert user is None

    @pytest.mark.asyncio
    async def test_returns_user_info_via_graph_api(self):
        adapter = self._make_adapter()
        self._seed_chat_state(
            adapter,
            {"teams:aadObjectId:29:abc": "aad-object-uuid"},
        )

        # Mock the Graph token + HTTP session.
        adapter._get_graph_token = AsyncMock(return_value="graph-token")  # type: ignore[method-assign]

        class _Resp:
            ok = True
            status = 200

            async def json(self):
                return {
                    "displayName": "Carol Manager",
                    "userPrincipalName": "carol@contoso.com",
                    "mail": "carol@contoso.com",
                }

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return None

        # session.get is a sync method that returns an async context manager;
        # use a plain lambda so audit_test_quality doesn't false-flag it as
        # an unawaited async method.
        session = MagicMock()
        session.get = lambda *args, **kwargs: _Resp()
        adapter._get_http_session = AsyncMock(return_value=session)  # type: ignore[method-assign]

        user = await adapter.get_user("29:abc")
        assert user is not None
        assert user.user_id == "29:abc"
        assert user.user_name == "carol@contoso.com"
        assert user.full_name == "Carol Manager"
        assert user.email == "carol@contoso.com"
        assert user.is_bot is False

    @pytest.mark.asyncio
    async def test_returns_none_when_graph_returns_4xx(self):
        adapter = self._make_adapter()
        self._seed_chat_state(
            adapter,
            {"teams:aadObjectId:29:abc": "aad-object-uuid"},
        )
        adapter._get_graph_token = AsyncMock(return_value="graph-token")  # type: ignore[method-assign]

        class _Resp:
            ok = False
            status = 403

            async def json(self):
                return {}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return None

        # session.get is a sync method that returns an async context manager;
        # use a plain lambda so audit_test_quality doesn't false-flag it as
        # an unawaited async method.
        session = MagicMock()
        session.get = lambda *args, **kwargs: _Resp()
        adapter._get_http_session = AsyncMock(return_value=session)  # type: ignore[method-assign]

        user = await adapter.get_user("29:abc")
        assert user is None

    @pytest.mark.asyncio
    async def test_rejects_aad_object_id_with_path_separator(self):
        """Defense in depth — a poisoned cache entry must not pivot the
        Graph URL even though aadObjectId is normally platform-trusted."""
        adapter = self._make_adapter()
        self._seed_chat_state(
            adapter,
            {"teams:aadObjectId:29:abc": "aad-uuid/../leak"},
        )
        adapter._get_graph_token = AsyncMock(return_value="graph-token")  # type: ignore[method-assign]
        # Should never reach the HTTP session.
        adapter._get_http_session = AsyncMock(  # type: ignore[method-assign]
            side_effect=AssertionError("should not call HTTP")
        )

        user = await adapter.get_user("29:abc")
        assert user is None


# =============================================================================
# WhatsApp — explicitly unsupported
# =============================================================================


class TestWhatsAppGetUser:
    @pytest.mark.asyncio
    async def test_raises_chat_not_implemented_error(self):
        from chat_sdk.adapters.whatsapp.adapter import WhatsAppAdapter
        from chat_sdk.adapters.whatsapp.types import WhatsAppAdapterConfig
        from chat_sdk.errors import ChatNotImplementedError
        from chat_sdk.logger import ConsoleLogger

        adapter = WhatsAppAdapter(
            WhatsAppAdapterConfig(
                access_token="t",
                app_secret="s",
                phone_number_id="123",
                verify_token="v",
                user_name="bot",
                logger=ConsoleLogger("error"),
            )
        )
        with pytest.raises(ChatNotImplementedError):
            await adapter.get_user("4917612345678")
