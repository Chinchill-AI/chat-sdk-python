"""Types for the Slack webhook primitives subpath.

Port of ``packages/adapter-slack/src/webhook/types.ts`` (vercel/chat#538).

These types describe the lightweight, runtime-free webhook surface:
verifying Slack requests, parsing Events API callbacks, slash commands and
interactive payloads — without the full Slack adapter, ``slack_sdk``, chat
state, dedupe, locks, or subscriptions.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

# Headers as accepted by the webhook helpers. Either a mapping (plain dict,
# framework multidict — matched case-insensitively; multi-value entries use
# the first value) or an iterable of ``(name, value)`` pairs.
#
# Upstream: ``Headers | Iterable<[string, string]> | Record<string, value>``.
SlackHeaders: TypeAlias = Mapping[str, Any] | Iterable[tuple[str, str]]

# Custom webhook verifier. Receives the original request object and the raw
# body string already consumed by the caller. Return:
#   - ``True`` (or any truthy non-string value) to accept the request as-is.
#   - A ``str`` to accept *and* substitute the verified body for downstream
#     parsing (useful when the verifier canonicalizes the payload).
#   - ``False``/falsy or raise to reject.
#
# May be sync or async.
#
# SECURITY: When a custom verifier replaces ``signing_secret``, the built-in
# HMAC + timestamp tolerance check is bypassed. The implementer is
# responsible for:
#   - constant-time signature comparison (use ``hmac.compare_digest``, never ``==``)
#   - replay protection (validate ``x-slack-request-timestamp`` freshness)
#   - any other freshness/origin checks the platform requires
#   - body-substitution safety: when returning a ``str`` to substitute the body
#     for downstream parsing, the returned bytes MUST be derived from a
#     verified payload. Returning attacker-controlled bytes (e.g. echoing the
#     unverified raw body or splicing in untrusted fields) grants payload
#     injection — downstream parsing trusts the substituted body unconditionally.
SlackWebhookVerifier = Callable[[Any, str], "bool | str | None | Awaitable[bool | str | None]"]


@dataclass
class SlackRetry:
    """Slack retry metadata from ``x-slack-retry-num`` / ``x-slack-retry-reason``."""

    num: float
    reason: str | None = None


@dataclass
class SlackContinuation:
    """Provider-native continuation data for message-like payloads."""

    channel_id: str
    thread_ts: str
    enterprise_id: str | None = None
    team_id: str | None = None


@dataclass
class SlackUrlVerificationPayload:
    """Slack ``url_verification`` handshake payload."""

    challenge: str
    raw: dict[str, Any]
    retry: SlackRetry | None = None
    kind: Literal["url_verification"] = "url_verification"


@dataclass
class _SlackEventBasePayload:
    """Shared fields for Events API message-like payloads."""

    channel_id: str
    continuation: SlackContinuation
    raw: dict[str, Any]
    text: str
    thread_ts: str
    ts: str
    api_app_id: str | None = None
    enterprise_id: str | None = None
    event_id: str | None = None
    event_time: float | None = None
    is_ext_shared_channel: bool | None = None
    retry: SlackRetry | None = None
    team_id: str | None = None
    user_id: str | None = None


@dataclass
class SlackAppMentionPayload(_SlackEventBasePayload):
    """An ``app_mention`` Events API callback."""

    event_type: Literal["app_mention"] = "app_mention"
    kind: Literal["app_mention"] = "app_mention"


@dataclass
class SlackDirectMessagePayload(_SlackEventBasePayload):
    """A ``message`` Events API callback delivered in an IM channel."""

    bot_id: str | None = None
    subtype: str | None = None
    event_type: Literal["message"] = "message"
    kind: Literal["direct_message"] = "direct_message"


@dataclass
class SlackSlashCommandPayload:
    """A slash-command form post."""

    channel_id: str
    command: str
    is_enterprise_install: bool
    raw: dict[str, str]
    text: str
    user_id: str
    channel_name: str | None = None
    enterprise_id: str | None = None
    response_url: str | None = None
    retry: SlackRetry | None = None
    team_id: str | None = None
    trigger_id: str | None = None
    user_name: str | None = None
    kind: Literal["slash_command"] = "slash_command"


@dataclass
class SlackAction:
    """A single action inside a ``block_actions`` payload."""

    action_id: str
    raw: dict[str, Any]
    type: str
    block_id: str | None = None
    label: str | None = None
    selected_option_value: str | None = None
    value: str | None = None


@dataclass
class SlackBlockActionsPayload:
    """A ``block_actions`` interactive payload."""

    actions: list[SlackAction]
    raw: dict[str, Any]
    user_id: str
    channel_id: str | None = None
    continuation: SlackContinuation | None = None
    enterprise_id: str | None = None
    is_enterprise_install: bool | None = None
    message_ts: str | None = None
    response_url: str | None = None
    retry: SlackRetry | None = None
    team_id: str | None = None
    thread_ts: str | None = None
    trigger_id: str | None = None
    user_name: str | None = None
    kind: Literal["block_actions"] = "block_actions"


@dataclass
class SlackBlockSuggestionPayload:
    """A ``block_suggestion`` (external select options) payload."""

    action_id: str
    block_id: str
    raw: dict[str, Any]
    user_id: str
    value: str
    channel_id: str | None = None
    enterprise_id: str | None = None
    retry: SlackRetry | None = None
    team_id: str | None = None
    kind: Literal["block_suggestion"] = "block_suggestion"


@dataclass
class SlackViewSubmissionPayload:
    """A ``view_submission`` interactive payload."""

    raw: dict[str, Any]
    user_id: str
    view: dict[str, Any]
    enterprise_id: str | None = None
    response_urls: list[Any] | None = None
    retry: SlackRetry | None = None
    team_id: str | None = None
    kind: Literal["view_submission"] = "view_submission"


@dataclass
class SlackViewClosedPayload:
    """A ``view_closed`` interactive payload."""

    raw: dict[str, Any]
    user_id: str
    view: dict[str, Any]
    enterprise_id: str | None = None
    retry: SlackRetry | None = None
    team_id: str | None = None
    kind: Literal["view_closed"] = "view_closed"


@dataclass
class SlackUnsupportedPayload:
    """Any payload the primitives recognize but do not model."""

    raw: Any
    type: str
    retry: SlackRetry | None = None
    kind: Literal["unsupported"] = "unsupported"


SlackWebhookPayload: TypeAlias = (
    SlackAppMentionPayload
    | SlackBlockActionsPayload
    | SlackBlockSuggestionPayload
    | SlackDirectMessagePayload
    | SlackSlashCommandPayload
    | SlackUnsupportedPayload
    | SlackUrlVerificationPayload
    | SlackViewClosedPayload
    | SlackViewSubmissionPayload
)


class SlackWebhookError(Exception):
    """Base error for the Slack webhook primitives."""


class SlackWebhookVerificationError(SlackWebhookError):
    """Raised when a Slack request fails verification."""


class SlackWebhookParseError(SlackWebhookError):
    """Raised when a Slack webhook body cannot be parsed."""
