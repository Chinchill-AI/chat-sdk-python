"""Tests for critical bug fixes and high-priority improvements.

Covers:
1. Telegram process_action uses snake_case keys
2. Telegram process_reaction uses snake_case keys
3. Google Chat stream() accepts AsyncIterable
4. Teams add_reaction / remove_reaction don't raise (just warn)
5. WhatsApp post_message calls Graph API correctly
6. WhatsApp add_reaction sends correct emoji payload
7. WhatsApp stream accumulates and posts
8. GitHub happy-path webhook dispatch calls process_message
9. GitHub self-message suppression
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

from chat_sdk.adapters.github.adapter import GitHubAdapter
from chat_sdk.adapters.google_chat.adapter import GoogleChatAdapter
from chat_sdk.adapters.google_chat.types import (
    GoogleChatAdapterConfig,
    ServiceAccountCredentials,
)
from chat_sdk.adapters.teams.adapter import TeamsAdapter
from chat_sdk.adapters.teams.types import TeamsAdapterConfig
from chat_sdk.adapters.telegram.adapter import TelegramAdapter
from chat_sdk.adapters.telegram.types import TelegramAdapterConfig
from chat_sdk.adapters.whatsapp.adapter import WhatsAppAdapter
from chat_sdk.adapters.whatsapp.types import WhatsAppAdapterConfig
from chat_sdk.logger import ConsoleLogger
from chat_sdk.types import RawMessage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_telegram_adapter(**overrides) -> TelegramAdapter:
    config = TelegramAdapterConfig(
        bot_token=overrides.pop("bot_token", "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"),
        **overrides,
    )
    return TelegramAdapter(config)


def _make_teams_adapter(**overrides) -> TeamsAdapter:
    config = TeamsAdapterConfig(
        app_id=overrides.pop("app_id", "test-app-id"),
        app_password=overrides.pop("app_password", "test-password"),
        **overrides,
    )
    return TeamsAdapter(config)


def _make_whatsapp_adapter(**overrides) -> WhatsAppAdapter:
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


def _make_github_adapter(**overrides) -> GitHubAdapter:
    defaults: dict[str, Any] = {
        "webhook_secret": "test-webhook-secret",
        "token": "ghp_testtoken",
        "logger": ConsoleLogger("error"),
    }
    defaults.update(overrides)
    return GitHubAdapter(defaults)


def _make_google_chat_adapter() -> GoogleChatAdapter:
    config = GoogleChatAdapterConfig(
        credentials=ServiceAccountCredentials(
            client_email="bot@project.iam.gserviceaccount.com",
            private_key="-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----",
            project_id="test-project",
        ),
    )
    return GoogleChatAdapter(config)


# ---------------------------------------------------------------------------
# GitHub webhook helpers
# ---------------------------------------------------------------------------

WEBHOOK_SECRET = "test-webhook-secret"


def _github_sign(body: str, secret: str = WEBHOOK_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def _issue_comment_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "action": "created",
        "comment": {
            "id": 100,
            "body": "Hello from test",
            "user": {"id": 1, "login": "testuser", "type": "User"},
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-01T00:00:00Z",
            "html_url": "https://github.com/acme/app/pull/42#issuecomment-100",
        },
        "issue": {
            "number": 42,
            "title": "Test PR",
            "pull_request": {"url": "https://api.github.com/repos/acme/app/pulls/42"},
        },
        "repository": {
            "id": 1,
            "name": "app",
            "full_name": "acme/app",
            "owner": {"id": 10, "login": "acme", "type": "Organization"},
        },
        "sender": {"id": 1, "login": "testuser", "type": "User"},
    }
    base.update(overrides)
    return base


@dataclass
class _FakeRequest:
    url: str
    method: str
    _body: str
    headers: dict[str, str]

    async def text(self) -> str:
        return self._body


def _make_github_request(
    body: str,
    event_type: str,
    *,
    signature: str | None = None,
) -> _FakeRequest:
    headers: dict[str, str] = {
        "content-type": "application/json",
        "x-github-event": event_type,
    }
    if signature is not None:
        headers["x-hub-signature-256"] = signature
    return _FakeRequest(
        url="https://example.com/api/webhooks/github",
        method="POST",
        _body=body,
        headers=headers,
    )


# ---------------------------------------------------------------------------
# 1. Telegram process_action uses snake_case keys
# ---------------------------------------------------------------------------


class TestTelegramProcessActionSnakeCase:
    """Verify that Telegram callback queries pass snake_case keys to process_action."""

    def test_process_action_uses_snake_case_keys(self):
        adapter = _make_telegram_adapter()
        mock_chat = MagicMock()
        mock_chat.process_action = MagicMock()
        adapter._chat = mock_chat

        callback_query = {
            "id": "cb-1",
            "data": "action:approve:val123",
            "message": {
                "message_id": 10,
                "chat": {"id": -1001234567890},
                "message_thread_id": None,
            },
            "from": {
                "id": 42,
                "is_bot": False,
                "first_name": "Alice",
                "last_name": "Smith",
                "username": "alice",
            },
        }

        adapter.handle_callback_query(callback_query)

        mock_chat.process_action.assert_called_once()
        event = mock_chat.process_action.call_args[0][0]

        # Must be an ActionEvent dataclass with snake_case attributes
        from chat_sdk.types import ActionEvent

        assert isinstance(event, ActionEvent)
        assert hasattr(event, "action_id")
        assert hasattr(event, "message_id")
        assert hasattr(event, "thread_id")
        # Ensure camelCase attributes are absent
        assert not hasattr(event, "actionId")
        assert not hasattr(event, "messageId")
        assert not hasattr(event, "threadId")


# ---------------------------------------------------------------------------
# 2. Telegram process_reaction uses snake_case keys
# ---------------------------------------------------------------------------


class TestTelegramProcessReactionSnakeCase:
    """Verify that Telegram reaction updates pass snake_case keys to process_reaction."""

    def test_process_reaction_uses_snake_case_keys(self):
        adapter = _make_telegram_adapter()
        mock_chat = MagicMock()
        mock_chat.process_reaction = MagicMock()
        adapter._chat = mock_chat

        reaction_update = {
            "chat": {"id": -1001234567890, "type": "supergroup", "title": "Test"},
            "message_id": 99,
            "user": {
                "id": 42,
                "is_bot": False,
                "first_name": "Bob",
                "username": "bob",
            },
            "date": 1700000000,
            "old_reaction": [],
            "new_reaction": [{"type": "emoji", "emoji": "\U0001f44d"}],
        }

        adapter.handle_message_reaction_update(reaction_update)

        mock_chat.process_reaction.assert_called_once()
        event = mock_chat.process_reaction.call_args[0][0]

        # Must be a ReactionEvent dataclass with snake_case attributes
        from chat_sdk.types import ReactionEvent

        assert isinstance(event, ReactionEvent)
        assert hasattr(event, "thread_id")
        assert hasattr(event, "message_id")
        assert hasattr(event, "raw_emoji")
        # Ensure camelCase attributes are absent
        assert not hasattr(event, "threadId")
        assert not hasattr(event, "messageId")
        assert not hasattr(event, "rawEmoji")
        # Verify added flag
        assert event.added is True

    def test_reaction_removal_uses_snake_case_keys(self):
        adapter = _make_telegram_adapter()
        mock_chat = MagicMock()
        mock_chat.process_reaction = MagicMock()
        adapter._chat = mock_chat

        reaction_update = {
            "chat": {"id": -1001234567890, "type": "supergroup", "title": "Test"},
            "message_id": 99,
            "user": {
                "id": 42,
                "is_bot": False,
                "first_name": "Bob",
                "username": "bob",
            },
            "date": 1700000000,
            "old_reaction": [{"type": "emoji", "emoji": "\U0001f44d"}],
            "new_reaction": [],
        }

        adapter.handle_message_reaction_update(reaction_update)

        mock_chat.process_reaction.assert_called_once()
        event = mock_chat.process_reaction.call_args[0][0]

        from chat_sdk.types import ReactionEvent

        assert isinstance(event, ReactionEvent)
        assert hasattr(event, "thread_id")
        assert hasattr(event, "message_id")
        assert hasattr(event, "raw_emoji")
        assert event.added is False


# ---------------------------------------------------------------------------
# 3. Google Chat stream() accepts AsyncIterable
# ---------------------------------------------------------------------------


class TestGoogleChatStreamSignature:
    """Verify that Google Chat stream() accepts AsyncIterable[str | StreamChunk]."""

    @pytest.mark.asyncio
    async def test_stream_accumulates_text_chunks(self):
        adapter = _make_google_chat_adapter()

        posted: list[tuple[str, Any]] = []

        async def fake_post_message(thread_id: str, message: Any) -> RawMessage:
            posted.append((thread_id, message))
            return RawMessage(id="msg-1", thread_id=thread_id, raw={})

        adapter.post_message = fake_post_message  # type: ignore[assignment]

        async def text_stream():
            yield "Hello "
            yield "world"

        result = await adapter.stream("gchat:spaces/abc", text_stream())

        assert result.id == "msg-1"
        assert len(posted) == 1
        assert posted[0][0] == "gchat:spaces/abc"
        assert posted[0][1].markdown == "Hello world"

    @pytest.mark.asyncio
    async def test_stream_handles_markdown_text_chunks(self):
        adapter = _make_google_chat_adapter()

        posted: list[tuple[str, Any]] = []

        async def fake_post_message(thread_id: str, message: Any) -> RawMessage:
            posted.append((thread_id, message))
            return RawMessage(id="msg-2", thread_id=thread_id, raw={})

        adapter.post_message = fake_post_message  # type: ignore[assignment]

        @dataclass
        class FakeChunk:
            type: str
            text: str

        async def mixed_stream():
            yield "Start "
            yield FakeChunk(type="markdown_text", text="middle ")
            yield "end"

        await adapter.stream("gchat:spaces/xyz", mixed_stream())

        assert posted[0][1].markdown == "Start middle end"


# ---------------------------------------------------------------------------
# 4. Teams add_reaction / remove_reaction don't raise
# ---------------------------------------------------------------------------


class TestTeamsReactionsGraceful:
    """Verify Teams reactions log a warning instead of raising NotImplementedError."""

    @pytest.mark.asyncio
    async def test_add_reaction_does_not_raise(self):
        mock_logger = MagicMock(
            debug=MagicMock(),
            info=MagicMock(),
            warn=MagicMock(),
            error=MagicMock(),
        )
        adapter = _make_teams_adapter(logger=mock_logger)

        # Should not raise
        await adapter.add_reaction("teams:conv123", "msg456", "thumbs_up")

        mock_logger.warn.assert_called_once()
        assert "not supported" in mock_logger.warn.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_remove_reaction_does_not_raise(self):
        mock_logger = MagicMock(
            debug=MagicMock(),
            info=MagicMock(),
            warn=MagicMock(),
            error=MagicMock(),
        )
        adapter = _make_teams_adapter(logger=mock_logger)

        # Should not raise
        await adapter.remove_reaction("teams:conv123", "msg456", "thumbs_up")

        mock_logger.warn.assert_called_once()
        assert "not supported" in mock_logger.warn.call_args[0][0].lower()


# ---------------------------------------------------------------------------
# 5. WhatsApp post_message calls Graph API correctly
# ---------------------------------------------------------------------------


class TestWhatsAppPostMessage:
    """Verify that WhatsApp post_message sends the right payload to the Graph API."""

    @pytest.mark.asyncio
    async def test_post_message_sends_text_via_graph_api(self):
        adapter = _make_whatsapp_adapter()

        captured_calls: list[tuple[str, Any]] = []

        async def fake_graph_api(path: str, body: Any) -> dict:
            captured_calls.append((path, body))
            return {"messages": [{"id": "wamid.123"}]}

        adapter._graph_api_request = fake_graph_api  # type: ignore[assignment]

        result = await adapter.post_message(
            "whatsapp:1234567890:15551234567",
            {"markdown": "Test message"},
        )

        assert result.id == "wamid.123"
        assert result.thread_id == "whatsapp:1234567890:15551234567"
        assert len(captured_calls) == 1

        path, body = captured_calls[0]
        assert "/1234567890/messages" in path
        assert body["messaging_product"] == "whatsapp"
        assert body["to"] == "15551234567"
        assert body["type"] == "text"
        assert body["text"]["body"] == "Test message"


# ---------------------------------------------------------------------------
# 6. WhatsApp add_reaction sends correct emoji payload
# ---------------------------------------------------------------------------


class TestWhatsAppAddReaction:
    """Verify that WhatsApp add_reaction sends the right payload."""

    @pytest.mark.asyncio
    async def test_add_reaction_sends_emoji_payload(self):
        adapter = _make_whatsapp_adapter()

        captured_calls: list[tuple[str, Any]] = []

        async def fake_graph_api(path: str, body: Any) -> dict:
            captured_calls.append((path, body))
            return {}

        adapter._graph_api_request = fake_graph_api  # type: ignore[assignment]

        await adapter.add_reaction(
            "whatsapp:1234567890:15551234567",
            "wamid.msg-1",
            "\U0001f44d",
        )

        assert len(captured_calls) == 1
        path, body = captured_calls[0]
        assert "/1234567890/messages" in path
        assert body["type"] == "reaction"
        assert body["reaction"]["message_id"] == "wamid.msg-1"
        assert body["reaction"]["emoji"] == "\U0001f44d"


# ---------------------------------------------------------------------------
# 7. WhatsApp stream accumulates and posts
# ---------------------------------------------------------------------------


class TestWhatsAppStream:
    """Verify that WhatsApp stream accumulates chunks and posts as a single message."""

    @pytest.mark.asyncio
    async def test_stream_accumulates_and_posts(self):
        adapter = _make_whatsapp_adapter()

        posted: list[tuple[str, Any]] = []

        async def fake_post_message(thread_id: str, message: Any) -> RawMessage:
            posted.append((thread_id, message))
            return RawMessage(id="wamid.streamed", thread_id=thread_id, raw={})

        adapter.post_message = fake_post_message  # type: ignore[assignment]

        async def text_stream():
            yield "chunk1 "
            yield "chunk2 "
            yield "chunk3"

        result = await adapter.stream(
            "whatsapp:1234567890:15551234567",
            text_stream(),
        )

        assert result.id == "wamid.streamed"
        assert len(posted) == 1
        assert posted[0][1].markdown == "chunk1 chunk2 chunk3"


# ---------------------------------------------------------------------------
# 8. GitHub happy-path webhook dispatch calls process_message
# ---------------------------------------------------------------------------


class TestGitHubWebhookHappyPath:
    """Verify that a valid issue_comment webhook dispatches to process_message."""

    @pytest.mark.asyncio
    async def test_issue_comment_dispatches_process_message(self):
        adapter = _make_github_adapter()
        mock_chat = MagicMock()
        mock_chat.process_message = MagicMock()
        await adapter.initialize(mock_chat)

        payload = _issue_comment_payload()
        body = json.dumps(payload)
        sig = _github_sign(body)
        request = _make_github_request(body, "issue_comment", signature=sig)

        response = await adapter.handle_webhook(request)

        assert response["status"] == 200
        mock_chat.process_message.assert_called_once()

        # Verify the adapter and thread_id were passed
        call_args = mock_chat.process_message.call_args[0]
        assert call_args[0] is adapter  # first arg is the adapter
        assert call_args[1] == "github:acme/app:42"  # thread_id


# ---------------------------------------------------------------------------
# 9. GitHub self-message suppression
# ---------------------------------------------------------------------------


class TestGitHubSelfMessageSuppression:
    """Verify that GitHub ignores messages from the bot itself."""

    @pytest.mark.asyncio
    async def test_ignores_message_from_bot(self):
        adapter = _make_github_adapter(bot_user_id=1)
        mock_chat = MagicMock()
        mock_chat.process_message = MagicMock()
        await adapter.initialize(mock_chat)

        # sender.id matches bot_user_id
        payload = _issue_comment_payload(
            sender={"id": 1, "login": "my-bot[bot]", "type": "Bot"},
            comment={
                "id": 100,
                "body": "Bot reply",
                "user": {"id": 1, "login": "my-bot[bot]", "type": "Bot"},
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
                "html_url": "https://github.com/acme/app/pull/42#issuecomment-100",
            },
        )
        body = json.dumps(payload)
        sig = _github_sign(body)
        request = _make_github_request(body, "issue_comment", signature=sig)

        response = await adapter.handle_webhook(request)

        assert response["status"] == 200
        mock_chat.process_message.assert_not_called()
