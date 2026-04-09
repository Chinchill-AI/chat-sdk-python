"""Integration tests for reaction event handling.

Verifies that reaction_added and reaction_removed events are dispatched
to the correct handlers, that emoji filtering works, and that self-reactions
are ignored.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from chat_sdk.emoji import get_emoji
from chat_sdk.types import Author, ReactionEvent

from .conftest import create_chat


class TestReactionFlow:
    """End-to-end tests for reaction event handling."""

    def _make_reaction(
        self,
        adapter: Any,
        emoji_name: str = "thumbs_up",
        raw_emoji: str = "+1",
        added: bool = True,
        is_me: bool = False,
        thread_id: str = "slack:C123:1234.5678",
    ) -> ReactionEvent:
        return ReactionEvent(
            emoji=get_emoji(emoji_name),
            raw_emoji=raw_emoji,
            added=added,
            user=Author(
                user_id="BOT" if is_me else "U123",
                user_name="testbot" if is_me else "user",
                full_name="Test Bot" if is_me else "Test User",
                is_bot=is_me,
                is_me=is_me,
            ),
            message_id="msg-1",
            thread_id=thread_id,
            adapter=adapter,
            thread=None,
            raw={},
        )

    @pytest.mark.asyncio
    async def test_reaction_added_triggers_handler(self):
        """A reaction_added event fires the on_reaction handler."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        calls: list[ReactionEvent] = []

        chat.on_reaction(lambda event: calls.append(event))

        event = self._make_reaction(adapter)
        chat.process_reaction(event)
        await asyncio.sleep(0.05)

        assert len(calls) == 1
        assert calls[0].added is True
        assert calls[0].emoji == get_emoji("thumbs_up")

    @pytest.mark.asyncio
    async def test_reaction_removed_triggers_handler(self):
        """A reaction_removed event fires the handler with added=False."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        calls: list[ReactionEvent] = []

        chat.on_reaction(lambda event: calls.append(event))

        event = self._make_reaction(adapter, added=False)
        chat.process_reaction(event)
        await asyncio.sleep(0.05)

        assert len(calls) == 1
        assert calls[0].added is False

    @pytest.mark.asyncio
    async def test_filtered_reaction_handler_matches_specific_emoji(self):
        """A handler registered with an emoji filter only fires for matching reactions."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        calls: list[ReactionEvent] = []

        chat.on_reaction(["thumbs_up", "heart"], lambda event: calls.append(event))

        # Should match
        thumbs = self._make_reaction(adapter, "thumbs_up", "+1")
        chat.process_reaction(thumbs)
        await asyncio.sleep(0.02)

        # Should not match
        fire = self._make_reaction(adapter, "fire", "fire")
        chat.process_reaction(fire)
        await asyncio.sleep(0.02)

        assert len(calls) == 1
        assert calls[0].emoji == get_emoji("thumbs_up")

    @pytest.mark.asyncio
    async def test_filtered_by_raw_emoji_string(self):
        """Filtering by raw emoji string works."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        calls: list[ReactionEvent] = []

        chat.on_reaction(["+1"], lambda event: calls.append(event))

        event = self._make_reaction(adapter, "thumbs_up", "+1")
        chat.process_reaction(event)
        await asyncio.sleep(0.02)

        assert len(calls) == 1
        assert calls[0].raw_emoji == "+1"

    @pytest.mark.asyncio
    async def test_reaction_from_self_is_ignored(self):
        """Reactions from the bot (is_me=True) are not dispatched."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        calls: list[ReactionEvent] = []

        chat.on_reaction(lambda event: calls.append(event))

        event = self._make_reaction(adapter, is_me=True)
        chat.process_reaction(event)
        await asyncio.sleep(0.05)

        assert len(calls) == 0

    @pytest.mark.asyncio
    async def test_reaction_event_has_thread_with_post_capability(self):
        """The thread on a reaction event supports posting."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        calls: list[ReactionEvent] = []

        async def handler(event: ReactionEvent):
            calls.append(event)
            await event.thread.post("Thanks for the reaction!")

        chat.on_reaction(handler)

        event = self._make_reaction(adapter)
        chat.process_reaction(event)
        await asyncio.sleep(0.05)

        assert len(calls) == 1
        assert calls[0].thread is not None
        assert calls[0].thread.id == "slack:C123:1234.5678"
        assert len(adapter._post_calls) == 1
        assert adapter._post_calls[0][1] == "Thanks for the reaction!"

    @pytest.mark.asyncio
    async def test_multiple_reaction_handlers_all_fire(self):
        """All matching reaction handlers are called (catch-all + filtered)."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        all_calls: list[ReactionEvent] = []
        filtered_calls: list[ReactionEvent] = []

        async def all_handler(event: ReactionEvent):
            all_calls.append(event)

        async def filtered_handler(event: ReactionEvent):
            filtered_calls.append(event)

        chat.on_reaction(all_handler)
        chat.on_reaction(["thumbs_up"], filtered_handler)

        event = self._make_reaction(adapter, "thumbs_up", "+1")
        chat.process_reaction(event)
        await asyncio.sleep(0.05)

        assert len(all_calls) == 1
        assert len(filtered_calls) == 1

    @pytest.mark.asyncio
    async def test_emoji_value_object_filter(self):
        """Filtering using EmojiValue objects works by name comparison."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        calls: list[ReactionEvent] = []

        thumbs_up = get_emoji("thumbs_up")
        chat.on_reaction([thumbs_up], lambda event: calls.append(event))

        event = self._make_reaction(adapter, "thumbs_up", "+1")
        chat.process_reaction(event)
        await asyncio.sleep(0.05)

        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_decorator_style_reaction_handler(self):
        """The decorator style @chat.on_reaction() works."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        calls: list[ReactionEvent] = []

        @chat.on_reaction()
        async def handler(event: ReactionEvent):
            calls.append(event)

        event = self._make_reaction(adapter)
        chat.process_reaction(event)
        await asyncio.sleep(0.05)

        assert len(calls) == 1
