"""Validate that adapter dispatch dicts use snake_case keys, not camelCase.

This test suite would have caught the systemic bug where adapters passed
camelCase keys (e.g. ``threadId``, ``messageId``, ``actionId``) in the
event dicts sent to ``self._chat.process_*`` methods.  The Chat class
expects snake_case keys throughout, so camelCase keys silently produce
``KeyError`` or ``None`` lookups downstream.

Strategy:
  1. Create each adapter with minimal config.
  2. Replace ``adapter._chat`` with a mock that records calls.
  3. Call the adapter's internal dispatch method with a realistic payload.
  4. Assert every key in the dict passed to ``process_*`` is snake_case.
"""

from __future__ import annotations

import asyncio
import dataclasses
import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# camelCase detection helper
# ---------------------------------------------------------------------------

CAMEL_RE = re.compile(r"^[a-z]+[A-Z]")

# Keys whose values are raw platform payloads -- these intentionally
# preserve the platform's native casing and should NOT be checked.
_RAW_PASSTHROUGH_KEYS = frozenset({"raw"})


def _to_dict(obj: Any) -> Any:
    """Convert dataclass instances to dicts for inspection.

    Plain dicts and other types are returned as-is.
    """
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: getattr(obj, f.name) for f in dataclasses.fields(obj)}
    return obj


def assert_no_camel_case_keys(
    d: Any,
    path: str = "",
    *,
    skip_raw: bool = True,
) -> None:
    """Recursively check that no dict keys use camelCase.

    Raises ``AssertionError`` with a descriptive message when a camelCase
    key is found.  Nested dicts, lists, and dataclass instances are traversed.

    When *skip_raw* is ``True`` (the default), the ``"raw"`` key's value
    is not inspected, because adapters intentionally pass through the
    original platform event under that key and its casing is outside
    the adapter's control.
    """
    d = _to_dict(d)
    if isinstance(d, dict):
        for k, v in d.items():
            if isinstance(k, str) and CAMEL_RE.match(k):
                raise AssertionError(f"camelCase key '{k}' found at {path}.{k}")
            if skip_raw and k in _RAW_PASSTHROUGH_KEYS:
                continue
            assert_no_camel_case_keys(v, f"{path}.{k}", skip_raw=skip_raw)
    elif isinstance(d, (list, tuple)):
        for i, item in enumerate(d):
            assert_no_camel_case_keys(item, f"{path}[{i}]", skip_raw=skip_raw)


def _collect_keys_flat(d: Any) -> set[str]:
    """Collect all string keys from a dict or dataclass (top-level only)."""
    d = _to_dict(d)
    if isinstance(d, dict):
        return {k for k in d if isinstance(k, str)}
    return set()


# ---------------------------------------------------------------------------
# Mock chat instance
# ---------------------------------------------------------------------------


def _make_mock_chat() -> MagicMock:
    """Create a mock ChatInstance that records all process_* calls."""
    mock = MagicMock()
    mock.process_message = MagicMock()
    mock.process_action = MagicMock()
    mock.process_reaction = MagicMock()
    mock.process_slash_command = MagicMock()
    mock.process_modal_submit = AsyncMock(return_value=None)
    mock.process_modal_close = MagicMock()
    mock.process_assistant_thread_started = MagicMock()
    mock.process_assistant_context_changed = MagicMock()
    mock.process_app_home_opened = MagicMock()
    mock.process_member_joined_channel = MagicMock()
    # get_state needed by some adapters
    mock_state = MagicMock()
    mock_state.get = AsyncMock(return_value=None)
    mock.get_state = MagicMock(return_value=mock_state)
    mock.get_logger = MagicMock(return_value=MagicMock())
    mock.get_user_name = MagicMock(return_value="bot")
    return mock


# ===========================================================================
# Slack adapter tests
# ===========================================================================


