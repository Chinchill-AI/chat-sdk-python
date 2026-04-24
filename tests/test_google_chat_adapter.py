"""Tests for the Google Chat adapter."""

from __future__ import annotations

import os

import pytest

from chat_sdk.adapters.google_chat.adapter import GoogleChatAdapter
from chat_sdk.adapters.google_chat.thread_utils import (
    GoogleChatThreadId,
    decode_thread_id,
    encode_thread_id,
    is_dm_thread,
)
from chat_sdk.adapters.google_chat.types import (
    GoogleChatAdapterConfig,
    ServiceAccountCredentials,
)
from chat_sdk.shared.errors import ValidationError

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
# create_google_chat_adapter factory
# ---------------------------------------------------------------------------


class TestCreateGoogleChatAdapter:
    """Tests for GoogleChatAdapter construction."""

    def test_with_credentials(self):
        adapter = _make_adapter()
        assert adapter.name == "gchat"

    def test_with_adc(self):
        adapter = GoogleChatAdapter(GoogleChatAdapterConfig(use_application_default_credentials=True))
        assert adapter.name == "gchat"

    def test_missing_auth(self):
        old_creds = os.environ.pop("GOOGLE_CHAT_CREDENTIALS", None)
        old_adc = os.environ.pop("GOOGLE_CHAT_USE_ADC", None)
        try:
            with pytest.raises(ValidationError, match="Authentication"):
                GoogleChatAdapter(GoogleChatAdapterConfig())
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
# rehydrate_attachment
# ---------------------------------------------------------------------------


class TestRehydrateAttachment:
    """Cover ``GoogleChatAdapter.rehydrate_attachment``."""

    def test_rehydrates_from_resource_name(self):
        from chat_sdk.types import Attachment

        adapter = _make_adapter()
        attachment = Attachment(
            type="image",
            fetch_metadata={"resourceName": "spaces/ABC/messages/X/attachments/Y"},
        )
        rehydrated = adapter.rehydrate_attachment(attachment)
        assert rehydrated.fetch_data is not None
        assert rehydrated.fetch_metadata == {
            "resourceName": "spaces/ABC/messages/X/attachments/Y",
        }

    def test_rehydrates_from_url_when_no_resource_name(self):
        from chat_sdk.types import Attachment

        adapter = _make_adapter()
        attachment = Attachment(
            type="file",
            url="https://chat.googleapis.com/v1/media/X?alt=media",
            fetch_metadata={"url": "https://chat.googleapis.com/v1/media/X?alt=media"},
        )
        rehydrated = adapter.rehydrate_attachment(attachment)
        assert rehydrated.fetch_data is not None

    def test_returns_unchanged_when_no_metadata(self):
        from chat_sdk.types import Attachment

        adapter = _make_adapter()
        attachment = Attachment(type="file", name="local.bin")
        rehydrated = adapter.rehydrate_attachment(attachment)
        assert rehydrated is attachment

    # Python-first divergence: SSRF guard on the downloadUri fallback path.
    # The resource_name branch stays trusted (the URL is constructed by
    # `_build_gchat_fetch_data` from a validated ``spaces/.../messages/...``
    # identifier, not from an attacker-controllable string).  The `url`
    # branch is the one that accepts serialized fetch_metadata.
    @pytest.mark.asyncio
    async def test_rehydrated_fetch_data_rejects_untrusted_url(self):
        from unittest.mock import AsyncMock

        from chat_sdk.types import Attachment

        adapter = _make_adapter()
        # These must never be awaited — validation rejects first.
        adapter._get_access_token = AsyncMock()  # type: ignore[method-assign]
        adapter._get_http_session = AsyncMock()  # type: ignore[method-assign]

        attachment = Attachment(
            type="image",
            url="https://attacker.example.com/pwn",
            fetch_metadata={"url": "https://attacker.example.com/pwn"},
        )
        rehydrated = adapter.rehydrate_attachment(attachment)
        assert rehydrated.fetch_data is not None
        with pytest.raises(ValidationError):
            await rehydrated.fetch_data()
        adapter._get_access_token.assert_not_awaited()
        adapter._get_http_session.assert_not_awaited()

    def test_is_trusted_gchat_download_url_allowlist(self):
        assert GoogleChatAdapter._is_trusted_gchat_download_url("https://chat.googleapis.com/v1/media/X?alt=media")
        assert GoogleChatAdapter._is_trusted_gchat_download_url("https://lh3.googleusercontent.com/photo.jpg")
        assert GoogleChatAdapter._is_trusted_gchat_download_url("https://foo.google.com/x")
        # Rejects non-HTTPS
        assert not GoogleChatAdapter._is_trusted_gchat_download_url("http://chat.googleapis.com/x")
        # Rejects arbitrary hosts
        assert not GoogleChatAdapter._is_trusted_gchat_download_url("https://attacker.example/x")
        # Rejects look-alikes
        assert not GoogleChatAdapter._is_trusted_gchat_download_url("https://chat.googleapis.com.attacker.tld/x")
