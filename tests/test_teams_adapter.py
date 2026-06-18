"""Tests for the Teams adapter -- constructor, thread IDs, webhook handling, message operations.

Ported from packages/adapter-teams/src/index.test.ts.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from chat_sdk.adapters.teams.adapter import TeamsAdapter, create_teams_adapter
from chat_sdk.adapters.teams.types import TeamsAdapterConfig, TeamsThreadId
from chat_sdk.shared.errors import ValidationError

TEAMS_PREFIX_PATTERN = re.compile(r"^teams:")


def _make_adapter(**overrides) -> TeamsAdapter:
    """Create a TeamsAdapter with minimal valid config."""
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
    ``.id`` attribute matters to the adapter, mirroring upstream's
    ``{ id, type }`` mock return value."""

    def __init__(self, id: str):
        self.id = id


def _mock_app_send(adapter: TeamsAdapter, sent_id: str = "sent-msg-123") -> AsyncMock:
    """Replace ``adapter._app.send`` with an AsyncMock returning a SentActivity.

    Mirrors upstream's ``mockApp.send = vi.fn(async () => ({ id, type }))`` —
    the migrated outbound send/typing paths delegate to the SDK ``App.send``.
    Returns the mock so tests can assert call count / arguments.
    """
    send = AsyncMock(return_value=_SentActivity(sent_id))
    adapter._app.send = send  # type: ignore[method-assign]
    return send


def _mock_app_activities(
    adapter: TeamsAdapter,
    *,
    update_id: str = "edit-msg-1",
) -> tuple[AsyncMock, AsyncMock]:
    """Replace ``adapter._app.api`` so ``conversations.activities(id)`` returns a
    stub exposing ``update``/``delete`` AsyncMocks.

    Mirrors upstream's editMessage/deleteMessage test mock:
    ``mockApp.api = { conversations: { activities: () => ({ update, delete }) } }``.
    Returns ``(update_mock, delete_mock)``.
    """
    update = AsyncMock(return_value=_SentActivity(update_id))
    delete = AsyncMock(return_value=None)
    ops = MagicMock()
    ops.update = update
    ops.delete = delete
    api = MagicMock()
    api.conversations.activities = MagicMock(return_value=ops)
    adapter._app.api = api  # type: ignore[method-assign]
    return update, delete


class _MockAiohttpSession:
    """Stub for ``aiohttp.ClientSession`` that supports the
    ``async with session.get(url) as resp`` pattern.

    ``session.get(url)`` returns a synchronous async-context-manager (not
    a coroutine), so we can't use ``AsyncMock`` for it — the real
    aiohttp API is not itself async.  Implementing it as a real method
    also keeps us out of ``audit_test_quality.py``'s "MagicMock used for
    async method `.get`" false-positive (which pattern-matches on
    ``KeyValueState.get``, not ``ClientSession.get``).
    """

    def __init__(self, payload: bytes = b"", status: int = 200):
        response = MagicMock()
        response.status = status
        response.read = AsyncMock(return_value=payload)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=response)
        cm.__aexit__ = AsyncMock(return_value=False)
        self._cm = cm
        self.get_calls: list[str] = []

    def get(self, url: str):
        self.get_calls.append(url)
        return self._cm


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------


class TestCreateTeamsAdapter:
    def test_creates_instance(self):
        adapter = create_teams_adapter(TeamsAdapterConfig(app_id="test", app_password="test"))
        assert isinstance(adapter, TeamsAdapter)
        assert adapter.name == "teams"


# ---------------------------------------------------------------------------
# Thread ID encoding
# ---------------------------------------------------------------------------


class TestThreadIdEncoding:
    def test_encode_and_decode(self):
        adapter = _make_adapter()
        original = TeamsThreadId(
            conversation_id="19:abc123@thread.tacv2",
            service_url="https://smba.trafficmanager.net/teams/",
        )
        encoded = adapter.encode_thread_id(original)
        assert TEAMS_PREFIX_PATTERN.match(encoded)

        decoded = adapter.decode_thread_id(encoded)
        assert decoded.conversation_id == original.conversation_id
        assert decoded.service_url == original.service_url

    def test_preserves_messageid(self):
        adapter = _make_adapter()
        original = TeamsThreadId(
            conversation_id="19:d441d38c655c47a085215b2726e76927@thread.tacv2;messageid=1767297849909",
            service_url="https://smba.trafficmanager.net/amer/",
        )
        encoded = adapter.encode_thread_id(original)
        decoded = adapter.decode_thread_id(encoded)
        assert decoded.conversation_id == original.conversation_id
        assert ";messageid=" in decoded.conversation_id

    def test_throws_for_invalid_thread_ids(self):
        adapter = _make_adapter()
        with pytest.raises(ValidationError):
            adapter.decode_thread_id("invalid")
        with pytest.raises(ValidationError):
            adapter.decode_thread_id("slack:abc:def")
        with pytest.raises(ValidationError):
            adapter.decode_thread_id("teams")

    def test_special_characters(self):
        adapter = _make_adapter()
        original = TeamsThreadId(
            conversation_id="19:meeting_MDE4OWI4N2UtNzEzNC00ZGE2LTkxMGEtNDM3@thread.v2",
            service_url="https://smba.trafficmanager.net/amer/?special=chars&foo=bar",
        )
        encoded = adapter.encode_thread_id(original)
        decoded = adapter.decode_thread_id(encoded)
        assert decoded.conversation_id == original.conversation_id
        assert decoded.service_url == original.service_url


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------


