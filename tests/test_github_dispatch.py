"""Tests for GitHub adapter webhook dispatch (happy paths and self-detection).

Covers: issue_comment dispatches to process_message, review_comment dispatches
to process_message, self-message detection (sender.id == botUserId),
and in_reply_to_id routing to root thread.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from chat_sdk.adapters.github.adapter import GitHubAdapter
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


# =============================================================================
# Tests -- eager bot-user-ID auto-detection (github slice of upstream 9824d33)
# =============================================================================


def _make_multi_tenant_adapter(**overrides: Any) -> GitHubAdapter:
    """Create a multi-tenant GitHub App adapter (no installation_id)."""
    defaults: dict[str, Any] = {
        "webhook_secret": WEBHOOK_SECRET,
        "app_id": "12345",
        "private_key": "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----",
        "logger": ConsoleLogger("error"),
    }
    defaults.update(overrides)
    return GitHubAdapter(defaults)


def _make_install_payload(*, sender_id: int = 100, installation_id: int = 54321) -> dict[str, Any]:
    """An issue_comment payload carrying an installation id (multi-tenant)."""
    payload = _make_issue_comment_payload(sender_id=sender_id, owner="acme", repo="app", pr_number=42)
    payload["installation"] = {"id": installation_id, "node_id": "MDIzOk"}
    return payload


class TestEagerBotUserIdDetection:
    """Eager bot-user-ID detection so is_me works and self-reply loops are prevented.

    Mirrors the github slice of upstream commit 9824d33: detection runs on the
    first webhook for an installation (so the very first reply has a populated
    bot id), is cached so subsequent webhooks don't re-fetch, and falls back
    from users.getAuthenticated (GET /user) to apps.getAuthenticated (GET /app)
    + users.getByUsername (GET /users/{slug}[bot]) for installation tokens.
    """

    @pytest.mark.asyncio
    async def test_webhook_populates_bot_user_id_before_dispatch(self):
        """First webhook for a new installation populates _bot_user_id before message handling."""
        adapter = _make_multi_tenant_adapter()
        chat = MagicMock()
        chat.process_message = MagicMock()
        chat.get_state = MagicMock(return_value=MagicMock(set=AsyncMock(), get=AsyncMock(return_value=None)))
        adapter._chat = chat

        # Installation tokens: /user is unavailable, so detection falls back to
        # /app then /users/{slug}[bot].
        async def fake_api(method: str, path: str, body: Any = None, *, installation_id: int | None = None) -> Any:
            if path == "/user":
                raise RuntimeError("404 /user not available for installation token")
            if path == "/app":
                return {"slug": "my-bot", "id": 1}
            if path == "/users/my-bot[bot]":
                return {"id": 7777, "login": "my-bot[bot]"}
            raise AssertionError(f"unexpected path {path}")

        api = AsyncMock(side_effect=fake_api)
        adapter._github_api_request = api

        # process_message must observe a populated bot id. Capture it at call time.
        observed: dict[str, Any] = {}
        chat.process_message.side_effect = lambda *a, **kw: observed.update({"bot_id": adapter._bot_user_id})

        payload = _make_install_payload(sender_id=100, installation_id=54321)
        body = json.dumps(payload)
        headers = {
            "x-hub-signature-256": _sign(body),
            "x-github-event": "issue_comment",
            "content-type": "application/json",
        }

        result = await adapter.handle_webhook(FakeRequest(body, headers))

        assert result["status"] == 200
        assert adapter._bot_user_id == 7777
        # Detection used the webhook's installation id for the API calls.
        assert api.await_args_list[0].kwargs["installation_id"] == 54321
        # process_message ran, and the bot id was already set when it did.
        chat.process_message.assert_called_once()
        assert observed["bot_id"] == 7777

    @pytest.mark.asyncio
    async def test_message_from_bot_filtered_as_is_me(self):
        """A message authored by the bot itself is filtered (self-reply loop prevented)."""
        adapter = _make_multi_tenant_adapter()
        chat = MagicMock()
        chat.process_message = MagicMock()
        chat.get_state = MagicMock(return_value=MagicMock(set=AsyncMock(), get=AsyncMock(return_value=None)))
        adapter._chat = chat

        async def fake_api(method: str, path: str, body: Any = None, *, installation_id: int | None = None) -> Any:
            if path == "/user":
                raise RuntimeError("404")
            if path == "/app":
                return {"slug": "my-bot"}
            if path == "/users/my-bot[bot]":
                return {"id": 8888, "login": "my-bot[bot]"}
            raise AssertionError(f"unexpected path {path}")

        adapter._github_api_request = AsyncMock(side_effect=fake_api)

        # Sender is the bot itself (id 8888) -> must be ignored.
        payload = _make_install_payload(sender_id=8888, installation_id=54321)
        body = json.dumps(payload)
        headers = {
            "x-hub-signature-256": _sign(body),
            "x-github-event": "issue_comment",
            "content-type": "application/json",
        }

        result = await adapter.handle_webhook(FakeRequest(body, headers))

        assert result["status"] == 200
        assert adapter._bot_user_id == 8888
        # Self-message: process_message NOT called -> no self-reply loop.
        chat.process_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_pat_path_uses_users_get_authenticated(self):
        """PAT path resolves the bot via GET /user (users.getAuthenticated), no /app fallback."""
        adapter = _make_adapter()  # token-based (PAT) adapter
        chat = MagicMock()
        chat.process_message = MagicMock()
        chat.get_state = MagicMock(return_value=MagicMock(set=AsyncMock(), get=AsyncMock(return_value=None)))
        adapter._chat = chat

        api = AsyncMock(return_value={"id": 4242, "login": "pat-bot"})
        adapter._github_api_request = api

        payload = _make_issue_comment_payload(sender_id=100)
        body = json.dumps(payload)
        headers = {
            "x-hub-signature-256": _sign(body),
            "x-github-event": "issue_comment",
            "content-type": "application/json",
        }

        result = await adapter.handle_webhook(FakeRequest(body, headers))

        assert result["status"] == 200
        assert adapter._bot_user_id == 4242
        # Only GET /user was used; no apps.getAuthenticated fallback.
        called_paths = [c.args[1] for c in api.await_args_list]
        assert called_paths == ["/user"]
        chat.process_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_second_webhook_does_not_refetch(self):
        """A second webhook for the same installation does NOT re-fetch (cache hit)."""
        adapter = _make_multi_tenant_adapter()
        chat = MagicMock()
        chat.process_message = MagicMock()
        chat.get_state = MagicMock(return_value=MagicMock(set=AsyncMock(), get=AsyncMock(return_value=None)))
        adapter._chat = chat

        async def fake_api(method: str, path: str, body: Any = None, *, installation_id: int | None = None) -> Any:
            if path == "/user":
                raise RuntimeError("404")
            if path == "/app":
                return {"slug": "my-bot"}
            if path == "/users/my-bot[bot]":
                return {"id": 9999, "login": "my-bot[bot]"}
            raise AssertionError(f"unexpected path {path}")

        api = AsyncMock(side_effect=fake_api)
        adapter._github_api_request = api

        headers_for = lambda b: {  # noqa: E731
            "x-hub-signature-256": _sign(b),
            "x-github-event": "issue_comment",
            "content-type": "application/json",
        }

        body1 = json.dumps(_make_install_payload(sender_id=100, installation_id=54321))
        await adapter.handle_webhook(FakeRequest(body1, headers_for(body1)))
        assert adapter._bot_user_id == 9999

        detection_calls_after_first = len(api.await_args_list)

        body2 = json.dumps(_make_install_payload(sender_id=101, installation_id=54321))
        await adapter.handle_webhook(FakeRequest(body2, headers_for(body2)))

        # No additional detection API calls on the second webhook (cache hit).
        assert len(api.await_args_list) == detection_calls_after_first
        assert chat.process_message.call_count == 2


# =============================================================================
# Fake aiohttp session (exercises the real auth-selection logic in
# _github_api_request: PAT vs App-JWT vs installation-token)
# =============================================================================


class _FakeResponse:
    """Minimal aiohttp-style response usable as an async context manager."""

    def __init__(self, status: int, payload: Any) -> None:
        self.status = status
        self._payload = payload

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def json(self) -> Any:
        return self._payload

    async def text(self) -> str:
        return json.dumps(self._payload)


class _FakeSession:
    """Routes requests by path, recording the Authorization header used.

    Lets a test assert *which* credential (app JWT vs installation token) was
    sent to each GitHub endpoint, which is the load-bearing behaviour for the
    /app-needs-JWT fix.
    """

    def __init__(self, handler: Any) -> None:
        self._handler = handler
        self.closed = False
        # path -> Authorization header value used for that path
        self.auth_by_path: dict[str, str] = {}

    def request(self, method: str, url: str, **kwargs: Any) -> _FakeResponse:
        path = url.replace("https://api.github.com", "")
        headers = kwargs.get("headers", {})
        self.auth_by_path[path] = headers.get("Authorization", "")
        status, payload = self._handler(method, path)
        return _FakeResponse(status, payload)


class TestAppEndpointAuthSelection:
    """GET /app must authenticate with the App JWT, not an installation token.

    GitHub's apps.getAuthenticated (GET /app) endpoint rejects installation
    tokens (401/403). For the GitHub-App/installation case the /user path also
    fails on installation tokens, so the /app fallback is what must work --
    and it only works when authenticated with the App JWT directly.

    This test exercises the real auth-selection branch in _github_api_request
    (the HTTP layer is faked, not _github_api_request itself).
    """

    @pytest.mark.asyncio
    async def test_app_endpoint_uses_jwt_not_installation_token(self):
        adapter = _make_multi_tenant_adapter()
        adapter._installation_id = 54321  # single resolvable installation

        # Spy the credential minting so we can assert which path used which.
        adapter._generate_app_jwt = MagicMock(return_value="APP_JWT")  # type: ignore[method-assign]
        adapter._get_installation_token = AsyncMock(return_value="INSTALL_TOKEN")  # type: ignore[method-assign]

        def handler(method: str, path: str) -> tuple[int, Any]:
            if path == "/user":
                # Installation token cannot use /user -> 403.
                return 403, {"message": "Resource not accessible by integration"}
            if path == "/app":
                return 200, {"slug": "my-app", "id": 1}
            if path == "/users/my-app[bot]":
                return 200, {"id": 12345, "login": "my-app[bot]"}
            raise AssertionError(f"unexpected path {path}")

        session = _FakeSession(handler)
        adapter._get_http_session = AsyncMock(return_value=session)  # type: ignore[method-assign]

        await adapter._detect_bot_user_id(installation_id=54321)

        # Detection resolved the bot id via /app -> /users/{slug}[bot].
        assert adapter._bot_user_id == 12345

        # The /app call was authenticated with the App JWT (not an install token).
        assert session.auth_by_path["/app"] == "Bearer APP_JWT"
        assert adapter._generate_app_jwt.called
        # And /app was NOT sent the installation token.
        assert session.auth_by_path["/app"] != "Bearer INSTALL_TOKEN"

        # The /user attempt (which 403s) and the public /users/{slug}[bot] lookup
        # both go through the installation-token exchange, as expected.
        assert session.auth_by_path["/user"] == "Bearer INSTALL_TOKEN"
        assert session.auth_by_path["/users/my-app[bot]"] == "Bearer INSTALL_TOKEN"


class TestConcurrentDetectionFetchesOnce:
    """Concurrent detection must fetch the bot identity only once (lock works)."""

    @pytest.mark.asyncio
    async def test_concurrent_detect_fetches_once(self):
        adapter = _make_multi_tenant_adapter()

        call_counts: dict[str, int] = {"/user": 0}
        started = asyncio.Event()

        async def fake_api(method: str, path: str, body: Any = None, *, installation_id: int | None = None) -> Any:
            if path == "/user":
                call_counts["/user"] += 1
                started.set()
                # Slow PAT-style success so concurrent callers pile up behind
                # the first one if locking is absent.
                await asyncio.sleep(0.05)
                return {"id": 4242, "login": "the-bot"}
            raise AssertionError(f"unexpected path {path}")

        adapter._github_api_request = AsyncMock(side_effect=fake_api)

        # Fire N concurrent detections; only the first should hit the API.
        await asyncio.gather(*[adapter._detect_bot_user_id(installation_id=54321) for _ in range(8)])

        assert adapter._bot_user_id == 4242
        # The lock + double-checked cache means /user is fetched exactly once.
        assert call_counts["/user"] == 1
        assert adapter._github_api_request.await_count == 1
