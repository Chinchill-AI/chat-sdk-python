"""Custom ``api_url`` config + GitHub public ``get_installation_id``.

Pre-existing parity gaps ported from upstream chat@4.30.0:

* Every high-level adapter (Slack/Discord/GitHub/Linear) gained an ``apiUrl``
  config (plus a ``*_API_URL`` env fallback) in upstream 4.27.0 (6b17c60),
  routing custom base URLs into the underlying API clients (proxies, mocks,
  Enterprise/self-host endpoints). These tests assert the override is actually
  used and that the default is unchanged when unset.
* GitHub gained a public ``getInstallationId(thread|string)`` (index.ts:458-480)
  returning the fixed / cached / ``None`` installation per auth mode.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from chat_sdk.adapters.discord.adapter import DISCORD_API_BASE, DiscordAdapter
from chat_sdk.adapters.discord.types import DiscordAdapterConfig
from chat_sdk.adapters.github.adapter import GITHUB_API_BASE_URL, GitHubAdapter
from chat_sdk.adapters.linear.adapter import LINEAR_API_URL, LinearAdapter
from chat_sdk.adapters.linear.types import LinearAdapterAPIKeyConfig
from chat_sdk.adapters.slack.adapter import SlackAdapter
from chat_sdk.adapters.slack.types import SlackAdapterConfig
from chat_sdk.logger import ConsoleLogger
from chat_sdk.shared.errors import ValidationError

pytest.importorskip("slack_sdk")

TEST_PUBLIC_KEY = "a" * 64

# Env vars these adapters read for their api_url fallback. Cleared before every
# test so a host-set value can't shadow assertions about the built-in default.
_API_URL_ENV_VARS = ("SLACK_API_URL", "DISCORD_API_URL", "GITHUB_API_URL", "LINEAR_API_URL")


@pytest.fixture(autouse=True)
def _isolate_api_url_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _API_URL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# Fake aiohttp session that records the URL of the first request it sees.
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.status = 200
        self.ok = True

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def json(self) -> dict[str, Any]:
        return self._payload

    async def text(self) -> str:
        return ""


class _RecordingSession:
    """Records the URL passed to ``post`` / ``request``."""

    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.urls: list[str] = []
        self._payload = payload or {}

    def post(self, url: str, *_args: object, **_kwargs: object) -> _FakeResponse:
        self.urls.append(url)
        return _FakeResponse(self._payload)

    def request(self, _method: str, url: str, *_args: object, **_kwargs: object) -> _FakeResponse:
        self.urls.append(url)
        return _FakeResponse(self._payload)


# ===========================================================================
# Slack — base_url threaded into both client constructions
# ===========================================================================


class _CapturingClient:
    """Records the kwargs the adapter passes to AsyncWebClient / WebClient.

    Independent of the real slack_sdk so the test is robust to sibling test
    files that install a fake ``slack_sdk`` stub into ``sys.modules``.
    """

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


def _patch_slack_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch both client classes on whatever slack_sdk modules are installed."""
    import slack_sdk
    import slack_sdk.web.async_client as async_mod

    monkeypatch.setattr(async_mod, "AsyncWebClient", _CapturingClient, raising=False)
    monkeypatch.setattr(slack_sdk, "WebClient", _CapturingClient, raising=False)


