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
