"""Tests for modal builder functions: Modal, TextInput, Select, RadioSelect, etc.

Also tests is_modal_element and filter_modal_children utilities.
"""

from __future__ import annotations

import warnings

import pytest

from chat_sdk.modals import (
    Modal,
    RadioSelect,
    Select,
    SelectOption,
    TextInput,
    filter_modal_children,
    is_modal_element,
)

# ---------------------------------------------------------------------------
# Modal builder
# ---------------------------------------------------------------------------


class TestModalBuilder:
    def test_basic_modal(self):
        modal = Modal(title="Test", callback_id="test_cb")
        assert modal["type"] == "modal"
        assert modal["title"] == "Test"
        assert modal["callback_id"] == "test_cb"
        assert modal["children"] == []

    def test_modal_with_children(self):
        child = TextInput(id="name", label="Name")
        modal = Modal(title="Form", callback_id="form_cb", children=[child])
        assert len(modal["children"]) == 1
        assert modal["children"][0]["type"] == "text_input"

    def test_modal_with_submit_label(self):
        modal = Modal(title="T", callback_id="cb", submit_label="Send")
        assert modal["submit_label"] == "Send"

    def test_modal_with_close_label(self):
        modal = Modal(title="T", callback_id="cb", close_label="Dismiss")
        assert modal["close_label"] == "Dismiss"

    def test_modal_with_notify_on_close(self):
        modal = Modal(title="T", callback_id="cb", notify_on_close=True)
        assert modal["notify_on_close"] is True

    def test_modal_with_private_metadata(self):
        modal = Modal(title="T", callback_id="cb", private_metadata="ctx-data")
        assert modal["private_metadata"] == "ctx-data"

    def test_modal_omits_none_values(self):
        modal = Modal(title="T", callback_id="cb")
        assert "submit_label" not in modal
        assert "close_label" not in modal
        assert "notify_on_close" not in modal
        assert "private_metadata" not in modal

    def test_modal_with_all_options(self):
        modal = Modal(
            title="Full Modal",
            callback_id="full",
            children=[TextInput(id="f1", label="Field")],
            submit_label="Go",
            close_label="Nah",
            notify_on_close=True,
            private_metadata='{"key": "val"}',
        )
        assert modal["title"] == "Full Modal"
        assert modal["submit_label"] == "Go"
        assert modal["close_label"] == "Nah"
        assert modal["notify_on_close"] is True
        assert modal["private_metadata"] == '{"key": "val"}'
        assert len(modal["children"]) == 1


# ---------------------------------------------------------------------------
# TextInput builder
# ---------------------------------------------------------------------------


class TestTextInputBuilder:
    def test_basic_text_input(self):
        ti = TextInput(id="msg", label="Message")
        assert ti["type"] == "text_input"
        assert ti["id"] == "msg"
        assert ti["label"] == "Message"

    def test_text_input_with_placeholder(self):
        ti = TextInput(id="msg", label="Message", placeholder="Type here...")
        assert ti["placeholder"] == "Type here..."

    def test_text_input_with_initial_value(self):
        ti = TextInput(id="msg", label="Message", initial_value="Default text")
        assert ti["initial_value"] == "Default text"

    def test_text_input_multiline(self):
        ti = TextInput(id="msg", label="Message", multiline=True)
        assert ti["multiline"] is True

    def test_text_input_max_length(self):
        ti = TextInput(id="msg", label="Message", max_length=500)
        assert ti["max_length"] == 500

    def test_text_input_optional(self):
        ti = TextInput(id="msg", label="Message", optional=True)
        assert ti["optional"] is True

    def test_text_input_omits_none(self):
        ti = TextInput(id="msg", label="Message")
        assert "placeholder" not in ti
        assert "initial_value" not in ti
        assert "multiline" not in ti
        assert "max_length" not in ti
        assert "optional" not in ti


# ---------------------------------------------------------------------------
# SelectOption builder
# ---------------------------------------------------------------------------


class TestSelectOptionBuilder:
    def test_basic_option(self):
        opt = SelectOption(label="Yes", value="yes")
        assert opt["label"] == "Yes"
        assert opt["value"] == "yes"

    def test_option_with_description(self):
        opt = SelectOption(label="Pro", value="pro", description="For teams")
        assert opt["description"] == "For teams"

    def test_option_omits_none_description(self):
        opt = SelectOption(label="Basic", value="basic")
        assert "description" not in opt


# ---------------------------------------------------------------------------
# Select builder
# ---------------------------------------------------------------------------


class TestSelectBuilder:
    def test_basic_select(self):
        options = [SelectOption(label="A", value="a"), SelectOption(label="B", value="b")]
        sel = Select(id="choice", label="Choose", options=options)
        assert sel["type"] == "select"
        assert sel["id"] == "choice"
        assert sel["label"] == "Choose"
        assert len(sel["options"]) == 2

    def test_select_with_placeholder(self):
        options = [SelectOption(label="A", value="a")]
        sel = Select(id="c", label="C", options=options, placeholder="Pick one")
        assert sel["placeholder"] == "Pick one"

    def test_select_with_initial_option(self):
        options = [SelectOption(label="A", value="a"), SelectOption(label="B", value="b")]
        sel = Select(id="c", label="C", options=options, initial_option="b")
        assert sel["initial_option"] == "b"

    def test_select_optional(self):
        options = [SelectOption(label="A", value="a")]
        sel = Select(id="c", label="C", options=options, optional=True)
        assert sel["optional"] is True

    def test_select_requires_options(self):
        with pytest.raises(ValueError, match="at least one option"):
            Select(id="c", label="C", options=[])

    def test_select_omits_none(self):
        options = [SelectOption(label="A", value="a")]
        sel = Select(id="c", label="C", options=options)
        assert "placeholder" not in sel
        assert "initial_option" not in sel
        assert "optional" not in sel


