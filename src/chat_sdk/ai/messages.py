"""Convert chat Messages to AI SDK format.

Python port of ``ai/messages.ts``. The shared text, link and attachment
helpers live in :mod:`chat_sdk.ai.message_content` (upstream
``ai/message-content.ts``).
"""

from __future__ import annotations

import inspect
import warnings
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from chat_sdk.ai.message_content import (
    TEXT_MIME_PREFIXES,
    _attachment_to_part,
    _build_message_text,
    _is_unsupported_attachment,
    _sort_by_date_sent,
)
from chat_sdk.shared._js_compat import JS_WHITESPACE
from chat_sdk.types import Attachment, Message

__all__ = [
    "TEXT_MIME_PREFIXES",
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

# ---------------------------------------------------------------------------
# AI message part types
# ---------------------------------------------------------------------------


class AiTextPart(TypedDict):
    """Text content part."""

    text: str
    type: Literal["text"]


class AiImagePart(TypedDict, total=False):
    """Image content part."""

    image: Any  # bytes | str | URL
    mediaType: str
    type: Literal["image"]


class AiFilePart(TypedDict, total=False):
    """File content part."""

    data: Any  # bytes | str | URL
    filename: str
    mediaType: str
    type: Literal["file"]


AiMessagePart = AiTextPart | AiImagePart | AiFilePart


class AiUserMessage(TypedDict):
    """User message for AI SDK."""

    content: str | list[AiMessagePart]
    role: Literal["user"]


class AiAssistantMessage(TypedDict):
    """Assistant message for AI SDK."""

    content: str
    role: Literal["assistant"]


AiMessage = AiUserMessage | AiAssistantMessage


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


@dataclass
class ToAiMessagesOptions:
    """Options for converting messages to AI SDK format."""

    include_names: bool = False
    on_unsupported_attachment: Callable[[Attachment, Message], None] | None = None
    transform_message: Callable[[AiMessage, Message], AiMessage | None | Awaitable[AiMessage | None]] | None = None


# ---------------------------------------------------------------------------
# Main conversion function
# ---------------------------------------------------------------------------


async def to_ai_messages(
    messages: list[Message],
    options: ToAiMessagesOptions | None = None,
) -> list[AiMessage]:
    """Convert chat SDK messages to AI SDK conversation format.

    - Keeps messages that have no text but carry content (images, text
      files, links). Only messages with no usable content at all are skipped,
      before ``transform_message`` runs.
    - Maps ``author.is_me == True`` to ``"assistant"``, otherwise ``"user"``
    - Uses ``message.text`` for content
    - Appends bounded link metadata inside an explicit untrusted-content fence
    - Includes image attachments and text files as ``AiFilePart``; a user
      message with attachments gets a leading text part only when it has
      text or links
    - Uses ``fetch_data()`` when available to include attachment data inline (base64)
    - Warns on unsupported attachment types (video, audio), including on
      messages that are then skipped for having no other content
    """
    opts = options or ToAiMessagesOptions()
    include_names = opts.include_names
    transform_message = opts.transform_message

    def _default_unsupported(att: Attachment, msg: Message) -> None:
        name_str = f" ({att.name})" if att.name else ""
        warnings.warn(
            f'toAiMessages: unsupported attachment type "{att.type}"{name_str} -- skipped',
            stacklevel=2,
        )

    on_unsupported = opts.on_unsupported_attachment or _default_unsupported

    results: list[AiMessage] = []

    for msg in _sort_by_date_sent(messages):
        role: Literal["user", "assistant"] = "assistant" if msg.author.is_me else "user"
        text_content = _build_message_text(msg, include_names=include_names, role=role)

        # Build attachment parts for images and text files (only for user messages)
        ai_message: AiMessage
        if role == "user":
            attachment_parts: list[AiMessagePart] = []
            for att in msg.attachments or []:
                part = await _attachment_to_part(att)
                if part is not None:
                    attachment_parts.append(part)
                elif _is_unsupported_attachment(att):
                    on_unsupported(att, msg)

            if attachment_parts:
                # Only prepend a text part when there is text: a message may
                # carry images (or other attachments) with no text at all.
                parts: list[AiMessagePart] = (
                    [AiTextPart(type="text", text=text_content), *attachment_parts]
                    if text_content
                    else attachment_parts
                )
                ai_message = AiUserMessage(role="user", content=parts)
            else:
                ai_message = AiUserMessage(role="user", content=text_content)
        else:
            ai_message = AiAssistantMessage(role="assistant", content=text_content)

        # Skip messages that carry no usable content (no text, attachments or
        # links) before the transform sees them. Explicit shape checks, not
        # bare truthiness (upstream: ``trim().length === 0`` / ``length === 0``).
        content = ai_message["content"]
        if (isinstance(content, str) and not content.strip(JS_WHITESPACE)) or (
            isinstance(content, list) and len(content) == 0
        ):
            continue

        if transform_message is not None:
            transformed = transform_message(ai_message, msg)
            # Handle both sync and async transform functions
            if inspect.isawaitable(transformed):
                transformed = await transformed  # type: ignore[misc]
            if transformed is None:
                continue
            ai_message = transformed  # type: ignore[assignment]

        results.append(ai_message)

    return results
