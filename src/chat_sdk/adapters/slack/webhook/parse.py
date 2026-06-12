"""Parsing for the Slack webhook primitives subpath.

Port of ``packages/adapter-slack/src/webhook/parse.ts`` (vercel/chat#538).

Parses Events API callbacks, slash commands, and interactive payloads into
typed payload dataclasses with provider-native continuation data, without
the full Slack adapter runtime.
"""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import parse_qsl

from chat_sdk.adapters.slack.webhook.types import (
    SlackAction,
    SlackAppMentionPayload,
    SlackBlockActionsPayload,
    SlackBlockSuggestionPayload,
    SlackContinuation,
    SlackDirectMessagePayload,
    SlackHeaders,
    SlackRetry,
    SlackSlashCommandPayload,
    SlackUnsupportedPayload,
    SlackUrlVerificationPayload,
    SlackViewClosedPayload,
    SlackViewSubmissionPayload,
    SlackWebhookPayload,
)
from chat_sdk.adapters.slack.webhook.utils import (
    get_header,
    get_retry,
    is_form_body,
    is_record,
    optional_string,
    parse_json_body,
    record_value,
    string_value,
)


def parse_slack_webhook_body(
    body: str,
    *,
    content_type: str | None = None,
    headers: SlackHeaders | None = None,
) -> SlackWebhookPayload:
    """Parse a raw Slack webhook body into a typed payload.

    ``content_type`` wins over a ``content-type`` header when both are given;
    with neither, the body shape decides (JSON object vs form-urlencoded).
    """
    resolved_content_type = content_type if content_type is not None else get_header(headers, "content-type")
    if resolved_content_type is None:
        resolved_content_type = ""
    retry = get_retry(headers)

    if is_form_body(body, resolved_content_type):
        return _parse_form_body(body, retry)

    raw = parse_json_body(body)
    return _classify_json_payload(raw, retry)


def _parse_form_body(body: str, retry: SlackRetry | None) -> SlackWebhookPayload:
    params = dict(parse_qsl(body, keep_blank_values=True))
    payload = params.get("payload")
    if payload is not None:
        raw = parse_json_body(payload)
        return _classify_interaction_payload(raw, retry)
    if "command" in params:
        return _parse_slash_command(params, retry)
    return SlackUnsupportedPayload(raw=params, retry=retry, type="form")


def _classify_json_payload(raw: Any, retry: SlackRetry | None) -> SlackWebhookPayload:
    if not is_record(raw):
        return SlackUnsupportedPayload(raw=raw, retry=retry, type="unknown")

    if raw.get("type") == "url_verification" and isinstance(raw.get("challenge"), str):
        return SlackUrlVerificationPayload(challenge=raw["challenge"], raw=raw, retry=retry)

    event = raw.get("event")
    if raw.get("type") != "event_callback" or not is_record(event):
        raw_type = raw.get("type")
        return SlackUnsupportedPayload(
            raw=raw,
            retry=retry,
            type=raw_type if isinstance(raw_type, str) else "unknown",
        )

    if event.get("type") == "app_mention":
        return _parse_message_event("app_mention", raw, event, retry)

    if event.get("type") == "message" and event.get("channel_type") == "im":
        return _parse_message_event("direct_message", raw, event, retry)

    event_type = event.get("type")
    return SlackUnsupportedPayload(
        raw=raw,
        retry=retry,
        type=event_type if isinstance(event_type, str) else "event_callback",
    )


def _classify_interaction_payload(raw: Any, retry: SlackRetry | None) -> SlackWebhookPayload:
    if not is_record(raw):
        return SlackUnsupportedPayload(raw=raw, retry=retry, type="interaction")

    payload_type = raw.get("type")
    if payload_type == "block_actions":
        return _parse_block_actions(raw, retry)
    if payload_type == "block_suggestion":
        return _parse_block_suggestion(raw, retry)
    if payload_type == "view_submission":
        return _parse_view_submission(raw, retry)
    if payload_type == "view_closed":
        return _parse_view_closed(raw, retry)
    return SlackUnsupportedPayload(
        raw=raw,
        retry=retry,
        type=payload_type if isinstance(payload_type, str) else "interaction",
    )


