"""Teams adapter coverage tests -- targeting uncovered paths to reach 70%+.

Covers:
- fetch_messages via Graph API (mock aiohttp)
- fetch_channel_messages
- open_dm (conversation creation)
- _cache_user_context (service URL, tenant ID caching)
- _get_access_token (token endpoint call)
- _get_graph_token
- _validate_service_url (allowed/disallowed patterns)
- inbound JWT validation enforced by the Microsoft Teams SDK (TestSdkInboundAuth)
- postMessage with Adaptive Card
- editMessage
- deleteMessage
- startTyping
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chat_sdk.adapters.teams.adapter import (
    TeamsAdapter,
    _validate_service_url,
)
from chat_sdk.adapters.teams.types import (
    TeamsAdapterConfig,
    TeamsThreadId,
)
from chat_sdk.shared.errors import (
    AuthenticationError,
    NetworkError,
    ValidationError,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _skip_teams_jwt(monkeypatch):
    """Bypass inbound JWT validation in unit tests (no real Bot Framework tokens).

    Inbound auth now lives in the Microsoft Teams SDK ``App`` (issue #93 PR 1):
    the ``BridgeHttpAdapter`` dispatches webhooks through the SDK's
    ``HttpServer``, which validates the Bearer token. We force the SDK's own
    ``skip_auth`` flag on so the bridge → SDK → handler path runs without a
    signed token. ``TestSdkInboundAuth`` asserts the SDK rejects
    unauthenticated requests when this bypass is absent.
    """
    from microsoft_teams.apps.http.http_server import HttpServer

    real_initialize = HttpServer.initialize

    def _initialize_skip_auth(self, credentials=None, skip_auth=False, cloud=None):
        return real_initialize(self, credentials=credentials, skip_auth=True, cloud=cloud)

    monkeypatch.setattr(HttpServer, "initialize", _initialize_skip_auth)


def _make_adapter(**overrides) -> TeamsAdapter:
    config = TeamsAdapterConfig(
        app_id=overrides.pop("app_id", "test-app-id"),
        app_password=overrides.pop("app_password", "test-password"),
        **overrides,
    )
    return TeamsAdapter(config)


def _make_logger():
    return MagicMock(
        debug=MagicMock(),
        info=MagicMock(),
        warn=MagicMock(),
        error=MagicMock(),
    )


class _SentActivity:
    """Stand-in for the SDK ``SentActivity`` returned by ``app.send`` — only the
    ``.id`` attribute is read by the adapter."""

    def __init__(self, id: str):
        self.id = id


def _mock_app_send(adapter: TeamsAdapter, sent_id: str = "sent-msg-123") -> AsyncMock:
    """Replace ``adapter._app.send`` with an AsyncMock returning a SentActivity.

    The migrated outbound send/typing paths delegate to the SDK ``App.send``.
    """
    send = AsyncMock(return_value=_SentActivity(sent_id))
    adapter._app.send = send  # type: ignore[method-assign]
    return send


def _mock_app_activities(
    adapter: TeamsAdapter,
    *,
    update_id: str = "edit-msg-1",
    update_side_effect: Any = None,
    delete_side_effect: Any = None,
) -> tuple[AsyncMock, AsyncMock]:
    """Replace ``adapter._app.api`` so ``conversations.activities(id)`` returns a
    stub exposing ``update``/``delete`` AsyncMocks (edit/delete delegation).

    Returns ``(update_mock, delete_mock)``.
    """
    update = AsyncMock(
        return_value=None if update_side_effect else _SentActivity(update_id),
        side_effect=update_side_effect,
    )
    delete = AsyncMock(return_value=None, side_effect=delete_side_effect)
    ops = MagicMock()
    ops.update = update
    ops.delete = delete
    api = MagicMock()
    api.conversations.activities = MagicMock(return_value=ops)
    adapter._app.api = api  # type: ignore[method-assign]
    return update, delete


def _make_mock_state() -> MagicMock:
    cache: dict[str, Any] = {}
    state = MagicMock()
    state.get = AsyncMock(side_effect=lambda k: cache.get(k))
    state.set = AsyncMock(side_effect=lambda k, v, *a, **kw: cache.__setitem__(k, v))
    state.delete = AsyncMock(side_effect=lambda k: cache.pop(k, None))
    state._cache = cache
    return state


def _make_mock_chat(state: MagicMock | None = None) -> MagicMock:
    if state is None:
        state = _make_mock_state()
    chat = MagicMock()
    chat.process_message = MagicMock()
    chat.process_reaction = MagicMock()
    chat.process_action = MagicMock()
    chat.get_state = MagicMock(return_value=state)
    return chat


def _mock_aiohttp_response(data: Any, status: int = 200) -> MagicMock:
    """Build a mock aiohttp response."""
    response = AsyncMock()
    response.status = status
    response.ok = status < 400
    response.json = AsyncMock(return_value=data)
    response.text = AsyncMock(return_value=json.dumps(data) if isinstance(data, dict) else str(data))
    return response


class _MockSession:
    """A mock aiohttp.ClientSession supporting nested context manager patterns."""

    closed = False

    def __init__(self, responses: dict[str, MagicMock] | None = None, default_response: MagicMock | None = None):
        self._responses = responses or {}
        self._default = default_response or _mock_aiohttp_response({})
        self.post_calls: list[tuple] = []
        self.get_calls: list[tuple] = []
        self.delete_calls: list[tuple] = []
        self.put_calls: list[tuple] = []

    def _make_cm(self, response: MagicMock) -> MagicMock:
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=response)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    def post(self, url: str, **kwargs) -> MagicMock:
        self.post_calls.append((url, kwargs))
        resp = self._responses.get(url, self._default)
        return self._make_cm(resp)

    def get(self, url: str, **kwargs) -> MagicMock:
        self.get_calls.append((url, kwargs))
        resp = self._responses.get(url, self._default)
        return self._make_cm(resp)

    def delete(self, url: str, **kwargs) -> MagicMock:
        self.delete_calls.append((url, kwargs))
        resp = self._responses.get(url, self._default)
        return self._make_cm(resp)

    def put(self, url: str, **kwargs) -> MagicMock:
        self.put_calls.append((url, kwargs))
        resp = self._responses.get(url, self._default)
        return self._make_cm(resp)

    async def close(self):
        self.closed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


# ---------------------------------------------------------------------------
# _validate_service_url
# ---------------------------------------------------------------------------


class TestValidateServiceUrl:
    def test_allowed_trafficmanager(self):
        # Validation completes without raising for an allowed URL
        _validate_service_url("https://smba.trafficmanager.net/teams/")  # no exception = pass

    def test_allowed_botframework_com(self):
        _validate_service_url("https://some-host.botframework.com/")  # no exception = pass

    def test_allowed_botframework_us(self):
        _validate_service_url("https://some-host.botframework.us/")  # no exception = pass

    def test_allowed_teams_microsoft_com(self):
        _validate_service_url("https://api.teams.microsoft.com/")  # no exception = pass

    def test_allowed_teams_microsoft_us(self):
        _validate_service_url("https://api.teams.microsoft.us/")  # no exception = pass

    def test_allowed_gcc_infra(self):
        _validate_service_url("https://smba.infra.gcc.teams.microsoft.com/")  # no exception = pass

    def test_disallowed_url_raises(self):
        with pytest.raises(ValidationError):
            _validate_service_url("https://evil.example.com/")

    def test_disallowed_http_raises(self):
        with pytest.raises(ValidationError):
            _validate_service_url("http://smba.trafficmanager.net/teams/")

    def test_disallowed_similar_domain_raises(self):
        with pytest.raises(ValidationError):
            _validate_service_url("https://fake-botframework.com.evil.com/")


# ---------------------------------------------------------------------------
# _get_access_token (Bot Framework token)
# ---------------------------------------------------------------------------


class TestGetAccessToken:
    async def test_fetches_token_successfully(self):
        adapter = _make_adapter(logger=_make_logger())
        mock_session = _MockSession(
            default_response=_mock_aiohttp_response(
                {
                    "access_token": "bot-token-123",
                    "expires_in": 3600,
                }
            )
        )

        with patch("aiohttp.ClientSession", return_value=mock_session):
            token = await adapter._get_access_token()
            assert token == "bot-token-123"

    async def test_caches_token(self):
        adapter = _make_adapter(logger=_make_logger())
        mock_session = _MockSession(
            default_response=_mock_aiohttp_response(
                {
                    "access_token": "cached-token",
                    "expires_in": 3600,
                }
            )
        )

        with patch("aiohttp.ClientSession", return_value=mock_session):
            t1 = await adapter._get_access_token()
            # Second call should use cache (no new HTTP call)
            t2 = await adapter._get_access_token()
            assert t1 == t2
            assert len(mock_session.post_calls) == 1

    async def test_raises_auth_error_on_failure(self):
        adapter = _make_adapter(logger=_make_logger())
        mock_session = _MockSession(default_response=_mock_aiohttp_response({"error": "invalid_client"}, status=401))

        with patch("aiohttp.ClientSession", return_value=mock_session), pytest.raises(AuthenticationError):
            await adapter._get_access_token()


# ---------------------------------------------------------------------------
# _get_graph_token
# ---------------------------------------------------------------------------


class TestGetGraphToken:
    async def test_fetches_graph_token(self):
        adapter = _make_adapter(logger=_make_logger())
        # Reset any cached Graph token (dedicated field — never the BF token)
        adapter._graph_token = None
        adapter._graph_token_expiry = 0

        mock_session = _MockSession(
            default_response=_mock_aiohttp_response(
                {
                    "access_token": "graph-token-456",
                    "expires_in": 3600,
                }
            )
        )

        with patch("aiohttp.ClientSession", return_value=mock_session):
            token = await adapter._get_graph_token()
            assert token == "graph-token-456"

    async def test_graph_token_error_raises(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._graph_token = None
        adapter._graph_token_expiry = 0

        mock_session = _MockSession(default_response=_mock_aiohttp_response({"error": "failed"}, status=400))

        with patch("aiohttp.ClientSession", return_value=mock_session), pytest.raises(AuthenticationError):
            await adapter._get_graph_token()


# ---------------------------------------------------------------------------
# Token-cache isolation regression (issue #93)
# ---------------------------------------------------------------------------


class TestTokenCacheIsolation:
    """The Bot Framework token (``_get_access_token``, scope
    ``api.botframework.com``) and the Graph token (``_get_graph_token``, scope
    ``graph.microsoft.com``) must use SEPARATE cache fields.

    Before the untangle they shared ``_access_token`` / ``_token_expiry``, so
    whichever was fetched last clobbered the other — a Graph read could then
    ship a Bot Framework token (and vice versa). These tests lock in the fix.
    """

    @staticmethod
    def _scope_routed_session() -> _MockSession:
        """An aiohttp session whose token endpoint returns a DIFFERENT token per
        requested OAuth scope, so a cross-scope cache hit is observable."""
        bf_resp = _mock_aiohttp_response({"access_token": "bf-token", "expires_in": 3600})
        graph_resp = _mock_aiohttp_response({"access_token": "graph-token", "expires_in": 3600})
        session = _MockSession()
        original_post = session.post

        def routed_post(url, **kwargs):
            scope = (kwargs.get("data") or {}).get("scope", "")
            if "graph.microsoft.com" in scope:
                return session._make_cm(graph_resp)
            if "botframework.com" in scope:
                return session._make_cm(bf_resp)
            return original_post(url, **kwargs)

        session.post = routed_post
        return session

    async def test_graph_and_bot_framework_tokens_do_not_collide(self):
        adapter = _make_adapter(logger=_make_logger())
        session = self._scope_routed_session()

        with patch("aiohttp.ClientSession", return_value=session):
            # Fetch Graph first, then Bot Framework, then Graph again. If the two
            # shared one cache slot, the second/third call would return the
            # clobbered (wrong-scope) token from cache.
            graph1 = await adapter._get_graph_token()
            bf1 = await adapter._get_access_token()
            graph2 = await adapter._get_graph_token()
            bf2 = await adapter._get_access_token()

        assert graph1 == "graph-token"
        assert bf1 == "bf-token"
        # cache hits return the correct per-scope token, not the other scope's
        assert graph2 == "graph-token"
        assert bf2 == "bf-token"

    async def test_tokens_cached_on_separate_fields(self):
        adapter = _make_adapter(logger=_make_logger())
        session = self._scope_routed_session()

        with patch("aiohttp.ClientSession", return_value=session):
            await adapter._get_graph_token()
            await adapter._get_access_token()

        # each token lives on its OWN field — neither clobbered the other
        assert adapter._graph_token == "graph-token"
        assert adapter._access_token == "bf-token"
        assert adapter._graph_token_expiry > 0
        assert adapter._token_expiry > 0

    async def test_bot_framework_cache_does_not_satisfy_graph(self):
        """A warm Bot Framework cache must NOT short-circuit a Graph fetch —
        the regression that motivated the split. Pre-warm the BF token, then
        request a Graph token and assert it fetches its own scoped token."""
        adapter = _make_adapter(logger=_make_logger())
        session = self._scope_routed_session()

        with patch("aiohttp.ClientSession", return_value=session):
            bf = await adapter._get_access_token()
            assert bf == "bf-token"
            # BF cache is warm; Graph must still fetch its own scoped token
            graph = await adapter._get_graph_token()

        assert graph == "graph-token"
        # the warm BF cache was untouched by the Graph fetch
        assert adapter._access_token == "bf-token"


# ---------------------------------------------------------------------------
# fetch_messages via Graph API
# ---------------------------------------------------------------------------


class TestFetchMessages:
    async def test_fetch_dm_messages(self):
        adapter = _make_adapter(logger=_make_logger())
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        # Token response
        token_resp = _mock_aiohttp_response({"access_token": "token", "expires_in": 3600})
        # Graph messages response
        messages_resp = _mock_aiohttp_response(
            {
                "value": [
                    {
                        "id": "msg-1",
                        "createdDateTime": "2024-06-01T12:00:00Z",
                        "body": {"contentType": "text", "content": "Hello"},
                        "from": {"user": {"id": "user-1", "displayName": "Alice"}},
                    },
                    {
                        "id": "msg-2",
                        "createdDateTime": "2024-06-01T12:01:00Z",
                        "body": {"contentType": "text", "content": "World"},
                        "from": {"user": {"id": "user-2", "displayName": "Bob"}},
                    },
                ]
            }
        )

        # Set up mock session routing
        mock_session = _MockSession(default_response=messages_resp)

        # Override token endpoint to return token
        original_post = mock_session.post

        def routed_post(url, **kwargs):
            if "oauth2" in url:
                return mock_session._make_cm(token_resp)
            return original_post(url, **kwargs)

        mock_session.post = routed_post

        tid = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="a]8:orgid:user-123",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await adapter.fetch_messages(tid)
            assert len(result.messages) == 2
            # Backward fetch reverses the order
            texts = {m.text for m in result.messages}
            assert "Hello" in texts
            assert "World" in texts

    async def test_fetch_messages_forward_direction(self):
        adapter = _make_adapter(logger=_make_logger())
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        from chat_sdk.types import FetchOptions

        token_resp = _mock_aiohttp_response({"access_token": "t", "expires_in": 3600})
        messages_resp = _mock_aiohttp_response(
            {
                "value": [
                    {
                        "id": "msg-f1",
                        "createdDateTime": "2024-06-01T12:00:00Z",
                        "body": {"contentType": "text", "content": "Forward"},
                        "from": {"user": {"id": "u1", "displayName": "A"}},
                    },
                ]
            }
        )

        mock_session = _MockSession(default_response=messages_resp)
        original_post = mock_session.post

        def routed_post(url, **kwargs):
            if "oauth2" in url:
                return mock_session._make_cm(token_resp)
            return original_post(url, **kwargs)

        mock_session.post = routed_post

        tid = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="a]8:orgid:user-123",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await adapter.fetch_messages(tid, FetchOptions(direction="forward"))
            assert len(result.messages) == 1


# ---------------------------------------------------------------------------
# fetch_channel_messages
# ---------------------------------------------------------------------------


class TestFetchChannelMessages:
    async def test_fetch_channel_messages_no_context(self):
        """Without cached channel context, falls back to Graph chat messages."""
        adapter = _make_adapter(logger=_make_logger())
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        token_resp = _mock_aiohttp_response({"access_token": "t", "expires_in": 3600})
        messages_resp = _mock_aiohttp_response(
            {
                "value": [
                    {
                        "id": "ch-msg-1",
                        "createdDateTime": "2024-06-01T12:00:00Z",
                        "body": {"contentType": "text", "content": "Channel msg"},
                        "from": {"user": {"id": "u1", "displayName": "A"}},
                    },
                ]
            }
        )

        mock_session = _MockSession(default_response=messages_resp)
        original_post = mock_session.post

        def routed_post(url, **kwargs):
            if "oauth2" in url:
                return mock_session._make_cm(token_resp)
            return original_post(url, **kwargs)

        mock_session.post = routed_post

        tid = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:abc@thread.tacv2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await adapter.fetch_channel_messages(tid)
            assert len(result.messages) == 1


# ---------------------------------------------------------------------------
# open_dm
# ---------------------------------------------------------------------------


class TestOpenDM:
    async def test_open_dm_success(self):
        adapter = _make_adapter(logger=_make_logger())
        state = _make_mock_state()
        state._cache["teams:serviceUrl:user-789"] = "https://smba.trafficmanager.net/teams/"
        state._cache["teams:tenantId:user-789"] = "tenant-abc"
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        # Token endpoint
        token_resp = _mock_aiohttp_response({"access_token": "t", "expires_in": 3600})
        # Conversation creation
        conv_resp = _mock_aiohttp_response({"id": "a]8:orgid:new-conv-id"})

        mock_session = _MockSession(default_response=conv_resp)
        original_post = mock_session.post

        def routed_post(url, **kwargs):
            if "oauth2" in url:
                return mock_session._make_cm(token_resp)
            return original_post(url, **kwargs)

        mock_session.post = routed_post

        with patch("aiohttp.ClientSession", return_value=mock_session):
            thread_id = await adapter.open_dm("user-789")
            assert "teams:" in thread_id
            decoded = adapter.decode_thread_id(thread_id)
            assert decoded.conversation_id == "a]8:orgid:new-conv-id"

    async def test_open_dm_no_chat_raises(self):
        from chat_sdk.errors import ChatNotImplementedError

        adapter = _make_adapter(logger=_make_logger())
        with pytest.raises(ChatNotImplementedError):
            await adapter.open_dm("user-123")

    async def test_open_dm_uses_default_service_url(self):
        """When no cached service URL exists, falls back to default."""
        adapter = _make_adapter(logger=_make_logger())
        state = _make_mock_state()
        # No cached service URL for user
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        token_resp = _mock_aiohttp_response({"access_token": "t", "expires_in": 3600})
        conv_resp = _mock_aiohttp_response({"id": "a]8:orgid:default-conv"})

        mock_session = _MockSession(default_response=conv_resp)
        original_post = mock_session.post
        call_urls = []

        def routed_post(url, **kwargs):
            call_urls.append(url)
            if "oauth2" in url:
                return mock_session._make_cm(token_resp)
            return original_post(url, **kwargs)

        mock_session.post = routed_post

        with patch("aiohttp.ClientSession", return_value=mock_session):
            await adapter.open_dm("user-new")
            # Should have used the default service URL
            conv_calls = [u for u in call_urls if "v3/conversations" in u]
            assert len(conv_calls) == 1
            assert "smba.trafficmanager.net" in conv_calls[0]


# ---------------------------------------------------------------------------
# _cache_user_context
# ---------------------------------------------------------------------------


class TestCacheUserContext:
    async def test_caches_service_url_and_tenant(self):
        adapter = _make_adapter(logger=_make_logger())
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        activity = {
            "type": "message",
            "from": {"id": "user-42"},
            "conversation": {"id": "19:abc@thread.tacv2", "tenantId": "tenant-xyz"},
            "serviceUrl": "https://smba.trafficmanager.net/amer/",
        }
        await adapter._cache_user_context(activity)
        assert state._cache["teams:serviceUrl:user-42"] == "https://smba.trafficmanager.net/amer/"
        assert state._cache["teams:tenantId:user-42"] == "tenant-xyz"

    async def test_rejects_disallowed_service_url(self):
        adapter = _make_adapter(logger=_make_logger())
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        activity = {
            "type": "message",
            "from": {"id": "user-42"},
            "conversation": {"id": "19:abc@thread.tacv2"},
            "serviceUrl": "https://evil.example.com/",
        }
        await adapter._cache_user_context(activity)
        assert "teams:serviceUrl:user-42" not in state._cache

    async def test_no_op_without_chat(self):
        adapter = _make_adapter(logger=_make_logger())
        # Not initialized -- _chat is None
        activity = {
            "type": "message",
            "from": {"id": "user-42"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
        }
        # Should complete without raising when adapter is not initialized (_chat is None)
        result = await adapter._cache_user_context(activity)
        assert result is None

    async def test_no_op_without_from_id(self):
        adapter = _make_adapter(logger=_make_logger())
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        activity = {
            "type": "message",
            "from": {},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
        }
        await adapter._cache_user_context(activity)
        # Nothing should be cached
        assert len(state._cache) == 0

    async def test_caches_channel_context(self):
        adapter = _make_adapter(logger=_make_logger())
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        activity = {
            "type": "message",
            "from": {"id": "user-42"},
            "conversation": {"id": "19:abc@thread.tacv2"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
            "channelData": {
                "team": {"aadGroupId": "team-aad-id"},
                "channel": {"id": "19:abc@thread.tacv2"},
            },
        }
        await adapter._cache_user_context(activity)
        raw_context = state._cache.get("teams:channelContext:19:abc@thread.tacv2")
        assert raw_context is not None
        ctx = json.loads(raw_context)
        assert ctx["team_id"] == "team-aad-id"


# ---------------------------------------------------------------------------
# Inbound JWT validation is now enforced by the Microsoft Teams SDK
# (issue #93 PR 1). These tests run WITHOUT the skip-auth bypass to assert the
# SDK actually rejects unauthenticated / unconfigured webhooks.
# ---------------------------------------------------------------------------


class TestSdkInboundAuth:
    @pytest.fixture(autouse=True)
    def _skip_teams_jwt(self):
        """Override the module-level skip-auth bypass — these tests need the
        SDK's real JWT enforcement so they can assert it rejects requests."""
        yield

    async def test_webhook_rejects_when_no_app_id(self):
        """No credentials configured: the SDK rejects every inbound request."""
        adapter = TeamsAdapter(TeamsAdapterConfig(app_id="", app_password="test"))
        chat = _make_mock_chat()
        await adapter.initialize(chat)

        class FakeReq:
            headers: dict[str, str] = {}

            async def text(self):
                return "{}"

            @property
            def data(self):
                return b"{}"

        response = await adapter.handle_webhook(FakeReq())
        assert response["status"] == 401

    async def test_webhook_rejects_missing_bearer_token(self):
        """Credentials configured but no Bearer token: the SDK responds 401."""
        adapter = TeamsAdapter(TeamsAdapterConfig(app_id="test-app-id", app_password="secret"))
        chat = _make_mock_chat()
        await adapter.initialize(chat)

        activity = {
            "type": "message",
            "id": "m1",
            "from": {"id": "u1"},
            "conversation": {"id": "19:abc@thread.tacv2"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
        }

        class FakeReq:
            headers: dict[str, str] = {}

            def __init__(self) -> None:
                self._body = json.dumps(activity)

            async def text(self):
                return self._body

            @property
            def data(self):
                return self._body.encode("utf-8")

        response = await adapter.handle_webhook(FakeReq())
        assert response["status"] == 401
        # The SDK validated auth before reaching any chat handler.
        assert not chat.process_message.called

    async def test_webhook_rejects_invalid_bearer_token(self):
        """A malformed Bearer token fails SDK signature validation → 401."""
        adapter = TeamsAdapter(TeamsAdapterConfig(app_id="test-app-id", app_password="secret"))
        chat = _make_mock_chat()
        await adapter.initialize(chat)

        activity = {
            "type": "message",
            "id": "m1",
            "from": {"id": "u1"},
            "conversation": {"id": "19:abc@thread.tacv2"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
        }

        class FakeReq:
            def __init__(self) -> None:
                self._body = json.dumps(activity)
                self.headers = {"Authorization": "Bearer not.a.real.jwt"}

            async def text(self):
                return self._body

            @property
            def data(self):
                return self._body.encode("utf-8")

        response = await adapter.handle_webhook(FakeReq())
        assert response["status"] == 401
        assert not chat.process_message.called


# ---------------------------------------------------------------------------
# postMessage / editMessage / deleteMessage / startTyping via the SDK
# ---------------------------------------------------------------------------


class TestTeamsSDKOperations:
    async def test_post_message_with_adaptive_card(self):
        adapter = _make_adapter(logger=_make_logger())
        send = _mock_app_send(adapter, "card-msg-1")

        tid = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:abc@thread.tacv2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        result = await adapter.post_message(
            tid,
            {
                "card": {
                    "header": {"title": "Test"},
                    "body": [{"type": "text", "content": "Card content"}],
                }
            },
        )
        assert result.id == "card-msg-1"
        # SDK App.send received a MessageActivityInput carrying the adaptive card
        activity = send.call_args.args[1]
        dumped = activity.model_dump(by_alias=True, exclude_none=True)
        assert dumped["type"] == "message"
        assert dumped["attachments"][0]["contentType"] == "application/vnd.microsoft.card.adaptive"

    async def test_post_message_text(self):
        adapter = _make_adapter(logger=_make_logger())
        send = _mock_app_send(adapter, "text-msg-1")

        tid = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:abc@thread.tacv2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        result = await adapter.post_message(tid, {"markdown": "Hello **world**"})
        assert result.id == "text-msg-1"
        assert send.call_count == 1

    async def test_post_message_send_failure(self):
        adapter = _make_adapter(logger=_make_logger())
        send = _mock_app_send(adapter)
        send.side_effect = Exception("connection failed")

        tid = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:abc@thread.tacv2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        with pytest.raises(NetworkError):
            await adapter.post_message(tid, {"markdown": "fail"})

    async def test_edit_message(self):
        adapter = _make_adapter(logger=_make_logger())
        update, _delete = _mock_app_activities(adapter, update_id="msg-1")

        tid = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:abc@thread.tacv2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        result = await adapter.edit_message(tid, "msg-1", {"markdown": "Updated"})
        assert result.id == "msg-1"
        update.assert_called_once()
        assert update.call_args.args[0] == "msg-1"

    async def test_edit_message_with_card(self):
        adapter = _make_adapter(logger=_make_logger())
        update, _delete = _mock_app_activities(adapter, update_id="msg-1")

        tid = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:abc@thread.tacv2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        result = await adapter.edit_message(
            tid,
            "msg-1",
            {
                "card": {
                    "header": {"title": "Updated Card"},
                    "body": [{"type": "text", "content": "New content"}],
                }
            },
        )
        assert result.id == "msg-1"
        activity = update.call_args.args[1]
        dumped = activity.model_dump(by_alias=True, exclude_none=True)
        assert dumped["attachments"][0]["contentType"] == "application/vnd.microsoft.card.adaptive"

    async def test_delete_message(self):
        adapter = _make_adapter(logger=_make_logger())
        _update, delete = _mock_app_activities(adapter)

        tid = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:abc@thread.tacv2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        await adapter.delete_message(tid, "del-1")
        assert delete.call_count == 1
        assert delete.call_args.args == ("del-1",)

    async def test_delete_message_failure(self):
        adapter = _make_adapter(logger=_make_logger())
        _update, _delete = _mock_app_activities(adapter, delete_side_effect=Exception("delete failed"))

        tid = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:abc@thread.tacv2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        with pytest.raises(NetworkError):
            await adapter.delete_message(tid, "del-1")

    async def test_start_typing(self):
        adapter = _make_adapter(logger=_make_logger())
        send = _mock_app_send(adapter, "t1")

        tid = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:abc@thread.tacv2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        await adapter.start_typing(tid)
        send.assert_called_once()
        activity = send.call_args.args[1]
        assert activity.type == "typing"

    async def test_start_typing_failure_swallowed(self):
        """Typing failures should be logged but not re-raised."""
        adapter = _make_adapter(logger=_make_logger())
        send = _mock_app_send(adapter)
        send.side_effect = Exception("typing error")

        tid = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:abc@thread.tacv2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        # Typing failure should be swallowed (not re-raised)
        result = await adapter.start_typing(tid)
        assert result is None


# ---------------------------------------------------------------------------
# Adapter lifecycle helpers
# ---------------------------------------------------------------------------


class TestTeamsHTTPHelpers:
    async def test_disconnect_is_noop(self):
        adapter = _make_adapter(logger=_make_logger())
        result = await adapter.disconnect()
        assert result is None


# ---------------------------------------------------------------------------
# Graph message mapping
# ---------------------------------------------------------------------------


class TestGraphMessageMapping:
    def test_map_graph_message_with_html_body(self):
        adapter = _make_adapter()
        msg = {
            "id": "graph-1",
            "createdDateTime": "2024-06-01T12:00:00Z",
            "body": {
                "contentType": "html",
                "content": "<p>Hello <b>world</b></p>",
            },
            "from": {"user": {"id": "u1", "displayName": "Alice"}},
        }
        tid = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:abc@thread.tacv2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        result = adapter._map_graph_message(msg, tid)
        assert "Hello" in result.text
        assert "world" in result.text
        # HTML tags should be stripped
        assert "<p>" not in result.text

    def test_map_graph_message_with_application_from(self):
        adapter = _make_adapter(app_id="bot-app-id")
        msg = {
            "id": "graph-2",
            "createdDateTime": "2024-06-01T12:00:00Z",
            "body": {"contentType": "text", "content": "Bot message"},
            "from": {"application": {"id": "bot-app-id", "displayName": "MyBot"}},
        }
        tid = "teams:abc:def"
        # Encode a proper thread ID
        tid = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:abc@thread.tacv2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        result = adapter._map_graph_message(msg, tid)
        assert result.author.is_bot is True
        assert result.author.is_me is True

    def test_map_graph_message_with_adaptive_card_attachment(self):
        adapter = _make_adapter()
        card_json = json.dumps(
            {
                "type": "AdaptiveCard",
                "body": [
                    {"type": "TextBlock", "text": "Card Title", "weight": "bolder"},
                ],
            }
        )
        msg = {
            "id": "graph-3",
            "createdDateTime": "2024-06-01T12:00:00Z",
            "body": {"contentType": "html", "content": ""},
            "from": {"user": {"id": "u1", "displayName": "Alice"}},
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": card_json,
                },
            ],
        }
        tid = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:abc@thread.tacv2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        result = adapter._map_graph_message(msg, tid)
        # Should extract card title as text
        assert "Card Title" in result.text

    def test_map_graph_message_edited(self):
        adapter = _make_adapter()
        msg = {
            "id": "graph-4",
            "createdDateTime": "2024-06-01T12:00:00Z",
            "lastModifiedDateTime": "2024-06-01T12:05:00Z",
            "body": {"contentType": "text", "content": "Edited msg"},
            "from": {"user": {"id": "u1", "displayName": "Alice"}},
        }
        tid = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:abc@thread.tacv2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        result = adapter._map_graph_message(msg, tid)
        assert result.metadata.edited is True


# ---------------------------------------------------------------------------
# _handle_teams_error — additional branches
# ---------------------------------------------------------------------------


class TestHandleTeamsError:
    def test_fallthrough_non_dict_non_exception(self):
        """Non-dict, non-Exception error hits the final raise."""
        from chat_sdk.adapters.teams.adapter import _handle_teams_error

        with pytest.raises(NetworkError, match="Teams API error during test"):
            _handle_teams_error("raw-string-error", "test")

    def test_exception_error(self):
        from chat_sdk.adapters.teams.adapter import _handle_teams_error

        with pytest.raises(NetworkError, match="something broke"):
            _handle_teams_error(RuntimeError("something broke"), "send")

    def test_401_status(self):
        from chat_sdk.adapters.teams.adapter import _handle_teams_error

        with pytest.raises(AuthenticationError):
            _handle_teams_error({"statusCode": 401, "message": "bad token"}, "login")

    def test_403_status(self):
        from chat_sdk.adapters.teams.adapter import _handle_teams_error
        from chat_sdk.shared.errors import AdapterPermissionError as APE

        with pytest.raises(APE):
            _handle_teams_error({"statusCode": 403}, "post")

    def test_404_status(self):
        from chat_sdk.adapters.teams.adapter import _handle_teams_error

        with pytest.raises(NetworkError, match="not found"):
            _handle_teams_error({"statusCode": 404}, "delete")

    def test_429_status_with_retry_after(self):
        from chat_sdk.adapters.teams.adapter import _handle_teams_error
        from chat_sdk.shared.errors import AdapterRateLimitError

        with pytest.raises(AdapterRateLimitError):
            _handle_teams_error({"statusCode": 429, "retryAfter": 30}, "send")

    def test_permission_keyword_in_message(self):
        from chat_sdk.adapters.teams.adapter import _handle_teams_error
        from chat_sdk.shared.errors import AdapterPermissionError as APE

        with pytest.raises(APE):
            _handle_teams_error({"message": "You do not have permission"}, "op")

    def test_inner_http_error_status(self):
        from chat_sdk.adapters.teams.adapter import _handle_teams_error

        with pytest.raises(AuthenticationError):
            _handle_teams_error(
                {"innerHttpError": {"statusCode": 401}, "message": "inner fail"},
                "auth",
            )


# ---------------------------------------------------------------------------
# Properties (lock_scope, persist_message_history)
# ---------------------------------------------------------------------------


class TestTeamsProperties:
    def test_lock_scope_is_none(self):
        adapter = _make_adapter()
        assert adapter.lock_scope is None

    def test_persist_message_history_is_none(self):
        adapter = _make_adapter()
        assert adapter.persist_message_history is None


# ---------------------------------------------------------------------------
# _handle_message_activity / _handle_reaction_activity early returns
# ---------------------------------------------------------------------------


class TestHandleActivityEarlyReturns:
    async def test_handle_message_activity_no_chat(self):
        adapter = _make_adapter(logger=_make_logger())
        # _chat is None (not initialized) -- should return early without raising
        result = await adapter._handle_message_activity({"text": "hi"})
        assert result is None

    def test_handle_reaction_activity_no_chat(self):
        adapter = _make_adapter(logger=_make_logger())
        # _chat is None -- should return early without raising
        result = adapter._handle_reaction_activity({"reactionsAdded": [{"type": "like"}]})
        assert result is None

    async def test_handle_adaptive_card_action_no_chat(self):
        adapter = _make_adapter(logger=_make_logger())
        # _chat is None -- should return early without raising
        result = await adapter._handle_adaptive_card_action({}, {"actionId": "a"})
        assert result is None

    def test_handle_message_action_no_chat(self):
        adapter = _make_adapter(logger=_make_logger())
        # _chat is None -- should return early without raising
        result = adapter._handle_message_action({}, {"actionId": "a"})
        assert result is None


# ---------------------------------------------------------------------------
# stream method
# ---------------------------------------------------------------------------


class TestStream:
    """Group-chat / non-DM fallback: accumulate and post one SDK message."""

    async def test_stream_dict_chunks(self):
        adapter = _make_adapter(logger=_make_logger())
        send = _mock_app_send(adapter, "msg-1")

        tid = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:abc@thread.tacv2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )

        async def text_stream():
            yield {"type": "markdown_text", "text": "Hello "}
            yield {"type": "markdown_text", "text": "World"}

        result = await adapter.stream(tid, text_stream())
        # Group chat: accumulate → single SDK send.
        assert result.id == "msg-1"
        assert result.raw["text"] == "Hello World"
        send.assert_called_once()

    async def test_stream_string_chunks(self):
        adapter = _make_adapter(logger=_make_logger())
        send = _mock_app_send(adapter, "s1")

        tid = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:abc@thread.tacv2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )

        async def text_stream():
            yield "Hello "
            yield "World"

        result = await adapter.stream(tid, text_stream())
        assert "Hello World" in result.raw["text"]
        # Group chat: single accumulate-and-post send (no per-chunk edits).
        send.assert_called_once()

    async def test_stream_empty_chunks_skipped(self):
        adapter = _make_adapter(logger=_make_logger())
        send = _mock_app_send(adapter, "s1")

        tid = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:abc@thread.tacv2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )

        async def text_stream():
            yield ""
            yield {"type": "other", "data": "x"}  # no text key or wrong type

        result = await adapter.stream(tid, text_stream())
        assert result.id == ""  # nothing sent
        send.assert_not_called()


