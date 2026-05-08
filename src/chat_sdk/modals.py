"""Modal elements for form dialogs."""

from __future__ import annotations

import warnings
from typing import Any, TypedDict

from chat_sdk.cards import FieldsElement, TextElement


class TextInputElement(TypedDict, total=False):
    """Text input form element."""

    type: str  # "text_input"
    id: str
    label: str
    placeholder: str
    initial_value: str
    multiline: bool
    max_length: int
    optional: bool


class SelectOptionElement(TypedDict, total=False):
    """Option for select elements."""

    label: str
    value: str
    description: str


class SelectElement(TypedDict, total=False):
    """Select/dropdown form element."""

    type: str  # "select"
    id: str
    label: str
    placeholder: str
    options: list[SelectOptionElement]
    initial_option: str
    optional: bool


class RadioSelectElement(TypedDict, total=False):
    """Radio button group form element."""

    type: str  # "radio_select"
    id: str
    label: str
    options: list[SelectOptionElement]
    initial_option: str
    optional: bool


class OptionsLoadGroup(TypedDict):
    """A labeled group of options returned by an ``onOptionsLoad`` handler.

    Maps to upstream TS ``OptionsLoadGroup``. Slack ``external_select`` renders
    grouped results as ``option_groups`` (mutually exclusive with top-level
    ``options`` per Slack's spec).
    """

    label: str
    options: list[SelectOptionElement]


class ExternalSelectElement(TypedDict, total=False):
    """External select form element (loads options dynamically from a handler)."""

    type: str  # "external_select"
    id: str
    label: str
    placeholder: str
    min_query_length: int
    optional: bool
    # Pre-selected option when the modal opens (must match an option returned
    # by the loader). Unlike static :class:`SelectElement`, the initial value
    # is the full ``{label, value}`` object since the loader has not run yet.
    initial_option: SelectOptionElement


# Union of all modal child types
ModalChild = TextInputElement | SelectElement | ExternalSelectElement | RadioSelectElement | TextElement | FieldsElement


class ModalElement(TypedDict, total=False):
    """Root modal element."""

    type: str  # "modal"
    title: str
    callback_id: str
    submit_label: str
    close_label: str
    notify_on_close: bool
    private_metadata: str
    children: list[ModalChild]


VALID_MODAL_CHILD_TYPES = {"text_input", "select", "external_select", "radio_select", "text", "fields"}


def is_modal_element(value: Any) -> bool:
    """Check if a value is a ModalElement."""
    return isinstance(value, dict) and value.get("type") == "modal"


def filter_modal_children(children: list[Any]) -> list[ModalChild]:
    """Filter modal children to only valid types."""
    valid: list[ModalChild] = []
    for c in children:
        if isinstance(c, dict) and c.get("type") in VALID_MODAL_CHILD_TYPES:
            valid.append(c)  # type: ignore[arg-type]
    if len(valid) < len(children):
        warnings.warn(
            "[chat] Modal contains unsupported child elements that were ignored",
            stacklevel=2,
        )
    return valid


# =============================================================================
# Builder Functions (PascalCase primary — matches source TS SDK)
# =============================================================================


def Modal(
    *,
    title: str,
    callback_id: str,
    children: list[ModalChild] | None = None,
    submit_label: str | None = None,
    close_label: str | None = None,
    notify_on_close: bool | None = None,
    private_metadata: str | None = None,
) -> ModalElement:
    """Build a :class:`ModalElement` dict."""
    result: ModalElement = {
        "type": "modal",
        "title": title,
        "callback_id": callback_id,
        "children": children or [],
    }
    if submit_label is not None:
        result["submit_label"] = submit_label
    if close_label is not None:
        result["close_label"] = close_label
    if notify_on_close is not None:
        result["notify_on_close"] = notify_on_close
    if private_metadata is not None:
        result["private_metadata"] = private_metadata
    return result


def TextInput(
    *,
    id: str,
    label: str,
    placeholder: str | None = None,
    initial_value: str | None = None,
    multiline: bool | None = None,
    max_length: int | None = None,
    optional: bool | None = None,
) -> TextInputElement:
    """Build a :class:`TextInputElement` dict."""
    result: TextInputElement = {
        "type": "text_input",
        "id": id,
        "label": label,
    }
    if placeholder is not None:
        result["placeholder"] = placeholder
    if initial_value is not None:
        result["initial_value"] = initial_value
    if multiline is not None:
        result["multiline"] = multiline
    if max_length is not None:
        result["max_length"] = max_length
    if optional is not None:
        result["optional"] = optional
    return result


def Select(
    *,
    id: str,
    label: str,
    options: list[SelectOptionElement],
    placeholder: str | None = None,
    initial_option: str | None = None,
    optional: bool | None = None,
) -> SelectElement:
    """Build a :class:`SelectElement` dict."""
    if not options:
        raise ValueError("Select requires at least one option")
    result: SelectElement = {
        "type": "select",
        "id": id,
        "label": label,
        "options": options,
    }
    if placeholder is not None:
        result["placeholder"] = placeholder
    if initial_option is not None:
        result["initial_option"] = initial_option
    if optional is not None:
        result["optional"] = optional
    return result


def ExternalSelect(
    *,
    id: str,
    label: str,
    placeholder: str | None = None,
    min_query_length: int | None = None,
    optional: bool | None = None,
    initial_option: SelectOptionElement | None = None,
) -> ExternalSelectElement:
    """Build an :class:`ExternalSelectElement` dict.

    Slack-only: renders to a Block Kit ``external_select`` element whose
    options are populated by an :func:`Chat.on_options_load` handler at
    runtime. ``initial_option`` is the full ``{label, value}`` object (the
    loader hasn't run yet so just a value string would be ambiguous).
    """
    result: ExternalSelectElement = {
        "type": "external_select",
        "id": id,
        "label": label,
    }
    if placeholder is not None:
        result["placeholder"] = placeholder
    if min_query_length is not None:
        result["min_query_length"] = min_query_length
    if optional is not None:
        result["optional"] = optional
    if initial_option is not None:
        result["initial_option"] = initial_option
    return result


def SelectOption(
    *,
    label: str,
    value: str,
    description: str | None = None,
) -> SelectOptionElement:
    """Build a :class:`SelectOptionElement` dict."""
    result: SelectOptionElement = {
        "label": label,
        "value": value,
    }
    if description is not None:
        result["description"] = description
    return result


def RadioSelect(
    *,
    id: str,
    label: str,
    options: list[SelectOptionElement],
    initial_option: str | None = None,
    optional: bool | None = None,
) -> RadioSelectElement:
    """Build a :class:`RadioSelectElement` dict."""
    if not options:
        raise ValueError("RadioSelect requires at least one option")
    result: RadioSelectElement = {
        "type": "radio_select",
        "id": id,
        "label": label,
        "options": options,
    }
    if initial_option is not None:
        result["initial_option"] = initial_option
    if optional is not None:
        result["optional"] = optional
    return result


# =============================================================================
# snake_case aliases for PEP 8 purists
# =============================================================================

modal = Modal
text_input = TextInput
select = Select
external_select = ExternalSelect
select_option = SelectOption
radio_select = RadioSelect