class TestSlackApiUrl:
    def _make(self, **overrides: Any) -> SlackAdapter:
        config = SlackAdapterConfig(
            signing_secret=overrides.pop("signing_secret", "test-signing-secret"),
            bot_token=overrides.pop("bot_token", "xoxb-default-token"),
            **overrides,
        )
        return SlackAdapter(config)

    def test_custom_api_url_passed_to_async_web_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_slack_clients(monkeypatch)
        adapter = self._make(api_url="https://slack.proxy.example/api/")
        client = adapter._get_client("xoxb-tok")
        assert client.kwargs == {"token": "xoxb-tok", "base_url": "https://slack.proxy.example/api/"}

    def test_custom_api_url_passed_to_sync_web_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_slack_clients(monkeypatch)
        adapter = self._make(api_url="https://slack.proxy.example/api/")
        client = adapter._get_web_client_for_token("xoxb-tok")
        assert client.kwargs == {"token": "xoxb-tok", "base_url": "https://slack.proxy.example/api/"}

    def test_no_base_url_passed_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # When no override is configured we must NOT pass base_url at all, so
        # slack_sdk keeps its built-in default (and never sees base_url=None,
        # which it rejects).
        _patch_slack_clients(monkeypatch)
        adapter = self._make()
        assert adapter._slack_api_url is None
        async_client = adapter._get_client("xoxb-tok")
        sync_client = adapter._get_web_client_for_token("xoxb-tok")
        assert "base_url" not in async_client.kwargs
        assert "base_url" not in sync_client.kwargs

    def test_env_fallback_used_when_config_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SLACK_API_URL", "https://slack.env.example/api/")
        _patch_slack_clients(monkeypatch)
        adapter = self._make()
        assert adapter._slack_api_url == "https://slack.env.example/api/"
        client = adapter._get_client("xoxb-tok")
        assert client.kwargs["base_url"] == "https://slack.env.example/api/"

    def test_config_wins_over_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SLACK_API_URL", "https://slack.env.example/api/")
        _patch_slack_clients(monkeypatch)
        adapter = self._make(api_url="https://slack.config.example/api/")
        client = adapter._get_client("xoxb-tok")
        assert client.kwargs["base_url"] == "https://slack.config.example/api/"

    def test_real_slack_sdk_normalizes_base_url(self) -> None:
        # End-to-end against the genuine slack_sdk client (skipped if a sibling
        # test stubbed it): the override must reach the real ``base_url``.
        import slack_sdk

        real_web_client = getattr(slack_sdk, "WebClient", None)
        if real_web_client is None or not getattr(real_web_client, "__module__", "").startswith("slack_sdk"):
            pytest.skip("slack_sdk is stubbed by a sibling test in this run")
        adapter = self._make(api_url="https://slack.proxy.example/api/")
        client = adapter._get_web_client_for_token("xoxb-tok")
        assert client.base_url == "https://slack.proxy.example/api/"


# ===========================================================================
# Discord — api_url threaded into _discord_fetch
# ===========================================================================


class TestDiscordApiUrl:
    def _make(self, **overrides: Any) -> DiscordAdapter:
        config = DiscordAdapterConfig(
            bot_token=overrides.pop("bot_token", "test-token"),
            public_key=overrides.pop("public_key", TEST_PUBLIC_KEY),
            application_id=overrides.pop("application_id", "test-app-id"),
            logger=ConsoleLogger("error"),
            **overrides,
        )
        return DiscordAdapter(config)

    def test_default_api_base_when_unset(self) -> None:
        adapter = self._make()
        assert adapter._api_base_url == DISCORD_API_BASE

    def test_custom_api_url_stored(self) -> None:
        adapter = self._make(api_url="https://discord.proxy.example/api/v10")
        assert adapter._api_base_url == "https://discord.proxy.example/api/v10"

    def test_env_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DISCORD_API_URL", "https://discord.env.example/api/v10")
        adapter = self._make()
        assert adapter._api_base_url == "https://discord.env.example/api/v10"

    def test_config_wins_over_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DISCORD_API_URL", "https://discord.env.example/api/v10")
        adapter = self._make(api_url="https://discord.config.example/api/v10")
        assert adapter._api_base_url == "https://discord.config.example/api/v10"

    async def test_custom_api_url_used_in_fetch(self) -> None:
        adapter = self._make(api_url="https://discord.proxy.example/api/v10")
        session = _RecordingSession({"id": "1"})
        adapter._get_http_session = AsyncMock(return_value=session)  # type: ignore[method-assign]
        await adapter._discord_fetch("/channels/123/messages", "POST", body={"content": "hi"})
        assert session.urls == ["https://discord.proxy.example/api/v10/channels/123/messages"]

    async def test_default_api_url_used_in_fetch(self) -> None:
        adapter = self._make()
        session = _RecordingSession({"id": "1"})
        adapter._get_http_session = AsyncMock(return_value=session)  # type: ignore[method-assign]
        await adapter._discord_fetch("/channels/123/messages", "POST", body={"content": "hi"})
        assert session.urls == [f"{DISCORD_API_BASE}/channels/123/messages"]