def _parse_message_event(
    kind: Literal["app_mention", "direct_message"],
    envelope: dict[str, Any],
    event: dict[str, Any],
    retry: SlackRetry | None,
) -> SlackAppMentionPayload | SlackDirectMessagePayload:
    channel_id = string_value(event.get("channel"))
    ts = string_value(event.get("ts"))
    thread_ts = string_value(event.get("thread_ts")) or ts
    team_id = optional_string(event.get("team_id")) or optional_string(envelope.get("team_id"))
    enterprise_id = optional_string(envelope.get("enterprise_id")) or optional_string(
        envelope.get("context_enterprise_id")
    )
    continuation = SlackContinuation(
        channel_id=channel_id,
        enterprise_id=enterprise_id,
        team_id=team_id,
        thread_ts=thread_ts,
    )
    event_time = envelope.get("event_time")
    base: dict[str, Any] = {
        "api_app_id": optional_string(envelope.get("api_app_id")),
        "channel_id": channel_id,
        "continuation": continuation,
        "enterprise_id": enterprise_id,
        "event_id": optional_string(envelope.get("event_id")),
        "event_time": event_time if isinstance(event_time, (int, float)) and not isinstance(event_time, bool) else None,
        "is_ext_shared_channel": (
            envelope.get("is_ext_shared_channel") if isinstance(envelope.get("is_ext_shared_channel"), bool) else None
        ),
        "raw": event,
        "retry": retry,
        "team_id": team_id,
        "text": string_value(event.get("text")),
        "thread_ts": thread_ts,
        "ts": ts,
        "user_id": optional_string(event.get("user")),
    }

    if kind == "app_mention":
        return SlackAppMentionPayload(**base)

    return SlackDirectMessagePayload(
        **base,
        bot_id=optional_string(event.get("bot_id")),
        subtype=optional_string(event.get("subtype")),
    )


def _parse_slash_command(params: dict[str, str], retry: SlackRetry | None) -> SlackSlashCommandPayload:
    return SlackSlashCommandPayload(
        channel_id=params.get("channel_id", ""),
        channel_name=params.get("channel_name") or None,
        command=params.get("command", ""),
        enterprise_id=params.get("enterprise_id") or None,
        is_enterprise_install=params.get("is_enterprise_install") == "true",
        raw=params,
        response_url=params.get("response_url") or None,
        retry=retry,
        team_id=params.get("team_id") or None,
        text=params.get("text", ""),
        trigger_id=params.get("trigger_id") or None,
        user_id=params.get("user_id", ""),
        user_name=params.get("user_name") or None,
    )


def _parse_block_actions(raw: dict[str, Any], retry: SlackRetry | None) -> SlackBlockActionsPayload:
    channel = record_value(raw.get("channel"))
    container = record_value(raw.get("container"))
    message = record_value(raw.get("message"))
    user = record_value(raw.get("user"))
    team = record_value(raw.get("team"))
    enterprise = record_value(raw.get("enterprise"))
    channel_id = optional_string(_get(channel, "id")) or optional_string(_get(container, "channel_id"))
    message_ts = optional_string(_get(message, "ts")) or optional_string(_get(container, "message_ts"))
    thread_ts = (
        optional_string(_get(message, "thread_ts")) or optional_string(_get(container, "thread_ts")) or message_ts
    )
    team_id = optional_string(_get(team, "id")) or optional_string(_get(user, "team_id"))
    enterprise_id = optional_string(_get(enterprise, "id")) or optional_string(_get(team, "enterprise_id"))
    continuation = (
        SlackContinuation(
            channel_id=channel_id,
            enterprise_id=enterprise_id,
            team_id=team_id,
            thread_ts=thread_ts,
        )
        if channel_id and thread_ts
        else None
    )
    actions = raw.get("actions")

    return SlackBlockActionsPayload(
        actions=[_parse_action(action) for action in actions] if isinstance(actions, list) else [],
        channel_id=channel_id,
        continuation=continuation,
        enterprise_id=enterprise_id,
        is_enterprise_install=(
            raw.get("is_enterprise_install") if isinstance(raw.get("is_enterprise_install"), bool) else None
        ),
        message_ts=message_ts,
        raw=raw,
        response_url=optional_string(raw.get("response_url")),
        retry=retry,
        team_id=team_id,
        thread_ts=thread_ts,
        trigger_id=optional_string(raw.get("trigger_id")),
        user_id=string_value(_get(user, "id")),
        user_name=optional_string(_get(user, "username")) or optional_string(_get(user, "name")),
    )


