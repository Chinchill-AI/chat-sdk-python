"""Replay integration tests: callbackUrl handling on buttons and modals.

Port of replay-callback-url.test.ts (4 tests).

When a button or modal carries a ``callback_url``, the SDK POSTs the action
payload to that URL in addition to firing any registered handler. These
tests replay real Slack webhook payloads (from tests/fixtures/replay/)
through the real ``SlackAdapter.handle_webhook()`` into a real ``Chat``,
with the SDK's encoded callback token (``__cb:<hex>``) and a stored modal
context with ``callbackUrl``, then assert the SDK resolves the URL and
POSTs the right shape.

Discord adapter encoding/decoding is covered by unit tests in
``tests/test_discord_cards.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from typing import Any
from unittest.mock import AsyncMock, patch
from urllib.parse import quote

import pytest

from chat_sdk.chat import Chat
from chat_sdk.testing import MockLogger, MockStateAdapter, create_mock_state
from chat_sdk.types import ActionEvent, ChatConfig, ModalSubmitEvent, WebhookOptions
from tests.fixtures.conftest import load_fixture

try:
    from chat_sdk.adapters.slack.adapter import SlackAdapter
    from chat_sdk.adapters.slack.types import SlackAdapterConfig

    _SLACK_OK = True
except ImportError:
    _SLACK_OK = False

CALLBACK_BUTTON_URL = "https://hook.example.com/button-cb"
CALLBACK_MODAL_URL = "https://hook.example.com/modal-cb"
CALLBACK_TOKEN = "abcdef0123456789"
SLACK_SIGNING_SECRET = "test-signing-secret"


# ---------------------------------------------------------------------------
# Slack webhook plumbing (same conventions as tests/test_fixture_replay.py)
# ---------------------------------------------------------------------------


class _FakeRequest:
    """Minimal request-like object for adapter webhook testing."""

    def __init__(self, body: str, headers: dict[str, str] | None = None):
        self._body = body.encode("utf-8")
        self.headers = headers or {}

    async def text(self) -> str:
        return self._body.decode("utf-8")


def _slack_signed_request(body: str, content_type: str = "application/json") -> _FakeRequest:
    ts = str(int(time.time()))
    sig_base = f"v0:{ts}:{body}"
    sig = "v0=" + hmac.new(SLACK_SIGNING_SECRET.encode(), sig_base.encode(), hashlib.sha256).hexdigest()
    return _FakeRequest(
        body,
        {
            "x-slack-request-timestamp": ts,
            "x-slack-signature": sig,
            "content-type": content_type,
        },
    )


def _slack_interactive_request(payload: dict[str, Any]) -> _FakeRequest:
    """Slack sends interactive payloads form-encoded as ``payload=<json>``."""
    body = f"payload={quote(json.dumps(payload))}"
    return _slack_signed_request(body, content_type="application/x-www-form-urlencoded")


class _SlackReplayContext:
    """Real Chat + real SlackAdapter wired together (TS createSlackTestContext)."""

    def __init__(self, fixture: dict[str, Any]):
        self.adapter = SlackAdapter(
            SlackAdapterConfig(
                signing_secret=SLACK_SIGNING_SECRET,
                bot_token="xoxb-test-token",
                bot_user_id=fixture.get("botUserId"),
            )
        )
        self.state: MockStateAdapter = create_mock_state()
        self.chat = Chat(
            ChatConfig(
                user_name=fixture.get("botName", "testbot"),
                adapters={"slack": self.adapter},
                state=self.state,
                logger=MockLogger(),
            )
        )

    async def send_webhook(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send an event-callback webhook and wait for the spawned tasks."""
        tasks: list[Any] = []
        request = _slack_signed_request(json.dumps(payload))
        response = await self.chat.webhooks["slack"](request, WebhookOptions(wait_until=tasks.append))
        await asyncio.gather(*tasks)
        return response

    async def send_interactive(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send a block_actions / view_submission webhook and drain its tasks."""
        tasks: list[Any] = []
        request = _slack_interactive_request(payload)
        response = await self.chat.webhooks["slack"](request, WebhookOptions(wait_until=tasks.append))
        await asyncio.gather(*tasks)
        return response

    async def shutdown(self) -> None:
        await self.chat.shutdown()


# ===========================================================================
# Slack button click with callback token
# ===========================================================================


@pytest.mark.skipif(not _SLACK_OK, reason="Slack adapter not available")
class TestSlackButtonClickWithCallbackToken:
    """describe("Slack button click with callback token")"""

    # it("decodes the token, POSTs the URL, and passes the original value to onAction")
    @pytest.mark.asyncio
    async def test_decodes_the_token_posts_the_url_and_passes_the_original_value_to_onaction(self):
        fixture = load_fixture("actions-reactions/slack.json")
        ctx = _SlackReplayContext(fixture)
        captured: list[ActionEvent] = []

        @ctx.chat.on_mention
        async def on_mention(thread, message, context):
            await thread.subscribe()

        ctx.chat.on_action(lambda event: captured.append(event))

        try:
            with patch("chat_sdk.callback_url._fetch", new=AsyncMock(return_value=(200, "ok"))) as mock_fetch:
                await ctx.send_webhook(fixture["mention"])

                # Pre-populate the callback URL store as the SDK would have
                # done at post time.
                await ctx.state.set(
                    f"chat:callback:{CALLBACK_TOKEN}",
                    {"url": CALLBACK_BUTTON_URL, "originalValue": "order-99"},
                )

                # Synthesize a block_actions payload with the SDK's encoded
                # token as the value.
                action = {
                    **fixture["action"],
                    "actions": [
                        {
                            **fixture["action"]["actions"][0],
                            "action_id": "approve",
                            "value": f"__cb:{CALLBACK_TOKEN}",
                        }
                    ],
                }

                mock_fetch.reset_mock()
                await ctx.send_interactive(action)

            # Handler sees the original value, not the encoded token.
            assert len(captured) == 1
            assert captured[0].action_id == "approve"
            assert captured[0].value == "order-99"

            # The SDK POSTed to the stored callback URL.
            callback_calls = [c for c in mock_fetch.await_args_list if c.args[0] == CALLBACK_BUTTON_URL]
            assert len(callback_calls) == 1

            assert callback_calls[0].kwargs["method"] == "POST"
            body = json.loads(callback_calls[0].kwargs["body"])
            assert body["type"] == "action"
            assert body["actionId"] == "approve"
            assert body["value"] == "order-99"
            assert body["user"]["id"] == "U00FAKEUSER1"
        finally:
            await ctx.shutdown()

    # it("treats an unknown token as a regular value when nothing is stored")
    @pytest.mark.asyncio
    async def test_treats_an_unknown_token_as_a_regular_value_when_nothing_is_stored(self):
        fixture = load_fixture("actions-reactions/slack.json")
        ctx = _SlackReplayContext(fixture)
        captured: list[ActionEvent] = []

        @ctx.chat.on_mention
        async def on_mention(thread, message, context):
            await thread.subscribe()

        ctx.chat.on_action(lambda event: captured.append(event))

        try:
            with patch("chat_sdk.callback_url._fetch", new=AsyncMock(return_value=(200, "ok"))) as mock_fetch:
                await ctx.send_webhook(fixture["mention"])

                action = {
                    **fixture["action"],
                    "actions": [
                        {
                            **fixture["action"]["actions"][0],
                            "action_id": "approve",
                            "value": "__cb:not-a-real-token",
                        }
                    ],
                }

                mock_fetch.reset_mock()
                await ctx.send_interactive(action)

            # Handler still fires; value is preserved verbatim because no
            # store entry exists.
            assert len(captured) == 1
            assert captured[0].value == "__cb:not-a-real-token"

            # No fetch went to any callback URL.
            callback_calls = [
                c for c in mock_fetch.await_args_list if str(c.args[0]).startswith("https://hook.example.com/")
            ]
            assert len(callback_calls) == 0
        finally:
            await ctx.shutdown()


# ===========================================================================
# Slack modal submit with stored callbackUrl
# ===========================================================================


@pytest.mark.skipif(not _SLACK_OK, reason="Slack adapter not available")
class TestSlackModalSubmitWithStoredCallbackUrl:
    """describe("Slack modal submit with stored callbackUrl")"""

    # it("POSTs the form values to the modal callbackUrl after the handler runs")
    @pytest.mark.asyncio
    async def test_posts_the_form_values_to_the_modal_callbackurl_after_the_handler_runs(self):
        fixture = load_fixture("modals/slack.json")
        ctx = _SlackReplayContext(fixture)
        captured: list[ModalSubmitEvent] = []

        ctx.chat.on_modal_submit(lambda event: captured.append(event))

        try:
            context_id = fixture["modalContext"]["contextId"]

            # Simulate what openModal would have stored, including the
            # callbackUrl.
            await ctx.state.set(
                f"modal-context:slack:{context_id}",
                {
                    "thread": fixture["modalContext"]["thread"],
                    "message": fixture["modalContext"]["message"],
                    "callbackUrl": CALLBACK_MODAL_URL,
                },
            )

            with patch("chat_sdk.callback_url._fetch", new=AsyncMock(return_value=(200, "ok"))) as mock_fetch:
                response = await ctx.send_interactive(fixture["viewSubmission"])
                assert response["status"] == 200

            # The user-provided handler still fires.
            assert len(captured) == 1
            assert captured[0].callback_id == "feedback_form"

            # SDK POSTed the modal_submit payload to the stored URL.
            callback_calls = [c for c in mock_fetch.await_args_list if c.args[0] == CALLBACK_MODAL_URL]
            assert len(callback_calls) == 1

            assert callback_calls[0].kwargs["method"] == "POST"
            body = json.loads(callback_calls[0].kwargs["body"])
            assert body["type"] == "modal_submit"
            assert body["callbackId"] == "feedback_form"
            assert body["values"] == {
                "message": "Hello!",
                "category": "feature",
                "email": "user@example.com",
            }
        finally:
            await ctx.shutdown()

    # it("does not POST when the modal context lacks a callbackUrl")
    @pytest.mark.asyncio
    async def test_does_not_post_when_the_modal_context_lacks_a_callbackurl(self):
        fixture = load_fixture("modals/slack.json")
        ctx = _SlackReplayContext(fixture)
        captured: list[ModalSubmitEvent] = []

        ctx.chat.on_modal_submit(lambda event: captured.append(event))

        try:
            context_id = fixture["modalContext"]["contextId"]

            # Modal context exists but has no callbackUrl -- the existing flow.
            await ctx.state.set(
                f"modal-context:slack:{context_id}",
                {
                    "thread": fixture["modalContext"]["thread"],
                    "message": fixture["modalContext"]["message"],
                },
            )

            with patch("chat_sdk.callback_url._fetch", new=AsyncMock(return_value=(200, "ok"))) as mock_fetch:
                await ctx.send_interactive(fixture["viewSubmission"])

            # The handler ran against the replayed payload, but nothing was
            # POSTed to any callback URL.
            assert len(captured) == 1
            callback_calls = [
                c for c in mock_fetch.await_args_list if str(c.args[0]).startswith("https://hook.example.com/")
            ]
            assert len(callback_calls) == 0
        finally:
            await ctx.shutdown()
