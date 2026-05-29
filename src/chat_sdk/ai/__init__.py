"""AI SDK integration for the chat SDK.

Python port of the ``chat/ai`` subpath. Mirrors the upstream structure where
``ai.ts`` was split into ``ai/messages.ts`` and ``ai/tools.ts`` (plus the
``ai/tools/*`` helpers) to make room for the tool factory surface.

Re-exports the message-conversion helpers and the tool factory + supporting
types so callers can do ``from chat_sdk.ai import to_ai_messages,
create_chat_tools`` regardless of how upstream splits the source files.
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
from chat_sdk.ai.tools import (
    ApprovalConfig,
    ChatBinding,
    ChatTool,
    ChatToolName,
    ChatToolPreset,
    ChatTools,
    ChatToolsOptions,
    ChatWriteToolName,
    ToolOptions,
    ToolOverrides,
    add_reaction,
    create_chat_tools,
    delete_message,
    edit_message,
    fetch_channel_messages,
    fetch_messages,
    fetch_thread,
    get_channel_info,
    get_thread_participants,
    get_user,
    list_threads,
    post_channel_message,
    post_message,
    remove_reaction,
    send_direct_message,
    start_typing,
    subscribe_thread,
    unsubscribe_thread,
)

__all__ = [
    "AiAssistantMessage",
    "AiFilePart",
    "AiImagePart",
    "AiMessage",
    "AiMessagePart",
    "AiTextPart",
    "AiUserMessage",
    "ApprovalConfig",
    "ChatBinding",
    "ChatTool",
    "ChatToolName",
    "ChatToolPreset",
    "ChatTools",
    "ChatToolsOptions",
    "ChatWriteToolName",
    "ToAiMessagesOptions",
    "ToolOptions",
    "ToolOverrides",
    "add_reaction",
    "create_chat_tools",
    "delete_message",
    "edit_message",
    "fetch_channel_messages",
    "fetch_messages",
    "fetch_thread",
    "get_channel_info",
    "get_thread_participants",
    "get_user",
    "list_threads",
    "post_channel_message",
    "post_message",
    "remove_reaction",
    "send_direct_message",
    "start_typing",
    "subscribe_thread",
    "unsubscribe_thread",
    "to_ai_messages",
]
