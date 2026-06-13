"""Slack Block Kit size limits.

Port of ``packages/adapter-slack/src/blocks/limits.ts`` (vercel/chat#555).
Docs-backed character and element caps Slack enforces on Block Kit
payloads. Field names are snake_case (camelCase upstream) since these are
internal constants, not a serialization boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class _Limits:
    action_id: int
    actions_elements: int
    block_id: int
    blocks: int
    button_text: int
    button_url: int
    button_value: int
    fields: int
    field_text: int
    header_text: int
    image_alt: int
    image_url: int
    option_description: int
    option_text: int
    option_value: int
    options: int
    placeholder: int
    radio_options: int
    section_text: int
    table_columns: int
    table_rows: int
    text_object: int


LIMITS: Final = _Limits(
    action_id=255,
    actions_elements=25,
    block_id=255,
    blocks=50,
    button_text=75,
    button_url=3000,
    button_value=2000,
    fields=10,
    field_text=2000,
    header_text=150,
    image_alt=2000,
    image_url=3000,
    option_description=75,
    option_text=75,
    option_value=150,
    options=100,
    placeholder=150,
    radio_options=10,
    section_text=3000,
    table_columns=20,
    table_rows=100,
    text_object=3000,
)