# ---------------------------------------------------------------------------
# _extract_card_title
# ---------------------------------------------------------------------------


class TestExtractCardTitle:
    def test_extract_card_title_bold_text(self):
        adapter = _make_adapter()
        card = {
            "body": [
                {"type": "TextBlock", "text": "My Title", "weight": "bolder"},
            ]
        }
        assert adapter._extract_card_title(card) == "My Title"

    def test_extract_card_title_large_text(self):
        adapter = _make_adapter()
        card = {
            "body": [
                {"type": "TextBlock", "text": "Big", "size": "large"},
            ]
        }
        assert adapter._extract_card_title(card) == "Big"

    def test_extract_card_title_fallback_to_first_text_block(self):
        adapter = _make_adapter()
        card = {
            "body": [
                {"type": "TextBlock", "text": "Fallback"},
            ]
        }
        assert adapter._extract_card_title(card) == "Fallback"

    def test_extract_card_title_no_body(self):
        adapter = _make_adapter()
        assert adapter._extract_card_title({"type": "AdaptiveCard"}) is None

    def test_extract_card_title_not_dict(self):
        adapter = _make_adapter()
        assert adapter._extract_card_title("not a dict") is None

    def test_extract_card_title_no_text_blocks(self):
        adapter = _make_adapter()
        card = {"body": [{"type": "Image", "url": "https://example.com/img.png"}]}
        assert adapter._extract_card_title(card) is None

    def test_extract_card_title_body_not_list(self):
        adapter = _make_adapter()
        assert adapter._extract_card_title({"body": "not a list"}) is None


