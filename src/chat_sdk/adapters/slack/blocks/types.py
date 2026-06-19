"""Typed card-input and Block Kit shapes for the Slack blocks subpath.

Port of ``packages/adapter-slack/src/blocks/types.ts`` (vercel/chat#555).

The card-input shapes (``SlackCardElement`` and its children) are a
self-contained copy of the Chat SDK card model, declared here (rather than
imported from ``chat_sdk.cards``) so the blocks subpath stays runtime-free.
Input field names are snake_case (``image_url``, ``initial_option``,
``callback_url``), matching ``chat_sdk.cards`` and the Python port's
internal convention — upstream uses camelCase. The emitted Block Kit dicts
keep Slack's API field names verbatim (``alt_text``, ``action_id``,
``block_id``, ``static_select``, ``raw_text``, ...), which is the Slack
serialization boundary.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, NotRequired, TypedDict

SlackButtonStyle = Literal["danger", "default", "primary"]
SlackTextStyle = Literal["bold", "muted", "plain"]
SlackTableAlignment = Literal["center", "left", "right"]


class SlackTextElement(TypedDict):
    """A run of card text, optionally styled."""

    type: Literal["text"]
    content: str
    style: NotRequired[SlackTextStyle]


class SlackImageElement(TypedDict):
    """A standalone image block."""

    type: Literal["image"]
    url: str
    alt: NotRequired[str]


class SlackDividerElement(TypedDict):
    """A horizontal rule."""

    type: Literal["divider"]


class SlackButtonElement(TypedDict):
    """An interactive button that emits an ``action_id``/``value``."""

    type: Literal["button"]
    id: str
    label: str
    callback_url: NotRequired[str]
    disabled: NotRequired[bool]
    style: NotRequired[SlackButtonStyle]
    value: NotRequired[str]


class SlackLinkButtonElement(TypedDict):
    """A button that opens a URL."""

    type: Literal["link-button"]
    label: str
    url: str
    id: NotRequired[str]
    style: NotRequired[SlackButtonStyle]


class SlackSelectOptionElement(TypedDict):
    """One option in a select or radio group."""

    label: str
    value: str
    description: NotRequired[str]


class SlackSelectElement(TypedDict):
    """A static single-select menu."""

    type: Literal["select"]
    id: str
    label: str
    options: list[SlackSelectOptionElement]
    initial_option: NotRequired[str]
    placeholder: NotRequired[str]


class SlackRadioSelectElement(TypedDict):
    """A radio-button group."""

    type: Literal["radio_select"]
    id: str
    label: str
    options: list[SlackSelectOptionElement]
    initial_option: NotRequired[str]


class SlackLinkElement(TypedDict):
    """A standalone hyperlink rendered as a section."""

    type: Literal["link"]
    label: str
    url: str


class SlackFieldElement(TypedDict):
    """A label/value pair inside a fields block."""

    type: Literal["field"]
    label: str
    value: str


class SlackFieldsElement(TypedDict):
    """A two-column fields block."""

    type: Literal["fields"]
    children: list[SlackFieldElement]


class SlackTableElement(TypedDict):
    """A table rendered natively or as an ASCII fallback."""

    type: Literal["table"]
    headers: list[str]
    rows: list[list[str]]
    align: NotRequired[list[SlackTableAlignment]]


class SlackActionsElement(TypedDict):
    """A row of interactive action elements."""

    type: Literal["actions"]
    children: list[SlackButtonElement | SlackLinkButtonElement | SlackRadioSelectElement | SlackSelectElement]


class SlackSectionElement(TypedDict):
    """A grouping element whose children are flattened inline."""

    type: Literal["section"]
    children: list[SlackCardChild]


# A child of a card or section. Discriminated on the ``type`` key.
SlackCardChild = (
    SlackActionsElement
    | SlackDividerElement
    | SlackFieldsElement
    | SlackImageElement
    | SlackLinkElement
    | SlackSectionElement
    | SlackTableElement
    | SlackTextElement
)


class SlackCardElement(TypedDict):
    """The root card object converted by :func:`cardToSlackBlocks`."""

    type: Literal["card"]
    children: list[SlackCardChild]
    image_url: NotRequired[str]
    subtitle: NotRequired[str]
    title: NotRequired[str]


# An emitted Slack Block Kit block (``{"type": ..., ...}``).
SlackBlock = dict[str, Any]

# A Slack text object (``{"type": "mrkdwn" | "plain_text", "text": ...}``).
SlackTextObject = dict[str, Any]


class SlackBlocksOptions(TypedDict, total=False):
    """Options for :func:`cardToSlackBlocks`."""

    convert_emoji: Callable[[str], str]
    max_blocks: int
