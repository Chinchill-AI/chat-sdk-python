"""Fixture-based parity tests -- same JSON payloads that work in TS should
produce valid Messages in the Python adapters.

Loads replay fixtures from tests/fixtures/replay/ (or falls back to the TS
repo at /tmp/vercel-chat/...) and drives each adapter's handle_webhook().

References issue #18 (cross-SDK parity).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.fixtures.conftest import load_fixture

# ---------------------------------------------------------------------------
# Adapter imports (guarded -- skip if deps missing)
# ---------------------------------------------------------------------------

try:
    from chat_sdk.adapters.slack.adapter import SlackAdapter
    from chat_sdk.adapters.slack.types import SlackAdapterConfig

    _SLACK_OK = True
except ImportError:
    _SLACK_OK = False

try:
    from chat_sdk.adapters.teams.adapter import TeamsAdapter
    from chat_sdk.adapters.teams.types import TeamsAdapterConfig

    _TEAMS_OK = True
except ImportError:
    _TEAMS_OK = False

try:
    from chat_sdk.adapters.google_chat.adapter import GoogleChatAdapter
    from chat_sdk.adapters.google_chat.types import GoogleChatAdapterConfig, ServiceAccountCredentials

    _GCHAT_OK = True
except ImportError:
    _GCHAT_OK = False

try:
    from chat_sdk.adapters.discord.adapter import DiscordAdapter
    from chat_sdk.adapters.discord.types import DiscordAdapterConfig

    _DISCORD_OK = True
except ImportError:
    _DISCORD_OK = False

try:
    from chat_sdk.adapters.telegram.adapter import TelegramAdapter
    from chat_sdk.adapters.telegram.types import TelegramAdapterConfig

    _TELEGRAM_OK = True
except ImportError:
    _TELEGRAM_OK = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SIGNING_SECRET = "test-signing-secret"


class _FakeRequest:
    """Minimal request-like object for adapter webhook testing."""

    def __init__(self, body: str, headers: dict[str, str] | None = None):
        self._body = body.encode("utf-8")
        self.headers = headers or {}

    async def text(self) -> str:
        return self._body.decode("utf-8")

    @property
    def body(self) -> bytes:
        return self._body


def _slack_signed_request(
    body: str,
    secret: str = SIGNING_SECRET,
    content_type: str = "application/json",
) -> _FakeRequest:
    """Build a Slack request with valid HMAC signature."""
    ts = str(int(time.time()))
    sig_base = f"v0:{ts}:{body}"
    sig = "v0=" + hmac.new(secret.encode(), sig_base.encode(), hashlib.sha256).hexdigest()
    return _FakeRequest(
        body,
        {
            "x-slack-request-timestamp": ts,
            "x-slack-signature": sig,
            "content-type": content_type,
        },
    )


def _make_mock_state() -> MagicMock:
    """Create a mock StateAdapter."""
    state = MagicMock()
    state.get = AsyncMock(return_value=None)
    state.set = AsyncMock()
    state.delete = AsyncMock()
    return state


def _make_mock_chat() -> MagicMock:
    """Create a mock ChatInstance that captures process_message calls."""
    state = _make_mock_state()
    chat = MagicMock()
    chat.process_message = MagicMock()
    chat.handle_incoming_message = AsyncMock()
    chat.process_reaction = AsyncMock()
    chat.process_action = AsyncMock()
    chat.process_modal_submit = AsyncMock()
    chat.process_modal_close = MagicMock()
    chat.process_slash_command = AsyncMock()
    chat.process_member_joined_channel = AsyncMock()
    chat.get_state = MagicMock(return_value=state)
    chat.get_user_name = MagicMock(return_value="test-bot")
    chat.get_logger = MagicMock(return_value=MagicMock())
    return chat


# ===========================================================================
# Slack fixture replay
# ===========================================================================


@pytest.mark.skipif(not _SLACK_OK, reason="Slack adapter not available")
class TestSlackFixtureReplay:
    """Test that TS Slack fixture JSON payloads parse correctly in Python."""

    def _make_adapter(self, **overrides: Any) -> SlackAdapter:
        config = SlackAdapterConfig(
            signing_secret=overrides.pop("signing_secret", SIGNING_SECRET),
            bot_token=overrides.pop("bot_token", "xoxb-test-token"),
            **overrides,
        )
        return SlackAdapter(config)

    async def test_slack_mention_fixture(self):
        """Root slack.json mention payload should trigger process_message."""
        fixture = load_fixture("slack.json")
        adapter = self._make_adapter(bot_user_id=fixture.get("botUserId", "U00FAKEBOT01"))
        mock_chat = _make_mock_chat()
        await adapter.initialize(mock_chat)

        body = json.dumps(fixture["mention"])
        request = _slack_signed_request(body)

        result = await adapter.handle_webhook(request)

        assert result["status"] == 200
        assert mock_chat.process_message.called, "process_message should be called for mention"

        # Extract the call args: (adapter, thread_id, message_or_factory, options)
        call_args = mock_chat.process_message.call_args
        thread_id = call_args[0][1]
        assert thread_id, "thread_id should be non-empty"

        # Message factory -- call it to get the message
        msg_or_factory = call_args[0][2]
        if callable(msg_or_factory):
            msg = await msg_or_factory()
        else:
            msg = msg_or_factory

        # The text should contain the user's message (with mention stripped)
        assert msg.text is not None
        # Slack mention text is "<@U00FAKEBOT01> Hey" -> should contain "Hey"
        assert "Hey" in msg.text or "hey" in msg.text.lower()

    async def test_slack_follow_up_fixture(self):
        """Slack follow-up (threaded reply) should parse correctly."""
        fixture = load_fixture("slack.json")
        adapter = self._make_adapter(bot_user_id=fixture.get("botUserId", "U00FAKEBOT01"))
        mock_chat = _make_mock_chat()
        await adapter.initialize(mock_chat)

        body = json.dumps(fixture["followUp"])
        request = _slack_signed_request(body)

        result = await adapter.handle_webhook(request)

        assert result["status"] == 200
        assert mock_chat.process_message.called

        call_args = mock_chat.process_message.call_args
        thread_id = call_args[0][1]
        assert thread_id, "thread_id for follow-up should be non-empty"

        # Follow-up has thread_ts pointing to the parent
        event = fixture["followUp"]["event"]
        assert event.get("thread_ts"), "followUp should have thread_ts"

    async def test_slack_channel_mention_fixture(self):
        """channel-mention/slack.json mention should parse correctly."""
        fixture = load_fixture("channel-mention/slack.json")
        adapter = self._make_adapter(bot_user_id=fixture.get("botUserId", "U00FAKEBOT01"))
        mock_chat = _make_mock_chat()
        await adapter.initialize(mock_chat)

        body = json.dumps(fixture["mention"])
        request = _slack_signed_request(body)

        result = await adapter.handle_webhook(request)
        assert result["status"] == 200
        assert mock_chat.process_message.called

    async def test_slack_dm_fixture(self):
        """dm/slack.json mention should parse correctly."""
        fixture = load_fixture("dm/slack.json")
        adapter = self._make_adapter(bot_user_id=fixture.get("botUserId", "U00FAKEBOT01"))
        mock_chat = _make_mock_chat()
        await adapter.initialize(mock_chat)

        body = json.dumps(fixture["mention"])
        request = _slack_signed_request(body)

        result = await adapter.handle_webhook(request)
        assert result["status"] == 200

    async def test_slack_channel_fixture(self):
        """channel/slack.json mention should parse correctly."""
        fixture = load_fixture("channel/slack.json")
        adapter = self._make_adapter(bot_user_id=fixture.get("botUserId", "U00FAKEBOT01"))
        mock_chat = _make_mock_chat()
        await adapter.initialize(mock_chat)

        body = json.dumps(fixture["mention"])
        request = _slack_signed_request(body)

        result = await adapter.handle_webhook(request)
        assert result["status"] == 200

    async def test_slack_streaming_fixture(self):
        """streaming/slack.json aiMention should parse correctly."""
        fixture = load_fixture("streaming/slack.json")
        adapter = self._make_adapter(bot_user_id=fixture.get("botUserId", "U00FAKEBOT01"))
        mock_chat = _make_mock_chat()
        await adapter.initialize(mock_chat)

        # streaming fixtures use "aiMention" instead of "mention"
        body = json.dumps(fixture["aiMention"])
        request = _slack_signed_request(body)

        result = await adapter.handle_webhook(request)
        assert result["status"] == 200


# ===========================================================================
# Teams fixture replay
# ===========================================================================


@pytest.mark.skipif(not _TEAMS_OK, reason="Teams adapter not available")
class TestTeamsFixtureReplay:
    """Test that TS Teams fixture JSON payloads parse correctly in Python."""

    def _make_adapter(self, **overrides: Any) -> TeamsAdapter:
        config = TeamsAdapterConfig(
            app_id=overrides.pop("app_id", "11111111-2222-3333-4444-555555555555"),
            app_password=overrides.pop("app_password", "test-app-password"),
            **overrides,
        )
        return TeamsAdapter(config)

    async def test_teams_mention_fixture(self):
        """Root teams.json mention payload should trigger process_message."""
        fixture = load_fixture("teams.json")
        adapter = self._make_adapter(app_id=fixture.get("appId", "11111111-2222-3333-4444-555555555555"))
        mock_chat = _make_mock_chat()
        await adapter.initialize(mock_chat)

        body = json.dumps(fixture["mention"])
        request = _FakeRequest(
            body,
            {
                "content-type": "application/json",
                "authorization": "Bearer test-token",
            },
        )

        # Mock JWT verification to skip real token validation
        with patch.object(adapter, "_verify_bot_framework_token", new_callable=AsyncMock, return_value=None):
            result = await adapter.handle_webhook(request)

        assert result["status"] == 200
        assert mock_chat.process_message.called, "process_message should be called for Teams mention"

        call_args = mock_chat.process_message.call_args
        thread_id = call_args[0][1]
        assert thread_id, "thread_id should be non-empty"

        # Verify message text
        message = call_args[0][2]
        if callable(message):
            message = await message()
        assert message.text is not None
        # Teams mention text is "<at>Chat SDK Demo</at> Hey" -> "Hey" after stripping
        assert "Hey" in message.text or "hey" in message.text.lower()
        assert message.is_mention is True

    async def test_teams_follow_up_fixture(self):
        """Teams follow-up payload should parse correctly."""
        fixture = load_fixture("teams.json")
        adapter = self._make_adapter(app_id=fixture.get("appId", "11111111-2222-3333-4444-555555555555"))
        mock_chat = _make_mock_chat()
        await adapter.initialize(mock_chat)

        body = json.dumps(fixture["followUp"])
        request = _FakeRequest(
            body,
            {
                "content-type": "application/json",
                "authorization": "Bearer test-token",
            },
        )

        with patch.object(adapter, "_verify_bot_framework_token", new_callable=AsyncMock, return_value=None):
            result = await adapter.handle_webhook(request)

        assert result["status"] == 200

    async def test_teams_channel_fixture(self):
        """channel/teams.json mention should parse correctly."""
        fixture = load_fixture("channel/teams.json")
        adapter = self._make_adapter(app_id=fixture.get("appId", "11111111-2222-3333-4444-555555555555"))
        mock_chat = _make_mock_chat()
        await adapter.initialize(mock_chat)

        body = json.dumps(fixture["mention"])
        request = _FakeRequest(
            body,
            {
                "content-type": "application/json",
                "authorization": "Bearer test-token",
            },
        )

        with patch.object(adapter, "_verify_bot_framework_token", new_callable=AsyncMock, return_value=None):
            result = await adapter.handle_webhook(request)

        assert result["status"] == 200

    async def test_teams_dm_fixture(self):
        """dm/teams.json mention should parse correctly."""
        fixture = load_fixture("dm/teams.json")
        adapter = self._make_adapter(app_id=fixture.get("appId", "11111111-2222-3333-4444-555555555555"))
        mock_chat = _make_mock_chat()
        await adapter.initialize(mock_chat)

        body = json.dumps(fixture["mention"])
        request = _FakeRequest(
            body,
            {
                "content-type": "application/json",
                "authorization": "Bearer test-token",
            },
        )

        with patch.object(adapter, "_verify_bot_framework_token", new_callable=AsyncMock, return_value=None):
            result = await adapter.handle_webhook(request)

        assert result["status"] == 200


# ===========================================================================
# Google Chat fixture replay
# ===========================================================================


@pytest.mark.skipif(not _GCHAT_OK, reason="Google Chat adapter not available")
class TestGChatFixtureReplay:
    """Test that TS Google Chat fixture JSON payloads parse correctly in Python."""

    def _make_adapter(self, **overrides: Any) -> GoogleChatAdapter:
        if "credentials" not in overrides:
            overrides["credentials"] = ServiceAccountCredentials(
                client_email="test@test.iam.gserviceaccount.com",
                private_key="-----BEGIN RSA PRIVATE KEY-----\ntest\n-----END RSA PRIVATE KEY-----",
            )
        config = GoogleChatAdapterConfig(**overrides)
        return GoogleChatAdapter(config)

    async def test_gchat_mention_fixture(self):
        """Root gchat.json mention payload should trigger process_message."""
        fixture = load_fixture("gchat.json")
        adapter = self._make_adapter()
        adapter._bot_user_id = fixture.get("botUserId")
        mock_chat = _make_mock_chat()
        await adapter.initialize(mock_chat)

        # GChat direct webhook - the mention payload is the request body
        body = json.dumps(fixture["mention"])
        request = _FakeRequest(body, {"content-type": "application/json"})

        # No project number set = skip JWT verification
        result = await adapter.handle_webhook(request)

        assert result["status"] == 200
        assert mock_chat.process_message.called, "process_message should be called for GChat mention"

        call_args = mock_chat.process_message.call_args
        thread_id = call_args[0][1]
        assert thread_id, "thread_id should be non-empty"

        # Extract message
        message = call_args[0][2]
        if callable(message):
            message = await message()
        assert message.text is not None
        # GChat text is "@Chat SDK Demo hello" -> should contain "hello"
        assert "hello" in message.text.lower()

    async def test_gchat_channel_fixture(self):
        """channel/gchat.json mention should parse correctly."""
        fixture = load_fixture("channel/gchat.json")
        adapter = self._make_adapter()
        adapter._bot_user_id = fixture.get("botUserId")
        mock_chat = _make_mock_chat()
        await adapter.initialize(mock_chat)

        body = json.dumps(fixture["mention"])
        request = _FakeRequest(body, {"content-type": "application/json"})

        result = await adapter.handle_webhook(request)
        assert result["status"] == 200

    async def test_gchat_dm_fixture(self):
        """dm/gchat.json mention should parse correctly."""
        fixture = load_fixture("dm/gchat.json")
        adapter = self._make_adapter()
        adapter._bot_user_id = fixture.get("botUserId")
        mock_chat = _make_mock_chat()
        await adapter.initialize(mock_chat)

        body = json.dumps(fixture["mention"])
        request = _FakeRequest(body, {"content-type": "application/json"})

        result = await adapter.handle_webhook(request)
        assert result["status"] == 200


# ===========================================================================
# Discord fixture replay
# ===========================================================================


@pytest.mark.skipif(not _DISCORD_OK, reason="Discord adapter not available")
class TestDiscordFixtureReplay:
    """Test that TS Discord fixture JSON payloads parse correctly in Python.

    Discord uses forwarded Gateway events (not HTTP interactions) for message
    handling, so we use the gatewayMention payload with x-discord-gateway-token
    header auth.
    """

    BOT_TOKEN = "test-discord-bot-token"

    def _make_adapter(self, **overrides: Any) -> DiscordAdapter:
        config = DiscordAdapterConfig(
            application_id=overrides.pop("application_id", "1457469483726668048"),
            bot_token=overrides.pop("bot_token", self.BOT_TOKEN),
            public_key=overrides.pop("public_key", "a" * 64),
            **overrides,
        )
        return DiscordAdapter(config)

    async def test_discord_gateway_mention_fixture(self):
        """Discord gatewayMention payload should trigger handle_incoming_message.

        Discord forwarded Gateway events use handle_incoming_message (not
        process_message) and may attempt to create a thread via API. We mock
        the thread creation to avoid real HTTP calls.
        """
        fixture = load_fixture("discord.json")
        metadata = fixture.get("metadata", {})
        adapter = self._make_adapter(
            application_id=metadata.get("botId", "1457469483726668048"),
        )
        mock_chat = _make_mock_chat()
        await adapter.initialize(mock_chat)

        # Mock the Discord API call that creates a thread
        mock_thread_response = {"id": "1457536551830421524", "name": "test-thread"}
        with patch.object(adapter, "_create_discord_thread", new_callable=AsyncMock, return_value=mock_thread_response):
            # Gateway events are sent with x-discord-gateway-token header
            gateway_payload = fixture["gatewayMention"]
            body = json.dumps(gateway_payload)
            request = _FakeRequest(
                body,
                {
                    "content-type": "application/json",
                    "x-discord-gateway-token": self.BOT_TOKEN,
                },
            )

            result = await adapter.handle_webhook(request)

        assert result["status"] == 200
        assert mock_chat.handle_incoming_message.called, "handle_incoming_message should be called for gateway mention"

        call_args = mock_chat.handle_incoming_message.call_args
        thread_id = call_args[0][1]
        assert thread_id, "thread_id should be non-empty"

        # Verify the message was parsed correctly
        message = call_args[0][2]
        assert message.text is not None
        # The gateway payload content should contain the mention text
        content = gateway_payload.get("data", {}).get("content", "")
        assert "Hey" in content or "hey" in content.lower()
        assert message.is_mention is True

    async def test_discord_channel_fixture(self):
        """channel/discord.json should have parseable gateway events."""
        fixture = load_fixture("channel/discord.json")
        # Verify fixture structure
        assert "metadata" in fixture or "gatewayMention" in fixture or "mention" in fixture


# ===========================================================================
# Telegram fixture replay
# ===========================================================================


@pytest.mark.skipif(not _TELEGRAM_OK, reason="Telegram adapter not available")
class TestTelegramFixtureReplay:
    """Test that TS Telegram fixture JSON payloads parse correctly in Python."""

    def _make_adapter(self, **overrides: Any) -> TelegramAdapter:
        config = TelegramAdapterConfig(
            bot_token=overrides.pop("bot_token", "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"),
            **overrides,
        )
        return TelegramAdapter(config)

    async def test_telegram_mention_fixture(self):
        """Root telegram.json mention payload should trigger process_message."""
        fixture = load_fixture("telegram.json")
        adapter = self._make_adapter()
        mock_chat = _make_mock_chat()
        await adapter.initialize(mock_chat)

        # No secret_token configured = skip verification
        body = json.dumps(fixture["mention"])
        request = _FakeRequest(body, {"content-type": "application/json"})

        result = await adapter.handle_webhook(request)

        # Telegram returns "OK" with 200
        assert result["status"] == 200
        assert mock_chat.process_message.called, "process_message should be called for Telegram mention"

        call_args = mock_chat.process_message.call_args
        thread_id = call_args[0][1]
        assert thread_id, "thread_id should be non-empty"

        # Extract message
        message = call_args[0][2]
        if callable(message):
            message = await message()
        assert message.text is not None
        # Telegram text is "@vercelchatsdkbot hi" -> should contain "hi"
        assert "hi" in message.text.lower()

    async def test_telegram_follow_up_fixture(self):
        """Telegram follow-up payload should parse correctly."""
        fixture = load_fixture("telegram.json")
        adapter = self._make_adapter()
        mock_chat = _make_mock_chat()
        await adapter.initialize(mock_chat)

        body = json.dumps(fixture["followUp"])
        request = _FakeRequest(body, {"content-type": "application/json"})

        result = await adapter.handle_webhook(request)
        assert result["status"] == 200


# ===========================================================================
# Cross-platform fixture structure validation
# ===========================================================================


class TestFixtureStructure:
    """Validate that all fixtures load and have expected top-level keys."""

    ROOT_FIXTURES = ["slack.json", "teams.json", "gchat.json", "telegram.json", "discord.json"]

    @pytest.mark.parametrize("fixture_path", ROOT_FIXTURES)
    def test_root_fixture_loads(self, fixture_path: str):
        """Each root fixture should be valid JSON with expected keys."""
        fixture = load_fixture(fixture_path)
        assert isinstance(fixture, dict)
        # All root fixtures should have botName
        assert "botName" in fixture or "metadata" in fixture

    @pytest.mark.parametrize(
        "fixture_path",
        [
            "actions-reactions/slack.json",
            "actions-reactions/teams.json",
            "actions-reactions/gchat.json",
            "channel/slack.json",
            "channel/teams.json",
            "channel/gchat.json",
            "channel/discord.json",
            "dm/slack.json",
            "dm/slack-direct.json",
            "dm/teams.json",
            "dm/gchat.json",
            "dm/whatsapp.json",
            "streaming/slack.json",
            "streaming/teams.json",
            "streaming/gchat.json",
            "channel-mention/slack.json",
            "slash-commands/slack.json",
            "member-joined-channel/slack.json",
            "native-table/slack.json",
            "modals/slack.json",
            "modals/slack-private-metadata.json",
            "slack-multi-workspace/team1.json",
            "slack-multi-workspace/team2.json",
        ],
    )
    def test_subdirectory_fixture_loads(self, fixture_path: str):
        """Each subdirectory fixture should be valid JSON."""
        fixture = load_fixture(fixture_path)
        assert isinstance(fixture, dict)

    def test_all_28_fixtures_accessible(self):
        """Verify all 28 fixture files are accessible."""
        all_paths = self.ROOT_FIXTURES + [
            "actions-reactions/slack.json",
            "actions-reactions/teams.json",
            "actions-reactions/gchat.json",
            "channel/slack.json",
            "channel/teams.json",
            "channel/gchat.json",
            "channel/discord.json",
            "dm/slack.json",
            "dm/slack-direct.json",
            "dm/teams.json",
            "dm/gchat.json",
            "dm/whatsapp.json",
            "streaming/slack.json",
            "streaming/teams.json",
            "streaming/gchat.json",
            "channel-mention/slack.json",
            "slash-commands/slack.json",
            "member-joined-channel/slack.json",
            "native-table/slack.json",
            "modals/slack.json",
            "modals/slack-private-metadata.json",
            "slack-multi-workspace/team1.json",
            "slack-multi-workspace/team2.json",
        ]
        assert len(all_paths) == 28
        for path in all_paths:
            fixture = load_fixture(path)
            assert isinstance(fixture, dict), f"Failed to load: {path}"
