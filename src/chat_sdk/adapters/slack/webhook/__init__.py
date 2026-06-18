"""Slack webhook primitives — a lightweight, runtime-free subpath.

Port of ``packages/adapter-slack/src/webhook`` (vercel/chat#538), exposed
upstream as ``@chat-adapter/slack/webhook``. Provides primitives for
verifying Slack requests, reading signed webhook bodies, parsing Events API
callbacks, slash commands, and interactive payloads, and returning
provider-native continuation data — without the full Slack adapter,
``slack_sdk``, chat state, dedupe, locks, or subscriptions.

Importing this module never imports ``slack_sdk``, HTTP clients, or the
high-level :mod:`chat_sdk.adapters.slack.adapter`.
"""

from chat_sdk.adapters.slack.webhook.parse import parse_slack_webhook_body
from chat_sdk.adapters.slack.webhook.types import (
    SlackAction,
    SlackAppMentionPayload,
    SlackBlockActionsPayload,
    SlackBlockSuggestionPayload,
    SlackContinuation,
    SlackDirectMessagePayload,
    SlackFile,
    SlackHeaders,
    SlackRetry,
    SlackSlashCommandPayload,
    SlackUnsupportedPayload,
    SlackUrlVerificationPayload,
    SlackUser,
    SlackViewClosedPayload,
    SlackViewStateValue,
    SlackViewSubmissionPayload,
    SlackWebhookError,
    SlackWebhookParseError,
    SlackWebhookPayload,
    SlackWebhookVerificationError,
    SlackWebhookVerifier,
)
from chat_sdk.adapters.slack.webhook.utils import read_slack_request_body
from chat_sdk.adapters.slack.webhook.verify import (
    read_slack_webhook,
    verify_slack_request,
    verify_slack_signature,
)

__all__ = [
    "SlackAction",
    "SlackAppMentionPayload",
    "SlackBlockActionsPayload",
    "SlackBlockSuggestionPayload",
    "SlackContinuation",
    "SlackDirectMessagePayload",
    "SlackFile",
    "SlackHeaders",
    "SlackRetry",
    "SlackSlashCommandPayload",
    "SlackUnsupportedPayload",
    "SlackUrlVerificationPayload",
    "SlackUser",
    "SlackViewClosedPayload",
    "SlackViewStateValue",
    "SlackViewSubmissionPayload",
    "SlackWebhookError",
    "SlackWebhookParseError",
    "SlackWebhookPayload",
    "SlackWebhookVerificationError",
    "SlackWebhookVerifier",
    "parse_slack_webhook_body",
    "read_slack_request_body",
    "read_slack_webhook",
    "verify_slack_request",
    "verify_slack_signature",
]
