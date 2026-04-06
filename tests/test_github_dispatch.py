"""Tests for GitHub adapter webhook dispatch (happy paths and self-detection).

Covers: issue_comment dispatches to process_message, review_comment dispatches
to process_message, self-message detection (sender.id == botUserId),
and in_reply_to_id routing to root thread.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from chat_sdk.adapters.github.adapter import GitHubAdapter
from chat_sdk.adapters.github.types import GitHubThreadId
from chat_sdk.logger import ConsoleLogger


# =============================================================================
# Helpers
# =============================================================================

WEBHOOK_SECRET = "test-webhook-secret"


def _make_adapter(**overrides: Any) -> GitHubAdapter:
    """Create a GitHubAdapter with minimal valid config."""
    defaults: dict[str, Any] = {
        "webhook_secret": WEBHOOK_SECRET,
        "token": "ghp_testtoken",
        "logger": ConsoleLogger("error"),
    }
    defaults.update(overrides)
    return GitHubAdapter(defaults)


def _sign(body: str) -> str:
    """Compute the x-hub-signature-256 header value."""
    digest = hmac.new(
        WEBHOOK_SECRET.encode("utf-8"),
        body.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"sha256={digest}"


def _make_issue_comment_payload(
    *,
    sender_id: int = 100,
    comment_id: int = 5000,
    comment_body: str = "Looks good to me!",
    pr_number: int = 42,
    owner: str = "octocat",
    repo: str = "hello-world",
) -> dict[str, Any]:
    """Build a minimal issue_comment webhook payload."""
    return {
        "action": "created",
        "comment": {
            "id": comment_id,
            "body": comment_body,
            "user": {"id": sender_id, "login": "alice", "type": "User"},
            "created_at": "2024-06-01T12:00:00Z",
            "updated_at": "2024-06-01T12:00:00Z",
        },
        "issue": {
            "number": pr_number,
            "title": "Add feature X",
            "pull_request": {"url": "https://api.github.com/repos/octocat/hello-world/pulls/42"},
        },
        "repository": {
            "name": repo,
            "full_name": f"{owner}/{repo}",
            "owner": {"id": 1, "login": owner, "type": "User"},
        },
        "sender": {"id": sender_id, "login": "alice", "type": "User"},
    }


def _make_review_comment_payload(
    *,
    sender_id: int = 100,
    comment_id: int = 7000,
    comment_body: str = "Nit: rename this variable",
    pr_number: int = 42,
    owner: str = "octocat",
    repo: str = "hello-world",
    in_reply_to_id: int | None = None,
) -> dict[str, Any]:
    """Build a minimal pull_request_review_comment webhook payload."""
    comment: dict[str, Any] = {
        "id": comment_id,
        "body": comment_body,
        "user": {"id": sender_id, "login": "bob", "type": "User"},
        "created_at": "2024-06-01T12:00:00Z",
        "updated_at": "2024-06-01T12:00:00Z",
        "path": "src/main.py",
        "diff_hunk": "@@ -1,3 +1,4 @@",
    }
    if in_reply_to_id is not None:
        comment["in_reply_to_id"] = in_reply_to_id
    return {
        "action": "created",
        "comment": comment,
        "pull_request": {
            "number": pr_number,
            "title": "Add feature X",
            "user": {"id": 200, "login": "carol", "type": "User"},
        },
        "repository": {
            "name": repo,
            "full_name": f"{owner}/{repo}",
            "owner": {"id": 1, "login": owner, "type": "User"},
        },
        "sender": {"id": sender_id, "login": "bob", "type": "User"},
    }


class FakeRequest:
    """Minimal request for webhook testing."""

    def __init__(self, body: str, headers: dict[str, str]) -> None:
        self.body = body.encode("utf-8")
        self.headers = headers


# =============================================================================
# Tests -- issue_comment dispatches to process_message
# =============================================================================


class TestIssueCommentDispatch:
    """Issue comment webhook dispatches to process_message with correct thread and message."""

    @pytest.mark.asyncio
    async def test_issue_comment_dispatches_to_process_message(self):
        adapter = _make_adapter()
        chat = MagicMock()
        chat.process_message = MagicMock()
        chat.get_state = MagicMock(return_value=MagicMock(set=AsyncMock()))
        adapter._chat = chat
        adapter._bot_user_id = 999  # Different from sender

        payload = _make_issue_comment_payload(sender_id=100, pr_number=42)
        body = json.dumps(payload)
        headers = {
            "x-hub-signature-256": _sign(body),
            "x-github-event": "issue_comment",
            "content-type": "application/json",
        }

        result = await adapter.handle_webhook(FakeRequest(body, headers))

        assert result["status"] == 200
        chat.process_message.assert_called_once()

        # Verify the thread_id
        call_args = chat.process_message.call_args[0]
        thread_id = call_args[1]
        message = call_args[2]

        assert "octocat/hello-world:42" in thread_id
        assert message.text is not None
        assert "Looks good" in message.text


# =============================================================================
# Tests -- review_comment dispatches to process_message
# =============================================================================


class TestReviewCommentDispatch:
    """Review comment webhook dispatches to process_message."""

    @pytest.mark.asyncio
    async def test_review_comment_dispatches_to_process_message(self):
        adapter = _make_adapter()
        chat = MagicMock()
        chat.process_message = MagicMock()
        chat.get_state = MagicMock(return_value=MagicMock(set=AsyncMock()))
        adapter._chat = chat
        adapter._bot_user_id = 999

        payload = _make_review_comment_payload(
            sender_id=100,
            comment_id=7000,
            comment_body="Nit: rename this variable",
        )
        body = json.dumps(payload)
        headers = {
            "x-hub-signature-256": _sign(body),
            "x-github-event": "pull_request_review_comment",
            "content-type": "application/json",
        }

        result = await adapter.handle_webhook(FakeRequest(body, headers))

        assert result["status"] == 200
        chat.process_message.assert_called_once()

        call_args = chat.process_message.call_args[0]
        message = call_args[2]
        assert "rename" in message.text


# =============================================================================
# Tests -- self-message ignored
# =============================================================================


class TestSelfMessageIgnored:
    """When sender.id equals bot_user_id, process_message is NOT called."""

    @pytest.mark.asyncio
    async def test_self_message_ignored(self):
        bot_user_id = 555
        adapter = _make_adapter(bot_user_id=bot_user_id)
        chat = MagicMock()
        chat.process_message = MagicMock()
        chat.get_state = MagicMock(return_value=MagicMock(set=AsyncMock()))
        adapter._chat = chat

        payload = _make_issue_comment_payload(sender_id=bot_user_id)
        body = json.dumps(payload)
        headers = {
            "x-hub-signature-256": _sign(body),
            "x-github-event": "issue_comment",
            "content-type": "application/json",
        }

        result = await adapter.handle_webhook(FakeRequest(body, headers))

        assert result["status"] == 200
        # process_message must NOT have been called
        chat.process_message.assert_not_called()


# =============================================================================
# Tests -- in_reply_to_id routes to root thread
# =============================================================================


class TestInReplyToIdRoutesToRootThread:
    """When a review comment has in_reply_to_id, it routes to the root comment's thread."""

    @pytest.mark.asyncio
    async def test_in_reply_to_id_routes_to_root_thread(self):
        adapter = _make_adapter()
        chat = MagicMock()
        chat.process_message = MagicMock()
        chat.get_state = MagicMock(return_value=MagicMock(set=AsyncMock()))
        adapter._chat = chat
        adapter._bot_user_id = 999

        # Comment 8000 is a reply to root comment 3000
        root_comment_id = 3000
        payload = _make_review_comment_payload(
            sender_id=100,
            comment_id=8000,
            in_reply_to_id=root_comment_id,
        )
        body = json.dumps(payload)
        headers = {
            "x-hub-signature-256": _sign(body),
            "x-github-event": "pull_request_review_comment",
            "content-type": "application/json",
        }

        result = await adapter.handle_webhook(FakeRequest(body, headers))

        assert result["status"] == 200
        chat.process_message.assert_called_once()

        call_args = chat.process_message.call_args[0]
        thread_id = call_args[1]

        # The thread_id should reference the ROOT comment (3000), not the reply (8000)
        decoded = adapter.decode_thread_id(thread_id)
        assert decoded.review_comment_id == root_comment_id
        # The thread_id format should contain :rc:3000
        assert f":rc:{root_comment_id}" in thread_id
