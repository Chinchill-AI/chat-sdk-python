"""Normalize async iterable streams for use with ``thread.post()``.

Python port of from-full-stream.ts.

Handles three stream types automatically:

- **Text streams** (``AsyncIterable[str]``) -- passed through as-is.
- **Full streams** (``AsyncIterable[object]``) -- extracts ``text-delta``
  events and injects ``"\\n\\n"`` separators between steps so that
  multi-step agent output reads naturally.
- **StreamChunk objects** (``task_update``, ``plan_update``,
  ``markdown_text``) -- passed through as-is for adapters with native
  structured chunk support.
"""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator

from chat_sdk.types import StreamChunk

_STREAM_CHUNK_TYPES = frozenset({"markdown_text", "task_update", "plan_update"})


async def from_full_stream(
    stream: AsyncIterable[object],
) -> AsyncIterator[str | StreamChunk]:
    """Normalize an async iterable stream for use with ``thread.post()``.

    Yields either plain ``str`` chunks or ``StreamChunk`` objects.
    """
    needs_separator = False
    has_emitted_text = False

    async for event in stream:
        # Plain string chunk (e.g. from AI SDK textStream)
        if isinstance(event, str):
            yield event
            continue

        # Skip non-dicts / non-objects
        if event is None or not isinstance(event, dict):
            continue

        event_type = event.get("type")
        if event_type is None:
            continue

        # Pass through StreamChunk objects
        if event_type in _STREAM_CHUNK_TYPES:
            yield event  # type: ignore[misc]
            continue

        # AI SDK v5 uses textDelta, v6 uses text
        text_delta = event.get("textDelta") if event.get("textDelta") is not None else event.get("text_delta")
        text_content = (
            text_delta if text_delta is not None else (event.get("text") if event.get("text") is not None else event.get("delta"))
        )

        if event_type == "text-delta" and isinstance(text_content, str):
            if needs_separator and has_emitted_text:
                yield "\n\n"
            needs_separator = False
            has_emitted_text = True
            yield text_content
        elif event_type == "finish-step":
            needs_separator = True
