"""Root package re-export surface.

Upstream (`packages/chat/src/index.ts`) re-exports `toAiMessages` plus eight
deprecated AI type aliases from the package root for backwards compatibility,
each marked `@deprecated` and pointing at the canonical `chat/ai` subpath.

These tests pin the Python equivalent: `chat_sdk.AiMessage` (and friends)
resolve at the root and are identical objects to their canonical
`chat_sdk.ai` home, while `chat_sdk.ai` remains the preferred import.
"""

from __future__ import annotations

import chat_sdk
import chat_sdk.ai as chat_sdk_ai
import chat_sdk.cards as chat_sdk_cards
import chat_sdk.history as chat_sdk_history
import chat_sdk.modals as chat_sdk_modals
import chat_sdk.types as chat_sdk_types

# The exact set of deprecated AI type aliases re-exported from the root,
# mirroring upstream index.ts:8-27.
_DEPRECATED_AI_TYPE_ALIASES = (
    "AiAssistantMessage",
    "AiFilePart",
    "AiImagePart",
    "AiMessage",
    "AiMessagePart",
    "AiTextPart",
    "AiUserMessage",
    "ToAiMessagesOptions",
)


def test_deprecated_ai_type_aliases_resolve_at_root() -> None:
    for name in _DEPRECATED_AI_TYPE_ALIASES:
        assert hasattr(chat_sdk, name), f"chat_sdk.{name} should resolve at the root"


def test_root_ai_aliases_are_the_canonical_objects() -> None:
    # The root re-export must be the same object as the canonical chat_sdk.ai
    # home — not a fresh shadow type — so isinstance / identity checks agree.
    for name in _DEPRECATED_AI_TYPE_ALIASES:
        assert getattr(chat_sdk, name) is getattr(chat_sdk_ai, name)


def test_deprecated_ai_type_aliases_in_dunder_all() -> None:
    for name in _DEPRECATED_AI_TYPE_ALIASES:
        assert name in chat_sdk.__all__, f"{name} missing from chat_sdk.__all__"


def test_to_ai_messages_still_re_exported_at_root() -> None:
    # The helper that the aliases accompany stays available and canonical.
    assert chat_sdk.to_ai_messages is chat_sdk_ai.to_ai_messages
    assert "to_ai_messages" in chat_sdk.__all__


# Card/modal builders, aliases and types added in the 4.41 wave (#202),
# keyed by their canonical module.
_CARD_MODAL_4_41_EXPORTS = {
    chat_sdk_cards: (
        "CardWidth",
        "Chart",
        "ChartDataPoint",
        "ChartDefinition",
        "ChartElement",
        "ChartSegment",
        "ChartSeries",
        "PieChartDefinition",
        "SeriesChartDefinition",
        "TableGridStyle",
        "TableVerticalAlignment",
        "chart",
        "chart_element_to_fallback_text",
    ),
    chat_sdk_modals: (
        "DateInput",
        "DateInputElement",
        "NumberInput",
        "NumberInputElement",
        "date_input",
        "number_input",
    ),
}


def test_card_and_modal_4_41_additions_are_root_exports() -> None:
    for module, names in _CARD_MODAL_4_41_EXPORTS.items():
        for name in names:
            assert name in chat_sdk.__all__, f"{name} missing from chat_sdk.__all__"
            assert getattr(chat_sdk, name) is getattr(module, name)
    # snake_case aliases are the PascalCase builders themselves
    assert chat_sdk.chart is chat_sdk.Chart
    assert chat_sdk.date_input is chat_sdk.DateInput
    assert chat_sdk.number_input is chat_sdk.NumberInput


# History API names added in the 4.41 wave (#197), mirroring upstream
# index.ts, keyed by their canonical module.
_HISTORY_4_41_EXPORTS = {
    chat_sdk_history: ("HistoryApiImpl", "PromptEntry", "to_prompt_entries"),
    chat_sdk_types: (
        "ChannelHistoryApi",
        "HistoryApi",
        "HistoryConfig",
        "HistoryEntry",
        "ThreadHistoryApi",
        "UserHistoryApi",
        "UserHistoryConfig",
        "UserHistoryEntry",
    ),
}


def test_history_4_41_additions_are_root_exports() -> None:
    for module, names in _HISTORY_4_41_EXPORTS.items():
        for name in names:
            assert name in chat_sdk.__all__, f"{name} missing from chat_sdk.__all__"
            assert getattr(chat_sdk, name) is getattr(module, name)
    # The canonical entry names are the deprecated ones, not new classes.
    assert chat_sdk.HistoryEntry is chat_sdk.TranscriptEntry
    assert chat_sdk.UserHistoryEntry is chat_sdk.TranscriptEntry
    assert chat_sdk.UserHistoryApi is chat_sdk.TranscriptsApi
