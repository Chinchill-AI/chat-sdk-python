"""Tests for the Google Chat adapter."""

from __future__ import annotations

import os

import pytest

from chat_sdk.adapters.google_chat.adapter import GoogleChatAdapter
from chat_sdk.adapters.google_chat.thread_utils import (
    GoogleChatMessageName,
    GoogleChatThreadId,
    decode_thread_id,
    encode_thread_id,
    is_dm_thread,
    parse_message_name,
)
from chat_sdk.adapters.google_chat.types import (
    GoogleChatAdapterConfig,
    ServiceAccountCredentials,
)
from chat_sdk.shared.errors import AdapterRateLimitError, ValidationError
from chat_sdk.types import Attachment

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_credentials() -> ServiceAccountCredentials:
    return ServiceAccountCredentials(
        client_email="bot@project.iam.gserviceaccount.com",
        private_key="-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----",
        project_id="test-project",
    )


def _make_adapter(**overrides) -> GoogleChatAdapter:
    """Create a GoogleChatAdapter with minimal valid config."""
    # The adapter now fails closed unless a verification gating field is set
    # (google_chat_project_number, pubsub_audience, or the explicit opt-out).
    # These tests exercise non-verification mechanics, so default to the
    # explicit opt-out; individual tests override it via kwargs as needed.
    overrides.setdefault("disable_signature_verification", True)
    config = GoogleChatAdapterConfig(
        credentials=overrides.pop("credentials", _make_credentials()),
        **overrides,
    )
    return GoogleChatAdapter(config)


# ---------------------------------------------------------------------------
# Thread ID encode / decode
# ---------------------------------------------------------------------------


class TestGoogleChatThreadId:
    """Thread ID encoding and decoding."""

    def test_encode_space_only(self):
        tid = encode_thread_id(GoogleChatThreadId(space_name="spaces/ABC123"))
        assert tid == "gchat:spaces/ABC123"

    def test_encode_with_thread_name(self):
        tid = encode_thread_id(
            GoogleChatThreadId(
                space_name="spaces/ABC123",
                thread_name="spaces/ABC123/threads/xyz",
            )
        )
        assert tid.startswith("gchat:spaces/ABC123:")
        # Should have base64url encoded thread name
        assert "gchat:spaces/ABC123:" in tid

    def test_encode_dm_thread(self):
        tid = encode_thread_id(GoogleChatThreadId(space_name="spaces/DM123", is_dm=True))
        assert tid.endswith(":dm")
        assert tid.startswith("gchat:spaces/DM123")

    def test_encode_dm_with_thread(self):
        tid = encode_thread_id(
            GoogleChatThreadId(
                space_name="spaces/DM123",
                thread_name="spaces/DM123/threads/t1",
                is_dm=True,
            )
        )
        assert tid.endswith(":dm")
        assert "gchat:spaces/DM123" in tid

    def test_decode_space_only(self):
        decoded = decode_thread_id("gchat:spaces/ABC123")
        assert decoded.space_name == "spaces/ABC123"
        assert decoded.thread_name is None
        assert decoded.is_dm is False

    def test_decode_with_thread(self):
        # First encode, then decode
        original = GoogleChatThreadId(
            space_name="spaces/ABC123",
            thread_name="spaces/ABC123/threads/xyz",
        )
        encoded = encode_thread_id(original)
        decoded = decode_thread_id(encoded)
        assert decoded.space_name == "spaces/ABC123"
        assert decoded.thread_name == "spaces/ABC123/threads/xyz"

    def test_decode_dm(self):
        decoded = decode_thread_id("gchat:spaces/DM123:dm")
        assert decoded.space_name == "spaces/DM123"
        assert decoded.is_dm is True

    def test_roundtrip_simple(self):
        original = GoogleChatThreadId(space_name="spaces/test")
        encoded = encode_thread_id(original)
        decoded = decode_thread_id(encoded)
        assert decoded.space_name == original.space_name

    def test_roundtrip_with_thread(self):
        original = GoogleChatThreadId(
            space_name="spaces/room1",
            thread_name="spaces/room1/threads/thread42",
        )
        encoded = encode_thread_id(original)
        decoded = decode_thread_id(encoded)
        assert decoded.space_name == original.space_name
        assert decoded.thread_name == original.thread_name

    def test_roundtrip_dm_with_thread(self):
        original = GoogleChatThreadId(
            space_name="spaces/dm99",
            thread_name="spaces/dm99/threads/t1",
            is_dm=True,
        )
        encoded = encode_thread_id(original)
        decoded = decode_thread_id(encoded)
        assert decoded.space_name == original.space_name
        assert decoded.thread_name == original.thread_name
        assert decoded.is_dm is True

    def test_decode_invalid_prefix(self):
        with pytest.raises(ValidationError):
            decode_thread_id("slack:C123:ts")

    def test_decode_missing_prefix(self):
        with pytest.raises(ValidationError):
            decode_thread_id("spaces/ABC123")


