"""Linear adapter for chat SDK.

Supports comment threads on Linear issues.
Authentication via personal API key, OAuth access token, or client credentials.

Python port of packages/adapter-linear/src/index.ts.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, cast

from chat_sdk.adapters.linear.cards import card_to_linear_markdown
from chat_sdk.adapters.linear.format_converter import LinearFormatConverter
from chat_sdk.adapters.linear.types import (
    CommentWebhookPayload,
    LinearAdapterBaseConfig,
    LinearAdapterConfig,
    LinearCommentData,
    LinearRawMessage,
    LinearThreadId,
    LinearWebhookActor,
    ReactionWebhookPayload,
)
from chat_sdk.emoji import convert_emoji_placeholders
from chat_sdk.logger import ConsoleLogger, Logger
from chat_sdk.shared.adapter_utils import extract_card
from chat_sdk.shared.errors import (
    AdapterError,
    AuthenticationError,
    NetworkError,
    ValidationError,
)
from chat_sdk.types import (
    AdapterPostableMessage,
    Author,
    ChatInstance,
    EmojiValue,
    FetchOptions,
    FetchResult,
    FormattedContent,
    LockScope,
    Message,
    MessageMetadata,
    PostableRaw,
    RawMessage,
    StreamOptions,
    ThreadInfo,
    UserInfo,
    WebhookOptions,
    _parse_iso,
)

COMMENT_THREAD_PATTERN = re.compile(r"^([^:]+):c:([^:]+)$")

# Linear GraphQL API endpoint
LINEAR_API_URL = "https://api.linear.app/graphql"

# Linear OAuth token endpoint
LINEAR_TOKEN_URL = "https://api.linear.app/oauth/token"

# Emoji mapping for Linear reactions (unicode)
EMOJI_MAPPING: dict[str, str] = {
    "thumbs_up": "\U0001f44d",
    "thumbs_down": "\U0001f44e",
    "heart": "\u2764\ufe0f",
    "fire": "\U0001f525",
    "rocket": "\U0001f680",
    "eyes": "\U0001f440",
    "check": "\u2705",
    "warning": "\u26a0\ufe0f",
    "sparkles": "\u2728",
    "wave": "\U0001f44b",
    "raised_hands": "\U0001f64c",
    "laugh": "\U0001f604",
    "hooray": "\U0001f389",
    "confused": "\U0001f615",
}


class LinearAdapter:
    """Linear adapter for chat SDK.

    Implements the Adapter interface for Linear issue comments.
    """

    def __init__(self, config: LinearAdapterConfig | None = None) -> None:
        if config is None:
            config = LinearAdapterBaseConfig()

        webhook_secret = getattr(config, "webhook_secret", None) or os.environ.get("LINEAR_WEBHOOK_SECRET")
        if not webhook_secret:
            raise ValidationError(
                "linear",
                "webhook_secret is required. Set LINEAR_WEBHOOK_SECRET or provide it in config.",
            )

        self._name = "linear"
        self._webhook_secret = webhook_secret
        self._logger: Logger = getattr(config, "logger", None) or ConsoleLogger("info", prefix="linear")
        self._user_name = getattr(config, "user_name", None) or os.environ.get("LINEAR_BOT_USERNAME", "linear-bot")
        self._chat: ChatInstance | None = None
        self._bot_user_id: str | None = None
        self._format_converter = LinearFormatConverter()

        # Shared aiohttp session for connection pooling
        self._http_session: Any | None = None

        # Authentication state
        self._access_token: str | None = None
        self._access_token_expiry: float | None = None
        self._client_credentials: dict[str, str] | None = None
        self._token_lock = asyncio.Lock()

        # Determine auth method
        api_key = getattr(config, "api_key", None)
        access_token = getattr(config, "access_token", None)
        client_id = getattr(config, "client_id", None)
        client_secret = getattr(config, "client_secret", None)

        if api_key:
            self._access_token = api_key
        elif access_token:
            self._access_token = access_token
        elif client_id and client_secret:
            self._client_credentials = {
                "client_id": client_id,
                "client_secret": client_secret,
            }
        else:
            # Auto-detect from env vars
            env_api_key = os.environ.get("LINEAR_API_KEY")
            if env_api_key:
                self._access_token = env_api_key
            else:
                env_access_token = os.environ.get("LINEAR_ACCESS_TOKEN")
                if env_access_token:
                    self._access_token = env_access_token
                else:
                    env_client_id = os.environ.get("LINEAR_CLIENT_ID")
                    env_client_secret = os.environ.get("LINEAR_CLIENT_SECRET")
                    if env_client_id and env_client_secret:
                        self._client_credentials = {
                            "client_id": env_client_id,
                            "client_secret": env_client_secret,
                        }
                    else:
                        raise ValidationError(
                            "linear",
                            "Authentication is required. Set LINEAR_API_KEY, LINEAR_ACCESS_TOKEN, "
                            "or LINEAR_CLIENT_ID/LINEAR_CLIENT_SECRET, or provide auth in config.",
                        )

    @property
    def name(self) -> str:
        return self._name

    @property
    def user_name(self) -> str:
        return self._user_name

    @property
    def bot_user_id(self) -> str | None:
        return self._bot_user_id

    @property
    def lock_scope(self) -> LockScope | None:
        return None

    @property
    def persist_message_history(self) -> bool | None:
        return None

    async def initialize(self, chat: ChatInstance) -> None:
        """Initialize the adapter and fetch the bot's user ID."""
        self._chat = chat

        # For client credentials mode, fetch an access token first
        if self._client_credentials:
            await self._refresh_client_credentials_token()

        # Fetch the bot's user ID for self-message detection
        try:
            viewer = await self._graphql_query("query { viewer { id displayName } }")
            viewer_data = viewer.get("data", {}).get("viewer", {})
            self._bot_user_id = viewer_data.get("id")
            self._logger.info(
                "Linear auth completed",
                {
                    "botUserId": self._bot_user_id,
                    "displayName": viewer_data.get("displayName"),
                },
            )
        except Exception as error:
            self._logger.warn("Could not fetch Linear bot user ID", {"error": str(error)})

    async def _refresh_client_credentials_token(self) -> None:
        """Fetch a new access token using client credentials grant."""
        if not self._client_credentials:
            return

        import aiohttp  # lazy import (needed for ClientError)

        try:
            session = await self._get_http_session()
            async with session.post(
                LINEAR_TOKEN_URL,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_credentials["client_id"],
                    "client_secret": self._client_credentials["client_secret"],
                    "scope": "read,write,comments:create,issues:create",
                },
            ) as response:
                if not response.ok:
                    error_body = await response.text()
                    raise AuthenticationError(
                        "linear",
                        f"Failed to fetch Linear client credentials token: {response.status} {error_body}",
                    )

                data = await response.json()
                self._access_token = data["access_token"]
                # Track expiry with 1 hour buffer
                self._access_token_expiry = time.time() + data.get("expires_in", 86400) - 3600

                self._logger.info(
                    "Linear client credentials token obtained",
                    {
                        "expiresIn": f"{round(data.get('expires_in', 0) / 86400)} days",
                    },
                )
        except AuthenticationError:
            raise
        except aiohttp.ClientError as exc:
            raise NetworkError(
                "linear",
                f"Network error obtaining Linear client credentials token: {exc}",
                exc,
            ) from exc

    async def _ensure_valid_token(self) -> None:
        """Ensure the client credentials token is still valid. Refresh if expired."""
        if not (self._client_credentials and self._access_token_expiry and time.time() > self._access_token_expiry):
            return
        async with self._token_lock:
            # Double-check after acquiring lock to avoid redundant refreshes
            if self._access_token_expiry and time.time() > self._access_token_expiry:
                self._logger.info("Linear access token expired, refreshing...")
                await self._refresh_client_credentials_token()

    async def get_user(self, user_id: str) -> UserInfo | None:
        """Look up a Linear user by UUID via the GraphQL ``user`` query.

        Returns ``None`` on any failure (auth missing, user not found,
        network error). Mirrors upstream ``LinearAdapter.getUser``
        (vercel/chat#391), which uses the official Linear SDK; we issue
        the equivalent GraphQL query directly so we don't take a runtime
        dependency on the JS SDK.
        """
        try:
            await self._ensure_valid_token()
            data = await self._graphql_query(
                "query GetUser($id: String!) {  user(id: $id) {    id displayName name email avatarUrl  }}",
                {"id": user_id},
            )
        except Exception:
            return None
        user = (data.get("data") or {}).get("user") if isinstance(data, dict) else None
        if not user or not isinstance(user, dict):
            return None
        display_name = user.get("displayName") or user.get("name") or user_id
        return UserInfo(
            user_id=user.get("id") or user_id,
            user_name=display_name,
            full_name=user.get("name") or display_name,
            is_bot=False,
            avatar_url=user.get("avatarUrl"),
            email=user.get("email"),
        )

    async def handle_webhook(
        self,
        request: Any,
        options: WebhookOptions | None = None,
    ) -> Any:
        """Handle incoming webhook from Linear.

        See: https://linear.app/developers/webhooks
        """
        body = await self._get_request_body(request)
        self._logger.debug("Linear webhook raw body", {"body": body[:500] if body else ""})

        # Verify request signature (Linear-Signature header)
        signature = self._get_header(request, "linear-signature")
        if not self._verify_signature(body, signature):
            return self._make_response("Invalid signature", 401)

        try:
            payload: dict[str, Any] = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            self._logger.error(
                "Linear webhook invalid JSON",
                {
                    "contentType": self._get_header(request, "content-type"),
                    "bodyPreview": body[:200] if body else "",
                },
            )
            return self._make_response("Invalid JSON", 400)

        # Validate webhook timestamp (within 5 minutes)
        webhook_timestamp = payload.get("webhookTimestamp")
        if webhook_timestamp:
            time_diff = abs(int(time.time() * 1000) - webhook_timestamp)
            if time_diff > 5 * 60 * 1000:
                self._logger.warn(
                    "Linear webhook timestamp too old",
                    {
                        "webhookTimestamp": webhook_timestamp,
                        "timeDiff": time_diff,
                    },
                )
                return self._make_response("Webhook expired", 401)

        # Handle events based on type. The payload shape is determined by
        # `type` at runtime — cast to the matching TypedDict so each handler
        # sees the right variant.
        payload_type = payload.get("type")
        if payload_type == "Comment":
            if payload.get("action") == "create":
                self._handle_comment_created(cast("CommentWebhookPayload", payload), options)
        elif payload_type == "Reaction":
            self._handle_reaction(cast("ReactionWebhookPayload", payload))

        return self._make_response("ok", 200)

    def _verify_signature(self, body: str, signature: str | None) -> bool:
        """Verify Linear webhook signature using HMAC-SHA256."""
        if not signature:
            return False

        computed = hmac.new(
            self._webhook_secret.encode("utf-8"),
            body.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        try:
            return hmac.compare_digest(
                bytes.fromhex(computed),
                bytes.fromhex(signature),
            )
        except (ValueError, TypeError):
            return False

    def _handle_comment_created(
        self,
        payload: CommentWebhookPayload,
        options: WebhookOptions | None = None,
    ) -> None:
        """Handle a new comment created on an issue."""
        if not self._chat:
            self._logger.warn("Chat instance not initialized, ignoring comment")
            return

        # TypedDict `.get()` unions every field-type from the union of shapes
        # (comment-created payloads vs older camel/snake fallbacks), producing
        # `object | str`. Cast to `str` where we've runtime-narrowed via the
        # truthy check — the dispatch block already filtered to `Comment`
        # events, so these keys are known to be strings.
        data = cast("LinearCommentData", payload.get("data", {}))
        actor = cast("LinearWebhookActor", payload.get("actor", {}))

        # Skip non-issue comments
        issue_id = cast("str | None", data.get("issueId") or data.get("issue_id"))
        if not issue_id:
            self._logger.debug("Ignoring non-issue comment", {"commentId": data.get("id")})
            return

        # Determine thread
        parent_id = data.get("parentId") or data.get("parent_id")
        root_comment_id = cast("str | None", parent_id or data.get("id"))
        thread_id = self.encode_thread_id(
            LinearThreadId(
                issue_id=issue_id,
                comment_id=root_comment_id,
            )
        )

        message = self._build_message(data, actor, thread_id)

        # Skip bot's own messages
        user_id = data.get("userId") or data.get("user_id")
        if user_id == self._bot_user_id:
            self._logger.debug("Ignoring message from self", {"messageId": data.get("id")})
            return

        self._chat.process_message(self, thread_id, message, options)

    def _handle_reaction(self, payload: ReactionWebhookPayload) -> None:
        """Handle reaction events (logging only)."""
        if not self._chat:
            return

        data = payload.get("data", {})
        actor = payload.get("actor", {})

        self._logger.debug(
            "Received reaction webhook",
            {
                "reactionId": data.get("id"),
                "emoji": data.get("emoji"),
                "commentId": data.get("commentId") or data.get("comment_id"),
                "action": payload.get("action"),
                "actorName": actor.get("name"),
            },
        )

    def _build_message(
        self,
        comment: LinearCommentData,
        actor: LinearWebhookActor,
        thread_id: str,
    ) -> Message:
        """Build a Message from a Linear comment and actor."""
        # `comment.get("body")` unions every value type across the TypedDict
        # variants, giving `object | str`. Cast to `str` where the runtime
        # shape guarantees a string (Linear webhook `Comment` payloads
        # always have `body`, `userId`, `createdAt`, `updatedAt` as strings).
        text = cast("str", comment.get("body", ""))
        user_id = cast("str", comment.get("userId") or comment.get("user_id", ""))

        author = Author(
            user_id=user_id,
            user_name=actor.get("name", "unknown"),
            full_name=actor.get("name", "unknown"),
            is_bot=actor.get("type", "user") != "user",
            is_me=user_id == self._bot_user_id,
        )

        formatted = self._format_converter.to_ast(text)

        created_at = cast("str", comment.get("createdAt") or comment.get("created_at", ""))
        updated_at = cast("str", comment.get("updatedAt") or comment.get("updated_at", ""))

        return Message(
            id=comment.get("id", ""),
            thread_id=thread_id,
            text=text,
            formatted=formatted,
            raw=LinearRawMessage(comment=comment),
            author=author,
            metadata=MessageMetadata(
                date_sent=_parse_iso(created_at) if created_at else datetime.now(timezone.utc),
                edited=created_at != updated_at,
                edited_at=_parse_iso(updated_at) if (created_at != updated_at and updated_at) else None,
            ),
            attachments=[],
        )

    async def post_message(
        self,
        thread_id: str,
        message: AdapterPostableMessage,
    ) -> RawMessage:
        """Post a message to a thread (create a comment on an issue)."""
        await self._ensure_valid_token()
        decoded = self.decode_thread_id(thread_id)

        # Render message to markdown
        card = extract_card(message)
        body = card_to_linear_markdown(card) if card else self._format_converter.render_postable(message)

        # Convert emoji placeholders to unicode
        body = convert_emoji_placeholders(body, "linear")

        # Create comment via GraphQL API
        result = await self._graphql_query(
            """
            mutation CommentCreate($input: CommentCreateInput!) {
                commentCreate(input: $input) {
                    success
                    comment {
                        id
                        body
                        url
                        createdAt
                        updatedAt
                    }
                }
            }
            """,
            {
                "input": {
                    "issueId": decoded.issue_id,
                    "body": body,
                    **({"parentId": decoded.comment_id} if decoded.comment_id else {}),
                }
            },
        )

        comment_data = result.get("data", {}).get("commentCreate", {}).get("comment")
        if not comment_data:
            raise AdapterError("Failed to create comment on Linear issue", "linear")

        return RawMessage(
            id=comment_data.get("id", ""),
            thread_id=thread_id,
            raw=LinearRawMessage(
                comment={
                    "id": comment_data.get("id", ""),
                    "body": comment_data.get("body", ""),
                    "issueId": decoded.issue_id,
                    "userId": self._bot_user_id or "",
                    "createdAt": comment_data.get("createdAt", ""),
                    "updatedAt": comment_data.get("updatedAt", ""),
                    "url": comment_data.get("url"),
                },
            ),
        )

    async def edit_message(
        self,
        thread_id: str,
        message_id: str,
        message: AdapterPostableMessage,
    ) -> RawMessage:
        """Edit an existing message (update a comment)."""
        await self._ensure_valid_token()
        decoded = self.decode_thread_id(thread_id)

        card = extract_card(message)
        body = card_to_linear_markdown(card) if card else self._format_converter.render_postable(message)

        body = convert_emoji_placeholders(body, "linear")

        result = await self._graphql_query(
            """
            mutation CommentUpdate($id: String!, $input: CommentUpdateInput!) {
                commentUpdate(id: $id, input: $input) {
                    success
                    comment {
                        id
                        body
                        url
                        createdAt
                        updatedAt
                    }
                }
            }
            """,
            {"id": message_id, "input": {"body": body}},
        )

        comment_data = result.get("data", {}).get("commentUpdate", {}).get("comment")
        if not comment_data:
            raise AdapterError("Failed to update comment on Linear", "linear")

        return RawMessage(
            id=comment_data.get("id", ""),
            thread_id=thread_id,
            raw=LinearRawMessage(
                comment={
                    "id": comment_data.get("id", ""),
                    "body": comment_data.get("body", ""),
                    "issueId": decoded.issue_id,
                    "userId": self._bot_user_id or "",
                    "createdAt": comment_data.get("createdAt", ""),
                    "updatedAt": comment_data.get("updatedAt", ""),
                    "url": comment_data.get("url"),
                },
            ),
        )

    async def delete_message(self, thread_id: str, message_id: str) -> None:
        """Delete a message (delete a comment)."""
        await self._ensure_valid_token()

        await self._graphql_query(
            """
            mutation CommentDelete($id: String!) {
                commentDelete(id: $id) {
                    success
                }
            }
            """,
            {"id": message_id},
        )

    async def add_reaction(
        self,
        thread_id: str,
        message_id: str,
        emoji: EmojiValue | str,
    ) -> None:
        """Add a reaction to a comment."""
        await self._ensure_valid_token()
        emoji_str = self._resolve_emoji(emoji)

        await self._graphql_query(
            """
            mutation ReactionCreate($input: ReactionCreateInput!) {
                reactionCreate(input: $input) {
                    success
                }
            }
            """,
            {"input": {"commentId": message_id, "emoji": emoji_str}},
        )

    async def remove_reaction(
        self,
        thread_id: str,
        message_id: str,
        emoji: EmojiValue | str,
    ) -> None:
        """Remove a reaction from a comment (limited support)."""
        self._logger.warn("removeReaction is not fully supported on Linear - reaction ID lookup would be required")

    async def start_typing(self, thread_id: str, status: str | None = None) -> None:
        """Start typing indicator. Not supported by Linear."""
        pass

    async def fetch_messages(
        self,
        thread_id: str,
        options: FetchOptions | None = None,
    ) -> FetchResult:
        """Fetch messages from a thread."""
        await self._ensure_valid_token()
        decoded = self.decode_thread_id(thread_id)

        if options is None:
            options = FetchOptions()

        limit = options.limit if options.limit is not None else 50

        if decoded.comment_id:
            return await self._fetch_comment_thread(thread_id, decoded.issue_id, decoded.comment_id, limit)

        return await self._fetch_issue_comments(thread_id, decoded.issue_id, limit)

    async def _fetch_issue_comments(
        self,
        thread_id: str,
        issue_id: str,
        limit: int,
    ) -> FetchResult:
        """Fetch top-level comments on an issue."""
        result = await self._graphql_query(
            """
            query IssueComments($issueId: String!, $first: Int) {
                issue(id: $issueId) {
                    comments(first: $first) {
                        nodes {
                            id
                            body
                            createdAt
                            updatedAt
                            url
                            user {
                                id
                                displayName
                                name
                            }
                        }
                        pageInfo {
                            hasNextPage
                            endCursor
                        }
                    }
                }
            }
            """,
            {"issueId": issue_id, "first": limit},
        )

        comments = result.get("data", {}).get("issue", {}).get("comments", {})
        nodes = comments.get("nodes", [])
        page_info = comments.get("pageInfo", {})

        messages = [self._comment_node_to_message(node, thread_id, issue_id) for node in nodes]

        return FetchResult(
            messages=messages,
            next_cursor=page_info.get("endCursor") if page_info.get("hasNextPage") else None,
        )

    async def _fetch_comment_thread(
        self,
        thread_id: str,
        issue_id: str,
        comment_id: str,
        limit: int,
    ) -> FetchResult:
        """Fetch a comment thread (root comment + its children/replies)."""
        result = await self._graphql_query(
            """
            query CommentThread($commentId: String!, $first: Int) {
                comment(id: $commentId) {
                    id
                    body
                    createdAt
                    updatedAt
                    url
                    user {
                        id
                        displayName
                        name
                    }
                    children(first: $first) {
                        nodes {
                            id
                            body
                            createdAt
                            updatedAt
                            url
                            user {
                                id
                                displayName
                                name
                            }
                        }
                        pageInfo {
                            hasNextPage
                            endCursor
                        }
                    }
                }
            }
            """,
            {"commentId": comment_id, "first": limit},
        )

        comment = result.get("data", {}).get("comment")
        if not comment:
            return FetchResult(messages=[])

        # Root comment as first message
        messages = [self._comment_node_to_message(comment, thread_id, issue_id)]

        # Child comments
        children = comment.get("children", {})
        for node in children.get("nodes", []):
            messages.append(self._comment_node_to_message(node, thread_id, issue_id))

        page_info = children.get("pageInfo", {})

        return FetchResult(
            messages=messages,
            next_cursor=page_info.get("endCursor") if page_info.get("hasNextPage") else None,
        )

    def _comment_node_to_message(
        self,
        node: dict[str, Any],
        thread_id: str,
        issue_id: str,
    ) -> Message:
        """Convert a GraphQL comment node to a Message."""
        user = node.get("user") or {}
        user_id = user.get("id", "unknown")

        return Message(
            id=node.get("id", ""),
            thread_id=thread_id,
            text=node.get("body", ""),
            formatted=self._format_converter.to_ast(node.get("body", "")),
            raw=LinearRawMessage(
                comment={
                    "id": node.get("id", ""),
                    "body": node.get("body", ""),
                    "issueId": issue_id,
                    "userId": user_id,
                    "createdAt": node.get("createdAt", ""),
                    "updatedAt": node.get("updatedAt", ""),
                    "url": node.get("url", ""),
                },
            ),
            author=Author(
                user_id=user_id,
                user_name=user.get("displayName", "unknown"),
                full_name=user.get("name") or user.get("displayName", "unknown"),
                is_bot=False,
                is_me=user_id == self._bot_user_id,
            ),
            metadata=MessageMetadata(
                date_sent=_parse_iso(node["createdAt"]) if node.get("createdAt") else datetime.now(timezone.utc),
                edited=node.get("createdAt") != node.get("updatedAt"),
                edited_at=(
                    _parse_iso(node["updatedAt"])
                    if node.get("createdAt") != node.get("updatedAt") and node.get("updatedAt")
                    else None
                ),
            ),
            attachments=[],
        )

    async def fetch_thread(self, thread_id: str) -> ThreadInfo:
        """Fetch thread info for a Linear issue."""
        await self._ensure_valid_token()
        decoded = self.decode_thread_id(thread_id)

        result = await self._graphql_query(
            """
            query Issue($issueId: String!) {
                issue(id: $issueId) {
                    identifier
                    title
                    url
                }
            }
            """,
            {"issueId": decoded.issue_id},
        )

        issue = result.get("data", {}).get("issue", {})

        return ThreadInfo(
            id=thread_id,
            channel_id=decoded.issue_id,
            channel_name=f"{issue.get('identifier', '')}: {issue.get('title', '')}",
            is_dm=False,
            metadata={
                "issueId": decoded.issue_id,
                "issue_id": decoded.issue_id,  # snake_case alias for compatibility
                "identifier": issue.get("identifier"),
                "title": issue.get("title"),
                "url": issue.get("url"),
            },
        )

    def encode_thread_id(self, platform_data: LinearThreadId) -> str:
        """Encode a Linear thread ID.

        Formats:
        - Issue-level: linear:{issue_id}
        - Comment thread: linear:{issue_id}:c:{comment_id}
        """
        if platform_data.comment_id:
            return f"linear:{platform_data.issue_id}:c:{platform_data.comment_id}"
        return f"linear:{platform_data.issue_id}"

    def decode_thread_id(self, thread_id: str) -> LinearThreadId:
        """Decode a Linear thread ID."""
        if not thread_id.startswith("linear:"):
            raise ValidationError("linear", f"Invalid Linear thread ID: {thread_id}")

        without_prefix = thread_id[7:]
        if not without_prefix:
            raise ValidationError("linear", f"Invalid Linear thread ID format: {thread_id}")

        # Check for comment thread format: {issueId}:c:{commentId}
        match = COMMENT_THREAD_PATTERN.match(without_prefix)
        if match:
            return LinearThreadId(
                issue_id=match.group(1),
                comment_id=match.group(2),
            )

        # Issue-level format: {issueId}
        return LinearThreadId(issue_id=without_prefix)

    def channel_id_from_thread_id(self, thread_id: str) -> str:
        """Derive channel ID from a Linear thread ID."""
        decoded = self.decode_thread_id(thread_id)
        return f"linear:{decoded.issue_id}"

    def parse_message(self, raw: LinearRawMessage) -> Message:
        """Parse platform message format to normalized format.

        TypedDict `.get()` unions every value-type across camel/snake-case
        aliases, producing `object | str`. Cast the string fields we know
        are strings at runtime so downstream constructors (`Author`,
        `_parse_iso`) receive `str` instead of `object`.
        """
        comment = raw.get("comment", {})
        text = cast("str", comment.get("body", ""))
        user_id = cast("str", comment.get("userId") or comment.get("user_id", ""))

        created_at = cast("str", comment.get("createdAt") or comment.get("created_at", ""))
        updated_at = cast("str", comment.get("updatedAt") or comment.get("updated_at", ""))

        return Message(
            id=comment.get("id", ""),
            thread_id="",
            text=text,
            formatted=self._format_converter.to_ast(text),
            author=Author(
                user_id=user_id,
                user_name="unknown",
                full_name="unknown",
                is_bot=False,
                is_me=user_id == self._bot_user_id,
            ),
            metadata=MessageMetadata(
                date_sent=(_parse_iso(created_at) if created_at else datetime.now(timezone.utc)),
                edited=created_at != updated_at,
                edited_at=(_parse_iso(updated_at) if created_at != updated_at and updated_at else None),
            ),
            attachments=[],
            raw=raw,
        )

    def render_formatted(self, content: FormattedContent) -> str:
        """Render formatted content to Linear markdown."""
        return self._format_converter.from_ast(content)

    async def stream(
        self,
        thread_id: str,
        text_stream: Any,
        options: StreamOptions | None = None,
    ) -> RawMessage:
        """Stream responses by accumulating chunks and posting/editing a single comment.

        Linear does not support native streaming, so this accumulates the
        full text and posts (or edits) a single comment at the end.
        """
        accumulated = ""
        message_id: str | None = None

        async for chunk in text_stream:
            text = ""
            if isinstance(chunk, str):
                text = chunk
            elif isinstance(chunk, dict) and chunk.get("type") == "markdown_text":
                text = chunk.get("text", "")
            if not text:
                continue
            accumulated += text

        # Post the accumulated text as a single comment
        if accumulated:
            postable: AdapterPostableMessage = PostableRaw(raw=accumulated)
            result = await self.post_message(thread_id, postable)
            message_id = result.id

        return RawMessage(
            id=message_id or "",
            thread_id=thread_id,
            raw={"text": accumulated},
        )

    async def _get_http_session(self) -> Any:
        """Return the shared aiohttp session, creating it lazily if needed."""
        import aiohttp

        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession()
        return self._http_session

    async def disconnect(self) -> None:
        """Cleanup hook. Close the shared HTTP session."""
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
            self._http_session = None
        self._logger.debug("Linear adapter disconnecting")

    def _resolve_emoji(self, emoji: EmojiValue | str) -> str:
        """Resolve an emoji value to a unicode string."""
        emoji_name = emoji if isinstance(emoji, str) else emoji.name
        return EMOJI_MAPPING.get(emoji_name, emoji_name)

    # =========================================================================
    # GraphQL API helper
    # =========================================================================

    async def _graphql_query(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute a GraphQL query against the Linear API."""
        headers = {
            "Content-Type": "application/json",
            "Authorization": self._access_token or "",
        }

        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables

        session = await self._get_http_session()
        async with session.post(
            LINEAR_API_URL,
            headers=headers,
            json=payload,
        ) as response:
            if not response.ok:
                error_text = await response.text()
                raise NetworkError(
                    "linear",
                    f"Linear API error: {response.status} {error_text}",
                )
            return await response.json()

    # =========================================================================
    # Request/Response helpers (framework-agnostic)
    # =========================================================================

    @staticmethod
    async def _get_request_body(request: Any) -> str:
        """Extract the request body as a string."""
        # `hasattr` narrows `Any` → `object` (not awaitable); using
        # `getattr(..., None)` preserves `Any` for framework duck-typing.
        # Handle both callable and non-callable `request.text`. Gating
        # entry on callability would drop populated string attributes.
        text_attr = getattr(request, "text", None)
        if text_attr is not None:
            if callable(text_attr):
                result = text_attr()
                text_attr = await result if inspect.isawaitable(result) else result
            return text_attr.decode("utf-8") if isinstance(text_attr, (bytes, bytearray)) else str(text_attr)
        body = getattr(request, "body", None)
        if body is not None:
            if callable(body):
                body = body()
            # Some frameworks expose `body` as an async method; if calling it
            # produced a coroutine, await it before treating as bytes/str.
            if inspect.isawaitable(body):
                body = await body
            if hasattr(body, "read"):
                raw_result = body.read()
                raw = await raw_result if inspect.isawaitable(raw_result) else raw_result
                return raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
            return body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else str(body)
        data = getattr(request, "data", None)
        if data is not None:
            return data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else str(data)
        return ""

    def _get_header(self, request: Any, name: str) -> str | None:
        """Extract a header value from the request."""
        if hasattr(request, "headers"):
            headers = request.headers
            if isinstance(headers, dict):
                return headers.get(name) or headers.get(name.title())
            if hasattr(headers, "get"):
                return headers.get(name)
        return None

    def _make_response(self, body: str, status: int) -> Any:
        """Create a simple text response."""
        return {"body": body, "status": status, "headers": {"Content-Type": "text/plain"}}


def create_linear_adapter(config: LinearAdapterConfig | None = None) -> LinearAdapter:
    """Factory function to create a Linear adapter."""
    return LinearAdapter(config)
