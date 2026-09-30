"""Webhook / message log hygiene across adapters and ``Chat`` (issue #187).

Python-specific coverage for the upstream log-hygiene hardening
(``fc7df9c4`` GitHub, ``f485255b`` GChat/Slack/Teams/``chat.ts``) plus the
Python-ahead divergences recorded in ``docs/UPSTREAM_SYNC.md``:

- WhatsApp / Linear log request-shape metadata instead of raw-body previews.
- GChat "message event" / "Pub/Sub parsed message" and the slash-command
  debug logs (``Chat``, Slack, Discord) carry no message text or display
  names — only a ``textLength``.

Every payload carries the shared sentinels from ``tests/_log_capture.py``;
none may reach any logger call. The GitHub ports of upstream's own tests live
in ``tests/test_github_webhook.py``; the Teams bridge test lives in
``tests/test_teams_bridge.py``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlencode

import pytest

from chat_sdk.adapters.discord.adapter import DiscordAdapter
from chat_sdk.adapters.discord.types import DiscordAdapterConfig
from chat_sdk.adapters.google_chat.adapter import GoogleChatAdapter
from chat_sdk.adapters.google_chat.types import GoogleChatAdapterConfig, ServiceAccountCredentials
from chat_sdk.adapters.linear.adapter import LinearAdapter
from chat_sdk.adapters.linear.types import LinearAdapterAPIKeyConfig
from chat_sdk.adapters.slack.adapter import SlackAdapter
from chat_sdk.adapters.slack.types import SlackAdapterConfig
from chat_sdk.adapters.whatsapp.adapter import WhatsAppAdapter
from chat_sdk.adapters.whatsapp.types import WhatsAppAdapterConfig
from chat_sdk.chat import Chat
from chat_sdk.shared.log_utils import utf8_byte_length
from chat_sdk.testing import (
    MockLogger,
    create_mock_adapter,
    create_mock_state,
    create_test_message,
)
from chat_sdk.types import Author, ChatConfig, SlashCommandEvent
from tests._log_capture import MULTIBYTE_TEXT, assert_body_not_logged, logged_contexts, stringify_logger_calls

# Message text carrying every sentinel plus multi-byte characters.
SENTINEL_TEXT = f"Authorization: Bearer secret-token secret-access-token secret-refresh-token {MULTIBYTE_TEXT}"
SENTINEL_NAME = "customer-team-slug"


def _byte_len(body: str) -> int:
    encoded = len(body.encode("utf-8"))
    # Guard the fixture itself: a byte-length assertion only proves anything
    # when the payload's byte and code-point lengths differ.
    assert encoded != len(body)
    return encoded


class _TextRequest:
    """Request double exposing ``text()`` + ``headers`` (+ ``method``/``url``)."""

    def __init__(self, body: str, headers: dict[str, str] | None = None, method: str = "POST"):
        self._body = body
        self.headers = headers or {}
        self.method = method
        self.url = "https://example.com/webhook"

    async def text(self) -> str:
        return self._body


def _hmac_hex(secret: str, body: str) -> str:
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# utf8_byte_length
# ---------------------------------------------------------------------------


class TestUtf8ByteLength:
    def test_counts_utf8_bytes_not_code_points(self):
        # "é" = 2 bytes, "☃" = 3 bytes, "🚀" = 4 bytes.
        assert utf8_byte_length("é☃🚀") == 9

    def test_none_and_empty_are_zero(self):
        assert utf8_byte_length(None) == 0
        assert utf8_byte_length("") == 0

    def test_bytes_are_measured_directly(self):
        assert utf8_byte_length(b"\xc3\xa9") == 2
        assert utf8_byte_length(bytearray(b"abc")) == 3

    def test_lone_surrogate_does_not_raise(self):
        # A lone surrogate cannot be strict-UTF-8 encoded; computing a log
        # field must never turn a malformed request into a 500. Counted as 3
        # bytes, like Node's U+FFFD substitution in ``Buffer.byteLength``.
        assert utf8_byte_length("a\ud800b") == 5


# ---------------------------------------------------------------------------
# Google Chat
# ---------------------------------------------------------------------------


def _make_gchat_adapter(logger: MockLogger) -> GoogleChatAdapter:
    return GoogleChatAdapter(
        GoogleChatAdapterConfig(
            credentials=ServiceAccountCredentials(
                client_email="test@test.iam.gserviceaccount.com",
                private_key="-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----\n",
                project_id="test-project",
            ),
            disable_signature_verification=True,
            logger=logger,
        )
    )


def _make_gchat_chat() -> MagicMock:
    storage: dict[str, Any] = {}
    state = MagicMock()
    state.get = AsyncMock(side_effect=lambda k: storage.get(k))
    state.set = AsyncMock(side_effect=lambda k, v, *a, **kw: storage.__setitem__(k, v))
    state.delete = AsyncMock(side_effect=lambda k: storage.pop(k, None))
    chat = MagicMock()
    chat.get_state = MagicMock(return_value=state)
    chat.process_message = MagicMock()
    return chat


class TestGoogleChatLogHygiene:
    @pytest.mark.asyncio
    async def test_message_event_logs_body_length_and_text_length_only(self):
        logger = MockLogger()
        adapter = _make_gchat_adapter(logger)
        await adapter.initialize(_make_gchat_chat())
        body = json.dumps(
            {
                "chat": {
                    "messagePayload": {
                        "space": {"name": "spaces/ABC123", "type": "ROOM"},
                        "message": {
                            "name": "spaces/ABC123/messages/msg1",
                            "sender": {"name": "users/100", "displayName": SENTINEL_NAME, "type": "HUMAN"},
                            "text": SENTINEL_TEXT,
                            "createTime": "2024-01-01T00:00:00Z",
                        },
                    },
                },
            },
            ensure_ascii=False,
        )

        response = await adapter.handle_webhook(_TextRequest(body))

        assert response["status"] == 200
        assert ("GChat webhook received", {"bodyLength": _byte_len(body)}) in logger.debug.calls
        assert (
            "message event",
            {"space": "spaces/ABC123", "textLength": len(SENTINEL_TEXT)},
        ) in logger.debug.calls
        assert_body_not_logged(logger, body)

    @pytest.mark.asyncio
    async def test_message_event_without_text_logs_zero_text_length(self):
        # Attachment-only messages carry no ``text`` key; the log line must
        # not raise (``len(None)``) and turn a valid webhook into a 500.
        logger = MockLogger()
        adapter = _make_gchat_adapter(logger)
        chat = _make_gchat_chat()
        await adapter.initialize(chat)
        body = json.dumps(
            {
                "chat": {
                    "messagePayload": {
                        "space": {"name": "spaces/A", "type": "ROOM"},
                        "message": {
                            "name": "spaces/A/messages/msg2",
                            "sender": {"name": "users/100", "displayName": SENTINEL_NAME, "type": "HUMAN"},
                            "attachment": [
                                {
                                    "name": "spaces/A/messages/msg2/attachments/a1",
                                    "contentName": "photo.png",
                                    "contentType": "image/png",
                                }
                            ],
                            "createTime": "2024-01-01T00:00:00Z",
                        },
                    },
                },
            }
        )

        response = await adapter.handle_webhook(_TextRequest(body))

        assert response["status"] == 200
        assert logged_contexts(logger.debug, "message event") == [{"space": "spaces/A", "textLength": 0}]
        chat.process_message.assert_called_once()
        assert_body_not_logged(logger, body)

    @pytest.mark.asyncio
    async def test_pubsub_parsed_message_log_has_no_text_or_author(self):
        logger = MockLogger()
        adapter = _make_gchat_adapter(logger)
        chat = _make_gchat_chat()
        await adapter.initialize(chat)
        notification = {
            "message": {
                "name": "spaces/ABC123/messages/msg1",
                "sender": {"name": "users/100", "displayName": SENTINEL_NAME, "type": "HUMAN"},
                "text": SENTINEL_TEXT,
                "createTime": "2024-01-01T00:00:00Z",
            },
        }
        body = json.dumps(
            {
                "message": {
                    "data": base64.b64encode(json.dumps(notification).encode()).decode(),
                    "messageId": "pubsub-msg-1",
                    "publishTime": "2024-01-01T00:00:00Z",
                    "attributes": {
                        "ce-type": "google.workspace.chat.message.v1.created",
                        "ce-subject": "//chat.googleapis.com/spaces/ABC123",
                        "ce-time": "2024-01-01T00:00:00Z",
                    },
                },
                "subscription": "projects/test/subscriptions/test-sub",
            }
        )

        response = await adapter.handle_webhook(_TextRequest(body))
        assert response["status"] == 200
        # The adapter hands Chat a lazy factory; run it to reach the parse log.
        factory = chat.process_message.call_args[0][2]
        parsed = await factory()

        assert parsed.text == SENTINEL_TEXT
        pubsub_logs = logged_contexts(logger.debug, "Pub/Sub parsed message")
        assert pubsub_logs == [
            {"threadId": parsed.thread_id, "messageId": parsed.id, "isBot": False, "isMe": False},
        ]
        assert_body_not_logged(logger, body)
        # The Pub/Sub envelope is base64, so also check the decoded content.
        assert SENTINEL_TEXT not in stringify_logger_calls(logger)


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------

SLACK_SECRET = "test-signing-secret"


def _slack_request(body: str, *, content_type: str = "application/json", valid: bool = True) -> _TextRequest:
    ts = str(int(time.time()))
    sig = "v0=" + (_hmac_hex(SLACK_SECRET, f"v0:{ts}:{body}") if valid else "invalid")
    return _TextRequest(
        body,
        {"x-slack-request-timestamp": ts, "x-slack-signature": sig, "content-type": content_type},
    )


def _make_slack_adapter(logger: MockLogger) -> SlackAdapter:
    return SlackAdapter(SlackAdapterConfig(signing_secret=SLACK_SECRET, bot_token="xoxb-test-token", logger=logger))


def _slack_payload() -> str:
    return json.dumps(
        {
            "type": "url_verification",
            "challenge": "challenge-value",
            "access_token": "secret-access-token",
            "note": SENTINEL_TEXT,
            "team_domain": SENTINEL_NAME,
        },
        ensure_ascii=False,
    )


class TestSlackLogHygiene:
    @pytest.mark.asyncio
    async def test_invalid_signature_logs_nothing_about_the_body(self):
        logger = MockLogger()
        adapter = _make_slack_adapter(logger)
        body = _slack_payload()

        response = await adapter.handle_webhook(_slack_request(body, valid=False))

        assert response["status"] == 401
        # Nothing about the request is logged before verification succeeds.
        assert logged_contexts(logger.debug, "Slack webhook received") == []
        assert_body_not_logged(logger, body)

    @pytest.mark.asyncio
    async def test_verified_request_logs_body_length_only(self):
        logger = MockLogger()
        adapter = _make_slack_adapter(logger)
        body = _slack_payload()

        response = await adapter.handle_webhook(_slack_request(body))

        assert response["status"] == 200
        assert ("Slack webhook received", {"bodyLength": _byte_len(body)}) in logger.debug.calls
        assert_body_not_logged(logger, body)

    @pytest.mark.asyncio
    async def test_slash_command_log_has_text_length_not_text(self):
        logger = MockLogger()
        adapter = _make_slack_adapter(logger)
        chat = MagicMock()
        chat.process_slash_command = MagicMock()
        await adapter.initialize(chat)
        adapter._lookup_user = AsyncMock(return_value={"display_name": "u", "real_name": "u"})
        body = urlencode(
            {"command": "/deploy", "text": SENTINEL_TEXT, "user_id": "U123", "channel_id": "C456"},
        )

        response = await adapter.handle_webhook(_slack_request(body, content_type="application/x-www-form-urlencoded"))

        assert response["status"] == 200
        assert chat.process_slash_command.call_args[0][0].text == SENTINEL_TEXT
        assert (
            "Processing Slack slash command",
            {"command": "/deploy", "textLength": len(SENTINEL_TEXT), "userId": "U123", "channelId": "C456"},
        ) in logger.debug.calls
        assert_body_not_logged(logger, body)
        assert SENTINEL_TEXT not in stringify_logger_calls(logger)


# ---------------------------------------------------------------------------
# Linear (Python-only raw-body log removed — restores upstream parity)
# ---------------------------------------------------------------------------

LINEAR_SECRET = "test-webhook-secret"


def _make_linear_adapter(logger: MockLogger) -> LinearAdapter:
    return LinearAdapter(
        LinearAdapterAPIKeyConfig(
            api_key="test-api-key",
            webhook_secret=LINEAR_SECRET,
            user_name="test-bot",
            logger=logger,
        )
    )


def _linear_request(body: str, signature: str) -> _TextRequest:
    return _TextRequest(body, {"content-type": "application/json", "linear-signature": signature})


class TestLinearLogHygiene:
    @pytest.mark.asyncio
    async def test_invalid_signature_logs_no_body(self):
        logger = MockLogger()
        adapter = _make_linear_adapter(logger)
        body = json.dumps({"type": "Comment", "data": {"body": SENTINEL_TEXT, "user": SENTINEL_NAME}})

        response = await adapter.handle_webhook(_linear_request(body, "bad-signature"))

        assert response["status"] == 401
        assert_body_not_logged(logger, body)

    @pytest.mark.asyncio
    async def test_invalid_json_logs_byte_length_and_content_type(self):
        logger = MockLogger()
        adapter = _make_linear_adapter(logger)
        body = f"not-json {SENTINEL_TEXT} {SENTINEL_NAME} access_token refresh_token"

        response = await adapter.handle_webhook(_linear_request(body, _hmac_hex(LINEAR_SECRET, body)))

        assert response["status"] == 400
        assert (
            "Linear webhook invalid JSON",
            {"bodyBytes": _byte_len(body), "contentType": "application/json"},
        ) in logger.error.calls
        assert_body_not_logged(logger, body)


# ---------------------------------------------------------------------------
# WhatsApp (divergence: upstream 4.41.1 still logs raw-body previews)
# ---------------------------------------------------------------------------

WHATSAPP_SECRET = "test-secret"


def _make_whatsapp_adapter(logger: MockLogger) -> WhatsAppAdapter:
    return WhatsAppAdapter(
        WhatsAppAdapterConfig(
            access_token="test-token",
            app_secret=WHATSAPP_SECRET,
            phone_number_id="123456789",
            verify_token="test-verify-token",
            user_name="test-bot",
            logger=logger,
        )
    )


def _whatsapp_request(body: str, signature: str) -> _TextRequest:
    return _TextRequest(body, {"content-type": "application/json", "x-hub-signature-256": signature})


def _whatsapp_payload() -> str:
    return json.dumps(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "metadata": {"phone_number_id": "123456789"},
                                "contacts": [{"profile": {"name": SENTINEL_NAME}, "wa_id": "15551234567"}],
                                "messages": [
                                    {
                                        "id": "wamid.xxx",
                                        "from": "15551234567",
                                        "timestamp": "1700000000",
                                        "type": "text",
                                        "text": {"body": SENTINEL_TEXT},
                                    }
                                ],
                            },
                        }
                    ]
                }
            ]
        },
        ensure_ascii=False,
    )


class TestWhatsAppLogHygiene:
    """Regression guard for a divergence from upstream — see docs/UPSTREAM_SYNC.md."""

    @pytest.mark.asyncio
    async def test_invalid_signature_logs_no_body(self):
        logger = MockLogger()
        adapter = _make_whatsapp_adapter(logger)
        body = _whatsapp_payload()

        response = await adapter.handle_webhook(_whatsapp_request(body, "sha256=bad"))

        assert response["status"] == 401
        assert_body_not_logged(logger, body)

    @pytest.mark.asyncio
    async def test_valid_webhook_logs_no_body(self):
        logger = MockLogger()
        adapter = _make_whatsapp_adapter(logger)
        body = _whatsapp_payload()

        response = await adapter.handle_webhook(_whatsapp_request(body, "sha256=" + _hmac_hex(WHATSAPP_SECRET, body)))

        assert response["status"] == 200
        assert_body_not_logged(logger, body)

    @pytest.mark.asyncio
    async def test_invalid_json_logs_byte_length_and_content_type(self):
        logger = MockLogger()
        adapter = _make_whatsapp_adapter(logger)
        body = f"not-json {SENTINEL_TEXT} {SENTINEL_NAME} access_token refresh_token"

        response = await adapter.handle_webhook(_whatsapp_request(body, "sha256=" + _hmac_hex(WHATSAPP_SECRET, body)))

        assert response["status"] == 400
        assert (
            "WhatsApp webhook invalid JSON",
            {"bodyBytes": _byte_len(body), "contentType": "application/json"},
        ) in logger.error.calls
        assert_body_not_logged(logger, body)


# ---------------------------------------------------------------------------
# Discord slash command
# ---------------------------------------------------------------------------


class TestDiscordLogHygiene:
    def test_slash_command_log_has_text_length_not_text(self):
        logger = MockLogger()
        adapter = DiscordAdapter(
            DiscordAdapterConfig(
                bot_token="test-token",
                public_key="a" * 64,
                application_id="test-app-id",
                logger=logger,
            )
        )
        chat = MagicMock()
        chat.process_slash_command = MagicMock()
        adapter._chat = chat
        interaction = {
            "id": "interaction123",
            "application_id": "test-app-id",
            "token": "interaction-token",
            "type": 2,
            "guild_id": "guild123",
            "channel_id": "channel456",
            "channel": {"id": "channel456", "type": 0},
            "user": {"id": "user789", "username": SENTINEL_NAME, "global_name": SENTINEL_NAME},
            "data": {"name": "ask", "type": 1, "options": [{"name": "q", "type": 3, "value": SENTINEL_TEXT}]},
        }

        adapter._handle_application_command_interaction(interaction, None)

        event = chat.process_slash_command.call_args[0][0]
        assert event.text == SENTINEL_TEXT
        logged = logged_contexts(logger.debug, "Processing Discord slash command")
        assert logged == [
            {
                "command": "/ask",
                "textLength": len(SENTINEL_TEXT),
                "userId": "user789",
                "channelId": "discord:guild123:channel456",
            }
        ]
        assert SENTINEL_TEXT not in stringify_logger_calls(logger)


# ---------------------------------------------------------------------------
# Chat core
# ---------------------------------------------------------------------------


async def _init_chat() -> tuple[Chat, MockLogger, Any]:
    logger = MockLogger()
    adapter = create_mock_adapter("slack")
    chat = Chat(ChatConfig(user_name="testbot", adapters={"slack": adapter}, state=create_mock_state(), logger=logger))
    await chat.webhooks["slack"]("request")
    return chat, logger, adapter


class TestChatLogHygiene:
    @pytest.mark.asyncio
    async def test_incoming_message_log_has_no_author(self):
        chat, logger, adapter = await _init_chat()
        author = Author(
            user_id="U123",
            user_name=SENTINEL_NAME,
            full_name=SENTINEL_NAME,
            is_bot=True,
            is_me=False,
        )
        msg = create_test_message("msg-1", SENTINEL_TEXT, author=author)

        await chat.handle_incoming_message(adapter, "slack:C123:1234.5678", msg)

        incoming = logged_contexts(logger.debug, "Incoming message")
        # Upstream key set (f485255b): adapter, threadId, messageId, isBot, isMe.
        assert incoming == [
            {
                "adapter": "slack",
                "thread_id": "slack:C123:1234.5678",
                "message_id": "msg-1",
                "is_bot": True,
                "is_me": False,
            }
        ]
        assert SENTINEL_NAME not in stringify_logger_calls(logger)

    @pytest.mark.asyncio
    async def test_slash_command_log_has_text_length_not_text(self):
        chat, logger, adapter = await _init_chat()
        event = SlashCommandEvent(
            command="/deploy",
            text=SENTINEL_TEXT,
            user=Author(user_id="U123", user_name="alice", full_name="Alice", is_bot=False, is_me=False),
            adapter=adapter,
            channel=None,
            raw={"channel_id": "C456"},
        )
        object.__setattr__(event, "channel_id", "slack:C456")

        await chat._handle_slash_command_event(event)

        incoming = logged_contexts(logger.debug, "Incoming slash command")
        assert incoming == [
            {"adapter": "slack", "command": "/deploy", "textLength": len(SENTINEL_TEXT), "user": "alice"},
        ]
        assert SENTINEL_TEXT not in stringify_logger_calls(logger)