# ---------------------------------------------------------------------------
# is_dm_thread
# ---------------------------------------------------------------------------


class TestIsDmThread:
    """Tests for is_dm_thread."""

    def test_dm_thread(self):
        assert is_dm_thread("gchat:spaces/DM123:dm") is True

    def test_non_dm_thread(self):
        assert is_dm_thread("gchat:spaces/ROOM123") is False

    def test_thread_with_encoded_data(self):
        tid = encode_thread_id(
            GoogleChatThreadId(
                space_name="spaces/room",
                thread_name="spaces/room/threads/t",
            )
        )
        assert is_dm_thread(tid) is False


# ---------------------------------------------------------------------------
# parse_message_name (upstream thread-utils.test.ts, d6343460)
# ---------------------------------------------------------------------------


class TestParseMessageName:
    def test_parses_a_server_assigned_message_name(self):
        assert parse_message_name("spaces/AAQAJ9CXYcg/messages/FGEOaAwNIcs.FGEOaAwNIcs") == GoogleChatMessageName(
            space_name="spaces/AAQAJ9CXYcg",
            message_id="FGEOaAwNIcs.FGEOaAwNIcs",
        )

    def test_parses_a_client_assigned_message_name(self):
        space_name, message_id = parse_message_name("spaces/ABC_1-2/messages/client-my_id-3")
        assert space_name == "spaces/ABC_1-2"
        assert message_id == "client-my_id-3"

    @pytest.mark.parametrize(
        "name",
        [
            "spaces/ABC/messages/../../OTHER/messages/x",
            "spaces/ABC/messages/./x",
            "spaces/ABC/messages/x/",
            "spaces/ABC/messages/",
            "spaces/ABC/messages/x?y=1",
            "spaces/ABC/messages/x#y",
            "spaces/ABC/messages/%2e%2e",
            "spaces/ABC/messages/a..b",
            "spaces//messages/x",
            "spaces/ABC/threads/x",
            "/spaces/ABC/messages/x",
            "https://chat.googleapis.com/v1/spaces/ABC/messages/x",
            "x",
            "",
        ],
    )
    def test_rejects(self, name: str):
        with pytest.raises(ValidationError, match="Invalid Google Chat message id"):
            parse_message_name(name)

    # Python-only sweep: ``re.match`` + ``$`` would accept a trailing newline,
    # and ``\w`` / ``\d`` would accept non-ASCII letters and digits.
    @pytest.mark.parametrize(
        "name",
        [
            "spaces/ABC/messages/x\n",
            "spaces/ABC/messages/x\r\n",
            "spaces/ABC/messages/\uff58",
            "spaces/ABC/messages/x\u0661",
            "spaces/ABC\n/messages/x",
            "spaces/ABC/messages/.x",
            "spaces/ABC/messages/x.",
        ],
    )
    def test_rejects_non_ascii_and_trailing_newlines(self, name: str):
        with pytest.raises(ValidationError, match="Invalid Google Chat message id"):
            parse_message_name(name)

    def test_rejects_a_non_string_id(self):
        with pytest.raises(ValidationError, match="Invalid Google Chat message id"):
            parse_message_name(None)


