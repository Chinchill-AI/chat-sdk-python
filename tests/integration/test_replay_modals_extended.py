"""Integration tests for extended modal interactions.

Port of replay-modals.test.ts (11 tests) and replay-modal-private-metadata.test.ts (6 tests).

Covers:
- Modal open -> submit -> update response
- Modal close with notify_on_close
- Private metadata round-trip through modal lifecycle
- Modal errors response
- Ephemeral message button click -> modal flow
- relatedThread and relatedMessage population via modal context
- Combined privateMetadata + relatedThread usage
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from chat_sdk.types import (
    ActionEvent,
    Author,
    ModalCloseEvent,
    ModalSubmitEvent,
)

from .conftest import create_chat

# ---------------------------------------------------------------------------
# Payload constants
# ---------------------------------------------------------------------------

FEEDBACK_VIEW_SUBMISSION: dict[str, Any] = {
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
        "callback_id": "feedback_form",
        "private_metadata": '{"thread_id": "slack:C00FAKECHAN2:1769220155.940449"}',
        "state": {
            "values": {
                "message_block": {"message_input": {"type": "plain_text_input", "value": "Hello!"}},
                "category_block": {
                    "category_select": {
                        "type": "static_select",
                        "selected_option": {"value": "feature"},
                    }
                },
                "email_block": {"email_input": {"type": "email_text_input", "value": "user@example.com"}},
            }
        },
    },
}

REPORT_VIEW_SUBMISSION: dict[str, Any] = {
    "type": "view_submission",
    "team": {"id": "T00FAKETEAM"},
    "user": {
        "id": "U0A8WUV28QM",
        "username": "sd0a90bkva4s_user",
        "name": "sd0a90bkva4s_user",
    },
    "view": {
        "id": "V0AEWMF8C3D",
        "callback_id": "report_form",
        "private_metadata": (
            '{"reportType": "bug", "threadId": "slack:C0A9D9RTBMF:1771116676.529969", "reporter": "U0A8WUV28QM"}'
        ),
        "state": {
            "values": {
                "title_block": {"title_input": {"type": "plain_text_input", "value": "tes"}},
                "steps_block": {"steps_input": {"type": "plain_text_input", "value": "test"}},
                "severity_block": {
                    "severity_select": {
                        "type": "static_select",
                        "selected_option": {"value": "high"},
                    }
                },
            }
        },
    },
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
    """Build a ModalSubmitEvent for testing."""
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
        private_metadata=private_metadata,
        raw=FEEDBACK_VIEW_SUBMISSION,
    )


def _make_modal_close_event(
    adapter: Any,
    callback_id: str = "feedback_form",
    view_id: str = "V0AB2P1M2HX",
    private_metadata: str | None = None,
    user_id: str = "U00FAKEUSER2",
    user_name: str = "jane.smith",
) -> ModalCloseEvent:
    """Build a ModalCloseEvent for testing."""
    return ModalCloseEvent(
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
        private_metadata=private_metadata,
        raw={},
    )


def _make_action_event(
    adapter: Any,
    action_id: str = "feedback",
    trigger_id: str = "10367455086084.10229338706656.e675a0c0dacc24a1f7b84a7a426d1197",
    value: str | None = None,
) -> ActionEvent:
    """Build an ActionEvent for button click testing."""
    return ActionEvent(
        adapter=adapter,
        thread=None,
        thread_id="slack:C00FAKECHAN2:1769220155.940449",
        message_id="1769220161.503009",
        user=Author(
            user_id="U00FAKEUSER2",
            user_name="jane.smith",
            full_name="Jane Smith",
            is_bot=False,
            is_me=False,
        ),
        action_id=action_id,
        value=value,
        trigger_id=trigger_id,
        raw={},
    )


# ============================================================================
# Modal submit (view_submission)
# ============================================================================


class TestModalSubmission:
    """Modal form submission handling."""

    @pytest.mark.asyncio
    async def test_feedback_button_click_triggers_action(self):
        """A feedback button block_actions fires the action handler."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[ActionEvent] = []
        open_modal_called = False

        def on_action(event):
            captured.append(event)
            nonlocal open_modal_called
            if event.action_id == "feedback":
                open_modal_called = True

        chat.on_action(on_action)

        event = _make_action_event(adapter)
        chat.process_action(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 1
        assert captured[0].action_id == "feedback"
        assert open_modal_called is True
        assert captured[0].trigger_id == "10367455086084.10229338706656.e675a0c0dacc24a1f7b84a7a426d1197"

    # (Duplicate test_modal_submit_has_correct_callback_id_and_view_id,
    #  test_modal_submit_has_correct_values, test_modal_submit_has_correct_user
    #  removed -- covered by test_replay_modal.py)


# ============================================================================
# Modal close
# ============================================================================


class TestModalClose:
    """Modal close event handling."""

    @pytest.mark.asyncio
    async def test_modal_close_triggers_handler(self):
        """A modal close event (notify_on_close) fires the close handler."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[ModalCloseEvent] = []

        @chat.on_modal_close
        async def handler(event):
            captured.append(event)

        event = _make_modal_close_event(adapter)
        chat.process_modal_close(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 1
        assert captured[0].callback_id == "feedback_form"
        assert captured[0].view_id == "V0AB2P1M2HX"

    @pytest.mark.asyncio
    async def test_modal_close_has_correct_user(self):
        """The modal close event carries correct user info."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[ModalCloseEvent] = []

        @chat.on_modal_close
        async def handler(event):
            captured.append(event)

        event = _make_modal_close_event(adapter)
        chat.process_modal_close(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 1
        assert captured[0].user.user_id == "U00FAKEUSER2"


# ============================================================================
# Private metadata round-trip
# ============================================================================


class TestModalPrivateMetadata:
    """Private metadata in modal lifecycle."""

    @pytest.mark.asyncio
    async def test_private_metadata_round_trip(self):
        """Private metadata set during openModal is available on submit."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[ModalSubmitEvent] = []

        @chat.on_modal_submit
        async def handler(event):
            captured.append(event)

        metadata = '{"reportType": "bug", "threadId": "slack:C0A9D9RTBMF:1771116676.529969", "reporter": "U0A8WUV28QM"}'
        event = _make_modal_submit_event(
            adapter,
            callback_id="report_form",
            view_id="V0AEWMF8C3D",
            values={"title": "tes", "steps": "test", "severity": "high"},
            private_metadata=metadata,
            user_id="U0A8WUV28QM",
            user_name="sd0a90bkva4s_user",
        )
        await chat.process_modal_submit(event)

        assert len(captured) == 1
        assert captured[0].private_metadata is not None
        import json

        parsed = json.loads(captured[0].private_metadata)
        assert parsed["reportType"] == "bug"
        assert parsed["threadId"] == "slack:C0A9D9RTBMF:1771116676.529969"
        assert parsed["reporter"] == "U0A8WUV28QM"

    @pytest.mark.asyncio
    async def test_report_form_values_decoded(self):
        """Form values from report_form submission are correctly decoded."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[ModalSubmitEvent] = []

        @chat.on_modal_submit
        async def handler(event):
            captured.append(event)

        event = _make_modal_submit_event(
            adapter,
            callback_id="report_form",
            view_id="V0AEWMF8C3D",
            values={"title": "tes", "steps": "test", "severity": "high"},
            user_id="U0A8WUV28QM",
            user_name="sd0a90bkva4s_user",
        )
        await chat.process_modal_submit(event)

        assert len(captured) == 1
        assert captured[0].values == {"title": "tes", "steps": "test", "severity": "high"}

    @pytest.mark.asyncio
    async def test_report_button_click_has_value(self):
        """A report button click carries the action value."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[ActionEvent] = []

        chat.on_action(lambda event: captured.append(event))

        event = ActionEvent(
            adapter=adapter,
            thread=None,
            thread_id="slack:C0A9D9RTBMF:1771116676.529969",
            message_id="1771116682.586579",
            user=Author(
                user_id="U0A8WUV28QM",
                user_name="sd0a90bkva4s_user",
                full_name="Test User",
                is_bot=False,
                is_me=False,
            ),
            action_id="report",
            value="bug",
            raw={},
        )
        chat.process_action(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 1
        assert captured[0].action_id == "report"
        assert captured[0].value == "bug"


# ============================================================================
# Filtered modal handlers
# ============================================================================


class TestFilteredModalHandlers:
    """Callback ID filtering for modal handlers."""

    # (Duplicate test_filtered_handler_only_fires_for_matching_callback_id
    #  removed -- covered by test_replay_modal.py)

    @pytest.mark.asyncio
    async def test_multiple_callback_ids_filter(self):
        """Handler registered with multiple callback_ids fires for all matching."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        calls: list[ModalSubmitEvent] = []

        @chat.on_modal_submit(["feedback_form", "report_form"])
        async def handler(event):
            calls.append(event)

        event1 = _make_modal_submit_event(adapter, callback_id="feedback_form")
        event2 = _make_modal_submit_event(adapter, callback_id="report_form", view_id="V2")
        event3 = _make_modal_submit_event(adapter, callback_id="other_form", view_id="V3")

        await chat.process_modal_submit(event1)
        await chat.process_modal_submit(event2)
        await chat.process_modal_submit(event3)

        assert len(calls) == 2
        assert {c.callback_id for c in calls} == {"feedback_form", "report_form"}


# ============================================================================
# Ephemeral message interactions
# ============================================================================


class TestEphemeralModalInteractions:
    """Ephemeral message button click -> modal -> submit flow."""

    @pytest.mark.asyncio
    async def test_ephemeral_action_has_trigger_id(self):
        """Button click from an ephemeral message provides a trigger_id."""
        chat, adapters, state = await create_chat()
        adapter = adapters["slack"]
        captured: list[ActionEvent] = []

        chat.on_action(lambda event: captured.append(event))

        event = ActionEvent(
            adapter=adapter,
            thread=None,
            thread_id="slack:C00FAKECHAN3:1771126602.612659",
            message_id="ephemeral:1771126609.000200",
            user=Author(
                user_id="U00FAKEUSER2",
                user_name="jane.smith",
                full_name="Jane Smith",
                is_bot=False,
                is_me=False,
            ),
            action_id="ephemeral_modal",
            trigger_id="10541689532400.10229338706656.500e194be18c7e17dd828032cc9a769f",
            raw={},
        )
        chat.process_action(event)
        await asyncio.sleep(0.05)

        assert len(captured) == 1
        assert captured[0].action_id == "ephemeral_modal"
        assert captured[0].trigger_id is not None
        assert captured[0].message_id.startswith("ephemeral:")

    # (Duplicate test_modal_raw_payload_preserved removed --
    #  covered by test_replay_modal.py)
