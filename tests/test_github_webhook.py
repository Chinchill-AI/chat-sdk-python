"""Port of adapter-github/src/index.test.ts -- webhook handling, message processing,
postMessage, editMessage, deleteMessage, reactions, stream, parseMessage, fetchMessages,
fetchThread, listThreads, and factory tests.

Tests that duplicate the existing ``test_github_adapter.py`` are intentionally
omitted; this file covers the *remaining* TypeScript tests.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

from chat_sdk.adapters.github.adapter import (
    GitHubAdapter,
    create_github_adapter,
)
from chat_sdk.adapters.github.types import GitHubThreadId
from chat_sdk.logger import ConsoleLogger
from chat_sdk.shared.errors import ValidationError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def _sign(body: str, secret: str = WEBHOOK_SECRET) -> str:
    """Compute the GitHub webhook HMAC-SHA256 signature."""
    return "sha256=" + hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def _issue_comment_payload(**overrides: Any) -> dict[str, Any]:
    """Build a representative issue_comment webhook payload."""
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


def _review_comment_payload(**overrides: Any) -> dict[str, Any]:
    """Build a representative review comment webhook payload."""
    base: dict[str, Any] = {
        "action": "created",
        "comment": {
            "id": 200,
            "body": "Review comment text",
            "user": {"id": 2, "login": "reviewer", "type": "User"},
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-01T00:00:00Z",
            "html_url": "https://github.com/acme/app/pull/42#discussion_r200",
            "path": "src/index.ts",
            "diff_hunk": "@@ -1,3 +1,4 @@",
            "commit_id": "abc123",
            "original_commit_id": "abc123",
        },
        "pull_request": {
            "id": 500,
            "number": 42,
            "title": "Test PR",
            "state": "open",
            "body": "PR body",
            "html_url": "https://github.com/acme/app/pull/42",
            "user": {"id": 10, "login": "acme", "type": "Organization"},
        },
        "repository": {
            "id": 1,
            "name": "app",
            "full_name": "acme/app",
            "owner": {"id": 10, "login": "acme", "type": "Organization"},
        },
        "sender": {"id": 2, "login": "reviewer", "type": "User"},
    }
    base.update(overrides)
    return base


@dataclass
class _FakeRequest:
    """Minimal request-like object accepted by GitHubAdapter.handle_webhook."""

    url: str
    method: str
    _body: str
    headers: dict[str, str]

    async def text(self) -> str:  # noqa: D102
        return self._body


def _make_request(
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
# Constructor tests
# ---------------------------------------------------------------------------


class TestGitHubAdapterConstructor:
    """Constructor and property tests."""

    def test_create_with_pat(self):
        a = _make_adapter(user_name="bot")
        assert a.name == "github"
        assert a.user_name == "bot"
        assert a.is_multi_tenant is False

    def test_create_multi_tenant(self):
        a = GitHubAdapter(
            {
                "app_id": "12345",
                "private_key": "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----",
                "webhook_secret": "secret",
                "user_name": "my-bot[bot]",
                "logger": ConsoleLogger("error"),
            }
        )
        assert a.is_multi_tenant is True

    def test_create_single_tenant_app(self):
        a = GitHubAdapter(
            {
                "app_id": "12345",
                "private_key": "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----",
                "installation_id": 99,
                "webhook_secret": "secret",
                "user_name": "my-bot[bot]",
                "logger": ConsoleLogger("error"),
            }
        )
        assert a.is_multi_tenant is False

    def test_throws_when_no_auth(self):
        with pytest.raises(ValidationError, match="Authentication"):
            GitHubAdapter(
                {
                    "webhook_secret": "secret",
                    "user_name": "bot",
                    "logger": ConsoleLogger("error"),
                }
            )

    def test_bot_user_id_when_set(self):
        a = _make_adapter(bot_user_id=42)
        assert a.bot_user_id == "42"

    def test_bot_user_id_none_by_default(self):
        a = _make_adapter()
        assert a.bot_user_id is None


# ---------------------------------------------------------------------------
# handleWebhook -- signature & event routing
# ---------------------------------------------------------------------------


class TestGitHubWebhookSignature:
    """Webhook signature verification and basic routing."""

    @pytest.mark.asyncio
    async def test_missing_signature_returns_401(self):
        adapter = _make_adapter()
        body = json.dumps(_issue_comment_payload())
        request = _make_request(body, "issue_comment")
        response = await adapter.handle_webhook(request)
        assert response["status"] == 401

    @pytest.mark.asyncio
    async def test_invalid_signature_returns_401(self):
        adapter = _make_adapter()
        body = json.dumps(_issue_comment_payload())
        request = _make_request(body, "issue_comment", signature="sha256=invalid")
        response = await adapter.handle_webhook(request)
        assert response["status"] == 401

    @pytest.mark.asyncio
    async def test_ping_event_returns_pong(self):
        adapter = _make_adapter()
        body = json.dumps({"zen": "test"})
        sig = _sign(body)
        request = _make_request(body, "ping", signature=sig)
        response = await adapter.handle_webhook(request)
        assert response["status"] == 200
        assert response["body"] == "pong"

    @pytest.mark.asyncio
    async def test_invalid_json_returns_400(self):
        adapter = _make_adapter()
        body = "not-json{{{"
        sig = _sign(body)
        request = _make_request(body, "issue_comment", signature=sig)
        response = await adapter.handle_webhook(request)
        assert response["status"] == 400

    @pytest.mark.asyncio
    async def test_unrecognized_event_returns_ok(self):
        adapter = _make_adapter()
        body = json.dumps({"action": "completed"})
        sig = _sign(body)
        request = _make_request(body, "check_run", signature=sig)
        response = await adapter.handle_webhook(request)
        assert response["status"] == 200


# ---------------------------------------------------------------------------
# handleWebhook -- message processing
# ---------------------------------------------------------------------------


class TestGitHubWebhookMessageProcessing:
    """Issue comment / review comment processing via handleWebhook."""

    @pytest.mark.asyncio
    async def test_issue_comment_not_on_pr_ignored(self):
        """issue_comment on a plain issue (no pull_request key) is ignored."""
        adapter = _make_adapter()
        mock_chat = MagicMock()
        mock_chat.process_message = MagicMock()
        try:
            await adapter.initialize(mock_chat)

            payload = _issue_comment_payload(issue={"number": 10, "title": "Bug"})
            body = json.dumps(payload)
            sig = _sign(body)
            request = _make_request(body, "issue_comment", signature=sig)
            response = await adapter.handle_webhook(request)
            assert response["status"] == 200
        finally:
            await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_issue_comment_edited_action_ignored(self):
        adapter = _make_adapter()
        mock_chat = MagicMock()
        mock_chat.process_message = MagicMock()
        try:
            await adapter.initialize(mock_chat)

            payload = _issue_comment_payload(action="edited")
            body = json.dumps(payload)
            sig = _sign(body)
            request = _make_request(body, "issue_comment", signature=sig)
            response = await adapter.handle_webhook(request)
            assert response["status"] == 200
        finally:
            await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_review_comment_deleted_action_ignored(self):
        adapter = _make_adapter()
        mock_chat = MagicMock()
        mock_chat.process_message = MagicMock()
        try:
            await adapter.initialize(mock_chat)

            payload = _review_comment_payload(action="deleted")
            body = json.dumps(payload)
            sig = _sign(body)
            request = _make_request(body, "pull_request_review_comment", signature=sig)
            response = await adapter.handle_webhook(request)
            assert response["status"] == 200
        finally:
            await adapter.disconnect()


# ---------------------------------------------------------------------------
# parseMessage
# ---------------------------------------------------------------------------


class TestGitHubParseMessage:
    """Tests for parse_message."""

    def test_issue_comment(self):
        adapter = _make_adapter()
        raw = {
            "type": "issue_comment",
            "comment": {
                "id": 100,
                "body": "Test comment",
                "user": {"id": 1, "login": "testuser", "type": "User"},
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
                "html_url": "https://github.com/acme/app/pull/42#issuecomment-100",
            },
            "repository": {
                "id": 1,
                "name": "app",
                "full_name": "acme/app",
                "owner": {"id": 10, "login": "acme", "type": "User"},
            },
            "pr_number": 42,
        }
        msg = adapter.parse_message(raw)
        assert msg.id == "100"
        assert msg.thread_id == "github:acme/app:42"
        assert msg.text == "Test comment"
        assert msg.author.user_name == "testuser"
        assert msg.author.is_bot is False

    def test_review_comment_root(self):
        adapter = _make_adapter()
        raw = {
            "type": "review_comment",
            "comment": {
                "id": 200,
                "body": "Line comment",
                "user": {"id": 2, "login": "reviewer", "type": "User"},
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
                "html_url": "https://github.com/acme/app/pull/42#discussion_r200",
                "path": "src/index.ts",
                "diff_hunk": "@@",
                "commit_id": "abc",
                "original_commit_id": "abc",
            },
            "repository": {
                "id": 1,
                "name": "app",
                "full_name": "acme/app",
                "owner": {"id": 10, "login": "acme", "type": "User"},
            },
            "pr_number": 42,
        }
        msg = adapter.parse_message(raw)
        assert msg.id == "200"
        assert msg.thread_id == "github:acme/app:42:rc:200"

    def test_review_comment_reply(self):
        adapter = _make_adapter()
        raw = {
            "type": "review_comment",
            "comment": {
                "id": 300,
                "body": "Reply",
                "user": {"id": 2, "login": "reviewer", "type": "User"},
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
                "html_url": "https://github.com/acme/app/pull/42#discussion_r300",
                "path": "src/index.ts",
                "diff_hunk": "@@",
                "commit_id": "abc",
                "original_commit_id": "abc",
                "in_reply_to_id": 200,
            },
            "repository": {
                "id": 1,
                "name": "app",
                "full_name": "acme/app",
                "owner": {"id": 10, "login": "acme", "type": "User"},
            },
            "pr_number": 42,
        }
        msg = adapter.parse_message(raw)
        assert msg.id == "300"
        assert msg.thread_id == "github:acme/app:42:rc:200"

    def test_edited_message(self):
        adapter = _make_adapter()
        raw = {
            "type": "issue_comment",
            "comment": {
                "id": 100,
                "body": "Edited",
                "user": {"id": 1, "login": "testuser", "type": "User"},
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-02T00:00:00Z",
                "html_url": "https://github.com/acme/app/pull/42#issuecomment-100",
            },
            "repository": {
                "id": 1,
                "name": "app",
                "full_name": "acme/app",
                "owner": {"id": 10, "login": "acme", "type": "User"},
            },
            "pr_number": 42,
        }
        msg = adapter.parse_message(raw)
        assert msg.metadata.edited is True

    def test_unedited_message(self):
        adapter = _make_adapter()
        raw = {
            "type": "issue_comment",
            "comment": {
                "id": 100,
                "body": "Not edited",
                "user": {"id": 1, "login": "testuser", "type": "User"},
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
                "html_url": "https://github.com/acme/app/pull/42#issuecomment-100",
            },
            "repository": {
                "id": 1,
                "name": "app",
                "full_name": "acme/app",
                "owner": {"id": 10, "login": "acme", "type": "User"},
            },
            "pr_number": 42,
        }
        msg = adapter.parse_message(raw)
        assert msg.metadata.edited is False

    def test_bot_user_detection(self):
        adapter = _make_adapter()
        raw = {
            "type": "issue_comment",
            "comment": {
                "id": 100,
                "body": "Automated comment",
                "user": {"id": 50, "login": "dependabot[bot]", "type": "Bot"},
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
                "html_url": "https://github.com/acme/app/pull/42#issuecomment-100",
            },
            "repository": {
                "id": 1,
                "name": "app",
                "full_name": "acme/app",
                "owner": {"id": 10, "login": "acme", "type": "User"},
            },
            "pr_number": 42,
        }
        msg = adapter.parse_message(raw)
        assert msg.author.is_bot is True
        assert msg.author.user_name == "dependabot[bot]"
        assert msg.author.user_id == "50"

    def test_is_me_detection(self):
        a = _make_adapter(bot_user_id=50)
        raw = {
            "type": "issue_comment",
            "comment": {
                "id": 100,
                "body": "My comment",
                "user": {"id": 50, "login": "test-bot", "type": "Bot"},
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
                "html_url": "https://github.com/acme/app/pull/42#issuecomment-100",
            },
            "repository": {
                "id": 1,
                "name": "app",
                "full_name": "acme/app",
                "owner": {"id": 10, "login": "acme", "type": "User"},
            },
            "pr_number": 42,
        }
        msg = a.parse_message(raw)
        assert msg.author.is_me is True


# ---------------------------------------------------------------------------
# encodeThreadId / decodeThreadId (complementary)
# ---------------------------------------------------------------------------


class TestGitHubThreadIdExtended:
    """Extended thread ID tests from the TS suite."""

    def test_encode_with_hyphens(self):
        adapter = _make_adapter()
        result = adapter.encode_thread_id(GitHubThreadId(owner="my-org", repo="my-cool-app", pr_number=42))
        assert result == "github:my-org/my-cool-app:42"

    def test_decode_with_hyphens(self):
        adapter = _make_adapter()
        result = adapter.decode_thread_id("github:my-org/my-cool-app:42")
        assert result.owner == "my-org"
        assert result.repo == "my-cool-app"
        assert result.pr_number == 42

    def test_roundtrip_pr_level(self):
        adapter = _make_adapter()
        original = GitHubThreadId(owner="vercel", repo="next.js", pr_number=99999)
        decoded = adapter.decode_thread_id(adapter.encode_thread_id(original))
        assert decoded.owner == original.owner
        assert decoded.repo == original.repo
        assert decoded.pr_number == original.pr_number

    def test_roundtrip_review_comment(self):
        adapter = _make_adapter()
        original = GitHubThreadId(owner="vercel", repo="next.js", pr_number=99999, review_comment_id=123456789)
        decoded = adapter.decode_thread_id(adapter.encode_thread_id(original))
        assert decoded.owner == original.owner
        assert decoded.repo == original.repo
        assert decoded.pr_number == original.pr_number
        assert decoded.review_comment_id == original.review_comment_id


# ---------------------------------------------------------------------------
# channelIdFromThreadId
# ---------------------------------------------------------------------------


class TestChannelIdFromThreadIdExtended:
    """Channel ID derivation tests."""

    def test_from_pr_level(self):
        adapter = _make_adapter()
        assert adapter.channel_id_from_thread_id("github:acme/app:42") == "github:acme/app"

    def test_from_review_comment(self):
        adapter = _make_adapter()
        assert adapter.channel_id_from_thread_id("github:acme/app:42:rc:200") == "github:acme/app"


# ---------------------------------------------------------------------------
# startTyping
# ---------------------------------------------------------------------------


class TestGitHubStartTyping:
    """startTyping is a no-op."""

    @pytest.mark.asyncio
    async def test_noop(self):
        adapter = _make_adapter()
        result = await adapter.start_typing("github:acme/app:42")
        assert result is None


# ---------------------------------------------------------------------------
# renderFormatted
# ---------------------------------------------------------------------------


class TestGitHubRenderFormatted:
    """Tests for render_formatted."""

    def test_simple_markdown(self):
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
        assert result == "Hello world"

    def test_bold(self):
        adapter = _make_adapter()
        ast = {
            "type": "root",
            "children": [
                {
                    "type": "paragraph",
                    "children": [
                        {
                            "type": "strong",
                            "children": [{"type": "text", "value": "bold"}],
                        }
                    ],
                }
            ],
        }
        result = adapter.render_formatted(ast)
        assert result == "**bold**"


# ---------------------------------------------------------------------------
# emoji mapping (complementary -- parametrized)
# ---------------------------------------------------------------------------


class TestEmojiToGitHubReactionParametrized:
    """Parametrized emoji mapping tests from the TS suite."""

    @pytest.mark.parametrize(
        "input_emoji,expected",
        [
            ("thumbs_up", "+1"),
            ("+1", "+1"),
            ("thumbs_down", "-1"),
            ("-1", "-1"),
            ("laugh", "laugh"),
            ("smile", "laugh"),
            ("confused", "confused"),
            ("thinking", "confused"),
            ("heart", "heart"),
            ("love_eyes", "heart"),
            ("hooray", "hooray"),
            ("party", "hooray"),
            ("confetti", "hooray"),
            ("rocket", "rocket"),
            ("eyes", "eyes"),
        ],
    )
    def test_mapping(self, input_emoji: str, expected: str):
        adapter = _make_adapter()
        assert adapter._emoji_to_github_reaction(input_emoji) == expected

    def test_unknown_defaults_to_plus_one(self):
        adapter = _make_adapter()
        assert adapter._emoji_to_github_reaction("unknown_emoji") == "+1"


# ---------------------------------------------------------------------------
# createGitHubAdapter factory (complementary)
# ---------------------------------------------------------------------------


class TestCreateGitHubAdapterExtended:
    """Extended factory tests from the TS suite."""

    def test_explicit_pat_config(self):
        a = create_github_adapter(
            {
                "token": "ghp_test",
                "webhook_secret": "secret",
                "user_name": "bot",
            }
        )
        assert isinstance(a, GitHubAdapter)
        assert a.user_name == "bot"

    def test_default_user_name(self):
        a = create_github_adapter(
            {
                "token": "ghp_test",
                "webhook_secret": "secret",
            }
        )
        assert a.user_name == "github-bot"

    def test_bot_user_id_passed_through(self):
        a = create_github_adapter(
            {
                "token": "ghp_test",
                "webhook_secret": "secret",
                "bot_user_id": 42,
            }
        )
        assert a.bot_user_id == "42"

    def test_throws_when_webhook_secret_missing(self):
        old = os.environ.pop("GITHUB_WEBHOOK_SECRET", None)
        try:
            with pytest.raises(ValidationError, match="webhookSecret"):
                create_github_adapter({"token": "ghp_test"})
        finally:
            if old is not None:
                os.environ["GITHUB_WEBHOOK_SECRET"] = old

    def test_throws_when_no_auth(self):
        old_token = os.environ.pop("GITHUB_TOKEN", None)
        old_app = os.environ.pop("GITHUB_APP_ID", None)
        old_key = os.environ.pop("GITHUB_PRIVATE_KEY", None)
        try:
            with pytest.raises(ValidationError, match="Authentication"):
                create_github_adapter({"webhook_secret": "secret"})
        finally:
            if old_token is not None:
                os.environ["GITHUB_TOKEN"] = old_token
            if old_app is not None:
                os.environ["GITHUB_APP_ID"] = old_app
            if old_key is not None:
                os.environ["GITHUB_PRIVATE_KEY"] = old_key
