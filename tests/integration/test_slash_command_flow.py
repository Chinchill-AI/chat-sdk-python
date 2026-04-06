"""Integration tests for slash command handling.

Verifies that slash commands are dispatched to the correct handlers,
command text is parsed, and filtered handlers only fire for matching commands.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from chat_sdk.types import Author, SlashCommandEvent

from .conftest import create_chat


class TestSlashCommandFlow:
    """End-to-end tests for slash command handling."""

    def _make_slash_command(
        self,
        adapter: Any,
        command: str = "/help",
        text: str = "please show me help",
        trigger_id: str | None = "T12345",
        is_me: bool = False,
    ) -> SlashCommandEvent:
        return SlashCommandEvent(
            adapter=adapter,
            channel=None,
            user=Author(
                user_id="BOT" if is_me else "U789",
                user_name="testbot" if is_me else "bob",
                full_name="Test Bot" if is_me else "Bob",
                is_bot=is_me,
                is_me=is_me,
            ),
            command=command,
            text=text,
            trigger_id=trigger_id,
            raw={"raw": "slash-data"},
        )

    @pytest.mark.asyncio
    async def test_slash_command_triggers_catch_all_handler(self):
        """A slash command fires a catch-all on_slash_command handler."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        calls: list[SlashCommandEvent] = []

        @chat.on_slash_command
        async def handler(event: SlashCommandEvent):
            calls.append(event)

        event = self._make_slash_command(adapter)
        chat.process_slash_command(event)
        await asyncio.sleep(0.05)

        assert len(calls) == 1
        assert calls[0].command == "/help"
        assert calls[0].text == "please show me help"

    @pytest.mark.asyncio
    async def test_command_text_parsed_correctly(self):
        """The command text is available on the event."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        calls: list[SlashCommandEvent] = []

        @chat.on_slash_command
        async def handler(event: SlashCommandEvent):
            calls.append(event)

        event = self._make_slash_command(adapter, "/status", "check all services")
        chat.process_slash_command(event)
        await asyncio.sleep(0.05)

        assert len(calls) == 1
        assert calls[0].command == "/status"
        assert calls[0].text == "check all services"

    @pytest.mark.asyncio
    async def test_filtered_handler_matches_specific_command(self):
        """Handler registered for /help only fires for /help, not /status."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        help_calls: list[SlashCommandEvent] = []

        @chat.on_slash_command("/help")
        async def handler(event: SlashCommandEvent):
            help_calls.append(event)

        help_event = self._make_slash_command(adapter, "/help", "show help")
        status_event = self._make_slash_command(adapter, "/status", "check status")

        chat.process_slash_command(help_event)
        chat.process_slash_command(status_event)
        await asyncio.sleep(0.05)

        assert len(help_calls) == 1
        assert help_calls[0].command == "/help"

    @pytest.mark.asyncio
    async def test_filtered_handler_for_multiple_commands(self):
        """Handler registered for ['/status', '/health'] fires for both."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        calls: list[SlashCommandEvent] = []

        @chat.on_slash_command(["/status", "/health"])
        async def handler(event: SlashCommandEvent):
            calls.append(event)

        status_event = self._make_slash_command(adapter, "/status", "")
        health_event = self._make_slash_command(adapter, "/health", "")
        help_event = self._make_slash_command(adapter, "/help", "")

        chat.process_slash_command(status_event)
        chat.process_slash_command(health_event)
        chat.process_slash_command(help_event)
        await asyncio.sleep(0.05)

        assert len(calls) == 2
        commands = {c.command for c in calls}
        assert "/status" in commands
        assert "/health" in commands

    @pytest.mark.asyncio
    async def test_slash_command_from_self_ignored(self):
        """Slash commands from the bot itself are not dispatched."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        calls: list[SlashCommandEvent] = []

        @chat.on_slash_command
        async def handler(event: SlashCommandEvent):
            calls.append(event)

        event = self._make_slash_command(adapter, is_me=True)
        chat.process_slash_command(event)
        await asyncio.sleep(0.05)

        assert len(calls) == 0

    @pytest.mark.asyncio
    async def test_slash_command_user_info(self):
        """The event carries correct user information."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        calls: list[SlashCommandEvent] = []

        @chat.on_slash_command
        async def handler(event: SlashCommandEvent):
            calls.append(event)

        event = self._make_slash_command(adapter)
        chat.process_slash_command(event)
        await asyncio.sleep(0.05)

        assert len(calls) == 1
        assert calls[0].user.user_id == "U789"
        assert calls[0].user.user_name == "bob"
        assert calls[0].user.is_bot is False

    @pytest.mark.asyncio
    async def test_slash_command_trigger_id(self):
        """The event carries the trigger_id for modal opening."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        calls: list[SlashCommandEvent] = []

        @chat.on_slash_command
        async def handler(event: SlashCommandEvent):
            calls.append(event)

        event = self._make_slash_command(adapter, trigger_id="TRIGGER-99")
        chat.process_slash_command(event)
        await asyncio.sleep(0.05)

        assert len(calls) == 1
        assert calls[0].trigger_id == "TRIGGER-99"

    @pytest.mark.asyncio
    async def test_catch_all_and_filtered_both_fire(self):
        """Both a catch-all and a filtered handler fire for a matching command."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        all_calls: list[SlashCommandEvent] = []
        help_calls: list[SlashCommandEvent] = []

        @chat.on_slash_command
        async def catch_all(event: SlashCommandEvent):
            all_calls.append(event)

        @chat.on_slash_command("/help")
        async def help_handler(event: SlashCommandEvent):
            help_calls.append(event)

        event = self._make_slash_command(adapter, "/help")
        chat.process_slash_command(event)
        await asyncio.sleep(0.05)

        assert len(all_calls) == 1
        assert len(help_calls) == 1

    @pytest.mark.asyncio
    async def test_command_without_leading_slash_normalized(self):
        """Commands registered without leading / are normalized."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        calls: list[SlashCommandEvent] = []

        # Register without leading slash - should be normalized to /help
        chat.on_slash_command("help", lambda event: calls.append(event))

        event = self._make_slash_command(adapter, "/help")
        chat.process_slash_command(event)
        await asyncio.sleep(0.05)

        assert len(calls) == 1
