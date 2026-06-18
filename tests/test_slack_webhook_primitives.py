"""Tests for the Slack webhook primitives subpath.

Port of ``packages/adapter-slack/src/webhook/index.test.ts`` and
``webhook/boundary.test.ts`` (vercel/chat#538).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import subprocess
import sys
from typing import Any
from unittest.mock import Mock
from urllib.parse import parse_qsl, urlencode

import pytest

from chat_sdk.adapters.slack.webhook import (
    SlackContinuation,
    SlackFile,
    SlackRetry,
    SlackUser,
    SlackViewStateValue,
    SlackWebhookParseError,
    SlackWebhookVerificationError,
    parse_slack_webhook_body,
    read_slack_webhook,
    verify_slack_request,
    verify_slack_signature,
)

SECRET = "8f742231b10e8888abcd99yyyzzz85a5"
TIMESTAMP = 1_531_420_618


def _now() -> float:
    return TIMESTAMP


def _sign(body: str, time: int = TIMESTAMP) -> str:
    digest = hmac.new(SECRET.encode(), f"v0:{time}:{body}".encode(), hashlib.sha256).hexdigest()
    return f"v0={digest}"


def _headers(body: str, time: int = TIMESTAMP) -> dict[str, str]:
    return {
        "content-type": "application/json",
        "x-slack-request-timestamp": str(time),
        "x-slack-signature": _sign(body, time),
    }


class _FakeRequest:
    """Minimal request-like object (body + headers, async ``text()``)."""

    def __init__(self, body: str, headers: dict[str, str] | None = None):
        self._body = body
        self.headers = headers or {}

    async def text(self) -> str:
        return self._body


def _request(body: str, *, content_type: str = "application/json", time: int = TIMESTAMP) -> _FakeRequest:
    return _FakeRequest(
        body,
        {
            "content-type": content_type,
            "x-slack-request-timestamp": str(time),
            "x-slack-signature": _sign(body, time),
        },
    )


class TestVerifySlackSignature:
    def test_accepts_a_valid_slack_signature(self):
        body = (
            "token=xyzz0WbapA4vBCDEFasx0q6G&team_id=T1DC2JH3J&team_domain=testteamnow"
            "&channel_id=G8PSS9T3V&channel_name=foobar&user_id=U2CERLKJA&user_name=roadrunner"
            "&command=%2Fwebhook-collect&text="
            "&response_url=https%3A%2F%2Fhooks.slack.com%2Fcommands%2FT1DC2JH3J%2F397700885554%2F96rGlfmibIGlgcZRskXaIFfN"
            "&trigger_id=398738663015.47445629121.803a0bc887a14d10d2c447fce8b6703c"
        )

        assert verify_slack_signature(body, _headers(body), signing_secret=SECRET, now=_now) is None

    def test_rejects_stale_timestamps(self):
        body = "payload"

        with pytest.raises(SlackWebhookVerificationError):
            verify_slack_signature(body, _headers(body, TIMESTAMP - 301), signing_secret=SECRET, now=_now)

    def test_rejects_invalid_signatures(self):
        body = "payload"
        signed_headers = _headers(body)
        signed_headers["x-slack-signature"] = "v0=bad"

        with pytest.raises(SlackWebhookVerificationError):
            verify_slack_signature(body, signed_headers, signing_secret=SECRET, now=_now)

    def test_rejects_well_formed_signatures_with_the_wrong_digest(self):
        body = "payload"
        signed_headers = _headers(body)
        signed_headers["x-slack-signature"] = "v0=" + "0" * 64

        with pytest.raises(SlackWebhookVerificationError):
            verify_slack_signature(body, signed_headers, signing_secret=SECRET, now=_now)

    def test_accepts_plain_object_headers_case_insensitively(self):
        body = "payload"

        result = verify_slack_signature(
            body,
            {
                "Content-Type": "application/json",
                "X-Slack-Request-Timestamp": str(TIMESTAMP),
                "X-Slack-Signature": _sign(body),
            },
            signing_secret=SECRET,
            now=_now,
        )

        assert result is None

    def test_rejects_when_signing_secret_is_missing(self):
        """Fail closed: no secret means no verification is possible."""
        body = "payload"

        with pytest.raises(SlackWebhookVerificationError, match="signing secret"):
            verify_slack_signature(body, _headers(body), signing_secret=None, now=_now)

    def test_rejects_missing_signature_headers(self):
        body = "payload"

        with pytest.raises(SlackWebhookVerificationError, match="headers are required"):
            verify_slack_signature(
                body,
                {"x-slack-signature": _sign(body)},
                signing_secret=SECRET,
                now=_now,
            )
        with pytest.raises(SlackWebhookVerificationError, match="headers are required"):
            verify_slack_signature(
                body,
                {"x-slack-request-timestamp": str(TIMESTAMP)},
                signing_secret=SECRET,
                now=_now,
            )

    def test_rejects_non_numeric_timestamps(self):
        body = "payload"

        with pytest.raises(SlackWebhookVerificationError, match="timestamp is invalid"):
            verify_slack_signature(
                body,
                {
                    "x-slack-request-timestamp": "not-a-number",
                    "x-slack-signature": _sign(body),
                },
                signing_secret=SECRET,
                now=_now,
            )


class TestVerifySlackRequest:
    @pytest.mark.asyncio
    async def test_returns_the_verified_body(self):
        body = json.dumps({"type": "event_callback"})

        result = await verify_slack_request(_request(body), signing_secret=SECRET, now=_now)

        assert result == body

    @pytest.mark.asyncio
    async def test_uses_a_custom_verifier(self):
        verifier = Mock(return_value=True)
        body = "payload"
        request = _FakeRequest(body)

        result = await verify_slack_request(request, webhook_verifier=verifier)

        assert result == body
        verifier.assert_called_once_with(request, body)

    @pytest.mark.asyncio
    async def test_rejects_when_custom_verifier_returns_falsy(self):
        async def verifier(_request: Any, _body: str) -> bool:
            return False

        with pytest.raises(SlackWebhookVerificationError):
            await verify_slack_request(_FakeRequest("payload"), webhook_verifier=verifier)

    @pytest.mark.asyncio
    async def test_allows_a_custom_verifier_to_replace_the_body(self):
        verified_body = json.dumps({"challenge": "challenge-value", "type": "url_verification"})

        payload = await read_slack_webhook(
            _FakeRequest("original"),
            webhook_verifier=lambda _request, _body: verified_body,
        )

        assert payload.kind == "url_verification"
        assert payload.challenge == "challenge-value"
        assert payload.raw == {"challenge": "challenge-value", "type": "url_verification"}
        assert payload.retry is None


class TestParseSlackWebhookBody:
    def test_parses_url_verification_payloads(self):
        payload = parse_slack_webhook_body(
            json.dumps(
                {
                    "challenge": "3eZbrw1aBm2rZgRNFdxV2595E9CY3gmdALWMmHkvFXO7tYXAYM8P",
                    "token": "deprecated",
                    "type": "url_verification",
                }
            ),
            content_type="application/json",
        )

        assert payload.kind == "url_verification"
        assert payload.challenge == "3eZbrw1aBm2rZgRNFdxV2595E9CY3gmdALWMmHkvFXO7tYXAYM8P"

    def test_parses_app_mentions_with_provider_native_continuation(self):
        payload = parse_slack_webhook_body(
            json.dumps(
                {
                    "api_app_id": "A123",
                    "event": {
                        "channel": "C123",
                        "text": "<@U999> hello",
                        "files": [
                            {
                                "id": "F123",
                                "mimetype": "image/png",
                                "name": "chart.png",
                                "size": 123,
                                "title": "Chart",
                                "url_private": "https://files.slack.com/files-pri/chart.png",
                                "url_private_download": ("https://files.slack.com/files-pri/chart-download.png"),
                            }
                        ],
                        "thread_ts": "1710000000.000001",
                        "ts": "1710000000.000002",
                        "type": "app_mention",
                        "user": "U123",
                    },
                    "event_id": "Ev123",
                    "event_time": 1_710_000_000,
                    "is_ext_shared_channel": True,
                    "team_id": "T123",
                    "type": "event_callback",
                }
            ),
            content_type="application/json",
            headers={
                "x-slack-retry-num": "2",
                "x-slack-retry-reason": "http_timeout",
            },
        )

        assert payload.kind == "app_mention"
        assert payload.api_app_id == "A123"
        assert payload.channel_id == "C123"
        assert payload.continuation == SlackContinuation(
            channel_id="C123",
            team_id="T123",
            thread_ts="1710000000.000001",
        )
        assert payload.event_id == "Ev123"
        assert payload.event_time == 1_710_000_000
        assert payload.files == [
            SlackFile(
                download_url="https://files.slack.com/files-pri/chart-download.png",
                id="F123",
                mime_type="image/png",
                name="chart.png",
                raw=payload.files[0].raw,
                size=123,
                title="Chart",
                type="image",
                url="https://files.slack.com/files-pri/chart.png",
            )
        ]
        # ``raw`` is the verbatim Slack file object, not dropped.
        assert payload.files[0].raw["url_private"] == "https://files.slack.com/files-pri/chart.png"
        assert payload.is_ext_shared_channel is True
        assert payload.retry == SlackRetry(num=2, reason="http_timeout")
        assert payload.text == "<@U999> hello"
        assert payload.thread_ts == "1710000000.000001"
        assert payload.ts == "1710000000.000002"
        assert payload.user_id == "U123"

    def test_uses_ts_as_thread_ts_when_app_mentions_are_top_level_messages(self):
        payload = parse_slack_webhook_body(
            json.dumps(
                {
                    "event": {
                        "channel": "C123",
                        "text": "hello",
                        "ts": "1710000000.000002",
                        "type": "app_mention",
                        "user": "U123",
                    },
                    "team_id": "T123",
                    "type": "event_callback",
                }
            ),
            content_type="application/json",
        )

        assert payload.kind == "app_mention"
        assert payload.continuation.channel_id == "C123"
        assert payload.continuation.thread_ts == "1710000000.000002"
        assert payload.thread_ts == "1710000000.000002"

    def test_parses_direct_message_events(self):
        payload = parse_slack_webhook_body(
            json.dumps(
                {
                    "event": {
                        "bot_id": "B123",
                        "channel": "D123",
                        "channel_type": "im",
                        "subtype": "bot_message",
                        "text": "hello",
                        "ts": "1710000000.000002",
                        "type": "message",
                        "user": "U123",
                    },
                    "team_id": "T123",
                    "type": "event_callback",
                }
            )
        )

        assert payload.kind == "direct_message"
        assert payload.bot_id == "B123"
        assert payload.channel_id == "D123"
        assert payload.subtype == "bot_message"

    def test_parses_slash_command_form_posts(self):
        form = {
            "channel_id": "C123",
            "channel_name": "general",
            "command": "/deploy",
            "enterprise_id": "E123",
            "is_enterprise_install": "true",
            "response_url": "https://hooks.slack.com/commands/T123/1/abc",
            "team_id": "T123",
            "text": "prod",
            "trigger_id": "123.456.abc",
            "user_id": "U123",
            "user_name": "josh",
        }
        body = urlencode(form)

        payload = parse_slack_webhook_body(body, content_type="application/x-www-form-urlencoded")

        assert payload.kind == "slash_command"
        assert payload.channel_id == "C123"
        assert payload.channel_name == "general"
        assert payload.command == "/deploy"
        assert payload.enterprise_id == "E123"
        assert payload.is_enterprise_install is True
        assert payload.raw == dict(parse_qsl(body))
        assert payload.response_url == "https://hooks.slack.com/commands/T123/1/abc"
        assert payload.retry is None
        assert payload.team_id == "T123"
        assert payload.text == "prod"
        assert payload.trigger_id == "123.456.abc"
        assert payload.user_id == "U123"
        assert payload.user_name == "josh"

    def test_parses_block_action_payloads(self):
        raw = {
            "actions": [
                {
                    "action_id": "approve",
                    "block_id": "actions",
                    "selected_option": {
                        "text": {"text": "Yes", "type": "plain_text"},
                        "value": "yes",
                    },
                    "text": {"text": "Approve", "type": "plain_text"},
                    "type": "button",
                    "value": "approve-value",
                }
            ],
            "channel": {"id": "C123", "name": "general"},
            "container": {
                "channel_id": "C123",
                "message_ts": "1710000000.000002",
                "thread_ts": "1710000000.000001",
                "type": "message",
            },
            "message": {
                "blocks": [
                    {
                        "text": {"text": "Approve deployment?", "type": "mrkdwn"},
                        "type": "section",
                    }
                ],
                "thread_ts": "1710000000.000001",
                "ts": "1710000000.000002",
            },
            "response_url": "https://hooks.slack.com/actions/T123/1/abc",
            "team": {"enterprise_id": "E123", "id": "T123"},
            "trigger_id": "123.456.abc",
            "type": "block_actions",
            "user": {"id": "U123", "username": "josh"},
        }
        body = urlencode({"payload": json.dumps(raw)})

        payload = parse_slack_webhook_body(body, content_type="application/x-www-form-urlencoded")

        assert payload.kind == "block_actions"
        assert len(payload.actions) == 1
        action = payload.actions[0]
        assert action.action_id == "approve"
        assert action.block_id == "actions"
        # Upstream parse.ts:276 — the selected option's text wins over the
        # element text when both are present.
        assert action.label == "Yes"
        assert action.selected_option_label == "Yes"
        assert action.selected_option_value == "yes"
        assert action.type == "button"
        assert action.value == "approve-value"
        # The acting user is attached to every parsed action (parse.ts:296).
        assert action.user == SlackUser(id="U123", username="josh")
        assert payload.channel_id == "C123"
        assert payload.continuation == SlackContinuation(
            channel_id="C123",
            enterprise_id="E123",
            team_id="T123",
            thread_ts="1710000000.000001",
        )
        assert payload.message_blocks == [
            {
                "text": {"text": "Approve deployment?", "type": "mrkdwn"},
                "type": "section",
            }
        ]
        assert payload.message_prompt_block == {
            "text": {"text": "Approve deployment?", "type": "mrkdwn"},
            "type": "section",
        }
        assert payload.message_prompt_text == "Approve deployment?"
        assert payload.message_ts == "1710000000.000002"
        assert payload.response_url == "https://hooks.slack.com/actions/T123/1/abc"
        assert payload.team_id == "T123"
        assert payload.thread_ts == "1710000000.000001"
        assert payload.trigger_id == "123.456.abc"
        assert payload.user == SlackUser(id="U123", username="josh")
        assert payload.user_id == "U123"
        assert payload.user_name == "josh"

    def test_select_action_label_falls_back_to_element_text(self):
        """When no option is selected, the label falls back to the element's
        own text (parse.ts:276 — ``selectedText?.text || text?.text``)."""
        raw = {
            "actions": [
                {
                    "action_id": "approve",
                    "text": {"text": "Approve", "type": "plain_text"},
                    "type": "button",
                    "value": "approve-value",
                }
            ],
            "channel": {"id": "C123"},
            "container": {"channel_id": "C123", "thread_ts": "1710000000.000001"},
            "type": "block_actions",
            "user": {"id": "U123"},
        }

        payload = parse_slack_webhook_body(
            urlencode({"payload": json.dumps(raw)}),
            content_type="application/x-www-form-urlencoded",
        )

        action = payload.actions[0]
        assert action.label == "Approve"
        assert action.selected_option_label is None
        assert action.selected_option_value is None

    def test_parses_block_suggestion_payloads(self):
        raw = {
            "action_id": "external",
            "block_id": "input",
            "channel": {"id": "C123"},
            "enterprise": {"id": "E123"},
            "team": {"id": "T123"},
            "type": "block_suggestion",
            "user": {"id": "U123"},
            "value": "hel",
        }

        payload = parse_slack_webhook_body(
            urlencode({"payload": json.dumps(raw)}),
            content_type="application/x-www-form-urlencoded",
        )

        assert payload.kind == "block_suggestion"
        assert payload.action_id == "external"
        assert payload.block_id == "input"
        assert payload.channel_id == "C123"
        assert payload.enterprise_id == "E123"
        assert payload.team_id == "T123"
        assert payload.user_id == "U123"
        assert payload.value == "hel"

    def test_parses_view_submissions(self):
        raw = {
            "team": {"id": "T123"},
            "type": "view_submission",
            "user": {"id": "U123"},
            "view": {
                "callback_id": "feedback",
                "id": "V123",
                "private_metadata": '{"id":"123"}',
                "response_urls": [
                    {
                        "action_id": "target",
                        "channel_id": "C123",
                        "response_url": "https://hooks.slack.com/app/1/2/3",
                    }
                ],
                "state": {
                    "values": {
                        "feedback": {
                            "message": {
                                "type": "plain_text_input",
                                "value": "looks good",
                            }
                        }
                    }
                },
            },
        }

        payload = parse_slack_webhook_body(
            urlencode({"payload": json.dumps(raw)}),
            content_type="application/x-www-form-urlencoded",
        )

        assert payload.kind == "view_submission"
        assert payload.callback_id == "feedback"
        assert payload.private_metadata == '{"id":"123"}'
        assert payload.response_urls == [
            {
                "action_id": "target",
                "channel_id": "C123",
                "response_url": "https://hooks.slack.com/app/1/2/3",
            }
        ]
        assert payload.team_id == "T123"
        assert payload.user == SlackUser(id="U123")
        assert payload.user_id == "U123"
        # parseViewValues (parse.ts:404) flattens state.values into a list.
        assert payload.values == [
            SlackViewStateValue(
                action_id="message",
                block_id="feedback",
                raw={"type": "plain_text_input", "value": "looks good"},
                type="plain_text_input",
                value="looks good",
            )
        ]
        assert payload.view["callback_id"] == "feedback"
        assert payload.view["id"] == "V123"

    def test_parses_view_closed_payloads(self):
        raw = {
            "enterprise": {"id": "E123"},
            "team": None,
            "type": "view_closed",
            "user": {"id": "U123"},
            "view": {"id": "V123"},
        }

        payload = parse_slack_webhook_body(
            urlencode({"payload": json.dumps(raw)}),
            content_type="application/x-www-form-urlencoded",
        )

        assert payload.kind == "view_closed"
        assert payload.enterprise_id == "E123"
        assert payload.team_id is None
        assert payload.user == SlackUser(id="U123")
        assert payload.user_id == "U123"
        assert payload.view == {"id": "V123"}

    def test_infers_file_types_from_mimetype(self):
        """``inferFileType`` (parse.ts:368) maps the mimetype prefix to a
        coarse media kind, defaulting to ``file`` when absent/unknown."""
        event_files = [
            {"id": "F1", "mimetype": "image/png"},
            {"id": "F2", "mimetype": "video/mp4"},
            {"id": "F3", "mimetype": "audio/mpeg"},
            {"id": "F4", "mimetype": "application/pdf"},
            {"id": "F5"},
        ]
        payload = parse_slack_webhook_body(
            json.dumps(
                {
                    "event": {
                        "channel": "C123",
                        "files": event_files,
                        "text": "see attached",
                        "ts": "1710000000.000002",
                        "type": "app_mention",
                        "user": "U123",
                    },
                    "type": "event_callback",
                }
            ),
            content_type="application/json",
        )

        assert [f.type for f in payload.files] == ["image", "video", "audio", "file", "file"]

    def test_files_default_to_empty_list_when_absent(self):
        payload = parse_slack_webhook_body(
            json.dumps(
                {
                    "event": {
                        "channel": "C123",
                        "text": "hello",
                        "ts": "1710000000.000002",
                        "type": "app_mention",
                        "user": "U123",
                    },
                    "type": "event_callback",
                }
            ),
            content_type="application/json",
        )

        assert payload.files == []

    def test_view_values_surface_selected_option_for_select_inputs(self):
        """parseViewValues (parse.ts:411) extracts the selected option's
        label/value for static-select inputs."""
        raw = {
            "team": {"id": "T123"},
            "type": "view_submission",
            "user": {"id": "U123"},
            "view": {
                "id": "V123",
                "state": {
                    "values": {
                        "priority_block": {
                            "priority": {
                                "type": "static_select",
                                "selected_option": {
                                    "text": {"text": "High", "type": "plain_text"},
                                    "value": "high",
                                },
                            }
                        }
                    }
                },
            },
        }

        payload = parse_slack_webhook_body(
            urlencode({"payload": json.dumps(raw)}),
            content_type="application/x-www-form-urlencoded",
        )

        assert payload.values == [
            SlackViewStateValue(
                action_id="priority",
                block_id="priority_block",
                raw=payload.values[0].raw,
                selected_option_label="High",
                selected_option_value="high",
                type="static_select",
                value=None,
            )
        ]

    def test_returns_unsupported_for_valid_but_unsupported_payloads(self):
        payload = parse_slack_webhook_body(
            json.dumps(
                {
                    "event": {"type": "reaction_added"},
                    "type": "event_callback",
                }
            )
        )

        assert payload.kind == "unsupported"
        assert payload.raw == {"event": {"type": "reaction_added"}, "type": "event_callback"}
        assert payload.retry is None
        assert payload.type == "reaction_added"

    def test_throws_a_parse_error_for_invalid_json(self):
        with pytest.raises(SlackWebhookParseError):
            parse_slack_webhook_body("{", content_type="application/json")


class TestReadSlackWebhook:
    @pytest.mark.asyncio
    async def test_verifies_and_parses_requests(self):
        body = json.dumps({"challenge": "challenge-value", "type": "url_verification"})

        payload = await read_slack_webhook(_request(body), signing_secret=SECRET, now=_now)

        assert payload.kind == "url_verification"
        assert payload.challenge == "challenge-value"

    @pytest.mark.asyncio
    async def test_rejects_tampered_request_bodies(self):
        request = _request(json.dumps({"type": "url_verification", "challenge": "x"}))
        request._body = json.dumps({"type": "url_verification", "challenge": "evil"})

        with pytest.raises(SlackWebhookVerificationError):
            await read_slack_webhook(request, signing_secret=SECRET, now=_now)


class TestWebhookImportBoundary:
    def test_does_not_import_the_full_adapter_or_runtime_packages(self):
        """Importing the webhook subpath must not pull in slack_sdk, HTTP
        clients, or the high-level adapter module (port of upstream's
        ``webhook/boundary.test.ts``)."""
        code = (
            "import sys\n"
            "import chat_sdk.adapters.slack.webhook\n"
            "forbidden = [\n"
            "    'slack_sdk',\n"
            "    'httpx',\n"
            "    'aiohttp',\n"
            "    'chat_sdk.adapters.slack.adapter',\n"
            "]\n"
            "loaded = [name for name in forbidden if name in sys.modules]\n"
            "assert not loaded, f'webhook subpath imported runtime modules: {loaded}'\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
