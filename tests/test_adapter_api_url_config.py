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

import sys
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Stub slack_sdk so the Slack api_url tests run without the real dependency
# installed. ``slack_sdk`` lives only in the optional ``slack``/``all`` extras
# (not the dev group CI installs), so a module-level ``importorskip`` here would
# collect-as-skipped the ENTIRE file -- silently disabling the Discord/GitHub/
# Linear api_url tests and the get_installation_id regression tests in CI. We
# install a minimal fake (mirroring ``tests/test_slack_client_cache.py``)
# *before* importing ``SlackAdapter`` so its deferred ``from slack_sdk ...``
# imports resolve to the stub. The Slack tests then patch these classes onto the
# stub via ``_patch_slack_clients``.
# ---------------------------------------------------------------------------

_fake_slack_sdk = ModuleType("slack_sdk")
_fake_slack_sdk_web = ModuleType("slack_sdk.web")
_fake_slack_sdk_web_async = ModuleType("slack_sdk.web.async_client")


class _FakeAsyncWebClient:
    """Minimal stand-in for slack_sdk.web.async_client.AsyncWebClient."""

    def __init__(self, *, token: str = "", base_url: str | None = None) -> None:
        self.token = token
        self.base_url = base_url


class _FakeWebClient:
    """Minimal stand-in for slack_sdk.WebClient."""

    def __init__(self, *, token: str = "", base_url: str | None = None) -> None:
        self.token = token
        self.base_url = base_url


_fake_slack_sdk_web_async.AsyncWebClient = _FakeAsyncWebClient  # type: ignore[attr-defined]
_fake_slack_sdk_web.async_client = _fake_slack_sdk_web_async  # type: ignore[attr-defined]
_fake_slack_sdk.web = _fake_slack_sdk_web  # type: ignore[attr-defined]
_fake_slack_sdk.WebClient = _FakeWebClient  # type: ignore[attr-defined]

sys.modules.setdefault("slack_sdk", _fake_slack_sdk)
sys.modules.setdefault("slack_sdk.web", _fake_slack_sdk_web)
sys.modules.setdefault("slack_sdk.web.async_client", _fake_slack_sdk_web_async)

from chat_sdk.adapters.discord.adapter import DISCORD_API_BASE, DiscordAdapter  # noqa: E402
from chat_sdk.adapters.discord.types import DiscordAdapterConfig  # noqa: E402
from chat_sdk.adapters.github.adapter import GITHUB_API_BASE_URL, GitHubAdapter  # noqa: E402
from chat_sdk.adapters.linear.adapter import LINEAR_API_URL, LinearAdapter  # noqa: E402
from chat_sdk.adapters.linear.types import LinearAdapterAPIKeyConfig  # noqa: E402
from chat_sdk.adapters.slack.adapter import SlackAdapter  # noqa: E402
from chat_sdk.adapters.slack.types import SlackAdapterConfig  # noqa: E402
from chat_sdk.logger import ConsoleLogger  # noqa: E402
from chat_sdk.shared.errors import ValidationError  # noqa: E402

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

    def test_empty_api_url_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Upstream feeds the WebClients via the truthy spread
        # ``...(this.slackApiUrl ? {...} : {})`` (index.ts:577/621), so an empty
        # ``apiUrl`` must fall back to slack_sdk's built-in default -- never pass
        # ``base_url=""`` (which would point the WebClients at an empty host).
        _patch_slack_clients(monkeypatch)
        adapter = self._make(api_url="")
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
# Slack — webClientOptions spread into both client constructions (chat@4.31, 8336a3e)
# ===========================================================================


