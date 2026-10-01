"""Tests for the Teams adapter -- constructor, thread IDs, webhook handling, message operations.

Ported from packages/adapter-teams/src/index.test.ts.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import re
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
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
    """Replace the SDK activity sender with an AsyncMock returning a SentActivity.

    Outbound send/typing paths go through ``TeamsAdapter._send_to``, which hands
    ``(activity, ConversationReference)`` to ``App.activity_sender.send`` when the
    App has one (the 2.0.x shape, set here on every SDK line). Mirrors upstream's
    ``vi.spyOn(app.activitySender, "send")``. Returns the mock so tests can
    assert call count / arguments.
    """
    send = AsyncMock(return_value=_SentActivity(sent_id))
    adapter._app.activity_sender = SimpleNamespace(send=send)
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
    # The SDK default service URL: threads on it use ``App.api`` itself.
    api.service_url = "https://smba.trafficmanager.net/teams"
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

    def test_should_create_adapter_with_a_custom_token_factory(self):
        async def token(_scope, _tenant_id=None):
            return "custom-access-token"

        adapter = create_teams_adapter(TeamsAdapterConfig(app_id="test", app_tenant_id="test-tenant", token=token))
        assert isinstance(adapter, TeamsAdapter)
        # The factory reaches the SDK App (no client secret configured at all).
        assert adapter._app.credentials.token is token


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
        valid = adapter.encode_thread_id(
            TeamsThreadId(conversation_id="a:x", service_url="https://smba.trafficmanager.net/teams/")
        )
        # A fourth segment must be a known conversation type, and no fifth.
        with pytest.raises(ValidationError, match="conversation type"):
            adapter.decode_thread_id(f"{valid}:meeting")
        with pytest.raises(ValidationError):
            adapter.decode_thread_id(f"{valid}:groupChat:extra")

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
# file.download.info attachments (content.downloadUrl fallback)
#
# Python-first divergence (supersedes stale PR #136). A SharePoint/OneDrive
# file shared in a personal/group chat arrives as a
# ``application/vnd.microsoft.teams.file.download.info`` attachment whose
# top-level ``contentUrl`` (the SharePoint item) 403s on an anonymous GET,
# while the nested ``content.downloadUrl`` is a pre-signed link that works.
# Upstream reads ``contentUrl`` only (adapter-teams/src/index.ts:833), so the
# attachment is undownloadable. We prefer ``content.downloadUrl`` for this
# attachment type. See docs/UPSTREAM_SYNC.md Known Non-Parity.
# ---------------------------------------------------------------------------

_FILE_DOWNLOAD_INFO = "application/vnd.microsoft.teams.file.download.info"


def _file_download_activity(attachment: dict) -> dict:
    return {
        "type": "message",
        "id": "msg-file-dl",
        "text": "here is a file",
        "from": {"id": "user-1", "name": "Alice"},
        "conversation": {"id": "19:abc@thread.tacv2"},
        "serviceUrl": "https://smba.trafficmanager.net/teams/",
        "attachments": [attachment],
    }


class TestFileDownloadInfoAttachment:
    def test_prefers_download_url_over_403_content_url(self):
        """A file.download.info attachment carries BOTH a (403-ing) contentUrl
        and a pre-signed content.downloadUrl; the Attachment must use the
        downloadUrl, not the SharePoint contentUrl.

        Pins the exact URL (not just "non-None") so a mutation that reverts to
        ``contentUrl`` is caught: the SharePoint item URL would surface instead.
        """
        adapter = _make_adapter(app_id="test-app")
        content_url = "https://contoso.sharepoint.com/personal/jadams/Documents/report.pdf"
        download_url = "https://contoso.sharepoint.com/_layouts/download.aspx?presigned=abc123"
        activity = _file_download_activity(
            {
                "contentType": _FILE_DOWNLOAD_INFO,
                "contentUrl": content_url,
                "name": "report.pdf",
                "content": {
                    "downloadUrl": download_url,
                    "uniqueId": "1150D938-8870-4044-9F2C-5BBDEBA70C9D",
                    "fileType": "pdf",
                },
            }
        )
        msg = adapter.parse_message(activity)
        assert len(msg.attachments) == 1
        att = msg.attachments[0]
        assert att.url == download_url
        assert att.url != content_url
        # fetch_metadata must carry the SAME (working) URL so rehydrate rebuilds
        # the closure around the pre-signed link, not the 403-ing one.
        assert att.fetch_metadata == {"url": download_url}
        assert att.name == "report.pdf"
        assert att.type == "file"

    def test_uses_download_url_when_content_url_absent(self):
        """A file.download.info attachment with no top-level contentUrl still
        yields a downloadable Attachment via content.downloadUrl."""
        adapter = _make_adapter(app_id="test-app")
        download_url = "https://contoso.sharepoint.com/_layouts/download.aspx?presigned=xyz"
        activity = _file_download_activity(
            {
                "contentType": _FILE_DOWNLOAD_INFO,
                "name": "notes.txt",
                "content": {"downloadUrl": download_url, "fileType": "txt"},
            }
        )
        msg = adapter.parse_message(activity)
        assert len(msg.attachments) == 1
        att = msg.attachments[0]
        assert att.url == download_url
        assert att.fetch_metadata == {"url": download_url}
        assert att.fetch_data is not None

    def test_regular_attachment_uses_content_url_unchanged(self):
        """An ordinary (inline image) attachment keeps the upstream contentUrl
        path — the downloadUrl fallback must not perturb it.

        The attachment deliberately ALSO carries a ``content.downloadUrl`` so an
        over-eager mutation that prefers ``content.downloadUrl`` for *every*
        attachment type (rather than only ``file.download.info``) is caught: it
        would surface the downloadUrl instead of the inline-image contentUrl.
        """
        adapter = _make_adapter(app_id="test-app")
        content_url = "https://smba.trafficmanager.net/teams/v3/attachments/img/photo.png"
        activity = _file_download_activity(
            {
                "contentType": "image/png",
                "contentUrl": content_url,
                "name": "photo.png",
                # A bogus competing downloadUrl that must be IGNORED for a
                # non-file.download.info attachment.
                "content": {"downloadUrl": "https://attacker.example.com/wrong.png"},
            }
        )
        msg = adapter.parse_message(activity)
        assert len(msg.attachments) == 1
        att = msg.attachments[0]
        assert att.url == content_url
        assert att.type == "image"
        assert att.fetch_metadata == {"url": content_url}

    @pytest.mark.asyncio
    async def test_download_url_flows_through_trusted_fetch(self):
        """The pre-signed downloadUrl becomes the URL the SSRF-gated fetch
        closure GETs — proving the fallback URL is what actually gets fetched.

        A SharePoint host is in the Teams allowlist, so the GET proceeds and
        returns the stubbed bytes.
        """
        adapter = _make_adapter(app_id="test-app")
        download_url = "https://contoso.sharepoint.com/_layouts/download.aspx?presigned=ok"
        session = _MockAiohttpSession(payload=b"file-contents")
        adapter._get_http_session = AsyncMock(return_value=session)  # type: ignore[method-assign]

        activity = _file_download_activity(
            {
                "contentType": _FILE_DOWNLOAD_INFO,
                "contentUrl": "https://contoso.sharepoint.com/personal/jadams/Documents/x.pdf",
                "name": "x.pdf",
                "content": {"downloadUrl": download_url, "fileType": "pdf"},
            }
        )
        att = adapter.parse_message(activity).attachments[0]
        assert att.fetch_data is not None
        assert await att.fetch_data() == b"file-contents"
        # The pre-signed URL — not the SharePoint item — is what gets fetched.
        assert session.get_calls == [download_url]

    @pytest.mark.asyncio
    async def test_download_url_still_gated_by_ssrf_allowlist(self):
        """Even via the downloadUrl path, an untrusted host fails closed at
        fetch time — the fallback does NOT bypass the SSRF allowlist."""
        adapter = _make_adapter(app_id="test-app")
        # If the gate were bypassed, the session would be awaited — it must not.
        adapter._get_http_session = AsyncMock()  # type: ignore[method-assign]

        activity = _file_download_activity(
            {
                "contentType": _FILE_DOWNLOAD_INFO,
                "name": "evil.bin",
                "content": {"downloadUrl": "https://attacker.example.com/exfil.bin"},
            }
        )
        att = adapter.parse_message(activity).attachments[0]
        assert att.url == "https://attacker.example.com/exfil.bin"
        assert att.fetch_data is not None
        with pytest.raises(ValidationError):
            await att.fetch_data()
        adapter._get_http_session.assert_not_awaited()


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

        # microsoft-teams-apps 2.0.14+ renamed the SDK's ``skip_auth`` flag to
        # ``dangerously_allow_unauthenticated_requests``; force whichever flag this
        # version has and forward everything else untouched (the SDK calls
        # ``initialize`` with keywords only).
        skip_flag = (
            "dangerously_allow_unauthenticated_requests"
            if "dangerously_allow_unauthenticated_requests" in inspect.signature(real_initialize).parameters
            else "skip_auth"
        )

        def _initialize_skip_auth(self, *args, **kwargs):
            kwargs.pop("skip_auth", None)
            kwargs.pop("dangerously_allow_unauthenticated_requests", None)
            return real_initialize(self, *args, **{**kwargs, skip_flag: True})

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


@functools.cache
def _test_rsa_key():
    """One RSA key per test run for signing test JWTs (generation is slow)."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class TestInboundTokenIssuer:
    """Only Bot Framework-issued inbound tokens reach the handlers (#250).

    ``microsoft-teams-apps`` 2.1 also accepts Entra ID ("Agent ID") tokens
    for our app id from any tenant, and picks that branch (with a per-tenant
    JWKS fetch) from the token's unverified issuer. These tests run the real
    bridge -> SDK ``HttpServer`` -> SDK validator -> ``_dispatch_activity`` path
    with RS256 tokens signed by a test key. Only JWKS key *resolution* is
    stubbed (``PyJWKClient.get_signing_key_from_jwt`` returns the test public
    key and records which JWKS URI was asked), so the SDK's own signature,
    issuer, audience, expiry and ``serviceurl`` checks all run.
    """

    TENANT = "11111111-2222-3333-4444-555555555555"
    SERVICE_URL = "https://smba.trafficmanager.net/teams/"
    ACTIVITY = {
        "type": "message",
        "id": "msg-1",
        "text": "hello",
        "from": {"id": "user-1", "name": "Alice"},
        "recipient": {"id": "28:test-app-id", "name": "bot"},
        "conversation": {"id": "19:abc@thread.tacv2", "conversationType": "channel"},
        "channelId": "msteams",
        "serviceUrl": SERVICE_URL,
    }

    @pytest.fixture
    def signing_key(self):
        return _test_rsa_key()

    @pytest.fixture(autouse=True)
    def jwks_uris(self, monkeypatch, signing_key):
        """Resolve every JWKS lookup to the test key; return the URIs asked for."""
        from types import SimpleNamespace

        import jwt

        asked: list[str] = []

        def get_signing_key_from_jwt(self, token):
            asked.append(self.uri)
            return SimpleNamespace(key=signing_key.public_key())

        monkeypatch.setattr(jwt.PyJWKClient, "get_signing_key_from_jwt", get_signing_key_from_jwt)
        monkeypatch.delenv("CLOUD", raising=False)
        monkeypatch.delenv("DANGEROUSLY_ALLOW_UNAUTHENTICATED_REQUESTS", raising=False)
        return asked

    def _token(self, signing_key, issuer: str, **overrides) -> str:
        import time

        import jwt

        now = int(time.time())
        claims = {
            "iss": issuer,
            "aud": "test-app-id",
            "tid": self.TENANT,
            "serviceurl": self.SERVICE_URL,
            "iat": now,
            "nbf": now,
            "exp": now + 600,
            **overrides,
        }
        return jwt.encode(claims, signing_key, algorithm="RS256", headers={"kid": "test-kid"})

    async def _post(self, token: str):
        import json

        logger = _make_logger()
        adapter = _make_adapter(logger=logger)
        chat = MagicMock()
        chat.get_state = MagicMock(return_value=MagicMock(set=AsyncMock(), get=AsyncMock(return_value=None)))
        chat.process_message = MagicMock()
        await adapter.initialize(chat)
        request = _FakeRequest(
            json.dumps(self.ACTIVITY),
            {"content-type": "application/json", "authorization": f"Bearer {token}"},
        )
        response = await adapter.handle_webhook(request)
        return response, chat, logger

    @staticmethod
    def _issuer_warnings(logger):
        return [c.args[1] for c in logger.warn.call_args_list if "not issued by the Bot Framework" in c.args[0]]

    @pytest.mark.asyncio
    async def test_entra_issued_token_is_rejected_before_any_jwks_fetch(self, signing_key, jwks_uris):
        # A correctly signed Entra token for our app id: 2.1's validator would
        # accept it on its Entra branch after fetching the tenant's JWKS.
        issuer = f"https://login.microsoftonline.com/{self.TENANT}/v2.0"
        response, chat, logger = await self._post(self._token(signing_key, issuer))
        assert response["status"] == 401
        chat.process_message.assert_not_called()
        assert jwks_uris == []
        assert self._issuer_warnings(logger) == [{"issuer": issuer, "expectedIssuer": "https://api.botframework.com"}]

    @pytest.mark.asyncio
    async def test_attacker_chosen_tenant_jwks_is_never_fetched(self, jwks_uris):
        # Unsigned tokens naming arbitrary tenants must not make the bot fetch
        # those tenants' JWKS (v2 and v1 Entra issuer shapes).
        import jwt

        for issuer in (
            "https://login.microsoftonline.com/attacker-tenant/v2.0",
            "https://sts.windows.net/attacker-tenant/",
        ):
            token = jwt.encode({"iss": issuer, "aud": "test-app-id", "tid": "attacker-tenant"}, "k" * 40)
            response, _chat, _logger = await self._post(token)
            assert response["status"] == 401
        assert jwks_uris == []

    @pytest.mark.asyncio
    async def test_bot_framework_issued_token_is_dispatched(self, signing_key, jwks_uris):
        response, chat, logger = await self._post(self._token(signing_key, "https://api.botframework.com"))
        assert response["status"] == 200
        chat.process_message.assert_called_once()
        assert jwks_uris == ["https://login.botframework.com/v1/.well-known/keys"]
        assert self._issuer_warnings(logger) == []

    @pytest.mark.asyncio
    async def test_bot_framework_token_still_gets_sdk_validation(self, signing_key):
        # The pre-check only narrows: a Bot Framework-issued token for another
        # app id (audience) or another service URL is still refused by the SDK.
        for overrides in ({"aud": "someone-elses-app"}, {"serviceurl": "https://smba.trafficmanager.net/other/"}):
            token = self._token(signing_key, "https://api.botframework.com", **overrides)
            response, chat, _logger = await self._post(token)
            assert response["status"] == 401, overrides
            chat.process_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_sovereign_cloud_uses_its_own_issuer(self, monkeypatch, signing_key):
        # App.cloud comes from the CLOUD env var; the issuer rule must follow it.
        monkeypatch.setenv("CLOUD", "USGov")
        response, chat, _logger = await self._post(self._token(signing_key, "https://api.botframework.us"))
        assert response["status"] == 200
        chat.process_message.assert_called_once()

        response, chat, logger = await self._post(self._token(signing_key, "https://api.botframework.com"))
        assert response["status"] == 401
        chat.process_message.assert_not_called()
        assert self._issuer_warnings(logger) == [
            {"issuer": "https://api.botframework.com", "expectedIssuer": "https://api.botframework.us"}
        ]

    @pytest.mark.asyncio
    async def test_dispatch_rejects_validated_non_bot_framework_token(self, signing_key):
        # Defence in depth: even if a validated Entra token reached the SDK's
        # on_request callback, _dispatch_activity refuses it before routing.
        from types import SimpleNamespace

        from microsoft_teams.api import JsonWebToken

        logger = _make_logger()
        adapter = _make_adapter(logger=logger)
        adapter._handle_message_activity = AsyncMock()
        issuer = f"https://login.microsoftonline.com/{self.TENANT}/v2.0"
        event = SimpleNamespace(body=MagicMock(), token=JsonWebToken(value=self._token(signing_key, issuer)))
        response = await adapter._dispatch_activity(event)
        assert response == {"status": 401, "body": {"error": "Unauthorized"}}
        adapter._handle_message_activity.assert_not_awaited()
        assert self._issuer_warnings(logger) == [{"issuer": issuer, "expectedIssuer": "https://api.botframework.com"}]

    @pytest.mark.asyncio
    async def test_unauthenticated_mode_ignores_the_header(self, monkeypatch, jwks_uris):
        # In the SDK's dangerously_allow_unauthenticated_requests mode the SDK
        # ignores Authorization entirely; the pre-check must not add auth back.
        import jwt

        logger = _make_logger()
        adapter = _make_adapter(logger=logger)
        adapter._app.options.dangerously_allow_unauthenticated_requests = True
        token = jwt.encode({"iss": "https://login.microsoftonline.com/t/v2.0"}, "k" * 40)
        assert adapter._rejects_before_auth({"authorization": f"Bearer {token}"}) is False
        adapter._app.options.dangerously_allow_unauthenticated_requests = False
        assert adapter._rejects_before_auth({"authorization": f"Bearer {token}"}) is True
        assert jwks_uris == []


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


