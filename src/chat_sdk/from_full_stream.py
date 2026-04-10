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

        if event is None:
            continue

        # Support both dict and object-style events
        if isinstance(event, dict):
            event_type = event.get("type", "")
        elif hasattr(event, "type"):
            event_type = getattr(event, "type", "")
        else:
            continue

        if not event_type:
            continue

        # Pass through StreamChunk objects
        if event_type in _STREAM_CHUNK_TYPES:
            yield event  # type: ignore[misc]
            continue

        # AI SDK v6 uses "text", v5 uses "textDelta"; also accept "delta"
        # Priority: text > delta > textDelta > text_delta (matches TS)
        if isinstance(event, dict):
            text_content = next(
                (v for k in ("text", "delta", "textDelta", "text_delta") if (v := event.get(k)) is not None),
                None,
            )
        else:
            text_content = next(
                (v for k in ("text", "delta", "textDelta", "text_delta") if (v := getattr(event, k, None)) is not None),
                None,
            )

        if event_type == "text-delta" and isinstance(text_content, str):
            if needs_separator and has_emitted_text:
                yield "\n\n"
            needs_separator = False
            has_emitted_text = True
            yield text_content
        elif event_type == "finish-step":
            needs_separator = True
