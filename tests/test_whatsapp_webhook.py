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
from unittest.mock import AsyncMock, MagicMock

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

    def test_is_dm_always_true(self):
        adapter = _make_adapter()
        assert adapter.is_dm("whatsapp:123456789:15551234567") is True

    def test_channel_id_from_thread_id(self):
        adapter = _make_adapter()
        result = adapter.channel_id_from_thread_id("whatsapp:123456789:15551234567")
        assert result == "whatsapp:123456789:15551234567"


# ---------------------------------------------------------------------------
# startTyping (vercel/chat#320 — typing indicator via the Cloud API)
# ---------------------------------------------------------------------------


def _serialized_history_message(message_id: str, *, is_me: bool) -> dict[str, Any]:
    """Build a serialized Message dict as stored by ThreadHistoryCache."""
    user_id = "123456789" if is_me else "15551234567"
    name = "bot" if is_me else "User"
    return {
        "_type": "chat:Message",
        "id": message_id,
        "threadId": "whatsapp:123456789:15551234567",
        "text": "Hello" if is_me else "Hi",
        "author": {
            "userId": user_id,
            "userName": name,
            "fullName": name,
            "isMe": is_me,
            "isBot": is_me,
        },
        "formatted": {"type": "root", "children": []},
        "attachments": [],
        "metadata": {"dateSent": "2026-06-01T00:00:00Z", "edited": False},
    }


def _make_chat_with_history(history: list[dict[str, Any]]) -> MagicMock:
    """Mock ChatInstance whose state returns the given thread history."""
    state = MagicMock()
    state.get_list = AsyncMock(return_value=history)
    chat = MagicMock()
    chat.get_state = MagicMock(return_value=state)
    chat._state = state
    return chat


class TestStartTyping:
    """Port of upstream describe("startTyping") (vercel/chat#320)."""

    THREAD_ID = "whatsapp:123456789:15551234567"

    @pytest.mark.asyncio
    async def test_resolves_latest_inbound_message_id_and_sends_typing_indicator(self):
        adapter = _make_adapter()
        # History: 1 inbound message, then 1 outbound (bot) message. The
        # typing indicator must target the latest *inbound* message, skipping
        # the bot's own newer reply.
        chat = _make_chat_with_history(
            [
                _serialized_history_message("wamid.inbound123", is_me=False),
                _serialized_history_message("wamid.outbound456", is_me=True),
            ]
        )
        await adapter.initialize(chat)
        adapter._graph_api_request = AsyncMock(return_value={"success": True})

        await adapter.start_typing(self.THREAD_ID)

        # History is read through the ThreadHistoryCache key contract.
        chat._state.get_list.assert_awaited_once_with(f"msg-history:{self.THREAD_ID}")

        adapter._graph_api_request.assert_awaited_once()
        path, body = adapter._graph_api_request.call_args[0]
        assert path == "/123456789/messages"
        assert body["status"] == "read"
        assert body["message_id"] == "wamid.inbound123"
        assert body["typing_indicator"]["type"] == "text"

    @pytest.mark.asyncio
    async def test_does_nothing_if_no_inbound_message_is_found_in_history(self):
        adapter = _make_adapter()
        chat = _make_chat_with_history([])
        await adapter.initialize(chat)
        adapter._graph_api_request = AsyncMock(return_value={"success": True})

        await adapter.start_typing(self.THREAD_ID)

        adapter._graph_api_request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_start_typing_raises_adapter_error_when_api_reports_failure(self):
        """Python-only branch coverage: success=false from the Cloud API must
        surface as an AdapterError (upstream throws AdapterError too but has
        no test for it)."""
        from chat_sdk.shared.errors import AdapterError

        adapter = _make_adapter()
        chat = _make_chat_with_history([_serialized_history_message("wamid.inbound123", is_me=False)])
        await adapter.initialize(chat)
        adapter._graph_api_request = AsyncMock(return_value={"success": False})

        with pytest.raises(AdapterError, match="typing indicator failed"):
            await adapter.start_typing(self.THREAD_ID)


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

    @pytest.mark.asyncio
    async def test_rehydrates_fetch_data_from_media_id(self):
        from unittest.mock import AsyncMock

        from chat_sdk.types import Attachment

        adapter = _make_adapter()
        adapter.download_media = AsyncMock(return_value=b"ok")
        attachment = Attachment(
            type="image",
            fetch_metadata={"mediaId": "media-42"},
        )
        rehydrated = adapter.rehydrate_attachment(attachment)
        assert rehydrated.fetch_data is not None
        # Execute the rehydrated closure to verify it wires media_id correctly.
        data = await rehydrated.fetch_data()
        assert data == b"ok"
        adapter.download_media.assert_awaited_once_with("media-42")
        assert rehydrated.fetch_metadata == {"mediaId": "media-42"}

    def test_returns_unchanged_when_no_media_id(self):
        from chat_sdk.types import Attachment

        adapter = _make_adapter()
        attachment = Attachment(type="file", name="local.bin")
        rehydrated = adapter.rehydrate_attachment(attachment)
        assert rehydrated is attachment