# ---------------------------------------------------------------------------
# create_google_chat_adapter factory
# ---------------------------------------------------------------------------


class TestCreateGoogleChatAdapter:
    """Tests for GoogleChatAdapter construction."""

    def test_with_credentials(self):
        adapter = _make_adapter()
        assert adapter.name == "gchat"

    def test_with_adc(self):
        adapter = GoogleChatAdapter(
            GoogleChatAdapterConfig(
                use_application_default_credentials=True,
                disable_signature_verification=True,
            )
        )
        assert adapter.name == "gchat"

    def test_missing_auth(self):
        old_creds = os.environ.pop("GOOGLE_CHAT_CREDENTIALS", None)
        old_adc = os.environ.pop("GOOGLE_CHAT_USE_ADC", None)
        try:
            # Provide a verification gating field so construction reaches the
            # auth check rather than failing closed on verification first.
            with pytest.raises(ValidationError, match="Authentication"):
                GoogleChatAdapter(GoogleChatAdapterConfig(disable_signature_verification=True))
        finally:
            if old_creds is not None:
                os.environ["GOOGLE_CHAT_CREDENTIALS"] = old_creds
            if old_adc is not None:
                os.environ["GOOGLE_CHAT_USE_ADC"] = old_adc

    def test_adapter_properties(self):
        adapter = _make_adapter()
        assert adapter.name == "gchat"
        assert adapter.lock_scope is None
        assert adapter.persist_message_history is None
        assert adapter.bot_user_id is None
        assert adapter.user_name == "bot"

    def test_custom_user_name(self):
        adapter = _make_adapter(user_name="mybot")
        assert adapter.user_name == "mybot"


# ---------------------------------------------------------------------------
# Media download (upstream 32687038)
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    async def read(self) -> bytes:
        return self._body

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeSession:
    """Records ``session.get`` calls made by the media download closure."""

    def __init__(self, status: int = 200, body: bytes = b"\x89PNG") -> None:
        self.status = status
        self.body = body
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.closed = False

    def get(self, url: str, *, headers: dict[str, str]) -> _FakeResponse:
        self.calls.append((url, headers))
        return _FakeResponse(self.status, self.body)


def _media_adapter(status: int = 200, body: bytes = b"\x89PNG") -> tuple[GoogleChatAdapter, _FakeSession]:
    from unittest.mock import AsyncMock

    adapter = _make_adapter()
    session = _FakeSession(status, body)
    adapter._get_access_token = AsyncMock(return_value="test-token")  # type: ignore[method-assign]
    adapter._get_http_session = AsyncMock(return_value=session)  # type: ignore[method-assign]
    return adapter, session


def _attachment_event(attachment: dict) -> dict:
    return {
        "chat": {
            "messagePayload": {
                "space": {"name": "spaces/ABC123", "type": "ROOM"},
                "message": {
                    "name": "spaces/ABC123/messages/msg1",
                    "sender": {"name": "users/100", "displayName": "User", "type": "HUMAN"},
                    "text": "file",
                    "createTime": "2024-01-01T00:00:00Z",
                    "attachment": [attachment],
                },
            },
        },
    }


