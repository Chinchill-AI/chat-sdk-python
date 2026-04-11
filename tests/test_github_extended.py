"""Extended GitHub adapter tests -- closes the test gap from 96 to 130.

Covers:
- Multi-tenant mode (installation ID extraction from webhooks, per-repo caching)
- getInstallationId / storeInstallationId
- postMessage with review comment thread (createReplyForReviewComment)
- editMessage (issue comment vs review comment)
- deleteMessage
- fetchMessages (issue comments, review comment thread filtering)
- listThreads (PR listing with pagination)
- fetchChannelInfo (repo metadata)
- Error handling (API errors, auth failures)
- parseMessage edge cases (edited timestamps, HTML body)
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from chat_sdk.adapters.github.adapter import (
    GitHubAdapter,
)
from chat_sdk.adapters.github.types import GitHubThreadId
from chat_sdk.logger import ConsoleLogger
from chat_sdk.shared.errors import ValidationError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WEBHOOK_SECRET = "test-webhook-secret"


def _make_adapter(**overrides: Any) -> GitHubAdapter:
    defaults: dict[str, Any] = {
        "webhook_secret": WEBHOOK_SECRET,
        "token": "ghp_testtoken",
        "logger": ConsoleLogger("error"),
    }
    defaults.update(overrides)
    return GitHubAdapter(defaults)


def _sign(body: str, secret: str = WEBHOOK_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


@dataclass
class _FakeRequest:
    url: str
    method: str
    _body: str
    headers: dict[str, str]

    async def text(self) -> str:
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


def _make_mock_state() -> MagicMock:
    cache: dict[str, Any] = {}
    state = MagicMock()
    state.get = AsyncMock(side_effect=lambda k: cache.get(k))
    state.set = AsyncMock(side_effect=lambda k, v, *a, **kw: cache.__setitem__(k, v))
    state.delete = AsyncMock(side_effect=lambda k: cache.pop(k, None))
    state._cache = cache
    return state


def _make_mock_chat(state: MagicMock) -> MagicMock:
    chat = MagicMock()
    chat.process_message = MagicMock()
    chat.get_state = MagicMock(return_value=state)
    return chat


# ---------------------------------------------------------------------------
# Multi-tenant mode
# ---------------------------------------------------------------------------


class TestMultiTenantMode:
    """Multi-tenant GitHub App adapter tests."""

    def test_multi_tenant_flag(self):
        adapter = GitHubAdapter(
            {
                "app_id": "12345",
                "private_key": "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----",
                "webhook_secret": "secret",
                "logger": ConsoleLogger("error"),
            }
        )
        assert adapter.is_multi_tenant is True

    def test_single_tenant_has_installation_id(self):
        adapter = GitHubAdapter(
            {
                "app_id": "12345",
                "private_key": "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----",
                "installation_id": 99,
                "webhook_secret": "secret",
                "logger": ConsoleLogger("error"),
            }
        )
        assert adapter.is_multi_tenant is False

    @pytest.mark.asyncio
    async def test_webhook_stores_installation_id(self):
        """Webhook with installation.id stores mapping in multi-tenant mode."""
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        adapter = GitHubAdapter(
            {
                "app_id": "12345",
                "private_key": "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----",
                "webhook_secret": WEBHOOK_SECRET,
                "logger": ConsoleLogger("error"),
            }
        )
        await adapter.initialize(chat)

        payload = {
            "action": "created",
            "comment": {
                "id": 100,
                "body": "test",
                "user": {"id": 1, "login": "user", "type": "User"},
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
                "html_url": "https://github.com/acme/app/pull/42#issuecomment-100",
            },
            "issue": {
                "number": 42,
                "title": "PR",
                "pull_request": {"url": "https://api.github.com/repos/acme/app/pulls/42"},
            },
            "repository": {
                "id": 1,
                "name": "app",
                "full_name": "acme/app",
                "owner": {"id": 10, "login": "acme", "type": "Organization"},
            },
            "installation": {"id": 54321, "node_id": "MDIzOk"},
            "sender": {"id": 1, "login": "user", "type": "User"},
        }
        body = json.dumps(payload)
        sig = _sign(body)
        request = _make_request(body, "issue_comment", signature=sig)
        response = await adapter.handle_webhook(request)
        assert response["status"] == 200
        # Verify installation ID was stored
        assert state._cache.get("github:install:acme/app") == 54321

    @pytest.mark.asyncio
    async def test_get_installation_id_returns_cached(self):
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        adapter = GitHubAdapter(
            {
                "app_id": "12345",
                "private_key": "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----",
                "webhook_secret": WEBHOOK_SECRET,
                "logger": ConsoleLogger("error"),
            }
        )
        await adapter.initialize(chat)
        state._cache["github:install:acme/app"] = 99999

        result = await adapter._get_installation_id("acme", "app")
        assert result == 99999

    @pytest.mark.asyncio
    async def test_get_installation_id_returns_none_when_missing(self):
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        adapter = GitHubAdapter(
            {
                "app_id": "12345",
                "private_key": "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----",
                "webhook_secret": WEBHOOK_SECRET,
                "logger": ConsoleLogger("error"),
            }
        )
        await adapter.initialize(chat)

        result = await adapter._get_installation_id("no", "repo")
        assert result is None


# ---------------------------------------------------------------------------
# postMessage (mocked API)
# ---------------------------------------------------------------------------


class TestPostMessage:
    @pytest.mark.asyncio
    async def test_post_to_pr_level_thread(self):
        adapter = _make_adapter()
        adapter._github_api_request = AsyncMock(return_value={"id": 500, "body": "Hi"})

        thread_id = "github:acme/app:42"
        result = await adapter.post_message(thread_id, {"markdown": "Hello"})
        assert result.id == "500"
        assert result.thread_id == thread_id
        call = adapter._github_api_request.call_args
        assert call[0][0] == "POST"
        assert "/issues/42/comments" in call[0][1]

    @pytest.mark.asyncio
    async def test_post_to_review_comment_thread(self):
        adapter = _make_adapter()
        adapter._github_api_request = AsyncMock(return_value={"id": 600, "body": "Reply"})

        thread_id = "github:acme/app:42:rc:200"
        result = await adapter.post_message(thread_id, {"markdown": "Replying"})
        assert result.id == "600"
        call = adapter._github_api_request.call_args
        assert call[0][0] == "POST"
        assert "/comments/200/replies" in call[0][1]

    @pytest.mark.asyncio
    async def test_post_message_returns_correct_raw_type(self):
        adapter = _make_adapter()
        adapter._github_api_request = AsyncMock(return_value={"id": 700, "body": "test"})

        result = await adapter.post_message("github:acme/app:10", {"markdown": "Test"})
        assert result.raw["type"] == "issue_comment"

        result = await adapter.post_message("github:acme/app:10:rc:99", {"markdown": "Test"})
        assert result.raw["type"] == "review_comment"


# ---------------------------------------------------------------------------
# editMessage (mocked API)
# ---------------------------------------------------------------------------


class TestEditMessage:
    @pytest.mark.asyncio
    async def test_edit_issue_comment(self):
        adapter = _make_adapter()
        adapter._github_api_request = AsyncMock(return_value={"id": 100, "body": "Updated"})

        result = await adapter.edit_message("github:acme/app:42", "100", {"markdown": "Updated"})
        assert result.id == "100"
        call = adapter._github_api_request.call_args
        assert call[0][0] == "PATCH"
        assert "/issues/comments/100" in call[0][1]

    @pytest.mark.asyncio
    async def test_edit_review_comment(self):
        adapter = _make_adapter()
        adapter._github_api_request = AsyncMock(return_value={"id": 200, "body": "Updated review"})

        result = await adapter.edit_message("github:acme/app:42:rc:150", "200", {"markdown": "Updated"})
        assert result.id == "200"
        call = adapter._github_api_request.call_args
        assert call[0][0] == "PATCH"
        assert "/pulls/comments/200" in call[0][1]

    @pytest.mark.asyncio
    async def test_edit_returns_correct_thread_id(self):
        adapter = _make_adapter()
        adapter._github_api_request = AsyncMock(return_value={"id": 300})

        tid = "github:owner/repo:5"
        result = await adapter.edit_message(tid, "300", {"markdown": "x"})
        assert result.thread_id == tid


# ---------------------------------------------------------------------------
# deleteMessage (mocked API)
# ---------------------------------------------------------------------------


class TestDeleteMessage:
    @pytest.mark.asyncio
    async def test_delete_issue_comment(self):
        adapter = _make_adapter()
        adapter._github_api_request = AsyncMock(return_value=None)

        await adapter.delete_message("github:acme/app:42", "100")
        call = adapter._github_api_request.call_args
        assert call[0][0] == "DELETE"
        assert "/issues/comments/100" in call[0][1]

    @pytest.mark.asyncio
    async def test_delete_review_comment(self):
        adapter = _make_adapter()
        adapter._github_api_request = AsyncMock(return_value=None)

        await adapter.delete_message("github:acme/app:42:rc:150", "200")
        call = adapter._github_api_request.call_args
        assert call[0][0] == "DELETE"
        assert "/pulls/comments/200" in call[0][1]


# ---------------------------------------------------------------------------
# fetchMessages (mocked API)
# ---------------------------------------------------------------------------


class TestFetchMessages:
    @pytest.mark.asyncio
    async def test_fetch_issue_comments(self):
        adapter = _make_adapter()
        adapter._github_api_request = AsyncMock(
            return_value=[
                {
                    "id": 1,
                    "body": "First",
                    "user": {"id": 1, "login": "alice", "type": "User"},
                    "created_at": "2024-01-01T00:00:00Z",
                    "updated_at": "2024-01-01T00:00:00Z",
                },
                {
                    "id": 2,
                    "body": "Second",
                    "user": {"id": 2, "login": "bob", "type": "User"},
                    "created_at": "2024-01-01T01:00:00Z",
                    "updated_at": "2024-01-01T01:00:00Z",
                },
            ]
        )

        result = await adapter.fetch_messages("github:acme/app:42")
        assert len(result.messages) == 2
        assert result.messages[0].text == "First"
        assert result.messages[1].text == "Second"

    @pytest.mark.asyncio
    async def test_fetch_review_comment_thread_filters(self):
        adapter = _make_adapter()
        adapter._github_api_request = AsyncMock(
            return_value=[
                {
                    "id": 200,
                    "body": "Root comment",
                    "user": {"id": 1, "login": "alice", "type": "User"},
                    "created_at": "2024-01-01T00:00:00Z",
                    "updated_at": "2024-01-01T00:00:00Z",
                    "path": "src/index.ts",
                    "diff_hunk": "@@",
                    "commit_id": "abc",
                    "original_commit_id": "abc",
                },
                {
                    "id": 201,
                    "body": "Reply",
                    "user": {"id": 2, "login": "bob", "type": "User"},
                    "created_at": "2024-01-01T01:00:00Z",
                    "updated_at": "2024-01-01T01:00:00Z",
                    "in_reply_to_id": 200,
                    "path": "src/index.ts",
                    "diff_hunk": "@@",
                    "commit_id": "abc",
                    "original_commit_id": "abc",
                },
                {
                    "id": 300,
                    "body": "Unrelated thread",
                    "user": {"id": 3, "login": "carol", "type": "User"},
                    "created_at": "2024-01-01T02:00:00Z",
                    "updated_at": "2024-01-01T02:00:00Z",
                    "path": "src/other.ts",
                    "diff_hunk": "@@",
                    "commit_id": "def",
                    "original_commit_id": "def",
                },
            ]
        )

        result = await adapter.fetch_messages("github:acme/app:42:rc:200")
        # Should only include root (200) and reply (201), not unrelated (300)
        assert len(result.messages) == 2
        assert result.messages[0].id == "200"
        assert result.messages[1].id == "201"

    @pytest.mark.asyncio
    async def test_fetch_messages_sorted_by_date(self):
        adapter = _make_adapter()
        adapter._github_api_request = AsyncMock(
            return_value=[
                {
                    "id": 2,
                    "body": "Second",
                    "user": {"id": 1, "login": "u", "type": "User"},
                    "created_at": "2024-01-02T00:00:00Z",
                    "updated_at": "2024-01-02T00:00:00Z",
                },
                {
                    "id": 1,
                    "body": "First",
                    "user": {"id": 1, "login": "u", "type": "User"},
                    "created_at": "2024-01-01T00:00:00Z",
                    "updated_at": "2024-01-01T00:00:00Z",
                },
            ]
        )

        result = await adapter.fetch_messages("github:owner/repo:1")
        assert result.messages[0].id == "1"
        assert result.messages[1].id == "2"


# ---------------------------------------------------------------------------
# listThreads (mocked API)
# ---------------------------------------------------------------------------


class TestListThreads:
    @pytest.mark.asyncio
    async def test_list_threads_returns_prs(self):
        adapter = _make_adapter()
        adapter._github_api_request = AsyncMock(
            return_value=[
                {
                    "number": 10,
                    "title": "Feature A",
                    "state": "open",
                    "user": {"id": 1, "login": "alice", "type": "User"},
                    "created_at": "2024-01-01T00:00:00Z",
                    "updated_at": "2024-01-02T00:00:00Z",
                },
            ]
        )

        result = await adapter.list_threads("github:acme/app")
        assert len(result.threads) == 1
        assert result.threads[0].root_message.text == "Feature A"

    @pytest.mark.asyncio
    async def test_list_threads_pagination(self):
        adapter = _make_adapter()
        # Return exactly 30 PRs (default limit) to trigger next cursor
        prs = [
            {
                "number": i,
                "title": f"PR {i}",
                "state": "open",
                "user": {"id": 1, "login": "alice", "type": "User"},
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
            }
            for i in range(30)
        ]
        adapter._github_api_request = AsyncMock(return_value=prs)

        result = await adapter.list_threads("github:acme/app")
        assert result.next_cursor == "2"

    @pytest.mark.asyncio
    async def test_list_threads_no_next_when_fewer_results(self):
        adapter = _make_adapter()
        adapter._github_api_request = AsyncMock(
            return_value=[
                {
                    "number": 1,
                    "title": "Only PR",
                    "state": "open",
                    "user": {"id": 1, "login": "a", "type": "User"},
                    "created_at": "2024-01-01T00:00:00Z",
                    "updated_at": "2024-01-01T00:00:00Z",
                },
            ]
        )

        result = await adapter.list_threads("github:acme/app")
        assert result.next_cursor is None

    @pytest.mark.asyncio
    async def test_list_threads_invalid_channel_id(self):
        adapter = _make_adapter()
        with pytest.raises(ValidationError, match="Invalid GitHub channel ID"):
            await adapter.list_threads("slack:C123")


# ---------------------------------------------------------------------------
# fetchChannelInfo (mocked API)
# ---------------------------------------------------------------------------


class TestFetchChannelInfo:
    @pytest.mark.asyncio
    async def test_returns_repo_metadata(self):
        adapter = _make_adapter()
        adapter._github_api_request = AsyncMock(
            return_value={
                "full_name": "acme/app",
                "description": "A great app",
                "visibility": "public",
                "default_branch": "main",
                "open_issues_count": 42,
            }
        )

        info = await adapter.fetch_channel_info("github:acme/app")
        assert info.name == "acme/app"
        assert info.is_dm is False
        assert info.metadata["description"] == "A great app"
        assert info.metadata["visibility"] == "public"
        assert info.metadata["default_branch"] == "main"

    @pytest.mark.asyncio
    async def test_invalid_channel_id(self):
        adapter = _make_adapter()
        with pytest.raises(ValidationError, match="Invalid GitHub channel ID"):
            await adapter.fetch_channel_info("slack:C123")


# ---------------------------------------------------------------------------
# fetchThread (mocked API)
# ---------------------------------------------------------------------------


class TestFetchThread:
    @pytest.mark.asyncio
    async def test_returns_thread_info(self):
        adapter = _make_adapter()
        adapter._github_api_request = AsyncMock(
            return_value={
                "title": "Fix bug",
                "state": "open",
            }
        )

        thread_id = "github:acme/app:42"
        info = await adapter.fetch_thread(thread_id)
        assert info.id == thread_id
        assert info.channel_id == "github:acme/app"
        assert info.metadata["pr_number"] == 42
        assert info.metadata["pr_title"] == "Fix bug"

    @pytest.mark.asyncio
    async def test_review_comment_thread_info(self):
        adapter = _make_adapter()
        adapter._github_api_request = AsyncMock(return_value={"title": "PR", "state": "open"})

        thread_id = "github:acme/app:42:rc:200"
        info = await adapter.fetch_thread(thread_id)
        assert info.metadata["review_comment_id"] == 200

    @pytest.mark.asyncio
    async def test_issue_thread_fetches_issue_metadata(self):
        """fetch_thread for an issue thread uses the issues API, not pulls."""
        adapter = _make_adapter()
        adapter._github_api_request = AsyncMock(
            return_value={
                "title": "Bug report",
                "state": "open",
                "number": 10,
            }
        )

        info = await adapter.fetch_thread("github:acme/app:issue:10")

        # Verify the issues API was called (not pulls)
        call = adapter._github_api_request.call_args
        assert "/issues/10" in call[0][1]
        assert "/pulls/" not in call[0][1]

        assert info.id == "github:acme/app:issue:10"
        assert info.channel_id == "github:acme/app"
        assert info.channel_name == "app #10"
        assert info.is_dm is False
        assert info.metadata == {
            "owner": "acme",
            "repo": "app",
            "issue_number": 10,
            "issue_title": "Bug report",
            "issue_state": "open",
            "type": "issue",
        }


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_api_error_raises_runtime_error(self):
        adapter = _make_adapter()

        async def fail(*args, **kwargs):
            raise RuntimeError("GitHub API error: 500 Internal Server Error")

        adapter._github_api_request = fail

        with pytest.raises(RuntimeError, match="500"):
            await adapter.post_message("github:acme/app:42", {"markdown": "test"})

    def test_invalid_thread_id_in_post(self):
        adapter = _make_adapter()
        with pytest.raises(ValidationError):
            adapter.decode_thread_id("github:bad-format")

    def test_decode_thread_id_wrong_platform(self):
        adapter = _make_adapter()
        with pytest.raises(ValidationError, match="Invalid GitHub thread ID"):
            adapter.decode_thread_id("teams:abc:def")


# ---------------------------------------------------------------------------
# parseMessage edge cases
# ---------------------------------------------------------------------------


class TestParseMessageEdgeCases:
    def test_empty_body(self):
        adapter = _make_adapter()
        raw = {
            "type": "issue_comment",
            "comment": {
                "id": 1,
                "body": "",
                "user": {"id": 1, "login": "u", "type": "User"},
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
            },
            "repository": {
                "id": 1,
                "name": "repo",
                "full_name": "org/repo",
                "owner": {"id": 10, "login": "org", "type": "Organization"},
            },
            "pr_number": 1,
        }
        msg = adapter.parse_message(raw)
        assert msg.text == ""

    def test_markdown_body_with_code_block(self):
        adapter = _make_adapter()
        raw = {
            "type": "issue_comment",
            "comment": {
                "id": 1,
                "body": "```python\nprint('hello')\n```",
                "user": {"id": 1, "login": "u", "type": "User"},
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
            },
            "repository": {
                "id": 1,
                "name": "repo",
                "full_name": "org/repo",
                "owner": {"id": 10, "login": "org", "type": "Organization"},
            },
            "pr_number": 1,
        }
        msg = adapter.parse_message(raw)
        assert "print" in msg.text

    def test_review_comment_with_diff_hunk_context(self):
        adapter = _make_adapter()
        raw = {
            "type": "review_comment",
            "comment": {
                "id": 500,
                "body": "Consider renaming this",
                "user": {"id": 2, "login": "reviewer", "type": "User"},
                "created_at": "2024-06-15T10:30:00Z",
                "updated_at": "2024-06-15T10:30:00Z",
                "path": "src/utils.py",
                "diff_hunk": "@@ -10,7 +10,8 @@\n+def new_function():",
                "commit_id": "abc123",
                "original_commit_id": "abc123",
                "line": 15,
                "side": "RIGHT",
            },
            "repository": {
                "id": 1,
                "name": "repo",
                "full_name": "org/repo",
                "owner": {"id": 10, "login": "org", "type": "Organization"},
            },
            "pr_number": 7,
        }
        msg = adapter.parse_message(raw)
        assert msg.id == "500"
        assert msg.text == "Consider renaming this"
        assert msg.raw["type"] == "review_comment"

    def test_parse_preserves_repository_info(self):
        adapter = _make_adapter()
        raw = {
            "type": "issue_comment",
            "comment": {
                "id": 1,
                "body": "test",
                "user": {"id": 1, "login": "u", "type": "User"},
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
            },
            "repository": {
                "id": 99,
                "name": "my-repo",
                "full_name": "my-org/my-repo",
                "owner": {"id": 10, "login": "my-org", "type": "Organization"},
            },
            "pr_number": 5,
        }
        msg = adapter.parse_message(raw)
        assert msg.raw["repository"]["name"] == "my-repo"

    def test_parse_message_with_missing_user_fields(self):
        adapter = _make_adapter()
        raw = {
            "type": "issue_comment",
            "comment": {
                "id": 1,
                "body": "test",
                "user": {"id": 0},
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
            },
            "repository": {
                "id": 1,
                "name": "r",
                "full_name": "o/r",
                "owner": {"id": 1, "login": "o", "type": "User"},
            },
            "pr_number": 1,
        }
        msg = adapter.parse_message(raw)
        assert msg.author.user_id == "0"
        assert msg.author.user_name == ""


# ---------------------------------------------------------------------------
# stream (accumulates and posts)
# ---------------------------------------------------------------------------


class TestInitialize:
    @pytest.mark.asyncio
    async def test_initialize_stores_chat_instance(self):
        adapter = _make_adapter()
        mock_chat = MagicMock()
        try:
            await adapter.initialize(mock_chat)
            assert adapter._chat is mock_chat
        finally:
            await adapter.disconnect()


class TestStream:
    @pytest.mark.asyncio
    async def test_stream_accumulates_text(self):
        adapter = _make_adapter()
        adapter._github_api_request = AsyncMock(return_value={"id": 999, "body": "full text"})

        async def text_gen():
            yield "Hello "
            yield "world"

        result = await adapter.stream("github:acme/app:42", text_gen())
        assert result.id == "999"
        # The post should have been called with the accumulated text
        call_args = adapter._github_api_request.call_args
        assert call_args[0][2]["body"] == "Hello world"


class TestIssueThreadChannelRoundTrip:
    """fetch_thread().channel_id should work with channel APIs."""

    async def test_issue_thread_channel_id_round_trips(self):
        """channel_id from fetch_thread feeds into channel_id_from_thread_id."""
        adapter = _make_adapter()
        adapter._github_api_request = AsyncMock(return_value={"title": "Bug report", "state": "open", "number": 10})

        thread_id = adapter.encode_thread_id(GitHubThreadId(owner="acme", repo="app", pr_number=10, type="issue"))
        info = await adapter.fetch_thread(thread_id)

        # channel_id should have github: prefix
        assert info.channel_id.startswith("github:")

        # channel_id should work with channel_id_from_thread_id
        derived_channel = adapter.channel_id_from_thread_id(thread_id)
        assert info.channel_id == derived_channel
