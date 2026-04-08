"""Extended tests for the Linear adapter -- message ops, reactions, typing,
fetch, thread info, token refresh, auth modes, webhook handling.

Ported from the remaining test categories in
packages/adapter-linear/src/index.test.ts (lines ~960-2123).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chat_sdk.adapters.linear.adapter import (
    EMOJI_MAPPING,
    LinearAdapter,
    create_linear_adapter,
)
from chat_sdk.adapters.linear.types import (
    LinearAdapterAPIKeyConfig,
    LinearAdapterAppConfig,
    LinearAdapterBaseConfig,
    LinearAdapterOAuthConfig,
    LinearThreadId,
)
from chat_sdk.shared.errors import AuthenticationError, ValidationError

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
    config = LinearAdapterAPIKeyConfig(
        api_key=overrides.pop("api_key", "test-api-key"),
        webhook_secret=overrides.pop("webhook_secret", "test-secret"),
        user_name=overrides.pop("user_name", "test-bot"),
        logger=overrides.pop("logger", _make_logger()),
    )
    return LinearAdapter(config)


def _make_webhook_adapter(logger=None) -> LinearAdapter:
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


# ============================================================================
# postMessage
# ============================================================================


class TestPostMessage:
    @pytest.mark.asyncio
    async def test_creates_comment_via_graphql(self):
        adapter = _make_webhook_adapter()
        adapter._graphql_query = AsyncMock(
            return_value={
                "data": {
                    "commentCreate": {
                        "success": True,
                        "comment": {
                            "id": "new-comment-1",
                            "body": "Bot reply",
                            "url": "https://linear.app/test/comment/new-comment-1",
                            "createdAt": "2025-06-01T12:00:00.000Z",
                            "updatedAt": "2025-06-01T12:00:00.000Z",
                        },
                    }
                }
            }
        )

        result = await adapter.post_message("linear:issue-123:c:parent-comment", "Hello from bot")

        assert result.id == "new-comment-1"
        assert result.thread_id == "linear:issue-123:c:parent-comment"
        assert result.raw["comment"]["body"] == "Bot reply"

        call_args = adapter._graphql_query.call_args
        variables = call_args[0][1]
        assert variables["input"]["issueId"] == "issue-123"
        assert variables["input"]["parentId"] == "parent-comment"

    @pytest.mark.asyncio
    async def test_creates_top_level_comment(self):
        adapter = _make_webhook_adapter()
        adapter._graphql_query = AsyncMock(
            return_value={
                "data": {
                    "commentCreate": {
                        "success": True,
                        "comment": {
                            "id": "top-comment-1",
                            "body": "Top-level comment",
                            "url": "https://linear.app/test/comment/top-comment-1",
                            "createdAt": "2025-06-01T12:00:00.000Z",
                            "updatedAt": "2025-06-01T12:00:00.000Z",
                        },
                    }
                }
            }
        )

        await adapter.post_message("linear:issue-123", "Hello")

        call_args = adapter._graphql_query.call_args
        variables = call_args[0][1]
        assert variables["input"]["issueId"] == "issue-123"
        assert "parentId" not in variables["input"]

    @pytest.mark.asyncio
    async def test_throws_when_creation_returns_null(self):
        adapter = _make_webhook_adapter()
        adapter._graphql_query = AsyncMock(return_value={"data": {"commentCreate": {"comment": None}}})

        with pytest.raises(Exception, match="Failed to create comment"):
            await adapter.post_message("linear:issue-123", "Hello")

    @pytest.mark.asyncio
    async def test_handles_markdown_message_format(self):
        adapter = _make_webhook_adapter()
        adapter._graphql_query = AsyncMock(
            return_value={
                "data": {
                    "commentCreate": {
                        "success": True,
                        "comment": {
                            "id": "ast-comment-1",
                            "body": "**bold text**",
                            "url": "https://linear.app/test/comment/ast-comment-1",
                            "createdAt": "2025-06-01T12:00:00.000Z",
                            "updatedAt": "2025-06-01T12:00:00.000Z",
                        },
                    }
                }
            }
        )

        await adapter.post_message("linear:issue-123", {"markdown": "**bold text**"})

        call_args = adapter._graphql_query.call_args
        variables = call_args[0][1]
        assert "bold text" in variables["input"]["body"]

    @pytest.mark.asyncio
    async def test_calls_ensure_valid_token(self):
        adapter = _make_webhook_adapter()
        adapter._ensure_valid_token = AsyncMock()
        adapter._graphql_query = AsyncMock(
            return_value={
                "data": {
                    "commentCreate": {
                        "success": True,
                        "comment": {
                            "id": "c1",
                            "body": "test",
                            "url": "https://linear.app/test",
                            "createdAt": "2025-06-01T12:00:00.000Z",
                            "updatedAt": "2025-06-01T12:00:00.000Z",
                        },
                    }
                }
            }
        )

        await adapter.post_message("linear:issue-123", "test")

        assert adapter._ensure_valid_token.call_count == 1


# ============================================================================
# editMessage
# ============================================================================


class TestEditMessage:
    @pytest.mark.asyncio
    async def test_updates_comment_via_graphql(self):
        adapter = _make_webhook_adapter()
        adapter._graphql_query = AsyncMock(
            return_value={
                "data": {
                    "commentUpdate": {
                        "success": True,
                        "comment": {
                            "id": "edited-comment-1",
                            "body": "Updated body",
                            "url": "https://linear.app/test/comment/edited-comment-1",
                            "createdAt": "2025-06-01T12:00:00.000Z",
                            "updatedAt": "2025-06-01T13:00:00.000Z",
                        },
                    }
                }
            }
        )

        result = await adapter.edit_message(
            "linear:issue-123:c:parent-comment",
            "edited-comment-1",
            "Updated body",
        )

        assert result.id == "edited-comment-1"
        assert result.raw["comment"]["body"] == "Updated body"
        assert result.raw["comment"]["issue_id"] == "issue-123"

        call_args = adapter._graphql_query.call_args
        variables = call_args[0][1]
        assert variables["id"] == "edited-comment-1"
        assert variables["input"]["body"] == "Updated body"

    @pytest.mark.asyncio
    async def test_throws_when_update_returns_null(self):
        adapter = _make_webhook_adapter()
        adapter._graphql_query = AsyncMock(return_value={"data": {"commentUpdate": {"comment": None}}})

        with pytest.raises(Exception, match="Failed to update comment"):
            await adapter.edit_message("linear:issue-123", "comment-1", "Updated")


# ============================================================================
# deleteMessage
# ============================================================================


class TestDeleteMessage:
    @pytest.mark.asyncio
    async def test_calls_delete_via_graphql(self):
        adapter = _make_webhook_adapter()
        adapter._graphql_query = AsyncMock(return_value={"data": {"commentDelete": {"success": True}}})

        await adapter.delete_message("linear:issue-123", "comment-to-delete")

        call_args = adapter._graphql_query.call_args
        variables = call_args[0][1]
        assert variables["id"] == "comment-to-delete"


# ============================================================================
# addReaction
# ============================================================================


class TestAddReaction:
    @pytest.mark.asyncio
    async def test_creates_reaction_with_emoji_string(self):
        adapter = _make_webhook_adapter()
        adapter._graphql_query = AsyncMock(return_value={"data": {"reactionCreate": {"success": True}}})

        await adapter.add_reaction("linear:issue-123", "comment-1", "rocket")

        call_args = adapter._graphql_query.call_args
        variables = call_args[0][1]
        assert variables["input"]["commentId"] == "comment-1"
        assert variables["input"]["emoji"] == "\U0001f680"

    @pytest.mark.asyncio
    async def test_creates_reaction_with_emoji_value(self):
        from chat_sdk.types import EmojiValue

        adapter = _make_webhook_adapter()
        adapter._graphql_query = AsyncMock(return_value={"data": {"reactionCreate": {"success": True}}})

        await adapter.add_reaction("linear:issue-123", "comment-1", EmojiValue(name="heart"))

        call_args = adapter._graphql_query.call_args
        variables = call_args[0][1]
        assert variables["input"]["emoji"] == "\u2764\ufe0f"

    @pytest.mark.asyncio
    async def test_passes_through_unknown_emoji_names(self):
        adapter = _make_webhook_adapter()
        adapter._graphql_query = AsyncMock(return_value={"data": {"reactionCreate": {"success": True}}})

        await adapter.add_reaction("linear:issue-123", "comment-1", "custom_emoji")

        call_args = adapter._graphql_query.call_args
        variables = call_args[0][1]
        assert variables["input"]["emoji"] == "custom_emoji"

    @pytest.mark.asyncio
    async def test_resolves_all_known_emoji_mappings(self):
        adapter = _make_webhook_adapter()
        adapter._graphql_query = AsyncMock(return_value={"data": {"reactionCreate": {"success": True}}})

        for name, unicode_val in EMOJI_MAPPING.items():
            adapter._graphql_query.reset_mock()
            await adapter.add_reaction("linear:issue-123", "comment-1", name)
            call_args = adapter._graphql_query.call_args
            variables = call_args[0][1]
            assert variables["input"]["emoji"] == unicode_val, f"Failed for {name}"


# ============================================================================
# removeReaction
# ============================================================================


class TestRemoveReaction:
    @pytest.mark.asyncio
    async def test_logs_warning_not_supported(self):
        logger = _make_logger()
        config = LinearAdapterAPIKeyConfig(
            api_key="test-api-key",
            webhook_secret=WEBHOOK_SECRET,
            user_name="test-bot",
            logger=logger,
        )
        adapter = LinearAdapter(config)

        await adapter.remove_reaction("linear:issue-123", "comment-1", "heart")

        logger.warn.assert_called_once()
        assert "removeReaction is not fully supported" in logger.warn.call_args[0][0]


# ============================================================================
# startTyping
# ============================================================================


class TestStartTyping:
    @pytest.mark.asyncio
    async def test_is_noop(self):
        adapter = _make_adapter()
        # No assertion needed -- tests that start_typing completes without raising
        await adapter.start_typing("linear:issue-123")
        assert True


# ============================================================================
# fetchMessages
# ============================================================================


class TestFetchMessages:
    @pytest.mark.asyncio
    async def test_fetches_issue_level_comments(self):
        adapter = _make_webhook_adapter()
        adapter._graphql_query = AsyncMock(
            return_value={
                "data": {
                    "issue": {
                        "comments": {
                            "nodes": [
                                {
                                    "id": "comment-1",
                                    "body": "First comment",
                                    "createdAt": "2025-06-01T10:00:00.000Z",
                                    "updatedAt": "2025-06-01T10:00:00.000Z",
                                    "url": "https://linear.app/comment/1",
                                    "user": {
                                        "id": "user-1",
                                        "displayName": "Alice",
                                        "name": "Alice Smith",
                                    },
                                },
                                {
                                    "id": "comment-2",
                                    "body": "Second comment",
                                    "createdAt": "2025-06-01T11:00:00.000Z",
                                    "updatedAt": "2025-06-01T11:00:00.000Z",
                                    "url": "https://linear.app/comment/2",
                                    "user": {
                                        "id": "user-1",
                                        "displayName": "Alice",
                                        "name": "Alice Smith",
                                    },
                                },
                            ],
                            "pageInfo": {
                                "hasNextPage": False,
                                "endCursor": None,
                            },
                        }
                    }
                }
            }
        )

        result = await adapter.fetch_messages("linear:issue-abc")

        assert len(result.messages) == 2
        assert result.messages[0].text == "First comment"
        assert result.messages[1].text == "Second comment"
        assert result.next_cursor is None

    @pytest.mark.asyncio
    async def test_fetches_comment_thread(self):
        adapter = _make_webhook_adapter()
        # First call: fetch root comment, second call: fetch children
        adapter._graphql_query = AsyncMock(
            return_value={
                "data": {
                    "comment": {
                        "id": "root-comment",
                        "body": "Root comment",
                        "createdAt": "2025-06-01T10:00:00.000Z",
                        "updatedAt": "2025-06-01T10:00:00.000Z",
                        "url": "https://linear.app/comment/root",
                        "user": {
                            "id": "user-1",
                            "displayName": "Bob",
                            "name": "Bob Jones",
                        },
                        "children": {
                            "nodes": [
                                {
                                    "id": "child-1",
                                    "body": "Reply",
                                    "createdAt": "2025-06-01T11:00:00.000Z",
                                    "updatedAt": "2025-06-01T11:00:00.000Z",
                                    "url": "https://linear.app/comment/child-1",
                                    "user": {
                                        "id": "user-1",
                                        "displayName": "Bob",
                                        "name": "Bob Jones",
                                    },
                                }
                            ],
                            "pageInfo": {
                                "hasNextPage": False,
                                "endCursor": None,
                            },
                        },
                    }
                }
            }
        )

        result = await adapter.fetch_messages("linear:issue-abc:c:root-comment")

        # Root + 1 child
        assert len(result.messages) == 2
        assert result.messages[0].text == "Root comment"
        assert result.messages[1].text == "Reply"

    @pytest.mark.asyncio
    async def test_returns_empty_when_comment_not_found(self):
        adapter = _make_webhook_adapter()
        adapter._graphql_query = AsyncMock(return_value={"data": {"comment": None}})

        result = await adapter.fetch_messages("linear:issue-abc:c:nonexistent")

        assert len(result.messages) == 0

    @pytest.mark.asyncio
    async def test_passes_limit_option(self):
        adapter = _make_webhook_adapter()
        adapter._graphql_query = AsyncMock(
            return_value={
                "data": {
                    "issue": {
                        "comments": {
                            "nodes": [],
                            "pageInfo": {
                                "hasNextPage": False,
                                "endCursor": None,
                            },
                        }
                    }
                }
            }
        )

        from chat_sdk.types import FetchOptions

        await adapter.fetch_messages("linear:issue-abc", FetchOptions(limit=10))

        call_args = adapter._graphql_query.call_args
        # Check that the query uses the limit
        variables = call_args[0][1]
        assert variables.get("first") == 10 or "first: 10" in str(call_args)

    @pytest.mark.asyncio
    async def test_returns_next_cursor_when_has_next_page(self):
        adapter = _make_webhook_adapter()
        adapter._graphql_query = AsyncMock(
            return_value={
                "data": {
                    "issue": {
                        "comments": {
                            "nodes": [],
                            "pageInfo": {
                                "hasNextPage": True,
                                "endCursor": "cursor-abc",
                            },
                        }
                    }
                }
            }
        )

        result = await adapter.fetch_messages("linear:issue-abc")

        assert result.next_cursor == "cursor-abc"

    @pytest.mark.asyncio
    async def test_detects_edited_messages(self):
        adapter = _make_webhook_adapter()
        adapter._graphql_query = AsyncMock(
            return_value={
                "data": {
                    "issue": {
                        "comments": {
                            "nodes": [
                                {
                                    "id": "comment-edited",
                                    "body": "Edited text",
                                    "createdAt": "2025-06-01T10:00:00.000Z",
                                    "updatedAt": "2025-06-01T12:00:00.000Z",
                                    "url": "https://linear.app/comment/1",
                                    "user": {
                                        "id": "user-1",
                                        "displayName": "Alice",
                                        "name": "Alice",
                                    },
                                }
                            ],
                            "pageInfo": {"hasNextPage": False},
                        }
                    }
                }
            }
        )

        result = await adapter.fetch_messages("linear:issue-abc")

        assert result.messages[0].metadata.edited is True

    @pytest.mark.asyncio
    async def test_sets_is_me_when_matches_bot_user_id(self):
        adapter = _make_webhook_adapter()
        adapter._bot_user_id = "bot-id"
        adapter._graphql_query = AsyncMock(
            return_value={
                "data": {
                    "issue": {
                        "comments": {
                            "nodes": [
                                {
                                    "id": "comment-bot",
                                    "body": "Bot message",
                                    "createdAt": "2025-06-01T10:00:00.000Z",
                                    "updatedAt": "2025-06-01T10:00:00.000Z",
                                    "url": "https://linear.app/comment/bot",
                                    "user": {
                                        "id": "bot-id",
                                        "displayName": "BotUser",
                                        "name": "Bot",
                                    },
                                }
                            ],
                            "pageInfo": {"hasNextPage": False},
                        }
                    }
                }
            }
        )

        result = await adapter.fetch_messages("linear:issue-abc")

        assert result.messages[0].author.is_me is True
        assert result.messages[0].author.user_id == "bot-id"

    @pytest.mark.asyncio
    async def test_handles_comments_with_no_user(self):
        adapter = _make_webhook_adapter()
        adapter._graphql_query = AsyncMock(
            return_value={
                "data": {
                    "issue": {
                        "comments": {
                            "nodes": [
                                {
                                    "id": "comment-no-user",
                                    "body": "Orphan comment",
                                    "createdAt": "2025-06-01T10:00:00.000Z",
                                    "updatedAt": "2025-06-01T10:00:00.000Z",
                                    "url": "https://linear.app/comment/orphan",
                                    "user": None,
                                }
                            ],
                            "pageInfo": {"hasNextPage": False},
                        }
                    }
                }
            }
        )

        result = await adapter.fetch_messages("linear:issue-abc")

        assert result.messages[0].author.user_id == "unknown"


# ============================================================================
# fetchThread
# ============================================================================


class TestFetchThread:
    @pytest.mark.asyncio
    async def test_returns_thread_info_for_issue(self):
        adapter = _make_webhook_adapter()
        adapter._graphql_query = AsyncMock(
            return_value={
                "data": {
                    "issue": {
                        "identifier": "TEST-42",
                        "title": "Fix the thing",
                        "url": "https://linear.app/test/issue/TEST-42",
                    }
                }
            }
        )

        result = await adapter.fetch_thread("linear:issue-uuid-123")

        assert result.id == "linear:issue-uuid-123"
        assert "TEST-42" in result.channel_name
        assert "Fix the thing" in result.channel_name
        assert result.is_dm is False

    @pytest.mark.asyncio
    async def test_extracts_issue_id_from_comment_thread(self):
        adapter = _make_webhook_adapter()
        adapter._graphql_query = AsyncMock(
            return_value={
                "data": {
                    "issue": {
                        "identifier": "BUG-99",
                        "title": "Regression",
                        "url": "https://linear.app/test/issue/BUG-99",
                    }
                }
            }
        )

        await adapter.fetch_thread("linear:issue-xyz:c:comment-abc")

        # The GraphQL query should use issue-xyz as the issue ID
        call_args = adapter._graphql_query.call_args
        variables = call_args[0][1]
        assert variables["issueId"] == "issue-xyz"


# ============================================================================
# initialize
# ============================================================================


class TestInitialize:
    @pytest.mark.asyncio
    async def test_fetches_bot_user_id(self):
        logger = _make_logger()
        config = LinearAdapterAPIKeyConfig(
            api_key="test-api-key",
            webhook_secret="secret",
            user_name="my-bot",
            logger=logger,
        )
        adapter = LinearAdapter(config)
        adapter._graphql_query = AsyncMock(
            return_value={
                "data": {
                    "viewer": {
                        "id": "viewer-id-123",
                        "displayName": "My Bot",
                    }
                }
            }
        )

        mock_chat = MagicMock()
        await adapter.initialize(mock_chat)

        assert adapter.bot_user_id == "viewer-id-123"
        logger.info.assert_any_call(
            "Linear auth completed",
            {
                "botUserId": "viewer-id-123",
                "displayName": "My Bot",
            },
        )

    @pytest.mark.asyncio
    async def test_warns_when_viewer_fetch_fails(self):
        logger = _make_logger()
        config = LinearAdapterAPIKeyConfig(
            api_key="test-api-key",
            webhook_secret="secret",
            user_name="my-bot",
            logger=logger,
        )
        adapter = LinearAdapter(config)
        adapter._graphql_query = AsyncMock(side_effect=Exception("Auth failed"))

        mock_chat = MagicMock()
        await adapter.initialize(mock_chat)

        assert adapter.bot_user_id is None
        logger.warn.assert_called()


# ============================================================================
# ensureValidToken
# ============================================================================


class TestEnsureValidToken:
    @pytest.mark.asyncio
    async def test_no_refresh_when_no_client_credentials(self):
        adapter = _make_webhook_adapter()
        adapter._graphql_query = AsyncMock(return_value={"data": {"commentDelete": {"success": True}}})

        # Should not throw - just calls through
        await adapter.delete_message("linear:issue-123", "comment-1")
        assert adapter._graphql_query.call_count == 1

    @pytest.mark.asyncio
    async def test_refreshes_when_token_expired(self):
        logger = _make_logger()
        config = LinearAdapterAppConfig(
            client_id="test-client",
            client_secret="test-secret",
            webhook_secret="secret",
            user_name="bot",
            logger=logger,
        )
        adapter = LinearAdapter(config)

        # Set expiry in the past
        adapter._access_token_expiry = time.time() - 1000
        adapter._refresh_client_credentials_token = AsyncMock()
        adapter._graphql_query = AsyncMock(return_value={"data": {"commentDelete": {"success": True}}})

        await adapter.delete_message("linear:issue-123", "comment-1")

        assert adapter._refresh_client_credentials_token.call_count == 1

    @pytest.mark.asyncio
    async def test_no_refresh_when_token_valid(self):
        logger = _make_logger()
        config = LinearAdapterAppConfig(
            client_id="test-client",
            client_secret="test-secret",
            webhook_secret="secret",
            user_name="bot",
            logger=logger,
        )
        adapter = LinearAdapter(config)

        # Set expiry far in the future
        adapter._access_token_expiry = time.time() + 86400
        adapter._access_token = "valid-token"
        adapter._refresh_client_credentials_token = AsyncMock()
        adapter._graphql_query = AsyncMock(return_value={"data": {"commentDelete": {"success": True}}})

        await adapter.delete_message("linear:issue-123", "comment-1")

        assert adapter._refresh_client_credentials_token.call_count == 0


# ============================================================================
# refreshClientCredentialsToken
# ============================================================================


class TestRefreshClientCredentialsToken:
    @pytest.mark.asyncio
    async def test_is_noop_when_no_client_credentials(self):
        adapter = _make_webhook_adapter()  # API key mode

        # No assertion needed -- tests that the call completes without raising
        await adapter._refresh_client_credentials_token()
        assert True

    @pytest.mark.asyncio
    async def test_sets_access_token_expiry_with_buffer(self):
        logger = _make_logger()
        config = LinearAdapterAppConfig(
            client_id="test-client",
            client_secret="test-secret",
            webhook_secret="secret",
            user_name="bot",
            logger=logger,
        )
        adapter = LinearAdapter(config)

        expires_in = 2592000  # 30 days in seconds

        mock_response = AsyncMock()
        mock_response.ok = True
        mock_response.json = AsyncMock(
            return_value={
                "access_token": "token-123",
                "expires_in": expires_in,
            }
        )
        mock_response.text = AsyncMock(return_value="")

        # Create a mock context manager for the post call
        mock_post_cm = AsyncMock()
        mock_post_cm.__aenter__ = AsyncMock(return_value=mock_response)
        mock_post_cm.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_post_cm)

        # Create mock context manager for ClientSession
        mock_session_cm = MagicMock()
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=False)

        # aiohttp is lazily imported inside _refresh_client_credentials_token,
        # so we patch it at the module level where it's imported
        import importlib
        import sys

        mock_aiohttp = MagicMock()
        mock_aiohttp.ClientSession = MagicMock(return_value=mock_session_cm)

        # Temporarily replace aiohttp in sys.modules
        original = sys.modules.get("aiohttp")
        sys.modules["aiohttp"] = mock_aiohttp
        try:
            before = time.time()
            await adapter._refresh_client_credentials_token()
            after = time.time()
        finally:
            if original is not None:
                sys.modules["aiohttp"] = original
            else:
                del sys.modules["aiohttp"]

        # expiry should be approximately now + expires_in - 3600 (1 hour buffer)
        expected_min = before + expires_in - 3600
        expected_max = after + expires_in - 3600
        assert adapter._access_token_expiry >= expected_min
        assert adapter._access_token_expiry <= expected_max


# ============================================================================
# Webhook - reaction handling
# ============================================================================


class TestWebhookReactionEvents:
    @pytest.mark.asyncio
    async def test_logs_reaction_events(self):
        logger = _make_logger()
        adapter = _make_webhook_adapter(logger=logger)
        mock_chat = MagicMock()
        mock_chat.process_message = MagicMock()
        mock_chat.get_state = MagicMock(return_value=None)
        adapter._chat = mock_chat

        payload = _create_reaction_payload(emoji="\U0001f525")
        body = json.dumps(payload)
        sig = _sign_payload(body)
        request = _build_webhook_request(body, signature=sig)
        response = await adapter.handle_webhook(request)

        assert response["status"] == 200
        # Verify the reaction webhook was logged with the correct emoji
        found_reaction_log = False
        for call in logger.debug.call_args_list:
            if call[0][0] == "Received reaction webhook":
                log_data = call[0][1]
                assert log_data["emoji"] == "\U0001f525"
                assert log_data["action"] == "create"
                found_reaction_log = True
                break
        assert found_reaction_log, "Expected 'Received reaction webhook' debug log"

    @pytest.mark.asyncio
    async def test_silent_return_when_chat_not_initialized(self):
        logger = _make_logger()
        adapter = _make_webhook_adapter(logger=logger)
        # chat is None (not initialized)

        payload = _create_reaction_payload()
        body = json.dumps(payload)
        sig = _sign_payload(body)
        request = _build_webhook_request(body, signature=sig)
        response = await adapter.handle_webhook(request)

        assert response["status"] == 200


# ============================================================================
# Webhook - unknown event types
# ============================================================================


class TestWebhookUnknownEvents:
    @pytest.mark.asyncio
    async def test_returns_200_for_unhandled_types(self):
        adapter = _make_webhook_adapter()
        payload = {
            "type": "Issue",
            "action": "create",
            "webhookTimestamp": int(time.time() * 1000),
            "data": {"id": "issue-1"},
            "actor": {"id": "user-1", "name": "Test", "type": "user"},
        }
        body = json.dumps(payload)
        sig = _sign_payload(body)
        request = _build_webhook_request(body, signature=sig)
        response = await adapter.handle_webhook(request)
        assert response["status"] == 200


# ============================================================================
# buildMessage via webhook
# ============================================================================


class TestBuildMessageViaWebhook:
    @pytest.mark.asyncio
    async def test_sets_author_fields_from_actor(self):
        logger = _make_logger()
        adapter = _make_webhook_adapter(logger=logger)
        mock_chat = MagicMock()
        mock_chat.process_message = MagicMock()
        mock_chat.get_state = MagicMock(return_value=None)
        adapter._chat = mock_chat

        payload = _create_comment_payload(actor_type="user")
        body = json.dumps(payload)
        sig = _sign_payload(body)
        request = _build_webhook_request(body, signature=sig)
        await adapter.handle_webhook(request)

        message = mock_chat.process_message.call_args[0][2]
        assert message.author.user_name == "Test User"
        assert message.author.full_name == "Test User"
        assert message.author.is_bot is False
        assert message.author.is_me is False

    @pytest.mark.asyncio
    async def test_sets_is_bot_for_application_actors(self):
        logger = _make_logger()
        adapter = _make_webhook_adapter(logger=logger)
        mock_chat = MagicMock()
        mock_chat.process_message = MagicMock()
        mock_chat.get_state = MagicMock(return_value=None)
        adapter._chat = mock_chat

        payload = _create_comment_payload(actor_type="application")
        body = json.dumps(payload)
        sig = _sign_payload(body)
        request = _build_webhook_request(body, signature=sig)
        await adapter.handle_webhook(request)

        message = mock_chat.process_message.call_args[0][2]
        assert message.author.is_bot is True

    @pytest.mark.asyncio
    async def test_sets_is_bot_for_integration_actors(self):
        logger = _make_logger()
        adapter = _make_webhook_adapter(logger=logger)
        mock_chat = MagicMock()
        mock_chat.process_message = MagicMock()
        mock_chat.get_state = MagicMock(return_value=None)
        adapter._chat = mock_chat

        payload = _create_comment_payload(actor_type="integration")
        body = json.dumps(payload)
        sig = _sign_payload(body)
        request = _build_webhook_request(body, signature=sig)
        await adapter.handle_webhook(request)

        message = mock_chat.process_message.call_args[0][2]
        assert message.author.is_bot is True

    @pytest.mark.asyncio
    async def test_sets_date_sent_from_created_at(self):
        logger = _make_logger()
        adapter = _make_webhook_adapter(logger=logger)
        mock_chat = MagicMock()
        mock_chat.process_message = MagicMock()
        mock_chat.get_state = MagicMock(return_value=None)
        adapter._chat = mock_chat

        payload = _create_comment_payload()
        payload["data"]["createdAt"] = "2025-03-15T10:30:00.000Z"
        payload["data"]["updatedAt"] = "2025-03-15T10:30:00.000Z"
        body = json.dumps(payload)
        sig = _sign_payload(body)
        request = _build_webhook_request(body, signature=sig)
        await adapter.handle_webhook(request)

        message = mock_chat.process_message.call_args[0][2]
        assert message.metadata.date_sent.year == 2025
        assert message.metadata.date_sent.month == 3
        assert message.metadata.date_sent.day == 15
        assert message.metadata.edited is False

    @pytest.mark.asyncio
    async def test_detects_edited_from_differing_timestamps(self):
        logger = _make_logger()
        adapter = _make_webhook_adapter(logger=logger)
        mock_chat = MagicMock()
        mock_chat.process_message = MagicMock()
        mock_chat.get_state = MagicMock(return_value=None)
        adapter._chat = mock_chat

        payload = _create_comment_payload()
        payload["data"]["createdAt"] = "2025-03-15T10:30:00.000Z"
        payload["data"]["updatedAt"] = "2025-03-15T11:00:00.000Z"
        body = json.dumps(payload)
        sig = _sign_payload(body)
        request = _build_webhook_request(body, signature=sig)
        await adapter.handle_webhook(request)

        message = mock_chat.process_message.call_args[0][2]
        assert message.metadata.edited is True

    @pytest.mark.asyncio
    async def test_includes_raw_comment_data(self):
        logger = _make_logger()
        adapter = _make_webhook_adapter(logger=logger)
        mock_chat = MagicMock()
        mock_chat.process_message = MagicMock()
        mock_chat.get_state = MagicMock(return_value=None)
        adapter._chat = mock_chat

        payload = _create_comment_payload(body="Some text")
        body = json.dumps(payload)
        sig = _sign_payload(body)
        request = _build_webhook_request(body, signature=sig)
        await adapter.handle_webhook(request)

        message = mock_chat.process_message.call_args[0][2]
        assert message.raw["comment"]["body"] == "Some text"


# ============================================================================
# Webhook - chat not initialized
# ============================================================================


class TestWebhookChatNotInitialized:
    @pytest.mark.asyncio
    async def test_ignores_comments_when_not_initialized(self):
        logger = _make_logger()
        adapter = _make_webhook_adapter(logger=logger)
        # chat is None

        payload = _create_comment_payload()
        body = json.dumps(payload)
        sig = _sign_payload(body)
        request = _build_webhook_request(body, signature=sig)
        response = await adapter.handle_webhook(request)

        assert response["status"] == 200
        logger.warn.assert_called()


# ============================================================================
# createLinearAdapter factory
# ============================================================================


class TestCreateLinearAdapterFactory:
    def test_creates_with_api_key(self):
        adapter = create_linear_adapter(
            LinearAdapterAPIKeyConfig(
                api_key="lin_api_123",
                webhook_secret="secret",
            )
        )
        assert isinstance(adapter, LinearAdapter)
        assert adapter.name == "linear"

    def test_creates_with_access_token(self):
        adapter = create_linear_adapter(
            LinearAdapterOAuthConfig(
                access_token="lin_oauth_123",
                webhook_secret="secret",
            )
        )
        assert isinstance(adapter, LinearAdapter)

    def test_creates_with_client_credentials(self):
        adapter = create_linear_adapter(
            LinearAdapterAppConfig(
                client_id="client-id",
                client_secret="client-secret",
                webhook_secret="secret",
            )
        )
        assert isinstance(adapter, LinearAdapter)

    def test_throws_without_webhook_secret(self, monkeypatch):
        monkeypatch.delenv("LINEAR_WEBHOOK_SECRET", raising=False)
        with pytest.raises(ValidationError, match="webhook_secret"):
            create_linear_adapter(LinearAdapterAPIKeyConfig(api_key="key", webhook_secret=None))

    def test_uses_env_webhook_secret(self, monkeypatch):
        monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", "env-secret")
        adapter = create_linear_adapter(LinearAdapterAPIKeyConfig(api_key="key"))
        assert isinstance(adapter, LinearAdapter)

    def test_uses_env_api_key(self, monkeypatch):
        monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", "env-secret")
        monkeypatch.setenv("LINEAR_API_KEY", "env-api-key")
        adapter = create_linear_adapter()
        assert isinstance(adapter, LinearAdapter)

    def test_uses_env_access_token(self, monkeypatch):
        monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", "env-secret")
        monkeypatch.setenv("LINEAR_ACCESS_TOKEN", "env-access-token")
        monkeypatch.delenv("LINEAR_API_KEY", raising=False)
        adapter = create_linear_adapter()
        assert isinstance(adapter, LinearAdapter)

    def test_uses_env_client_credentials(self, monkeypatch):
        monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", "env-secret")
        monkeypatch.setenv("LINEAR_CLIENT_ID", "env-client-id")
        monkeypatch.setenv("LINEAR_CLIENT_SECRET", "env-client-secret")
        monkeypatch.delenv("LINEAR_API_KEY", raising=False)
        monkeypatch.delenv("LINEAR_ACCESS_TOKEN", raising=False)
        adapter = create_linear_adapter()
        assert isinstance(adapter, LinearAdapter)

    def test_throws_when_no_auth_available(self, monkeypatch):
        monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", "env-secret")
        monkeypatch.delenv("LINEAR_API_KEY", raising=False)
        monkeypatch.delenv("LINEAR_ACCESS_TOKEN", raising=False)
        monkeypatch.delenv("LINEAR_CLIENT_ID", raising=False)
        monkeypatch.delenv("LINEAR_CLIENT_SECRET", raising=False)
        with pytest.raises(ValidationError, match="Authentication is required"):
            create_linear_adapter()

    def test_uses_env_bot_username(self, monkeypatch):
        monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", "env-secret")
        monkeypatch.setenv("LINEAR_BOT_USERNAME", "custom-bot-name")
        monkeypatch.setenv("LINEAR_API_KEY", "key")
        adapter = create_linear_adapter()
        assert adapter.user_name == "custom-bot-name"

    def test_defaults_username_to_linear_bot(self, monkeypatch):
        monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", "env-secret")
        monkeypatch.setenv("LINEAR_API_KEY", "key")
        monkeypatch.delenv("LINEAR_BOT_USERNAME", raising=False)
        adapter = create_linear_adapter()
        assert adapter.user_name == "linear-bot"

    def test_prefers_config_username_over_env(self, monkeypatch):
        monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", "env-secret")
        monkeypatch.setenv("LINEAR_BOT_USERNAME", "env-name")
        adapter = create_linear_adapter(
            LinearAdapterAPIKeyConfig(
                api_key="key",
                webhook_secret="secret",
                user_name="config-name",
            )
        )
        assert adapter.user_name == "config-name"

    def test_accepts_custom_logger(self):
        custom_logger = _make_logger()
        adapter = create_linear_adapter(
            LinearAdapterAPIKeyConfig(
                api_key="key",
                webhook_secret="secret",
                logger=custom_logger,
            )
        )
        assert isinstance(adapter, LinearAdapter)


# ============================================================================
# Format converter coverage (renderPostable, toAst, fromAst)
# ============================================================================


class TestLinearFormatConverterAdditional:
    def test_render_postable_plain(self):
        from chat_sdk.adapters.linear.format_converter import LinearFormatConverter

        c = LinearFormatConverter()
        assert c.render_postable("Hello world") == "Hello world"

    def test_render_postable_raw(self):
        from chat_sdk.adapters.linear.format_converter import LinearFormatConverter

        c = LinearFormatConverter()
        assert c.render_postable({"raw": "raw content"}) == "raw content"

    def test_render_postable_markdown(self):
        from chat_sdk.adapters.linear.format_converter import LinearFormatConverter

        c = LinearFormatConverter()
        result = c.render_postable({"markdown": "**bold**"})
        assert "bold" in result


# ============================================================================
# initialize - client credentials mode
# ============================================================================


class TestInitializeClientCredentials:
    @pytest.mark.asyncio
    async def test_calls_refresh_token(self):
        logger = _make_logger()
        config = LinearAdapterAppConfig(
            client_id="test-client-id",
            client_secret="test-client-secret",
            webhook_secret="secret",
            user_name="my-bot",
            logger=logger,
        )
        adapter = LinearAdapter(config)
        adapter._refresh_client_credentials_token = AsyncMock()
        adapter._graphql_query = AsyncMock(
            return_value={
                "data": {
                    "viewer": {"id": "viewer-123", "displayName": "Bot"},
                }
            }
        )

        mock_chat = MagicMock()
        await adapter.initialize(mock_chat)

        assert adapter._refresh_client_credentials_token.call_count == 1


# ============================================================================
# postMessage - ensure_valid_token is called
# ============================================================================


class TestEditMessageEnsureToken:
    @pytest.mark.asyncio
    async def test_calls_ensure_valid_token(self):
        adapter = _make_webhook_adapter()
        adapter._ensure_valid_token = AsyncMock()
        adapter._graphql_query = AsyncMock(
            return_value={
                "data": {
                    "commentUpdate": {
                        "success": True,
                        "comment": {
                            "id": "c1",
                            "body": "updated",
                            "url": "https://linear.app/test",
                            "createdAt": "2025-06-01T12:00:00.000Z",
                            "updatedAt": "2025-06-01T13:00:00.000Z",
                        },
                    }
                }
            }
        )

        await adapter.edit_message("linear:issue-123", "c1", "updated")

        assert adapter._ensure_valid_token.call_count == 1


# ============================================================================
# deleteMessage - ensure_valid_token is called
# ============================================================================


class TestDeleteMessageEnsureToken:
    @pytest.mark.asyncio
    async def test_calls_ensure_valid_token(self):
        adapter = _make_webhook_adapter()
        adapter._ensure_valid_token = AsyncMock()
        adapter._graphql_query = AsyncMock(return_value={"data": {"commentDelete": {"success": True}}})

        await adapter.delete_message("linear:issue-123", "comment-1")

        assert adapter._ensure_valid_token.call_count == 1


# ============================================================================
# addReaction - ensure_valid_token is called
# ============================================================================


class TestAddReactionEnsureToken:
    @pytest.mark.asyncio
    async def test_calls_ensure_valid_token(self):
        adapter = _make_webhook_adapter()
        adapter._ensure_valid_token = AsyncMock()
        adapter._graphql_query = AsyncMock(return_value={"data": {"reactionCreate": {"success": True}}})

        await adapter.add_reaction("linear:issue-123", "comment-1", "heart")

        assert adapter._ensure_valid_token.call_count == 1


# ============================================================================
# fetchMessages - ensure_valid_token is called
# ============================================================================


class TestFetchMessagesEnsureToken:
    @pytest.mark.asyncio
    async def test_calls_ensure_valid_token(self):
        adapter = _make_webhook_adapter()
        adapter._ensure_valid_token = AsyncMock()
        adapter._graphql_query = AsyncMock(
            return_value={
                "data": {
                    "issue": {
                        "comments": {
                            "nodes": [],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        )

        await adapter.fetch_messages("linear:issue-abc")

        assert adapter._ensure_valid_token.call_count == 1


# ============================================================================
# fetchThread - ensure_valid_token is called
# ============================================================================


class TestFetchThreadEnsureToken:
    @pytest.mark.asyncio
    async def test_calls_ensure_valid_token(self):
        adapter = _make_webhook_adapter()
        adapter._ensure_valid_token = AsyncMock()
        adapter._graphql_query = AsyncMock(
            return_value={
                "data": {
                    "issue": {
                        "identifier": "TEST-1",
                        "title": "Title",
                        "url": "https://linear.app",
                    }
                }
            }
        )

        await adapter.fetch_thread("linear:issue-abc")

        assert adapter._ensure_valid_token.call_count == 1
