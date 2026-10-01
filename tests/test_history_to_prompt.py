"""Faithful translation of history/to-prompt.test.ts.

TS file: packages/chat/src/history/to-prompt.test.ts
"""

from __future__ import annotations

from chat_sdk import HistoryEntry, to_prompt_entries


def _entry(entry_id: str, role: str, text: str, timestamp: int) -> HistoryEntry:
    return HistoryEntry(
        id=entry_id,
        user_key="u1",
        role=role,  # type: ignore[arg-type]
        text=text,
        platform="slack",
        thread_id="slack:C:T",
        timestamp=timestamp,
    )


class TestToPromptEntries:
    # TS: "maps transcript entries to prompt entries preserving order"
    def test_maps_transcript_entries_to_prompt_entries_preserving_order(self):
        entries = [_entry("1", "user", "Hello", 1), _entry("2", "assistant", "Hi there", 2)]

        assert to_prompt_entries(entries) == [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]

    # TS: "skips entries with empty text"
    def test_skips_entries_with_empty_text(self):
        entries = [_entry("1", "user", "", 1), _entry("2", "assistant", "visible", 2)]

        assert to_prompt_entries(entries) == [{"role": "assistant", "content": "visible"}]