# ---------------------------------------------------------------------------
# RadioSelect builder
# ---------------------------------------------------------------------------


class TestRadioSelectBuilder:
    def test_basic_radio_select(self):
        options = [SelectOption(label="Yes", value="yes"), SelectOption(label="No", value="no")]
        rs = RadioSelect(id="confirm", label="Confirm", options=options)
        assert rs["type"] == "radio_select"
        assert rs["id"] == "confirm"
        assert rs["label"] == "Confirm"
        assert len(rs["options"]) == 2

    def test_radio_select_with_initial_option(self):
        options = [SelectOption(label="A", value="a")]
        rs = RadioSelect(id="r", label="R", options=options, initial_option="a")
        assert rs["initial_option"] == "a"

    def test_radio_select_optional(self):
        options = [SelectOption(label="A", value="a")]
        rs = RadioSelect(id="r", label="R", options=options, optional=True)
        assert rs["optional"] is True

    def test_radio_select_requires_options(self):
        with pytest.raises(ValueError, match="at least one option"):
            RadioSelect(id="r", label="R", options=[])

    def test_radio_select_omits_none(self):
        options = [SelectOption(label="A", value="a")]
        rs = RadioSelect(id="r", label="R", options=options)
        assert "initial_option" not in rs
        assert "optional" not in rs


# ---------------------------------------------------------------------------
# is_modal_element
# ---------------------------------------------------------------------------


class TestIsModalElement:
    def test_modal_element_detected(self):
        modal = Modal(title="T", callback_id="cb")
        assert is_modal_element(modal) is True

    def test_non_modal_dict_not_detected(self):
        assert is_modal_element({"type": "text_input", "id": "x"}) is False

    def test_non_dict_not_detected(self):
        assert is_modal_element("not a dict") is False
        assert is_modal_element(42) is False
        assert is_modal_element(None) is False
        assert is_modal_element([]) is False

    def test_dict_without_type_not_detected(self):
        assert is_modal_element({"title": "T"}) is False

    def test_dict_with_modal_type_detected(self):
        assert is_modal_element({"type": "modal"}) is True


# ---------------------------------------------------------------------------
# filter_modal_children
# ---------------------------------------------------------------------------


class TestFilterModalChildren:
    def test_valid_children_pass_through(self):
        children = [
            {"type": "text_input", "id": "a", "label": "A"},
            {"type": "select", "id": "b", "label": "B", "options": []},
            {"type": "radio_select", "id": "c", "label": "C", "options": []},
            {"type": "text", "content": "hello"},
            {"type": "fields", "items": []},
        ]
        result = filter_modal_children(children)
        assert len(result) == 5

    def test_filters_out_invalid_types(self):
        children = [
            {"type": "text_input", "id": "a", "label": "A"},
            {"type": "unknown_widget", "id": "b"},
            {"type": "button", "id": "c", "label": "Click"},
        ]
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = filter_modal_children(children)
            assert len(result) == 1
            assert result[0]["type"] == "text_input"
            assert len(w) == 1
            assert "unsupported child elements" in str(w[0].message).lower()

    def test_filters_out_non_dicts(self):
        children = [
            {"type": "text_input", "id": "a", "label": "A"},
            "not a dict",
            42,
            None,
        ]
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = filter_modal_children(children)
            assert len(result) == 1
            assert len(w) == 1

    def test_empty_list_returns_empty(self):
        result = filter_modal_children([])
        assert result == []

    def test_all_invalid_returns_empty_with_warning(self):
        children = [{"type": "bogus"}, {"type": "also_bogus"}]
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = filter_modal_children(children)
            assert result == []
            assert len(w) == 1

    def test_no_warning_when_all_valid(self):
        children = [
            {"type": "text_input", "id": "a", "label": "A"},
            {"type": "text", "content": "hello"},
        ]
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = filter_modal_children(children)
            assert len(result) == 2
            assert len(w) == 0


# ---------------------------------------------------------------------------
# Complex modal composition
# ---------------------------------------------------------------------------


class TestComplexModalComposition:
    def test_full_feedback_form(self):
        modal = Modal(
            title="Submit Feedback",
            callback_id="feedback_form",
            submit_label="Send",
            close_label="Cancel",
            notify_on_close=True,
            children=[
                TextInput(
                    id="message",
                    label="Your Feedback",
                    placeholder="Tell us...",
                    multiline=True,
                    max_length=3000,
                ),
                Select(
                    id="category",
                    label="Category",
                    options=[
                        SelectOption(label="Bug", value="bug", description="Report a bug"),
                        SelectOption(label="Feature", value="feature", description="Request a feature"),
                        SelectOption(label="Other", value="other"),
                    ],
                    placeholder="Select a category",
                ),
                TextInput(
                    id="email",
                    label="Email (optional)",
                    optional=True,
                ),
                RadioSelect(
                    id="severity",
                    label="Severity",
                    options=[
                        SelectOption(label="Low", value="low"),
                        SelectOption(label="Medium", value="medium"),
                        SelectOption(label="High", value="high"),
                    ],
                    initial_option="medium",
                ),
            ],
        )

        assert modal["type"] == "modal"
        assert modal["callback_id"] == "feedback_form"
        assert len(modal["children"]) == 4
        assert modal["children"][0]["type"] == "text_input"
        assert modal["children"][0]["multiline"] is True
        assert modal["children"][1]["type"] == "select"
        assert len(modal["children"][1]["options"]) == 3
        assert modal["children"][2]["optional"] is True
        assert modal["children"][3]["type"] == "radio_select"
        assert modal["children"][3]["initial_option"] == "medium"