class TestSdkDependencyDeclarations:
    def test_every_imported_sdk_package_is_declared_with_an_upper_bound(self):
        # uv.lock is not committed, so an SDK package that only arrives
        # transitively (microsoft-teams-apps leaves -common unbounded) can
        # drift to a new minor on a fresh install (#250/#251). Every
        # microsoft_teams.<pkg> the adapter imports must be declared, capped,
        # in each dependency list that installs the Teams SDK.
        import pathlib
        import tomllib

        root = pathlib.Path(__file__).resolve().parent.parent
        source = "\n".join(p.read_text() for p in (root / "src/chat_sdk/adapters/teams").glob("*.py"))
        imported = set(re.findall(r"\bmicrosoft_teams\.(\w+)", source))
        assert {"api", "apps", "common"} <= imported

        project = tomllib.loads((root / "pyproject.toml").read_text())
        lists = {
            "[teams]": project["project"]["optional-dependencies"]["teams"],
            "[all]": project["project"]["optional-dependencies"]["all"],
            "dev": project["dependency-groups"]["dev"],
        }
        for list_name, deps in lists.items():
            specs = {d.split(">")[0].split("<")[0].split("=")[0]: d for d in deps}
            for pkg in sorted(imported):
                dist = f"microsoft-teams-{pkg}"
                assert dist in specs, f"{dist} missing from {list_name}"
                assert "<" in specs[dist], f"{dist} has no upper bound in {list_name}"


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
        # delegates to the SDK sender with a MessageActivityInput carrying the
        # rendered text + markdown format and a reference to the conversation
        activity, ref = send.call_args.args
        assert ref.conversation.id == "19:abc@thread.tacv2"
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
    """Outbound ops address the thread's encoded service URL through a client
    for that URL (``_api_for`` / ``_send_to``) and never retarget the shared
    ``App.api`` — so concurrent sends to different regions or sovereign clouds
    cannot race. Edit/delete run on the REAL ``ApiClient`` with only the HTTP
    verb stubbed, so the SDK's own URL building stays in the test.
    """

    SOVEREIGN_URL = "https://smba.infra.gov.teams.microsoft.us/teams/"
    DEFAULT_URL = "https://smba.trafficmanager.net/teams"

    def _thread(self, adapter: TeamsAdapter, service_url: str | None = None) -> str:
        return adapter.encode_thread_id(
            TeamsThreadId(conversation_id="19:abc@thread.tacv2", service_url=service_url or self.SOVEREIGN_URL)
        )

    @staticmethod
    def _capture_wire(monkeypatch: pytest.MonkeyPatch, method: str) -> list[str]:
        """Stub the SDK HTTP client's ``method`` (class-wide) and record each URL.

        Class-wide so it also covers the per-URL client ``_api_for`` builds
        (2.1 clones share the connection but not the ``Client`` instance).
        2.1 adds a ``_metadata=`` kwarg (#250), absorbed by ``**_kwargs``.
        """
        from microsoft_teams.common.http.client import Client

        urls: list[str] = []

        class _Response:
            def json(self) -> dict[str, str]:
                return {"id": "edit-1"}

        async def fake_http(_self, url, **_kwargs):
            urls.append(url)
            return _Response()

        monkeypatch.setattr(Client, method, fake_http)
        return urls

    @pytest.mark.asyncio
    async def test_post_message_sends_to_the_thread_url_without_retargeting_app_api(self):
        adapter = _make_adapter(app_id="test-app-id", logger=_make_logger())
        send = _mock_app_send(adapter, "m")

        await adapter.post_message(self._thread(adapter), {"markdown": "hi"})

        _activity, ref = send.call_args.args
        # the trailing slash is normalized off, matching ApiClient's own rstrip
        assert ref.service_url == self.SOVEREIGN_URL.rstrip("/")
        assert adapter._app.api.service_url == self.DEFAULT_URL

    @pytest.mark.asyncio
    async def test_edit_message_uses_a_client_for_the_thread_url(self, monkeypatch: pytest.MonkeyPatch):
        adapter = _make_adapter(app_id="test-app-id", logger=_make_logger())
        urls = self._capture_wire(monkeypatch, "put")

        result = await adapter.edit_message(self._thread(adapter), "edit-1", {"markdown": "x"})

        assert urls == [f"{self.SOVEREIGN_URL.rstrip('/')}/v3/conversations/19:abc@thread.tacv2/activities/edit-1"]
        assert result.id == "edit-1"
        assert adapter._app.api.service_url == self.DEFAULT_URL

    @pytest.mark.asyncio
    async def test_delete_message_uses_a_client_for_the_thread_url(self, monkeypatch: pytest.MonkeyPatch):
        adapter = _make_adapter(app_id="test-app-id", logger=_make_logger())
        urls = self._capture_wire(monkeypatch, "delete")

        await adapter.delete_message(self._thread(adapter), "gone-1")

        assert urls == [f"{self.SOVEREIGN_URL.rstrip('/')}/v3/conversations/19:abc@thread.tacv2/activities/gone-1"]
        assert adapter._app.api.service_url == self.DEFAULT_URL

    @pytest.mark.asyncio
    async def test_concurrent_posts_to_different_service_urls_each_hit_their_own_url(self):
        """Python-specific: two sends interleaved on the event loop. The old
        ``_point_app_api_at`` mutated ``App.api`` before awaiting, so the
        second call could redirect the first."""
        adapter = _make_adapter(app_id="test-app-id", logger=_make_logger())
        seen: dict[str, list[str]] = {}

        async def fake_send(activity, ref):
            seen.setdefault(activity.text, []).append(ref.service_url)
            await asyncio.sleep(0)  # yield so the other send runs mid-flight
            seen[activity.text].append(ref.service_url)
            return _SentActivity(activity.text)

        adapter._app.activity_sender = SimpleNamespace(send=AsyncMock(side_effect=fake_send))
        emea = "https://smba.trafficmanager.net/emea/"

        results = await asyncio.gather(
            adapter.post_message(self._thread(adapter, emea), "to-emea"),
            adapter.post_message(self._thread(adapter), "to-gov"),
        )

        assert [r.id for r in results] == ["to-emea", "to-gov"]
        assert seen == {
            "to-emea": [emea.rstrip("/")] * 2,
            "to-gov": [self.SOVEREIGN_URL.rstrip("/")] * 2,
        }
        assert adapter._app.api.service_url == self.DEFAULT_URL

    @staticmethod
    def _card() -> Any:
        from chat_sdk.cards import Card
        from chat_sdk.types import PostableCard

        return PostableCard(card=Card(title="Results"))

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "operation",
        [
            "post_message_text",
            "post_message_card",
            "post_channel_message_text",
            "post_channel_message_card",
            "edit_message",
            "delete_message",
            "start_typing",
        ],
    )
    async def test_every_outbound_op_rejects_a_disallowed_thread_url_before_sending(
        self, operation: str, monkeypatch: pytest.MonkeyPatch
    ):
        """The SSRF allow-list check runs at each outbound call site (the
        per-URL client carries the bot token), so each site is pinned here:
        a thread ID naming an attacker host never reaches the SDK sender, the
        HTTP client, or the per-URL client cache."""
        from microsoft_teams.common.http.client import Client

        from chat_sdk.shared.errors import NetworkError

        adapter = _make_adapter(app_id="test-app-id", logger=_make_logger())
        send = _mock_app_send(adapter)
        http_calls: list[str] = []

        async def fake_http(_self, url, **_kwargs):
            http_calls.append(url)
            raise AssertionError(f"unexpected HTTP call to {url}")

        for verb in ("post", "put", "delete"):
            monkeypatch.setattr(Client, verb, fake_http)

        thread_id = self._thread(adapter, "https://evil.example.com/")
        calls = {
            "post_message_text": lambda: adapter.post_message(thread_id, "hi"),
            "post_message_card": lambda: adapter.post_message(thread_id, self._card()),
            "post_channel_message_text": lambda: adapter.post_channel_message(thread_id, "hi"),
            "post_channel_message_card": lambda: adapter.post_channel_message(thread_id, self._card()),
            "edit_message": lambda: adapter.edit_message(thread_id, "m-1", "hi"),
            "delete_message": lambda: adapter.delete_message(thread_id, "m-1"),
            "start_typing": lambda: adapter.start_typing(thread_id),
        }

        if operation == "start_typing":
            # typing failures are logged, never raised (upstream parity)
            await calls[operation]()
            assert "not an allowed Bot Framework endpoint" in str(adapter._logger.error.call_args)
        else:
            with pytest.raises(NetworkError, match="not an allowed Bot Framework endpoint"):
                await calls[operation]()
        send.assert_not_called()
        assert http_calls == []
        assert adapter._api_clients == {}