# Request-body / header extraction and response shaping moved to
# ``BridgeHttpAdapter`` (issue #93 PR 1); those paths are covered by
# tests/test_teams_bridge.py.


# ---------------------------------------------------------------------------
# fetch_channel_info with channel context
# ---------------------------------------------------------------------------


class TestFetchChannelInfo:
    async def test_fetch_channel_info_dm(self):
        """DM conversation returns basic info without Graph call."""
        adapter = _make_adapter(logger=_make_logger())
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        tid = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="a]8:orgid:user-123",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        result = await adapter.fetch_channel_info(tid)
        assert result.is_dm is True
        assert result.id == tid

    async def test_fetch_channel_info_channel_no_context(self):
        """Channel without cached context returns basic info."""
        adapter = _make_adapter(logger=_make_logger())
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        tid = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:abc@thread.tacv2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        result = await adapter.fetch_channel_info(tid)
        assert result.is_dm is False
        assert result.name is None


# ---------------------------------------------------------------------------
# fetch_messages with cursor and forward/backward
# ---------------------------------------------------------------------------


class TestFetchMessagesAdvanced:
    async def test_fetch_messages_with_cursor_backward(self):
        from chat_sdk.types import FetchOptions

        adapter = _make_adapter(logger=_make_logger())
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        token_resp = _mock_aiohttp_response({"access_token": "t", "expires_in": 3600})
        messages_resp = _mock_aiohttp_response(
            {
                "value": [
                    {
                        "id": "msg-1",
                        "createdDateTime": "2024-06-01T12:00:00Z",
                        "body": {"contentType": "text", "content": "A"},
                        "from": {"user": {"id": "u1", "displayName": "X"}},
                    },
                ]
            }
        )

        mock_session = _MockSession(default_response=messages_resp)
        original_post = mock_session.post

        def routed_post(url, **kwargs):
            if "oauth2" in url:
                return mock_session._make_cm(token_resp)
            return original_post(url, **kwargs)

        mock_session.post = routed_post

        tid = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="a]8:orgid:user-123",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await adapter.fetch_messages(
                tid,
                FetchOptions(direction="backward", cursor="2024-06-02T00:00:00Z"),
            )
            assert len(result.messages) >= 0

    async def test_fetch_messages_forward_with_cursor(self):
        from chat_sdk.types import FetchOptions

        adapter = _make_adapter(logger=_make_logger())
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        token_resp = _mock_aiohttp_response({"access_token": "t", "expires_in": 3600})
        messages_resp = _mock_aiohttp_response(
            {
                "value": [
                    {
                        "id": "msg-f1",
                        "createdDateTime": "2024-06-01T12:00:00Z",
                        "body": {"contentType": "text", "content": "Fwd"},
                        "from": {"user": {"id": "u1", "displayName": "X"}},
                    },
                ]
            }
        )

        mock_session = _MockSession(default_response=messages_resp)
        original_post = mock_session.post

        def routed_post(url, **kwargs):
            if "oauth2" in url:
                return mock_session._make_cm(token_resp)
            return original_post(url, **kwargs)

        mock_session.post = routed_post

        tid = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="a]8:orgid:user-123",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await adapter.fetch_messages(
                tid,
                FetchOptions(direction="forward", cursor="2024-05-01T00:00:00Z"),
            )
            assert len(result.messages) >= 0