class TestMediaDownload:
    """Attachment bytes come only from the media API (upstream 32687038)."""

    @pytest.mark.asyncio
    async def test_uses_media_download_api_when_attachment_data_ref_is_present(self):
        adapter, session = _media_adapter()
        msg = adapter.parse_message(
            _attachment_event(
                {
                    "name": "att1",
                    "contentName": "photo.png",
                    "contentType": "image/png",
                    "downloadUri": "https://example.com/photo.png",
                    "attachmentDataRef": {"resourceName": "spaces/ABC123/attachments/att1"},
                }
            )
        )
        att = msg.attachments[0]
        assert att.fetch_data is not None
        assert att.url == "https://example.com/photo.png"

        assert await att.fetch_data() == b"\x89PNG"
        assert session.calls == [
            (
                "https://chat.googleapis.com/v1/media/spaces/ABC123/attachments/att1?alt=media",
                {"Authorization": "Bearer test-token"},
            )
        ]

    @pytest.mark.asyncio
    async def test_provides_fetch_data_when_only_attachment_data_ref_is_present(self):
        adapter, session = _media_adapter()
        msg = adapter.parse_message(
            _attachment_event(
                {
                    "name": "att1",
                    "contentName": "photo.png",
                    "contentType": "image/png",
                    "attachmentDataRef": {"resourceName": "spaces/ABC123/attachments/att1"},
                }
            )
        )
        att = msg.attachments[0]
        assert att.fetch_data is not None
        assert att.url is None
        assert await att.fetch_data() == b"\x89PNG"
        assert [url for url, _ in session.calls] == [
            "https://chat.googleapis.com/v1/media/spaces/ABC123/attachments/att1?alt=media"
        ]

    @pytest.mark.asyncio
    async def test_does_not_fetch_download_uri_when_media_download_fails(self):
        from chat_sdk.adapters.google_chat.adapter import _GoogleApiError

        adapter, session = _media_adapter(status=403)
        msg = adapter.parse_message(
            _attachment_event(
                {
                    "name": "att1",
                    "contentName": "photo.png",
                    "contentType": "image/png",
                    "downloadUri": "https://example.com/photo.png",
                    "attachmentDataRef": {"resourceName": "spaces/ABC123/attachments/att1"},
                }
            )
        )
        att = msg.attachments[0]
        assert att.fetch_data is not None
        with pytest.raises(_GoogleApiError) as exc_info:
            await att.fetch_data()
        assert exc_info.value.code == 403
        # Exactly one request, to the media API -- never the downloadUri.
        assert [url for url, _ in session.calls] == [
            "https://chat.googleapis.com/v1/media/spaces/ABC123/attachments/att1?alt=media"
        ]

    @pytest.mark.asyncio
    async def test_raises_adapter_rate_limit_error_when_media_download_returns_429(self):
        adapter, _ = _media_adapter(status=429)
        msg = adapter.parse_message(
            _attachment_event(
                {
                    "name": "att1",
                    "contentName": "photo.png",
                    "contentType": "image/png",
                    "attachmentDataRef": {"resourceName": "spaces/ABC123/attachments/att1"},
                }
            )
        )
        att = msg.attachments[0]
        assert att.fetch_data is not None
        with pytest.raises(AdapterRateLimitError):
            await att.fetch_data()

    def test_does_not_provide_fetch_data_when_only_download_uri_is_present(self):
        adapter = _make_adapter()
        msg = adapter.parse_message(
            _attachment_event(
                {
                    "name": "att1",
                    "contentName": "photo.png",
                    "contentType": "image/png",
                    "downloadUri": "https://example.com/photo.png",
                }
            )
        )
        att = msg.attachments[0]
        assert att.fetch_data is None
        assert att.url == "https://example.com/photo.png"
        assert att.fetch_metadata == {"url": "https://example.com/photo.png"}


# ---------------------------------------------------------------------------
# rehydrate_attachment
# ---------------------------------------------------------------------------


