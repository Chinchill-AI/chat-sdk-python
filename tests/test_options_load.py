"""Slack adapter tests for ``block_suggestion`` dispatch.

Ports the two Slack adapter-level options-load tests from upstream
``packages/adapter-slack/src/index.test.ts`` (lines 703, 750). The
chat-orchestrator tests live alongside the other 1:1 ports in
``test_chat_faithful.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import quote_plus

import pytest

try:
    from chat_sdk.adapters.slack.adapter import SlackAdapter
    from chat_sdk.adapters.slack.types import SlackAdapterConfig

    _SLACK_AVAILABLE = True
except ImportError:
    _SLACK_AVAILABLE = False

pytestmark = pytest.mark.skipif(not _SLACK_AVAILABLE, reason="Slack adapter import failed")


# ---------------------------------------------------------------------------
# Helpers (mirrors pattern from tests/test_slack_webhook_extended.py)
# ---------------------------------------------------------------------------


def _slack_signature(body: str, secret: str, timestamp: int | None = None) -> tuple[str, str]:
    ts = str(timestamp or int(time.time()))
    sig_base = f"v0:{ts}:{body}"
    sig = "v0=" + hmac.new(secret.encode(), sig_base.encode(), hashlib.sha256).hexdigest()
    return ts, sig


class _FakeRequest:
    def __init__(self, body: str, headers: dict[str, str] | None = None):
        self.body = body.encode("utf-8")
        self.headers = headers or {}

    async def text(self) -> str:
        return self.body.decode("utf-8")


def _make_signed_request(
    body: str,
    secret: str = "test-signing-secret",
    content_type: str = "application/x-www-form-urlencoded",
) -> _FakeRequest:
    ts, sig = _slack_signature(body, secret)
    return _FakeRequest(
        body,
        {
            "x-slack-request-timestamp": ts,
            "x-slack-signature": sig,
            "content-type": content_type,
        },
    )


def _make_mock_chat() -> MagicMock:
    """Minimal ChatInstance mock for block_suggestion dispatch."""
    chat = MagicMock()
    chat.process_message = MagicMock()
    chat.handle_incoming_message = AsyncMock()
    chat.process_reaction = MagicMock()
    chat.process_action = MagicMock()
    chat.process_modal_submit = AsyncMock()
    chat.process_modal_close = MagicMock()
    chat.process_slash_command = MagicMock()
    chat.process_options_load = AsyncMock()
    chat.get_state = MagicMock(return_value=MagicMock())
    chat.get_user_name = MagicMock(return_value="test-bot")
    chat.get_logger = MagicMock(return_value=MagicMock())
    return chat


def _make_interactive_request(payload: dict[str, Any]) -> _FakeRequest:
    body = f"payload={quote_plus(json.dumps(payload))}"
    return _make_signed_request(body)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSlackBlockSuggestion:
    """Port of ``adapter-slack/src/index.test.ts`` line 703 + 750."""

    # TS: "handles block_suggestion payloads and returns options JSON"
    @pytest.mark.asyncio
    async def test_handles_block_suggestion_payloads_and_returns_options_json(self):
        chat = _make_mock_chat()
        chat.process_options_load = AsyncMock(return_value=[{"label": "Maria Garcia", "value": "person_123"}])

        adapter = SlackAdapter(SlackAdapterConfig(signing_secret="test-signing-secret", bot_token="xoxb-test"))
        await adapter.initialize(chat)

        payload = {
            "type": "block_suggestion",
            "team": {"id": "T123"},
            "user": {"id": "U123", "username": "testuser", "name": "Test User"},
            "action_id": "person_select",
            "block_id": "person_block",
            "value": "mar",
        }
        req = _make_interactive_request(payload)

        response = await adapter.handle_webhook(req)

        assert response["status"] == 200
        headers = response.get("headers") or {}
        content_type = headers.get("Content-Type") or headers.get("content-type") or ""
        assert "application/json" in content_type

        # Verify the event handed to the chat carried the right fields.
        chat.process_options_load.assert_awaited_once()
        call_args, _ = chat.process_options_load.call_args
        event = call_args[0]
        assert event.action_id == "person_select"
        assert event.query == "mar"
        assert event.user.user_id == "U123"

        parsed = json.loads(response["body"])
        assert parsed == {
            "options": [
                {
                    "text": {"type": "plain_text", "text": "Maria Garcia"},
                    "value": "person_123",
                }
            ]
        }

    # TS: "returns empty options when block_suggestion handler exceeds 2.5s budget"
    @pytest.mark.asyncio
    async def test_returns_empty_options_when_block_suggestion_handler_exceeds_budget(self):
        from chat_sdk.types import WebhookOptions

        chat = _make_mock_chat()

        slow_done = asyncio.Event()

        async def _slow_handler(event: Any, options: Any = None):
            # Handler that runs past the (patched 50ms) budget. The
            # adapter should time out and return [] before this completes.
            # Sleep is kept short so the orphaned task can be awaited in
            # the finally block without lingering and flaking other tests.
            try:
                await asyncio.sleep(0.5)
                return [{"label": "Too late", "value": "late"}]
            finally:
                slow_done.set()

        chat.process_options_load = AsyncMock(side_effect=_slow_handler)

        adapter = SlackAdapter(SlackAdapterConfig(signing_secret="test-signing-secret", bot_token="xoxb-test"))
        await adapter.initialize(chat)

        # Capture the shielded handler task via wait_until so we can
        # clean it up in finally. Same pattern as
        # test_timed_out_task_is_registered_with_wait_until.
        registered: list[Any] = []

        def _wait_until(awaitable: Any) -> None:
            registered.append(awaitable)

        options = WebhookOptions(wait_until=_wait_until)

        # Patch the module-level timeout so the test runs quickly (real
        # 2.5s budget is exercised in fidelity-level production code).
        import chat_sdk.adapters.slack.adapter as slack_adapter_mod

        original_timeout = slack_adapter_mod.OPTIONS_LOAD_TIMEOUT_MS
        slack_adapter_mod.OPTIONS_LOAD_TIMEOUT_MS = 50  # 50 ms for test speed
        try:
            payload = {
                "type": "block_suggestion",
                "team": {"id": "T123"},
                "user": {"id": "U123", "username": "testuser", "name": "Test User"},
                "action_id": "person_select",
                "block_id": "person_block",
                "value": "mar",
            }
            req = _make_interactive_request(payload)

            response = await adapter.handle_webhook(req, options)

            assert response["status"] == 200
            parsed = json.loads(response["body"])
            assert parsed == {"options": []}

            # The orphaned handler task must still be running (shielded),
            # not cancelled — that's how upstream logs late errors.
            assert not slow_done.is_set()
        finally:
            slack_adapter_mod.OPTIONS_LOAD_TIMEOUT_MS = original_timeout
            # Await the still-running slow task so it doesn't linger
            # past this test and flake adjacent async tests.
            if registered:
                await asyncio.gather(*registered, return_exceptions=True)

    # On timeout, the orphaned handler task must be registered with
    # WebhookOptions.wait_until so serverless runtimes (e.g. Vercel) keep
    # it alive until the late-error logging callback fires.
    @pytest.mark.asyncio
    async def test_timed_out_task_is_registered_with_wait_until(self):
        from chat_sdk.types import WebhookOptions

        chat = _make_mock_chat()

        async def _slow_handler(event: Any, options: Any = None):
            await asyncio.sleep(5.0)
            return []

        chat.process_options_load = AsyncMock(side_effect=_slow_handler)

        adapter = SlackAdapter(SlackAdapterConfig(signing_secret="test-signing-secret", bot_token="xoxb-test"))
        await adapter.initialize(chat)

        registered: list[Any] = []

        def _wait_until(awaitable: Any) -> None:
            registered.append(awaitable)

        options = WebhookOptions(wait_until=_wait_until)

        import chat_sdk.adapters.slack.adapter as slack_adapter_mod

        original_timeout = slack_adapter_mod.OPTIONS_LOAD_TIMEOUT_MS
        slack_adapter_mod.OPTIONS_LOAD_TIMEOUT_MS = 50
        try:
            payload = {
                "type": "block_suggestion",
                "team": {"id": "T123"},
                "user": {"id": "U123", "username": "testuser", "name": "Test User"},
                "action_id": "person_select",
                "block_id": "person_block",
                "value": "mar",
            }
            req = _make_interactive_request(payload)

            response = await adapter.handle_webhook(req, options)

            assert response["status"] == 200
            parsed = json.loads(response["body"])
            assert parsed == {"options": []}

            # The timed-out handler task must be handed off to wait_until
            # so serverless runtimes don't kill it prematurely.
            assert len(registered) == 1
            assert isinstance(registered[0], asyncio.Task)
            assert not registered[0].done()
        finally:
            slack_adapter_mod.OPTIONS_LOAD_TIMEOUT_MS = original_timeout
            # Cancel the still-running slow task so it doesn't leak.
            if registered and not registered[0].done():
                registered[0].cancel()

    # If the caller-supplied wait_until raises (e.g. an adapter bug or a
    # serverless runtime that rejects registration), the timeout branch
    # must still return the empty-options HTTP 200 fallback rather than
    # surfacing the exception to the webhook.
    # TS: "handles block_suggestion with option_groups response"
    # (vercel/chat#410, packages/adapter-slack/src/index.test.ts)
    @pytest.mark.asyncio
    async def test_handles_block_suggestion_with_option_groups_response(self):
        chat = _make_mock_chat()
        chat.process_options_load = AsyncMock(
            return_value=[
                {
                    "label": "Recent",
                    "options": [{"label": "Alice", "value": "u1"}],
                },
                {
                    "label": "All",
                    "options": [{"label": "Bob", "value": "u2"}],
                },
            ]
        )

        adapter = SlackAdapter(SlackAdapterConfig(signing_secret="test-signing-secret", bot_token="xoxb-test"))
        await adapter.initialize(chat)

        payload = {
            "type": "block_suggestion",
            "team": {"id": "T123"},
            "user": {"id": "U123", "username": "testuser", "name": "Test User"},
            "action_id": "user_select",
            "block_id": "user_block",
            "value": "",
        }
        req = _make_interactive_request(payload)

        response = await adapter.handle_webhook(req)

        assert response["status"] == 200
        parsed = json.loads(response["body"])
        assert parsed == {
            "option_groups": [
                {
                    "label": {"type": "plain_text", "text": "Recent"},
                    "options": [
                        {"text": {"type": "plain_text", "text": "Alice"}, "value": "u1"},
                    ],
                },
                {
                    "label": {"type": "plain_text", "text": "All"},
                    "options": [
                        {"text": {"type": "plain_text", "text": "Bob"}, "value": "u2"},
                    ],
                },
            ]
        }

    # Slack spec: option_groups and options are mutually exclusive in the
    # response body. Verify the adapter never emits both.
    @pytest.mark.asyncio
    async def test_option_groups_and_options_are_mutually_exclusive(self):
        chat = _make_mock_chat()
        chat.process_options_load = AsyncMock(
            return_value=[
                {
                    "label": "Recent",
                    "options": [{"label": "Alice", "value": "u1"}],
                },
            ]
        )

        adapter = SlackAdapter(SlackAdapterConfig(signing_secret="test-signing-secret", bot_token="xoxb-test"))
        await adapter.initialize(chat)

        payload = {
            "type": "block_suggestion",
            "team": {"id": "T123"},
            "user": {"id": "U123", "username": "testuser", "name": "Test User"},
            "action_id": "user_select",
            "block_id": "user_block",
            "value": "",
        }
        req = _make_interactive_request(payload)

        response = await adapter.handle_webhook(req)

        parsed = json.loads(response["body"])
        # Slack rejects payloads that include both. The adapter must pick one.
        assert "option_groups" in parsed
        assert "options" not in parsed

    # Slack spec: group label is plain_text, max 75 chars.
    @pytest.mark.asyncio
    async def test_option_groups_label_truncated_to_75_chars(self):
        chat = _make_mock_chat()
        long_label = "x" * 200
        chat.process_options_load = AsyncMock(
            return_value=[
                {
                    "label": long_label,
                    "options": [{"label": "A", "value": "a"}],
                }
            ]
        )

        adapter = SlackAdapter(SlackAdapterConfig(signing_secret="test-signing-secret", bot_token="xoxb-test"))
        await adapter.initialize(chat)

        payload = {
            "type": "block_suggestion",
            "team": {"id": "T123"},
            "user": {"id": "U123", "username": "testuser", "name": "Test User"},
            "action_id": "user_select",
            "block_id": "user_block",
            "value": "",
        }
        req = _make_interactive_request(payload)

        response = await adapter.handle_webhook(req)

        parsed = json.loads(response["body"])
        assert parsed["option_groups"][0]["label"]["text"] == "x" * 75

    # Slack spec: max 100 groups, max 100 options per group.
    @pytest.mark.asyncio
    async def test_option_groups_limits_to_100_groups_and_100_options(self):
        chat = _make_mock_chat()
        # 150 groups, each with 150 options — both should be capped to 100.
        chat.process_options_load = AsyncMock(
            return_value=[
                {
                    "label": f"Group {i}",
                    "options": [{"label": f"opt-{i}-{j}", "value": f"v{i}-{j}"} for j in range(150)],
                }
                for i in range(150)
            ]
        )

        adapter = SlackAdapter(SlackAdapterConfig(signing_secret="test-signing-secret", bot_token="xoxb-test"))
        await adapter.initialize(chat)

        payload = {
            "type": "block_suggestion",
            "team": {"id": "T123"},
            "user": {"id": "U123", "username": "testuser", "name": "Test User"},
            "action_id": "user_select",
            "block_id": "user_block",
            "value": "",
        }
        req = _make_interactive_request(payload)

        response = await adapter.handle_webhook(req)
        parsed = json.loads(response["body"])
        assert len(parsed["option_groups"]) == 100
        for group in parsed["option_groups"]:
            assert len(group["options"]) == 100

    # An empty list — common when a handler returns "no results" — must
    # render as ``{"options": []}``, not ``{"option_groups": []}``. Detection
    # must read the first element's shape, not just the outer container.
    @pytest.mark.asyncio
    async def test_empty_result_renders_as_options_not_groups(self):
        chat = _make_mock_chat()
        chat.process_options_load = AsyncMock(return_value=[])

        adapter = SlackAdapter(SlackAdapterConfig(signing_secret="test-signing-secret", bot_token="xoxb-test"))
        await adapter.initialize(chat)

        payload = {
            "type": "block_suggestion",
            "team": {"id": "T123"},
            "user": {"id": "U123", "username": "testuser", "name": "Test User"},
            "action_id": "user_select",
            "block_id": "user_block",
            "value": "",
        }
        req = _make_interactive_request(payload)

        response = await adapter.handle_webhook(req)

        parsed = json.loads(response["body"])
        assert parsed == {"options": []}

    # Per-option ``description`` should round-trip through both flat and
    # grouped result shapes (covers the shared selectOptionToSlackOption
    # helper extracted in vercel/chat#410).
    @pytest.mark.asyncio
    async def test_grouped_options_include_description(self):
        chat = _make_mock_chat()
        chat.process_options_load = AsyncMock(
            return_value=[
                {
                    "label": "Recent",
                    "options": [
                        {"label": "Alice", "value": "u1", "description": "Engineering"},
                    ],
                },
            ]
        )

        adapter = SlackAdapter(SlackAdapterConfig(signing_secret="test-signing-secret", bot_token="xoxb-test"))
        await adapter.initialize(chat)

        payload = {
            "type": "block_suggestion",
            "team": {"id": "T123"},
            "user": {"id": "U123", "username": "testuser", "name": "Test User"},
            "action_id": "user_select",
            "block_id": "user_block",
            "value": "",
        }
        req = _make_interactive_request(payload)

        response = await adapter.handle_webhook(req)
        parsed = json.loads(response["body"])
        assert parsed["option_groups"][0]["options"][0] == {
            "text": {"type": "plain_text", "text": "Alice"},
            "value": "u1",
            "description": {"type": "plain_text", "text": "Engineering"},
        }

    @pytest.mark.asyncio
    async def test_timeout_falls_back_when_wait_until_raises(self):
        from chat_sdk.types import WebhookOptions

        chat = _make_mock_chat()

        slow_task_ref: list[Any] = []

        async def _slow_handler(event: Any, options: Any = None):
            await asyncio.sleep(5.0)
            return []

        chat.process_options_load = AsyncMock(side_effect=_slow_handler)

        adapter = SlackAdapter(SlackAdapterConfig(signing_secret="test-signing-secret", bot_token="xoxb-test"))
        await adapter.initialize(chat)

        def _raising_wait_until(awaitable: Any) -> None:
            slow_task_ref.append(awaitable)
            raise RuntimeError("runtime refuses to register background task")

        options = WebhookOptions(wait_until=_raising_wait_until)

        import chat_sdk.adapters.slack.adapter as slack_adapter_mod

        original_timeout = slack_adapter_mod.OPTIONS_LOAD_TIMEOUT_MS
        slack_adapter_mod.OPTIONS_LOAD_TIMEOUT_MS = 50
        try:
            payload = {
                "type": "block_suggestion",
                "team": {"id": "T123"},
                "user": {"id": "U123", "username": "testuser", "name": "Test User"},
                "action_id": "person_select",
                "block_id": "person_block",
                "value": "mar",
            }
            req = _make_interactive_request(payload)

            # Must not raise — the guard should swallow the wait_until error
            # and still serve the timeout fallback response.
            response = await adapter.handle_webhook(req, options)

            assert response["status"] == 200
            headers = response.get("headers") or {}
            content_type = headers.get("Content-Type") or headers.get("content-type") or ""
            assert "application/json" in content_type
            parsed = json.loads(response["body"])
            assert parsed == {"options": []}

            # Sanity: wait_until was actually invoked (and raised) on the
            # shielded task — that's what the guard is there to protect.
            assert len(slow_task_ref) == 1
            assert isinstance(slow_task_ref[0], asyncio.Task)
        finally:
            slack_adapter_mod.OPTIONS_LOAD_TIMEOUT_MS = original_timeout
            if slow_task_ref and not slow_task_ref[0].done():
                slow_task_ref[0].cancel()