class TestSlackDispatchKeys:
    """Verify Slack adapter passes snake_case keys to process_* methods."""

    def _make_adapter(self) -> Any:
        from chat_sdk.adapters.slack.adapter import SlackAdapter
        from chat_sdk.adapters.slack.types import SlackAdapterConfig

        adapter = SlackAdapter(
            SlackAdapterConfig(
                signing_secret="test_signing_secret",
                bot_token="xoxb-test-token",
            )
        )
        adapter._chat = _make_mock_chat()
        adapter._bot_user_id = "U_BOT"
        return adapter

    def test_slack_message_dispatch_keys(self) -> None:
        """Message events use a factory function; verify process_message is called."""
        adapter = self._make_adapter()

        event = {
            "type": "message",
            "channel": "C12345",
            "ts": "1234567890.123456",
            "thread_ts": "1234567890.000001",
            "user": "U_USER",
            "text": "hello world",
        }

        adapter._handle_message_event(event)

        adapter._chat.process_message.assert_called_once()
        # process_message(self, thread_id, factory_or_message, options)
        # The Slack adapter passes a factory coroutine, not a dict, so the
        # key-validation concern is in the Message object (not a plain dict).
        # The key contract here is that it is called with (adapter, thread_id_str, ...).
        call_args = adapter._chat.process_message.call_args
        assert call_args is not None
        # thread_id should be a snake_case-formatted string
        _adapter_arg, thread_id_arg = call_args[0][0], call_args[0][1]
        assert isinstance(thread_id_arg, str)
        assert thread_id_arg.startswith("slack:")

    async def test_slack_reaction_dispatch_keys(self) -> None:
        """Reaction events should produce a dict with snake_case keys."""
        adapter = self._make_adapter()

        event = {
            "type": "reaction_added",
            "reaction": "thumbsup",
            "user": "U_USER",
            "item": {
                "type": "message",
                "channel": "C12345",
                "ts": "1234567890.123456",
            },
            "item_user": "U_OTHER",
            "event_ts": "1234567890.999999",
        }

        # The Slack reaction handler is async (it launches a task to resolve
        # the parent thread_ts).  We need to mock the Slack client so the
        # async resolution succeeds.
        mock_client = AsyncMock()
        mock_client.conversations_replies = AsyncMock(
            return_value={
                "messages": [
                    {"ts": "1234567890.123456", "thread_ts": "1234567890.000001"},
                ],
            }
        )
        adapter._get_client = MagicMock(return_value=mock_client)

        adapter._handle_reaction_event(event)

        # The handler uses asyncio.ensure_future() which schedules the
        # coroutine on the running event loop.  Give it a chance to run.
        await asyncio.sleep(0.05)

        adapter._chat.process_reaction.assert_called_once()
        reaction_obj = adapter._chat.process_reaction.call_args[0][0]

        # Verify required snake_case fields (dataclass or dict)
        assert hasattr(reaction_obj, "thread_id") or "thread_id" in reaction_obj
        assert hasattr(reaction_obj, "message_id") or "message_id" in reaction_obj
        assert hasattr(reaction_obj, "raw_emoji") or "raw_emoji" in reaction_obj

        # Verify NO camelCase keys anywhere
        assert_no_camel_case_keys(reaction_obj)

    def test_slack_action_dispatch_keys(self) -> None:
        """Block action events should produce dicts with snake_case keys."""
        adapter = self._make_adapter()

        payload = {
            "type": "block_actions",
            "trigger_id": "T_TRIGGER",
            "user": {"id": "U_USER", "username": "testuser", "name": "Test User"},
            "channel": {"id": "C12345"},
            "message": {
                "ts": "1234567890.123456",
                "thread_ts": "1234567890.000001",
            },
            "actions": [
                {
                    "action_id": "approve_btn",
                    "type": "button",
                    "value": "yes",
                },
            ],
        }

        adapter._handle_block_actions(payload)

        adapter._chat.process_action.assert_called_once()
        action_obj = adapter._chat.process_action.call_args[0][0]

        # Verify required snake_case fields (dataclass or dict)
        assert hasattr(action_obj, "action_id") or "action_id" in action_obj
        assert hasattr(action_obj, "message_id") or "message_id" in action_obj
        assert hasattr(action_obj, "thread_id") or "thread_id" in action_obj
        assert hasattr(action_obj, "trigger_id") or "trigger_id" in action_obj

        # Verify NO camelCase keys anywhere
        assert_no_camel_case_keys(action_obj)

    async def test_slack_slash_command_dispatch_keys(self) -> None:
        """Slash command events should produce dicts with snake_case keys."""
        adapter = self._make_adapter()

        # Mock _lookup_user since it does API calls
        adapter._lookup_user = AsyncMock(return_value={"display_name": "testuser", "real_name": "Test User"})

        params = {
            "command": ["/test"],
            "text": ["hello world"],
            "user_id": ["U_USER"],
            "channel_id": ["C12345"],
            "trigger_id": ["T_TRIGGER"],
        }

        await adapter._handle_slash_command(params)

        adapter._chat.process_slash_command.assert_called_once()
        cmd_obj = adapter._chat.process_slash_command.call_args[0][0]

        # Verify required snake_case fields (dataclass or dict)
        assert hasattr(cmd_obj, "user") or "user_id" in cmd_obj or "user" in cmd_obj
        assert hasattr(cmd_obj, "channel_id") or "channel_id" in cmd_obj
        assert hasattr(cmd_obj, "trigger_id") or "trigger_id" in cmd_obj

        # Verify NO camelCase keys anywhere
        assert_no_camel_case_keys(cmd_obj)

    async def test_slack_modal_submit_dispatch_keys(self) -> None:
        """View submission events should produce dicts with snake_case keys."""
        adapter = self._make_adapter()

        payload = {
            "type": "view_submission",
            "user": {"id": "U_USER", "username": "testuser", "name": "Test User"},
            "view": {
                "id": "V12345",
                "callback_id": "my_modal",
                "state": {
                    "values": {
                        "block1": {
                            "input1": {"value": "hello"},
                        },
                    },
                },
                "private_metadata": "",
            },
        }

        await adapter._handle_view_submission(payload)

        adapter._chat.process_modal_submit.assert_called_once()
        modal_obj = adapter._chat.process_modal_submit.call_args[0][0]

        # Verify required snake_case fields (dataclass or dict)
        assert hasattr(modal_obj, "callback_id") or "callback_id" in modal_obj
        assert hasattr(modal_obj, "view_id") or "view_id" in modal_obj

        # Check for the known camelCase bug: "privateMetadata" should be
        # "private_metadata" (this is the exact bug this test catches).
        top_keys = _collect_keys_flat(modal_obj)
        assert "privateMetadata" not in top_keys, (
            "camelCase key 'privateMetadata' found -- should be 'private_metadata'"
        )

        # Also verify the user sub-object uses snake_case
        user = getattr(modal_obj, "user", None) if hasattr(modal_obj, "user") else modal_obj.get("user")
        if user is not None:
            assert hasattr(user, "user_id") or (isinstance(user, dict) and "user_id" in user)
            assert_no_camel_case_keys(user)

        # Full recursive check
        assert_no_camel_case_keys(modal_obj)


