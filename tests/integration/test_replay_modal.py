"""Replay integration test: Slack modal submission webhook.

Constructs a realistic Slack ``view_submission`` interaction payload,
creates a Chat instance with a MockAdapter, dispatches the modal submit
event, and verifies the on_modal_submit handler is invoked with correct data.
"""

from __future__ import annotations

from typing import Any

import pytest
from chat_sdk.types import Author, ModalSubmitEvent

from .conftest import create_chat

# ---------------------------------------------------------------------------
# Realistic Slack view_submission interaction payload (sanitised)
# ---------------------------------------------------------------------------

SLACK_VIEW_SUBMISSION_PAYLOAD: dict[str, Any] = {
    "type": "view_submission",
    "team": {"id": "T00FAKETEAM", "domain": "fake-team"},
    "user": {
        "id": "U00FAKEUSER2",
        "username": "jane.smith",
        "name": "jane.smith",
        "team_id": "T00FAKETEAM",
    },
    "view": {
        "id": "V0AB2P1M2HX",
        "team_id": "T00FAKETEAM",
        "type": "modal",
        "title": {"type": "plain_text", "text": "Feedback Form"},
        "callback_id": "feedback_form",
        "private_metadata": '{"thread_id": "slack:C00FAKECHAN2:1769220155.940449"}',
        "state": {
            "values": {
                "message_block": {
                    "message_input": {
                        "type": "plain_text_input",
                        "value": "Hello!",
                    }
                },
                "category_block": {
                    "category_select": {
                        "type": "static_select",
                        "selected_option": {
                            "text": {"type": "plain_text", "text": "Feature Request"},
                            "value": "feature",
                        },
                    }
                },
                "email_block": {
                    "email_input": {
                        "type": "email_text_input",
                        "value": "user@example.com",
                    }
                },
            }
        },
    },
    "trigger_id": "10367455086084.10229338706656.modal_submit_trigger",
}

# ---------------------------------------------------------------------------
# Realistic Slack block_actions interaction payload for button click
# ---------------------------------------------------------------------------

SLACK_BLOCK_ACTIONS_PAYLOAD: dict[str, Any] = {
    "type": "block_actions",
    "team": {"id": "T00FAKETEAM", "domain": "fake-team"},
    "user": {
        "id": "U00FAKEUSER2",
        "username": "jane.smith",
        "name": "jane.smith",
        "team_id": "T00FAKETEAM",
    },
    "trigger_id": "10367455086084.10229338706656.e675a0c0dacc24a1f7b84a7a426d1197",
    "channel": {"id": "C00FAKECHAN2", "name": "general"},
    "message": {
        "ts": "1769220161.503009",
        "thread_ts": "1769220155.940449",
    },
    "actions": [
        {
            "action_id": "feedback",
            "block_id": "actions_block",
            "type": "button",
            "text": {"type": "plain_text", "text": "Give Feedback"},
            "value": "feedback_value",
        }
    ],
}


def _make_modal_submit_event(
    adapter: Any,
    callback_id: str = "feedback_form",
    view_id: str = "V0AB2P1M2HX",
    values: dict[str, str] | None = None,
    private_metadata: str | None = None,
    user_id: str = "U00FAKEUSER2",
    user_name: str = "jane.smith",
) -> ModalSubmitEvent:
    """Build a ModalSubmitEvent from replayed payload data."""
    return ModalSubmitEvent(
        adapter=adapter,
        user=Author(
            user_id=user_id,
            user_name=user_name,
            full_name=user_name.replace(".", " ").title(),
            is_bot=False,
            is_me=False,
        ),
        view_id=view_id,
        callback_id=callback_id,
        values=values or {"message": "Hello!", "category": "feature", "email": "user@example.com"},
        private_metadata=private_metadata or '{"thread_id": "slack:C00FAKECHAN2:1769220155.940449"}',
        raw=SLACK_VIEW_SUBMISSION_PAYLOAD,
    )


