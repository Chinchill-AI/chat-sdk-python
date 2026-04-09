"""Tests for the Linear adapter -- constructor, thread IDs, webhook handling, message parsing.

Ported from packages/adapter-linear/src/index.test.ts.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from unittest.mock import MagicMock

import pytest

from chat_sdk.adapters.linear.adapter import LinearAdapter
from chat_sdk.adapters.linear.types import (
    LinearAdapterAPIKeyConfig,
    LinearAdapterAppConfig,
    LinearAdapterBaseConfig,
    LinearAdapterOAuthConfig,
    LinearThreadId,
)
from chat_sdk.shared.errors import ValidationError

WEBHOOK_SECRET = "test-webhook-secret"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_logger():
    return MagicMock(
        debug=MagicMock(),
        info=MagicMock(),
        warn=MagicMock(),
        error=MagicMock(),
    )


def _make_adapter(**overrides) -> LinearAdapter:
    """Create a LinearAdapter with minimal valid config."""
    config = LinearAdapterAPIKeyConfig(
        api_key=overrides.pop("api_key", "test-api-key"),
        webhook_secret=overrides.pop("webhook_secret", "test-secret"),
        user_name=overrides.pop("user_name", "test-bot"),
        logger=overrides.pop("logger", _make_logger()),
    )
    return LinearAdapter(config)


def _make_webhook_adapter(logger=None) -> LinearAdapter:
    """Create adapter with the known webhook secret."""
    if logger is None:
        logger = _make_logger()
    config = LinearAdapterAPIKeyConfig(
        api_key="test-api-key",
        webhook_secret=WEBHOOK_SECRET,
        user_name="test-bot",
        logger=logger,
    )
    return LinearAdapter(config)


def _sign_payload(body: str, secret: str = WEBHOOK_SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()


class _FakeRequest:
    """A simple request-like object for testing webhook handlers."""

    def __init__(self, body: str, headers: dict[str, str] | None = None):
        self._body = body
        self.headers = headers or {}

    async def text(self) -> str:
        return self._body

    @property
    def data(self) -> bytes:
        return self._body.encode("utf-8")


def _build_webhook_request(body: str, signature: str | None = None):
    headers: dict[str, str] = {"content-type": "application/json"}
    if signature is not None:
        headers["linear-signature"] = signature
    return _FakeRequest(body, headers)


def _create_comment_payload(
    action: str = "create",
    user_id: str = "user-456",
    issue_id: str = "issue-123",
    comment_id: str = "comment-abc",
    parent_id: str | None = None,
    body: str = "Hello from webhook",
    actor_type: str = "user",
) -> dict:
    return {
        "type": "Comment",
        "action": action,
        "createdAt": "2025-06-01T12:00:00.000Z",
        "organizationId": "org-123",
        "url": "https://linear.app/test/issue/TEST-1#comment-abc",
        "webhookId": "webhook-1",
        "webhookTimestamp": int(time.time() * 1000),
        "data": {
            "id": comment_id,
            "body": body,
            "issueId": issue_id,
            "userId": user_id,
            "createdAt": "2025-06-01T12:00:00.000Z",
            "updatedAt": "2025-06-01T12:00:00.000Z",
            "parentId": parent_id,
        },
        "actor": {
            "id": user_id,
            "name": "Test User",
            "type": actor_type,
        },
    }


def _create_reaction_payload(
    action: str = "create",
    emoji: str = "\U0001f44d",
    comment_id: str = "comment-abc",
) -> dict:
    return {
        "type": "Reaction",
        "action": action,
        "createdAt": "2025-06-01T12:00:00.000Z",
        "organizationId": "org-123",
        "url": "https://linear.app/test/issue/TEST-1",
        "webhookId": "webhook-2",
        "webhookTimestamp": int(time.time() * 1000),
        "data": {
            "id": "reaction-1",
            "emoji": emoji,
            "commentId": comment_id,
            "userId": "user-456",
        },
        "actor": {
            "id": "user-456",
            "name": "Test User",
            "type": "user",
        },
    }


# ---------------------------------------------------------------------------
# encodeThreadId
# ---------------------------------------------------------------------------


class TestEncodeThreadId:
    def test_issue_level(self):
        adapter = _make_adapter()
        result = adapter.encode_thread_id(LinearThreadId(issue_id="abc123-def456-789"))
        assert result == "linear:abc123-def456-789"

    def test_uuid_issue_level(self):
        adapter = _make_adapter()
        result = adapter.encode_thread_id(LinearThreadId(issue_id="2174add1-f7c8-44e3-bbf3-2d60b5ea8bc9"))
        assert result == "linear:2174add1-f7c8-44e3-bbf3-2d60b5ea8bc9"

    def test_comment_level(self):
        adapter = _make_adapter()
        result = adapter.encode_thread_id(LinearThreadId(issue_id="issue-123", comment_id="comment-456"))
        assert result == "linear:issue-123:c:comment-456"

    def test_comment_level_uuids(self):
        adapter = _make_adapter()
        result = adapter.encode_thread_id(
            LinearThreadId(
                issue_id="2174add1-f7c8-44e3-bbf3-2d60b5ea8bc9",
                comment_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            )
        )
        assert result == "linear:2174add1-f7c8-44e3-bbf3-2d60b5ea8bc9:c:a1b2c3d4-e5f6-7890-abcd-ef1234567890"


# ---------------------------------------------------------------------------
# decodeThreadId
# ---------------------------------------------------------------------------


class TestDecodeThreadId:
    def test_issue_level(self):
        adapter = _make_adapter()
        result = adapter.decode_thread_id("linear:abc123-def456-789")
        assert result.issue_id == "abc123-def456-789"
        assert result.comment_id is None

    def test_uuid_issue_level(self):
        adapter = _make_adapter()
        result = adapter.decode_thread_id("linear:2174add1-f7c8-44e3-bbf3-2d60b5ea8bc9")
        assert result.issue_id == "2174add1-f7c8-44e3-bbf3-2d60b5ea8bc9"

    def test_comment_level(self):
        adapter = _make_adapter()
        result = adapter.decode_thread_id("linear:issue-123:c:comment-456")
        assert result.issue_id == "issue-123"
        assert result.comment_id == "comment-456"

    def test_comment_level_uuids(self):
        adapter = _make_adapter()
        result = adapter.decode_thread_id(
            "linear:2174add1-f7c8-44e3-bbf3-2d60b5ea8bc9:c:a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        )
        assert result.issue_id == "2174add1-f7c8-44e3-bbf3-2d60b5ea8bc9"
        assert result.comment_id == "a1b2c3d4-e5f6-7890-abcd-ef1234567890"

    def test_invalid_prefix(self):
        adapter = _make_adapter()
        with pytest.raises(ValidationError, match="Invalid Linear thread ID"):
            adapter.decode_thread_id("slack:C123:ts123")

    def test_empty_issue_id(self):
        adapter = _make_adapter()
        with pytest.raises(ValidationError, match="Invalid Linear thread ID"):
            adapter.decode_thread_id("linear:")

    def test_completely_wrong_format(self):
        adapter = _make_adapter()
        with pytest.raises(ValidationError, match="Invalid Linear thread ID"):
            adapter.decode_thread_id("nonsense")


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_issue_level(self):
        adapter = _make_adapter()
        original = LinearThreadId(issue_id="2174add1-f7c8-44e3-bbf3-2d60b5ea8bc9")
        encoded = adapter.encode_thread_id(original)
        decoded = adapter.decode_thread_id(encoded)
        assert decoded.issue_id == original.issue_id

    def test_comment_level(self):
        adapter = _make_adapter()
        original = LinearThreadId(
            issue_id="2174add1-f7c8-44e3-bbf3-2d60b5ea8bc9",
            comment_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        )
        encoded = adapter.encode_thread_id(original)
        decoded = adapter.decode_thread_id(encoded)
        assert decoded.issue_id == original.issue_id
        assert decoded.comment_id == original.comment_id


# ---------------------------------------------------------------------------
# renderFormatted
# ---------------------------------------------------------------------------


class TestRenderFormatted:
    def test_renders_markdown_from_ast(self):
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
    def test_parses_raw_linear_message(self):
        adapter = _make_adapter()
        raw = {
            "comment": {
                "id": "comment-abc123",
                "body": "Hello from Linear!",
                "issueId": "issue-123",
                "userId": "user-456",
                "createdAt": "2025-01-29T12:00:00.000Z",
                "updatedAt": "2025-01-29T12:00:00.000Z",
            }
        }
        msg = adapter.parse_message(raw)
        assert msg.id == "comment-abc123"
        assert msg.text == "Hello from Linear!"
        assert msg.author.user_id == "user-456"

    def test_detects_edited_messages(self):
        adapter = _make_adapter()
        raw = {
            "comment": {
                "id": "comment-abc123",
                "body": "Edited message",
                "issueId": "issue-123",
                "userId": "user-456",
                "createdAt": "2025-01-29T12:00:00.000Z",
                "updatedAt": "2025-01-29T13:00:00.000Z",
            }
        }
        msg = adapter.parse_message(raw)
        assert msg.metadata.edited is True

    def test_empty_body(self):
        adapter = _make_adapter()
        raw = {
            "comment": {
                "id": "comment-empty",
                "body": "",
                "issueId": "issue-1",
                "userId": "user-1",
                "createdAt": "2025-01-29T12:00:00.000Z",
                "updatedAt": "2025-01-29T12:00:00.000Z",
            }
        }
        msg = adapter.parse_message(raw)
        assert msg.text == ""
        assert msg.metadata.edited is False

    def test_edited_at_set_when_edited(self):
        adapter = _make_adapter()
        raw = {
            "comment": {
                "id": "comment-edited",
                "body": "Updated text",
                "issueId": "issue-1",
                "userId": "user-1",
                "createdAt": "2025-01-29T12:00:00.000Z",
                "updatedAt": "2025-01-29T14:30:00.000Z",
            }
        }
        msg = adapter.parse_message(raw)
        assert msg.metadata.edited is True
        assert msg.metadata.edited_at is not None

    def test_edited_at_none_when_not_edited(self):
        adapter = _make_adapter()
        raw = {
            "comment": {
                "id": "comment-unedited",
                "body": "Original text",
                "issueId": "issue-1",
                "userId": "user-1",
                "createdAt": "2025-01-29T12:00:00.000Z",
                "updatedAt": "2025-01-29T12:00:00.000Z",
            }
        }
        msg = adapter.parse_message(raw)
        assert msg.metadata.edited_at is None

    def test_regular_user_not_bot(self):
        adapter = _make_adapter()
        raw = {
            "comment": {
                "id": "comment-1",
                "body": "test",
                "issueId": "issue-1",
                "userId": "user-1",
                "createdAt": "2025-01-29T12:00:00.000Z",
                "updatedAt": "2025-01-29T12:00:00.000Z",
            }
        }
        msg = adapter.parse_message(raw)
        assert msg.author.is_bot is False
        assert msg.author.is_me is False


# ---------------------------------------------------------------------------
# Constructor / auth modes
# ---------------------------------------------------------------------------


class TestConstructor:
    def test_api_key_auth(self):
        config = LinearAdapterAPIKeyConfig(
            api_key="lin_api_key_123",
            webhook_secret="secret",
            user_name="my-bot",
            logger=_make_logger(),
        )
        adapter = LinearAdapter(config)
        assert adapter.name == "linear"
        assert adapter.user_name == "my-bot"

    def test_access_token_auth(self):
        config = LinearAdapterOAuthConfig(
            access_token="lin_oauth_token_123",
            webhook_secret="secret",
            user_name="my-bot",
            logger=_make_logger(),
        )
        adapter = LinearAdapter(config)
        assert adapter.name == "linear"

    def test_client_credentials_auth(self):
        config = LinearAdapterAppConfig(
            client_id="client-id",
            client_secret="client-secret",
            webhook_secret="secret",
            user_name="my-bot",
            logger=_make_logger(),
        )
        adapter = LinearAdapter(config)
        assert adapter.name == "linear"

    def test_throws_when_no_auth(self, monkeypatch):
        monkeypatch.delenv("LINEAR_API_KEY", raising=False)
        monkeypatch.delenv("LINEAR_ACCESS_TOKEN", raising=False)
        monkeypatch.delenv("LINEAR_CLIENT_ID", raising=False)
        monkeypatch.delenv("LINEAR_CLIENT_SECRET", raising=False)
        with pytest.raises(ValidationError, match="Authentication is required"):
            LinearAdapter(
                LinearAdapterBaseConfig(
                    webhook_secret="secret",
                    user_name="my-bot",
                    logger=_make_logger(),
                )
            )

    def test_bot_user_id_none_before_init(self):
        adapter = _make_adapter()
        assert adapter.bot_user_id is None


# ---------------------------------------------------------------------------
# channelIdFromThreadId
# ---------------------------------------------------------------------------


class TestChannelIdFromThreadId:
    def test_issue_level_returns_same(self):
        adapter = _make_adapter()
        result = adapter.channel_id_from_thread_id("linear:issue-123")
        assert result == "linear:issue-123"

    def test_strips_comment_part(self):
        adapter = _make_adapter()
        result = adapter.channel_id_from_thread_id("linear:issue-123:c:comment-456")
        assert result == "linear:issue-123"

    def test_throws_for_invalid(self):
        adapter = _make_adapter()
        with pytest.raises(ValidationError, match="Invalid Linear thread ID"):
            adapter.channel_id_from_thread_id("slack:C123:ts")


# ---------------------------------------------------------------------------
# Webhook - signature verification
# ---------------------------------------------------------------------------


class TestWebhookSignature:
    @pytest.mark.asyncio
    async def test_rejects_without_signature(self):
        adapter = _make_webhook_adapter()
        body = json.dumps(_create_comment_payload())
        request = _build_webhook_request(body, signature=None)
        response = await adapter.handle_webhook(request)
        assert response["status"] == 401

    @pytest.mark.asyncio
    async def test_rejects_invalid_signature(self):
        adapter = _make_webhook_adapter()
        body = json.dumps(_create_comment_payload())
        request = _build_webhook_request(body, signature="invalid-hex-signature")
        response = await adapter.handle_webhook(request)
        assert response["status"] == 401

    @pytest.mark.asyncio
    async def test_rejects_wrong_secret(self):
        adapter = _make_webhook_adapter()
        body = json.dumps(_create_comment_payload())
        wrong_sig = _sign_payload(body, "wrong-secret")
        request = _build_webhook_request(body, signature=wrong_sig)
        response = await adapter.handle_webhook(request)
        assert response["status"] == 401

    @pytest.mark.asyncio
    async def test_accepts_valid_signature(self):
        adapter = _make_webhook_adapter()
        body = json.dumps(_create_comment_payload())
        sig = _sign_payload(body)
        request = _build_webhook_request(body, signature=sig)
        response = await adapter.handle_webhook(request)
        assert response["status"] == 200


# ---------------------------------------------------------------------------
# Webhook - timestamp validation
# ---------------------------------------------------------------------------


class TestWebhookTimestamp:
    @pytest.mark.asyncio
    async def test_rejects_old_timestamp(self):
        logger = _make_logger()
        adapter = _make_webhook_adapter(logger=logger)
        payload = _create_comment_payload()
        payload["webhookTimestamp"] = int(time.time() * 1000) - 10 * 60 * 1000
        body = json.dumps(payload)
        sig = _sign_payload(body)
        request = _build_webhook_request(body, signature=sig)
        response = await adapter.handle_webhook(request)
        assert response["status"] == 401
        assert response["body"] == "Webhook expired"

    @pytest.mark.asyncio
    async def test_accepts_within_window(self):
        adapter = _make_webhook_adapter()
        payload = _create_comment_payload()
        payload["webhookTimestamp"] = int(time.time() * 1000) - 2 * 60 * 1000
        body = json.dumps(payload)
        sig = _sign_payload(body)
        request = _build_webhook_request(body, signature=sig)
        response = await adapter.handle_webhook(request)
        assert response["status"] == 200

    @pytest.mark.asyncio
    async def test_accepts_without_timestamp(self):
        adapter = _make_webhook_adapter()
        payload = _create_comment_payload()
        del payload["webhookTimestamp"]
        body = json.dumps(payload)
        sig = _sign_payload(body)
        request = _build_webhook_request(body, signature=sig)
        response = await adapter.handle_webhook(request)
        assert response["status"] == 200


# ---------------------------------------------------------------------------
# Webhook - invalid JSON
# ---------------------------------------------------------------------------


class TestWebhookInvalidJson:
    @pytest.mark.asyncio
    async def test_returns_400(self):
        logger = _make_logger()
        adapter = _make_webhook_adapter(logger=logger)
        body = "not-valid-json{{{"
        sig = _sign_payload(body)
        request = _build_webhook_request(body, signature=sig)
        response = await adapter.handle_webhook(request)
        assert response["status"] == 400
        assert response["body"] == "Invalid JSON"


# ---------------------------------------------------------------------------
# Webhook - comment created
# ---------------------------------------------------------------------------


class TestWebhookCommentCreated:
    @pytest.mark.asyncio
    async def test_processes_comment_create(self):
        logger = _make_logger()
        adapter = _make_webhook_adapter(logger=logger)
        mock_chat = MagicMock()
        mock_chat.process_message = MagicMock()
        mock_chat.get_state = MagicMock(return_value=None)
        adapter._chat = mock_chat

        payload = _create_comment_payload()
        body = json.dumps(payload)
        sig = _sign_payload(body)
        request = _build_webhook_request(body, signature=sig)
        response = await adapter.handle_webhook(request)

        assert response["status"] == 200
        mock_chat.process_message.assert_called_once()
        call_args = mock_chat.process_message.call_args[0]
        assert call_args[1] == "linear:issue-123:c:comment-abc"
        assert call_args[2].id == "comment-abc"
        assert call_args[2].text == "Hello from webhook"

    @pytest.mark.asyncio
    async def test_uses_parent_id_for_threaded_reply(self):
        logger = _make_logger()
        adapter = _make_webhook_adapter(logger=logger)
        mock_chat = MagicMock()
        mock_chat.process_message = MagicMock()
        mock_chat.get_state = MagicMock(return_value=None)
        adapter._chat = mock_chat

        payload = _create_comment_payload(comment_id="reply-1", parent_id="root-comment-id")
        body = json.dumps(payload)
        sig = _sign_payload(body)
        request = _build_webhook_request(body, signature=sig)
        response = await adapter.handle_webhook(request)

        assert response["status"] == 200
        call_args = mock_chat.process_message.call_args[0]
        assert call_args[1] == "linear:issue-123:c:root-comment-id"
        assert call_args[2].id == "reply-1"

    @pytest.mark.asyncio
    async def test_skips_non_create_actions(self):
        logger = _make_logger()
        adapter = _make_webhook_adapter(logger=logger)
        mock_chat = MagicMock()
        mock_chat.process_message = MagicMock()
        mock_chat.get_state = MagicMock(return_value=None)
        adapter._chat = mock_chat

        payload = _create_comment_payload(action="update")
        body = json.dumps(payload)
        sig = _sign_payload(body)
        request = _build_webhook_request(body, signature=sig)
        response = await adapter.handle_webhook(request)

        assert response["status"] == 200
        mock_chat.process_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_without_issue_id(self):
        logger = _make_logger()
        adapter = _make_webhook_adapter(logger=logger)
        mock_chat = MagicMock()
        mock_chat.process_message = MagicMock()
        mock_chat.get_state = MagicMock(return_value=None)
        adapter._chat = mock_chat

        payload = _create_comment_payload()
        payload["data"]["issueId"] = None
        body = json.dumps(payload)
        sig = _sign_payload(body)
        request = _build_webhook_request(body, signature=sig)
        response = await adapter.handle_webhook(request)

        assert response["status"] == 200
        mock_chat.process_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_own_messages(self):
        logger = _make_logger()
        adapter = _make_webhook_adapter(logger=logger)
        adapter._bot_user_id = "user-456"
        mock_chat = MagicMock()
        mock_chat.process_message = MagicMock()
        mock_chat.get_state = MagicMock(return_value=None)
        adapter._chat = mock_chat

        payload = _create_comment_payload(user_id="user-456")
        body = json.dumps(payload)
        sig = _sign_payload(body)
        request = _build_webhook_request(body, signature=sig)
        response = await adapter.handle_webhook(request)

        assert response["status"] == 200
        mock_chat.process_message.assert_not_called()
