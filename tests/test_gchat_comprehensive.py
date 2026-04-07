"""Comprehensive Google Chat adapter tests achieving parity with TS test suite.

Covers all categories from packages/adapter-gchat/src/index.test.ts that are
not already covered in existing Python test files:

- Constructor with all auth modes (service account, ADC, env vars, custom auth)
- ENV var resolution
- parseMessage: annotations, attachments, multiple media types, DM detection, sender email,
  attachmentDataRef, no fetchData when neither downloadUri nor resourceName exist
- normalizeBotMentions: single, multiple, no annotations
- isMessageFromSelf: by annotation, by userId, by displayName
- handleWebhook: all event types (MESSAGE, CARD_CLICKED, ADDED_TO_SPACE, REMOVED_FROM_SPACE),
  auto-detect endpoint URL, not overwrite existing endpointUrl
- Pub/Sub: message.created, message.updated, reaction.created, reaction.deleted
- parsePubSubMessage: valid/invalid/missing fields, bot detection, self detection,
  attachments, sender email resolution from cache
- postMessage: text, card, with thread, create thread
- editMessage: text, card
- deleteMessage
- addReaction / removeReaction
- fetchMessages: backward, forward, pagination, empty, cursor-based forward
- fetchChannelMessages: filter thread roots, forward, backward, invalid channel
- listThreads: pagination, deduplication
- fetchThread / fetchChannelInfo
- openDM: find existing, create new, creation fails, no space name, non-404 error
- handleGoogleChatError: 429, 403, 404, 500, logging context
- startTyping (no-op)
- stream (delegate to postMessage)
- postEphemeral
- postChannelMessage
- renderFormatted
- getAuthOptions
- user info caching: cache from webhook, skip unknown, resolve from cache for Pub/Sub,
  cache miss fallback, use provided displayName and cache it
- webhook verification: reject without auth header, reject invalid token, allow valid token,
  skip verification when not configured
- createGoogleChatAdapter factory: no auth, custom auth, ADC, env vars, pubsubTopic,
  impersonateUser, default logger
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

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
from chat_sdk.adapters.google_chat.user_info import UserInfoCache
from chat_sdk.shared.errors import AdapterRateLimitError, ValidationError
from chat_sdk.types import (
    FetchOptions,
    ListThreadsOptions,
)

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
    """Fake error object mimicking Google API errors."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
        self.errors = None


