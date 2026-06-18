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
