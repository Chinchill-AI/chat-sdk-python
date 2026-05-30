"""Shared utility functions for chat adapters."""

from __future__ import annotations

from chat_sdk.cards import CardElement, is_card_element
from chat_sdk.types import AdapterPostableMessage, Attachment, FileUpload


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