# ===========================================================================
# Google Chat adapter tests
# ===========================================================================


class TestGoogleChatDispatchKeys:
    """Verify Google Chat adapter passes snake_case keys to process_* methods."""

    def _make_adapter(self) -> Any:
        from chat_sdk.adapters.google_chat.adapter import GoogleChatAdapter
        from chat_sdk.adapters.google_chat.types import GoogleChatAdapterConfig

        adapter = GoogleChatAdapter(GoogleChatAdapterConfig(use_application_default_credentials=True))
        adapter._chat = _make_mock_chat()
        adapter._bot_user_id = "users/bot123"
        return adapter

    def test_gchat_action_dispatch_keys(self) -> None:
        """CARD_CLICKED events should produce dicts with snake_case keys."""
        adapter = self._make_adapter()

        event = {
            "commonEventObject": {
                "invokedFunction": "approve_action",
                "parameters": {
                    "actionId": "approve_action",
                    "value": "yes",
                },
            },
            "chat": {
                "buttonClickedPayload": {
                    "space": {"name": "spaces/AAAA"},
                    "message": {
                        "name": "spaces/AAAA/messages/msg123",
                        "thread": {"name": "spaces/AAAA/threads/thread123"},
                    },
                    "user": {
                        "name": "users/user123",
                        "displayName": "Test User",
                        "type": "HUMAN",
                    },
                },
            },
        }

        adapter._handle_card_click(event)

        adapter._chat.process_action.assert_called_once()
        action_dict = adapter._chat.process_action.call_args[0][0]

        # Verify required snake_case keys (dataclass or dict)
        assert hasattr(action_dict, "action_id") or "action_id" in action_dict
        assert hasattr(action_dict, "message_id") or "message_id" in action_dict
        assert hasattr(action_dict, "thread_id") or "thread_id" in action_dict

        # Verify NO camelCase keys at the top level of the event dict
        assert_no_camel_case_keys(action_dict)