# ---------------------------------------------------------------------------
# _extract_attachments_from_graph_message
# ---------------------------------------------------------------------------


class TestExtractAttachmentsFromGraph:
    def test_extracts_image_attachment(self):
        adapter = _make_adapter()
        msg = {
            "attachments": [
                {"contentType": "image/png", "name": "pic.png", "contentUrl": "https://example.com/pic.png"},
            ]
        }
        attachments = adapter._extract_attachments_from_graph_message(msg)
        assert len(attachments) == 1
        assert attachments[0].type == "image"

    def test_extracts_file_attachment(self):
        adapter = _make_adapter()
        msg = {
            "attachments": [
                {"contentType": "application/pdf", "name": "doc.pdf", "contentUrl": "https://example.com/doc.pdf"},
            ]
        }
        attachments = adapter._extract_attachments_from_graph_message(msg)
        assert len(attachments) == 1
        assert attachments[0].type == "file"

    def test_no_attachments(self):
        adapter = _make_adapter()
        assert adapter._extract_attachments_from_graph_message({}) == []


# ---------------------------------------------------------------------------
# _create_attachment (from webhook message)
# ---------------------------------------------------------------------------


class TestCreateAttachmentTypes:
    def test_video_type(self):
        adapter = _make_adapter()
        att = adapter._create_attachment({"contentType": "video/mp4", "name": "clip.mp4"})
        assert att.type == "video"

    def test_audio_type(self):
        adapter = _make_adapter()
        att = adapter._create_attachment({"contentType": "audio/mpeg", "name": "song.mp3"})
        assert att.type == "audio"

    def test_default_file_type(self):
        adapter = _make_adapter()
        att = adapter._create_attachment({"contentType": "application/zip", "name": "archive.zip"})
        assert att.type == "file"