# ===========================================================================
# GitHub — api_url threaded into api_request + installation-token URL
# ===========================================================================


class TestGitHubApiUrl:
    def _make(self, **overrides: Any) -> GitHubAdapter:
        config: dict[str, Any] = {
            "webhook_secret": "test-webhook-secret",
            "token": "ghp_testtoken",
            "logger": ConsoleLogger("error"),
        }
        config.update(overrides)
        return GitHubAdapter(config)

    def test_default_api_url_when_unset(self) -> None:
        adapter = self._make()
        assert adapter._api_url == GITHUB_API_BASE_URL

    def test_custom_api_url_strips_trailing_slash(self) -> None:
        # GitHub Enterprise endpoints often carry a trailing slash; strip it so
        # f"{base}{path}" joins cleanly with leading-slash paths.
        adapter = self._make(api_url="https://github.example.com/api/v3/")
        assert adapter._api_url == "https://github.example.com/api/v3"

    def test_env_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_API_URL", "https://github.env.example/api/v3")
        adapter = self._make()
        assert adapter._api_url == "https://github.env.example/api/v3"

    def test_config_wins_over_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_API_URL", "https://github.env.example/api/v3")
        adapter = self._make(api_url="https://github.config.example/api/v3")
        assert adapter._api_url == "https://github.config.example/api/v3"

    async def test_custom_api_url_used_in_request(self) -> None:
        adapter = self._make(api_url="https://github.example.com/api/v3")
        session = _RecordingSession({"id": 1})
        adapter._get_http_session = AsyncMock(return_value=session)  # type: ignore[method-assign]
        await adapter._github_api_request("GET", "/repos/acme/app/pulls/1")
        assert session.urls == ["https://github.example.com/api/v3/repos/acme/app/pulls/1"]

    async def test_default_api_url_used_in_request(self) -> None:
        adapter = self._make()
        session = _RecordingSession({"id": 1})
        adapter._get_http_session = AsyncMock(return_value=session)  # type: ignore[method-assign]
        await adapter._github_api_request("GET", "/repos/acme/app/pulls/1")
        assert session.urls == ["https://api.github.com/repos/acme/app/pulls/1"]

    async def test_custom_api_url_used_in_installation_token_exchange(self) -> None:
        adapter = GitHubAdapter(
            {
                "app_id": "12345",
                "private_key": "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----",
                "installation_id": 77,
                "webhook_secret": "secret",
                "logger": ConsoleLogger("error"),
                "api_url": "https://github.example.com/api/v3",
            }
        )
        session = _RecordingSession({"token": "ghs_x", "expires_at": ""})
        adapter._get_http_session = AsyncMock(return_value=session)  # type: ignore[method-assign]
        adapter._generate_app_jwt = MagicMock(return_value="jwt")  # type: ignore[method-assign]
        await adapter._get_installation_token(77)
        assert session.urls == ["https://github.example.com/api/v3/app/installations/77/access_tokens"]


# ===========================================================================
# Linear — api_url threaded into _graphql_query
# ===========================================================================


