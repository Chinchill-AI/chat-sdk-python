"""Prompt helpers for user history entries.

Python port of ``history/to-prompt.ts`` (and ``PromptEntry`` from
``history/types.ts``).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TypedDict

from chat_sdk.types import TranscriptEntry, TranscriptRole


class PromptEntry(TypedDict):
    """A normalized entry suitable for passing to an LLM as chat history."""

    role: TranscriptRole
    content: str


def to_prompt_entries(entries: Iterable[TranscriptEntry]) -> list[PromptEntry]:
    """Convert history entries into ``{"role", "content"}`` prompt entries.

    Only entries with non-empty text are included. Entry order is preserved
    (chronological, oldest first — the natural order returned by
    ``history.user.list()``).
    """
    result: list[PromptEntry] = []
    for entry in entries:
        if not entry.text:
            continue
        result.append({"role": entry.role, "content": entry.text})
    return result