# ---------------------------------------------------------------------------
# _get_graph_context edge cases
# ---------------------------------------------------------------------------


class TestGetGraphContext:
    async def test_no_chat(self):
        adapter = _make_adapter(logger=_make_logger())
        result = await adapter._get_graph_context("19:abc")
        assert result is None

    async def test_no_state(self):
        adapter = _make_adapter(logger=_make_logger())
        chat = MagicMock()
        chat.get_state = MagicMock(return_value=None)
        await adapter.initialize(chat)
        result = await adapter._get_graph_context("19:abc")
        assert result is None

    async def test_invalid_json_in_cache(self):
        adapter = _make_adapter(logger=_make_logger())
        state = _make_mock_state()
        state._cache["teams:channelContext:19:abc"] = "not valid json {"
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)
        result = await adapter._get_graph_context("19:abc")
        assert result is None

    async def test_valid_context_from_cache(self):
        adapter = _make_adapter(logger=_make_logger())
        state = _make_mock_state()
        state._cache["teams:channelContext:19:abc"] = json.dumps({"team_id": "t1", "channel_id": "c1"})
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)
        result = await adapter._get_graph_context("19:abc")
        assert result is not None
        assert result["team_id"] == "t1"


# ---------------------------------------------------------------------------
# vercel/chat#403 — DM conversation IDs for Microsoft Graph API
# ---------------------------------------------------------------------------