class TestSlackWebClientOptions:
    """``web_client_options`` is forwarded to BOTH the default async client and
    the per-token sync client. Mirrors upstream's index.test.ts webClientOptions
    block. DIVERGENCE: maps to slack_sdk ``WebClient`` kwargs (``timeout``,
    ``retry_handlers``), not ``@slack/web-api`` axios options.
    """

    def _make(self, **overrides: Any) -> SlackAdapter:
        config = SlackAdapterConfig(
            signing_secret=overrides.pop("signing_secret", "test-signing-secret"),
            bot_token=overrides.pop("bot_token", "xoxb-default-token"),
            **overrides,
        )
        return SlackAdapter(config)

    def test_option_reaches_default_async_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Upstream: "forwards webClientOptions to the internal WebClient".
        _patch_slack_clients(monkeypatch)
        adapter = self._make(web_client_options={"timeout": 15})
        client = adapter._get_client("xoxb-tok")
        assert client.kwargs["timeout"] == 15
        assert client.kwargs["token"] == "xoxb-tok"

    def test_option_reaches_per_token_sync_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Upstream: "forwards webClientOptions to token-bound WebClients".
        _patch_slack_clients(monkeypatch)
        adapter = self._make(bot_token=None, web_client_options={"timeout": 15})
        client = adapter._get_web_client_for_token("xoxb-context-token")
        assert client.kwargs["timeout"] == 15
        assert client.kwargs["token"] == "xoxb-context-token"

    def test_none_options_spreads_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The default (None) must add no extra kwargs at all.
        _patch_slack_clients(monkeypatch)
        adapter = self._make()
        assert adapter._web_client_options is None
        client = adapter._get_client("xoxb-tok")
        assert client.kwargs == {"token": "xoxb-tok"}

    def test_empty_dict_options_still_spreads(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Gate is ``is not None`` (not truthiness): an explicit empty ``{}`` is a
        # valid no-op spread, not a fall-through. Locks ``is not None`` vs ``or``.
        _patch_slack_clients(monkeypatch)
        adapter = self._make(web_client_options={})
        assert adapter._web_client_options == {}
        client = adapter._get_client("xoxb-tok")
        assert client.kwargs == {"token": "xoxb-tok"}

    def test_headers_isolated_across_per_token_clients(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Upstream: "isolates custom headers between token-bound WebClients".
        # Each cached client must get its OWN headers dict so mutating one (e.g.
        # the per-token Authorization header slack_sdk sets) cannot leak across
        # tokens. We deep-copy ``headers`` per client to guarantee this.
        _patch_slack_clients(monkeypatch)
        headers = {"X-Test": "value"}
        adapter = self._make(bot_token=None, web_client_options={"headers": headers})

        first = adapter._get_web_client_for_token("xoxb-first")
        second = adapter._get_web_client_for_token("xoxb-second")

        assert first.kwargs["headers"] == {"X-Test": "value"}
        assert second.kwargs["headers"] == {"X-Test": "value"}
        # Distinct dict objects, not a shared reference.
        assert first.kwargs["headers"] is not second.kwargs["headers"]

    def test_caller_input_headers_not_mutated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The caller's original ``headers`` dict must never be mutated or
        # aliased by the adapter. Upstream asserts the input ``headers`` is
        # unchanged after building two clients.
        _patch_slack_clients(monkeypatch)
        headers = {"X-Test": "value"}
        adapter = self._make(bot_token=None, web_client_options={"headers": headers})

        first = adapter._get_web_client_for_token("xoxb-first")
        adapter._get_web_client_for_token("xoxb-second")

        # The constructed client holds a copy, not the caller's object.
        assert first.kwargs["headers"] is not headers
        assert headers == {"X-Test": "value"}

    def test_option_reaches_both_client_kinds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The same option must land in BOTH the async default client and the
        # sync per-token client (two distinct construction sites in the adapter).
        _patch_slack_clients(monkeypatch)
        adapter = self._make(web_client_options={"timeout": 42})
        async_client = adapter._get_client("xoxb-tok")
        sync_client = adapter._get_web_client_for_token("xoxb-tok")
        assert async_client.kwargs["timeout"] == 42
        assert sync_client.kwargs["timeout"] == 42


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

    async def test_empty_api_url_falls_back_to_default(self) -> None:
        # ``_discord_fetch`` joins ``f"{base}{path}"`` directly, so an empty
        # ``apiUrl`` must resolve to ``DISCORD_API_BASE`` rather than producing
        # a relative ``/channels/...`` URL.
        adapter = self._make(api_url="")
        assert adapter._api_base_url == DISCORD_API_BASE
        session = _RecordingSession({"id": "1"})
        adapter._get_http_session = AsyncMock(return_value=session)  # type: ignore[method-assign]
        await adapter._discord_fetch("/channels/123/messages", "POST", body={"content": "hi"})
        assert session.urls == [f"{DISCORD_API_BASE}/channels/123/messages"]

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

    async def test_empty_api_url_falls_back_to_default(self) -> None:
        # Upstream's truthy spread ``...(this.apiUrl ? { baseUrl } : {})`` means
        # an empty ``apiUrl`` uses the default api.github.com endpoint, not an
        # empty base that would yield a relative request URL.
        adapter = self._make(api_url="")
        assert adapter._api_url == GITHUB_API_BASE_URL
        session = _RecordingSession({"id": 1})
        adapter._get_http_session = AsyncMock(return_value=session)  # type: ignore[method-assign]
        await adapter._github_api_request("GET", "/repos/acme/app/pulls/1")
        assert session.urls == ["https://api.github.com/repos/acme/app/pulls/1"]

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

    async def test_empty_api_url_falls_back_to_default(self) -> None:
        # ``_graphql_query`` POSTs straight to ``self._api_url``; the truthy
        # fallback (mirroring upstream ``...(this.apiUrl ? { apiUrl } : {})``)
        # means an empty ``apiUrl`` posts to the real Linear endpoint rather
        # than an empty/relative URL.
        adapter = self._make(api_url="")
        assert adapter._api_url == LINEAR_API_URL
        session = _RecordingSession({"data": {}})
        adapter._get_http_session = AsyncMock(return_value=session)  # type: ignore[method-assign]
        await adapter._graphql_query("query { viewer { id } }")
        assert session.urls == [LINEAR_API_URL]

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
