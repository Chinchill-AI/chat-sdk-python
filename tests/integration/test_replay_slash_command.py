"""Replay integration test: Slack slash command webhook.

Constructs realistic Slack slash command payloads, creates a Chat instance
with a MockAdapter, dispatches the event, and verifies the on_slash_command
handler is invoked with correct data.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from chat_sdk.types import Author, SlashCommandEvent

from .conftest import create_chat

# ---------------------------------------------------------------------------
# Realistic Slack slash command payload (URL-encoded form data, shown as dict)
# ---------------------------------------------------------------------------

SLACK_SLASH_HELP_PAYLOAD: dict[str, Any] = {
    "token": "XXYYZZ",
    "team_id": "T00FAKETEAM",
    "team_domain": "fake-team",
    "channel_id": "C00FAKECHAN1",
    "channel_name": "general",
    "user_id": "U00FAKEUSER1",
    "user_name": "test.user",
    "command": "/help",
    "text": "show commands",
    "api_app_id": "A00FAKEAPP1",
    "is_enterprise_install": "false",
    "response_url": "https://hooks.slack.com/commands/T00FAKETEAM/12345/FAKEHOOK",
    "trigger_id": "10367455086084.10229338706656.slash_cmd_trigger",
}

SLACK_SLASH_STATUS_PAYLOAD: dict[str, Any] = {
    "token": "XXYYZZ",
    "team_id": "T00FAKETEAM",
    "team_domain": "fake-team",
    "channel_id": "C00FAKECHAN2",
    "channel_name": "engineering",
    "user_id": "U00FAKEUSER2",
    "user_name": "jane.smith",
    "command": "/status",
    "text": "check all services",
    "api_app_id": "A00FAKEAPP1",
    "is_enterprise_install": "false",
    "response_url": "https://hooks.slack.com/commands/T00FAKETEAM/67890/FAKEHOOK2",
    "trigger_id": "10367455086084.10229338706656.slash_status_trigger",
}

SLACK_SLASH_DEPLOY_PAYLOAD: dict[str, Any] = {
    "token": "XXYYZZ",
    "team_id": "T00FAKETEAM",
    "team_domain": "fake-team",
    "channel_id": "C00FAKECHAN1",
    "channel_name": "general",
    "user_id": "U00FAKEUSER1",
    "user_name": "test.user",
    "command": "/deploy",
    "text": "production v2.1.0",
    "api_app_id": "A00FAKEAPP1",
    "is_enterprise_install": "false",
    "response_url": "https://hooks.slack.com/commands/T00FAKETEAM/11111/FAKEHOOK3",
    "trigger_id": "10367455086084.10229338706656.slash_deploy_trigger",
}


def _make_slash_event(
    adapter: Any,
    payload: dict[str, Any] | None = None,
    command: str | None = None,
    text: str | None = None,
    trigger_id: str | None = None,
    user_id: str | None = None,
    user_name: str | None = None,
) -> SlashCommandEvent:
    """Build a SlashCommandEvent from replayed payload data."""
    p = payload or SLACK_SLASH_HELP_PAYLOAD
    return SlashCommandEvent(
        adapter=adapter,
        channel=None,
        user=Author(
            user_id=user_id or p["user_id"],
            user_name=user_name or p["user_name"],
            full_name=(user_name or p["user_name"]).replace(".", " ").title(),
            is_bot=False,
            is_me=False,
        ),
        command=command or p["command"],
        text=text or p["text"],
        trigger_id=trigger_id or p.get("trigger_id"),
        raw=p,
    )


class TestReplaySlashCommand:
    """Replay Slack slash command webhooks."""

    @pytest.mark.asyncio
    async def test_slash_command_triggers_handler(self):
        """A /help slash command fires the on_slash_command handler."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[SlashCommandEvent] = []

        @chat.on_slash_command
        async def handler(event: SlashCommandEvent):
            captured.append(event)

        event = _make_slash_event(adapter, SLACK_SLASH_HELP_PAYLOAD)
        chat.process_slash_command(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 1
        assert captured[0].command == "/help"
        assert captured[0].text == "show commands"

    @pytest.mark.asyncio
    async def test_slash_command_has_correct_user(self):
        """The slash command event carries correct user information."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[SlashCommandEvent] = []

        @chat.on_slash_command
        async def handler(event: SlashCommandEvent):
            captured.append(event)

        event = _make_slash_event(adapter, SLACK_SLASH_HELP_PAYLOAD)
        chat.process_slash_command(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 1
        user = captured[0].user
        assert user.user_id == "U00FAKEUSER1"
        assert user.user_name == "test.user"
        assert user.is_bot is False
        assert user.is_me is False

    @pytest.mark.asyncio
    async def test_slash_command_has_trigger_id(self):
        """The slash command event carries the trigger_id for opening modals."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[SlashCommandEvent] = []

        @chat.on_slash_command
        async def handler(event: SlashCommandEvent):
            captured.append(event)

        event = _make_slash_event(adapter, SLACK_SLASH_HELP_PAYLOAD)
        chat.process_slash_command(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 1
        assert captured[0].trigger_id is not None
        assert "slash_cmd_trigger" in captured[0].trigger_id

    @pytest.mark.asyncio
    async def test_status_command_text_parsed(self):
        """The /status command text is correctly parsed."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[SlashCommandEvent] = []

        @chat.on_slash_command
        async def handler(event: SlashCommandEvent):
            captured.append(event)

        event = _make_slash_event(adapter, SLACK_SLASH_STATUS_PAYLOAD)
        chat.process_slash_command(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 1
        assert captured[0].command == "/status"
        assert captured[0].text == "check all services"

    @pytest.mark.asyncio
    async def test_deploy_command_with_version(self):
        """The /deploy command text includes the version argument."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[SlashCommandEvent] = []

        @chat.on_slash_command
        async def handler(event: SlashCommandEvent):
            captured.append(event)

        event = _make_slash_event(adapter, SLACK_SLASH_DEPLOY_PAYLOAD)
        chat.process_slash_command(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 1
        assert captured[0].command == "/deploy"
        assert "production" in captured[0].text
        assert "v2.1.0" in captured[0].text


class TestReplaySlashCommandFiltered:
    """Replay slash commands with command filtering."""

    @pytest.mark.asyncio
    async def test_filtered_handler_matches_specific_command(self):
        """Handler registered with specific commands only fires for matching ones."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        help_calls: list[SlashCommandEvent] = []
        all_calls: list[SlashCommandEvent] = []

        @chat.on_slash_command
        async def catch_all(event: SlashCommandEvent):
            all_calls.append(event)

        @chat.on_slash_command("/help")
        async def help_handler(event: SlashCommandEvent):
            help_calls.append(event)

        # /help command
        event1 = _make_slash_event(adapter, SLACK_SLASH_HELP_PAYLOAD)
        chat.process_slash_command(event1)

        # /status command
        event2 = _make_slash_event(adapter, SLACK_SLASH_STATUS_PAYLOAD)
        chat.process_slash_command(event2)
        await asyncio.sleep(0.05)

        assert len(all_calls) == 2
        assert len(help_calls) == 1
        assert help_calls[0].command == "/help"

    @pytest.mark.asyncio
    async def test_multiple_commands_filter(self):
        """Handler registered with multiple commands fires for all of them."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        calls: list[SlashCommandEvent] = []

        @chat.on_slash_command(["/help", "/status"])
        async def handler(event: SlashCommandEvent):
            calls.append(event)

        event1 = _make_slash_event(adapter, SLACK_SLASH_HELP_PAYLOAD)
        event2 = _make_slash_event(adapter, SLACK_SLASH_STATUS_PAYLOAD)
        event3 = _make_slash_event(adapter, SLACK_SLASH_DEPLOY_PAYLOAD)

        chat.process_slash_command(event1)
        chat.process_slash_command(event2)
        chat.process_slash_command(event3)
        await asyncio.sleep(0.05)

        assert len(calls) == 2
        assert {c.command for c in calls} == {"/help", "/status"}


class TestReplaySlashCommandRawPayload:
    """Verify the raw payload data is preserved."""

    @pytest.mark.asyncio
    async def test_raw_payload_preserved(self):
        """The original raw payload is accessible on the event."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[SlashCommandEvent] = []

        @chat.on_slash_command
        async def handler(event: SlashCommandEvent):
            captured.append(event)

        event = _make_slash_event(adapter, SLACK_SLASH_HELP_PAYLOAD)
        chat.process_slash_command(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 1
        assert captured[0].raw is not None
        assert captured[0].raw["command"] == "/help"
        assert captured[0].raw["channel_id"] == "C00FAKECHAN1"
        assert captured[0].raw["team_id"] == "T00FAKETEAM"
