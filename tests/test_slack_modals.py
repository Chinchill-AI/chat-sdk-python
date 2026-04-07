"""Tests for Slack modal (view) conversion.

Port of packages/adapter-slack/src/modals.test.ts.
"""

from __future__ import annotations

import json

from chat_sdk.adapters.slack.modals import (
    ModalMetadata,
    decode_modal_metadata,
    encode_modal_metadata,
    modal_to_slack_view,
)
from chat_sdk.modals import (
    ModalElement,
    RadioSelectElement,
    SelectElement,
    SelectOptionElement,
    TextInputElement,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _modal(
    *,
    callback_id: str = "test",
    title: str = "Test",
    children: list | None = None,
    submit_label: str | None = None,
    close_label: str | None = None,
    notify_on_close: bool | None = None,
    private_metadata: str | None = None,
) -> ModalElement:
    el: ModalElement = {
        "type": "modal",
        "callback_id": callback_id,
        "title": title,
        "children": children or [],
    }
    if submit_label:
        el["submit_label"] = submit_label
    if close_label:
        el["close_label"] = close_label
    if notify_on_close is not None:
        el["notify_on_close"] = notify_on_close
    if private_metadata:
        el["private_metadata"] = private_metadata
    return el


def _text_input(
    *,
    id: str,
    label: str,
    placeholder: str | None = None,
    initial_value: str | None = None,
    multiline: bool = False,
    max_length: int | None = None,
    optional: bool = False,
) -> TextInputElement:
    el: TextInputElement = {"type": "text_input", "id": id, "label": label, "multiline": multiline}
    if optional:
        el["optional"] = True
    if placeholder:
        el["placeholder"] = placeholder
    if initial_value:
        el["initial_value"] = initial_value
    if max_length:
        el["max_length"] = max_length
    return el


def _select(
    *,
    id: str,
    label: str,
    options: list[SelectOptionElement],
    placeholder: str | None = None,
    initial_option: str | None = None,
    optional: bool = False,
) -> SelectElement:
    el: SelectElement = {"type": "select", "id": id, "label": label, "options": options}
    if placeholder:
        el["placeholder"] = placeholder
    if initial_option:
        el["initial_option"] = initial_option
    if optional:
        el["optional"] = True
    return el


def _radio_select(
    *,
    id: str,
    label: str,
    options: list[SelectOptionElement],
    initial_option: str | None = None,
    optional: bool = False,
) -> RadioSelectElement:
    el: RadioSelectElement = {"type": "radio_select", "id": id, "label": label, "options": options}
    if initial_option:
        el["initial_option"] = initial_option
    if optional:
        el["optional"] = True
    return el


def _option(*, label: str, value: str, description: str | None = None) -> SelectOptionElement:
    opt: SelectOptionElement = {"label": label, "value": value}
    if description:
        opt["description"] = description
    return opt


# ---------------------------------------------------------------------------
# modalToSlackView
# ---------------------------------------------------------------------------


class TestModalToSlackView:
    def test_simple_modal_with_text_input(self):
        modal = _modal(
            callback_id="feedback_form",
            title="Send Feedback",
            children=[_text_input(id="message", label="Your Feedback")],
        )
        view = modal_to_slack_view(modal)
        assert view["type"] == "modal"
        assert view["callback_id"] == "feedback_form"
        assert view["title"] == {"type": "plain_text", "text": "Send Feedback"}
        assert view["submit"] == {"type": "plain_text", "text": "Submit"}
        assert view["close"] == {"type": "plain_text", "text": "Cancel"}
        assert len(view["blocks"]) == 1
        block = view["blocks"][0]
        assert block["type"] == "input"
        assert block["block_id"] == "message"
        assert block["label"] == {"type": "plain_text", "text": "Your Feedback"}
        assert block["element"]["type"] == "plain_text_input"
        assert block["element"]["action_id"] == "message"

    def test_custom_submit_and_close_labels(self):
        modal = _modal(title="Test Modal", submit_label="Send", close_label="Dismiss")
        view = modal_to_slack_view(modal)
        assert view["submit"] == {"type": "plain_text", "text": "Send"}
        assert view["close"] == {"type": "plain_text", "text": "Dismiss"}

    def test_multiline_text_input(self):
        modal = _modal(
            children=[
                _text_input(
                    id="description",
                    label="Description",
                    multiline=True,
                    placeholder="Enter description...",
                    max_length=500,
                ),
            ]
        )
        view = modal_to_slack_view(modal)
        el = view["blocks"][0]["element"]
        assert el["type"] == "plain_text_input"
        assert el["multiline"] is True
        assert el["placeholder"] == {"type": "plain_text", "text": "Enter description..."}
        assert el["max_length"] == 500

    def test_optional_text_input(self):
        modal = _modal(children=[_text_input(id="notes", label="Notes", optional=True)])
        view = modal_to_slack_view(modal)
        assert view["blocks"][0]["optional"] is True

    def test_text_input_with_initial_value(self):
        modal = _modal(children=[_text_input(id="name", label="Name", initial_value="John Doe")])
        view = modal_to_slack_view(modal)
        assert view["blocks"][0]["element"]["initial_value"] == "John Doe"

    def test_select_with_options(self):
        modal = _modal(
            children=[
                _select(
                    id="category",
                    label="Category",
                    options=[
                        _option(label="Bug Report", value="bug"),
                        _option(label="Feature Request", value="feature"),
                    ],
                ),
            ]
        )
        view = modal_to_slack_view(modal)
        block = view["blocks"][0]
        assert block["type"] == "input"
        assert block["block_id"] == "category"
        el = block["element"]
        assert el["type"] == "static_select"
        assert el["action_id"] == "category"
        assert len(el["options"]) == 2

    def test_select_with_initial_option(self):
        modal = _modal(
            children=[
                _select(
                    id="priority",
                    label="Priority",
                    options=[
                        _option(label="Low", value="low"),
                        _option(label="Medium", value="medium"),
                        _option(label="High", value="high"),
                    ],
                    initial_option="medium",
                ),
            ]
        )
        view = modal_to_slack_view(modal)
        el = view["blocks"][0]["element"]
        assert el["initial_option"] == {
            "text": {"type": "plain_text", "text": "Medium"},
            "value": "medium",
        }

    def test_select_with_placeholder(self):
        modal = _modal(
            children=[
                _select(
                    id="category",
                    label="Category",
                    placeholder="Select a category",
                    options=[_option(label="General", value="general")],
                ),
            ]
        )
        view = modal_to_slack_view(modal)
        el = view["blocks"][0]["element"]
        assert el["placeholder"] == {"type": "plain_text", "text": "Select a category"}

    def test_context_id_as_private_metadata(self):
        modal = _modal()
        view = modal_to_slack_view(modal, "context-uuid-123")
        assert view["private_metadata"] == "context-uuid-123"

    def test_no_private_metadata(self):
        modal = _modal()
        view = modal_to_slack_view(modal)
        assert view.get("private_metadata") is None

    def test_notify_on_close(self):
        modal = _modal(notify_on_close=True)
        view = modal_to_slack_view(modal)
        assert view["notify_on_close"] is True

    def test_truncates_long_title(self):
        modal = _modal(title="This is a very long modal title that exceeds the limit")
        view = modal_to_slack_view(modal)
        assert len(view["title"]["text"]) <= 24

    def test_complete_modal(self):
        modal = _modal(
            callback_id="feedback_form",
            title="Submit Feedback",
            submit_label="Send",
            close_label="Cancel",
            notify_on_close=True,
            children=[
                _text_input(id="message", label="Your Feedback", placeholder="Tell us...", multiline=True),
                _select(
                    id="category",
                    label="Category",
                    options=[
                        _option(label="Bug", value="bug"),
                        _option(label="Feature", value="feature"),
                        _option(label="Other", value="other"),
                    ],
                ),
                _text_input(id="email", label="Email (optional)", optional=True),
            ],
        )
        view = modal_to_slack_view(modal, "thread-context-123")
        assert view["callback_id"] == "feedback_form"
        assert view["private_metadata"] == "thread-context-123"
        assert len(view["blocks"]) == 3
        for block in view["blocks"]:
            assert block["type"] == "input"


# ---------------------------------------------------------------------------
# encodeModalMetadata
# ---------------------------------------------------------------------------


class TestEncodeModalMetadata:
    def test_returns_none_when_empty(self):
        assert encode_modal_metadata(ModalMetadata()) is None

    def test_encodes_context_id_only(self):
        encoded = encode_modal_metadata(ModalMetadata(context_id="uuid-123"))
        assert encoded is not None
        parsed = json.loads(encoded)
        assert parsed["c"] == "uuid-123"
        assert parsed.get("m") is None

    def test_encodes_private_metadata_only(self):
        encoded = encode_modal_metadata(ModalMetadata(private_metadata='{"chatId":"abc"}'))
        assert encoded is not None
        parsed = json.loads(encoded)
        assert parsed.get("c") is None
        assert parsed["m"] == '{"chatId":"abc"}'

    def test_encodes_both(self):
        encoded = encode_modal_metadata(ModalMetadata(context_id="uuid-123", private_metadata='{"chatId":"abc"}'))
        assert encoded is not None
        parsed = json.loads(encoded)
        assert parsed["c"] == "uuid-123"
        assert parsed["m"] == '{"chatId":"abc"}'


# ---------------------------------------------------------------------------
# decodeModalMetadata
# ---------------------------------------------------------------------------


class TestDecodeModalMetadata:
    def test_undefined_input(self):
        result = decode_modal_metadata(None)
        assert result.context_id is None
        assert result.private_metadata is None

    def test_empty_string(self):
        result = decode_modal_metadata("")
        assert result.context_id is None
        assert result.private_metadata is None

    def test_decodes_context_id(self):
        encoded = json.dumps({"c": "uuid-123"})
        result = decode_modal_metadata(encoded)
        assert result.context_id == "uuid-123"
        assert result.private_metadata is None

    def test_decodes_private_metadata(self):
        encoded = json.dumps({"m": '{"chatId":"abc"}'})
        result = decode_modal_metadata(encoded)
        assert result.context_id is None
        assert result.private_metadata == '{"chatId":"abc"}'

    def test_decodes_both(self):
        encoded = json.dumps({"c": "uuid-123", "m": '{"chatId":"abc"}'})
        result = decode_modal_metadata(encoded)
        assert result.context_id == "uuid-123"
        assert result.private_metadata == '{"chatId":"abc"}'

    def test_backward_compat_plain_string(self):
        result = decode_modal_metadata("plain-uuid-456")
        assert result.context_id == "plain-uuid-456"

    def test_backward_compat_json_without_keys(self):
        result = decode_modal_metadata('{"other":"value"}')
        assert result.context_id == '{"other":"value"}'

    def test_roundtrip(self):
        original = ModalMetadata(context_id="ctx-1", private_metadata='{"key":"val"}')
        encoded = encode_modal_metadata(original)
        decoded = decode_modal_metadata(encoded)
        assert decoded.context_id == original.context_id
        assert decoded.private_metadata == original.private_metadata


# ---------------------------------------------------------------------------
# Radio select in modals
# ---------------------------------------------------------------------------


class TestModalRadioSelect:
    def test_radio_select_element(self):
        modal = _modal(
            children=[
                _radio_select(
                    id="plan",
                    label="Choose Plan",
                    options=[
                        _option(label="Basic", value="basic"),
                        _option(label="Pro", value="pro"),
                        _option(label="Enterprise", value="enterprise"),
                    ],
                ),
            ]
        )
        view = modal_to_slack_view(modal)
        assert len(view["blocks"]) == 1
        block = view["blocks"][0]
        assert block["type"] == "input"
        assert block["block_id"] == "plan"
        assert block["label"] == {"type": "plain_text", "text": "Choose Plan"}
        el = block["element"]
        assert el["type"] == "radio_buttons"
        assert el["action_id"] == "plan"
        assert len(el["options"]) == 3

    def test_optional_radio_select(self):
        modal = _modal(
            children=[
                _radio_select(
                    id="preference",
                    label="Preference",
                    optional=True,
                    options=[
                        _option(label="Yes", value="yes"),
                        _option(label="No", value="no"),
                    ],
                ),
            ]
        )
        view = modal_to_slack_view(modal)
        assert view["blocks"][0]["optional"] is True

    def test_radio_select_uses_mrkdwn(self):
        modal = _modal(
            children=[
                _radio_select(
                    id="option",
                    label="Choose",
                    options=[_option(label="Option A", value="a")],
                ),
            ]
        )
        view = modal_to_slack_view(modal)
        el = view["blocks"][0]["element"]
        assert el["options"][0]["text"]["type"] == "mrkdwn"
        assert el["options"][0]["text"]["text"] == "Option A"

    def test_radio_select_limits_to_10(self):
        options = [_option(label=f"Option {i + 1}", value=f"opt{i + 1}") for i in range(15)]
        modal = _modal(children=[_radio_select(id="many", label="Many Options", options=options)])
        view = modal_to_slack_view(modal)
        el = view["blocks"][0]["element"]
        assert len(el["options"]) == 10


# ---------------------------------------------------------------------------
# Select option descriptions in modals
# ---------------------------------------------------------------------------


class TestModalSelectDescriptions:
    def test_select_description_plain_text(self):
        modal = _modal(
            children=[
                _select(
                    id="plan",
                    label="Plan",
                    options=[
                        _option(label="Basic", value="basic", description="For individuals"),
                        _option(label="Pro", value="pro", description="For teams"),
                    ],
                ),
            ]
        )
        view = modal_to_slack_view(modal)
        el = view["blocks"][0]["element"]
        assert el["options"][0]["description"] == {"type": "plain_text", "text": "For individuals"}
        assert el["options"][1]["description"] == {"type": "plain_text", "text": "For teams"}

    def test_radio_description_mrkdwn(self):
        modal = _modal(
            children=[
                _radio_select(
                    id="plan",
                    label="Plan",
                    options=[
                        _option(label="Basic", value="basic", description="For *individuals*"),
                        _option(label="Pro", value="pro", description="For _teams_"),
                    ],
                ),
            ]
        )
        view = modal_to_slack_view(modal)
        el = view["blocks"][0]["element"]
        assert el["options"][0]["description"] == {"type": "mrkdwn", "text": "For *individuals*"}
        assert el["options"][1]["description"] == {"type": "mrkdwn", "text": "For _teams_"}