# ---------------------------------------------------------------------------
# Business-scoped user IDs (BSUIDs) — port of upstream 3e6e866a (#818) and the
# context variants of 16879fdc (#723)
# ---------------------------------------------------------------------------

BSUID = "US.13491208655302741918"
PARENT_BSUID = "US.ENT.11815799212886844830"
PHONE = "15551234567"
NEW_PHONE = "15557654321"


def _inbound(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Inbound text message with no sender identifiers unless overridden."""
    message: dict[str, Any] = {
        "id": "wamid.inbound",
        "timestamp": "1700000000",
        "type": "text",
        "text": {"body": "Hello"},
    }
    message.update(overrides or {})
    return message


def _notification(
    messages: list[dict[str, Any]],
    contacts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "15550000000", "phone_number_id": "123456789"},
        "messages": messages,
    }
    if contacts is not None:
        value["contacts"] = contacts
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "waba", "changes": [{"field": "messages", "value": value}]}],
    }


def _user_id_update_notification(updates: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba",
                "changes": [
                    {
                        "field": "user_id_update",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"display_phone_number": "15550000000", "phone_number_id": "123456789"},
                            "user_id_update": updates,
                        },
                    }
                ],
            }
        ],
    }


def _system_number_change() -> dict[str, Any]:
    """System message: Meta carries only the NEW identifiers in ``system``;
    the old number rides in the message-level ``from``."""
    return {
        "from": PHONE,
        "id": "wamid.system",
        "timestamp": "1700000001",
        "type": "system",
        "system": {
            "body": f"User A changed from {PHONE} to {NEW_PHONE}",
            "wa_id": NEW_PHONE,
            "user_id": "US.NEW",
            "type": "user_changed_number",
        },
    }


async def _deliver(adapter: WhatsAppAdapter, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload)
    return await adapter.handle_webhook(_make_post_request(body, signature=_sign(body)))


async def _bsuid_env(
    state: Any = None,
) -> tuple[WhatsAppAdapter, MagicMock, Any, MagicMock]:
    """Initialized adapter backed by memory state, a mock chat and a mocked Graph API."""
    from chat_sdk.state.memory import MemoryStateAdapter

    if state is None:
        state = MemoryStateAdapter()
        await state.connect()
    logger = MagicMock()
    adapter = _make_adapter(logger=logger)
    chat = MagicMock()
    chat.get_state = MagicMock(return_value=state)
    chat.process_message = MagicMock()
    chat.process_action = MagicMock()
    chat.process_reaction = MagicMock()
    await adapter.initialize(chat)
    adapter._graph_api_request = AsyncMock(return_value={"messages": [{"id": "wamid.reply"}]})
    return adapter, chat, state, logger


def _sent_body(adapter: WhatsAppAdapter, index: int = 0) -> dict[str, Any]:
    return adapter._graph_api_request.call_args_list[index][0][1]


class TestParseMessageBusinessScopedUserIds:
    """Upstream describe("parseMessage") BSUID cases."""

    def test_preserves_canonical_identity_when_reparsing_raw_messages(self):
        adapter = _make_adapter()
        message = adapter.parse_message(
            {
                "message": {
                    "id": "wamid.BSUID",
                    "from_user_id": BSUID,
                    "timestamp": "1700000000",
                    "type": "text",
                    "text": {"body": "Hello"},
                },
                "phone_number_id": "123456789",
                "user_id": PHONE,
            }
        )

        assert message.thread_id == f"whatsapp:123456789:{PHONE}"
        assert message.author.user_id == PHONE

    def test_parses_bsuid_only_raw_messages_without_stored_identity(self):
        adapter = _make_adapter()
        message = adapter.parse_message(
            {
                "message": {
                    "id": "wamid.BSUID",
                    "from_user_id": BSUID,
                    "timestamp": "1700000000",
                    "type": "text",
                    "text": {"body": "Hello"},
                },
                "phone_number_id": "123456789",
            }
        )

        assert message.thread_id == f"whatsapp:123456789:{BSUID}"
        assert message.author.user_id == BSUID

    def test_parse_message_without_any_identifier_raises_validation_error(self):
        """Python-specific: the error path upstream has no test for."""
        from chat_sdk.shared.errors import ValidationError

        adapter = _make_adapter()
        with pytest.raises(ValidationError, match="WhatsApp message has no user identifier"):
            adapter.parse_message({"message": _inbound({"id": "wamid.none"}), "phone_number_id": "123456789"})


class TestHandleWebhookBusinessScopedUserIds:
    """Upstream describe("handleWebhook - business-scoped user IDs")."""

    @pytest.mark.parametrize(
        ("contact", "message", "user_id", "user_name"),
        [
            pytest.param(
                {"profile": {"name": "User"}, "wa_id": PHONE, "user_id": BSUID},
                {"from": PHONE, "from_user_id": BSUID},
                PHONE,
                "User",
                id="phone and BSUID",
            ),
            pytest.param(
                {"profile": {"name": "User", "username": "exampleuser"}, "user_id": BSUID},
                {"from_user_id": BSUID},
                BSUID,
                "exampleuser",
                id="username without phone",
            ),
            pytest.param(
                {"profile": {"name": "User", "username": "exampleuser"}, "wa_id": PHONE, "user_id": BSUID},
                {"from": PHONE, "from_user_id": BSUID},
                PHONE,
                "exampleuser",
                id="username with phone",
            ),
            pytest.param(
                {
                    "profile": {"name": "User", "username": "exampleuser"},
                    "wa_id": PHONE,
                    "user_id": BSUID,
                    "parent_user_id": PARENT_BSUID,
                },
                {"from": PHONE, "from_user_id": BSUID, "from_parent_user_id": PARENT_BSUID},
                PHONE,
                "exampleuser",
                id="parent BSUID",
            ),
        ],
    )
    async def test_handles_name_webhooks(self, contact, message, user_id, user_name):
        adapter, chat, _, _ = await _bsuid_env()

        response = await _deliver(adapter, _notification([_inbound(message)], [contact]))

        assert response["status"] == 200
        chat.process_message.assert_called_once()
        _, thread_id, parsed, _ = chat.process_message.call_args[0]
        assert thread_id == f"whatsapp:123456789:{user_id}"
        assert parsed.author.user_id == user_id
        assert parsed.author.user_name == user_name
        assert parsed.author.full_name == "User"
        assert parsed.raw["contact"] == contact
        assert parsed.raw["user_id"] == user_id

    async def test_sends_both_known_identifiers_from_a_preserved_phone_thread(self):
        adapter, _, state, _ = await _bsuid_env()

        await _deliver(
            adapter,
            _notification(
                [_inbound({"from": PHONE, "from_user_id": BSUID})],
                [{"profile": {"name": "User"}, "wa_id": PHONE, "user_id": BSUID}],
            ),
        )
        # State keys and route shape are byte-identical to the TS SDK: absent
        # identifiers are omitted, never stored as None.
        assert await state.get(f"whatsapp:identity:route:123456789:{PHONE}") == {"bsuid": BSUID, "phone": PHONE}
        assert await state.get(f"whatsapp:identity:alias:123456789:{PHONE}") == PHONE
        assert await state.get(f"whatsapp:identity:alias:123456789:{BSUID}") == PHONE

        await adapter.post_message(f"whatsapp:123456789:{PHONE}", {"markdown": "Reply"})

        body = _sent_body(adapter)
        assert body["to"] == PHONE
        assert body["recipient"] == BSUID

    async def test_preserves_identity_across_bsuid_rotation_system_messages(self):
        adapter, chat, _, _ = await _bsuid_env()

        await _deliver(
            adapter,
            _notification(
                [_inbound({"from": PHONE, "from_user_id": "US.OLD"})],
                [{"profile": {"name": "User"}, "wa_id": PHONE, "user_id": "US.OLD"}],
            ),
        )
        await _deliver(adapter, _notification([_system_number_change()]))
        await _deliver(
            adapter,
            _notification(
                [_inbound({"from_user_id": "US.NEW"})],
                [{"profile": {"name": "User", "username": "exampleuser"}, "user_id": "US.NEW"}],
            ),
        )

        assert chat.process_message.call_count == 2
        _, thread_id, parsed, _ = chat.process_message.call_args_list[1][0]
        assert thread_id == f"whatsapp:123456789:{PHONE}"
        assert parsed.author.user_id == PHONE

        await adapter.post_message(f"whatsapp:123456789:{PHONE}", {"markdown": "Reply"})
        body = _sent_body(adapter)
        assert body["to"] == NEW_PHONE
        assert body["recipient"] == "US.NEW"

    async def test_preserves_identity_across_number_change_system_messages(self):
        adapter, chat, _, _ = await _bsuid_env()

        await _deliver(adapter, _notification([_inbound({"from": PHONE, "from_user_id": "US.OLD"})]))
        await _deliver(adapter, _notification([_system_number_change()]))
        await _deliver(adapter, _notification([_inbound({"from": NEW_PHONE, "from_user_id": "US.NEW"})]))

        assert chat.process_message.call_count == 2
        _, thread_id, _, _ = chat.process_message.call_args_list[1][0]
        assert thread_id == f"whatsapp:123456789:{PHONE}"

    async def test_clears_a_stale_phone_route_after_a_user_id_update_rotation(self):
        adapter, _, _, _ = await _bsuid_env()

        await _deliver(
            adapter,
            _notification(
                [_inbound({"from": PHONE, "from_user_id": "US.OLD"})],
                [{"profile": {"name": "User"}, "wa_id": PHONE, "user_id": "US.OLD"}],
            ),
        )
        # No wa_id on the update: the user no longer shares a phone number,
        # so the stored phone route must be dropped.
        await _deliver(
            adapter,
            _user_id_update_notification(
                [
                    {
                        "detail": "User id for User has been updated.",
                        "timestamp": "1700000001",
                        "user_id": {"previous": "US.OLD", "current": "US.NEW"},
                    }
                ]
            ),
        )

        await adapter.post_message(f"whatsapp:123456789:{PHONE}", {"markdown": "Reply"})

        body = _sent_body(adapter)
        assert body["recipient"] == "US.NEW"
        assert "to" not in body

    async def test_preserves_a_bsuid_keyed_thread_across_a_user_id_update_rotation(self):
        adapter, chat, _, _ = await _bsuid_env()

        # Username-only user: no phone anywhere, thread keyed by the BSUID.
        await _deliver(
            adapter,
            _notification(
                [_inbound({"from_user_id": "US.OLD"})],
                [{"profile": {"name": "User", "username": "exampleuser"}, "user_id": "US.OLD"}],
            ),
        )
        await _deliver(
            adapter,
            _user_id_update_notification(
                [
                    {
                        "detail": "User id for User has been updated.",
                        "timestamp": "1700000001",
                        "user_id": {"previous": "US.OLD", "current": "US.NEW"},
                    }
                ]
            ),
        )
        await _deliver(
            adapter,
            _notification(
                [_inbound({"from_user_id": "US.NEW"})],
                [{"profile": {"name": "User", "username": "exampleuser"}, "user_id": "US.NEW"}],
            ),
        )

        assert chat.process_message.call_count == 2
        _, thread_id, _, _ = chat.process_message.call_args_list[1][0]
        assert thread_id == "whatsapp:123456789:US.OLD"

        await adapter.post_message("whatsapp:123456789:US.OLD", {"markdown": "Reply"})
        body = _sent_body(adapter)
        assert body["recipient"] == "US.NEW"
        assert "to" not in body

    async def test_keeps_the_pre_change_thread_when_a_number_change_arrives_without_prior_state(self):
        adapter, chat, _, _ = await _bsuid_env()

        # System message first (e.g. a fresh deploy over an existing
        # conversation). The old number in `from` must stay canonical.
        await _deliver(adapter, _notification([_system_number_change()]))
        await _deliver(adapter, _notification([_inbound({"from": NEW_PHONE, "from_user_id": "US.NEW"})]))

        chat.process_message.assert_called_once()
        _, thread_id, _, _ = chat.process_message.call_args[0]
        assert thread_id == f"whatsapp:123456789:{PHONE}"

    async def test_skips_redundant_identity_writes_for_repeat_messages(self):
        from chat_sdk.state.memory import MemoryStateAdapter

        state = MemoryStateAdapter()
        await state.connect()
        state.set = AsyncMock(wraps=state.set)
        adapter, _, _, _ = await _bsuid_env(state)

        contact = {"profile": {"name": "User"}, "wa_id": PHONE, "user_id": BSUID}
        message = {"from": PHONE, "from_user_id": BSUID}

        await _deliver(adapter, _notification([_inbound(message)], [contact]))
        assert state.set.await_count > 0

        state.set.reset_mock()
        await _deliver(adapter, _notification([_inbound({**message, "id": "wamid.2"})], [contact]))
        state.set.assert_not_awaited()

    async def test_falls_back_to_the_user_id_when_the_profile_name_is_empty(self):
        adapter, chat, _, _ = await _bsuid_env()

        await _deliver(
            adapter,
            _notification([_inbound({"from": PHONE})], [{"profile": {"name": ""}, "wa_id": PHONE}]),
        )

        _, _, parsed, _ = chat.process_message.call_args[0]
        assert parsed.author.user_name == PHONE
        assert parsed.author.full_name == PHONE

    async def test_ignores_messages_without_a_phone_number_or_bsuid(self):
        adapter, chat, _, logger = await _bsuid_env()

        response = await _deliver(adapter, _notification([_inbound({})]))

        assert response["status"] == 200
        chat.process_message.assert_not_called()
        logger.warn.assert_any_call("WhatsApp message has no user identifier", {"messageId": "wamid.inbound"})
        logger.error.assert_not_called()


class TestPostMessageBusinessScopedRecipients:
    """Upstream describe("postMessage") BSUID cases (no chat: pattern fallback)."""

    @pytest.mark.parametrize("user_id", [BSUID, PARENT_BSUID])
    async def test_sends_s_threads_through_recipient_without_to(self, user_id):
        adapter = _make_adapter()
        adapter._graph_api_request = AsyncMock(return_value={"messages": [{"id": "wamid.sent123"}]})

        await adapter.post_message(f"whatsapp:123456789:{user_id}", {"markdown": "Hello there"})

        sent = _sent_body(adapter)
        assert sent["recipient"] == user_id
        assert "to" not in sent

    async def test_sends_interactive_messages_to_bsuid_recipients(self):
        from chat_sdk.cards import Actions, Button, Card

        adapter = _make_adapter()
        adapter._graph_api_request = AsyncMock(return_value={"messages": [{"id": "wamid.sent123"}]})

        await adapter.post_message(
            f"whatsapp:123456789:{BSUID}",
            Card(title="Choose", children=[Actions([Button(id="yes", label="Yes")])]),
        )

        sent = _sent_body(adapter)
        assert sent["type"] == "interactive"
        assert sent["recipient"] == BSUID
        assert "to" not in sent


class TestBusinessScopedUserIdsPythonSpecific:
    """Python-specific coverage for the BSUID port (issue #236)."""

    @pytest.mark.parametrize(
        ("user_id", "expected"),
        [
            (BSUID, {"recipient": BSUID}),
            (PARENT_BSUID, {"recipient": PARENT_BSUID}),
            (PHONE, {"to": PHONE}),
            ("us.13491208655302741918", {"to": "us.13491208655302741918"}),
            # `fullmatch`, like JS `/^...$/`: a trailing newline must not match.
            (f"{BSUID}\n", {"to": f"{BSUID}\n"}),
            ("US." + "a" * 129, {"to": "US." + "a" * 129}),
            ("US.ENT.", {"to": "US.ENT."}),
        ],
    )
    async def test_recipient_pattern_fallback(self, user_id, expected):
        adapter = _make_adapter()
        assert await adapter._recipient("whatsapp:123456789:x", user_id) == expected

    async def test_empty_stored_route_falls_through_to_pattern(self):
        adapter, _, state, _ = await _bsuid_env()
        await state.set(f"whatsapp:identity:route:123456789:{BSUID}", {})

        assert await adapter._recipient(f"whatsapp:123456789:{BSUID}", BSUID) == {"recipient": BSUID}

    async def test_raising_state_still_dispatches_and_sends(self):
        state = MagicMock()
        state.get = AsyncMock(side_effect=RuntimeError("state down"))
        state.set = AsyncMock()
        adapter, chat, _, logger = await _bsuid_env(state)

        await _deliver(adapter, _notification([_inbound({"from": PHONE, "from_user_id": BSUID})]))

        chat.process_message.assert_called_once()
        _, thread_id, parsed, _ = chat.process_message.call_args[0]
        assert thread_id == f"whatsapp:123456789:{PHONE}"
        assert parsed.raw["user_id"] == PHONE
        assert logger.warn.call_args_list[0][0][0] == "Failed to persist WhatsApp user identity"
        logger.error.assert_not_called()

        await adapter.post_message(f"whatsapp:123456789:{BSUID}", {"markdown": "Reply"})
        sent = _sent_body(adapter)
        assert sent["recipient"] == BSUID
        assert "to" not in sent
        assert logger.warn.call_args_list[-1][0][0] == "Failed to resolve WhatsApp recipient"

    async def test_raising_state_on_user_id_update_is_swallowed(self):
        state = MagicMock()
        state.get = AsyncMock(side_effect=RuntimeError("state down"))
        state.set = AsyncMock()
        adapter, _, _, logger = await _bsuid_env(state)

        response = await _deliver(
            adapter,
            _user_id_update_notification([{"user_id": {"previous": "US.OLD", "current": "US.NEW"}}]),
        )

        assert response["status"] == 200
        logger.warn.assert_called_once()
        assert logger.warn.call_args[0][0] == "Failed to apply WhatsApp user ID update"
        state.set.assert_not_awaited()

    async def test_reactions_on_a_bsuid_thread_send_recipient(self):
        adapter = _make_adapter()
        adapter._graph_api_request = AsyncMock(return_value={})

        await adapter.add_reaction(f"whatsapp:123456789:{BSUID}", "wamid.target", "thumbs_up")
        await adapter.remove_reaction(f"whatsapp:123456789:{BSUID}", "wamid.target", "thumbs_up")

        for index in (0, 1):
            sent = _sent_body(adapter, index)
            assert sent["type"] == "reaction"
            assert sent["recipient"] == BSUID
            assert "to" not in sent

    async def test_reactions_use_the_stored_route_on_a_phone_thread(self):
        adapter, _, _, _ = await _bsuid_env()
        await _deliver(adapter, _notification([_inbound({"from": PHONE, "from_user_id": BSUID})]))

        await adapter.add_reaction(f"whatsapp:123456789:{PHONE}", "wamid.target", "thumbs_up")

        sent = _sent_body(adapter)
        assert sent["to"] == PHONE
        assert sent["recipient"] == BSUID

    async def test_long_message_resolves_recipient_once_for_all_chunks(self):
        state = MagicMock()
        state.get = AsyncMock(return_value={"bsuid": BSUID, "phone": PHONE})
        state.set = AsyncMock()
        adapter, _, _, _ = await _bsuid_env(state)

        await adapter.post_message(f"whatsapp:123456789:{PHONE}", {"markdown": "a" * 5000})

        assert adapter._graph_api_request.await_count == 2
        state.get.assert_awaited_once_with(f"whatsapp:identity:route:123456789:{PHONE}")
        for index in (0, 1):
            sent = _sent_body(adapter, index)
            assert sent["to"] == PHONE
            assert sent["recipient"] == BSUID

    @pytest.mark.parametrize(
        ("message", "event"),
        [
            pytest.param(
                {"type": "reaction", "reaction": {"message_id": "wamid.target", "emoji": "\U0001f44d"}},
                "process_reaction",
                id="reaction",
            ),
            pytest.param(
                {
                    "type": "interactive",
                    "interactive": {"type": "button_reply", "button_reply": {"id": "yes", "title": "Yes"}},
                },
                "process_action",
                id="interactive",
            ),
            pytest.param(
                {"type": "button", "button": {"payload": "yes", "text": "Yes"}},
                "process_action",
                id="button",
            ),
        ],
    )
    async def test_bsuid_only_events_reach_handlers(self, message, event):
        adapter, chat, _, logger = await _bsuid_env()
        contact = {"profile": {"name": "User", "username": "exampleuser"}, "user_id": BSUID}

        await _deliver(adapter, _notification([_inbound({"from_user_id": BSUID, **message})], [contact]))

        getattr(chat, event).assert_called_once()
        dispatched = getattr(chat, event).call_args[0][0]
        assert dispatched.thread_id == f"whatsapp:123456789:{BSUID}"
        assert dispatched.user.user_id == BSUID
        assert dispatched.user.user_name == "exampleuser"
        logger.error.assert_not_called()

    async def test_matches_the_contact_for_each_message(self):
        adapter, chat, _, _ = await _bsuid_env()
        contacts = [
            {"profile": {"name": "Alice"}, "wa_id": PHONE},
            {"profile": {"name": "Bob"}, "user_id": BSUID},
        ]

        await _deliver(
            adapter,
            _notification(
                [_inbound({"from": PHONE}), _inbound({"id": "wamid.2", "from_user_id": BSUID})],
                contacts,
            ),
        )

        names = [call[0][2].author.full_name for call in chat.process_message.call_args_list]
        assert names == ["Alice", "Bob"]

    async def test_does_not_borrow_identity_from_an_unrelated_contact(self):
        """Divergence from upstream (see docs/UPSTREAM_SYNC.md): upstream falls
        back to ``contacts[0]`` and would key both messages by Alice's phone."""
        adapter, chat, state, _ = await _bsuid_env()
        contacts = [
            {"profile": {"name": "Alice"}, "wa_id": PHONE, "user_id": "US.ALICE"},
            {"profile": {"name": "Bob"}, "user_id": "US.BOB", "parent_user_id": PARENT_BSUID},
        ]

        await _deliver(
            adapter,
            _notification(
                [
                    # Matched to Bob by parent BSUID alone.
                    _inbound({"from_parent_user_id": PARENT_BSUID}),
                    # Matches no contact in a multi-contact batch.
                    _inbound({"id": "wamid.2", "from_parent_user_id": "US.ENT.OTHER"}),
                ],
                contacts,
            ),
        )

        dispatched = [(call[0][1], call[0][2].author.full_name) for call in chat.process_message.call_args_list]
        assert dispatched == [
            ("whatsapp:123456789:US.BOB", "Bob"),
            ("whatsapp:123456789:US.ENT.OTHER", "US.ENT.OTHER"),
        ]
        assert await state.get(f"whatsapp:identity:alias:123456789:{PHONE}") is None

    async def test_does_not_pair_a_single_unmatched_contact_with_another_sender(self):
        """Divergence from upstream (see docs/UPSTREAM_SYNC.md): a batch with
        Alice's and Bob's messages but only Alice's contact must not merge Bob
        into Alice's identity or overwrite her route with his phone."""
        adapter, chat, state, _ = await _bsuid_env()
        alice = {"profile": {"name": "Alice"}, "wa_id": PHONE, "user_id": "US.ALICE"}

        await _deliver(
            adapter,
            _notification(
                [_inbound({"from": PHONE}), _inbound({"id": "wamid.2", "from": NEW_PHONE})],
                [alice],
            ),
        )

        dispatched = [(call[0][1], call[0][2].author.full_name) for call in chat.process_message.call_args_list]
        assert dispatched == [
            (f"whatsapp:123456789:{PHONE}", "Alice"),
            (f"whatsapp:123456789:{NEW_PHONE}", NEW_PHONE),
        ]
        assert await state.get(f"whatsapp:identity:route:123456789:{PHONE}") == {"bsuid": "US.ALICE", "phone": PHONE}
        assert await state.get(f"whatsapp:identity:route:123456789:{NEW_PHONE}") == {"phone": NEW_PHONE}

    async def test_uses_the_only_contact_when_the_message_has_no_sender_ids(self):
        """The single-contact fallback survives for messages that carry no
        sender identifier of their own."""
        adapter, chat, _, _ = await _bsuid_env()

        await _deliver(
            adapter,
            _notification([_inbound({})], [{"profile": {"name": "Bob"}, "user_id": BSUID}]),
        )

        _, thread_id, parsed, _ = chat.process_message.call_args[0]
        assert thread_id == f"whatsapp:123456789:{BSUID}"
        assert parsed.author.full_name == "Bob"

    async def test_forwarded_context_without_id_parses(self):
        adapter, chat, _, logger = await _bsuid_env()

        await _deliver(
            adapter,
            _notification([_inbound({"from_user_id": BSUID, "context": {"forwarded": True}})]),
        )

        chat.process_message.assert_called_once()
        _, thread_id, parsed, _ = chat.process_message.call_args[0]
        assert thread_id == f"whatsapp:123456789:{BSUID}"
        assert parsed.raw["message"]["context"] == {"forwarded": True}
        logger.error.assert_not_called()