class TestTeamsAppRouting:
    """Port of upstream ``app.test.ts`` (``TeamsApp.apiFor`` / ``sendTo``,
    chat@4.41.0) against ``TeamsAdapter._api_for`` / ``_send_to``."""

    APP_ID = "11111111-2222-3333-4444-555555555555"
    EMEA = "https://smba.trafficmanager.net/emea/"
    GATEWAY = "https://gateway.example/teams"

    def _adapter(self, **overrides) -> TeamsAdapter:
        return _make_adapter(app_id=self.APP_ID, app_password="secret", logger=_make_logger(), **overrides)

    def test_reuses_the_app_client_for_the_default_service_url(self):
        adapter = self._adapter()
        api = adapter._app.api
        assert adapter._api_for(api.service_url) is api
        assert adapter._api_for(f"{api.service_url}/") is api
        assert adapter._api_for("") is api

    def test_targets_other_service_urls_with_a_dedicated_client(self):
        adapter = self._adapter()
        regional = adapter._api_for(self.EMEA)
        assert regional is not adapter._app.api
        assert regional.service_url == "https://smba.trafficmanager.net/emea"
        # Python divergence (docs/UPSTREAM_SYNC.md): one cached client per
        # normalized URL rather than a new client per call.
        assert adapter._api_for(self.EMEA.rstrip("/")) is regional

    def test_keeps_every_client_on_the_configured_endpoint(self):
        adapter = self._adapter(api_url=self.GATEWAY)
        assert adapter._app.api.service_url == self.GATEWAY
        assert adapter._api_for(self.EMEA) is adapter._app.api

    def test_teams_api_url_env_pins_every_client(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("TEAMS_API_URL", self.GATEWAY)
        adapter = self._adapter()
        assert adapter._api_for(self.EMEA) is adapter._app.api

    @pytest.mark.asyncio
    async def test_sends_through_the_configured_endpoint_instead_of_the_thread_url(self):
        from microsoft_teams.api import MessageActivityInput

        adapter = self._adapter(api_url=self.GATEWAY)
        send = _mock_app_send(adapter, "sent")

        await adapter._send_to(
            TeamsThreadId(conversation_id="19:abc@thread.tacv2", service_url=self.EMEA),
            MessageActivityInput(text="hello"),
        )

        assert send.call_args.args[1].service_url == self.GATEWAY

    @pytest.mark.asyncio
    async def test_sends_through_the_sdk_with_the_threads_service_url_and_conversation(self):
        from microsoft_teams.api import MessageActivityInput

        adapter = self._adapter()
        send = _mock_app_send(adapter, "sent")

        await adapter._send_to(
            TeamsThreadId(
                conversation_id="19:abc@thread.tacv2",
                service_url=self.EMEA,
                conversation_type="channel",
            ),
            MessageActivityInput(text="hello"),
        )

        send.assert_called_once()
        activity, ref = send.call_args.args
        assert (activity.type, activity.text) == ("message", "hello")
        assert ref.model_dump(by_alias=True, exclude_none=True) == {
            "channelId": "msteams",
            "serviceUrl": "https://smba.trafficmanager.net/emea",
            "bot": {"id": self.APP_ID},
            "conversation": {"id": "19:abc@thread.tacv2", "conversationType": "channel"},
        }

    @pytest.mark.asyncio
    async def test_falls_back_to_the_default_service_url_when_the_thread_has_none(self):
        from microsoft_teams.api import MessageActivityInput

        adapter = self._adapter()
        send = _mock_app_send(adapter, "sent")

        await adapter._send_to(TeamsThreadId(conversation_id="a:dm", service_url=""), MessageActivityInput(text="hi"))

        ref = send.call_args.args[1]
        assert ref.service_url == adapter._app.api.service_url
        # conversation_type is put on the wire only when the thread ID knows it
        assert ref.conversation.model_dump(by_alias=True, exclude_none=True) == {"id": "a:dm"}

    @pytest.mark.asyncio
    async def test_rejects_sends_without_credentials(self, monkeypatch: pytest.MonkeyPatch):
        from microsoft_teams.api import MessageActivityInput

        for name in ("TEAMS_APP_ID", "CLIENT_ID"):
            monkeypatch.delenv(name, raising=False)
        adapter = _make_adapter(app_id="", logger=_make_logger())
        send = _mock_app_send(adapter)

        with pytest.raises(ValueError, match="credentials"):
            await adapter._send_to(
                TeamsThreadId(conversation_id="a:dm", service_url=""), MessageActivityInput(text="hello")
            )
        send.assert_not_called()

    @pytest.mark.asyncio
    async def test_sdk_without_activity_sender_sends_on_the_thread_urls_client(self, monkeypatch: pytest.MonkeyPatch):
        """``microsoft-teams-apps`` 2.1+ removed ``ActivitySender``: the send goes
        through ``send_or_update_activity`` with the ``_api_for`` client."""
        activity_send = pytest.importorskip("microsoft_teams.apps.activity_send")
        from microsoft_teams.api import MessageActivityInput

        adapter = self._adapter()
        if hasattr(adapter._app, "activity_sender"):
            del adapter._app.activity_sender
        calls: list[tuple[Any, Any, Any]] = []

        async def fake_send_or_update(api, activity, ref):
            calls.append((api, activity, ref))
            return _SentActivity("sent-21")

        monkeypatch.setattr(activity_send, "send_or_update_activity", fake_send_or_update)
        activity = MessageActivityInput(text="hello")

        sent = await adapter._send_to(TeamsThreadId(conversation_id="a:dm", service_url=self.EMEA), activity)

        assert sent.id == "sent-21"
        [(api, sent_activity, ref)] = calls
        assert api is adapter._api_for(self.EMEA)
        assert sent_activity is activity
        assert ref.service_url == "https://smba.trafficmanager.net/emea"

    @pytest.mark.asyncio
    async def test_real_sdk_puts_the_thread_conversation_on_the_wire(self, monkeypatch: pytest.MonkeyPatch):
        """Unstubbed SDK send path on whichever SDK line is installed; only the
        HTTP ``post`` is replaced."""
        from microsoft_teams.common.http.client import Client

        posts: list[tuple[str, dict[str, Any]]] = []

        class _Response:
            def json(self) -> dict[str, str]:
                return {"id": "wire-1"}

        async def fake_post(_self, url, *, json=None, **_kwargs):
            posts.append((url, json))
            return _Response()

        monkeypatch.setattr(Client, "post", fake_post)
        adapter = self._adapter()
        tid = adapter.encode_thread_id(
            TeamsThreadId(conversation_id="a:group-chat", service_url=self.EMEA, conversation_type="groupChat")
        )

        result = await adapter.post_message(tid, "hello")

        assert result.id == "wire-1"
        [(url, body)] = posts
        assert url == "https://smba.trafficmanager.net/emea/v3/conversations/a:group-chat/activities"
        assert body["conversation"] == {"id": "a:group-chat", "conversationType": "groupChat"}
        assert body["from"]["id"] == self.APP_ID
        assert body["text"] == "hello"

    def test_client_cache_follows_the_app_api_it_was_built_from(self):
        adapter = self._adapter()
        first = adapter._api_for(self.EMEA)
        replacement = MagicMock()
        replacement.service_url = "https://smba.trafficmanager.net/teams"
        scoped = object()
        replacement.from_service_url = MagicMock(return_value=scoped)
        adapter._app.api = replacement

        assert adapter._api_for(self.EMEA) is scoped
        assert scoped is not first
        replacement.from_service_url.assert_called_once_with("https://smba.trafficmanager.net/emea")

    def test_client_cache_is_bounded(self):
        from chat_sdk.adapters.teams.adapter import _MAX_CACHED_API_CLIENTS

        adapter = self._adapter()
        urls = [f"https://region{i}.botframework.com" for i in range(_MAX_CACHED_API_CLIENTS + 1)]
        first = adapter._api_for(urls[0])
        for url in urls[1:]:
            adapter._api_for(url)

        assert len(adapter._api_clients) == _MAX_CACHED_API_CLIENTS
        assert urls[0] not in adapter._api_clients
        assert adapter._api_for(urls[0]) is not first


class TestConversationTypeRouting:
    """Port of upstream ``index.test.ts`` › "Teams conversation type routing"
    (chat@4.36.0 / chat@4.40.0)."""

    SERVICE_URL = "https://smba.trafficmanager.net/teams/"

    def test_keeps_the_legacy_id_when_the_conversation_type_agrees_with_its_prefix(self):
        adapter = _make_adapter()
        personal = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="a:personal-conversation", service_url=self.SERVICE_URL, conversation_type="personal"
            )
        )
        legacy_personal = adapter.encode_thread_id(
            TeamsThreadId(conversation_id="a:personal-conversation", service_url=self.SERVICE_URL)
        )
        channel = adapter.encode_thread_id(
            TeamsThreadId(
                conversation_id="19:channel@thread.tacv2", service_url=self.SERVICE_URL, conversation_type="channel"
            )
        )
        legacy_channel = adapter.encode_thread_id(
            TeamsThreadId(conversation_id="19:channel@thread.tacv2", service_url=self.SERVICE_URL)
        )

        assert personal == legacy_personal
        assert channel == legacy_channel
        assert personal.count(":") == 2

    def _message(self, adapter: TeamsAdapter, conversation: dict[str, Any], **extra: Any):
        return adapter.parse_message(
            {
                "conversation": conversation,
                "from": {"id": "user-1", "name": "Alice"},
                "id": "message-1",
                "serviceUrl": self.SERVICE_URL,
                "text": "hello",
                "type": "message",
                **extra,
            }
        )

    def test_falls_back_to_is_group_when_conversation_type_is_missing(self):
        adapter = _make_adapter()
        group = self._message(adapter, {"id": "a:group-chat-id", "isGroup": True})
        channel = self._message(
            adapter, {"id": "a:channel-id", "isGroup": True}, channelData={"team": {"id": "team-id"}}
        )
        personal = self._message(adapter, {"id": "19:personal-id", "isGroup": False})

        assert adapter.decode_thread_id(group.thread_id).conversation_type == "groupChat"
        assert adapter.is_dm(group.thread_id) is False
        assert adapter.decode_thread_id(channel.thread_id).conversation_type == "channel"
        assert adapter.is_dm(channel.thread_id) is False
        assert adapter.decode_thread_id(personal.thread_id).conversation_type == "personal"
        assert adapter.is_dm(personal.thread_id) is True

    def test_prefers_an_explicit_conversation_type_over_is_group(self):
        adapter = _make_adapter()
        message = self._message(adapter, {"conversationType": "personal", "id": "19:personal-id", "isGroup": True})
        assert adapter.is_dm(message.thread_id) is True

    def test_is_group_is_checked_by_identity_not_truthiness(self):
        adapter = _make_adapter()
        # A non-bool isGroup is "missing": the 19:/a: heuristic decides and
        # the thread ID keeps its legacy three-segment form.
        message = self._message(adapter, {"id": "a:conversation", "isGroup": "false"})
        assert adapter.decode_thread_id(message.thread_id).conversation_type is None
        assert message.thread_id.count(":") == 2
        assert adapter.is_dm(message.thread_id) is True
        # A falsy non-bool is not ``False``: no ``personal`` override.
        channel = self._message(adapter, {"id": "19:channel@thread.tacv2", "isGroup": 0})
        assert adapter.decode_thread_id(channel.thread_id).conversation_type is None
        assert adapter.is_dm(channel.thread_id) is False

    def test_explicit_group_chat_id_round_trips_with_a_fourth_segment(self):
        adapter = _make_adapter()
        thread_id = adapter.encode_thread_id(
            TeamsThreadId(conversation_id="a:group", service_url=self.SERVICE_URL, conversation_type="groupChat")
        )
        assert thread_id.endswith(":groupChat")
        assert adapter.decode_thread_id(thread_id) == TeamsThreadId(
            conversation_id="a:group", service_url=self.SERVICE_URL, conversation_type="groupChat"
        )
        # channel IDs keep the type, so a channel-level post stays a group chat
        assert adapter.channel_id_from_thread_id(thread_id) == thread_id

    def test_encode_rejects_an_unknown_conversation_type(self):
        adapter = _make_adapter()
        with pytest.raises(ValidationError, match="conversation type"):
            adapter.encode_thread_id(
                TeamsThreadId(conversation_id="a:x", service_url=self.SERVICE_URL, conversation_type="bogus")  # type: ignore[arg-type]
            )

    @pytest.mark.asyncio
    async def test_a_group_chat_is_not_routed_as_a_dm(self):
        """An ``a:`` group chat goes to ``process_message`` without a native
        streamer (Teams streams natively only in 1:1 chats)."""
        adapter = _make_adapter()
        adapter._create_streamer = MagicMock(side_effect=AssertionError("group chats must not stream natively"))  # type: ignore[method-assign]
        chat = MagicMock()
        chat.process_message = MagicMock(return_value=None)
        adapter._chat = chat

        await adapter._handle_message_activity(
            {
                "type": "message",
                "id": "m-1",
                "text": "hi",
                "from": {"id": "user-1", "name": "Alice"},
                "conversation": {"id": "a:group-chat", "conversationType": "groupChat"},
                "serviceUrl": self.SERVICE_URL,
            }
        )

        chat.process_message.assert_called_once()
        thread_id = chat.process_message.call_args.args[1]
        assert adapter.is_dm(thread_id) is False
        assert adapter._active_streams == {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("handler", ["message_action", "card_invoke", "reaction"])
    async def test_actions_and_reactions_in_a_group_chat_get_the_typed_thread_id(self, handler: str):
        """Button clicks and reactions in an ``a:`` group chat share the
        message's ``:groupChat`` thread ID (and so its subscription/state
        keys), not the untyped DM-looking three-segment form."""
        adapter = _make_adapter()
        chat = MagicMock()
        adapter._chat = chat
        activity: dict[str, Any] = {
            "id": "m-1",
            "replyToId": "m-0",
            "from": {"id": "user-1", "name": "Alice"},
            "conversation": {"id": "a:group-chat", "conversationType": "groupChat"},
            "serviceUrl": self.SERVICE_URL,
        }
        expected = adapter.encode_thread_id(
            TeamsThreadId(conversation_id="a:group-chat", service_url=self.SERVICE_URL, conversation_type="groupChat")
        )

        if handler == "message_action":
            adapter._handle_message_action({**activity, "type": "message"}, {"actionId": "approve"})
            event = chat.process_action.call_args.args[0]
        elif handler == "card_invoke":
            await adapter._handle_adaptive_card_action({**activity, "type": "invoke"}, {"actionId": "approve"})
            event = chat.process_action.call_args.args[0]
        else:
            adapter._handle_reaction_activity(
                {**activity, "type": "messageReaction", "reactionsAdded": [{"type": "like"}]}
            )
            event = chat.process_reaction.call_args.args[0]

        assert event.thread_id == expected
        assert event.thread_id.endswith(":groupChat")
        assert adapter.is_dm(event.thread_id) is False


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
        SDK sender, back to the camelCase wire dicts — proving the file
        attachments actually reached the SDK boundary (not just the raw echo)."""
        activity = send.call_args.args[0]
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
        activity, ref = send.call_args.args
        assert ref.conversation.id == "19:abc@thread.tacv2"
        # delegates a TypingActivityInput (type == "typing") to the SDK sender
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