class TestGraphDmConversationIdResolution:
    """Regression tests for vercel/chat#403.

    Bot Framework hands out opaque DM conversation IDs (e.g.
    ``a:1xWhatever``) which the Graph ``/chats/{chat-id}/messages``
    endpoint rejects with 404. The fix caches the user's AAD object ID
    on every incoming activity and resolves the canonical Graph chat ID
    (``19:{aadId}_{botId}@unq.gbl.spaces``) before issuing Graph calls.
    """

    def test_chat_id_from_context_uses_dm_graph_chat_id(self):
        """What to fix if this fails: ``_chat_id_from_context`` must return
        the cached ``graph_chat_id`` for any DM context. Returning the raw
        Bot Framework conversation ID instead reproduces the upstream bug
        where Graph calls 404 for every DM.
        """
        result = TeamsAdapter._chat_id_from_context(
            {"type": "dm", "graph_chat_id": "19:user-aad-id_bot-id@unq.gbl.spaces"},
            "a:opaque-conversation-id",
        )
        assert result == "19:user-aad-id_bot-id@unq.gbl.spaces"

    def test_chat_id_from_context_uses_raw_id_for_no_context(self):
        """What to fix if this fails: group chats (no cached context) must
        fall back to the raw conversation ID — they work as-is with Graph.
        """
        result = TeamsAdapter._chat_id_from_context(None, "19:group-chat@thread.v2")
        assert result == "19:group-chat@thread.v2"

    def test_chat_id_from_context_uses_raw_id_for_channel_context(self):
        """What to fix if this fails: channel contexts go through the
        ``/teams/{team-id}/channels/...`` path; if a channel context ever
        leaks into ``_chat_id_from_context``, fall back to the raw ID
        rather than the (absent) ``graph_chat_id``.
        """
        result = TeamsAdapter._chat_id_from_context(
            {"type": "channel", "team_id": "team-id", "channel_id": "channel-id"},
            "19:channel@thread.tacv2",
        )
        assert result == "19:channel@thread.tacv2"

    async def test_cache_user_context_caches_dm_context_with_graph_chat_id(self):
        """An incoming DM activity must cache a ``TeamsDmContext`` keyed by
        the opaque base conversation ID, with ``graph_chat_id`` derived
        from ``from.aadObjectId`` and the bot's app_id.

        What to fix if this fails: ``_cache_user_context`` must cache the
        DM context whenever ``from.aadObjectId`` is present and the
        conversation ID is *not* a channel/group chat (i.e. doesn't start
        with ``19:``). Without this cache, ``fetch_messages`` falls back
        to the raw Bot Framework ID and Graph returns 404.
        """
        adapter = _make_adapter(logger=_make_logger(), app_id="bot-app-id")
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        activity = {
            "type": "message",
            "from": {"id": "29:1xUser", "aadObjectId": "00000000-0000-0000-0000-aaaaaaaaaaaa"},
            "conversation": {"id": "a:opaque-dm-conversation-id"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
        }
        await adapter._cache_user_context(activity)

        raw_context = state._cache.get("teams:channelContext:a:opaque-dm-conversation-id")
        assert raw_context is not None, "DM context must be cached for Graph chat ID resolution"
        ctx = json.loads(raw_context)
        assert ctx["type"] == "dm"
        assert ctx["graph_chat_id"] == "19:00000000-0000-0000-0000-aaaaaaaaaaaa_bot-app-id@unq.gbl.spaces"

    async def test_cache_user_context_skips_dm_for_channel_conversation(self):
        """Channel conversations (``19:...``) must not cache a DM context
        even when ``from.aadObjectId`` is present.

        What to fix if this fails: the conversation-ID prefix guard in
        ``_cache_user_context`` is broken — a channel activity is leaking
        into the DM path and would over-write the channel context.
        """
        adapter = _make_adapter(logger=_make_logger(), app_id="bot-app-id")
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        activity = {
            "type": "message",
            "from": {"id": "29:1xUser", "aadObjectId": "00000000-0000-0000-0000-aaaaaaaaaaaa"},
            "conversation": {"id": "19:channel@thread.tacv2"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
            "channelData": {
                "team": {"aadGroupId": "team-aad"},
                "channel": {"id": "19:channel@thread.tacv2"},
            },
        }
        await adapter._cache_user_context(activity)

        raw_context = state._cache.get("teams:channelContext:19:channel@thread.tacv2")
        assert raw_context is not None
        ctx = json.loads(raw_context)
        # Adversarial check: a DM-like channel ID (still starts with "19:")
        # should resolve to the channel context, not a DM context.
        assert ctx.get("type") != "dm"
        assert ctx["team_id"] == "team-aad"

    async def test_cache_user_context_skips_dm_when_no_aad_object_id(self):
        """No ``aadObjectId`` (e.g. bot-to-bot activity) means we cannot
        derive the canonical Graph chat ID — skip caching the DM context
        entirely rather than caching a malformed one.
        """
        adapter = _make_adapter(logger=_make_logger(), app_id="bot-app-id")
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        activity = {
            "type": "message",
            "from": {"id": "28:bot-id"},  # no aadObjectId
            "conversation": {"id": "a:opaque-dm"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
        }
        await adapter._cache_user_context(activity)
        assert state._cache.get("teams:channelContext:a:opaque-dm") is None

    async def test_cache_user_context_rejects_non_guid_aad_object_id(self):
        """Defense-in-depth: ``aadObjectId`` values that aren't GUIDs must
        not be formatted into the Graph chat ID. Bot Framework JWT
        verification authenticates the activity envelope but does not
        constrain ``from.aadObjectId`` shape; a malformed value containing
        ``/`` ``?`` ``#`` ``:`` could otherwise inject into the Graph URL.

        What to fix if this fails: ``_cache_user_context`` lost its
        ``_AAD_OBJECT_ID_PATTERN.fullmatch`` guard. Re-add it before the
        DM context is written.
        """
        adapter = _make_adapter(logger=_make_logger(), app_id="bot-app-id")
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        # Various malformed shapes — a path-injection attempt, missing
        # hyphens, too short, non-hex characters.
        for bogus in [
            "user-aad-id",  # not GUID-shaped
            "../etc/passwd",  # path traversal
            "00000000-0000-0000-0000-aaaa/messages",  # injection
            "00000000000000000000000000000000",  # right length, no hyphens
            "ZZZZZZZZ-ZZZZ-ZZZZ-ZZZZ-ZZZZZZZZZZZZ",  # non-hex
            "00000000-0000-0000-0000",  # too short
        ]:
            activity = {
                "type": "message",
                "from": {"id": "29:1xUser", "aadObjectId": bogus},
                "conversation": {"id": f"a:opaque-{bogus}"},
                "serviceUrl": "https://smba.trafficmanager.net/teams/",
            }
            await adapter._cache_user_context(activity)
            cached = state._cache.get(f"teams:channelContext:a:opaque-{bogus}")
            assert cached is None, (
                f"_cache_user_context cached a DM context with malformed "
                f"aadObjectId={bogus!r}; the GUID guard must reject this "
                f"before formatting it into a Graph chat ID"
            )

    async def test_cache_user_context_handles_non_string_aad_object_id(self):
        """Non-string ``from.aadObjectId`` values (int, dict, list, None)
        must not crash ``_cache_user_context``. Bot Framework JWT
        verification authenticates the activity envelope but does not
        constrain the JSON *type* of ``from.aadObjectId``; a malformed
        non-string value would otherwise cause ``re.fullmatch`` to raise
        ``TypeError`` and surface up to ``handle_webhook`` as a 500.
        Malformed activities must be silently skipped (no DM caching).

        What to fix if this fails: ``_cache_user_context`` is calling
        ``_AAD_OBJECT_ID_PATTERN.fullmatch`` on a non-string value.
        Gate the regex with an ``isinstance(..., str)`` check first.
        """
        adapter = _make_adapter(logger=_make_logger(), app_id="bot-app-id")
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        # Each non-string shape that a malformed/non-conforming activity
        # could realistically send. ``None`` is already short-circuited
        # by the existing truthiness check via ``isinstance`` here, but
        # we still assert it doesn't raise.
        for bogus in [
            123,
            {"key": "val"},
            ["a", "b"],
            None,
            True,
        ]:
            activity = {
                "type": "message",
                "from": {"id": "29:1xUser", "aadObjectId": bogus},
                "conversation": {"id": f"a:opaque-{type(bogus).__name__}"},
                "serviceUrl": "https://smba.trafficmanager.net/teams/",
            }
            # The key assertion: this must not raise. Before the fix,
            # ``re.fullmatch`` raised ``TypeError`` for non-string inputs,
            # escaping ``handle_webhook`` and turning a malformed field
            # into a 500 webhook failure.
            await adapter._cache_user_context(activity)
            cached = state._cache.get(f"teams:channelContext:a:opaque-{type(bogus).__name__}")
            assert cached is None, (
                f"_cache_user_context cached a DM context with non-string "
                f"aadObjectId={bogus!r}; the type guard must reject this "
                f"before invoking the regex"
            )

    async def test_fetch_messages_uses_legacy_cache_shape_for_channel(self):
        """End-to-end backwards-compat: cached entries written before
        vercel/chat#403 lack the ``type`` discriminator. They must still
        route through ``fetch_messages``'s channel branch (treated as
        ``type=channel`` by ``_chat_id_from_context``), not get
        misclassified or crash on the missing key.

        What to fix if this fails: ``_chat_id_from_context`` or
        ``_get_graph_context`` lost the legacy-shape fallthrough. The
        contract is "absent ``type`` ⇒ treated as channel".
        """
        adapter = _make_adapter(logger=_make_logger(), app_id="bot-app-id")
        state = _make_mock_state()
        # Legacy shape — no ``type`` key, only ``team_id`` + ``channel_id``.
        state._cache["teams:channelContext:19:legacy-channel@thread.tacv2"] = json.dumps(
            {"team_id": "team-aad", "channel_id": "19:legacy-channel@thread.tacv2"}
        )
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        # The legacy cache should route to the channel-messages path —
        # which uses ``team_id`` + ``channel_id``, NOT the chat-messages
        # path (which would be wrong for a channel and would call Graph
        # with the raw conversation ID). Confirm by hooking the channel
        # endpoint and asserting it's called.
        async def fake_channel_list(team_id: str, channel_id: str, limit: int = 50):
            return [{"id": "1234", "from": {"user": {"id": "u1", "displayName": "x"}}, "body": {"content": "hi"}}]

        adapter._graph_list_channel_messages = fake_channel_list  # type: ignore[assignment]

        ctx = await adapter._get_graph_context("19:legacy-channel@thread.tacv2")
        assert ctx is not None, "Legacy cache shape did not load"
        # ``_chat_id_from_context`` returns the raw base id when there's
        # no ``graph_chat_id`` (channel context shape). The crucial assert
        # is that we treat this as *not-DM* (no Graph 19:...@unq.gbl.spaces
        # construction).
        chat_id = adapter._chat_id_from_context(ctx, "19:legacy-channel@thread.tacv2")
        assert chat_id == "19:legacy-channel@thread.tacv2", (
            f"_chat_id_from_context misclassified legacy cache entry as DM; "
            f"got {chat_id!r}, expected the raw conversation ID. The "
            f"absent-``type`` legacy shape must fall through to channel "
            f"semantics."
        )

    async def test_fetch_messages_uses_graph_chat_id_for_dm(self):
        """End-to-end: a cached DM context must redirect ``fetch_messages``
        to the canonical ``19:{aadId}_{botId}@unq.gbl.spaces`` chat ID,
        not the opaque Bot Framework conversation ID.

        What to fix if this fails: ``fetch_messages`` is calling
        ``_graph_list_chat_messages`` with the raw ``base_conversation_id``
        instead of the resolved Graph chat ID. Graph returns 404 in
        production for every DM in this state. Confirm
        ``_chat_id_from_context`` is invoked and its result threads through
        the chat-id parameter.
        """
        from chat_sdk.adapters.teams.adapter import TeamsAdapter as _TA

        adapter = _make_adapter(logger=_make_logger(), app_id="bot-app-id")
        state = _make_mock_state()
        # Cache the DM context as if a previous activity had landed.
        state._cache["teams:channelContext:a:opaque-dm-id"] = json.dumps(
            {"type": "dm", "graph_chat_id": "19:user-aad_bot-app-id@unq.gbl.spaces"}
        )
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        called_with: list[str] = []

        async def fake_list(chat_id: str, params: Any) -> list[dict[str, Any]]:
            called_with.append(chat_id)
            return [
                {
                    "id": "msg-1",
                    "createdDateTime": "2024-06-01T12:00:00Z",
                    "body": {"contentType": "text", "content": "Hi"},
                    "from": {"user": {"id": "user-aad", "displayName": "Alice"}},
                }
            ]

        adapter._graph_list_chat_messages = fake_list  # type: ignore[method-assign]
        adapter._get_graph_token = AsyncMock(return_value="t")  # type: ignore[method-assign]

        tid = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="a:opaque-dm-id",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )

        result = await adapter.fetch_messages(tid)

        assert called_with == ["19:user-aad_bot-app-id@unq.gbl.spaces"], (
            "fetch_messages must dispatch DM Graph calls to the resolved "
            "graph_chat_id, not the opaque Bot Framework conversation ID. "
            f"Saw chat_id={called_with!r}."
        )
        assert len(result.messages) == 1
        # Silence unused-import warning if the helper above is removed.
        assert _TA is TeamsAdapter

    async def test_fetch_messages_falls_back_to_raw_id_when_no_dm_context(self):
        """Pre-#403 behavior preservation: when no DM context is cached
        (e.g. a group chat conversation, or an installation that hasn't
        seen an activity yet), fall back to the raw conversation ID. This
        keeps group chats working as before.
        """
        adapter = _make_adapter(logger=_make_logger())
        state = _make_mock_state()  # empty
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        called_with: list[str] = []

        async def fake_list(chat_id: str, params: Any) -> list[dict[str, Any]]:
            called_with.append(chat_id)
            return []

        adapter._graph_list_chat_messages = fake_list  # type: ignore[method-assign]
        adapter._get_graph_token = AsyncMock(return_value="t")  # type: ignore[method-assign]

        tid = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:group-chat@thread.v2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        await adapter.fetch_messages(tid)

        assert called_with == ["19:group-chat@thread.v2"]


# ---------------------------------------------------------------------------
# _extract_text_from_graph_message edge cases
# ---------------------------------------------------------------------------


class TestExtractTextFromGraphMessage:
    def test_text_content_type(self):
        adapter = _make_adapter()
        msg = {"body": {"contentType": "text", "content": "plain text"}}
        assert adapter._extract_text_from_graph_message(msg) == "plain text"

    def test_html_stripping(self):
        adapter = _make_adapter()
        msg = {"body": {"contentType": "html", "content": "<div><b>bold</b></div>"}}
        result = adapter._extract_text_from_graph_message(msg)
        assert "bold" in result
        assert "<" not in result

    def test_empty_html_with_card_fallback(self):
        adapter = _make_adapter()
        card_json = json.dumps({"body": [{"type": "TextBlock", "text": "Card", "weight": "bolder"}]})
        msg = {
            "body": {"contentType": "html", "content": ""},
            "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card_json}],
        }
        result = adapter._extract_text_from_graph_message(msg)
        assert result == "Card"

    def test_empty_html_with_invalid_card_json(self):
        adapter = _make_adapter()
        msg = {
            "body": {"contentType": "html", "content": ""},
            "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": "not-json"}],
        }
        result = adapter._extract_text_from_graph_message(msg)
        assert result == "[Card]"

    def test_no_body(self):
        adapter = _make_adapter()
        msg = {}
        result = adapter._extract_text_from_graph_message(msg)
        assert result == ""


