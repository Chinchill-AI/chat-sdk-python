"""Fixture-based replay tests -- real JSON payloads through real adapter code paths.

Loads replay fixtures from tests/fixtures/replay/ and drives each adapter's
handle_webhook() with the actual payloads, verifying that process_message (or
handle_incoming_message for Discord) is called with the correct message text,
thread_id, and author information.

This is the single most important test file in the Python SDK: it proves that
the same webhook payloads that work in the TypeScript SDK also work here.

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
    secret: str,
    content_type: str = "application/json",
) -> _FakeRequest:
    """Build a Slack request with valid HMAC-SHA256 signature.

    Uses the signing secret from the fixture (or a test default) to compute
    the v0= signature exactly the way Slack does.
    """
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


def _teams_request(body: str) -> _FakeRequest:
    """Build a Teams request (JWT verification mocked out in tests)."""
    return _FakeRequest(
        body,
        {
            "content-type": "application/json",
            "authorization": "Bearer test-token",
        },
    )


def _gchat_request(body: str) -> _FakeRequest:
    """Build a Google Chat request (JWT verification skipped when no project number)."""
    return _FakeRequest(body, {"content-type": "application/json"})


def _discord_gateway_request(body: str, bot_token: str) -> _FakeRequest:
    """Build a Discord forwarded Gateway event request."""
    return _FakeRequest(
        body,
        {
            "content-type": "application/json",
            "x-discord-gateway-token": bot_token,
        },
    )


def _telegram_request(body: str) -> _FakeRequest:
    """Build a Telegram webhook request (no secret token = skip verification)."""
    return _FakeRequest(body, {"content-type": "application/json"})


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


async def _extract_message(call_args: Any) -> Any:
    """Extract message from process_message call_args, resolving factory if needed."""
    msg_or_factory = call_args[0][2]
    if callable(msg_or_factory):
        return await msg_or_factory()
    return msg_or_factory


# ---------------------------------------------------------------------------
# Test signing secret -- used for all Slack fixtures
# ---------------------------------------------------------------------------
SLACK_SIGNING_SECRET = "test-signing-secret"

# Discord test bot token
DISCORD_BOT_TOKEN = "test-discord-bot-token"


# ===========================================================================
# Slack fixture replay
# ===========================================================================


@pytest.mark.skipif(not _SLACK_OK, reason="Slack adapter not available")
class TestSlackFixtureReplay:
    """Replay every Slack fixture through the real SlackAdapter.handle_webhook()."""

    def _make_adapter(self, fixture: dict[str, Any]) -> SlackAdapter:
        """Create a SlackAdapter configured from fixture metadata."""
        config = SlackAdapterConfig(
            signing_secret=SLACK_SIGNING_SECRET,
            bot_token="xoxb-test-token",
            bot_user_id=fixture.get("botUserId"),
        )
        return SlackAdapter(config)

    async def _send_and_assert_message(
        self,
        fixture: dict[str, Any],
        payload_key: str,
        expected_text_fragment: str | None = None,
    ) -> tuple[Any, str]:
        """Send a fixture payload through handle_webhook and assert process_message was called.

        Returns (message, thread_id) for additional assertions.
        """
        adapter = self._make_adapter(fixture)
        mock_chat = _make_mock_chat()
        await adapter.initialize(mock_chat)

        body = json.dumps(fixture[payload_key])
        request = _slack_signed_request(body, SLACK_SIGNING_SECRET)

        result = await adapter.handle_webhook(request)
        assert result["status"] == 200

        assert mock_chat.process_message.called, f"process_message should be called for Slack {payload_key}"

        call_args = mock_chat.process_message.call_args
        thread_id = call_args[0][1]
        assert thread_id, "thread_id should be non-empty"

        message = await _extract_message(call_args)
        assert message.text is not None

        if expected_text_fragment:
            assert expected_text_fragment.lower() in message.text.lower(), (
                f"Expected '{expected_text_fragment}' in message text '{message.text}'"
            )

        return message, thread_id

    # --- Root fixture (slack.json) ---

    async def test_root_mention(self):
        """slack.json mention: '<@BOT> Hey' should produce message containing 'Hey'."""
        fixture = load_fixture("slack.json")
        message, thread_id = await self._send_and_assert_message(fixture, "mention", "Hey")
        # Thread ID should contain the channel
        assert "C00FAKECHAN1" in thread_id
        # Author should be the user, not the bot
        assert message.author.user_id == fixture["mention"]["event"]["user"]

    async def test_root_follow_up(self):
        """slack.json followUp: threaded reply should have thread_ts in thread_id."""
        fixture = load_fixture("slack.json")
        message, thread_id = await self._send_and_assert_message(fixture, "followUp", "Hi")
        # Follow-up has thread_ts pointing to parent message
        event = fixture["followUp"]["event"]
        assert event["thread_ts"] == "1767224888.280449"
        # Thread ID should reference the parent thread
        assert "1767224888.280449" in thread_id

    # --- channel-mention/slack.json ---

    async def test_channel_mention(self):
        """channel-mention/slack.json mention should parse correctly."""
        fixture = load_fixture("channel-mention/slack.json")
        await self._send_and_assert_message(fixture, "mention")

    # --- dm/slack.json ---

    async def test_dm_mention(self):
        """dm/slack.json mention should parse correctly."""
        fixture = load_fixture("dm/slack.json")
        await self._send_and_assert_message(fixture, "mention")

    # --- dm/slack-direct.json ---

    async def test_dm_direct(self):
        """dm/slack-direct.json directDM: a non-mention DM should still trigger processing."""
        fixture = load_fixture("dm/slack-direct.json")
        message, _ = await self._send_and_assert_message(fixture, "directDM", "hello")

    # --- channel/slack.json ---

    async def test_channel_mention_fixture(self):
        """channel/slack.json mention should parse correctly."""
        fixture = load_fixture("channel/slack.json")
        await self._send_and_assert_message(fixture, "mention")

    # --- streaming/slack.json ---

    async def test_streaming_ai_mention(self):
        """streaming/slack.json aiMention should parse correctly."""
        fixture = load_fixture("streaming/slack.json")
        message, _ = await self._send_and_assert_message(fixture, "aiMention", "love")

    async def test_streaming_follow_up(self):
        """streaming/slack.json followUp should parse correctly."""
        fixture = load_fixture("streaming/slack.json")
        await self._send_and_assert_message(fixture, "followUp")

    # --- slack-multi-workspace fixtures ---

    async def test_multi_workspace_team1(self):
        """slack-multi-workspace/team1.json mention should parse correctly."""
        fixture = load_fixture("slack-multi-workspace/team1.json")
        message, _ = await self._send_and_assert_message(fixture, "mention", "testing")
        assert message.author.user_id == fixture["mention"]["event"]["user"]

    async def test_multi_workspace_team2(self):
        """slack-multi-workspace/team2.json mention should parse correctly."""
        fixture = load_fixture("slack-multi-workspace/team2.json")
        await self._send_and_assert_message(fixture, "mention")

    # --- member-joined-channel/slack.json ---

    async def test_member_joined_channel_mention(self):
        """member-joined-channel/slack.json mention should parse correctly."""
        fixture = load_fixture("member-joined-channel/slack.json")
        await self._send_and_assert_message(fixture, "mention")


# ===========================================================================
# Teams fixture replay
# ===========================================================================


@pytest.mark.skipif(not _TEAMS_OK, reason="Teams adapter not available")
class TestTeamsFixtureReplay:
    """Replay every Teams fixture through the real TeamsAdapter.handle_webhook()."""

    def _make_adapter(self, fixture: dict[str, Any]) -> TeamsAdapter:
        """Create a TeamsAdapter configured from fixture metadata."""
        app_id = fixture.get("appId", "11111111-2222-3333-4444-555555555555")
        config = TeamsAdapterConfig(
            app_id=app_id,
            app_password="test-app-password",
        )
        return TeamsAdapter(config)

    async def _send_and_assert_message(
        self,
        fixture: dict[str, Any],
        payload_key: str,
        expected_text_fragment: str | None = None,
    ) -> tuple[Any, str]:
        """Send a fixture payload and assert process_message was called."""
        adapter = self._make_adapter(fixture)
        mock_chat = _make_mock_chat()
        await adapter.initialize(mock_chat)

        body = json.dumps(fixture[payload_key])
        request = _teams_request(body)

        with patch.object(adapter, "_verify_bot_framework_token", new_callable=AsyncMock, return_value=None):
            result = await adapter.handle_webhook(request)

        assert result["status"] == 200

        assert mock_chat.process_message.called, f"process_message should be called for Teams {payload_key}"

        call_args = mock_chat.process_message.call_args
        thread_id = call_args[0][1]
        assert thread_id, "thread_id should be non-empty"

        message = await _extract_message(call_args)
        assert message.text is not None

        if expected_text_fragment:
            assert expected_text_fragment.lower() in message.text.lower(), (
                f"Expected '{expected_text_fragment}' in message text '{message.text}'"
            )

        return message, thread_id

    # --- Root fixture (teams.json) ---

    async def test_root_mention(self):
        """teams.json mention: '<at>Bot</at> Hey' should produce message containing 'Hey'."""
        fixture = load_fixture("teams.json")
        message, thread_id = await self._send_and_assert_message(fixture, "mention", "Hey")
        assert message.is_mention is True
        # Author should be from the fixture's 'from' field
        assert message.author.user_id == fixture["mention"]["from"]["id"]
        assert message.author.full_name == fixture["mention"]["from"]["name"]

    async def test_root_follow_up(self):
        """teams.json followUp: follow-up reply should parse correctly."""
        fixture = load_fixture("teams.json")
        message, thread_id = await self._send_and_assert_message(fixture, "followUp", "Hi")
        # Thread ID should be consistent with the mention (same conversation)
        assert thread_id, "follow-up thread_id should be non-empty"

    # --- channel/teams.json ---

    async def test_channel_mention(self):
        """channel/teams.json mention should parse correctly."""
        fixture = load_fixture("channel/teams.json")
        await self._send_and_assert_message(fixture, "mention")

    # --- dm/teams.json ---

    async def test_dm_mention(self):
        """dm/teams.json mention should parse correctly."""
        fixture = load_fixture("dm/teams.json")
        message, _ = await self._send_and_assert_message(fixture, "mention", "Hey")
        assert message.is_mention is True

    # --- streaming/teams.json ---

    async def test_streaming_ai_mention(self):
        """streaming/teams.json aiMention should parse correctly."""
        fixture = load_fixture("streaming/teams.json")
        message, _ = await self._send_and_assert_message(fixture, "aiMention", "love")
        assert message.is_mention is True

    async def test_streaming_follow_up(self):
        """streaming/teams.json followUp should parse correctly."""
        fixture = load_fixture("streaming/teams.json")
        await self._send_and_assert_message(fixture, "followUp")


# ===========================================================================
# Google Chat fixture replay
# ===========================================================================


@pytest.mark.skipif(not _GCHAT_OK, reason="Google Chat adapter not available")
class TestGChatFixtureReplay:
    """Replay every Google Chat fixture through the real GoogleChatAdapter.handle_webhook()."""

    def _make_adapter(self, fixture: dict[str, Any]) -> GoogleChatAdapter:
        """Create a GoogleChatAdapter configured from fixture metadata."""
        config = GoogleChatAdapterConfig(
            credentials=ServiceAccountCredentials(
                client_email="test@test.iam.gserviceaccount.com",
                private_key="-----BEGIN RSA PRIVATE KEY-----\ntest\n-----END RSA PRIVATE KEY-----",
            ),
        )
        adapter = GoogleChatAdapter(config)
        # Set bot user ID from fixture so the adapter can detect self-messages
        adapter._bot_user_id = fixture.get("botUserId")
        return adapter

    async def _send_and_assert_message(
        self,
        fixture: dict[str, Any],
        payload_key: str,
        expected_text_fragment: str | None = None,
    ) -> tuple[Any, str]:
        """Send a fixture payload and assert process_message was called."""
        adapter = self._make_adapter(fixture)
        mock_chat = _make_mock_chat()
        await adapter.initialize(mock_chat)

        body = json.dumps(fixture[payload_key])
        request = _gchat_request(body)

        # No project number configured = skip JWT verification
        result = await adapter.handle_webhook(request)

        assert result["status"] == 200

        assert mock_chat.process_message.called, f"process_message should be called for GChat {payload_key}"

        call_args = mock_chat.process_message.call_args
        thread_id = call_args[0][1]
        assert thread_id, "thread_id should be non-empty"

        message = await _extract_message(call_args)
        assert message.text is not None

        if expected_text_fragment:
            assert expected_text_fragment.lower() in message.text.lower(), (
                f"Expected '{expected_text_fragment}' in message text '{message.text}'"
            )

        return message, thread_id

    # --- Root fixture (gchat.json) ---

    async def test_root_mention(self):
        """gchat.json mention: '@Bot hello' should produce message containing 'hello'."""
        fixture = load_fixture("gchat.json")
        message, thread_id = await self._send_and_assert_message(fixture, "mention", "hello")
        # Author should be the human user
        assert message.author.is_bot is False

    async def test_root_follow_up(self):
        """gchat.json followUp: Pub/Sub push message should parse correctly."""
        fixture = load_fixture("gchat.json")
        adapter = self._make_adapter(fixture)
        mock_chat = _make_mock_chat()
        await adapter.initialize(mock_chat)

        body = json.dumps(fixture["followUp"])
        request = _gchat_request(body)

        result = await adapter.handle_webhook(request)
        assert result["status"] == 200

        # Follow-up via Pub/Sub triggers process_message
        assert mock_chat.process_message.called, "process_message should be called for GChat Pub/Sub follow-up"

        call_args = mock_chat.process_message.call_args
        thread_id = call_args[0][1]
        assert thread_id, "thread_id should be non-empty for Pub/Sub follow-up"

        message = await _extract_message(call_args)
        assert message.text is not None
        assert "hey" in message.text.lower()

    # --- channel/gchat.json ---

    async def test_channel_mention(self):
        """channel/gchat.json mention should parse correctly."""
        fixture = load_fixture("channel/gchat.json")
        await self._send_and_assert_message(fixture, "mention")

    # --- dm/gchat.json ---

    async def test_dm_mention(self):
        """dm/gchat.json mention should parse correctly."""
        fixture = load_fixture("dm/gchat.json")
        await self._send_and_assert_message(fixture, "mention")

    # --- streaming/gchat.json ---

    async def test_streaming_ai_mention(self):
        """streaming/gchat.json aiMention should parse correctly."""
        fixture = load_fixture("streaming/gchat.json")
        await self._send_and_assert_message(fixture, "aiMention")

    async def test_streaming_follow_up(self):
        """streaming/gchat.json followUp: Pub/Sub follow-up should parse correctly."""
        fixture = load_fixture("streaming/gchat.json")
        adapter = self._make_adapter(fixture)
        mock_chat = _make_mock_chat()
        await adapter.initialize(mock_chat)

        body = json.dumps(fixture["followUp"])
        request = _gchat_request(body)

        result = await adapter.handle_webhook(request)
        assert result["status"] == 200

        assert mock_chat.process_message.called, "process_message should be called for streaming GChat follow-up"


# ===========================================================================
# Discord fixture replay
# ===========================================================================


@pytest.mark.skipif(not _DISCORD_OK, reason="Discord adapter not available")
class TestDiscordFixtureReplay:
    """Replay every Discord fixture through the real DiscordAdapter.handle_webhook().

    Discord uses forwarded Gateway events (not HTTP interactions) for message
    handling, authenticated via x-discord-gateway-token header.
    """

    def _make_adapter(self, fixture: dict[str, Any]) -> DiscordAdapter:
        """Create a DiscordAdapter configured from fixture metadata."""
        metadata = fixture.get("metadata", {})
        config = DiscordAdapterConfig(
            application_id=metadata.get("botId", fixture.get("applicationId", "1457469483726668048")),
            bot_token=DISCORD_BOT_TOKEN,
            public_key="a" * 64,  # Not used for gateway events
        )
        return DiscordAdapter(config)

    async def _send_gateway_and_assert(
        self,
        fixture: dict[str, Any],
        payload_key: str,
        expected_text_fragment: str | None = None,
    ) -> tuple[Any, str]:
        """Send a gateway fixture payload and assert handle_incoming_message was called.

        Discord gateway events go through handle_incoming_message (not
        process_message), and the adapter may attempt to create a thread.
        """
        adapter = self._make_adapter(fixture)
        mock_chat = _make_mock_chat()
        await adapter.initialize(mock_chat)

        payload = fixture[payload_key]
        body = json.dumps(payload)
        request = _discord_gateway_request(body, DISCORD_BOT_TOKEN)

        # Mock thread creation (avoids real HTTP calls to Discord API)
        mock_thread_response = {"id": "mock-thread-id-123", "name": "test-thread"}
        with patch.object(adapter, "_create_discord_thread", new_callable=AsyncMock, return_value=mock_thread_response):
            result = await adapter.handle_webhook(request)

        assert result["status"] == 200

        assert mock_chat.handle_incoming_message.called, (
            f"handle_incoming_message should be called for Discord {payload_key}"
        )

        call_args = mock_chat.handle_incoming_message.call_args
        thread_id = call_args[0][1]
        assert thread_id, "thread_id should be non-empty"

        message = call_args[0][2]
        assert message.text is not None

        if expected_text_fragment:
            assert expected_text_fragment.lower() in message.text.lower(), (
                f"Expected '{expected_text_fragment}' in message text '{message.text}'"
            )

        return message, thread_id

    # --- Root fixture (discord.json) ---

    async def test_root_gateway_mention(self):
        """discord.json gatewayMention: '<@BOT> Hey' should produce message with 'Hey'."""
        fixture = load_fixture("discord.json")
        message, thread_id = await self._send_gateway_and_assert(fixture, "gatewayMention", "Hey")
        assert message.is_mention is True
        # Author should be the human user
        metadata = fixture["metadata"]
        assert message.author.user_id == metadata["userId"]

    async def test_root_gateway_ai_mention(self):
        """discord.json gatewayAIMention: '<@BOT> AI What is love' should parse."""
        fixture = load_fixture("discord.json")
        message, _ = await self._send_gateway_and_assert(fixture, "gatewayAIMention", "love")
        assert message.is_mention is True

    async def test_root_gateway_subscribed_message(self):
        """discord.json gatewaySubscribedMessage: in-thread message should parse."""
        fixture = load_fixture("discord.json")
        adapter = self._make_adapter(fixture)
        mock_chat = _make_mock_chat()
        await adapter.initialize(mock_chat)

        payload = fixture["gatewaySubscribedMessage"]
        body = json.dumps(payload)
        request = _discord_gateway_request(body, DISCORD_BOT_TOKEN)

        with patch.object(
            adapter,
            "_create_discord_thread",
            new_callable=AsyncMock,
            return_value={"id": "mock-thread-id", "name": "test-thread"},
        ):
            result = await adapter.handle_webhook(request)

        assert result["status"] == 200
        # Subscribed messages (non-mention in-thread) should still be processed
        assert mock_chat.handle_incoming_message.called

    async def test_root_gateway_dm_request(self):
        """discord.json gatewayDMRequest: DM to bot should parse correctly."""
        fixture = load_fixture("discord.json")
        adapter = self._make_adapter(fixture)
        mock_chat = _make_mock_chat()
        await adapter.initialize(mock_chat)

        payload = fixture["gatewayDMRequest"]
        body = json.dumps(payload)
        request = _discord_gateway_request(body, DISCORD_BOT_TOKEN)

        with patch.object(
            adapter,
            "_create_discord_thread",
            new_callable=AsyncMock,
            return_value={"id": "mock-thread-id", "name": "test-thread"},
        ):
            result = await adapter.handle_webhook(request)

        assert result["status"] == 200
        assert mock_chat.handle_incoming_message.called

    # --- channel/discord.json ---

    async def test_channel_mention(self):
        """channel/discord.json mention (gateway) should parse correctly."""
        fixture = load_fixture("channel/discord.json")
        message, _ = await self._send_gateway_and_assert(fixture, "mention", "Test")
        assert message.is_mention is True


# ===========================================================================
# Telegram fixture replay
# ===========================================================================


@pytest.mark.skipif(not _TELEGRAM_OK, reason="Telegram adapter not available")
class TestTelegramFixtureReplay:
    """Replay every Telegram fixture through the real TelegramAdapter.handle_webhook()."""

    def _make_adapter(self, fixture: dict[str, Any]) -> TelegramAdapter:
        """Create a TelegramAdapter configured from fixture metadata."""
        config = TelegramAdapterConfig(
            bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            # No secret_token = skip verification (matches TS behavior)
        )
        return TelegramAdapter(config)

    async def _send_and_assert_message(
        self,
        fixture: dict[str, Any],
        payload_key: str,
        expected_text_fragment: str | None = None,
    ) -> tuple[Any, str]:
        """Send a fixture payload and assert process_message was called."""
        adapter = self._make_adapter(fixture)
        mock_chat = _make_mock_chat()
        await adapter.initialize(mock_chat)

        body = json.dumps(fixture[payload_key])
        request = _telegram_request(body)

        result = await adapter.handle_webhook(request)
        assert result["status"] == 200

        assert mock_chat.process_message.called, f"process_message should be called for Telegram {payload_key}"

        call_args = mock_chat.process_message.call_args
        thread_id = call_args[0][1]
        assert thread_id, "thread_id should be non-empty"

        message = await _extract_message(call_args)
        assert message.text is not None

        if expected_text_fragment:
            assert expected_text_fragment.lower() in message.text.lower(), (
                f"Expected '{expected_text_fragment}' in message text '{message.text}'"
            )

        return message, thread_id

    # --- Root fixture (telegram.json) ---

    async def test_root_mention(self):
        """telegram.json mention: '@bot hi' should produce message containing 'hi'."""
        fixture = load_fixture("telegram.json")
        message, thread_id = await self._send_and_assert_message(fixture, "mention", "hi")
        # Author should be the Telegram user
        assert message.author.user_name == "telegram_test_user"
        assert message.author.is_bot is False

    async def test_root_follow_up(self):
        """telegram.json followUp: follow-up message should parse correctly."""
        fixture = load_fixture("telegram.json")
        message, _ = await self._send_and_assert_message(fixture, "followUp", "how are you")


# ===========================================================================
# Cross-platform fixture structure validation
# ===========================================================================


class TestFixtureStructure:
    """Validate that all 28 fixtures load and have expected top-level keys."""

    ROOT_FIXTURES = ["slack.json", "teams.json", "gchat.json", "telegram.json", "discord.json"]

    @pytest.mark.parametrize("fixture_path", ROOT_FIXTURES)
    def test_root_fixture_loads(self, fixture_path: str):
        """Each root fixture should be valid JSON with expected keys."""
        fixture = load_fixture(fixture_path)
        assert isinstance(fixture, dict)
        # All root fixtures should have botName or metadata
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

    @pytest.mark.parametrize(
        "fixture_path,expected_key",
        [
            ("slack.json", "mention"),
            ("slack.json", "followUp"),
            ("teams.json", "mention"),
            ("teams.json", "followUp"),
            ("gchat.json", "mention"),
            ("gchat.json", "followUp"),
            ("telegram.json", "mention"),
            ("telegram.json", "followUp"),
            ("discord.json", "gatewayMention"),
            ("streaming/slack.json", "aiMention"),
            ("streaming/teams.json", "aiMention"),
            ("streaming/gchat.json", "aiMention"),
            ("dm/slack.json", "mention"),
            ("dm/teams.json", "mention"),
            ("dm/gchat.json", "mention"),
            ("channel/slack.json", "mention"),
            ("channel/teams.json", "mention"),
            ("channel/gchat.json", "mention"),
            ("channel/discord.json", "mention"),
            ("channel-mention/slack.json", "mention"),
        ],
    )
    def test_fixture_has_scenario_key(self, fixture_path: str, expected_key: str):
        """Each fixture should have the expected scenario payload key."""
        fixture = load_fixture(fixture_path)
        assert expected_key in fixture, (
            f"Fixture '{fixture_path}' missing key '{expected_key}'. Available keys: {list(fixture.keys())}"
        )
