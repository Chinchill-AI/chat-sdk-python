"""Type definitions for the GitHub adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypedDict, Union

from chat_sdk.logger import Logger

# =============================================================================
# Configuration
# =============================================================================


class GitHubAdapterBaseConfig(TypedDict, total=False):
    """Base configuration options shared by all auth methods.

    Attributes:
        bot_user_id: Bot's GitHub user ID (numeric). Used for self-message
            detection. If not provided, will be fetched on first API call.
        logger: Logger instance for error reporting. Defaults to ConsoleLogger.
        user_name: Bot username (e.g., "my-bot" or "my-bot[bot]" for GitHub Apps).
            Used for @-mention detection.
            Defaults to GITHUB_BOT_USERNAME env var or "github-bot".
        webhook_secret: Webhook secret for HMAC-SHA256 verification.
            Set this in your GitHub webhook settings.
            Defaults to GITHUB_WEBHOOK_SECRET env var.
    """

    bot_user_id: int
    logger: Logger
    user_name: str
    webhook_secret: str


class GitHubAdapterPATConfig(GitHubAdapterBaseConfig, total=False):
    """Configuration using a Personal Access Token (PAT).

    Simpler setup, suitable for personal bots or testing.

    Attributes:
        token: Personal Access Token with appropriate scopes (repo, write:discussion).
    """

    token: str  # required in practice


class GitHubAdapterAppConfig(GitHubAdapterBaseConfig, total=False):
    """Configuration using a GitHub App with a fixed installation.

    Use this when your bot is only installed on a single org/repo.

    Attributes:
        app_id: GitHub App ID.
        installation_id: Installation ID for the app (for single-tenant apps).
        private_key: GitHub App private key (PEM format).
    """

    app_id: str
    installation_id: int
    private_key: str


class GitHubAdapterMultiTenantAppConfig(GitHubAdapterBaseConfig, total=False):
    """Configuration using a GitHub App for multi-tenant (public) apps.

    The installation ID is automatically extracted from each webhook payload.
    Use this when your bot can be installed by anyone.

    Attributes:
        app_id: GitHub App ID.
        private_key: GitHub App private key (PEM format).
    """

    app_id: str
    private_key: str


class GitHubAdapterAutoConfig(GitHubAdapterBaseConfig, total=False):
    """Configuration with no auth fields - will auto-detect from env vars."""


# Union of all configuration types
GitHubAdapterConfig = Union[
    GitHubAdapterPATConfig,
    GitHubAdapterAppConfig,
    GitHubAdapterMultiTenantAppConfig,
    GitHubAdapterAutoConfig,
]

# =============================================================================
# Thread ID
# =============================================================================


@dataclass
class GitHubThreadId:
    """Decoded thread ID for GitHub.

    Thread types:
    - PR-level: Comments in the "Conversation" tab (issue_comment API)
    - Review comment: Line-specific comments in "Files changed" tab
      (pull request review comment API)
    """

    owner: str
    """Repository owner (user or organization)."""

    pr_number: int
    """Pull request number."""

    repo: str
    """Repository name."""

    review_comment_id: int | None = None
    """Root review comment ID for line-specific threads.

    If present, this is a review comment thread.
    If absent, this is a PR-level (issue comment) thread.
    """


# =============================================================================
# Webhook Payloads
# =============================================================================


class GitHubUser(TypedDict, total=False):
    """GitHub user object (simplified)."""

    avatar_url: str
    id: int  # required
    login: str  # required
    type: str  # "User" | "Bot" | "Organization"


class GitHubRepository(TypedDict, total=False):
    """GitHub repository object (simplified)."""

    full_name: str
    id: int
    name: str
    owner: GitHubUser


class GitHubPullRequest(TypedDict, total=False):
    """GitHub pull request object (simplified)."""

    body: str | None
    html_url: str
    id: int
    number: int
    state: str  # "open" | "closed"
    title: str
    user: GitHubUser


class GitHubReactions(TypedDict, total=False):
    """Reactions summary on a GitHub comment."""

    url: str
    total_count: int
    plus_one: int  # "+1" in JSON
    minus_one: int  # "-1" in JSON
    laugh: int
    hooray: int
    confused: int
    heart: int
    rocket: int
    eyes: int


class GitHubIssueComment(TypedDict, total=False):
    """GitHub issue comment (PR-level comment in Conversation tab)."""

    body: str
    created_at: str
    html_url: str
    id: int
    reactions: GitHubReactions
    updated_at: str
    user: GitHubUser


class GitHubReviewComment(TypedDict, total=False):
    """GitHub pull request review comment (line-specific comment in Files Changed tab)."""

    body: str
    commit_id: str
    """The commit SHA the comment is associated with."""

    created_at: str
    diff_hunk: str
    """The diff hunk the comment applies to."""

    html_url: str
    id: int

    in_reply_to_id: int
    """The ID of the comment this is a reply to.

    If present, this is a reply in an existing thread.
    If absent, this is the root of a new thread.
    """

    line: int
    """Line number in the diff."""

    original_commit_id: str
    """The original commit SHA (for outdated comments)."""

    original_line: int
    """Original line number."""

    path: str
    """Path to the file being commented on."""

    reactions: GitHubReactions

    side: str
    """Side of the diff ("LEFT" or "RIGHT")."""

    start_line: int | None
    """Start line for multi-line comments."""

    start_side: str | None
    """Start side for multi-line comments ("LEFT" or "RIGHT" or None)."""

    updated_at: str
    user: GitHubUser


class GitHubInstallation(TypedDict, total=False):
    """GitHub App installation info included in webhooks."""

    id: int
    node_id: str


class _IssueCommentIssue(TypedDict, total=False):
    """Nested issue object in IssueCommentWebhookPayload."""

    number: int
    title: str
    pull_request: dict[str, str]


class IssueCommentWebhookPayload(TypedDict, total=False):
    """Webhook payload for issue_comment events."""

    action: str  # "created" | "edited" | "deleted"
    comment: GitHubIssueComment
    installation: GitHubInstallation
    issue: _IssueCommentIssue
    repository: GitHubRepository
    sender: GitHubUser


class PullRequestReviewCommentWebhookPayload(TypedDict, total=False):
    """Webhook payload for pull_request_review_comment events."""

    action: str  # "created" | "edited" | "deleted"
    comment: GitHubReviewComment
    installation: GitHubInstallation
    pull_request: GitHubPullRequest
    repository: GitHubRepository
    sender: GitHubUser


# =============================================================================
# Raw Message Type
# =============================================================================


class GitHubRawIssueComment(TypedDict):
    """Platform-specific raw message for issue comments."""

    type: Literal["issue_comment"]
    comment: GitHubIssueComment
    repository: GitHubRepository
    pr_number: int


class GitHubRawReviewComment(TypedDict):
    """Platform-specific raw message for review comments."""

    type: Literal["review_comment"]
    comment: GitHubReviewComment
    repository: GitHubRepository
    pr_number: int


GitHubRawMessage = Union[GitHubRawIssueComment, GitHubRawReviewComment]

# =============================================================================
# GitHub API Response Types
# =============================================================================

GitHubReactionContent = Literal["+1", "-1", "laugh", "confused", "heart", "hooray", "rocket", "eyes"]