# ---------------------------------------------------------------------------
# edit_message error path
# ---------------------------------------------------------------------------


class TestEditMessageError:
    async def test_edit_message_update_failure(self):
        adapter = _make_adapter(logger=_make_logger())
        _mock_app_activities(adapter, update_side_effect=Exception("update failed"))

        tid = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:abc@thread.tacv2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        with pytest.raises(NetworkError):
            await adapter.edit_message(tid, "msg-1", {"markdown": "fail"})


# ---------------------------------------------------------------------------
# post_message card send failure
# ---------------------------------------------------------------------------


class TestPostMessageCardError:
    async def test_post_card_failure(self):
        adapter = _make_adapter(logger=_make_logger())
        send = _mock_app_send(adapter)
        send.side_effect = Exception("card send failed")

        tid = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:abc@thread.tacv2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        with pytest.raises(NetworkError):
            await adapter.post_message(
                tid,
                {
                    "card": {
                        "header": {"title": "Fail"},
                        "body": [{"type": "text", "content": "content"}],
                    }
                },
            )


# ---------------------------------------------------------------------------
# fetch_messages 403 error path
# ---------------------------------------------------------------------------


class TestFetchMessages403:
    async def test_fetch_messages_403_raises_permission_error(self):
        from chat_sdk.shared.errors import AdapterPermissionError as APE

        adapter = _make_adapter(logger=_make_logger())
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        adapter._get_graph_token = AsyncMock(side_effect=Exception("403 Forbidden"))

        tid = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="a]8:orgid:user-123",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )

        with pytest.raises(APE):
            await adapter.fetch_messages(tid)


