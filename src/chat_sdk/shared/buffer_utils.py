"""Buffer conversion utilities for handling file uploads."""

from __future__ import annotations

import base64

from chat_sdk.shared.errors import ValidationError


async def to_buffer(
    data: bytes | bytearray | memoryview | object, platform: str, *, throw_on_unsupported: bool = True
) -> bytes | None:
    """Convert various data types to bytes."""
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    if isinstance(data, memoryview):
        return bytes(data)
    if throw_on_unsupported:
        raise ValidationError(platform, "Unsupported file data type")
    return None


def to_buffer_sync(
    data: bytes | bytearray | memoryview | object, platform: str, *, throw_on_unsupported: bool = True
) -> bytes | None:
    """Synchronous version of to_buffer."""
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    if isinstance(data, memoryview):
        return bytes(data)
    if throw_on_unsupported:
        raise ValidationError(platform, "Unsupported file data type")
    return None


def buffer_to_data_uri(data: bytes, mime_type: str = "application/octet-stream") -> str:
    """Convert bytes to a data URI string."""
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime_type};base64,{b64}"