class TestLinearApiUrl:
    def _make(self, **overrides: Any) -> LinearAdapter:
        config = LinearAdapterAPIKeyConfig(
            api_key=overrides.pop("api_key", "lin_api_test"),
            webhook_secret=overrides.pop("webhook_secret", "test-secret"),
            logger=ConsoleLogger("error"),
            **overrides,
        )
        return LinearAdapter(config)

    def test_default_api_url_when_unset(self) -> None:
        adapter = self._make()
        assert adapter._api_url == LINEAR_API_URL

    def test_custom_api_url_stored(self) -> None:
        adapter = self._make(api_url="https://linear.proxy.example/graphql")
        assert adapter._api_url == "https://linear.proxy.example/graphql"

    def test_env_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LINEAR_API_URL", "https://linear.env.example/graphql")
        adapter = self._make()
        assert adapter._api_url == "https://linear.env.example/graphql"

    def test_config_wins_over_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LINEAR_API_URL", "https://linear.env.example/graphql")
        adapter = self._make(api_url="https://linear.config.example/graphql")
        assert adapter._api_url == "https://linear.config.example/graphql"

    async def test_custom_api_url_used_in_graphql_query(self) -> None:
        adapter = self._make(api_url="https://linear.proxy.example/graphql")
        session = _RecordingSession({"data": {}})
        adapter._get_http_session = AsyncMock(return_value=session)  # type: ignore[method-assign]
        await adapter._graphql_query("query { viewer { id } }")
        assert session.urls == ["https://linear.proxy.example/graphql"]

    async def test_default_api_url_used_in_graphql_query(self) -> None:
        adapter = self._make()
        session = _RecordingSession({"data": {}})
        adapter._get_http_session = AsyncMock(return_value=session)  # type: ignore[method-assign]
        await adapter._graphql_query("query { viewer { id } }")
        assert session.urls == [LINEAR_API_URL]


# ===========================================================================
# GitHub public get_installation_id (gap B)
# ===========================================================================


def _make_mock_chat_with_store(store: dict[str, Any]) -> MagicMock:
    state = MagicMock()
    state.get = AsyncMock(side_effect=lambda k: store.get(k))
    state.set = AsyncMock(side_effect=lambda k, v, *a, **kw: store.__setitem__(k, v))
    chat = MagicMock()
    chat.get_state = MagicMock(return_value=state)
    return chat


class TestGitHubGetInstallationId:
    _APP_KEY = "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----"

    async def test_pat_mode_returns_none(self) -> None:
        adapter = GitHubAdapter({"webhook_secret": "s", "token": "ghp_x", "logger": ConsoleLogger("error")})
        assert await adapter.get_installation_id("github:acme/app:42") is None

    async def test_single_tenant_returns_fixed_id(self) -> None:
        adapter = GitHubAdapter(
            {
                "app_id": "12345",
                "private_key": self._APP_KEY,
                "installation_id": 99,
                "webhook_secret": "s",
                "logger": ConsoleLogger("error"),
            }
        )
        # Fixed id is returned regardless of the thread (even a garbage one).
        assert await adapter.get_installation_id("not-a-real-thread") == 99

    async def test_multi_tenant_returns_cached_id_for_thread(self) -> None:
        store = {"github:install:acme/app": 4242}
        chat = _make_mock_chat_with_store(store)
        adapter = GitHubAdapter(
            {
                "app_id": "12345",
                "private_key": self._APP_KEY,
                "webhook_secret": "s",
                "logger": ConsoleLogger("error"),
            }
        )
        await adapter.initialize(chat)
        assert await adapter.get_installation_id("github:acme/app:42") == 4242

    async def test_multi_tenant_accepts_thread_object(self) -> None:
        store = {"github:install:acme/app": 4242}
        chat = _make_mock_chat_with_store(store)
        adapter = GitHubAdapter(
            {
                "app_id": "12345",
                "private_key": self._APP_KEY,
                "webhook_secret": "s",
                "logger": ConsoleLogger("error"),
            }
        )
        await adapter.initialize(chat)
        thread = MagicMock()
        thread.id = "github:acme/app:42"
        assert await adapter.get_installation_id(thread) == 4242

    async def test_multi_tenant_uncached_returns_none(self) -> None:
        chat = _make_mock_chat_with_store({})
        adapter = GitHubAdapter(
            {
                "app_id": "12345",
                "private_key": self._APP_KEY,
                "webhook_secret": "s",
                "logger": ConsoleLogger("error"),
            }
        )
        await adapter.initialize(chat)
        assert await adapter.get_installation_id("github:acme/app:42") is None

    async def test_multi_tenant_without_init_raises(self) -> None:
        adapter = GitHubAdapter(
            {
                "app_id": "12345",
                "private_key": self._APP_KEY,
                "webhook_secret": "s",
                "logger": ConsoleLogger("error"),
            }
        )
        with pytest.raises(ValidationError):
            await adapter.get_installation_id("github:acme/app:42")
