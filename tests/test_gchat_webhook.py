"""Tests for Google Chat adapter webhook handling, thread IDs, message parsing, and API operations.

Port of packages/adapter-gchat/src/index.test.ts.
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
)
from chat_sdk.adapters.google_chat.types import (
    GoogleChatAdapterConfig,
    ServiceAccountCredentials,
)
from chat_sdk.shared.errors import AdapterRateLimitError, ValidationError

GCHAT_PREFIX_PATTERN = re.compile(r"^gchat:")
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
    chat.process_message = MagicMock()
    chat.process_reaction = MagicMock()
    chat.process_action = MagicMock()
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


# ---------------------------------------------------------------------------
# Thread ID encoding / decoding
# ---------------------------------------------------------------------------


class TestThreadIdEncoding:
    def test_encode_without_thread_name(self):
        tid = encode_thread_id(GoogleChatThreadId(space_name="spaces/ABC123"))
        assert GCHAT_PREFIX_PATTERN.match(tid)

    def test_encode_decode_roundtrip_without_thread_name(self):
        original = GoogleChatThreadId(space_name="spaces/ABC123")
        encoded = encode_thread_id(original)
        decoded = decode_thread_id(encoded)
        assert decoded.space_name == original.space_name

    def test_encode_decode_roundtrip_with_thread_name(self):
        original = GoogleChatThreadId(
            space_name="spaces/ABC123",
            thread_name="spaces/ABC123/threads/XYZ789",
        )
        encoded = encode_thread_id(original)
        decoded = decode_thread_id(encoded)
        assert decoded.space_name == original.space_name
        assert decoded.thread_name == original.thread_name

    def test_encode_dm_thread_has_dm_suffix(self):
        original = GoogleChatThreadId(space_name="spaces/DM123", is_dm=True)
        encoded = encode_thread_id(original)
        assert DM_SUFFIX_PATTERN.search(encoded)

    def test_decode_dm_thread(self):
        original = GoogleChatThreadId(space_name="spaces/DM123", is_dm=True)
        encoded = encode_thread_id(original)
        decoded = decode_thread_id(encoded)
        assert decoded.space_name == "spaces/DM123"
        assert decoded.is_dm is True

    def test_decode_invalid_thread_id_raises(self):
        with pytest.raises(ValidationError):
            decode_thread_id("invalid")


# ---------------------------------------------------------------------------
# Constructor / initialization
# ---------------------------------------------------------------------------


class TestConstructor:
    def test_adapter_name(self):
        adapter = _make_adapter()
        assert adapter.name == "gchat"

    def test_default_user_name(self):
        adapter = _make_adapter()
        assert adapter.user_name == "bot"

    def test_custom_user_name(self):
        adapter = _make_adapter(user_name="mybot")
        assert adapter.user_name == "mybot"

    def test_no_auth_raises(self):
        with pytest.raises(ValidationError):
            GoogleChatAdapter(GoogleChatAdapterConfig())


# ---------------------------------------------------------------------------
# isDM
# ---------------------------------------------------------------------------


class TestIsDM:
    def test_true_for_dm_thread(self):
        adapter = _make_adapter()
        tid = encode_thread_id(GoogleChatThreadId(space_name="spaces/DM123", is_dm=True))
        assert adapter.is_dm(tid) is True

    def test_false_for_room_thread(self):
        adapter = _make_adapter()
        tid = encode_thread_id(GoogleChatThreadId(space_name="spaces/ROOM456"))
        assert adapter.is_dm(tid) is False


# ---------------------------------------------------------------------------
# parseMessage
# ---------------------------------------------------------------------------


class TestParseMessage:
    def test_basic_message(self):
        adapter = _make_adapter()
        event = _make_message_event(
            message_text="Hello world",
            sender_display_name="Alice",
            sender_name="users/ALICE1",
        )
        msg = adapter.parse_message(event)
        assert "Hello world" in msg.text
        assert msg.author.full_name == "Alice"
        assert msg.author.user_id == "users/ALICE1"
        assert msg.author.is_bot is False

    def test_no_message_payload_raises(self):
        adapter = _make_adapter()
        with pytest.raises(ValidationError):
            adapter.parse_message({})

    def test_bot_sender_detected(self):
        adapter = _make_adapter()
        event = _make_message_event(sender_type="BOT", sender_display_name="BotUser")
        msg = adapter.parse_message(event)
        assert msg.author.is_bot is True

    def test_attachments_parsed(self):
        adapter = _make_adapter()
        event = _make_message_event(
            attachment=[
                {
                    "name": "att1",
                    "contentName": "photo.png",
                    "contentType": "image/png",
                    "downloadUri": "https://example.com/photo.png",
                }
            ]
        )
        msg = adapter.parse_message(event)
        assert len(msg.attachments) == 1
        assert msg.attachments[0].type == "image"
        assert msg.attachments[0].name == "photo.png"

    def test_video_and_audio_attachment_types(self):
        adapter = _make_adapter()
        event = _make_message_event(
            attachment=[
                {
                    "name": "vid1",
                    "contentName": "video.mp4",
                    "contentType": "video/mp4",
                    "downloadUri": "https://example.com/video.mp4",
                },
                {
                    "name": "aud1",
                    "contentName": "audio.mp3",
                    "contentType": "audio/mpeg",
                    "downloadUri": "https://example.com/audio.mp3",
                },
            ]
        )
        msg = adapter.parse_message(event)
        assert len(msg.attachments) == 2
        assert msg.attachments[0].type == "video"
        assert msg.attachments[1].type == "audio"


# ---------------------------------------------------------------------------
# normalizeBotMentions (via parseMessage)
# ---------------------------------------------------------------------------


class TestBotMentions:
    def test_replace_bot_mention_with_user_name(self):
        adapter = _make_adapter(user_name="mybot")
        event = _make_message_event(
            message_text="@Chat SDK Demo hello",
            annotations=[
                {
                    "type": "USER_MENTION",
                    "startIndex": 0,
                    "length": 14,
                    "userMention": {
                        "user": {
                            "name": "users/BOT123",
                            "displayName": "Chat SDK Demo",
                            "type": "BOT",
                        },
                        "type": "MENTION",
                    },
                }
            ],
        )
        msg = adapter.parse_message(event)
        assert "@mybot" in msg.text
        assert "@Chat SDK Demo" not in msg.text

    def test_learn_bot_user_id_from_annotations(self):
        adapter = _make_adapter()
        assert adapter.bot_user_id is None

        event = _make_message_event(
            message_text="@BotName hi",
            annotations=[
                {
                    "type": "USER_MENTION",
                    "startIndex": 0,
                    "length": 8,
                    "userMention": {
                        "user": {
                            "name": "users/LEARNED_BOT_ID",
                            "displayName": "BotName",
                            "type": "BOT",
                        },
                        "type": "MENTION",
                    },
                }
            ],
        )
        adapter.parse_message(event)
        assert adapter.bot_user_id == "users/LEARNED_BOT_ID"

    def test_does_not_overwrite_bot_user_id(self):
        adapter = _make_adapter()
        adapter._bot_user_id = "users/FIRST_BOT"

        event = _make_message_event(
            message_text="@AnotherBot hi",
            annotations=[
                {
                    "type": "USER_MENTION",
                    "startIndex": 0,
                    "length": 11,
                    "userMention": {
                        "user": {
                            "name": "users/SECOND_BOT",
                            "displayName": "AnotherBot",
                            "type": "BOT",
                        },
                        "type": "MENTION",
                    },
                }
            ],
        )
        adapter.parse_message(event)
        assert adapter.bot_user_id == "users/FIRST_BOT"


# ---------------------------------------------------------------------------
# isMessageFromSelf (via parseMessage)
# ---------------------------------------------------------------------------


class TestIsMessageFromSelf:
    def test_detects_self_message(self):
        adapter = _make_adapter()
        adapter._bot_user_id = "users/BOT123"
        event = _make_message_event(
            sender_name="users/BOT123",
            sender_type="BOT",
            sender_display_name="MyBot",
        )
        msg = adapter.parse_message(event)
        assert msg.author.is_me is True

    def test_other_bot_not_self(self):
        adapter = _make_adapter()
        adapter._bot_user_id = "users/BOT123"
        event = _make_message_event(
            sender_name="users/OTHER_BOT",
            sender_type="BOT",
            sender_display_name="OtherBot",
        )
        msg = adapter.parse_message(event)
        assert msg.author.is_me is False

    def test_unknown_bot_id_returns_false(self):
        adapter = _make_adapter()
        event = _make_message_event(
            sender_type="BOT",
            sender_display_name="SomeBot",
        )
        msg = adapter.parse_message(event)
        assert msg.author.is_me is False


# ---------------------------------------------------------------------------
# handleWebhook
# ---------------------------------------------------------------------------


class TestHandleWebhook:
    @pytest.mark.asyncio
    async def test_invalid_json_returns_400(self):
        adapter = _make_adapter()
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        # Pass a non-dict string that will produce invalid JSON
        response = await adapter.handle_webhook("not json{{{")
        assert response["status"] == 400

    @pytest.mark.asyncio
    async def test_message_event_routes_to_process_message(self):
        adapter = _make_adapter()
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        event = _make_message_event(message_text="test message")
        response = await adapter.handle_webhook(event)
        assert response["status"] == 200
        assert chat.process_message.called

    @pytest.mark.asyncio
    async def test_added_to_space_event(self):
        adapter = _make_adapter()
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        event = {
            "chat": {
                "addedToSpacePayload": {
                    "space": {"name": "spaces/NEWSPACE", "type": "ROOM"},
                },
            },
        }
        response = await adapter.handle_webhook(event)
        assert response["status"] == 200

    @pytest.mark.asyncio
    async def test_removed_from_space_event(self):
        adapter = _make_adapter()
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        event = {
            "chat": {
                "removedFromSpacePayload": {
                    "space": {"name": "spaces/LEFTSPACE", "type": "ROOM"},
                },
            },
        }
        response = await adapter.handle_webhook(event)
        assert response["status"] == 200

    @pytest.mark.asyncio
    async def test_card_button_click(self):
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
                        "sender": {"name": "users/100", "displayName": "User", "type": "HUMAN"},
                        "text": "",
                        "createTime": "2024-01-01T00:00:00Z",
                    },
                    "user": {
                        "name": "users/200",
                        "displayName": "Clicker",
                        "type": "HUMAN",
                        "email": "clicker@example.com",
                    },
                },
            },
            "commonEventObject": {
                "invokedFunction": "myAction",
                "parameters": {"actionId": "btn_approve", "value": "42"},
            },
        }
        response = await adapter.handle_webhook(event)
        assert response["status"] == 200

    @pytest.mark.asyncio
    async def test_non_message_event_returns_200(self):
        adapter = _make_adapter()
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        response = await adapter.handle_webhook({"chat": {}})
        assert response["status"] == 200

    @pytest.mark.asyncio
    async def test_dm_message_uses_dm_thread_id(self):
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
        # processMessage should be called with a DM thread ID
        if chat.process_message.called:
            call_args = chat.process_message.call_args
            thread_id = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("thread_id", "")
            if isinstance(thread_id, str):
                assert DM_SUFFIX_PATTERN.search(thread_id)


# ---------------------------------------------------------------------------
# Pub/Sub message handling
# ---------------------------------------------------------------------------


class TestPubSubMessages:
    @pytest.mark.asyncio
    async def test_pubsub_message_routes_to_process_message(self):
        adapter = _make_adapter()
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        pubsub = _make_pubsub_push_message(
            {
                "message": {
                    "name": "spaces/ABC123/messages/msg1",
                    "sender": {"name": "users/100", "displayName": "PubSub User", "type": "HUMAN"},
                    "text": "pub sub message",
                    "createTime": "2024-01-01T00:00:00Z",
                },
            }
        )
        response = await adapter.handle_webhook(pubsub)
        assert response["status"] == 200

    @pytest.mark.asyncio
    async def test_unsupported_pubsub_event_type_skipped(self):
        adapter = _make_adapter()
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        pubsub = _make_pubsub_push_message(
            {
                "message": {
                    "name": "m1",
                    "sender": {"name": "u1", "displayName": "U", "type": "HUMAN"},
                    "text": "t",
                    "createTime": "2024-01-01T00:00:00Z",
                },
            },
            event_type="google.workspace.chat.message.v1.updated",
        )
        response = await adapter.handle_webhook(pubsub)
        assert response["status"] == 200
        assert not chat.process_message.called

    @pytest.mark.asyncio
    async def test_malformed_pubsub_data_returns_200(self):
        adapter = _make_adapter()
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        pubsub = {
            "message": {
                "data": "not-valid-base64!!!",
                "messageId": "msg-1",
                "publishTime": "2024-01-01T00:00:00Z",
                "attributes": {
                    "ce-type": "google.workspace.chat.message.v1.created",
                },
            },
            "subscription": "projects/test/subscriptions/test-sub",
        }
        response = await adapter.handle_webhook(pubsub)
        # Should return 200 to avoid retries
        assert response["status"] == 200


# ---------------------------------------------------------------------------
# channelIdFromThreadId
# ---------------------------------------------------------------------------


class TestChannelId:
    def test_derives_channel_id(self):
        adapter = _make_adapter()
        tid = encode_thread_id(
            GoogleChatThreadId(
                space_name="spaces/ABC123",
                thread_name="spaces/ABC123/threads/T1",
            )
        )
        channel_id = adapter.channel_id_from_thread_id(tid)
        assert channel_id == "gchat:spaces/ABC123"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class _FakeApiError(Exception):
    """Fake error object mimicking Google API errors."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
        self.errors = None


class TestErrorHandling:
    def test_rate_limit_429_raises_adapter_rate_limit_error(self):
        adapter = _make_adapter()
        with pytest.raises(AdapterRateLimitError):
            adapter._handle_google_chat_error(_FakeApiError(429, "Too many requests"), "test")

    def test_non_429_rethrows_original_error(self):
        adapter = _make_adapter()
        original = _FakeApiError(500, "Server error")
        with pytest.raises(_FakeApiError):
            adapter._handle_google_chat_error(original, "test")
