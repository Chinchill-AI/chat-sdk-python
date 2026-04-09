"""Convert chat Messages to AI SDK format.

Python port of ai.ts.
"""

from __future__ import annotations

import base64
import inspect
import logging
import warnings
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from chat_sdk.types import Attachment, Message

logger = logging.getLogger(__name__)

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
# MIME helpers
# ---------------------------------------------------------------------------

TEXT_MIME_PREFIXES = (
    "text/",
    "application/json",
    "application/xml",
    "application/javascript",
    "application/typescript",
    "application/yaml",
    "application/x-yaml",
    "application/toml",
)


def _is_text_mime_type(mime_type: str) -> bool:
    return any(mime_type == p or mime_type.startswith(p) for p in TEXT_MIME_PREFIXES)


# ---------------------------------------------------------------------------
# Attachment conversion
# ---------------------------------------------------------------------------


async def _attachment_to_part(att: Attachment) -> AiMessagePart | None:
    """Build an AI SDK content part from an attachment.

    Uses ``fetch_data`` to get base64 data when available.
    Returns ``None`` for unsupported attachments.
    """
    if att.type == "image":
        if att.fetch_data is not None:
            try:
                buffer = await att.fetch_data()
                mime_type = att.mime_type or "image/png"
                b64 = base64.b64encode(buffer).decode("ascii")
                return AiFilePart(
                    type="file",
                    data=f"data:{mime_type};base64,{b64}",
                    mediaType=mime_type,
                    filename=att.name or "",
                )
            except Exception:
                logger.exception("toAiMessages: failed to fetch image data")
                return None
        return None

    if att.type == "file" and att.mime_type and _is_text_mime_type(att.mime_type):
        if att.fetch_data is not None:
            try:
                buffer = await att.fetch_data()
                b64 = base64.b64encode(buffer).decode("ascii")
                return AiFilePart(
                    type="file",
                    data=f"data:{att.mime_type};base64,{b64}",
                    filename=att.name or "",
                    mediaType=att.mime_type,
                )
            except Exception:
                logger.exception("toAiMessages: failed to fetch file data")
                return None
        return None

    # Unsupported type -- caller handles warning
    return None


# ---------------------------------------------------------------------------
# Main conversion function
# ---------------------------------------------------------------------------


async def to_ai_messages(
    messages: list[Message],
    options: ToAiMessagesOptions | None = None,
) -> list[AiMessage]:
    """Convert chat SDK messages to AI SDK conversation format.

    - Filters out messages with empty/whitespace-only text
    - Maps ``author.is_me == True`` to ``"assistant"``, otherwise ``"user"``
    - Uses ``message.text`` for content
    - Appends link metadata when available
    - Includes image attachments and text files as ``AiFilePart``
    - Uses ``fetch_data()`` when available to include attachment data inline (base64)
    - Warns on unsupported attachment types (video, audio)
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

    # Sort chronologically (oldest first)
    sorted_msgs = sorted(
        messages,
        key=lambda m: m.metadata.date_sent.timestamp() if m.metadata.date_sent else 0,
    )

    filtered = [m for m in sorted_msgs if m.text.strip()]

    results: list[AiMessage] = []

    for msg in filtered:
        role: Literal["user", "assistant"] = "assistant" if msg.author.is_me else "user"
        text_content = f"[{msg.author.user_name}]: {msg.text}" if include_names and role == "user" else msg.text

        # Append link metadata when available
        if msg.links:
            link_parts_list: list[str] = []
            for link in msg.links:
                parts: list[str] = []
                if link.fetch_message:
                    parts.append(f"[Embedded message: {link.url}]")
                else:
                    parts.append(link.url)
                if link.title:
                    parts.append(f"Title: {link.title}")
                if link.description:
                    parts.append(f"Description: {link.description}")
                if link.site_name:
                    parts.append(f"Site: {link.site_name}")
                link_parts_list.append("\n".join(parts))
            text_content += "\n\nLinks:\n" + "\n\n".join(link_parts_list)

        # Build attachment parts for images and text files (only for user messages)
        ai_message: AiMessage
        if role == "user":
            attachment_parts: list[AiMessagePart] = []
            for att in msg.attachments or []:
                part = await _attachment_to_part(att)
                if part is not None:
                    attachment_parts.append(part)
                elif att.type in ("video", "audio"):
                    on_unsupported(att, msg)

            if attachment_parts:
                ai_message = AiUserMessage(
                    role="user",
                    content=[
                        AiTextPart(type="text", text=text_content),
                        *attachment_parts,
                    ],
                )
            else:
                ai_message = AiUserMessage(role="user", content=text_content)
        else:
            ai_message = AiAssistantMessage(role="assistant", content=text_content)

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