# ===========================================================================
# Discord adapter tests
# ===========================================================================


class TestDiscordDispatchKeys:
    """Verify Discord adapter passes snake_case keys to process_* methods."""

    def _make_adapter(self) -> Any:
        from chat_sdk.adapters.discord.adapter import DiscordAdapter
        from chat_sdk.adapters.discord.types import DiscordAdapterConfig

        adapter = DiscordAdapter(
            DiscordAdapterConfig(
                bot_token="test-bot-token",
                public_key="a" * 64,  # 64-char hex string
                application_id="APP123",
            )
        )
        adapter._chat = _make_mock_chat()
        return adapter

    def test_discord_action_dispatch_keys(self) -> None:
        """MESSAGE_COMPONENT interactions should produce dicts with snake_case keys."""
        adapter = self._make_adapter()

        interaction = {
            "type": 3,  # MESSAGE_COMPONENT
            "data": {"custom_id": "approve_btn"},
            "member": {
                "user": {
                    "id": "USER123",
                    "username": "testuser",
                    "global_name": "Test User",
                },
            },
            "channel_id": "CH123",
            "guild_id": "GUILD123",
            "message": {"id": "MSG123"},
            "channel": {"type": 0},
            "token": "interaction_token",
        }

        adapter._handle_component_interaction(interaction)

        adapter._chat.process_action.assert_called_once()
        action_dict = adapter._chat.process_action.call_args[0][0]

        # Verify required snake_case fields (dataclass or dict)
        assert hasattr(action_dict, "action_id") or "action_id" in action_dict
        assert hasattr(action_dict, "message_id") or "message_id" in action_dict
        assert hasattr(action_dict, "thread_id") or "thread_id" in action_dict

        # Verify NO camelCase keys
        assert_no_camel_case_keys(action_dict)


# ===========================================================================
# Telegram adapter tests
# ===========================================================================


