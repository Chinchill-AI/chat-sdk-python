"""Tests for Slack adapter webhook handling, thread IDs, message parsing, and API operations.

Port of packages/adapter-slack/src/index.test.ts.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests._slack_file_transport import FakeFileResponse, FakeFileTransport

try:
    from chat_sdk.adapters.slack.adapter import SlackAdapter
    from chat_sdk.adapters.slack.types import SlackAdapterConfig, SlackInstallation, SlackThreadId
    from chat_sdk.shared.errors import ValidationError

    _SLACK_AVAILABLE = True
except ImportError:
    _SLACK_AVAILABLE = False

pytestmark = pytest.mark.skipif(not _SLACK_AVAILABLE, reason="Slack adapter import failed")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_adapter(**overrides: Any) -> SlackAdapter:
    config = SlackAdapterConfig(
        signing_secret=overrides.pop("signing_secret", "test-signing-secret"),
        bot_token=overrides.pop("bot_token", "xoxb-test-token"),
        **overrides,
    )
    return SlackAdapter(config)


def _slack_signature(body: str, secret: str, timestamp: int | None = None) -> tuple[str, str]:
    """Compute Slack request signature. Returns (timestamp_str, signature)."""
    ts = str(timestamp or int(time.time()))
    sig_base = f"v0:{ts}:{body}"
    sig = "v0=" + hmac.new(secret.encode(), sig_base.encode(), hashlib.sha256).hexdigest()
    return ts, sig


class _FakeRequest:
    """Minimal request-like object for adapter webhook testing."""

    def __init__(self, body: str, headers: dict[str, str] | None = None, url: str = ""):
        self.body = body.encode("utf-8")
        self.headers = headers or {}
        self.url = url

    async def text(self) -> str:
        return self.body.decode("utf-8")


def _make_signed_request(
    body: str,
    secret: str = "test-signing-secret",
    content_type: str = "application/json",
    timestamp_offset: int = 0,
) -> _FakeRequest:
    ts, sig = _slack_signature(body, secret, int(time.time()) + timestamp_offset)
    return _FakeRequest(
        body,
        {
            "x-slack-request-timestamp": ts,
            "x-slack-signature": sig,
            "content-type": content_type,
        },
    )


def _make_mock_state() -> MagicMock:
    cache: dict[str, Any] = {}
    state = MagicMock()
    state.get = AsyncMock(side_effect=lambda k: cache.get(k))
    state.set = AsyncMock(side_effect=lambda k, v, *a, **kw: cache.__setitem__(k, v))
    state.delete = AsyncMock(side_effect=lambda k: cache.pop(k, None))
    state.append_to_list = AsyncMock()
    state.get_list = AsyncMock(return_value=[])
    state._cache = cache
    return state


def _make_mock_chat(state: MagicMock) -> MagicMock:
    chat = MagicMock()
    chat.process_message = MagicMock()
    chat.handle_incoming_message = AsyncMock()
    chat.process_reaction = MagicMock()
    chat.process_action = MagicMock()
    chat.process_modal_submit = AsyncMock()
    chat.process_modal_close = MagicMock()
    chat.process_slash_command = MagicMock()
    chat.process_member_joined_channel = MagicMock()
    chat.process_message_updated = MagicMock()
    chat.process_message_deleted = MagicMock()
    chat.get_state = MagicMock(return_value=state)
    chat.get_user_name = MagicMock(return_value="test-bot")
    chat.get_logger = MagicMock(return_value=MagicMock())
    return chat


# ---------------------------------------------------------------------------
# Factory function tests
# ---------------------------------------------------------------------------


class TestCreateSlackAdapter:
    def test_creates_instance(self):
        adapter = _make_adapter()
        assert isinstance(adapter, SlackAdapter)
        assert adapter.name == "slack"

    def test_default_user_name(self):
        adapter = _make_adapter()
        assert adapter.user_name == "bot"

    def test_custom_user_name(self):
        adapter = _make_adapter(user_name="custombot")
        assert adapter.user_name == "custombot"

    def test_stores_bot_user_id(self):
        adapter = _make_adapter(bot_user_id="U12345")
        assert adapter.bot_user_id == "U12345"


# ---------------------------------------------------------------------------
# Thread ID encoding / decoding
# ---------------------------------------------------------------------------


class TestThreadIdEncoding:
    def test_encode(self):
        adapter = _make_adapter()
        tid = adapter.encode_thread_id(SlackThreadId(channel="C12345", thread_ts="1234567890.123456"))
        assert tid == "slack:C12345:1234567890.123456"

    def test_encode_empty_thread_ts(self):
        adapter = _make_adapter()
        tid = adapter.encode_thread_id(SlackThreadId(channel="C12345", thread_ts=""))
        assert tid == "slack:C12345:"

    def test_decode(self):
        adapter = _make_adapter()
        result = adapter.decode_thread_id("slack:C12345:1234567890.123456")
        assert result.channel == "C12345"
        assert result.thread_ts == "1234567890.123456"

    def test_decode_empty_thread_ts(self):
        adapter = _make_adapter()
        result = adapter.decode_thread_id("slack:C12345:")
        assert result.channel == "C12345"
        assert result.thread_ts == ""

    def test_decode_channel_only(self):
        adapter = _make_adapter()
        result = adapter.decode_thread_id("slack:C12345")
        assert result.channel == "C12345"
        assert result.thread_ts == ""

    def test_decode_invalid_raises(self):
        adapter = _make_adapter()
        with pytest.raises(ValidationError):
            adapter.decode_thread_id("invalid")
        with pytest.raises(ValidationError):
            adapter.decode_thread_id("slack")
        with pytest.raises(ValidationError):
            adapter.decode_thread_id("teams:C12345:123")
        with pytest.raises(ValidationError):
            adapter.decode_thread_id("slack:A:B:C:D")


# ---------------------------------------------------------------------------
# isDM
# ---------------------------------------------------------------------------


class TestIsDM:
    def test_dm_channel(self):
        adapter = _make_adapter()
        assert adapter.is_dm("slack:D12345:1234567890.123456") is True

    def test_public_channel(self):
        adapter = _make_adapter()
        assert adapter.is_dm("slack:C12345:1234567890.123456") is False

    def test_private_channel(self):
        adapter = _make_adapter()
        assert adapter.is_dm("slack:G12345:1234567890.123456") is False


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


class TestSignatureVerification:
    @pytest.mark.asyncio
    async def test_rejects_missing_timestamp(self):
        adapter = _make_adapter()
        body = json.dumps({"type": "url_verification"})
        req = _FakeRequest(body, {"x-slack-signature": "v0=invalid", "content-type": "application/json"})
        response = await adapter.handle_webhook(req)
        assert response["status"] == 401

    @pytest.mark.asyncio
    async def test_rejects_missing_signature(self):
        adapter = _make_adapter()
        body = json.dumps({"type": "url_verification"})
        req = _FakeRequest(
            body, {"x-slack-request-timestamp": str(int(time.time())), "content-type": "application/json"}
        )
        response = await adapter.handle_webhook(req)
        assert response["status"] == 401

    @pytest.mark.asyncio
    async def test_rejects_invalid_signature(self):
        adapter = _make_adapter()
        body = json.dumps({"type": "url_verification"})
        req = _FakeRequest(
            body,
            {
                "x-slack-request-timestamp": str(int(time.time())),
                "x-slack-signature": "v0=invalid",
                "content-type": "application/json",
            },
        )
        response = await adapter.handle_webhook(req)
        assert response["status"] == 401

    @pytest.mark.asyncio
    async def test_rejects_old_timestamp(self):
        adapter = _make_adapter()
        body = json.dumps({"type": "url_verification"})
        req = _make_signed_request(body, timestamp_offset=-400)
        response = await adapter.handle_webhook(req)
        assert response["status"] == 401

    @pytest.mark.asyncio
    async def test_accepts_valid_signature(self):
        adapter = _make_adapter()
        body = json.dumps({"type": "url_verification", "challenge": "test-challenge"})
        req = _make_signed_request(body)
        response = await adapter.handle_webhook(req)
        assert response["status"] == 200


# ---------------------------------------------------------------------------
# URL verification
# ---------------------------------------------------------------------------


class TestURLVerification:
    @pytest.mark.asyncio
    async def test_responds_to_challenge(self):
        adapter = _make_adapter()
        body = json.dumps({"type": "url_verification", "challenge": "test-challenge-123"})
        req = _make_signed_request(body)
        response = await adapter.handle_webhook(req)
        assert response["status"] == 200
        resp_body = response.get("body", "")
        parsed = json.loads(resp_body) if isinstance(resp_body, str) else resp_body
        assert parsed == {"challenge": "test-challenge-123"}


# ---------------------------------------------------------------------------
# Event callbacks
# ---------------------------------------------------------------------------


class TestEventCallbacks:
    def _make_event_request(self, event_data: dict[str, Any]) -> _FakeRequest:
        body = json.dumps({"type": "event_callback", "event": event_data})
        return _make_signed_request(body)

    @pytest.mark.asyncio
    async def test_handles_message_events(self):
        adapter = _make_adapter()
        req = self._make_event_request(
            {
                "type": "message",
                "user": "U123",
                "channel": "C456",
                "text": "Hello world",
                "ts": "1234567890.123456",
            }
        )
        response = await adapter.handle_webhook(req)
        assert response["status"] == 200

    @pytest.mark.asyncio
    async def test_handles_app_mention_events(self):
        adapter = _make_adapter()
        req = self._make_event_request(
            {
                "type": "app_mention",
                "user": "U123",
                "channel": "C456",
                "text": "<@U_BOT> hello",
                "ts": "1234567890.123456",
            }
        )
        response = await adapter.handle_webhook(req)
        assert response["status"] == 200


# ---------------------------------------------------------------------------
# Interactive payloads (block_actions)
# ---------------------------------------------------------------------------


class TestInteractivePayloads:
    def _make_interactive_req(self, payload: dict[str, Any]) -> _FakeRequest:
        payload_str = json.dumps(payload)
        body = f"payload={payload_str}"
        return _make_signed_request(body, content_type="application/x-www-form-urlencoded")

    @pytest.mark.asyncio
    async def test_handles_block_actions(self):
        adapter = _make_adapter()
        req = self._make_interactive_req(
            {
                "type": "block_actions",
                "user": {"id": "U123", "username": "testuser", "name": "Test User"},
                "container": {"type": "message", "message_ts": "1234567890.123456", "channel_id": "C456"},
                "channel": {"id": "C456", "name": "general"},
                "message": {"ts": "1234567890.123456", "thread_ts": "1234567890.000000"},
                "actions": [{"type": "button", "action_id": "approve_btn", "value": "approved"}],
            }
        )
        response = await adapter.handle_webhook(req)
        assert response["status"] == 200

    @pytest.mark.asyncio
    async def test_dm_block_action_does_not_thread(self):
        """A click on a top-level DM message must resolve to a DM-root thread_id.

        Regression: _handle_block_actions fell back to message_ts for thread_ts
        in DMs, so HITL approval result cards posted via event.thread.post became
        phantom "1 reply" threads in the DM. DMs have no threads — the ActionEvent
        must carry an empty thread_ts, mirroring _handle_message_event.
        """
        adapter = _make_adapter()
        chat = _make_mock_chat(_make_mock_state())
        await adapter.initialize(chat)

        req = self._make_interactive_req(
            {
                "type": "block_actions",
                "user": {"id": "U123", "username": "testuser", "name": "Test User"},
                "container": {"type": "message", "message_ts": "1234567890.123456", "channel_id": "D456"},
                "channel": {"id": "D456", "name": "directmessage"},
                "message": {"ts": "1234567890.123456"},  # top-level DM message: no thread_ts
                "actions": [{"type": "button", "action_id": "approve_btn", "value": "approved"}],
            }
        )
        response = await adapter.handle_webhook(req)
        assert response["status"] == 200

        chat.process_action.assert_called_once()
        action_event = chat.process_action.call_args[0][0]
        decoded = adapter.decode_thread_id(action_event.thread_id)
        assert decoded.channel == "D456"
        assert decoded.thread_ts == "", (
            f"DM block action threaded under {decoded.thread_ts!r}; a top-level DM click "
            "must resolve to a DM-root thread_id (empty thread_ts), else the approval "
            "result card posts as a phantom reply thread."
        )

    @pytest.mark.asyncio
    async def test_channel_block_action_threads_under_clicked_message(self):
        """A click on a top-level channel message must thread under that message.

        Counterpart to the DM case: channels DO have threads, so a button click on
        a top-level (no thread_ts) channel message must fall back to the clicked
        message's own ts so the response card threads under it. The DM-only guard
        must not leak into channels.
        """
        adapter = _make_adapter()
        chat = _make_mock_chat(_make_mock_state())
        await adapter.initialize(chat)

        req = self._make_interactive_req(
            {
                "type": "block_actions",
                "user": {"id": "U123", "username": "testuser", "name": "Test User"},
                "container": {"type": "message", "message_ts": "1234567890.123456", "channel_id": "C456"},
                "channel": {"id": "C456", "name": "general"},
                "message": {"ts": "1234567890.123456"},  # top-level channel message: no thread_ts
                "actions": [{"type": "button", "action_id": "approve_btn", "value": "approved"}],
            }
        )
        response = await adapter.handle_webhook(req)
        assert response["status"] == 200

        chat.process_action.assert_called_once()
        action_event = chat.process_action.call_args[0][0]
        decoded = adapter.decode_thread_id(action_event.thread_id)
        assert decoded.channel == "C456"
        assert decoded.thread_ts == "1234567890.123456", (
            f"channel block action resolved to thread_ts {decoded.thread_ts!r}; a top-level "
            "channel click must thread under the clicked message's own ts, not a DM-root "
            "empty thread_ts."
        )

    @pytest.mark.asyncio
    async def test_returns_400_for_missing_payload(self):
        adapter = _make_adapter()
        req = _make_signed_request("foo=bar", content_type="application/x-www-form-urlencoded")
        response = await adapter.handle_webhook(req)
        assert response["status"] == 400

    @pytest.mark.asyncio
    async def test_returns_400_for_invalid_payload_json(self):
        adapter = _make_adapter()
        req = _make_signed_request("payload=invalid-json", content_type="application/x-www-form-urlencoded")
        response = await adapter.handle_webhook(req)
        assert response["status"] == 400

    @pytest.mark.asyncio
    async def test_handles_view_submission(self):
        adapter = _make_adapter()
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        req = self._make_interactive_req(
            {
                "type": "view_submission",
                "trigger_id": "trigger123",
                "user": {"id": "U123", "username": "testuser"},
                "view": {
                    "id": "V123",
                    "callback_id": "feedback_form",
                    "private_metadata": "thread-context",
                    "state": {"values": {}},
                },
            }
        )
        response = await adapter.handle_webhook(req)
        assert response["status"] == 200

    async def _submit_values(self, state_values: dict[str, Any]) -> dict[str, str]:
        adapter = _make_adapter()
        chat = _make_mock_chat(_make_mock_state())
        chat.process_modal_submit = AsyncMock(return_value=None)
        await adapter.initialize(chat)

        req = self._make_interactive_req(
            {
                "type": "view_submission",
                "trigger_id": "trigger123",
                "user": {"id": "U123", "username": "testuser", "name": "Test User"},
                "view": {"id": "V123", "callback_id": "renewal_form", "state": {"values": state_values}},
            }
        )
        response = await adapter.handle_webhook(req)
        assert response["status"] == 200
        chat.process_modal_submit.assert_awaited_once()
        event = chat.process_modal_submit.await_args.args[0]
        assert event.callback_id == "renewal_form"
        return event.values

    @pytest.mark.asyncio
    async def test_flattens_datepicker_and_number_input_state_into_submitted_values(self):
        values = await self._submit_values(
            {
                "renewal_date": {"renewal_date": {"type": "datepicker", "selected_date": "2026-08-01"}},
                "quantity": {"quantity": {"type": "number_input", "value": "3"}},
            }
        )
        assert values == {"renewal_date": "2026-08-01", "quantity": "3"}

    @pytest.mark.asyncio
    async def test_submits_empty_and_unset_inputs_with_nullish_fallbacks(self):
        # Upstream ``value ?? selected_date ?? selected_option?.value ?? ""``: a cleared
        # text input keeps its "" rather than falling through to the next key.
        values = await self._submit_values(
            {
                "note": {"note": {"type": "plain_text_input", "value": "", "selected_date": "2026-08-01"}},
                "date": {"date": {"type": "datepicker", "selected_date": None}},
                "plan": {"plan": {"type": "static_select", "selected_option": {"value": "pro"}}},
                "none": {"none": {"type": "static_select", "selected_option": None}},
            }
        )
        assert values == {"note": "", "date": "", "plan": "pro", "none": ""}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("selected", ["team", ""])
    async def test_dispatch_action_select_in_a_modal_reaches_on_action_with_the_selected_value(self, selected):
        # A ``dispatch_action`` select inside a modal sends ``block_actions`` from a
        # ``view`` container with no channel; it must still reach process_action.
        adapter = _make_adapter()
        chat = _make_mock_chat(_make_mock_state())
        await adapter.initialize(chat)

        req = self._make_interactive_req(
            {
                "type": "block_actions",
                "trigger_id": "trigger123",
                "user": {"id": "U123", "username": "testuser", "name": "Test User"},
                "container": {"type": "view", "view_id": "V123"},
                "view": {"id": "V123", "callback_id": "permissions"},
                "actions": [
                    {
                        "type": "static_select",
                        "action_id": "scope",
                        "block_id": "scope",
                        "selected_option": {"text": {"type": "plain_text", "text": "Team"}, "value": selected},
                        "value": "stale",
                    }
                ],
            }
        )
        response = await adapter.handle_webhook(req)
        assert response["status"] == 200

        chat.process_action.assert_called_once()
        action_event = chat.process_action.call_args.args[0]
        assert action_event.action_id == "scope"
        # ``selected_option?.value ?? value``: an empty option value is kept, not replaced.
        assert action_event.value == selected
        assert action_event.trigger_id == "trigger123"
        assert action_event.thread_id == ""

    @pytest.mark.asyncio
    async def test_handles_view_closed(self):
        adapter = _make_adapter()
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        req = self._make_interactive_req(
            {
                "type": "view_closed",
                "user": {"id": "U123", "username": "testuser"},
                "view": {"id": "V123", "callback_id": "feedback_form", "private_metadata": "thread-context"},
            }
        )
        response = await adapter.handle_webhook(req)
        assert response["status"] == 200


# ---------------------------------------------------------------------------
# JSON parsing
# ---------------------------------------------------------------------------


class TestJSONParsing:
    @pytest.mark.asyncio
    async def test_returns_400_for_invalid_json(self):
        adapter = _make_adapter()
        req = _make_signed_request("not valid json")
        response = await adapter.handle_webhook(req)
        assert response["status"] == 400


# ---------------------------------------------------------------------------
# parseMessage
# ---------------------------------------------------------------------------


class TestParseMessage:
    def test_basic_message(self):
        adapter = _make_adapter(bot_user_id="U_BOT")
        event = {
            "type": "message",
            "user": "U123",
            "channel": "C456",
            "text": "Hello world",
            "ts": "1234567890.123456",
        }
        msg = adapter.parse_message(event)
        assert msg.id == "1234567890.123456"
        assert msg.text == "Hello world"
        assert msg.author.user_id == "U123"
        assert msg.author.is_bot is False
        assert msg.author.is_me is False

    def test_bot_message(self):
        adapter = _make_adapter(bot_user_id="U_BOT")
        event = {
            "type": "message",
            "bot_id": "B123",
            "channel": "C456",
            "text": "Bot message",
            "ts": "1234567890.123456",
            "subtype": "bot_message",
        }
        msg = adapter.parse_message(event)
        assert msg.author.user_id == "B123"
        assert msg.author.is_bot is True

    def test_detects_self_message(self):
        adapter = _make_adapter(bot_user_id="U_BOT")
        event = {
            "type": "message",
            "user": "U_BOT",
            "channel": "C456",
            "text": "Self message",
            "ts": "1234567890.123456",
        }
        msg = adapter.parse_message(event)
        assert msg.author.is_me is True

    def test_message_with_thread_ts(self):
        adapter = _make_adapter(bot_user_id="U_BOT")
        event = {
            "type": "message",
            "user": "U123",
            "channel": "C456",
            "text": "Thread reply",
            "ts": "1234567891.123456",
            "thread_ts": "1234567890.123456",
        }
        msg = adapter.parse_message(event)
        assert msg.thread_id == "slack:C456:1234567890.123456"

    def test_message_with_files(self):
        adapter = _make_adapter(bot_user_id="U_BOT")
        event = {
            "type": "message",
            "user": "U123",
            "channel": "C456",
            "text": "Message with file",
            "ts": "1234567890.123456",
            "files": [
                {
                    "id": "F123",
                    "mimetype": "image/png",
                    "url_private": "https://files.slack.com/file.png",
                    "name": "image.png",
                    "size": 12345,
                    "original_w": 800,
                    "original_h": 600,
                }
            ],
        }
        msg = adapter.parse_message(event)
        assert len(msg.attachments) == 1
        assert msg.attachments[0].type == "image"
        assert msg.attachments[0].name == "image.png"
        assert msg.attachments[0].mime_type == "image/png"

    def test_different_file_types(self):
        adapter = _make_adapter(bot_user_id="U_BOT")

        def make_event(mimetype: str) -> dict:
            return {
                "type": "message",
                "user": "U123",
                "channel": "C456",
                "text": "",
                "ts": "1234567890.123456",
                "files": [{"id": "F123", "mimetype": mimetype, "url_private": "https://example.com"}],
            }

        assert adapter.parse_message(make_event("image/jpeg")).attachments[0].type == "image"
        assert adapter.parse_message(make_event("video/mp4")).attachments[0].type == "video"
        assert adapter.parse_message(make_event("audio/mpeg")).attachments[0].type == "audio"
        assert adapter.parse_message(make_event("application/pdf")).attachments[0].type == "file"

    def test_attachment_captures_team_id_in_fetch_metadata(self):
        """The team_id from the event is stored on fetch_metadata for later rehydration."""
        adapter = _make_adapter(bot_user_id="U_BOT")
        event = {
            "type": "message",
            "user": "U123",
            "channel": "C456",
            "text": "",
            "ts": "1234567890.123456",
            "team": "T_TEAM_42",
            "files": [
                {
                    "id": "F123",
                    "mimetype": "image/png",
                    "url_private": "https://files.slack.com/img.png",
                }
            ],
        }
        msg = adapter.parse_message(event)
        assert msg.attachments[0].fetch_metadata == {
            "url": "https://files.slack.com/img.png",
            "teamId": "T_TEAM_42",
        }


# ---------------------------------------------------------------------------
# rehydrate_attachment (port of TS describe("rehydrateAttachment"))
# ---------------------------------------------------------------------------


class TestRehydrateAttachment:
    """Port of TS ``describe("rehydrateAttachment")`` in adapter-slack/src/index.test.ts."""

    # TS: "should resolve token from installation when teamId is present"
    @pytest.mark.asyncio
    async def test_should_resolve_token_from_installation_when_teamid_is_present(self):
        from chat_sdk.types import Attachment

        adapter = _make_adapter(
            signing_secret="test-secret",
            bot_token=None,
            client_id="client-id",
            client_secret="client-secret",
        )
        state = _make_mock_state()
        await adapter.initialize(_make_mock_chat(state))

        await adapter.set_installation(
            "T_MULTI_1",
            SlackInstallation(
                bot_token="xoxb-multi-workspace-token",
                bot_user_id="U_BOT_MULTI",
            ),
        )

        # Stub the download transport — assert the tenant token is forwarded.
        transport = FakeFileTransport(FakeFileResponse(b"workspace-bytes"))
        adapter._file_transport = transport

        rehydrated = adapter.rehydrate_attachment(
            Attachment(
                type="image",
                url="https://files.slack.com/img.png",
                fetch_metadata={
                    "url": "https://files.slack.com/img.png",
                    "teamId": "T_MULTI_1",
                },
            )
        )

        assert rehydrated.fetch_data is not None
        result = await rehydrated.fetch_data()
        assert result == b"workspace-bytes"
        assert [url for url, _ in transport.calls] == ["https://files.slack.com/img.png"]
        assert transport.authorizations == ["Bearer xoxb-multi-workspace-token"]

    # TS: "should fall back to getToken when no teamId in fetchMetadata"
    @pytest.mark.asyncio
    async def test_should_fall_back_to_gettoken_when_no_teamid_in_fetchmetadata(self):
        from chat_sdk.types import Attachment

        adapter = _make_adapter(bot_token="xoxb-single")
        transport = FakeFileTransport(FakeFileResponse(b"single-bytes"))
        adapter._file_transport = transport

        rehydrated = adapter.rehydrate_attachment(
            Attachment(
                type="image",
                url="https://files.slack.com/img.png",
                fetch_metadata={"url": "https://files.slack.com/img.png"},
            )
        )
        assert rehydrated.fetch_data is not None
        result = await rehydrated.fetch_data()
        assert result == b"single-bytes"
        # Bot token (not a workspace-specific install token) is forwarded.
        assert transport.authorizations == ["Bearer xoxb-single"]

    # TS: "should return attachment unchanged when no url"
    def test_should_return_attachment_unchanged_when_no_url(self):
        from chat_sdk.types import Attachment

        adapter = _make_adapter(bot_token="xoxb-test")
        attachment = Attachment(type="file", name="test.bin")
        rehydrated = adapter.rehydrate_attachment(attachment)

        assert rehydrated.fetch_data is None
        # Upstream asserts `toBe(attachment)` — identical object.
        assert rehydrated is attachment

    # Python-first divergence: reject SSRF vectors at fetch time even if the
    # serialized attachment appeared valid when it was queued.
    @pytest.mark.asyncio
    async def test_rehydrated_fetch_data_rejects_untrusted_host(self):
        from chat_sdk.types import Attachment

        resolver = MagicMock(return_value="xoxb-ssrf-token")
        adapter = _make_adapter(bot_token=resolver)
        transport = FakeFileTransport()
        adapter._file_transport = transport

        rehydrated = adapter.rehydrate_attachment(
            Attachment(
                type="image",
                url="https://attacker.example.com/steal",
                fetch_metadata={"url": "https://attacker.example.com/steal"},
            )
        )
        assert rehydrated.fetch_data is not None
        with pytest.raises(ValidationError):
            await rehydrated.fetch_data()
        # Refused before the token is resolved and before any request.
        assert transport.calls == []
        resolver.assert_not_called()

    def test_is_trusted_slack_download_url_allowlist(self):
        # Accepts Slack-owned HTTPS hosts
        assert SlackAdapter._is_trusted_slack_download_url("https://files.slack.com/f.png")
        assert SlackAdapter._is_trusted_slack_download_url("https://foo.slack-edge.com/x.png")
        assert SlackAdapter._is_trusted_slack_download_url("https://edge.slack.com/x")
        # Rejects non-HTTPS even on a trusted host
        assert not SlackAdapter._is_trusted_slack_download_url("http://files.slack.com/x")
        # Rejects arbitrary hosts
        assert not SlackAdapter._is_trusted_slack_download_url("https://attacker.example/x")
        # Rejects look-alike hosts that merely contain "slack.com"
        assert not SlackAdapter._is_trusted_slack_download_url("https://slack.com.attacker.tld/x")
        # GovSlack and slack-files hosts (vercel/chat 7c269653)
        for url in (
            "https://files.slack-gov.com/f.png",
            "https://slack-gov.com/files-pri/T/F/f.png",
            "https://slack-files.com/files-tmb/T-F-x/f.png",
            "https://slack-files-gov.com/f.png",
        ):
            assert SlackAdapter._is_trusted_slack_download_url(url), url
        assert not SlackAdapter._is_trusted_slack_download_url("https://slack-files.com.attacker.tld/x")
        # GovSlack subdomains are downloadable (the ``*.slack.com`` analogue)
        # but are not auth origins, so they never receive the token.
        assert SlackAdapter._is_trusted_slack_download_url("https://edge.slack-gov.com/x")
        assert not SlackAdapter._is_slack_auth_url("https://edge.slack-gov.com/x")
        assert not SlackAdapter._is_trusted_slack_download_url("https://slack-gov.com.attacker.tld/x")
        # The configured api_url origin, exactly (scheme, host and port)
        api_url = "https://slack-proxy.example:8443/api/"
        assert SlackAdapter._is_trusted_slack_download_url("https://slack-proxy.example:8443/files/f.png", api_url)
        assert not SlackAdapter._is_trusted_slack_download_url("https://slack-proxy.example/files/f.png", api_url)
        assert not SlackAdapter._is_trusted_slack_download_url("https://slack-proxy.example:8443/files/f.png")

    def test_slack_auth_url_is_an_exact_origin_match(self):
        # Port of upstream ``isSlackAuthUrl`` (file.ts): only these origins
        # (plus the api_url origin) ever receive the bot token.
        for url in (
            "https://files.slack.com/f",
            "https://FILES.SLACK.COM/f",
            "https://files.slack.com:443/f",
            "https://files.slack-gov.com/f",
            "https://slack-files.com/f",
            "https://slack-files-gov.com/f",
            "https://slack.com/f",
            "https://slack-gov.com/f",
        ):
            assert SlackAdapter._is_slack_auth_url(url), url
        for url in (
            "https://edge.slack.com/f",
            "https://foo.slack-edge.com/f",
            "http://files.slack.com/f",
            "https://files.slack.com:8443/f",
            "https://files.slack.com.attacker.example/f",
            "https://files.slack.com@attacker.example/f",
            "not a url",
            "",
        ):
            assert not SlackAdapter._is_slack_auth_url(url), url
        assert SlackAdapter._is_slack_auth_url("https://proxy.example/f", "https://proxy.example/api/")
        assert not SlackAdapter._is_slack_auth_url("https://proxy.example:444/f", "https://proxy.example/api/")


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_missing_text(self):
        adapter = _make_adapter()
        event = {"type": "message", "user": "U123", "channel": "C456", "ts": "1234567890.123456"}
        msg = adapter.parse_message(event)
        assert msg.text == ""

    def test_missing_user(self):
        adapter = _make_adapter()
        event = {"type": "message", "channel": "C456", "text": "Anonymous", "ts": "1234567890.123456"}
        msg = adapter.parse_message(event)
        assert msg.author.user_id == "unknown"

    def test_missing_ts(self):
        adapter = _make_adapter()
        event = {"type": "message", "user": "U123", "channel": "C456", "text": "No timestamp"}
        msg = adapter.parse_message(event)
        assert msg.id == ""


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------


class TestDateParsing:
    def test_parses_slack_timestamp(self):
        adapter = _make_adapter()
        event = {
            "type": "message",
            "user": "U123",
            "channel": "C456",
            "text": "Hello",
            "ts": "1609459200.000000",  # 2021-01-01 00:00:00 UTC
        }
        msg = adapter.parse_message(event)
        assert msg.metadata.date_sent is not None
        assert msg.metadata.date_sent.year == 2021


# ---------------------------------------------------------------------------
# channelIdFromThreadId
# ---------------------------------------------------------------------------


class TestChannelIdFromThreadId:
    def test_extracts_channel_id(self):
        adapter = _make_adapter()
        assert adapter.channel_id_from_thread_id("slack:C123:1234567890.000000") == "slack:C123"

    def test_works_with_empty_thread_ts(self):
        adapter = _make_adapter()
        assert adapter.channel_id_from_thread_id("slack:C456:") == "slack:C456"


# ---------------------------------------------------------------------------
# Message subtype handling
# ---------------------------------------------------------------------------


class TestMessageSubtypes:
    def _make_subtype_req(self, subtype: str, **event_overrides: Any) -> _FakeRequest:
        event = {
            "type": "message",
            "subtype": subtype,
            "channel": "C_CHAN",
            "ts": "1234567890.111111",
            **event_overrides,
        }
        body = json.dumps({"type": "event_callback", "team_id": "T123", "event": event})
        return _make_signed_request(body)

    @staticmethod
    def _event_req(event: dict[str, Any]) -> _FakeRequest:
        return _make_signed_request(json.dumps({"type": "event_callback", "team_id": "T123", "event": event}))

    @staticmethod
    async def _init(**overrides: Any) -> tuple[SlackAdapter, MagicMock]:
        adapter = _make_adapter(bot_user_id="U_BOT", **overrides)
        chat = _make_mock_chat(_make_mock_state())
        await adapter.initialize(chat)
        return adapter, chat

    # TS: "routes the message, its edit, and its delete to one thread id in %s"
    # Only the "a flat DM" case; #214 adds "a threaded agent_view DM".
    @pytest.mark.asyncio
    async def test_routes_the_message_its_edit_and_its_delete_to_one_thread_id_in_a_flat_dm(self):
        adapter, chat = await self._init()
        dm = {
            "type": "message",
            "user": "U_USER",
            "channel": "D_DM",
            "channel_type": "im",
            "text": "hello",
            "ts": "1111.0001",
        }
        await adapter.handle_webhook(self._event_req(dm))
        await adapter.handle_webhook(
            self._event_req(
                {
                    "type": "message",
                    "subtype": "message_changed",
                    "channel": "D_DM",
                    "channel_type": "im",
                    "ts": "1111.0002",
                    "message": {**dm, "text": "edited", "edited": {"ts": "1111.0002"}},
                    "previous_message": dm,
                }
            )
        )
        await adapter.handle_webhook(
            self._event_req(
                {
                    "type": "message",
                    "subtype": "message_deleted",
                    "channel": "D_DM",
                    "channel_type": "im",
                    "ts": "1111.0003",
                    "deleted_ts": "1111.0001",
                    "previous_message": dm,
                }
            )
        )

        expected = "slack:D_DM:"
        assert chat.process_message.call_args.args[1] == expected
        assert chat.process_message_updated.call_args.args[1] == expected
        assert chat.process_message_deleted.call_args.args[0].thread_id == expected

    # TS: "dispatches message_changed subtypes as message updates"
    @pytest.mark.asyncio
    async def test_dispatches_message_changed_subtypes_as_message_updates(self):
        adapter, chat = await self._init()
        await adapter.handle_webhook(
            self._event_req(
                {
                    "type": "message",
                    "subtype": "message_changed",
                    "channel": "C_CHAN",
                    "ts": "1234567891.111111",
                    "message": {
                        "type": "message",
                        "user": "U_USER",
                        "channel": "C_CHAN",
                        "text": "edited text",
                        "ts": "1234567890.111111",
                        "edited": {"ts": "1234567891.111111"},
                    },
                }
            )
        )

        assert not chat.process_message.called
        chat.process_message_updated.assert_called_once()
        call = chat.process_message_updated.call_args
        assert call.args[0] is adapter
        assert call.args[1] == "slack:C_CHAN:1234567890.111111"
        assert callable(call.args[2])
        assert call.kwargs["options"] is None
        # Edits never reach the delete path either.
        assert not chat.process_message_deleted.called
        msg = await call.args[2]()
        assert msg.id == "1234567890.111111"
        assert msg.text == "edited text"
        assert msg.metadata.edited is True

    # TS: "forwards the pre-edit message so handlers can diff the change"
    @pytest.mark.asyncio
    async def test_forwards_the_pre_edit_message_so_handlers_can_diff_the_change(self):
        adapter, chat = await self._init()
        before = {
            "type": "message",
            "user": "U_USER",
            "username": "user",
            "channel": "C_CHAN",
            "text": "before",
            "ts": "1234567890.111111",
        }
        await adapter.handle_webhook(
            self._event_req(
                {
                    "type": "message",
                    "subtype": "message_changed",
                    "channel": "C_CHAN",
                    "ts": "1234567891.111111",
                    "message": {**before, "text": "after", "edited": {"ts": "1234567891.111111"}},
                    "previous_message": before,
                }
            )
        )

        previous_factory = chat.process_message_updated.call_args.kwargs["previous_message"]
        assert callable(previous_factory)
        # The pre-edit snapshot parses through the same async path as the new
        # message so mention rendering matches on both sides of the diff.
        previous = await previous_factory()
        assert previous.text == "before"
        assert previous.thread_id == "slack:C_CHAN:1234567890.111111"
        assert previous.metadata.edited is False

    # TS: "ignores a message_changed where nothing actually changed"
    @pytest.mark.asyncio
    async def test_ignores_a_message_changed_where_nothing_actually_changed(self):
        adapter, chat = await self._init()
        # Slack's automatic language detection updates locale metadata and
        # dispatches message_changed without the message itself changing.
        unchanged = {
            "type": "message",
            "user": "U_USER",
            "channel": "C_CHAN",
            "text": "same text",
            "ts": "1234567890.111111",
        }
        await adapter.handle_webhook(
            self._event_req(
                {
                    "type": "message",
                    "subtype": "message_changed",
                    "channel": "C_CHAN",
                    "ts": "1234567891.111111",
                    "message": {**unchanged},
                    "previous_message": {**unchanged},
                }
            )
        )

        assert not chat.process_message_updated.called
        assert not chat.process_message.called

    # TS: "leaves previousMessage undefined when Slack omits it"
    @pytest.mark.asyncio
    async def test_leaves_previousmessage_undefined_when_slack_omits_it(self):
        adapter, chat = await self._init()
        await adapter.handle_webhook(
            self._event_req(
                {
                    "type": "message",
                    "subtype": "message_changed",
                    "channel": "C_CHAN",
                    "ts": "1234567891.111111",
                    "message": {
                        "type": "message",
                        "user": "U_USER",
                        "channel": "C_CHAN",
                        "text": "after",
                        "ts": "1234567890.111111",
                        "edited": {"ts": "1234567891.111111"},
                    },
                }
            )
        )

        chat.process_message_updated.assert_called_once()
        assert chat.process_message_updated.call_args.kwargs["previous_message"] is None

    # TS: "dispatches hidden message_changed edits as message updates"
    @pytest.mark.asyncio
    async def test_dispatches_hidden_message_changed_edits_as_message_updates(self):
        adapter, chat = await self._init()
        await adapter.handle_webhook(
            self._event_req(
                {
                    "type": "message",
                    "subtype": "message_changed",
                    "hidden": True,
                    "channel": "D_DM",
                    "channel_type": "im",
                    "ts": "1779425554.000100",
                    "event_ts": "1779425554.000100",
                    "message": {
                        "type": "message",
                        "user": "U_USER",
                        "text": "What do you see in this attachment? Test",
                        "ts": "1779271807.493869",
                        "thread_ts": "1779271794.544339",
                        "edited": {"user": "U_USER", "ts": "1779425554.000000"},
                    },
                    "previous_message": {
                        "type": "message",
                        "user": "U_USER",
                        "text": "What do you see in this attachment?",
                        "ts": "1779271807.493869",
                        "thread_ts": "1779271794.544339",
                    },
                }
            )
        )

        assert not chat.process_message.called
        chat.process_message_updated.assert_called_once()
        call = chat.process_message_updated.call_args
        assert call.args[0] is adapter
        assert call.args[1] == "slack:D_DM:1779271794.544339"
        assert callable(call.args[2])
        assert call.kwargs["options"] is None

    # TS: "ignores hidden message_changed thread metadata updates after deletes"
    @pytest.mark.asyncio
    async def test_ignores_hidden_message_changed_thread_metadata_updates_after_deletes(self):
        adapter, chat = await self._init()
        snapshot = {
            "type": "message",
            "subtype": "assistant_app_thread",
            "user": "U_BOT",
            "text": "New Assistant Thread",
            "ts": "1778127887.294739",
            "thread_ts": "1778127887.294739",
            "edited": {"user": "U_BOT", "ts": "1778128187.000000"},
            "reply_count": 41,
            "latest_reply": "1779271265.010909",
        }
        await adapter.handle_webhook(
            self._event_req(
                {
                    "type": "message",
                    "subtype": "message_changed",
                    "hidden": True,
                    "channel": "D_DM",
                    "channel_type": "im",
                    "ts": "1779425682.000300",
                    "event_ts": "1779425682.000300",
                    "message": {**snapshot},
                    "previous_message": {**snapshot},
                }
            )
        )

        assert not chat.process_message.called
        assert not chat.process_message_updated.called

    # TS: "dispatches message_deleted subtypes as message deletes"
    @pytest.mark.asyncio
    async def test_dispatches_message_deleted_subtypes_as_message_deletes(self):
        from datetime import datetime, timezone

        from chat_sdk.types import MessageDeletedEvent

        adapter, chat = await self._init()
        event = {
            "type": "message",
            "subtype": "message_deleted",
            "channel": "C_CHAN",
            "deleted_ts": "1234567890.111111",
            "event_ts": "1234567891.111111",
            "previous_message": {
                "type": "message",
                "user": "U_USER",
                "channel": "C_CHAN",
                "text": "deleted text",
                "ts": "1234567890.111111",
            },
        }
        await adapter.handle_webhook(self._event_req(event))

        assert not chat.process_message.called
        assert not chat.process_message_updated.called
        chat.process_message_deleted.assert_called_once()
        deleted, options = chat.process_message_deleted.call_args.args
        assert options is None
        assert isinstance(deleted, MessageDeletedEvent)
        assert deleted.adapter is adapter
        assert deleted.channel_id == "C_CHAN"
        assert deleted.message_id == "1234567890.111111"
        assert deleted.thread_id == "slack:C_CHAN:1234567890.111111"
        assert deleted.deleted_at == datetime.fromtimestamp(1234567891.111111, tz=timezone.utc)
        # The untouched Slack payload (the webhook copies the envelope team_id
        # onto it, as upstream does).
        assert deleted.raw == {**event, "team_id": "T123"}
        assert deleted.platform is None
        assert deleted.previous_message is not None
        assert deleted.previous_message.text == "deleted text"
        assert deleted.previous_message.id == "1234567890.111111"

    # TS: "ignores message_changed tombstone subtypes"
    @pytest.mark.asyncio
    async def test_ignores_message_changed_tombstone_subtypes(self):
        adapter, chat = await self._init()
        await adapter.handle_webhook(
            self._event_req(
                {
                    "type": "message",
                    "subtype": "message_changed",
                    "channel": "C_CHAN",
                    "channel_type": "channel",
                    "hidden": True,
                    "ts": "1779426065.000200",
                    "event_ts": "1779426065.000200",
                    "message": {
                        "type": "message",
                        "subtype": "tombstone",
                        "user": "USLACKBOT",
                        "text": "This message was deleted.",
                        "hidden": True,
                        "ts": "1778050260.824689",
                        "thread_ts": "1778050260.824689",
                    },
                    "previous_message": {
                        "type": "message",
                        "user": "U_USER",
                        "channel": "C_CHAN",
                        "text": "<@U_BOT> deleted message",
                        "ts": "1778050260.824689",
                        "thread_ts": "1778050260.824689",
                    },
                }
            )
        )

        assert not chat.process_message.called
        assert not chat.process_message_updated.called
        assert not chat.process_message_deleted.called

    @pytest.mark.asyncio
    async def test_ignores_channel_join(self):
        adapter = _make_adapter(bot_user_id="U_BOT")
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)
        await adapter.handle_webhook(self._make_subtype_req("channel_join", user="U_USER"))
        assert not chat.process_message.called

    @pytest.mark.asyncio
    async def test_allows_file_share(self):
        adapter = _make_adapter(bot_user_id="U_BOT")
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)
        await adapter.handle_webhook(
            self._make_subtype_req(
                "file_share",
                user="U_USER",
                text="Check this file",
                thread_ts="1234567890.000000",
                files=[
                    {
                        "id": "F123",
                        "mimetype": "image/png",
                        "url_private": "https://files.slack.com/file.png",
                        "name": "screenshot.png",
                        "size": 12345,
                    }
                ],
            )
        )
        assert chat.process_message.called


class TestMessageLifecyclePythonSpecific:
    """Python-specific coverage for the Slack edit/delete emitters (#211).

    What to fix if this fails: ``_handle_message_changed`` /
    ``_handle_message_deleted`` / ``_parse_slack_timestamp`` in
    ``src/chat_sdk/adapters/slack/adapter.py``.
    """

    @staticmethod
    def _event_req(event: dict[str, Any]) -> _FakeRequest:
        return _make_signed_request(json.dumps({"type": "event_callback", "team_id": "T123", "event": event}))

    @staticmethod
    async def _init() -> tuple[SlackAdapter, MagicMock, MagicMock]:
        adapter = _make_adapter(bot_user_id="U_BOT")
        chat = _make_mock_chat(_make_mock_state())
        await adapter.initialize(chat)
        client = MagicMock()
        client.users_info = AsyncMock(
            return_value={"user": {"name": "alice", "profile": {"display_name": "Alice", "real_name": "Alice A"}}}
        )
        adapter._get_client = lambda token=None: client  # type: ignore[method-assign]
        return adapter, chat, client

    @pytest.mark.asyncio
    async def test_previous_message_factory_falls_back_to_sync_parse_when_lookup_raises(self):
        from chat_sdk.testing import MockLogger

        logger = MockLogger()
        adapter = _make_adapter(bot_user_id="U_BOT", logger=logger)
        chat = _make_mock_chat(_make_mock_state())
        await adapter.initialize(chat)
        adapter._lookup_user = AsyncMock(side_effect=RuntimeError("users.info down"))  # type: ignore[method-assign]

        before = {"type": "message", "user": "U_USER", "text": "before", "ts": "1234567890.111111"}
        await adapter.handle_webhook(
            self._event_req(
                {
                    "type": "message",
                    "subtype": "message_changed",
                    "channel": "C_CHAN",
                    "ts": "1234567891.111111",
                    "message": {**before, "text": "after", "edited": {"ts": "1234567891.111111"}},
                    "previous_message": before,
                }
            )
        )

        previous = await chat.process_message_updated.call_args.kwargs["previous_message"]()
        adapter._lookup_user.assert_awaited_once_with("U_USER")
        # Sync parse: no lookup, so the author name falls back to the user ID.
        assert previous.text == "before"
        assert previous.author.user_name == "U_USER"
        assert previous.thread_id == "slack:C_CHAN:1234567890.111111"
        warnings = [c for c in logger.warn.calls if c[0] == "Falling back to sync parse for pre-edit message"]
        assert len(warnings) == 1
        assert warnings[0][1]["threadId"] == "slack:C_CHAN:1234567890.111111"
        assert str(warnings[0][1]["error"]) == "users.info down"

    @pytest.mark.asyncio
    async def test_previous_message_factory_propagates_cancellation(self):
        import asyncio as _asyncio

        adapter, chat, _ = await self._init()
        adapter._lookup_user = AsyncMock(side_effect=_asyncio.CancelledError())  # type: ignore[method-assign]
        before = {"type": "message", "user": "U_USER", "text": "before", "ts": "1234567890.111111"}
        await adapter.handle_webhook(
            self._event_req(
                {
                    "type": "message",
                    "subtype": "message_changed",
                    "channel": "C_CHAN",
                    "ts": "1234567891.111111",
                    "message": {**before, "text": "after"},
                    "previous_message": before,
                }
            )
        )

        with pytest.raises(_asyncio.CancelledError):
            await chat.process_message_updated.call_args.kwargs["previous_message"]()

    @pytest.mark.parametrize("value", [None, "", "abc", "nan", "inf"])
    def test_parse_slack_timestamp_returns_none_for_missing_or_non_numeric(self, value: str | None):
        assert SlackAdapter._parse_slack_timestamp(value) is None

    def test_parse_slack_timestamp_returns_utc_datetime(self):
        from datetime import datetime, timezone

        parsed = SlackAdapter._parse_slack_timestamp("1700000000.123")
        assert parsed == datetime(2023, 11, 14, 22, 13, 20, 123000, tzinfo=timezone.utc)
        assert parsed is not None
        assert parsed.tzinfo is timezone.utc

    @pytest.mark.asyncio
    async def test_bot_own_edit_skips_user_lookup_and_update_dispatch(self):
        # Divergence from upstream (docs/UPSTREAM_SYNC.md): the bot's own
        # edits (post+edit streaming) return before the message is parsed.
        adapter, chat, client = await self._init()
        await adapter.handle_webhook(
            self._event_req(
                {
                    "type": "message",
                    "subtype": "message_changed",
                    "channel": "C_CHAN",
                    "ts": "1234567891.111111",
                    "message": {
                        "type": "message",
                        "user": "U_BOT",
                        "text": "streamed reply, more text",
                        "ts": "1234567890.111111",
                        "edited": {"ts": "1234567891.111111"},
                    },
                    "previous_message": {
                        "type": "message",
                        "user": "U_BOT",
                        "text": "streamed reply",
                        "ts": "1234567890.111111",
                    },
                }
            )
        )
        await asyncio.sleep(0)

        assert not chat.process_message_updated.called
        assert not chat.process_message.called
        client.users_info.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_other_user_edit_still_dispatches_and_resolves_the_author(self):
        # Companion to the self-edit short-circuit: a non-bot edit parses with
        # the async user lookup.
        adapter, chat, client = await self._init()
        await adapter.handle_webhook(
            self._event_req(
                {
                    "type": "message",
                    "subtype": "message_changed",
                    "channel": "C_CHAN",
                    "ts": "1234567891.111111",
                    "message": {
                        "type": "message",
                        "user": "U_ALICE",
                        "text": "after",
                        "ts": "1234567890.111111",
                        "edited": {"ts": "1234567891.111111"},
                    },
                }
            )
        )

        msg = await chat.process_message_updated.call_args.args[2]()
        client.users_info.assert_awaited_once_with(user="U_ALICE")
        assert msg.author.user_name == "Alice"
        assert msg.author.is_me is False

    @pytest.mark.asyncio
    async def test_message_deleted_falls_back_to_previous_ts_and_inherits_the_dm_rule(self):
        adapter, chat, _ = await self._init()
        await adapter.handle_webhook(
            self._event_req(
                {
                    "type": "message",
                    "subtype": "message_deleted",
                    "channel": "D_DM",
                    "channel_type": "im",
                    "ts": "1111.0003",
                    "previous_message": {
                        "type": "message",
                        "user": "U_USER",
                        "text": "reply",
                        "ts": "1111.0002",
                        "thread_ts": "1111.0001",
                    },
                }
            )
        )

        deleted = chat.process_message_deleted.call_args.args[0]
        assert deleted.message_id == "1111.0002"
        assert deleted.thread_id == "slack:D_DM:1111.0001"
        assert deleted.previous_message.raw["channel"] == "D_DM"
        assert deleted.previous_message.raw["channel_type"] == "im"
        # No event_ts: deleted_at comes from the outer ts.
        assert deleted.deleted_at is not None
        assert deleted.deleted_at.timestamp() == pytest.approx(1111.0003)

    @pytest.mark.asyncio
    async def test_message_deleted_without_previous_message_uses_deleted_ts(self):
        adapter, chat, _ = await self._init()
        await adapter.handle_webhook(
            self._event_req(
                {
                    "type": "message",
                    "subtype": "message_deleted",
                    "channel": "C_CHAN",
                    "deleted_ts": "1234567890.111111",
                    "event_ts": "not-a-number",
                }
            )
        )

        deleted = chat.process_message_deleted.call_args.args[0]
        assert deleted.message_id == "1234567890.111111"
        assert deleted.thread_id == "slack:C_CHAN:1234567890.111111"
        assert deleted.previous_message is None
        assert deleted.deleted_at is None

    @pytest.mark.asyncio
    async def test_real_chat_runs_lifecycle_handlers_for_edit_and_delete(self):
        # End to end through Chat: the adapter's process_message_updated /
        # process_message_deleted calls match core's signatures, and edits and
        # deletes never reach the message handlers.
        from chat_sdk.chat import Chat
        from chat_sdk.testing import create_mock_state
        from chat_sdk.types import ChatConfig, WebhookOptions

        adapter = _make_adapter(bot_user_id="U_BOT")
        chat = Chat(ChatConfig(user_name="testbot", adapters={"slack": adapter}, state=create_mock_state()))
        updates: list[tuple[str, str, str | None]] = []
        deletes: list[tuple[str, str, str, str | None]] = []
        new_messages: list[str] = []

        @chat.on_message_updated
        async def _on_update(thread: Any, message: Any, previous: Any) -> None:
            updates.append((thread.id, message.text, previous.text if previous is not None else None))

        @chat.on_message_deleted
        async def _on_delete(event: Any) -> None:
            prev_text = event.previous_message.text if event.previous_message is not None else None
            deletes.append((event.thread_id, event.message_id, event.platform, prev_text))

        @chat.on_message(r".*")
        async def _on_message(thread: Any, message: Any) -> None:
            new_messages.append(message.text)

        tasks: list[Any] = []
        options = WebhookOptions(wait_until=tasks.append)
        original = {
            "type": "message",
            "user": "U_USER",
            "username": "alice",
            "channel": "C_CHAN",
            "text": "before",
            "ts": "1234567890.111111",
        }
        await chat.webhooks["slack"](
            self._event_req(
                {
                    "type": "message",
                    "subtype": "message_changed",
                    "channel": "C_CHAN",
                    "ts": "1234567891.111111",
                    "message": {**original, "text": "after", "edited": {"ts": "1234567891.111111"}},
                    "previous_message": original,
                }
            ),
            options,
        )
        await chat.webhooks["slack"](
            self._event_req(
                {
                    "type": "message",
                    "subtype": "message_deleted",
                    "channel": "C_CHAN",
                    "deleted_ts": "1234567890.111111",
                    "event_ts": "1234567892.111111",
                    "previous_message": {**original, "text": "after"},
                }
            ),
            options,
        )
        await asyncio.gather(*tasks)

        assert len(tasks) == 2
        assert updates == [("slack:C_CHAN:1234567890.111111", "after", "before")]
        assert deletes == [("slack:C_CHAN:1234567890.111111", "1234567890.111111", "slack", "after")]
        assert new_messages == []

    @pytest.mark.asyncio
    async def test_message_deleted_without_any_ts_is_dropped(self):
        adapter, chat, _ = await self._init()
        await adapter.handle_webhook(
            self._event_req({"type": "message", "subtype": "message_deleted", "channel": "C_CHAN", "ts": "1.2"})
        )

        assert not chat.process_message_deleted.called
        assert not chat.process_message.called


# ---------------------------------------------------------------------------
# Multi-workspace mode
# ---------------------------------------------------------------------------


class TestMultiWorkspace:
    def test_creates_adapter_without_bot_token(self):
        adapter = SlackAdapter(SlackAdapterConfig(signing_secret="test-secret"))
        assert isinstance(adapter, SlackAdapter)
        assert adapter.name == "slack"

    @pytest.mark.asyncio
    async def test_set_installation_throws_before_initialize(self):
        adapter = SlackAdapter(SlackAdapterConfig(signing_secret="test-secret"))
        with pytest.raises(Exception, match="[Nn]ot initialized|[Aa]dapter"):
            await adapter.set_installation("T123", SlackInstallation(bot_token="xoxb-token"))

    @pytest.mark.asyncio
    async def test_installation_roundtrip(self):
        state = _make_mock_state()
        adapter = SlackAdapter(SlackAdapterConfig(signing_secret="test-secret"))
        await adapter.initialize(_make_mock_chat(state))

        installation = SlackInstallation(
            bot_token="xoxb-workspace-token",
            bot_user_id="U_BOT_123",
            team_name="Test Team",
        )
        await adapter.set_installation("T_TEAM_1", installation)
        retrieved = await adapter.get_installation("T_TEAM_1")
        assert retrieved is not None
        assert retrieved.bot_token == "xoxb-workspace-token"

    @pytest.mark.asyncio
    async def test_get_installation_unknown_returns_none(self):
        state = _make_mock_state()
        adapter = SlackAdapter(SlackAdapterConfig(signing_secret="test-secret"))
        await adapter.initialize(_make_mock_chat(state))
        result = await adapter.get_installation("T_UNKNOWN")
        assert result is None

    @pytest.mark.asyncio
    async def test_delete_installation(self):
        state = _make_mock_state()
        adapter = SlackAdapter(SlackAdapterConfig(signing_secret="test-secret"))
        await adapter.initialize(_make_mock_chat(state))
        await adapter.set_installation("T_TEAM_2", SlackInstallation(bot_token="xoxb-token"))
        assert await adapter.get_installation("T_TEAM_2") is not None
        await adapter.delete_installation("T_TEAM_2")
        assert await adapter.get_installation("T_TEAM_2") is None


# ---------------------------------------------------------------------------
# OAuth callback -- redirect_uri handling
# ---------------------------------------------------------------------------


def _make_oauth_adapter() -> tuple[SlackAdapter, MagicMock, AsyncMock]:
    """Create a SlackAdapter wired for OAuth with a mocked oauth_v2_access."""
    adapter = SlackAdapter(
        SlackAdapterConfig(
            signing_secret="test-signing-secret",
            client_id="client-id",
            client_secret="client-secret",
        )
    )
    mock_access = AsyncMock(
        return_value={
            "ok": True,
            "access_token": "xoxb-oauth-bot-token",
            "bot_user_id": "U_BOT_OAUTH",
            "team": {"id": "T_OAUTH_1", "name": "OAuth Team"},
        }
    )
    # Patch the client returned by _get_client("") to have oauth_v2_access
    mock_client = MagicMock()
    mock_client.oauth_v2_access = mock_access
    mock_client.auth_test = AsyncMock(
        return_value={
            "ok": True,
            "user_id": "U_BOT_OAUTH",
            "bot_id": "B_BOT",
            "user": "bot",
        }
    )
    adapter._client_cache[""] = mock_client
    return adapter, mock_client, mock_access


class TestOAuthRedirectUri:
    """Port of upstream handleOAuthCallback redirect_uri tests (commit 1856198)."""

    @pytest.mark.asyncio
    async def test_exchanges_code_for_token_and_saves_installation(self):
        adapter, _, mock_access = _make_oauth_adapter()
        state = _make_mock_state()
        await adapter.initialize(_make_mock_chat(state))

        req = _FakeRequest(
            "",
            url="https://example.com/auth/callback/slack?code=oauth-code-123",
        )
        result = await adapter.handle_oauth_callback(req)

        assert result["team_id"] == "T_OAUTH_1"
        stored = await adapter.get_installation("T_OAUTH_1")
        assert stored is not None
        assert stored.bot_token == "xoxb-oauth-bot-token"
        mock_access.assert_called_once_with(
            client_id="client-id",
            client_secret="client-secret",
            code="oauth-code-123",
        )

    @pytest.mark.asyncio
    async def test_forwards_redirect_uri_from_callback_options(self):
        adapter, _, mock_access = _make_oauth_adapter()
        state = _make_mock_state()
        await adapter.initialize(_make_mock_chat(state))

        req = _FakeRequest(
            "",
            url="https://example.com/auth/callback/slack?code=oauth-code-123",
        )
        await adapter.handle_oauth_callback(req, options={"redirect_uri": "https://example.com/install/callback"})

        mock_access.assert_called_once_with(
            client_id="client-id",
            client_secret="client-secret",
            code="oauth-code-123",
            redirect_uri="https://example.com/install/callback",
        )

    @pytest.mark.asyncio
    async def test_prefers_callback_options_redirect_uri_over_query_param(self):
        adapter, _, mock_access = _make_oauth_adapter()
        state = _make_mock_state()
        await adapter.initialize(_make_mock_chat(state))

        req = _FakeRequest(
            "",
            url="https://example.com/auth/callback/slack?code=oauth-code-123&redirect_uri=https%3A%2F%2Fexample.com%2Fquery-callback",
        )
        await adapter.handle_oauth_callback(req, options={"redirect_uri": "https://example.com/explicit-callback"})

        mock_access.assert_called_once_with(
            client_id="client-id",
            client_secret="client-secret",
            code="oauth-code-123",
            redirect_uri="https://example.com/explicit-callback",
        )

    @pytest.mark.asyncio
    async def test_falls_back_to_redirect_uri_from_query_param(self):
        adapter, _, mock_access = _make_oauth_adapter()
        state = _make_mock_state()
        await adapter.initialize(_make_mock_chat(state))

        req = _FakeRequest(
            "",
            url="https://example.com/auth/callback/slack?code=oauth-code-123&redirect_uri=https%3A%2F%2Fexample.com%2Fquery-callback",
        )
        await adapter.handle_oauth_callback(req)

        mock_access.assert_called_once_with(
            client_id="client-id",
            client_secret="client-secret",
            code="oauth-code-123",
            redirect_uri="https://example.com/query-callback",
        )

    @pytest.mark.asyncio
    async def test_throws_when_code_missing(self):
        adapter, _, _ = _make_oauth_adapter()
        state = _make_mock_state()
        await adapter.initialize(_make_mock_chat(state))

        req = _FakeRequest("", url="https://example.com/auth/callback/slack")
        with pytest.raises(ValidationError, match="Missing 'code'"):
            await adapter.handle_oauth_callback(req)

    @pytest.mark.asyncio
    async def test_throws_without_client_id_and_client_secret(self):
        adapter = SlackAdapter(SlackAdapterConfig(signing_secret="test-secret"))
        state = _make_mock_state()
        await adapter.initialize(_make_mock_chat(state))

        req = _FakeRequest(
            "",
            url="https://example.com/auth/callback/slack?code=abc",
        )
        with pytest.raises(ValidationError, match="client_id"):
            await adapter.handle_oauth_callback(req)


# ---------------------------------------------------------------------------
# Link unfurl metadata enrichment (port of vercel/chat#395 / chat@4.27.0)
# ---------------------------------------------------------------------------


class TestUnfurlMetadata:
    """Slack delivers link unfurl metadata via legacy ``attachments`` (and
    via ``message_changed`` events that arrive ~100-2000ms later). We
    enrich each ``LinkPreview`` so handlers see real metadata instead of
    bare URLs.

    What to fix if this fails:

    - ``_extract_links`` must read ``event["attachments"]`` and merge
      ``title``/``text``/``image_url``/``service_name`` into the link
      preview.
    - ``_handle_message_changed`` must store unfurl metadata in state
      keyed by ``slack:unfurls:{installation_scope}{channel}:{ts}``
      (vercel/chat#877; the scope is empty in single-workspace mode).
    - ``_enrich_links`` must read that key and merge it into the links.
    - Trailing-slash mismatch between ``url`` and the attachment's
      ``from_url`` must be tolerated in both directions.
    """

    def test_extract_links_inline_attachments_merge_title_and_description(self):
        adapter = _make_adapter()
        event = {
            "text": "Check <https://example.com>",
            "attachments": [
                {
                    "from_url": "https://example.com",
                    "title": "Example Domain",
                    "text": "An illustrative example",
                    "image_url": "https://example.com/img.png",
                    "service_name": "Example",
                }
            ],
        }
        links = adapter._extract_links(event)
        assert len(links) == 1
        link = links[0]
        assert link.url == "https://example.com"
        assert link.title == "Example Domain"
        assert link.description == "An illustrative example"
        assert link.image_url == "https://example.com/img.png"
        assert link.site_name == "Example"

    def test_extract_links_attachment_only_url_is_added(self):
        # If the URL is only mentioned in an attachment (not the text),
        # we still create a LinkPreview for it.
        adapter = _make_adapter()
        event = {
            "text": "no urls here",
            "attachments": [
                {
                    "from_url": "https://side.example.com",
                    "title": "Side",
                    "text": "Side preview",
                },
            ],
        }
        links = adapter._extract_links(event)
        assert len(links) == 1
        assert links[0].url == "https://side.example.com"
        assert links[0].title == "Side"

    def test_extract_links_attachment_without_title_or_text_is_skipped(self):
        # Adversarial: bare attachment with from_url but no title/text —
        # nothing useful to merge, don't pollute the URL set with it.
        adapter = _make_adapter()
        event = {
            "text": "",
            "attachments": [{"from_url": "https://no-meta.example.com"}],
        }
        links = adapter._extract_links(event)
        assert links == []

    def test_extract_links_trailing_slash_normalization(self):
        # Slack canonicalizes URLs with a trailing slash. The event's text
        # might say <https://example.com> while the attachment's from_url
        # is https://example.com/. The two URLs become two LinkPreview
        # entries (matching upstream TS), but both should pick up the
        # unfurl metadata via the trailing-slash-tolerant lookup.
        adapter = _make_adapter()
        event = {
            "text": "Look at <https://example.com>",
            "attachments": [
                {
                    "from_url": "https://example.com/",  # trailing slash
                    "title": "Example",
                    "text": "Body",
                },
            ],
        }
        links = adapter._extract_links(event)
        # Both URL variants get the unfurl title — neither is left bare.
        titles = sorted((link.url, link.title) for link in links)
        assert any(url == "https://example.com" and title == "Example" for url, title in titles), (
            "text URL should pick up unfurl via trailing-slash-tolerant lookup"
        )
        assert any(url == "https://example.com/" and title == "Example" for url, title in titles), (
            "attachment URL should still get its own unfurl"
        )

    @pytest.mark.asyncio
    async def test_message_changed_caches_unfurls_in_state(self):
        adapter = _make_adapter(bot_user_id="U_BOT")
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        body = json.dumps(
            {
                "type": "event_callback",
                "team_id": "T123",
                "event": {
                    "type": "message",
                    "subtype": "message_changed",
                    "channel": "C_CHAN",
                    "ts": "1234567890.222222",
                    "message": {
                        "ts": "1234567890.111111",
                        "attachments": [
                            {
                                "from_url": "https://example.com",
                                "title": "Cached Title",
                                "text": "Cached body",
                                "image_url": "https://example.com/i.png",
                                "service_name": "Example",
                            },
                        ],
                    },
                },
            }
        )
        await adapter.handle_webhook(_make_signed_request(body))

        # Give the spawned task a chance to run.
        await asyncio.sleep(0)
        cached = state._cache.get("slack:unfurls:C_CHAN:1234567890.111111")
        assert cached is not None
        assert cached["https://example.com"]["title"] == "Cached Title"
        # And process_message must NOT be called for message_changed.
        assert not chat.process_message.called

    # TS: "should not re-dispatch message_changed as a new message"
    @pytest.mark.asyncio
    async def test_should_not_re_dispatch_message_changed_as_a_new_message(self):
        adapter = _make_adapter(bot_user_id="U_BOT")
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        body = json.dumps(
            {
                "type": "event_callback",
                "event": {
                    "type": "message",
                    "subtype": "message_changed",
                    "hidden": True,
                    "channel": "C123",
                    "ts": "1234567891.000000",
                    "message": {
                        "type": "message",
                        "user": "U_USER",
                        "text": "https://example.com",
                        "ts": "1234567890.123456",
                        "attachments": [
                            {
                                "from_url": "https://example.com",
                                "title": "Example Site",
                                "text": "Welcome to Example",
                            },
                        ],
                    },
                },
            }
        )
        await adapter.handle_webhook(_make_signed_request(body))
        await asyncio.sleep(0)

        assert not chat.process_message.called
        assert not chat.process_message_updated.called
        # The unfurl-only update still feeds the unfurl cache.
        assert state._cache["slack:unfurls:C123:1234567890.123456"]["https://example.com"]["title"] == "Example Site"

    # TS: "should ignore hidden message_changed without unfurl attachments"
    @pytest.mark.asyncio
    async def test_should_ignore_hidden_message_changed_without_unfurl_attachments(self):
        adapter = _make_adapter(bot_user_id="U_BOT")
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        body = json.dumps(
            {
                "type": "event_callback",
                "event": {
                    "type": "message",
                    "subtype": "message_changed",
                    "hidden": True,
                    "channel": "C123",
                    "ts": "1234567891.000000",
                    "message": {
                        "type": "message",
                        "user": "U_USER",
                        "text": "edited text",
                        "ts": "1234567890.123456",
                        "edited": {"user": "U_USER", "ts": "1234567891.000000"},
                    },
                },
            }
        )
        await adapter.handle_webhook(_make_signed_request(body))

        assert not chat.process_message.called
        assert not chat.process_message_updated.called

    @pytest.mark.asyncio
    async def test_unfurl_cache_is_written_when_message_changed_also_carries_an_edit(self):
        # Python-specific: unfurl caching is a side step, so an edit that also
        # carries unfurl attachments both caches and dispatches the update.
        adapter = _make_adapter(bot_user_id="U_BOT")
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        body = json.dumps(
            {
                "type": "event_callback",
                "event": {
                    "type": "message",
                    "subtype": "message_changed",
                    "channel": "C123",
                    "ts": "1234567891.000000",
                    "message": {
                        "type": "message",
                        "user": "U_USER",
                        "text": "see https://example.com",
                        "ts": "1234567890.123456",
                        "edited": {"user": "U_USER", "ts": "1234567891.000000"},
                        "attachments": [{"from_url": "https://example.com", "title": "Example Site"}],
                    },
                    "previous_message": {
                        "type": "message",
                        "user": "U_USER",
                        "text": "https://example.com",
                        "ts": "1234567890.123456",
                    },
                },
            }
        )
        await adapter.handle_webhook(_make_signed_request(body))
        await asyncio.sleep(0)

        assert state._cache["slack:unfurls:C123:1234567890.123456"]["https://example.com"]["title"] == "Example Site"
        chat.process_message_updated.assert_called_once()
        assert chat.process_message_updated.call_args.args[1] == "slack:C123:1234567890.123456"
        assert not chat.process_message.called

    @pytest.mark.asyncio
    async def test_message_changed_with_no_unfurls_does_not_write_state(self):
        adapter = _make_adapter(bot_user_id="U_BOT")
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        body = json.dumps(
            {
                "type": "event_callback",
                "team_id": "T123",
                "event": {
                    "type": "message",
                    "subtype": "message_changed",
                    "channel": "C_CHAN",
                    "ts": "1234567890.000001",
                    "message": {
                        "ts": "1234567890.000002",
                        "text": "edited body, no unfurls",
                    },
                },
            }
        )
        await adapter.handle_webhook(_make_signed_request(body))
        await asyncio.sleep(0)
        # Nothing should have been written to state.
        assert not any(k.startswith("slack:unfurls:") for k in state._cache)

    @pytest.mark.asyncio
    async def test_enrich_links_pulls_from_state_cache(self):
        from chat_sdk.types import LinkPreview as _LinkPreview

        adapter = _make_adapter()
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        # Pre-seed the cache as if message_changed had already landed.
        state._cache["slack:unfurls:C1:1234567890.111111"] = {
            "https://example.com": {
                "title": "From Cache",
                "description": "Cached body",
                "image_url": None,
                "site_name": None,
            }
        }

        original = [_LinkPreview(url="https://example.com")]
        enriched = await adapter._enrich_links(original, "C1", "1234567890.111111")
        assert len(enriched) == 1
        assert enriched[0].title == "From Cache"
        assert enriched[0].description == "Cached body"

    @pytest.mark.asyncio
    async def test_enrich_links_preserves_user_supplied_title(self):
        # Adversarial: link already has a title (e.g. extracted from a
        # Slack message URL). Cached unfurl must NOT clobber it.
        from chat_sdk.types import LinkPreview as _LinkPreview

        adapter = _make_adapter()
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        state._cache["slack:unfurls:C1:t1"] = {
            "https://example.com": {"title": "From Cache", "description": None},
        }
        original = [_LinkPreview(url="https://example.com", title="User Title")]
        enriched = await adapter._enrich_links(original, "C1", "t1")
        assert enriched[0].title == "User Title"

    @pytest.mark.asyncio
    async def test_enrich_links_returns_unchanged_with_no_chat(self):
        # When chat isn't initialized, enrichment is a no-op.
        from chat_sdk.types import LinkPreview as _LinkPreview

        adapter = _make_adapter()
        original = [_LinkPreview(url="https://example.com")]
        enriched = await adapter._enrich_links(original, "C1", "ts1")
        assert enriched is original

    @pytest.mark.asyncio
    async def test_enrich_links_returns_unchanged_without_a_channel(self):
        """Upstream ``!(this.chat && channelId && messageTs)``: without a
        channel there is no scoped unfurl key to poll, so return at once
        rather than polling ``slack:unfurls:...None:{ts}`` for ~2s."""
        from chat_sdk.types import LinkPreview as _LinkPreview

        adapter = _make_adapter()
        state = _make_mock_state()
        state.get = AsyncMock(return_value=None)
        await adapter.initialize(_make_mock_chat(state))

        original = [_LinkPreview(url="https://example.com")]
        enriched = await adapter._enrich_links(original, None, "1234567890.111111")

        assert enriched is original
        state.get.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_enrich_links_unfurl_overrides_existing_description(self):
        """Unfurl description WINS over a pre-existing preview description.

        TS does ``{ ...link, ...unfurl }`` (spread) which overwrites the
        preview's description. The previous Python implementation
        preserved the preview's description when non-None — silently
        diverging from upstream.

        What to fix if this fails: ``_merge_unfurl_into_preview`` in
        ``src/chat_sdk/adapters/slack/adapter.py`` must let the unfurl
        values win over the preview's description / image_url /
        site_name (only ``title`` is short-circuited at the
        ``_enrich_links`` level).
        """
        from chat_sdk.types import LinkPreview as _LinkPreview

        adapter = _make_adapter()
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        state._cache["slack:unfurls:C1:t-override"] = {
            "https://example.com": {
                "title": None,  # title not present → no short-circuit clobber
                "description": "new",
                "image_url": "https://example.com/new.png",
                "site_name": "Example",
            }
        }
        # Preview already has an "old" description — unfurl must win.
        original = [
            _LinkPreview(
                url="https://example.com",
                description="old",
                image_url="https://example.com/old.png",
                site_name="OldSite",
            )
        ]
        enriched = await adapter._enrich_links(original, "C1", "t-override")
        assert enriched[0].description == "new"
        assert enriched[0].image_url == "https://example.com/new.png"
        assert enriched[0].site_name == "Example"

    @pytest.mark.asyncio
    async def test_message_changed_overwrites_cached_unfurl_not_merge(self):
        """Two ``message_changed`` events for the same ts overwrite the cache.

        Slack delivers multi-edit unfurls as separate ``message_changed``
        events. Each event carries the FULL, current attachment list — a
        merge would keep stale entries from the previous edit. The cache
        ``set()`` semantics must overwrite, not merge.

        What to fix if this fails: ``_handle_message_changed`` in
        ``src/chat_sdk/adapters/slack/adapter.py`` must call
        ``state.set(...)`` (which overwrites) and never read-merge-write.
        """
        adapter = _make_adapter(bot_user_id="U_BOT")
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        def _make_changed_body(url: str, title: str) -> str:
            return json.dumps(
                {
                    "type": "event_callback",
                    "team_id": "T123",
                    "event": {
                        "type": "message",
                        "subtype": "message_changed",
                        "channel": "C_CHAN",
                        "ts": "1234567890.222222",
                        "message": {
                            "ts": "1234567890.111111",
                            "attachments": [
                                {
                                    "from_url": url,
                                    "title": title,
                                    "text": f"body for {title}",
                                },
                            ],
                        },
                    },
                }
            )

        # First edit caches a single unfurl for URL_A.
        await adapter.handle_webhook(_make_signed_request(_make_changed_body("https://a.example.com", "First")))
        await asyncio.sleep(0)
        first = state._cache.get("slack:unfurls:C_CHAN:1234567890.111111")
        assert first is not None
        # Use ``.get`` for explicit dict-key membership — avoids tripping
        # CodeQL's URL-substring-sanitization heuristic which fires on
        # bare ``url_literal in container`` even when ``container`` is a
        # dict and ``in`` is a key check, not a substring check.
        assert first.get("https://a.example.com") is not None

        # Second edit caches an unfurl for a DIFFERENT URL (URL_B).
        # If the implementation merged, URL_A would still be in the cache.
        await adapter.handle_webhook(_make_signed_request(_make_changed_body("https://b.example.com", "Second")))
        await asyncio.sleep(0)
        second = state._cache.get("slack:unfurls:C_CHAN:1234567890.111111")
        assert second is not None
        assert second.get("https://b.example.com") is not None
        assert second.get("https://a.example.com") is None, "second message_changed must overwrite, not merge"
        assert second["https://b.example.com"]["title"] == "Second"

    def test_extract_links_url_with_open_paren_survives_parser(self):
        """A URL containing ``(`` (unbalanced open paren) is preserved.

        Slack delivers URLs in angle brackets — ``<URL>`` — which the
        adapter parses with ``_BRACKETED_URL_PATTERN``
        (``<(https?://[^>]{1,2048})>``). The character class accepts ``(``
        so a URL such as
        ``https://en.wikipedia.org/wiki/Pi_(letter)`` makes it through
        intact. The other URL extraction path (rich_text blocks) gets
        the URL as a struct field, so parens are also fine there.

        What to fix if this fails: the URL pattern in ``_extract_links``
        in ``src/chat_sdk/adapters/slack/adapter.py`` was tightened in a
        way that drops parentheses.
        """
        adapter = _make_adapter()
        url_with_paren = "https://en.wikipedia.org/wiki/Pi_(letter"  # unbalanced `(` no closing `)`
        event = {"text": f"see <{url_with_paren}>"}
        links = adapter._extract_links(event)
        assert len(links) == 1
        assert links[0].url == url_with_paren

    def test_parses_bracketed_links_from_text_and_bounds_their_length(self):
        """Port of upstream ``parses bracketed links from text and bounds
        their length`` (vercel/chat#779).

        What to fix if this fails: ``_BRACKETED_URL_PATTERN`` in
        ``src/chat_sdk/adapters/slack/adapter.py`` must stay
        ``<(https?://[^>]{1,2048})>`` — bounded so the fallback scan stays
        linear on adversarial text, while ordinary links still parse.
        """
        adapter = _make_adapter()
        over_long = f"https://example.com/{'a' * 4000}"
        event = {
            "type": "message",
            "channel": "C123",
            "ts": "1234567890.123456",
            "text": f"ok <https://example.com/x> and <{over_long}>",
            "user": "U_USER",
        }

        urls = [link.url for link in adapter._extract_links(event)]

        assert urls == ["https://example.com/x"]


# ---------------------------------------------------------------------------
# Installation-scoped caches (vercel/chat#724 cache part, #877)
# ---------------------------------------------------------------------------


class TestInstallationScopedCaches:
    """Port of upstream ``describe("installation-scoped caches")``.

    In multi-workspace mode, installation-owned state (user profiles, the
    display-name reverse index, channel names, unfurl metadata) is keyed by
    the resolved installation so data fetched with one workspace's token is
    never served to another. Single-workspace mode keeps unscoped keys.

    The two ``withBotToken`` cases belong to #213 (they need
    ``with_bot_token(..., installation_id=...)``).

    What to fix if this fails: ``_installation_cache_scope`` /
    ``_unfurl_cache_key`` in ``src/chat_sdk/adapters/slack/adapter.py``
    and every key site that uses them.
    """

    @staticmethod
    def _users_info_mock() -> AsyncMock:
        return AsyncMock(
            return_value={
                "user": {
                    "name": "alice",
                    "profile": {"display_name": "Alice", "real_name": "Alice Example"},
                    "real_name": "Alice Example",
                }
            }
        )

    async def _make_cache_adapter(self) -> tuple[SlackAdapter, Any, MagicMock]:
        from chat_sdk.state.memory import MemoryStateAdapter

        state = MemoryStateAdapter()
        await state.connect()
        # No bot_token: multi-workspace mode.
        adapter = SlackAdapter(SlackAdapterConfig(signing_secret="test-signing-secret"))
        await adapter.initialize(_make_mock_chat(state))  # type: ignore[arg-type]
        client = MagicMock()
        client.users_info = self._users_info_mock()
        client.conversations_info = AsyncMock(return_value={"channel": {"name": "general"}})
        adapter._get_client = lambda token=None: client  # type: ignore[method-assign]
        return adapter, state, client

    @staticmethod
    async def _run_as(adapter: SlackAdapter, installation_id: str, fn: Any) -> Any:
        from chat_sdk.adapters.slack.types import RequestContext

        tok = adapter._request_context.set(
            RequestContext(token=f"xoxb-{installation_id}", installation_id=installation_id)
        )
        try:
            result = fn()
            if asyncio.iscoroutine(result):
                result = await result
            return result
        finally:
            adapter._request_context.reset(tok)

    @staticmethod
    async def _wait_for(predicate: Any) -> None:
        # Cache writes / invalidations are fire-and-forget tasks; give them
        # a bounded number of loop turns to land.
        for _ in range(20):
            if await predicate():
                return
            await asyncio.sleep(0)

    @pytest.mark.asyncio
    async def test_scopes_the_user_profile_cache_by_installation(self):
        adapter, state, client = await self._make_cache_adapter()

        await self._run_as(adapter, "T_A", lambda: adapter._lookup_user("U1"))
        await self._run_as(adapter, "T_B", lambda: adapter._lookup_user("U1"))

        # Each installation fetched and cached independently
        assert client.users_info.await_count == 2
        assert (await state.get("slack:user:T_A:U1"))["display_name"] == "Alice"
        assert (await state.get("slack:user:T_B:U1"))["display_name"] == "Alice"
        assert await state.get("slack:user:U1") is None

    @pytest.mark.asyncio
    async def test_scopes_the_channel_name_cache_by_installation(self):
        adapter, state, client = await self._make_cache_adapter()
        client.conversations_info = AsyncMock(
            side_effect=[
                {"channel": {"name": "team-a-private"}},
                {"channel": {"name": "team-b-general"}},
            ]
        )

        assert await self._run_as(adapter, "T_A", lambda: adapter._lookup_channel("C1")) == "team-a-private"
        assert await self._run_as(adapter, "T_B", lambda: adapter._lookup_channel("C1")) == "team-b-general"

        assert client.conversations_info.await_count == 2
        assert await state.get("slack:channel:T_A:C1") == {"name": "team-a-private"}
        assert await state.get("slack:channel:T_B:C1") == {"name": "team-b-general"}
        assert await state.get("slack:channel:C1") is None

    @pytest.mark.asyncio
    async def test_uses_unscoped_keys_without_a_request_context_single_workspace(self):
        from chat_sdk.state.memory import MemoryStateAdapter

        state = MemoryStateAdapter()
        await state.connect()
        adapter = _make_adapter(bot_user_id="U_BOT", bot_token="xoxb-single-token")
        await adapter.initialize(_make_mock_chat(state))  # type: ignore[arg-type]
        client = MagicMock()
        client.users_info = self._users_info_mock()
        adapter._get_client = lambda token=None: client  # type: ignore[method-assign]

        await adapter._lookup_user("U1")

        assert (await state.get("slack:user:U1"))["display_name"] == "Alice"
        assert await state.get_list("slack:user-by-name:alice") == ["U1"]

    @pytest.mark.asyncio
    async def test_empty_installation_id_uses_unscoped_keys(self):
        """Upstream ``installationId ? `${installationId}:` : ""``: only a
        truthy id scopes the key; ``""`` must not produce ``slack:user::U1``."""
        adapter, state, _ = await self._make_cache_adapter()

        await self._run_as(adapter, "", lambda: adapter._lookup_user("U1"))

        assert (await state.get("slack:user:U1"))["display_name"] == "Alice"
        assert await state.get("slack:user::U1") is None

    @pytest.mark.asyncio
    async def test_scopes_the_display_name_reverse_index_by_installation(self):
        adapter, state, _ = await self._make_cache_adapter()

        await self._run_as(adapter, "T_A", lambda: adapter._lookup_user("U1"))

        assert await state.get_list("slack:user-by-name:T_A:alice") == ["U1"]
        assert await state.get_list("slack:user-by-name:alice") == []

    @pytest.mark.asyncio
    async def test_scopes_unfurl_metadata_by_installation_and_channel(self):
        from chat_sdk.types import LinkPreview as _LinkPreview

        adapter, state, _ = await self._make_cache_adapter()

        def changed(channel: str, title: str) -> dict[str, Any]:
            return {
                "type": "message",
                "subtype": "message_changed",
                "hidden": True,
                "channel": channel,
                "ts": "1234567890.123456",
                "message": {
                    "type": "message",
                    "channel": channel,
                    "ts": "1111111111.111111",
                    "attachments": [{"from_url": "https://example.com/shared", "title": title}],
                },
            }

        await self._run_as(adapter, "T_A", lambda: adapter._handle_message_changed(changed("C1", "Team A")))
        await self._run_as(adapter, "T_B", lambda: adapter._handle_message_changed(changed("C2", "Team B")))

        async def both_written() -> bool:
            a = await state.get("slack:unfurls:T_A:C1:1111111111.111111")
            b = await state.get("slack:unfurls:T_B:C2:1111111111.111111")
            return a is not None and b is not None

        await self._wait_for(both_written)
        assert await both_written()
        assert await state.get("slack:unfurls:1111111111.111111") is None

        enriched_a = await self._run_as(
            adapter,
            "T_A",
            lambda: adapter._enrich_links([_LinkPreview(url="https://example.com/shared")], "C1", "1111111111.111111"),
        )
        enriched_b = await self._run_as(
            adapter,
            "T_B",
            lambda: adapter._enrich_links([_LinkPreview(url="https://example.com/shared")], "C2", "1111111111.111111"),
        )
        assert [link.title for link in enriched_a] == ["Team A"]
        assert [link.title for link in enriched_b] == ["Team B"]

    @pytest.mark.asyncio
    async def test_resolves_outgoing_mentions_from_the_installation_scoped_index(self):
        adapter, state, _ = await self._make_cache_adapter()
        await state.append_to_list("slack:user-by-name:T_A:alice", "U_ALICE_A")
        await state.append_to_list("slack:user-by-name:alice", "U_ALICE_GLOBAL")

        resolved = await self._run_as(
            adapter, "T_A", lambda: adapter._resolve_outgoing_mentions("hi @alice", "slack:C1:1.1")
        )

        assert resolved == "hi <@U_ALICE_A>"

    @pytest.mark.asyncio
    async def test_invalidates_the_scoped_cache_entry_on_user_change(self):
        adapter, state, _ = await self._make_cache_adapter()
        await self._run_as(adapter, "T_A", lambda: adapter._lookup_user("U1"))
        assert (await state.get("slack:user:T_A:U1"))["display_name"] == "Alice"

        await self._run_as(
            adapter, "T_A", lambda: adapter._handle_user_change({"type": "user_change", "user": {"id": "U1"}})
        )

        async def invalidated() -> bool:
            return await state.get("slack:user:T_A:U1") is None

        await self._wait_for(invalidated)
        assert await invalidated()
