"""GitHub adapter for chat SDK.

Supports both PR-level comments (Conversation tab) and review comment threads
(Files Changed tab - line-specific).

Python port of packages/adapter-github/src/index.ts.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import time
from collections.abc import AsyncIterable
from datetime import datetime, timezone
from typing import Any

from chat_sdk.adapters.github.cards import card_to_github_markdown
from chat_sdk.adapters.github.format_converter import GitHubFormatConverter
from chat_sdk.adapters.github.types import (
    GitHubAdapterConfig,
    GitHubIssueComment,
    GitHubRawMessage,
    GitHubReactionContent,
    GitHubReviewComment,
    GitHubThreadId,
    GitHubUser,
    IssueCommentWebhookPayload,
    PullRequestReviewCommentWebhookPayload,
)
from chat_sdk.emoji import convert_emoji_placeholders
from chat_sdk.logger import ConsoleLogger, Logger
from chat_sdk.shared.adapter_utils import extract_card
from chat_sdk.shared.errors import ValidationError
from chat_sdk.types import (
    AdapterPostableMessage,
    Author,
    ChannelInfo,
    ChatInstance,
    EmojiValue,
    FetchOptions,
    FetchResult,
    FormattedContent,
    ListThreadsOptions,
    ListThreadsResult,
    Message,
    MessageMetadata,
    RawMessage,
    StreamChunk,
    StreamOptions,
    ThreadInfo,
    ThreadSummary,
    WebhookOptions,
    _parse_iso,
)

REVIEW_COMMENT_THREAD_PATTERN = re.compile(r"^([^/]+)/([^:]+):(\d+):rc:(\d+)$")
PR_THREAD_PATTERN = re.compile(r"^([^/]+)/([^:]+):(\d+)$")

# GitHub reaction content types
EMOJI_TO_GITHUB_REACTION: dict[str, GitHubReactionContent] = {
    "thumbs_up": "+1",
    "+1": "+1",
    "thumbs_down": "-1",
    "-1": "-1",
    "laugh": "laugh",
    "smile": "laugh",
    "confused": "confused",
    "thinking": "confused",
    "heart": "heart",
    "love_eyes": "heart",
    "hooray": "hooray",
    "party": "hooray",
    "confetti": "hooray",
    "rocket": "rocket",
    "eyes": "eyes",
}


class GitHubAdapter:
    """GitHub adapter for chat SDK.

    Supports PAT auth, single-tenant GitHub App, and multi-tenant GitHub App modes.
    """

    def __init__(self, config: GitHubAdapterConfig | None = None) -> None:
        config = config or {}
        self._name = "github"

        webhook_secret = config.get("webhook_secret") or os.environ.get("GITHUB_WEBHOOK_SECRET")
        if not webhook_secret:
            raise ValidationError(
                "github",
                "webhookSecret is required. Set GITHUB_WEBHOOK_SECRET or provide it in config.",
            )
        self._webhook_secret = webhook_secret
        self._logger: Logger = config.get("logger") or ConsoleLogger("info").child("github")
        self._user_name = config.get("user_name") or os.environ.get("GITHUB_BOT_USERNAME") or "github-bot"
        self._bot_user_id: int | None = config.get("bot_user_id")
        self._chat: ChatInstance | None = None
        self._format_converter = GitHubFormatConverter()

        # Auth configuration
        self._auth_token: str | None = None
        self._app_credentials: dict[str, str] | None = None
        self._installation_id: int | None = None
        self._installation_token_cache: dict[int, tuple[str, float]] = {}
        self._token_lock = asyncio.Lock()

        # Shared aiohttp session for connection pooling
        self._http_session: Any | None = None

        has_explicit_auth = bool(config.get("token") or config.get("app_id") or config.get("private_key"))

        if config.get("token"):
            self._auth_token = config["token"]
        elif config.get("app_id") and config.get("private_key"):
            if config.get("installation_id"):
                self._app_credentials = {
                    "app_id": config["app_id"],
                    "private_key": config["private_key"],
                }
                self._installation_id = config["installation_id"]
            else:
                self._app_credentials = {
                    "app_id": config["app_id"],
                    "private_key": config["private_key"],
                }
                self._logger.info(
                    "GitHub adapter initialized in multi-tenant mode (installation ID will be extracted from webhooks)"
                )
        elif has_explicit_auth:
            raise ValidationError(
                "github",
                "Authentication is required. Set GITHUB_TOKEN or GITHUB_APP_ID/GITHUB_PRIVATE_KEY.",
            )
        else:
            # Auto-detect from env vars
            token = os.environ.get("GITHUB_TOKEN")
            if token:
                self._auth_token = token
            else:
                app_id = os.environ.get("GITHUB_APP_ID")
                private_key = os.environ.get("GITHUB_PRIVATE_KEY")
                if app_id and private_key:
                    installation_id_raw = os.environ.get("GITHUB_INSTALLATION_ID")
                    if installation_id_raw:
                        self._app_credentials = {"app_id": app_id, "private_key": private_key}
                        self._installation_id = int(installation_id_raw)
                    else:
                        self._app_credentials = {"app_id": app_id, "private_key": private_key}
                        self._logger.info(
                            "GitHub adapter initialized in multi-tenant mode "
                            "(installation ID will be extracted from webhooks)"
                        )
                else:
                    raise ValidationError(
                        "github",
                        "Authentication is required. Set GITHUB_TOKEN or GITHUB_APP_ID/GITHUB_PRIVATE_KEY.",
                    )

    @property
    def name(self) -> str:
        return self._name

    @property
    def user_name(self) -> str:
        return self._user_name

    @property
    def bot_user_id(self) -> str | None:
        return str(self._bot_user_id) if self._bot_user_id else None

    @property
    def lock_scope(self) -> str | None:
        return None

    @property
    def persist_message_history(self) -> bool | None:
        return None

    @property
    def is_multi_tenant(self) -> bool:
        return self._app_credentials is not None and self._auth_token is None and self._installation_id is None

    async def initialize(self, chat: ChatInstance) -> None:
        """Initialize the adapter."""
        self._chat = chat

        if not self._bot_user_id and self._auth_token:
            try:
                user_data = await self._github_api_request("GET", "/user")
                self._bot_user_id = user_data.get("id")
                self._logger.info(
                    "GitHub auth completed",
                    {
                        "botUserId": self._bot_user_id,
                        "login": user_data.get("login"),
                    },
                )
            except Exception as error:
                self._logger.warn("Could not fetch bot user ID", {"error": str(error)})

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

    async def handle_webhook(self, request: Any, options: WebhookOptions | None = None) -> Any:
        """Handle incoming webhook from GitHub."""
        body = await self._get_request_body(request)
        self._logger.debug("GitHub webhook raw body", {"body": body[:500]})

        # Verify request signature
        signature = self._get_header(request, "x-hub-signature-256")
        if not self._verify_signature(body, signature):
            return self._make_response("Invalid signature", 401)

        event_type = self._get_header(request, "x-github-event")
        self._logger.debug("GitHub webhook event type", {"eventType": event_type})

        if event_type == "ping":
            self._logger.info("GitHub webhook ping received")
            return self._make_response("pong", 200)

        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            self._logger.error(
                "GitHub webhook invalid JSON",
                {
                    "contentType": self._get_header(request, "content-type"),
                    "bodyPreview": body[:200],
                },
            )
            return self._make_response(
                "Invalid JSON. Make sure webhook Content-Type is set to application/json",
                400,
            )

        # Store installation ID for multi-tenant mode
        installation_id = (payload.get("installation") or {}).get("id")
        if installation_id and self.is_multi_tenant:
            repo = payload.get("repository", {})
            owner_login = repo.get("owner", {}).get("login", "")
            repo_name = repo.get("name", "")
            if owner_login and repo_name:
                await self._store_installation_id(owner_login, repo_name, installation_id)

        if event_type == "issue_comment":
            if payload.get("action") == "created" and payload.get("issue", {}).get("pull_request"):
                self._handle_issue_comment(payload, installation_id, options)
        elif event_type == "pull_request_review_comment" and payload.get("action") == "created":
            self._handle_review_comment(payload, installation_id, options)

        return self._make_response("ok", 200)

    def _verify_signature(self, body: str, signature: str | None) -> bool:
        """Verify GitHub webhook signature using HMAC-SHA256."""
        if not signature:
            return False
        expected = (
            "sha256="
            + hmac.new(
                self._webhook_secret.encode("utf-8"),
                body.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
        )
        try:
            return hmac.compare_digest(signature, expected)
        except Exception:
            return False

    def _handle_issue_comment(
        self,
        payload: IssueCommentWebhookPayload,
        installation_id: int | None,
        options: WebhookOptions | None = None,
    ) -> None:
        """Handle issue_comment webhook (PR-level comments)."""
        if not self._chat:
            self._logger.warn("Chat instance not initialized, ignoring comment")
            return

        comment = payload["comment"]
        issue = payload["issue"]
        repository = payload["repository"]
        sender = payload["sender"]

        thread_id = self.encode_thread_id(
            GitHubThreadId(
                owner=repository["owner"]["login"],
                repo=repository["name"],
                pr_number=issue["number"],
            )
        )

        message = self._parse_issue_comment(comment, repository, issue["number"], thread_id)

        if sender.get("id") == self._bot_user_id:
            self._logger.debug("Ignoring message from self", {"messageId": comment["id"]})
            return

        self._chat.process_message(self, thread_id, message, options)

    def _handle_review_comment(
        self,
        payload: PullRequestReviewCommentWebhookPayload,
        installation_id: int | None,
        options: WebhookOptions | None = None,
    ) -> None:
        """Handle pull_request_review_comment webhook."""
        if not self._chat:
            self._logger.warn("Chat instance not initialized, ignoring comment")
            return

        comment = payload["comment"]
        pull_request = payload["pull_request"]
        repository = payload["repository"]
        sender = payload["sender"]

        root_comment_id = comment.get("in_reply_to_id") or comment["id"]

        thread_id = self.encode_thread_id(
            GitHubThreadId(
                owner=repository["owner"]["login"],
                repo=repository["name"],
                pr_number=pull_request["number"],
                review_comment_id=root_comment_id,
            )
        )

        message = self._parse_review_comment(comment, repository, pull_request["number"], thread_id)

        if sender.get("id") == self._bot_user_id:
            self._logger.debug("Ignoring message from self", {"messageId": comment["id"]})
            return

        self._chat.process_message(self, thread_id, message, options)

    def _parse_issue_comment(
        self,
        comment: GitHubIssueComment,
        repository: dict[str, Any],
        pr_number: int,
        thread_id: str,
    ) -> Message:
        """Parse an issue comment into a normalized Message."""
        author = self._parse_author(comment["user"])
        body_text = comment.get("body", "")

        created_at = comment.get("created_at", "")
        updated_at = comment.get("updated_at", "")
        edited = created_at != updated_at

        return Message(
            id=str(comment["id"]),
            thread_id=thread_id,
            text=self._format_converter.extract_plain_text(body_text),
            formatted=self._format_converter.to_ast(body_text),
            raw={
                "type": "issue_comment",
                "comment": comment,
                "repository": {
                    "id": 0,
                    "name": repository.get("name", ""),
                    "full_name": f"{repository.get('owner', {}).get('login', '')}/{repository.get('name', '')}",
                    "owner": repository.get("owner", {}),
                },
                "pr_number": pr_number,
            },
            author=author,
            metadata=MessageMetadata(
                date_sent=_parse_iso(created_at) if created_at else datetime.now(tz=timezone.utc),
                edited=edited,
                edited_at=_parse_iso(updated_at) if edited and updated_at else None,
            ),
            attachments=[],
        )

    def _parse_review_comment(
        self,
        comment: GitHubReviewComment,
        repository: dict[str, Any],
        pr_number: int,
        thread_id: str,
    ) -> Message:
        """Parse a review comment into a normalized Message."""
        author = self._parse_author(comment["user"])
        body_text = comment.get("body", "")

        created_at = comment.get("created_at", "")
        updated_at = comment.get("updated_at", "")
        edited = created_at != updated_at

        return Message(
            id=str(comment["id"]),
            thread_id=thread_id,
            text=self._format_converter.extract_plain_text(body_text),
            formatted=self._format_converter.to_ast(body_text),
            raw={
                "type": "review_comment",
                "comment": comment,
                "repository": {
                    "id": 0,
                    "name": repository.get("name", ""),
                    "full_name": f"{repository.get('owner', {}).get('login', '')}/{repository.get('name', '')}",
                    "owner": repository.get("owner", {}),
                },
                "pr_number": pr_number,
            },
            author=author,
            metadata=MessageMetadata(
                date_sent=_parse_iso(created_at) if created_at else datetime.now(tz=timezone.utc),
                edited=edited,
                edited_at=_parse_iso(updated_at) if edited and updated_at else None,
            ),
            attachments=[],
        )

    def _parse_author(self, user: GitHubUser) -> Author:
        """Parse a GitHub user into an Author."""
        return Author(
            user_id=str(user.get("id", 0)),
            user_name=user.get("login", ""),
            full_name=user.get("login", ""),
            is_bot=user.get("type") == "Bot",
            is_me=user.get("id") == self._bot_user_id,
        )

    async def post_message(self, thread_id: str, message: AdapterPostableMessage) -> RawMessage:
        """Post a message to a thread."""
        decoded = self.decode_thread_id(thread_id)

        # Render message to GitHub markdown
        card = extract_card(message)
        body = card_to_github_markdown(card) if card else self._format_converter.render_postable(message)

        body = convert_emoji_placeholders(body, "github")

        if decoded.review_comment_id:
            # Review comment thread - reply with in_reply_to
            comment = await self._github_api_request(
                "POST",
                f"/repos/{decoded.owner}/{decoded.repo}/pulls/{decoded.pr_number}/comments/{decoded.review_comment_id}/replies",
                {"body": body},
            )
        else:
            # PR-level thread - issue comment
            comment = await self._github_api_request(
                "POST",
                f"/repos/{decoded.owner}/{decoded.repo}/issues/{decoded.pr_number}/comments",
                {"body": body},
            )

        return RawMessage(
            id=str(comment["id"]),
            thread_id=thread_id,
            raw={
                "type": "review_comment" if decoded.review_comment_id else "issue_comment",
                "comment": comment,
                "repository": {
                    "id": 0,
                    "name": decoded.repo,
                    "full_name": f"{decoded.owner}/{decoded.repo}",
                    "owner": {"id": 0, "login": decoded.owner, "type": "User"},
                },
                "pr_number": decoded.pr_number,
            },
        )

    async def edit_message(self, thread_id: str, message_id: str, message: AdapterPostableMessage) -> RawMessage:
        """Edit an existing message."""
        decoded = self.decode_thread_id(thread_id)
        comment_id = int(message_id)

        card = extract_card(message)
        body = card_to_github_markdown(card) if card else self._format_converter.render_postable(message)
        body = convert_emoji_placeholders(body, "github")

        if decoded.review_comment_id:
            comment = await self._github_api_request(
                "PATCH",
                f"/repos/{decoded.owner}/{decoded.repo}/pulls/comments/{comment_id}",
                {"body": body},
            )
        else:
            comment = await self._github_api_request(
                "PATCH",
                f"/repos/{decoded.owner}/{decoded.repo}/issues/comments/{comment_id}",
                {"body": body},
            )

        return RawMessage(
            id=str(comment["id"]),
            thread_id=thread_id,
            raw={
                "type": "review_comment" if decoded.review_comment_id else "issue_comment",
                "comment": comment,
                "repository": {
                    "id": 0,
                    "name": decoded.repo,
                    "full_name": f"{decoded.owner}/{decoded.repo}",
                    "owner": {"id": 0, "login": decoded.owner, "type": "User"},
                },
                "pr_number": decoded.pr_number,
            },
        )

    async def stream(
        self,
        thread_id: str,
        text_stream: AsyncIterable[str | StreamChunk],
        options: StreamOptions | None = None,
    ) -> RawMessage:
        """Stream by accumulating text and posting once."""
        text = ""
        async for chunk in text_stream:
            if isinstance(chunk, str):
                text += chunk
            elif hasattr(chunk, "type") and chunk.type == "markdown_text":
                text += chunk.text
        return await self.post_message(thread_id, {"markdown": text})

    async def delete_message(self, thread_id: str, message_id: str) -> None:
        """Delete a message."""
        decoded = self.decode_thread_id(thread_id)
        comment_id = int(message_id)

        if decoded.review_comment_id:
            await self._github_api_request(
                "DELETE",
                f"/repos/{decoded.owner}/{decoded.repo}/pulls/comments/{comment_id}",
            )
        else:
            await self._github_api_request(
                "DELETE",
                f"/repos/{decoded.owner}/{decoded.repo}/issues/comments/{comment_id}",
            )

    async def add_reaction(self, thread_id: str, message_id: str, emoji: EmojiValue | str) -> None:
        """Add a reaction to a message."""
        decoded = self.decode_thread_id(thread_id)
        comment_id = int(message_id)
        content = self._emoji_to_github_reaction(emoji)

        if decoded.review_comment_id:
            await self._github_api_request(
                "POST",
                f"/repos/{decoded.owner}/{decoded.repo}/pulls/comments/{comment_id}/reactions",
                {"content": content},
            )
        else:
            await self._github_api_request(
                "POST",
                f"/repos/{decoded.owner}/{decoded.repo}/issues/comments/{comment_id}/reactions",
                {"content": content},
            )

    async def remove_reaction(self, thread_id: str, message_id: str, emoji: EmojiValue | str) -> None:
        """Remove a reaction from a message."""
        if not self._bot_user_id and self._auth_token:
            try:
                user_data = await self._github_api_request("GET", "/user")
                self._bot_user_id = user_data.get("id")
            except Exception:
                self._logger.warn("Could not detect bot user ID for reaction removal")

        decoded = self.decode_thread_id(thread_id)
        comment_id = int(message_id)
        content = self._emoji_to_github_reaction(emoji)

        # List reactions to find the one to delete
        if decoded.review_comment_id:
            reactions = await self._github_api_request(
                "GET",
                f"/repos/{decoded.owner}/{decoded.repo}/pulls/comments/{comment_id}/reactions",
            )
        else:
            reactions = await self._github_api_request(
                "GET",
                f"/repos/{decoded.owner}/{decoded.repo}/issues/comments/{comment_id}/reactions",
            )

        # Find the bot's reaction with matching content
        reaction = next(
            (r for r in reactions if r.get("content") == content and r.get("user", {}).get("id") == self._bot_user_id),
            None,
        )

        if reaction:
            if decoded.review_comment_id:
                await self._github_api_request(
                    "DELETE",
                    f"/repos/{decoded.owner}/{decoded.repo}/pulls/comments/{comment_id}/reactions/{reaction['id']}",
                )
            else:
                await self._github_api_request(
                    "DELETE",
                    f"/repos/{decoded.owner}/{decoded.repo}/issues/comments/{comment_id}/reactions/{reaction['id']}",
                )

    def _emoji_to_github_reaction(self, emoji: EmojiValue | str) -> str:
        """Convert SDK emoji to GitHub reaction content."""
        emoji_name = emoji if isinstance(emoji, str) else emoji.name
        return EMOJI_TO_GITHUB_REACTION.get(emoji_name, "+1")

    async def start_typing(self, thread_id: str, status: str | None = None) -> None:
        """No-op for GitHub."""
        pass

    async def fetch_messages(self, thread_id: str, options: FetchOptions | None = None) -> FetchResult:
        """Fetch messages from a thread."""
        decoded = self.decode_thread_id(thread_id)
        _raw_limit = options.limit if options else None
        limit = _raw_limit if _raw_limit is not None else 100
        direction = (options.direction if options else None) or "backward"

        if decoded.review_comment_id:
            all_comments = await self._github_api_request(
                "GET",
                f"/repos/{decoded.owner}/{decoded.repo}/pulls/{decoded.pr_number}/comments?per_page=100",
            )
            thread_comments = [
                c
                for c in all_comments
                if c["id"] == decoded.review_comment_id or c.get("in_reply_to_id") == decoded.review_comment_id
            ]
            messages = [
                self._parse_review_comment(
                    c,
                    {"owner": {"id": 0, "login": decoded.owner, "type": "User"}, "name": decoded.repo},
                    decoded.pr_number,
                    thread_id,
                )
                for c in thread_comments
            ]
        else:
            comments = await self._github_api_request(
                "GET",
                f"/repos/{decoded.owner}/{decoded.repo}/issues/{decoded.pr_number}/comments?per_page={limit}",
            )
            messages = [
                self._parse_issue_comment(
                    c,
                    {"owner": {"id": 0, "login": decoded.owner, "type": "User"}, "name": decoded.repo},
                    decoded.pr_number,
                    thread_id,
                )
                for c in comments
            ]

        messages.sort(key=lambda m: m.metadata.date_sent)

        if direction == "backward" and len(messages) > limit:
            messages = messages[-limit:]
        elif direction == "forward" and len(messages) > limit:
            messages = messages[:limit]

        return FetchResult(messages=messages)

    async def fetch_thread(self, thread_id: str) -> ThreadInfo:
        """Fetch thread metadata."""
        decoded = self.decode_thread_id(thread_id)

        pr = await self._github_api_request(
            "GET",
            f"/repos/{decoded.owner}/{decoded.repo}/pulls/{decoded.pr_number}",
        )

        return ThreadInfo(
            id=thread_id,
            channel_id=f"github:{decoded.owner}/{decoded.repo}",
            channel_name=f"{decoded.repo} #{decoded.pr_number}",
            is_dm=False,
            metadata={
                "owner": decoded.owner,
                "repo": decoded.repo,
                "pr_number": decoded.pr_number,
                "pr_title": pr.get("title"),
                "pr_state": pr.get("state"),
                "review_comment_id": decoded.review_comment_id,
            },
        )

    def encode_thread_id(self, platform_data: GitHubThreadId) -> str:
        """Encode platform data into a thread ID string."""
        if platform_data.review_comment_id:
            return (
                f"github:{platform_data.owner}/{platform_data.repo}"
                f":{platform_data.pr_number}:rc:{platform_data.review_comment_id}"
            )
        return f"github:{platform_data.owner}/{platform_data.repo}:{platform_data.pr_number}"

    def decode_thread_id(self, thread_id: str) -> GitHubThreadId:
        """Decode thread ID string back to platform data."""
        if not thread_id.startswith("github:"):
            raise ValidationError("github", f"Invalid GitHub thread ID: {thread_id}")

        without_prefix = thread_id[7:]

        rc_match = REVIEW_COMMENT_THREAD_PATTERN.match(without_prefix)
        if rc_match:
            return GitHubThreadId(
                owner=rc_match.group(1),
                repo=rc_match.group(2),
                pr_number=int(rc_match.group(3)),
                review_comment_id=int(rc_match.group(4)),
            )

        pr_match = PR_THREAD_PATTERN.match(without_prefix)
        if pr_match:
            return GitHubThreadId(
                owner=pr_match.group(1),
                repo=pr_match.group(2),
                pr_number=int(pr_match.group(3)),
            )

        raise ValidationError("github", f"Invalid GitHub thread ID format: {thread_id}")

    def channel_id_from_thread_id(self, thread_id: str) -> str:
        """Derive channel ID from a GitHub thread ID."""
        decoded = self.decode_thread_id(thread_id)
        return f"github:{decoded.owner}/{decoded.repo}"

    async def list_threads(
        self,
        channel_id: str,
        options: ListThreadsOptions | None = None,
    ) -> ListThreadsResult:
        """List threads (PRs) in a GitHub repository."""
        if not channel_id.startswith("github:"):
            raise ValidationError("github", f"Invalid GitHub channel ID: {channel_id}")

        without_prefix = channel_id[7:]
        slash_idx = without_prefix.index("/")
        owner = without_prefix[:slash_idx]
        repo = without_prefix[slash_idx + 1 :]

        _raw_limit = options.limit if options else None
        limit = _raw_limit if _raw_limit is not None else 30
        page = int(options.cursor) if options and options.cursor else 1

        pulls = await self._github_api_request(
            "GET",
            f"/repos/{owner}/{repo}/pulls?state=open&sort=updated&direction=desc&per_page={limit}&page={page}",
        )

        threads = []
        for pr in pulls:
            tid = self.encode_thread_id(GitHubThreadId(owner=owner, repo=repo, pr_number=pr["number"]))
            pr_user = pr.get("user", {})
            root_message = Message(
                id=str(pr["number"]),
                thread_id=tid,
                text=pr.get("title", ""),
                formatted=self._format_converter.to_ast(pr.get("title", "")),
                raw={
                    "type": "issue_comment",
                    "comment": pr,
                    "repository": {
                        "id": 0,
                        "name": repo,
                        "full_name": f"{owner}/{repo}",
                        "owner": {"id": 0, "login": owner, "type": "User"},
                    },
                    "pr_number": pr["number"],
                },
                author=self._parse_author(pr_user),
                metadata=MessageMetadata(
                    date_sent=_parse_iso(pr.get("created_at", "").replace("Z", "+00:00"))
                    if pr.get("created_at")
                    else datetime.now(tz=timezone.utc),
                    edited=pr.get("created_at") != pr.get("updated_at"),
                ),
            )
            threads.append(
                ThreadSummary(
                    id=tid,
                    root_message=root_message,
                    last_reply_at=_parse_iso(pr.get("updated_at", "").replace("Z", "+00:00"))
                    if pr.get("updated_at")
                    else None,
                )
            )

        next_cursor = str(page + 1) if len(pulls) == limit else None
        return ListThreadsResult(threads=threads, next_cursor=next_cursor)

    async def fetch_channel_info(self, channel_id: str) -> ChannelInfo:
        """Fetch GitHub repository info as channel metadata."""
        if not channel_id.startswith("github:"):
            raise ValidationError("github", f"Invalid GitHub channel ID: {channel_id}")

        without_prefix = channel_id[7:]
        slash_idx = without_prefix.index("/")
        owner = without_prefix[:slash_idx]
        repo = without_prefix[slash_idx + 1 :]

        repo_data = await self._github_api_request("GET", f"/repos/{owner}/{repo}")

        return ChannelInfo(
            id=channel_id,
            name=repo_data.get("full_name"),
            is_dm=False,
            metadata={
                "owner": owner,
                "repo": repo,
                "description": repo_data.get("description"),
                "visibility": repo_data.get("visibility"),
                "default_branch": repo_data.get("default_branch"),
                "open_issues_count": repo_data.get("open_issues_count"),
            },
        )

    def parse_message(self, raw: GitHubRawMessage) -> Message:
        """Parse a raw message into normalized format."""
        if raw.get("type") == "issue_comment":
            thread_id = self.encode_thread_id(
                GitHubThreadId(
                    owner=raw["repository"]["owner"]["login"],
                    repo=raw["repository"]["name"],
                    pr_number=raw["pr_number"],
                )
            )
            return self._parse_issue_comment(raw["comment"], raw["repository"], raw["pr_number"], thread_id)
        else:
            root_comment_id = raw["comment"].get("in_reply_to_id") or raw["comment"]["id"]
            thread_id = self.encode_thread_id(
                GitHubThreadId(
                    owner=raw["repository"]["owner"]["login"],
                    repo=raw["repository"]["name"],
                    pr_number=raw["pr_number"],
                    review_comment_id=root_comment_id,
                )
            )
            return self._parse_review_comment(raw["comment"], raw["repository"], raw["pr_number"], thread_id)

    def render_formatted(self, content: FormattedContent) -> str:
        """Render formatted content to GitHub markdown."""
        return self._format_converter.from_ast(content)

    # =========================================================================
    # Private helpers
    # =========================================================================

    def _generate_app_jwt(self) -> str:
        """Create an RS256 JWT for GitHub App authentication.

        The JWT is used to exchange for an installation access token.
        Uses PyJWT (``import jwt``), lazily imported.
        """
        import jwt  # PyJWT

        if not self._app_credentials:
            raise RuntimeError("App credentials not configured")

        now = int(time.time())
        payload = {
            "iss": self._app_credentials["app_id"],
            "iat": now - 60,  # 60 seconds in the past to allow clock drift
            "exp": now + 600,  # 10 minute maximum
        }
        return jwt.encode(payload, self._app_credentials["private_key"], algorithm="RS256")

    async def _get_installation_token(self, installation_id: int) -> str:
        """Exchange a GitHub App JWT for an installation access token.

        Caches the token until 60 seconds before expiry.  Returns the
        cached token when still valid.
        """

        # Purge expired entries and enforce hard size limit
        now = time.time()
        expired_ids = [iid for iid, (_, exp) in self._installation_token_cache.items() if now >= exp]
        for iid in expired_ids:
            del self._installation_token_cache[iid]
        if len(self._installation_token_cache) > 100:
            keys = list(self._installation_token_cache.keys())
            for k in keys[: len(keys) - 100]:
                del self._installation_token_cache[k]

        # Check cache
        cached = self._installation_token_cache.get(installation_id)
        if cached:
            token, expires_at = cached
            if time.time() < expires_at - 60:  # Refresh 60s before expiry
                return token

        async with self._token_lock:
            # Double-check after acquiring lock to avoid redundant refreshes
            cached = self._installation_token_cache.get(installation_id)
            if cached:
                token, expires_at = cached
                if time.time() < expires_at - 60:
                    return token

            app_jwt = self._generate_app_jwt()
            url = f"https://api.github.com/app/installations/{installation_id}/access_tokens"
            headers = {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {app_jwt}",
                "X-GitHub-Api-Version": "2022-11-28",
            }

            session = await self._get_http_session()
            async with session.post(url, headers=headers) as response:
                if response.status >= 400:
                    error_body = await response.text()
                    raise RuntimeError(f"GitHub App token exchange failed: {response.status} {error_body}")
                data = await response.json()

            token = data["token"]
            # Parse ISO-8601 expiry; default 1h if absent
            expires_at_str = data.get("expires_at", "")
            expires_at = _parse_iso(expires_at_str).timestamp() if expires_at_str else time.time() + 3600

            self._installation_token_cache[installation_id] = (token, expires_at)
            self._logger.debug(
                "Obtained installation token",
                {"installationId": installation_id, "expiresAt": expires_at_str},
            )
            return token

    async def _github_api_request(self, method: str, path: str, body: Any = None) -> Any:
        """Make a request to the GitHub API.

        Supports PAT auth (``_auth_token``) and GitHub App auth
        (``_app_credentials`` with JWT -> installation token exchange).
        """
        auth_token = self._auth_token

        # GitHub App auth: exchange JWT for installation token
        if not auth_token and self._app_credentials:
            installation_id = self._installation_id
            if not installation_id:
                raise RuntimeError(
                    "Installation ID required for GitHub App authentication. "
                    "This usually means you're trying to make an API call outside of a webhook context. "
                    "For proactive messages, use thread IDs from previous webhook interactions."
                )
            auth_token = await self._get_installation_token(installation_id)

        headers: dict[str, str] = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"

        url = f"https://api.github.com{path}" if path.startswith("/") else path

        session = await self._get_http_session()
        kwargs: dict[str, Any] = {"headers": headers}
        if body is not None:
            kwargs["json"] = body

        async with session.request(method, url, **kwargs) as response:
            if response.status == 204:
                return None
            if response.status >= 400:
                error_body = await response.text()
                self._logger.error(
                    "GitHub API error",
                    {
                        "status": response.status,
                        "body": error_body,
                        "path": path,
                    },
                )
                raise RuntimeError(f"GitHub API error: {response.status} {error_body}")

            return await response.json()

    async def _store_installation_id(self, owner: str, repo: str, installation_id: int) -> None:
        """Store the installation ID for a repository (multi-tenant mode)."""
        if not (self._chat and self.is_multi_tenant):
            return
        key = f"github:install:{owner}/{repo}"
        await self._chat.get_state().set(key, installation_id)
        self._logger.debug("Stored installation ID", {"owner": owner, "repo": repo, "installationId": installation_id})

    async def _get_installation_id(self, owner: str, repo: str) -> int | None:
        """Get the installation ID for a repository (multi-tenant mode)."""
        if not (self._chat and self.is_multi_tenant):
            return None
        key = f"github:install:{owner}/{repo}"
        return await self._chat.get_state().get(key)

    @staticmethod
    async def _get_request_body(request: Any) -> str:
        """Extract body text from a request object."""
        if hasattr(request, "text") and callable(request.text):
            return await request.text()
        if hasattr(request, "body"):
            body = request.body
            return body.decode("utf-8") if isinstance(body, bytes) else str(body)
        return ""

    @staticmethod
    def _get_header(request: Any, name: str) -> str | None:
        """Get a header value from a request object."""
        if hasattr(request, "headers"):
            headers = request.headers
            if isinstance(headers, dict):
                for k, v in headers.items():
                    if k.lower() == name.lower():
                        return v
                return None
            return headers.get(name)
        return None

    @staticmethod
    def _make_response(body: str, status: int) -> dict[str, Any]:
        """Create a response dict."""
        return {"body": body, "status": status}


def create_github_adapter(config: GitHubAdapterConfig | None = None) -> GitHubAdapter:
    """Factory function to create a GitHub adapter."""
    return GitHubAdapter(config)
