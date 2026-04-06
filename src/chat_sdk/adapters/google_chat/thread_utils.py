"""Thread ID encoding/decoding utilities for Google Chat adapter.

Python port of thread-utils.ts.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

from chat_sdk.shared import ValidationError


@dataclass
class GoogleChatThreadId:
    """Google Chat-specific thread ID data."""

    space_name: str
    thread_name: str | None = None
    is_dm: bool | None = None


def encode_thread_id(platform_data: GoogleChatThreadId) -> str:
    """Encode platform-specific data into a thread ID string.

    Format: gchat:{spaceName}:{base64url(threadName)}:{dm}
    """
    thread_part = ""
    if platform_data.thread_name:
        encoded = base64.urlsafe_b64encode(platform_data.thread_name.encode("utf-8")).decode("ascii")
        # Strip padding to match Node's base64url behavior
        encoded = encoded.rstrip("=")
        thread_part = f":{encoded}"

    # Add :dm suffix for DM threads to enable is_dm() detection
    dm_part = ":dm" if platform_data.is_dm else ""

    return f"gchat:{platform_data.space_name}{thread_part}{dm_part}"


def decode_thread_id(thread_id: str) -> GoogleChatThreadId:
    """Decode thread ID string back to platform-specific data."""
    # Remove :dm suffix if present
    is_dm = thread_id.endswith(":dm")
    clean_id = thread_id[:-3] if is_dm else thread_id

    parts = clean_id.split(":")
    if len(parts) < 2 or parts[0] != "gchat":
        raise ValidationError(
            "gchat",
            f"Invalid Google Chat thread ID: {thread_id}",
        )

    space_name = parts[1]
    thread_name: str | None = None
    if len(parts) > 2 and parts[2]:
        # Add padding back for base64url decoding
        encoded = parts[2]
        padding = 4 - len(encoded) % 4
        if padding != 4:
            encoded += "=" * padding
        thread_name = base64.urlsafe_b64decode(encoded).decode("utf-8")

    return GoogleChatThreadId(
        space_name=space_name,
        thread_name=thread_name,
        is_dm=is_dm,
    )


def is_dm_thread(thread_id: str) -> bool:
    """Check if a thread is a direct message conversation.

    Checks for the :dm marker in the thread ID which is set when
    processing DM messages or opening DMs.
    """
    return thread_id.endswith(":dm")