class TestTelegramDispatchKeys:
    """Verify Telegram adapter passes snake_case keys to process_* methods."""

    def _make_adapter(self) -> Any:
        from chat_sdk.adapters.telegram.adapter import TelegramAdapter
        from chat_sdk.adapters.telegram.types import TelegramAdapterConfig

        adapter = TelegramAdapter(TelegramAdapterConfig(bot_token="123456:ABC-DEF"))
        adapter._chat = _make_mock_chat()
        adapter._bot_user_id = "123456"
        return adapter

    def test_telegram_action_dispatch_keys(self) -> None:
        """callback_query events should produce dicts with snake_case keys."""
        adapter = self._make_adapter()

        callback_query = {
            "id": "CQ123",
            "from_user": {
                "id": 99999,
                "is_bot": False,
                "first_name": "Test",
                "last_name": "User",
                "username": "testuser",
            },
            "message": {
                "message_id": 42,
                "chat": {"id": 12345, "type": "private"},
                "text": "Click a button",
                "date": 1700000000,
            },
            "data": "approve_action",
        }

        adapter.handle_callback_query(callback_query)

        adapter._chat.process_action.assert_called_once()
        action_dict = adapter._chat.process_action.call_args[0][0]

        # Verify required snake_case keys (dataclass or dict)
        assert hasattr(action_dict, "action_id") or "action_id" in action_dict
        assert hasattr(action_dict, "message_id") or "message_id" in action_dict
        assert hasattr(action_dict, "thread_id") or "thread_id" in action_dict

        # Verify NO camelCase keys
        assert_no_camel_case_keys(action_dict)

    def test_telegram_reaction_dispatch_keys(self) -> None:
        """message_reaction updates should produce dicts with snake_case keys."""
        adapter = self._make_adapter()

        reaction_update = {
            "chat": {"id": 12345, "type": "private"},
            "message_id": 42,
            "date": 1700000000,
            "user": {
                "id": 99999,
                "is_bot": False,
                "first_name": "Test",
                "username": "testuser",
            },
            "old_reaction": [],
            "new_reaction": [
                {"type": "emoji", "emoji": "\U0001f44d"},
            ],
        }

        adapter.handle_message_reaction_update(reaction_update)

        adapter._chat.process_reaction.assert_called_once()
        reaction_dict = adapter._chat.process_reaction.call_args[0][0]

        # Verify required snake_case keys (dataclass or dict)
        assert hasattr(reaction_dict, "thread_id") or "thread_id" in reaction_dict
        assert hasattr(reaction_dict, "message_id") or "message_id" in reaction_dict
        assert hasattr(reaction_dict, "raw_emoji") or "raw_emoji" in reaction_dict

        # Verify NO camelCase keys
        assert_no_camel_case_keys(reaction_dict)


# ===========================================================================
# Teams adapter tests
# ===========================================================================


class TestTeamsDispatchKeys:
    """Verify Teams adapter passes snake_case keys to process_* methods."""

    def _make_adapter(self) -> Any:
        from chat_sdk.adapters.teams.adapter import TeamsAdapter
        from chat_sdk.adapters.teams.types import TeamsAdapterConfig

        adapter = TeamsAdapter(
            TeamsAdapterConfig(
                app_id="APP_ID",
                app_password="APP_PASSWORD",
            )
        )
        adapter._chat = _make_mock_chat()
        return adapter

    def test_teams_action_dispatch_keys(self) -> None:
        """Action.Submit (message activity with actionId) should produce snake_case keys."""
        adapter = self._make_adapter()

        activity = {
            "type": "message",
            "conversation": {"id": "conv123"},
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
            "from": {"id": "USER123", "name": "Test User"},
            "id": "activity123",
            "replyToId": "reply123",
            "value": {
                "actionId": "approve_action",
                "value": "yes",
            },
        }

        action_value = activity["value"]
        adapter._handle_message_action(activity, action_value)

        adapter._chat.process_action.assert_called_once()
        action_dict = adapter._chat.process_action.call_args[0][0]

        # Verify required snake_case fields (dataclass or dict)
        assert hasattr(action_dict, "action_id") or "action_id" in action_dict

        # Verify NO camelCase keys in the dispatched event
        assert_no_camel_case_keys(action_dict)


# ===========================================================================
# Linear adapter tests
# ===========================================================================


