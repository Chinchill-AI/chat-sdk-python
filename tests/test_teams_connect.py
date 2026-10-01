"""Teams auth extension points: webhook verifier, lazy app id, custom token factory.

Python port of ``packages/adapter-teams/src/connect.test.ts`` (chat@4.41.1;
upstream ``139d337e`` "Add Vercel Connect support for Microsoft Teams" and
``e06b4b60`` "forward a custom token factory to the Teams SDK"). Only the
portable, Connect-agnostic parts are ported: the ``webhook_verifier`` hook in
the bridge, a callable ``app_id`` resolved during ``initialize()``, and the
``token`` factory's precedence over every client-secret source. The shared
``connectWebhookContract`` suite belongs to Vercel Connect and is N/A.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from microsoft_teams.api import TokenCredentials
from microsoft_teams.apps import App

from chat_sdk.adapters.teams.adapter import TeamsAdapter
from chat_sdk.adapters.teams.types import TeamsAdapterConfig
from chat_sdk.logger import ConsoleLogger
from chat_sdk.shared.errors import AuthenticationError, ValidationError
from chat_sdk.testing import create_mock_state
from chat_sdk.types import WebhookOptions

_LOGGER = ConsoleLogger("silent", prefix="teams")

_ACTIVITY: dict[str, Any] = {
    "id": "connect-message",
    "type": "message",
    "channelId": "msteams",
    "serviceUrl": "https://smba.trafficmanager.net/teams/",
    "from": {"id": "user-1", "name": "User"},
    "recipient": {"id": "28:test-app", "name": "Bot"},
    "conversation": {"id": "19:channel@thread.tacv2", "conversationType": "channel"},
    "text": "Hello",
}


class _Request:
    """Framework-agnostic request double (``text()`` + ``headers``)."""

    def __init__(self, body: str | None = None, headers: dict[str, str] | None = None) -> None:
        self._body = json.dumps(_ACTIVITY) if body is None else body
        self.headers = {"content-type": "application/json", **(headers or {})}

    async def text(self) -> str:
        return self._body


class _CountingAdapter(TeamsAdapter):
    """Counts handler registrations (upstream ``TestAdapter.registerCount``)."""

    register_count = 0

    def _register_event_handlers(self) -> None:
        self.register_count += 1
        super()._register_event_handlers()


def _make_adapter(**overrides: Any) -> _CountingAdapter:
    """Upstream ``createAdapter``: a token factory, so no secret is needed."""
    fields: dict[str, Any] = {"app_id": "test-app", "token": lambda _scope, _tenant: "unused-token", "logger": _LOGGER}
    fields.update(overrides)
    return _CountingAdapter(TeamsAdapterConfig(**fields))


def _make_chat() -> MagicMock:
    chat = MagicMock()
    chat.process_message = MagicMock()
    chat.process_action = MagicMock()
    chat.process_reaction = MagicMock()
    chat.get_state = MagicMock(return_value=create_mock_state())
    return chat


def _unsigned_jwt(exp_offset: int = 3600) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": int(time.time()) + exp_offset}).encode()).rstrip(b"=")
    # Unsigned is fine: the SDK only decodes the token for its expiry.
    return f"e30.{payload.decode()}.c2ln"


# ---------------------------------------------------------------------------
# Connect webhook dispatch
# ---------------------------------------------------------------------------


class TestConnectWebhookDispatch:
    async def test_verifies_the_exact_raw_body_before_parsing_or_routing(self) -> None:
        verifier = AsyncMock(return_value=False)
        adapter = _make_adapter(webhook_verifier=verifier)
        chat = _make_chat()
        await adapter.initialize(chat)
        incoming = _Request(" invalid json ")

        response = await adapter.handle_webhook(incoming)

        assert response["status"] == 401
        verifier.assert_awaited_once_with(incoming, " invalid json ")
        chat.process_message.assert_not_called()

    @pytest.mark.parametrize("body", ["invalid json", ""])
    async def test_returns_400_for_verified_invalid_json(self, body: str) -> None:
        adapter = _make_adapter(webhook_verifier=lambda _request, _body: True)
        chat = _make_chat()
        await adapter.initialize(chat)

        response = await adapter.handle_webhook(_Request(body))

        # An empty body is invalid JSON too (upstream ``JSON.parse("")``); the
        # verifier skipped the SDK's JWT check, so it must not route as ``{}``.
        assert response["status"] == 400
        chat.process_message.assert_not_called()

    async def test_rejects_asynchronously_failed_verification_before_activity_processing(self) -> None:
        async def verifier(_request: Any, _body: str) -> bool:
            raise RuntimeError("invalid")

        adapter = _make_adapter(webhook_verifier=verifier)
        chat = _make_chat()
        await adapter.initialize(chat)

        response = await adapter.handle_webhook(_Request())

        assert response["status"] == 401
        chat.process_message.assert_not_called()

    async def test_rejects_synchronously_raising_verification(self) -> None:
        def verifier(_request: Any, _body: str) -> bool:
            raise RuntimeError("invalid")

        adapter = _make_adapter(webhook_verifier=verifier)
        chat = _make_chat()
        await adapter.initialize(chat)

        response = await adapter.handle_webhook(_Request())

        assert response["status"] == 401
        chat.process_message.assert_not_called()

    async def test_retains_native_authentication_when_no_verifier_is_configured(self) -> None:
        # Token-factory credentials still get the SDK's JWT validation.
        adapter = _make_adapter()
        chat = _make_chat()
        await adapter.initialize(chat)

        assert (await adapter.handle_webhook(_Request()))["status"] == 401
        invalid = _Request(headers={"authorization": "Bearer invalid"})
        assert (await adapter.handle_webhook(invalid))["status"] == 401
        chat.process_message.assert_not_called()

    async def test_routes_verified_messages_with_their_webhook_options(self) -> None:
        adapter = _make_adapter(webhook_verifier=lambda _request, _body: {"verified": True})
        chat = _make_chat()
        await adapter.initialize(chat)
        options = WebhookOptions(wait_until=MagicMock())

        response = await adapter.handle_webhook(_Request(), options)

        assert response["status"] == 200
        chat.process_message.assert_called_once()
        args = chat.process_message.call_args.args
        assert args[0] is adapter
        assert args[1].startswith("teams:")
        assert args[2].text == "Hello"
        # Channel messages forward the per-activity options object unchanged.
        assert args[3] is options

    async def test_retains_native_dm_streaming_and_wait_until_dispatch(self) -> None:
        adapter = _make_adapter(webhook_verifier=lambda _request, _body: True)
        chat = _make_chat()

        def process_message(_adapter: Any, _thread_id: str, _message: Any, options: Any = None) -> None:
            done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            done.set_result(None)
            options.wait_until(done)

        chat.process_message = MagicMock(side_effect=process_message)
        await adapter.initialize(chat)
        wait_until = MagicMock()
        body = json.dumps({**_ACTIVITY, "conversation": {"id": "a:personal", "conversationType": "personal"}})

        response = await adapter.handle_webhook(_Request(body), WebhookOptions(wait_until=wait_until))

        assert response["status"] == 200
        chat.process_message.assert_called_once()
        wait_until.assert_called_once()

    async def test_preserves_invoke_responses_from_the_sdk_route(self) -> None:
        adapter = _make_adapter(webhook_verifier=lambda _request, _body: True)
        await adapter.initialize(_make_chat())
        body = json.dumps(
            {
                **_ACTIVITY,
                "type": "invoke",
                "name": "adaptiveCard/action",
                "value": {"action": {"type": "Action.Execute", "verb": "test", "data": {}}},
            }
        )

        response = await adapter.handle_webhook(_Request(body))

        assert response["status"] == 200
        assert json.loads(response["body"]) == {
            "statusCode": 200,
            "type": "application/vnd.microsoft.activity.message",
            "value": "",
        }

    async def test_verifier_replaces_the_bot_framework_issuer_precheck(self) -> None:
        # Python-only interaction with #250: the issuer pre-check guards the
        # SDK's JWT validator, which a verifier replaces (skip-auth App). A
        # verified request carrying a non-Bot-Framework bearer token (here an
        # Entra-style issuer) must still route, as it does upstream.
        header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
        claims = {"iss": "https://login.microsoftonline.com/attacker-tenant/v2.0", "exp": int(time.time()) + 3600}
        payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
        adapter = _make_adapter(webhook_verifier=lambda _request, _body: True)
        chat = _make_chat()
        await adapter.initialize(chat)

        response = await adapter.handle_webhook(_Request(headers={"authorization": f"Bearer {header}.{payload}."}))

        assert response["status"] == 200
        chat.process_message.assert_called_once()


# ---------------------------------------------------------------------------
# Lazy Teams identity
# ---------------------------------------------------------------------------


class TestLazyTeamsIdentity:
    async def test_captures_authentication_config_before_lazy_initialization(self) -> None:
        async def app_id() -> str:
            return "lazy-app"

        config = TeamsAdapterConfig(app_id=app_id, token=lambda _s, _t: "unused-token", logger=_LOGGER)
        adapter = _CountingAdapter(config)
        # Mutating the caller's config after construction must not disable
        # native authentication on the lazily built App.
        config.webhook_verifier = lambda _request, _body: True
        await adapter.initialize(_make_chat())

        assert (await adapter.handle_webhook(_Request()))["status"] == 401

    async def test_resolves_once_across_concurrent_initialization_and_retains_bot_identity(self) -> None:
        app_id = AsyncMock(return_value="lazy-app")
        adapter = _make_adapter(app_id=app_id)
        app_id.assert_not_called()
        assert adapter.bot_user_id is None
        with pytest.raises(ValidationError):
            _ = adapter._app
        assert adapter.parse_message(_ACTIVITY).author.is_me is False

        chat = _make_chat()
        await asyncio.gather(adapter.initialize(chat), adapter.initialize(chat))
        await adapter.initialize(chat)

        app_id.assert_awaited_once()
        assert adapter.bot_user_id == "28:lazy-app"
        assert adapter._app.id == "lazy-app"
        assert adapter.register_count == 1
        assert adapter.parse_message({**_ACTIVITY, "from": {"id": "28:lazy-app"}}).author.is_me is True

    async def test_supports_synchronous_resolvers(self) -> None:
        adapter = _make_adapter(app_id=lambda: "sync-app")
        await adapter.initialize(_make_chat())
        assert adapter.bot_user_id == "28:sync-app"

    async def test_retries_a_failed_resolver_through_initialize(self) -> None:
        app_id = AsyncMock(side_effect=[RuntimeError("metadata unavailable"), "retry-app"])
        adapter = _make_adapter(app_id=app_id)
        chat = _make_chat()

        with pytest.raises(RuntimeError, match="metadata unavailable"):
            await adapter.initialize(chat)
        await adapter.initialize(chat)

        assert app_id.await_count == 2
        assert adapter.bot_user_id == "28:retry-app"

    @pytest.mark.parametrize("value", ["", "   ", None, 123])
    async def test_rejects_invalid_resolved_identity(self, value: Any) -> None:
        adapter = _make_adapter(app_id=lambda: value)
        with pytest.raises(ValidationError, match="appId resolver must return a nonempty string"):
            await adapter.initialize(_make_chat())
        # Nothing was built from the invalid value.
        assert adapter.bot_user_id is None

    async def test_retries_sdk_initialization_without_repeating_metadata_or_handlers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_initialize = App.initialize
        calls = {"n": 0}

        async def flaky_initialize(self: App) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("SDK unavailable")
            await real_initialize(self)

        monkeypatch.setattr(App, "initialize", flaky_initialize)
        app_id = AsyncMock(return_value="retry-app")
        adapter = _make_adapter(app_id=app_id)
        chat = _make_chat()

        with pytest.raises(RuntimeError, match="SDK unavailable"):
            await adapter.initialize(chat)
        await adapter.initialize(chat)

        assert calls["n"] == 2
        app_id.assert_awaited_once()
        assert adapter.register_count == 1
        # The retry completed the wiring the failed attempt never reached.
        assert adapter._bridge._handler is not None

    def test_preserves_immediate_bot_identity_for_string_and_environment_configuration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert _make_adapter().bot_user_id == "28:test-app"
        monkeypatch.setenv("TEAMS_APP_ID", "env-app")
        assert _make_adapter(app_id=None).bot_user_id == "28:env-app"

    async def test_reports_initialization_required_for_lazy_outbound_operations(self) -> None:
        adapter = _make_adapter(app_id=AsyncMock(return_value="lazy-app"))
        thread_id = adapter.encode_thread_id(
            _thread("19:channel@thread.tacv2", "https://smba.trafficmanager.net/teams/")
        )
        # Upstream exercises ``fetchThread`` (a Graph read there); the Python
        # ``fetch_thread`` is a pure thread-id decode, so use the Graph-backed
        # ``fetch_messages`` and the hand-rolled Bot Framework token path.
        with pytest.raises(ValidationError, match=r"Ensure chat\.initialize\(\) has completed"):
            await adapter.fetch_messages(thread_id)
        with pytest.raises(ValidationError, match=r"Ensure chat\.initialize\(\) has completed"):
            await adapter._get_access_token()

    async def test_cancelled_caller_does_not_cancel_shared_initialization(self) -> None:
        gate = asyncio.Event()

        async def app_id() -> str:
            await gate.wait()
            return "lazy-app"

        adapter = _make_adapter(app_id=app_id)
        chat = _make_chat()
        first = asyncio.get_running_loop().create_task(adapter.initialize(chat))
        second = asyncio.get_running_loop().create_task(adapter.initialize(chat))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        gate.set()
        await second

        assert adapter.bot_user_id == "28:lazy-app"
        assert adapter.register_count == 1


def _thread(conversation_id: str, service_url: str) -> Any:
    from chat_sdk.adapters.teams.types import TeamsThreadId

    return TeamsThreadId(conversation_id=conversation_id, service_url=service_url)


# ---------------------------------------------------------------------------
# Custom token precedence
# ---------------------------------------------------------------------------


class TestCustomTokenPrecedence:
    async def test_uses_custom_bot_framework_and_graph_tokens_despite_configured_and_environment_secrets(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLIENT_SECRET", "generic-secret")
        monkeypatch.setenv("TEAMS_APP_PASSWORD", "teams-secret")
        jwt = _unsigned_jwt()
        token = MagicMock(return_value=jwt)
        adapter = _make_adapter(app_password="explicit-secret", federated={"client_id": "identity"}, token=token)

        credentials = adapter._app.credentials
        assert isinstance(credentials, TokenCredentials)
        assert credentials.client_id == "test-app"
        assert credentials.token is token

        await adapter._app._get_bot_token()
        await adapter._app._get_graph_token("graph-tenant")

        scopes = [call.args for call in token.call_args_list]
        assert scopes[0][0] == "https://api.botframework.com/.default"
        assert isinstance(scopes[0][1], str)
        assert scopes[1] == ("https://graph.microsoft.com/.default", "graph-tenant")

    def test_token_factory_beats_client_secret_env_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The Python SDK checks ``client_id and client_secret`` (with a
        # ``CLIENT_SECRET`` env fallback) BEFORE ``client_id and token``; the
        # adapter's App subclass must still pick the factory.
        monkeypatch.setenv("CLIENT_SECRET", "generic-secret")
        monkeypatch.delenv("TEAMS_APP_PASSWORD", raising=False)
        token = MagicMock(return_value="factory-token")
        adapter = _make_adapter(token=token)

        credentials = adapter._app.credentials
        assert isinstance(credentials, TokenCredentials)
        assert credentials.token is token

    def test_client_secret_env_still_applies_without_a_factory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from microsoft_teams.api import ClientCredentials

        monkeypatch.setenv("CLIENT_SECRET", "generic-secret")
        monkeypatch.delenv("TEAMS_APP_PASSWORD", raising=False)
        adapter = _make_adapter(token=None)

        credentials = adapter._app.credentials
        assert isinstance(credentials, ClientCredentials)
        assert credentials.client_secret == "generic-secret"

    async def test_hand_rolled_token_paths_call_the_factory_without_the_password(self) -> None:
        calls: list[tuple[Any, Any]] = []

        async def token(scope: Any, tenant_id: Any) -> str:
            calls.append((scope, tenant_id))
            return f"tok-{len(calls)}"

        adapter = _make_adapter(app_password="explicit-secret", app_tenant_id="tenant-1", token=token)
        # Any HTTP use (the client-credentials POST) would fail loudly.
        adapter._get_http_session = AsyncMock(side_effect=AssertionError("no token HTTP call expected"))  # type: ignore[method-assign]

        assert await adapter._get_access_token() == "tok-1"
        assert await adapter._get_graph_token() == "tok-2"
        # Not cached: the factory owns token lifetime.
        assert await adapter._get_graph_token() == "tok-3"
        assert calls == [
            ("https://api.botframework.com/.default", "tenant-1"),
            ("https://graph.microsoft.com/.default", "tenant-1"),
            ("https://graph.microsoft.com/.default", "tenant-1"),
        ]

    @pytest.mark.parametrize(
        ("overrides", "expected"),
        [
            # MultiTenant omits the tenant from the SDK credentials, so the SDK
            # falls back to the cloud login tenant (Bot Framework) and
            # ``common`` (Graph); the hand-rolled paths must agree.
            (
                {"app_type": "MultiTenant", "app_tenant_id": "cust-tenant"},
                ("botframework.com", "common"),
            ),
            ({"app_type": "SingleTenant", "app_tenant_id": "cust-tenant"}, ("cust-tenant", "cust-tenant")),
        ],
    )
    async def test_hand_rolled_factory_tenant_matches_the_sdk(
        self, monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any], expected: tuple[str, str]
    ) -> None:
        monkeypatch.delenv("TENANT_ID", raising=False)
        monkeypatch.delenv("TEAMS_APP_TENANT_ID", raising=False)
        calls: list[tuple[Any, Any]] = []

        def token(scope: Any, tenant_id: Any) -> str:
            calls.append((scope, tenant_id))
            return _unsigned_jwt()

        adapter = _make_adapter(token=token, **overrides)
        await adapter._app._get_bot_token()
        await adapter._get_access_token()
        await adapter._app._get_graph_token()
        await adapter._get_graph_token()

        sdk_bot, ours_bot, sdk_graph, ours_graph = (tenant for _scope, tenant in calls)
        assert (sdk_bot, sdk_graph) == expected
        assert (ours_bot, ours_graph) == (sdk_bot, sdk_graph)

    async def test_hand_rolled_bot_token_uses_the_sdk_cloud_scope(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Under a sovereign cloud the SDK asks the factory for that cloud's Bot
        # Framework scope; ``open_dm`` posts to the same cloud's Connector, so
        # the hand-rolled path must ask for the same scope and tenant.
        monkeypatch.setenv("CLOUD", "USGov")
        monkeypatch.delenv("TENANT_ID", raising=False)
        calls: list[tuple[Any, Any]] = []

        def token(scope: Any, tenant_id: Any) -> str:
            calls.append((scope, tenant_id))
            return _unsigned_jwt()

        adapter = _make_adapter(token=token, app_type="MultiTenant")
        await adapter._app._get_bot_token()
        await adapter._get_access_token()

        assert calls[0] == ("https://api.botframework.us/.default", "MicrosoftServices.onmicrosoft.us")
        assert calls[1] == calls[0]

    @pytest.mark.parametrize("result", ["", None, 42])
    async def test_factory_without_a_token_raises_authentication_error(self, result: Any) -> None:
        adapter = _make_adapter(token=lambda _scope, _tenant: result)
        with pytest.raises(AuthenticationError, match="returned no token"):
            await adapter._get_access_token()

    async def test_raising_factory_raises_authentication_error(self) -> None:
        def token(_scope: Any, _tenant: Any) -> str:
            raise RuntimeError("bridge down")

        adapter = _make_adapter(token=token)
        with pytest.raises(AuthenticationError, match="bridge down"):
            await adapter._get_graph_token()