class TestRehydrateAttachment:
    """Cover ``GoogleChatAdapter.rehydrate_attachment``."""

    @pytest.mark.asyncio
    async def test_rehydrates_from_resource_name(self):
        adapter, session = _media_adapter()
        attachment = Attachment(
            type="image",
            url="https://example.com/display.png",
            fetch_metadata={"resourceName": "spaces/ABC/messages/X/attachments/Y"},
        )
        rehydrated = adapter.rehydrate_attachment(attachment)
        assert rehydrated.fetch_data is not None
        assert rehydrated.url == "https://example.com/display.png"
        assert rehydrated.fetch_metadata == {
            "resourceName": "spaces/ABC/messages/X/attachments/Y",
        }
        assert await rehydrated.fetch_data() == b"\x89PNG"
        assert [url for url, _ in session.calls] == [
            "https://chat.googleapis.com/v1/media/spaces/ABC/messages/X/attachments/Y?alt=media"
        ]

    def test_does_not_rehydrate_fetch_data_from_a_download_url(self):
        adapter = _make_adapter()
        attachment = Attachment(
            type="file",
            url="https://example.com/document.pdf",
            fetch_metadata={"url": "https://example.com/document.pdf"},
        )
        rehydrated = adapter.rehydrate_attachment(attachment)
        assert rehydrated is attachment
        assert rehydrated.fetch_data is None

    def test_returns_unchanged_when_no_metadata(self):
        adapter = _make_adapter()
        attachment = Attachment(type="file", name="local.bin")
        rehydrated = adapter.rehydrate_attachment(attachment)
        assert rehydrated is attachment

    # Divergence from upstream -- see docs/UPSTREAM_SYNC.md (media
    # resourceName validation). ``resourceName`` is interpolated into the
    # request path and can come from serialized ``fetch_metadata``, so shapes
    # that would change what ``/v1/media/{resourceName}`` addresses are
    # rejected before a token is minted or a request is made.
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "resource_name",
        [
            "../../spaces/X/messages/Y",
            "spaces/ABC/attachments/../../../spaces/X",
            "spaces/ABC/./attachments/Y",
            "..",
            ".",
            "spaces/ABC/attachments/Y?alt=json",
            "spaces/ABC/attachments/Y#frag",
            "spaces/ABC/attachments/%2e%2e",
            "spaces/ABC/attachments/Y Z",
            "spaces/ABC/attachments/Y\n",
            "spaces/ABC/attachments/Y\t",
            "spaces/ABC/attachments/Y\x00",
            "spaces/ABC/attachments/Y\x85",
            "spaces\\..\\x",
            "spaces/ABC/attachments/\uff0e\uff0e",
        ],
    )
    async def test_rehydrated_fetch_data_rejects_unsafe_resource_names(self, resource_name: str):
        adapter, session = _media_adapter()
        attachment = Attachment(type="image", fetch_metadata={"resourceName": resource_name})
        rehydrated = adapter.rehydrate_attachment(attachment)
        assert rehydrated.fetch_data is not None
        with pytest.raises(ValidationError, match="Invalid Google Chat attachment resource name"):
            await rehydrated.fetch_data()
        adapter._get_access_token.assert_not_awaited()  # type: ignore[attr-defined]
        adapter._get_http_session.assert_not_awaited()  # type: ignore[attr-defined]
        assert session.calls == []

    @pytest.mark.asyncio
    async def test_rehydrated_fetch_data_rejects_a_non_string_resource_name(self):
        adapter, session = _media_adapter()
        rehydrated = adapter.rehydrate_attachment(Attachment(type="file", fetch_metadata={"resourceName": 42}))
        assert rehydrated.fetch_data is not None
        with pytest.raises(ValidationError, match="Invalid Google Chat attachment resource name"):
            await rehydrated.fetch_data()
        assert session.calls == []

    @pytest.mark.asyncio
    async def test_accepts_opaque_resource_names(self):
        # resourceName is opaque (it may be a base64-style token), so only the
        # denylisted shapes are rejected.
        adapter, session = _media_adapter()
        attachment = Attachment(
            type="image",
            fetch_metadata={"resourceName": "ClxjaGF0LmNvbS9+abc_DEF-123=/x.y"},
        )
        rehydrated = adapter.rehydrate_attachment(attachment)
        assert rehydrated.fetch_data is not None
        assert await rehydrated.fetch_data() == b"\x89PNG"
        assert [url for url, _ in session.calls] == [
            "https://chat.googleapis.com/v1/media/ClxjaGF0LmNvbS9+abc_DEF-123=/x.y?alt=media"
        ]