class TestReplayModalSubmission:
    """Replay a Slack view_submission webhook."""

    @pytest.mark.asyncio
    async def test_modal_submit_triggers_handler(self):
        """A view_submission fires the on_modal_submit handler."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[ModalSubmitEvent] = []

        @chat.on_modal_submit
        async def handler(event):
            captured.append(event)

        event = _make_modal_submit_event(adapter)
        await chat.process_modal_submit(event)

        assert len(captured) == 1
        assert captured[0].callback_id == "feedback_form"
        assert captured[0].view_id == "V0AB2P1M2HX"

    @pytest.mark.asyncio
    async def test_modal_submit_has_correct_values(self):
        """The modal submit event carries the correct form values."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[ModalSubmitEvent] = []

        @chat.on_modal_submit
        async def handler(event):
            captured.append(event)

        event = _make_modal_submit_event(
            adapter,
            values={
                "message": "Hello!",
                "category": "feature",
                "email": "user@example.com",
            },
        )
        await chat.process_modal_submit(event)

        assert len(captured) == 1
        assert captured[0].values["message"] == "Hello!"
        assert captured[0].values["category"] == "feature"
        assert captured[0].values["email"] == "user@example.com"

    @pytest.mark.asyncio
    async def test_modal_submit_has_correct_user(self):
        """The modal submit event carries the correct user information."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[ModalSubmitEvent] = []

        @chat.on_modal_submit
        async def handler(event):
            captured.append(event)

        event = _make_modal_submit_event(adapter)
        await chat.process_modal_submit(event)

        assert len(captured) == 1
        user = captured[0].user
        assert user.user_id == "U00FAKEUSER2"
        assert user.user_name == "jane.smith"
        assert user.is_bot is False
        assert user.is_me is False

    @pytest.mark.asyncio
    async def test_modal_submit_has_private_metadata(self):
        """The modal submit event carries private_metadata."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[ModalSubmitEvent] = []

        @chat.on_modal_submit
        async def handler(event):
            captured.append(event)

        event = _make_modal_submit_event(adapter)
        await chat.process_modal_submit(event)

        assert len(captured) == 1
        assert captured[0].private_metadata is not None
        assert "thread_id" in captured[0].private_metadata


class TestReplayModalFiltered:
    """Replay modal submissions with callback_id filtering."""

    @pytest.mark.asyncio
    async def test_filtered_modal_handler_matches_callback_id(self):
        """Handler registered with callback_ids only fires for matching modals."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        feedback_calls: list[ModalSubmitEvent] = []
        all_calls: list[ModalSubmitEvent] = []

        @chat.on_modal_submit
        async def all_handler(event):
            all_calls.append(event)

        @chat.on_modal_submit("feedback_form")
        async def feedback_handler(event):
            feedback_calls.append(event)

        # Matching callback_id
        event1 = _make_modal_submit_event(adapter, callback_id="feedback_form")
        await chat.process_modal_submit(event1)

        # Non-matching callback_id
        event2 = _make_modal_submit_event(adapter, callback_id="other_form", view_id="V_OTHER")
        await chat.process_modal_submit(event2)

        assert len(all_calls) == 2
        assert len(feedback_calls) == 1
        assert feedback_calls[0].callback_id == "feedback_form"

    @pytest.mark.asyncio
    async def test_decorator_style_modal_handler(self):
        """The decorator style @chat.on_modal_submit('id') works."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        calls: list[ModalSubmitEvent] = []

        @chat.on_modal_submit("feedback_form")
        async def handler(event: ModalSubmitEvent):
            calls.append(event)

        event = _make_modal_submit_event(adapter)
        await chat.process_modal_submit(event)

        assert len(calls) == 1
        assert calls[0].callback_id == "feedback_form"


class TestReplayModalRawPayload:
    """Verify the raw payload data is preserved."""

    @pytest.mark.asyncio
    async def test_raw_payload_preserved(self):
        """The original raw payload is accessible on the event."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[ModalSubmitEvent] = []

        @chat.on_modal_submit
        async def handler(event):
            captured.append(event)

        event = _make_modal_submit_event(adapter)
        await chat.process_modal_submit(event)

        assert len(captured) == 1
        assert captured[0].raw is not None
        assert captured[0].raw["type"] == "view_submission"
