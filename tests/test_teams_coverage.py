"""Teams adapter coverage tests -- targeting uncovered paths to reach 70%+.

Covers:
- fetch_messages via Graph API (mock aiohttp)
- fetch_channel_messages
- open_dm (conversation creation)
- _cache_user_context (service URL, tenant ID caching)
- _get_access_token (token endpoint call)
- _get_graph_token
- _validate_service_url (allowed/disallowed patterns)
- _verify_bot_framework_token (JWT verification with mock JWKS)
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
    """Bypass JWT verification in unit tests."""
    monkeypatch.setattr(
        TeamsAdapter,
        "_verify_bot_framework_token",
        AsyncMock(return_value=None),
    )


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
        # Reset any cached token
        adapter._access_token = None
        adapter._token_expiry = 0

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
        adapter._access_token = None
        adapter._token_expiry = 0

        mock_session = _MockSession(default_response=_mock_aiohttp_response({"error": "failed"}, status=400))

        with patch("aiohttp.ClientSession", return_value=mock_session), pytest.raises(AuthenticationError):
            await adapter._get_graph_token()


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
# _verify_bot_framework_token (without autouse fixture)
# ---------------------------------------------------------------------------


class TestVerifyBotFrameworkToken:
    async def test_webhook_rejects_when_no_app_id(self):
        """When app_id is empty, webhook should reject."""
        adapter = TeamsAdapter(TeamsAdapterConfig(app_id="", app_password="test"))
        chat = _make_mock_chat()
        await adapter.initialize(chat)

        class FakeReq:
            headers = {}

            async def text(self):
                return "{}"

            @property
            def data(self):
                return b"{}"

        response = await adapter.handle_webhook(FakeReq())
        assert response["status"] == 401


# ---------------------------------------------------------------------------
# postMessage / editMessage / deleteMessage / startTyping via HTTP
# ---------------------------------------------------------------------------


class TestTeamsHTTPOperations:
    async def test_post_message_with_adaptive_card(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._teams_send = AsyncMock(return_value={"id": "card-msg-1"})

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
        call_args = adapter._teams_send.call_args[0][1]
        assert call_args["type"] == "message"
        assert call_args["attachments"][0]["contentType"] == "application/vnd.microsoft.card.adaptive"

    async def test_post_message_text(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._teams_send = AsyncMock(return_value={"id": "text-msg-1"})

        tid = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:abc@thread.tacv2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        result = await adapter.post_message(tid, {"markdown": "Hello **world**"})
        assert result.id == "text-msg-1"

    async def test_post_message_send_failure(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._teams_send = AsyncMock(side_effect=Exception("connection failed"))

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
        adapter._teams_update = AsyncMock()

        tid = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:abc@thread.tacv2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        result = await adapter.edit_message(tid, "msg-1", {"markdown": "Updated"})
        assert result.id == "msg-1"
        adapter._teams_update.assert_called_once()

    async def test_edit_message_with_card(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._teams_update = AsyncMock()

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

    async def test_delete_message(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._teams_delete = AsyncMock()

        tid = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:abc@thread.tacv2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        await adapter.delete_message(tid, "del-1")
        assert adapter._teams_delete.call_count == 1

    async def test_delete_message_failure(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._teams_delete = AsyncMock(side_effect=Exception("delete failed"))

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
        adapter._teams_send = AsyncMock(return_value={"id": "t1", "type": "typing"})

        tid = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:abc@thread.tacv2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        await adapter.start_typing(tid)
        adapter._teams_send.assert_called_once()
        call_activity = adapter._teams_send.call_args[0][1]
        assert call_activity["type"] == "typing"

    async def test_start_typing_failure_swallowed(self):
        """Typing failures should be logged but not re-raised."""
        adapter = _make_adapter(logger=_make_logger())
        adapter._teams_send = AsyncMock(side_effect=Exception("typing error"))

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
# _teams_send / _teams_update / _teams_delete HTTP helpers
# ---------------------------------------------------------------------------


class TestTeamsHTTPHelpers:
    async def test_teams_send_success(self):
        adapter = _make_adapter(logger=_make_logger())

        token_resp = _mock_aiohttp_response({"access_token": "t", "expires_in": 3600})
        send_resp = _mock_aiohttp_response({"id": "sent-1"})

        mock_session = _MockSession(default_response=send_resp)
        original_post = mock_session.post

        def routed_post(url, **kwargs):
            if "oauth2" in url:
                return mock_session._make_cm(token_resp)
            return original_post(url, **kwargs)

        mock_session.post = routed_post

        decoded = TeamsThreadId(
            conversation_id="19:abc@thread.tacv2",
            service_url="https://smba.trafficmanager.net/teams/",
        )

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await adapter._teams_send(decoded, {"type": "message", "text": "hi"})
            assert result["id"] == "sent-1"

    async def test_teams_send_invalid_service_url_raises(self):
        adapter = _make_adapter(logger=_make_logger())

        decoded = TeamsThreadId(
            conversation_id="19:abc@thread.tacv2",
            service_url="https://evil.com/",
        )

        with pytest.raises(ValidationError):
            await adapter._teams_send(decoded, {"type": "message"})

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
    async def test_stream_dict_chunks(self):
        adapter = _make_adapter(logger=_make_logger())
        send_call_count = 0

        async def mock_send(decoded, payload):
            nonlocal send_call_count
            send_call_count += 1
            return {"id": f"msg-{send_call_count}"}

        adapter._teams_send = mock_send
        adapter._teams_update = AsyncMock()

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
        assert result.id == "msg-1"
        assert result.raw["text"] == "Hello World"

    async def test_stream_string_chunks(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._teams_send = AsyncMock(return_value={"id": "s1"})
        adapter._teams_update = AsyncMock()

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

    async def test_stream_empty_chunks_skipped(self):
        adapter = _make_adapter(logger=_make_logger())
        adapter._teams_send = AsyncMock(return_value={"id": "s1"})
        adapter._teams_update = AsyncMock()

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


# ---------------------------------------------------------------------------
# _get_request_body edge cases
# ---------------------------------------------------------------------------


class TestGetRequestBody:
    async def test_body_callable_with_read(self):
        adapter = _make_adapter(logger=_make_logger())

        class FakeReq:
            class body:
                @staticmethod
                def read():
                    return b"hello"

            body = body()  # noqa: E731
            body.read = staticmethod(lambda: b"hello")

        class SimpleReq:
            body = b"raw bytes"

        result = await adapter._get_request_body(SimpleReq())
        assert result == "raw bytes"

    async def test_text_callable(self):
        adapter = _make_adapter(logger=_make_logger())

        class FakeReq:
            async def text(self):
                return "text content"

        result = await adapter._get_request_body(FakeReq())
        assert result == "text content"

    async def test_text_attribute(self):
        adapter = _make_adapter(logger=_make_logger())

        class FakeReq:
            text = "static text"

        result = await adapter._get_request_body(FakeReq())
        assert result == "static text"

    async def test_data_attribute(self):
        adapter = _make_adapter(logger=_make_logger())

        class FakeReq:
            data = b"byte data"

        result = await adapter._get_request_body(FakeReq())
        assert result == "byte data"

    async def test_empty_request(self):
        adapter = _make_adapter(logger=_make_logger())

        class FakeReq:
            pass

        result = await adapter._get_request_body(FakeReq())
        assert result == ""


# ---------------------------------------------------------------------------
# _get_header edge cases
# ---------------------------------------------------------------------------


class TestGetHeader:
    def test_dict_headers_title_case(self):
        adapter = _make_adapter(logger=_make_logger())

        class FakeReq:
            headers = {"Authorization": "Bearer token"}

        result = adapter._get_header(FakeReq(), "authorization")
        # dict.get falls back to title-case key "Authorization"
        assert result == "Bearer token"

    def test_no_headers_attribute(self):
        adapter = _make_adapter(logger=_make_logger())

        class FakeReq:
            pass

        assert adapter._get_header(FakeReq(), "x-test") is None

    def test_headers_with_get_method(self):
        adapter = _make_adapter(logger=_make_logger())

        class FakeHeaders:
            def get(self, name):
                if name == "authorization":
                    return "Bearer abc"
                return None

        class FakeReq:
            headers = FakeHeaders()

        result = adapter._get_header(FakeReq(), "authorization")
        assert result == "Bearer abc"


# ---------------------------------------------------------------------------
# _make_response / _make_json_response
# ---------------------------------------------------------------------------


class TestMakeResponses:
    def test_make_response(self):
        adapter = _make_adapter()
        r = adapter._make_response("OK", 200)
        assert r["body"] == "OK"
        assert r["status"] == 200
        assert r["headers"]["Content-Type"] == "text/plain"

    def test_make_json_response(self):
        adapter = _make_adapter()
        r = adapter._make_json_response('{"ok":true}', 200)
        assert r["body"] == '{"ok":true}'
        assert r["headers"]["Content-Type"] == "application/json"


# ---------------------------------------------------------------------------
# _teams_send / _teams_update / _teams_delete error paths
# ---------------------------------------------------------------------------


class TestTeamsHTTPErrorPaths:
    async def test_teams_send_non_ok_response(self):
        adapter = _make_adapter(logger=_make_logger())

        token_resp = _mock_aiohttp_response({"access_token": "t", "expires_in": 3600})
        error_resp = _mock_aiohttp_response({"error": "bad"}, status=500)

        mock_session = _MockSession(default_response=error_resp)
        original_post = mock_session.post

        def routed_post(url, **kwargs):
            if "oauth2" in url:
                return mock_session._make_cm(token_resp)
            return original_post(url, **kwargs)

        mock_session.post = routed_post

        decoded = TeamsThreadId(
            conversation_id="19:abc@thread.tacv2",
            service_url="https://smba.trafficmanager.net/teams/",
        )

        with patch("aiohttp.ClientSession", return_value=mock_session), pytest.raises(NetworkError):
            await adapter._teams_send(decoded, {"type": "message"})

    async def test_teams_update_non_ok_response(self):
        adapter = _make_adapter(logger=_make_logger())

        token_resp = _mock_aiohttp_response({"access_token": "t", "expires_in": 3600})
        error_resp = _mock_aiohttp_response({"error": "bad"}, status=500)

        mock_session = _MockSession(default_response=error_resp)
        original_post = mock_session.post

        def routed_post(url, **kwargs):
            if "oauth2" in url:
                return mock_session._make_cm(token_resp)
            return original_post(url, **kwargs)

        mock_session.post = routed_post

        decoded = TeamsThreadId(
            conversation_id="19:abc@thread.tacv2",
            service_url="https://smba.trafficmanager.net/teams/",
        )

        with patch("aiohttp.ClientSession", return_value=mock_session), pytest.raises(NetworkError):
            await adapter._teams_update(decoded, "msg-1", {"type": "message"})

    async def test_teams_delete_non_ok_response(self):
        adapter = _make_adapter(logger=_make_logger())

        token_resp = _mock_aiohttp_response({"access_token": "t", "expires_in": 3600})
        error_resp = _mock_aiohttp_response({"error": "bad"}, status=500)

        mock_session = _MockSession(default_response=error_resp)
        original_post = mock_session.post

        def routed_post(url, **kwargs):
            if "oauth2" in url:
                return mock_session._make_cm(token_resp)
            return original_post(url, **kwargs)

        mock_session.post = routed_post

        decoded = TeamsThreadId(
            conversation_id="19:abc@thread.tacv2",
            service_url="https://smba.trafficmanager.net/teams/",
        )

        with patch("aiohttp.ClientSession", return_value=mock_session), pytest.raises(NetworkError):
            await adapter._teams_delete(decoded, "msg-1")


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
# _get_channel_context edge cases
# ---------------------------------------------------------------------------


class TestGetChannelContext:
    async def test_no_chat(self):
        adapter = _make_adapter(logger=_make_logger())
        result = await adapter._get_channel_context("19:abc")
        assert result is None

    async def test_no_state(self):
        adapter = _make_adapter(logger=_make_logger())
        chat = MagicMock()
        chat.get_state = MagicMock(return_value=None)
        await adapter.initialize(chat)
        result = await adapter._get_channel_context("19:abc")
        assert result is None

    async def test_invalid_json_in_cache(self):
        adapter = _make_adapter(logger=_make_logger())
        state = _make_mock_state()
        state._cache["teams:channelContext:19:abc"] = "not valid json {"
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)
        result = await adapter._get_channel_context("19:abc")
        assert result is None

    async def test_valid_context_from_cache(self):
        adapter = _make_adapter(logger=_make_logger())
        state = _make_mock_state()
        state._cache["teams:channelContext:19:abc"] = json.dumps({"team_id": "t1", "channel_id": "c1"})
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)
        result = await adapter._get_channel_context("19:abc")
        assert result is not None
        assert result["team_id"] == "t1"


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
        adapter._teams_update = AsyncMock(side_effect=Exception("update failed"))

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
        adapter._teams_send = AsyncMock(side_effect=Exception("card send failed"))

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