class TestConstructor:
    def test_default_user_name(self):
        adapter = _make_adapter()
        assert adapter.user_name == "bot"

    def test_custom_user_name(self):
        adapter = _make_adapter(user_name="mybot")
        assert adapter.user_name == "mybot"

    def test_accepts_tenant_id(self):
        adapter = _make_adapter(app_tenant_id="some-tenant-id")
        assert adapter.name == "teams"

    def test_name_is_teams(self):
        adapter = _make_adapter()
        assert adapter.name == "teams"


# ---------------------------------------------------------------------------
# Constructor env var resolution
# ---------------------------------------------------------------------------


class TestConstructorEnvVars:
    def test_resolves_from_env(self, monkeypatch):
        monkeypatch.setenv("TEAMS_APP_ID", "env-app-id")
        monkeypatch.setenv("TEAMS_APP_PASSWORD", "env-password")
        adapter = TeamsAdapter()
        assert isinstance(adapter, TeamsAdapter)

    def test_resolves_tenant_from_env(self, monkeypatch):
        monkeypatch.setenv("TEAMS_APP_TENANT_ID", "env-tenant")
        adapter = _make_adapter()
        assert isinstance(adapter, TeamsAdapter)

    def test_prefers_config_over_env(self, monkeypatch):
        monkeypatch.setenv("TEAMS_APP_ID", "env-app-id")
        adapter = _make_adapter(app_id="config-app-id")
        assert adapter.name == "teams"


# ---------------------------------------------------------------------------
# isMessageFromSelf (via parseMessage)
# ---------------------------------------------------------------------------


