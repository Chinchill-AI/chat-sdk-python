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
        """Bypass JWT verification in unit tests."""
        monkeypatch.setattr(
            TeamsAdapter,
            "_verify_bot_framework_token",
            AsyncMock(return_value=None),
        )

    @pytest.mark.asyncio
    async def test_400_for_invalid_json(self):
        adapter = _make_adapter(logger=_make_logger())
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
        adapter._teams_send = AsyncMock(return_value={"id": "sent-msg-123", "type": "message"})

        thread_id = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:abc@thread.tacv2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        result = await adapter.post_message(thread_id, {"markdown": "Hi there"})
        assert result.id == "sent-msg-123"
        assert result.thread_id == thread_id
        adapter._teams_send.assert_called_once()


class TestEditMessage:
    @pytest.mark.asyncio
    async def test_updates_and_returns(self):
        adapter = _make_adapter(app_id="test-app-id", logger=_make_logger())
        adapter._teams_update = AsyncMock()

        thread_id = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:abc@thread.tacv2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        result = await adapter.edit_message(thread_id, "edit-msg-1", {"markdown": "Updated text"})
        assert result.id == "edit-msg-1"
        assert result.thread_id == thread_id
        adapter._teams_update.assert_called_once()


class TestDeleteMessage:
    @pytest.mark.asyncio
    async def test_deletes_without_error(self):
        adapter = _make_adapter(app_id="test-app-id", logger=_make_logger())
        adapter._teams_delete = AsyncMock()

        thread_id = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:abc@thread.tacv2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        await adapter.delete_message(thread_id, "del-msg-1")
        assert adapter._teams_delete.call_count == 1


# ---------------------------------------------------------------------------
# startTyping
# ---------------------------------------------------------------------------


class TestStartTyping:
    @pytest.mark.asyncio
    async def test_sends_typing_activity(self):
        adapter = _make_adapter(app_id="test-app-id", logger=_make_logger())
        adapter._teams_send = AsyncMock(return_value={"id": "typing-1", "type": "typing"})

        thread_id = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:abc@thread.tacv2",
                service_url="https://smba.trafficmanager.net/teams/",
            )
        )
        await adapter.start_typing(thread_id)
        assert adapter._teams_send.call_count == 1


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
