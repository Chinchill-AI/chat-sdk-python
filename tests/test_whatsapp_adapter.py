"""Tests for the WhatsApp adapter."""

from __future__ import annotations

import hashlib
import hmac
import os

import pytest

from chat_sdk.adapters.whatsapp.adapter import (
    WHATSAPP_MESSAGE_LIMIT,
    WhatsAppAdapter,
    create_whatsapp_adapter,
    split_message,
)
from chat_sdk.adapters.whatsapp.types import WhatsAppAdapterConfig, WhatsAppThreadId
from chat_sdk.logger import ConsoleLogger
from chat_sdk.shared.errors import ValidationError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_adapter(**overrides) -> WhatsAppAdapter:
    """Create a WhatsAppAdapter with minimal valid config."""
    defaults = {
        "access_token": "test-token",
        "app_secret": "test-secret",
        "phone_number_id": "1234567890",
        "verify_token": "verify-me",
        "user_name": "test-bot",
        "logger": ConsoleLogger("error"),
    }
    defaults.update(overrides)
    return WhatsAppAdapter(WhatsAppAdapterConfig(**defaults))


# ---------------------------------------------------------------------------
# Thread ID encode / decode
# ---------------------------------------------------------------------------


class TestWhatsAppThreadId:
    """Thread ID encoding and decoding."""

    def test_encode_thread_id(self):
        adapter = _make_adapter()
        tid = adapter.encode_thread_id(WhatsAppThreadId(phone_number_id="111", user_wa_id="222"))
        assert tid == "whatsapp:111:222"

    def test_decode_thread_id(self):
        adapter = _make_adapter()
        decoded = adapter.decode_thread_id("whatsapp:111:222")
        assert decoded.phone_number_id == "111"
        assert decoded.user_wa_id == "222"

    def test_roundtrip(self):
        adapter = _make_adapter()
        original = WhatsAppThreadId(phone_number_id="abc", user_wa_id="def")
        encoded = adapter.encode_thread_id(original)
        decoded = adapter.decode_thread_id(encoded)
        assert decoded.phone_number_id == original.phone_number_id
        assert decoded.user_wa_id == original.user_wa_id

    def test_decode_invalid_prefix(self):
        adapter = _make_adapter()
        with pytest.raises(ValidationError):
            adapter.decode_thread_id("telegram:111:222")

    def test_decode_empty_after_prefix(self):
        adapter = _make_adapter()
        with pytest.raises(ValidationError):
            adapter.decode_thread_id("whatsapp:")

    def test_decode_missing_user_id(self):
        adapter = _make_adapter()
        with pytest.raises(ValidationError):
            adapter.decode_thread_id("whatsapp:111")

    def test_decode_too_many_parts(self):
        adapter = _make_adapter()
        with pytest.raises(ValidationError):
            adapter.decode_thread_id("whatsapp:111:222:333")

    def test_decode_empty_parts(self):
        adapter = _make_adapter()
        with pytest.raises(ValidationError):
            adapter.decode_thread_id("whatsapp::222")

    def test_channel_id_from_thread_id(self):
        adapter = _make_adapter()
        tid = "whatsapp:111:222"
        assert adapter.channel_id_from_thread_id(tid) == tid

    def test_is_dm_always_true(self):
        adapter = _make_adapter()
        assert adapter.is_dm("whatsapp:111:222") is True


# ---------------------------------------------------------------------------
# split_message
# ---------------------------------------------------------------------------


class TestSplitMessage:
    """Tests for split_message."""

    def test_short_message_not_split(self):
        text = "Hello, world!"
        chunks = split_message(text)
        assert chunks == [text]

    def test_exactly_at_limit(self):
        text = "x" * WHATSAPP_MESSAGE_LIMIT
        chunks = split_message(text)
        assert chunks == [text]

    def test_long_message_split(self):
        # Create text just over the limit
        text = "x" * (WHATSAPP_MESSAGE_LIMIT + 100)
        chunks = split_message(text)
        assert len(chunks) >= 2
        # All chunks should fit within the limit
        for chunk in chunks:
            assert len(chunk) <= WHATSAPP_MESSAGE_LIMIT
        # Concatenation of chunks should yield the original content (minus whitespace)
        reassembled = "".join(chunks)
        assert reassembled == text  # no whitespace breaks in pure 'x' string

    def test_split_on_paragraph_boundary(self):
        part1 = "a" * (WHATSAPP_MESSAGE_LIMIT - 100)
        part2 = "b" * 200
        text = part1 + "\n\n" + part2
        chunks = split_message(text)
        assert len(chunks) == 2
        assert chunks[0].strip() == part1
        assert chunks[1].strip() == part2

    def test_split_on_line_boundary(self):
        # No paragraph boundary, only line boundary
        part1 = "a" * (WHATSAPP_MESSAGE_LIMIT - 50)
        part2 = "b" * 100
        text = part1 + "\n" + part2
        chunks = split_message(text)
        assert len(chunks) == 2

    def test_empty_message(self):
        assert split_message("") == [""]


# ---------------------------------------------------------------------------
# verify_signature
# ---------------------------------------------------------------------------