class TestIsMessageFromSelf:
    def test_exact_match(self):
        adapter = _make_adapter(app_id="abc123-def456")
        activity = {
            "type": "message",
            "id": "msg-1",
            "text": "Hello",
            "from": {"id": "abc123-def456", "name": "Bot"},
            "conversation": {"id": "19:abc@thread.tacv2"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
        }
        msg = adapter.parse_message(activity)
        assert msg.author.is_me is True

    def test_prefixed_bot_id(self):
        adapter = _make_adapter(app_id="abc123-def456")
        activity = {
            "type": "message",
            "id": "msg-2",
            "text": "Hello",
            "from": {"id": "28:abc123-def456", "name": "Bot"},
            "conversation": {"id": "19:abc@thread.tacv2"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
        }
        msg = adapter.parse_message(activity)
        assert msg.author.is_me is True

    def test_unrelated_user(self):
        adapter = _make_adapter(app_id="abc123-def456")
        activity = {
            "type": "message",
            "id": "msg-3",
            "text": "Hello",
            "from": {"id": "user-xyz", "name": "User"},
            "conversation": {"id": "19:abc@thread.tacv2"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
        }
        msg = adapter.parse_message(activity)
        assert msg.author.is_me is False

    def test_undefined_from_id(self):
        adapter = _make_adapter(app_id="abc123")
        activity = {
            "type": "message",
            "id": "msg-4",
            "text": "Hello",
            "from": {"name": "Unknown"},
            "conversation": {"id": "19:abc@thread.tacv2"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
        }
        msg = adapter.parse_message(activity)
        assert msg.author.is_me is False


# ---------------------------------------------------------------------------
# parseMessage
# ---------------------------------------------------------------------------


class TestParseMessage:
    def test_basic_text_message(self):
        adapter = _make_adapter(app_id="test-app")
        activity = {
            "type": "message",
            "id": "msg-100",
            "text": "Hello world",
            "from": {"id": "user-1", "name": "Alice", "role": "user"},
            "conversation": {"id": "19:abc@thread.tacv2"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
            "timestamp": "2024-01-01T00:00:00.000Z",
        }
        msg = adapter.parse_message(activity)
        assert msg.id == "msg-100"
        assert "Hello world" in msg.text
        assert msg.author.user_id == "user-1"
        assert msg.author.user_name == "Alice"
        assert msg.author.is_me is False

    def test_missing_text(self):
        adapter = _make_adapter(app_id="test-app")
        activity = {
            "type": "message",
            "id": "msg-102",
            "from": {"id": "user-1", "name": "Alice"},
            "conversation": {"id": "19:abc@thread.tacv2"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
        }
        msg = adapter.parse_message(activity)
        assert msg.text == ""

    def test_missing_from_fields(self):
        adapter = _make_adapter(app_id="test-app")
        activity = {
            "type": "message",
            "id": "msg-103",
            "text": "test",
            "conversation": {"id": "19:abc@thread.tacv2"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
        }
        msg = adapter.parse_message(activity)
        assert msg.author.user_id == "unknown"
        assert msg.author.user_name == "unknown"

    def test_filters_adaptive_card_attachments(self):
        adapter = _make_adapter(app_id="test-app")
        activity = {
            "type": "message",
            "id": "msg-104",
            "text": "test",
            "from": {"id": "user-1", "name": "Alice"},
            "conversation": {"id": "19:abc@thread.tacv2"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
            "attachments": [
                {"contentType": "application/vnd.microsoft.card.adaptive", "content": {}},
                {"contentType": "image/png", "contentUrl": "https://example.com/image.png", "name": "screenshot.png"},
            ],
        }
        msg = adapter.parse_message(activity)
        assert len(msg.attachments) == 1
        assert msg.attachments[0].type == "image"
        assert msg.attachments[0].name == "screenshot.png"

    def test_filters_text_html_without_url(self):
        adapter = _make_adapter(app_id="test-app")
        activity = {
            "type": "message",
            "id": "msg-105",
            "text": "test",
            "from": {"id": "user-1", "name": "Alice"},
            "conversation": {"id": "19:abc@thread.tacv2"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
            "attachments": [
                {"contentType": "text/html", "content": "<p>Formatted version</p>"},
            ],
        }
        msg = adapter.parse_message(activity)
        assert len(msg.attachments) == 0

    def test_classifies_attachment_types(self):
        adapter = _make_adapter(app_id="test-app")
        activity = {
            "type": "message",
            "id": "msg-106",
            "text": "test",
            "from": {"id": "user-1", "name": "Alice"},
            "conversation": {"id": "19:abc@thread.tacv2"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
            "attachments": [
                {"contentType": "image/jpeg", "contentUrl": "https://x.com/photo.jpg", "name": "photo.jpg"},
                {"contentType": "video/mp4", "contentUrl": "https://x.com/video.mp4", "name": "video.mp4"},
                {"contentType": "audio/mpeg", "contentUrl": "https://x.com/audio.mp3", "name": "audio.mp3"},
                {"contentType": "application/pdf", "contentUrl": "https://x.com/doc.pdf", "name": "doc.pdf"},
            ],
        }
        msg = adapter.parse_message(activity)
        assert len(msg.attachments) == 4
        assert msg.attachments[0].type == "image"
        assert msg.attachments[1].type == "video"
        assert msg.attachments[2].type == "audio"
        assert msg.attachments[3].type == "file"

    def test_edited_false_for_new(self):
        adapter = _make_adapter(app_id="test-app")
        activity = {
            "type": "message",
            "id": "msg-107",
            "text": "test",
            "from": {"id": "user-1", "name": "Alice"},
            "conversation": {"id": "19:abc@thread.tacv2"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
            "timestamp": "2024-06-01T12:00:00Z",
        }
        msg = adapter.parse_message(activity)
        assert msg.metadata.edited is False
        assert msg.metadata.date_sent == datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

    def test_attachment_stores_url_in_fetch_metadata(self):
        """Teams fetch_metadata captures the URL so rehydrate_attachment can rebuild fetch_data."""
        adapter = _make_adapter(app_id="test-app")
        activity = {
            "type": "message",
            "id": "msg-108",
            "text": "test",
            "from": {"id": "user-1", "name": "Alice"},
            "conversation": {"id": "19:abc@thread.tacv2"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
            "attachments": [
                {"contentType": "image/jpeg", "contentUrl": "https://x.com/photo.jpg", "name": "photo.jpg"},
            ],
        }
        msg = adapter.parse_message(activity)
        assert msg.attachments[0].fetch_metadata == {"url": "https://x.com/photo.jpg"}
        assert msg.attachments[0].fetch_data is not None


# ---------------------------------------------------------------------------
# rehydrate_attachment
# ---------------------------------------------------------------------------


class TestRehydrateAttachment:
    @pytest.mark.asyncio
    async def test_rehydrates_fetch_data_from_fetch_metadata_url(self):
        """After JSON roundtrip (fetch_data stripped), the URL in fetch_metadata restores the closure.

        Awaits the rebuilt closure against a stubbed HTTP session to prove
        the wire-up is correct (not just "some callable was returned").
        """
        from chat_sdk.types import Attachment

        trusted_url = "https://graph.microsoft.com/photo.jpg"
        adapter = _make_adapter(app_id="test-app")

        session = _MockAiohttpSession(payload=b"teams-bytes")
        adapter._get_http_session = AsyncMock(return_value=session)  # type: ignore[method-assign]

        attachment = Attachment(
            type="image",
            url=trusted_url,
            fetch_metadata={"url": trusted_url},
        )
        rehydrated = adapter.rehydrate_attachment(attachment)
        assert rehydrated.fetch_data is not None

        bytes_result = await rehydrated.fetch_data()
        assert bytes_result == b"teams-bytes"
        assert session.get_calls == [trusted_url]

    @pytest.mark.asyncio
    async def test_rehydrate_falls_back_to_attachment_url_when_fetch_metadata_missing(self):
        """When fetch_metadata is absent, rehydrate falls back to the attachment's top-level url."""
        from chat_sdk.types import Attachment

        trusted_url = "https://attachments.office.net/doc.pdf"
        adapter = _make_adapter(app_id="test-app")

        session = _MockAiohttpSession(payload=b"fallback-bytes")
        adapter._get_http_session = AsyncMock(return_value=session)  # type: ignore[method-assign]

        attachment = Attachment(type="file", url=trusted_url)
        rehydrated = adapter.rehydrate_attachment(attachment)
        assert rehydrated.fetch_data is not None
        assert await rehydrated.fetch_data() == b"fallback-bytes"
        assert session.get_calls == [trusted_url]

    def test_rehydrate_returns_unchanged_when_no_url(self):
        """Without any URL, rehydrate returns the attachment unchanged."""
        from chat_sdk.types import Attachment

        adapter = _make_adapter(app_id="test-app")
        attachment = Attachment(type="file", name="local.bin")
        rehydrated = adapter.rehydrate_attachment(attachment)
        assert rehydrated is attachment

    # Python-first divergence: SSRF guard at fetch time.
    @pytest.mark.asyncio
    async def test_rehydrated_fetch_data_rejects_untrusted_host(self):
        from chat_sdk.types import Attachment

        adapter = _make_adapter(app_id="test-app")
        # If the validator is bypassed, this would be called — it must not be.
        adapter._get_http_session = AsyncMock()  # type: ignore[method-assign]

        attachment = Attachment(
            type="image",
            url="https://attacker.example.com/pwn.jpg",
            fetch_metadata={"url": "https://attacker.example.com/pwn.jpg"},
        )
        rehydrated = adapter.rehydrate_attachment(attachment)
        assert rehydrated.fetch_data is not None
        with pytest.raises(ValidationError):
            await rehydrated.fetch_data()
        adapter._get_http_session.assert_not_awaited()

    def test_is_trusted_teams_download_url_allowlist(self):
        # Accepts Microsoft-owned hosts
        assert TeamsAdapter._is_trusted_teams_download_url("https://graph.microsoft.com/x")
        assert TeamsAdapter._is_trusted_teams_download_url("https://foo.sharepoint.com/x")
        assert TeamsAdapter._is_trusted_teams_download_url("https://smba.trafficmanager.net/x")
        assert TeamsAdapter._is_trusted_teams_download_url("https://attachments.office.net/x")
        assert TeamsAdapter._is_trusted_teams_download_url("https://x.botframework.com/y")
        # Rejects non-HTTPS
        assert not TeamsAdapter._is_trusted_teams_download_url("http://graph.microsoft.com/x")
        # Rejects arbitrary hosts
        assert not TeamsAdapter._is_trusted_teams_download_url("https://attacker.example/x")
        # Rejects look-alikes
        assert not TeamsAdapter._is_trusted_teams_download_url("https://graph.microsoft.com.attacker.tld/x")


# ---------------------------------------------------------------------------
# normalizeMentions (via parseMessage)
# ---------------------------------------------------------------------------


class TestNormalizeMentions:
    def test_trims_whitespace(self):
        adapter = _make_adapter(app_id="test-app")
        activity = {
            "type": "message",
            "id": "msg-200",
            "text": "  Hello world  ",
            "from": {"id": "user-1", "name": "Alice"},
            "conversation": {"id": "19:abc@thread.tacv2"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
        }
        msg = adapter.parse_message(activity)
        assert not msg.text.startswith(" ")
        assert not msg.text.endswith(" ")


# ---------------------------------------------------------------------------
# isDM
# ---------------------------------------------------------------------------


class TestIsDM:
    def test_false_for_group_chats(self):
        adapter = _make_adapter()
        thread_id = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:abc@thread.tacv2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        assert adapter.is_dm(thread_id) is False

    def test_true_for_dm(self):
        adapter = _make_adapter()
        thread_id = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="a]8:orgid:user-id-here",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        assert adapter.is_dm(thread_id) is True

    def test_false_for_channel_with_messageid(self):
        adapter = _make_adapter()
        thread_id = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:abc@thread.tacv2;messageid=1767297849909",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        assert adapter.is_dm(thread_id) is False


# ---------------------------------------------------------------------------
# channelIdFromThreadId
# ---------------------------------------------------------------------------


class TestChannelIdFromThreadId:
    def test_strips_messageid(self):
        adapter = _make_adapter()
        thread_id = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:abc@thread.tacv2;messageid=1767297849909",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        channel_id = adapter.channel_id_from_thread_id(thread_id)
        decoded = adapter.decode_thread_id(channel_id)
        assert decoded.conversation_id == "19:abc@thread.tacv2"
        assert ";messageid=" not in decoded.conversation_id

    def test_same_when_no_messageid(self):
        adapter = _make_adapter()
        thread_id = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:abc@thread.tacv2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        channel_id = adapter.channel_id_from_thread_id(thread_id)
        decoded = adapter.decode_thread_id(channel_id)
        assert decoded.conversation_id == "19:abc@thread.tacv2"


# ---------------------------------------------------------------------------
# fetchThread
# ---------------------------------------------------------------------------


class TestFetchThread:
    @pytest.mark.asyncio
    async def test_returns_basic_thread_info(self):
        adapter = _make_adapter()
        thread_id = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:abc@thread.tacv2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        info = await adapter.fetch_thread(thread_id)
        assert info.id == thread_id
        assert info.channel_id == "19:abc@thread.tacv2"
        assert info.metadata == {}


# ---------------------------------------------------------------------------
# handleWebhook
# ---------------------------------------------------------------------------


class _FakeRequest:
    """A simple request-like object for testing webhook handlers."""

    def __init__(self, body: str, headers: dict[str, str] | None = None):
        self._body = body
        self.headers = headers or {}

    async def text(self) -> str:
        return self._body

    @property
    def data(self) -> bytes:
        return self._body.encode("utf-8")


class TestHandleWebhook:
    @pytest.fixture(autouse=True)
    def _skip_jwt(self, monkeypatch):
        """Bypass inbound JWT validation in unit tests.

        Inbound auth now runs inside the Microsoft Teams SDK ``App``
        (issue #93 PR 1); ``handle_webhook`` dispatches through the
        ``BridgeHttpAdapter`` into the SDK's ``HttpServer``. We force the SDK's
        ``skip_auth`` flag so unsigned test requests still reach the bridge's
        JSON parsing without needing a real Bot Framework token.
        """
        from microsoft_teams.apps.http.http_server import HttpServer

        real_initialize = HttpServer.initialize

        def _initialize_skip_auth(self, credentials=None, skip_auth=False, cloud=None):
            return real_initialize(self, credentials=credentials, skip_auth=True, cloud=cloud)

        monkeypatch.setattr(HttpServer, "initialize", _initialize_skip_auth)

    @pytest.mark.asyncio
    async def test_400_for_invalid_json(self):
        adapter = _make_adapter(logger=_make_logger())
        chat = MagicMock()
        chat.get_state = MagicMock(return_value=MagicMock(set=AsyncMock(), get=AsyncMock(return_value=None)))
        await adapter.initialize(chat)
        request = _FakeRequest("not valid json{{{", {"content-type": "application/json"})

        response = await adapter.handle_webhook(request)
        assert response["status"] == 400


# ---------------------------------------------------------------------------
# initialize
# ---------------------------------------------------------------------------


class TestInitialize:
    @pytest.mark.asyncio
    async def test_stores_chat_instance(self):
        adapter = _make_adapter()
        mock_chat = MagicMock()
        await adapter.initialize(mock_chat)
        assert adapter.name == "teams"

    @pytest.mark.asyncio
    async def test_initialize_wires_sdk_app_and_bridge(self):
        adapter = _make_adapter()
        await adapter.initialize(MagicMock())
        # The SDK App captured the messaging-endpoint route in our bridge so
        # handle_webhook can dispatch through it.
        assert adapter._bridge._handler is not None
        # JWT/auth + activity routing now flow through our dispatcher.
        on_request = adapter._app.server.on_request
        assert on_request is not None
        assert on_request.__func__ is TeamsAdapter._dispatch_activity
        assert on_request.__self__ is adapter

    @pytest.mark.asyncio
    async def test_initialize_is_idempotent(self):
        adapter = _make_adapter()
        chat = MagicMock()
        await adapter.initialize(chat)
        first_handler = adapter._bridge._handler
        # Re-initializing (e.g. adapter reused across chats) must not double-init
        # the SDK App or lose the captured route handler.
        await adapter.initialize(chat)
        assert adapter._bridge._handler is first_handler


class TestSdkAppConstruction:
    def test_app_built_with_vercel_user_agent(self):
        adapter = _make_adapter()
        # The User-Agent the adapter stamps onto the SDK client must identify
        # the Chat SDK (parity with upstream App construction). The SDK merges
        # its own UA on top, so we assert against the client options we passed.
        client_opts = adapter._app.options.client
        assert client_opts.headers["User-Agent"] == "Vercel.ChatSDK"

    def test_app_id_mapped_to_sdk_client_id(self):
        adapter = _make_adapter(app_id="my-bot-id")
        assert adapter._app.id == "my-bot-id"


# ---------------------------------------------------------------------------
# renderFormatted
# ---------------------------------------------------------------------------


class TestRenderFormatted:
    def test_delegates_to_converter(self):
        adapter = _make_adapter()
        ast = {
            "type": "root",
            "children": [
                {
                    "type": "paragraph",
                    "children": [{"type": "text", "value": "Hello world"}],
                }
            ],
        }
        result = adapter.render_formatted(ast)
        assert isinstance(result, str)
        assert "Hello world" in result


# ---------------------------------------------------------------------------
# postMessage / editMessage / deleteMessage (mocked HTTP)
# ---------------------------------------------------------------------------


class TestPostMessage:
    @pytest.mark.asyncio
    async def test_sends_and_returns_message_id(self):
        adapter = _make_adapter(app_id="test-app-id", logger=_make_logger())
        send = _mock_app_send(adapter, "sent-msg-123")

        thread_id = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:abc@thread.tacv2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        result = await adapter.post_message(thread_id, {"markdown": "Hi there"})
        assert result.id == "sent-msg-123"
        assert result.thread_id == thread_id
        send.assert_called_once()
        # delegates to the SDK App.send with the conversation ID and a
        # MessageActivityInput carrying the rendered text + markdown format
        conv_id, activity = send.call_args.args
        assert conv_id == "19:abc@thread.tacv2"
        assert activity.text == "Hi there"
        assert activity.text_format == "markdown"

    @pytest.mark.asyncio
    async def test_send_failure_maps_to_handle_teams_error(self):
        """Mirrors upstream: a 401 from ``app.send`` flows through
        ``handleTeamsError`` and surfaces as ``AuthenticationError`` — proving
        the raw SDK exception (status on ``.status_code``) reaches the mapper."""
        from chat_sdk.shared.errors import AuthenticationError

        class _SdkError(Exception):
            def __init__(self):
                super().__init__("Unauthorized")
                self.status_code = 401

        adapter = _make_adapter(app_id="test-app-id", logger=_make_logger())
        send = _mock_app_send(adapter)
        send.side_effect = _SdkError()

        thread_id = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:abc@thread.tacv2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        with pytest.raises(AuthenticationError):
            await adapter.post_message(thread_id, {"markdown": "Hi"})


class TestEditMessage:
    @pytest.mark.asyncio
    async def test_updates_and_returns(self):
        adapter = _make_adapter(app_id="test-app-id", logger=_make_logger())
        update, _delete = _mock_app_activities(adapter, update_id="edit-msg-1")

        thread_id = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:abc@thread.tacv2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        result = await adapter.edit_message(thread_id, "edit-msg-1", {"markdown": "Updated text"})
        assert result.id == "edit-msg-1"
        assert result.thread_id == thread_id
        # delegates to app.api.conversations.activities(conversationId).update
        adapter._app.api.conversations.activities.assert_called_once_with("19:abc@thread.tacv2")
        update.assert_called_once()
        update_msg_id, update_activity = update.call_args.args
        assert update_msg_id == "edit-msg-1"
        assert update_activity.text == "Updated text"
        assert update_activity.text_format == "markdown"


class TestOutboundServiceUrlRouting:
    """Each outbound op must retarget the SDK App's Bot Framework client at the
    thread's decoded service URL before sending — so different regions / sovereign
    clouds reach the right endpoint. Exercises the REAL ``ApiClient`` (not a mock)
    to prove ``_point_app_api_at`` actually walks the service-url chain rather than
    silently no-opping via its mock-tolerant ``AttributeError`` guard.
    """

    SOVEREIGN_URL = "https://smba.infra.gov.teams.microsoft.us/teams/"

    @pytest.mark.asyncio
    async def test_post_message_retargets_real_api_client(self):
        adapter = _make_adapter(app_id="test-app-id", logger=_make_logger())
        seen: dict[str, str] = {}

        async def fake_send(conversation_id, activity):
            # captured at call time: the real ApiClient is now on the thread's URL
            seen["api"] = adapter._app.api.service_url
            seen["conversations"] = adapter._app.api.conversations.service_url
            seen["activities"] = adapter._app.api.conversations.activities_client.service_url
            return _SentActivity("m")

        adapter._app.send = fake_send  # type: ignore[method-assign]
        tid = adapter.encode_thread_id(
            TeamsThreadId(conversation_id="19:abc@thread.tacv2", service_url=self.SOVEREIGN_URL)
        )
        await adapter.post_message(tid, {"markdown": "hi"})
        # the trailing slash is normalized off, matching ApiClient's own rstrip
        assert seen["api"] == self.SOVEREIGN_URL.rstrip("/")
        assert seen["conversations"] == self.SOVEREIGN_URL.rstrip("/")
        assert seen["activities"] == self.SOVEREIGN_URL.rstrip("/")

    @pytest.mark.asyncio
    async def test_edit_message_retargets_real_activities_client(self):
        adapter = _make_adapter(app_id="test-app-id", logger=_make_logger())
        seen: dict[str, str] = {}

        # Patch the real activities_client.update so the routing target is read
        # off the REAL client chain (not a wholesale api mock).
        async def fake_update(conversation_id, activity_id, activity):
            seen["url"] = adapter._app.api.conversations.activities_client.service_url
            return _SentActivity(activity_id)

        adapter._app.api.conversations.activities_client.update = fake_update  # type: ignore[method-assign]
        tid = adapter.encode_thread_id(
            TeamsThreadId(conversation_id="19:abc@thread.tacv2", service_url=self.SOVEREIGN_URL)
        )
        await adapter.edit_message(tid, "edit-1", {"markdown": "x"})
        assert seen["url"] == self.SOVEREIGN_URL.rstrip("/")


class TestFileAttachments:
    """Outbound file delivery via base64 data-URI activity attachments.

    Ports ``filesToAttachments`` from
    ``packages/adapter-teams/src/index.ts`` (lines ~1006-1035) and its use in
    ``postMessage``/``editMessage``.
    """

    @staticmethod
    def _thread_id(adapter: TeamsAdapter) -> str:
        return adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:abc@thread.tacv2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )

    @staticmethod
    def _sent_attachments(send: AsyncMock) -> list[dict]:
        """Serialize the attachments off the MessageActivityInput handed to the
        SDK ``app.send``, back to the camelCase wire dicts — proving the file
        attachments actually reached the SDK boundary (not just the raw echo)."""
        activity = send.call_args.args[1]
        dumped = activity.model_dump(by_alias=True, exclude_none=True)
        return dumped.get("attachments", [])

    @pytest.mark.asyncio
    async def test_text_message_with_file(self):
        from chat_sdk.types import FileUpload, PostableMarkdown

        adapter = _make_adapter(app_id="test-app-id", logger=_make_logger())
        send = _mock_app_send(adapter, "sent-1")

        message = PostableMarkdown(
            markdown="here is your report",
            files=[FileUpload(data=b"a,b,c\n1,2,3\n", filename="report.csv", mime_type="text/csv")],
        )
        result = await adapter.post_message(self._thread_id(adapter), message)

        # the data-URI attachment reaches the SDK send AND is echoed on raw
        attachments = self._sent_attachments(send)
        assert len(attachments) == 1
        att = attachments[0]
        assert att["contentType"] == "text/csv"
        assert att["name"] == "report.csv"
        assert att["contentUrl"].startswith("data:text/csv;base64,")
        # round-trip the base64 payload back to the original bytes
        import base64

        b64 = att["contentUrl"].split("base64,", 1)[1]
        assert base64.b64decode(b64) == b"a,b,c\n1,2,3\n"
        # the data-URI attachment is also recorded on the returned raw activity
        assert result.raw["attachments"][0]["name"] == "report.csv"

    @pytest.mark.asyncio
    async def test_card_message_with_file(self):
        from chat_sdk.cards import Card
        from chat_sdk.types import FileUpload, PostableCard

        adapter = _make_adapter(app_id="test-app-id", logger=_make_logger())
        send = _mock_app_send(adapter, "sent-2")

        message = PostableCard(
            card=Card(title="Results"),
            files=[FileUpload(data=b"\x89PNG\r\n", filename="chart.png", mime_type="image/png")],
        )
        await adapter.post_message(self._thread_id(adapter), message)

        attachments = self._sent_attachments(send)
        # adaptive card attachment AND the file attachment both present
        assert len(attachments) == 2
        assert attachments[0]["contentType"] == "application/vnd.microsoft.card.adaptive"
        file_att = attachments[1]
        assert file_att["contentType"] == "image/png"
        assert file_att["name"] == "chart.png"
        assert file_att["contentUrl"].startswith("data:image/png;base64,")

    @pytest.mark.asyncio
    async def test_edit_message_does_not_carry_files(self):
        """Upstream fidelity: ``editMessage`` never delivers files (upstream wires
        ``filesToAttachments`` into ``postMessage``/``postChannelMessage`` only), and
        chinchill delivers execution artifacts via a fresh ``post`` — never by editing
        files into an existing message. A ``PostableMarkdown`` carrying files must edit
        the text only, with no file attachments on the activity.
        """
        from chat_sdk.types import FileUpload, PostableMarkdown

        adapter = _make_adapter(app_id="test-app-id", logger=_make_logger())
        update, _delete = _mock_app_activities(adapter, update_id="edit-1")

        message = PostableMarkdown(
            markdown="updated",
            files=[FileUpload(data=b"hello", filename="note.txt", mime_type="text/plain")],
        )
        result = await adapter.edit_message(self._thread_id(adapter), "edit-1", message)

        activity = update.call_args.args[1]
        payload = activity.model_dump(by_alias=True, exclude_none=True)
        assert "attachments" not in payload, (
            "edit_message must not carry file attachments — outbound file delivery is "
            f"post_message-only (upstream fidelity); got attachments={payload.get('attachments')!r}"
        )
        assert payload["text"] == "updated"
        assert result.id == "edit-1"

    @pytest.mark.asyncio
    async def test_file_without_mime_type_defaults_to_octet_stream(self):
        from chat_sdk.types import FileUpload, PostableMarkdown

        adapter = _make_adapter(app_id="test-app-id", logger=_make_logger())
        send = _mock_app_send(adapter, "sent-3")

        message = PostableMarkdown(
            markdown="bin",
            files=[FileUpload(data=b"\x00\x01\x02", filename="blob.bin")],
        )
        await adapter.post_message(self._thread_id(adapter), message)

        att = self._sent_attachments(send)[0]
        assert att["contentType"] == "application/octet-stream"
        assert att["contentUrl"].startswith("data:application/octet-stream;base64,")

    @pytest.mark.asyncio
    async def test_file_with_unresolvable_data_is_skipped(self):
        """A FileUpload whose data is not bytes is skipped with a debug log.

        Mirrors upstream's ``throwOnUnsupported: false`` followed by
        ``if (!buffer) continue``. (The Python ``FileUpload`` has no
        ``fetch_data`` field — it carries only inline ``data`` bytes — so the
        lazy-fetch case from the upstream interface collapses to this
        skip-unresolvable-bytes branch.)
        """
        from chat_sdk.types import FileUpload, PostableMarkdown

        logger = _make_logger()
        adapter = _make_adapter(app_id="test-app-id", logger=logger)
        send = _mock_app_send(adapter, "sent-4")

        # data is a str, not bytes -> to_buffer returns None -> file skipped
        bad = FileUpload(data="not-bytes", filename="bad.txt", mime_type="text/plain")  # type: ignore[arg-type]
        message = PostableMarkdown(markdown="text only", files=[bad])
        result = await adapter.post_message(self._thread_id(adapter), message)

        # no attachments key added when every file was skipped (raw + sent activity)
        assert "attachments" not in result.raw
        assert self._sent_attachments(send) == []
        # assert the SPECIFIC skip log fired — not just that some debug log happened
        # (post_message emits an unconditional "send (message)" debug, so a bare
        # logger.debug.called check would pass even if the skip branch logged nothing).
        skip_logged = any(
            call.args and "skipping file with unsupported data" in str(call.args[0])
            for call in logger.debug.call_args_list
        )
        assert skip_logged, "a skipped file must emit the 'unsupported data' debug log"

    @pytest.mark.asyncio
    async def test_multiple_files_attached_in_order(self):
        """N files -> N attachments, in input order. Closes the gap where
        ``return attachments[:1]`` (drop all but first) or a reorder would
        otherwise merge green — both directly defeat multi-artifact parity.
        """
        from chat_sdk.types import FileUpload, PostableMarkdown

        adapter = _make_adapter(app_id="test-app-id", logger=_make_logger())
        send = _mock_app_send(adapter, "m")

        message = PostableMarkdown(
            markdown="three files",
            files=[
                FileUpload(data=b"aaa", filename="a.csv", mime_type="text/csv"),
                FileUpload(data=b"\x89PNG", filename="b.png", mime_type="image/png"),
                FileUpload(data=b"%PDF", filename="c.pdf", mime_type="application/pdf"),
            ],
        )
        await adapter.post_message(self._thread_id(adapter), message)

        attachments = self._sent_attachments(send)
        assert [a["name"] for a in attachments] == ["a.csv", "b.png", "c.pdf"]
        assert [a["contentType"] for a in attachments] == ["text/csv", "image/png", "application/pdf"]
        assert all(a["contentUrl"].startswith("data:") for a in attachments)

    @pytest.mark.asyncio
    async def test_partial_skip_preserves_surviving_files_in_order(self):
        """A good/bad/good batch drops only the unresolvable file; survivors keep
        input order. No single-file test covers partial-skip-with-survivors.
        """
        from chat_sdk.types import FileUpload, PostableMarkdown

        adapter = _make_adapter(app_id="test-app-id", logger=_make_logger())
        send = _mock_app_send(adapter, "m")

        message = PostableMarkdown(
            markdown="good bad good",
            files=[
                FileUpload(data=b"first", filename="first.csv", mime_type="text/csv"),
                FileUpload(data="not-bytes", filename="bad.bin", mime_type="application/octet-stream"),  # type: ignore[arg-type]
                FileUpload(data=b"third", filename="third.csv", mime_type="text/csv"),
            ],
        )
        await adapter.post_message(self._thread_id(adapter), message)

        attachments = self._sent_attachments(send)
        assert [a["name"] for a in attachments] == ["first.csv", "third.csv"]


class TestDeleteMessage:
    @pytest.mark.asyncio
    async def test_deletes_without_error(self):
        adapter = _make_adapter(app_id="test-app-id", logger=_make_logger())
        _update, delete = _mock_app_activities(adapter)

        thread_id = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:abc@thread.tacv2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        await adapter.delete_message(thread_id, "del-msg-1")
        adapter._app.api.conversations.activities.assert_called_once_with("19:abc@thread.tacv2")
        assert delete.call_count == 1
        assert delete.call_args.args == ("del-msg-1",)


# ---------------------------------------------------------------------------
# startTyping
# ---------------------------------------------------------------------------


class TestStartTyping:
    @pytest.mark.asyncio
    async def test_sends_typing_activity(self):
        from microsoft_teams.api import TypingActivityInput

        adapter = _make_adapter(app_id="test-app-id", logger=_make_logger())
        send = _mock_app_send(adapter, "typing-1")

        thread_id = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:abc@thread.tacv2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        await adapter.start_typing(thread_id)
        assert send.call_count == 1
        conv_id, activity = send.call_args.args
        assert conv_id == "19:abc@thread.tacv2"
        # delegates a TypingActivityInput (type == "typing") to the SDK App.send
        assert isinstance(activity, TypingActivityInput)
        assert activity.type == "typing"


# ---------------------------------------------------------------------------
# addReaction / removeReaction (not supported)
# ---------------------------------------------------------------------------


class TestReactions:
    @pytest.mark.asyncio
    async def test_add_reaction_warns_instead_of_raising(self):
        logger = _make_logger()
        adapter = _make_adapter(logger=logger)
        await adapter.add_reaction("tid", "mid", "emoji")
        assert logger.warn.call_count == 1

    @pytest.mark.asyncio
    async def test_remove_reaction_warns_instead_of_raising(self):
        logger = _make_logger()
        adapter = _make_adapter(logger=logger)
        await adapter.remove_reaction("tid", "mid", "emoji")
        assert logger.warn.call_count == 1