def _parse_action(action: Any) -> SlackAction:
    raw = action if is_record(action) else {}
    selected_option = record_value(raw.get("selected_option"))
    text = record_value(raw.get("text"))
    return SlackAction(
        action_id=string_value(raw.get("action_id")),
        block_id=optional_string(raw.get("block_id")),
        label=optional_string(_get(text, "text")),
        raw=raw,
        selected_option_value=optional_string(_get(selected_option, "value")),
        type=string_value(raw.get("type")),
        value=optional_string(raw.get("value")),
    )


def _parse_block_suggestion(raw: dict[str, Any], retry: SlackRetry | None) -> SlackBlockSuggestionPayload:
    channel = record_value(raw.get("channel"))
    team = record_value(raw.get("team"))
    enterprise = record_value(raw.get("enterprise"))
    user = record_value(raw.get("user"))
    return SlackBlockSuggestionPayload(
        action_id=string_value(raw.get("action_id")),
        block_id=string_value(raw.get("block_id")),
        channel_id=optional_string(_get(channel, "id")),
        enterprise_id=optional_string(_get(enterprise, "id")) or optional_string(_get(team, "enterprise_id")),
        raw=raw,
        retry=retry,
        team_id=optional_string(_get(team, "id")),
        user_id=string_value(_get(user, "id")),
        value=string_value(raw.get("value")),
    )


def _parse_view_submission(raw: dict[str, Any], retry: SlackRetry | None) -> SlackViewSubmissionPayload:
    team = record_value(raw.get("team"))
    enterprise = record_value(raw.get("enterprise"))
    user = record_value(raw.get("user"))
    view = record_value(raw.get("view"))
    if view is None:
        view = {}
    response_urls = view.get("response_urls")
    return SlackViewSubmissionPayload(
        enterprise_id=optional_string(_get(enterprise, "id")) or optional_string(_get(team, "enterprise_id")),
        raw=raw,
        response_urls=response_urls if isinstance(response_urls, list) else None,
        retry=retry,
        team_id=optional_string(_get(team, "id")),
        user_id=string_value(_get(user, "id")),
        view=view,
    )


def _parse_view_closed(raw: dict[str, Any], retry: SlackRetry | None) -> SlackViewClosedPayload:
    team = record_value(raw.get("team"))
    enterprise = record_value(raw.get("enterprise"))
    user = record_value(raw.get("user"))
    view = record_value(raw.get("view"))
    return SlackViewClosedPayload(
        enterprise_id=optional_string(_get(enterprise, "id")) or optional_string(_get(team, "enterprise_id")),
        raw=raw,
        retry=retry,
        team_id=optional_string(_get(team, "id")),
        user_id=string_value(_get(user, "id")),
        view=view if view is not None else {},
    )


def _get(record: dict[str, Any] | None, key: str) -> Any:
    """Optional-chained lookup: ``record?.[key]``."""
    return record.get(key) if record is not None else None