class TestVerifySignature:
    """Tests for _verify_signature."""

    def test_valid_signature(self):
        adapter = _make_adapter(app_secret="my-secret")
        body = '{"test": true}'
        sig = (
            "sha256="
            + hmac.new(
                b"my-secret",
                body.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
        )
        assert adapter._verify_signature(body, sig) is True

    def test_invalid_signature(self):
        adapter = _make_adapter(app_secret="my-secret")
        body = '{"test": true}'
        assert adapter._verify_signature(body, "sha256=invalid") is False

    def test_none_signature(self):
        adapter = _make_adapter(app_secret="my-secret")
        assert adapter._verify_signature("body", None) is False

    def test_empty_signature(self):
        adapter = _make_adapter(app_secret="my-secret")
        assert adapter._verify_signature("body", "") is False


# ---------------------------------------------------------------------------
# _extract_text_content
# ---------------------------------------------------------------------------


class TestExtractTextContent:
    """Tests for _extract_text_content."""

    def test_text_message(self):
        adapter = _make_adapter()
        msg = {"type": "text", "text": {"body": "Hello"}}
        assert adapter._extract_text_content(msg) == "Hello"

    def test_image_with_caption(self):
        adapter = _make_adapter()
        msg = {"type": "image", "image": {"caption": "My photo"}}
        assert adapter._extract_text_content(msg) == "My photo"

    def test_image_without_caption(self):
        adapter = _make_adapter()
        msg = {"type": "image", "image": {}}
        assert adapter._extract_text_content(msg) == "[Image]"

    def test_document_with_caption(self):
        adapter = _make_adapter()
        msg = {"type": "document", "document": {"caption": "See attached"}}
        assert adapter._extract_text_content(msg) == "See attached"

    def test_document_with_filename(self):
        adapter = _make_adapter()
        msg = {"type": "document", "document": {"filename": "report.pdf"}}
        assert adapter._extract_text_content(msg) == "[Document: report.pdf]"

    def test_document_default(self):
        adapter = _make_adapter()
        msg = {"type": "document", "document": {}}
        assert adapter._extract_text_content(msg) == "[Document: file]"

    def test_audio_message(self):
        adapter = _make_adapter()
        msg = {"type": "audio"}
        assert adapter._extract_text_content(msg) == "[Audio message]"

    def test_voice_message(self):
        adapter = _make_adapter()
        msg = {"type": "voice"}
        assert adapter._extract_text_content(msg) == "[Voice message]"

    def test_video_message(self):
        adapter = _make_adapter()
        msg = {"type": "video"}
        assert adapter._extract_text_content(msg) == "[Video]"

    def test_sticker_message(self):
        adapter = _make_adapter()
        msg = {"type": "sticker"}
        assert adapter._extract_text_content(msg) == "[Sticker]"

    def test_location_with_name(self):
        adapter = _make_adapter()
        msg = {
            "type": "location",
            "location": {"latitude": 37.7749, "longitude": -122.4194, "name": "SF"},
        }
        result = adapter._extract_text_content(msg)
        assert result is not None
        assert "SF" in result

    def test_location_with_address(self):
        adapter = _make_adapter()
        msg = {
            "type": "location",
            "location": {
                "latitude": 37.7749,
                "longitude": -122.4194,
                "address": "123 Main St",
            },
        }
        result = adapter._extract_text_content(msg)
        assert result is not None
        assert "123 Main St" in result

    def test_location_coordinates_only(self):
        adapter = _make_adapter()
        msg = {
            "type": "location",
            "location": {"latitude": 37.7749, "longitude": -122.4194},
        }
        result = adapter._extract_text_content(msg)
        assert result is not None
        assert "37.7749" in result

    def test_unsupported_type(self):
        adapter = _make_adapter()
        msg = {"type": "contacts"}
        assert adapter._extract_text_content(msg) is None

    def test_unknown_type(self):
        adapter = _make_adapter()
        msg = {"type": "unknown_future_type"}
        assert adapter._extract_text_content(msg) is None


# ---------------------------------------------------------------------------
# create_whatsapp_adapter factory
# ---------------------------------------------------------------------------


class TestCreateWhatsAppAdapter:
    """Tests for create_whatsapp_adapter factory."""

    def test_with_explicit_params(self):
        adapter = create_whatsapp_adapter(
            access_token="tok",
            app_secret="sec",
            phone_number_id="123",
            verify_token="vt",
            user_name="mybot",
        )
        assert adapter.name == "whatsapp"
        assert adapter.user_name == "mybot"

    def test_missing_access_token(self):
        # Ensure env vars are not set
        env = {
            "WHATSAPP_ACCESS_TOKEN": "",
            "WHATSAPP_APP_SECRET": "s",
            "WHATSAPP_PHONE_NUMBER_ID": "p",
            "WHATSAPP_VERIFY_TOKEN": "v",
        }
        old_env = {}
        for k, v in env.items():
            old_env[k] = os.environ.get(k)
            if v:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
        try:
            with pytest.raises(ValidationError, match="accessToken"):
                create_whatsapp_adapter()
        finally:
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_missing_app_secret(self):
        with pytest.raises(ValidationError, match="appSecret"):
            create_whatsapp_adapter(access_token="tok")

    def test_missing_phone_number_id(self):
        with pytest.raises(ValidationError, match="phoneNumberId"):
            create_whatsapp_adapter(access_token="tok", app_secret="sec")

    def test_missing_verify_token(self):
        with pytest.raises(ValidationError, match="verifyToken"):
            create_whatsapp_adapter(
                access_token="tok",
                app_secret="sec",
                phone_number_id="123",
            )

    def test_default_user_name(self):
        adapter = create_whatsapp_adapter(
            access_token="tok",
            app_secret="sec",
            phone_number_id="123",
            verify_token="vt",
        )
        assert adapter.user_name == "whatsapp-bot"

    def test_adapter_properties(self):
        adapter = _make_adapter()
        assert adapter.name == "whatsapp"
        assert adapter.lock_scope == "channel"
        assert adapter.persist_message_history is True
        assert adapter.bot_user_id is None  # not yet initialized
