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

        # Stub the network GET — assert the tenant token is forwarded.
        fetch_mock = AsyncMock(return_value=b"workspace-bytes")
        adapter._fetch_slack_file = fetch_mock  # type: ignore[method-assign]

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
        fetch_mock.assert_awaited_once_with(
            "https://files.slack.com/img.png",
            "xoxb-multi-workspace-token",
        )

    # TS: "should fall back to getToken when no teamId in fetchMetadata"
    @pytest.mark.asyncio
    async def test_should_fall_back_to_gettoken_when_no_teamid_in_fetchmetadata(self):
        from chat_sdk.types import Attachment

        adapter = _make_adapter(bot_token="xoxb-single")
        fetch_mock = AsyncMock(return_value=b"single-bytes")
        adapter._fetch_slack_file = fetch_mock  # type: ignore[method-assign]

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
        fetch_mock.assert_awaited_once_with(
            "https://files.slack.com/img.png",
            "xoxb-single",
        )

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

        adapter = _make_adapter(bot_token="xoxb-ssrf-token")

        # Sentinel — should NEVER be reached because validation rejects first.
        evil_fetch = AsyncMock(return_value=b"should-not-run")
        adapter._fetch_slack_file = evil_fetch  # type: ignore[method-assign]
        # Restore the real validator + wrap it so we can assert on behavior.
        real_fetch = SlackAdapter._fetch_slack_file

        async def guarded_fetch(url: str, token: str) -> bytes:
            return await real_fetch(adapter, url, token)

        adapter._fetch_slack_file = guarded_fetch  # type: ignore[method-assign]

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

    @pytest.mark.asyncio
    async def test_message_changed_does_not_route_to_process_message(self):
        # ``message_changed`` is now routed to the unfurl-cache handler
        # (see TestUnfurlMetadata) rather than into ``chat.process_message``.
        # The visible contract from the chat layer's perspective is the
        # same: message_changed events do not surface as new messages.
        adapter = _make_adapter(bot_user_id="U_BOT")
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)
        await adapter.handle_webhook(self._make_subtype_req("message_changed"))
        assert not chat.process_message.called

    @pytest.mark.asyncio
    async def test_ignores_message_deleted(self):
        adapter = _make_adapter(bot_user_id="U_BOT")
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)
        await adapter.handle_webhook(self._make_subtype_req("message_deleted"))
        assert not chat.process_message.called

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
      keyed by ``slack:unfurls:{ts}``.
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
        cached = state._cache.get("slack:unfurls:1234567890.111111")
        assert cached is not None
        assert cached["https://example.com"]["title"] == "Cached Title"
        # And process_message must NOT be called for message_changed.
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
        state._cache["slack:unfurls:1234567890.111111"] = {
            "https://example.com": {
                "title": "From Cache",
                "description": "Cached body",
                "image_url": None,
                "site_name": None,
            }
        }

        original = [_LinkPreview(url="https://example.com")]
        enriched = await adapter._enrich_links(original, "1234567890.111111")
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

        state._cache["slack:unfurls:t1"] = {
            "https://example.com": {"title": "From Cache", "description": None},
        }
        original = [_LinkPreview(url="https://example.com", title="User Title")]
        enriched = await adapter._enrich_links(original, "t1")
        assert enriched[0].title == "User Title"

    @pytest.mark.asyncio
    async def test_enrich_links_returns_unchanged_with_no_chat(self):
        # When chat isn't initialized, enrichment is a no-op.
        from chat_sdk.types import LinkPreview as _LinkPreview

        adapter = _make_adapter()
        original = [_LinkPreview(url="https://example.com")]
        enriched = await adapter._enrich_links(original, "ts1")
        assert enriched is original

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

        state._cache["slack:unfurls:t-override"] = {
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
        enriched = await adapter._enrich_links(original, "t-override")
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
        first = state._cache.get("slack:unfurls:1234567890.111111")
        assert first is not None
        assert "https://a.example.com" in first

        # Second edit caches an unfurl for a DIFFERENT URL (URL_B).
        # If the implementation merged, URL_A would still be in the cache.
        await adapter.handle_webhook(_make_signed_request(_make_changed_body("https://b.example.com", "Second")))
        await asyncio.sleep(0)
        second = state._cache.get("slack:unfurls:1234567890.111111")
        assert second is not None
        assert "https://b.example.com" in second
        assert "https://a.example.com" not in second, "second message_changed must overwrite, not merge"
        assert second["https://b.example.com"]["title"] == "Second"

    def test_extract_links_url_with_open_paren_survives_parser(self):
        """A URL containing ``(`` (unbalanced open paren) is preserved.

        Slack delivers URLs in angle brackets — ``<URL>`` — which the
        adapter parses with ``re.finditer(r"<(https?://[^>]+)>", ...)``.
        The character class ``[^>]+`` accepts ``(`` so a URL such as
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
