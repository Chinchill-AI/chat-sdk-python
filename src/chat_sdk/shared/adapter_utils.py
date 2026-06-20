"""Shared utility functions for chat adapters."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from chat_sdk.cards import CardElement, is_card_element
from chat_sdk.types import AdapterPostableMessage, Attachment, FileUpload

# Optional opt-in hook signature an adapter/consumer may set on the adapter
# instance as ``render_thinking`` to receive streaming ``ThinkingChunk``
# content. May be sync or async; both are supported by
# :func:`maybe_render_thinking`.
RenderThinking = Callable[[str], Awaitable[None] | None]


def is_thinking_chunk(chunk: Any) -> bool:
    """Return True if a stream chunk is a Python-only ``ThinkingChunk``.

    Matches both the dataclass form (``chunk.type == "thinking"``) and the
    dict-normalized form (``chunk["type"] == "thinking"``). Used by adapter
    stream loops to *skip* thinking when accumulating the posted message —
    thinking is streaming-only reasoning, not message content.
    """
    if isinstance(chunk, dict):
        return chunk.get("type") == "thinking"
    return getattr(chunk, "type", None) == "thinking"


def thinking_content(chunk: Any) -> str:
    """Extract the reasoning text from a ``ThinkingChunk`` (dict or dataclass)."""
    value = chunk.get("content") if isinstance(chunk, dict) else getattr(chunk, "content", None)
    return value if isinstance(value, str) else ""


async def maybe_render_thinking(hook: RenderThinking | None, chunk: Any) -> None:
    """Invoke an opt-in ``render_thinking`` hook for a ``ThinkingChunk``, else skip.

    Default behavior (``hook is None``) is a no-op: the thinking chunk is
    silently skipped so the posted message stays byte-identical to upstream and
    adapters that don't render thinking never crash. When a hook is provided it
    is called with the reasoning text; both sync and async hooks are supported.
    """
    if hook is None:
        return
    content = thinking_content(chunk)
    result = hook(content)
    if inspect.isawaitable(result):
        await result


def extract_card(message: AdapterPostableMessage) -> CardElement | None:
    """Extract CardElement from an AdapterPostableMessage if present."""
    if is_card_element(message):
        return message  # type: ignore[return-value]
    if isinstance(message, dict) and "card" in message:
        return message["card"]
    if hasattr(message, "card"):
        return message.card  # type: ignore[union-attr]
    return None


def extract_files(message: AdapterPostableMessage) -> list[FileUpload]:
    """Extract FileUpload array from an AdapterPostableMessage if present."""
    if isinstance(message, str):
        return []
    if hasattr(message, "files") and message.files:  # type: ignore[union-attr]
        return message.files  # type: ignore[union-attr]
    if isinstance(message, dict) and "files" in message:
        return message.get("files") or []
    return []


def extract_postable_attachments(message: AdapterPostableMessage) -> list[Attachment]:
    """Extract a typed Attachment array from an AdapterPostableMessage.

    Port of upstream ``extractPostableAttachments`` (vercel/chat#485). Returns
    the message's ``attachments`` array when present, else an empty list.
    Non-object messages (plain strings, cards) yield an empty list.
    """
    if isinstance(message, str):
        return []
    if hasattr(message, "attachments"):
        attachments = message.attachments  # type: ignore[union-attr]
        return attachments if attachments is not None else []
    if isinstance(message, dict) and "attachments" in message:
        attachments = message.get("attachments")
        return attachments if attachments is not None else []
    return []
