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
        chat = _make_mock_chat()

        slow_done = asyncio.Event()

        async def _slow_handler(event: Any, options: Any = None):
            # Handler that runs well past the 2.5s budget. The adapter
            # should time out and return [] before this completes.
            try:
                await asyncio.sleep(5.0)
                return [{"label": "Too late", "value": "late"}]
            finally:
                slow_done.set()

        chat.process_options_load = AsyncMock(side_effect=_slow_handler)

        adapter = SlackAdapter(SlackAdapterConfig(signing_secret="test-signing-secret", bot_token="xoxb-test"))
        await adapter.initialize(chat)

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

            response = await adapter.handle_webhook(req)

            assert response["status"] == 200
            parsed = json.loads(response["body"])
            assert parsed == {"options": []}

            # The orphaned handler task must still be running (shielded),
            # not cancelled — that's how upstream logs late errors.
            assert not slow_done.is_set()
        finally:
            slack_adapter_mod.OPTIONS_LOAD_TIMEOUT_MS = original_timeout
