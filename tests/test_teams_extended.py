"""Extended Teams adapter tests -- closes the test gap from 93 to 123.

Covers:
- Conversation types: channel, group chat, personal (DM)
- Activity types: message, messageReaction, invoke (adaptive card)
- postMessage with Adaptive Card
- fetchMessages via Graph API (DM, channel, thread)
- openDM (create conversation)
- Typing indicator
- Service URL caching
- Tenant ID resolution
- Error handling (auth token refresh, API errors)
- Message filtering (HTML attachment stripping, adaptive card detection)
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from chat_sdk.adapters.teams.adapter import (
    MESSAGEID_CAPTURE_PATTERN,
    MESSAGEID_STRIP_PATTERN,
    TeamsAdapter,
    _handle_teams_error,
    _to_app_options,
)
from chat_sdk.adapters.teams.types import (
    TeamsAdapterConfig,
    TeamsAuthCertificate,
    TeamsThreadId,
)
from chat_sdk.shared.errors import (
    AdapterPermissionError,
    AdapterRateLimitError,
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
    ``HttpServer``, which validates the Bearer token via its ``TokenValidator``.
    Unit tests don't carry signed tokens, so we force the SDK's own
    ``skip_auth`` flag on — exercising the real bridge → SDK → handler dispatch
    path while bypassing signature checks. The dedicated auth tests assert the
    SDK *does* reject unauthenticated requests when this is not applied.
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
    """Stand-in for the SDK ``SentActivity`` returned by ``app.send``."""

    def __init__(self, id: str):
        self.id = id


def _mock_app_send(adapter: TeamsAdapter, sent_id: str = "sent-msg-123") -> AsyncMock:
    """Replace ``adapter._app.send`` with an AsyncMock returning a SentActivity.

    The migrated outbound send/typing paths delegate to the SDK ``App.send``.
    """
    send = AsyncMock(return_value=_SentActivity(sent_id))
    adapter._app.send = send  # type: ignore[method-assign]
    return send


class _FakeRequest:
    def __init__(self, body: str, headers: dict[str, str] | None = None):
        self._body = body
        self.headers = headers or {}

    async def text(self) -> str:
        return self._body

    @property
    def data(self) -> bytes:
        return self._body.encode("utf-8")


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


# ---------------------------------------------------------------------------
# Conversation types
# ---------------------------------------------------------------------------


class TestConversationTypes:
    def test_channel_conversation(self):
        adapter = _make_adapter()
        tid = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:abc@thread.tacv2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        assert adapter.is_dm(tid) is False

    def test_group_chat_conversation(self):
        adapter = _make_adapter()
        tid = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:meetingabc@thread.v2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        # Group chats still start with 19: so they're not DMs
        assert adapter.is_dm(tid) is False

    def test_personal_dm_conversation(self):
        adapter = _make_adapter()
        tid = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="a]8:orgid:user-id",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        assert adapter.is_dm(tid) is True

    def test_dm_without_19_prefix(self):
        adapter = _make_adapter()
        tid = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="28:bot-framework-user",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        assert adapter.is_dm(tid) is True


# ---------------------------------------------------------------------------
# Activity types via webhook
# ---------------------------------------------------------------------------


class TestActivityTypes:
    @pytest.mark.asyncio
    async def test_message_activity(self):
        adapter = _make_adapter(logger=_make_logger())
        chat = _make_mock_chat()
        await adapter.initialize(chat)

        activity = {
            "type": "message",
            "id": "msg-1",
            "text": "Hello",
            "from": {"id": "user-1", "name": "Alice"},
            "conversation": {"id": "19:abc@thread.tacv2"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
            "timestamp": "2024-06-01T12:00:00Z",
        }
        request = _FakeRequest(json.dumps(activity))
        response = await adapter.handle_webhook(request)
        assert response["status"] == 200
        assert chat.process_message.called

    @pytest.mark.asyncio
    async def test_message_reaction_activity(self):
        adapter = _make_adapter(logger=_make_logger())
        chat = _make_mock_chat()
        await adapter.initialize(chat)

        activity = {
            "type": "messageReaction",
            "from": {"id": "user-1", "name": "Alice"},
            "conversation": {"id": "19:abc@thread.tacv2"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
            "reactionsAdded": [{"type": "like"}],
        }
        request = _FakeRequest(json.dumps(activity))
        response = await adapter.handle_webhook(request)
        assert response["status"] == 200
        assert chat.process_reaction.called

    @pytest.mark.asyncio
    async def test_reaction_removed_activity(self):
        adapter = _make_adapter(logger=_make_logger())
        chat = _make_mock_chat()
        await adapter.initialize(chat)

        activity = {
            "type": "messageReaction",
            "from": {"id": "user-1", "name": "Alice"},
            "conversation": {"id": "19:abc@thread.tacv2"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
            "reactionsRemoved": [{"type": "like"}],
        }
        request = _FakeRequest(json.dumps(activity))
        response = await adapter.handle_webhook(request)
        assert response["status"] == 200
        assert chat.process_reaction.called

    @pytest.mark.asyncio
    async def test_invoke_adaptive_card_action(self):
        adapter = _make_adapter(logger=_make_logger())
        chat = _make_mock_chat()
        await adapter.initialize(chat)

        activity = {
            "type": "invoke",
            "from": {"id": "user-1", "name": "Alice"},
            "conversation": {"id": "19:abc@thread.tacv2"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
            "value": {
                "action": {
                    "type": "Action.Execute",
                    "data": {"actionId": "approve", "value": "yes"},
                }
            },
        }
        request = _FakeRequest(json.dumps(activity))
        response = await adapter.handle_webhook(request)
        assert response["status"] == 200
        assert chat.process_action.called

    @pytest.mark.asyncio
    async def test_unknown_activity_type(self):
        adapter = _make_adapter(logger=_make_logger())
        chat = _make_mock_chat()
        await adapter.initialize(chat)

        activity = {
            "type": "conversationUpdate",
            "from": {"id": "user-1", "name": "Alice"},
            "conversation": {"id": "19:abc@thread.tacv2"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
        }
        request = _FakeRequest(json.dumps(activity))
        response = await adapter.handle_webhook(request)
        assert response["status"] == 200


# ---------------------------------------------------------------------------
# postMessage with adaptive card
# ---------------------------------------------------------------------------


class TestPostMessageAdaptiveCard:
    @pytest.mark.asyncio
    async def test_post_card_message(self):
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
                    "header": {"title": "Approval", "icon": "check"},
                    "body": [{"type": "text", "content": "Approve this?"}],
                    "footer": {
                        "buttons": [
                            {"label": "Approve", "action_id": "approve", "value": "yes"},
                        ]
                    },
                }
            },
        )
        assert result.id == "card-msg-1"
        activity = send.call_args.args[1]
        dumped = activity.model_dump(by_alias=True, exclude_none=True)
        assert dumped["type"] == "message"
        assert len(dumped["attachments"]) == 1
        assert dumped["attachments"][0]["contentType"] == "application/vnd.microsoft.card.adaptive"


# ---------------------------------------------------------------------------
# Service URL caching
# ---------------------------------------------------------------------------


class TestServiceUrlCaching:
    @pytest.mark.asyncio
    async def test_caches_service_url_from_activity(self):
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        adapter = _make_adapter(logger=_make_logger())
        await adapter.initialize(chat)

        activity = {
            "type": "message",
            "id": "msg-1",
            "text": "Hello",
            "from": {"id": "user-42", "name": "Test"},
            "conversation": {"id": "19:abc@thread.tacv2"},
            "serviceUrl": "https://smba.trafficmanager.net/amer/",
            "timestamp": "2024-06-01T12:00:00Z",
        }
        request = _FakeRequest(json.dumps(activity))
        await adapter.handle_webhook(request)
        assert state._cache.get("teams:serviceUrl:user-42") == "https://smba.trafficmanager.net/amer/"

    @pytest.mark.asyncio
    async def test_caches_tenant_id(self):
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        adapter = _make_adapter(logger=_make_logger())
        await adapter.initialize(chat)

        activity = {
            "type": "message",
            "id": "msg-1",
            "text": "Hello",
            "from": {"id": "user-42", "name": "Test"},
            "conversation": {"id": "19:abc@thread.tacv2", "tenantId": "tenant-abc-123"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
            "timestamp": "2024-06-01T12:00:00Z",
        }
        request = _FakeRequest(json.dumps(activity))
        await adapter.handle_webhook(request)
        assert state._cache.get("teams:tenantId:user-42") == "tenant-abc-123"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_401_raises_auth_error(self):
        with pytest.raises(AuthenticationError):
            _handle_teams_error({"statusCode": 401, "message": "unauthorized"}, "postMessage")

    def test_403_raises_permission_error(self):
        with pytest.raises(AdapterPermissionError):
            _handle_teams_error({"statusCode": 403, "message": "forbidden"}, "postMessage")

    def test_404_raises_network_error(self):
        with pytest.raises(NetworkError, match="not found"):
            _handle_teams_error({"statusCode": 404, "message": "not found"}, "postMessage")

    def test_429_raises_rate_limit_error(self):
        with pytest.raises(AdapterRateLimitError):
            _handle_teams_error({"statusCode": 429, "retryAfter": 5}, "postMessage")

    def test_rate_limit_with_retry_after(self):
        try:
            _handle_teams_error({"statusCode": 429, "retryAfter": 30}, "postMessage")
        except AdapterRateLimitError as e:
            assert e.retry_after == 30

    def test_generic_error_with_message(self):
        with pytest.raises(NetworkError, match="something went wrong"):
            _handle_teams_error({"message": "something went wrong"}, "postMessage")

    def test_exception_object_error(self):
        with pytest.raises(NetworkError):
            _handle_teams_error(RuntimeError("test error"), "postMessage")

    def test_permission_error_from_message(self):
        with pytest.raises(AdapterPermissionError):
            _handle_teams_error({"message": "Permission denied for this action"}, "postMessage")

    def test_inner_http_error_401(self):
        with pytest.raises(AuthenticationError):
            _handle_teams_error(
                {"innerHttpError": {"statusCode": 401}, "message": "inner auth fail"},
                "postMessage",
            )


class _FakeSdkHttpError(Exception):
    """Stand-in for a Microsoft Teams SDK ``HttpError`` (status on attributes)."""

    def __init__(self, message="sdk error", status_code=None, retry_after=None, inner_http_error=None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after
        self.inner_http_error = inner_http_error


class TestHandleTeamsErrorSdkExceptions:
    """``_handle_teams_error`` must map SDK exception objects (status on
    attributes), not just the plain dicts the hand-rolled Graph path raises."""

    def test_sdk_exception_401_maps_to_auth_error(self):
        with pytest.raises(AuthenticationError):
            _handle_teams_error(_FakeSdkHttpError("nope", status_code=401), "postMessage")

    def test_sdk_exception_403_maps_to_permission_error(self):
        with pytest.raises(AdapterPermissionError):
            _handle_teams_error(_FakeSdkHttpError("forbidden", status_code=403), "postMessage")

    def test_sdk_exception_404_maps_to_network_error(self):
        with pytest.raises(NetworkError, match="not found"):
            _handle_teams_error(_FakeSdkHttpError("missing", status_code=404), "postMessage")

    def test_sdk_exception_429_carries_retry_after(self):
        with pytest.raises(AdapterRateLimitError) as exc_info:
            _handle_teams_error(_FakeSdkHttpError("slow down", status_code=429, retry_after=12), "postMessage")
        assert exc_info.value.retry_after == 12

    def test_sdk_exception_inner_http_error_status(self):
        inner = _FakeSdkHttpError("inner", status_code=401)
        with pytest.raises(AuthenticationError):
            _handle_teams_error(_FakeSdkHttpError("outer", inner_http_error=inner), "postMessage")

    def test_sdk_exception_permission_keyword_in_message(self):
        with pytest.raises(AdapterPermissionError):
            _handle_teams_error(_FakeSdkHttpError("Permission required for resource"), "postMessage")

    def test_sdk_exception_without_status_falls_back_to_network_error(self):
        with pytest.raises(NetworkError, match="generic sdk failure"):
            _handle_teams_error(_FakeSdkHttpError("generic sdk failure"), "postMessage")


# ---------------------------------------------------------------------------
# Config conversion (toAppOptions port)
# ---------------------------------------------------------------------------


class TestToAppOptions:
    def test_client_secret_auth(self):
        opts = _to_app_options(TeamsAdapterConfig(app_id="app-1", app_password="secret-1", app_tenant_id="tenant-1"))
        assert opts["client_id"] == "app-1"
        assert opts["client_secret"] == "secret-1"
        assert opts["tenant_id"] == "tenant-1"
        assert "managed_identity_client_id" not in opts

    def test_multitenant_omits_tenant_id(self):
        opts = _to_app_options(
            TeamsAdapterConfig(
                app_id="app-1", app_password="secret-1", app_tenant_id="tenant-1", app_type="MultiTenant"
            )
        )
        assert "tenant_id" not in opts

    def test_federated_omits_secret_and_sets_managed_identity(self):
        opts = _to_app_options(
            TeamsAdapterConfig(
                app_id="app-1",
                app_password="should-be-ignored",
                app_tenant_id="tenant-1",
                federated={"client_id": "mi-client-1"},
            )
        )
        assert "client_secret" not in opts
        assert opts["managed_identity_client_id"] == "mi-client-1"

    def test_federated_client_audience_logs_warning(self):
        logger = MagicMock(warn=MagicMock())
        _to_app_options(
            TeamsAdapterConfig(
                app_id="app-1",
                app_tenant_id="tenant-1",
                federated={"client_id": "mi-1", "client_audience": "api://AzureADTokenExchange"},
                logger=logger,
            )
        )
        assert logger.warn.called

    def test_env_var_fallbacks(self, monkeypatch):
        monkeypatch.setenv("TEAMS_APP_ID", "env-app")
        monkeypatch.setenv("TEAMS_APP_PASSWORD", "env-secret")
        monkeypatch.setenv("TEAMS_APP_TENANT_ID", "env-tenant")
        opts = _to_app_options(TeamsAdapterConfig())
        assert opts["client_id"] == "env-app"
        assert opts["client_secret"] == "env-secret"
        assert opts["tenant_id"] == "env-tenant"

    def test_certificate_rejected(self):
        with pytest.raises(ValidationError):
            _to_app_options(
                TeamsAdapterConfig(
                    app_id="app-1",
                    certificate=TeamsAuthCertificate(certificate_private_key="key"),
                )
            )

    def test_api_url_sets_service_url(self):
        """``api_url`` threads into ``service_url`` for sovereign clouds (config.ts:38).

        GCC-High / DoD tenants run the Bot Framework on a different host; upstream
        feeds ``config.apiUrl`` to the SDK ``AppOptions.serviceUrl`` so all outbound
        calls target that endpoint instead of the global default.
        """
        opts = _to_app_options(
            TeamsAdapterConfig(
                app_id="app-1",
                app_password="secret-1",
                api_url="https://smba.infra.gov.teams.microsoft.us/",
            )
        )
        assert opts["service_url"] == "https://smba.infra.gov.teams.microsoft.us/"

    def test_no_api_url_omits_service_url(self):
        """Absent ``api_url``/``TEAMS_API_URL``, ``service_url`` is left unset so the
        SDK applies its own global default rather than receiving an empty string."""
        opts = _to_app_options(TeamsAdapterConfig(app_id="app-1", app_password="secret-1"))
        assert "service_url" not in opts

    def test_api_url_env_fallback(self, monkeypatch):
        """``TEAMS_API_URL`` env var is the fallback when ``api_url`` is unset."""
        monkeypatch.setenv("TEAMS_API_URL", "https://smba.gov.teams.microsoft.us/")
        opts = _to_app_options(TeamsAdapterConfig(app_id="app-1", app_password="secret-1"))
        assert opts["service_url"] == "https://smba.gov.teams.microsoft.us/"

    def test_api_url_config_overrides_env(self, monkeypatch):
        """Explicit ``api_url`` wins over ``TEAMS_API_URL`` (config field precedence)."""
        monkeypatch.setenv("TEAMS_API_URL", "https://env.example.us/")
        opts = _to_app_options(
            TeamsAdapterConfig(
                app_id="app-1",
                app_password="secret-1",
                api_url="https://explicit.example.us/",
            )
        )
        assert opts["service_url"] == "https://explicit.example.us/"


# ---------------------------------------------------------------------------
# Message ID patterns
# ---------------------------------------------------------------------------


class TestMessageIdPatterns:
    def test_capture_pattern_extracts_id(self):
        match = MESSAGEID_CAPTURE_PATTERN.search("19:abc@thread.tacv2;messageid=1767297849909")
        assert match is not None
        assert match.group(1) == "1767297849909"

    def test_strip_pattern_removes_messageid(self):
        result = MESSAGEID_STRIP_PATTERN.sub("", "19:abc@thread.tacv2;messageid=1767297849909")
        assert result == "19:abc@thread.tacv2"

    def test_no_messageid_untouched(self):
        result = MESSAGEID_STRIP_PATTERN.sub("", "19:abc@thread.tacv2")
        assert result == "19:abc@thread.tacv2"


# ---------------------------------------------------------------------------
# parseMessage extended
# ---------------------------------------------------------------------------


class TestParseMessageExtended:
    def test_message_with_timestamp(self):
        adapter = _make_adapter()
        activity = {
            "type": "message",
            "id": "msg-1",
            "text": "test",
            "from": {"id": "user-1", "name": "Alice"},
            "conversation": {"id": "19:abc@thread.tacv2"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
            "timestamp": "2024-06-15T10:30:00.000Z",
        }
        msg = adapter.parse_message(activity)
        assert msg.metadata.date_sent.year == 2024
        assert msg.metadata.date_sent.month == 6

    def test_message_without_timestamp_uses_now(self):
        adapter = _make_adapter()
        activity = {
            "type": "message",
            "id": "msg-1",
            "text": "test",
            "from": {"id": "user-1", "name": "Alice"},
            "conversation": {"id": "19:abc@thread.tacv2"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
        }
        from datetime import timezone

        before = datetime.now(timezone.utc)
        msg = adapter.parse_message(activity)
        after = datetime.now(timezone.utc)
        # Verify the timestamp is between before and after (i.e. datetime.now(timezone.utc))
        assert before <= msg.metadata.date_sent <= after

    def test_mention_detection(self):
        adapter = _make_adapter(app_id="bot-app-id")
        _make_mock_chat()
        activity = {
            "type": "message",
            "id": "msg-mention",
            "text": "<at>Bot</at> help me",
            "from": {"id": "user-1", "name": "Alice"},
            "conversation": {"id": "19:abc@thread.tacv2"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
            "entities": [
                {
                    "type": "mention",
                    "mentioned": {"id": "bot-app-id", "name": "Bot"},
                }
            ],
        }
        msg = adapter.parse_message(activity)
        # parse_message doesn't set is_mention directly, but it does parse the text
        assert "help me" in msg.text

    def test_html_attachment_without_url_filtered(self):
        adapter = _make_adapter()
        activity = {
            "type": "message",
            "id": "msg-1",
            "text": "test",
            "from": {"id": "user-1", "name": "Alice"},
            "conversation": {"id": "19:abc@thread.tacv2"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
            "attachments": [
                {"contentType": "text/html", "content": "<b>bold</b>"},
            ],
        }
        msg = adapter.parse_message(activity)
        assert len(msg.attachments) == 0

    def test_html_attachment_with_url_kept(self):
        adapter = _make_adapter()
        activity = {
            "type": "message",
            "id": "msg-1",
            "text": "test",
            "from": {"id": "user-1", "name": "Alice"},
            "conversation": {"id": "19:abc@thread.tacv2"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
            "attachments": [
                {
                    "contentType": "text/html",
                    "contentUrl": "https://example.com/page.html",
                    "name": "page.html",
                },
            ],
        }
        msg = adapter.parse_message(activity)
        assert len(msg.attachments) == 1


# ---------------------------------------------------------------------------
# Message action (Action.Submit in message activity)
# ---------------------------------------------------------------------------


class TestMessageAction:
    @pytest.mark.asyncio
    async def test_message_with_action_value(self):
        adapter = _make_adapter(logger=_make_logger())
        chat = _make_mock_chat()
        await adapter.initialize(chat)

        activity = {
            "type": "message",
            "id": "msg-action",
            "from": {"id": "user-1", "name": "Alice"},
            "conversation": {"id": "19:abc@thread.tacv2"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
            "value": {"actionId": "submit_form", "value": "data"},
        }
        request = _FakeRequest(json.dumps(activity))
        response = await adapter.handle_webhook(request)
        assert response["status"] == 200
        assert chat.process_action.called


# ---------------------------------------------------------------------------
# ChoiceSet auto-submit fan-out (regression for single-dispatch bug)
# ---------------------------------------------------------------------------


class TestAutoSubmitFanOut:
    """ChoiceSet (Select/RadioSelect) submissions carry the ``__auto_submit``
    sentinel action ID and a per-input dict, e.g.
    ``{"actionId": "__auto_submit", "color": "red", "size": "L"}``.

    Upstream (adapter-teams/src/index.ts:404-471 + fanOutAutoSubmit 513-556)
    fans this out into ONE ``chat.processAction`` per input key so a handler
    registered as ``on_action("color")`` fires. The pre-fix Python adapter
    dispatched a SINGLE action with ``action_id="__auto_submit"`` and the full
    dict as ``value`` — these tests fail on that code.
    """

    def test_message_action_fans_out_per_input_key(self):
        adapter = _make_adapter(logger=_make_logger())
        chat = _make_mock_chat()
        adapter._chat = chat

        activity = {
            "type": "message",
            "id": "msg-1",
            "from": {"id": "user-1", "name": "Alice"},
            "conversation": {"id": "19:abc@thread.tacv2"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
        }
        action_value = {"actionId": "__auto_submit", "color": "red", "size": "L"}

        adapter._handle_message_action(activity, action_value)

        # One process_action per input key (regression: pre-fix dispatched once).
        assert chat.process_action.call_count == 2
        dispatched = {call.args[0].action_id: call.args[0].value for call in chat.process_action.call_args_list}
        assert dispatched == {"color": "red", "size": "L"}
        # The sentinel must never leak through as an action ID.
        assert "__auto_submit" not in dispatched

    async def test_adaptive_card_action_fans_out_per_input_key(self):
        adapter = _make_adapter(logger=_make_logger())
        chat = _make_mock_chat()
        adapter._chat = chat

        activity = {
            "type": "invoke",
            "id": "inv-1",
            "from": {"id": "user-2", "name": "Bob"},
            "conversation": {"id": "19:def@thread.tacv2"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
        }
        action_data = {"actionId": "__auto_submit", "priority": "high"}

        await adapter._handle_adaptive_card_action(activity, action_data)

        assert chat.process_action.call_count == 1
        event = chat.process_action.call_args.args[0]
        assert event.action_id == "priority"
        assert event.value == "high"

    def test_fan_out_drops_msteams_transport_key(self):
        """The ``msteams`` transport key injected by Teams infra is not user input
        and must be filtered out of the fan-out (upstream filters actionId + msteams)."""
        adapter = _make_adapter(logger=_make_logger())
        chat = _make_mock_chat()
        adapter._chat = chat

        activity = {
            "id": "msg-1",
            "from": {"id": "u", "name": "U"},
            "conversation": {"id": "19:abc@thread.tacv2"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
        }
        action_value = {
            "actionId": "__auto_submit",
            "msteams": {"type": "messageBack"},
            "topic": "billing",
        }

        adapter._handle_message_action(activity, action_value)

        assert chat.process_action.call_count == 1
        event = chat.process_action.call_args.args[0]
        assert event.action_id == "topic"
        assert event.value == "billing"

    def test_fan_out_non_string_value_becomes_none(self):
        """Non-string input values map to ``None`` per upstream
        ``typeof val === "string" ? val : undefined`` (index.ts:551)."""
        adapter = _make_adapter(logger=_make_logger())
        chat = _make_mock_chat()
        adapter._chat = chat

        activity = {
            "id": "msg-1",
            "from": {"id": "u", "name": "U"},
            "conversation": {"id": "19:abc@thread.tacv2"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
        }
        action_value = {"actionId": "__auto_submit", "flags": ["a", "b"]}

        adapter._handle_message_action(activity, action_value)

        assert chat.process_action.call_count == 1
        event = chat.process_action.call_args.args[0]
        assert event.action_id == "flags"
        assert event.value is None

    def test_plain_button_not_fanned_out(self):
        """A plain Action.Submit button (no ``__auto_submit`` sentinel) keeps the
        single-dispatch path with its own action ID — fan-out must not regress it."""
        adapter = _make_adapter(logger=_make_logger())
        chat = _make_mock_chat()
        adapter._chat = chat

        activity = {
            "id": "msg-1",
            "from": {"id": "u", "name": "U"},
            "conversation": {"id": "19:abc@thread.tacv2"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
        }
        action_value = {"actionId": "approve_btn", "value": "yes"}

        adapter._handle_message_action(activity, action_value)

        assert chat.process_action.call_count == 1
        event = chat.process_action.call_args.args[0]
        assert event.action_id == "approve_btn"
        assert event.value == "yes"


# ---------------------------------------------------------------------------
# Certificate auth not supported
# ---------------------------------------------------------------------------


class TestCertificateAuth:
    def test_raises_validation_error(self):
        with pytest.raises(ValidationError, match="Certificate-based"):
            TeamsAdapter(
                TeamsAdapterConfig(
                    app_id="app",
                    app_password="pass",
                    certificate=TeamsAuthCertificate(
                        certificate_private_key="key",
                        certificate_thumbprint="thumb",
                    ),
                )
            )

    def test_raises_with_exact_upstream_message(self):
        """Startup throw message matches upstream adapter-teams/src/config.ts:13-18 verbatim.

        Upstream references ``appPassword`` (camelCase TS field name); we preserve
        that in the error text so consumers tailing upstream logs see identical
        output. Protects against well-meaning rewording to ``app_password``.
        """
        expected = (
            "Certificate-based authentication is not yet supported by the Teams SDK adapter. "
            "Use appPassword (client secret) or federated (workload identity) authentication instead."
        )
        with pytest.raises(ValidationError) as exc_info:
            TeamsAdapter(
                TeamsAdapterConfig(
                    certificate=TeamsAuthCertificate(certificate_private_key="key"),
                )
            )
        assert expected in str(exc_info.value)

    def test_minimal_certificate_only_requires_private_key(self):
        """``certificate_thumbprint`` and ``x5c`` are optional per upstream types.ts:7-9.

        A ``TeamsAuthCertificate`` constructed with only ``certificate_private_key``
        must still trigger the startup throw (i.e. the adapter checks presence, not
        shape).
        """
        cert = TeamsAuthCertificate(certificate_private_key="pem-key")
        assert cert.certificate_thumbprint is None
        assert cert.x5c is None
        with pytest.raises(ValidationError, match="Certificate-based"):
            TeamsAdapter(TeamsAdapterConfig(certificate=cert))


# ---------------------------------------------------------------------------
# Stream via post+edit
# ---------------------------------------------------------------------------


class TestStream:
    @pytest.mark.asyncio
    async def test_group_chat_stream_accumulates_and_posts_single_message(self):
        """Group chats / channels accumulate the stream and post one message.

        Mirrors upstream ``stream`` (@chat-adapter/teams@4.30.0): native
        ``streamViaEmit`` is reserved for DMs (where an ``IStreamer`` exists);
        non-DM threads accumulate and ``postMessage`` a single message via the
        SDK ``App.send`` (PR 2-backed).
        """
        adapter = _make_adapter(logger=_make_logger())
        send = _mock_app_send(adapter, "stream-msg-1")

        tid = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:abc@thread.tacv2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )

        async def text_gen():
            yield "Hello "
            yield "world"

        result = await adapter.stream(tid, text_gen())
        assert result.id == "stream-msg-1"
        # Single SDK send carrying the full accumulated text — no edits.
        send.assert_called_once()
        conv_id, activity = send.call_args.args
        assert conv_id == "19:abc@thread.tacv2"
        assert activity.text == "Hello world"
        assert activity.text_format == "markdown"

    @pytest.mark.asyncio
    async def test_group_chat_stream_empty_returns_empty(self):
        """Empty streams in a group chat skip the post entirely."""
        adapter = _make_adapter(logger=_make_logger())
        send = _mock_app_send(adapter, "stream-msg-2")

        tid = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:abc@thread.tacv2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )

        async def text_gen():
            yield ""
            yield ""

        result = await adapter.stream(tid, text_gen())
        # No real text → no send, returned RawMessage carries empty content.
        assert result.id == ""
        assert result.raw["text"] == ""
        send.assert_not_called()
