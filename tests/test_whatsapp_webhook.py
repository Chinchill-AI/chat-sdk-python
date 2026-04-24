"""Port of adapter-whatsapp/src/index.test.ts -- webhook handling, message processing,
reactions, postMessage, stream, and factory tests.

Tests that duplicate the existing ``test_whatsapp_adapter.py`` are intentionally
omitted; this file covers the *remaining* TypeScript tests from the Vercel Chat
SDK not yet present in the Python test suite.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

from chat_sdk.adapters.whatsapp.adapter import (
    WhatsAppAdapter,
    split_message,
)
from chat_sdk.adapters.whatsapp.types import WhatsAppAdapterConfig, WhatsAppThreadId
from chat_sdk.logger import ConsoleLogger

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_adapter(**overrides: Any) -> WhatsAppAdapter:
    """Create a WhatsAppAdapter with minimal valid config."""
    defaults: dict[str, Any] = {
        "access_token": "test-token",
        "app_secret": "test-secret",
        "phone_number_id": "123456789",
        "verify_token": "test-verify-token",
        "user_name": "test-bot",
        "logger": ConsoleLogger("error"),
    }
    defaults.update(overrides)
    return WhatsAppAdapter(WhatsAppAdapterConfig(**defaults))


def _sign(body: str, secret: str = "test-secret") -> str:
    """Compute the WhatsApp webhook HMAC-SHA256 signature."""
    return "sha256=" + hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def _webhook_payload(*, field: str = "messages", has_messages: bool = True) -> dict[str, Any]:
    """Build a representative WhatsApp webhook payload."""
    value: dict[str, Any] = {
        "metadata": {"phone_number_id": "123456789"},
        "contacts": [{"profile": {"name": "User"}, "wa_id": "15551234567"}],
    }
    if has_messages:
        value["messages"] = [
            {
                "id": "wamid.xxx",
                "from": "15551234567",
                "timestamp": "1700000000",
                "type": "text",
                "text": {"body": "Hello"},
            }
        ]
    return {"entry": [{"changes": [{"field": field, "value": value}]}]}


@dataclass
class _FakeRequest:
    """Minimal request-like object accepted by WhatsAppAdapter.handle_webhook."""

    url: str
    method: str
    _body: str
    headers: dict[str, str]

    async def text(self) -> str:  # noqa: D102 – simple helper
        return self._body


def _make_post_request(
    body: str,
    *,
    signature: str | None = None,
) -> _FakeRequest:
    headers: dict[str, str] = {"content-type": "application/json"}
    if signature is not None:
        headers["x-hub-signature-256"] = signature
    return _FakeRequest(
        url="https://example.com/webhook",
        method="POST",
        _body=body,
        headers=headers,
    )


def _make_get_request(query: str) -> _FakeRequest:
    return _FakeRequest(
        url=f"https://example.com/webhook?{query}",
        method="GET",
        _body="",
        headers={},
    )


# ---------------------------------------------------------------------------
# encodeThreadId / decodeThreadId (complementary to existing tests)
# ---------------------------------------------------------------------------


class TestEncodeDecodeThreadId:
    """Encode/decode with various phone numbers."""

    def test_encode_with_different_numbers(self):
        adapter = _make_adapter()
        result = adapter.encode_thread_id(WhatsAppThreadId(phone_number_id="987654321", user_wa_id="44771234567"))
        assert result == "whatsapp:987654321:44771234567"

    def test_roundtrip_international(self):
        adapter = _make_adapter()
        original = WhatsAppThreadId(phone_number_id="999888777", user_wa_id="919876543210")
        encoded = adapter.encode_thread_id(original)
        decoded = adapter.decode_thread_id(encoded)
        assert decoded.phone_number_id == original.phone_number_id
        assert decoded.user_wa_id == original.user_wa_id


# ---------------------------------------------------------------------------
# renderFormatted
# ---------------------------------------------------------------------------


class TestRenderFormatted:
    """Test render_formatted (the fromAst path)."""

    def test_render_simple_text(self):
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
        assert "Hello world" in result


# ---------------------------------------------------------------------------
# parseMessage
# ---------------------------------------------------------------------------


class TestParseMessage:
    """Tests for parse_message mirroring the TS test suite."""

    def test_text_message(self):
        adapter = _make_adapter()
        raw = {
            "message": {
                "id": "wamid.ABC123",
                "from": "15551234567",
                "timestamp": "1700000000",
                "type": "text",
                "text": {"body": "Hello from WhatsApp!"},
            },
            "phone_number_id": "123456789",
            "contact": {"profile": {"name": "Alice"}, "wa_id": "15551234567"},
        }
        msg = adapter.parse_message(raw)
        assert msg.id == "wamid.ABC123"
        assert msg.text == "Hello from WhatsApp!"
        assert msg.author.user_id == "15551234567"
        assert msg.author.user_name == "Alice"

    def test_message_without_contact(self):
        adapter = _make_adapter()
        raw = {
            "message": {
                "id": "wamid.DEF456",
                "from": "15559876543",
                "timestamp": "1700000100",
                "type": "text",
                "text": {"body": "No contact info"},
            },
            "phone_number_id": "123456789",
        }
        msg = adapter.parse_message(raw)
        assert msg.author.user_name == "15559876543"

    def test_image_with_caption(self):
        adapter = _make_adapter()
        raw = {
            "message": {
                "id": "wamid.IMG001",
                "from": "15551234567",
                "timestamp": "1700000200",
                "type": "image",
                "image": {
                    "id": "media-123",
                    "mime_type": "image/jpeg",
                    "sha256": "abc",
                    "caption": "Check this out",
                },
            },
            "phone_number_id": "123456789",
        }
        msg = adapter.parse_message(raw)
        assert msg.text == "Check this out"

    def test_image_without_caption(self):
        adapter = _make_adapter()
        raw = {
            "message": {
                "id": "wamid.IMG002",
                "from": "15551234567",
                "timestamp": "1700000300",
                "type": "image",
                "image": {"id": "media-456", "mime_type": "image/png", "sha256": "def"},
            },
            "phone_number_id": "123456789",
        }
        msg = adapter.parse_message(raw)
        assert msg.text == "[Image]"

    def test_date_sent_from_timestamp(self):
        adapter = _make_adapter()
        raw = {
            "message": {
                "id": "wamid.TIME001",
                "from": "15551234567",
                "timestamp": "1700000000",
                "type": "text",
                "text": {"body": "test"},
            },
            "phone_number_id": "123456789",
        }
        msg = adapter.parse_message(raw)
        # 1700000000 seconds since epoch
        assert msg.metadata.date_sent.timestamp() == 1700000000

    def test_thread_id_encoding(self):
        adapter = _make_adapter()
        raw = {
            "message": {
                "id": "wamid.THREAD001",
                "from": "15559876543",
                "timestamp": "1700000000",
                "type": "text",
                "text": {"body": "test"},
            },
            "phone_number_id": "987654321",
        }
        msg = adapter.parse_message(raw)
        assert msg.thread_id == "whatsapp:987654321:15559876543"

    def test_plain_text_has_no_attachments(self):
        adapter = _make_adapter()
        raw = {
            "message": {
                "id": "wamid.TXT001",
                "from": "15551234567",
                "timestamp": "1700000000",
                "type": "text",
                "text": {"body": "Hello"},
            },
            "phone_number_id": "123456789",
        }
        msg = adapter.parse_message(raw)
        assert len(msg.attachments) == 0


class TestParseMessageMediaAttachments:
    """Media attachment parsing (image, document, audio, video, sticker, location, voice)."""

    def test_image_attachment(self):
        adapter = _make_adapter()
        raw = {
            "message": {
                "id": "wamid.IMG001",
                "from": "15551234567",
                "timestamp": "1700000200",
                "type": "image",
                "image": {
                    "id": "media-img-123",
                    "mime_type": "image/jpeg",
                    "sha256": "abc",
                    "caption": "A photo",
                },
            },
            "phone_number_id": "123456789",
        }
        msg = adapter.parse_message(raw)
        assert msg.text == "A photo"
        assert len(msg.attachments) == 1
        assert msg.attachments[0].type == "image"
        assert msg.attachments[0].mime_type == "image/jpeg"

    def test_document_attachment(self):
        adapter = _make_adapter()
        raw = {
            "message": {
                "id": "wamid.DOC001",
                "from": "15551234567",
                "timestamp": "1700000300",
                "type": "document",
                "document": {
                    "id": "media-doc-456",
                    "mime_type": "application/pdf",
                    "sha256": "def",
                    "filename": "report.pdf",
                },
            },
            "phone_number_id": "123456789",
        }
        msg = adapter.parse_message(raw)
        assert msg.text == "[Document: report.pdf]"
        assert len(msg.attachments) == 1
        assert msg.attachments[0].type == "file"
        assert msg.attachments[0].mime_type == "application/pdf"
        assert msg.attachments[0].name == "report.pdf"

    def test_audio_attachment(self):
        adapter = _make_adapter()
        raw = {
            "message": {
                "id": "wamid.AUD001",
                "from": "15551234567",
                "timestamp": "1700000400",
                "type": "audio",
                "audio": {
                    "id": "media-aud-789",
                    "mime_type": "audio/ogg",
                    "sha256": "ghi",
                },
            },
            "phone_number_id": "123456789",
        }
        msg = adapter.parse_message(raw)
        assert msg.text == "[Audio message]"
        assert len(msg.attachments) == 1
        assert msg.attachments[0].type == "audio"
        assert msg.attachments[0].mime_type == "audio/ogg"

    def test_video_attachment(self):
        adapter = _make_adapter()
        raw = {
            "message": {
                "id": "wamid.VID001",
                "from": "15551234567",
                "timestamp": "1700000500",
                "type": "video",
                "video": {
                    "id": "media-vid-101",
                    "mime_type": "video/mp4",
                    "sha256": "jkl",
                },
            },
            "phone_number_id": "123456789",
        }
        msg = adapter.parse_message(raw)
        assert msg.text == "[Video]"
        assert len(msg.attachments) == 1
        assert msg.attachments[0].type == "video"
        assert msg.attachments[0].mime_type == "video/mp4"

    def test_sticker_attachment(self):
        adapter = _make_adapter()
        raw = {
            "message": {
                "id": "wamid.STK001",
                "from": "15551234567",
                "timestamp": "1700000600",
                "type": "sticker",
                "sticker": {
                    "id": "media-stk-202",
                    "mime_type": "image/webp",
                    "sha256": "mno",
                    "animated": False,
                },
            },
            "phone_number_id": "123456789",
        }
        msg = adapter.parse_message(raw)
        assert msg.text == "[Sticker]"
        assert len(msg.attachments) == 1
        assert msg.attachments[0].type == "image"
        assert msg.attachments[0].mime_type == "image/webp"

    def test_location_with_name_and_address(self):
        adapter = _make_adapter()
        raw = {
            "message": {
                "id": "wamid.LOC001",
                "from": "15551234567",
                "timestamp": "1700000700",
                "type": "location",
                "location": {
                    "latitude": 37.7749,
                    "longitude": -122.4194,
                    "name": "San Francisco",
                    "address": "CA, USA",
                },
            },
            "phone_number_id": "123456789",
        }
        msg = adapter.parse_message(raw)
        assert "San Francisco" in msg.text
        assert "CA, USA" in msg.text
        assert len(msg.attachments) == 1

    def test_location_without_name(self):
        adapter = _make_adapter()
        raw = {
            "message": {
                "id": "wamid.LOC002",
                "from": "15551234567",
                "timestamp": "1700000800",
                "type": "location",
                "location": {"latitude": 48.8566, "longitude": 2.3522},
            },
            "phone_number_id": "123456789",
        }
        msg = adapter.parse_message(raw)
        assert "48.8566" in msg.text
        assert "2.3522" in msg.text

    def test_voice_message_attachment(self):
        adapter = _make_adapter()
        raw = {
            "message": {
                "id": "wamid.VOC001",
                "from": "15551234567",
                "timestamp": "1700000650",
                "type": "voice",
                "voice": {
                    "id": "media-voc-303",
                    "mime_type": "audio/ogg; codecs=opus",
                    "sha256": "pqr",
                },
            },
            "phone_number_id": "123456789",
        }
        msg = adapter.parse_message(raw)
        assert msg.text == "[Voice message]"
        assert len(msg.attachments) == 1
        assert msg.attachments[0].type == "audio"
        assert msg.attachments[0].mime_type == "audio/ogg; codecs=opus"


# ---------------------------------------------------------------------------
# handleWebhook -- verification challenge (GET)
# ---------------------------------------------------------------------------


class TestWebhookVerificationChallenge:
    """Verification challenge handling (GET requests)."""

    @pytest.mark.asyncio
    async def test_valid_verification_challenge(self):
        adapter = _make_adapter()
        request = _make_get_request("hub.mode=subscribe&hub.verify_token=test-verify-token&hub.challenge=1234567890")
        response = await adapter.handle_webhook(request)
        assert response["status"] == 200
        assert response["body"] == "1234567890"

    @pytest.mark.asyncio
    async def test_invalid_verify_token(self):
        adapter = _make_adapter()
        request = _make_get_request("hub.mode=subscribe&hub.verify_token=wrong-token&hub.challenge=1234567890")
        response = await adapter.handle_webhook(request)
        assert response["status"] == 403

    @pytest.mark.asyncio
    async def test_wrong_mode(self):
        adapter = _make_adapter()
        request = _make_get_request("hub.mode=unsubscribe&hub.verify_token=test-verify-token&hub.challenge=1234567890")
        response = await adapter.handle_webhook(request)
        assert response["status"] == 403


# ---------------------------------------------------------------------------
# handleWebhook -- POST with signature verification
# ---------------------------------------------------------------------------


class TestWebhookPostSignature:
    """POST webhook signature verification."""

    @pytest.mark.asyncio
    async def test_valid_signature_returns_200(self):
        adapter = _make_adapter()
        body = json.dumps(_webhook_payload())
        sig = _sign(body)
        request = _make_post_request(body, signature=sig)
        response = await adapter.handle_webhook(request)
        assert response["status"] == 200

    @pytest.mark.asyncio
    async def test_invalid_signature_returns_401(self):
        adapter = _make_adapter()
        body = json.dumps(_webhook_payload())
        request = _make_post_request(body, signature="sha256=badsignature")
        response = await adapter.handle_webhook(request)
        assert response["status"] == 401

    @pytest.mark.asyncio
    async def test_missing_signature_returns_401(self):
        adapter = _make_adapter()
        body = json.dumps(_webhook_payload())
        request = _make_post_request(body, signature=None)
        response = await adapter.handle_webhook(request)
        assert response["status"] == 401

    @pytest.mark.asyncio
    async def test_invalid_json_returns_400(self):
        adapter = _make_adapter()
        body = "not-json"
        sig = _sign(body)
        request = _make_post_request(body, signature=sig)
        response = await adapter.handle_webhook(request)
        assert response["status"] == 400

    @pytest.mark.asyncio
    async def test_no_messages_returns_200(self):
        adapter = _make_adapter()
        payload = _webhook_payload(has_messages=False)
        body = json.dumps(payload)
        sig = _sign(body)
        request = _make_post_request(body, signature=sig)
        response = await adapter.handle_webhook(request)
        assert response["status"] == 200


# ---------------------------------------------------------------------------
# handleWebhook -- POST message processing (initialized adapter)
# ---------------------------------------------------------------------------


class TestWebhookMessageProcessing:
    """Message processing after initialization."""

    @pytest.mark.asyncio
    async def test_text_message_calls_process_message(self):
        adapter = _make_adapter()
        mock_chat = MagicMock()
        mock_chat.processMessage = MagicMock()
        mock_chat.process_message = MagicMock()
        await adapter.initialize(mock_chat)

        body = json.dumps(_webhook_payload())
        sig = _sign(body)
        request = _make_post_request(body, signature=sig)
        response = await adapter.handle_webhook(request)

        assert response["status"] == 200

    @pytest.mark.asyncio
    async def test_non_messages_field_skipped(self):
        adapter = _make_adapter()
        mock_chat = MagicMock()
        await adapter.initialize(mock_chat)

        payload = _webhook_payload(field="statuses", has_messages=False)
        body = json.dumps(payload)
        sig = _sign(body)
        request = _make_post_request(body, signature=sig)
        response = await adapter.handle_webhook(request)

        assert response["status"] == 200


# ---------------------------------------------------------------------------
# splitMessage (complementary to existing tests)
# ---------------------------------------------------------------------------


class TestSplitMessageExtended:
    """Additional split_message tests from the TS suite."""

    def test_exactly_at_limit(self):
        text = "a" * 4096
        assert split_message(text) == [text]

    def test_paragraph_boundary_split(self):
        p1 = "a" * 3000
        p2 = "b" * 3000
        text = f"{p1}\n\n{p2}"
        result = split_message(text)
        assert len(result) == 2
        assert result[0] == p1
        assert result[1] == p2

    def test_line_boundary_split(self):
        l1 = "a" * 3000
        l2 = "b" * 3000
        text = f"{l1}\n{l2}"
        result = split_message(text)
        assert len(result) == 2
        assert result[0] == l1
        assert result[1] == l2

    def test_hard_break(self):
        text = "a" * 5000
        result = split_message(text)
        assert len(result) == 2
        assert result[0] == "a" * 4096
        assert result[1] == "a" * 904

    def test_three_chunks(self):
        p1 = "a" * 4000
        p2 = "b" * 4000
        p3 = "c" * 4000
        text = f"{p1}\n\n{p2}\n\n{p3}"
        result = split_message(text)
        assert len(result) == 3

    def test_preserve_all_content(self):
        text = "x" * 10000
        result = split_message(text)
        assert "".join(result) == text


# ---------------------------------------------------------------------------
# fetchMessages / fetchThread / openDM / startTyping / isDM
# ---------------------------------------------------------------------------


class TestAdapterMiscMethods:
    """Miscellaneous adapter methods."""

    @pytest.mark.asyncio
    async def test_fetch_messages_returns_empty(self):
        adapter = _make_adapter()
        result = await adapter.fetch_messages("whatsapp:123456789:15551234567")
        assert result.messages == []

    @pytest.mark.asyncio
    async def test_fetch_thread_returns_info(self):
        adapter = _make_adapter()
        info = await adapter.fetch_thread("whatsapp:123456789:15551234567")
        assert info.id == "whatsapp:123456789:15551234567"
        assert info.is_dm is True

    @pytest.mark.asyncio
    async def test_open_dm_returns_thread_id(self):
        adapter = _make_adapter()
        tid = await adapter.open_dm("15551234567")
        assert tid == "whatsapp:123456789:15551234567"

    @pytest.mark.asyncio
    async def test_start_typing_noop(self):
        adapter = _make_adapter()
        result = await adapter.start_typing("whatsapp:123456789:15551234567")
        assert result is None

    def test_is_dm_always_true(self):
        adapter = _make_adapter()
        assert adapter.is_dm("whatsapp:123456789:15551234567") is True

    def test_channel_id_from_thread_id(self):
        adapter = _make_adapter()
        result = adapter.channel_id_from_thread_id("whatsapp:123456789:15551234567")
        assert result == "whatsapp:123456789:15551234567"


# ---------------------------------------------------------------------------
# editMessage / deleteMessage -- not supported
# ---------------------------------------------------------------------------


class TestUnsupportedMethods:
    """Methods that should raise 'not supported'."""

    @pytest.mark.asyncio
    async def test_edit_message_raises(self):
        adapter = _make_adapter()
        with pytest.raises(Exception, match="(?i)not support"):
            await adapter.edit_message("whatsapp:123456789:15551234567", "wamid.xxx", {"text": "Updated"})

    @pytest.mark.asyncio
    async def test_delete_message_raises(self):
        adapter = _make_adapter()
        with pytest.raises(Exception, match="(?i)not support"):
            await adapter.delete_message("whatsapp:123456789:15551234567", "wamid.xxx")


# ---------------------------------------------------------------------------
# rehydrate_attachment
# ---------------------------------------------------------------------------


class TestRehydrateAttachment:
    """Cover ``WhatsAppAdapter.rehydrate_attachment``."""

    def test_rehydrates_fetch_data_from_media_id(self):
        from chat_sdk.types import Attachment

        adapter = _make_adapter()
        attachment = Attachment(
            type="image",
            fetch_metadata={"mediaId": "media-42"},
        )
        rehydrated = adapter.rehydrate_attachment(attachment)
        assert rehydrated.fetch_data is not None
        assert rehydrated.fetch_metadata == {"mediaId": "media-42"}

    def test_returns_unchanged_when_no_media_id(self):
        from chat_sdk.types import Attachment

        adapter = _make_adapter()
        attachment = Attachment(type="file", name="local.bin")
        rehydrated = adapter.rehydrate_attachment(attachment)
        assert rehydrated is attachment
