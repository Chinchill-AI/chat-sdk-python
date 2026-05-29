"""AI SDK integration for the chat SDK.

Python port of the ``chat/ai`` subpath. Mirrors the upstream structure where
``ai.ts`` was split into ``ai/messages.ts`` (and, in later PRs, ``ai/tools.ts``)
to make room for tool factories.

Re-exports everything the former ``chat_sdk.ai`` module exposed so existing
imports such as ``from chat_sdk.ai import to_ai_messages`` keep working.
"""

from __future__ import annotations

from chat_sdk.ai.messages import (
    AiAssistantMessage,
    AiFilePart,
    AiImagePart,
    AiMessage,
    AiMessagePart,
    AiTextPart,
    AiUserMessage,
    ToAiMessagesOptions,
    to_ai_messages,
)

__all__ = [
    "AiAssistantMessage",
    "AiFilePart",
    "AiImagePart",
    "AiMessage",
    "AiMessagePart",
    "AiTextPart",
    "AiUserMessage",
    "ToAiMessagesOptions",
    "to_ai_messages",
]