class TestLinearDispatchKeys:
    """Verify Linear adapter passes snake_case keys to process_* methods."""

    def _make_adapter(self) -> Any:
        from chat_sdk.adapters.linear.adapter import LinearAdapter
        from chat_sdk.adapters.linear.types import LinearAdapterAPIKeyConfig

        adapter = LinearAdapter(
            LinearAdapterAPIKeyConfig(
                api_key="lin_api_test_key",
                webhook_secret="test_webhook_secret",
            )
        )
        adapter._chat = _make_mock_chat()
        adapter._bot_user_id = "bot-user-id"
        return adapter

    def test_linear_reaction_dispatch_keys(self) -> None:
        """Reaction webhook events should log with snake_case keys.

        The current Linear adapter's ``_handle_reaction`` only logs (it
        does not call ``process_reaction``).  This test verifies that the
        internal handling does not introduce camelCase keys in any dict
        that is passed to the chat instance.

        If the adapter is later updated to dispatch reaction events via
        ``process_reaction``, this test should be updated to validate
        the event dict keys.
        """
        adapter = self._make_adapter()

        payload = {
            "type": "Reaction",
            "action": "create",
            "data": {
                "id": "reaction-123",
                "emoji": "\U0001f44d",
                "commentId": "comment-456",
                "userId": "user-789",
            },
            "actor": {
                "id": "user-789",
                "name": "Test User",
                "type": "user",
            },
        }

        # _handle_reaction currently only logs, but if it dispatches a
        # process_reaction call, this test would catch camelCase keys.
        adapter._handle_reaction(payload)

        # If process_reaction was called, validate the keys
        if adapter._chat.process_reaction.called:
            reaction_dict = adapter._chat.process_reaction.call_args[0][0]
            assert_no_camel_case_keys(reaction_dict)
        # When process_reaction is not called, _handle_reaction still completed without raising

    def test_linear_comment_dispatch_keys(self) -> None:
        """Comment webhook events should call process_message correctly."""
        adapter = self._make_adapter()

        payload = {
            "type": "Comment",
            "action": "create",
            "data": {
                "id": "comment-123",
                "body": "Hello from Linear!",
                "issueId": "issue-456",
                "userId": "user-789",
                "createdAt": "2024-01-15T10:00:00.000Z",
                "updatedAt": "2024-01-15T10:00:00.000Z",
            },
            "actor": {
                "id": "user-789",
                "name": "Test User",
                "type": "user",
            },
        }

        adapter._handle_comment_created(payload)

        adapter._chat.process_message.assert_called_once()
        # process_message is called with (adapter, thread_id, message, options)
        call_args = adapter._chat.process_message.call_args[0]
        thread_id = call_args[1]
        assert isinstance(thread_id, str)
        assert thread_id.startswith("linear:")


# ===========================================================================
# Cross-adapter parametric test for top-level key validation
# ===========================================================================


class TestCamelCaseDetectionHelper:
    """Verify the helper function itself works correctly."""

    def test_catches_camelCase(self) -> None:
        with pytest.raises(AssertionError, match="camelCase key 'threadId'"):
            assert_no_camel_case_keys({"threadId": "abc"})

    def test_catches_nested_camelCase(self) -> None:
        with pytest.raises(AssertionError, match="camelCase key 'messageId'"):
            assert_no_camel_case_keys({"user": {"messageId": "abc"}})

    def test_catches_camelCase_in_list(self) -> None:
        with pytest.raises(AssertionError, match="camelCase key 'actionId'"):
            assert_no_camel_case_keys([{"actionId": "abc"}])

    def test_allows_snake_case(self) -> None:
        # Should complete without raising for snake_case keys
        assert_no_camel_case_keys(
            {
                "thread_id": "abc",
                "message_id": "123",
                "user": {
                    "user_id": "U123",
                    "user_name": "test",
                },
            }
        )  # no exception = pass

    def test_allows_single_word(self) -> None:
        """Single-word keys like 'adapter', 'value', 'raw' are fine."""
        assert_no_camel_case_keys(
            {
                "adapter": "slack",
                "value": "yes",
                "raw": {},
                "user": {},
                "emoji": "thumbsup",
            }
        )  # no exception = pass

    def test_allows_non_string_keys(self) -> None:
        """Non-string keys should be ignored."""
        assert_no_camel_case_keys({1: "number key", 2: "bool key"})  # no exception = pass
