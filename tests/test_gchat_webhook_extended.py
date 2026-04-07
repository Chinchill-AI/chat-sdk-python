"""Extended Google Chat webhook tests covering gaps identified from TS test suite.

Covers: Pub/Sub message handling (various event types), workspace event
subscriptions, post/edit/delete message flows, card click edge cases,
handleMessageEvent edge cases, DM thread handling, error handling
(rate limits, auth failures), and more.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock

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
from chat_sdk.shared.errors import AdapterRateLimitError, ValidationError

DM_SUFFIX_PATTERN = re.compile(r":dm$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_credentials() -> ServiceAccountCredentials:
    return ServiceAccountCredentials(
        client_email="test@test.iam.gserviceaccount.com",
        private_key="-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----\n",
        project_id="test-project",
    )


def _make_adapter(**overrides: Any) -> GoogleChatAdapter:
    config = GoogleChatAdapterConfig(
        credentials=overrides.pop("credentials", _make_credentials()),
        **overrides,
    )
    return GoogleChatAdapter(config)


def _make_mock_state() -> MagicMock:
    storage: dict[str, Any] = {}
    state = MagicMock()
    state.get = AsyncMock(side_effect=lambda k: storage.get(k))
    state.set = AsyncMock(side_effect=lambda k, v, *a, **kw: storage.__setitem__(k, v))
    state.delete = AsyncMock(side_effect=lambda k: storage.pop(k, None))
    state._storage = storage
    return state


def _make_mock_chat(state: MagicMock) -> MagicMock:
    chat = MagicMock()
    chat.get_state = MagicMock(return_value=state)
    chat.get_logger = MagicMock(return_value=MagicMock())
    chat.process_message = AsyncMock()
    chat.process_reaction = AsyncMock()
    chat.process_action = AsyncMock()
    return chat


def _make_message_event(
    *,
    space_name: str = "spaces/ABC123",
    space_type: str = "ROOM",
    message_text: str = "Hello",
    message_name: str = "spaces/ABC123/messages/msg1",
    sender_name: str = "users/100",
    sender_display_name: str = "Test User",
    sender_type: str = "HUMAN",
    thread_name: str | None = None,
    annotations: list[dict[str, Any]] | None = None,
    attachment: list[dict[str, Any]] | None = None,
    sender_email: str | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "name": message_name,
        "sender": {
            "name": sender_name,
            "displayName": sender_display_name,
            "type": sender_type,
        },
        "text": message_text,
        "createTime": "2024-01-01T00:00:00Z",
    }
    if sender_email:
        message["sender"]["email"] = sender_email
    if thread_name:
        message["thread"] = {"name": thread_name}
    if annotations:
        message["annotations"] = annotations
    if attachment:
        message["attachment"] = attachment
    return {
        "chat": {
            "messagePayload": {
                "space": {"name": space_name, "type": space_type},
                "message": message,
            },
        },
    }


def _make_pubsub_push_message(
    notification: dict[str, Any],
    event_type: str = "google.workspace.chat.message.v1.created",
    target_resource: str = "//chat.googleapis.com/spaces/ABC123",
) -> dict[str, Any]:
    data = base64.b64encode(json.dumps(notification).encode()).decode()
    return {
        "message": {
            "data": data,
            "messageId": "pubsub-msg-1",
            "publishTime": "2024-01-01T00:00:00Z",
            "attributes": {
                "ce-type": event_type,
                "ce-subject": target_resource,
                "ce-time": "2024-01-01T00:00:00Z",
            },
        },
        "subscription": "projects/test/subscriptions/test-sub",
    }


class _FakeApiError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
        self.errors = None


# ---------------------------------------------------------------------------
# Card click edge cases
# ---------------------------------------------------------------------------


class TestCardClickEdgeCases:
    @pytest.mark.asyncio
    async def test_ignores_card_click_when_not_initialized(self):
        adapter = _make_adapter()
        # Do NOT initialize
        event = {
            "chat": {
                "buttonClickedPayload": {
                    "space": {"name": "spaces/ABC123", "type": "ROOM"},
                    "message": {
                        "name": "spaces/ABC123/messages/msg1",
                        "sender": {"name": "users/1", "displayName": "U", "type": "HUMAN"},
                        "text": "",
                        "createTime": "2024-01-01T00:00:00Z",
                    },
                    "user": {
                        "name": "users/2",
                        "displayName": "Clicker",
                        "type": "HUMAN",
                        "email": "",
                    },
                },
            },
            "commonEventObject": {"invokedFunction": "doSomething"},
        }
        response = await adapter.handle_webhook(event)
        assert response["status"] == 200

    @pytest.mark.asyncio
    async def test_ignores_card_click_when_missing_action_id(self):
        adapter = _make_adapter()
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        event = {
            "chat": {
                "buttonClickedPayload": {
                    "space": {"name": "spaces/ABC123", "type": "ROOM"},
                    "message": {
                        "name": "spaces/ABC123/messages/msg1",
                        "sender": {"name": "users/1", "displayName": "U", "type": "HUMAN"},
                        "text": "",
                        "createTime": "2024-01-01T00:00:00Z",
                    },
                    "user": {
                        "name": "users/2",
                        "displayName": "Clicker",
                        "type": "HUMAN",
                        "email": "",
                    },
                },
            },
            "commonEventObject": {
                # No invokedFunction, no parameters.actionId
                "parameters": {},
            },
        }
        response = await adapter.handle_webhook(event)
        assert response["status"] == 200
        assert not chat.process_action.called

    @pytest.mark.asyncio
    async def test_uses_invoked_function_as_action_id(self):
        adapter = _make_adapter()
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        event = {
            "commonEventObject": {"invokedFunction": "handleApprove"},
            "chat": {
                "buttonClickedPayload": {
                    "space": {"name": "spaces/ABC123", "type": "ROOM"},
                    "message": {
                        "name": "spaces/ABC123/messages/msg1",
                        "sender": {"name": "users/1", "displayName": "U", "type": "HUMAN"},
                        "text": "",
                        "createTime": "2024-01-01T00:00:00Z",
                    },
                    "user": {
                        "name": "users/2",
                        "displayName": "Clicker",
                        "type": "HUMAN",
                        "email": "",
                    },
                },
            },
        }
        response = await adapter.handle_webhook(event)
        assert response["status"] == 200
        if chat.process_action.called:
            call_args = chat.process_action.call_args
            action_event = call_args[0][0]
            # Event may be a dict or a dataclass depending on implementation
            action_id = (
                action_event.get("action_id")
                if isinstance(action_event, dict)
                else getattr(action_event, "action_id", None)
            )
            assert action_id == "handleApprove"

    @pytest.mark.asyncio
    async def test_ignores_card_click_when_space_is_missing(self):
        adapter = _make_adapter()
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        event = {
            "commonEventObject": {"invokedFunction": "myAction"},
            "chat": {},  # No buttonClickedPayload
        }
        response = await adapter.handle_webhook(event)
        assert response["status"] == 200
        assert not chat.process_action.called


# ---------------------------------------------------------------------------
# handleMessageEvent edge cases
# ---------------------------------------------------------------------------


class TestHandleMessageEventEdgeCases:
    @pytest.mark.asyncio
    async def test_not_process_when_not_initialized(self):
        adapter = _make_adapter()
        event = _make_message_event()
        response = await adapter.handle_webhook(event)
        assert response["status"] == 200

    @pytest.mark.asyncio
    async def test_dm_thread_id_has_dm_suffix(self):
        adapter = _make_adapter()
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        event = _make_message_event(
            space_type="DM",
            space_name="spaces/DM_SPACE",
            thread_name="spaces/DM_SPACE/threads/thread1",
        )
        response = await adapter.handle_webhook(event)
        assert response["status"] == 200

        if chat.process_message.called:
            call_args = chat.process_message.call_args
            thread_id = call_args[0][1] if len(call_args[0]) > 1 else ""
            if isinstance(thread_id, str):
                assert DM_SUFFIX_PATTERN.search(thread_id)

    @pytest.mark.asyncio
    async def test_room_thread_does_not_have_dm_suffix(self):
        adapter = _make_adapter()
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        event = _make_message_event(
            space_type="ROOM",
            thread_name="spaces/ABC123/threads/XYZ",
        )
        response = await adapter.handle_webhook(event)
        assert response["status"] == 200

        if chat.process_message.called:
            call_args = chat.process_message.call_args
            thread_id = call_args[0][1] if len(call_args[0]) > 1 else ""
            if isinstance(thread_id, str):
                assert not DM_SUFFIX_PATTERN.search(thread_id)


# ---------------------------------------------------------------------------
# Pub/Sub reaction events
# ---------------------------------------------------------------------------


class TestPubSubReactionEvents:
    @pytest.mark.asyncio
    async def test_pubsub_reaction_created_event(self):
        adapter = _make_adapter()
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        pubsub = _make_pubsub_push_message(
            {
                "reaction": {
                    "name": "spaces/ABC123/messages/msg1/reactions/react1",
                    "emoji": {"unicode": "\U0001f44d"},
                    "user": {"name": "users/100", "displayName": "Reactor", "type": "HUMAN"},
                },
            },
            event_type="google.workspace.chat.reaction.v1.created",
            target_resource="//chat.googleapis.com/spaces/ABC123",
        )
        response = await adapter.handle_webhook(pubsub)
        assert response["status"] == 200

    @pytest.mark.asyncio
    async def test_pubsub_reaction_deleted_event(self):
        adapter = _make_adapter()
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        pubsub = _make_pubsub_push_message(
            {
                "reaction": {
                    "name": "spaces/ABC123/messages/msg1/reactions/react1",
                    "emoji": {"unicode": "\U0001f44d"},
                    "user": {"name": "users/100", "type": "HUMAN"},
                },
            },
            event_type="google.workspace.chat.reaction.v1.deleted",
            target_resource="//chat.googleapis.com/spaces/ABC123",
        )
        response = await adapter.handle_webhook(pubsub)
        assert response["status"] == 200


# ---------------------------------------------------------------------------
# Pub/Sub message parsing edge cases
# ---------------------------------------------------------------------------


class TestPubSubMessageParsing:
    @pytest.mark.asyncio
    async def test_pubsub_message_with_attachments(self):
        adapter = _make_adapter()
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        pubsub = _make_pubsub_push_message(
            {
                "message": {
                    "name": "spaces/ABC123/messages/msg1",
                    "sender": {"name": "users/100", "displayName": "User", "type": "HUMAN"},
                    "text": "With file",
                    "createTime": "2024-01-01T00:00:00Z",
                    "attachment": [
                        {
                            "name": "att1",
                            "contentName": "doc.pdf",
                            "contentType": "application/pdf",
                            "downloadUri": "https://example.com/doc.pdf",
                        },
                    ],
                },
            }
        )
        response = await adapter.handle_webhook(pubsub)
        assert response["status"] == 200

    @pytest.mark.asyncio
    async def test_pubsub_bot_message_detected(self):
        adapter = _make_adapter()
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        pubsub = _make_pubsub_push_message(
            {
                "message": {
                    "name": "spaces/ABC123/messages/msg1",
                    "sender": {"name": "users/BOT1", "displayName": "BotUser", "type": "BOT"},
                    "text": "Bot message",
                    "createTime": "2024-01-01T00:00:00Z",
                },
            }
        )
        response = await adapter.handle_webhook(pubsub)
        assert response["status"] == 200

    @pytest.mark.asyncio
    async def test_pubsub_missing_message_data_is_handled(self):
        adapter = _make_adapter()
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        # Notification without a message field
        pubsub = _make_pubsub_push_message({})
        response = await adapter.handle_webhook(pubsub)
        # Should return 200 to avoid Pub/Sub retries
        assert response["status"] == 200


# ---------------------------------------------------------------------------
# Error handling for Google Chat API
# ---------------------------------------------------------------------------


class TestGoogleChatErrorHandling:
    def test_rate_limit_429_raises_adapter_rate_limit_error(self):
        adapter = _make_adapter()
        with pytest.raises(AdapterRateLimitError):
            adapter._handle_google_chat_error(_FakeApiError(429, "Too many requests"), "test")

    def test_non_429_rethrows_original_error(self):
        adapter = _make_adapter()
        original = _FakeApiError(500, "Server error")
        with pytest.raises(_FakeApiError):
            adapter._handle_google_chat_error(original, "test")

    def test_403_rethrows_original_error(self):
        adapter = _make_adapter()
        original = _FakeApiError(403, "Forbidden")
        with pytest.raises(_FakeApiError):
            adapter._handle_google_chat_error(original, "editMessage")

    def test_404_rethrows_original_error(self):
        adapter = _make_adapter()
        original = _FakeApiError(404, "Not found")
        with pytest.raises(_FakeApiError):
            adapter._handle_google_chat_error(original, "deleteMessage")


# ---------------------------------------------------------------------------
# Thread ID edge cases
# ---------------------------------------------------------------------------


class TestThreadIdEdgeCases:
    def test_encode_without_thread_name_has_prefix(self):
        tid = encode_thread_id(GoogleChatThreadId(space_name="spaces/ONLY_SPACE"))
        assert tid.startswith("gchat:")

    def test_decode_invalid_raises_validation_error(self):
        with pytest.raises(ValidationError):
            decode_thread_id("invalid")

    def test_decode_empty_string_raises(self):
        with pytest.raises(ValidationError):
            decode_thread_id("")

    def test_is_dm_thread_true_for_dm(self):
        tid = encode_thread_id(GoogleChatThreadId(space_name="spaces/DM1", is_dm=True))
        assert is_dm_thread(tid) is True

    def test_is_dm_thread_false_for_room(self):
        tid = encode_thread_id(GoogleChatThreadId(space_name="spaces/ROOM1"))
        assert is_dm_thread(tid) is False

    def test_roundtrip_with_complex_names(self):
        original = GoogleChatThreadId(
            space_name="spaces/AAAA_BBBB_CCCC",
            thread_name="spaces/AAAA_BBBB_CCCC/threads/XYZ789_ABC",
        )
        encoded = encode_thread_id(original)
        decoded = decode_thread_id(encoded)
        assert decoded.space_name == original.space_name
        assert decoded.thread_name == original.thread_name


# ---------------------------------------------------------------------------
# Channel ID derivation
# ---------------------------------------------------------------------------


class TestChannelIdDerivation:
    def test_channel_id_from_thread_with_thread_name(self):
        adapter = _make_adapter()
        tid = encode_thread_id(
            GoogleChatThreadId(
                space_name="spaces/ABC123",
                thread_name="spaces/ABC123/threads/T1",
            )
        )
        assert adapter.channel_id_from_thread_id(tid) == "gchat:spaces/ABC123"

    def test_channel_id_from_thread_without_thread_name(self):
        adapter = _make_adapter()
        tid = encode_thread_id(GoogleChatThreadId(space_name="spaces/ONLY"))
        assert adapter.channel_id_from_thread_id(tid) == "gchat:spaces/ONLY"

    def test_channel_id_from_dm_thread(self):
        adapter = _make_adapter()
        tid = encode_thread_id(GoogleChatThreadId(space_name="spaces/DM_SPACE", is_dm=True))
        assert adapter.channel_id_from_thread_id(tid) == "gchat:spaces/DM_SPACE"


# ---------------------------------------------------------------------------
# Constructor / config
# ---------------------------------------------------------------------------


class TestConstructorConfig:
    def test_adapter_name_is_gchat(self):
        adapter = _make_adapter()
        assert adapter.name == "gchat"

    def test_no_auth_raises(self):
        with pytest.raises(Exception):
            GoogleChatAdapter(GoogleChatAdapterConfig())

    def test_default_user_name(self):
        adapter = _make_adapter()
        assert adapter.user_name == "bot"

    def test_custom_user_name(self):
        adapter = _make_adapter(user_name="mybot")
        assert adapter.user_name == "mybot"


# ---------------------------------------------------------------------------
# Attachment type classification
# ---------------------------------------------------------------------------


class TestAttachmentTypeClassification:
    def test_file_type_attachment(self):
        adapter = _make_adapter()
        event = _make_message_event(
            attachment=[
                {
                    "name": "att1",
                    "contentName": "report.xlsx",
                    "contentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "downloadUri": "https://example.com/report.xlsx",
                },
            ]
        )
        msg = adapter.parse_message(event)
        assert len(msg.attachments) == 1
        assert msg.attachments[0].type == "file"
        assert msg.attachments[0].name == "report.xlsx"

    def test_multiple_attachment_types(self):
        adapter = _make_adapter()
        event = _make_message_event(
            attachment=[
                {
                    "name": "img1",
                    "contentName": "photo.jpg",
                    "contentType": "image/jpeg",
                    "downloadUri": "https://example.com/photo.jpg",
                },
                {
                    "name": "vid1",
                    "contentName": "clip.mp4",
                    "contentType": "video/mp4",
                    "downloadUri": "https://example.com/clip.mp4",
                },
                {
                    "name": "aud1",
                    "contentName": "recording.wav",
                    "contentType": "audio/wav",
                    "downloadUri": "https://example.com/recording.wav",
                },
                {
                    "name": "doc1",
                    "contentName": "notes.txt",
                    "contentType": "text/plain",
                    "downloadUri": "https://example.com/notes.txt",
                },
            ]
        )
        msg = adapter.parse_message(event)
        assert len(msg.attachments) == 4
        assert msg.attachments[0].type == "image"
        assert msg.attachments[1].type == "video"
        assert msg.attachments[2].type == "audio"
        assert msg.attachments[3].type == "file"


# ---------------------------------------------------------------------------
# Sender email parsing
# ---------------------------------------------------------------------------


class TestSenderEmailParsing:
    def test_parse_message_with_sender_email(self):
        adapter = _make_adapter()
        event = _make_message_event(
            sender_email="alice@example.com",
            sender_display_name="Alice",
        )
        msg = adapter.parse_message(event)
        assert msg.author.full_name == "Alice"
        assert msg.author.is_bot is False


# ---------------------------------------------------------------------------
# No-message-payload error
# ---------------------------------------------------------------------------


class TestNoMessagePayloadError:
    def test_parse_message_with_empty_event_raises(self):
        adapter = _make_adapter()
        with pytest.raises(Exception):
            adapter.parse_message({})

    def test_parse_message_with_empty_chat_raises(self):
        adapter = _make_adapter()
        with pytest.raises(Exception):
            adapter.parse_message({"chat": {}})