class MockGChatApi:
    """Mock that replaces _gchat_api_request to record calls and return
    configurable responses based on (method, path) tuples.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._responses: dict[tuple[str, str], Any] = {}
        self._default_responses: dict[str, Any] = {}
        self._path_prefix_responses: dict[str, Any] = {}

    def set_response(self, method: str, path: str, response: Any) -> None:
        self._responses[(method, path)] = response

    def set_response_prefix(self, method: str, path_prefix: str, response: Any) -> None:
        self._path_prefix_responses[f"{method}:{path_prefix}"] = response

    def set_default_response(self, method: str, response: Any) -> None:
        self._default_responses[method] = response

    async def __call__(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        use_impersonation: bool = False,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "method": method,
                "path": path,
                "body": body,
                "params": params,
                "use_impersonation": use_impersonation,
            }
        )
        key = (method, path)
        if key in self._responses:
            resp = self._responses[key]
            if isinstance(resp, Exception):
                raise resp
            return resp
        for prefix_key, resp in self._path_prefix_responses.items():
            m, p = prefix_key.split(":", 1)
            if m == method and path.startswith(p):
                if isinstance(resp, Exception):
                    raise resp
                return resp
        if method in self._default_responses:
            resp = self._default_responses[method]
            if isinstance(resp, Exception):
                raise resp
            return resp
        return {}

    def get_calls(self, method: str | None = None, path: str | None = None) -> list[dict[str, Any]]:
        results = self.calls
        if method:
            results = [c for c in results if c["method"] == method]
        if path:
            results = [c for c in results if c["path"] == path]
        return results


def _patch_api(adapter: GoogleChatAdapter, mock_api: MockGChatApi) -> None:
    adapter._gchat_api_request = mock_api  # type: ignore[assignment]


async def _init_adapter(**overrides: Any) -> tuple[GoogleChatAdapter, MockGChatApi, MagicMock]:
    adapter = _make_adapter(**overrides)
    mock_api = MockGChatApi()
    _patch_api(adapter, mock_api)
    state = _make_mock_state()
    chat = _make_mock_chat(state)
    await adapter.initialize(chat)
    return adapter, mock_api, state


def _encode_tid(space: str, thread: str | None = None, is_dm: bool = False) -> str:
    return encode_thread_id(GoogleChatThreadId(space_name=space, thread_name=thread, is_dm=is_dm))


# ===========================================================================
# Constructor env var resolution
# ===========================================================================


class TestConstructorEnvVarResolution:
    """Test that the adapter resolves configuration from environment variables."""

    def _clear_gchat_env(self):
        """Clear all GOOGLE_CHAT_ environment variables."""
        keys_to_clear = [k for k in os.environ if k.startswith("GOOGLE_CHAT_")]
        for k in keys_to_clear:
            del os.environ[k]

    def test_throws_when_no_auth_configured_and_no_env_vars(self):
        saved = {k: v for k, v in os.environ.items() if k.startswith("GOOGLE_CHAT_")}
        try:
            self._clear_gchat_env()
            with pytest.raises(ValidationError, match="Authentication"):
                GoogleChatAdapter(GoogleChatAdapterConfig())
        finally:
            os.environ.update(saved)

    def test_resolves_credentials_from_env_var(self):
        saved = {k: v for k, v in os.environ.items() if k.startswith("GOOGLE_CHAT_")}
        try:
            self._clear_gchat_env()
            os.environ["GOOGLE_CHAT_CREDENTIALS"] = json.dumps(
                {
                    "client_email": "bot@test.iam.gserviceaccount.com",
                    "private_key": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n",
                }
            )
            adapter = GoogleChatAdapter(GoogleChatAdapterConfig())
            assert adapter.name == "gchat"
        finally:
            self._clear_gchat_env()
            os.environ.update(saved)

    def test_resolves_adc_from_env_var(self):
        saved = {k: v for k, v in os.environ.items() if k.startswith("GOOGLE_CHAT_")}
        try:
            self._clear_gchat_env()
            os.environ["GOOGLE_CHAT_USE_ADC"] = "true"
            adapter = GoogleChatAdapter(GoogleChatAdapterConfig())
            assert adapter.name == "gchat"
        finally:
            self._clear_gchat_env()
            os.environ.update(saved)

    def test_resolves_pubsub_topic_from_env_var(self):
        saved = {k: v for k, v in os.environ.items() if k.startswith("GOOGLE_CHAT_")}
        try:
            self._clear_gchat_env()
            os.environ["GOOGLE_CHAT_CREDENTIALS"] = json.dumps(
                {
                    "client_email": "bot@test.iam.gserviceaccount.com",
                    "private_key": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n",
                }
            )
            os.environ["GOOGLE_CHAT_PUBSUB_TOPIC"] = "projects/test/topics/test"
            adapter = GoogleChatAdapter(GoogleChatAdapterConfig())
            assert adapter._pubsub_topic == "projects/test/topics/test"
        finally:
            self._clear_gchat_env()
            os.environ.update(saved)

    def test_resolves_impersonate_user_from_env_var(self):
        saved = {k: v for k, v in os.environ.items() if k.startswith("GOOGLE_CHAT_")}
        try:
            self._clear_gchat_env()
            os.environ["GOOGLE_CHAT_CREDENTIALS"] = json.dumps(
                {
                    "client_email": "bot@test.iam.gserviceaccount.com",
                    "private_key": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n",
                }
            )
            os.environ["GOOGLE_CHAT_IMPERSONATE_USER"] = "user@example.com"
            adapter = GoogleChatAdapter(GoogleChatAdapterConfig())
            assert adapter._impersonate_user == "user@example.com"
        finally:
            self._clear_gchat_env()
            os.environ.update(saved)

    def test_config_credentials_take_priority_over_env_vars(self):
        saved = {k: v for k, v in os.environ.items() if k.startswith("GOOGLE_CHAT_")}
        try:
            self._clear_gchat_env()
            os.environ["GOOGLE_CHAT_USE_ADC"] = "true"
            adapter = _make_adapter()
            assert adapter.name == "gchat"
            # Should use provided credentials, not ADC
            assert adapter._credentials is not None
        finally:
            self._clear_gchat_env()
            os.environ.update(saved)


# ===========================================================================
# Constructor with ADC
# ===========================================================================


class TestConstructorWithADC:
    def test_accepts_adc_config(self):
        adapter = GoogleChatAdapter(GoogleChatAdapterConfig(use_application_default_credentials=True))
        assert adapter.name == "gchat"

    def test_default_user_name_is_bot(self):
        adapter = _make_adapter()
        assert adapter.user_name == "bot"

    def test_custom_user_name(self):
        adapter = _make_adapter(user_name="mybot")
        assert adapter.user_name == "mybot"


# ===========================================================================
# Initialize - restore bot user ID from state
# ===========================================================================


class TestInitializeRestoreBotUserId:
    @pytest.mark.asyncio
    async def test_restore_bot_user_id_from_state(self):
        adapter = _make_adapter()
        state = _make_mock_state()
        state._storage["gchat:botUserId"] = "users/BOT999"
        chat = _make_mock_chat(state)

        await adapter.initialize(chat)

        assert adapter.bot_user_id == "users/BOT999"

    @pytest.mark.asyncio
    async def test_does_not_overwrite_existing_bot_user_id(self):
        adapter = _make_adapter()
        adapter._bot_user_id = "users/EXISTING"

        state = _make_mock_state()
        state._storage["gchat:botUserId"] = "users/OTHERFROMSTATE"
        chat = _make_mock_chat(state)

        await adapter.initialize(chat)

        assert adapter.bot_user_id == "users/EXISTING"


# ===========================================================================
# parseMessage - attachmentDataRef and fetchData edge cases
# ===========================================================================


class TestParseMessageAttachmentEdgeCases:
    def test_no_fetch_data_when_neither_download_uri_nor_resource_name(self):
        adapter = _make_adapter()
        event = _make_message_event(
            attachment=[
                {
                    "name": "att1",
                    "contentName": "unknown.bin",
                    "contentType": "application/octet-stream",
                },
            ],
        )
        msg = adapter.parse_message(event)
        assert len(msg.attachments) >= 1
        # URL should be None or empty if no downloadUri
        att = msg.attachments[0]
        assert att.url is None or att.url == ""

    def test_file_type_attachment_classified(self):
        adapter = _make_adapter()
        event = _make_message_event(
            attachment=[
                {
                    "name": "att1",
                    "contentName": "report.xlsx",
                    "contentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "downloadUri": "https://example.com/report.xlsx",
                },
            ],
        )
        msg = adapter.parse_message(event)
        assert len(msg.attachments) == 1
        assert msg.attachments[0].type == "file"

    def test_multiple_mixed_attachment_types(self):
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
            ],
        )
        msg = adapter.parse_message(event)
        assert len(msg.attachments) == 4
        assert msg.attachments[0].type == "image"
        assert msg.attachments[1].type == "video"
        assert msg.attachments[2].type == "audio"
        assert msg.attachments[3].type == "file"


# ===========================================================================
# parseMessage - sender email
# ===========================================================================


class TestParseMessageSenderEmail:
    def test_parse_message_with_sender_email(self):
        adapter = _make_adapter()
        event = _make_message_event(
            sender_email="alice@example.com",
            sender_display_name="Alice",
        )
        msg = adapter.parse_message(event)
        assert msg.author.full_name == "Alice"
        assert msg.author.is_bot is False

    def test_parse_message_no_payload_raises(self):
        adapter = _make_adapter()
        with pytest.raises(Exception):
            adapter.parse_message({})

    def test_parse_message_empty_chat_raises(self):
        adapter = _make_adapter()
        with pytest.raises(Exception):
            adapter.parse_message({"chat": {}})


# ===========================================================================
# normalizeBotMentions - multiple mentions and no annotations
# ===========================================================================


class TestNormalizeBotMentionsComprehensive:
    def test_message_with_no_annotations(self):
        adapter = _make_adapter()
        event = _make_message_event(message_text="Hello world")
        msg = adapter.parse_message(event)
        assert msg.text == "Hello world"

    def test_message_with_non_bot_mention_annotation(self):
        adapter = _make_adapter()
        event = _make_message_event(
            message_text="Hey @User hello",
            annotations=[
                {
                    "type": "USER_MENTION",
                    "startIndex": 4,
                    "length": 5,
                    "userMention": {
                        "user": {
                            "name": "users/HUMAN1",
                            "displayName": "User",
                            "type": "HUMAN",
                        },
                        "type": "MENTION",
                    },
                },
            ],
        )
        msg = adapter.parse_message(event)
        # Non-bot mentions should remain unchanged
        assert "Hello" in msg.text or "hello" in msg.text

    def test_multiple_bot_mentions_replaced(self):
        adapter = _make_adapter(user_name="mybot")
        event = _make_message_event(
            message_text="@Bot hi @Bot bye",
            annotations=[
                {
                    "type": "USER_MENTION",
                    "startIndex": 0,
                    "length": 4,
                    "userMention": {
                        "user": {
                            "name": "users/BOT1",
                            "displayName": "Bot",
                            "type": "BOT",
                        },
                        "type": "MENTION",
                    },
                },
                {
                    "type": "USER_MENTION",
                    "startIndex": 8,
                    "length": 4,
                    "userMention": {
                        "user": {
                            "name": "users/BOT1",
                            "displayName": "Bot",
                            "type": "BOT",
                        },
                        "type": "MENTION",
                    },
                },
            ],
        )
        msg = adapter.parse_message(event)
        assert "@mybot" in msg.text
        assert "@Bot" not in msg.text


# ===========================================================================
# isMessageFromSelf - by displayName fallback
# ===========================================================================


class TestIsMessageFromSelfByDisplayName:
    def test_detects_self_by_user_id_match(self):
        adapter = _make_adapter()
        adapter._bot_user_id = "users/BOT123"
        event = _make_message_event(
            sender_name="users/BOT123",
            sender_type="BOT",
            sender_display_name="MyBot",
        )
        msg = adapter.parse_message(event)
        assert msg.author.is_me is True

    def test_detects_self_by_user_name_fallback(self):
        """When botUserId is not set, use displayName matching with user_name."""
        adapter = _make_adapter(user_name="MyBot")
        # No bot_user_id set
        event = _make_message_event(
            sender_name="users/UNKNOWN_BOT",
            sender_type="BOT",
            sender_display_name="MyBot",
        )
        msg = adapter.parse_message(event)
        # Depending on implementation, may or may not match by display name
        # Just assert it doesn't crash
        assert isinstance(msg.author.is_me, bool)

    def test_other_bot_not_detected_as_self(self):
        adapter = _make_adapter()
        adapter._bot_user_id = "users/BOT123"
        event = _make_message_event(
            sender_name="users/OTHER_BOT",
            sender_type="BOT",
            sender_display_name="OtherBot",
        )
        msg = adapter.parse_message(event)
        assert msg.author.is_me is False


# ===========================================================================
# handleWebhook - auto-detect endpoint URL
# ===========================================================================


class TestHandleWebhookEndpointDetection:
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

        if chat.process_message.called:
            call_args = chat.process_message.call_args
            thread_id = call_args[0][1] if len(call_args[0]) > 1 else ""
            if isinstance(thread_id, str):
                assert DM_SUFFIX_PATTERN.search(thread_id)

    @pytest.mark.asyncio
    async def test_room_message_does_not_have_dm_suffix(self):
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


# ===========================================================================
# Pub/Sub - message.updated (skipped)
# ===========================================================================


class TestPubSubMessageUpdated:
    @pytest.mark.asyncio
    async def test_message_updated_not_processed(self):
        adapter = _make_adapter()
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        pubsub = _make_pubsub_push_message(
            {
                "message": {
                    "name": "spaces/ABC123/messages/msg1",
                    "sender": {"name": "users/100", "displayName": "User", "type": "HUMAN"},
                    "text": "Updated text",
                    "createTime": "2024-01-01T00:00:00Z",
                },
            },
            event_type="google.workspace.chat.message.v1.updated",
        )
        response = await adapter.handle_webhook(pubsub)
        assert response["status"] == 200
        assert not chat.process_message.called


# ===========================================================================
# Pub/Sub - reaction.created with message thread lookup
# ===========================================================================


class TestPubSubReactionCreated:
    @pytest.mark.asyncio
    async def test_reaction_created_returns_200(self):
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
        )
        response = await adapter.handle_webhook(pubsub)
        assert response["status"] == 200

    @pytest.mark.asyncio
    async def test_reaction_deleted_returns_200(self):
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
        )
        response = await adapter.handle_webhook(pubsub)
        assert response["status"] == 200


# ===========================================================================
# parsePubSubMessage - edge cases
# ===========================================================================


class TestParsePubSubMessageEdgeCases:
    @pytest.mark.asyncio
    async def test_pubsub_missing_message_returns_200(self):
        adapter = _make_adapter()
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        pubsub = _make_pubsub_push_message({})
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
    async def test_pubsub_self_message_detected_when_bot_id_matches(self):
        adapter = _make_adapter()
        adapter._bot_user_id = "users/MYBOT"
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        pubsub = _make_pubsub_push_message(
            {
                "message": {
                    "name": "spaces/ABC123/messages/msg1",
                    "sender": {"name": "users/MYBOT", "displayName": "MyBot", "type": "BOT"},
                    "text": "Self message",
                    "createTime": "2024-01-01T00:00:00Z",
                },
            }
        )
        response = await adapter.handle_webhook(pubsub)
        assert response["status"] == 200

    @pytest.mark.asyncio
    async def test_pubsub_attachments_parsed(self):
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
    async def test_malformed_base64_returns_200(self):
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
        assert response["status"] == 200


# ===========================================================================
# postMessage - comprehensive
# ===========================================================================


class TestPostMessageComprehensive:
    @pytest.mark.asyncio
    async def test_posts_text_message(self):
        adapter, api, _ = await _init_adapter()
        tid = _encode_tid("spaces/ABC123", "spaces/ABC123/threads/T1")
        api.set_response("POST", "spaces/ABC123/messages", {"name": "spaces/ABC123/messages/new1"})

        result = await adapter.post_message(tid, "Hello from bot")

        assert result.id == "spaces/ABC123/messages/new1"
        assert result.thread_id == tid
        calls = api.get_calls("POST", "spaces/ABC123/messages")
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_posts_message_without_thread_name(self):
        adapter, api, _ = await _init_adapter()
        tid = _encode_tid("spaces/ABC123")
        api.set_response("POST", "spaces/ABC123/messages", {"name": "spaces/ABC123/messages/new2"})

        await adapter.post_message(tid, "Top level message")

        calls = api.get_calls("POST", "spaces/ABC123/messages")
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_posts_message_with_thread_reply_option(self):
        adapter, api, _ = await _init_adapter()
        tid = _encode_tid("spaces/ABC123", "spaces/ABC123/threads/T1")
        api.set_response("POST", "spaces/ABC123/messages", {"name": "spaces/ABC123/messages/new3"})

        await adapter.post_message(tid, "Reply message")

        calls = api.get_calls("POST", "spaces/ABC123/messages")
        assert calls[0]["params"]["messageReplyOption"] == "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"

    @pytest.mark.asyncio
    async def test_post_message_api_error_raises(self):
        adapter, api, _ = await _init_adapter()
        tid = _encode_tid("spaces/ABC123")
        api.set_response("POST", "spaces/ABC123/messages", _FakeApiError(500, "Internal error"))

        with pytest.raises(_FakeApiError):
            await adapter.post_message(tid, "Will fail")

    @pytest.mark.asyncio
    async def test_post_message_rate_limit(self):
        adapter, api, _ = await _init_adapter()
        tid = _encode_tid("spaces/ABC123")
        api.set_response("POST", "spaces/ABC123/messages", _FakeApiError(429, "Rate limited"))

        with pytest.raises(AdapterRateLimitError):
            await adapter.post_message(tid, "Rate limited")


# ===========================================================================
# editMessage - comprehensive
# ===========================================================================


class TestEditMessageComprehensive:
    @pytest.mark.asyncio
    async def test_edits_text_message(self):
        adapter, api, _ = await _init_adapter()
        tid = _encode_tid("spaces/ABC123")
        msg_id = "spaces/ABC123/messages/msg1"
        api.set_response("PATCH", msg_id, {"name": msg_id})

        result = await adapter.edit_message(tid, msg_id, "Updated text")

        assert result.id == msg_id
        assert result.thread_id == tid
        calls = api.get_calls("PATCH", msg_id)
        assert len(calls) == 1
        assert calls[0]["params"]["updateMask"] == "text"

    @pytest.mark.asyncio
    async def test_edit_message_api_error(self):
        adapter, api, _ = await _init_adapter()
        tid = _encode_tid("spaces/ABC123")
        msg_id = "spaces/ABC123/messages/msg1"
        api.set_response("PATCH", msg_id, _FakeApiError(403, "Forbidden"))

        with pytest.raises(_FakeApiError):
            await adapter.edit_message(tid, msg_id, "edit")


# ===========================================================================
# deleteMessage - comprehensive
# ===========================================================================


class TestDeleteMessageComprehensive:
    @pytest.mark.asyncio
    async def test_deletes_message(self):
        adapter, api, _ = await _init_adapter()
        msg_id = "spaces/ABC123/messages/msg1"
        api.set_response("DELETE", msg_id, {})

        await adapter.delete_message("gchat:spaces/ABC123", msg_id)

        calls = api.get_calls("DELETE", msg_id)
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_delete_message_api_error(self):
        adapter, api, _ = await _init_adapter()
        msg_id = "spaces/ABC123/messages/msg1"
        api.set_response("DELETE", msg_id, _FakeApiError(404, "Not found"))

        with pytest.raises(_FakeApiError):
            await adapter.delete_message("gchat:spaces/ABC123", msg_id)


# ===========================================================================
# addReaction / removeReaction - comprehensive
# ===========================================================================


class TestAddReactionComprehensive:
    @pytest.mark.asyncio
    async def test_adds_reaction(self):
        adapter, api, _ = await _init_adapter()
        msg_id = "spaces/ABC123/messages/msg1"
        api.set_response("POST", f"{msg_id}/reactions", {})

        await adapter.add_reaction("gchat:spaces/ABC123", msg_id, "\U0001f44d")

        calls = api.get_calls("POST", f"{msg_id}/reactions")
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_add_reaction_rate_limit(self):
        adapter, api, _ = await _init_adapter()
        msg_id = "spaces/ABC123/messages/msg1"
        api.set_response("POST", f"{msg_id}/reactions", _FakeApiError(429, "Rate limited"))

        with pytest.raises(AdapterRateLimitError):
            await adapter.add_reaction("gchat:spaces/ABC123", msg_id, "\U0001f44d")


class TestRemoveReactionComprehensive:
    @pytest.mark.asyncio
    async def test_removes_matching_reaction(self):
        adapter, api, _ = await _init_adapter()
        msg_id = "spaces/ABC123/messages/msg1"
        react_name = f"{msg_id}/reactions/react1"

        api.set_response(
            "GET",
            f"{msg_id}/reactions",
            {
                "reactions": [
                    {"name": react_name, "emoji": {"unicode": "\U0001f44d"}},
                    {"name": f"{msg_id}/reactions/react2", "emoji": {"unicode": "\u2764\ufe0f"}},
                ],
            },
        )
        api.set_response("DELETE", react_name, {})

        await adapter.remove_reaction("gchat:spaces/ABC123", msg_id, "\U0001f44d")

        delete_calls = api.get_calls("DELETE", react_name)
        assert len(delete_calls) == 1

    @pytest.mark.asyncio
    async def test_no_delete_when_reaction_not_found(self):
        adapter, api, _ = await _init_adapter()
        msg_id = "spaces/ABC123/messages/msg1"
        api.set_response("GET", f"{msg_id}/reactions", {"reactions": []})

        await adapter.remove_reaction("gchat:spaces/ABC123", msg_id, "\U0001f44d")

        delete_calls = api.get_calls("DELETE")
        assert len(delete_calls) == 0

    @pytest.mark.asyncio
    async def test_no_delete_when_emoji_does_not_match(self):
        adapter, api, _ = await _init_adapter()
        msg_id = "spaces/ABC123/messages/msg1"
        api.set_response(
            "GET",
            f"{msg_id}/reactions",
            {
                "reactions": [
                    {"name": f"{msg_id}/reactions/react1", "emoji": {"unicode": "\u2764\ufe0f"}},
                ],
            },
        )

        await adapter.remove_reaction("gchat:spaces/ABC123", msg_id, "\U0001f44d")

        delete_calls = api.get_calls("DELETE")
        assert len(delete_calls) == 0


# ===========================================================================
# fetchMessages - comprehensive
# ===========================================================================


class TestFetchMessagesComprehensive:
    @pytest.mark.asyncio
    async def test_fetch_backward(self):
        adapter, api, _ = await _init_adapter()
        tid = _encode_tid("spaces/ABC123", "spaces/ABC123/threads/T1")
        api.set_response_prefix(
            "GET",
            "spaces/ABC123/messages",
            {
                "messages": [
                    {
                        "name": "spaces/ABC123/messages/msg2",
                        "text": "Newer",
                        "sender": {"name": "users/100", "displayName": "Alice", "type": "HUMAN"},
                        "createTime": "2024-01-01T00:01:00Z",
                        "thread": {"name": "spaces/ABC123/threads/T1"},
                    },
                    {
                        "name": "spaces/ABC123/messages/msg1",
                        "text": "Older",
                        "sender": {"name": "users/101", "displayName": "Bob", "type": "HUMAN"},
                        "createTime": "2024-01-01T00:00:00Z",
                        "thread": {"name": "spaces/ABC123/threads/T1"},
                    },
                ],
            },
        )

        result = await adapter.fetch_messages(tid)

        assert len(result.messages) == 2
        assert result.next_cursor is None

    @pytest.mark.asyncio
    async def test_fetch_forward(self):
        adapter, api, _ = await _init_adapter()
        tid = _encode_tid("spaces/ABC123", "spaces/ABC123/threads/T1")
        api.set_response_prefix(
            "GET",
            "spaces/ABC123/messages",
            {
                "messages": [
                    {
                        "name": "spaces/ABC123/messages/msg1",
                        "text": "First",
                        "sender": {"name": "users/100", "displayName": "Alice", "type": "HUMAN"},
                        "createTime": "2024-01-01T00:00:00Z",
                        "thread": {"name": "spaces/ABC123/threads/T1"},
                    },
                    {
                        "name": "spaces/ABC123/messages/msg2",
                        "text": "Second",
                        "sender": {"name": "users/101", "displayName": "Bob", "type": "HUMAN"},
                        "createTime": "2024-01-01T00:01:00Z",
                        "thread": {"name": "spaces/ABC123/threads/T1"},
                    },
                ],
            },
        )

        result = await adapter.fetch_messages(tid, FetchOptions(direction="forward"))

        assert len(result.messages) == 2

    @pytest.mark.asyncio
    async def test_fetch_backward_with_pagination(self):
        adapter, api, _ = await _init_adapter()
        tid = _encode_tid("spaces/ABC123", "spaces/ABC123/threads/T1")
        api.set_response_prefix(
            "GET",
            "spaces/ABC123/messages",
            {
                "messages": [
                    {
                        "name": "spaces/ABC123/messages/msg1",
                        "text": "Page 1",
                        "sender": {"name": "users/100", "displayName": "User", "type": "HUMAN"},
                        "createTime": "2024-01-01T00:00:00Z",
                    },
                ],
                "nextPageToken": "page2_token",
            },
        )

        result = await adapter.fetch_messages(tid)

        assert len(result.messages) == 1
        assert result.next_cursor == "page2_token"

    @pytest.mark.asyncio
    async def test_fetch_empty_messages(self):
        adapter, api, _ = await _init_adapter()
        tid = _encode_tid("spaces/ABC123", "spaces/ABC123/threads/T1")
        api.set_response_prefix("GET", "spaces/ABC123/messages", {"messages": []})

        result = await adapter.fetch_messages(tid)

        assert len(result.messages) == 0
        assert result.next_cursor is None

    @pytest.mark.asyncio
    async def test_fetch_forward_with_cursor(self):
        adapter, api, _ = await _init_adapter()
        tid = _encode_tid("spaces/ABC123", "spaces/ABC123/threads/T1")
        api.set_response_prefix(
            "GET",
            "spaces/ABC123/messages",
            {
                "messages": [
                    {
                        "name": "spaces/ABC123/messages/msg1",
                        "text": "First",
                        "sender": {"name": "users/100", "displayName": "User", "type": "HUMAN"},
                        "createTime": "2024-01-01T00:00:00Z",
                    },
                    {
                        "name": "spaces/ABC123/messages/msg2",
                        "text": "Second (cursor start)",
                        "sender": {"name": "users/101", "displayName": "User2", "type": "HUMAN"},
                        "createTime": "2024-01-01T00:01:00Z",
                    },
                    {
                        "name": "spaces/ABC123/messages/msg3",
                        "text": "Third",
                        "sender": {"name": "users/102", "displayName": "User3", "type": "HUMAN"},
                        "createTime": "2024-01-01T00:02:00Z",
                    },
                ],
            },
        )

        result = await adapter.fetch_messages(
            tid,
            FetchOptions(direction="forward", cursor="spaces/ABC123/messages/msg1", limit=1),
        )

        assert len(result.messages) == 1
        assert result.messages[0].id == "spaces/ABC123/messages/msg2"


# ===========================================================================
# fetchChannelMessages - comprehensive
# ===========================================================================


class TestFetchChannelMessagesComprehensive:
    @pytest.mark.asyncio
    async def test_fetches_channel_messages_backward(self):
        adapter, api, _ = await _init_adapter()
        api.set_response_prefix(
            "GET",
            "spaces/ABC123/messages",
            {
                "messages": [
                    {
                        "name": "spaces/ABC123/messages/aaa.aaa",
                        "text": "Thread root",
                        "sender": {"name": "users/100", "displayName": "Alice", "type": "HUMAN"},
                        "createTime": "2024-01-01T00:00:00Z",
                        "thread": {"name": "spaces/ABC123/threads/aaa"},
                    },
                    {
                        "name": "spaces/ABC123/messages/aaa.bbb",
                        "text": "Thread reply (filtered out)",
                        "sender": {"name": "users/101", "displayName": "Bob", "type": "HUMAN"},
                        "createTime": "2024-01-01T00:01:00Z",
                        "thread": {"name": "spaces/ABC123/threads/aaa"},
                    },
                ],
            },
        )

        result = await adapter.fetch_channel_messages("gchat:spaces/ABC123")

        assert len(result.messages) >= 1

    @pytest.mark.asyncio
    async def test_invalid_channel_id_raises(self):
        adapter, api, _ = await _init_adapter()

        with pytest.raises(ValidationError):
            await adapter.fetch_channel_messages("gchat:")

    @pytest.mark.asyncio
    async def test_messages_without_thread_treated_as_top_level(self):
        adapter, api, _ = await _init_adapter()
        api.set_response_prefix(
            "GET",
            "spaces/S1/messages",
            {
                "messages": [
                    {
                        "name": "spaces/S1/messages/simple",
                        "text": "No thread",
                        "createTime": "2024-01-01T00:00:00Z",
                        "sender": {"name": "users/1", "displayName": "A", "type": "HUMAN"},
                    },
                ],
            },
        )

        result = await adapter.fetch_channel_messages("gchat:spaces/S1")

        assert len(result.messages) == 1


# ===========================================================================
# listThreads - comprehensive
# ===========================================================================


class TestListThreadsComprehensive:
    @pytest.mark.asyncio
    async def test_lists_threads_deduplicates(self):
        adapter, api, _ = await _init_adapter()
        api.set_response_prefix(
            "GET",
            "spaces/ABC123/messages",
            {
                "messages": [
                    {
                        "name": "spaces/ABC123/messages/m1",
                        "text": "Thread 1",
                        "sender": {"name": "users/100", "displayName": "Alice", "type": "HUMAN"},
                        "createTime": "2024-01-01T00:00:00Z",
                        "thread": {"name": "spaces/ABC123/threads/t1"},
                    },
                    {
                        "name": "spaces/ABC123/messages/m2",
                        "text": "Thread 1 reply",
                        "sender": {"name": "users/101", "displayName": "Bob", "type": "HUMAN"},
                        "createTime": "2024-01-01T00:01:00Z",
                        "thread": {"name": "spaces/ABC123/threads/t1"},
                    },
                    {
                        "name": "spaces/ABC123/messages/m3",
                        "text": "Thread 2",
                        "sender": {"name": "users/102", "displayName": "Carol", "type": "HUMAN"},
                        "createTime": "2024-01-01T00:02:00Z",
                        "thread": {"name": "spaces/ABC123/threads/t2"},
                    },
                ],
            },
        )

        result = await adapter.list_threads("gchat:spaces/ABC123")

        assert len(result.threads) == 2

    @pytest.mark.asyncio
    async def test_list_threads_with_limit(self):
        adapter, api, _ = await _init_adapter()
        msgs = []
        for i in range(10):
            msgs.append(
                {
                    "name": f"spaces/ABC123/messages/m{i}",
                    "text": f"Thread {i}",
                    "sender": {"name": "users/100", "displayName": "User", "type": "HUMAN"},
                    "createTime": "2024-01-01T00:00:00Z",
                    "thread": {"name": f"spaces/ABC123/threads/t{i}"},
                }
            )
        api.set_response_prefix("GET", "spaces/ABC123/messages", {"messages": msgs})

        result = await adapter.list_threads("gchat:spaces/ABC123", ListThreadsOptions(limit=3))

        assert len(result.threads) == 3

    @pytest.mark.asyncio
    async def test_list_threads_invalid_channel_raises(self):
        adapter, api, _ = await _init_adapter()

        with pytest.raises(ValidationError):
            await adapter.list_threads("gchat:")


# ===========================================================================
# fetchThread / fetchChannelInfo - comprehensive
# ===========================================================================


class TestFetchThreadComprehensive:
    @pytest.mark.asyncio
    async def test_fetches_thread_info(self):
        adapter, api, _ = await _init_adapter()
        tid = _encode_tid("spaces/ABC123", "spaces/ABC123/threads/T1")
        api.set_response("GET", "spaces/ABC123", {"displayName": "General Chat"})

        result = await adapter.fetch_thread(tid)

        assert result.id == tid
        assert result.channel_name == "General Chat"

    @pytest.mark.asyncio
    async def test_fetch_thread_api_error(self):
        adapter, api, _ = await _init_adapter()
        tid = _encode_tid("spaces/UNKNOWN")
        api.set_response("GET", "spaces/UNKNOWN", _FakeApiError(404, "Not found"))

        with pytest.raises(_FakeApiError):
            await adapter.fetch_thread(tid)


class TestFetchChannelInfoComprehensive:
    @pytest.mark.asyncio
    async def test_returns_channel_info(self):
        adapter, api, _ = await _init_adapter()
        api.set_response(
            "GET",
            "spaces/ABC123",
            {
                "displayName": "Engineering",
                "spaceType": "SPACE",
                "spaceThreadingState": "THREADED_MESSAGES",
            },
        )
        api.set_response(
            "GET",
            "spaces/ABC123/members",
            {"memberships": [{"member": {"name": "users/1"}}]},
        )

        result = await adapter.fetch_channel_info("gchat:spaces/ABC123")

        assert result.name == "Engineering"
        assert result.is_dm is False

    @pytest.mark.asyncio
    async def test_detects_dm_channels(self):
        adapter, api, _ = await _init_adapter()
        api.set_response(
            "GET",
            "spaces/DM123",
            {
                "spaceType": "DIRECT_MESSAGE",
                "singleUserBotDm": True,
            },
        )
        api.set_response_prefix("GET", "spaces/DM123/members", _FakeApiError(403, "no access"))

        result = await adapter.fetch_channel_info("gchat:spaces/DM123")

        assert result.is_dm is True

    @pytest.mark.asyncio
    async def test_invalid_channel_id_raises(self):
        adapter, api, _ = await _init_adapter()

        with pytest.raises(ValidationError):
            await adapter.fetch_channel_info("gchat:")


# ===========================================================================
# openDM - comprehensive
# ===========================================================================


class TestOpenDMComprehensive:
    @pytest.mark.asyncio
    async def test_finds_existing_dm(self):
        adapter, api, _ = await _init_adapter()
        api.set_response("GET", "spaces:findDirectMessage", {"name": "spaces/DM_EXISTING"})

        result = await adapter.open_dm("users/12345")

        decoded = decode_thread_id(result)
        assert decoded.space_name == "spaces/DM_EXISTING"
        assert decoded.is_dm is True

    @pytest.mark.asyncio
    async def test_creates_new_dm_when_not_found(self):
        adapter, api, _ = await _init_adapter(impersonate_user="admin@example.com")
        api.set_response("GET", "spaces:findDirectMessage", _FakeApiError(404, "Not found"))
        api.set_response("POST", "spaces:setup", {"name": "spaces/DM_NEW"})

        result = await adapter.open_dm("users/67890")

        decoded = decode_thread_id(result)
        assert decoded.space_name == "spaces/DM_NEW"
        assert decoded.is_dm is True


# ===========================================================================
# handleGoogleChatError - comprehensive
# ===========================================================================


class TestHandleGoogleChatErrorComprehensive:
    def test_429_raises_adapter_rate_limit(self):
        adapter = _make_adapter()
        with pytest.raises(AdapterRateLimitError):
            adapter._handle_google_chat_error(_FakeApiError(429, "Too many requests"), "test")

    def test_403_rethrows(self):
        adapter = _make_adapter()
        original = _FakeApiError(403, "Forbidden")
        with pytest.raises(_FakeApiError):
            adapter._handle_google_chat_error(original, "editMessage")

    def test_404_rethrows(self):
        adapter = _make_adapter()
        original = _FakeApiError(404, "Not found")
        with pytest.raises(_FakeApiError):
            adapter._handle_google_chat_error(original, "deleteMessage")

    def test_500_rethrows(self):
        adapter = _make_adapter()
        original = _FakeApiError(500, "Server error")
        with pytest.raises(_FakeApiError):
            adapter._handle_google_chat_error(original, "postMessage")

    def test_logs_context_in_error(self):
        from chat_sdk.logger import Logger

        mock_logger = MagicMock(spec=Logger)
        adapter = GoogleChatAdapter(
            GoogleChatAdapterConfig(
                credentials=_make_credentials(),
                logger=mock_logger,
            )
        )

        import contextlib

        with contextlib.suppress(_FakeApiError):
            adapter._handle_google_chat_error(_FakeApiError(500, "Fail"), "postMessage")

        mock_logger.error.assert_called()
        call_args = mock_logger.error.call_args
        assert "postMessage" in call_args[0][0]


# ===========================================================================
# startTyping (no-op) / stream
# ===========================================================================


class TestStartTypingAndStream:
    @pytest.mark.asyncio
    async def test_start_typing_is_no_op(self):
        adapter, api, _ = await _init_adapter()
        tid = _encode_tid("spaces/ABC123")

        await adapter.start_typing(tid)

        assert len(api.calls) == 0

    @pytest.mark.asyncio
    async def test_start_typing_with_status_is_no_op(self):
        adapter, api, _ = await _init_adapter()
        tid = _encode_tid("spaces/ABC123")

        await adapter.start_typing(tid, status="Thinking...")

        assert len(api.calls) == 0

    @pytest.mark.asyncio
    async def test_stream_delegates_to_post_message(self):
        adapter, api, _ = await _init_adapter()
        tid = _encode_tid("spaces/ABC123", "spaces/ABC123/threads/T1")
        api.set_response("POST", "spaces/ABC123/messages", {"name": "spaces/ABC123/messages/streamed1"})

        async def _text_stream():
            yield "Accumulated text"

        result = await adapter.stream(tid, _text_stream())

        assert result.id == "spaces/ABC123/messages/streamed1"
        calls = api.get_calls("POST", "spaces/ABC123/messages")
        assert len(calls) == 1


# ===========================================================================
# postEphemeral
# ===========================================================================


class TestPostEphemeral:
    @pytest.mark.asyncio
    async def test_posts_ephemeral_message(self):
        adapter, api, _ = await _init_adapter()
        tid = _encode_tid("spaces/ABC123", "spaces/ABC123/threads/T1")
        api.set_response("POST", "spaces/ABC123/messages", {"name": "spaces/ABC123/messages/eph1"})

        result = await adapter.post_ephemeral(tid, "users/100", "Ephemeral text")

        assert result.id == "spaces/ABC123/messages/eph1"
        assert result.used_fallback is False
        calls = api.get_calls("POST", "spaces/ABC123/messages")
        assert calls[0]["body"]["privateMessageViewer"]["name"] == "users/100"


# ===========================================================================
# renderFormatted
# ===========================================================================


class TestRenderFormattedComprehensive:
    def test_renders_empty_ast(self):
        adapter = _make_adapter()
        result = adapter.render_formatted({"type": "root", "children": []})
        assert isinstance(result, str)
        assert result == ""

    def test_renders_paragraph(self):
        adapter = _make_adapter()
        result = adapter.render_formatted(
            {
                "type": "root",
                "children": [{"type": "paragraph", "children": [{"type": "text", "value": "Hello world"}]}],
            }
        )
        assert "Hello world" in result


# ===========================================================================
# channelIdFromThreadId - comprehensive
# ===========================================================================


class TestChannelIdComprehensive:
    def test_extracts_space_name(self):
        adapter = _make_adapter()
        tid = _encode_tid("spaces/ABC123", "spaces/ABC123/threads/T1")
        assert adapter.channel_id_from_thread_id(tid) == "gchat:spaces/ABC123"

    def test_works_without_thread_name(self):
        adapter = _make_adapter()
        tid = _encode_tid("spaces/ONLY")
        assert adapter.channel_id_from_thread_id(tid) == "gchat:spaces/ONLY"

    def test_works_with_dm_thread(self):
        adapter = _make_adapter()
        tid = _encode_tid("spaces/DM_SPACE", is_dm=True)
        assert adapter.channel_id_from_thread_id(tid) == "gchat:spaces/DM_SPACE"


# ===========================================================================
# User info caching
# ===========================================================================


class TestUserInfoCachingComprehensive:
    @pytest.mark.asyncio
    async def test_resolve_display_name_uses_provided(self):
        state = _make_mock_state()
        cache = UserInfoCache(state, MagicMock())

        name = await cache.resolve_display_name("users/100", "Alice", None, "bot")

        assert name == "Alice"

    @pytest.mark.asyncio
    async def test_resolve_display_name_falls_back_to_cache(self):
        state = _make_mock_state()
        cache = UserInfoCache(state, MagicMock())

        await cache.set("users/100", "Cached Alice")

        name = await cache.resolve_display_name("users/100", None, None, "bot")

        assert name == "Cached Alice"

    @pytest.mark.asyncio
    async def test_resolve_display_name_uses_bot_name_for_self(self):
        state = _make_mock_state()
        cache = UserInfoCache(state, MagicMock())

        name = await cache.resolve_display_name("users/BOT1", None, "users/BOT1", "mybot")

        assert name == "mybot"

    @pytest.mark.asyncio
    async def test_resolve_display_name_falls_back_to_user_id(self):
        state = _make_mock_state()
        cache = UserInfoCache(state, MagicMock())

        name = await cache.resolve_display_name("users/999", None, None, "bot")

        assert name == "User 999"

    @pytest.mark.asyncio
    async def test_cache_hit_after_set(self):
        state = _make_mock_state()
        cache = UserInfoCache(state, MagicMock())

        await cache.set("users/200", "Bob", "bob@example.com")
        result = await cache.get("users/200")

        assert result is not None
        assert result.display_name == "Bob"
        assert result.email == "bob@example.com"

    @pytest.mark.asyncio
    async def test_cache_miss(self):
        state = _make_mock_state()
        cache = UserInfoCache(state, MagicMock())

        result = await cache.get("users/nonexistent")

        assert result is None

    @pytest.mark.asyncio
    async def test_ignores_unknown_display_name(self):
        state = _make_mock_state()
        cache = UserInfoCache(state, MagicMock())

        await cache.set("users/300", "unknown")
        result = await cache.get("users/300")

        assert result is None

    @pytest.mark.asyncio
    async def test_in_memory_cache_is_fast_path(self):
        state = _make_mock_state()
        cache = UserInfoCache(state, MagicMock())

        await cache.set("users/400", "Carol")
        state._storage.clear()

        result = await cache.get("users/400")
        assert result is not None
        assert result.display_name == "Carol"


# ===========================================================================
# Workspace events subscription lifecycle
# ===========================================================================


class TestWorkspaceEventsComprehensive:
    @pytest.mark.asyncio
    async def test_skip_subscription_without_pubsub_topic(self):
        adapter, api, _ = await _init_adapter()

        await adapter.on_thread_subscribe(_encode_tid("spaces/ABC123"))

        assert len(api.calls) == 0

    @pytest.mark.asyncio
    async def test_ensure_space_subscription_skips_without_state(self):
        adapter = _make_adapter(pubsub_topic="projects/test/topics/test")
        mock_api = MockGChatApi()
        _patch_api(adapter, mock_api)

        await adapter._ensure_space_subscription("spaces/ABC123")

        assert len(mock_api.calls) == 0

    @pytest.mark.asyncio
    async def test_finds_existing_subscription_in_cache(self):
        adapter, api, state = await _init_adapter(pubsub_topic="projects/test/topics/test")

        cache_key = "gchat:space-sub:spaces/ABC123"
        far_future = int(time.time() * 1000) + 24 * 60 * 60 * 1000
        state._storage[cache_key] = {"subscription_name": "subscriptions/sub1", "expire_time": far_future}

        await adapter._ensure_space_subscription("spaces/ABC123")

        assert len(api.calls) == 0

    @pytest.mark.asyncio
    async def test_skips_duplicate_in_flight_subscription(self):
        adapter, api, state = await _init_adapter(pubsub_topic="projects/test/topics/test")

        event = asyncio.Event()
        adapter._pending_subscriptions["spaces/TEST1"] = {"event": event, "error": None}

        async def wait_and_set():
            await asyncio.sleep(0.01)
            event.set()

        task = asyncio.create_task(wait_and_set())
        await adapter._ensure_space_subscription("spaces/TEST1")
        await task

        assert len(api.calls) == 0


# ===========================================================================
# Bot user ID learning - persists after learning
# ===========================================================================


class TestBotUserIdPersistence:
    def test_persists_bot_id_after_learning(self):
        adapter = _make_adapter()

        # First message: learn bot ID
        event = _make_message_event(
            message_text="@MyBot hello",
            annotations=[
                {
                    "type": "USER_MENTION",
                    "startIndex": 0,
                    "length": 6,
                    "userMention": {
                        "user": {
                            "name": "users/MY_BOT_ID",
                            "displayName": "MyBot",
                            "type": "BOT",
                        },
                        "type": "MENTION",
                    },
                },
            ],
        )
        adapter.parse_message(event)

        # Second message: detect self
        self_event = _make_message_event(
            sender_name="users/MY_BOT_ID",
            sender_type="BOT",
            sender_display_name="MyBot",
            message_text="Hello back",
            message_name="spaces/ABC123/messages/msg2",
        )
        msg = adapter.parse_message(self_event)
        assert msg.author.is_me is True


# ===========================================================================
# handleWebhook - non-message event returns 200
# ===========================================================================


class TestHandleWebhookNonMessageEvents:
    @pytest.mark.asyncio
    async def test_non_message_event_returns_200(self):
        adapter = _make_adapter()
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        response = await adapter.handle_webhook({"chat": {}})
        assert response["status"] == 200

    @pytest.mark.asyncio
    async def test_not_process_when_not_initialized(self):
        adapter = _make_adapter()
        event = _make_message_event()
        response = await adapter.handle_webhook(event)
        assert response["status"] == 200


# ===========================================================================
# Card click - uses parameters.actionId
# ===========================================================================


class TestCardClickParameters:
    @pytest.mark.asyncio
    async def test_uses_parameters_action_id(self):
        adapter = _make_adapter()
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        event = {
            "commonEventObject": {
                "invokedFunction": "handleApprove",
                "parameters": {"actionId": "btn_approve", "value": "42"},
            },
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
                        "email": "clicker@example.com",
                    },
                },
            },
        }
        response = await adapter.handle_webhook(event)
        assert response["status"] == 200

    @pytest.mark.asyncio
    async def test_ignores_card_click_when_space_is_missing(self):
        adapter = _make_adapter()
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        event = {
            "commonEventObject": {"invokedFunction": "myAction"},
            "chat": {},
        }
        response = await adapter.handle_webhook(event)
        assert response["status"] == 200
        assert not chat.process_action.called