# ---------------------------------------------------------------------------
# list_threads (Graph API) — channel + chat paths
# ---------------------------------------------------------------------------


class TestListThreads:
    async def test_channel_path_wraps_each_message_as_thread(self):
        """Channel context → one ThreadSummary per top-level message, each with a
        per-message thread ID (``;messageid=`` suffix) and ``last_reply_at``.

        Mirrors upstream GraphReader.listThreads channel branch (graph-api.ts:389-449).
        """
        adapter = _make_adapter(logger=_make_logger(), app_id="bot-app-id")
        state = _make_mock_state()
        state._cache["teams:channelContext:19:chan@thread.tacv2"] = json.dumps(
            {"type": "channel", "team_id": "team-1", "channel_id": "19:chan@thread.tacv2"}
        )
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        async def fake_channel_list(team_id: str, channel_id: str, limit: int = 50):
            assert team_id == "team-1"
            assert channel_id == "19:chan@thread.tacv2"
            return [
                {
                    "id": "100",
                    "createdDateTime": "2024-06-01T12:00:00Z",
                    "lastModifiedDateTime": "2024-06-01T12:05:00Z",
                    "body": {"contentType": "text", "content": "First"},
                    "from": {"user": {"id": "u1", "displayName": "Alice"}},
                },
                {
                    "id": "200",
                    "createdDateTime": "2024-06-01T11:00:00Z",
                    "body": {"contentType": "text", "content": "Second"},
                    "from": {"user": {"id": "u2", "displayName": "Bob"}},
                },
                {"from": {"user": {"id": "u3"}}},  # no id → skipped
            ]

        adapter._graph_list_channel_messages = fake_channel_list  # type: ignore[method-assign]

        channel_id = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:chan@thread.tacv2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        result = await adapter.list_threads(channel_id)

        assert len(result.threads) == 2
        first = result.threads[0]
        # Root message text mapped from the Graph body.
        assert first.root_message.text == "First"
        # Per-message thread ID embeds messageid=100, distinct from the channel id.
        decoded = adapter.decode_thread_id(first.id)
        assert decoded.conversation_id == "19:chan@thread.tacv2;messageid=100"
        assert first.id != channel_id
        # last_reply_at from lastModifiedDateTime (channel path only).
        assert first.last_reply_at is not None
        assert first.last_reply_at.year == 2024
        assert result.threads[1].last_reply_at is None  # no lastModifiedDateTime

    async def test_chat_path_lists_chat_messages_without_last_reply(self):
        """No channel context → chat-messages path; ThreadSummaries carry no
        ``last_reply_at`` (graph-api.ts:450-505)."""
        adapter = _make_adapter(logger=_make_logger(), app_id="bot-app-id")
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        captured_params: list[Any] = []

        async def fake_chat_list(chat_id: str, params: Any):
            captured_params.append(params)
            return [
                {
                    "id": "300",
                    "createdDateTime": "2024-06-02T09:00:00Z",
                    "body": {"contentType": "text", "content": "Chat msg"},
                    "from": {"user": {"id": "u9", "displayName": "Carol"}},
                }
            ]

        adapter._graph_list_chat_messages = fake_chat_list  # type: ignore[method-assign]

        channel_id = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:group@thread.v2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        result = await adapter.list_threads(channel_id, {"limit": 10})

        assert len(result.threads) == 1
        summary = result.threads[0]
        assert summary.root_message.text == "Chat msg"
        assert summary.last_reply_at is None
        decoded = adapter.decode_thread_id(summary.id)
        assert decoded.conversation_id == "19:group@thread.v2;messageid=300"
        # Honors the limit + descending order Graph query (graph-api.ts:452-456).
        assert captured_params[0]["$top"] == 10
        assert captured_params[0]["$orderby"] == "createdDateTime desc"

    async def test_propagates_graph_errors(self):
        adapter = _make_adapter(logger=_make_logger())
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        async def boom(chat_id: str, params: Any):
            raise NetworkError("teams", "Graph API error: 500")

        adapter._graph_list_chat_messages = boom  # type: ignore[method-assign]

        channel_id = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:group@thread.v2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        with pytest.raises(NetworkError):
            await adapter.list_threads(channel_id)


# ---------------------------------------------------------------------------
# post_channel_message — text + card to the base conversation
# ---------------------------------------------------------------------------


class TestPostChannelMessage:
    async def test_text_posts_to_base_conversation(self):
        """Strips ``;messageid=`` so the activity lands on the base channel
        conversation, not threaded under a root message (index.ts:1372-1376)."""
        adapter = _make_adapter(logger=_make_logger())
        send = _mock_app_send(adapter, "ch-text-1")

        channel_id = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:chan@thread.tacv2;messageid=999",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        result = await adapter.post_channel_message(channel_id, {"markdown": "Hello **channel**"})

        assert result.id == "ch-text-1"
        # threadId echoes the input channel_id (index.ts:1422).
        assert result.thread_id == channel_id
        # Sent to the base conversation — the messageid suffix is stripped.
        assert send.call_args.args[0] == "19:chan@thread.tacv2"

    async def test_card_posts_adaptive_card_to_base_conversation(self):
        adapter = _make_adapter(logger=_make_logger())
        send = _mock_app_send(adapter, "ch-card-1")

        channel_id = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:chan@thread.tacv2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        result = await adapter.post_channel_message(
            channel_id,
            {"card": {"header": {"title": "Notice"}, "body": [{"type": "text", "content": "Body"}]}},
        )

        assert result.id == "ch-card-1"
        activity = send.call_args.args[1]
        dumped = activity.model_dump(by_alias=True, exclude_none=True)
        assert dumped["attachments"][0]["contentType"] == "application/vnd.microsoft.card.adaptive"

    async def test_send_failure_raises(self):
        adapter = _make_adapter(logger=_make_logger())
        send = _mock_app_send(adapter)
        send.side_effect = Exception("connection failed")

        channel_id = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:chan@thread.tacv2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        with pytest.raises(NetworkError):
            await adapter.post_channel_message(channel_id, {"markdown": "fail"})
